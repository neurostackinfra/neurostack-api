"""Window opening, reservations and egress on real PostgreSQL (Group 4a).

A fresh child database at ``head`` (039) of the smoke cluster.  The ledger's
reference clock is replaced at class level so the scenarios live on chosen
civil dates; every lock, constraint and trigger is PostgreSQL's own.

* concurrent openings of one window charge each maintenance target ONCE
  (the account lock serializes; one effect per window and category; the
  partial unique index refuses a second opening under direct SQL);
* an insufficient capacity consumes what there is and records the rest as
  debt, preserving conservation (lots + debt = what was debited);
* an early renewal's maintenance is charged at
  ``start_of_civil_day(cycle_start)`` against the NEW lot, never at the
  grant and never as debt against the previous cycle;
* a deletion lowers the maintenance only from the next window, without any
  refund;
* lock order: a trace debit (tenant ``FOR SHARE`` first) and a media upload
  (the real capacity guard, then the reservation) never deadlock, in both
  orders; a concurrent ``DELETE`` of a sibling clinic never deadlocks with a
  reservation's readback;
* the 039 tables are append-only, the egress delivery checks its tenant's
  builder, and a hold refuses a download's charge leaving nothing.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.models.billing import BillingTopupOrder
from src.models.builder import Builder
from src.models.credit import CreditEnforcementState, CreditLedgerEntry, CreditLot
from src.models.credit_inventory import CreditAllocation
from src.models.event import Event
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository, LedgerMovement, fold_ledger
from src.services import credit_inventory
from src.services.credit_ledger import (
    CreditLedger,
    GrantSpec,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    start_of_civil_day,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_rollout import CreditRolloutService
from src.services.media_assets import media_payload_sha256, media_storage_capacity_guard
from tests.integration.test_asaas_billing_postgres import (
    _sqlstate,
    _validated_admin_url,
)
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

SUB = UUID("00000000-0000-4000-8000-0000000a4a02")
_WAIT = 0.4  # seconds a blocked contender must still be waiting
_TIMEOUT = 20.0


def _instant(day: date, hour: int = 12) -> datetime:
    return start_of_civil_day(day) + timedelta(hours=hour)


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    def session(self) -> AsyncSession:
        return self.sessions()


@pytest.fixture(scope="module")
async def harness() -> AsyncIterator[_Harness]:
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
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "042"
            )
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


@dataclass
class _Clock:
    now: datetime


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
    current = _Clock(_instant(date(2026, 11, 2)))

    async def reference_now(_self: CreditLedger) -> datetime:
        return current.now

    monkeypatch.setattr(CreditLedger, "reference_now", reference_now)
    monkeypatch.setattr(credit_inventory, "_now", lambda: current.now)
    yield current


@dataclass(frozen=True)
class _World:
    builder: UUID
    tenant: UUID
    sibling: UUID


@pytest.fixture
async def world(harness: _Harness) -> _World:
    world = _World(uuid4(), uuid4(), uuid4())
    async with harness.session() as session, session.begin():
        session.add(Builder(id=world.builder, name="Maintenance builder"))
        await session.flush()
        for tenant_id in (world.tenant, world.sibling):
            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"Maintenance {tenant_id.hex[:6]}",
                    slug=f"maintenance-{tenant_id.hex}",
                    api_key_hash=f"hash-{tenant_id.hex}",
                    plan_type="clinic",
                    builder_id=world.builder,
                    config={},
                )
            )
    return world


@pytest.fixture
async def enforced(harness: _Harness) -> AsyncIterator[datetime]:
    """``credit_enforcement_at`` before every window of these scenarios."""

    at = _instant(date(2026, 9, 1))
    async with harness.session() as session, session.begin():
        await session.execute(text("DELETE FROM credit_enforcement_state"))
        session.add(
            CreditEnforcementState(
                id=1, credit_enforcement_at=at, set_by="t", set_at=at
            )
        )
    yield at


async def _grant(
    harness: _Harness,
    builder: UUID,
    at: datetime,
    *,
    granted_mc: int,
    cycle: tuple[date, date] | None = None,
) -> None:
    async with harness.session() as session, session.begin():
        ledger = CreditLedger(session)
        book = await ledger.open_book(builder, at=at)
        if cycle is not None:
            start, end = cycle
            spec = GrantSpec(
                lot_kind="monthly",
                effect_id=monthly_grant_effect_id(SUB, start, end),
                payment_id=f"pay-{start}",
                paid_at=start,
                granted_mc=granted_mc,
                expires_at=monthly_lot_expires_at(end),
                subscription_id=SUB,
                cycle_start=start,
                cycle_end=end,
            )
        else:
            order = uuid4()
            spec = GrantSpec(
                lot_kind="topup",
                effect_id=topup_grant_effect_id(order),
                payment_id=f"pay-{order}",
                paid_at=date(2026, 10, 1),
                granted_mc=granted_mc,
                expires_at=topup_lot_expires_at(date(2026, 10, 1)),
                order_id=order,
            )
        book.grant(spec, at=at)
        await ledger.persist(book)


async def test_cutoff_report_blocks_recent_builder_traffic_without_capacity(
    harness: _Harness, world: _World
) -> None:
    """Debt eligibility is not capacity coverage for enforcement rollout."""

    instant = _instant(date(2026, 11, 2))
    async with harness.session() as session, session.begin():
        session.add(
            Event(
                id=uuid4(),
                tenant_id=world.tenant,
                session_id=None,
                event_type="message.in",
                event_data={},
                event_metadata={},
                created_at=instant,
            )
        )
    async with harness.session() as session:
        report = await CreditRolloutService(session).cutoff_report(now=instant)

    assert any(
        finding.tenant_id == world.tenant
        and finding.reason == "builder_without_coverage"
        for finding in report.findings
    )
    assert not report.accepted


def _document(tenant: UUID, characters: int) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid4(),
        tenant_id=tenant,
        title="Doc",
        content="x" * characters,
        content_type="text",
        document_metadata={},
        is_indexed=False,
        chunk_count=0,
        size_bytes=characters,
    )


def _media(tenant: UUID, size: int) -> MediaAsset:
    payload = b"%PDF-" + b"0" * (size - 5)
    return MediaAsset(
        id=uuid4(),
        tenant_id=tenant,
        display_name="Media",
        original_filename="media.pdf",
        category="pdf",
        mime_type="application/pdf",
        size_bytes=size,
        sha256=media_payload_sha256(payload),
        payload=payload,
        usage_trigger=None,
        auto_suggest=False,
    )


async def _store(harness: _Harness, *rows: Any) -> list[UUID]:
    async with harness.session() as session, session.begin():
        session.add_all(rows)
    return [row.id for row in rows]


async def _ledger(harness: _Harness, builder: UUID) -> dict[str, list[tuple[str, int]]]:
    async with harness.session() as session:
        rows = (
            await session.execute(
                select(CreditLedgerEntry).where(CreditLedgerEntry.builder_id == builder)
            )
        ).scalars()
        out: dict[str, list[tuple[str, int]]] = {}
        for row in rows:
            out.setdefault(row.effect_id, []).append((row.entry_type, row.amount_mc))
    return {key: sorted(value) for key, value in out.items()}


async def _allocations(harness: _Harness, builder: UUID) -> list[CreditAllocation]:
    async with harness.session() as session:
        return list(
            (
                await session.execute(
                    select(CreditAllocation).where(
                        CreditAllocation.builder_id == builder
                    )
                )
            ).scalars()
        )


async def _reserve_in(
    session: AsyncSession, tenant: UUID, objects: list[UUID], enforced_at: datetime
) -> credit_inventory.AllocationOutcome:
    return await credit_inventory.reserve_upload(
        session,
        tenant_id=tenant,
        category="rag",
        object_ids=objects,
        upload_id=objects[0],
        enforcement_at=enforced_at,
    )


async def _blocked(task: asyncio.Task[Any]) -> None:
    await asyncio.sleep(_WAIT)
    assert not task.done(), "the contender did not wait for the lock"


# ---------------------------------------------------------------------------
# Opening and maintenance.
# ---------------------------------------------------------------------------


async def test_concurrent_openings_charge_each_maintenance_target_once(
    harness: _Harness, world: _World, clock: _Clock, enforced: datetime
) -> None:
    await _grant(
        harness,
        world.builder,
        clock.now,
        granted_mc=50_000,
        cycle=(date(2026, 11, 1), date(2026, 12, 1)),
    )
    await _store(
        harness, _document(world.tenant, 25_000), _media(world.sibling, 30_000_000)
    )
    first_doc, second_doc = await _store(
        harness, _document(world.tenant, 5_000), _document(world.sibling, 5_000)
    )
    first = harness.session()
    second = harness.session()
    try:
        await first.begin()
        outcome = await _reserve_in(first, world.tenant, [first_doc], enforced)
        assert outcome.status == "applied"

        async def contender() -> credit_inventory.AllocationOutcome:
            await second.begin()
            return await _reserve_in(second, world.sibling, [second_doc], enforced)

        task = asyncio.create_task(contender())
        await _blocked(task)  # waits on the builder's account lock
        await first.commit()
        other = await asyncio.wait_for(task, _TIMEOUT)
        await second.commit()
    finally:
        await first.close()
        await second.close()
    # The first opening counts everything stored but its own upload (the
    # sibling's document included: 30,000 characters), then its delta; the
    # second finds the window open and its document already covered.
    assert (outcome.charged_mc, other.charged_mc) == (1_000, 0)
    assert other.status == "applied"
    ledger = await _ledger(harness, world.builder)
    maintenance = {
        key: value for key, value in ledger.items() if key.endswith("-maintenance")
    }
    assert maintenance == {
        "window:2026-11-01:rag-maintenance": [("debit", 6_000)],
        "window:2026-11-01:media-maintenance": [("debit", 1_500)],
    }
    openings = [
        row
        for row in await _allocations(harness, world.builder)
        if row.allocation_kind == "maintenance"
    ]
    assert len(openings) == 2
    # The partial unique index: one opening per window and category.
    row = openings[0]
    with pytest.raises(DBAPIError) as info:
        async with harness.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO credit_allocations (id, builder_id, account_id, "
                    "window_start, window_end, category, allocation_kind, source_key, "
                    "units, target_mc, charged_mc) VALUES (gen_random_uuid(), :b, :a, "
                    ":s, :e, :c, 'baseline', 'probe-second-opening', 0, 0, 0)"
                ),
                {
                    "b": row.builder_id,
                    "a": row.account_id,
                    "s": row.window_start,
                    "e": row.window_end,
                    "c": row.category,
                },
            )
    assert _sqlstate(info.value) == "23505"


async def test_insufficient_capacity_becomes_debt_and_conservation_holds(
    harness: _Harness, world: _World, clock: _Clock, enforced: datetime
) -> None:
    await _grant(harness, world.builder, clock.now, granted_mc=3_000)
    await _store(harness, _document(world.tenant, 20_000))  # 4 credits of maintenance
    (doc,) = await _store(harness, _document(world.tenant, 10_000))
    async with harness.session() as session, session.begin():
        outcome = await _reserve_in(session, world.tenant, [doc], enforced)
    assert outcome.charged_mc == 2_000
    ledger = await _ledger(harness, world.builder)
    assert ledger["window:2026-11-01:rag-maintenance"] == [
        ("debit", 3_000),
        ("debt", 1_000),
    ]
    assert ledger[f"rag:{world.tenant}:{doc}:reserve"] == [("debt", 2_000)]
    async with harness.session() as session:
        rows = (
            await session.execute(
                select(
                    CreditLedgerEntry.entry_type,
                    CreditLedgerEntry.amount_mc,
                    CreditLedgerEntry.lot_id,
                ).where(CreditLedgerEntry.builder_id == world.builder)
            )
        ).all()
        derived = await CreditRepository(session).derived_balance(world.builder)
    folded = fold_ledger(LedgerMovement(*row) for row in rows)
    assert folded == derived
    assert (folded.lots_total_mc, folded.debt_mc) == (0, 3_000)
    # Every allocation's expected value is its effect's debit + debt.
    for allocation in await _allocations(harness, world.builder):
        if allocation.effect_id is None:
            continue
        assert allocation.charged_mc == sum(
            amount for _kind, amount in ledger[allocation.effect_id]
        )


async def test_an_early_renewal_is_charged_at_its_start_against_the_new_lot(
    harness: _Harness, world: _World, clock: _Clock, enforced: datetime
) -> None:
    current = (date(2026, 10, 15), date(2026, 11, 15))
    renewal = (date(2026, 11, 15), date(2026, 12, 15))
    clock.now = _instant(date(2026, 10, 20))
    await _grant(harness, world.builder, clock.now, granted_mc=5_000, cycle=current)
    await _store(harness, _document(world.tenant, 10_000))

    async def trace() -> None:
        async with harness.session() as session, session.begin():
            await CreditRepository(session).tenant_scope(world.tenant, lock=True)
            result = await CreditLedger(session).debit(
                world.builder,
                f"trace:{world.tenant}:{uuid4()}:decision:1",
                1_000,
                at=clock.now,
                effect_kind="trace",
                tenant_id=world.tenant,
            )
            assert result.status == "applied"

    await trace()  # opens 2026-10-15: 2 credits of maintenance
    clock.now = _instant(date(2026, 11, 10))
    await _grant(harness, world.builder, clock.now, granted_mc=5_000, cycle=renewal)
    await trace()
    ledger = await _ledger(harness, world.builder)
    assert "window:2026-11-15:rag-maintenance" not in ledger
    clock.now = start_of_civil_day(renewal[0]) + timedelta(seconds=30)
    await trace()
    ledger = await _ledger(harness, world.builder)
    assert ledger["window:2026-11-15:rag-maintenance"] == [("debit", 2_000)]
    async with harness.session() as session:
        renewal_lot = (
            await session.execute(
                select(CreditLot.id).where(
                    CreditLot.builder_id == world.builder,
                    CreditLot.cycle_start == renewal[0],
                )
            )
        ).scalar_one()
        lots = (
            await session.execute(
                select(CreditLedgerEntry.lot_id).where(
                    CreditLedgerEntry.effect_id == "window:2026-11-15:rag-maintenance"
                )
            )
        ).scalars()
        assert list(lots) == [renewal_lot]


async def test_a_deletion_lowers_maintenance_only_from_the_next_window(
    harness: _Harness, world: _World, clock: _Clock, enforced: datetime
) -> None:
    await _grant(harness, world.builder, clock.now, granted_mc=100_000)
    (doc,) = await _store(harness, _document(world.tenant, 20_000))
    async with harness.session() as session, session.begin():
        assert (
            await _reserve_in(session, world.tenant, [doc], enforced)
        ).charged_mc == 4_000
    before = await _ledger(harness, world.builder)
    async with harness.session() as session, session.begin():
        await session.execute(
            text("DELETE FROM knowledge_documents WHERE id = :d"), {"d": doc}
        )
    (small,) = await _store(harness, _document(world.tenant, 1_000))
    async with harness.session() as session, session.begin():
        assert (
            await _reserve_in(session, world.tenant, [small], enforced)
        ).charged_mc == 0
    after = await _ledger(harness, world.builder)
    # No refund: every earlier effect is unchanged and nothing came back.
    assert {key: value for key, value in after.items() if key in before} == before
    assert not any(
        kind in ("reversal", "lot_regrant") for v in after.values() for kind, _ in v
    )
    clock.now = _instant(date(2026, 12, 2))
    (late,) = await _store(harness, _document(world.tenant, 1))
    async with harness.session() as session, session.begin():
        await _reserve_in(session, world.tenant, [late], enforced)
    december = await _ledger(harness, world.builder)
    assert december["window:2026-12-01:rag-maintenance"] == [("debit", 200)]


# ---------------------------------------------------------------------------
# Lock order.
# ---------------------------------------------------------------------------


async def _media_upload(
    session: AsyncSession,
    world: _World,
    enforced_at: datetime,
    *,
    pause: asyncio.Event | None = None,
) -> credit_inventory.AllocationOutcome:
    """The media route's own sequence: the real capacity guard, the insert,
    then the reservation, in one transaction."""

    target = await session.get(Tenant, world.tenant)
    assert target is not None
    asset = _media(world.tenant, 1_000_000)
    async with media_storage_capacity_guard(session, None, target) as capacity:
        await capacity.validate_projection(asset.size_bytes)
        session.add(asset)
        await session.flush()
        outcome = await credit_inventory.reserve_upload(
            session,
            tenant_id=world.tenant,
            category="media",
            object_ids=[asset.id],
            upload_id=asset.id,
            enforcement_at=enforced_at,
        )
    if pause is not None:
        await pause.wait()
    return outcome


async def _trace_in(session: AsyncSession, world: _World, at: datetime) -> str:
    await CreditRepository(session).tenant_scope(world.tenant, lock=True)
    result = await CreditLedger(session).debit(
        world.builder,
        f"trace:{world.tenant}:{uuid4()}:decision:1",
        1_000,
        at=at,
        effect_kind="trace",
        tenant_id=world.tenant,
    )
    return result.status


@pytest.mark.parametrize("first", ["trace", "upload"])
async def test_a_trace_and_a_media_upload_never_deadlock(
    harness: _Harness, world: _World, clock: _Clock, enforced: datetime, first: str
) -> None:
    await _grant(harness, world.builder, clock.now, granted_mc=10_000)
    holder = harness.session()
    contender_session = harness.session()
    try:
        await holder.begin()
        await holder.execute(text("SET LOCAL lock_timeout = '15s'"))
        if first == "trace":
            assert await _trace_in(holder, world, clock.now) == "applied"
        else:
            assert (await _media_upload(holder, world, enforced)).status == "applied"

        async def contender() -> Any:
            await contender_session.begin()
            await contender_session.execute(text("SET LOCAL lock_timeout = '15s'"))
            if first == "trace":
                return (await _media_upload(contender_session, world, enforced)).status
            return await _trace_in(contender_session, world, clock.now)

        task = asyncio.create_task(contender())
        await _blocked(task)
        await holder.commit()
        assert await asyncio.wait_for(task, _TIMEOUT) == "applied"
        await contender_session.commit()
    finally:
        await holder.close()
        await contender_session.close()
    ledger = await _ledger(harness, world.builder)
    assert sum(1 for key in ledger if key.startswith("trace:")) == 1
    assert sum(1 for key in ledger if key.startswith("media:")) == 1


async def test_a_download_charge_holding_its_first_lock_never_deadlocks_an_upload(
    harness: _Harness,
    world: _World,
    clock: _Clock,
    enforced: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock order step 1 before step 3 in the egress charge itself: the
    download pauses right after its FIRST credit lock while a media upload of
    the same tenant starts (the capacity guard locks the tenant ``FOR
    UPDATE``, then the reservation the account).  Tenant first, the upload
    waits on the tenant and both complete; account first, each would wait
    on the other's lock."""

    await _grant(harness, world.builder, clock.now, granted_mc=10_000)
    (media,) = await _store(harness, _media(world.tenant, 1_000))
    download = harness.session()
    upload = harness.session()
    paused, release = asyncio.Event(), asyncio.Event()
    first_lock: list[str] = []
    original_scope = CreditRepository.tenant_scope
    original_lock = CreditRepository.lock_account

    async def hold_first(repository: CreditRepository, name: str) -> None:
        if repository.session is download and not first_lock:
            first_lock.append(name)
            paused.set()
            await release.wait()

    async def tenant_scope(
        self: CreditRepository, tenant_id: UUID, *, lock: bool = False
    ) -> Any:
        scope = await original_scope(self, tenant_id, lock=lock)
        if lock:
            await hold_first(self, "tenant")
        return scope

    async def lock_account(self: CreditRepository, builder_id: UUID) -> Any:
        account = await original_lock(self, builder_id)
        await hold_first(self, "account")
        return account

    monkeypatch.setattr(CreditRepository, "tenant_scope", tenant_scope)
    monkeypatch.setattr(CreditRepository, "lock_account", lock_account)
    try:

        async def charge() -> str:
            await download.begin()
            await download.execute(text("SET LOCAL lock_timeout = '15s'"))
            outcome = await credit_inventory.charge_egress(
                download,
                tenant_id=world.tenant,
                media_id=media,
                size_bytes=1_000,
                enforcement_at=enforced,
            )
            await download.commit()
            return outcome.status

        async def store() -> str:
            await upload.begin()
            await upload.execute(text("SET LOCAL lock_timeout = '15s'"))
            outcome = await _media_upload(upload, world, enforced)
            await upload.commit()
            return outcome.status

        charging = asyncio.create_task(charge())
        await asyncio.wait_for(paused.wait(), _TIMEOUT)
        storing = asyncio.create_task(store())
        await _blocked(storing)  # waits on a lock the download holds
        release.set()
        assert await asyncio.wait_for(charging, _TIMEOUT) == "applied"
        assert await asyncio.wait_for(storing, _TIMEOUT) == "applied"
    finally:
        release.set()
        await download.close()
        await upload.close()
    assert first_lock == ["tenant"]
    ledger = await _ledger(harness, world.builder)
    assert sum(1 for key in ledger if key.startswith("media:")) == 1
    async with harness.engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM credit_egress_deliveries "
                    "WHERE tenant_id = :t AND mode = 'enforced'"
                ),
                {"t": world.tenant},
            )
            == 1
        )


