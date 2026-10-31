"""Credit inventory, window opening and reservations (Group 4a), SQLite.

The real application's SQLite ``:memory:`` database (``client`` fixture);
the ledger's reference clock is replaced at class level so the scenarios can
live on chosen civil dates.  What is pinned here (PostgreSQL concurrency is
``tests/integration/test_credit_maintenance_postgres.py``):

* the readback appends corrective inventory versions (new, resized, gone,
  moved) and is a no-op when nothing changed;
* a window opens once: ``maintenance`` (one effect per window and category)
  when it starts after ``credit_enforcement_at``, a free ``baseline`` when it
  was already open; the triggering upload is never part of the opening;
* a reservation debits ``target - coverage`` (deleted content refunds
  nothing; new content reuses the freed coverage), can become debt, and
  every allocation row carries the expected value of its effect;
* RAG reprocessing is included up to 100 %, the excess is charged through
  the aggregate delta and a retried job is a duplicate;
* egress: 200 MB per media credit covered, the overage per 100 MB, every
  delivery counted once;
* a trace debit opens its window first; an early renewal's maintenance is
  charged at ``start_of_civil_day(cycle_start)``, never at the grant;
* a standalone tenant, a quarantine hold or a failed readback is
  ``credit_pending`` with nothing written;
* the mode is read once; the observational writer never raises and swallows
  injected failures.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from src.models.billing import BillingTopupOrder
from src.models.builder import Builder
from src.models.credit import (
    CreditEffect,
    CreditEnforcementState,
    CreditLedgerEntry,
    CreditObservationalEffect,
)
from src.models.credit_inventory import (
    CreditAllocation,
    CreditEgressDelivery,
    CreditInventoryVersion,
)
from src.models.credit_object_copy import CreditObjectCopyJob, CreditQdrantJob
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.repositories.credit_inventory import CreditInventoryRepository, SourceRow
from src.repositories.database import get_db_session
from src.services import credit_inventory
from src.services.credit_allocations import maintenance_effect_id
from src.services.credit_ledger import (
    CreditLedger,
    GrantSpec,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    start_of_civil_day,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_metering import CreditEnforcementUnavailable
from src.services.credit_rollout import BindingCoverage, CreditRolloutService

SUB = UUID("00000000-0000-4000-8000-0000000a4a01")


def _instant(day: date, hour: int = 12) -> datetime:
    return start_of_civil_day(day) + timedelta(hours=hour)


@dataclass
class Clock:
    now: datetime


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    current = Clock(_instant(date(2026, 11, 2)))

    async def reference_now(_self: CreditLedger) -> datetime:
        return current.now

    monkeypatch.setattr(CreditLedger, "reference_now", reference_now)
    monkeypatch.setattr(credit_inventory, "_now", lambda: current.now)
    return current


@pytest.fixture(autouse=True)
def _forget(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # This Group 4 inventory suite models an already-reconciled capacity
    # source.  Binding truth itself is exercised by the Group 7 rollout
    # service tests; keeping it explicit here preserves the suite's focus on
    # allocation and lifecycle arithmetic.
    async def covered(
        _self: CreditRolloutService, _builder_id: UUID, *, at: datetime | None = None
    ) -> BindingCoverage:
        del at
        return BindingCoverage(monthly_binding=True)

    monkeypatch.setattr(CreditRolloutService, "binding_coverage", covered)
    credit_inventory.forget_enforcement_seen()
    yield
    credit_inventory.forget_enforcement_seen()


@dataclass(frozen=True)
class World:
    builder: UUID
    tenant: UUID
    sibling: UUID
    standalone: UUID


def run(client: Any, coroutine: Any) -> Any:
    return client._submit(coroutine)


async def _seed() -> World:
    world = World(uuid4(), uuid4(), uuid4(), uuid4())
    session = get_db_session()
    try:
        async with session.begin():
            session.add(Builder(id=world.builder, name="Inventory builder"))
            await session.flush()
            for tenant_id, builder in (
                (world.tenant, world.builder),
                (world.sibling, world.builder),
                (world.standalone, None),
            ):
                session.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Inventory {tenant_id.hex[:6]}",
                        slug=f"inventory-{tenant_id.hex}",
                        api_key_hash=f"hash-{tenant_id.hex}",
                        builder_id=builder,
                        config={},
                    )
                )
    finally:
        await session.close()
    return world


@pytest.fixture
def world(client: Any) -> World:
    return run(client, _seed())


async def _enforce(at: datetime) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            session.add(
                CreditEnforcementState(
                    id=1, credit_enforcement_at=at, set_by="test", set_at=at
                )
            )
    finally:
        await session.close()


async def _grant_monthly(
    builder: UUID, start: date, end: date, granted_mc: int, at: datetime
) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            ledger = CreditLedger(session)
            book = await ledger.open_book(builder, at=at)
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
                at=at,
            )
            await ledger.persist(book)
    finally:
        await session.close()


async def _grant_topup(
    builder: UUID, granted_mc: int, paid_at: date, at: datetime
) -> None:
    order = uuid4()
    session = get_db_session()
    try:
        async with session.begin():
            ledger = CreditLedger(session)
            book = await ledger.open_book(builder, at=at)
            book.grant(
                GrantSpec(
                    lot_kind="topup",
                    effect_id=topup_grant_effect_id(order),
                    payment_id=f"pay-{order}",
                    paid_at=paid_at,
                    granted_mc=granted_mc,
                    expires_at=topup_lot_expires_at(paid_at),
                    order_id=order,
                ),
                at=at,
            )
            await ledger.persist(book)
    finally:
        await session.close()


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
        sha256=b"\x00" * 32,
        payload=payload,
        usage_trigger=None,
        auto_suggest=False,
    )


async def _store(*rows: Any) -> list[UUID]:
    session = get_db_session()
    try:
        async with session.begin():
            session.add_all(rows)
    finally:
        await session.close()
    return [row.id for row in rows]


async def _delete(model: Any, object_id: UUID) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            row = await session.get(model, object_id)
            assert row is not None
            await session.delete(row)
    finally:
        await session.close()


async def _reserve(
    tenant: UUID,
    category: str,
    objects: list[UUID],
    enforced_at: datetime,
    *,
    upload_id: UUID | None = None,
    commit: bool = True,
) -> credit_inventory.AllocationOutcome:
    session = get_db_session()
    try:
        outcome = await credit_inventory.reserve_upload(
            session,
            tenant_id=tenant,
            category=category,  # type: ignore[arg-type]
            object_ids=objects,
            upload_id=upload_id or objects[0],
            enforcement_at=enforced_at,
        )
        if commit and outcome.status == "applied":
            await session.commit()
        else:
            await session.rollback()
        return outcome
    finally:
        await session.close()


async def _rows(model: Any, *conditions: Any) -> list[Any]:
    session = get_db_session()
    try:
        return list((await session.execute(select(model).where(*conditions))).scalars())
    finally:
        await session.close()


async def _ledger(builder: UUID) -> dict[str, list[tuple[str, int]]]:
    rows = await _rows(CreditLedgerEntry, CreditLedgerEntry.builder_id == builder)
    out: dict[str, list[tuple[str, int]]] = {}
    for row in rows:
        out.setdefault(row.effect_id, []).append((row.entry_type, row.amount_mc))
    for value in out.values():
        value.sort()
    return out


async def _available(builder: UUID, at: datetime) -> tuple[int, int]:
    session = get_db_session()
    try:
        book = await CreditLedger(session).open_book(builder, at=at)
        result = (book.available_mc(at), book.open_debt_mc)
        await session.rollback()
        return result
    finally:
        await session.close()


def test_unreconciled_binding_is_credit_pending_before_inventory_debit(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Group 7 capacity fence also protects enforced RAG/media work."""

    async def unreconciled(
        _self: CreditRolloutService, _builder_id: UUID, *, at: datetime | None = None
    ) -> BindingCoverage:
        del at
        return BindingCoverage(reconciliation_failure=True)

    monkeypatch.setattr(CreditRolloutService, "binding_coverage", unreconciled)

    async def scenario() -> None:
        document = _document(world.tenant, 128)
        await _store(document)
        outcome = await _reserve(
            world.tenant,
            "rag",
            [document.id],
            _instant(date(2026, 11, 1)),
        )
        assert outcome.status == "credit_pending"
        assert await _ledger(world.builder) == {}

    run(client, scenario())


