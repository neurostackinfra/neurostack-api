"""ASAAS webhook receiver, payment projections and financial lifecycle.

Trust boundary.  ``IngressBodyLimitMiddleware`` authenticates the
``asaas-access-token`` header in constant time before reading a body, bounds
the wire body to 262,144 bytes and five seconds (chunked included), and hands
this module a strictly parsed JSON object.  Nothing here persists headers, the
raw body, customer/account objects or payment objects: a delivery row is the
minimal envelope ``{provider, event_id, event_name, date_created, received_at,
resource_kind, resource_id, body_bytes, body_sha256}``.

Tenant isolation.  A builder is derived only from a persisted checkout
session, subscription, top-up order or projection that an exact provider ID
(or our own session UUID echoed as ``externalReference``) resolves to.  Names,
e-mails, amounts and "most recent" heuristics never correlate anything; a
known event without an exact entity is recorded, blocked and alerted.

Money.  Settled, reversed and restored cents come from the persisted snapshot
(session/order/subscription ``amount_cents``) or, for partial refunds, from
an exact provider readback of ``DONE`` refunds bounded by the snapshot.  The
webhook body can block an effect (amount drift) but can never price one.

Serialization.  Every authenticated, allowlisted event takes, in order: the
delivery idempotency key (``ON CONFLICT DO NOTHING``), the environment rollout
lock (``lock_rollout_control`` — the same lock Group 2 eligibility holds, so
a session→subscription handoff can never interleave with a checkout
eligibility decision), the global roster fence when a subscription row or a
builder entitlement may change, then the session, subscription/order and
projection row locks.  Effects are unique on ``(provider, payment_id,
effect_kind, source_revision)``; ``PAYMENT_CONFIRMED`` and
``PAYMENT_RECEIVED`` share ``settlement:v1``.

Settlement (owner decision 2026-10-18).  A card capture (``CONFIRMED``) is
commercially paid: the first accepted settle-class evidence (``CONFIRMED`` or
``RECEIVED``, event or exact read) creates the only settlement effect, cycle,
fact revision, activation and Transaction; a later one is normally an inert
confirmation (``RECEIVED`` is "funds available", ~D+32 for a card).  It is
not always inert: a terminal pre-settlement refund marker, a disputed or
reversal label (H-1/L-3), a stale capture against a newer refusal or
refund-first row (M2/L-2), or a canary capture while a refund/chargeback
delivery of that payment is still open (M-2) needs a read instead.
``paid_at`` is write-once: the first strict ``YYYY-MM-DD`` of
``confirmedDate``, ``clientPaymentDate``, ``paymentDate`` that is not later
than the São Paulo day of the observation (``resolve_paid_at``); nothing is
inferred from a timestamp, and no usable date fails closed.

Network.  No provider call is made while a database lock is held.  Readbacks
run after the delivery commit (in a background task and from the backlog
sweep) and apply their result in a fresh locked transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Final, Mapping
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr
from sqlalchemy import func, or_, select
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.config.settings import settings
from src.models.billing import (
    BillingCheckoutSession,
    BillingPaymentEffect,
    BillingPaymentProjection,
    BillingSubscription,
    BillingTopupOrder,
    BillingWebhookDelivery,
)
from src.models.builder import Builder
from src.models.transaction import Transaction
from src.observability.metrics import (
    BILLING_ALERTS_TOTAL,
    BILLING_READBACK_BACKLOG,
    BILLING_READBACKS_TOTAL,
    BILLING_WEBHOOK_CONFIG_READY,
    BILLING_WEBHOOK_DELIVERIES_TOTAL,
    BILLING_WEBHOOK_FAILURES_TOTAL,
    BILLING_WEBHOOK_REJECTIONS_TOTAL,
)
from src.repositories.billing import (
    BillingCanaryBudgetExceeded,
    BillingCapabilityRejected,
    BillingRepository,
    BillingStateUnavailable,
    BillingTransitionRejected,
)
from src.services.asaas import (
    AsaasClient,
    PaymentReadback,
    SubscriptionReadback,
    WebhookConfig,
    WebhookSettings,
    provider_cents,
)
from src.services.billing_checkout import (
    CHECKOUT_CONVERGENCE_LIMIT,
    ORPHANED_CREATING_AFTER,
    BillingCheckoutService,
)
from src.services.billing_outbox import BillingFactProducer

logger = get_logger(__name__)

PROVIDER: Final = "asaas"
ASAAS_WEBHOOK_PATH: Final = "/v1/billing/webhook/asaas"
ASAAS_WEBHOOK_TOKEN_HEADER: Final = "asaas-access-token"
ASAAS_WEBHOOK_BODY_BYTES: Final = 262_144
ASAAS_WEBHOOK_READ_TIMEOUT_SECONDS: Final = 5.0
# Keys under ``scope["state"]`` written only by the ingress boundary.
ASAAS_INGRESS_BODY_STATE: Final = "asaas_webhook_body"
ASAAS_INGRESS_PAYLOAD_STATE: Final = "asaas_webhook_payload"

# ---------------------------------------------------------------------------
# Closed allowlist and normalized mapping (DESIGN "allowlist fechada").
# ---------------------------------------------------------------------------

CHECKOUT_EVENT_STATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "CHECKOUT_CREATED": "active",
        "CHECKOUT_PAID": "paid_pending_subscription",
        "CHECKOUT_CANCELED": "terminal_pending_readback",
        "CHECKOUT_EXPIRED": "terminal_pending_readback",
    }
)
SUBSCRIPTION_EVENT_STATUSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "SUBSCRIPTION_CREATED": "active_linked",
        "SUBSCRIPTION_UPDATED": "readback_required",
        "SUBSCRIPTION_INACTIVATED": "non_billable_pending_readback",
        "SUBSCRIPTION_DELETED": "non_billable_pending_readback",
    }
)
PAYMENT_EVENT_STATUSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "PAYMENT_CREATED": "pending",
        "PAYMENT_AWAITING_RISK_ANALYSIS": "pending",
        "PAYMENT_APPROVED_BY_RISK_ANALYSIS": "pending",
        "PAYMENT_AUTHORIZED": "pending",
        "PAYMENT_UPDATED": "pending",
        "PAYMENT_REPROVED_BY_RISK_ANALYSIS": "failed",
        "PAYMENT_CREDIT_CARD_CAPTURE_REFUSED": "failed",
        "PAYMENT_OVERDUE": "overdue",
        # Card capture: commercially paid (funds arrive ~D+32 as RECEIVED).
        "PAYMENT_CONFIRMED": "settled",
        "PAYMENT_RECEIVED": "settled",
        "PAYMENT_REFUND_IN_PROGRESS": "reversal_pending",
        "PAYMENT_PARTIALLY_REFUNDED": "partially_reversed",
        "PAYMENT_REFUNDED": "reversed",
        "PAYMENT_CHARGEBACK_REQUESTED": "chargeback_requested",
        "PAYMENT_CHARGEBACK_DISPUTE": "chargeback_dispute",
        "PAYMENT_AWAITING_CHARGEBACK_REVERSAL": "restoration_pending",
        "PAYMENT_DELETED": "deleted",
        "PAYMENT_RESTORED": "readback_required",
        # Official (docs.asaas.com, boleto-only today): a refund that did not
        # happen; only an exact readback may restore the projection.
        "PAYMENT_REFUND_DENIED": "readback_required",
    }
)
# 27 names: DESIGN's closed allowlist (26) plus ``PAYMENT_REFUND_DENIED``,
# an official event (docs.asaas.com "Webhook para cobranças") that ASAAS emits
# only for boleto; NeuroStack sells no boleto, so it is readback-only.
WEBHOOK_EVENTS: Final = frozenset(
    (*CHECKOUT_EVENT_STATES, *SUBSCRIPTION_EVENT_STATUSES, *PAYMENT_EVENT_STATUSES)
)
_EVENT_FAMILIES: Final[Mapping[str, str]] = MappingProxyType(
    {
        **{name: "checkout" for name in CHECKOUT_EVENT_STATES},
        **{name: "subscription" for name in SUBSCRIPTION_EVENT_STATUSES},
        **{name: "payment" for name in PAYMENT_EVENT_STATUSES},
    }
)

# Provider payment status (exact-ID readback) -> normalized projection status.
_READBACK_PAYMENT_STATUSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "PENDING": "pending",
        "AWAITING_RISK_ANALYSIS": "pending",
        "OVERDUE": "overdue",
        "CONFIRMED": "settled",
        "RECEIVED": "settled",
        # Cash received outside ASAAS is never a NeuroStack settlement.
        "RECEIVED_IN_CASH": "pending",
        "REFUND_REQUESTED": "reversal_pending",
        "REFUND_IN_PROGRESS": "reversal_pending",
        "REFUNDED": "reversed",
        "CHARGEBACK_REQUESTED": "chargeback_requested",
        "CHARGEBACK_DISPUTE": "chargeback_dispute",
        "AWAITING_CHARGEBACK_REVERSAL": "restoration_pending",
    }
)
# Provider statuses that are still collectable (a "pending charge").
_PENDING_CHARGE_STATUSES: Final = frozenset(
    {"PENDING", "OVERDUE", "AWAITING_RISK_ANALYSIS"}
)

# Tie-break rank for events sharing one provider second; never regresses.
_STATUS_RANK: Final[Mapping[str, int]] = MappingProxyType(
    {
        "pending": 10,
        "failed": 20,
        "overdue": 20,
        "settled": 30,
        "reversal_pending": 40,
        "chargeback_requested": 40,
        "chargeback_dispute": 45,
        "partially_reversed": 50,
        "reversed": 60,
        "restoration_pending": 65,
        "restored": 66,
        "deleted": 70,
        "readback_required": 80,
    }
)
_MONEY_SETTLED_STATUSES: Final = frozenset(
    {
        "settled",
        "reversal_pending",
        "partially_reversed",
        "reversed",
        "chargeback_requested",
        "chargeback_dispute",
        "restoration_pending",
        "restored",
    }
)
_NON_FINANCIAL_STATUSES: Final = frozenset({"pending", "failed", "overdue"})
_LIVE_PROVIDER_STATUSES: Final = frozenset({"pending", "overdue", "settled"})
# Newer settle-class evidence on these labels is never inert: a won chargeback
# ends in CONFIRMED/RECEIVED (H-1), and a capture or receipt after a refund
# contradicts the reversal (L-3).  Only an exact read relabels them; never a
# second effect and never an alert.
_SETTLE_EVIDENCE_NEEDS_READ: Final = frozenset(
    {
        "restoration_pending",
        "chargeback_requested",
        "chargeback_dispute",
        "reversal_pending",
        "partially_reversed",
        "reversed",
    }
)
# M-2: deliveries that, while not ``processed``, keep a canary payment's
# first write from settling from an event (the DB cannot store the canary
# refund marker).  Derived from the closed allowlist, never hand-listed.
_REVERSAL_CLASS_EVENTS: Final = frozenset(
    name
    for name, status in PAYMENT_EVENT_STATUSES.items()
    if status
    in {
        "reversal_pending",
        "partially_reversed",
        "reversed",
        "chargeback_requested",
        "chargeback_dispute",
        "restoration_pending",
    }
)
# H-2: a newer "unpaid" event on settled money contradicts the settlement.
_UNPAID_STATUSES: Final = frozenset({"failed", "overdue"})
# The event whose envelope time anchors the ``paid_at`` bound.
_CAPTURE_EVENT: Final = "PAYMENT_CONFIRMED"

_LIVE_CHECKOUT_STATES: Final = (
    "creating",
    "active",
    "paid_pending_subscription",
    "reconciliation_required",
    "terminal_pending_readback",
)
_TERMINAL_CHECKOUT_STATES: Final = frozenset(
    {"completed", "abandoned", "cancelled", "expired", "failed"}
)
_LIVE_SUBSCRIPTION_STATUSES: Final = frozenset(
    {"active", "active_linked", "readback_required"}
)
_TERMINAL_SUBSCRIPTION_STATUSES: Final = ("inactive", "cancelled")
# Group 1 canary trigger pins the plan slug of ``plan_plus_monthly_v1``.
OFFER_PLANS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "plan_plus_monthly_v1": "plus",
        "plan_pro_monthly_v1": "pro",
        "plan_max_monthly_v1": "max",
    }
)
SETTLEMENT_REVISION: Final = "settlement:v1"
REFUNDED_BEFORE_SETTLEMENT: Final = "refunded_before_settlement"

# Provider event ids are opaque: the documented shape is
# ``evt_<32 hex>&<digits>`` and ``&`` is outside the Group 1 delivery CHECK
# ``^[A-Za-z0-9._:-]+$``.  The stored key is therefore derived, never the raw
# string (see ``event_key``).  Only control characters, invalid Unicode and an
# absurd length are refused; nothing else about the provider string is assumed.
MAX_PROVIDER_EVENT_ID_CHARS: Final = 512
_EVENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_BRAZIL: Final = ZoneInfo("America/Sao_Paulo")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite returns naive UTC timestamps; PostgreSQL returns aware ones."""

    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Sanitized metrics.  Telemetry must never break or reveal a financial path.
