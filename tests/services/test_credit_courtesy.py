"""Courtesy credits (creditos-cortesia Group 2): ledger rules, the service on
SQLite and the 046 revision gate.

The pure :class:`LedgerBook` operations (``grant_courtesy``,
``remove_courtesy``), the capacity rule (a removal is funding removed, never
usage), the FEFO position of a courtesy lot, and the service
(:mod:`src.services.credit_courtesy`) through the SQLite adapter: replay,
reused key, inactive builder, lost-key race through the savepoint, logs
without the reason.  PostgreSQL proofs (CHECKs, concurrency, enforced
lifecycle, no ``billing_*`` write) live in
``tests/integration/test_credit_courtesy_postgres.py``.

Run also under ``-p tests.credit_switch_forced_plugin``: the gate test below
then asserts the forced reading opens the courtesy gate, and without the
plugin (SQLite never has ``alembic_version``) that it stays closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.api.routes.credits import _used_percent as route_used_percent
from src.models.base import Base
from src.models.builder import Builder
from src.models.credit import (
    CreditAccount,
    CreditCourtesyEvent,
    CreditEffect,
    CreditLedgerEntry,
    CreditLot,
)
from src.repositories.credit import (
    LOT_DECREASES,
    LOT_INCREASES,
    CreditRepository,
    LedgerMovement,
    fold_ledger,
)
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import credit_courtesy, credit_switch
from src.services.credit_courtesy import (
    CourtesyBuilderInactive,
    CourtesyBuilderNotFound,
    CourtesyIdempotencyKeyReused,
    courtesy_request_digest,
    grant_body,
    removal_body,
)
from src.services.credit_ledger import (
    COURTESY_REMOVAL_EFFECT,
    CREDIT_TIMEZONE,
    LEDGER_EFFECT_KINDS,
    CourtesyLotNotFound,
    CourtesyRemovalExceedsBalance,
    CreditLedger,
    GrantSpec,
    LedgerBook,
    courtesy_grant_effect_id,
    courtesy_lot_expires_at,
    courtesy_removal_effect_id,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    start_of_civil_day,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_lifecycle import capacity_in_force_mc, thresholds_reached
from src.services.credit_switch import (
    COURTESY_SCHEMA_MIN_REVISION,
    CREDIT_SCHEMA_MIN_REVISION,
    CreditSwitch,
    CreditSwitchReading,
    ImageMigrationChainError,
    courtesy_gate_open,
    credit_switch_reading,
    load_image_migration_chain,
)

T0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
SUB = UUID("5a000000-0000-4000-8000-0000000000cc")
MC = 1_000


def _book() -> LedgerBook:
    return LedgerBook(builder_id=uuid4(), account_id=uuid4())


def _monthly(
    book: LedgerBook,
    *,
    grant: int,
    start: date = date(2026, 11, 1),
    end: date = date(2026, 12, 1),
    at: datetime = T0,
) -> UUID:
    return book.grant(
        GrantSpec(
            lot_kind="monthly",
            effect_id=monthly_grant_effect_id(SUB, start, end),
            payment_id=f"pay-{start}",
            paid_at=start,
            granted_mc=grant,
            expires_at=monthly_lot_expires_at(end),
            subscription_id=SUB,
            cycle_start=start,
            cycle_end=end,
        ),
        at=at,
    ).id


def _topup(book: LedgerBook, *, grant: int, at: datetime = T0) -> UUID:
    order = uuid4()
    return book.grant(
        GrantSpec(
            lot_kind="topup",
            effect_id=topup_grant_effect_id(order),
            payment_id=f"pay-{order}",
            paid_at=at.date(),
            granted_mc=grant,
            expires_at=topup_lot_expires_at(at.date()),
            order_id=order,
        ),
        at=at,
    ).id


def _used_percent(capacity: int, available: int) -> tuple[int, int]:
    """``used_mc`` and ``used_percent`` of ``/v1/credits/thresholds``: the
    route's ``used = max(capacity - available, 0)`` and its own
    ``_used_percent`` (``src/api/routes/credits.py``), not a copy of it."""

    used = max(capacity - available, 0)
    return used, route_used_percent(capacity, available, used)


# ---------------------------------------------------------------------------
# Calendar, effect ids and direction maps.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("granted", "expires"),
    [
        # 12:00 in Sao Paulo: the civil date is the UTC date.
        (T0, datetime(2026, 12, 9, 3, 0, tzinfo=timezone.utc)),
        # 23:30 in Sao Paulo on Nov 9 is already Nov 10 in UTC.
        (
            datetime(2026, 11, 10, 2, 30, tzinfo=timezone.utc),
            datetime(2026, 12, 9, 3, 0, tzinfo=timezone.utc),
        ),
        # Across a month end.
        (
            datetime(2027, 1, 31, 12, 0, tzinfo=timezone.utc),
            datetime(2027, 3, 2, 3, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_a_courtesy_lot_lasts_thirty_civil_days(
    granted: datetime, expires: datetime
) -> None:
    book = _book()
    lot = book.grant_courtesy(uuid4(), 5_000 * MC, at=granted)

    granted_on = granted.astimezone(CREDIT_TIMEZONE).date()
    assert lot.paid_at == granted_on
    assert (
        lot.expires_at == expires == start_of_civil_day(granted_on + timedelta(days=30))
    )
    assert courtesy_lot_expires_at(granted_on) == expires


def test_the_courtesy_grant_records_a_lot_without_payment() -> None:
    book = _book()
    event = uuid4()
    lot = book.grant_courtesy(event, 10_000 * MC, at=T0)

    [row] = book.writes.lots
    assert row["lot_kind"] == "courtesy"
    assert row["payment_id"] is None
    assert row["subscription_id"] is None and row["order_id"] is None
    assert row["cycle_start"] is None and row["cycle_end"] is None
    assert row["grant_effect_id"] == courtesy_grant_effect_id(event)
    assert courtesy_grant_effect_id(event) == f"courtesy:{event}:grant"
    assert [(e["entry_type"], e["amount_mc"]) for e in book.writes.entries] == [
        ("grant", 10_000 * MC)
    ]
    assert lot.spend_starts_at() is None
    assert book.available_mc(T0) == 10_000 * MC


@pytest.mark.parametrize("amount", [0, -1, 1.5, True])
def test_a_non_positive_courtesy_grant_writes_nothing(amount: Any) -> None:
    book = _book()
    with pytest.raises(ValueError, match="granted_mc must be a positive integer"):
        book.grant_courtesy(uuid4(), amount, at=T0)
    assert book.writes.effects == [] and book.writes.entries == []
    assert book.writes.lots == []


def test_direction_maps_count_a_removal_as_a_lot_decrease() -> None:
    assert "courtesy_removal" in LOT_DECREASES
    assert "courtesy_removal" not in LOT_INCREASES
    assert COURTESY_REMOVAL_EFFECT in LEDGER_EFFECT_KINDS
    lot = uuid4()
    fold = fold_ledger(
        [
            LedgerMovement("grant", 10_000 * MC, lot),
            LedgerMovement("courtesy_removal", 6_000 * MC, lot),
        ]
    )
    assert fold.lots == {lot: 4_000 * MC} and fold.debt_mc == 0


# ---------------------------------------------------------------------------
# Removal rules (DESIGN Decision 5).
# ---------------------------------------------------------------------------


def test_a_partial_removal_takes_whole_credits_and_never_consumption() -> None:
    book = _book()
    lot_id = book.grant_courtesy(uuid4(), 10_000 * MC, at=T0).id
    book.debit("trace:1", 1_500 * MC, at=T0, effect_kind="trace")
    event = uuid4()

    removed = book.remove_courtesy(lot_id, event, credits=2_000, at=T0)

    lot = book.lot(lot_id)
    assert removed == 2_000 * MC
    assert lot.sums["courtesy_removal"] == 2_000 * MC
    assert lot.consumed_mc == 1_500 * MC
    assert lot.available_mc(T0) == 6_500 * MC
    assert book.open_debt_mc == 0
    entries = [
        (e["entry_type"], e["amount_mc"], e["effect_id"])
        for e in book.writes.entries
        if e["entry_type"] == "courtesy_removal"
    ]
    assert entries == [
        ("courtesy_removal", 2_000 * MC, courtesy_removal_effect_id(event))
    ]
    assert courtesy_removal_effect_id(event) == f"courtesy-removal:{event}"
    [effect] = [
        e for e in book.writes.effects if e["effect_id"].startswith("courtesy-")
    ]
    assert effect["effect_kind"] == COURTESY_REMOVAL_EFFECT


def test_the_fraction_stays_unless_remove_all() -> None:
    """4,999,400 mc: ``{credits: 4999}`` leaves 400 mc, ``remove_all`` 0."""

    book = _book()
    lot_id = book.grant_courtesy(uuid4(), 5_000 * MC, at=T0).id
    book.debit("rag:1", 600, at=T0, effect_kind="rag")
    assert book.lot(lot_id).available_mc(T0) == 4_999_400

    with pytest.raises(CourtesyRemovalExceedsBalance) as refusal:
        book.remove_courtesy(lot_id, uuid4(), credits=5_000, at=T0)
    assert (refusal.value.requested_mc, refusal.value.spendable_mc) == (
        5_000_000,
        4_999_400,
    )
    assert refusal.value.code == "courtesy_removal_exceeds_balance"

    assert book.remove_courtesy(lot_id, uuid4(), credits=4_999, at=T0) == 4_999_000
    assert book.lot(lot_id).available_mc(T0) == 400
    with pytest.raises(CourtesyRemovalExceedsBalance):
        book.remove_courtesy(lot_id, uuid4(), credits=1, at=T0)
    assert book.remove_courtesy(lot_id, uuid4(), remove_all=True, at=T0) == 400
    assert book.lot(lot_id).available_mc(T0) == 0
    assert book.lot(lot_id).consumed_mc == 600


def test_remove_all_of_an_empty_or_expired_lot_is_refused_before_any_entry() -> None:
    book = _book()
    emptied = book.grant_courtesy(uuid4(), 1_000 * MC, at=T0).id
    book.remove_courtesy(emptied, uuid4(), remove_all=True, at=T0)
    expiring = book.grant_courtesy(uuid4(), 1_000 * MC, at=T0).id
    before = len(book.writes.entries)

    with pytest.raises(CourtesyRemovalExceedsBalance) as zeroed:
        book.remove_courtesy(emptied, uuid4(), remove_all=True, at=T0)
    assert (zeroed.value.requested_mc, zeroed.value.spendable_mc) == (None, 0)
    later = book.lot(expiring).expires_at
    with pytest.raises(CourtesyRemovalExceedsBalance):
        book.remove_courtesy(expiring, uuid4(), remove_all=True, at=later)
    assert not [
        e for e in book.writes.entries[before:] if e["entry_type"] == "courtesy_removal"
    ]


@pytest.mark.parametrize(
    ("credits", "remove_all", "message"),
    [
        (None, False, "exactly one"),
        (1, True, "exactly one"),
        (0, False, "positive integer"),
        (-5, False, "positive integer"),
        (True, False, "positive integer"),
    ],
)
def test_a_malformed_removal_is_a_value_error(
    credits: Any, remove_all: bool, message: str
) -> None:
    book = _book()
    lot_id = book.grant_courtesy(uuid4(), 1_000 * MC, at=T0).id
    with pytest.raises(ValueError, match=message):
        book.remove_courtesy(
            lot_id, uuid4(), credits=credits, remove_all=remove_all, at=T0
        )


def test_only_a_courtesy_lot_accepts_a_removal() -> None:
    book = _book()
    monthly = _monthly(book, grant=1_000 * MC)
    topup = _topup(book, grant=1_000 * MC)
    for lot_id in (monthly, topup, uuid4()):
        with pytest.raises(CourtesyLotNotFound):
            book.remove_courtesy(lot_id, uuid4(), credits=1, at=T0)
    assert not [e for e in book.writes.entries if e["entry_type"] == "courtesy_removal"]


# ---------------------------------------------------------------------------
# Capacity, FEFO and debt (DESIGN Decisions 7 and 8, "Capacidade").
# ---------------------------------------------------------------------------


def test_a_removal_is_funding_removed_not_usage() -> None:
    book = _book()
    lot_id = book.grant_courtesy(uuid4(), 10_000 * MC, at=T0).id
    book.remove_courtesy(lot_id, uuid4(), credits=6_000, at=T0)

    capacity = capacity_in_force_mc(book, T0)
    available = book.available_trace_mc(T0)
    assert capacity == available == 4_000 * MC
    assert _used_percent(capacity, available) == (0, 0)
    assert thresholds_reached(capacity, available) == ()

    book.debit("trace:1", 1_000 * MC, at=T0, effect_kind="trace")
    capacity = capacity_in_force_mc(book, T0)
    available = book.available_trace_mc(T0)
    assert (capacity, available) == (4_000 * MC, 3_000 * MC)
    assert _used_percent(capacity, available) == (1_000 * MC, 25)
    assert thresholds_reached(capacity, available) == ()


def test_fefo_spends_monthly_then_courtesy_then_a_later_topup() -> None:
    """The started monthly drains first even though it expires AFTER the
    courtesy (cycle 11-05 -> 12-20), so the order is "monthly first", not
    merely "earliest expiry first"; then the courtesy, then the later top-up."""

    book = _book()
    monthly = _monthly(
        book, grant=1_000 * MC, start=date(2026, 11, 5), end=date(2026, 12, 20)
    )
    topup = _topup(book, grant=1_000 * MC)
    courtesy = book.grant_courtesy(uuid4(), 1_000 * MC, at=T0).id
    assert book.lot(courtesy).expires_at < book.lot(monthly).expires_at
    assert book.lot(courtesy).expires_at < book.lot(topup).expires_at

    book.debit("trace:1", 1_500 * MC, at=T0, effect_kind="trace")
    assert book.lot(monthly).available_mc(T0) == 0
    assert book.lot(courtesy).available_mc(T0) == 500 * MC
    assert book.lot(topup).available_mc(T0) == 1_000 * MC
    book.debit("trace:2", 1_000 * MC, at=T0, effect_kind="trace")
    assert book.lot(courtesy).available_mc(T0) == 0
    assert book.lot(topup).available_mc(T0) == 500 * MC


def test_the_courtesy_grant_settles_open_debt_first() -> None:
    book = _book()
    book.debit("trace:1", 300 * MC, at=T0, effect_kind="trace")
    assert book.open_debt_mc == 300 * MC

    lot = book.grant_courtesy(uuid4(), 1_000 * MC, at=T0 + timedelta(minutes=1))

    assert book.open_debt_mc == 0
    assert lot.available_mc(T0 + timedelta(minutes=1)) == 700 * MC
    assert lot.sums["debt_settlement"] == 300 * MC
    [settlement] = book.writes.settlements
    assert settlement["settling_grant_lot_id"] == lot.id
    assert settlement["effect_id"] == lot.grant_effect_id


def test_an_expired_courtesy_leaves_the_builder_on_the_debt_path() -> None:
    book = _book()
    lot = book.grant_courtesy(uuid4(), 1_000 * MC, at=T0)
    after = lot.expires_at + timedelta(hours=1)
    book.sweep_expired(after)
    assert lot.sums["expiry"] == 1_000 * MC
    result = book.debit("trace:1", 200 * MC, at=after, effect_kind="trace")
    assert (result.debited_mc, result.debt_mc) == (0, 200 * MC)
    assert capacity_in_force_mc(book, after) == 0


# ---------------------------------------------------------------------------
# The service on SQLite.
# ---------------------------------------------------------------------------


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'courtesy.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    finally:
        await engine.dispose()


def _clock(instant: datetime) -> Callable[[], Awaitable[datetime]]:
    async def clock() -> datetime:
        return instant

    return clock


def _ledger(session: AsyncSession, at: datetime = T0) -> CreditLedger:
    return CreditLedger(session, reference_clock=_clock(at))


async def _builder(
    sessions: async_sessionmaker[AsyncSession],
    *,
    is_active: bool = True,
    status: str = "active",
) -> UUID:
    builder_id = uuid4()
    async with sessions() as session, session.begin():
        session.add(
            Builder(id=builder_id, name="Courtesy", is_active=is_active, status=status)
        )
    return builder_id


async def _grant(
    sessions: async_sessionmaker[AsyncSession],
    builder_id: UUID,
    *,
    credits: int = 10_000,
    key: str = "key-1",
    reason: str = "pilot account",
    at: datetime = T0,
) -> credit_courtesy.CourtesyGrantResult:
    async with sessions() as session, session.begin():
        return await credit_courtesy.grant(
            session,
            builder_id=builder_id,
            credits=credits,
            reason=reason,
            actor_user_id=ACTOR,
            idempotency_key=key,
            at=at,
            ledger=_ledger(session, at),
        )


async def _remove(
    sessions: async_sessionmaker[AsyncSession],
    lot_id: UUID,
    *,
    key: str,
    credits: int | None = None,
    remove_all: bool = False,
    reason: str = "pilot over",
    at: datetime = T0,
) -> credit_courtesy.CourtesyRemovalResult:
    async with sessions() as session, session.begin():
        return await credit_courtesy.remove(
            session,
            lot_id=lot_id,
            credits=credits,
            remove_all=remove_all,
            reason=reason,
            actor_user_id=ACTOR,
            idempotency_key=key,
            at=at,
            ledger=_ledger(session, at),
        )


async def _count(sessions: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _counts(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, ...]:
    return tuple(
        [
            await _count(sessions, model)
            for model in (
                CreditAccount,
                CreditEffect,
                CreditLot,
                CreditLedgerEntry,
                CreditCourtesyEvent,
            )
        ]
    )


ACTOR = UUID("a0000000-0000-4000-8000-0000000000aa")


async def test_the_service_grants_once_and_replays_the_same_request(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = await _builder(sessions)

    first = await _grant(sessions, builder_id)
    again = await _grant(sessions, builder_id, at=T0 + timedelta(minutes=3))

    assert not first.replayed and again.replayed
    assert (again.event_id, again.lot_id, again.granted_mc) == (
        first.event_id,
        first.lot_id,
        10_000 * MC,
    )
    assert again.expires_at == first.expires_at == courtesy_lot_expires_at(T0.date())
    assert again.occurred_at == first.occurred_at == T0
    async with sessions() as session:
        [lot] = (await session.execute(select(CreditLot))).scalars().all()
        [event] = (await session.execute(select(CreditCourtesyEvent))).scalars().all()
    assert lot.id == first.lot_id and lot.payment_id is None
    assert lot.grant_effect_id == courtesy_grant_effect_id(first.event_id)
    assert (event.id, event.event_kind, event.amount_mc) == (
        first.event_id,
        "grant",
        10_000 * MC,
    )
    assert event.actor_user_id == ACTOR and event.reason == "pilot account"
    assert event.request_digest == courtesy_request_digest(
        "grant",
        builder_id=builder_id,
        lot_id=None,
        body=grant_body(builder_id, 10_000, "pilot account"),
    )


async def test_a_reused_key_on_another_builder_body_or_lot_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = await _builder(sessions)
    other = await _builder(sessions)
    granted = await _grant(sessions, builder_id, key="grant-key")
    second = await _grant(sessions, builder_id, key="grant-key-2", credits=50)
    await _remove(sessions, granted.lot_id, key="removal-key", credits=10)
    before = await _counts(sessions)

    for attempt in (
        _grant(sessions, other, key="grant-key"),
        _grant(sessions, builder_id, key="grant-key", credits=10_001),
        _grant(sessions, builder_id, key="grant-key", reason="another reason"),
        _remove(sessions, second.lot_id, key="removal-key", credits=10),
        _remove(sessions, granted.lot_id, key="removal-key", credits=11),
        _remove(sessions, granted.lot_id, key="removal-key", remove_all=True),
        _remove(sessions, granted.lot_id, key="grant-key", credits=10),
    ):
        with pytest.raises(CourtesyIdempotencyKeyReused) as refusal:
            await attempt
        assert refusal.value.code == "idempotency_key_reused"

    assert await _counts(sessions) == before


async def test_an_inactive_builder_gets_no_grant_but_its_courtesy_can_be_removed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    active = await _builder(sessions)
    granted = await _grant(sessions, active)
    inactive = await _builder(sessions, is_active=False)
    suspended = await _builder(sessions, status="suspended")
    before = await _counts(sessions)

    for builder_id in (inactive, suspended):
        with pytest.raises(CourtesyBuilderInactive) as refusal:
            await _grant(sessions, builder_id, key=f"k-{builder_id}")
        assert refusal.value.code == "builder_inactive"
    with pytest.raises(CourtesyBuilderNotFound) as missing:
        await _grant(sessions, uuid4(), key="k-missing")
    assert missing.value.code == "builder_not_found"
    assert await _counts(sessions) == before

    async with sessions() as session, session.begin():
        builder = await session.get(Builder, active)
        assert builder is not None
        builder.is_active = False
    removed = await _remove(sessions, granted.lot_id, key="r-1", credits=6_000)
    assert removed.removed_mc == 6_000 * MC and not removed.replayed
    replay = await _remove(sessions, granted.lot_id, key="r-1", credits=6_000)
    assert replay.replayed and replay.event_id == removed.event_id


async def test_a_removal_of_a_paid_or_unknown_lot_is_lot_not_found(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = await _builder(sessions)
    async with sessions() as session, session.begin():
        ledger = _ledger(session)
        book = await ledger.open_book(builder_id, at=T0)
        topup = _topup(book, grant=1_000 * MC)
        await ledger.persist(book)
    before = await _counts(sessions)

    for lot_id in (topup, uuid4()):
        with pytest.raises(CourtesyLotNotFound) as refusal:
            await _remove(sessions, lot_id, key=f"r-{lot_id}", credits=1)
        assert refusal.value.code == "courtesy_lot_not_found"
    assert await _counts(sessions) == before


async def test_removal_refusals_write_nothing_and_the_ledger_matches_the_book(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = await _builder(sessions)
    granted = await _grant(sessions, builder_id, credits=10_000)
    await _remove(sessions, granted.lot_id, key="r-6000", credits=6_000)
    before = await _counts(sessions)
    with pytest.raises(CourtesyRemovalExceedsBalance):
        await _remove(sessions, granted.lot_id, key="r-big", credits=4_001)
    assert await _counts(sessions) == before
    await _remove(sessions, granted.lot_id, key="r-all", remove_all=True)
    with pytest.raises(CourtesyRemovalExceedsBalance):
        await _remove(sessions, granted.lot_id, key="r-again", remove_all=True)

    async with sessions() as session, session.begin():
        book = await _ledger(session).open_book(builder_id, at=T0)
        derived = await CreditRepository(session).derived_balance(builder_id)
        lot = book.lot(granted.lot_id)
        assert lot.balance_mc == derived.lots[granted.lot_id] == 0
        assert lot.sums["courtesy_removal"] == 10_000 * MC
        assert lot.consumed_mc == 0 and book.open_debt_mc == derived.debt_mc == 0
        assert capacity_in_force_mc(book, T0) == 0
        events = (
            await session.execute(
                select(
                    CreditCourtesyEvent.event_kind, CreditCourtesyEvent.amount_mc
                ).order_by(
                    CreditCourtesyEvent.occurred_at, CreditCourtesyEvent.amount_mc
                )
            )
        ).all()
    assert sorted(events) == sorted(
        [("grant", 10_000 * MC), ("removal", 6_000 * MC), ("removal", 4_000 * MC)]
    )


async def test_a_lost_key_race_rolls_its_savepoint_back_and_answers_by_digest(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer that loses ``uq_credit_courtesy_events_idempotency_key`` to
    another builder's writer (both past the look-ups) keeps none of its rows
    and answers ``idempotency_key_reused``; the PostgreSQL suite runs the
    real concurrent race."""

    winner = await _builder(sessions)
    loser = await _builder(sessions)
    await _grant(sessions, winner, key="raced")
    before = await _counts(sessions)
    original = CreditRepository.courtesy_event_by_key
    calls = 0

    async def blind_twice(self: CreditRepository, key: str) -> Any:
        nonlocal calls
        calls += 1
        if calls <= 2:  # the look-ups before and under the lock miss it
            return None
        return await original(self, key)

    monkeypatch.setattr(CreditRepository, "courtesy_event_by_key", blind_twice)

    async with sessions() as session, session.begin():
        with pytest.raises(CourtesyIdempotencyKeyReused):
            await credit_courtesy.grant(
                session,
                builder_id=loser,
                credits=10,
                reason="race",
                actor_user_id=ACTOR,
                idempotency_key="raced",
                at=T0,
                ledger=_ledger(session),
            )
        # The savepoint is gone; the outer transaction still works and holds
        # only the account row of the lock step (a route rolls a refusal
        # back; this test commits it to show nothing else is left).
        lots = await session.scalar(
            select(func.count())
            .select_from(CreditLot)
            .where(CreditLot.builder_id == loser)
        )
        assert lots == 0
    assert calls == 3
    accounts, *rest = await _counts(sessions)
    assert (accounts - 1, *rest) == before  # the committed account row only


