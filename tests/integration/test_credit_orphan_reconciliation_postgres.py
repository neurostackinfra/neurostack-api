"""Orphan reconciliation of credit effects on real PostgreSQL (item 11).

``CreditRolloutService.orphan_report`` per anomaly kind and effect type the
code supports, each on a fresh database:

* decision without effect -- an enforced ingress claim (``POST /v1/events``
  or a provider webhook) whose trace effect was never written;
* effect without decision -- a trace effect with no claim; a RAG, media,
  egress or maintenance effect with no allocation naming it;
* partial debit without debt -- traces only: debit + debt != 1000 mc;
* orphan outbox -- a response-outbox row whose effect does not exist;
* handoff without effect -- a handoff (synchronous or the Meta file
  handoff: both are ``handoff_records`` rows) naming a missing effect;
* zero effects (``trace_credit_pending``, ``trace_no_response``) are closed;
* a deleted tenant's effect is closed by one idempotent ``deletion_closure``
  that copies the decision digest;
* after enforcement the periodic pass detects an injected orphan and keeps
  ``has_alert`` true until it closes.

* the worker's every-tick pass is window-bounded (``ORPHAN_PERIODIC_WINDOW``)
  with a statement count independent of the number of rows, serialized
  across processes by an advisory lock, and joined every
  ``ORPHAN_FULL_SWEEP_SECONDS`` by a full sweep whose count keeps the gauge
  up for an orphan older than the window; ``arm`` and the admin reads scan
  the full history; deletion closures are age-independent and batched.

There is no ``event_type`` bound: the code reconciles claims and
``handoff_records``, not ``events`` (see below).

Not buildable without production changes (reported, not faked): "decision
without effect" for RAG/media/egress/maintenance (an allocation cannot name
a missing effect: ``fk_credit_allocations_effect``); the item-11 joins to
``events`` (``srm.decision`` / ``handoff.request``) -- the code reconciles
claims and ``handoff_records`` instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select, text

from src.models.credit import CreditEffect
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.services import credit_rollout
from src.services.credit_rollout import (
    ORPHAN_CLOSURE_BATCH,
    ORPHAN_FULL_SWEEP_SECONDS,
    ORPHAN_PASS_SKIPPED,
    ORPHAN_PERIODIC_WINDOW,
    CreditEnforcementRejected,
    CreditRolloutService,
    OrphanReport,
    OrphanSweepState,
    credit_orphan_pass_advisory_key,
    run_credit_orphan_reconciliation_once,
)
from tests.integration.credit_rollout_world import (
    clean,
    execute,
    make_tenant,
    scalar,
    subscribed_builder,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness

pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

DIGEST = "c" * 64


def _orphan_gauge() -> float:
    """``neurostack_credit_orphan_effects`` as a scrape would read it."""

    (value,) = [
        sample.value
        for metric in credit_rollout.CREDIT_ORPHAN_EFFECTS.collect()
        for sample in metric.samples
    ]
    return value


async def _report(harness: _Harness) -> OrphanReport:
    async with harness.sessions() as session:
        report = await CreditRolloutService(session).orphan_report()
        await session.rollback()
    return report


async def _claim(
    harness: _Harness, tenant_id: UUID, builder_id: UUID, *, source: str = "events"
) -> str:
    """An enforced claim; returns the trace effect id the code expects."""

    interaction = uuid4()
    await execute(
        harness,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, idempotency_key,
            provider_message_id, interaction_id, payload_sha256, decision_version,
            mode
        ) VALUES (:id, :tenant, :builder, :source, :principal, :key, :message,
                  :interaction, :sha, 1, 'enforced')
        """,
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        source=source,
        principal=f"{source}:{tenant_id}",
        key="key-1" if source == "events" else None,
        message=None if source == "events" else f"msg-{interaction.hex}",
        interaction=interaction,
        sha="d" * 64,
    )
    return f"trace:{tenant_id}:{interaction}:decision:1"


async def _effect(
    harness: _Harness,
    tenant_id: UUID | None,
    builder_id: UUID,
    effect_id: str,
    kind: str,
) -> None:
    await execute(
        harness,
        "INSERT INTO credit_effects (id, builder_id, tenant_id, effect_id, "
        "effect_kind, decision_digest) VALUES (:id, :b, :t, :e, :k, :d)",
        id=uuid4(),
        b=builder_id,
        t=tenant_id,
        e=effect_id,
        k=kind,
        d=DIGEST,
    )


async def _account(harness: _Harness, builder_id: UUID) -> UUID:
    return UUID(
        str(
            await scalar(
                harness,
                "SELECT id FROM credit_accounts WHERE builder_id = :b",
                b=builder_id,
            )
        )
    )


async def _debit(
    harness: _Harness, builder_id: UUID, lot_id: UUID, effect_id: str, amount: int
) -> None:
    await execute(
        harness,
        """
        INSERT INTO credit_ledger_entries (
            id, builder_id, account_id, effect_id, lot_id, entry_type, amount_mc,
            occurred_at
        ) VALUES (:id, :b, :a, :e, :lot, 'debit', :amount, now())
        """,
        id=uuid4(),
        b=builder_id,
        a=await _account(harness, builder_id),
        e=effect_id,
        lot=lot_id,
        amount=amount,
    )


async def _debt(
    harness: _Harness, builder_id: UUID, effect_id: str, amount: int
) -> None:
    await execute(
        harness,
        "INSERT INTO credit_debts (id, builder_id, account_id, debt_source_kind, "
        "debt_source_effect_id, amount_mc) VALUES (:id, :b, :a, 'consumption', :e, "
        ":amount)",
        id=uuid4(),
        b=builder_id,
        a=await _account(harness, builder_id),
        e=effect_id,
        amount=amount,
    )


