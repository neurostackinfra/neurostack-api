"""RAG and media reservations, cycle opening and egress (Group 4a).

PostgreSQL only for the guarantees (SQLite runs the same code in the unit
suites).  The rules are :mod:`src.services.credit_allocations`; this module
applies them in a database transaction.

Modes, read ONCE per request by :func:`read_credit_mode`:

* switch off (or a mocked session): nothing here runs -- ``main``'s path;
* switch on, no ``credit_enforcement_at``: observational.  The route keeps
  ``main``'s answer, limits and allowlist and, after its own transaction
  ended, :func:`record_observational_upload` /
  :func:`record_observational_egress` write best-effort observational
  effects (``rag``, ``media``, ``egress``, ``maintenance``; what WOULD be
  debited) in their own transaction of the process pool, inside one budget
  of :data:`~src.services.credit_metering.OBSERVATIONAL_WRITE_BUDGET_SECONDS`,
  exactly like the Group 3a writer: any failure is logged and swallowed;
* switch on and ``credit_enforcement_at`` set: enforced.  The route applies
  the PDF's limits first, then -- in its own transaction, before its commit
  -- :func:`reserve_upload` (or :func:`charge_egress` before a download is
  answered) debits the reservation.

Enforced transaction, in the lock order of :mod:`src.repositories.credit`:
tenant ``FOR SHARE`` (a standalone tenant: ``credit_pending``) -> account
``FOR UPDATE`` (created on demand) -> the quarantine hold (``credit_pending``,
nothing written) -> lots ``FOR UPDATE`` -> the deadline sweep and the start
catch-up of the ledger -> the window's opening when it has none yet -> the
reservation, egress or reprocessing delta -> effects, ledger rows,
allocations, inventory versions and deliveries.

The opening of a window (deliverable 6, owner decisions C and E) happens at
the first metered effect of the window -- a reservation, a download, a
reprocessing job or a trace debit (:func:`open_window_for_debit`, hooked in
:meth:`~src.services.credit_ledger.CreditLedger.debit`) -- under the account
lock, after the ledger settled open debt and expired lots: per category, the
aggregate target of all the active content (new objects of the triggering
upload excluded) is charged ONCE as ``maintenance`` (one effect per window
and category) -- or, for the window already open at
``credit_enforcement_at``, recorded as a free ``baseline`` (nothing before
the enforcement is charged, nothing is inherited).  A grant never opens a
window ("nunca na concessão"): an early renewal is opened at
``start_of_civil_day(cycle_start)`` at the earliest, against the lots
spendable then.

The readback (owner decision D): the opening and every reservation re-read
``media_assets.size_bytes`` and ``char_length(knowledge_documents.content)``
of the builder's tenants under the account lock and append corrective
versions to ``credit_inventory_versions``; the inventory is never presumed
empty.  A readback that cannot be trusted raises
:class:`InventoryReadbackFailed`: the operation answers ``credit_pending``
and nothing is written.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import aliased
from starlette.requests import HTTPConnection

from src.config.logging import get_logger
from src.config.settings import settings
from src.models.credit_object_copy import (
    CreditObjectCopy,
    CreditObjectCopyJob,
    CreditQdrantJob,
)
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository, TenantScope
from src.repositories.credit_inventory import CreditInventoryRepository, SourceRow
from src.services.credit_allocations import (
    AccountingWindow,
    AllocationCategory,
    Category,
    accounting_window,
    aggregate_target_mc,
    charge_delta,
    egress_allowance_bytes,
    egress_effect_id,
    egress_target_mc,
    maintenance_effect_id,
    observational_maintenance_effect_id,
    opening_source_key,
    rag_units,
    reprocessing_effect_id,
    reservation_effect_id,
)
from src.services.credit_ledger import CreditLedger, LedgerBook, LedgerInvariantError
from src.services.credit_metering import (
    _SET_SERVER_TIMEOUTS,
    OBSERVATIONAL_SERVER_TIMEOUT,
    OBSERVATIONAL_WRITE_BUDGET_SECONDS,
    WRITE_DROPPED_LOG,
    CreditEnforcementUnavailable,
    CreditTenantGone,
    _close_within_budget,
    _invalidate_connection,
    _pool_exhausted,
    _single_connection_pool,
    end_route_transaction,
    enforcement_seen,
)
from src.services.credit_switch import credit_switch_effective, request_credit_switch

logger = get_logger(__name__)

CATEGORIES: Final[tuple[Category, Category]] = ("rag", "media")
MAINTENANCE_EFFECT_KIND: Final = "maintenance"
EGRESS_EFFECT_KIND: Final = "egress"
ENFORCED_LOG: Final = "credit enforced reservation"
ObservationalOutcome = Literal[
    "recorded",
    "replayed",
    "enforced",
    "tenant_gone",
    "route_busy",
    "pool_exhausted",
    "timeout",
    "failed",
]
AllocationStatus = Literal["applied", "duplicate", "credit_pending"]


class InventoryReadbackFailed(RuntimeError):
    """The readback of the builder's content cannot be trusted: the metered
    operation answers ``credit_pending`` and nothing is written."""


class CreditPendingError(Exception):
    """Raised by a route to leave its transaction (rolled back) with 402."""


# ---------------------------------------------------------------------------
# The mode of a request.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreditMode:
    """Captured once per request: the switch, and ``credit_enforcement_at``."""

    switch_on: bool
    enforcement_at: datetime | None = None

    @property
    def enforced(self) -> bool:
        return self.switch_on and self.enforcement_at is not None

    @property
    def observational(self) -> bool:
        return self.switch_on and self.enforcement_at is None


CREDIT_OFF: Final = CreditMode(False)
# The enforcement is one-way (the Group 3b-1 rule D1): once THIS process saw
# ``credit_enforcement_at`` set, an unreadable value is never "not yet".
_enforcement_seen = False


def forget_enforcement_seen() -> None:
    """Back to a process that never saw the enforcement (tests only)."""

    global _enforcement_seen
    _enforcement_seen = False


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def read_credit_mode(request: HTTPConnection, session: object) -> CreditMode:
    """The request's credit mode; call it FIRST in the route, before any ORM
    object is loaded (a failed read rolls the session back).

    Switch off, or a session that is not a real ``AsyncSession`` or a
    request that is not an ASGI one (the suites' fakes): :data:`CREDIT_OFF`,
    no SQL.  A failed read keeps the
    request observational (``main``'s answer) -- unless this process already
    saw ``credit_enforcement_at`` set: then
    :class:`~src.services.credit_metering.CreditEnforcementUnavailable`.
    """

    global _enforcement_seen

    if not isinstance(session, AsyncSession):
        return CREDIT_OFF
    if not isinstance(getattr(request, "scope", None), dict):
        return CREDIT_OFF  # not an ASGI request (a suite's fake)
    if not request_credit_switch(request):
        return CREDIT_OFF
    try:
        enforced_at = await CreditRepository(session).enforcement_at()
    except Exception as error:
        with suppress(Exception):
            await session.rollback()
        if _enforcement_seen or enforcement_seen():
            logger.error(
                ENFORCED_LOG,
                alert="credit_enforcement_unavailable",
                reason="enforcement_unreadable_after_enforced",
                error_type=type(error).__name__,
            )
            raise CreditEnforcementUnavailable(
                "credit_enforcement_at was set and can no longer be read"
            ) from error
        logger.warning(
            ENFORCED_LOG,
            alert="credit_enforcement_read",
            reason="enforcement_unreadable",
            error_type=type(error).__name__,
        )
        return CreditMode(True, None)
    if isinstance(enforced_at, datetime):
        _enforcement_seen = True
        return CreditMode(True, _aware(enforced_at))
    return CreditMode(True, None)


# ---------------------------------------------------------------------------
# The readback: an inventory snapshot of the builder (or tenant).
# ---------------------------------------------------------------------------

ObjectKey = tuple[str, UUID]


@dataclass(frozen=True)
class InventorySnapshot:
    """The builder's stored objects as read now (never presumed empty)."""

    objects: tuple[SourceRow, ...]

    @classmethod
    def of(cls, rows: Iterable[SourceRow]) -> "InventorySnapshot":
        objects = tuple(rows)
        seen: set[tuple[str, UUID, UUID]] = set()
        for row in objects:
            if row.category not in CATEGORIES:
                raise InventoryReadbackFailed(f"unknown category {row.category!r}")
            if type(row.units) is not int or row.units < 0:
                raise InventoryReadbackFailed("an object has no measurable size")
            key = (row.category, row.tenant_id, row.object_id)
            if key in seen:
                raise InventoryReadbackFailed("an object was read twice")
            seen.add(key)
        return cls(objects)

    def _selected(
        self, category: str, exclude: frozenset[ObjectKey]
    ) -> list[SourceRow]:
        return [
            row
            for row in self.objects
            if row.category == category and (category, row.object_id) not in exclude
        ]

    def units(self, category: str, exclude: frozenset[ObjectKey] = frozenset()) -> int:
        return sum(row.units for row in self._selected(category, exclude))

    def digest(self, category: str, exclude: frozenset[ObjectKey] = frozenset()) -> str:
        """SHA-256 of the canonical, sorted composition (the snapshot
        version recorded on the allocation row)."""

        items = sorted(
            [str(row.tenant_id), str(row.object_id), row.units]
            for row in self._selected(category, exclude)
        )
        payload = json.dumps(
            {"category": category, "objects": items},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def readback(
    session: AsyncSession, builder_id: UUID
) -> tuple[InventorySnapshot, list[dict[str, Any]]]:
    """Re-read the builder's content and compute the corrective inventory
    versions (not yet inserted): a new or resized object, an object that
    moved to this builder, and one that left it (``units = 0``)."""

    repository = CreditInventoryRepository(session)
    snapshot = InventorySnapshot.of(await repository.builder_sources(builder_id))
    current = {
        (row.tenant_id, row.category, row.object_id): row.units
        for row in snapshot.objects
    }
    latest = {
        (row.tenant_id, row.category, row.object_id): row
        for row in await repository.latest_versions(
            builder_id, {row.tenant_id for row in snapshot.objects}
        )
    }
    versions: list[dict[str, Any]] = []
    for key, units in sorted(current.items(), key=lambda item: str(item[0])):
        recorded = latest.get(key)
        if (
            recorded is not None
            and recorded.builder_id == builder_id
            and recorded.units == units
        ):
            continue
        versions.append(_version(key, builder_id, units, recorded))
    for key, recorded in sorted(latest.items(), key=lambda item: str(item[0])):
        if key in current or recorded.builder_id != builder_id or recorded.units == 0:
            continue
        versions.append(_version(key, builder_id, 0, recorded))
    return snapshot, versions


def _version(
    key: tuple[UUID, str, UUID], builder_id: UUID, units: int, recorded: Any
) -> dict[str, Any]:
    tenant_id, category, object_id = key
    return {
        "id": uuid4(),
        "builder_id": builder_id,
        "tenant_id": tenant_id,
        "category": category,
        "object_id": object_id,
        "version": 1 if recorded is None else int(recorded.version) + 1,
        "units": units,
    }


# ---------------------------------------------------------------------------
# One window of one builder, under its account lock.
# ---------------------------------------------------------------------------


@dataclass
class WindowState:
    """What a window already covers (the high-water target per category)."""

    window: AccountingWindow
    coverage: dict[str, int] = field(
        default_factory=lambda: {"rag": 0, "media": 0, "egress": 0}
    )
    opened: set[str] = field(default_factory=set)
    reprocessed_characters: int = 0
    source_keys: set[str] = field(default_factory=set)


@dataclass
class _Pending:
    """Rows written after the book (they reference its effects)."""

    versions: list[dict[str, Any]] = field(default_factory=list)
    allocations: list[dict[str, Any]] = field(default_factory=list)
    deliveries: list[dict[str, Any]] = field(default_factory=list)


class WindowWork:
    """The opening and the charges of one window, on one book."""

    def __init__(
        self,
        session: AsyncSession,
        ledger: CreditLedger,
        book: LedgerBook,
        enforcement_at: datetime,
    ) -> None:
        if book.clock is None:
            raise LedgerInvariantError("a swept book always has a clock")
        self.session = session
        self.ledger = ledger
        self.book = book
        self.enforcement_at = _aware(enforcement_at)
        self.at = book.clock
        self.repository = CreditInventoryRepository(session)
        self.window = accounting_window(
            (
                (lot.cycle_start, lot.cycle_end)
                for lot in book.lots
                if lot.lot_kind == "monthly"
                and lot.cycle_start is not None
                and lot.cycle_end is not None
            ),
            self.at,
        )
        self.state = WindowState(self.window)
        self.snapshot: InventorySnapshot | None = None
        self.pending = _Pending()

    async def prepare(
        self, *, need_snapshot: bool, exclude: frozenset[ObjectKey] = frozenset()
    ) -> None:
        """Load the window's coverage; read back the content when the window
        has no opening yet or the caller needs it; open the window."""

        for row in await self.repository.window_allocations(
            self.book.builder_id, self.window.start
        ):
            category = row.category
            self.state.coverage[category] = max(
                self.state.coverage[category], row.target_mc
            )
            if row.allocation_kind in ("baseline", "maintenance"):
                self.state.opened.add(category)
            if row.allocation_kind == "reprocessing":
                self.state.reprocessed_characters += row.units
            self.state.source_keys.add(row.source_key)
        missing = [
            category for category in CATEGORIES if category not in self.state.opened
        ]
        if not missing and not need_snapshot:
            return
        snapshot, versions = await readback(self.session, self.book.builder_id)
        self.snapshot = snapshot
        self.pending.versions.extend(versions)
        for category in missing:
            await self._open(category, snapshot, exclude)

    async def _open(
        self,
        category: Category,
        snapshot: InventorySnapshot,
        exclude: frozenset[ObjectKey],
    ) -> None:
        units = snapshot.units(category, exclude)
        target = aggregate_target_mc(units, category)
        # Owner decision E: the window open AT the enforcement -- one that
        # starts exactly then included ([start, end) contains it) -- gets a
        # free baseline; maintenance starts with a window that begins after.
        maintenance = self.window.starts_at > self.enforcement_at
        effect_id: str | None = None
        charged = 0
        if maintenance and target > 0:
            effect_id = maintenance_effect_id(self.window, category)
            existing = await CreditRepository(self.session).existing_effect_ids(
                self.book.builder_id, (effect_id,)
            )
            if existing:
                raise LedgerInvariantError("a maintenance effect without its opening")
            result = self.book.debit(
                effect_id, target, at=self.at, effect_kind=MAINTENANCE_EFFECT_KIND
            )
            if result.status != "applied":
                raise LedgerInvariantError(f"the maintenance debit was {result.status}")
            charged = target
        self._allocation(
            category,
            "maintenance" if maintenance else "baseline",
            source_key=opening_source_key(self.window, category),
            units=units,
            target=target,
            charged=charged,
            effect_id=effect_id,
            digest=snapshot.digest(category, exclude),
        )
        self.state.coverage[category] = max(self.state.coverage[category], target)
        self.state.opened.add(category)

    def charge(
        self,
        category: AllocationCategory,
        kind: str,
        *,
        source_key: str,
        effect_id: str,
        target: int,
        units: int,
        tenant_id: UUID | None,
        digest: str | None,
        always_record: bool = False,
    ) -> int:
        """Debit ``target - coverage`` (never negative) under ``effect_id``
        and record the allocation; returns the charged millicredits."""

        coverage = self.state.coverage[category]
        delta = charge_delta(target, coverage)
        if delta:
            result = self.book.debit(
                effect_id,
                delta,
                at=self.at,
                effect_kind=category,
                tenant_id=tenant_id,
            )
            if result.status != "applied":
                raise LedgerInvariantError(f"the {category} debit was {result.status}")
        if delta or always_record:
            self._allocation(
                category,
                kind,
                source_key=source_key,
                units=units,
                target=max(coverage, target),
                charged=delta,
                effect_id=effect_id if delta else None,
                digest=digest,
            )
        self.state.coverage[category] = max(coverage, target)
        return delta

    def _allocation(
        self,
        category: str,
        kind: str,
        *,
        source_key: str,
        units: int,
        target: int,
        charged: int,
        effect_id: str | None,
        digest: str | None,
    ) -> None:
        self.state.source_keys.add(source_key)
        self.pending.allocations.append(
            {
                "id": uuid4(),
                "builder_id": self.book.builder_id,
                "account_id": self.book.account_id,
                "window_start": self.window.start,
                "window_end": self.window.end,
                "category": category,
                "allocation_kind": kind,
                "source_key": source_key,
                "units": units,
                "target_mc": target,
                "charged_mc": charged,
                "effect_id": effect_id,
                "snapshot_digest": digest,
            }
        )

    async def write_pending(self) -> None:
        """After :meth:`CreditLedger.persist`: versions, allocations and
        deliveries (the allocations reference the book's effects)."""

        await self.repository.insert_versions(self.pending.versions)
        await self.repository.insert_allocations(self.pending.allocations)
        for delivery in self.pending.deliveries:
            await self.repository.insert_delivery(**delivery)
        self.pending = _Pending()

    async def finish(self) -> None:
        await self.ledger.persist(self.book)
        await self.write_pending()


async def open_window_for_debit(
    ledger: CreditLedger, book: LedgerBook
) -> WindowWork | None:
    """The opening a trace (or any :meth:`CreditLedger.debit`) triggers
    before its own debit, once the enforcement is set; ``None`` when the
    enforcement is not set.  The caller persists the book, then
    :meth:`WindowWork.write_pending`.  Raises
    :class:`InventoryReadbackFailed` (the debit answers
    ``credit_pending``)."""

    enforcement_at = await ledger.repository.enforcement_at()
    if enforcement_at is None:
        return None
    work = WindowWork(ledger.session, ledger, book, enforcement_at)
    await work.prepare(need_snapshot=False)
    return work


# ---------------------------------------------------------------------------
# Enforced operations (the caller's transaction; never a commit).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllocationOutcome:
    status: AllocationStatus
    charged_mc: int = 0
    target_mc: int = 0


_PENDING: Final = AllocationOutcome("credit_pending")


async def _enforced_book(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    at: datetime,
    effect_ids: tuple[str, ...],
) -> tuple[TenantScope, CreditLedger, LedgerBook] | None:
    """Lock order steps 1-4; ``None`` is ``credit_pending``.

    Once the immutable enforcement fence is set, RAG, media, maintenance and
    egress are subject to the same builder binding evidence as message
    ingress.  The coverage check is deliberately before opening a ledger
    book: a reconciliation failure may not create a debit, debt, allocation
    or lifecycle row.  A genuinely empty but reconciled source remains
    usable and reaches the existing debt lifecycle.
    """

    repository = CreditRepository(session)
    scope = await repository.tenant_scope(tenant_id, lock=True)
    if scope is None:
        raise CreditTenantGone("the tenant vanished before its reservation")
    if scope.builder_id is None:
        return None
    from src.services.credit_rollout import CreditRolloutService

    if not (
        await CreditRolloutService(session).binding_coverage(scope.builder_id, at=at)
    ).usable:
        return None
    ledger = CreditLedger(session)
    book = await ledger.open_book(scope.builder_id, at=at, effect_ids=effect_ids)
    if book.spend_hold.active:
        return None
    return scope, ledger, book


def _log_pending(reason: str, tenant_id: UUID, **extra: Any) -> None:
    logger.info(ENFORCED_LOG, reason=reason, tenant_id=str(tenant_id), **extra)


async def reserve_upload(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    category: Category,
    object_ids: Iterable[UUID],
    upload_id: UUID,
    enforcement_at: datetime,
    at: datetime | None = None,
) -> AllocationOutcome:
    """The reservation of one upload (its new objects already flushed in
    this transaction): the window's opening, then ``target - coverage``.

    ``upload_id`` names the effect (a document, a batch, a media asset): an
    internal retry of the same upload is a duplicate.  Validation (format,
    size, count) happens BEFORE this call."""

    effect_id = reservation_effect_id(tenant_id, category, upload_id)
    opened = await _enforced_book(
        session, tenant_id, at=at or _now(), effect_ids=(effect_id,)
    )
    if opened is None:
        _log_pending("standalone_or_hold", tenant_id, category=category)
        return _PENDING
    _scope, ledger, book = opened
    if book.has_effect(effect_id):
        return AllocationOutcome("duplicate")
    work = WindowWork(session, ledger, book, enforcement_at)
    exclude = frozenset((category, object_id) for object_id in object_ids)
    try:
        await work.prepare(need_snapshot=True, exclude=exclude)
    except InventoryReadbackFailed as error:
        _log_pending("readback_failed", tenant_id, error=str(error))
        return _PENDING
    assert work.snapshot is not None
    units = work.snapshot.units(category)
    if category == "rag":
        units = rag_units(units, work.state.reprocessed_characters)
    target = aggregate_target_mc(units, category)
    charged = work.charge(
        category,
        "reservation",
        source_key=effect_id,
        effect_id=effect_id,
        target=target,
        units=units,
        tenant_id=tenant_id,
        digest=work.snapshot.digest(category),
    )
    await work.finish()
    return AllocationOutcome("applied", charged_mc=charged, target_mc=target)


async def charge_egress(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    media_id: UUID,
    size_bytes: int,
    enforcement_at: datetime,
    at: datetime | None = None,
    delivery_id: UUID | None = None,
) -> AllocationOutcome:
    """One download, BEFORE it is answered (owner decision G): the window's
    opening, the delivery, and the overage beyond 200 MB per media credit
    covered.  Every request is a delivery (server id, owner decision F)."""

    delivery = delivery_id or uuid4()
    effect_id = egress_effect_id(tenant_id, media_id, delivery)
    opened = await _enforced_book(
        session, tenant_id, at=at or _now(), effect_ids=(effect_id,)
    )
    if opened is None:
        _log_pending("standalone_or_hold", tenant_id, category="egress")
        return _PENDING
    _scope, ledger, book = opened
    if book.has_effect(effect_id):
        return AllocationOutcome("duplicate")
    work = WindowWork(session, ledger, book, enforcement_at)
    try:
        await work.prepare(need_snapshot=False)
    except InventoryReadbackFailed as error:
        _log_pending("readback_failed", tenant_id, error=str(error))
        return _PENDING
    delivered = await work.repository.delivered_bytes(
        window_start=work.window.start, mode="enforced", builder_id=book.builder_id
    )
    allowance = egress_allowance_bytes(work.state.coverage["media"])
    target = egress_target_mc(delivered + size_bytes, allowance)
    charged = work.charge(
        "egress",
        "egress",
        source_key=effect_id,
        effect_id=effect_id,
        target=target,
        units=size_bytes,
        tenant_id=tenant_id,
        digest=None,
    )
    work.pending.deliveries.append(
        {
            "tenant_id": tenant_id,
            "builder_id": book.builder_id,
            "media_id": media_id,
            "delivery_id": delivery,
            "window_start": work.window.start,
            "size_bytes": size_bytes,
            "mode": "enforced",
        }
    )
    await work.finish()
    return AllocationOutcome("applied", charged_mc=charged, target_mc=target)


async def charge_rag_reprocessing(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    object_id: UUID,
    job_id: str,
    characters: int,
    enforcement_at: datetime,
    at: datetime | None = None,
) -> AllocationOutcome:
    """One reprocessing (re-index) job of one document (the Group 4b jobs
    call it): included up to 100 % of the window's active characters, the
    excess charged through the aggregate delta; a retry of the same job is a
    duplicate."""

    if type(characters) is not int or characters < 0:
        raise ValueError("characters must be a non-negative integer")
    source_key = reprocessing_effect_id(tenant_id, object_id, job_id)
    opened = await _enforced_book(
        session, tenant_id, at=at or _now(), effect_ids=(source_key,)
    )
    if opened is None:
        _log_pending("standalone_or_hold", tenant_id, category="rag")
        return _PENDING
    _scope, ledger, book = opened
    work = WindowWork(session, ledger, book, enforcement_at)
    try:
        await work.prepare(need_snapshot=True)
    except InventoryReadbackFailed as error:
        _log_pending("readback_failed", tenant_id, error=str(error))
        return _PENDING
    if source_key in work.state.source_keys or book.has_effect(source_key):
        return AllocationOutcome("duplicate")
    assert work.snapshot is not None
    reprocessed = work.state.reprocessed_characters + characters
    units = rag_units(work.snapshot.units("rag"), reprocessed)
    target = aggregate_target_mc(units, "rag")
    charged = work.charge(
        "rag",
        "reprocessing",
        source_key=source_key,
        effect_id=source_key,
        target=target,
        units=characters,
        tenant_id=tenant_id,
        digest=work.snapshot.digest("rag"),
        always_record=True,
    )
    await work.finish()
    return AllocationOutcome("applied", charged_mc=charged, target_mc=target)


async def lock_tenant_for_reservation(session: AsyncSession, tenant_id: UUID) -> bool:
    """Lock order step 1 before the upload's own writes: ``False`` for a
    standalone tenant (``credit_pending`` before anything is written)."""

    scope = await CreditRepository(session).tenant_scope(tenant_id, lock=True)
    if scope is None:
        raise CreditTenantGone("the tenant vanished before its upload")
    return scope.builder_id is not None


# ---------------------------------------------------------------------------
# Additional object copies and derived Qdrant work (Group 4b).
#
# All entries below are intentionally after the Group 4a accounting API: they
# do not participate in credit debits, nor can a failure change an API answer.
# Source bytes stay in PostgreSQL.  A job is durable so source deletion and a
# process crash merely delay the best-effort external copy/cleanup.
# ---------------------------------------------------------------------------

_COPY_BATCH_SIZE: Final = 16
_OBJECT_JOB_LEASE: Final = timedelta(minutes=5)


def object_copy_runtime_enabled(switch_on: bool | None = None) -> bool:
    """Whether Group 4b may touch external stores, fail-closed.

    There is no production exception: rollout opening owns that decision.
    Empty settings leave development/test harmless and inert.
    ``switch_on`` is a request's captured switch verdict; ``None`` (the
    worker) reads the process-wide verdict.
    """

    effective = credit_switch_effective() if switch_on is None else switch_on
    return bool(
        effective
        and settings.environment in ("development", "test")
        and settings.credit_object_store_endpoint
        and settings.credit_object_store_bucket
        and settings.credit_object_store_access_key_id
        and settings.credit_object_store_secret_access_key.get_secret_value()
    )


def qdrant_runtime_enabled(switch_on: bool | None = None) -> bool:
    """Derived-vector work has the same switch/environment dominance but
    shares the explicit additional-store configuration boundary."""

    return bool(
        object_copy_runtime_enabled(switch_on)
        and settings.qdrant_host
        and settings.qdrant_collection_name
    )


async def enqueue_object_copy(
    session: AsyncSession,
    *,
    source_kind: Literal["media", "knowledge"],
    source_id: UUID,
    tenant_id: UUID,
    switch_on: bool | None = None,
) -> None:
    """Persist post-commit work only when the captured switch is effective.

    Callers invoke this after the authoritative route transaction committed,
    passing the switch verdict their request captured at entry
    (``request_credit_switch``/``CreditMode.switch_on``).  The queue insert
    is best effort; it never rolls back the primary content write and a
    repeated enqueue is safe (a completed duplicate does nothing).
    """

    copy_enabled = object_copy_runtime_enabled(switch_on)
    qdrant_enabled = qdrant_runtime_enabled(switch_on)
    if not copy_enabled and not qdrant_enabled:
        return
    if copy_enabled:
        copy_exists = await session.scalar(
            select(CreditObjectCopyJob.id).where(
                CreditObjectCopyJob.source_kind == source_kind,
                CreditObjectCopyJob.source_id == source_id,
                CreditObjectCopyJob.operation == "copy",
                CreditObjectCopyJob.state.in_(("pending", "running")),
            )
        )
        if copy_exists is None:
            session.add(
                CreditObjectCopyJob(
                    source_kind=source_kind,
                    source_id=source_id,
                    tenant_id=tenant_id,
                    operation="copy",
                )
            )
    # Qdrant's rebuildable representation is document text.  Raw media stays
    # in PostgreSQL/S3 copies and never gets misrepresented as searchable
    # text merely because the optional vector service is reachable.
    if qdrant_enabled and source_kind == "knowledge":
        qdrant_exists = await session.scalar(
            select(CreditQdrantJob.id).where(
                CreditQdrantJob.source_kind == source_kind,
                CreditQdrantJob.source_id == source_id,
                CreditQdrantJob.operation == "rebuild",
                CreditQdrantJob.state.in_(("pending", "running")),
            )
        )
        if qdrant_exists is None:
            session.add(
                CreditQdrantJob(
                    source_kind=source_kind,
                    source_id=source_id,
                    tenant_id=tenant_id,
                    operation="rebuild",
                )
            )
    await session.commit()


async def enqueue_source_collection(
    session: AsyncSession,
    *,
    source_kind: Literal["media", "knowledge"],
    source_id: UUID,
    tenant_id: UUID,
    switch_on: bool | None = None,
) -> None:
    """Schedule source-GC after its authoritative delete committed.

    ``switch_on`` is the verdict the deleting request captured at entry."""

    copy_enabled = object_copy_runtime_enabled(switch_on)
    qdrant_enabled = qdrant_runtime_enabled(switch_on)
    if not copy_enabled and not qdrant_enabled:
        return
    jobs: list[tuple[Any, str]] = []
    if copy_enabled:
        jobs.append((CreditObjectCopyJob, "gc"))
    if qdrant_enabled and source_kind == "knowledge":
        jobs.append((CreditQdrantJob, "delete"))
    for model, operation in jobs:
        exists = await session.scalar(
            select(model.id).where(
                model.source_kind == source_kind,
                model.source_id == source_id,
                model.operation == operation,
                model.state.in_(("pending", "running")),
            )
        )
        if exists is None:
            session.add(
                model(
                    source_kind=source_kind,
                    source_id=source_id,
                    tenant_id=tenant_id,
                    operation=operation,
                )
            )
    await session.commit()


async def _claim_copy_jobs(session: AsyncSession) -> list[CreditObjectCopyJob]:
    stale_before = _now() - _OBJECT_JOB_LEASE
    statement = (
        select(CreditObjectCopyJob)
        .where(
            or_(
                CreditObjectCopyJob.state == "pending",
                and_(
                    CreditObjectCopyJob.state == "running",
                    CreditObjectCopyJob.claimed_at < stale_before,
                ),
            )
        )
        .order_by(CreditObjectCopyJob.created_at, CreditObjectCopyJob.id)
        .limit(_COPY_BATCH_SIZE)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    jobs = list((await session.execute(statement)).scalars())
    now = _now()
    for job in jobs:
        job.state = "running"
        job.claimed_at = now
        job.attempts += 1
    await session.commit()
    return jobs


async def _enqueue_orphan_copy_collection(session: AsyncSession) -> None:
    """Discover CASCADE-orphaned registry rows.

    Clinic/builder deletion removes source rows through database cascades and
    therefore cannot enqueue per-source work in the request transaction. The
    registry deliberately has no source FK, so this bounded scan is the
    durable recovery path. It does nothing while the Group 4b runtime is off.

    "Source missing" and "no collection job yet" are decided in SQL, before
    the batch limit: live copies (however many, however old) never occupy a
    batch, so an orphan is always reachable.  A ``pending``/``running`` job
    counts as queued; a ``held`` job only while a ``held`` copy of the same
    source exists (the hold release re-queues it).  A ``held`` job with no
    ``held`` copy left -- the S3 hold-ON call failed, so the copy stayed
    ``ready`` -- is re-queued here instead of duplicated.
    """

    def gc_job(states: tuple[str, ...]) -> Any:
        return (
            select(CreditObjectCopyJob.id)
            .where(
                CreditObjectCopyJob.source_kind == CreditObjectCopy.source_kind,
                CreditObjectCopyJob.source_id == CreditObjectCopy.source_id,
                CreditObjectCopyJob.operation == "gc",
                CreditObjectCopyJob.state.in_(states),
            )
            .exists()
        )

    held_sibling = aliased(CreditObjectCopy)
    held_copy = (
        select(held_sibling.id)
        .where(
            held_sibling.source_kind == CreditObjectCopy.source_kind,
            held_sibling.source_id == CreditObjectCopy.source_id,
            held_sibling.state == "held",
        )
        .exists()
    )
    gc_queued = or_(gc_job(("pending", "running")), and_(gc_job(("held",)), held_copy))
    sources: dict[tuple[str, UUID], UUID] = {}
    for source_kind, model in (
        ("media", MediaAsset),
        ("knowledge", KnowledgeDocument),
    ):
        source_exists = (
            select(model.id)
            .where(
                model.id == CreditObjectCopy.source_id,
                model.tenant_id == CreditObjectCopy.tenant_id,
            )
            .exists()
        )
        rows = await session.execute(
            select(CreditObjectCopy.source_id, CreditObjectCopy.tenant_id)
            .where(
                CreditObjectCopy.state == "ready",
                CreditObjectCopy.source_kind == source_kind,
                ~source_exists,
                ~gc_queued,
            )
            .order_by(CreditObjectCopy.created_at, CreditObjectCopy.id)
            .limit(_COPY_BATCH_SIZE)
        )
        for source_id, tenant_id in rows:
            sources.setdefault((source_kind, source_id), tenant_id)
    for (source_kind, source_id), tenant_id in sources.items():
        if not await _requeue_held_collection(session, source_kind, source_id):
            session.add(
                CreditObjectCopyJob(
                    source_kind=source_kind,
                    source_id=source_id,
                    tenant_id=tenant_id,
                    operation="gc",
                )
            )
        if source_kind == "knowledge" and qdrant_runtime_enabled():
            qdrant_exists = await session.scalar(
                select(CreditQdrantJob.id).where(
                    CreditQdrantJob.source_kind == source_kind,
                    CreditQdrantJob.source_id == source_id,
                    CreditQdrantJob.operation == "delete",
                    CreditQdrantJob.state.in_(("pending", "running")),
                )
            )
            if qdrant_exists is None:
                session.add(
                    CreditQdrantJob(
                        source_kind=source_kind,
                        source_id=source_id,
                        tenant_id=tenant_id,
                        operation="delete",
                    )
                )
    await session.commit()


async def _finish_copy_job(
    session: AsyncSession, job_id: UUID, *, state: str, error: str | None = None
) -> None:
    """Move one job to its outcome, committed here.

    The job table keys ``(source_kind, source_id, operation, state)``, so a
    second ``completed``/``failed``/``held`` job of one source cannot take
    that state as its own row: the existing outcome row is refreshed (the
    latest error and time) and this job's row is removed -- one outcome row
    per source and state, never an ``IntegrityError`` leaving the job
    ``running`` forever.  A concurrent twin committed between the read and
    the write is retried once through the same merge.

    Callers commit their own writes first: this function may roll back.
    """

    completed_at = _now() if state != "failed" else None
    for attempt in range(2):
        try:
            job = (
                await session.execute(
                    select(
                        CreditObjectCopyJob.source_kind,
                        CreditObjectCopyJob.source_id,
                        CreditObjectCopyJob.operation,
                    ).where(CreditObjectCopyJob.id == job_id)
                )
            ).one_or_none()
            if job is None:
                await session.commit()
                return
            twin = await session.scalar(
                select(CreditObjectCopyJob.id).where(
                    CreditObjectCopyJob.source_kind == job.source_kind,
                    CreditObjectCopyJob.source_id == job.source_id,
                    CreditObjectCopyJob.operation == job.operation,
                    CreditObjectCopyJob.state == state,
                    CreditObjectCopyJob.id != job_id,
                )
            )
            if twin is None:
                await session.execute(
                    update(CreditObjectCopyJob)
                    .where(CreditObjectCopyJob.id == job_id)
                    .values(state=state, last_error=error, completed_at=completed_at)
                )
            else:
                await session.execute(
                    update(CreditObjectCopyJob)
                    .where(CreditObjectCopyJob.id == twin)
                    .values(last_error=error, completed_at=completed_at)
                )
                await session.execute(
                    delete(CreditObjectCopyJob).where(CreditObjectCopyJob.id == job_id)
                )
            await session.commit()
            return
        except IntegrityError:
            await session.rollback()
            if attempt:
                raise


async def _claim_qdrant_jobs(session: AsyncSession) -> list[CreditQdrantJob]:
    stale_before = _now() - _OBJECT_JOB_LEASE
    statement = (
        select(CreditQdrantJob)
        .where(
            or_(
                CreditQdrantJob.state == "pending",
                and_(
                    CreditQdrantJob.state == "running",
                    CreditQdrantJob.claimed_at < stale_before,
                ),
            )
        )
        .order_by(CreditQdrantJob.created_at, CreditQdrantJob.id)
        .limit(_COPY_BATCH_SIZE)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    jobs = list((await session.execute(statement)).scalars())
    for job in jobs:
        job.state = "running"
        job.claimed_at = _now()
        job.attempts += 1
    await session.commit()
    return jobs


async def _finish_qdrant_job(
    session: AsyncSession, job_id: UUID, *, state: str, error: str | None = None
) -> None:
    await session.execute(
        update(CreditQdrantJob)
        .where(CreditQdrantJob.id == job_id)
        .values(
            state=state,
            last_error=error,
            completed_at=_now() if state != "failed" else None,
        )
    )
    await session.commit()


async def _run_qdrant_job(
    factory: Callable[[], AsyncSession], job: CreditQdrantJob
) -> None:
    """Operate only rebuildable document chunks, never source content."""

    from src.storage.credit_qdrant import (
        CreditQdrantError,
        delete_derived_chunks,
        replace_derived_chunks,
    )

    try:
        if job.operation == "delete":
            await delete_derived_chunks(
                tenant_id=job.tenant_id, source_id=job.source_id
            )
        else:
            async with factory() as session:
                content = await session.scalar(
                    select(KnowledgeDocument.content).where(
                        KnowledgeDocument.id == job.source_id,
                        KnowledgeDocument.tenant_id == job.tenant_id,
                    )
                )
            # A concurrent source deletion is not a Qdrant failure.  Its
            # paired delete job (or the next source delete) owns collection.
            if content is not None:
                await replace_derived_chunks(
                    tenant_id=job.tenant_id,
                    source_id=job.source_id,
                    content=str(content),
                )
    except CreditQdrantError as error:
        async with factory() as session:
            await _finish_qdrant_job(
                session, job.id, state="failed", error=str(error)[:500]
            )
        return
    async with factory() as session:
        await _finish_qdrant_job(session, job.id, state="completed")


async def _source_bytes(
    session: AsyncSession, job: CreditObjectCopyJob
) -> tuple[bytes, bytes, UUID | None] | None:
    """Load source only for a worker; never to serve an application request."""

    if job.source_kind == "media":
        row = (
            await session.execute(
                select(MediaAsset.payload, MediaAsset.sha256, Tenant.builder_id)
                .join(Tenant, Tenant.id == MediaAsset.tenant_id)
                .where(
                    MediaAsset.id == job.source_id,
                    MediaAsset.tenant_id == job.tenant_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return bytes(row.payload), bytes(row.sha256), row.builder_id
    knowledge_row = (
        await session.execute(
            select(KnowledgeDocument.content, Tenant.builder_id)
            .join(Tenant, Tenant.id == KnowledgeDocument.tenant_id)
            .where(
                KnowledgeDocument.id == job.source_id,
                KnowledgeDocument.tenant_id == job.tenant_id,
            )
        )
    ).one_or_none()
    if knowledge_row is None:
        return None
    payload = str(knowledge_row.content).encode("utf-8")
    return payload, hashlib.sha256(payload).digest(), knowledge_row.builder_id


async def _copy_one(
    factory: Callable[[], AsyncSession], job: CreditObjectCopyJob
) -> None:
    from src.storage.credit_object_store import ObjectCopyError, put_immutable_copy

    async with factory() as session:
        source = await _source_bytes(session, job)
    if source is None:
        async with factory() as session:
            await _finish_copy_job(session, job.id, state="completed")
        return
    payload, digest, builder_id = source
    try:
        stored = await asyncio.to_thread(
            put_immutable_copy,
            tenant_id=job.tenant_id,
            object_id=job.source_id,
            payload=payload,
            expected_sha256=digest,
        )
    except ObjectCopyError as error:
        logger.warning(
            "credit object copy failed",
            alert="credit_object_copy",
            error_type=type(error).__name__,
        )
        async with factory() as session:
            await _finish_copy_job(
                session, job.id, state="failed", error=str(error)[:500]
            )
        return
    async with factory() as session:
        existing = (
            await session.execute(
                select(CreditObjectCopy.id).where(
                    CreditObjectCopy.source_kind == job.source_kind,
                    CreditObjectCopy.source_id == job.source_id,
                    CreditObjectCopy.content_sha256 == stored.content_sha256,
                )
            )
        ).first()
        if existing is None:
            session.add(
                CreditObjectCopy(
                    source_kind=job.source_kind,
                    source_id=job.source_id,
                    tenant_id=job.tenant_id,
                    builder_id=builder_id,
                    object_key=stored.object_key,
                    version_id=stored.version_id,
                    etag=stored.etag,
                    content_sha256=stored.content_sha256,
                )
            )
            await session.commit()
        await _finish_copy_job(session, job.id, state="completed")


@dataclass(frozen=True)
class _CopyHoldScopes:
    """The credit legal holds (``credit_hold_events``) folded per scope."""

    held_builders: frozenset[UUID] = frozenset()
    held_tenants: frozenset[UUID] = frozenset()
    released_builders: frozenset[UUID] = frozenset()
    released_tenants: frozenset[UUID] = frozenset()

    @property
    def empty(self) -> bool:
        return not (
            self.held_builders
            or self.held_tenants
            or self.released_builders
            or self.released_tenants
        )


async def _copy_hold_scopes(session: AsyncSession) -> _CopyHoldScopes:
    """Fold every hold intent with Group 5's own rule (``fold_holds``: a
    hold counts from its request until its release).  Hold events are rare
    and append-only, so the whole stream is read once per worker pass."""

    from src.models.content_retention import CreditHoldEvent
    from src.services.content_retention import fold_holds

    events = list((await session.execute(select(CreditHoldEvent))).scalars())
    held_builders: set[UUID] = set()
    held_tenants: set[UUID] = set()
    released_builders: set[UUID] = set()
    released_tenants: set[UUID] = set()
    for state in fold_holds(events).values():
        origin = state.requested
        if origin is None:
            continue
        if state.effective:
            builders, tenants = held_builders, held_tenants
        elif state.released is not None:
            builders, tenants = released_builders, released_tenants
        else:  # pragma: no cover - fold_holds: effective or released
            continue
        if origin.scope == "builder":
            builders.add(origin.builder_id)
        else:
            tenants.add(UUID(origin.scope.split(":", 1)[1]))
    return _CopyHoldScopes(
        frozenset(held_builders),
        frozenset(held_tenants),
        frozenset(released_builders),
        frozenset(released_tenants),
    )


def _copy_covered(builders: Iterable[UUID], tenants: Iterable[UUID]) -> Any:
    """Copies inside a builder-wide scope (the builder recorded at write
    time or the tenant's current builder) or a tenant scope.  NULL-safe: a
    standalone copy is never "not covered" by NULL comparison."""

    builder_ids = list(builders)
    return or_(
        and_(
            CreditObjectCopy.builder_id.is_not(None),
            CreditObjectCopy.builder_id.in_(builder_ids),
        ),
        CreditObjectCopy.tenant_id.in_(list(tenants)),
        CreditObjectCopy.tenant_id.in_(
            select(Tenant.id).where(Tenant.builder_id.in_(builder_ids))
        ),
    )


async def _copy_under_credit_hold(
    session: AsyncSession, copy: CreditObjectCopy
) -> bool:
    """PostgreSQL is the authority of the credit legal hold: a hold on the
    builder recorded with the copy, on the tenant's current builder, or on
    the tenant itself protects the copy, whatever the S3 side says."""

    from src.repositories.content_retention import ContentRetentionRepository
    from src.services.content_retention import hold_effective

    current_builder = await session.scalar(
        select(Tenant.builder_id).where(Tenant.id == copy.tenant_id)
    )
    repository = ContentRetentionRepository(session)
    scopes = ("builder", f"tenant:{copy.tenant_id}")
    for builder_id in {copy.builder_id, current_builder} - {None}:
        assert builder_id is not None
        if hold_effective(await repository.hold_events(builder_id, scopes)):
            return True
    return False


async def _set_copy_legal_hold(copy: CreditObjectCopy, *, enabled: bool) -> bool:
    """``PutObjectLegalHold`` ON/OFF on the registered exact version."""

    from botocore.exceptions import BotoCoreError

    from src.storage.credit_object_store import (
        ObjectCopyError,
        StoredCopy,
        set_legal_hold,
    )

    try:
        await asyncio.to_thread(
            set_legal_hold,
            copy=StoredCopy(
                copy.object_key, copy.version_id, copy.etag, copy.content_sha256
            ),
            enabled=enabled,
        )
    except (ObjectCopyError, BotoCoreError) as error:
        logger.warning(
            "credit object copy legal hold update failed",
            alert="credit_object_copy_hold",
            enabled=enabled,
            error_type=type(error).__name__,
        )
        return False
    return True


async def _mark_copy(
    factory: Callable[[], AsyncSession], copy_id: UUID, *, before: str, after: str
) -> bool:
    """Compare-and-set one copy's state: ``True`` only for the pass that
    moved it (two concurrent passes never both act on one copy)."""

    async with factory() as session:
        moved = await session.scalar(
            update(CreditObjectCopy)
            .where(CreditObjectCopy.id == copy_id, CreditObjectCopy.state == before)
            .values(state=after, updated_at=_now())
            .returning(CreditObjectCopy.id)
        )
        await session.commit()
    return moved is not None


async def _hold_copy(
    factory: Callable[[], AsyncSession], copy: CreditObjectCopy
) -> None:
    """Record ``held`` BEFORE ``PutObjectLegalHold=ON``: a crash in between
    leaves a ``held`` copy, which the release scan sees (an OFF on a version
    never set ON is harmless), never a ``ready`` copy with the S3 hold ON.
    A failed ON reverts to ``ready`` so the next pass retries it."""

    if not await _mark_copy(factory, copy.id, before="ready", after="held"):
        return
    if not await _set_copy_legal_hold(copy, enabled=True):
        await _mark_copy(factory, copy.id, before="held", after="ready")


async def _requeue_held_collection(
    session: AsyncSession, source_kind: str, source_id: UUID
) -> bool:
    """Put a source's ``held`` collection job back to ``pending`` (one row
    per source: the job table's state is part of its key).  ``True`` when a
    ``held`` job exists (re-queued now, or left because a job of the source
    is already pending/running).  Not committed here."""

    held_job = await session.scalar(
        select(CreditObjectCopyJob.id).where(
            CreditObjectCopyJob.source_kind == source_kind,
            CreditObjectCopyJob.source_id == source_id,
            CreditObjectCopyJob.operation == "gc",
            CreditObjectCopyJob.state == "held",
        )
    )
    if held_job is None:
        return False
    active = await session.scalar(
        select(CreditObjectCopyJob.id).where(
            CreditObjectCopyJob.source_kind == source_kind,
            CreditObjectCopyJob.source_id == source_id,
            CreditObjectCopyJob.operation == "gc",
            CreditObjectCopyJob.state.in_(("pending", "running")),
        )
    )
    if active is None:
        # Guarded: a concurrent worker that already claimed this row
        # (``running``) is never reset to ``pending``.
        await session.execute(
            update(CreditObjectCopyJob)
            .where(
                CreditObjectCopyJob.id == held_job,
                CreditObjectCopyJob.state == "held",
            )
            .values(
                state="pending", last_error=None, claimed_at=None, completed_at=None
            )
        )
    return True


async def _reconcile_copy_legal_holds(factory: Callable[[], AsyncSession]) -> None:
    """The legal-hold job of the S3 copies, driven by ``credit_hold_events``.

    A hold in force (from its request, as Group 5 counts it) moves every
    ``ready`` copy it covers to ``held`` and then puts
    ``PutObjectLegalHold=ON`` on its version.  A ``held`` copy that no hold
    in force covers, but that lies in the scope of at least one RELEASED
    credit hold, gets ``PutObjectLegalHold=OFF``, is ``ready`` again, and a
    collection held for it is re-queued (a copy whose source is gone and
    that has no job is found by the orphan scan).  The S3 flag has no owner:
    the OFF lifts whatever S3 legal hold the version carries, including one
    placed outside this worker, once a released credit hold covers that
    scope; a ``held`` copy in a scope no credit hold ever covered is left
    alone.  Both selections are in SQL, bounded and oldest first; every
    state change is a compare-and-set.  Runs only under the Group 4b gate.
    """

    async with factory() as session:
        scopes = await _copy_hold_scopes(session)
        if scopes.empty:
            return
        protected = _copy_covered(scopes.held_builders, scopes.held_tenants)
        to_hold = list(
            (
                await session.execute(
                    select(CreditObjectCopy)
                    .where(CreditObjectCopy.state == "ready", protected)
                    .order_by(CreditObjectCopy.created_at, CreditObjectCopy.id)
                    .limit(_COPY_BATCH_SIZE)
                )
            ).scalars()
        )
        to_release = list(
            (
                await session.execute(
                    select(CreditObjectCopy)
                    .where(
                        CreditObjectCopy.state == "held",
                        ~protected,
                        _copy_covered(
                            scopes.released_builders, scopes.released_tenants
                        ),
                    )
                    .order_by(CreditObjectCopy.created_at, CreditObjectCopy.id)
                    .limit(_COPY_BATCH_SIZE)
                )
            ).scalars()
        )
    for copy in to_hold:
        await _hold_copy(factory, copy)
    for copy in to_release:
        # OFF first, then the state: a crash in between leaves a ``held``
        # copy whose OFF the next pass repeats.
        if not await _set_copy_legal_hold(copy, enabled=False):
            continue
        async with factory() as session:
            released = await session.scalar(
                update(CreditObjectCopy)
                .where(CreditObjectCopy.id == copy.id, CreditObjectCopy.state == "held")
                .values(state="ready", updated_at=_now())
                .returning(CreditObjectCopy.id)
            )
            if released is not None:
                await _requeue_held_collection(
                    session, copy.source_kind, copy.source_id
                )
            await session.commit()


async def _collect_one(
    factory: Callable[[], AsyncSession], job: CreditObjectCopyJob
) -> None:
    from botocore.exceptions import BotoCoreError

    from src.storage.credit_object_store import (
        ObjectCopyError,
        ObjectCopyHeld,
        StoredCopy,
        delete_exact_copy,
    )

    async with factory() as session:
        copies = list(
            (
                await session.execute(
                    select(CreditObjectCopy).where(
                        CreditObjectCopy.source_kind == job.source_kind,
                        CreditObjectCopy.source_id == job.source_id,
                        CreditObjectCopy.tenant_id == job.tenant_id,
                        CreditObjectCopy.state == "ready",
                    )
                )
            ).scalars()
        )
    held = False
    for copy in copies:
        # The PostgreSQL hold is re-checked for EACH copy just before its
        # delete (a hold requested mid-batch is seen): the S3-side legal
        # hold may not be placed yet, or may have failed.  Fail closed: an
        # unreadable hold state deletes nothing.
        try:
            async with factory() as session:
                protected = await _copy_under_credit_hold(session, copy)
        except Exception as error:
            logger.warning(
                "credit object copy hold check failed",
                alert="credit_object_copy_held",
                source_kind=copy.source_kind,
                error_type=type(error).__name__,
            )
            async with factory() as session:
                await _finish_copy_job(
                    session, job.id, state="failed", error="hold check failed"
                )
            return
        if protected:
            held = True
            logger.warning(
                "credit object copy collection held",
                alert="credit_object_copy_held",
                source_kind=copy.source_kind,
                reason="credit_legal_hold",
            )
            await _hold_copy(factory, copy)
            continue
        try:
            await asyncio.to_thread(
                delete_exact_copy,
                copy=StoredCopy(
                    copy.object_key, copy.version_id, copy.etag, copy.content_sha256
                ),
            )
        except ObjectCopyHeld as error:
            logger.warning(
                "credit object copy collection held",
                alert="credit_object_copy_held",
                source_kind=copy.source_kind,
            )
            await _mark_copy(factory, copy.id, before="ready", after="held")
            async with factory() as session:
                await _finish_copy_job(
                    session, job.id, state="held", error=str(error)[:500]
                )
            return
        except (ObjectCopyError, BotoCoreError) as error:
            async with factory() as session:
                await _finish_copy_job(
                    session, job.id, state="failed", error=str(error)[:500]
                )
            return
        await _mark_copy(factory, copy.id, before="ready", after="deleted")
    async with factory() as session:
        # A copy still ``held`` (now or from an earlier pass) keeps the job
        # ``held``: the hold release re-queues this same row.
        still_held = await session.scalar(
            select(CreditObjectCopy.id)
            .where(
                CreditObjectCopy.source_kind == job.source_kind,
                CreditObjectCopy.source_id == job.source_id,
                CreditObjectCopy.state == "held",
            )
            .limit(1)
        )
        if held or still_held is not None:
            await _finish_copy_job(
                session, job.id, state="held", error="credit legal hold in force"
            )
        else:
            await _finish_copy_job(session, job.id, state="completed")


async def run_credit_object_copy_once(
    session_factory: Callable[[], AsyncSession] | None = None,
) -> int:
    """Claim a bounded batch using ``SKIP LOCKED`` and run no work when off.

    Each step is isolated: a legal-hold reconcile or a job that raises is
    logged (alert, error type only) and the rest of the pass still runs; a
    job left ``running`` is reclaimed after its lease."""

    copy_enabled = object_copy_runtime_enabled()
    qdrant_enabled = qdrant_runtime_enabled()
    if not copy_enabled and not qdrant_enabled:
        return 0
    factory = session_factory or _session_factory()
    if copy_enabled:
        try:
            await _reconcile_copy_legal_holds(factory)
        except Exception as error:
            logger.warning(
                "credit object copy legal hold reconcile failed",
                alert="credit_object_copy_hold",
                error_type=type(error).__name__,
            )
    async with factory() as session:
        if copy_enabled:
            await _enqueue_orphan_copy_collection(session)
            jobs = await _claim_copy_jobs(session)
        else:
            jobs = []
        qdrant_jobs = await _claim_qdrant_jobs(session) if qdrant_enabled else []
    for job in jobs:
        try:
            if job.operation == "copy":
                await _copy_one(factory, job)
            else:
                await _collect_one(factory, job)
        except Exception as error:
            logger.warning(
                "credit object copy job failed",
                alert="credit_object_copy",
                operation=job.operation,
                error_type=type(error).__name__,
            )
    for qdrant_job in qdrant_jobs:
        await _run_qdrant_job(factory, qdrant_job)
    return len(jobs) + len(qdrant_jobs)


async def run_credit_object_workers(
    interval: float | None = None,
    *,
    session_factory: Callable[[], AsyncSession] | None = None,
) -> None:
    """Cancellable lifespan loop; it does not even claim rows while off."""

    delay = float(
        settings.credit_object_worker_interval_seconds if interval is None else interval
    )
    while True:
        try:
            await run_credit_object_copy_once(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "credit object worker failed",
                alert="credit_object_worker",
                error_type=type(error).__name__,
            )
        await asyncio.sleep(delay)


def start_credit_object_workers() -> asyncio.Task[None] | None:
    """Start a dormant worker; the loop itself stays inert switch-off/staging."""

    if settings.environment not in ("development", "test"):
        return None
    return asyncio.create_task(run_credit_object_workers(), name="credit-object-worker")


async def stop_credit_object_workers(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# The observational writer (switch on, before the enforcement).
# ---------------------------------------------------------------------------

_Steps = Callable[[AsyncSession, TenantScope, Callable[[], None]], Awaitable[bool]]


def _session_factory() -> Callable[[], AsyncSession]:
    from src.repositories.database import get_db_session_factory

    return get_db_session_factory()


async def _observational_steps(
    session: AsyncSession,
    held: list[AsyncConnection],
    due: Callable[[], None],
    tenant_id: UUID,
    steps: _Steps,
) -> ObservationalOutcome:
    connection: AsyncConnection | None = None
    try:
        if _pool_exhausted(session):
            return "pool_exhausted"
        connection = await session.connection()
        held.append(connection)
        due()
        if connection.dialect.name == "postgresql":
            await session.execute(
                text(_SET_SERVER_TIMEOUTS), {"timeout": OBSERVATIONAL_SERVER_TIMEOUT}
            )
        repository = CreditRepository(session)
        if await repository.enforcement_at() is not None:
            return "enforced"
        due()
        scope = await repository.tenant_scope(tenant_id, lock=True)
        if scope is None:
            return "tenant_gone"
        due()
        inserted = await steps(session, scope, due)
        due()
        # The writer's own transaction, committed through its transaction
        # object: the route's commits (one per successful write, pinned by
        # the knowledge suites through ``AsyncSession.commit``) stay its own.
        transaction = session.get_transaction()
        if transaction is not None:
            await transaction.commit()
        return "recorded" if inserted else "replayed"
    except asyncio.CancelledError:
        if connection is not None and not _single_connection_pool(session):
            await _invalidate_connection(connection)
        raise


async def _observational_write(
    factory: Callable[[], AsyncSession],
    deadline: float,
    tenant_id: UUID,
    steps: _Steps,
) -> ObservationalOutcome:
    """One budget around checkout, statements, commit and close (the Group
    3a writer's rules: no wait for the pool, a single-connection pool is
    never cancelled mid-statement, an interrupted connection is
    invalidated)."""

    session = factory()
    held: list[AsyncConnection] = []
    single = _single_connection_pool(session)
    loop = asyncio.get_running_loop()

    def due() -> None:
        if single and loop.time() >= deadline:
            raise TimeoutError

    if single:
        try:
            return await _observational_steps(session, held, due, tenant_id, steps)
        finally:
            await session.close()
    async with asyncio.timeout_at(deadline):
        try:
            return await _observational_steps(session, held, due, tenant_id, steps)
        finally:
            await _close_within_budget(session, held)


async def _run_observational(
    route_session: object,
    *,
    tenant_id: UUID,
    source: str,
    steps: _Steps,
    session_factory: Callable[[], AsyncSession] | None,
    budget_seconds: float,
) -> ObservationalOutcome:
    error_type: str | None = None
    if not await end_route_transaction(route_session):
        outcome: ObservationalOutcome = "route_busy"
    else:
        deadline = asyncio.get_running_loop().time() + budget_seconds
        try:
            factory = session_factory or _session_factory()
            outcome = await _observational_write(factory, deadline, tenant_id, steps)
        except TimeoutError:
            outcome = "timeout"
            error_type = "TimeoutError"
        except Exception as error:
            outcome = "failed"
            error_type = type(error).__name__
        else:
            if outcome in ("recorded", "replayed"):
                return outcome
            if outcome == "enforced":
                logger.info(
                    WRITE_DROPPED_LOG,
                    reason=outcome,
                    source=source,
                    tenant_id=str(tenant_id),
                )
                return outcome
    logger.warning(
        WRITE_DROPPED_LOG,
        alert="credit_observational_write",
        reason=outcome,
        source=source,
        tenant_id=str(tenant_id),
        error_type=error_type,
    )
    return outcome


async def _observed_window(
    session: AsyncSession, scope: TenantScope
) -> tuple[AccountingWindow, InventorySnapshot]:
    repository = CreditInventoryRepository(session)
    builder_id = scope.builder_id
    cycles = [] if builder_id is None else await repository.monthly_cycles(builder_id)
    window = accounting_window(cycles, _now())
    sources = (
        await repository.tenant_sources(scope.tenant_id)
        if builder_id is None
        else await repository.builder_sources(builder_id)
    )
    return window, InventorySnapshot.of(sources)


async def _observe_maintenance(
    session: AsyncSession,
    scope: TenantScope,
    window: AccountingWindow,
    snapshot: InventorySnapshot,
    exclude: frozenset[ObjectKey],
) -> None:
    """What a full opening of the window would charge, once per window and
    category of the builder (or standalone tenant)."""

    inventory = CreditInventoryRepository(session)
    credit = CreditRepository(session)
    scope_id = scope.builder_id or scope.tenant_id
    for category in CATEGORIES:
        effect_id = observational_maintenance_effect_id(scope_id, window, category)
        if await inventory.observational_effect_recorded(
            effect_id=effect_id, builder_id=scope.builder_id, tenant_id=scope.tenant_id
        ):
            continue
        await credit.insert_observational_effect(
            tenant_id=scope.tenant_id,
            builder_id=scope.builder_id,
            effect_id=effect_id,
            effect_kind=MAINTENANCE_EFFECT_KIND,
            would_debit_mc=aggregate_target_mc(
                snapshot.units(category, exclude), category
            ),
        )


async def record_observational_upload(
    route_session: object,
    *,
    tenant_id: UUID,
    category: Category,
    object_ids: Iterable[UUID],
    upload_id: UUID,
    session_factory: Callable[[], AsyncSession] | None = None,
    budget_seconds: float = OBSERVATIONAL_WRITE_BUDGET_SECONDS,
) -> ObservationalOutcome:
    """After the upload committed: what its reservation WOULD debit (the
    aggregate delta its objects add) and the window's maintenance.  Never
    raises except a cancellation of the caller."""

    exclude = frozenset((category, object_id) for object_id in object_ids)

    async def steps(
        session: AsyncSession, scope: TenantScope, due: Callable[[], None]
    ) -> bool:
        window, snapshot = await _observed_window(session, scope)
        due()
        await _observe_maintenance(session, scope, window, snapshot, exclude)
        before = aggregate_target_mc(snapshot.units(category, exclude), category)
        after = aggregate_target_mc(snapshot.units(category), category)
        due()
        return await CreditRepository(session).insert_observational_effect(
            tenant_id=tenant_id,
            builder_id=scope.builder_id,
            effect_id=reservation_effect_id(tenant_id, category, upload_id),
            effect_kind=category,
            would_debit_mc=max(after - before, 0),
        )

    return await _run_observational(
        route_session,
        tenant_id=tenant_id,
        source=f"{category}_upload",
        steps=steps,
        session_factory=session_factory,
        budget_seconds=budget_seconds,
    )


async def record_observational_egress(
    route_session: object,
    *,
    tenant_id: UUID,
    media_id: UUID,
    size_bytes: int,
    session_factory: Callable[[], AsyncSession] | None = None,
    budget_seconds: float = OBSERVATIONAL_WRITE_BUDGET_SECONDS,
) -> ObservationalOutcome:
    """After a download was answered: the delivery and what its overage
    WOULD debit.  Never raises except a cancellation of the caller."""

    delivery_id = uuid4()

    async def steps(
        session: AsyncSession, scope: TenantScope, due: Callable[[], None]
    ) -> bool:
        window, snapshot = await _observed_window(session, scope)
        due()
        await _observe_maintenance(session, scope, window, snapshot, frozenset())
        inventory = CreditInventoryRepository(session)
        allowance = egress_allowance_bytes(
            aggregate_target_mc(snapshot.units("media"), "media")
        )
        before = await inventory.delivered_bytes(
            window_start=window.start,
            mode="observational",
            builder_id=scope.builder_id,
            tenant_id=scope.tenant_id,
        )
        await inventory.insert_delivery(
            tenant_id=tenant_id,
            builder_id=scope.builder_id,
            media_id=media_id,
            delivery_id=delivery_id,
            window_start=window.start,
            size_bytes=size_bytes,
            mode="observational",
        )
        due()
        would = egress_target_mc(before + size_bytes, allowance) - egress_target_mc(
            before, allowance
        )
        return await CreditRepository(session).insert_observational_effect(
            tenant_id=tenant_id,
            builder_id=scope.builder_id,
            effect_id=egress_effect_id(tenant_id, media_id, delivery_id),
            effect_kind=EGRESS_EFFECT_KIND,
            would_debit_mc=max(would, 0),
        )

    return await _run_observational(
        route_session,
        tenant_id=tenant_id,
        source="media_egress",
        steps=steps,
        session_factory=session_factory,
        budget_seconds=budget_seconds,
    )


__all__ = [
    "CATEGORIES",
    "CREDIT_OFF",
    "EGRESS_EFFECT_KIND",
    "MAINTENANCE_EFFECT_KIND",
    "AllocationOutcome",
    "CreditMode",
    "CreditPendingError",
    "InventoryReadbackFailed",
    "InventorySnapshot",
    "WindowState",
    "WindowWork",
    "charge_egress",
    "charge_rag_reprocessing",
    "enqueue_object_copy",
    "enqueue_source_collection",
    "forget_enforcement_seen",
    "lock_tenant_for_reservation",
    "open_window_for_debit",
    "object_copy_runtime_enabled",
    "qdrant_runtime_enabled",
    "read_credit_mode",
    "readback",
    "record_observational_egress",
    "record_observational_upload",
    "reserve_upload",
    "run_credit_object_copy_once",
    "run_credit_object_workers",
    "start_credit_object_workers",
    "stop_credit_object_workers",
]
