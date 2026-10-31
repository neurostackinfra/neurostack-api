"""The Group 7 enforcement transaction on real PostgreSQL, and its fence.

``CreditRolloutService.arm_enforcement`` (item 7) is the only writer of
``credit_enforcement_at``.  Proven here, each on a fresh migrated database:

* it refuses with an open orphan, with readiness false, with an unaccepted
  tenant cutoff and with an ambiguous open ASAAS subscription -- every
  non-terminal status counts (``active``, ``active_linked``,
  ``readback_required``, ``non_billable_pending_readback``), and a
  ``readback_required``/``non_billable_pending_readback`` subscription with
  a current cycle, or two candidate cycles, block;
* on success it writes ``credit_enforcement_at`` exactly once; a second call
  is refused and the boundary never moves; ``at`` may be future-dated
  (published before activation) but never earlier than the database clock;
* a concurrent subscription change serializes under ``lock_rollout_control``:
  committed before the gate it is seen by the reread, after the gate it waits
  for the gate's commit and is then captured by the by-id reread;
* ``accept_cutoff`` is bound to the tenant AND the report digest;
* the cutoff report names each tenant by its CURRENT ``tenants.builder_id``
  (a tenant detached to standalone inside the window is standalone);
* by AST over ``src`` and ``scripts``: ``arm_enforcement``,
  ``record_capability_readiness``, ``accept_cutoff`` and ``PurgeExecutor``
  have no reference outside their defining modules; no route arms.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from src.config.settings import settings
from src.models.credit import CreditEnforcementState
from src.repositories.credit import CreditRepository
from src.services.credit_ledger import start_of_civil_day
from src.services.credit_metering import EventsEnforcement, precheck_events
from src.services.credit_principal import (
    TenantBuilder,
    credit_identity,
    derive_credit_principal,
)
from src.services.credit_rollout import (
    CreditEnforcementRejected,
    CreditRolloutService,
    CutoffReport,
)
from tests.integration.credit_rollout_world import (
    PLANT,
    civil_today,
    clean,
    execute,
    make_builder,
    make_cycle,
    make_monthly_grant,
    make_subscription,
    make_tenant,
    make_topup,
    message_in,
    observation,
    scalar,
    set_subscription_status,
    subscribed_builder,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

NOW = datetime.now(timezone.utc)
TODAY = civil_today(NOW)
OWNER = uuid4()


async def _arm(harness: _Harness, *, at: datetime | None = None) -> datetime:
    async with harness.sessions() as session:
        try:
            state = await CreditRolloutService(session).arm_enforcement(
                set_by="owner-decision", at=at
            )
            armed = state.credit_enforcement_at
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
    assert armed is not None
    return armed


async def _refused(harness: _Harness, match: str) -> None:
    with pytest.raises(CreditEnforcementRejected, match=match):
        await _arm(harness)
    assert await _state_rows(harness) == (0, None)


async def _state_rows(harness: _Harness) -> tuple[int, datetime | None]:
    count = await scalar(harness, "SELECT count(*) FROM credit_enforcement_state")
    at = await scalar(
        harness, "SELECT max(credit_enforcement_at) FROM credit_enforcement_state"
    )
    return int(count), at


async def _cutoff(harness: _Harness) -> CutoffReport:
    async with harness.sessions() as session:
        report = await CreditRolloutService(session).cutoff_report(now=NOW)
        await session.rollback()
    return report


async def _accept(
    harness: _Harness,
    *,
    tenant_id: UUID,
    builder_id: UUID | None,
    reason: str,
    digest: str,
) -> None:
    async with harness.sessions() as session:
        await CreditRolloutService(session).accept_cutoff(
            tenant_id=tenant_id,
            builder_id=builder_id,
            reason=reason,
            report_digest=digest,
            author_user_id=OWNER,
            accepted_at=NOW,
        )
        await session.commit()


async def _two_bound_candidates(harness: _Harness) -> None:
    """A readback-only gate failure today: two candidate current cycles of
    one subscription, both exactly bound (no reconciliation lag)."""

    world = await subscribed_builder(harness)
    start, end = TODAY - timedelta(days=1), TODAY + timedelta(days=29)
    second = await make_cycle(
        harness, world.builder_id, world.subscription_id, start=start, end=end
    )
    await make_monthly_grant(
        harness,
        builder_id=world.builder_id,
        subscription_id=world.subscription_id,
        cycle_id=second,
        start=start,
        end=end,
    )


# ---------------------------------------------------------------------------
# Success, once, never backdated.
# ---------------------------------------------------------------------------


async def test_a_clean_world_arms_exactly_once(clean: _Harness) -> None:
    await subscribed_builder(clean)
    async with clean.sessions() as session:
        before = await CreditRepository(session).database_now()
        await session.rollback()
    armed = await _arm(clean)
    async with clean.sessions() as session:
        after = await CreditRepository(session).database_now()
        stored = await session.scalar(select(CreditEnforcementState))
        assert stored is not None
        set_by = stored.set_by
        await session.rollback()
    # ``at`` omitted is the database clock of the transaction.
    assert before <= armed <= after
    assert set_by == "owner-decision"
    assert await _state_rows(clean) == (1, armed)

    with pytest.raises(CreditEnforcementRejected, match="already armed"):
        await _arm(clean)
    assert await _state_rows(clean) == (1, armed)


@pytest.mark.parametrize(
    "ahead", [timedelta(seconds=30), timedelta(hours=6), timedelta(days=60)]
)
async def test_a_future_timestamp_is_refused_and_writes_nothing(
    clean: _Harness, ahead: timedelta
) -> None:
    """Every hot-path reader treats a non-NULL boundary as enforced from the
    commit: a future boundary would not delay enforcement, it would only
    make earlier events non-observational."""

    await subscribed_builder(clean)
    with pytest.raises(CreditEnforcementRejected, match="cannot be future-dated"):
        await _arm(clean, at=datetime.now(timezone.utc) + ahead)
    assert await _state_rows(clean) == (0, None)


async def test_a_far_future_timestamp_does_not_pass_vacuously(
    clean: _Harness,
) -> None:
    """Today this world must be refused (two exactly bound candidate cycles
    -- a readback-only failure -- and unaccepted standalone traffic).  Evaluated 120 days ahead there would be
    no current cycle and no traffic in the window: the boundary must not be
    accepted that way."""

    await _two_bound_candidates(clean)
    standalone = await make_tenant(clean, None)
    await message_in(clean, standalone, NOW - timedelta(hours=1))
    with pytest.raises(CreditEnforcementRejected):
        await _arm(clean, at=NOW + timedelta(days=120))
    with pytest.raises(CreditEnforcementRejected, match="readiness is incomplete"):
        await _arm(clean)
    assert await _state_rows(clean) == (0, None)


async def test_the_gate_is_evaluated_at_the_database_clock(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clock seam is ``CreditRepository.database_now``: with it moved 40
    days ahead today's current cycle is no longer current (this world then
    arms) and the boundary is exactly that instant; an explicit ``at`` equal
    to it is the same boundary, one before it is refused."""

    await _two_bound_candidates(clean)
    with pytest.raises(CreditEnforcementRejected, match="readiness is incomplete"):
        await _arm(clean)

    shifted = NOW + timedelta(days=40)

    async def database_now(self: CreditRepository) -> datetime:
        return shifted

    monkeypatch.setattr(CreditRepository, "database_now", database_now)
    with pytest.raises(CreditEnforcementRejected, match="cannot be backdated"):
        await _arm(clean, at=NOW)
    assert await _arm(clean, at=shifted) == shifted
    assert await _state_rows(clean) == (1, shifted)