# ---------------------------------------------------------------------------
# The readback.
# ---------------------------------------------------------------------------


def test_the_readback_appends_corrective_versions_once(
    client: Any, world: World
) -> None:
    async def scenario() -> None:
        doc, media = await _store(
            _document(world.tenant, 7_000), _media(world.sibling, 30_000_000)
        )
        session = get_db_session()
        try:
            snapshot, versions = await credit_inventory.readback(session, world.builder)
            assert snapshot.units("rag") == 7_000
            assert snapshot.units("media") == 30_000_000
            assert sorted(
                (v["category"], v["version"], v["units"]) for v in versions
            ) == [
                ("media", 1, 30_000_000),
                ("rag", 1, 7_000),
            ]
            await CreditInventoryRepository(session).insert_versions(versions)
            await session.commit()
            # Nothing changed: nothing to correct.
            _snapshot, again = await credit_inventory.readback(session, world.builder)
            assert again == []
        finally:
            await session.close()
        await _delete(KnowledgeDocument, doc)
        session = get_db_session()
        try:
            snapshot, versions = await credit_inventory.readback(session, world.builder)
            assert snapshot.units("rag") == 0
            # Gone: a version with zero units, never a deletion of history.
            assert [(v["object_id"], v["version"], v["units"]) for v in versions] == [
                (doc, 2, 0)
            ]
            await CreditInventoryRepository(session).insert_versions(versions)
            await session.commit()
        finally:
            await session.close()
        recorded = await _rows(
            CreditInventoryVersion, CreditInventoryVersion.object_id == media
        )
        assert [(row.builder_id, row.units) for row in recorded] == [
            (world.builder, 30_000_000)
        ]

    run(client, scenario())


def test_a_moved_tenant_leaves_its_old_builder_and_joins_the_new_one(
    client: Any, world: World
) -> None:
    async def scenario() -> None:
        (doc,) = await _store(_document(world.sibling, 5_000))
        other = uuid4()
        session = get_db_session()
        try:
            async with session.begin():
                session.add(Builder(id=other, name="Other builder"))
            _snapshot, versions = await credit_inventory.readback(
                session, world.builder
            )
            await CreditInventoryRepository(session).insert_versions(versions)
            await session.commit()
            async with session.begin():
                tenant = await session.get(Tenant, world.sibling)
                assert tenant is not None
                tenant.builder_id = other
            snapshot, versions = await credit_inventory.readback(session, world.builder)
            assert snapshot.units("rag") == 0
            assert [(v["builder_id"], v["version"], v["units"]) for v in versions] == [
                (world.builder, 2, 0)
            ]
            await CreditInventoryRepository(session).insert_versions(versions)
            await session.commit()
            snapshot, versions = await credit_inventory.readback(session, other)
            assert snapshot.units("rag") == 5_000
            assert [(v["builder_id"], v["version"], v["units"]) for v in versions] == [
                (other, 3, 5_000)
            ]
        finally:
            await session.close()
        assert doc

    run(client, scenario())


