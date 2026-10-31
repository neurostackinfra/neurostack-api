"""Credit read API (neurostack-credit-system Group 6): ``/v1/credits/*``.

A credit surface (``credit_router``): while the credit switch is not
effective every route answers the stable ``503 {"code":
"credit_unavailable"}`` before any dependency, authentication check or
database work, and the UI hides its credit panels on that answer.

Read-only.  No route accepts a body or writes a row, takes a lock or opens a
ledger book under the account lock: the figures come from the stored rows,
assembled into a :class:`~src.services.credit_ledger.LedgerBook` and swept at
the database clock IN MEMORY (expiries due and the start catch-up of a
started monthly allowance are applied to the view, never persisted), so
usable capacity is ``LedgerBook.available_mc`` and open debt is the swept
``LedgerBook.open_debt_mc`` -- never the stored derived balance as usable
(the ledger module's note to Groups 6 and 7).  Nothing here buys credits:
the packages are sold by the ASAAS checkout (``POST /v1/billing/checkout``,
``offer_id`` and ``method`` only), and a purchase shows here only after its
settled fact reached the ledger.

Scope (the principal derivation of the wish): the builder is never taken
from the client.  A JWT ``builder`` reads its own builder's account, through
its own tenant or, with ``?tenant_id=``, one of its clinics; a JWT
``super_admin`` reads the builder of any tenant it selects.  The builder is
``tenants.builder_id`` RE-READ for that tenant (a plain read, lock-order step
0) and, for a builder, must equal its JWT claim.  A clinic of another
builder and a tenant that does not exist get the same ``403 {"detail":
{"code": "tenant_override_forbidden"}}`` before any credit or billing table
is read.  API keys and other roles get ``403 {"detail": {"code":
"credit_scope_forbidden"}}`` before any database work: the account is the
builder's financial state, which ``main`` only shows to the builder
(``/v1/billing/*`` requires the builder JWT).  A standalone tenant (no
builder) has no account: zeros, state ``pending`` (``standalone``).

``state`` precedence: ``deletion_due`` > ``suspended`` > ``grace`` (the
recorded transitions of the open exhaustion episode; episodes exist only
after ``credit_enforcement_at``) > ``pending`` (``credit_pending``: a
quarantined ASAAS order or cycle, a standalone tenant, or -- once enforced --
a binding coverage that is not usable: ``reconciliation``) > ``active``.
``mode`` is ``observational`` until ``credit_enforcement_at``: nothing is
charged then, and the consumption shows what WOULD have been debited.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, TypeVar, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    resolve_effective_tenant,
)
from src.api.schemas.credits import (
    CreditAccountingWindow,
    CreditBalanceResponse,
    CreditCatalogResponse,
    CreditConsumptionItem,
    CreditConsumptionResponse,
    CreditDebtItem,
    CreditDebtResponse,
    CreditExpiriesResponse,
    CreditExpiry,
    CreditLot,
    CreditLotsResponse,
    CreditMode,
    CreditNextExpiry,
    CreditPackage,
    CreditRefusal,
    CreditReservation,
    CreditReservationsResponse,
    CreditStateResponse,
    CreditSummaryResponse,
    CreditThresholdAlertItem,
    CreditThresholdsResponse,
    CreditUnavailable,
    CreditUnits,
    LotKind,
    LotStatus,
    ReservationCategory,
)
from src.models.content_retention import (
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
    CreditThresholdAlert,
)
from src.models.credit import CreditAccount, CreditEffect, CreditLedgerEntry
from src.models.credit import CreditLot as CreditLotRow
from src.models.credit import CreditObservationalEffect
from src.repositories.credit import CreditRepository
from src.repositories.credit_inventory import AllocationRow, CreditInventoryRepository
from src.repositories.database import get_db_session_dependency
from src.services.billing_catalog import BILLING_CATALOG, CATALOG_VERSION_ID
from src.services.credit_allocations import (
    EGRESS_INCLUDED_BYTES_PER_CREDIT,
    EGRESS_OVERAGE_BYTES_PER_CREDIT,
    MEDIA_BYTES_PER_CREDIT,
    RAG_CHARACTERS_PER_CREDIT,
    AccountingWindow,
    accounting_window,
    egress_allowance_bytes,
)
from src.services.credit_ledger import (
    LEDGER_EFFECT_KINDS,
    MILLICREDITS_PER_CREDIT,
    TOPUP_VALIDITY_MONTHS,
    LedgerBook,
    LotState,
    assemble_book,
)
from src.services.credit_lifecycle import (
    arm_key,
    capacity_in_force_mc,
    lifecycle_state,
    thresholds_reached,
)
from src.services.credit_metering import TRACE_COST_MC
from src.services.credit_rollout import CreditRolloutService
from src.services.credit_switch import credit_router

router = credit_router()

_T = TypeVar("_T")
_Route = TypeVar("_Route", bound=Callable[..., Any])

SCOPE_FORBIDDEN_CODE = "credit_scope_forbidden"
TENANT_FORBIDDEN_CODE = "tenant_override_forbidden"
_SCOPE_ROLES = frozenset({"builder", "super_admin"})
_CATEGORIES: tuple[ReservationCategory, ...] = ("rag", "media", "egress")
# Ledger entries that are consumption: a debit, and the consumption debt a
# debit beyond capacity created (same effect).  Effects of the ledger's own
# kinds (grants, reversals, settlements ...) are never consumption.
_CONSUMPTION_ENTRIES = ("debit", "debt")

# PostgreSQL: the first statement of the snapshot's transaction.
_READ_ONLY_SNAPSHOT = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Not authenticated."},
    403: {
        "model": CreditRefusal,
        "description": (
            "credit_scope_forbidden (API key or a role other than builder or "
            "super_admin) or tenant_override_forbidden (a clinic of another "
            "builder, or a tenant that does not exist: the same body)."
        ),
    },
    503: {
        "model": CreditUnavailable,
        "description": "credit_unavailable: the credit switch is not effective.",
    },
}

_TENANT_QUERY = Query(
    default=None,
    description=(
        "Builders: one of their clinics (default: their own tenant); super "
        "admins: any tenant. The account read is always the builder that "
        "tenant belongs to now (tenants.builder_id, re-read)."
    ),
)


def _refusal(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": code})


def _no_store(response: Response) -> None:
    """Per builder and time-dependent: no copy may be reused."""

    response.headers["Cache-Control"] = "no-store"


# ---------------------------------------------------------------------------
# Scope: derived from the authenticated principal, never from the client.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreditScope:
    """The tenant read through and its builder as stored NOW."""

    tenant_id: UUID
    builder_id: UUID | None


async def resolve_credit_scope(
    request: Request,
    tenant_id: Optional[UUID] = _TENANT_QUERY,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditScope:
    """Builder JWT (own builder) or super admin JWT; refusals before any
    credit or billing read, a foreign and an absent tenant alike."""

    if (
        tenant.api_key
        or tenant.authentication_mode != "jwt"
        or tenant.role not in _SCOPE_ROLES
    ):
        raise _refusal(SCOPE_FORBIDDEN_CODE)
    if tenant.role == "builder" and tenant.builder_id is None:
        raise _refusal(TENANT_FORBIDDEN_CODE)
    try:
        effective = await resolve_effective_tenant(request, tenant, tenant_id, session)
        found = await CreditRepository(session).tenant_scope(effective)
    except HTTPException:
        await session.rollback()
        raise
    if found is None or (
        tenant.role == "builder" and found.builder_id != tenant.builder_id
    ):
        await session.rollback()
        raise _refusal(TENANT_FORBIDDEN_CODE)
    return CreditScope(tenant_id=effective, builder_id=found.builder_id)


# ---------------------------------------------------------------------------
# One snapshot per request: every figure from the same rows and instant.
# ---------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    """SQLite drops the zone of TIMESTAMPTZ; its stored values are UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