async def test_a_ledger_clock_ahead_is_raised_for_the_route_with_nothing_written(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """DESIGN Decision 16: ``LedgerClockAhead`` reaches the caller (the route
    answers ``503 credit_clock_ahead``); the retry with the same key is safe."""

    builder_id = await _builder(sessions)
    ahead = T0 + timedelta(hours=1)
    granted = await _grant(sessions, builder_id, key="ahead", at=ahead)
    before = await _counts(sessions)

    for attempt in (
        _grant(sessions, builder_id, key="behind", at=T0),
        _remove(sessions, granted.lot_id, key="behind-r", credits=1, at=T0),
    ):
        with pytest.raises(credit_courtesy.LedgerClockAhead):
            await attempt
    assert await _counts(sessions) == before
    retried = await _grant(sessions, builder_id, key="behind", at=ahead)
    assert not retried.replayed


async def test_the_logs_never_carry_the_reason(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = await _builder(sessions)
    secret = "motivo-interno-nunca-logado"
    with structlog.testing.capture_logs() as records:
        granted = await _grant(sessions, builder_id, reason=secret)
        await _remove(sessions, granted.lot_id, key="r", credits=1, reason=secret)
        with pytest.raises(CourtesyRemovalExceedsBalance):
            await _remove(
                sessions, granted.lot_id, key="r2", credits=10_000, reason=secret
            )
        with pytest.raises(CourtesyIdempotencyKeyReused):
            await _grant(sessions, builder_id, reason=secret + "!")

    events = [record["event"] for record in records]
    # Pre-registered guards: each log was emitted, so the absence is not vacuous.
    assert "credit courtesy granted" in events
    assert "credit courtesy removed" in events
    assert "credit courtesy removal refused: exceeds the lot's balance" in events
    assert "credit courtesy refused: idempotency key reused" in events
    granted_log = next(r for r in records if r["event"] == "credit courtesy granted")
    assert granted_log["builder_id"] == str(builder_id)
    assert granted_log["amount_mc"] == 10_000 * MC
    assert granted_log["actor_user_id"] == str(ACTOR)
    assert secret not in repr(records)
    assert all("reason" not in record for record in records)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"credits": 0}, "positive whole number"),
        ({"credits": 100_001}, "must not exceed 100000"),
        ({"credits": 2.0}, "positive whole number"),
        ({"reason": ""}, "reason must have 1 to 500"),
        ({"reason": "x" * 501}, "reason must have 1 to 500"),
        ({"reason": "a\x00b"}, "reason must not contain NUL"),
        ({"idempotency_key": "k\x00"}, "idempotency_key must not contain NUL"),
        ({"idempotency_key": ""}, "idempotency_key must have 1 to 200"),
        ({"idempotency_key": "k" * 201}, "idempotency_key must have 1 to 200"),
    ],
)
async def test_grant_arguments_outside_the_contract_raise_before_any_read(
    kwargs: dict[str, Any], message: str
) -> None:
    arguments: dict[str, Any] = {
        "builder_id": uuid4(),
        "credits": 1,
        "reason": "r",
        "actor_user_id": ACTOR,
        "idempotency_key": "k",
    }
    arguments.update(kwargs)
    with pytest.raises(ValueError, match=message):
        await credit_courtesy.grant(object(), **arguments)  # type: ignore[arg-type]