@pytest.mark.parametrize(
    "backdate", [timedelta(seconds=30), timedelta(days=1), timedelta(days=60)]
)
async def test_a_past_timestamp_is_refused_and_writes_nothing(
    clean: _Harness, backdate: timedelta
) -> None:
    await subscribed_builder(clean)
    with pytest.raises(CreditEnforcementRejected, match="cannot be backdated"):
        await _arm(clean, at=datetime.now(timezone.utc) - backdate)
    assert await _state_rows(clean) == (0, None)


# ---------------------------------------------------------------------------
# Refusals.
# ---------------------------------------------------------------------------


async def test_an_open_orphan_refuses(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    # An enforced decision whose trace effect was never written.
    await execute(
        clean,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, idempotency_key,
            interaction_id, payload_sha256, decision_version, mode
        ) VALUES (:id, :tenant, :builder, 'events', 'api_key:probe', 'k1',
                  :interaction, :sha, 1, 'enforced')
        """,
        id=uuid4(),
        tenant=world.tenant_id,
        builder=world.builder_id,
        interaction=uuid4(),
        sha="b" * 64,
    )
    await _refused(clean, "orphan reconciliation is incomplete")


async def test_readiness_false_refuses(clean: _Harness) -> None:
    await subscribed_builder(clean)
    await execute(
        clean,
        "INSERT INTO credit_revision_gaps (aggregate_kind, aggregate_id, "
        "first_missing_revision) VALUES ('topup_order', :id, 2)",
        id=uuid4(),
    )
    await _refused(clean, "readiness is incomplete")


async def test_consumer_lag_refuses_even_though_requests_are_served(
    clean: _Harness,
) -> None:
    await subscribed_builder(clean, revision=2, binding_revision=1)
    await _refused(clean, "readiness is incomplete")


@pytest.mark.parametrize(
    "status", ["readback_required", "non_billable_pending_readback"]
)
async def test_an_ambiguous_open_subscription_with_a_current_cycle_refuses(
    clean: _Harness, status: str
) -> None:
    """Even with an exactly matching binding: the row awaits a readback."""

    world = await subscribed_builder(clean)
    await set_subscription_status(clean, world.subscription_id, status)
    async with clean.sessions() as session:
        readback = await CreditRolloutService(session).readback_report(now=NOW)
        await session.rollback()
    assert readback.ambiguous_current_cycles == 1
    await _refused(clean, "readiness is incomplete")


@pytest.mark.parametrize(
    "status",
    ["active", "active_linked", "readback_required", "non_billable_pending_readback"],
)
async def test_every_open_status_with_an_unbound_current_cycle_refuses(
    clean: _Harness, status: str
) -> None:
    builder_id = await make_builder(clean)
    subscription_id = await make_subscription(clean, builder_id, status=status)
    await make_cycle(
        clean,
        builder_id,
        subscription_id,
        start=TODAY - timedelta(days=3),
        end=TODAY + timedelta(days=27),
    )
    async with clean.sessions() as session:
        readback = await CreditRolloutService(session).readback_report(now=NOW)
        await session.rollback()
    # The subscription is inside the gate (counted by the readback itself,
    # not only by the reconciliation's binding_lag).
    assert not readback.complete
    assert readback.missing_or_stale_bindings + readback.ambiguous_current_cycles == 1
    await _refused(clean, "readiness is incomplete")


@pytest.mark.parametrize("status", ["inactive", "cancelled"])
async def test_a_terminal_subscription_is_outside_the_readback(
    clean: _Harness, status: str
) -> None:
    builder_id = await make_builder(clean)
    subscription_id = await make_subscription(clean, builder_id, status=status)
    await make_cycle(
        clean,
        builder_id,
        subscription_id,
        start=TODAY - timedelta(days=3),
        end=TODAY + timedelta(days=27),
    )
    async with clean.sessions() as session:
        readback = await CreditRolloutService(session).readback_report(now=NOW)
        await session.rollback()
    assert readback.complete


async def test_two_candidate_cycles_refuse(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    await make_cycle(
        clean,
        world.builder_id,
        world.subscription_id,
        start=TODAY - timedelta(days=1),
        end=TODAY + timedelta(days=29),
    )
    await _refused(clean, "readiness is incomplete")


async def test_an_unaccepted_cutoff_refuses(clean: _Harness) -> None:
    await subscribed_builder(clean)
    standalone = await make_tenant(clean, None)
    await message_in(clean, standalone, NOW - timedelta(hours=1))
    await observation(clean, standalone, None, NOW - timedelta(hours=1))
    await _refused(clean, "cutoff report is unaccepted")


# ---------------------------------------------------------------------------
# accept_cutoff: bound to the tenant and to the report digest.
# ---------------------------------------------------------------------------


async def test_acceptance_counts_only_for_its_tenant_and_digest(
    clean: _Harness,
) -> None:
    world = await subscribed_builder(clean)
    standalone = await make_tenant(clean, None)
    await message_in(clean, standalone, NOW - timedelta(hours=1))
    await observation(clean, standalone, None, NOW - timedelta(hours=1))
    report = await _cutoff(clean)
    assert [(f.tenant_id, f.builder_id, f.reason) for f in report.findings] == [
        (standalone, None, "standalone")
    ]

    # Another tenant (with the same reason and digest) does not count.
    await _accept(
        clean,
        tenant_id=world.tenant_id,
        builder_id=None,
        reason="standalone",
        digest=report.digest,
    )
    # Nor does this tenant under another report digest.
    await _accept(
        clean,
        tenant_id=standalone,
        builder_id=None,
        reason="standalone",
        digest="b" * 64,
    )
    assert not (await _cutoff(clean)).ready
    await _refused(clean, "cutoff report is unaccepted")

    await _accept(
        clean,
        tenant_id=standalone,
        builder_id=None,
        reason="standalone",
        digest=report.digest,
    )
    assert (await _cutoff(clean)).ready
    await _arm(clean)
    assert (await _state_rows(clean))[0] == 1


async def test_new_traffic_changes_the_digest_and_voids_the_acceptance(
    clean: _Harness,
) -> None:
    await subscribed_builder(clean)
    standalone = await make_tenant(clean, None)
    await message_in(clean, standalone, NOW - timedelta(hours=1))
    await observation(clean, standalone, None, NOW - timedelta(hours=1))
    report = await _cutoff(clean)
    await _accept(
        clean,
        tenant_id=standalone,
        builder_id=None,
        reason="standalone",
        digest=report.digest,
    )
    other = await make_tenant(clean, None)
    await message_in(clean, other, NOW - timedelta(hours=1))
    await observation(clean, other, None, NOW - timedelta(hours=1))
    changed = await _cutoff(clean)
    assert changed.digest != report.digest and not changed.ready


# ---------------------------------------------------------------------------
# Cutoff builder: the current tenant row wins.
# ---------------------------------------------------------------------------


async def test_a_tenant_detached_inside_the_window_is_standalone(
    clean: _Harness,
) -> None:
    """Its observation recorded the old (covered) builder; the tenant row
    now has none, so the report must name it standalone."""

    world = await subscribed_builder(clean)
    await message_in(clean, world.tenant_id, NOW - timedelta(days=2))
    await observation(clean, world.tenant_id, world.builder_id, NOW - timedelta(days=2))
    assert (await _cutoff(clean)).findings == ()

    await execute(
        clean,
        "UPDATE tenants SET builder_id = NULL WHERE id = :id",
        id=world.tenant_id,
    )
    findings = (await _cutoff(clean)).findings
    assert [(f.tenant_id, f.builder_id, f.reason) for f in findings] == [
        (world.tenant_id, None, "standalone")
    ]


async def test_a_tenant_reparented_inside_the_window_uses_its_new_builder(
    clean: _Harness,
) -> None:
    covered = await subscribed_builder(clean)
    uncovered = await make_builder(clean)
    await message_in(clean, covered.tenant_id, NOW - timedelta(days=2))
    await observation(
        clean, covered.tenant_id, covered.builder_id, NOW - timedelta(days=2)
    )
    await execute(
        clean,
        "UPDATE tenants SET builder_id = :b WHERE id = :id",
        b=uncovered,
        id=covered.tenant_id,
    )
    findings = (await _cutoff(clean)).findings
    assert [(f.tenant_id, f.builder_id, f.reason) for f in findings] == [
        (covered.tenant_id, uncovered, "builder_without_coverage")
    ]


async def test_a_deleted_tenant_falls_back_to_its_recorded_builder(
    clean: _Harness,
) -> None:
    uncovered = await make_builder(clean)
    tenant_id = await make_tenant(clean, uncovered)
    await observation(clean, tenant_id, uncovered, NOW - timedelta(days=2))
    await execute(clean, "DELETE FROM tenants WHERE id = :id", id=tenant_id)
    findings = (await _cutoff(clean)).findings
    assert [(f.tenant_id, f.builder_id, f.reason) for f in findings] == [
        (tenant_id, uncovered, "builder_without_coverage")
    ]


# ---------------------------------------------------------------------------
# The cutoff report, one AC clause per case.
# ---------------------------------------------------------------------------

_THIS_MONTH = TODAY.replace(day=1)
_PREVIOUS_MONTH = (_THIS_MONTH - timedelta(days=1)).replace(day=1)
# Noon (São Paulo) of the first day of the previous month: inside the window.
PREVIOUS_MONTH_NOON = start_of_civil_day(_PREVIOUS_MONTH) + timedelta(hours=12)
# Noon of the last day before the previous month: outside the window.
BEFORE_WINDOW_NOON = start_of_civil_day(
    _PREVIOUS_MONTH - timedelta(days=1)
) + timedelta(hours=12)


async def _traffic(
    harness: _Harness,
    tenant_id: UUID,
    builder_id: UUID | None,
    at: datetime,
    **observation_kwargs: object,
) -> None:
    await message_in(harness, tenant_id, at)
    await observation(harness, tenant_id, builder_id, at, **observation_kwargs)


async def _unknown_claim(
    harness: _Harness, tenant_id: UUID, builder_id: UUID, source: str
) -> None:
    await execute(
        harness,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, provider_message_id,
            interaction_id, payload_sha256, decision_version, mode, created_at
        ) VALUES (:id, :tenant, :builder, :source, :principal, :message,
                  :interaction, :sha, 1, 'observational', :at)
        """,
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        source=source,
        principal=f"{source}:{tenant_id}",
        message=f"msg-{uuid4().hex}",
        interaction=uuid4(),
        sha="d" * 64,
        at=NOW - timedelta(hours=2),
    )