# ---------------------------------------------------------------------------


def _count(metric: Any, **labels: str) -> None:
    try:
        (metric.labels(**labels) if labels else metric).inc()
    except Exception:  # pragma: no cover - telemetry is best effort
        logger.warning("billing metric emission failed")


def record_rejection(reason: str) -> None:
    """Count one pre-persistence rejection with a closed reason."""

    _count(BILLING_WEBHOOK_REJECTIONS_TOTAL, reason=reason)


def alert(kind: str) -> None:
    """Raise one sanitized operator alert (closed vocabulary, no identities)."""

    _count(BILLING_ALERTS_TOTAL, kind=kind)
    logger.warning("billing alert", alert_kind=kind)


def _readback_metric(
    resource: str, outcome: str, exc: BaseException | None = None
) -> None:
    _count(BILLING_READBACKS_TOTAL, resource=resource, outcome=outcome)
    if outcome == "failed":
        if exc is not None:
            logger.warning(
                "billing readback failure",
                resource=resource,
                failure=classify_failure(exc)[0],
                failure_class=type(exc).__name__,
            )
        alert("readback_failure")
    elif outcome == "drift":
        alert("drift")


# ---------------------------------------------------------------------------
# Envelope parsing.
# ---------------------------------------------------------------------------


class WebhookEnvelopeRejected(ValueError):
    """The authenticated body is not a usable ASAAS event envelope."""

    code = "billing_webhook_invalid_event"


class WebhookProcessingUnavailable(RuntimeError):
    """A transient failure: nothing was committed, the provider must retry."""

    code = "billing_webhook_unavailable"

    def __init__(self, failure_class: str = "unexpected") -> None:
        self.failure_class = failure_class
        super().__init__(self.code)


_DETERMINISTIC_FAILURES: Final[tuple[tuple[type[BaseException], str], ...]] = (
    (IntegrityError, "integrity"),
    (DataError, "data_error"),
    (BillingCanaryBudgetExceeded, "canary_budget_exceeded"),
    (BillingTransitionRejected, "transition_rejected"),
    (BillingCapabilityRejected, "capability_rejected"),
    (BillingStateUnavailable, "state_unavailable"),
)
_SERIALIZATION_SQLSTATES: Final = frozenset({"40001", "40P01"})
_LOCK_SQLSTATES: Final = frozenset({"55P03", "57014"})


def _sqlstate(exc: BaseException) -> str | None:
    for candidate in (getattr(exc, "orig", None), getattr(exc, "__cause__", None)):
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(candidate, attribute, None)
            if isinstance(value, str):
                return value
        nested = getattr(candidate, "__cause__", None)
        value = getattr(nested, "sqlstate", None)
        if isinstance(value, str):
            return value
    return None


def classify_failure(exc: BaseException) -> tuple[str, bool]:
    """Return ``(bounded class label, deterministic?)`` for one failure.

    Deterministic: DB integrity/data errors and domain refusals that repeat
    identically on every retry.  Transient (retry): serialization/deadlock,
    lock and statement timeouts, any other database/connection/timeout
    failure, and every unexpected error (fail safe: the provider retries).
    """

    for kind, label in _DETERMINISTIC_FAILURES:
        if isinstance(exc, kind):
            return label, True
    if isinstance(exc, DBAPIError):
        state = _sqlstate(exc)
        if state in _SERIALIZATION_SQLSTATES:
            return "serialization", False
        if state in _LOCK_SQLSTATES:
            return "lock_timeout", False
        return "database", False
    if isinstance(exc, TimeoutError):
        return "timeout", False
    if isinstance(exc, OSError):
        return "connection", False
    return "unexpected", False


_EFFECT_IDEMPOTENCY_MARKERS: Final = (
    "uq_billing_payment_effects_idempotency",
    "billing_payment_effects.provider, billing_payment_effects.payment_id, "
    "billing_payment_effects.effect_kind, billing_payment_effects.source_revision",
)


def effect_already_applied(exc: BaseException) -> bool:
    """True only when the effect-idempotency unique key itself refused."""

    if not isinstance(exc, IntegrityError):
        return False
    message = str(getattr(exc, "orig", exc))
    return any(marker in message for marker in _EFFECT_IDEMPOTENCY_MARKERS)


def record_failure(label: str, disposition: str, exc: BaseException) -> None:
    """Count and log one failure with its class name only (no payload/ids)."""

    _count(BILLING_WEBHOOK_FAILURES_TOTAL, failure=label, disposition=disposition)
    logger.warning(
        "billing webhook failure",
        failure=label,
        failure_class=type(exc).__name__,
        disposition=disposition,
    )
    # The quarantine alert itself is raised once, from the committed outcome.


def _opaque_id(value: object) -> str | None:
    if isinstance(value, str) and _PROVIDER_ID.fullmatch(value):
        return value
    return None


def _uuid_reference(value: object) -> UUID | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value.lower() else None


def _provider_instant(value: object) -> datetime:
    """Parse ``dateCreated`` (``YYYY-MM-DD HH:MM:SS`` São Paulo, or ISO)."""

    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        raise WebhookEnvelopeRejected()
    parsed: datetime | None = None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise WebhookEnvelopeRejected() from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_BRAZIL)
    instant = parsed.astimezone(timezone.utc)
    if not 2000 <= instant.year <= 2200:
        raise WebhookEnvelopeRejected()
    return instant


def _billing_type(value: object) -> str | None:
    return {"CREDIT_CARD": "credit_card", "PIX": "pix"}.get(
        value if isinstance(value, str) else ""
    )


@dataclass(frozen=True, slots=True)
class CheckoutFacts:
    checkout_id: str | None
    external_reference: UUID | None


@dataclass(frozen=True, slots=True)
class SubscriptionFacts:
    subscription_id: str | None
    external_reference: UUID | None
    status: str | None
    deleted: bool
    cycle: str | None
    value_cents: int | None
    next_due_date: date | None


@dataclass(frozen=True, slots=True)
class PaymentFacts:
    payment_id: str | None
    subscription_id: str | None
    checkout_id: str | None
    external_reference: UUID | None
    value_cents: int | None
    billing_type: str | None
    due_date: date | None = None
    payment_date: date | None = None
    confirmed_date: date | None = None
    client_payment_date: date | None = None


@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    """Minimal authenticated envelope plus exact identifiers of its resource.

    ``event_id`` is the derived storage key ``sha256:<hex>`` of the exact
    provider id (``event_key``); the raw provider id is never persisted and is
    not needed for correctness.
    """

    event_id: str
    event_name: str
    provider_created_at: datetime
    body_bytes: int
    body_sha256: str
    family: str | None
    checkout: CheckoutFacts | None = None
    subscription: SubscriptionFacts | None = None
    payment: PaymentFacts | None = None
    # Exact IDs of an unknown event, used only to correlate resource_id.
    loose_ids: tuple[tuple[str, str], ...] = ()

    @property
    def known(self) -> bool:
        return self.family is not None

    def ordering_key(self, status: str) -> str:
        return ordering_key(self.provider_created_at, status, self.event_id)


def event_key(provider_event_id: str) -> str:
    """Deterministic, injective storage key for an opaque provider event id.

    ``sha256:`` + the hex SHA-256 of the exact UTF-8 provider string: 71 ASCII
    characters inside the delivery CHECK and the 160-character column, equal
    for redeliveries of the same event and distinct for ids that differ in any
    character (including after ``&``).
    """

    return "sha256:" + hashlib.sha256(provider_event_id.encode("utf-8")).hexdigest()


def _provider_event_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_PROVIDER_EVENT_ID_CHARS
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise WebhookEnvelopeRejected()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:  # lone surrogate from a JSON escape
        raise WebhookEnvelopeRejected() from exc
    return value


def ordering_key(instant: datetime, status: str, event_id: str) -> str:
    """Lexicographically ordered ``provider time . lifecycle rank . event key``.

    Provider time decides; the lifecycle rank breaks same-second ties; the
    final component (the hashed event key) only orders two *different* events
    with the same second and the same rank.  That last tie-break carries no
    meaning, but it is deterministic and independent of arrival order, so a
    replay of the same set of events converges to the same projection, and a
    redelivery (same key) is never newer than itself.
    """

    micros = int(_aware(instant).timestamp() * 1_000_000)
    return f"{micros:017d}.{_STATUS_RANK.get(status, 90):02d}.{event_id}"