def test_an_untrustworthy_readback_is_credit_pending_and_writes_nothing(
    client: Any, world: World, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_self: Any, builder_id: UUID) -> list[SourceRow]:
        return [SourceRow("rag", world.tenant, uuid4(), -1)]

    async def scenario() -> None:
        await _enforce(clock.now - timedelta(days=30))
        (doc,) = await _store(_document(world.tenant, 5_000))
        monkeypatch.setattr(CreditInventoryRepository, "builder_sources", broken)
        outcome = await _reserve(
            world.tenant, "rag", [doc], clock.now - timedelta(days=30)
        )
        assert outcome.status == "credit_pending"
        assert await _rows(CreditAllocation) == []
        assert await _rows(CreditInventoryVersion) == []
        assert await _ledger(world.builder) == {}

    run(client, scenario())


# ---------------------------------------------------------------------------
# Opening and reservations.
# ---------------------------------------------------------------------------


def test_the_window_open_at_the_enforcement_gets_a_free_baseline(
    client: Any, world: World, clock: Clock
) -> None:
    """Owner decision E: nothing stored before the enforcement is charged in
    the window that was open then; only content added afterwards."""

    async def scenario() -> None:
        start, end = date(2026, 10, 15), date(2026, 11, 15)
        await _grant_monthly(world.builder, start, end, 5_000_000, clock.now)
        old = await _store(
            _document(world.tenant, 50_000), _media(world.sibling, 40_000_000)
        )
        enforced_at = clock.now - timedelta(days=1)
        await _enforce(enforced_at)
        (new,) = await _store(_document(world.tenant, 5_001))
        outcome = await _reserve(world.tenant, "rag", [new], enforced_at)
        assert (outcome.status, outcome.charged_mc) == ("applied", 1_001)
        rows = {
            (row.category, row.allocation_kind): row
            for row in await _rows(CreditAllocation)
        }
        assert set(rows) == {
            ("rag", "baseline"),
            ("media", "baseline"),
            ("rag", "reservation"),
        }
        assert (
            rows["rag", "baseline"].target_mc,
            rows["rag", "baseline"].charged_mc,
        ) == (
            10_000,
            0,
        )
        assert rows["media", "baseline"].target_mc == 2_000
        assert rows["rag", "baseline"].effect_id is None
        reservation = rows["rag", "reservation"]
        assert (reservation.target_mc, reservation.charged_mc) == (11_001, 1_001)
        assert reservation.effect_id == f"rag:{world.tenant}:{new}:reserve"
        assert reservation.window_start == start
        ledger = await _ledger(world.builder)
        assert ledger[reservation.effect_id] == [("debit", 1_001)]
        assert not any(key.endswith("-maintenance") for key in ledger)
        assert old

    run(client, scenario())