def _findings(report: CutoffReport) -> list[tuple[UUID, UUID | None, str]]:
    return [(f.tenant_id, f.builder_id, f.reason) for f in report.findings]


async def test_previous_month_standalone_traffic_is_listed(clean: _Harness) -> None:
    standalone = await make_tenant(clean, None)
    await _traffic(clean, standalone, None, PREVIOUS_MONTH_NOON)
    assert _findings(await _cutoff(clean)) == [(standalone, None, "standalone")]


async def test_traffic_older_than_the_two_months_is_not_listed(
    clean: _Harness,
) -> None:
    standalone = await make_tenant(clean, None)
    await _traffic(clean, standalone, None, BEFORE_WINDOW_NOON)
    assert _findings(await _cutoff(clean)) == []


async def test_current_month_builder_without_coverage_is_listed(
    clean: _Harness,
) -> None:
    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    await _traffic(clean, tenant_id, builder_id, NOW - timedelta(hours=1))
    assert _findings(await _cutoff(clean)) == [
        (tenant_id, builder_id, "builder_without_coverage")
    ]


@pytest.mark.parametrize("source", ["uazapi", "evolution"])
async def test_uazapi_and_evolution_traffic_is_listed(
    clean: _Harness, source: str
) -> None:
    world = await subscribed_builder(clean)
    await _traffic(clean, world.tenant_id, world.builder_id, NOW - timedelta(hours=1))
    await _unknown_claim(clean, world.tenant_id, world.builder_id, source)
    assert _findings(await _cutoff(clean)) == [
        (world.tenant_id, world.builder_id, "unknown_unauthenticated_traffic")
    ]