def _date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def parse_envelope(body: bytes, payload: object) -> WebhookEnvelope:
    """Validate the envelope and extract only allowlisted exact identifiers."""

    if not isinstance(payload, dict):
        raise WebhookEnvelopeRejected()
    event_id = event_key(_provider_event_id(payload.get("id")))
    event_name = payload.get("event")
    if not isinstance(event_name, str) or not _EVENT_NAME.fullmatch(event_name):
        raise WebhookEnvelopeRejected()
    if not 2 <= len(body) <= ASAAS_WEBHOOK_BODY_BYTES:
        raise WebhookEnvelopeRejected()
    created_at = _provider_instant(payload.get("dateCreated"))
    common: dict[str, Any] = {
        "event_id": event_id,
        "event_name": event_name,
        "provider_created_at": created_at,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
    family = _EVENT_FAMILIES.get(event_name)
    if family is None:
        loose: list[tuple[str, str]] = []
        for key in ("checkout", "subscription", "payment"):
            resource = payload.get(key)
            identifier = (
                _opaque_id(resource.get("id")) if isinstance(resource, dict) else None
            )
            if identifier is not None:
                loose.append((key, identifier))
        return WebhookEnvelope(family=None, loose_ids=tuple(loose), **common)
    resource = payload.get(family)
    data: Mapping[str, Any] = resource if isinstance(resource, dict) else {}
    if family == "checkout":
        return WebhookEnvelope(
            family=family,
            checkout=CheckoutFacts(
                checkout_id=_opaque_id(data.get("id")),
                external_reference=_uuid_reference(data.get("externalReference")),
            ),
            **common,
        )
    if family == "subscription":
        return WebhookEnvelope(
            family=family,
            subscription=SubscriptionFacts(
                subscription_id=_opaque_id(data.get("id")),
                external_reference=_uuid_reference(data.get("externalReference")),
                status=(
                    data.get("status") if isinstance(data.get("status"), str) else None
                ),
                deleted=data.get("deleted") is True,
                cycle=data.get("cycle") if isinstance(data.get("cycle"), str) else None,
                value_cents=provider_cents(data.get("value")),
                next_due_date=_date(data.get("nextDueDate")),
            ),
            **common,
        )
    return WebhookEnvelope(
        family=family,
        payment=PaymentFacts(
            payment_id=_opaque_id(data.get("id")),
            subscription_id=_opaque_id(data.get("subscription")),
            checkout_id=_opaque_id(data.get("checkoutSession")),
            external_reference=_uuid_reference(data.get("externalReference")),
            value_cents=provider_cents(data.get("value")),
            billing_type=_billing_type(data.get("billingType")),
            due_date=_date(data.get("dueDate")),
            payment_date=_date(data.get("paymentDate")),
            confirmed_date=_date(data.get("confirmedDate")),
            client_payment_date=_date(data.get("clientPaymentDate")),
        ),
        **common,
    )


def _payment_facts_from_readback(readback: PaymentReadback) -> PaymentFacts:
    return PaymentFacts(
        payment_id=readback.payment_id,
        subscription_id=readback.subscription_id,
        checkout_id=readback.checkout_id,
        external_reference=_uuid_reference(readback.external_reference),
        value_cents=readback.value_cents,
        billing_type=_billing_type(readback.billing_type),
        due_date=readback.due_date,
        payment_date=readback.payment_date,
        confirmed_date=readback.confirmed_date,
        client_payment_date=readback.client_payment_date,
    )


def _sao_paulo_day(instant: datetime) -> date:
    """The civil date of an instant in ``America/Sao_Paulo`` (never UTC)."""

    return _aware(instant).astimezone(_BRAZIL).date()


def resolve_paid_at(facts: PaymentFacts, *, bound: date) -> date | None:
    """The civil date on which the payment first became commercially paid.

    The first of ``confirmedDate``, ``clientPaymentDate``, ``paymentDate``
    (each already a strict ``YYYY-MM-DD``) that is not later than ``bound``,
    the São Paulo day of the observation.  ``clientPaymentDate`` is the
    capture day on the sandbox evidence, while ``paymentDate`` is ASAAS's
    "data de liquidação" (the card funds day, ~D+32), so it comes last.  A
    later candidate is skipped, never clamped.  ``None`` means no settlement
    (fail closed): ``paid_at`` is never inferred from an event, payment or
    readback timestamp.
    """

    for candidate in (
        facts.confirmed_date,
        facts.client_payment_date,
        facts.payment_date,
    ):
        if candidate is not None and candidate <= bound:
            return candidate
    return None


def _readback_evidence_usable(
    readback: PaymentReadback, normalized: str, amount_cents: int
) -> bool:
    """An exact read whose status, refund list and amounts can be trusted."""

    refunded = readback.refunded_cents
    return not (
        normalized == "unknown"
        or readback.status not in _READBACK_PAYMENT_STATUSES
        or refunded is None
        or refunded > amount_cents
        # A REFUNDED label without any exact DONE refund value is
        # contradictory provider evidence; a date alone proves nothing.
        or (readback.status == "REFUNDED" and refunded == 0)
    )


# ---------------------------------------------------------------------------
# Correlation targets.
# ---------------------------------------------------------------------------


@dataclass
class _PaymentTarget:
    builder_id: UUID
    aggregate_kind: str
    subscription_id: UUID | None
    topup_order_id: UUID | None
    session_id: UUID | None
    amount_cents: int


@dataclass
class _Handoff:
    session_id: UUID
    builder_id: UUID
    external_subscription_id: str


@dataclass
class _Outcome:
    """Result of one delivery transaction (metrics are emitted after commit)."""

    family: str
    outcome: str
    delivery_id: UUID | None = None
    readback: bool = False
    alerts: list[str] = field(default_factory=list)


class _Uncorrelated(Exception):
    """Known event without an exactly correlatable entity: block and alert."""


# ---------------------------------------------------------------------------
# Financial state machine (caller-owned transaction, locks already ordered).
# ---------------------------------------------------------------------------


class _FinancialState:
    """Shared projection/effect transitions for events and readbacks."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        environment: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._db = db
        self._repo = BillingRepository(db)
        self._environment = environment
        self._clock = clock
        self.alerts: list[str] = []

    # -- lookups (lock-free; the rollout lock already serializes writers) --

    async def session_by_external(
        self, checkout_id: str
    ) -> BillingCheckoutSession | None:
        result = await self._db.execute(
            select(BillingCheckoutSession)
            .where(
                BillingCheckoutSession.provider == PROVIDER,
                BillingCheckoutSession.external_checkout_id == checkout_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def session_by_id(self, session_id: UUID) -> BillingCheckoutSession | None:
        result = await self._db.execute(
            select(BillingCheckoutSession)
            .where(BillingCheckoutSession.id == session_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def originating_session(
        self, subscription: BillingSubscription
    ) -> BillingCheckoutSession | None:
        """The checkout that created this subscription (its intent is the row)."""

        result = await self._db.execute(
            select(BillingCheckoutSession)
            .where(
                BillingCheckoutSession.builder_id == subscription.builder_id,
                BillingCheckoutSession.intent_kind == "subscription",
                BillingCheckoutSession.intent_id == subscription.id,
                BillingCheckoutSession.resulting_external_subscription_id
                == subscription.external_subscription_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def subscription_by_external(
        self, external_subscription_id: str
    ) -> BillingSubscription | None:
        result = await self._db.execute(
            select(BillingSubscription)
            .where(
                BillingSubscription.provider == PROVIDER,
                BillingSubscription.external_subscription_id
                == external_subscription_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def projection(self, payment_id: str) -> BillingPaymentProjection | None:
        result = await self._db.execute(
            select(BillingPaymentProjection)
            .where(
                BillingPaymentProjection.provider == PROVIDER,
                BillingPaymentProjection.payment_id == payment_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    # -- correlation --

    async def correlate_payment(self, facts: PaymentFacts) -> _PaymentTarget | _Handoff:
        """Resolve one payment to exactly one aggregate or raise ``_Uncorrelated``.

        Every identifier that is present must agree; a conflict is never
        resolved by preference.  ``externalReference`` counts only when it is
        exactly one of our checkout-session UUIDs.
        """

        if facts.payment_id is None:
            raise _Uncorrelated()
        by_checkout = (
            await self.session_by_external(facts.checkout_id)
            if facts.checkout_id is not None
            else None
        )
        by_reference = (
            await self.session_by_id(facts.external_reference)
            if facts.external_reference is not None
            else None
        )
        if by_checkout is not None and by_reference is not None:
            if by_checkout.id != by_reference.id:
                raise _Uncorrelated()
        session = by_checkout or by_reference
        subscription = (
            await self.subscription_by_external(facts.subscription_id)
            if facts.subscription_id is not None
            else None
        )
        existing = await self.projection(facts.payment_id)
        if existing is not None:
            if subscription is not None and existing.subscription_id != subscription.id:
                raise _Uncorrelated()
            if session is not None and (
                session.builder_id != existing.builder_id
                or (
                    existing.checkout_session_id is not None
                    and existing.checkout_session_id != session.id
                )
            ):
                raise _Uncorrelated()
            return _PaymentTarget(
                builder_id=existing.builder_id,
                aggregate_kind=existing.aggregate_kind,
                subscription_id=existing.subscription_id,
                topup_order_id=existing.topup_order_id,
                session_id=existing.checkout_session_id,
                amount_cents=existing.amount_cents,
            )
        if subscription is not None:
            origin = await self.originating_session(subscription)
            if session is not None and (origin is None or origin.id != session.id):
                raise _Uncorrelated()
            if subscription.amount_cents is None or subscription.amount_cents <= 0:
                raise _Uncorrelated()
            return _PaymentTarget(
                builder_id=subscription.builder_id,
                aggregate_kind="subscription",
                subscription_id=subscription.id,
                topup_order_id=None,
                session_id=origin.id if origin is not None else None,
                amount_cents=int(subscription.amount_cents),
            )
        if session is None:
            raise _Uncorrelated()
        if session.intent_kind == "topup" and session.topup_order_id is not None:
            if facts.subscription_id is not None:
                raise _Uncorrelated()
            return _PaymentTarget(
                builder_id=session.builder_id,
                aggregate_kind="topup",
                subscription_id=None,
                topup_order_id=session.topup_order_id,
                session_id=session.id,
                amount_cents=int(session.amount_cents),
            )
        if (
            session.intent_kind == "subscription"
            and facts.subscription_id is not None
            and session.resulting_external_subscription_id is None
            and session.state in _LIVE_CHECKOUT_STATES
        ):
            # The payment itself pairs our exact checkout ID with the provider
            # subscription ID: an exact, provider-asserted link.
            return _Handoff(
                session_id=session.id,
                builder_id=session.builder_id,
                external_subscription_id=facts.subscription_id,
            )
        raise _Uncorrelated()

    # -- session -> subscription handoff (M-A: rollout lock already held) --

    async def handoff(
        self,
        session_id: UUID,
        builder_id: UUID,
        external_subscription_id: str,
        *,
        facts: SubscriptionFacts | None,
    ) -> BillingSubscription:
        """Turn one live subscription checkout into its canonical row.

        Must run under ``lock_rollout_control``: Group 2 eligibility reads
        subscriptions, migrations and live sessions as three statements under
        the same lock, so this commit can never land between them.
        """

        # Lock order: rollout (caller) -> roster fence -> session row.
        roster_revision = await self._repo.lock_billing_roster()
        session = await self._repo.lock_checkout_session(
            builder_id=builder_id, checkout_session_id=session_id
        )
        if session is None or session.intent_kind != "subscription":
            raise _Uncorrelated()
        existing = await self.subscription_by_external(external_subscription_id)
        if existing is not None:
            if existing.builder_id != builder_id or existing.id != session.intent_id:
                raise _Uncorrelated()
            return existing
        plan = OFFER_PLANS.get(session.offer_id)
        if (
            session.migration_id is not None  # adoption belongs to Group 5
            or session.state not in _LIVE_CHECKOUT_STATES
            or session.resulting_external_subscription_id
            not in (
                None,
                external_subscription_id,
            )
            or plan is None
            or session.next_due_date is None
        ):
            raise _Uncorrelated()
        other = await self._db.scalar(
            select(BillingSubscription.id)
            .where(
                BillingSubscription.builder_id == builder_id,
                BillingSubscription.id != session.intent_id,
                or_(
                    BillingSubscription.provider.is_(None),
                    BillingSubscription.is_active.is_(True),
                    BillingSubscription.status.not_in(_TERMINAL_SUBSCRIPTION_STATUSES),
                ),
            )
            .limit(1)
        )
        if other is not None:
            # A second recurring subscription must never be bound silently.
            self.alerts.append("drift")
            raise _Uncorrelated()
        status = "active_linked"
        if facts is not None and not self._subscription_matches(
            facts.cycle,
            facts.value_cents,
            facts.next_due_date,
            facts.status,
            facts.deleted,
            amount_cents=int(session.amount_cents),
            first_due_date=session.next_due_date,
            exact_due_date=True,
        ):
            status = "readback_required"
            self.alerts.append("drift")
        snapshot_coupon = (session.pricing_snapshot or {}).get("coupon_code")
        subscription = BillingSubscription(
            id=session.intent_id,
            builder_id=builder_id,
            provider=PROVIDER,
            external_subscription_id=external_subscription_id,
            is_active=True,
            plan=plan,
            billing_cycle="monthly",
            coupon_code=snapshot_coupon if isinstance(snapshot_coupon, str) else None,
            catalog_version_id=session.catalog_version_id,
            offer_id=session.offer_id,
            amount_cents=int(session.amount_cents),
            status=status,
            # billing_roster_revision is saga-owned (DB trigger); a new ASAAS
            # row records the fence it was created under (>0 by CHECK).
            roster_revision=max(roster_revision, 1),
        )
        self._db.add(subscription)
        await self._db.flush()
        session.resulting_external_subscription_id = external_subscription_id
        session.state = "completed"
        await self._db.flush()
        return subscription

    @staticmethod
    def _subscription_matches(
        cycle: str | None,
        value_cents: int | None,
        next_due_date: date | None,
        status: str | None,
        deleted: bool,
        *,
        amount_cents: int,
        first_due_date: date | None,
        exact_due_date: bool,
    ) -> bool:
        if deleted or cycle != "MONTHLY" or value_cents != amount_cents:
            return False
        if status not in (None, "ACTIVE"):
            return False
        if first_due_date is not None and next_due_date is not None:
            if exact_due_date and next_due_date != first_due_date:
                return False
            if next_due_date < first_due_date:
                return False
        elif first_due_date is not None:
            return False
        return True

    # -- payment projection --

    async def lock_target_rows(self, target: _PaymentTarget) -> tuple[
        BillingSubscription | None,
        BillingTopupOrder | None,
        BillingCheckoutSession | None,
    ]:
        if target.aggregate_kind == "subscription":
            await self._repo.lock_billing_roster()
        session = None
        if target.session_id is not None:
            session = await self._repo.lock_checkout_session(
                builder_id=target.builder_id, checkout_session_id=target.session_id
            )
            if session is None:
                raise _Uncorrelated()
        subscription = order = None
        if target.subscription_id is not None:
            subscription = await self._repo.lock_subscription(
                builder_id=target.builder_id, subscription_id=target.subscription_id
            )
            if subscription is None:
                raise _Uncorrelated()
        if target.topup_order_id is not None:
            order = await self._repo.lock_topup_order(
                builder_id=target.builder_id, order_id=target.topup_order_id
            )
            if order is None:
                raise _Uncorrelated()
        return subscription, order, session

    async def ensure_projection(
        self,
        target: _PaymentTarget,
        *,
        payment_id: str,
        session: BillingCheckoutSession | None,
        status: str,
        provider_created_at: datetime,
        key: str,
        event_id: str,
    ) -> BillingPaymentProjection:
        await self._repo.insert_payment_projection_if_absent(
            {
                "id": uuid4(),
                "builder_id": target.builder_id,
                "provider": PROVIDER,
                "payment_id": payment_id,
                "aggregate_kind": target.aggregate_kind,
                "subscription_id": target.subscription_id,
                "topup_order_id": target.topup_order_id,
                "checkout_session_id": target.session_id,
                "canary_environment": session.canary_environment if session else None,
                "canary_slot": session.canary_slot if session else None,
                "status": status,
                "amount_cents": target.amount_cents,
                "settled_cents": 0,
                "reversed_cents": 0,
                "restored_cents": 0,
                "restoration_epoch": 0,
                "provider_created_at": provider_created_at,
                "ordering_key": key,
                "last_event_id": event_id,
            }
        )
        projection = await self._repo.lock_payment_projection(
            provider=PROVIDER, payment_id=payment_id
        )
        if projection is None or projection.builder_id != target.builder_id:
            raise _Uncorrelated()
        return projection

    @staticmethod
    def _is_canary_subscription(
        target: _PaymentTarget, session: BillingCheckoutSession | None
    ) -> bool:
        return bool(
            target.aggregate_kind == "subscription"
            and session is not None
            and session.canary_slot == "subscription"
        )

    async def apply_payment(
        self,
        target: _PaymentTarget,
        *,
        payment_id: str,
        normalized: str,
        provider_created_at: datetime,
        key: str,
        event_id: str,
        event_name: str,
        delivery: BillingWebhookDelivery,
        facts: PaymentFacts,
        readback: PaymentReadback | None,
    ) -> str:
        """Apply one event (or readback) to the payment; return delivery status."""

        if normalized == "unknown":
            # A provider status outside the closed mapping proves nothing.
            self.alerts.append("drift")
            return "readback_required"
        subscription, order, session = await self.lock_target_rows(target)
        canary = self._is_canary_subscription(target, session)
        existing = await self._repo.lock_payment_projection(
            provider=PROVIDER, payment_id=payment_id
        )
        if normalized in _MONEY_SETTLED_STATUSES and (
            facts.value_cents is None or facts.value_cents != target.amount_cents
        ):
            # Every money transition needs the provider to report exactly the
            # charge the snapshot sold.  A divergent, missing or non-exact
            # ``value`` blocks it (fail closed); an exact readback that does
            # carry the right amount is the only way forward.
            # The projection keeps its status: "needs readback" is recorded on
            # the delivery (``readback_required``) and by the alert, never by
            # relabelling the row (that would erase a reversed/deleted truth).
            self.alerts.append("drift")
            return "readback_required"
        if existing is None:
            if normalized in _NON_FINANCIAL_STATUSES:
                if canary and (subscription is None or subscription.status != "active"):
                    # The DB pins canary projections to an ``active`` Plus row;
                    # nothing financial happened yet, so there is nothing to
                    # project until settlement activates the subscription.
                    return "processed"
                await self.ensure_projection(
                    target,
                    payment_id=payment_id,
                    session=session,
                    status=normalized,
                    provider_created_at=provider_created_at,
                    key=key,
                    event_id=event_id,
                )
                return "processed"
            if normalized != "settled" and readback is None:
                # Reversal/chargeback/deletion of a payment we never saw
                # settle: only an exact readback can establish the history.
                if canary and (subscription is None or subscription.status != "active"):
                    return "readback_required"
                await self.ensure_projection(
                    target,
                    payment_id=payment_id,
                    session=session,
                    status="readback_required",
                    provider_created_at=provider_created_at,
                    key=key,
                    event_id=event_id,
                )
                return "readback_required"
        paid_at: date | None = None
        if normalized == "settled" or readback is not None:
            # Events: the envelope time; readbacks: the read clock.  Both are
            # superseded by the earliest capture delivery of this payment.
            paid_at = resolve_paid_at(
                facts,
                bound=await self._paid_at_bound(
                    builder_id=target.builder_id,
                    payment_id=payment_id,
                    observed_at=provider_created_at,
                ),
            )
        pre_activated = False
        if (
            existing is None
            and canary
            and (subscription is None or subscription.status != "active")
        ):
            # The DB pins every canary projection write to an ``active`` Plus
            # row, and activation belongs to the settling branch only (Q-C):
            # decide whether this first write settles before writing at all.
            if not self._first_write_settles(
                normalized=normalized,
                readback=readback,
                amount_cents=target.amount_cents,
                paid_at=paid_at,
            ):
                if readback is not None:
                    self.alerts.append("drift")
                return "readback_required"
            if readback is None and await self._has_open_reversal_delivery(
                builder_id=target.builder_id, payment_id=payment_id
            ):
                # M-2: a refund/chargeback event of this payment is still
                # unresolved, and the canary refund marker cannot be stored
                # (the DB refuses the row until the Plus row is active).  A
                # capture event must not grant and spend the slot on stale
                # evidence; only an exact read may settle it.
                return "readback_required"
            await self._activate_subscription_before_projection(subscription)
            pre_activated = True
        projection = existing or await self.ensure_projection(
            target,
            payment_id=payment_id,
            session=session,
            status="pending",
            provider_created_at=provider_created_at,
            key=key,
            event_id=event_id,
        )
        if readback is not None:
            result = await self._apply_readback(
                projection,
                target=target,
                readback=readback,
                normalized=normalized,
                key=key,
                delivery=delivery,
                event_name=event_name,
                subscription=subscription,
                order=order,
                session=session,
                facts=facts,
                observed_at=provider_created_at,
                paid_at=paid_at,
            )
        else:
            result = await self._apply_event(
                projection,
                target=target,
                normalized=normalized,
                key=key,
                event_id=event_id,
                event_name=event_name,
                delivery=delivery,
                subscription=subscription,
                order=order,
                session=session,
                facts=facts,
                paid_at=paid_at,
            )
        if pre_activated and projection.settled_cents <= 0:
            # Activation without the settlement it was decided for is never
            # committed: refuse the whole transaction (quarantine/retry).
            raise BillingTransitionRejected("canary activation without settlement")
        return result

    async def _paid_at_bound(
        self, *, builder_id: UUID, payment_id: str, observed_at: datetime
    ) -> date:
        """São Paulo day that no ``paid_at`` candidate may exceed (+0 days).

        Anchored on the envelope ``dateCreated`` of the earliest
        ``PAYMENT_CONFIRMED`` delivery bound to this payment (any status), so
        a later receipt or read can never admit the card funds day; without
        one, the observation itself (envelope time, or the read clock).  The
        payment's own ``dateCreated`` is never read.
        """

        anchor = await self._db.scalar(
            select(func.min(BillingWebhookDelivery.provider_created_at)).where(
                BillingWebhookDelivery.builder_id == builder_id,
                BillingWebhookDelivery.provider == PROVIDER,
                BillingWebhookDelivery.resource_kind == "payment",
                BillingWebhookDelivery.resource_id == payment_id,
                BillingWebhookDelivery.event_name == _CAPTURE_EVENT,
            )
        )
        return _sao_paulo_day(anchor if anchor is not None else observed_at)

    async def _has_open_reversal_delivery(
        self, *, builder_id: UUID, payment_id: str
    ) -> bool:
        """Whether a refund/chargeback delivery of this payment is unresolved."""

        found = await self._db.scalar(
            select(BillingWebhookDelivery.id)
            .where(
                BillingWebhookDelivery.builder_id == builder_id,
                BillingWebhookDelivery.provider == PROVIDER,
                BillingWebhookDelivery.resource_kind == "payment",
                BillingWebhookDelivery.resource_id == payment_id,
                BillingWebhookDelivery.event_name.in_(sorted(_REVERSAL_CLASS_EVENTS)),
                BillingWebhookDelivery.status != "processed",
            )
            .limit(1)
        )
        return found is not None

    @staticmethod
    def _first_write_settles(
        *,
        normalized: str,
        readback: PaymentReadback | None,
        amount_cents: int,
        paid_at: date | None,
    ) -> bool:
        """Whether applying this evidence to a brand-new projection settles.

        Mirrors ``_apply_event``/``_apply_readback`` on a fresh ``pending``
        row (no money, no marker, not deleted, never stale-vs-failed).
        """

        if paid_at is None:
            return False
        if readback is None:
            return normalized == "settled"
        if not _readback_evidence_usable(readback, normalized, amount_cents):
            return False
        refunded = int(readback.refunded_cents or 0)
        if refunded > 0 and normalized != "settled":
            return False  # the pre-settlement refund marker, not a settlement
        return normalized in _MONEY_SETTLED_STATUSES or refunded > 0

    async def _activate_subscription_before_projection(
        self, subscription: BillingSubscription | None
    ) -> None:
        # ``active_linked`` -> ``active`` only on the branch that settles
        # (owner decision Q-C), before the settled projection is written (the
        # Group 1 canary trigger requires an ``active`` Plus row for every
        # canary projection write).
        if subscription is not None and subscription.status == "active_linked":
            subscription.status = "active"
            await self._db.flush()

    async def _apply_event(
        self,
        projection: BillingPaymentProjection,
        *,
        target: _PaymentTarget,
        normalized: str,
        key: str,
        event_id: str,
        event_name: str,
        delivery: BillingWebhookDelivery,
        subscription: BillingSubscription | None,
        order: BillingTopupOrder | None,
        session: BillingCheckoutSession | None,
        facts: PaymentFacts,
        paid_at: date | None,
    ) -> str:
        stale = key <= projection.ordering_key
        if projection.status == REFUNDED_BEFORE_SETTLEMENT:
            # An exact provider readback already established a completed
            # refund before any accepted receipt.  A delayed receipt event is
            # contradictory evidence and must never create a settlement.
            self.alerts.append("drift")
            return "readback_required"
        if projection.settled_cents > 0 and projection.paid_at is None:
            # 035 cannot backfill historical settlement dates. Require an
            # exact provider read before a later event can emit a fact.
            return "readback_required"
        if normalized == "settled":
            if projection.settled_cents > 0:
                if not stale and projection.status in _SETTLE_EVIDENCE_NEEDS_READ:
                    # H-1/L-3: a won dispute, or capture evidence after a
                    # refund: only an exact read may relabel the row.
                    return "readback_required"
                if paid_at != projection.paid_at:
                    # ``paid_at`` is write-once: the funds receipt (or any
                    # later capture evidence) never moves it.
                    logger.info("billing settlement evidence date ignored")
                # The second settle-class event: same settlement:v1, inert.
                return "processed"
            if stale and projection.status in ("failed", "readback_required"):
                # M2/L-2: an older capture never overrides a newer refusal,
                # nor the newer refund/chargeback/deletion/restoration event
                # that projected this row (the only way a row is created
                # ``readback_required``); only the provider's current word
                # (a read) may settle it.
                return "readback_required"
            if paid_at is None:
                return "readback_required"
            if projection.status not in (*_NON_FINANCIAL_STATUSES, "readback_required"):
                return "readback_required"
            await self._settle(
                projection,
                target=target,
                key=key,
                event_id=event_id,
                event_name=event_name,
                delivery=delivery,
                subscription=subscription,
                order=order,
                session=session,
                billing_type=facts.billing_type,
                cycle_start=facts.due_date,
                paid_at=paid_at,
            )
            return "processed"
        # Monetary transitions are monotone in their cumulative key, so their
        # provider time cannot regress anything: they are never order-gated.
        if normalized == "reversed":
            if projection.settled_cents <= 0:
                # Reversal of money we never saw settle: only a readback can
                # establish the history.  The row keeps its status.
                return "readback_required"
            await self._reverse(
                projection,
                cumulative=projection.settled_cents,
                key=key,
                event_id=event_id,
                delivery=delivery,
                order=order,
                status="reversed",
            )
            return "processed"
        if normalized == "partially_reversed" and projection.settled_cents > 0:
            # The cumulative refunded amount is not priced by the event.
            return "readback_required"
        if stale:
            return "processed"  # an old event never regresses the projection
        if normalized in _UNPAID_STATUSES and projection.settled_cents > 0:
            # H-2: a newer refusal/overdue on settled money contradicts the
            # settlement (e.g. a capture later reproved).  Money never
            # un-settles here; an exact read (or an operator) must decide.
            self.alerts.append("drift")
            return "readback_required"
        if normalized in _NON_FINANCIAL_STATUSES:
            if event_name == "PAYMENT_UPDATED" and projection.status != "pending":
                return "readback_required"
            if projection.status in _NON_FINANCIAL_STATUSES:
                self._touch(projection, status=normalized, key=key, event_id=event_id)
                await self._db.flush()
                return "processed"
            return (
                "readback_required" if event_name == "PAYMENT_UPDATED" else "processed"
            )
        if normalized in (
            "reversal_pending",
            "chargeback_requested",
            "chargeback_dispute",
            "restoration_pending",
            "partially_reversed",
        ):
            if projection.settled_cents <= 0:
                return "readback_required"  # status kept; delivery records it
            if normalized == "partially_reversed":
                # The cumulative refunded amount is not priced by the event.
                return "readback_required"
            self._touch(projection, status=normalized, key=key, event_id=event_id)
            await self._db.flush()
            return "processed"
        if normalized == "deleted":
            if projection.settled_cents > 0:
                return "readback_required"
            self._touch(projection, status="deleted", key=key, event_id=event_id)
            await self._db.flush()
            return "processed"
        # PAYMENT_RESTORED: only a readback may move the projection back live.
        return "readback_required"

    @staticmethod
    def _touch(
        projection: BillingPaymentProjection, *, status: str, key: str, event_id: str
    ) -> None:
        projection.status = status
        if key > projection.ordering_key:
            projection.ordering_key = key
            projection.last_event_id = event_id

    async def _apply_readback(
        self,
        projection: BillingPaymentProjection,
        *,
        target: _PaymentTarget,
        readback: PaymentReadback,
        normalized: str,
        key: str,
        delivery: BillingWebhookDelivery,
        event_name: str,
        subscription: BillingSubscription | None,
        order: BillingTopupOrder | None,
        session: BillingCheckoutSession | None,
        facts: PaymentFacts,
        observed_at: datetime,
        paid_at: date | None,
    ) -> str:
        """Converge the projection to one exact provider read."""

        if not _readback_evidence_usable(readback, normalized, projection.amount_cents):
            self.alerts.append("drift")
            return "readback_required"
        target_reversed = int(readback.refunded_cents or 0)
        if projection.status == REFUNDED_BEFORE_SETTLEMENT:
            # The marker is terminal for automatic financial projection.  A
            # provider read that continues to show a completed refund is
            # consistent; any receipt or loss of the refund is quarantined.
            if projection.settled_cents == 0 and normalized != "settled":
                evidence_matches = (
                    projection.pre_settlement_refund_cents == target_reversed
                    and projection.pre_settlement_refund_status == readback.status
                    and projection.pre_settlement_refund_observed_at is not None
                )
                if evidence_matches and target_reversed > 0:
                    return "processed"
            self.alerts.append("drift")
            return "readback_required"
        if (
            projection.settled_cents == 0
            and target_reversed > 0
            and normalized != "settled"
        ):
            # A completed refund read while nothing is settled here and the
            # provider status is not itself a receipt (CONFIRMED/RECEIVED
            # settle, then reverse: owner decision Q-A).  Preserve that
            # history as a terminal marker; never manufacture a grant.
            self._touch(
                projection,
                status=REFUNDED_BEFORE_SETTLEMENT,
                key=key,
                event_id=projection.last_event_id,
            )
            projection.pre_settlement_refund_cents = target_reversed
            projection.pre_settlement_refund_status = readback.status
            projection.pre_settlement_refund_observed_at = observed_at
            self.alerts.append("drift")
            await self._db.flush()
            return "processed"
        money_settled = normalized in _MONEY_SETTLED_STATUSES or target_reversed > 0
        if projection.settled_cents > 0:
            if projection.paid_at is None:
                # Legacy (pre-035) settlement: repair from the same chain.
                if paid_at is None:
                    self.alerts.append("drift")
                    return "readback_required"
                projection.paid_at = paid_at
            # Otherwise ``paid_at`` is write-once and never compared: a later
            # read (CONFIRMED, RECEIVED with the funds day, a refund) keeps it.
        elif money_settled and paid_at is None:
            self.alerts.append("drift")
            return "readback_required"
        net_reversed = projection.reversed_cents - projection.restored_cents
        event_id = projection.last_event_id
        # 1. restoration: reversed/deleted -> live, proven by this read.  A
        # reversed state is decided by the persisted cumulative cents
        # (``net_reversed > 0``), never by the status label, which status-only
        # events may legitimately change; money must actually come back
        # (``target < net``), so re-reading an unchanged partial refund is not
        # a restoration.  A deleted payment (never settled) is the only
        # label-based case, and nothing relabels a deleted row; it is restored
        # (with zero money) only when the read shows no completed refund — a
        # read with refunds is settled and reversed by steps 2-3 instead.
        restored_now = False
        reversed_money = net_reversed > 0 and target_reversed < net_reversed
        deleted_row = (
            projection.status == "deleted"
            and projection.settled_cents == 0
            and target_reversed == 0
        )
        if normalized in _LIVE_PROVIDER_STATUSES and (reversed_money or deleted_row):
            if projection.settled_cents > 0 and not money_settled:
                self.alerts.append("drift")  # settled money cannot be pending
                return "readback_required"
            await self._restore(
                projection,
                delta=net_reversed - target_reversed,
                key=key,
                delivery=delivery,
                order=order,
                status=normalized,
            )
            restored_now = True
            net_reversed = target_reversed
        # 2. settlement proven by the read (also after a restoration).
        if money_settled and projection.settled_cents == 0:
            await self._settle(
                projection,
                target=target,
                key=key,
                event_id=event_id,
                event_name=event_name,
                delivery=delivery,
                subscription=subscription,
                order=order,
                session=session,
                billing_type=facts.billing_type,
                cycle_start=facts.due_date,
                paid_at=paid_at,
            )
        elif not money_settled and projection.settled_cents > 0:
            self.alerts.append("drift")  # money can never un-settle
            return "readback_required"
        # 3. cumulative reversal proven by the read.
        if target_reversed > net_reversed:
            if projection.restored_cents > 0:
                # A second reversal after a restoration would exceed the
                # schema's restored<=reversed<=settled lattice: manual.
                self.alerts.append("drift")
                return "readback_required"
            await self._reverse(
                projection,
                cumulative=target_reversed,
                key=key,
                event_id=event_id,
                delivery=delivery,
                order=order,
                status=(
                    "reversed"
                    if target_reversed == projection.settled_cents
                    else "partially_reversed"
                ),
            )
            return "processed"
        if normalized == "deleted" and projection.settled_cents > 0:
            self.alerts.append("drift")
            return "readback_required"
        status = normalized
        # The label of a live settled payment follows its cumulative cents, so
        # replaying the same read never flips a row with reversed or restored
        # money; a deleted row restored with zero money is labelled
        # ``restored`` only by the read that proved it (``settled`` after).
        if normalized == "settled":
            if projection.reversed_cents > projection.restored_cents:
                status = "partially_reversed"
            elif projection.restored_cents > 0 or restored_now:
                status = "restored"
        if projection.status != status:
            self._touch(projection, status=status, key=key, event_id=event_id)
            await self._db.flush()
        return "processed"

    # -- effects --

    async def _effect(
        self,
        projection: BillingPaymentProjection,
        *,
        delivery: BillingWebhookDelivery,
        kind: str,
        revision: str,
        amount_cents: int,
        canary: bool,
    ) -> BillingPaymentEffect | None:
        found = await self._repo.find_payment_effect(
            provider=PROVIDER,
            payment_id=projection.payment_id,
            effect_kind=kind,
            source_revision=revision,
        )
        if found is not None:
            return None
        effect = BillingPaymentEffect(
            id=uuid4(),
            builder_id=projection.builder_id,
            projection_id=projection.id,
            delivery_id=delivery.id,
            provider=PROVIDER,
            payment_id=projection.payment_id,
            effect_kind=kind,
            source_revision=revision,
            amount_cents=amount_cents,
            canary_environment=projection.canary_environment if canary else None,
            canary_slot=projection.canary_slot if canary else None,
        )
        self._db.add(effect)
        await self._db.flush()
        return effect

    async def _settle(
        self,
        projection: BillingPaymentProjection,
        *,
        target: _PaymentTarget,
        key: str,
        event_id: str,
        event_name: str,
        delivery: BillingWebhookDelivery,
        subscription: BillingSubscription | None,
        order: BillingTopupOrder | None,
        session: BillingCheckoutSession | None,
        billing_type: str | None,
        cycle_start: date | None,
        paid_at: date | None,
    ) -> None:
        if paid_at is None:
            raise RuntimeError("ASAAS settlement is missing paid_at")
        if projection.paid_at is not None and projection.paid_at != paid_at:
            raise RuntimeError("ASAAS paid_at changed after settlement")
        # Q-C: the only place a subscription is activated by a payment.
        await self._activate_subscription_before_projection(subscription)
        projection.paid_at = paid_at
        amount = projection.amount_cents
        self._touch(projection, status="settled", key=key, event_id=event_id)
        projection.settled_cents = amount
        await self._db.flush()
        canary = projection.canary_environment is not None
        effect = await self._effect(
            projection,
            delivery=delivery,
            kind="settlement",
            revision=SETTLEMENT_REVISION,
            amount_cents=amount,
            canary=canary,
        )
        if effect is None:
            return
        if canary:
            await self._repo.reserve_canary_settlement(
                environment=str(projection.canary_environment),
                payment_effect_id=effect.id,
            )
        await self._commercial_settlement(
            effect,
            projection=projection,
            event_name=event_name,
            subscription=subscription,
            order=order,
            session=session,
            billing_type=billing_type,
        )
        await self._financial_fact_hook(effect, projection, cycle_start=cycle_start)

    async def _commercial_settlement(
        self,
        effect: BillingPaymentEffect,
        *,
        projection: BillingPaymentProjection,
        event_name: str,
        subscription: BillingSubscription | None,
        order: BillingTopupOrder | None,
        session: BillingCheckoutSession | None,
        billing_type: str | None,
    ) -> None:
        """Mirror the legacy paid-event commercial effect with ASAAS identity.

        Legacy effect: activate the builder plan, consume the coupon, log a
        ``Transaction``.  Here every value comes from persisted rows: the
        Group 1 four-tuple links the Transaction to this effect, amounts are
        the snapshot's, and the coupon is consumed once per checkout session by
        ``consume_checkout_coupon``.  A top-up only settles its order; no
        credit is granted (Group 4 / credit system).
        """

        builder = (
            await self._db.execute(
                select(Builder)
                .where(Builder.id == effect.builder_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if builder is None:
            # FK-guaranteed; reaching this is corruption, so roll everything
            # back (provider retries) instead of committing a partial effect.
            raise RuntimeError("billing builder row is unavailable")
        snapshot_method = None
        if session is not None:
            snapshot_method = (session.pricing_snapshot or {}).get("payment_method")
        method = billing_type or (
            snapshot_method if isinstance(snapshot_method, str) else "unknown"
        )
        if subscription is not None:
            plan, cycle = subscription.plan, "monthly"
        else:
            plan, cycle = "topup", "detached"
        transaction = Transaction(
            id=uuid4(),
            builder_id=effect.builder_id,
            builder_name=builder.name,
            amount_cents=effect.amount_cents,
            method=method,
            cycle=cycle,
            plan=plan,
            coupon_code=None,
            external_id=effect.payment_id[:100],
            provider=PROVIDER,
            provider_payment_id=effect.payment_id,
            billing_payment_effect_id=effect.id,
            event_type=event_name[:50],
        )
        self._db.add(transaction)
        await self._db.flush()
        if session is not None:
            # Group 2 hand-off: same transaction, after the flush, settlement
            # only; the session row lock serializes concurrent settlements.
            await self._repo.consume_checkout_coupon(
                builder_id=effect.builder_id, transaction_id=transaction.id
            )
        if subscription is not None:
            if subscription.status in _LIVE_SUBSCRIPTION_STATUSES:
                builder.subscription_plan = subscription.plan
                builder.subscription_status = "active"
                builder.billing_cycle = "monthly"
        elif order is not None:
            order.settled_cents = projection.settled_cents
            order.state = "settled" if order.state == "pending" else order.state
        await self._db.flush()

    async def _reverse(
        self,
        projection: BillingPaymentProjection,
        *,
        cumulative: int,
        key: str,
        event_id: str,
        delivery: BillingWebhookDelivery,
        order: BillingTopupOrder | None,
        status: str,
    ) -> None:
        if (
            cumulative <= projection.reversed_cents
            or cumulative > projection.settled_cents
        ):
            return
        delta = cumulative - projection.reversed_cents
        self._touch(projection, status=status, key=key, event_id=event_id)
        projection.reversed_cents = cumulative
        await self._db.flush()
        effect = await self._effect(
            projection,
            delivery=delivery,
            kind="reversal",
            revision=f"reversed_cents:{cumulative}",
            amount_cents=delta,
            canary=False,
        )
        if order is not None:
            order.reversed_cents = projection.reversed_cents
            order.state = (
                status if status in ("reversed", "partially_reversed") else order.state
            )
            await self._db.flush()
        # Decision 10: financial sync only; no new access blocking on reversal.
        if effect is not None:
            await self._financial_fact_hook(effect, projection)

    async def _restore(
        self,
        projection: BillingPaymentProjection,
        *,
        delta: int,
        key: str,
        delivery: BillingWebhookDelivery,
        order: BillingTopupOrder | None,
        status: str,
    ) -> None:
        epoch = projection.restoration_epoch + 1
        projection.restoration_epoch = epoch
        projection.restored_cents += delta
        self._touch(
            projection, status=status, key=key, event_id=projection.last_event_id
        )
        await self._db.flush()
        effect = await self._effect(
            projection,
            delivery=delivery,
            kind="restoration",
            revision=f"restoration_epoch:{epoch}",
            amount_cents=delta,
            canary=False,
        )
        if order is not None and delta > 0:
            order.restored_cents = projection.restored_cents
            order.state = "restored"
            await self._db.flush()
        if effect is not None:
            await self._financial_fact_hook(effect, projection)

    async def _financial_fact_hook(
        self,
        effect: BillingPaymentEffect,
        projection: BillingPaymentProjection,
        *,
        cycle_start: date | None = None,
    ) -> None:
        """Atomically append the Group 4 fact after a newly persisted effect."""

        await BillingFactProducer(self._db, now=self._clock).record_effect(
            effect, projection, cycle_start=cycle_start
        )


# ---------------------------------------------------------------------------
# Receiver.
# ---------------------------------------------------------------------------


class BillingWebhookService:
    """Persist one authenticated delivery and apply its deterministic transition."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        environment: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._db = db
        self._environment = environment
        self._clock = clock
        self._repo = BillingRepository(db)
        self._resource: tuple[UUID | None, str | None, str | None] = (None, None, None)

    async def receive(self, envelope: WebhookEnvelope) -> _Outcome:
        """Commit the delivery (and any effect) or raise for a provider retry.

        Failure policy (``classify_failure``): a *transient* failure rolls
        back and raises ``WebhookProcessingUnavailable`` (HTTP 503, the
        provider retries).  A *deterministic* rejection (DB invariant, domain
        transition refusal, permanently missing control row) would fail the
        same way on every retry and, with sequential delivery, interrupt the
        provider queue after 15 attempts: it rolls back, then persists only the
        minimal envelope as ``readback_required`` (or ``processed`` when the
        effect provably exists already), alerts with a bounded class label and
        returns 200.  A rolled-back transaction never leaves a delivery marked
        processed without its effect.
        """

        family = envelope.family or "unknown"
        self._resource = (None, None, None)
        try:
            outcome = await self._receive(envelope)
            await self._db.commit()
        except WebhookProcessingUnavailable:
            await self._rollback()
            raise
        except Exception as exc:
            await self._rollback()
            label, deterministic = classify_failure(exc)
            if not deterministic:
                record_failure(label, "retry", exc)
                raise WebhookProcessingUnavailable(label) from exc
            applied = effect_already_applied(exc)
            record_failure(label, "already_applied" if applied else "quarantined", exc)
            try:
                outcome = await self._quarantine(envelope, already_applied=applied)
                await self._db.commit()
            except Exception as second:
                await self._rollback()
                second_label, _ = classify_failure(second)
                record_failure(second_label, "retry", second)
                raise WebhookProcessingUnavailable(second_label) from second
        _count(BILLING_WEBHOOK_DELIVERIES_TOTAL, family=family, outcome=outcome.outcome)
        for kind in outcome.alerts:
            alert(kind)
        return outcome

    async def _rollback(self) -> None:
        try:
            await self._db.rollback()
        except Exception:
            return

    def _delivery_values(
        self, envelope: WebhookEnvelope, status: str
    ) -> dict[str, object]:
        builder_id, kind, resource_id = self._resource
        return {
            "id": uuid4(),
            "builder_id": builder_id,
            "provider": PROVIDER,
            "event_id": envelope.event_id,
            "event_name": envelope.event_name,
            "provider_created_at": envelope.provider_created_at,
            "received_at": self._clock(),
            "resource_kind": kind,
            "resource_id": resource_id,
            "body_bytes": envelope.body_bytes,
            "body_sha256": envelope.body_sha256,
            "status": status,
        }

    async def _reserve(
        self, envelope: WebhookEnvelope, status: str
    ) -> BillingWebhookDelivery | None:
        created = await self._repo.reserve_webhook_delivery(
            self._delivery_values(envelope, status)
        )
        if not created:
            return None
        return await self._repo.lock_webhook_delivery(
            provider=PROVIDER, event_id=envelope.event_id
        )

    async def _duplicate(self, envelope: WebhookEnvelope) -> _Outcome:
        existing = await self._repo.lock_webhook_delivery(
            provider=PROVIDER, event_id=envelope.event_id
        )
        alerts: list[str] = []
        if existing is not None and existing.body_sha256 != envelope.body_sha256:
            alerts.append("drift")  # same provider event id, different bytes
        return _Outcome(envelope.family or "unknown", "duplicate", alerts=alerts)

    async def _quarantine(
        self, envelope: WebhookEnvelope, *, already_applied: bool = False
    ) -> _Outcome:
        if already_applied:
            # The idempotency backstop proved the effect exists: not drift.
            delivery = await self._reserve(envelope, "processed")
            if delivery is None:
                return await self._duplicate(envelope)
            return _Outcome(
                envelope.family or "unknown", "processed", delivery_id=delivery.id
            )
        delivery = await self._reserve(envelope, "readback_required")
        if delivery is None:
            return await self._duplicate(envelope)
        return _Outcome(
            envelope.family or "unknown",
            "quarantined",
            delivery_id=delivery.id,
            readback=delivery.resource_kind is not None,
            alerts=["quarantine"],
        )

    def _bind(
        self,
        delivery: BillingWebhookDelivery,
        *,
        builder_id: UUID,
        kind: str,
        resource_id: str,
    ) -> None:
        delivery.builder_id = builder_id
        delivery.resource_kind = kind
        delivery.resource_id = resource_id
        self._resource = (builder_id, kind, resource_id)

    async def _receive(self, envelope: WebhookEnvelope) -> _Outcome:
        if not envelope.known:
            return await self._receive_unknown(envelope)
        delivery = await self._reserve(envelope, "received")
        if delivery is None:
            return await self._duplicate(envelope)
        await self._repo.lock_rollout_control(environment=self._environment)
        state = _FinancialState(
            self._db, environment=self._environment, clock=self._clock
        )
        family = str(envelope.family)
        try:
            if family == "checkout":
                status = await self._on_checkout(envelope, delivery, state)
            elif family == "subscription":
                status = await self._on_subscription(envelope, delivery, state)
            else:
                status = await self._on_payment(envelope, delivery, state)
        except _Uncorrelated:
            delivery.status = "readback_required"
            await self._db.flush()
            return _Outcome(
                family,
                "uncorrelated",
                delivery_id=delivery.id,
                readback=delivery.resource_kind is not None,
                alerts=[*state.alerts, "uncorrelated_entity"],
            )
        delivery.status = status
        await self._db.flush()
        return _Outcome(
            family,
            "processed" if status == "processed" else "readback_required",
            delivery_id=delivery.id,
            readback=status == "readback_required",
            alerts=list(state.alerts),
        )

    async def _receive_unknown(self, envelope: WebhookEnvelope) -> _Outcome:
        """Persist only the minimal envelope; correlate resource_id if exact."""

        state = _FinancialState(
            self._db, environment=self._environment, clock=self._clock
        )
        for key, identifier in envelope.loose_ids:
            if key == "checkout":
                session = await state.session_by_external(identifier)
                if session is not None:
                    self._resource = (
                        session.builder_id,
                        "checkout_session",
                        str(session.id),
                    )
                    break
            elif key == "subscription":
                row = await state.subscription_by_external(identifier)
                if row is not None:
                    self._resource = (row.builder_id, "subscription", str(row.id))
                    break
            else:
                projection = await state.projection(identifier)
                if projection is not None:
                    self._resource = (projection.builder_id, "payment", identifier)
                    break
        delivery = await self._reserve(envelope, "unknown")
        if delivery is None:
            return await self._duplicate(envelope)
        return _Outcome(
            "unknown",
            "unknown_event",
            delivery_id=delivery.id,
            readback=delivery.resource_kind is not None,
            alerts=["unknown_event"],
        )

    # -- checkout --

    async def _on_checkout(
        self,
        envelope: WebhookEnvelope,
        delivery: BillingWebhookDelivery,
        state: _FinancialState,
    ) -> str:
        facts = envelope.checkout
        assert facts is not None
        by_id = (
            await state.session_by_external(facts.checkout_id)
            if facts.checkout_id is not None
            else None
        )
        by_reference = (
            await state.session_by_id(facts.external_reference)
            if facts.external_reference is not None
            else None
        )
        if (
            by_id is not None
            and by_reference is not None
            and by_id.id != by_reference.id
        ):
            raise _Uncorrelated()
        candidate = by_id or by_reference
        if candidate is None:
            raise _Uncorrelated()
        session = await self._repo.lock_checkout_session(
            builder_id=candidate.builder_id, checkout_session_id=candidate.id
        )
        if session is None:
            raise _Uncorrelated()
        if facts.checkout_id is not None and session.external_checkout_id not in (
            None,
            facts.checkout_id,
        ):
            raise _Uncorrelated()
        self._bind(
            delivery,
            builder_id=session.builder_id,
            kind="checkout_session",
            resource_id=str(session.id),
        )
        if session.external_checkout_id is None and facts.checkout_id is not None:
            session.external_checkout_id = facts.checkout_id  # write-once (trigger)
        current = session.state
        name = envelope.event_name
        result = "processed"
        if name == "CHECKOUT_CREATED":
            # ``creating`` stays: Group 2 marks it ``active`` after its POST.
            if current == "reconciliation_required":
                session.state = "active"
        elif name == "CHECKOUT_PAID":
            if current in (
                "creating",
                "reconciliation_required",
                "active",
                "terminal_pending_readback",
            ):
                session.state = (
                    "completed"
                    if session.intent_kind == "topup"
                    else "paid_pending_subscription"
                )
            elif current in ("abandoned", "cancelled", "expired", "failed"):
                state.alerts.append("drift")  # paid after we closed the attempt
                result = "readback_required"
        else:  # CHECKOUT_CANCELED / CHECKOUT_EXPIRED
            if current in ("creating", "reconciliation_required", "active"):
                session.state = "terminal_pending_readback"
                result = "readback_required"
            elif current in ("paid_pending_subscription", "terminal_pending_readback"):
                result = "readback_required"
        await self._db.flush()
        return result

    # -- subscription --

    async def _on_subscription(
        self,
        envelope: WebhookEnvelope,
        delivery: BillingWebhookDelivery,
        state: _FinancialState,
    ) -> str:
        facts = envelope.subscription
        assert facts is not None
        if facts.subscription_id is None:
            raise _Uncorrelated()
        row = await state.subscription_by_external(facts.subscription_id)
        name = envelope.event_name
        if row is None:
            if name != "SUBSCRIPTION_CREATED" or facts.external_reference is None:
                raise _Uncorrelated()
            session = await state.session_by_id(facts.external_reference)
            if session is None or session.intent_kind != "subscription":
                raise _Uncorrelated()
            created = await state.handoff(
                session.id,
                session.builder_id,
                facts.subscription_id,
                facts=facts,
            )
            self._bind(
                delivery,
                builder_id=created.builder_id,
                kind="subscription",
                resource_id=str(created.id),
            )
            return (
                "processed"
                if created.status == "active_linked"
                else "readback_required"
            )
        if facts.external_reference is not None:
            origin = await state.originating_session(row)
            if origin is not None and origin.id != facts.external_reference:
                raise _Uncorrelated()
        await self._repo.lock_billing_roster()
        locked = await self._repo.lock_subscription(
            builder_id=row.builder_id, subscription_id=row.id
        )
        if locked is None:
            raise _Uncorrelated()
        self._bind(
            delivery,
            builder_id=locked.builder_id,
            kind="subscription",
            resource_id=str(locked.id),
        )
        if name == "SUBSCRIPTION_CREATED":
            return "processed"
        if name == "SUBSCRIPTION_UPDATED":
            if locked.status in ("active", "active_linked"):
                locked.status = "readback_required"
                await self._db.flush()
            return "readback_required"
        # INACTIVATED / DELETED: non-billable only after a converged readback.
        if locked.status in _LIVE_SUBSCRIPTION_STATUSES:
            locked.status = "non_billable_pending_readback"
            locked.is_active = False
            await self._db.flush()
        if locked.status in _TERMINAL_SUBSCRIPTION_STATUSES:
            return "processed"
        return "readback_required"

    # -- payment --

    async def _on_payment(
        self,
        envelope: WebhookEnvelope,
        delivery: BillingWebhookDelivery,
        state: _FinancialState,
    ) -> str:
        facts = envelope.payment
        if facts is None or facts.payment_id is None:
            raise _Uncorrelated()
        target = await state.correlate_payment(facts)
        if isinstance(target, _Handoff):
            subscription = await state.handoff(
                target.session_id,
                target.builder_id,
                target.external_subscription_id,
                facts=None,
            )
            target = await state.correlate_payment(facts)
            if (
                isinstance(target, _Handoff)
                or subscription.id != target.subscription_id
            ):
                raise _Uncorrelated()
        self._bind(
            delivery,
            builder_id=target.builder_id,
            kind="payment",
            resource_id=facts.payment_id,
        )
        await self._db.flush()
        normalized = PAYMENT_EVENT_STATUSES[envelope.event_name]
        return await state.apply_payment(
            target,
            payment_id=facts.payment_id,
            normalized=normalized,
            provider_created_at=envelope.provider_created_at,
            key=envelope.ordering_key(normalized),
            event_id=envelope.event_id,
            event_name=envelope.event_name,
            delivery=delivery,
            facts=facts,
            readback=None,
        )


# ---------------------------------------------------------------------------
# Readback convergence (no lock across network I/O).
# ---------------------------------------------------------------------------


class BillingReadbackService:
    """Exact-ID provider readbacks that converge deliveries and aggregates."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        client_factory: Callable[[], AsaasClient],
        environment: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._db = db
        self._client_factory = client_factory
        self._environment = environment
        self._clock = clock
        self._repo = BillingRepository(db)

    async def _rollback(self) -> None:
        try:
            await self._db.rollback()
        except Exception:
            return

    async def process_delivery(self, delivery_id: UUID) -> str:
        """Read back the resource a delivery correlated to; close it if converged."""

        try:
            # Plain columns: a rollback expires ORM instances.
            found = (
                await self._db.execute(
                    select(
                        BillingWebhookDelivery.status,
                        BillingWebhookDelivery.resource_kind,
                        BillingWebhookDelivery.resource_id,
                        BillingWebhookDelivery.event_name,
                    ).where(BillingWebhookDelivery.id == delivery_id)
                )
            ).one_or_none()
            await self._db.rollback()
        except Exception:
            await self._rollback()
            return "failed"
        if found is None or found[0] not in ("readback_required", "unknown"):
            return "skipped"
        _status, kind, resource_id, event_name = found
        if kind is None or resource_id is None:
            return "pending"
        if kind == "payment":
            outcome = await self.readback_payment(resource_id, delivery_id=delivery_id)
        elif kind == "subscription":
            outcome = await self.readback_subscription(UUID(resource_id))
        elif kind == "checkout_session":
            outcome = await self.readback_checkout(
                UUID(resource_id), terminal_event=event_name
            )
        else:
            outcome = "pending"
        if outcome == "converged":
            await self._close_delivery(delivery_id)
        return outcome

    async def _close_delivery(self, delivery_id: UUID) -> None:
        try:
            locked = await self._repo.lock_webhook_delivery_by_id(
                delivery_id=delivery_id
            )
            if locked is not None and locked.status in ("readback_required", "unknown"):
                locked.status = "processed"
            await self._db.commit()
        except Exception:
            await self._rollback()

    # -- payments --

    async def readback_payment(self, payment_id: str, *, delivery_id: UUID) -> str:
        try:
            readback = await self._client_factory().get_payment(payment_id)
        except Exception as exc:
            _readback_metric("payment", "failed", exc)
            return "failed"
        facts = _payment_facts_from_readback(readback)
        normalized = (
            "deleted"
            if readback.deleted
            else _READBACK_PAYMENT_STATUSES.get(readback.status or "", "unknown")
        )
        state = _FinancialState(
            self._db, environment=self._environment, clock=self._clock
        )
        try:
            delivery = await self._repo.lock_webhook_delivery_by_id(
                delivery_id=delivery_id
            )
            if delivery is None:
                await self._db.rollback()
                return "skipped"
            await self._repo.lock_rollout_control(environment=self._environment)
            target = await state.correlate_payment(facts)
            if isinstance(target, _Handoff):
                await state.handoff(
                    target.session_id,
                    target.builder_id,
                    target.external_subscription_id,
                    facts=None,
                )
                target = await state.correlate_payment(facts)
                if isinstance(target, _Handoff):
                    raise _Uncorrelated()
            if delivery.builder_id not in (None, target.builder_id):
                raise _Uncorrelated()
            delivery.builder_id = target.builder_id
            await self._db.flush()
            now = self._clock()
            status = await state.apply_payment(
                target,
                payment_id=payment_id,
                normalized=normalized,
                provider_created_at=now,
                key=ordering_key(now, normalized, "readback"),
                event_id=delivery.event_id,
                event_name=delivery.event_name,
                delivery=delivery,
                facts=facts,
                readback=readback,
            )
            await self._db.commit()
        except _Uncorrelated:
            await self._rollback()
            _readback_metric("payment", "pending")
            alert("uncorrelated_entity")
            return "pending"
        except Exception as exc:
            await self._rollback()
            _readback_metric("payment", "failed", exc)
            return "failed"
        for kind in state.alerts:
            alert(kind)
        outcome = (
            "converged"
            if status == "processed"
            else ("drift" if "drift" in state.alerts else "pending")
        )
        _count(BILLING_READBACKS_TOTAL, resource="payment", outcome=outcome)
        return outcome

    # -- subscriptions (M-D: terminal only after a converged read) --

    async def readback_subscription(self, subscription_id: UUID) -> str:
        try:
            row = (
                await self._db.execute(
                    select(BillingSubscription)
                    .where(BillingSubscription.id == subscription_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            first_due = None
            snapshot: tuple[UUID, str | None, int | None, str] | None = None
            if row is not None:
                origin = await _FinancialState(
                    self._db, environment=self._environment, clock=self._clock
                ).originating_session(row)
                first_due = origin.next_due_date if origin is not None else None
                # Plain values: the rollback below expires ORM instances.
                snapshot = (
                    row.builder_id,
                    row.provider,
                    row.amount_cents,
                    row.external_subscription_id,
                )
            await self._db.rollback()
        except Exception as exc:
            await self._rollback()
            _readback_metric("subscription", "failed", exc)
            return "failed"
        if snapshot is None or snapshot[1] != PROVIDER or snapshot[2] is None:
            return "skipped"
        builder_id, _provider, _amount, external_id = snapshot
        try:
            client = self._client_factory()
            remote = await client.get_subscription(external_id)
            charges = await client.list_payments_by_subscription(external_id)
        except Exception as exc:
            _readback_metric("subscription", "failed", exc)
            return "failed"
        pending_charges = any(
            not charge.deleted and charge.status in _PENDING_CHARGE_STATUSES
            for charge in charges
        )
        non_billable = remote.deleted or remote.status in ("INACTIVE", "EXPIRED")
        try:
            await self._repo.lock_rollout_control(environment=self._environment)
            await self._repo.lock_billing_roster()
            locked = await self._repo.lock_subscription(
                builder_id=builder_id, subscription_id=subscription_id
            )
            if locked is None:
                await self._db.rollback()
                return "skipped"
            outcome = await self._converge_subscription(
                locked,
                remote,
                pending_charges=pending_charges,
                non_billable=non_billable,
                first_due=first_due,
            )
            await self._db.commit()
        except Exception as exc:
            await self._rollback()
            _readback_metric("subscription", "failed", exc)
            return "failed"
        _readback_metric("subscription", outcome)
        return outcome

    async def _converge_subscription(
        self,
        row: BillingSubscription,
        remote: SubscriptionReadback,
        *,
        pending_charges: bool,
        non_billable: bool,
        first_due: date | None,
    ) -> str:
        if non_billable:
            if pending_charges:
                # Not converged: a collectable charge still exists.
                if row.status in _LIVE_SUBSCRIPTION_STATUSES:
                    row.status = "non_billable_pending_readback"
                    row.is_active = False
                    await self._db.flush()
                return "pending"
            target = "cancelled" if remote.deleted else "inactive"
            if row.status == "cancelled" or row.status == target:
                return "converged"
            if row.status in _LIVE_SUBSCRIPTION_STATUSES:
                row.status = "non_billable_pending_readback"
                row.is_active = False
                await self._db.flush()
            row.status = target
            row.is_active = False
            await self._db.flush()
            return "converged"
        if row.status in ("non_billable_pending_readback", "inactive", "cancelled"):
            # The provider says live again after we saw it end: never reopen
            # recurrence silently.
            return "drift"
        matches = _FinancialState._subscription_matches(
            remote.cycle,
            remote.value_cents,
            remote.next_due_date,
            remote.status,
            remote.deleted,
            amount_cents=int(row.amount_cents or 0),
            first_due_date=first_due,
            exact_due_date=False,
        )
        if not matches:
            if row.status in ("active", "active_linked"):
                row.status = "readback_required"
                await self._db.flush()
            return "drift"
        if row.status == "readback_required":
            settled = await self._db.scalar(
                select(func.count(BillingPaymentProjection.id)).where(
                    BillingPaymentProjection.builder_id == row.builder_id,
                    BillingPaymentProjection.subscription_id == row.id,
                    BillingPaymentProjection.settled_cents > 0,
                )
            )
            row.status = "active" if settled else "active_linked"
            await self._db.flush()
        return "converged"

    # -- checkout sessions --

    async def readback_checkout(
        self, checkout_session_id: UUID, *, terminal_event: str | None = None
    ) -> str:
        """Converge ``terminal_pending_readback``/``paid_pending_subscription``.

        A cancelled/expired checkout only frees the builder after an exact
        read proves no subscription and no collectable payment; a found
        subscription is linked exactly (M-A handoff under the rollout lock).
        """

        try:
            # Plain columns: a rollback expires ORM instances.
            found = (
                await self._db.execute(
                    select(
                        BillingCheckoutSession.builder_id,
                        BillingCheckoutSession.state,
                        BillingCheckoutSession.intent_kind,
                        BillingCheckoutSession.external_checkout_id,
                        BillingCheckoutSession.created_at,
                    ).where(BillingCheckoutSession.id == checkout_session_id)
                )
            ).one_or_none()
            if terminal_event is None and found is not None:
                terminal_event = await self._db.scalar(
                    select(BillingWebhookDelivery.event_name)
                    .where(
                        BillingWebhookDelivery.provider == PROVIDER,
                        BillingWebhookDelivery.resource_kind == "checkout_session",
                        BillingWebhookDelivery.resource_id == str(checkout_session_id),
                        BillingWebhookDelivery.event_name.in_(
                            ("CHECKOUT_CANCELED", "CHECKOUT_EXPIRED")
                        ),
                    )
                    .order_by(BillingWebhookDelivery.provider_created_at.desc())
                    .limit(1)
                )
            await self._db.rollback()
        except Exception as exc:
            await self._rollback()
            _readback_metric("checkout_session", "failed", exc)
            return "failed"
        if found is None:
            return "skipped"
        builder_id, state_name, intent_kind, external_checkout_id, created_at = found
        if state_name in _TERMINAL_CHECKOUT_STATES:
            return "converged"
        if state_name not in (
            "terminal_pending_readback",
            "paid_pending_subscription",
        ):
            return "pending"
        provider_terminal = terminal_event in ("CHECKOUT_CANCELED", "CHECKOUT_EXPIRED")
        # Without a provider terminal event only the expiry-based limit plus
        # externalReference reads could free the builder, and that evidence is
        # dormant until ASAAS_AUTO_ABANDON_ENABLED (M-C).  A CANCELED/EXPIRED
        # checkout was never paid, and its payments are read by the documented
        # exact ``checkoutSession`` filter.  The reads below always run: they
        # can still link a subscription exactly (handoff).
        may_abandon = bool(
            settings.asaas_auto_abandon_enabled
            and created_at is not None
            and self._clock() >= _aware(created_at) + CHECKOUT_CONVERGENCE_LIMIT
        )
        try:
            client = self._client_factory()
            subscriptions = (
                await client.list_subscriptions_by_external_reference(
                    checkout_session_id
                )
                if intent_kind == "subscription"
                else ()
            )
            payments = (
                await client.list_payments_by_checkout(external_checkout_id)
                if external_checkout_id is not None
                else ()
            )
            by_reference = await client.list_payments_by_external_reference(
                checkout_session_id
            )
        except Exception as exc:
            _readback_metric("checkout_session", "failed", exc)
            return "failed"
        live_subscriptions = [s for s in subscriptions if not s.deleted]
        # A record without a readable status counts as live (fail closed).
        live_payments = [
            p
            for p in (*payments, *by_reference)
            if not p.deleted and p.status != "REFUNDED"
        ]
        state = _FinancialState(
            self._db, environment=self._environment, clock=self._clock
        )
        try:
            await self._repo.lock_rollout_control(environment=self._environment)
            if len(live_subscriptions) > 1:
                await self._db.rollback()
                _readback_metric("checkout_session", "drift")
                return "drift"
            if live_subscriptions:
                await state.handoff(
                    checkout_session_id,
                    builder_id,
                    live_subscriptions[0].record_id,
                    facts=None,
                )
                await self._db.commit()
                _readback_metric("checkout_session", "converged")
                return "converged"
            locked = await self._repo.lock_checkout_session(
                builder_id=builder_id, checkout_session_id=checkout_session_id
            )
            if locked is None or locked.state in _TERMINAL_CHECKOUT_STATES:
                await self._db.rollback()
                return "converged"
            if live_payments or locked.state == "paid_pending_subscription":
                # Money or a paid checkout without its subscription: keep
                # blocking and surface it; never free the builder here.
                await self._db.rollback()
                _readback_metric("checkout_session", "pending")
                alert("backlog")
                return "pending"
            if not provider_terminal and not may_abandon:
                await self._db.rollback()
                _readback_metric("checkout_session", "pending")
                return "pending"
            locked.state = {
                "CHECKOUT_CANCELED": "cancelled",
                "CHECKOUT_EXPIRED": "expired",
            }.get(terminal_event or "", "abandoned")
            locked.reconciliation_attempts += 1
            await self._db.commit()
        except _Uncorrelated:
            await self._rollback()
            _readback_metric("checkout_session", "drift")
            return "drift"
        except Exception as exc:
            await self._rollback()
            _readback_metric("checkout_session", "failed", exc)
            return "failed"
        _readback_metric("checkout_session", "converged")
        return "converged"

    # -- backlog sweep --

    async def reconcile_backlog(self, *, limit: int = 50) -> dict[str, int]:
        """Retry open readbacks and publish backlog gauges (sanitized counts)."""

        try:
            deliveries = list(
                (
                    await self._db.execute(
                        select(BillingWebhookDelivery.id)
                        .where(
                            BillingWebhookDelivery.provider == PROVIDER,
                            BillingWebhookDelivery.status.in_(
                                ("readback_required", "unknown")
                            ),
                            BillingWebhookDelivery.resource_kind.is_not(None),
                        )
                        .order_by(BillingWebhookDelivery.received_at)
                        .limit(limit)
                    )
                ).scalars()
            )
            subscriptions = list(
                (
                    await self._db.execute(
                        select(BillingSubscription.id)
                        .where(
                            BillingSubscription.provider == PROVIDER,
                            BillingSubscription.status.in_(
                                ("readback_required", "non_billable_pending_readback")
                            ),
                        )
                        .limit(limit)
                    )
                ).scalars()
            )
            sessions = list(
                (
                    await self._db.execute(
                        select(BillingCheckoutSession.id)
                        .where(
                            BillingCheckoutSession.provider == PROVIDER,
                            BillingCheckoutSession.state.in_(
                                (
                                    "terminal_pending_readback",
                                    "paid_pending_subscription",
                                )
                            ),
                        )
                        .limit(limit)
                    )
                ).all()
            )
            ambiguous = list(
                (
                    await self._db.execute(
                        select(
                            BillingCheckoutSession.id, BillingCheckoutSession.builder_id
                        )
                        .where(
                            BillingCheckoutSession.provider == PROVIDER,
                            BillingCheckoutSession.state.in_(
                                ("creating", "reconciliation_required")
                            ),
                            BillingCheckoutSession.created_at
                            <= self._clock() - ORPHANED_CREATING_AFTER,
                        )
                        .limit(limit)
                    )
                ).all()
            )
            await self._db.rollback()
        except Exception:
            await self._rollback()
            alert("readback_failure")
            return {}
        counts = {"delivery": 0, "subscription": 0, "checkout_session": 0}
        # M-C: orphaned ``creating`` and never-abandoned
        # ``reconciliation_required`` attempts converge without the builder.
        for session_id, builder_id in ambiguous:
            try:
                view = await BillingCheckoutService(
                    self._db, client_factory=self._client_factory, clock=self._clock
                ).reconcile_checkout(
                    builder_id=builder_id, checkout_session_id=session_id
                )
                if view.state in _LIVE_CHECKOUT_STATES:
                    counts["checkout_session"] += 1
            except Exception as exc:
                await self._rollback()
                counts["checkout_session"] += 1
                _readback_metric("checkout_session", "failed", exc)
        for delivery_id in deliveries:
            if await self.process_delivery(delivery_id) != "converged":
                counts["delivery"] += 1
        for subscription_id in subscriptions:
            if await self.readback_subscription(subscription_id) not in (
                "converged",
                "skipped",
            ):
                counts["subscription"] += 1
        for (session_id,) in sessions:
            if await self.readback_checkout(session_id) not in ("converged", "skipped"):
                counts["checkout_session"] += 1
        for resource, value in counts.items():
            try:
                BILLING_READBACK_BACKLOG.labels(resource=resource).set(value)
            except Exception:  # pragma: no cover - telemetry is best effort
                pass
        if any(counts.values()):
            alert("backlog")
        return counts


# ---------------------------------------------------------------------------
# Provider-side webhook provisioning, readback and readiness gate.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WebhookReadiness:
    """Sanitized readiness verdict; ``reasons`` is a closed vocabulary."""

    ready: bool
    reasons: tuple[str, ...]
    webhook_id: str | None = None


def desired_webhook_settings(
    *, environment: str, url: str, email: str
) -> WebhookSettings:
    """The one webhook this environment owns: exact name, URL and event set."""

    return WebhookSettings(
        name=f"neurostack-billing-{environment}",
        url=url,
        email=email,
        events=tuple(sorted(WEBHOOK_EVENTS)),
        send_type="SEQUENTIALLY",
        api_version=3,
    )


def compare_webhook(config: WebhookConfig, desired: WebhookSettings) -> tuple[str, ...]:
    """Return every drift reason between a readback and the desired config."""

    reasons: list[str] = []
    if config.name != desired.name:
        reasons.append("name")
    if config.url != desired.url:
        reasons.append("url")
    if config.api_version != desired.api_version:
        reasons.append("api_version")
    if config.send_type != desired.send_type:
        reasons.append("send_type")
    if config.enabled is not True:
        reasons.append("disabled")
    if config.interrupted is not False:
        reasons.append("interrupted")
    if (
        config.events is None
        or frozenset(config.events) != frozenset(desired.events)
        or len(config.events) != len(desired.events)
    ):
        reasons.append("events")
    if config.has_auth_token is not True:
        reasons.append("token_absent")
    return tuple(reasons)


class AsaasWebhookProvisioner:
    """Create/converge and read back this environment's ASAAS webhook."""

    def __init__(
        self,
        client: AsaasClient,
        desired: WebhookSettings,
        *,
        auth_token: SecretStr,
    ) -> None:
        self._client = client
        self._desired = desired
        self._token = auth_token

    def _token_ready(self) -> bool:
        token = self._token.get_secret_value() if self._token else ""
        return 32 <= len(token) <= 255 and all(33 <= ord(c) <= 126 for c in token)

    async def _owned(self) -> tuple[WebhookConfig | None, tuple[str, ...]]:
        configs = await self._client.list_webhooks()
        owned = [
            c
            for c in configs
            if c.name == self._desired.name or c.url == self._desired.url
        ]
        if len(owned) > 1:
            return None, ("ambiguous",)
        return (owned[0] if owned else None), ()

    async def readback(self) -> WebhookReadiness:
        """Read-only verdict: exact config, token present locally and remotely."""

        if not self._token_ready():
            _readback_metric("webhook", "drift")
            return WebhookReadiness(False, ("token_unconfigured",))
        try:
            config, problems = await self._owned()
            if problems:
                _readback_metric("webhook", "drift")
                return WebhookReadiness(False, problems)
            if config is None:
                _readback_metric("webhook", "drift")
                return WebhookReadiness(False, ("absent",))
            exact = await self._client.get_webhook(config.webhook_id)
        except Exception as exc:
            _readback_metric("webhook", "failed", exc)
            return WebhookReadiness(False, ("readback_failed",))
        reasons = compare_webhook(exact, self._desired)
        _readback_metric("webhook", "drift" if reasons else "converged")
        return WebhookReadiness(not reasons, reasons, webhook_id=exact.webhook_id)

    async def reconcile(self) -> WebhookReadiness:
        """Create or converge the webhook (token from the secret store), then read back."""

        if not self._token_ready():
            _readback_metric("webhook", "drift")
            return WebhookReadiness(False, ("token_unconfigured",))
        try:
            config, problems = await self._owned()
            if problems:
                _readback_metric("webhook", "drift")
                return WebhookReadiness(False, problems)
            if config is None:
                await self._client.create_webhook(self._desired, auth_token=self._token)
            elif compare_webhook(config, self._desired):
                await self._client.update_webhook(
                    config.webhook_id, self._desired, auth_token=self._token
                )
        except Exception as exc:
            _readback_metric("webhook", "failed", exc)
            return WebhookReadiness(False, ("provision_failed",))
        return await self.readback()

    async def smoke(self, transport: httpx.AsyncBaseTransport | None = None) -> bool:
        """Token smoke against our own public receiver; persists nothing.

        A wrong token must be refused (401) and the configured token must be
        accepted by the boundary yet refused as an envelope (``{}`` -> 400),
        proving authentication happens before parsing/persistence.
        """

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(5.0),
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as http:
                wrong = await http.post(
                    self._desired.url,
                    content=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        ASAAS_WEBHOOK_TOKEN_HEADER: "smoke-" + uuid4().hex,
                    },
                )
                right = await http.post(
                    self._desired.url,
                    content=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        ASAAS_WEBHOOK_TOKEN_HEADER: self._token.get_secret_value(),
                    },
                )
        except Exception as exc:
            _readback_metric("webhook", "failed", exc)
            return False
        passed = wrong.status_code == 401 and right.status_code == 400
        _readback_metric("webhook", "converged" if passed else "drift")
        return passed


