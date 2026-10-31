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

The code has no ``event_type``/time-window bound on these joins (item 11
asks for one: every claim, effect, outbox row and handoff is read); that is
an owner decision still pending, not exercised here.

Not buildable without production changes (reported, not faked): "decision
without effect" for RAG/media/egress/maintenance (an allocation cannot name
a missing effect: ``fk_credit_allocations_effect``); the item-11 joins to
``events`` (``srm.decision`` / ``handoff.request``) -- the code reconciles
claims and ``handoff_records`` instead.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from src.models.credit import CreditEffect
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.services import credit_rollout
from src.services.credit_rollout import (
    CreditRolloutService,
    OrphanReport,
    run_credit_orphan_reconciliation_once,
)
from tests.integration.credit_rollout_world import (
    clean,
    execute,
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
        await CreditRolloutService(session).arm_enforcement(set_by="owner-decision")
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