async def test_unknown_unauthenticated_observation_is_listed(
    clean: _Harness,
) -> None:
    world = await subscribed_builder(clean)
    await _traffic(
        clean,
        world.tenant_id,
        world.builder_id,
        NOW - timedelta(hours=1),
        origin="unknown",
        authenticated=False,
    )
    assert _findings(await _cutoff(clean)) == [
        (world.tenant_id, world.builder_id, "unknown_unauthenticated_traffic")
    ]


async def test_old_unknown_message_rows_do_not_list_a_covered_tenant(
    clean: _Harness,
) -> None:
    """``events.interaction_origin`` is 'unknown' for every row written before
    the origin deploy (and immutable); it is never read as unknown traffic."""

    world = await subscribed_builder(clean)
    await _traffic(clean, world.tenant_id, world.builder_id, NOW - timedelta(days=1))
    assert (
        await scalar(
            clean,
            "SELECT count(*) FROM events WHERE tenant_id = :t "
            "AND interaction_origin = 'unknown'",
            t=world.tenant_id,
        )
        == 1
    )
    assert _findings(await _cutoff(clean)) == []


@pytest.mark.parametrize(
    ("topup", "covered"),
    [
        ({}, True),
        ({"reversed_mc": 500_000}, False),
        ({"expires_at": NOW - timedelta(seconds=1)}, False),
        ({"accepted": False}, False),
    ],
    ids=["reconciled", "fully-reversed", "expired", "unreconciled"],
)
async def test_a_topup_only_tenant_is_covered_only_by_a_live_reconciled_lot(
    clean: _Harness, topup: dict[str, object], covered: bool
) -> None:
    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    await make_topup(clean, builder_id, **topup)  # type: ignore[arg-type]
    await _traffic(clean, tenant_id, builder_id, NOW - timedelta(hours=1))
    expected = [] if covered else [(tenant_id, builder_id, "builder_without_coverage")]
    assert _findings(await _cutoff(clean)) == expected


