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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Final, Literal, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from src.services.billing_rollout import (
    BillingCheckoutUnavailable,
    require_billing_checkout_rollout,
)

logger = get_logger(__name__)

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
_KINDS: Final = frozenset(("subscription", "topup"))
# Cycle states that can be fully paid; ``current_period_end`` additionally
# requires ``reversed_cents = restored_cents`` (nothing net reversed).
_PAID_CYCLE_STATES: Final = ("settled", "restored")


class BillingReadUnavailable(RuntimeError):
    """The confirmed financial state could not be read without guessing."""

    code = BILLING_SUMMARY_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SubscriptionReadback:
    """The builder's current (non-terminal) ASAAS subscription record."""

    offer_id: str
    catalog_version_id: str
    status: SubscriptionFinancialStatus
    amount_cents: int
    current_period_end: date | None


@dataclass(frozen=True, slots=True)
class LiveCheckoutReadback:
    """The builder's most recent live checkout attempt (no provider URL)."""

    checkout_session_id: UUID
    kind: CheckoutKind
    state: LiveCheckoutState


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


async def read_current_subscription(
    db: AsyncSession, *, builder_id: UUID
) -> SubscriptionReadback | None:
    """Return the builder's non-terminal ASAAS subscription, else ``None``.

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
    ever was.  A partially reversed cycle does not count (conservative owner
    policy default).  Legacy (non-ASAAS) and terminal rows
    are not a current ASAAS subscription.  An unexpected shape (two current
    rows, a missing snapshot column, an unknown status) fails closed.
    """

    rows = (
        await db.execute(
            select(
                BillingSubscription.id,
                BillingSubscription.offer_id,
                BillingSubscription.catalog_version_id,
                BillingSubscription.status,
                BillingSubscription.amount_cents,
            )
            .where(
                BillingSubscription.builder_id == builder_id,
                BillingSubscription.provider == "asaas",
                BillingSubscription.status.not_in(TERMINAL_SUBSCRIPTION_STATUSES),
            )
            .order_by(BillingSubscription.created_at.desc(), BillingSubscription.id)
            .limit(2)
        )
    ).all()
    if not rows:
        return None
    if len(rows) != 1:
        # A second recurring subscription is a drift state, not a choice.
        raise BillingReadUnavailable()
    subscription_id, offer_id, catalog_version_id, status, amount_cents = rows[0]
    if (
        not isinstance(offer_id, str)
        or not offer_id
        or not isinstance(catalog_version_id, str)
        or not catalog_version_id
        or status not in _SUBSCRIPTION_STATUSES
        or type(amount_cents) is not int
        or amount_cents < 0
    ):
        raise BillingReadUnavailable()
    period_end = await db.scalar(
        select(func.max(BillingSubscriptionCycle.cycle_end)).where(
            BillingSubscriptionCycle.builder_id == builder_id,
            BillingSubscriptionCycle.subscription_id == subscription_id,
            # Fully paid net of reversals: settled, or restored in full.
            BillingSubscriptionCycle.state.in_(_PAID_CYCLE_STATES),
            BillingSubscriptionCycle.reversed_cents
            == BillingSubscriptionCycle.restored_cents,
        )
    )
    if period_end is not None and not isinstance(period_end, date):
        raise BillingReadUnavailable()
    return SubscriptionReadback(
        offer_id=offer_id,
        catalog_version_id=catalog_version_id,
        status=cast(SubscriptionFinancialStatus, status),
        amount_cents=amount_cents,
        current_period_end=period_end,
    )


async def read_live_checkout(
    db: AsyncSession, *, builder_id: UUID
) -> LiveCheckoutReadback | None:
    """Return the builder's newest live checkout attempt, else ``None``."""

    row = (
        await db.execute(
            select(
                BillingCheckoutSession.id,
                BillingCheckoutSession.intent_kind,
                BillingCheckoutSession.state,
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
    checkout_session_id, kind, state = row
    if kind not in _KINDS or state not in LIVE_CHECKOUT_STATES:
        raise BillingReadUnavailable()
    if not isinstance(checkout_session_id, UUID):
        checkout_session_id = UUID(str(checkout_session_id))
    return LiveCheckoutReadback(
        checkout_session_id=checkout_session_id,
        kind=cast(CheckoutKind, kind),
        state=cast(LiveCheckoutState, state),
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
