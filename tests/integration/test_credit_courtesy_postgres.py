"""Courtesy credits on real PostgreSQL (creditos-cortesia Group 2).

What SQLite cannot show, through :mod:`src.services.credit_courtesy` on a
database migrated to head (047), with ``credit_enforcement_at`` set (the
lifecycle evaluation runs after every write, as in production after L10):

* "Capacidade": 10,000 granted and 6,000 removed is ``used_mc`` 0 and no
  alert; 1,000 consumed after that is 25 % (1,000 of 4,000);
* "Retirada": the fraction 4,999,400 mc (4,999 leaves 400, ``remove_all``
  leaves 0); a removal above the balance, or ``remove_all`` of an emptied or
  expired lot, is ``courtesy_removal_exceeds_balance`` with nothing written;
  ``remove_all`` of an enforced builder with no other source opens the
  exhaustion episode;
* "Em modo enforced": FEFO monthly -> courtesy -> a later top-up; a grant
  settles open debt first; after the courtesy expires, a builder with no
  other source follows the existing debt path and its 72 h suspension;
* idempotency: the same key and request is one lot; a key reused on another
  lot, builder or body is ``idempotency_key_reused``; concurrent writers of
  one key -- the same builder (serialized by the account lock) and two
  builders (racing to ``uq_credit_courtesy_events_idempotency_key``, the
  loser's savepoint rolled back) -- leave one lot and raise nothing
  unhandled;
* an inactive builder gets no grant (no row at all, not even an account) and
  its courtesy can still be removed;
* grant and removal write no ``billing_*`` row (every ``billing_*`` table
  counted before and after);
* every row the service writes satisfies the 046 CHECKs and the append-only
  trigger of ``credit_courtesy_events``.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.api.routes.credits import _used_percent as route_used_percent
from src.models.builder import Builder
from src.repositories.credit import CreditRepository
from src.services import credit_courtesy
from src.services.credit_courtesy import (
    CourtesyBuilderInactive,
    CourtesyGrantResult,
    CourtesyIdempotencyKeyReused,
    CourtesyRemovalResult,
)
from src.services.credit_ledger import (
    CourtesyRemovalExceedsBalance,
    CreditLedger,
    GrantSpec,
    LedgerBook,
    courtesy_lot_expires_at,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_lifecycle import DEBT_GRACE, capacity_in_force_mc
from tests.integration.test_asaas_billing_postgres import (
    _sqlstate,
    _validated_admin_url,
)
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

MC = 1_000
ENFORCED_AT = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
ACTOR = UUID("a0000000-0000-4000-8000-0000000000aa")
_WAIT = 15.0


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    def session(self) -> AsyncSession:
        return self.sessions()


@pytest.fixture(scope="module")
async def postgres_harness() -> AsyncIterator[_Harness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    engine: AsyncEngine | None = None
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        engine = create_async_engine(cluster.url(name), poolclass=NullPool)
        async with engine.connect() as connection:
            version = (
                await connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
            ).scalar_one()
        assert version == "047"
        yield _Harness(
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
        )
    finally:
        if engine is not None:
            await engine.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


@pytest.fixture
async def enforced(postgres_harness: _Harness) -> AsyncIterator[None]:
    """``credit_enforcement_at`` set before every instant these tests use."""

    await _run(
        postgres_harness,
        "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, set_by, "
        "set_at) VALUES (1, :at, 'user:courtesy-test', :at)",
        at=ENFORCED_AT,
    )
    try:
        yield
    finally:
        await _run(postgres_harness, "DELETE FROM credit_enforcement_state")


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


async def _run(harness: _Harness, statement: str, **params: Any) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(text(statement), params)


async def _value(harness: _Harness, statement: str, **params: Any) -> Any:
    async with harness.engine.connect() as connection:
        return (await connection.execute(text(statement), params)).scalar()


async def _builder(harness: _Harness, *, is_active: bool = True) -> UUID:
    builder_id = uuid4()
    async with harness.session() as session, session.begin():
        session.add(Builder(id=builder_id, name="Courtesy", is_active=is_active))
    return builder_id


def _ledger(session: AsyncSession, at: datetime) -> CreditLedger:
    """An adapter whose database clock reads the simulated ``at``."""

    async def clock() -> datetime:
        return at

    return CreditLedger(session, reference_clock=clock)


async def _grant(
    harness: _Harness,
    builder_id: UUID,
    *,
    credits: int = 10_000,
    key: str | None = None,
    reason: str = "conta piloto",
    at: datetime = T0,
) -> CourtesyGrantResult:
    async with harness.session() as session, session.begin():
        return await credit_courtesy.grant(
            session,
            builder_id=builder_id,
            credits=credits,
            reason=reason,
            actor_user_id=ACTOR,
            idempotency_key=key or f"grant-{uuid4()}",
            at=at,
            ledger=_ledger(session, at),
        )


async def _remove(
    harness: _Harness,
    lot_id: UUID,
    *,
    credits: int | None = None,
    remove_all: bool = False,
    key: str | None = None,
    reason: str = "fim do piloto",
    at: datetime = T0,
) -> CourtesyRemovalResult:
    async with harness.session() as session, session.begin():
        return await credit_courtesy.remove(
            session,
            lot_id=lot_id,
            credits=credits,
            remove_all=remove_all,
            reason=reason,
            actor_user_id=ACTOR,
            idempotency_key=key or f"removal-{uuid4()}",
            at=at,
            ledger=_ledger(session, at),
        )


async def _book(harness: _Harness, builder_id: UUID, at: datetime) -> LedgerBook:
    """A freshly loaded, swept book (read only: rolled back)."""

    async with harness.session() as session:
        book = await _ledger(session, at).open_book(builder_id, at=at)
        await session.rollback()
        return book


async def _debit(
    harness: _Harness, builder_id: UUID, amount_mc: int, at: datetime
) -> tuple[int, int]:
    """A trace debit through the book (the consumption rules under test),
    persisted with the lifecycle evaluation."""

    async with harness.session() as session, session.begin():
        ledger = _ledger(session, at)
        book = await ledger.open_book(builder_id, at=at)
        result = book.debit(f"trace:{uuid4()}", amount_mc, at=at, effect_kind="trace")
        assert result.status == "applied", result
        await ledger.persist(book)
        return result.debited_mc, result.debt_mc


async def _grant_paid(
    harness: _Harness,
    builder_id: UUID,
    *,
    monthly_mc: int,
    topup_mc: int,
    cycle: tuple[date, date] = (date(2026, 11, 1), date(2026, 12, 1)),
) -> tuple[UUID, UUID]:
    subscription = uuid4()
    start, end = cycle
    order = uuid4()
    async with harness.session() as session, session.begin():
        ledger = _ledger(session, T0)
        book = await ledger.open_book(builder_id, at=T0)
        monthly = book.grant(
            GrantSpec(
                lot_kind="monthly",
                effect_id=monthly_grant_effect_id(subscription, start, end),
                payment_id="pay-monthly",
                paid_at=start,
                granted_mc=monthly_mc,
                expires_at=monthly_lot_expires_at(end),
                subscription_id=subscription,
                cycle_start=start,
                cycle_end=end,
            ),
            at=T0,
        ).id
        topup = book.grant(
            GrantSpec(
                lot_kind="topup",
                effect_id=topup_grant_effect_id(order),
                payment_id="pay-topup",
                paid_at=T0.date(),
                granted_mc=topup_mc,
                expires_at=topup_lot_expires_at(T0.date()),
                order_id=order,
            ),
            at=T0,
        ).id
        await ledger.persist(book)
    return monthly, topup


async def _rows(harness: _Harness, builder_id: UUID) -> dict[str, int]:
    counts = {}
    for table in (
        "credit_accounts",
        "credit_effects",
        "credit_lots",
        "credit_ledger_entries",
        "credit_debts",
        "credit_debt_settlements",
        "credit_courtesy_events",
    ):
        counts[table] = int(
            await _value(
                harness,
                f"SELECT count(*) FROM {table} WHERE builder_id = :builder",
                builder=builder_id,
            )
        )
    return counts


async def _billing_counts(harness: _Harness) -> dict[str, int]:
    async with harness.engine.connect() as connection:
        tables = [
            str(name)
            for (name,) in await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'billing\\_%' "
                    "ORDER BY table_name"
                )
            )
        ]
        counts = {}
        for table in tables:
            counts[table] = int(
                (
                    await connection.execute(text(f'SELECT count(*) FROM "{table}"'))
                ).scalar_one()
            )
    return counts


async def _episodes(harness: _Harness, builder_id: UUID) -> list[Any]:
    async with harness.engine.connect() as connection:
        return list(
            await connection.execute(
                text(
                    "SELECT id, exhausted_at, suspension_due_at FROM "
                    "credit_lifecycle_episodes WHERE builder_id = :builder "
                    "ORDER BY exhausted_at"
                ),
                {"builder": builder_id},
            )
        )


async def _alerts(harness: _Harness, builder_id: UUID) -> list[int]:
    async with harness.engine.connect() as connection:
        return [
            int(threshold)
            for (threshold,) in await connection.execute(
                text(
                    "SELECT threshold FROM credit_threshold_alerts "
                    "WHERE builder_id = :builder ORDER BY threshold"
                ),
                {"builder": builder_id},
            )
        ]


def _used(book: LedgerBook, at: datetime) -> tuple[int, int, int]:
    """``capacity_mc``, ``used_mc`` and ``used_percent`` as
    ``/v1/credits/thresholds`` computes them."""

    capacity = capacity_in_force_mc(book, at)
    available = book.available_trace_mc(at)
    used = max(capacity - available, 0)
    return capacity, used, route_used_percent(capacity, available, used)


async def _wait_for_lock_waiter(harness: _Harness) -> None:
    """Until some backend of this database waits on a lock."""

    deadline = asyncio.get_running_loop().time() + _WAIT
    while asyncio.get_running_loop().time() < deadline:
        waiting = await _value(
            harness,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock'",
        )
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("no contender ever waited on a lock")


# ---------------------------------------------------------------------------
# Capacity and removal.
# ---------------------------------------------------------------------------


async def test_a_removal_is_funding_removed_and_consumption_alone_is_usage(
    postgres_harness: _Harness, enforced: None
) -> None:
    builder_id = await _builder(postgres_harness)
    granted = await _grant(postgres_harness, builder_id, credits=10_000)
    assert granted.granted_mc == 10_000 * MC
    assert granted.expires_at == courtesy_lot_expires_at(date(2026, 11, 9))

    removed = await _remove(
        postgres_harness, granted.lot_id, credits=6_000, at=T0 + timedelta(minutes=1)
    )
    assert removed.removed_mc == 6_000 * MC

    at = T0 + timedelta(minutes=2)
    book = await _book(postgres_harness, builder_id, at)
    assert _used(book, at) == (4_000 * MC, 0, 0)
    lot = book.lot(granted.lot_id)
    assert (lot.available_mc(at), lot.consumed_mc, lot.sums["courtesy_removal"]) == (
        4_000 * MC,
        0,
        6_000 * MC,
    )
    assert await _alerts(postgres_harness, builder_id) == []

    assert await _debit(postgres_harness, builder_id, 1_000 * MC, at) == (
        1_000 * MC,
        0,
    )
    book = await _book(postgres_harness, builder_id, at + timedelta(minutes=1))
    assert _used(book, at + timedelta(minutes=1)) == (4_000 * MC, 1_000 * MC, 25)
    assert await _alerts(postgres_harness, builder_id) == []
    assert await _episodes(postgres_harness, builder_id) == []

    # The SQL-derived balance agrees with the book (courtesy_removal is a
    # lot decrease in LOT_DECREASES).
    lots = await _value(
        postgres_harness,
        "SELECT sum(CASE WHEN entry_type IN ('grant', 'settlement_release', "
        "'lot_regrant') THEN amount_mc ELSE -amount_mc END) FROM "
        "credit_ledger_entries WHERE lot_id = :lot",
        lot=granted.lot_id,
    )
    assert int(lots) == 3_000 * MC


async def test_the_fraction_and_the_refusals_of_an_emptied_or_expired_lot(
    postgres_harness: _Harness, enforced: None
) -> None:
    builder_id = await _builder(postgres_harness)
    granted = await _grant(postgres_harness, builder_id, credits=5_000)
    await _debit(postgres_harness, builder_id, 600, T0)
    assert (await _book(postgres_harness, builder_id, T0)).lot(
        granted.lot_id
    ).available_mc(T0) == 4_999_400

    before = await _rows(postgres_harness, builder_id)
    with pytest.raises(CourtesyRemovalExceedsBalance) as refusal:
        await _remove(postgres_harness, granted.lot_id, credits=5_000)
    assert refusal.value.code == "courtesy_removal_exceeds_balance"
    assert await _rows(postgres_harness, builder_id) == before

    assert (
        await _remove(postgres_harness, granted.lot_id, credits=4_999)
    ).removed_mc == 4_999_000
    assert (await _book(postgres_harness, builder_id, T0)).lot(
        granted.lot_id
    ).available_mc(T0) == 400
    assert (
        await _remove(postgres_harness, granted.lot_id, remove_all=True)
    ).removed_mc == 400
    before = await _rows(postgres_harness, builder_id)
    with pytest.raises(CourtesyRemovalExceedsBalance):
        await _remove(postgres_harness, granted.lot_id, remove_all=True)
    assert await _rows(postgres_harness, builder_id) == before

    expiring = await _grant(postgres_harness, builder_id, credits=10)
    later = expiring.expires_at + timedelta(hours=1)
    with pytest.raises(CourtesyRemovalExceedsBalance):
        await _remove(postgres_harness, expiring.lot_id, remove_all=True, at=later)
    # Nothing at all was written, not even the expiry the sweep computed.
    assert (await _rows(postgres_harness, builder_id))["credit_ledger_entries"] == (
        before["credit_ledger_entries"] + 1
    )


async def test_remove_all_of_the_only_source_opens_the_exhaustion_episode(
    postgres_harness: _Harness, enforced: None
) -> None:
    builder_id = await _builder(postgres_harness)
    granted = await _grant(postgres_harness, builder_id, credits=100)
    assert await _episodes(postgres_harness, builder_id) == []

    at = T0 + timedelta(minutes=5)
    removed = await _remove(postgres_harness, granted.lot_id, remove_all=True, at=at)

    assert removed.removed_mc == 100 * MC
    [episode] = await _episodes(postgres_harness, builder_id)
    assert episode.exhausted_at == at
    assert episode.suspension_due_at == at + DEBT_GRACE
    assert (await _book(postgres_harness, builder_id, at)).open_debt_mc == 0


# ---------------------------------------------------------------------------
# Enforced consumption, debt and expiry.
# ---------------------------------------------------------------------------


async def test_fefo_spends_monthly_then_courtesy_then_the_later_topup(
    postgres_harness: _Harness, enforced: None
) -> None:
    """The started monthly (cycle 11-05 -> 12-20) expires AFTER the courtesy
    and still drains first: "monthly first", not "earliest expiry first"."""

    builder_id = await _builder(postgres_harness)
    monthly, topup = await _grant_paid(
        postgres_harness,
        builder_id,
        monthly_mc=1_000 * MC,
        topup_mc=1_000 * MC,
        cycle=(date(2026, 11, 5), date(2026, 12, 20)),
    )
    courtesy = (await _grant(postgres_harness, builder_id, credits=1_000)).lot_id

    at = T0 + timedelta(minutes=1)
    book = await _book(postgres_harness, builder_id, at)
    assert book.lot(courtesy).expires_at < book.lot(monthly).expires_at
    assert book.lot(courtesy).expires_at < book.lot(topup).expires_at
    await _debit(postgres_harness, builder_id, 1_500 * MC, at)
    book = await _book(postgres_harness, builder_id, at)
    assert [book.lot(lot).available_mc(at) for lot in (monthly, courtesy, topup)] == [
        0,
        500 * MC,
        1_000 * MC,
    ]
    await _debit(postgres_harness, builder_id, 1_000 * MC, at)
    book = await _book(postgres_harness, builder_id, at)
    assert [book.lot(lot).available_mc(at) for lot in (monthly, courtesy, topup)] == [
        0,
        0,
        500 * MC,
    ]


async def test_a_grant_settles_open_debt_first(
    postgres_harness: _Harness, enforced: None
) -> None:
    builder_id = await _builder(postgres_harness)
    assert await _debit(postgres_harness, builder_id, 300 * MC, T0) == (0, 300 * MC)
    [opened] = await _episodes(postgres_harness, builder_id)

    granted = await _grant(
        postgres_harness, builder_id, credits=1_000, at=T0 + timedelta(minutes=1)
    )

    book = await _book(postgres_harness, builder_id, T0 + timedelta(minutes=1))
    assert book.open_debt_mc == 0
    assert book.lot(granted.lot_id).available_mc(T0 + timedelta(minutes=1)) == (
        700 * MC
    )
    settled = await _value(
        postgres_harness,
        "SELECT sum(amount_mc) FROM credit_debt_settlements "
        "WHERE builder_id = :builder AND settling_grant_lot_id = :lot",
        builder=builder_id,
        lot=granted.lot_id,
    )
    assert int(settled) == 300 * MC
    # Debt paid and capacity positive: the episode closed on the grant.
    reactivated = await _value(
        postgres_harness,
        "SELECT reactivated_at FROM credit_lifecycle_episodes WHERE id = :id",
        id=opened.id,
    )
    assert reactivated == T0 + timedelta(minutes=1)


async def test_after_the_courtesy_expires_the_builder_follows_the_72h_debt_path(
    postgres_harness: _Harness, enforced: None
) -> None:
    builder_id = await _builder(postgres_harness)
    granted = await _grant(postgres_harness, builder_id, credits=1_000)
    await _debit(postgres_harness, builder_id, 400 * MC, T0)

    after = granted.expires_at + timedelta(hours=2)
    assert await _debit(postgres_harness, builder_id, 50 * MC, after) == (0, 50 * MC)

    expired = await _value(
        postgres_harness,
        "SELECT amount_mc FROM credit_ledger_entries WHERE lot_id = :lot "
        "AND entry_type = 'expiry'",
        lot=granted.lot_id,
    )
    assert int(expired) == 600 * MC
    [episode] = await _episodes(postgres_harness, builder_id)
    assert episode.exhausted_at == after
    assert episode.suspension_due_at == after + DEBT_GRACE

    # Past the 72 h deadline the existing transition suspends the builder.
    due = after + DEBT_GRACE + timedelta(minutes=1)
    async with postgres_harness.session() as session, session.begin():
        ledger = _ledger(session, due)
        book = await ledger.open_book(builder_id, at=due)
        await ledger.persist(book, lifecycle_source="worker")
        assert ledger.lifecycle_outcome.transitions == ["suspended"]
        assert ledger.lifecycle_outcome.state == "suspended"


# ---------------------------------------------------------------------------
# Idempotency.
# ---------------------------------------------------------------------------


async def test_same_key_same_request_is_one_lot_and_reuse_writes_nothing(
    postgres_harness: _Harness, enforced: None
) -> None:
    builder_id = await _builder(postgres_harness)
    other = await _builder(postgres_harness)
    key = f"k-{uuid4()}"
    first = await _grant(postgres_harness, builder_id, key=key)
    replay = await _grant(postgres_harness, builder_id, key=key, at=T0 + timedelta(1))
    assert replay.replayed and replay.lot_id == first.lot_id
    second = await _grant(postgres_harness, builder_id, credits=10)
    removal_key = f"r-{uuid4()}"
    await _remove(postgres_harness, first.lot_id, credits=1, key=removal_key)
    before = (
        await _rows(postgres_harness, builder_id),
        await _rows(postgres_harness, other),
    )

    for attempt in (
        _grant(postgres_harness, other, key=key),
        _grant(postgres_harness, builder_id, key=key, credits=9_999),
        _grant(postgres_harness, builder_id, key=key, reason="outro motivo"),
        _remove(postgres_harness, second.lot_id, credits=1, key=removal_key),
        _remove(postgres_harness, first.lot_id, credits=2, key=removal_key),
        _remove(postgres_harness, first.lot_id, credits=1, key=key),
    ):
        with pytest.raises(CourtesyIdempotencyKeyReused):
            await attempt
    after = (
        await _rows(postgres_harness, builder_id),
        await _rows(postgres_harness, other),
    )
    assert after == before
    assert before[1]["credit_accounts"] == 0  # refused before the lock step


async def test_concurrent_same_builder_writers_of_one_key_make_one_lot(
    postgres_harness: _Harness, enforced: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id = await _builder(postgres_harness)
    # The account exists, so the contender waits on the account lock itself.
    earlier = await _grant(postgres_harness, builder_id, credits=1)
    inserts: list[bool] = []
    original = CreditRepository.insert_courtesy_event

    async def spy(self: CreditRepository, **values: Any) -> bool:
        inserted = await original(self, **values)
        inserts.append(inserted)
        return inserted

    monkeypatch.setattr(CreditRepository, "insert_courtesy_event", spy)
    key = f"k-{uuid4()}"
    async with postgres_harness.session() as holder:
        async with holder.begin():
            first = await credit_courtesy.grant(
                holder,
                builder_id=builder_id,
                credits=500,
                reason="corrida",
                actor_user_id=ACTOR,
                idempotency_key=key,
                at=T0,
                ledger=_ledger(holder, T0),
            )
            contender = asyncio.create_task(
                _grant(
                    postgres_harness, builder_id, credits=500, key=key, reason="corrida"
                )
            )
            await _wait_for_lock_waiter(postgres_harness)  # on the account lock
            assert not contender.done()
        second = await asyncio.wait_for(contender, _WAIT)

    # Pre-registered: the contender never reached the insert; it answered
    # from the re-read under the account lock (not from a lost unique key).
    assert inserts == [True]
    assert second.replayed and second.lot_id == first.lot_id != earlier.lot_id
    rows = await _rows(postgres_harness, builder_id)
    assert (rows["credit_lots"], rows["credit_courtesy_events"]) == (2, 2)

    # And unordered: four tasks, one key.
    racing = await _builder(postgres_harness)
    key = f"k-{uuid4()}"
    results = await asyncio.gather(
        *[_grant(postgres_harness, racing, credits=7, key=key) for _ in range(4)]
    )
    assert len({result.lot_id for result in results}) == 1
    assert sorted(result.replayed for result in results) == [False, True, True, True]
    rows = await _rows(postgres_harness, racing)
    assert (rows["credit_lots"], rows["credit_courtesy_events"]) == (1, 1)


async def test_concurrent_remove_all_of_one_lot_with_different_keys(
    postgres_harness: _Harness, enforced: None
) -> None:
    """Two ``remove_all`` of one lot under different keys: the account lock
    serializes them, the second judges the lot after the first committed and
    is refused ``courtesy_removal_exceeds_balance``; the lot ends at 0."""

    builder_id = await _builder(postgres_harness)
    granted = await _grant(postgres_harness, builder_id, credits=300)
    at = T0 + timedelta(minutes=1)
    async with postgres_harness.session() as holder:
        async with holder.begin():
            first = await credit_courtesy.remove(
                holder,
                lot_id=granted.lot_id,
                remove_all=True,
                reason="fim do piloto",
                actor_user_id=ACTOR,
                idempotency_key=f"r-{uuid4()}",
                at=at,
                ledger=_ledger(holder, at),
            )
            contender = asyncio.create_task(
                _remove(postgres_harness, granted.lot_id, remove_all=True, at=at)
            )
            await _wait_for_lock_waiter(postgres_harness)  # on the account lock
            assert not contender.done()
        with pytest.raises(CourtesyRemovalExceedsBalance) as refusal:
            await asyncio.wait_for(contender, _WAIT)

    assert first.removed_mc == 300 * MC and not first.replayed
    assert refusal.value.code == "courtesy_removal_exceeds_balance"
    assert (refusal.value.requested_mc, refusal.value.spendable_mc) == (None, 0)
    book = await _book(postgres_harness, builder_id, at)
    assert book.lot(granted.lot_id).available_mc(at) == 0
    assert book.lot(granted.lot_id).sums["courtesy_removal"] == 300 * MC
    rows = await _rows(postgres_harness, builder_id)
    assert (rows["credit_lots"], rows["credit_courtesy_events"]) == (1, 2)


async def test_two_builders_racing_to_one_key_leave_one_lot_and_no_error(
    postgres_harness: _Harness, enforced: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different builders hold different account locks: both pass the
    look-ups, the second waits on the first's uncommitted unique key, loses
    it, rolls its savepoint back and answers ``idempotency_key_reused``."""

    inserts: list[bool] = []
    original = CreditRepository.insert_courtesy_event

    async def spy(self: CreditRepository, **values: Any) -> bool:
        inserted = await original(self, **values)
        inserts.append(inserted)
        return inserted

    monkeypatch.setattr(CreditRepository, "insert_courtesy_event", spy)
    winner = await _builder(postgres_harness)
    loser = await _builder(postgres_harness)
    key = f"k-{uuid4()}"
    async with postgres_harness.session() as holder:
        async with holder.begin():
            await credit_courtesy.grant(
                holder,
                builder_id=winner,
                credits=500,
                reason="corrida",
                actor_user_id=ACTOR,
                idempotency_key=key,
                at=T0,
                ledger=_ledger(holder, T0),
            )
            contender = asyncio.create_task(
                _grant(postgres_harness, loser, credits=500, key=key, reason="corrida")
            )
            await _wait_for_lock_waiter(postgres_harness)  # on the unique key
            assert not contender.done()
        with pytest.raises(CourtesyIdempotencyKeyReused):
            await asyncio.wait_for(contender, _WAIT)

    # Pre-registered: the loser really reached the insert and lost it there
    # (not a look-up refusal), so the savepoint path is what ran.
    assert inserts == [True, False]
    assert (await _rows(postgres_harness, winner))["credit_lots"] == 1
    lost = await _rows(postgres_harness, loser)
    assert lost == {table: 0 for table in lost}  # the caller rolled back
    assert (
        int(
            await _value(
                postgres_harness,
                "SELECT count(*) FROM credit_courtesy_events WHERE idempotency_key = :k",
                k=key,
            )
        )
        == 1
    )