async def test_after_an_accepted_cutoff_each_tenant_follows_its_normal_rule(
    clean: _Harness,
) -> None:
    """Accepted is not exempt: the uncovered builder reaches the normal
    B+C path (served, debt), the standalone tenant is ``credit_pending``,
    and Uazapi/Evolution traffic stays unauthenticated (never enforced, so
    never debited)."""

    covered = await subscribed_builder(clean)
    uncovered = await make_builder(clean)
    uncovered_tenant = await make_tenant(clean, uncovered)
    standalone = await make_tenant(clean, None)
    hour_ago = NOW - timedelta(hours=1)
    await _traffic(clean, uncovered_tenant, uncovered, hour_ago)
    await _traffic(clean, standalone, None, hour_ago)
    await _traffic(clean, covered.tenant_id, covered.builder_id, hour_ago)
    await _unknown_claim(clean, covered.tenant_id, covered.builder_id, "uazapi")
    report = await _cutoff(clean)
    assert _findings(report) == sorted(
        [
            (uncovered_tenant, uncovered, "builder_without_coverage"),
            (standalone, None, "standalone"),
            (covered.tenant_id, covered.builder_id, "unknown_unauthenticated_traffic"),
        ],
        key=lambda row: (str(row[0]), row[2]),
    )
    for tenant_id, builder_id, reason in _findings(report):
        await _accept(
            clean,
            tenant_id=tenant_id,
            builder_id=builder_id,
            reason=reason,
            digest=report.digest,
        )
    await _arm(clean)

    assert await _precheck(clean, uncovered_tenant) == "ok"
    assert await _precheck(clean, standalone) == "credit_pending"
    assert await _precheck(clean, covered.tenant_id) == "ok"
    for source in ("uazapi", "evolution"):
        identity = credit_identity(source, covered.tenant_id)
        assert identity is not None
        principal = derive_credit_principal(
            identity,
            TenantBuilder(
                tenant_id=covered.tenant_id,
                builder_id=covered.builder_id,
                is_active=True,
            ),
        )
        assert principal.authenticated is False