_LOT_COLUMNS = (
    "id",
    "lot_kind",
    "paid_at",
    "expires_at",
    "granted_mc",
    "grant_effect_id",
    "payment_id",
    "subscription_id",
    "cycle_start",
    "cycle_end",
    "order_id",
)
_DEBT_COLUMNS = (
    "id",
    "created_at",
    "debt_source_kind",
    "debt_source_effect_id",
    "original_grant_lot_id",
    "amount_mc",
)


class CreditSnapshot:
    """Lazily read, once per request, in the request's session (read-only;
    the caller ends the transaction with a rollback)."""

    def __init__(self, session: AsyncSession, scope: CreditScope) -> None:
        self.session = session
        self.scope = scope
        self.repository = CreditRepository(session)
        self._cache: dict[str, Any] = {}

    async def _once(self, key: str, load: Callable[[], Awaitable[_T]]) -> _T:
        if key not in self._cache:
            self._cache[key] = await load()
        value: _T = self._cache[key]
        return value

    # -- the book --------------------------------------------------------

    async def book(self) -> LedgerBook | None:
        return await self._once("book", self._load_book)

    async def _load_book(self) -> LedgerBook | None:
        builder_id = self.scope.builder_id
        now = await self.repository.database_now()
        self._cache["now"] = now
        if builder_id is None:
            return None
        repository = self.repository
        account_id = await self.session.scalar(
            select(CreditAccount.id).where(CreditAccount.builder_id == builder_id)
        )
        lots = (
            (
                await self.session.execute(
                    select(CreditLotRow)
                    .where(CreditLotRow.builder_id == builder_id)
                    .order_by(CreditLotRow.id)
                )
            )
            .scalars()
            .all()
        )
        book = assemble_book(
            builder_id=builder_id,
            # A builder that never had a credit row has no account: an empty
            # book (no lot can exist without one).
            account_id=account_id if account_id is not None else builder_id,
            lots=[
                {column: getattr(lot, column) for column in _LOT_COLUMNS}
                for lot in lots
            ],
            lot_sums=await repository.lot_entry_sums(builder_id),
            debts=[
                {column: getattr(debt, column) for column in _DEBT_COLUMNS}
                for debt in await repository.debts(builder_id)
            ],
            settled=await repository.settlement_sums(builder_id),
            known_effects=await repository.effect_ids_of_kinds(
                builder_id, LEDGER_EFFECT_KINDS
            ),
            release_rows=await repository.release_sums(builder_id),
            ledger_debt_mc=(await repository.derived_balance(builder_id)).debt_mc,
            clock_floor=await repository.latest_occurred_at(builder_id),
        )
        # In memory only: the due expiries and the start catch-up of a
        # started allowance, as the next writer would record them.
        book.sweep_expired(now)
        return book

    async def as_of(self) -> datetime:
        book = await self.book()
        if book is not None and book.clock is not None:
            return book.clock
        now: datetime = self._cache["now"]
        return now

    async def window(self) -> AccountingWindow:
        async def load() -> AccountingWindow:
            book = await self.book()
            cycles = (
                []
                if book is None
                else [
                    (lot.cycle_start, lot.cycle_end)
                    for lot in book.lots
                    if lot.lot_kind == "monthly"
                    and lot.cycle_start is not None
                    and lot.cycle_end is not None
                ]
            )
            return accounting_window(cycles, await self.as_of())

        return await self._once("window", load)

    # -- state -----------------------------------------------------------

    async def enforcement_at(self) -> datetime | None:
        async def load() -> datetime | None:
            return _optional_utc(await self.repository.enforcement_at())

        return await self._once("enforcement_at", load)

    async def enforced(self) -> bool:
        enforcement = await self.enforcement_at()
        return enforcement is not None and enforcement <= await self.as_of()

    async def mode(self) -> CreditMode:
        return "enforced" if await self.enforced() else "observational"

    async def state(self) -> CreditStateResponse:
        return await self._once("state", self._load_state)

    async def _load_state(self) -> CreditStateResponse:
        as_of = await self.as_of()
        enforcement = await self.enforcement_at()
        mode = await self.mode()
        builder_id = self.scope.builder_id
        if builder_id is None:
            return CreditStateResponse(
                as_of=as_of,
                mode=mode,
                enforcement_at=enforcement,
                state="pending",
                pending_reasons=["standalone"],
            )
        episode = await self.session.scalar(
            select(CreditLifecycleEpisode).where(
                CreditLifecycleEpisode.builder_id == builder_id,
                CreditLifecycleEpisode.reactivated_at.is_(None),
            )
        )
        recorded = set(
            (
                await self.session.execute(
                    select(CreditLifecycleTransition.transition)
                    .join(
                        CreditLifecycleEpisode,
                        and_(
                            CreditLifecycleEpisode.id
                            == CreditLifecycleTransition.episode_id,
                            CreditLifecycleEpisode.builder_id
                            == CreditLifecycleTransition.builder_id,
                        ),
                    )
                    .where(
                        CreditLifecycleTransition.builder_id == builder_id,
                        CreditLifecycleEpisode.reactivated_at.is_(None),
                    )
                )
            ).scalars()
        )
        quarantined = await self.repository.quarantine_hold(builder_id)
        lifecycle = lifecycle_state(
            open_episode=episode is not None,
            suspended="suspended" in recorded,
            deletion_marked="deletion_due" in recorded,
        )
        reasons: list[Any] = ["quarantine"] if quarantined else []
        # Once enforced, a request is answered ``credit_pending`` whenever the
        # binding coverage is not usable (identity conflict, an unbound
        # current cycle outside grace, an unreconciled top-up); the quarantine
        # is checked first, exactly as on the enforced path.  Read-only.
        if (
            not quarantined
            and mode == "enforced"
            and not (
                await CreditRolloutService(self.session).binding_coverage(
                    builder_id, at=as_of, quarantine_checked=True
                )
            ).usable
        ):
            reasons.append("reconciliation")
        state: Any = lifecycle
        if lifecycle == "active" and reasons:
            state = "pending"
        deletion = None if episode is None else _utc(episode.deletion_due_at)
        return CreditStateResponse(
            as_of=as_of,
            mode=mode,
            enforcement_at=enforcement,
            state=state,
            pending_reasons=reasons,
            exhausted_at=None if episode is None else _utc(episode.exhausted_at),
            suspension_due_at=(
                None if episode is None else _utc(episode.suspension_due_at)
            ),
            deletion_due_at=deletion,
            export_deadline_at=deletion,
        )

    # -- reservations ----------------------------------------------------

    async def allocations(self) -> list[AllocationRow]:
        async def load() -> list[AllocationRow]:
            if self.scope.builder_id is None:
                return []
            window = await self.window()
            return await CreditInventoryRepository(self.session).window_allocations(
                self.scope.builder_id, window.start
            )

        return await self._once("allocations", load)

    async def reservations(self) -> CreditReservationsResponse:
        async def load() -> CreditReservationsResponse:
            window = await self.window()
            coverage: dict[str, tuple[int, int]] = {c: (0, 0) for c in _CATEGORIES}
            charged: Counter[str] = Counter()
            for row in await self.allocations():
                if row.category not in coverage:
                    continue
                if row.target_mc > coverage[row.category][0]:
                    coverage[row.category] = (row.target_mc, row.units)
                charged[row.category] += row.charged_mc
            delivered = 0
            if self.scope.builder_id is not None:
                delivered = await CreditInventoryRepository(
                    self.session
                ).delivered_bytes(
                    window_start=window.start,
                    mode="enforced",
                    builder_id=self.scope.builder_id,
                )
            return CreditReservationsResponse(
                as_of=await self.as_of(),
                window=_window(window),
                reservations=[
                    CreditReservation(
                        category=category,
                        units=coverage[category][1],
                        coverage_mc=coverage[category][0],
                        charged_mc=charged[category],
                    )
                    for category in _CATEGORIES
                ],
                egress_allowance_bytes=egress_allowance_bytes(coverage["media"][0]),
                egress_delivered_bytes=delivered,
            )

        return await self._once("reservations", load)

    # -- balance, lots, expiries, debt -------------------------------------

    async def balance(self) -> CreditBalanceResponse:
        book = await self.book()
        as_of = await self.as_of()
        reserved = sum(row.charged_mc for row in await self.allocations())
        if book is None:
            return CreditBalanceResponse(
                as_of=as_of,
                monthly_available_mc=0,
                monthly_not_started_mc=0,
                topup_available_mc=0,
                available_mc=0,
                open_debt_mc=0,
                net_available_mc=0,
                reserved_mc=reserved,
            )
        monthly = [lot for lot in book.lots if lot.lot_kind == "monthly"]
        topups = [lot for lot in book.lots if lot.lot_kind == "topup"]
        expiries = self.expiries_of(book, as_of)
        return CreditBalanceResponse(
            as_of=as_of,
            monthly_available_mc=sum(lot.spendable_mc(as_of) for lot in monthly),
            monthly_not_started_mc=sum(
                lot.available_mc(as_of) for lot in monthly if _not_started(lot, as_of)
            ),
            topup_available_mc=sum(lot.spendable_mc(as_of) for lot in topups),
            available_mc=book.available_mc(as_of),
            open_debt_mc=book.open_debt_mc,
            net_available_mc=book.available_trace_mc(as_of),
            reserved_mc=reserved,
            next_expiry=(
                None if not expiries else CreditNextExpiry(**expiries[0].model_dump())
            ),
        )

    @staticmethod
    def expiries_of(book: LedgerBook, as_of: datetime) -> list[CreditExpiry]:
        holding = [lot for lot in book.lots if lot.available_mc(as_of) > 0]
        holding.sort(key=lambda lot: (lot.expires_at, lot.fefo_key()))
        return [
            CreditExpiry(
                lot_id=lot.id,
                kind=cast(LotKind, lot.lot_kind),
                expires_at=lot.expires_at,
                available_mc=lot.available_mc(as_of),
            )
            for lot in holding
        ]

    async def expiries(self) -> list[CreditExpiry]:
        book = await self.book()
        return [] if book is None else self.expiries_of(book, await self.as_of())

    async def lots(self) -> list[CreditLot]:
        book = await self.book()
        as_of = await self.as_of()
        if book is None:
            return []
        return [
            CreditLot(
                lot_id=lot.id,
                kind=cast(LotKind, lot.lot_kind),
                status=_lot_status(lot, as_of),
                granted_mc=lot.granted_mc,
                available_mc=lot.available_mc(as_of),
                consumed_mc=max(lot.consumed_mc, 0),
                reversed_mc=max(lot.sums["reversal"] - lot.sums["lot_regrant"], 0),
                expired_mc=lot.sums["expiry"],
                paid_at=lot.paid_at,
                expires_at=lot.expires_at,
                cycle_start=lot.cycle_start,
                cycle_end=lot.cycle_end,
            )
            for lot in book.lots
        ]

    async def debt(self) -> CreditDebtResponse:
        book = await self.book()
        as_of = await self.as_of()
        if book is None:
            return CreditDebtResponse(as_of=as_of, open_debt_mc=0, debts=[])
        open_by_id = book.open_debt_by_id()
        return CreditDebtResponse(
            as_of=as_of,
            open_debt_mc=book.open_debt_mc,
            debts=[
                CreditDebtItem(
                    debt_id=debt.id,
                    source_kind=debt.source_kind,
                    amount_mc=debt.amount_mc,
                    settled_mc=debt.settled_mc,
                    open_mc=open_by_id[debt.id],
                    created_at=_utc(debt.created_at),
                )
                for debt in book.debts
                if open_by_id[debt.id] > 0
            ],
        )

    # -- thresholds --------------------------------------------------------

    async def thresholds(self) -> CreditThresholdsResponse:
        book = await self.book()
        as_of = await self.as_of()
        capacity = 0 if book is None else capacity_in_force_mc(book, as_of)
        available = 0 if book is None else book.available_trace_mc(as_of)
        used = max(capacity - available, 0)
        alerts: list[CreditThresholdAlertItem] = []
        builder_id = self.scope.builder_id
        if builder_id is not None:
            closed = int(
                await self.session.scalar(
                    select(func.count())
                    .select_from(CreditLifecycleEpisode)
                    .where(
                        CreditLifecycleEpisode.builder_id == builder_id,
                        CreditLifecycleEpisode.reactivated_at.is_not(None),
                    )
                )
                or 0
            )
            key = arm_key(closed, (await self.window()).start.isoformat())
            rows = await self.session.execute(
                select(CreditThresholdAlert.threshold, CreditThresholdAlert.occurred_at)
                .where(
                    CreditThresholdAlert.builder_id == builder_id,
                    CreditThresholdAlert.arm_key == key,
                )
                .order_by(CreditThresholdAlert.threshold)
            )
            alerts = [
                CreditThresholdAlertItem(
                    threshold=int(threshold), occurred_at=_utc(occurred_at)
                )
                for threshold, occurred_at in rows
            ]
        return CreditThresholdsResponse(
            as_of=as_of,
            capacity_mc=capacity,
            available_mc=available,
            used_mc=used,
            used_percent=_used_percent(capacity, available, used),
            reached=list(thresholds_reached(capacity, available)),
            alerts=alerts,
        )

    # -- consumption -------------------------------------------------------

    async def consumption(self) -> CreditConsumptionResponse:
        window = await self.window()
        start, end = _utc(window.starts_at), _utc(window.ends_at)
        charged: dict[str, int] = {}
        observed: dict[str, int] = {}
        builder_id = self.scope.builder_id
        if builder_id is not None:
            entry, effect = CreditLedgerEntry, CreditEffect
            charged_rows = await self.session.execute(
                select(effect.effect_kind, func.sum(entry.amount_mc))
                .select_from(entry)
                .join(
                    effect,
                    and_(
                        effect.builder_id == entry.builder_id,
                        effect.effect_id == entry.effect_id,
                    ),
                )
                .where(
                    entry.builder_id == builder_id,
                    entry.entry_type.in_(_CONSUMPTION_ENTRIES),
                    effect.effect_kind.not_in(LEDGER_EFFECT_KINDS),
                    entry.occurred_at >= start,
                    entry.occurred_at < end,
                )
                .group_by(effect.effect_kind)
            )
            charged = {str(kind): int(total) for kind, total in charged_rows}
        observation = CreditObservationalEffect
        owner = (
            observation.builder_id == builder_id
            if builder_id is not None
            else observation.tenant_id == self.scope.tenant_id
        )
        observed_rows = await self.session.execute(
            select(observation.effect_kind, func.sum(observation.would_debit_mc))
            .where(
                owner,
                observation.recorded_at >= start,
                observation.recorded_at < end,
            )
            .group_by(observation.effect_kind)
        )
        observed = {str(kind): int(total) for kind, total in observed_rows}
        return CreditConsumptionResponse(
            as_of=await self.as_of(),
            mode=await self.mode(),
            window=_window(window),
            charged_mc=sum(charged.values()),
            charged=_items(charged),
            observed_mc=sum(observed.values()),
            observed=_items(observed),
        )


