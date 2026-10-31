"""Binding, grace and top-ups of an enforced request on real PostgreSQL.

Owner decisions B+C, H3-(i) and M2 (Group 7, item 2), read through
``CreditRolloutService.binding_coverage`` -- the gate of every enforced
request -- and through the real ``precheck_events`` of ``POST /v1/events``:

* the monthly allowance is usable only through the current cycle's binding
  or, with the subscription active, the previous one inside its grace (until
  the next binding commits or K = ``credit_binding_grace_days`` civil days
  after ``cycle_end``); a cancelled subscription has no grace and an expired
  allowance never comes back;
* a reconciled, unexpired top-up with balance stays usable without any
  binding, cancelled subscription included; builder drift (identity
  divergence or a ``quarantined`` line) blocks the top-ups too;
* consumer lag (an identity-matched active binding behind the cycle's
  ``financial_revision``) does not block a request but keeps readback and
  readiness false and alerts;
* a fully charged-back (``reversed``) binding is no monthly capacity but
  not a reconciliation failure: the request follows the 72 h debt path;
* ``credit_pending`` (``reconciliation_failure``) is only an identity
  conflict, a current cycle with no binding outside grace, or an
  unreconciled top-up source.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from src.config.settings import settings
from src.services.credit_ledger import start_of_civil_day
from src.services.credit_metering import EventsEnforcement, precheck_events
from src.services.credit_rollout import BindingCoverage, CreditRolloutService
from tests.integration.credit_rollout_world import (
    SHA,
    civil_today,
    clean,
    make_builder,
    make_cycle,
    make_monthly_grant,
    make_subscription,
    make_tenant,
    make_topup,
    reverse_binding,
    set_cycle_revision,
    set_subscription_status,
    subscribed_builder,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness

pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

NOW = datetime.now(timezone.utc)
TODAY = civil_today(NOW)


async def _coverage(
    harness: _Harness, builder_id: UUID, at: datetime = NOW
) -> BindingCoverage:
    async with harness.sessions() as session:
        coverage = await CreditRolloutService(session).binding_coverage(
            builder_id, at=at
        )
        await session.rollback()
    return coverage


async def _precheck(harness: _Harness, tenant_id: UUID) -> str:
    async with harness.sessions() as session:
        return await precheck_events(
            session,
            EventsEnforcement(
                tenant_id=tenant_id,
                session_id=uuid4(),
                principal_id=f"api_key:{tenant_id}",
                payload_sha256=SHA,
                idempotency_key="probe",
                key_problem=None,
            ),
        )


async def _readback_complete(harness: _Harness) -> bool:
    async with harness.sessions() as session:
        report = await CreditRolloutService(session).readback_report(now=NOW)
        await session.rollback()
    return report.complete


async def _readiness(harness: _Harness) -> tuple[bool, bool]:
    async with harness.sessions() as session:
        ready = await CreditRolloutService(session).capability_ready(now=NOW)
        await session.rollback()
    return ready.readback_complete, ready.has_alert


async def _previous_cycle(
    harness: _Harness,
    *,
    ended_days_ago: int,
    status: str = "active",
    next_binding_status: str | None = None,
) -> tuple[UUID, UUID]:
    """A builder whose last settled cycle ended ``ended_days_ago`` civil
    days ago; optionally the next cycle is current with its own binding."""

    builder_id = await make_builder(harness)
    tenant_id = await make_tenant(harness, builder_id)
    subscription_id = await make_subscription(harness, builder_id)
    end = TODAY - timedelta(days=ended_days_ago)
    start = end - timedelta(days=30)
    cycle_id = await make_cycle(
        harness, builder_id, subscription_id, start=start, end=end
    )
    await make_monthly_grant(
        harness,
        builder_id=builder_id,
        subscription_id=subscription_id,
        cycle_id=cycle_id,
        start=start,
        end=end,
    )
    if next_binding_status is not None:
        following = await make_cycle(
            harness,
            builder_id,
            subscription_id,
            start=end,
            end=end + timedelta(days=30),
            revision=2 if next_binding_status == "reversed" else 1,
        )
        grant = await make_monthly_grant(
            harness,
            builder_id=builder_id,
            subscription_id=subscription_id,
            cycle_id=following,
            start=end,
            end=end + timedelta(days=30),
            binding_created_at=NOW - timedelta(hours=1),
        )
        if next_binding_status == "reversed":
            assert grant.binding_id is not None
            await reverse_binding(harness, grant.binding_id, 2)
    if status != "active":
        await set_subscription_status(harness, subscription_id, status)
    return builder_id, tenant_id


# ---------------------------------------------------------------------------
# The current cycle: exact, lag, reversed, conflict, missing.
# ---------------------------------------------------------------------------


async def test_an_exact_current_binding_is_monthly_coverage(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    coverage = await _coverage(clean, world.builder_id)
    assert coverage.monthly_binding and coverage.usable and coverage.covered
    assert not coverage.reconciliation_failure
    assert await _precheck(clean, world.tenant_id) == "ok"
    assert await _readback_complete(clean)


async def test_consumer_lag_serves_the_request_but_keeps_readiness_false(
    clean: _Harness,
) -> None:
    """The cycle moved to revision 2 (a fact the consumer has not applied
    yet); the identity-matched active binding is still at revision 1."""

    world = await subscribed_builder(clean)
    await set_cycle_revision(clean, world.cycle_id, 2)

    coverage = await _coverage(clean, world.builder_id)
    assert coverage.monthly_binding
    assert coverage.usable and coverage.covered
    assert not coverage.reconciliation_failure
    assert await _precheck(clean, world.tenant_id) == "ok"
    readback_complete, has_alert = await _readiness(clean)
    assert readback_complete is False
    assert has_alert is True  # credit_reconciliation binding_lag


async def test_a_binding_ahead_of_its_cycle_is_not_usable(clean: _Harness) -> None:
    """Unchanged rule: a binding revision HIGHER than the cycle's names a
    fact the producer does not have -- not capacity, a reconciliation
    failure."""

    world = await subscribed_builder(clean, revision=1, binding_revision=2)
    coverage = await _coverage(clean, world.builder_id)
    assert not coverage.monthly_binding
    assert coverage.reconciliation_failure and not coverage.usable
    assert await _precheck(clean, world.tenant_id) == "credit_pending"


async def test_a_reversed_binding_is_debt_not_credit_pending(clean: _Harness) -> None:
    """A full chargeback of the plan line: no monthly capacity, yet no
    reconciliation failure -- the request reaches the 72 h debt path."""

    world = await subscribed_builder(clean)
    await set_cycle_revision(clean, world.cycle_id, 2)
    assert world.grant.binding_id is not None
    await reverse_binding(clean, world.grant.binding_id, 2)

    coverage = await _coverage(clean, world.builder_id)
    assert not coverage.monthly_binding
    assert not coverage.reconciliation_failure
    assert coverage.usable and not coverage.covered
    assert await _precheck(clean, world.tenant_id) == "ok"


async def test_an_identity_conflict_is_credit_pending_even_with_a_topup(
    clean: _Harness,
) -> None:
    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    subscription_id = await make_subscription(clean, builder_id)
    start, end = TODAY - timedelta(days=10), TODAY + timedelta(days=20)
    cycle_id = await make_cycle(
        clean, builder_id, subscription_id, start=start, end=end
    )
    # The binding names this cycle id but another period: builder drift.
    await make_monthly_grant(
        clean,
        builder_id=builder_id,
        subscription_id=subscription_id,
        cycle_id=cycle_id,
        start=start,
        end=end,
        binding_end=end + timedelta(days=1),
    )
    await make_topup(clean, builder_id)

    coverage = await _coverage(clean, builder_id)
    assert coverage.identity_conflict and coverage.reconciliation_failure
    assert not coverage.usable and not coverage.covered
    assert await _precheck(clean, tenant_id) == "credit_pending"


async def test_a_current_cycle_without_binding_outside_grace_is_credit_pending(
    clean: _Harness,
) -> None:
    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    subscription_id = await make_subscription(clean, builder_id)
    await make_cycle(
        clean,
        builder_id,
        subscription_id,
        start=TODAY - timedelta(days=10),
        end=TODAY + timedelta(days=20),
    )
    coverage = await _coverage(clean, builder_id)
    assert coverage.reconciliation_failure and not coverage.usable
    assert await _precheck(clean, tenant_id) == "credit_pending"
    assert not await _readback_complete(clean)


async def test_a_current_cycle_without_binding_inside_grace_is_covered(
    clean: _Harness,
) -> None:
    """The next cycle row exists, its binding is not committed yet: the
    previous binding keeps serving inside its grace."""

    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    subscription_id = await make_subscription(clean, builder_id)
    end = TODAY - timedelta(days=1)
    previous = await make_cycle(
        clean, builder_id, subscription_id, start=end - timedelta(days=30), end=end
    )
    await make_monthly_grant(
        clean,
        builder_id=builder_id,
        subscription_id=subscription_id,
        cycle_id=previous,
        start=end - timedelta(days=30),
        end=end,
    )
    await make_cycle(
        clean, builder_id, subscription_id, start=end, end=end + timedelta(days=30)
    )
    coverage = await _coverage(clean, builder_id)
    assert coverage.monthly_binding and not coverage.reconciliation_failure
    assert await _precheck(clean, tenant_id) == "ok"


# ---------------------------------------------------------------------------
# Grace (B+C, H3-(i)): the previous binding after cycle_end.
# ---------------------------------------------------------------------------


async def test_the_previous_binding_serves_inside_k_days(clean: _Harness) -> None:
    builder_id, tenant_id = await _previous_cycle(clean, ended_days_ago=2)
    coverage = await _coverage(clean, builder_id)
    assert coverage.monthly_binding and coverage.covered
    assert await _precheck(clean, tenant_id) == "ok"


async def test_the_expired_allowance_never_comes_back(clean: _Harness) -> None:
    builder_id, tenant_id = await _previous_cycle(
        clean, ended_days_ago=settings.credit_binding_grace_days + 1
    )
    coverage = await _coverage(clean, builder_id)
    assert not coverage.monthly_binding and not coverage.covered
    # No current cycle row: no reconciliation failure, the debt path.
    assert coverage.usable and not coverage.reconciliation_failure
    assert await _precheck(clean, tenant_id) == "ok"


async def test_k_comes_from_credit_binding_grace_days(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _tenant = await _previous_cycle(clean, ended_days_ago=3)
    assert (await _coverage(clean, builder_id)).monthly_binding
    monkeypatch.setattr(settings, "credit_binding_grace_days", 2)
    assert not (await _coverage(clean, builder_id)).monthly_binding


async def test_the_next_committed_binding_cuts_the_grace(clean: _Harness) -> None:
    """The next cycle's binding committed (here: then fully charged back),
    so the previous allowance stops at that commit even inside K days."""

    builder_id, tenant_id = await _previous_cycle(
        clean, ended_days_ago=1, next_binding_status="reversed"
    )
    coverage = await _coverage(clean, builder_id)
    assert not coverage.monthly_binding
    assert not coverage.reconciliation_failure
    assert await _precheck(clean, tenant_id) == "ok"


async def test_a_cancelled_subscription_has_no_grace(clean: _Harness) -> None:
    builder_id, tenant_id = await _previous_cycle(
        clean, ended_days_ago=1, status="cancelled"
    )
    coverage = await _coverage(clean, builder_id)
    assert not coverage.monthly_binding and not coverage.covered
    assert coverage.usable
    assert await _precheck(clean, tenant_id) == "ok"


# ---------------------------------------------------------------------------
# Top-ups (M2): independent of the subscription, blocked by drift.
# ---------------------------------------------------------------------------


async def test_a_reconciled_topup_stays_usable_with_a_cancelled_subscription(
    clean: _Harness,
) -> None:
    builder_id, tenant_id = await _previous_cycle(
        clean, ended_days_ago=1, status="cancelled"
    )
    await make_topup(clean, builder_id)
    coverage = await _coverage(clean, builder_id)
    assert coverage.usable_topup and coverage.covered and coverage.usable
    assert not coverage.monthly_binding
    assert await _precheck(clean, tenant_id) == "ok"


async def test_a_fully_reversed_topup_does_not_cover(clean: _Harness) -> None:
    builder_id = await make_builder(clean)
    await make_tenant(clean, builder_id)
    await make_topup(clean, builder_id, reversed_mc=500_000)
    coverage = await _coverage(clean, builder_id)
    assert not coverage.usable_topup and not coverage.covered


async def test_an_unreconciled_topup_source_is_credit_pending(
    clean: _Harness,
) -> None:
    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    await make_topup(clean, builder_id, accepted=False)
    coverage = await _coverage(clean, builder_id)
    assert not coverage.usable_topup and coverage.reconciliation_failure
    assert await _precheck(clean, tenant_id) == "credit_pending"


async def test_a_quarantined_line_blocks_the_topups_too(clean: _Harness) -> None:
    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    await make_topup(clean, builder_id)
    await make_topup(clean, builder_id, state="quarantined")
    coverage = await _coverage(clean, builder_id)
    assert coverage.quarantined and not coverage.usable and not coverage.covered
    assert await _precheck(clean, tenant_id) == "credit_pending"


async def test_a_reversed_binding_ahead_of_its_cycle_is_not_reconciled(
    clean: _Harness,
) -> None:
    """The revision-ahead rule applies whatever the status: a reversed
    binding at revision 2 over a cycle row still at 1 names a fact the
    producer does not have."""

    world = await subscribed_builder(clean)
    assert world.grant.binding_id is not None
    await reverse_binding(clean, world.grant.binding_id, 2)
    coverage = await _coverage(clean, world.builder_id)
    assert not coverage.monthly_binding
    assert coverage.reconciliation_failure and not coverage.usable
    assert await _precheck(clean, world.tenant_id) == "credit_pending"


# ---------------------------------------------------------------------------
# Civil-date boundaries.  America/Sao_Paulo has had no DST since 2019 (it is
# UTC-3 all year), so the only civil-time edge left is midnight itself and
# the UTC/São Paulo offset; the DST-era midnight rule of start_of_civil_day
# is covered by the ledger suite.
# ---------------------------------------------------------------------------

BOUNDARY_DAY = date(2027, 2, 15)
NOON_SP = start_of_civil_day(BOUNDARY_DAY) + timedelta(hours=12)
# 02:30 UTC on the 15th is still 23:30 of the 14th in São Paulo.
EARLY_UTC = datetime(2027, 2, 15, 2, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("instant", "start", "end", "current"),
    [
        # cycle_end == today: the cycle is over (half-open interval).
        (NOON_SP, BOUNDARY_DAY - timedelta(days=30), BOUNDARY_DAY, False),
        # cycle_start == today: the cycle is current.
        (NOON_SP, BOUNDARY_DAY, BOUNDARY_DAY + timedelta(days=30), True),
        # At 02:30 UTC the civil day is still the 14th.
        (EARLY_UTC, BOUNDARY_DAY - timedelta(days=30), BOUNDARY_DAY, True),
        (EARLY_UTC, BOUNDARY_DAY, BOUNDARY_DAY + timedelta(days=30), False),
    ],
    ids=["end-is-today", "start-is-today", "utc-0230-end", "utc-0230-start"],
)
async def test_the_current_cycle_is_the_sao_paulo_civil_day(
    clean: _Harness, instant: datetime, start: date, end: date, current: bool
) -> None:
    """An unbound cycle of an active subscription: a gate failure (readback
    and request coverage) exactly when it is current."""

    assert CreditRolloutService.current_civil_date(instant) == (
        BOUNDARY_DAY if instant == NOON_SP else BOUNDARY_DAY - timedelta(days=1)
    )
    builder_id = await make_builder(clean)
    subscription_id = await make_subscription(clean, builder_id)
    await make_cycle(clean, builder_id, subscription_id, start=start, end=end)
    async with clean.sessions() as session:
        rollout = CreditRolloutService(session)
        readback = await rollout.readback_report(now=instant)
        coverage = await rollout.binding_coverage(builder_id, at=instant)
        await session.rollback()
    assert readback.complete is (not current)
    assert coverage.reconciliation_failure is current


# ---------------------------------------------------------------------------
# The K boundary, to the microsecond.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grace_days", [None, 2])
async def test_the_grace_ends_exactly_k_civil_days_after_cycle_end(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch, grace_days: int | None
) -> None:
    if grace_days is not None:
        monkeypatch.setattr(settings, "credit_binding_grace_days", grace_days)
    k = settings.credit_binding_grace_days
    builder_id, _tenant = await _previous_cycle(clean, ended_days_ago=1)
    boundary = start_of_civil_day(TODAY - timedelta(days=1)) + timedelta(days=k)
    inside = await _coverage(clean, builder_id, at=boundary - timedelta(microseconds=1))
    outside = await _coverage(clean, builder_id, at=boundary)
    assert inside.monthly_binding and inside.covered
    assert not outside.monthly_binding and not outside.covered
    assert outside.usable and not outside.reconciliation_failure


# ---------------------------------------------------------------------------
# More top-up and subscription cases.
# ---------------------------------------------------------------------------


async def test_an_expired_topup_is_not_usable(clean: _Harness) -> None:
    builder_id = await make_builder(clean)
    await make_tenant(clean, builder_id)
    await make_topup(clean, builder_id, expires_at=NOW - timedelta(seconds=1))
    coverage = await _coverage(clean, builder_id)
    assert not coverage.usable_topup and not coverage.covered
    # An expired lot is no source at all: the debt path, not credit_pending.
    assert coverage.usable and not coverage.reconciliation_failure


async def test_an_inactive_subscription_has_no_grace(clean: _Harness) -> None:
    builder_id, tenant_id = await _previous_cycle(
        clean, ended_days_ago=1, status="inactive"
    )
    coverage = await _coverage(clean, builder_id)
    assert not coverage.monthly_binding and not coverage.covered
    assert await _precheck(clean, tenant_id) == "ok"


async def test_a_quarantined_cycle_blocks_the_topups_too(clean: _Harness) -> None:
    world = await subscribed_builder(clean)
    await make_topup(clean, world.builder_id)
    await make_cycle(
        clean,
        world.builder_id,
        world.subscription_id,
        start=TODAY + timedelta(days=40),
        end=TODAY + timedelta(days=70),
        state="quarantined",
    )
    coverage = await _coverage(clean, world.builder_id)
    assert coverage.quarantined and not coverage.usable and not coverage.covered
    assert await _precheck(clean, world.tenant_id) == "credit_pending"