async def _precheck(harness: _Harness, tenant_id: UUID) -> str:
    async with harness.sessions() as session:
        return await precheck_events(
            session,
            EventsEnforcement(
                tenant_id=tenant_id,
                session_id=uuid4(),
                principal_id=f"api_key:{tenant_id}",
                payload_sha256="d" * 64,
                idempotency_key="probe",
                key_problem=None,
            ),
        )


# ---------------------------------------------------------------------------
# A concurrent subscription change serializes under lock_rollout_control.
# ---------------------------------------------------------------------------


async def _locked_change(
    harness: _Harness,
    statements: tuple[tuple[str, dict[str, object]], ...],
    *,
    acquired: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
) -> None:
    """An ASAAS-webhook-shaped writer: take the rollout lock, mutate, commit."""

    engine, sessions = harness.factory()
    try:
        async with sessions() as session:
            await CreditRepository(session).lock_rollout_control(
                environment=settings.environment
            )
            if acquired is not None:
                acquired.set()
            await session.execute(text(PLANT))
            for sql, params in statements:
                await session.execute(text(sql), params)
            if release is not None:
                await release.wait()
            await session.commit()
    finally:
        await engine.dispose()


async def _lock_waiters(harness: _Harness) -> int:
    """Backends of this database blocked on a heavyweight lock right now."""

    return int(
        await scalar(
            harness,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = "
            "current_database() AND wait_event_type = 'Lock' "
            "AND pid <> pg_backend_pid()",
        )
    )


async def _until_blocked(harness: _Harness, task: asyncio.Task[object]) -> None:
    """Poll until a backend waits on a lock (the task's own); never a fixed
    negative sleep.  Fails if the task finished first (it did not wait)."""

    deadline = asyncio.get_running_loop().time() + 20
    while await _lock_waiters(harness) == 0:
        assert not task.done(), "it finished without waiting for the lock"
        assert asyncio.get_running_loop().time() < deadline, "never blocked"
        await asyncio.sleep(0.05)
    assert not task.done()