async def test_the_unique_key_the_race_relies_on_is_enforced(
    postgres_harness: _Harness,
) -> None:
    builder_id = await _builder(postgres_harness)
    granted = await _grant(postgres_harness, builder_id, credits=1)
    key = await _value(
        postgres_harness,
        "SELECT idempotency_key FROM credit_courtesy_events WHERE id = :id",
        id=granted.event_id,
    )
    with pytest.raises(
        DBAPIError, match=re.escape("uq_credit_courtesy_events_idempotency_key")
    ) as info:
        await _run(
            postgres_harness,
            "INSERT INTO credit_courtesy_events (id, builder_id, lot_id, event_kind, "
            "amount_mc, reason, actor_user_id, idempotency_key, request_digest, "
            "occurred_at) VALUES (:id, :builder, :lot, 'removal', 1, 'r', :actor, "
            ":key, :digest, now())",
            id=uuid4(),
            builder=builder_id,
            lot=granted.lot_id,
            actor=ACTOR,
            key=key,
            digest="b" * 64,
        )
    assert _sqlstate(info.value) == "23505"
    with pytest.raises(
        DBAPIError, match=re.escape("credit_courtesy_events is append-only")
    ) as mutation:
        await _run(
            postgres_harness,
            "UPDATE credit_courtesy_events SET reason = 'x' WHERE id = :id",
            id=granted.event_id,
        )
    assert _sqlstate(mutation.value) == "23000"