def _not_started(lot: LotState, at: datetime) -> bool:
    starts = lot.spend_starts_at()
    return starts is not None and at < starts


def _lot_status(lot: LotState, at: datetime) -> LotStatus:
    if lot.expired(at):
        return "expired"
    if _not_started(lot, at):
        return "not_started"
    if lot.available_mc(at) <= 0:
        return "exhausted"
    return "active"


def _used_percent(capacity: int, available: int, used: int) -> int:
    """Floor percentage; nothing usable is always 100 % (the lifecycle's
    rule: an account with no capacity at all reports exhaustion)."""

    if available <= 0:
        return 100
    if capacity <= 0:
        return 0
    return min(used * 100 // capacity, 100)


def _window(window: AccountingWindow) -> CreditAccountingWindow:
    return CreditAccountingWindow(start=window.start, end=window.end, kind=window.kind)


def _items(amounts: dict[str, int]) -> list[CreditConsumptionItem]:
    return [
        CreditConsumptionItem(kind=kind, amount_mc=amount)
        for kind, amount in sorted(amounts.items())
        if amount
    ]


async def _read(
    session: AsyncSession,
    scope: CreditScope,
    build: Callable[[CreditSnapshot], Awaitable[_T]],
) -> _T:
    """Build the answer in ONE read-only snapshot, then end it.

    The scope's plain reads are ended first, so on PostgreSQL the snapshot's
    first statement opens a ``REPEATABLE READ, READ ONLY`` transaction: every
    row of the book (lots, entry sums, debts, settlements, the derived debt)
    comes from the same instant even while a writer commits in between, and
    the database itself refuses any write.  The final rollback expires ORM
    rows, so it comes only after every value was copied.
    """

    await session.rollback()
    try:
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(_READ_ONLY_SNAPSHOT)
        return await build(CreditSnapshot(session, scope))
    finally:
        await session.rollback()


def _get(
    path: str, *, summary: str, description: str, model: Any
) -> Callable[[_Route], _Route]:
    return router.get(
        path,
        summary=summary,
        description=description,
        response_model=model,
        responses=_RESPONSES,
        dependencies=[Depends(_no_store)],
    )


# ---------------------------------------------------------------------------
# Routes (GET only).
# ---------------------------------------------------------------------------


@_get(
    "/catalog",
    summary="Credit units and top-up packages",
    description=(
        "What one credit buys and the detached top-up packages of the ASAAS "
        "catalog (500/1,000/2,000/4,000 credits, card or PIX, valid "
        f"{TOPUP_VALIDITY_MONTHS} months). The subscription is a monthly card "
        "subscription only. Packages are bought through POST "
        "/v1/billing/checkout (offer_id and method only); this API sells "
        "nothing."
    ),
    model=CreditCatalogResponse,
)
async def credit_catalog(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditCatalogResponse:
    del scope
    await session.rollback()
    return CreditCatalogResponse(
        catalog_version_id=CATALOG_VERSION_ID,
        millicredits_per_credit=MILLICREDITS_PER_CREDIT,
        units=CreditUnits(
            trace_mc=TRACE_COST_MC,
            rag_characters_per_credit=RAG_CHARACTERS_PER_CREDIT,
            media_bytes_per_credit=MEDIA_BYTES_PER_CREDIT,
            egress_included_bytes_per_credit=EGRESS_INCLUDED_BYTES_PER_CREDIT,
            egress_overage_bytes_per_credit=EGRESS_OVERAGE_BYTES_PER_CREDIT,
        ),
        packages=[
            CreditPackage(
                offer_id=offer.offer_id,
                credits=offer.credits,
                amount_cents=offer.amount_cents,
                payment_methods=["credit_card", "pix"],
                validity_months=TOPUP_VALIDITY_MONTHS,
            )
            for offer in sorted(
                (o for o in BILLING_CATALOG.values() if o.cycle == "DETACHED"),
                key=lambda o: o.credits,
            )
        ],
        subscription_payment_methods=["credit_card"],
    )


@_get(
    "/summary",
    summary="Credit summary",
    description=(
        "State, balance by origin, expiries, open debt, the current window's "
        "reservations, thresholds and consumption, all from one snapshot of "
        "the ledger at the database clock."
    ),
    model=CreditSummaryResponse,
)
async def credit_summary(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditSummaryResponse:
    async def build(snapshot: CreditSnapshot) -> CreditSummaryResponse:
        return CreditSummaryResponse(
            as_of=await snapshot.as_of(),
            state=await snapshot.state(),
            balance=await snapshot.balance(),
            expiries=await snapshot.expiries(),
            debt=await snapshot.debt(),
            reservations=await snapshot.reservations(),
            thresholds=await snapshot.thresholds(),
            consumption=await snapshot.consumption(),
        )

    return await _read(session, scope, build)


@_get(
    "/balance",
    summary="Credit balance by origin",
    description=(
        "Usable capacity of the monthly allowance and of the top-ups, the "
        "allowance granted before its cycle starts, open debt, capacity net "
        "of debt, what the window's reservations debited and the next expiry."
    ),
    model=CreditBalanceResponse,
)
async def credit_balance(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditBalanceResponse:
    return await _read(session, scope, CreditSnapshot.balance)


@_get(
    "/lots",
    summary="Credit lots",
    description=(
        "Every grant (monthly allowances and top-ups) with what it holds, "
        "consumed, lost to refunds and expired, and its expiry."
    ),
    model=CreditLotsResponse,
)
async def credit_lots(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditLotsResponse:
    async def build(snapshot: CreditSnapshot) -> CreditLotsResponse:
        return CreditLotsResponse(
            as_of=await snapshot.as_of(), lots=await snapshot.lots()
        )

    return await _read(session, scope, build)


@_get(
    "/expiries",
    summary="Credit expiries",
    description="Lots still holding capacity, the earliest expiry first.",
    model=CreditExpiriesResponse,
)
async def credit_expiries(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditExpiriesResponse:
    async def build(snapshot: CreditSnapshot) -> CreditExpiriesResponse:
        return CreditExpiriesResponse(
            as_of=await snapshot.as_of(), expiries=await snapshot.expiries()
        )

    return await _read(session, scope, build)


@_get(
    "/debt",
    summary="Credit debt",
    description=(
        "Open debt and each open debt's origin; the next capacity settles it, "
        "oldest first."
    ),
    model=CreditDebtResponse,
)
async def credit_debt(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditDebtResponse:
    return await _read(session, scope, CreditSnapshot.debt)


@_get(
    "/reservations",
    summary="Credit reservations",
    description=(
        "The current accounting window's RAG, media and egress coverage and "
        "what its reservations debited, with the window's download allowance."
    ),
    model=CreditReservationsResponse,
)
async def credit_reservations(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditReservationsResponse:
    return await _read(session, scope, CreditSnapshot.reservations)


@_get(
    "/thresholds",
    summary="Credit thresholds",
    description=(
        "Share of the capacity in force consumed (counting open debt), the "
        "50/90/100 % thresholds reached and the alerts recorded this arm."
    ),
    model=CreditThresholdsResponse,
)
async def credit_thresholds(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditThresholdsResponse:
    return await _read(session, scope, CreditSnapshot.thresholds)


@_get(
    "/state",
    summary="Credit state",
    description=(
        "active, pending (credit_pending), grace, suspended or deletion_due, "
        "the deadlines of an open exhaustion episode (the export deadline is "
        "its deletion deadline) and the mode (observational until "
        "credit_enforcement_at)."
    ),
    model=CreditStateResponse,
)
async def credit_state(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditStateResponse:
    return await _read(session, scope, CreditSnapshot.state)


@_get(
    "/consumption",
    summary="Credit consumption",
    description=(
        "Consumption of the current window by effect kind: charged (ledger, "
        "after credit_enforcement_at) and observed (what the observational "
        "metering would have debited, never charged)."
    ),
    model=CreditConsumptionResponse,
)
async def credit_consumption(
    scope: CreditScope = Depends(resolve_credit_scope),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditConsumptionResponse:
    return await _read(session, scope, CreditSnapshot.consumption)


__all__ = [
    "SCOPE_FORBIDDEN_CODE",
    "TENANT_FORBIDDEN_CODE",
    "CreditScope",
    "CreditSnapshot",
    "resolve_credit_scope",
    "router",
]