async def _allocation(
    harness: _Harness, builder_id: UUID, effect_id: str, category: str, kind: str
) -> None:
    await execute(
        harness,
        """
        INSERT INTO credit_allocations (
            id, builder_id, account_id, window_start, window_end, category,
            allocation_kind, source_key, units, target_mc, charged_mc, effect_id
        ) VALUES (:id, :b, :a, :start, :end, :category, :kind, :source, 1, 1000,
                  1000, :e)
        """,
        id=uuid4(),
        b=builder_id,
        a=await _account(harness, builder_id),
        start=date(2026, 11, 1),
        end=date(2026, 12, 1),
        category=category,
        kind=kind,
        source=f"source:{uuid4()}",
        e=effect_id,
    )


async def _handoff(harness: _Harness, tenant_id: UUID, effect_id: str) -> None:
    session_id = uuid4()
    await execute(
        harness,
        "INSERT INTO sessions (id, tenant_id, channel, is_active, message_count) "
        "VALUES (:id, :t, 'whatsapp', true, 0)",
        id=session_id,
        t=tenant_id,
    )
    await execute(
        harness,
        """
        INSERT INTO handoff_records (
            id, tenant_id, session_id, reason, context, urgency, status,
            webhook_sent
        ) VALUES (:id, :t, :s, 'probe', CAST(:context AS JSONB), 'normal',
                  'pending', false)
        """,
        id=uuid4(),
        t=tenant_id,
        s=session_id,
        context=f'{{"credit_effect_id": "{effect_id}"}}',
    )


# ---------------------------------------------------------------------------
# Traces: POST /v1/events and webhook ingress.
# ---------------------------------------------------------------------------


