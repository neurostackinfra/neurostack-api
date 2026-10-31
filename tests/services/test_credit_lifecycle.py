"""Credit exhaustion lifecycle (Group 5): deadlines, episodes, transitions.

Pure rules (boundaries, America/Sao_Paulo DST, the H3-(i) cases) and the
evaluation on the real application's SQLite ``:memory:`` database (the
``client`` fixture): the episode opens on the first effect after the
enforcement, keeps one ``exhausted_at``, reactivates only with debt paid and
positive capacity, and the 72 h / 30 d transitions are written once by the
worker or the request gate with the same effect id.  Concurrency, the guard
trigger and the append-only rules run on PostgreSQL in
``tests/integration/test_credit_retention_postgres.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from src.models.builder import Builder
from src.models.content_retention import (
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
    CreditThresholdAlert,
)
from src.models.credit import CreditEnforcementBinding, CreditEnforcementState
from src.models.tenant import Tenant
from src.repositories.content_retention import ContentRetentionRepository
from src.repositories.database import get_db_session
from src.services import credit_inventory, credit_lifecycle
from src.services.credit_ledger import (
    CreditLedger,
    GrantSpec,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    start_of_civil_day,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_lifecycle import (
    DEBT_GRACE,
    add_civil_days,
    binding_grace_end,
    deletion_due,
    suspension_due,
    thresholds_reached,
    transition_effect_id,
)
from src.services.credit_switch import CreditSwitch

UTC = timezone.utc
SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Pure rules.
# ---------------------------------------------------------------------------


def test_debt_window_is_exactly_72_hours_at_its_boundaries() -> None:
    exhausted = datetime(2026, 3, 10, 14, 30, 15, 123456, tzinfo=UTC)
    due = suspension_due(exhausted)
    assert due - exhausted == timedelta(hours=72)
    assert due == datetime(2026, 3, 13, 14, 30, 15, 123456, tzinfo=UTC)


def test_retention_is_30_civil_days_at_the_same_local_time() -> None:
    exhausted = datetime(2026, 1, 31, 23, 59, tzinfo=SAO_PAULO)
    assert deletion_due(exhausted).astimezone(SAO_PAULO) == datetime(
        2026, 3, 2, 23, 59, tzinfo=SAO_PAULO
    )


def test_retention_keeps_the_local_wall_clock_across_a_dst_change() -> None:
    """Brazil had DST until 2019: on 2018-11-04 clocks went 00:00 -> 01:00.
    Thirty civil days keep 12:00 local, which is one hour less than 720 h."""

    exhausted = datetime(2018, 10, 20, 12, 0, tzinfo=SAO_PAULO)  # UTC-3
    due = deletion_due(exhausted)
    assert due.astimezone(SAO_PAULO).replace(tzinfo=None) == datetime(
        2018, 11, 19, 12, 0
    )
    assert due - exhausted == timedelta(days=30) - timedelta(hours=1)
    # The 72 h debt window, by contrast, is absolute hours across the change.
    before = datetime(2018, 11, 3, 12, 0, tzinfo=SAO_PAULO)
    assert suspension_due(before) - before == DEBT_GRACE


def test_a_retention_deadline_in_a_dst_gap_is_deterministic() -> None:
    # 2018-11-04 00:30 did not exist in Sao Paulo.
    exhausted = datetime(2018, 10, 5, 0, 30, tzinfo=SAO_PAULO)
    first, second = deletion_due(exhausted), deletion_due(exhausted)
    assert first == second > exhausted


def test_naive_instants_are_refused() -> None:
    for call in (
        lambda: deletion_due(datetime(2026, 1, 1)),
        lambda: suspension_due(datetime(2026, 1, 1)),
        lambda: binding_grace_end(datetime(2026, 1, 1), grace_days=5, is_active=True),
    ):
        with pytest.raises(ValueError, match="timezone-aware"):
            call()


CYCLE_END = at(2, 3)  # start_of_civil_day of a cycle_end (00:00 local)


def test_topup_only_builder_gets_72_hours_then_suspends() -> None:
    exhausted = at(20)
    assert suspension_due(exhausted) == exhausted + DEBT_GRACE
    assert deletion_due(exhausted) == add_civil_days(exhausted, 30)


def test_cancelled_subscription_has_no_grace() -> None:
    grace = binding_grace_end(CYCLE_END, grace_days=5, is_active=False)
    assert grace == CYCLE_END
    exhausted = at(3)
    assert (
        suspension_due(exhausted, cycle_end=CYCLE_END, binding_grace_ends_at=grace)
        == exhausted + DEBT_GRACE
    )


def test_active_subscription_without_settlement_suspends_at_grace_end_plus_72h() -> (
    None
):
    grace = binding_grace_end(CYCLE_END, grace_days=5, is_active=True)
    assert grace == add_civil_days(CYCLE_END, 5)
    exhausted = at(3)
    assert (
        suspension_due(exhausted, cycle_end=CYCLE_END, binding_grace_ends_at=grace)
        == grace + DEBT_GRACE
    )
    # Retention still counts from exhausted_at.
    assert deletion_due(exhausted) == add_civil_days(exhausted, 30)


def test_cancellation_observed_mid_grace_cuts_it_at_the_observation() -> None:
    observed = at(4)
    grace = binding_grace_end(
        CYCLE_END, grace_days=5, is_active=False, observed_inactive_at=observed
    )
    assert grace == observed
    early = at(3)
    assert (
        suspension_due(early, cycle_end=CYCLE_END, binding_grace_ends_at=grace)
        == observed + DEBT_GRACE
    )
    late = at(5)  # exhausted after the observation: 72 h from exhausted_at
    assert (
        suspension_due(late, cycle_end=CYCLE_END, binding_grace_ends_at=grace)
        == late + DEBT_GRACE
    )


def test_next_binding_only_shortens_the_grace() -> None:
    assert binding_grace_end(
        CYCLE_END, grace_days=5, is_active=True, next_binding_at=at(3)
    ) == at(3)
    # A binding committed before cycle_end never ends the grace before it.
    assert (
        binding_grace_end(
            CYCLE_END, grace_days=5, is_active=True, next_binding_at=at(1)
        )
        == CYCLE_END
    )
    # Never extends past cycle_end + K.
    assert binding_grace_end(
        CYCLE_END, grace_days=5, is_active=True, next_binding_at=at(20)
    ) == add_civil_days(CYCLE_END, 5)


def test_exhaustion_before_cycle_end_gains_nothing_from_the_grace() -> None:
    grace = binding_grace_end(CYCLE_END, grace_days=5, is_active=True)
    exhausted = CYCLE_END - timedelta(minutes=1)
    assert (
        suspension_due(exhausted, cycle_end=CYCLE_END, binding_grace_ends_at=grace)
        == exhausted + DEBT_GRACE
    )


@pytest.mark.parametrize(
    "capacity,available,expected",
    [
        (10_000, 10_000, ()),
        (10_000, 5_001, ()),
        (10_000, 5_000, (50,)),
        (10_000, 1_000, (50, 90)),
        (10_000, 1, (50, 90)),
        (10_000, 0, (50, 90, 100)),
        (0, 0, (50, 90, 100)),
    ],
)
def test_thresholds(capacity: int, available: int, expected: tuple[int, ...]) -> None:
    assert thresholds_reached(capacity, available) == expected


# ---------------------------------------------------------------------------
# Evaluation on the application database.
# ---------------------------------------------------------------------------

SUB = UUID("5ab5c000-0000-4000-8000-000000000001")
ENFORCED = datetime(2026, 11, 1, 12, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime


@dataclass(frozen=True)
class World:
    builder: UUID
    tenant: UUID


def run(client: Any, coroutine: Any) -> Any:
    return client._submit(coroutine)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    current = Clock(ENFORCED + timedelta(hours=1))

    async def reference_now(_self: CreditLedger) -> datetime:
        return current.now

    monkeypatch.setattr(CreditLedger, "reference_now", reference_now)
    monkeypatch.setattr(credit_inventory, "_now", lambda: current.now)
    return current


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Any:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


@pytest.fixture(autouse=True)
def _forget() -> Any:
    credit_inventory.forget_enforcement_seen()
    yield
    credit_inventory.forget_enforcement_seen()


async def _seed() -> World:
    world = World(uuid4(), uuid4())
    session = get_db_session()
    try:
        async with session.begin():
            session.add(Builder(id=world.builder, name="Lifecycle builder"))
            await session.flush()
            session.add(
                Tenant(
                    id=world.tenant,
                    name=f"Lifecycle {world.tenant.hex[:6]}",
                    slug=f"lifecycle-{world.tenant.hex}",
                    api_key_hash=f"hash-{world.tenant.hex}",
                    builder_id=world.builder,
                    config={},
                )
            )
    finally:
        await session.close()
    return world


@pytest.fixture
def world(client: Any) -> World:
    return run(client, _seed())


async def _enforce(instant: datetime = ENFORCED) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            session.add(
                CreditEnforcementState(
                    id=1, credit_enforcement_at=instant, set_by="test", set_at=instant
                )
            )
    finally:
        await session.close()


async def _grant_topup(builder: UUID, granted_mc: int, instant: datetime) -> UUID:
    order = uuid4()
    session = get_db_session()
    try:
        async with session.begin():
            ledger = CreditLedger(session)
            book = await ledger.open_book(builder, at=instant)
            book.grant(
                GrantSpec(
                    lot_kind="topup",
                    effect_id=topup_grant_effect_id(order),
                    payment_id=f"pay-{order}",
                    paid_at=instant.date(),
                    granted_mc=granted_mc,
                    expires_at=topup_lot_expires_at(instant.date()),
                    order_id=order,
                ),
                at=instant,
            )
            await ledger.persist(book)
    finally:
        await session.close()
    return order


async def _grant_monthly(
    builder: UUID, start: date, end: date, granted_mc: int, instant: datetime
) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            ledger = CreditLedger(session)
            book = await ledger.open_book(builder, at=instant)
            book.grant(
                GrantSpec(
                    lot_kind="monthly",
                    effect_id=monthly_grant_effect_id(SUB, start, end),
                    payment_id=f"pay-{start}",
                    paid_at=start,
                    granted_mc=granted_mc,
                    expires_at=monthly_lot_expires_at(end),
                    subscription_id=SUB,
                    cycle_start=start,
                    cycle_end=end,
                ),
                at=instant,
            )
            await ledger.persist(book)
    finally:
        await session.close()


async def _debit(world: World, amount: int, instant: datetime) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            result = await CreditLedger(session).debit(
                world.builder,
                f"trace:{world.tenant}:{uuid4()}",
                amount,
                at=instant,
                effect_kind="trace",
                tenant_id=world.tenant,
            )
            assert result.status == "applied", result
    finally:
        await session.close()


async def _episodes(builder: UUID) -> list[CreditLifecycleEpisode]:
    session = get_db_session()
    try:
        return list(
            (
                await session.execute(
                    select(CreditLifecycleEpisode)
                    .where(CreditLifecycleEpisode.builder_id == builder)
                    .order_by(CreditLifecycleEpisode.exhausted_at)
                )
            ).scalars()
        )
    finally:
        await session.close()


async def _transitions(builder: UUID) -> list[CreditLifecycleTransition]:
    session = get_db_session()
    try:
        return list(
            (
                await session.execute(
                    select(CreditLifecycleTransition)
                    .where(CreditLifecycleTransition.builder_id == builder)
                    .order_by(CreditLifecycleTransition.occurred_at)
                )
            ).scalars()
        )
    finally:
        await session.close()


async def _alerts(builder: UUID) -> list[tuple[str, int]]:
    session = get_db_session()
    try:
        rows = await session.execute(
            select(CreditThresholdAlert.arm_key, CreditThresholdAlert.threshold)
            .where(CreditThresholdAlert.builder_id == builder)
            .order_by(CreditThresholdAlert.arm_key, CreditThresholdAlert.threshold)
        )
        return [(str(key), int(threshold)) for key, threshold in rows]
    finally:
        await session.close()


async def _gate(builder: UUID, now: datetime) -> str:
    session = get_db_session()
    try:
        async with session.begin():
            return await credit_lifecycle.request_gate(session, builder, now=now)
    finally:
        await session.close()


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def test_nothing_happens_before_the_enforcement_even_when_exhausted(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    """Switch on, no enforcement: a topup consumed and then expired leaves no
    episode, transition or alert, and the worker claims nothing."""

    start = ENFORCED - timedelta(days=200)
    run(client, _grant_topup(world.builder, 1_000, start))
    clock.now = ENFORCED - timedelta(days=1)
    # The lot expired (six months) before this instant: the sweep records it.
    assert run(client, credit_lifecycle.run_lifecycle_once(now=clock.now)) == 0
    assert run(client, _episodes(world.builder)) == []
    assert run(client, _transitions(world.builder)) == []
    assert run(client, _alerts(world.builder)) == []


def test_exact_zero_opens_grace_and_later_zeros_keep_the_timestamp(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    run(client, _enforce())
    run(client, _grant_topup(world.builder, 2_000, ENFORCED + timedelta(minutes=5)))
    first = ENFORCED + timedelta(hours=1)
    clock.now = first
    run(client, _debit(world, 1_000, first))  # 50 %
    assert run(client, _episodes(world.builder)) == []
    run(client, _debit(world, 1_000, first + timedelta(minutes=1)))  # exact zero
    [episode] = run(client, _episodes(world.builder))
    exhausted = first + timedelta(minutes=1)
    assert _utc(episode.exhausted_at) == exhausted
    assert _utc(episode.suspension_due_at) == exhausted + DEBT_GRACE
    assert _utc(episode.deletion_due_at) == deletion_due(exhausted)
    # A partial debt and a repeated zero never move the deadline.
    run(client, _debit(world, 1_000, first + timedelta(hours=2)))
    run(client, _debit(world, 500, first + timedelta(hours=3)))
    [same] = run(client, _episodes(world.builder))
    assert same.id == episode.id
    assert _utc(same.exhausted_at) == exhausted
    assert _utc(same.suspension_due_at) == exhausted + DEBT_GRACE
    assert [t.transition for t in run(client, _transitions(world.builder))] == [
        "opened"
    ]
    keys = run(client, _alerts(world.builder))
    assert [threshold for _key, threshold in keys] == [50, 90, 100]
    assert len({key for key, _threshold in keys}) == 1


def test_no_episode_is_inherited_at_the_enforcement_instant(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    """A builder already at zero when the enforcement is set opens its
    episode only at the first effect after it, at that effect's instant."""

    run(client, _enforce())
    clock.now = ENFORCED + timedelta(days=2)
    assert run(client, credit_lifecycle.run_lifecycle_once(now=clock.now)) == 0
    assert run(client, _episodes(world.builder)) == []
    effect_at = ENFORCED + timedelta(days=3)
    clock.now = effect_at
    run(client, _debit(world, 1_000, effect_at))  # a builder with no grant: debt
    [episode] = run(client, _episodes(world.builder))
    assert _utc(episode.exhausted_at) == effect_at