async def test_a_removal_reason_or_key_with_nul_raises_before_any_read() -> None:
    """PostgreSQL text cannot hold NUL: the service refuses it as a
    ``ValueError`` (the route's 422), never a database error (a 500)."""

    for kwargs, message in (
        ({"reason": "fim\x00"}, "reason must not contain NUL"),
        ({"idempotency_key": "\x00"}, "idempotency_key must not contain NUL"),
    ):
        arguments: dict[str, Any] = {
            "lot_id": uuid4(),
            "credits": 1,
            "reason": "r",
            "actor_user_id": ACTOR,
            "idempotency_key": "k",
        }
        arguments.update(kwargs)
        with pytest.raises(ValueError, match=message):
            await credit_courtesy.remove(object(), **arguments)  # type: ignore[arg-type]


async def test_a_ledger_bound_to_another_session_is_refused(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = await _builder(sessions)
    granted = await _grant(sessions, builder_id)
    before = await _counts(sessions)

    async with sessions() as session, sessions() as other:
        foreign = _ledger(other)
        with pytest.raises(ValueError, match="bound to the given session"):
            await credit_courtesy.grant(
                session,
                builder_id=builder_id,
                credits=1,
                reason="r",
                actor_user_id=ACTOR,
                idempotency_key="foreign-grant",
                at=T0,
                ledger=foreign,
            )
        with pytest.raises(ValueError, match="bound to the given session"):
            await credit_courtesy.remove(
                session,
                lot_id=granted.lot_id,
                credits=1,
                reason="r",
                actor_user_id=ACTOR,
                idempotency_key="foreign-removal",
                at=T0,
                ledger=foreign,
            )
    assert await _counts(sessions) == before


async def test_grant_and_removal_are_allowed_under_an_enforced_quarantine_hold(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DESIGN Decision 15: the hold refuses spend, never a courtesy grant or
    removal.  The hold is made ACTIVE (quarantined and enforced), and the
    pre-registered guard shows the book really carried it."""

    builder_id = await _builder(sessions)
    hold_reads: list[UUID] = []

    async def quarantined(self: CreditRepository, builder: UUID) -> bool:
        hold_reads.append(builder)
        return True

    async def enforced(self: CreditRepository) -> datetime:
        return T0 - timedelta(days=30)

    monkeypatch.setattr(CreditRepository, "quarantine_hold", quarantined)
    monkeypatch.setattr(CreditRepository, "enforcement_at", enforced)

    async with sessions() as session, session.begin():
        assert (await _ledger(session).spend_hold(builder_id)).active
    hold_reads.clear()

    granted = await _grant(sessions, builder_id, credits=100, key="held-grant")
    assert not granted.replayed and granted.granted_mc == 100 * MC
    removed = await _remove(sessions, granted.lot_id, key="held-removal", credits=40)
    assert not removed.replayed and removed.removed_mc == 40 * MC
    assert hold_reads == [builder_id, builder_id]  # each write read the hold

    async with sessions() as session:
        [lot] = (await session.execute(select(CreditLot))).scalars().all()
        kinds = sorted(
            (await session.execute(select(CreditCourtesyEvent.event_kind)))
            .scalars()
            .all()
        )
    assert lot.id == granted.lot_id and kinds == ["grant", "removal"]


def test_the_digest_covers_operation_builder_lot_and_body() -> None:
    builder, lot = uuid4(), uuid4()
    base = courtesy_request_digest(
        "removal",
        builder_id=builder,
        lot_id=lot,
        body=removal_body(credits=5, remove_all=False, reason="r"),
    )
    assert len(base) == 64 and base == courtesy_request_digest(
        "removal",
        builder_id=builder,
        lot_id=lot,
        body={"reason": "r", "credits": 5},
    )
    variants = {
        courtesy_request_digest(
            "grant",
            builder_id=builder,
            lot_id=lot,
            body=removal_body(credits=5, remove_all=False, reason="r"),
        ),
        courtesy_request_digest(
            "removal",
            builder_id=uuid4(),
            lot_id=lot,
            body=removal_body(credits=5, remove_all=False, reason="r"),
        ),
        courtesy_request_digest(
            "removal",
            builder_id=builder,
            lot_id=uuid4(),
            body=removal_body(credits=5, remove_all=False, reason="r"),
        ),
        courtesy_request_digest(
            "removal",
            builder_id=builder,
            lot_id=lot,
            body=removal_body(credits=None, remove_all=True, reason="r"),
        ),
        courtesy_request_digest(
            "removal",
            builder_id=builder,
            lot_id=lot,
            body=removal_body(credits=5, remove_all=False, reason="r2"),
        ),
    }
    assert base not in variants and len(variants) == 5


# ---------------------------------------------------------------------------
# The 046 gate (wish Decision 6).
# ---------------------------------------------------------------------------


def test_the_courtesy_minimum_is_046_and_the_credit_minimum_stays_042() -> None:
    assert COURTESY_SCHEMA_MIN_REVISION == "046"
    assert CREDIT_SCHEMA_MIN_REVISION == "042"
    assert load_image_migration_chain()[-1] == "046"


@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        (CreditSwitchReading(True, "schema_ready", "045"), False),
        (CreditSwitchReading(True, "schema_ready", "042"), False),
        (CreditSwitchReading(True, "schema_ready", "046"), True),
        (CreditSwitchReading(True, "schema_ready", None), False),
        (CreditSwitchReading(True, "schema_ready", ""), False),
        (CreditSwitchReading(True, "schema_ready", "999"), False),
        (CreditSwitchReading(False, "config_disabled"), False),
        (CreditSwitchReading(False, "not_read"), False),
        (CreditSwitchReading(False, "read_error", "046"), False),
    ],
)
def test_the_gate_opens_only_at_046_or_later(
    reading: CreditSwitchReading, expected: bool
) -> None:
    assert courtesy_gate_open(reading) is expected


@pytest.mark.parametrize(
    "error",
    [ImageMigrationChainError("no chain"), RuntimeError("boom"), KeyError("x")],
)
def test_any_exception_closes_the_gate_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> tuple[str, ...]:
        raise error

    monkeypatch.setattr(credit_switch, "load_image_migration_chain", broken)
    with structlog.testing.capture_logs() as records:
        assert (
            courtesy_gate_open(CreditSwitchReading(True, "schema_ready", "046"))
            is False
        )
    [record] = [r for r in records if r["event"].startswith("credit courtesy gate")]
    assert record["error_type"] == type(error).__name__

    class Hostile:
        @property
        def effective(self) -> bool:
            raise error

    assert courtesy_gate_open(Hostile()) is False  # type: ignore[arg-type]


def test_the_gate_captures_nothing_beyond_the_reading_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One capture per request: the gate never reads the switch itself."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the gate read the switch on its own")

    monkeypatch.setattr(credit_switch, "credit_switch_reading", refuse)
    monkeypatch.setattr(credit_switch, "credit_switch_effective", refuse)
    monkeypatch.setattr(CreditSwitch, "reading", refuse)
    monkeypatch.setattr(CreditSwitch, "effective", refuse)
    assert courtesy_gate_open(CreditSwitchReading(True, "schema_ready", "046")) is True
    assert courtesy_gate_open(CreditSwitchReading(True, "schema_ready", "045")) is False


async def test_the_gate_follows_the_forced_plugin(
    request: pytest.FixtureRequest,
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ``-p tests.credit_switch_forced_plugin`` the forced reading opens
    the gate; without it, the real switch reading SQLite (``create_all``, no
    ``alembic_version``) keeps it closed."""

    monkeypatch.setattr(
        credit_switch._SWITCH, "_reading", credit_switch._SWITCH._reading
    )
    published = await credit_switch.refresh_credit_switch(
        enabled=True, session_factory=sessions
    )
    assert published == CreditSwitchReading(False, "read_error")
    forced = request.config.pluginmanager.has_plugin(
        "tests.credit_switch_forced_plugin"
    )
    reading = credit_switch_reading()
    if forced:
        head = load_image_migration_chain()[-1]
        assert reading == CreditSwitchReading(True, "schema_ready", head)
        assert courtesy_gate_open(reading) is True
    else:
        assert reading == published
        assert courtesy_gate_open(reading) is False