# ---------------------------------------------------------------------------
# Builder activity and isolation from billing.
# ---------------------------------------------------------------------------


async def test_an_inactive_builder_gets_nothing_but_its_courtesy_can_be_removed(
    postgres_harness: _Harness, enforced: None
) -> None:
    inactive = await _builder(postgres_harness, is_active=False)
    with pytest.raises(CourtesyBuilderInactive) as refusal:
        await _grant(postgres_harness, inactive)
    assert refusal.value.code == "builder_inactive"
    assert await _rows(postgres_harness, inactive) == {
        table: 0 for table in await _rows(postgres_harness, inactive)
    }

    builder_id = await _builder(postgres_harness)
    granted = await _grant(postgres_harness, builder_id, credits=50)
    await _run(
        postgres_harness,
        "UPDATE builders SET is_active = false, status = 'inactive' WHERE id = :id",
        id=builder_id,
    )
    removed = await _remove(postgres_harness, granted.lot_id, credits=20)
    assert removed.removed_mc == 20 * MC
    with pytest.raises(CourtesyBuilderInactive):
        await _grant(postgres_harness, builder_id, credits=1)


async def test_grant_and_removal_write_no_billing_row(
    postgres_harness: _Harness, enforced: None
) -> None:
    before = await _billing_counts(postgres_harness)
    assert {
        "billing_subscriptions",
        "billing_subscription_cycles",
        "billing_checkout_sessions",
        "billing_topup_orders",
        "billing_outbox",
        "billing_fact_acks",
    } <= set(before)

    builder_id = await _builder(postgres_harness)
    granted = await _grant(postgres_harness, builder_id, credits=3_000)
    await _remove(postgres_harness, granted.lot_id, credits=1_000)
    await _debit(postgres_harness, builder_id, 500 * MC, T0)
    await _remove(postgres_harness, granted.lot_id, remove_all=True)

    assert await _billing_counts(postgres_harness) == before
    rows = await _rows(postgres_harness, builder_id)
    assert (rows["credit_lots"], rows["credit_courtesy_events"]) == (1, 3)
    kinds = await _value(
        postgres_harness,
        "SELECT string_agg(entry_type, ',' ORDER BY occurred_at, entry_type) "
        "FROM credit_ledger_entries WHERE lot_id = :lot",
        lot=granted.lot_id,
    )
    assert kinds == "courtesy_removal,courtesy_removal,debit,grant"
    payment = await _value(
        postgres_harness,
        "SELECT payment_id IS NULL AND lot_kind = 'courtesy' AND paid_at = :day "
        "FROM credit_lots WHERE id = :lot",
        lot=granted.lot_id,
        day=date(2026, 11, 9),
    )
    assert payment is True