def test_reactivation_needs_debt_paid_and_positive_capacity(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    run(client, _enforce())
    clock.now = ENFORCED + timedelta(hours=1)
    run(client, _debit(world, 1_000, clock.now))  # 1 credit of debt
    [episode] = run(client, _episodes(world.builder))
    # A grant smaller than the debt: still in debt, same episode.
    run(client, _grant_topup(world.builder, 400, clock.now + timedelta(minutes=1)))
    # A grant that pays the rest exactly: zero capacity is no reactivation.
    run(client, _grant_topup(world.builder, 600, clock.now + timedelta(minutes=2)))
    [still] = run(client, _episodes(world.builder))
    assert still.reactivated_at is None
    # Positive capacity with no debt: reactivated, thresholds re-armed.
    run(client, _grant_topup(world.builder, 5_000, clock.now + timedelta(minutes=3)))
    [closed] = run(client, _episodes(world.builder))
    assert closed.id == episode.id
    assert _utc(closed.reactivated_at) == clock.now + timedelta(minutes=3)
    assert [t.transition for t in run(client, _transitions(world.builder))] == [
        "opened",
        "reactivated",
    ]
    # A new exhaustion opens a new episode with new deadlines and a new arm.
    later = clock.now + timedelta(hours=5)
    clock.now = later
    run(client, _debit(world, 5_000, later))
    episodes = run(client, _episodes(world.builder))
    assert len(episodes) == 2
    assert _utc(episodes[1].exhausted_at) == later
    arms = {key for key, _threshold in run(client, _alerts(world.builder))}
    assert len(arms) == 2


def test_72h_transition_once_by_worker_or_request_with_one_effect_id(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    run(client, _enforce())
    exhausted = ENFORCED + timedelta(hours=1)
    clock.now = exhausted
    run(client, _debit(world, 1_000, exhausted))
    [episode] = run(client, _episodes(world.builder))
    due = exhausted + DEBT_GRACE

    # One microsecond before the deadline: neither path suspends.
    clock.now = due - timedelta(microseconds=1)
    assert run(client, _gate(world.builder, clock.now)) == "ok"
    run(client, credit_lifecycle.run_lifecycle_once(now=clock.now))
    assert "suspended" not in [
        t.transition for t in run(client, _transitions(world.builder))
    ]

    # At the deadline the worker records it; retries and the request gate
    # never record it again.
    clock.now = due
    assert run(client, credit_lifecycle.run_lifecycle_once(now=due)) == 1
    # The retry finds nothing pending (the transition is recorded): it does
    # not even claim the builder, so it cannot write anything twice.
    assert run(client, credit_lifecycle.run_lifecycle_once(now=due)) == 0
    assert run(client, _gate(world.builder, due + timedelta(minutes=1))) == "suspended"
    suspended = [
        t
        for t in run(client, _transitions(world.builder))
        if t.transition == "suspended"
    ]
    assert len(suspended) == 1
    assert suspended[0].effect_id == transition_effect_id(episode.id, "suspended")
    assert suspended[0].source == "worker"
    assert _utc(suspended[0].occurred_at) == due

    # 30 civil days: deletion_due, by the request gate this time.
    deadline = deletion_due(exhausted)
    clock.now = deadline
    assert run(client, _gate(world.builder, deadline)) == "suspended"
    run(client, credit_lifecycle.run_lifecycle_once(now=deadline))
    marked = [
        t
        for t in run(client, _transitions(world.builder))
        if t.transition == "deletion_due"
    ]
    assert len(marked) == 1
    assert marked[0].source == "effect"
    assert marked[0].effect_id == transition_effect_id(episode.id, "deletion_due")
    assert _utc(marked[0].occurred_at) == deadline


def test_request_gate_records_suspension_first_and_worker_adds_nothing(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    run(client, _enforce())
    exhausted = ENFORCED + timedelta(hours=1)
    clock.now = exhausted
    run(client, _debit(world, 1_000, exhausted))
    due = exhausted + DEBT_GRACE
    clock.now = due + timedelta(seconds=5)
    assert run(client, _gate(world.builder, clock.now)) == "suspended"
    run(client, credit_lifecycle.run_lifecycle_once(now=clock.now))
    suspended = [
        t
        for t in run(client, _transitions(world.builder))
        if t.transition == "suspended"
    ]
    assert [(t.source, _utc(t.occurred_at)) for t in suspended] == [("effect", due)]


def test_switch_off_worker_claims_nothing_even_when_due(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    run(client, _enforce())
    exhausted = ENFORCED + timedelta(hours=1)
    clock.now = exhausted
    run(client, _debit(world, 1_000, exhausted))
    switch(False)
    clock.now = exhausted + DEBT_GRACE + timedelta(hours=1)
    assert run(client, credit_lifecycle.run_lifecycle_once(now=clock.now)) == 0
    assert "suspended" not in [
        t.transition for t in run(client, _transitions(world.builder))
    ]


def test_early_renewal_pays_only_from_cycle_start_and_then_reactivates(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    """MEDIUM-9/MEDIUM-7: suspended with debt; a monthly allowance paid early
    does not pay before ``cycle_start``; at ``start_of_civil_day(cycle_start)``
    the worker -- with no traffic -- settles and reactivates."""

    run(client, _enforce())
    exhausted = ENFORCED + timedelta(hours=1)
    clock.now = exhausted
    run(client, _debit(world, 1_000, exhausted))
    clock.now = exhausted + DEBT_GRACE + timedelta(hours=1)
    run(client, credit_lifecycle.run_lifecycle_once(now=clock.now))
    start = (clock.now + timedelta(days=3)).astimezone(SAO_PAULO).date()
    end = date(start.year + (start.month == 12), start.month % 12 + 1, start.day)
    run(client, _grant_monthly(world.builder, start, end, 10_000, clock.now))
    run(client, credit_lifecycle.run_lifecycle_once(now=clock.now))
    [episode] = run(client, _episodes(world.builder))
    assert episode.reactivated_at is None
    opening = start_of_civil_day(start)
    clock.now = opening - timedelta(seconds=1)
    run(client, credit_lifecycle.run_lifecycle_once(now=clock.now))
    assert run(client, _episodes(world.builder))[0].reactivated_at is None
    clock.now = opening
    run(client, credit_lifecycle.run_lifecycle_once(now=opening))
    [closed] = run(client, _episodes(world.builder))
    assert _utc(closed.reactivated_at) == opening
    assert run(client, _gate(world.builder, opening + timedelta(minutes=1))) == "ok"


async def _bind(
    builder: UUID, cycle_start: date, cycle_end: date, created: datetime
) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            session.add(
                CreditEnforcementBinding(
                    id=uuid4(),
                    builder_id=builder,
                    subscription_id=SUB,
                    subscription_cycle_id=uuid4(),
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                    financial_revision=1,
                    grant_effect_id=monthly_grant_effect_id(
                        SUB, cycle_start, cycle_end
                    ),
                    status="active",
                    created_at=created,
                    updated_at=created,
                )
            )
    finally:
        await session.close()


def test_grace_of_an_active_subscription_then_a_cancellation_shortens_it(
    client: Any,
    world: World,
    clock: Clock,
    switch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = {"value": True}

    async def is_active(_self: Any, _subscription: UUID) -> bool:
        return active["value"]

    monkeypatch.setattr(ContentRetentionRepository, "subscription_is_active", is_active)
    run(client, _enforce())
    cycle_start, cycle_end = date(2026, 10, 15), date(2026, 11, 15)
    clock.now = ENFORCED + timedelta(hours=1)
    run(client, _grant_monthly(world.builder, cycle_start, cycle_end, 1_000, clock.now))
    run(client, _bind(world.builder, cycle_start, cycle_end, clock.now))
    cycle_end_at = start_of_civil_day(cycle_end)
    exhausted = cycle_end_at + timedelta(hours=10)  # the allowance expired: debt
    clock.now = exhausted
    run(client, _debit(world, 500, exhausted))
    [episode] = run(client, _episodes(world.builder))
    grace_end = add_civil_days(cycle_end_at, 5)
    assert _utc(episode.grace_ends_at) == grace_end
    assert _utc(episode.suspension_due_at) == grace_end + DEBT_GRACE
    # Cancelled during the grace: the first observation cuts it (first wins).
    active["value"] = False
    observed = exhausted + timedelta(days=1)
    clock.now = observed
    run(client, _debit(world, 100, observed))
    clock.now = observed + timedelta(hours=1)
    run(client, _debit(world, 100, clock.now))
    [shortened] = run(client, _episodes(world.builder))
    assert _utc(shortened.observed_inactive_at) == observed
    assert _utc(shortened.grace_ends_at) == observed
    assert _utc(shortened.suspension_due_at) == observed + DEBT_GRACE
    assert _utc(shortened.deletion_due_at) == deletion_due(exhausted)
    assert [t.transition for t in run(client, _transitions(world.builder))] == [
        "opened",
        "grace_shortened",
    ]


def test_reservation_verdict_blocks_only_new_debt_of_an_exhausted_builder(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    """Deliverable 3: only an upload that needs NEW capacity (a positive
    charge none of which a lot covered) of an already exhausted builder is
    refused; a zero delta is accepted while the episode is open."""

    async def verdict(effect_id: str, charged: int) -> str:
        session = get_db_session()
        try:
            return await credit_lifecycle.reservation_verdict(
                session,
                builder_id=world.builder,
                effect_id=effect_id,
                charged_mc=charged,
            )
        finally:
            await session.close()

    run(client, _enforce())
    run(client, _grant_topup(world.builder, 2_000, ENFORCED + timedelta(minutes=1)))
    # Not exhausted: whatever the charge.
    assert run(client, verdict(f"rag:{world.tenant}:{uuid4()}:reserve", 500)) == "ok"
    clock.now = ENFORCED + timedelta(hours=1)
    run(client, _debit(world, 2_000, clock.now))
    assert len(run(client, _episodes(world.builder))) == 1
    # Exhausted: a zero delta still passes; a charge with no lot debit
    # (all of it would be new debt) is the exhausted category.
    assert run(client, verdict(f"rag:{world.tenant}:{uuid4()}:reserve", 0)) == "ok"
    assert (
        run(client, verdict(f"rag:{world.tenant}:{uuid4()}:reserve", 500))
        == "category_exhausted"
    )


async def _quiet_backlog(count: int, exhausted: datetime) -> None:
    """``count`` builders with an open, ALREADY suspended episode whose
    deletion is not due: nothing pending, but older than any later one."""

    from src.models.credit import CreditAccount

    session = get_db_session()
    try:
        async with session.begin():
            for index in range(count):
                builder, account, episode = uuid4(), uuid4(), uuid4()
                session.add(Builder(id=builder, name=f"Backlog {index}"))
                await session.flush()
                session.add(CreditAccount(id=account, builder_id=builder))
                await session.flush()
                session.add(
                    CreditLifecycleEpisode(
                        id=episode,
                        builder_id=builder,
                        account_id=account,
                        opened_effect_id=f"lifecycle:{builder}:episode:0:opened",
                        exhausted_at=exhausted,
                        suspension_due_at=exhausted + DEBT_GRACE,
                        deletion_due_at=deletion_due(exhausted),
                    )
                )
                await session.flush()
                session.add(
                    CreditLifecycleTransition(
                        id=uuid4(),
                        builder_id=builder,
                        episode_id=episode,
                        transition="suspended",
                        effect_id=transition_effect_id(episode, "suspended"),
                        occurred_at=exhausted + DEBT_GRACE,
                        source="worker",
                        detail={},
                    )
                )
    finally:
        await session.close()


def test_a_backlog_of_quiet_episodes_never_starves_a_later_builder(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    """MEDIUM-1: more than one batch (50) of older open episodes with
    nothing pending must not fill the worker's batch: the later builder is
    still suspended at its deadline and, with no traffic, reactivated when
    its early-paid allowance starts."""

    run(client, _enforce())
    run(client, _quiet_backlog(credit_lifecycle.WORKER_BATCH + 10, ENFORCED))
    exhausted = ENFORCED + timedelta(hours=1)
    clock.now = exhausted
    run(client, _debit(world, 1_000, exhausted))
    due = exhausted + DEBT_GRACE
    clock.now = due
    assert run(client, credit_lifecycle.run_lifecycle_once(now=due)) == 1
    assert "suspended" in [
        t.transition for t in run(client, _transitions(world.builder))
    ]
    start = (due + timedelta(days=3)).astimezone(SAO_PAULO).date()
    end = date(start.year + (start.month == 12), start.month % 12 + 1, start.day)
    run(client, _grant_monthly(world.builder, start, end, 10_000, due))
    opening = start_of_civil_day(start)
    clock.now = opening
    assert run(client, credit_lifecycle.run_lifecycle_once(now=opening)) == 1
    [closed] = run(client, _episodes(world.builder))
    assert _utc(closed.reactivated_at) == opening


async def _line_targets(
    builder: UUID, order: UUID, reversal: int, restoration: int, instant: datetime
) -> None:
    """A refund (reversal) / its restoration of a top-up line, cumulative."""

    session = get_db_session()
    try:
        async with session.begin():
            ledger = CreditLedger(session)
            book = await ledger.open_book(builder, at=instant)
            lot = book.topup_lot(order)
            assert lot is not None
            book.apply_line_targets(
                lot.id,
                line_id=f"line-{order}",
                payment_id=f"pay-{order}",
                financial_revision=reversal + restoration,
                reversal_target_mc=reversal,
                restoration_target_mc=restoration,
                at=instant,
            )
            await ledger.persist(book)
    finally:
        await session.close()


def test_a_reversal_before_the_enforcement_opens_no_episode(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    """Switch on, enforcement set for later: a lot consumed and then fully
    refunded (consumption becomes debt, capacity zero) before the
    enforcement opens nothing -- and nothing is inherited at or after it
    without a new effect."""

    run(client, _enforce())
    early = ENFORCED - timedelta(days=10)
    clock.now = early
    order = run(client, _grant_topup(world.builder, 1_000, early))
    run(client, _debit(world, 600, early + timedelta(minutes=1)))
    clock.now = early + timedelta(minutes=2)
    run(client, _line_targets(world.builder, order, 1_000, 0, clock.now))
    assert run(client, _episodes(world.builder)) == []
    assert run(client, _transitions(world.builder)) == []
    assert run(client, _alerts(world.builder)) == []
    clock.now = ENFORCED + timedelta(days=4)
    assert run(client, credit_lifecycle.run_lifecycle_once(now=clock.now)) == 0
    assert run(client, _episodes(world.builder)) == []


def test_a_restoration_reactivates_only_with_debt_paid_and_capacity_left(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    """A refund after the enforcement turns consumption into debt and opens
    the episode; a partial restoration that leaves debt keeps it open; the
    full restoration pays the debt and leaves capacity: reactivated.
    (Deleting content refunds nothing -- owner decision 38 B -- so there is
    no deallocation effect that could reactivate.)"""

    run(client, _enforce())
    clock.now = ENFORCED + timedelta(minutes=5)
    order = run(client, _grant_topup(world.builder, 2_000, clock.now))
    clock.now = ENFORCED + timedelta(hours=1)
    run(client, _debit(world, 1_500, clock.now))
    assert run(client, _episodes(world.builder)) == []
    refunded = ENFORCED + timedelta(hours=2)
    clock.now = refunded
    run(client, _line_targets(world.builder, order, 2_000, 0, refunded))
    [episode] = run(client, _episodes(world.builder))
    assert _utc(episode.exhausted_at) == refunded
    clock.now = refunded + timedelta(hours=1)
    run(client, _line_targets(world.builder, order, 2_000, 1_000, clock.now))
    assert run(client, _episodes(world.builder))[0].reactivated_at is None
    restored = refunded + timedelta(hours=2)
    clock.now = restored
    run(client, _line_targets(world.builder, order, 2_000, 2_000, restored))
    [closed] = run(client, _episodes(world.builder))
    assert closed.id == episode.id
    assert _utc(closed.reactivated_at) == restored
    assert [t.transition for t in run(client, _transitions(world.builder))] == [
        "opened",
        "reactivated",
    ]


def test_h3_exhaustions_are_served_as_debt_never_credit_pending(
    client: Any,
    world: World,
    clock: Clock,
    switch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H3-(i): a top-up-only builder whose lot runs out, and one whose
    subscription was cancelled (no grace), keep being served as debt (every
    debit ``applied``, no spend hold) for 72 h, then suspend -- never
    ``credit_pending``."""

    async def inactive(_self: Any, _subscription: UUID) -> bool:
        return False

    monkeypatch.setattr(ContentRetentionRepository, "subscription_is_active", inactive)

    async def spend_hold(builder: UUID, instant: datetime) -> bool:
        session = get_db_session()
        try:
            async with session.begin():
                book = await CreditLedger(session).open_book(builder, at=instant)
                return bool(book.spend_hold.active)
        finally:
            await session.close()

    run(client, _enforce())
    cancelled = run(client, _seed())
    cycle_start, cycle_end = date(2026, 10, 15), date(2026, 11, 15)
    clock.now = ENFORCED + timedelta(minutes=5)
    run(client, _grant_topup(world.builder, 1_000, clock.now))
    run(
        client,
        _grant_monthly(cancelled.builder, cycle_start, cycle_end, 1_000, clock.now),
    )
    run(client, _bind(cancelled.builder, cycle_start, cycle_end, clock.now))
    exhausted = ENFORCED + timedelta(hours=1)
    clock.now = exhausted
    for builder_world in (world, cancelled):
        run(client, _debit(builder_world, 1_000, exhausted))  # exact zero
    for builder_world in (world, cancelled):
        [episode] = run(client, _episodes(builder_world.builder))
        assert _utc(episode.suspension_due_at) == exhausted + DEBT_GRACE
        # Inside the 72 h: still served as debt, no hold.
        clock.now = exhausted + DEBT_GRACE - timedelta(microseconds=1)
        run(client, _debit(builder_world, 300, clock.now))
        assert run(client, spend_hold(builder_world.builder, clock.now)) is False
        assert run(client, _gate(builder_world.builder, clock.now)) == "ok"
        assert (
            run(client, _gate(builder_world.builder, exhausted + DEBT_GRACE))
            == "suspended"
        )
        clock.now = exhausted


def test_deletion_due_is_not_recorded_one_microsecond_before_30_days(
    client: Any, world: World, clock: Clock, switch: Any
) -> None:
    run(client, _enforce())
    exhausted = ENFORCED + timedelta(hours=1)
    clock.now = exhausted
    run(client, _debit(world, 1_000, exhausted))
    deadline = deletion_due(exhausted)
    clock.now = deadline - timedelta(microseconds=1)
    run(client, credit_lifecycle.run_lifecycle_once(now=clock.now))
    assert run(client, _gate(world.builder, clock.now)) == "suspended"
    names = [t.transition for t in run(client, _transitions(world.builder))]
    assert "suspended" in names and "deletion_due" not in names
    state, _episode = run(client, _state(world.builder))
    assert state == "suspended"
    clock.now = deadline
    run(client, credit_lifecycle.run_lifecycle_once(now=deadline))
    [marked] = [
        t
        for t in run(client, _transitions(world.builder))
        if t.transition == "deletion_due"
    ]
    assert _utc(marked.occurred_at) == deadline
    assert run(client, _state(world.builder))[0] == "deletion_due"


async def _state(builder: UUID) -> Any:
    session = get_db_session()
    try:
        return await credit_lifecycle.builder_state(session, builder)
    finally:
        await session.close()


async def test_the_lifecycle_worker_starts_with_the_config_and_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.config.settings import settings

    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    assert credit_lifecycle.start_credit_lifecycle_worker() is None
    ticks: list[int] = []

    async def tick(*_args: Any, **_kwargs: Any) -> int:
        ticks.append(1)
        return 0

    monkeypatch.setattr(credit_lifecycle, "run_lifecycle_once", tick)
    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    task = credit_lifecycle.start_credit_lifecycle_worker()
    assert task is not None
    await asyncio.sleep(0.05)
    await credit_lifecycle.stop_credit_lifecycle_worker(task)
    assert task.done() and ticks == [1]


def test_the_lifespan_starts_and_stops_the_lifecycle_worker() -> None:
    import ast
    import inspect

    from src.api import main as main_module

    called = {
        node.func.id
        for node in ast.walk(ast.parse(inspect.getsource(main_module.lifespan)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"start_credit_lifecycle_worker", "stop_credit_lifecycle_worker"} <= called