async def test_a_sibling_clinic_delete_never_deadlocks_with_a_readback(
    harness: _Harness, world: _World, clock: _Clock, enforced: datetime
) -> None:
    await _store(
        harness, _document(world.sibling, 5_000), _media(world.sibling, 2_000_000)
    )
    (doc,) = await _store(harness, _document(world.tenant, 5_000))
    reserving = harness.session()
    try:
        await reserving.begin()
        assert (
            await _reserve_in(reserving, world.tenant, [doc], enforced)
        ).status == "applied"
        # The delete takes no credit lock: it completes while the reservation
        # still holds the builder's account.
        async with harness.engine.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :t"), {"t": world.sibling}
            )
        await reserving.commit()
    finally:
        await reserving.close()
    # The next readback records the sibling's objects as gone.
    async with harness.session() as session:
        _snapshot, versions = await credit_inventory.readback(session, world.builder)
    assert sorted(version["units"] for version in versions) == [0, 0]


# ---------------------------------------------------------------------------
# The 039 tables themselves.
# ---------------------------------------------------------------------------


async def test_the_039_tables_are_append_only_and_deliveries_check_the_builder(
    harness: _Harness, world: _World, clock: _Clock, enforced: datetime
) -> None:
    (doc,) = await _store(harness, _document(world.tenant, 5_000))
    async with harness.session() as session, session.begin():
        await _reserve_in(session, world.tenant, [doc], enforced)
    for table in ("credit_allocations", "credit_inventory_versions"):
        for statement in (f"UPDATE {table} SET units = units", f"DELETE FROM {table}"):
            with pytest.raises(DBAPIError, match=re.escape("append-only")) as info:
                async with harness.engine.begin() as connection:
                    await connection.execute(text(statement))
            assert _sqlstate(info.value) == "23000"
    with pytest.raises(DBAPIError, match="builder does not match") as info:
        async with harness.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO credit_egress_deliveries (id, tenant_id, builder_id, "
                    "media_id, delivery_id, window_start, bytes, mode) VALUES "
                    "(gen_random_uuid(), :t, gen_random_uuid(), gen_random_uuid(), "
                    "gen_random_uuid(), DATE '2026-11-01', 1, 'enforced')"
                ),
                {"t": world.tenant},
            )
    assert _sqlstate(info.value) == "23000"


async def test_a_hold_refuses_the_download_charge_and_writes_nothing(
    harness: _Harness, world: _World, clock: _Clock, enforced: datetime
) -> None:
    (media,) = await _store(harness, _media(world.tenant, 1_000))
    async with harness.session() as session, session.begin():
        session.add(
            BillingTopupOrder(
                id=uuid4(),
                builder_id=world.builder,
                payment_method="pix",
                catalog_version_id="payment-2026-10-03",
                offer_id="package_credits_500_v1",
                amount_cents=10_000,
                credit_grant_mc=500_000,
                pricing_snapshot={},
                financial_snapshot={},
                state="quarantined",
            )
        )
    async with harness.session() as session, session.begin():
        outcome = await credit_inventory.charge_egress(
            session,
            tenant_id=world.tenant,
            media_id=media,
            size_bytes=1_000,
            enforcement_at=enforced,
        )
    assert outcome.status == "credit_pending"
    assert await _ledger(harness, world.builder) == {}
    assert await _allocations(harness, world.builder) == []
    async with harness.engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM credit_egress_deliveries WHERE tenant_id = :t"
                ),
                {"t": world.tenant},
            )
            == 0
        )
