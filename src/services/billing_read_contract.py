"""Read-only builder billing contract for the payments screen (ASAAS Group 6b).

Every function here only reads.  None commits, flushes, reserves, takes an
advisory lock or a row lock: each statement is a plain ``SELECT`` (PostgreSQL
``ACCESS SHARE`` only), so no writer is ever blocked by a summary read.  The
builder identity always comes from the caller (the JWT principal resolved by
``require_builder``); nothing here accepts a client-supplied builder or tenant.

``available_offer_ids`` is advisory.  It is composed from the very checks the
``POST /v1/billing/checkout`` path applies (the persisted rollout pre-check,
the live-session reuse, ``subscription_checkout_blocker``, the canary slot and
the top-up capability gate), evaluated without the rollout lock, so a
concurrent transition can make it stale; the ``POST`` stays the authority.  It
never says *why* an offer is absent.

``payment_state`` (owner decision A1) is derived here, never by the client,
from the subscription status, its newest cycle and the paid-through date
against the São Paulo calendar date of an injectable clock
(``derive_payment_state``).  A subscription the provider deleted
(``cancelled``) stays visible while its paid period runs (owner decision A2);
that window is display only and changes no checkout eligibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Final, Literal, TypeVar, cast
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.selectable import ScalarSelect

from src.config.logging import get_logger
from src.config.settings import settings
from src.models.billing import (
    BillingCheckoutSession,
    BillingSubscription,
    BillingSubscriptionCycle,
)
from src.observability.metrics import BILLING_SUMMARY_CHECKOUT_UNAVAILABLE_TOTAL
from src.repositories.billing import (
    LIVE_CHECKOUT_STATES,
    TERMINAL_SUBSCRIPTION_STATUSES,
    BillingRepository,
)
from src.services.billing_catalog import BILLING_CATALOG
from src.services.billing_checkout import CheckoutUnavailable, billing_local_date
from src.services.billing_rollout import (
    BillingCheckoutUnavailable,
    require_billing_checkout_rollout,
)

logger = get_logger(__name__)

_T = TypeVar("_T")

BILLING_SUMMARY_UNAVAILABLE: Final = "billing_summary_unavailable"

CheckoutKind = Literal["subscription", "topup"]
# Non-terminal ASAAS subscription states, i.e. the CHECK-admitted statuses of a
# ``provider = 'asaas'`` row minus ``TERMINAL_SUBSCRIPTION_STATUSES``.
SubscriptionFinancialStatus = Literal[
    "active",
    "active_linked",
    "readback_required",
    "non_billable_pending_readback",
]
# What the summary reports: a current status, or ``cancelled`` (the provider
# deleted the subscription) while its paid period still runs (owner A2).
SubscriptionSummaryStatus = Literal[
    "active",
    "active_linked",
    "readback_required",
    "non_billable_pending_readback",
    "cancelled",
]
# Owner A1: derived server side so the page never infers it from dates.
PaymentState = Literal["paid", "overdue", "reversed", "pending"]
# The newest cycle, classified by ``latest_cycle_kind``.
LatestCycleKind = Literal["pending", "paid", "reversed", "quarantined"]
LiveCheckoutState = Literal[
    "creating",
    "active",
    "paid_pending_subscription",
    "reconciliation_required",
    "terminal_pending_readback",
]

_SUBSCRIPTION_STATUSES: Final = frozenset(
    (
        "active",
        "active_linked",
        "readback_required",
        "non_billable_pending_readback",
    )
)
_CANCELLED: Final = "cancelled"
_SUMMARY_STATUSES: Final = _SUBSCRIPTION_STATUSES | {_CANCELLED}
_KINDS: Final = frozenset(("subscription", "topup"))
# Cycle states that can be fully paid; ``current_period_end`` additionally
# requires ``reversed_cents = restored_cents`` (nothing net reversed).
_PAID_CYCLE_STATES: Final = ("settled", "restored")
# ``ck_billing_subscription_cycles_state``; anything else fails closed.
_CYCLE_STATES: Final = frozenset(
    ("pending", "settled", "reversed", "restored", "quarantined")
)
# Provider evidence is still pending: no payment state is claimed from cycles.
_EVIDENCE_PENDING_STATUSES: Final = frozenset(
    ("readback_required", "non_billable_pending_readback")
)


class BillingReadUnavailable(RuntimeError):
    """The confirmed financial state could not be read without guessing."""

    code = BILLING_SUMMARY_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SubscriptionReadback:
    """The builder's current ASAAS subscription record, or the cancelled one
    whose paid period still runs."""

    offer_id: str
    catalog_version_id: str
    status: SubscriptionSummaryStatus
    amount_cents: int
    current_period_end: date | None
    payment_state: PaymentState


@dataclass(frozen=True, slots=True)
class LiveCheckoutReadback:
    """The builder's most recent live checkout attempt (no provider URL)."""

    checkout_session_id: UUID
    kind: CheckoutKind
    state: LiveCheckoutState
    offer_id: str