class WebhookNotReady(RuntimeError):
    """Hosted checkout creation is blocked until the webhook reads back ready."""


class WebhookReadinessGate:
    """Process-local, fail-closed readiness cache for checkout creation.

    The gate starts closed.  A converged readback opens it for ``hard_ttl``;
    after ``soft_ttl`` a refresh is scheduled in the background (never inside
    the request, never under a database lock).  Drift or a failed readback
    closes it immediately.  Readback/containment paths never consult it.
    """

    def __init__(
        self,
        *,
        soft_ttl: float = 300.0,
        hard_ttl: float = 900.0,
        retry_interval: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._soft_ttl = soft_ttl
        self._hard_ttl = hard_ttl
        self._retry_interval = retry_interval
        self._monotonic = monotonic
        self._verdict: WebhookReadiness | None = None
        self._verified_at: float | None = None
        self._attempted_at: float | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def verdict(self) -> WebhookReadiness | None:
        return self._verdict

    def record(self, verdict: WebhookReadiness) -> None:
        now = self._monotonic()
        self._verdict = verdict
        self._attempted_at = now
        self._verified_at = now if verdict.ready else None
        try:
            BILLING_WEBHOOK_CONFIG_READY.set(1 if verdict.ready else 0)
        except Exception:  # pragma: no cover - telemetry is best effort
            pass

    def reset(self) -> None:
        self._verdict = None
        self._verified_at = None
        self._attempted_at = None

    def is_ready(self) -> bool:
        if (
            self._verdict is None
            or not self._verdict.ready
            or self._verified_at is None
        ):
            return False
        return self._monotonic() - self._verified_at < self._hard_ttl

    def require_ready(
        self, refresh: Callable[[], Awaitable[WebhookReadiness]] | None = None
    ) -> None:
        """Raise unless ready; schedule a background refresh when due."""

        now = self._monotonic()
        due = (
            self._verified_at is None or now - self._verified_at >= self._soft_ttl
        ) and (
            self._attempted_at is None
            or now - self._attempted_at >= self._retry_interval
        )
        if refresh is not None and due:
            self._schedule(refresh)
        if not self.is_ready():
            raise WebhookNotReady()

    def _schedule(self, refresh: Callable[[], Awaitable[WebhookReadiness]]) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._attempted_at = self._monotonic()

        async def run() -> None:
            try:
                verdict = await refresh()
            except Exception:
                verdict = WebhookReadiness(False, ("readback_failed",))
            self.record(verdict)

        self._task = loop.create_task(run())

    def is_fresh(self) -> bool:
        return bool(
            self.is_ready()
            and self._verified_at is not None
            and self._monotonic() - self._verified_at < self._soft_ttl
        )

    async def ensure_fresh(
        self,
        refresh: Callable[[], Awaitable[WebhookReadiness]],
        *,
        timeout: float,
    ) -> None:
        """Await at most ``timeout`` for a refresh when the verdict is stale.

        Called by the checkout path before any lock or transaction; a slow or
        failed readback simply leaves the gate as it was (closed once expired).
        One refresh is in flight at a time per process; repeated failures are
        retried at most every ``retry_interval``.
        """

        if self.is_fresh():
            return
        if self._task is None or self._task.done():
            now = self._monotonic()
            if self._attempted_at is not None and (
                now - self._attempted_at < self._retry_interval
            ):
                return
            self._schedule(refresh)
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except Exception:
            # Timeout or failed readback: the gate stays as recorded (closed
            # once expired).  Cancellation of the caller still propagates.
            return

    async def run_refresher(
        self,
        refresh: Callable[[], Awaitable[WebhookReadiness]],
        *,
        interval: float | None = None,
        max_backoff: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Keep this process's verdict warm (app lifespan task, until cancelled).

        Success re-reads every ``interval`` (default ``soft_ttl``, inside the
        ``hard_ttl`` window, so an idle worker never expires).  Failure keeps
        the gate closed and retries with exponential backoff from
        ``retry_interval`` up to ``max_backoff``.  Every worker process runs its
        own refresher and gate; one worker's failed readback never opens or
        closes another's.
        """

        period = interval if interval is not None else self._soft_ttl
        ceiling = max_backoff if max_backoff is not None else period
        backoff = self._retry_interval
        while True:
            verdict = await self.refresh_now(refresh)
            if verdict.ready:
                backoff = self._retry_interval
                await sleep(period)
            else:
                await sleep(min(backoff, ceiling))
                backoff = min(backoff * 2, ceiling)

    async def refresh_now(
        self, refresh: Callable[[], Awaitable[WebhookReadiness]]
    ) -> WebhookReadiness:
        try:
            verdict = await refresh()
        except Exception:
            verdict = WebhookReadiness(False, ("readback_failed",))
        self.record(verdict)
        return verdict


ASAAS_WEBHOOK_READINESS: Final = WebhookReadinessGate()


async def run_delivery_readback(
    delivery_id: UUID,
    *,
    session_factory: Callable[[], AsyncSession],
    client_factory: Callable[[], AsaasClient],
    environment: str,
) -> None:
    """Background readback of one delivery with its own session; never raises."""

    try:
        async with session_factory() as session:
            await BillingReadbackService(
                session, client_factory=client_factory, environment=environment
            ).process_delivery(delivery_id)
    except Exception as exc:
        logger.warning(
            "billing readback failure",
            resource="delivery",
            failure=classify_failure(exc)[0],
            failure_class=type(exc).__name__,
        )
        alert("readback_failure")


__all__ = [
    "ASAAS_INGRESS_BODY_STATE",
    "ASAAS_INGRESS_PAYLOAD_STATE",
    "ASAAS_WEBHOOK_BODY_BYTES",
    "ASAAS_WEBHOOK_PATH",
    "ASAAS_WEBHOOK_READINESS",
    "ASAAS_WEBHOOK_READ_TIMEOUT_SECONDS",
    "ASAAS_WEBHOOK_TOKEN_HEADER",
    "AsaasWebhookProvisioner",
    "BillingReadbackService",
    "BillingWebhookService",
    "CHECKOUT_EVENT_STATES",
    "PAYMENT_EVENT_STATUSES",
    "SUBSCRIPTION_EVENT_STATUSES",
    "WEBHOOK_EVENTS",
    "WebhookEnvelope",
    "WebhookEnvelopeRejected",
    "WebhookNotReady",
    "WebhookProcessingUnavailable",
    "WebhookReadiness",
    "WebhookReadinessGate",
    "alert",
    "compare_webhook",
    "desired_webhook_settings",
    "event_key",
    "ordering_key",
    "parse_envelope",
    "record_rejection",
    "run_delivery_readback",
]