async def _settle(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_change_committed_before_the_gate_is_seen(clean: _Harness) -> None:
    """The writer holds the lock with the change uncommitted; the gate blocks
    on it (seen in pg_stat_activity) and its reread sees the committed
    ``readback_required``."""

    world = await subscribed_builder(clean)
    acquired, release = asyncio.Event(), asyncio.Event()
    writer = asyncio.create_task(
        _locked_change(
            clean,
            (
                (
                    "UPDATE billing_subscriptions SET status = 'readback_required' "
                    "WHERE id = :id",
                    {"id": world.subscription_id},
                ),
            ),
            acquired=acquired,
            release=release,
        )
    )
    gate: asyncio.Task[datetime] | None = None
    try:
        await asyncio.wait_for(acquired.wait(), timeout=20)
        gate = asyncio.create_task(_arm(clean))
        await _until_blocked(clean, gate)
        release.set()
        await asyncio.wait_for(writer, timeout=20)
        with pytest.raises(CreditEnforcementRejected, match="readiness is incomplete"):
            await asyncio.wait_for(gate, timeout=60)
    finally:
        release.set()
        await _settle(writer, *([gate] if gate is not None else []))
    assert await _state_rows(clean) == (0, None)


async def test_a_change_after_the_gate_waits_and_is_captured_by_id(
    clean: _Harness,
) -> None:
    """The gate holds the lock until its commit; the writer blocks on it
    (seen in pg_stat_activity), lands after it, and the by-id reread of the
    current cycle sees it."""

    world = await subscribed_builder(clean)
    writer: asyncio.Task[None] | None = None
    try:
        async with clean.sessions() as gate:
            await CreditRolloutService(gate).arm_enforcement(set_by="owner-decision")
            writer = asyncio.create_task(
                _locked_change(
                    clean,
                    (
                        (
                            "UPDATE billing_subscription_cycles "
                            "SET financial_revision = 2 WHERE id = :id",
                            {"id": world.cycle_id},
                        ),
                    ),
                )
            )
            await _until_blocked(clean, writer)
            await gate.commit()
        await asyncio.wait_for(writer, timeout=20)
    finally:
        if writer is not None:
            await _settle(writer)

    count, armed = await _state_rows(clean)
    assert count == 1 and armed is not None
    async with clean.sessions() as session:
        rollout = CreditRolloutService(session)
        readback = await rollout.readback_report(now=NOW)
        coverage = await rollout.binding_coverage(world.builder_id, at=NOW)
        await session.rollback()
    # Captured: the binding is now behind its cycle -- readback false, the
    # request still served (lag).
    assert readback.missing_or_stale_bindings == 1
    assert coverage.monthly_binding and not coverage.reconciliation_failure


# ---------------------------------------------------------------------------
# Fence: no route, no worker, no script reaches the single writers.
# ---------------------------------------------------------------------------

_FENCED = {
    "arm_enforcement": "src/services/credit_rollout.py",
    "record_capability_readiness": "src/services/credit_rollout.py",
    "accept_cutoff": "src/services/credit_rollout.py",
    "PurgeExecutor": "src/services/content_retention.py",
}
# Only this function may construct the enforcement row or set its boundary.
_STATE_WRITER = ("src/services/credit_rollout.py", "arm_enforcement")


def _exempt(tree: ast.Module) -> set[int]:
    """Docstrings and the ``__all__`` strings: the owner's declarations."""

    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                exempt.add(id(body[0].value))
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            exempt.update(id(inner) for inner in ast.walk(node.value))
    return exempt


def _references(path: Path, names: set[str], root: Path = ROOT) -> list[str]:
    """Every name, attribute, import alias or string literal naming one of
    *names* in *path* (a string covers ``getattr`` and ``import_module``).

    Applied to the owner module too: only the ``def``/``class`` statement
    itself (a str field, not a reference), its docstrings and ``__all__``
    are allowed there, so a worker in the same module calling the writer is
    caught."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    exempt = _exempt(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        name: str | None = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.name.rsplit(".", 1)[-1]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # getattr(service, "arm_enforcement") and import_module literals.
            name = node.value if node.value in names else None
        if name in names:
            found.append(f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}")
    return found


def _state_writes(path: Path, root: Path, allowed: str | None) -> list[str]:
    """``CreditEnforcementState(...)`` or ``.credit_enforcement_at =`` outside
    the function named *allowed*."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        inside = allowed is not None and function == allowed
        if not inside:
            if isinstance(node, ast.Call) and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "CreditEnforcementState"
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "CreditEnforcementState"
                )
            ):
                found.append(f"{path.relative_to(root)}:{node.lineno}:construct")
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                for inner in ast.walk(target):
                    if (
                        isinstance(inner, ast.Attribute)
                        and inner.attr == "credit_enforcement_at"
                    ):
                        found.append(f"{path.relative_to(root)}:{node.lineno}:assign")
            if isinstance(node, ast.Call) and any(
                keyword.arg == "credit_enforcement_at" for keyword in node.keywords
            ):
                found.append(f"{path.relative_to(root)}:{node.lineno}:keyword")
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return found