@dataclass(frozen=True, slots=True)
class CommercialReadback:
    """Advisory checkout availability and the commercial top-up flag.

    ``available`` is false only when the advisory read itself failed; the
    other two fields are then closed (nothing listed, flag false).
    """

    available_offer_ids: tuple[str, ...]
    topup_commercial_enabled: bool
    available: bool


# The read succeeded and nothing is for sale (closed rollout, no offer).
_CLOSED: Final = CommercialReadback(
    available_offer_ids=(), topup_commercial_enabled=False, available=True
)
# The read failed: closed for buying, and marked as such.
_UNAVAILABLE: Final = CommercialReadback(
    available_offer_ids=(), topup_commercial_enabled=False, available=False
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def billing_today(now: datetime) -> date:
    """The São Paulo calendar date of an aware instant (the commercial
    calendar of ``billing_local_date``); a naive instant fails closed."""

    try:
        return billing_local_date(now)
    except CheckoutUnavailable:
        raise BillingReadUnavailable() from None


def latest_cycle_kind(
    state: object, reversed_cents: object, restored_cents: object
) -> LatestCycleKind:
    """Classify a cycle row; an unknown state or a non-integer total fails
    closed.

    ``paid`` is the F1 rule (``settled``/``restored`` with
    ``reversed_cents = restored_cents``).  Every other settled-then-reversed
    shape (``reversed``, partially restored, a ``settled`` row carrying a net
    reversal) is ``reversed``: a partial reversal does not count as paid
    (owner decision B).
    """

    if (
        not isinstance(state, str)
        or state not in _CYCLE_STATES
        or type(reversed_cents) is not int
        or type(restored_cents) is not int
    ):
        raise BillingReadUnavailable()
    if state == "pending":
        return "pending"
    if state == "quarantined":
        return "quarantined"
    if state in _PAID_CYCLE_STATES and reversed_cents == restored_cents:
        return "paid"
    return "reversed"


def derive_payment_state(
    *,
    status: SubscriptionSummaryStatus,
    latest: LatestCycleKind | None,
    paid_through: date | None,
    today: date,
) -> PaymentState:
    """The closed payment state of one subscription on ``today``.

    In order: pending provider evidence (``readback_required``,
    ``non_billable_pending_readback``), no cycle at all, or a quarantined
    newest cycle is ``pending``; a reversed or partially restored newest
    cycle is ``reversed`` (even while an older paid cycle still covers
    today); otherwise ``paid`` while ``paid_through >= today`` (the São Paulo
    date; the last paid day is still paid), ``overdue`` after it, and
    ``pending`` when no cycle was ever fully paid.  The full matrix is
    documented in ``docs/api-contracts.md``.
    """

    if status in _EVIDENCE_PENDING_STATUSES:
        return "pending"
    if latest is None:
        return "pending"
    if latest == "quarantined":
        return "pending"
    if latest == "reversed":
        return "reversed"
    if paid_through is None:
        return "pending"
    if paid_through >= today:
        return "paid"
    return "overdue"


async def read_current_subscription(
    db: AsyncSession,
    *,
    builder_id: UUID,
    clock: Callable[[], datetime] | None = None,
) -> SubscriptionReadback | None:
    """Return the builder's current ASAAS subscription, else the cancelled one
    still inside its paid period, else ``None``.

    ``status`` is ``billing_subscriptions.status``, the financial state the
    webhook/readback owns (``active_linked`` until the first settlement,
    ``active`` after it, ``readback_required``/``non_billable_pending_readback``
    while provider evidence is pending); never the one-way
    ``builders.subscription_status`` flag.  ``current_period_end`` is the
    latest ``cycle_end`` among the subscription's cycles that are fully paid
    net of reversals (``state`` ``settled`` or ``restored`` and
    ``reversed_cents = restored_cents``); a reversed, partially restored,
    pending or quarantined cycle is skipped, so a currently reversed cycle
    falls back to the previous fully paid one, and ``None`` means no cycle
    ever was.  A partially reversed cycle does not count (owner decision B).

    Owner decision A2: with no current (non-terminal) row, a ``cancelled``
    row (the provider read back ``deleted: true``) is returned while its
    ``current_period_end`` is on or after today's São Paulo date (``clock``,
    default the UTC wall clock, read once); among several, the longest paid
    period, then the newest.  ``inactive`` (provider ``INACTIVE``/``EXPIRED``),
    legacy (non-ASAAS) and every other ended row are not returned.

    One statement (one snapshot) reads the row, its paid-through date and its
    newest cycle (by ``cycle_start``), from which ``payment_state`` is
    derived.  An unexpected shape (two current rows, a missing snapshot
    column, an unknown status or cycle state, a paid-through value that is
    not exactly a ``date``, a naive clock) fails closed.
    """

    today = billing_today(clock() if clock is not None else _utc_now())
    cycle = BillingSubscriptionCycle
    own_cycles = and_(
        cycle.builder_id == builder_id,
        cycle.subscription_id == BillingSubscription.id,
    )
    paid_through = (
        select(func.max(cycle.cycle_end))
        .where(
            own_cycles,
            # Fully paid net of reversals: settled, or restored in full.
            cycle.state.in_(_PAID_CYCLE_STATES),
            cycle.reversed_cents == cycle.restored_cents,
        )
        .correlate(BillingSubscription)
        .scalar_subquery()
    )

    def newest(column: InstrumentedAttribute[_T]) -> ScalarSelect[_T]:
        return (
            select(column)
            .where(own_cycles)
            .order_by(cycle.cycle_start.desc(), cycle.created_at.desc(), cycle.id)
            .limit(1)
            .correlate(BillingSubscription)
            .scalar_subquery()
        )

    current = BillingSubscription.status.not_in(TERMINAL_SUBSCRIPTION_STATUSES)
    rows = (
        await db.execute(
            select(
                BillingSubscription.offer_id,
                BillingSubscription.catalog_version_id,
                BillingSubscription.status,
                BillingSubscription.amount_cents,
                paid_through.label("paid_through"),
                newest(cycle.state).label("newest_state"),
                newest(cycle.reversed_cents).label("newest_reversed_cents"),
                newest(cycle.restored_cents).label("newest_restored_cents"),
            )
            .where(
                BillingSubscription.builder_id == builder_id,
                BillingSubscription.provider == "asaas",
                or_(
                    current,
                    and_(
                        BillingSubscription.status == _CANCELLED,
                        paid_through >= today,
                    ),
                ),
            )
            # Current rows first; then the longest paid period, the newest.
            .order_by(
                case((current, 0), else_=1),
                paid_through.desc(),
                BillingSubscription.created_at.desc(),
                BillingSubscription.id,
            )
            .limit(2)
        )
    ).all()
    if not rows:
        return None
    if len(rows) > 1 and rows[1].status != _CANCELLED:
        # Current rows sort first, so this is a second current recurring
        # subscription: a drift state, not a choice.
        raise BillingReadUnavailable()
    (
        offer_id,
        catalog_version_id,
        status,
        amount_cents,
        period_end,
        newest_state,
        newest_reversed_cents,
        newest_restored_cents,
    ) = rows[0]
    if (
        not isinstance(offer_id, str)
        or not offer_id
        or not isinstance(catalog_version_id, str)
        or not catalog_version_id
        or status not in _SUMMARY_STATUSES
        or type(amount_cents) is not int
        or amount_cents < 0
    ):
        raise BillingReadUnavailable()
    if period_end is not None and type(period_end) is not date:
        # Owner A4: a calendar date only.  ``datetime`` subclasses ``date``,
        # so an instant (with or without a zone) must not pass for one.
        raise BillingReadUnavailable()
    latest = (
        None
        if newest_state is None
        else latest_cycle_kind(
            newest_state, newest_reversed_cents, newest_restored_cents
        )
    )
    summary_status = cast(SubscriptionSummaryStatus, status)
    return SubscriptionReadback(
        offer_id=offer_id,
        catalog_version_id=catalog_version_id,
        status=summary_status,
        amount_cents=amount_cents,
        current_period_end=period_end,
        payment_state=derive_payment_state(
            status=summary_status,
            latest=latest,
            paid_through=period_end,
            today=today,
        ),
    )


async def read_live_checkout(
    db: AsyncSession, *, builder_id: UUID
) -> LiveCheckoutReadback | None:
    """Return the builder's newest live checkout attempt, else ``None``.

    ``offer_id`` is that attempt's own catalog offer (its session row, owner
    decision A3), never inferred from the catalog or the subscription.
    """

    row = (
        await db.execute(
            select(
                BillingCheckoutSession.id,
                BillingCheckoutSession.intent_kind,
                BillingCheckoutSession.state,
                BillingCheckoutSession.offer_id,
            )
            .where(
                BillingCheckoutSession.builder_id == builder_id,
                BillingCheckoutSession.state.in_(LIVE_CHECKOUT_STATES),
            )
            .order_by(
                BillingCheckoutSession.created_at.desc(),
                BillingCheckoutSession.id,
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    checkout_session_id, kind, state, offer_id = row
    if (
        kind not in _KINDS
        or state not in LIVE_CHECKOUT_STATES
        or not isinstance(offer_id, str)
        or not offer_id
    ):
        raise BillingReadUnavailable()
    if not isinstance(checkout_session_id, UUID):
        checkout_session_id = UUID(str(checkout_session_id))
    return LiveCheckoutReadback(
        checkout_session_id=checkout_session_id,
        kind=cast(CheckoutKind, kind),
        state=cast(LiveCheckoutState, state),
        offer_id=offer_id,
    )


async def read_commercial_availability(
    db: AsyncSession,
    *,
    builder_id: UUID,
    clock: Callable[[], datetime] = _utc_now,
) -> CommercialReadback:
    """Advisory offer availability plus ``topup_commercial_enabled``.

    An offer is listed exactly when a no-coupon ``POST /v1/billing/checkout``
    for it (with a permitted method) would pass every rollout/eligibility
    decision and return a checkout instead of a 409/503:

    (It does not evaluate the financial-egress kill switch, webhook readiness
    or the payment method, so the ``POST`` can still answer 503 for a listed
    offer.)

    1. the persisted rollout pre-check (``require_billing_checkout_rollout``:
       compatible ASAAS configuration and ``general``, or ``canary_allowlist``
       for the canary builder) must pass, otherwise nothing is listed;
    2. an offer with a live attempt of this builder is listed (the ``POST``
       returns that attempt unchanged);
    3. otherwise, under ``general``: a subscription offer needs no
       ``subscription_checkout_blocker``; a top-up needs the open top-up gate
       with a currently valid capability; under ``canary_allowlist``: only the
       canary offer whose lifetime slot is still free
       (``BillingRepository.canary_reservation_admits``), a subscription also
       without a blocker.

    ``topup_commercial_enabled`` is true only when the stored flag is true,
    the rollout is ``general`` and the active capability is currently valid
    (``BillingRepository.active_topup_capability_is_current``), and the
    pre-check passes.

    A closed rollout is a successful read (``available=True``, nothing
    listed).  Any read failure (including the pre-check's own failed query,
    which it reports as ``BillingCheckoutUnavailable`` with the database error
    chained as ``__cause__``) answers ``_UNAVAILABLE``: no offer, flag false,
    ``available=False``, one counter increment and one structured warning.
    The outcome is per request; nothing is cached or shared across builders.
    """

    try:
        try:
            await require_billing_checkout_rollout(db, builder_id)
        except BillingCheckoutUnavailable as exc:
            if exc.__cause__ is not None:
                raise
            return _CLOSED
        return await _commercial_availability(db, builder_id=builder_id, now=clock())
    except Exception as exc:
        return _unavailable(exc)


def _unavailable(exc: BaseException) -> CommercialReadback:
    cause = exc.__cause__ if isinstance(exc, BillingCheckoutUnavailable) else exc
    try:
        BILLING_SUMMARY_CHECKOUT_UNAVAILABLE_TOTAL.inc()
    except Exception:  # pragma: no cover - telemetry is best effort
        logger.warning("billing metric emission failed")
    # The class name only: driver messages can carry SQL or identifiers.
    logger.warning(
        "billing summary checkout availability unavailable",
        error_type=type(cause).__name__,
    )
    return _UNAVAILABLE


async def _commercial_availability(
    db: AsyncSession, *, builder_id: UUID, now: datetime
) -> CommercialReadback:
    environment = settings.environment
    repository = BillingRepository(db)
    control = await repository.read_rollout_control(environment=environment)
    if control is None or control.state not in ("general", "canary_allowlist"):
        return _CLOSED
    general = control.state == "general"
    topup_open = bool(
        general
        and control.topup_commercial_enabled is True
        and await repository.active_topup_capability_is_current(
            control=control, environment=environment, now=now, for_update=False
        )
    )
    subscription_blocked = (
        await repository.subscription_checkout_blocker(builder_id=builder_id)
        is not None
    )
    live_offer_ids = frozenset(
        (
            await db.scalars(
                select(BillingCheckoutSession.offer_id).where(
                    BillingCheckoutSession.builder_id == builder_id,
                    BillingCheckoutSession.state.in_(LIVE_CHECKOUT_STATES),
                )
            )
        ).all()
    )

    available: list[str] = []
    for offer_id, offer in BILLING_CATALOG.items():
        subscription = offer.cycle == "MONTHLY"
        if offer_id in live_offer_ids:
            admitted = True
        elif general:
            admitted = not subscription_blocked if subscription else topup_open
        else:
            admitted = repository.canary_reservation_admits(
                control, builder_id=builder_id, offer_id=offer_id
            ) and not (subscription and subscription_blocked)
        if admitted:
            available.append(offer_id)
    return CommercialReadback(
        available_offer_ids=tuple(available),
        topup_commercial_enabled=topup_open,
        available=True,
    )