def test_a_window_starting_after_the_enforcement_charges_maintenance_once(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        start, end = date(2026, 11, 1), date(2026, 12, 1)
        enforced_at = _instant(date(2026, 10, 20))
        await _enforce(enforced_at)
        await _grant_monthly(world.builder, start, end, 5_000_000, clock.now)
        await _store(_document(world.tenant, 9_999), _media(world.sibling, 20_000_001))
        (new,) = await _store(_media(world.tenant, 1_000))
        outcome = await _reserve(world.tenant, "media", [new], enforced_at)
        # Maintenance of what was there (new upload excluded), then the delta:
        # the window pays ceil(all), rounded once.
        assert outcome.status == "applied"
        ledger = await _ledger(world.builder)
        rag_maintenance = maintenance_effect_id(
            credit_inventory.accounting_window([(start, end)], clock.now), "rag"
        )
        assert ledger[rag_maintenance] == [("debit", 2_000)]
        assert ledger["window:2026-11-01:media-maintenance"] == [("debit", 1_001)]
        # 20,000,001 + 1,000 bytes = 1,001 mc in total: the upload added 0.
        assert outcome.charged_mc == 0
        kinds = sorted(
            (row.category, row.allocation_kind, row.charged_mc)
            for row in await _rows(CreditAllocation)
        )
        assert kinds == [("media", "maintenance", 1_001), ("rag", "maintenance", 2_000)]
        # A second upload in the same window opens nothing again.
        (second,) = await _store(_document(world.sibling, 1))
        again = await _reserve(world.sibling, "rag", [second], enforced_at)
        assert (again.status, again.charged_mc) == ("applied", 0)
        assert len(await _rows(CreditAllocation)) == 2
        effects = await _rows(CreditEffect, CreditEffect.effect_kind == "maintenance")
        assert sorted(effect.effect_id for effect in effects) == [
            "window:2026-11-01:media-maintenance",
            "window:2026-11-01:rag-maintenance",
        ]

    run(client, scenario())


def test_deletion_refunds_nothing_and_new_content_reuses_the_coverage(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        enforced_at = clock.now - timedelta(days=60)
        await _enforce(enforced_at)
        await _grant_topup(world.builder, 100_000, date(2026, 10, 1), clock.now)
        (first,) = await _store(_document(world.tenant, 10_000))
        assert (
            await _reserve(world.tenant, "rag", [first], enforced_at)
        ).charged_mc == 2_000
        available, _debt = await _available(world.builder, clock.now)
        await _delete(KnowledgeDocument, first)
        # A smaller document after the deletion: its target is below the
        # coverage, and nothing is refunded (never a negative charge).
        (smaller,) = await _store(_document(world.tenant, 1_000))
        outcome = await _reserve(world.tenant, "rag", [smaller], enforced_at)
        assert (outcome.status, outcome.charged_mc) == ("applied", 0)
        assert await _available(world.builder, clock.now) == (available, 0)
        await _delete(KnowledgeDocument, smaller)
        (second,) = await _store(_document(world.tenant, 10_000))
        outcome = await _reserve(world.tenant, "rag", [second], enforced_at)
        assert (outcome.status, outcome.charged_mc) == ("applied", 0)
        # Nothing came back to the lots and nothing more left them.
        assert await _available(world.builder, clock.now) == (available, 0)
        (third,) = await _store(_document(world.tenant, 2_500))
        assert (
            await _reserve(world.tenant, "rag", [third], enforced_at)
        ).charged_mc == 500
        # Next window: the maintenance counts only what is active.
        clock.now = _instant(date(2026, 12, 3))
        (fourth,) = await _store(_document(world.tenant, 1))
        outcome = await _reserve(world.tenant, "rag", [fourth], enforced_at)
        ledger = await _ledger(world.builder)
        assert ledger["window:2026-12-01:rag-maintenance"] == [("debit", 2_500)]
        assert outcome.charged_mc == 1

    run(client, scenario())


def test_insufficient_capacity_consumes_what_there_is_and_records_debt(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        enforced_at = clock.now - timedelta(days=60)
        await _enforce(enforced_at)
        await _grant_topup(world.builder, 1_500, date(2026, 10, 1), clock.now)
        (doc,) = await _store(_document(world.tenant, 20_000))
        outcome = await _reserve(world.tenant, "rag", [doc], enforced_at)
        assert (outcome.status, outcome.charged_mc) == ("applied", 4_000)
        effect = f"rag:{world.tenant}:{doc}:reserve"
        assert (await _ledger(world.builder))[effect] == [
            ("debit", 1_500),
            ("debt", 2_500),
        ]
        assert await _available(world.builder, clock.now) == (0, 2_500)
        [row] = [
            row
            for row in await _rows(CreditAllocation)
            if row.allocation_kind == "reservation"
        ]
        # The allocation's expected value is the effect's debit plus debt.
        assert row.charged_mc == 4_000

    run(client, scenario())


def test_a_retried_reservation_is_a_duplicate(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        enforced_at = clock.now - timedelta(days=60)
        await _enforce(enforced_at)
        (doc,) = await _store(_document(world.tenant, 6_000))
        first = await _reserve(world.tenant, "rag", [doc], enforced_at)
        again = await _reserve(world.tenant, "rag", [doc], enforced_at)
        assert (first.status, first.charged_mc) == ("applied", 1_200)
        assert again.status == "duplicate"
        assert (await _ledger(world.builder))[f"rag:{world.tenant}:{doc}:reserve"] == [
            ("debt", 1_200)
        ]

    run(client, scenario())


def test_a_standalone_tenant_or_a_hold_is_credit_pending_with_nothing_written(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        enforced_at = clock.now - timedelta(days=60)
        await _enforce(enforced_at)
        (alone,) = await _store(_document(world.standalone, 5_000))
        assert (
            await _reserve(world.standalone, "rag", [alone], enforced_at)
        ).status == ("credit_pending")
        await _grant_topup(world.builder, 10_000, date(2026, 10, 1), clock.now)
        session = get_db_session()
        try:
            async with session.begin():
                session.add(_quarantined_order(world.builder))
        finally:
            await session.close()
        (doc,) = await _store(_document(world.tenant, 5_000))
        before = await _ledger(world.builder)
        assert (await _reserve(world.tenant, "rag", [doc], enforced_at)).status == (
            "credit_pending"
        )
        assert await _ledger(world.builder) == before
        assert await _rows(CreditAllocation) == []

    run(client, scenario())


def _quarantined_order(builder: UUID) -> BillingTopupOrder:
    """An ASAAS order in quarantine: the builder's spend hold."""

    return BillingTopupOrder(
        id=uuid4(),
        builder_id=builder,
        payment_method="pix",
        catalog_version_id="payment-2026-10-03",
        offer_id="package_credits_500_v1",
        amount_cents=10_000,
        credit_grant_mc=500_000,
        pricing_snapshot={},
        financial_snapshot={},
        state="quarantined",
    )


# ---------------------------------------------------------------------------
# Reprocessing and egress.
# ---------------------------------------------------------------------------


def test_reprocessing_is_included_and_its_excess_uses_the_aggregate_delta(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        enforced_at = clock.now - timedelta(days=60)
        await _enforce(enforced_at)
        (doc,) = await _store(_document(world.tenant, 10_000))
        await _reserve(world.tenant, "rag", [doc], enforced_at)

        async def reprocess(
            job: str, characters: int
        ) -> credit_inventory.AllocationOutcome:
            session = get_db_session()
            try:
                outcome = await credit_inventory.charge_rag_reprocessing(
                    session,
                    tenant_id=world.tenant,
                    object_id=doc,
                    job_id=job,
                    characters=characters,
                    enforcement_at=enforced_at,
                )
                await session.commit()
                return outcome
            finally:
                await session.close()

        assert (await reprocess("job-1", 10_000)).charged_mc == 0  # 100 % included
        assert (await reprocess("job-1", 10_000)).status == "duplicate"  # retry
        assert (await reprocess("job-2", 5_001)).charged_mc == 1_001
        rows = sorted(
            (row.allocation_kind, row.units, row.charged_mc)
            for row in await _rows(CreditAllocation)
            if row.category == "rag"
        )
        assert ("reprocessing", 10_000, 0) in rows
        assert ("reprocessing", 5_001, 1_001) in rows

    run(client, scenario())


def test_every_download_counts_and_only_the_overage_is_charged(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        enforced_at = clock.now - timedelta(days=60)
        await _enforce(enforced_at)
        (media,) = await _store(_media(world.tenant, 20_000_000))
        await _reserve(world.tenant, "media", [media], enforced_at)  # 1 credit
        session = get_db_session()
        try:
            charged = []
            for _ in range(11):  # 11 x 20 MB = 220 MB; 200 MB are included
                outcome = await credit_inventory.charge_egress(
                    session,
                    tenant_id=world.tenant,
                    media_id=media,
                    size_bytes=20_000_000,
                    enforcement_at=enforced_at,
                )
                await session.commit()
                charged.append(outcome.charged_mc)
        finally:
            await session.close()
        assert charged == [0] * 10 + [200]
        deliveries = await _rows(CreditEgressDelivery)
        assert len(deliveries) == 11 and {d.mode for d in deliveries} == {"enforced"}
        assert len({d.delivery_id for d in deliveries}) == 11
        egress = [
            row for row in await _rows(CreditAllocation) if row.category == "egress"
        ]
        assert [(row.target_mc, row.charged_mc) for row in egress] == [(200, 200)]
        # The overage adds no storage coverage.
        media_rows = [
            row for row in await _rows(CreditAllocation) if row.category == "media"
        ]
        assert max(row.target_mc for row in media_rows) == 1_000

    run(client, scenario())


# ---------------------------------------------------------------------------
# The trace debit opens its window; early renewal (O5).
# ---------------------------------------------------------------------------


def test_a_trace_debit_opens_its_window_before_debiting(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        enforced_at = _instant(date(2026, 10, 20))
        await _enforce(enforced_at)
        await _grant_monthly(
            world.builder, date(2026, 11, 1), date(2026, 12, 1), 3_000, clock.now
        )
        await _store(_document(world.tenant, 10_000))
        session = get_db_session()
        try:
            result = await CreditLedger(session).debit(
                world.builder,
                f"trace:{world.tenant}:{uuid4()}:decision:1",
                1_000,
                at=clock.now,
                effect_kind="trace",
                tenant_id=world.tenant,
            )
            await session.commit()
        finally:
            await session.close()
        # Maintenance first (2 credits), then the trace: 1 credit of 3 left 0.
        assert (result.status, result.debited_mc, result.debt_mc) == (
            "applied",
            1_000,
            0,
        )
        ledger = await _ledger(world.builder)
        assert ledger["window:2026-11-01:rag-maintenance"] == [("debit", 2_000)]
        assert await _available(world.builder, clock.now) == (0, 0)

    run(client, scenario())


def test_an_early_renewal_is_opened_at_its_cycle_start_never_at_the_grant(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        enforced_at = _instant(date(2026, 9, 1))
        await _enforce(enforced_at)
        current = (date(2026, 10, 15), date(2026, 11, 15))
        renewal = (date(2026, 11, 15), date(2026, 12, 15))
        clock.now = _instant(date(2026, 10, 20))
        await _grant_monthly(world.builder, *current, 5_000_000, clock.now)
        await _store(_document(world.tenant, 10_000))

        async def trace() -> None:
            session = get_db_session()
            try:
                await CreditLedger(session).debit(
                    world.builder,
                    f"trace:{world.tenant}:{uuid4()}:decision:1",
                    1_000,
                    at=clock.now,
                    effect_kind="trace",
                    tenant_id=world.tenant,
                )
                await session.commit()
            finally:
                await session.close()

        await trace()  # opens 2026-10-15 (after the enforcement)
        # The renewal is paid early: the grant opens nothing.
        clock.now = _instant(date(2026, 11, 10))
        await _grant_monthly(world.builder, *renewal, 5_000_000, clock.now)
        await trace()
        ledger = await _ledger(world.builder)
        assert "window:2026-11-15:rag-maintenance" not in ledger
        # At the renewal's cycle start the maintenance is charged against the
        # NEW lot (the previous one expired at that instant), never as debt.
        clock.now = start_of_civil_day(renewal[0]) + timedelta(minutes=1)
        await trace()
        ledger = await _ledger(world.builder)
        assert ledger["window:2026-11-15:rag-maintenance"] == [("debit", 2_000)]
        entries = await _rows(
            CreditLedgerEntry,
            CreditLedgerEntry.effect_id == "window:2026-11-15:rag-maintenance",
        )
        session = get_db_session()
        try:
            from src.models.credit import CreditLot

            renewal_lot = (
                await session.execute(
                    select(CreditLot.id).where(CreditLot.cycle_start == renewal[0])
                )
            ).scalar_one()
        finally:
            await session.close()
        assert [entry.lot_id for entry in entries] == [renewal_lot]

    run(client, scenario())


def test_a_window_starting_exactly_at_the_enforcement_gets_the_free_baseline(
    client: Any, world: World, clock: Clock
) -> None:
    """Owner decision E at its boundary: ``credit_enforcement_at`` equal to
    the window's first instant -- that window is the one open at the
    enforcement ([start, end) contains it): the content already stored is a
    free baseline, never maintenance; only what is added is charged."""

    async def scenario() -> None:
        start, end = date(2026, 11, 1), date(2026, 12, 1)
        enforced_at = start_of_civil_day(start)
        await _grant_monthly(world.builder, start, end, 5_000_000, clock.now)
        await _store(_document(world.tenant, 50_000), _media(world.sibling, 40_000_000))
        await _enforce(enforced_at)
        (new,) = await _store(_document(world.tenant, 5_000))
        outcome = await _reserve(world.tenant, "rag", [new], enforced_at)
        assert (outcome.status, outcome.charged_mc) == ("applied", 1_000)
        kinds = sorted(
            (row.category, row.allocation_kind, row.target_mc, row.charged_mc)
            for row in await _rows(CreditAllocation)
        )
        assert kinds == [
            ("media", "baseline", 2_000, 0),
            ("rag", "baseline", 10_000, 0),
            ("rag", "reservation", 11_000, 1_000),
        ]
        assert (
            await _rows(CreditEffect, CreditEffect.effect_kind == "maintenance") == []
        )
        ledger = await _ledger(world.builder)
        assert ledger[f"rag:{world.tenant}:{new}:reserve"] == [("debit", 1_000)]
        assert not any(key.endswith("-maintenance") for key in ledger)

    run(client, scenario())


def test_a_late_renewal_never_opens_its_window_twice(
    client: Any, world: World, clock: Clock
) -> None:
    """Owner decision C: without a current cycle the window is
    ``[last cycle_end, +1 month)``; a renewal paid late (its ``cycle_start``
    is that ``cycle_end``) falls in the SAME window -- one effect id per
    window and category -- so its maintenance is never charged twice.  The
    maintenance charged while no lot covered it is debt, which the late
    grant settles."""

    async def scenario() -> None:
        enforced_at = _instant(date(2026, 9, 1))
        await _enforce(enforced_at)
        previous = (date(2026, 9, 15), date(2026, 10, 15))
        renewal = (date(2026, 10, 15), date(2026, 11, 15))
        clock.now = _instant(date(2026, 9, 20))
        await _grant_monthly(world.builder, *previous, 5_000, clock.now)
        await _store(_document(world.tenant, 10_000))

        async def trace() -> str:
            session = get_db_session()
            try:
                result = await CreditLedger(session).debit(
                    world.builder,
                    f"trace:{world.tenant}:{uuid4()}:decision:1",
                    1_000,
                    at=clock.now,
                    effect_kind="trace",
                    tenant_id=world.tenant,
                )
                await session.commit()
                return result.status
            finally:
                await session.close()

        assert await trace() == "applied"  # opens 2026-09-15
        # The cycle ended, the renewal is not paid yet: the continuation
        # window [2026-10-15, 2026-11-15) opens with the next trace, as debt.
        clock.now = _instant(date(2026, 10, 20))
        assert await trace() == "applied"
        ledger = await _ledger(world.builder)
        assert ledger["window:2026-10-15:rag-maintenance"] == [("debt", 2_000)]
        # Paid late: the renewal's cycle starts at the last cycle_end.
        clock.now = _instant(date(2026, 10, 22))
        await _grant_monthly(world.builder, *renewal, 5_000_000, clock.now)
        clock.now = _instant(date(2026, 10, 23))
        assert await trace() == "applied"
        ledger = await _ledger(world.builder)
        maintenance = sorted(key for key in ledger if key.endswith("-maintenance"))
        assert maintenance == [
            "window:2026-09-15:rag-maintenance",
            "window:2026-10-15:rag-maintenance",
        ]
        assert ledger["window:2026-10-15:rag-maintenance"] == [("debt", 2_000)]
        openings = sorted(
            (row.window_start, row.category)
            for row in await _rows(CreditAllocation)
            if row.allocation_kind in ("baseline", "maintenance")
        )
        assert openings == [
            (date(2026, 9, 15), "media"),
            (date(2026, 9, 15), "rag"),
            (date(2026, 10, 15), "media"),
            (date(2026, 10, 15), "rag"),
        ]
        # The late grant settled the window's maintenance debt and the
        # trace's (3,000 mc); the last trace was debited once (1,000 mc).
        assert await _available(world.builder, clock.now) == (
            5_000_000 - 2_000 - 1_000 - 1_000,
            0,
        )

    run(client, scenario())


# ---------------------------------------------------------------------------
# The mode and the observational writer.
# ---------------------------------------------------------------------------


def _request(effective: bool) -> Any:
    from starlette.requests import Request

    return Request({"type": "http", "state": {"credit_switch_effective": effective}})


def test_the_mode_is_off_for_fakes_and_read_once_otherwise(
    client: Any, world: World, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        assert await credit_inventory.read_credit_mode(_request(True), object()) == (
            credit_inventory.CREDIT_OFF
        )
        session = get_db_session()
        try:
            assert not (
                await credit_inventory.read_credit_mode(_request(False), session)
            ).switch_on
            mode = await credit_inventory.read_credit_mode(_request(True), session)
            assert mode.observational and not mode.enforced
            await session.rollback()
            await _enforce(clock.now)
            mode = await credit_inventory.read_credit_mode(_request(True), session)
            assert mode.enforced and mode.enforcement_at is not None

            async def broken(_self: Any) -> Any:
                raise RuntimeError("credit_enforcement_state unreadable")

            from src.repositories.credit import CreditRepository

            monkeypatch.setattr(CreditRepository, "enforcement_at", broken)
            # Seen set once: an unreadable value is never "not yet".
            with pytest.raises(CreditEnforcementUnavailable):
                await credit_inventory.read_credit_mode(_request(True), session)
            credit_inventory.forget_enforcement_seen()
            from src.services import credit_metering

            credit_metering.forget_enforcement_seen()
            await session.rollback()
            mode = await credit_inventory.read_credit_mode(_request(True), session)
            assert mode.observational
        finally:
            await session.close()

    run(client, scenario())


def test_the_observational_writer_records_once_and_never_raises(
    client: Any, world: World, clock: Clock
) -> None:
    async def scenario() -> None:
        (doc,) = await _store(_document(world.tenant, 10_000))
        outcome = await credit_inventory.record_observational_upload(
            None,
            tenant_id=world.tenant,
            category="rag",
            object_ids=[doc],
            upload_id=doc,
            budget_seconds=5,
        )
        assert outcome == "recorded"
        (second,) = await _store(_document(world.sibling, 1))
        assert (
            await credit_inventory.record_observational_upload(
                None,
                tenant_id=world.sibling,
                category="rag",
                object_ids=[second],
                upload_id=second,
                budget_seconds=5,
            )
            == "recorded"
        )
        effects = {row.effect_id: row for row in await _rows(CreditObservationalEffect)}
        assert effects[f"rag:{world.tenant}:{doc}:reserve"].would_debit_mc == 2_000
        assert effects[f"rag:{world.sibling}:{second}:reserve"].would_debit_mc == 1
        maintenance = [e for e in effects.values() if e.effect_kind == "maintenance"]
        # Once per window and category of the builder, whatever the tenant.
        assert sorted(
            (e.effect_id.split(":")[-1], e.would_debit_mc) for e in maintenance
        ) == [
            ("media", 0),
            ("rag", 0),
        ]
        # Nothing financial before the enforcement.
        assert await _rows(CreditAllocation) == []
        assert await _ledger(world.builder) == {}
        # Egress: the delivery and what its overage would cost.
        (media,) = await _store(_media(world.tenant, 1_000))
        assert (
            await credit_inventory.record_observational_egress(
                None,
                tenant_id=world.tenant,
                media_id=media,
                size_bytes=1_000,
                budget_seconds=5,
            )
            == "recorded"
        )
        [delivery] = await _rows(CreditEgressDelivery)
        assert (delivery.mode, delivery.bytes) == ("observational", 1_000)

    run(client, scenario())


@pytest.mark.parametrize("failure", ["factory", "steps", "timeout", "enforced", "gone"])
def test_an_injected_writer_failure_is_logged_and_swallowed(
    client: Any,
    world: World,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    async def scenario() -> str:
        (doc,) = await _store(_document(world.tenant, 10))
        tenant = world.tenant
        factory: Any = None
        if failure == "factory":

            def unavailable() -> Any:
                raise RuntimeError("PostgreSQL unavailable")

            factory = unavailable
        elif failure == "steps":

            async def broken(_self: Any, builder_id: UUID) -> Any:
                raise RuntimeError("constraint violation")

            monkeypatch.setattr(CreditInventoryRepository, "builder_sources", broken)
        elif failure == "timeout":

            async def slow(_self: Any, builder_id: UUID) -> Any:
                await asyncio.sleep(1)
                return []

            monkeypatch.setattr(CreditInventoryRepository, "builder_sources", slow)
        elif failure == "enforced":
            await _enforce(clock.now)
        else:
            tenant = uuid4()
        return await credit_inventory.record_observational_upload(
            None,
            tenant_id=tenant,
            category="rag",
            object_ids=[doc],
            upload_id=doc,
            session_factory=factory,
            budget_seconds=0.2 if failure == "timeout" else 5,
        )

    outcome = run(client, scenario())
    assert (
        outcome
        == {
            "factory": "failed",
            "steps": "failed",
            "timeout": "timeout",
            "enforced": "enforced",
            "gone": "tenant_gone",
        }[failure]
    )
    assert run(client, _rows(CreditObservationalEffect)) == []
    assert run(client, _rows(CreditAllocation)) == []


def test_the_observational_writer_refuses_a_route_session_with_pending_writes(
    client: Any, world: World
) -> None:
    async def scenario() -> str:
        session = get_db_session()
        try:
            session.add(_document(world.tenant, 10))
            await session.execute(select(func.count()).select_from(Tenant))
            return await credit_inventory.record_observational_upload(
                session,
                tenant_id=world.tenant,
                category="rag",
                object_ids=[],
                upload_id=uuid4(),
            )
        finally:
            await session.rollback()
            await session.close()

    assert run(client, scenario()) == "route_busy"


def test_object_copy_worker_is_inert_without_the_effective_runtime(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switch-off/staging must not even construct a database session."""

    def unavailable() -> Any:
        raise AssertionError("an inert copy worker must not claim a job")

    monkeypatch.setattr(
        credit_inventory, "object_copy_runtime_enabled", lambda *_: False
    )
    monkeypatch.setattr(credit_inventory, "qdrant_runtime_enabled", lambda *_: False)

    assert run(client, credit_inventory.run_credit_object_copy_once(unavailable)) == 0


def test_object_copy_lifecycle_is_not_registered_outside_development_or_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credit_inventory.settings, "environment", "staging")

    assert credit_inventory.start_credit_object_workers() is None


def test_object_copy_lifecycle_cancels_cleanly(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credit_inventory.settings, "environment", "test")
    monkeypatch.setattr(
        credit_inventory.settings, "credit_object_worker_interval_seconds", 300
    )
    monkeypatch.setattr(
        credit_inventory, "object_copy_runtime_enabled", lambda *_: False
    )
    monkeypatch.setattr(credit_inventory, "qdrant_runtime_enabled", lambda *_: False)

    async def scenario() -> None:
        task = credit_inventory.start_credit_object_workers()
        assert task is not None and task.get_name() == "credit-object-worker"
        await credit_inventory.stop_credit_object_workers(task)
        assert task.cancelled()

    run(client, scenario())


def test_stale_object_copy_claim_is_recoverable_after_cancellation(
    client: Any, clock: Clock
) -> None:
    async def scenario() -> None:
        job_id = uuid4()
        session = get_db_session()
        try:
            async with session.begin():
                session.add(
                    CreditObjectCopyJob(
                        id=job_id,
                        source_kind="media",
                        source_id=uuid4(),
                        tenant_id=uuid4(),
                        operation="copy",
                        state="running",
                        claimed_at=clock.now
                        - credit_inventory._OBJECT_JOB_LEASE
                        - timedelta(seconds=1),
                    )
                )
        finally:
            await session.close()
        session = get_db_session()
        try:
            claimed = await credit_inventory._claim_copy_jobs(session)
            assert [job.id for job in claimed] == [job_id]
            assert claimed[0].attempts == 1
        finally:
            await session.close()

    run(client, scenario())


def test_qdrant_rebuild_job_is_durable_and_derived_only(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.storage import credit_qdrant

    rebuilt: list[tuple[UUID, UUID, str]] = []

    async def replace(
        *, tenant_id: UUID, source_id: UUID, content: str, **_kwargs: Any
    ) -> int:
        rebuilt.append((tenant_id, source_id, content))
        return 1

    monkeypatch.setattr(
        credit_inventory, "object_copy_runtime_enabled", lambda *_: False
    )
    monkeypatch.setattr(credit_inventory, "qdrant_runtime_enabled", lambda *_: True)
    monkeypatch.setattr(credit_qdrant, "replace_derived_chunks", replace)

    async def scenario() -> None:
        document = _document(world.tenant, 42)
        await _store(document)
        session = get_db_session()
        try:
            await credit_inventory.enqueue_object_copy(
                session,
                source_kind="knowledge",
                source_id=document.id,
                tenant_id=world.tenant,
            )
        finally:
            await session.close()
        assert await credit_inventory.run_credit_object_copy_once(get_db_session) == 1
        assert rebuilt == [(world.tenant, document.id, document.content)]
        session = get_db_session()
        try:
            state = await session.scalar(
                select(CreditQdrantJob.state).where(
                    CreditQdrantJob.source_id == document.id
                )
            )
            assert state == "completed"
        finally:
            await session.close()

    run(client, scenario())


def _fill_object_store(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    """Every S3 setting filled and the switch forced effective: only the
    environment can still close the Group 4b gate."""

    from pydantic import SecretStr

    config = credit_inventory.settings
    monkeypatch.setattr(credit_inventory, "credit_switch_effective", lambda: True)
    monkeypatch.setattr(config, "environment", environment)
    monkeypatch.setattr(config, "credit_object_store_endpoint", "http://s3.invalid")
    monkeypatch.setattr(config, "credit_object_store_bucket", "credit-copies")
    monkeypatch.setattr(config, "credit_object_store_access_key_id", "key")
    monkeypatch.setattr(
        config, "credit_object_store_secret_access_key", SecretStr("secret")
    )
    monkeypatch.setattr(config, "qdrant_host", "qdrant.invalid")
    monkeypatch.setattr(config, "qdrant_collection_name", "credit")


async def _object_job_rows() -> tuple[int, int]:
    session = get_db_session()
    try:
        copies = await session.scalar(
            select(func.count()).select_from(CreditObjectCopyJob)
        )
        vectors = await session.scalar(
            select(func.count()).select_from(CreditQdrantJob)
        )
        return int(copies or 0), int(vectors or 0)
    finally:
        await session.close()


async def _enqueue_both(tenant: UUID, switch_on: bool | None) -> None:
    session = get_db_session()
    try:
        for kind in ("media", "knowledge"):
            await credit_inventory.enqueue_object_copy(
                session,
                source_kind=kind,  # type: ignore[arg-type]
                source_id=uuid4(),
                tenant_id=tenant,
                switch_on=switch_on,
            )
            await credit_inventory.enqueue_source_collection(
                session,
                source_kind=kind,  # type: ignore[arg-type]
                source_id=uuid4(),
                tenant_id=tenant,
                switch_on=switch_on,
            )
    finally:
        await session.close()


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_the_copy_gate_is_closed_in_staging_and_production_with_everything_else_on(
    client: Any,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    """Production runs ``ENVIRONMENT=staging``; neither value may open the
    gate, whatever the switch and the S3 settings say (no ``is_production``)."""

    _fill_object_store(monkeypatch, environment)

    assert credit_inventory.object_copy_runtime_enabled() is False
    assert credit_inventory.object_copy_runtime_enabled(True) is False
    assert credit_inventory.qdrant_runtime_enabled(True) is False
    run(client, _enqueue_both(world.tenant, None))
    run(client, _enqueue_both(world.tenant, True))
    assert run(client, _object_job_rows()) == (0, 0)


def test_the_copy_gate_opens_only_in_development_or_test(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the test above: the same setup with ``ENVIRONMENT=test``
    opens the gate and writes the job rows."""

    _fill_object_store(monkeypatch, "test")

    assert credit_inventory.object_copy_runtime_enabled() is True
    run(client, _enqueue_both(world.tenant, True))
    # copy + gc per kind; a Qdrant rebuild/delete for knowledge only.
    assert run(client, _object_job_rows()) == (4, 2)


def test_the_enqueue_obeys_the_switch_captured_at_request_entry(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that captured the switch ON keeps it for its post-commit
    enqueue, and one that captured OFF writes nothing, whatever the
    process-wide verdict says by then."""

    _fill_object_store(monkeypatch, "test")
    monkeypatch.setattr(credit_inventory, "credit_switch_effective", lambda: True)
    run(client, _enqueue_both(world.tenant, False))
    assert run(client, _object_job_rows()) == (0, 0)

    monkeypatch.setattr(credit_inventory, "credit_switch_effective", lambda: False)
    run(client, _enqueue_both(world.tenant, True))
    assert run(client, _object_job_rows()) == (4, 2)