async def test_a_reconciled_world_has_no_orphan(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    effect_id = await _claim(clean, world.tenant_id, world.builder_id)
    await _effect(clean, world.tenant_id, world.builder_id, effect_id, "trace")
    await _debit(clean, world.builder_id, world.grant.lot_id, effect_id, 1000)
    assert (await _report(clean)).open_count == 0


@pytest.mark.parametrize("source", ["events", "zapi", "meta", "uazapi", "evolution"])
async def test_a_decision_without_effect_is_open(clean: _Harness, source: str) -> None:
    world = await subscribed_builder(clean)
    await _claim(clean, world.tenant_id, world.builder_id, source=source)
    report = await _report(clean)
    assert report.decision_without_effect == 1 and report.open_count == 1


@pytest.mark.parametrize("kind", ["trace", "trace_credit_pending", "trace_no_response"])
async def test_a_trace_effect_without_decision_is_open(
    clean: _Harness, kind: str
) -> None:
    world = await subscribed_builder(clean)
    effect_id = f"trace:{world.tenant_id}:{uuid4()}:decision:1"
    await _effect(clean, world.tenant_id, world.builder_id, effect_id, kind)
    if kind == "trace":
        await _debit(clean, world.builder_id, world.grant.lot_id, effect_id, 1000)
    report = await _report(clean)
    assert report.effect_without_decision == 1 and report.open_count == 1


@pytest.mark.parametrize(
    ("debit", "debt", "partial"),
    [
        (1000, 0, 0),
        (400, 600, 0),
        (0, 1000, 0),
        (400, 0, 1),
        (0, 0, 1),
        (1000, 1000, 1),
    ],
)
async def test_the_partial_debit_check_is_debit_plus_debt_equals_one_trace(
    clean: _Harness, debit: int, debt: int, partial: int
) -> None:
    world = await subscribed_builder(clean)
    effect_id = await _claim(clean, world.tenant_id, world.builder_id)
    await _effect(clean, world.tenant_id, world.builder_id, effect_id, "trace")
    if debit:
        await _debit(clean, world.builder_id, world.grant.lot_id, effect_id, debit)
    if debt:
        await _debt(clean, world.builder_id, effect_id, debt)
    report = await _report(clean)
    assert report.trace_partial_debit == partial
    assert report.open_count == partial


@pytest.mark.parametrize("kind", ["trace_credit_pending", "trace_no_response"])
async def test_zero_effects_are_closed(clean: _Harness, kind: str) -> None:
    world = await subscribed_builder(clean)
    effect_id = await _claim(clean, world.tenant_id, world.builder_id)
    await _effect(clean, world.tenant_id, world.builder_id, effect_id, kind)
    assert (await _report(clean)).open_count == 0


async def test_an_orphan_response_outbox_is_open(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    await execute(
        clean,
        "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload) "
        "VALUES (:id, :t, :e, '{}'::jsonb)",
        id=uuid4(),
        t=world.tenant_id,
        e=f"trace:{world.tenant_id}:{uuid4()}:decision:1",
    )
    report = await _report(clean)
    assert report.orphan_outbox == 1 and report.open_count == 1


async def test_a_response_outbox_of_an_existing_effect_is_closed(
    clean: _Harness,
) -> None:
    world = await subscribed_builder(clean)
    effect_id = await _claim(clean, world.tenant_id, world.builder_id)
    await _effect(
        clean, world.tenant_id, world.builder_id, effect_id, "trace_no_response"
    )
    await execute(
        clean,
        "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload) "
        "VALUES (:id, :t, :e, '{}'::jsonb)",
        id=uuid4(),
        t=world.tenant_id,
        e=effect_id,
    )
    assert (await _report(clean)).open_count == 0


# ---------------------------------------------------------------------------
# Handoff (synchronous and Meta file handoff).
# ---------------------------------------------------------------------------


async def test_a_handoff_naming_a_missing_effect_is_open(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    await _handoff(
        clean, world.tenant_id, f"trace:{world.tenant_id}:{uuid4()}:decision:1"
    )
    report = await _report(clean)
    assert report.handoff_without_effect == 1 and report.open_count == 1


async def test_a_handoff_with_its_effect_is_closed(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    effect_id = await _claim(clean, world.tenant_id, world.builder_id)
    await _effect(clean, world.tenant_id, world.builder_id, effect_id, "trace")
    await _debit(clean, world.builder_id, world.grant.lot_id, effect_id, 1000)
    await _handoff(clean, world.tenant_id, effect_id)
    assert (await _report(clean)).open_count == 0


async def test_a_handoff_naming_another_tenants_effect_is_open(
    clean: _Harness,
) -> None:
    """The join is tenant-scoped: an effect of tenant B does not close a
    handoff of tenant A that names its id."""

    first = await subscribed_builder(clean)
    second = await subscribed_builder(clean)
    effect_id = await _claim(clean, second.tenant_id, second.builder_id)
    await _effect(clean, second.tenant_id, second.builder_id, effect_id, "trace")
    await _debit(clean, second.builder_id, second.grant.lot_id, effect_id, 1000)
    await _handoff(clean, first.tenant_id, effect_id)
    report = await _report(clean)
    assert report.handoff_without_effect == 1 and report.open_count == 1


# ---------------------------------------------------------------------------
# RAG, media, egress and maintenance: effect <-> allocation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "category", "allocation_kind"),
    [
        ("rag", "rag", "reservation"),
        ("media", "media", "reservation"),
        ("egress", "egress", "egress"),
        ("maintenance", "rag", "maintenance"),
    ],
)
async def test_an_inventory_effect_needs_its_allocation(
    clean: _Harness, kind: str, category: str, allocation_kind: str
) -> None:
    world = await subscribed_builder(clean)
    effect_id = f"{kind}:{world.tenant_id}:{uuid4()}"
    await _effect(clean, world.tenant_id, world.builder_id, effect_id, kind)
    report = await _report(clean)
    assert report.effect_without_decision == 1 and report.open_count == 1
    await _allocation(clean, world.builder_id, effect_id, category, allocation_kind)
    assert (await _report(clean)).open_count == 0


async def test_a_partial_inventory_debit_is_outside_the_partial_check(
    clean: _Harness,
) -> None:
    """LOW-10: RAG/media/maintenance have no per-effect expected value yet."""

    world = await subscribed_builder(clean)
    effect_id = f"rag:{world.tenant_id}:{uuid4()}"
    await _effect(clean, world.tenant_id, world.builder_id, effect_id, "rag")
    await _allocation(clean, world.builder_id, effect_id, "rag", "reservation")
    await _debit(clean, world.builder_id, world.grant.lot_id, effect_id, 300)
    assert (await _report(clean)).trace_partial_debit == 0


# ---------------------------------------------------------------------------
# Deleted tenant: one idempotent deletion closure.
# ---------------------------------------------------------------------------


async def test_deleted_tenant_effect_gets_an_idempotent_digest_closure(
    clean: _Harness,
) -> None:
    world = await subscribed_builder(clean)
    effect_id = f"trace:{world.tenant_id}:{uuid4()}:decision:2"
    async with clean.sessions() as session:
        repository = CreditRepository(session)
        assert await repository.insert_effect(
            builder_id=world.builder_id,
            tenant_id=world.tenant_id,
            effect_id=effect_id,
            effect_kind="trace_no_response",
            decision_digest=DIGEST,
        )
        await session.execute(delete(Tenant).where(Tenant.id == world.tenant_id))
        before = await CreditRolloutService(session).orphan_report()
        first = await CreditRolloutService(session).reconcile_orphans()
        second = await CreditRolloutService(session).reconcile_orphans()
        closures = list(
            (
                await session.execute(
                    select(CreditEffect).where(
                        CreditEffect.effect_kind == "deletion_closure"
                    )
                )
            ).scalars()
        )
        await session.commit()

    assert before.effect_without_decision == 1
    assert first.open_count == 0 and second.open_count == 0
    assert len(closures) == 1
    assert closures[0].decision_digest == DIGEST
    assert closures[0].tenant_id is None
    assert closures[0].builder_id == world.builder_id


# ---------------------------------------------------------------------------
# After enforcement: the periodic pass.
# ---------------------------------------------------------------------------


async def test_the_periodic_pass_keeps_has_alert_until_the_orphan_closes(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await subscribed_builder(clean)
    async with clean.sessions() as session:
        rollout = CreditRolloutService(session)
        # This proof must arm: confirm the report as the task does, at the
        # database clock.
        reviewed = await rollout.cutoff_report(
            now=await CreditRepository(session).database_now()
        )
        await rollout.arm_enforcement(
            set_by="owner-decision", expected_report_digest=reviewed.digest
        )
        await session.commit()
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)

    credit_rollout.CREDIT_ORPHAN_EFFECTS.set(-1)  # a sentinel no pass writes
    assert await run_credit_orphan_reconciliation_once(clean.sessions) == 0
    assert _orphan_gauge() == 0
    effect_id = await _claim(clean, world.tenant_id, world.builder_id)
    assert await run_credit_orphan_reconciliation_once(clean.sessions) == 1
    assert _orphan_gauge() == 1
    async with clean.sessions() as session:
        assert (await CreditRolloutService(session).capability_ready()).has_alert
        await session.rollback()

    await _effect(
        clean, world.tenant_id, world.builder_id, effect_id, "trace_no_response"
    )
    assert await run_credit_orphan_reconciliation_once(clean.sessions) == 0
    assert _orphan_gauge() == 0
    async with clean.sessions() as session:
        assert not (await CreditRolloutService(session).capability_ready()).has_alert
        await session.rollback()


async def test_before_enforcement_the_periodic_pass_does_nothing(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await subscribed_builder(clean)
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    await _claim(clean, world.tenant_id, world.builder_id)
    credit_rollout.CREDIT_ORPHAN_EFFECTS.set(-1)
    assert await run_credit_orphan_reconciliation_once(clean.sessions) == 0
    assert _orphan_gauge() == -1  # dormant: not even the gauge is written
    # The orphan is real; only the pass is dormant before enforcement.
    assert (await _report(clean)).decision_without_effect == 1


# ---------------------------------------------------------------------------
# Bounded periodic pass (HIGH-1): window, statement count, advisory lock.
# ---------------------------------------------------------------------------


@contextmanager
def _statements(harness: _Harness) -> Iterator[list[str]]:
    """Every SQL statement the harness engine sends while open."""

    seen: list[str] = []

    def record(*args: object) -> None:
        seen.append(str(args[2]))

    event.listen(harness.engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(harness.engine.sync_engine, "before_cursor_execute", record)


async def _window_start(harness: _Harness) -> datetime:
    async with harness.sessions() as session:
        since = await CreditRolloutService(session).periodic_orphan_since()
        await session.rollback()
    return since


async def _windowed(harness: _Harness) -> OrphanReport:
    since = await _window_start(harness)
    async with harness.sessions() as session:
        report = await CreditRolloutService(session).orphan_report(since=since)
        await session.rollback()
    return report


async def _reconciled_traces(harness: _Harness, world: object, n: int) -> None:
    """*n* complete decisions (claim, trace effect, 1000 mc debit), plus a
    response-outbox row and a handoff naming each effect -- set-based."""

    tenant, builder = world.tenant_id, world.builder_id  # type: ignore[attr-defined]
    account = await _account(harness, builder)
    lot = world.grant.lot_id  # type: ignore[attr-defined]
    params = {
        "t": tenant,
        "b": builder,
        "n": n,
        "a": account,
        "lot": lot,
        "p": f"events:{tenant}",
    }
    for sql in (
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, idempotency_key,
            provider_message_id, interaction_id, payload_sha256, decision_version,
            mode
        ) SELECT gen_random_uuid(), :t, :b, 'events', :p, 'k-' || g || '-' || :n,
                 NULL, gen_random_uuid(), repeat('d', 64), 1, 'enforced'
          FROM generate_series(1, :n) AS g
        """,
        """
        INSERT INTO credit_effects (id, builder_id, tenant_id, effect_id,
                                    effect_kind, decision_digest)
        SELECT gen_random_uuid(), builder_id, tenant_id,
               'trace:' || tenant_id || ':' || interaction_id || ':decision:1',
               'trace', repeat('c', 64)
          FROM credit_ingress_claims c WHERE tenant_id = :t AND NOT EXISTS (
              SELECT 1 FROM credit_effects e WHERE e.builder_id = c.builder_id
                 AND e.effect_id = 'trace:' || c.tenant_id || ':'
                     || c.interaction_id || ':decision:1')
        """,
        """
        INSERT INTO credit_ledger_entries (
            id, builder_id, account_id, effect_id, lot_id, entry_type, amount_mc,
            occurred_at
        ) SELECT gen_random_uuid(), builder_id, :a, effect_id, :lot, 'debit', 1000,
                 now()
          FROM credit_effects e WHERE tenant_id = :t AND effect_kind = 'trace'
           AND NOT EXISTS (SELECT 1 FROM credit_ledger_entries l
                            WHERE l.effect_id = e.effect_id)
        """,
        """
        INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload)
        SELECT gen_random_uuid(), tenant_id, effect_id, '{}'::jsonb
          FROM credit_effects e WHERE tenant_id = :t AND effect_kind = 'trace'
           AND NOT EXISTS (SELECT 1 FROM credit_response_outbox o
                            WHERE o.effect_id = e.effect_id)
        """,
    ):
        await execute(
            harness,
            sql,
            **{k: v for k, v in params.items() if f":{k}" in sql},
        )
    session_id = uuid4()
    await execute(
        harness,
        "INSERT INTO sessions (id, tenant_id, channel, is_active, message_count) "
        "VALUES (:id, :t, 'whatsapp', true, 0)",
        id=session_id,
        t=tenant,
    )
    await execute(
        harness,
        """
        INSERT INTO handoff_records (
            id, tenant_id, session_id, reason, context, urgency, status,
            webhook_sent
        ) SELECT gen_random_uuid(), tenant_id, :s, 'probe',
                 jsonb_build_object('credit_effect_id', effect_id), 'normal',
                 'pending', false
          FROM credit_effects e WHERE tenant_id = :t AND effect_kind = 'trace'
           AND NOT EXISTS (SELECT 1 FROM handoff_records h
                            WHERE h.context->>'credit_effect_id' = e.effect_id)
        """,
        s=session_id,
        t=tenant,
    )


async def _count_statements(harness: _Harness, *, windowed: bool) -> int:
    since = await _window_start(harness) if windowed else None
    async with harness.sessions() as session:
        with _statements(harness) as seen:
            report = await CreditRolloutService(session).orphan_report(since=since)
        await session.rollback()
    assert report.open_count == 0, report
    return len(seen)


@pytest.mark.parametrize("windowed", [True, False], ids=["window", "full"])
async def test_the_orphan_pass_statement_count_does_not_grow_with_rows(
    clean: _Harness, windowed: bool
) -> None:
    """10 and 200 reconciled decisions (each with its outbox row and handoff)
    cost the same number of statements: no per-effect/outbox/handoff query."""

    world = await subscribed_builder(clean)
    await _reconciled_traces(clean, world, 10)
    small = await _count_statements(clean, windowed=windowed)
    await _reconciled_traces(clean, world, 190)
    assert (
        int(
            await scalar(
                clean,
                "SELECT count(*) FROM credit_effects WHERE effect_kind = 'trace'",
            )
        )
        == 200
    )
    large = await _count_statements(clean, windowed=windowed)
    assert small == large
    assert large <= 9


async def _old_claim(harness: _Harness, tenant_id: UUID, builder_id: UUID) -> str:
    """An enforced claim created before the periodic window."""

    interaction = uuid4()
    await execute(
        harness,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, idempotency_key,
            provider_message_id, interaction_id, payload_sha256, decision_version,
            mode, created_at
        ) VALUES (:id, :tenant, :builder, 'events', :principal, :key, NULL,
                  :interaction, :sha, 1, 'enforced',
                  now() - make_interval(secs => :age))
        """,
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        principal=f"events:{tenant_id}",
        key=f"old-{interaction.hex}",
        interaction=interaction,
        sha="d" * 64,
        age=(ORPHAN_PERIODIC_WINDOW + timedelta(hours=2)).total_seconds(),
    )
    return f"trace:{tenant_id}:{interaction}:decision:1"


@pytest.mark.parametrize(
    "kind",
    [
        "decision_without_effect",
        "trace_without_decision",
        "inventory_without_allocation",
        "trace_partial_debit",
        "orphan_outbox",
        "handoff_without_effect",
    ],
)
async def test_the_windowed_pass_finds_an_orphan_inside_the_window(
    clean: _Harness, kind: str
) -> None:
    world = await subscribed_builder(clean)
    await _reconciled_traces(clean, world, 5)  # noise that must not count
    tenant, builder = world.tenant_id, world.builder_id
    if kind == "decision_without_effect":
        await _claim(clean, tenant, builder)
    elif kind == "trace_without_decision":
        effect_id = f"trace:{tenant}:{uuid4()}:decision:1"
        await _effect(clean, tenant, builder, effect_id, "trace")
        await _debit(clean, builder, world.grant.lot_id, effect_id, 1000)
    elif kind == "inventory_without_allocation":
        await _effect(clean, tenant, builder, f"rag:{tenant}:{uuid4()}", "rag")
    elif kind == "trace_partial_debit":
        effect_id = await _claim(clean, tenant, builder)
        await _effect(clean, tenant, builder, effect_id, "trace")
        await _debit(clean, builder, world.grant.lot_id, effect_id, 400)
    elif kind == "orphan_outbox":
        await execute(
            clean,
            "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload) "
            "VALUES (:id, :t, :e, '{}'::jsonb)",
            id=uuid4(),
            t=tenant,
            e=f"trace:{tenant}:{uuid4()}:decision:1",
        )
    else:
        await _handoff(clean, tenant, f"trace:{tenant}:{uuid4()}:decision:1")
    windowed = await _windowed(clean)
    full = await _report(clean)
    assert windowed == full
    assert windowed.open_count == 1


async def test_an_effect_of_an_older_claim_is_not_an_orphan_in_the_window(
    clean: _Harness,
) -> None:
    """A takeover/replay writes the effect inside the window for a claim
    created before it: the windowed pass looks that claim up."""

    world = await subscribed_builder(clean)
    effect_id = await _old_claim(clean, world.tenant_id, world.builder_id)
    await _effect(
        clean, world.tenant_id, world.builder_id, effect_id, "trace_no_response"
    )
    assert (await _windowed(clean)).open_count == 0
    assert (await _report(clean)).open_count == 0


_OLD = "now() - make_interval(secs => :age)"


def _age() -> float:
    return (ORPHAN_PERIODIC_WINDOW + timedelta(hours=2)).total_seconds()


async def _old_effect(
    harness: _Harness, tenant_id: UUID, builder_id: UUID, effect_id: str, kind: str
) -> None:
    await execute(
        harness,
        "INSERT INTO credit_effects (id, builder_id, tenant_id, effect_id, "
        f"effect_kind, decision_digest, created_at) VALUES (:id, :b, :t, :e, :k, "
        f":d, {_OLD})",
        id=uuid4(),
        b=builder_id,
        t=tenant_id,
        e=effect_id,
        k=kind,
        d=DIGEST,
        age=_age(),
    )


@pytest.mark.parametrize(
    "kind",
    [
        "decision_without_effect",
        "trace_without_decision",
        "inventory_without_allocation",
        "trace_partial_debit",
        "orphan_outbox",
        "handoff_without_effect",
    ],
)
async def test_an_orphan_older_than_the_window_is_found_by_arm_full_scan(
    clean: _Harness, kind: str
) -> None:
    """The periodic window does not see it; ``arm`` (full history) refuses."""

    world = await subscribed_builder(clean)
    tenant, builder = world.tenant_id, world.builder_id
    if kind == "decision_without_effect":
        await _old_claim(clean, tenant, builder)
    elif kind == "trace_without_decision":
        effect_id = f"trace:{tenant}:{uuid4()}:decision:1"
        await _old_effect(clean, tenant, builder, effect_id, "trace")
        await _debit(clean, builder, world.grant.lot_id, effect_id, 1000)
    elif kind == "inventory_without_allocation":
        await _old_effect(clean, tenant, builder, f"rag:{tenant}:{uuid4()}", "rag")
    elif kind == "trace_partial_debit":
        effect_id = await _old_claim(clean, tenant, builder)
        await _old_effect(clean, tenant, builder, effect_id, "trace")
        await _debit(clean, builder, world.grant.lot_id, effect_id, 400)
    elif kind == "orphan_outbox":
        await execute(
            clean,
            "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload, "
            f"created_at) VALUES (:id, :t, :e, '{{}}'::jsonb, {_OLD})",
            id=uuid4(),
            t=tenant,
            e=f"trace:{tenant}:{uuid4()}:decision:1",
            age=_age(),
        )
    else:
        await _handoff(clean, tenant, f"trace:{tenant}:{uuid4()}:decision:1")
        await execute(
            clean,
            f"UPDATE handoff_records SET created_at = {_OLD} WHERE tenant_id = :t",
            t=tenant,
            age=_age(),
        )
    assert (await _windowed(clean)).open_count == 0
    full = await _report(clean)
    assert full.open_count == 1, full
    async with clean.sessions() as session:
        rollout = CreditRolloutService(session)
        reviewed = await rollout.cutoff_report(
            now=await CreditRepository(session).database_now()
        )
        with pytest.raises(CreditEnforcementRejected, match="orphan reconciliation"):
            await rollout.arm_enforcement(
                set_by="owner-decision", expected_report_digest=reviewed.digest
            )
        await session.rollback()


async def test_the_periodic_pass_skips_while_another_process_holds_the_lock(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await subscribed_builder(clean)
    async with clean.sessions() as session:
        rollout = CreditRolloutService(session)
        reviewed = await rollout.cutoff_report(
            now=await CreditRepository(session).database_now()
        )
        await rollout.arm_enforcement(
            set_by="owner-decision", expected_report_digest=reviewed.digest
        )
        await session.commit()
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    await _claim(clean, world.tenant_id, world.builder_id)  # one open orphan

    credit_rollout.CREDIT_ORPHAN_EFFECTS.set(-1)
    async with clean.engine.connect() as holder:
        await holder.execute(
            text("SELECT pg_advisory_lock(:key)"),
            {"key": credit_orphan_pass_advisory_key()},
        )
        try:
            # Another process is mid-pass: this one skips, never waits.
            skipped = await run_credit_orphan_reconciliation_once(clean.sessions)
            assert skipped == ORPHAN_PASS_SKIPPED  # distinct from a clean 0
            assert _orphan_gauge() == -1
        finally:
            await holder.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": credit_orphan_pass_advisory_key()},
            )
    assert await run_credit_orphan_reconciliation_once(clean.sessions) == 1
    assert _orphan_gauge() == 1


async def _arm(harness: _Harness) -> None:
    async with harness.sessions() as session:
        rollout = CreditRolloutService(session)
        reviewed = await rollout.cutoff_report(
            now=await CreditRepository(session).database_now()
        )
        await rollout.arm_enforcement(
            set_by="owner-decision", expected_report_digest=reviewed.digest
        )
        await session.commit()


async def _closures(harness: _Harness, builder_id: UUID) -> int:
    return int(
        await scalar(
            harness,
            "SELECT count(*) FROM credit_effects WHERE builder_id = :b "
            "AND effect_kind = 'deletion_closure' AND tenant_id IS NULL",
            b=builder_id,
        )
    )


async def _full_has_alert(harness: _Harness) -> bool:
    async with harness.sessions() as session:
        readiness = await CreditRolloutService(session).capability_ready()
        await session.rollback()
    return readiness.has_alert


async def test_a_deleted_tenants_old_effect_is_closed_by_the_periodic_pass(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH-R1: after ``arm``, a tenant deleted with an effect older than the
    window still gets its closure from the windowed pass (closure selection
    is age-independent), and the full ``has_alert`` falls back to False."""

    world = await subscribed_builder(clean)
    await _arm(clean)
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    second = await make_tenant(clean, world.builder_id)
    effect_id = await _old_claim(clean, second, world.builder_id)
    await _old_effect(clean, second, world.builder_id, effect_id, "trace_no_response")
    await execute(clean, "DELETE FROM tenants WHERE id = :t", t=second)
    assert await _full_has_alert(clean) is True  # the effect lost its decision

    # The full sweep is NOT due: only the windowed pass runs.
    sweep = OrphanSweepState(clock=lambda: 0.0, last_full_at=0.0)
    assert await run_credit_orphan_reconciliation_once(clean.sessions, state=sweep) == 0
    assert await _closures(clean, world.builder_id) == 1
    assert await _full_has_alert(clean) is False
    async with clean.sessions() as session:
        [(digest, closure_id)] = (
            await session.execute(
                select(CreditEffect.decision_digest, CreditEffect.effect_id).where(
                    CreditEffect.effect_kind == "deletion_closure"
                )
            )
        ).all()
        await session.rollback()
    assert digest == DIGEST
    assert closure_id == CreditRolloutService._deletion_closure_id(effect_id)


async def test_writing_deletion_closures_costs_the_same_statements_for_any_n(
    clean: _Harness,
) -> None:
    """The pass that writes 3 or 30 closures (old effects, deleted tenant)
    issues the same statements: one set-based INSERT ... SELECT."""

    counts = []
    for n in (3, 30):
        world = await subscribed_builder(clean)
        for _ in range(n):
            await _old_effect(
                clean,
                world.tenant_id,
                world.builder_id,
                f"trace:{world.tenant_id}:{uuid4()}:decision:1",
                "trace_no_response",
            )
        await execute(clean, "DELETE FROM tenants WHERE id = :t", t=world.tenant_id)
        since = await _window_start(clean)
        async with clean.sessions() as session:
            with _statements(clean) as seen:
                report = await CreditRolloutService(session).reconcile_orphans(
                    since=since, closure_limit=ORPHAN_CLOSURE_BATCH
                )
            await session.commit()
        assert report.open_count == 0
        assert await _closures(clean, world.builder_id) == n
        assert await _full_has_alert(clean) is False
        counts.append(len(seen))
    assert counts[0] == counts[1]


async def test_deletion_closures_are_batched_across_passes(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    for _ in range(5):
        await _old_effect(
            clean,
            world.tenant_id,
            world.builder_id,
            f"trace:{world.tenant_id}:{uuid4()}:decision:1",
            "trace_no_response",
        )
    await execute(clean, "DELETE FROM tenants WHERE id = :t", t=world.tenant_id)
    written = []
    for _ in range(4):
        async with clean.sessions() as session:
            await CreditRolloutService(session).reconcile_orphans(
                since=await _window_start(clean), closure_limit=2
            )
            await session.commit()
        written.append(await _closures(clean, world.builder_id))
    assert written == [2, 4, 5, 5]


async def test_an_old_orphan_keeps_the_gauge_up_until_a_full_sweep_sees_it_closed(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIUM-R2: the windowed pass alone cannot see an orphan older than the
    window; the low-cadence full sweep can, and the gauge publishes
    max(window, last full sweep), so the alert keeps firing until a later
    full sweep finds the orphan closed."""

    world = await subscribed_builder(clean)
    await _arm(clean)
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    effect_id = await _old_claim(clean, world.tenant_id, world.builder_id)
    now = [1_000.0]
    sweep = OrphanSweepState(clock=lambda: now[0])

    async def tick(advance: float) -> int:
        now[0] += advance
        return await run_credit_orphan_reconciliation_once(clean.sessions, state=sweep)

    assert await tick(0) == 1  # a fresh state sweeps at once
    assert _orphan_gauge() == 1
    assert (await _windowed(clean)).open_count == 0  # invisible to the window
    assert await tick(60) == 1  # sweep not due: the last full count holds
    assert _orphan_gauge() == 1
    assert await _full_has_alert(clean) is True  # admin readiness: full scan

    await _effect(
        clean, world.tenant_id, world.builder_id, effect_id, "trace_no_response"
    )
    assert await _full_has_alert(clean) is False
    assert await tick(60) == 1  # still the last full sweep's count
    assert await tick(ORPHAN_FULL_SWEEP_SECONDS) == 0  # the next sweep sees it
    assert _orphan_gauge() == 0


async def test_an_outbox_row_naming_another_tenants_effect_is_open(
    clean: _Harness,
) -> None:
    """The outbox anti-join is tenant-scoped: tenant B's effect does not
    close tenant A's outbox row that names its id (window and full)."""

    first = await subscribed_builder(clean)
    second = await subscribed_builder(clean)
    effect_id = await _claim(clean, second.tenant_id, second.builder_id)
    await _effect(clean, second.tenant_id, second.builder_id, effect_id, "trace")
    await _debit(clean, second.builder_id, second.grant.lot_id, effect_id, 1000)
    await execute(
        clean,
        "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload) "
        "VALUES (:id, :t, :e, '{}'::jsonb)",
        id=uuid4(),
        t=first.tenant_id,
        e=effect_id,
    )
    for report in (await _report(clean), await _windowed(clean)):
        assert report.orphan_outbox == 1 and report.open_count == 1


async def test_a_new_orphan_raises_the_gauge_between_full_sweeps(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The every-tick windowed count is part of the max: a fresh orphan is
    published at once, not only at the next full sweep."""

    world = await subscribed_builder(clean)
    await _arm(clean)
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    sweep = OrphanSweepState(clock=lambda: 0.0, last_full_at=0.0, last_full_count=0)
    effect_id = await _claim(clean, world.tenant_id, world.builder_id)
    assert await run_credit_orphan_reconciliation_once(clean.sessions, state=sweep) == 1
    assert _orphan_gauge() == 1
    await _effect(
        clean, world.tenant_id, world.builder_id, effect_id, "trace_no_response"
    )
    assert await run_credit_orphan_reconciliation_once(clean.sessions, state=sweep) == 0
    assert _orphan_gauge() == 0
    assert sweep.last_full_at == 0.0  # no full sweep ran in between


async def test_no_closure_is_written_for_a_live_tenants_effects(
    clean: _Harness,
) -> None:
    """The PostgreSQL closure INSERT ... SELECT is limited to effects whose
    tenant no longer exists."""

    world = await subscribed_builder(clean)
    for _ in range(3):
        effect_id = await _old_claim(clean, world.tenant_id, world.builder_id)
        await _old_effect(
            clean, world.tenant_id, world.builder_id, effect_id, "trace_no_response"
        )
    for since in (None, await _window_start(clean)):
        async with clean.sessions() as session:
            report = await CreditRolloutService(session).reconcile_orphans(
                since=since, closure_limit=ORPHAN_CLOSURE_BATCH
            )
            await session.commit()
        assert report.open_count == 0
    assert await _closures(clean, world.builder_id) == 0


async def test_the_worker_caps_closures_at_the_batch_per_pass(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await subscribed_builder(clean)
    await _arm(clean)
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    monkeypatch.setattr(credit_rollout, "ORPHAN_CLOSURE_BATCH", 2)
    gone = await make_tenant(clean, world.builder_id)
    for _ in range(5):
        await _old_effect(
            clean,
            gone,
            world.builder_id,
            f"trace:{gone}:{uuid4()}:decision:1",
            "trace_no_response",
        )
    await execute(clean, "DELETE FROM tenants WHERE id = :t", t=gone)
    sweep = OrphanSweepState(clock=lambda: 0.0, last_full_at=0.0)
    written = []
    for _ in range(3):
        await run_credit_orphan_reconciliation_once(clean.sessions, state=sweep)
        written.append(await _closures(clean, world.builder_id))
    assert written == [2, 4, 5]


async def test_minute_ticks_sweep_the_full_history_at_0_and_900_seconds(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await subscribed_builder(clean)
    await _arm(clean)
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    original = CreditRolloutService.orphan_report
    now = [0.0]
    swept: list[float] = []

    async def spy(self: CreditRolloutService, **kwargs: object) -> OrphanReport:
        if kwargs.get("since") is None:
            swept.append(now[0])
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(CreditRolloutService, "orphan_report", spy)
    sweep = OrphanSweepState(clock=lambda: now[0])
    for tick in range(17):  # 0 s, 60 s, ..., 960 s
        now[0] = tick * 60.0
        await run_credit_orphan_reconciliation_once(clean.sessions, state=sweep)
    assert swept == [0.0, ORPHAN_FULL_SWEEP_SECONDS]


class _Warnings:
    def __init__(self) -> None:
        self.fields: list[dict[str, object]] = []

    def warning(self, _message: str, **fields: object) -> None:
        self.fields.append(fields)


@pytest.mark.parametrize("remembered", [0, 5])
async def test_a_failing_full_sweep_never_freezes_the_gauge(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch, remembered: int
) -> None:
    """LOW-2: the full sweep fails inside the database (an aborted
    statement); the window count is still published as max(window, last
    successful full count), the failure is logged, the remembered count is
    kept and the window pass's closures still commit."""

    world = await subscribed_builder(clean)
    await _arm(clean)
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    warnings = _Warnings()
    monkeypatch.setattr(credit_rollout, "logger", warnings)
    original = CreditRolloutService.orphan_report

    async def failing(self: CreditRolloutService, **kwargs: object) -> OrphanReport:
        if kwargs.get("since") is None:
            await self.session.execute(text("SELECT 1 / 0"))  # aborts the sweep
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(CreditRolloutService, "orphan_report", failing)
    await _claim(clean, world.tenant_id, world.builder_id)  # window count 1
    gone = await make_tenant(clean, world.builder_id)
    await _old_effect(
        clean,
        gone,
        world.builder_id,
        f"trace:{gone}:{uuid4()}:decision:1",
        "trace_no_response",
    )
    await execute(clean, "DELETE FROM tenants WHERE id = :t", t=gone)
    credit_rollout.CREDIT_ORPHAN_EFFECTS.set(-1)
    sweep = OrphanSweepState(clock=lambda: 0.0, last_full_count=remembered)
    published = await run_credit_orphan_reconciliation_once(clean.sessions, state=sweep)
    assert published == max(1, remembered)
    assert _orphan_gauge() == max(1, remembered)
    assert sweep.last_full_count == remembered and sweep.last_full_at is None
    assert {"alert": "credit_orphan_worker", "error_type": "DBAPIError"} in [
        {k: f.get(k) for k in ("alert", "error_type")} for f in warnings.fields
    ]
    assert await _closures(clean, world.builder_id) == 1  # committed anyway


@pytest.mark.parametrize("remembered", [0, 5])
async def test_the_window_count_is_published_even_if_the_pass_then_fails(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch, remembered: int
) -> None:
    """The window count reaches the gauge before the full sweep and the
    commit: a pass that fails after the window (here, its commit) still
    leaves the gauge current instead of frozen at an older value."""

    from sqlalchemy.ext.asyncio import AsyncSession

    world = await subscribed_builder(clean)
    await _arm(clean)
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    await _claim(clean, world.tenant_id, world.builder_id)  # window count 1
    credit_rollout.CREDIT_ORPHAN_EFFECTS.set(-1)

    async def boom(self: AsyncSession) -> None:
        raise RuntimeError("commit lost")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", boom)
        with pytest.raises(RuntimeError, match="commit lost"):
            await run_credit_orphan_reconciliation_once(
                clean.sessions,
                state=OrphanSweepState(
                    clock=lambda: 0.0, last_full_at=0.0, last_full_count=remembered
                ),
            )
    assert _orphan_gauge() == max(1, remembered)


async def test_a_cancelled_full_sweep_is_not_swallowed(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ordinary errors of the full sweep are absorbed: a cancellation
    escapes the pass and leaves the remembered full sweep untouched."""

    import asyncio

    await subscribed_builder(clean)
    await _arm(clean)
    monkeypatch.setattr(credit_rollout, "credit_switch_effective", lambda: True)
    original = CreditRolloutService.orphan_report

    async def cancelled(self: CreditRolloutService, **kwargs: object) -> OrphanReport:
        if kwargs.get("since") is None:
            raise asyncio.CancelledError
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(CreditRolloutService, "orphan_report", cancelled)
    sweep = OrphanSweepState(clock=lambda: 0.0, last_full_count=3)
    with pytest.raises(asyncio.CancelledError):
        await run_credit_orphan_reconciliation_once(clean.sessions, state=sweep)
    assert sweep.last_full_at is None
    assert sweep.last_full_count == 3