def _sources() -> list[Path]:
    return sorted([*ROOT.glob("src/**/*.py"), *ROOT.glob("scripts/**/*.py")])


def test_the_single_writers_have_no_caller_anywhere() -> None:
    offenders: list[str] = []
    for path in _sources():
        offenders.extend(_references(path, set(_FENCED)))
    assert offenders == []


def test_only_arm_enforcement_writes_the_enforcement_row() -> None:
    offenders: list[str] = []
    for path in _sources():
        relative = path.relative_to(ROOT).as_posix()
        allowed = _STATE_WRITER[1] if relative == _STATE_WRITER[0] else None
        offenders.extend(_state_writes(path, ROOT, allowed))
    assert offenders == []
    # The allowed writer really is where the sweep says it is.
    owner = ROOT / _STATE_WRITER[0]
    assert _state_writes(owner, ROOT, None)


@pytest.mark.parametrize(
    "source",
    [
        "async def f(s):\n    await s.arm_enforcement(set_by='x')\n",
        "def f(s):\n    return getattr(s, 'record_capability_readiness')\n",
        "from src.services.content_retention import PurgeExecutor\n",
        "import src.services.content_retention.PurgeExecutor\n",
        "from src.services.credit_rollout import accept_cutoff as innocuous\n",
        # The owner module itself: a worker beside the writer calling it.
        '"""Module."""\n__all__ = ["CreditRolloutService"]\n'
        "class CreditRolloutService:\n"
        "    async def record_capability_readiness(self):\n"
        '        """Writer."""\n'
        "async def run_worker_once(session):\n"
        "    await CreditRolloutService(session).record_capability_readiness()\n",
        "class PurgeExecutor:\n    pass\n\n\ndef build():\n"
        "    return PurgeExecutor()\n",
    ],
)
def test_the_fence_analysis_sees_every_reference_form(
    tmp_path: Path, source: str
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _references(probe, set(_FENCED), root=tmp_path)


def test_the_owner_declarations_alone_are_allowed(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        '"""Mentions arm_enforcement in prose."""\n'
        '__all__ = ["PurgeExecutor"]\n'
        "class PurgeExecutor:\n"
        '    """PurgeExecutor."""\n'
        "    async def arm_enforcement(self):\n"
        '        """accept_cutoff"""\n',
        encoding="utf-8",
    )
    assert _references(probe, set(_FENCED), root=tmp_path) == []


@pytest.mark.parametrize(
    "source",
    [
        "def f(s):\n    s.add(CreditEnforcementState(id=1))\n",
        "def f(m):\n    return m.CreditEnforcementState(id=1)\n",
        "def f(row, at):\n    row.credit_enforcement_at = at\n",
        "def f(t, at):\n    return update(t).values(credit_enforcement_at=at)\n",
    ],
)
def test_the_state_writer_analysis_sees_every_write_form(
    tmp_path: Path, source: str
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _state_writes(probe, tmp_path, "arm_enforcement")


def test_the_fence_sweep_covers_routes_workers_and_scripts() -> None:
    swept = {path.relative_to(ROOT).as_posix() for path in _sources()}
    assert {
        "src/api/main.py",
        "src/api/routes/admin_credits.py",
        "src/services/credit_rollout.py",
        "src/services/content_retention.py",
    } <= swept
    assert any(name.startswith("scripts/") for name in swept)


async def test_the_fence_starts_unset_after_migration(clean: _Harness) -> None:
    assert await _state_rows(clean) == (0, None)
    async with clean.sessions() as session:
        assert await CreditRepository(session).enforcement_at() is None
        assert (
            await session.scalar(
                select(func.count()).select_from(CreditEnforcementState)
            )
        ) == 0
        await session.rollback()
