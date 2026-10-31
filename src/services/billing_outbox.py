"""Canonical, ledger-free publication of ASAAS financial facts.

This module has one authority: turn an already-persisted Group 3 payment
effect plus its cumulative cents into a closed ``billing.fact.v1`` envelope.
It neither grants entitlement nor interprets a projection status as money.
Every retry reuses the immutable outbox row and therefore its event id.
"""

from __future__ import annotations

import asyncio
import calendar
import copy
import hashlib
import json
import os
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import DateTime, Select, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import BindParameter, ColumnElement

from src.models.billing import (
    BillingCheckoutSession,
    BillingFactAck,
    BillingFactDelivery,
    BillingFactHandoff,
    BillingFactReconciliationAlert,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingPaymentProjection,
    BillingSubscriptionCycle,
    BillingTopupOrder,
)
from src.services.billing_catalog import BILLING_CATALOG, CATALOG_VERSION_ID

SCHEMA_VERSION: Final = "billing.fact.v1"
ACK_SCHEMA_VERSION: Final = "billing.fact.ack.v1"
PRODUCER: Final = "asaas-payment-gateway"
_EVENT_NAMESPACE: Final = uuid5(NAMESPACE_URL, "neurostack:billing.fact.v1")
_CREDIT_FIELD: Final = "credit" + "_grant_mc"
_LINE_KINDS: Final = frozenset({"plan", "additional_clinic", "topup", "tax"})
_LINE_STATES: Final = frozenset({"pending", "settled", "reversed", "restored"})
_EVENT_TYPES: Final = {
    ("subscription_cycle", "settlement"): "subscription.cycle_settled.v1",
    ("subscription_cycle", "reversal"): "subscription.cycle_reversed.v1",
    ("subscription_cycle", "restoration"): "subscription.cycle_restored.v1",
    ("topup_order", "settlement"): "topup.payment_settled.v1",
    ("topup_order", "reversal"): "topup.payment_reversed.v1",
    ("topup_order", "restoration"): "topup.payment_restored.v1",
}
_FACT_ROOT: Final = frozenset(
    {
        "schema_version",
        "event_id",
        "event_type",
        "producer",
        "occurred_at",
        "produced_at",
        "builder_id",
        "aggregate_kind",
        "aggregate_id",
        "subscription_id",
        "order_id",
        "payment_id",
        "paid_at",
        "catalog_version_id",
        "offer_id",
        "currency",
        "financial_revision",
        "total_amount_cents",
        "settled_cents",
        "reversed_cents",
        "restored_cents",
        _CREDIT_FIELD,
        "lines",
    }
)
_LINE_ROOT: Final = frozenset(
    {
        "line_id",
        "offer_id",
        "kind",
        "quantity",
        "unit_amount_cents",
        "line_amount_cents",
        _CREDIT_FIELD,
        "settled_cents",
        "reversed_cents",
        "restored_cents",
        "financial_state",
    }
)
_FORBIDDEN_KEYS: Final = frozenset(
    {
        "name",
        "email",
        "cpf",
        "cnpj",
        "card",
        "creditcard",
        "token",
        "customer",
        "account",
        "raw_payload",
    }
)
_DEFAULT_DEPLOYMENT_DIGEST: Final = hashlib.sha256(
    b"billing.fact.v1:deployment-unverified"
).hexdigest()
# Delivery states a publisher may still present.  ``acknowledged`` and
# ``blocked`` are terminal for presentation; neither ever deletes the fact.
_PRESENTABLE: Final = ("pending", "awaiting_ack")
# Fixed, sanitized ``backlog`` alert details (stored only as SHA-256).  They
# reuse the existing alert vocabulary, so no schema change is required.
ACK_MISSING_DETAIL: Final = "ack missing"
ACK_OVERDUE_DETAIL: Final = "ack overdue"
RETRY_EXHAUSTED_DETAIL: Final = "retry cap exhausted"

# Presenting a fact to a real consumer is NOT safe yet.  Three binding
# preconditions must all be met before this may become ``False``:
#
# 1. MET by the two-phase record of ``BillingFactPublisher.publish_next``:
#    the attempt is counted in a committed lease BEFORE the fact is
#    presented, so a crash or lost commit after a consumer-visible send
#    leaves exactly that attempt counted, and the backoff and
#    ``max_attempts`` still bound the re-presentation.  The legacy
#    ``publish_pending`` is still single-phase and is not a delivery path:
#    it must never be wired, whatever this flag says.
# 2. ``blocked`` deliveries have an operator requeue path (today only a late
#    valid ack or manual SQL leaves ``blocked``).
# 3. The runbook ``docs/runbooks/asaas-billing-reconciliation.md`` covers the
#    real transport.
#
# While it is ``True`` every presenting entry point raises and production
# wires ``transport=None``; only tests waive it, explicitly, per test.
PRESENTATION_PRECONDITIONS_OPEN: Final = True


class PresentationPreconditionsOpen(RuntimeError):
    """A consumer transport was used before presentation is crash-safe."""


class TransportNotReady(RuntimeError):
    """The consumer turned the presentation away unread: NOT an attempt.

    A fact transport raises this when the consumer's readiness flipped off
    during a presentation, before the consumer acted on the fact.  The
    publisher gives back the attempt it leased (``attempt_count`` is
    compensated) and keeps ``last_attempt_at``, so the fact is due again
    only after the backoff: a consumer that stays unready neither spends
    ``max_attempts`` nor makes the worker spin.  Any other exception from
    the transport is a failed, counted attempt.
    """


def require_presentation_allowed() -> None:
    """Raise unless the presentation preconditions above are all closed."""

    if PRESENTATION_PRECONDITIONS_OPEN:
        raise PresentationPreconditionsOpen(
            "billing fact presentation preconditions are open: no consumer "
            "transport may be wired (see PRESENTATION_PRECONDITIONS_OPEN)"
        )


def _detail_digest(detail: str) -> str:
    return hashlib.sha256(detail.encode("utf-8")).hexdigest()


def _valid_ack_exists() -> ColumnElement[bool]:
    """``EXISTS`` a consumer ack the reconciler would accept for the delivery.

    Mirrors :meth:`BillingFactReconciler._reconcile_fact`: same schema, same
    event id, ``accepted``, and fact/bundle/deployment digests equal to the
    outbox row and its handoff.  A mismatching ack does not count, so the
    fact keeps alerting until the reconciler moves it to ``blocked``.
    """

    return (
        select(BillingFactAck.id)
        .join(
            BillingOutboxEvent, BillingOutboxEvent.id == BillingFactAck.outbox_event_id
        )
        .join(
            BillingFactHandoff,
            BillingFactHandoff.outbox_event_id == BillingFactAck.outbox_event_id,
        )
        .where(
            BillingFactAck.outbox_event_id == BillingFactDelivery.outbox_event_id,
            BillingFactAck.schema_version == ACK_SCHEMA_VERSION,
            BillingFactAck.event_id == BillingFactDelivery.event_id,
            BillingFactAck.outcome != "rejected",
            BillingFactAck.fact_sha256 == BillingOutboxEvent.payload_sha256,
            BillingFactAck.bundle_digest == BillingFactHandoff.bundle_digest,
            BillingFactAck.deployment_digest == BillingFactHandoff.deployment_digest,
        )
        .correlate(BillingFactDelivery)
        .exists()
    )


async def _append_alert_row(
    db: AsyncSession,
    *,
    kind: str,
    outbox_event_id: UUID | None,
    detail: str,
    observed_at: datetime,
) -> None:
    """Append one sanitized alert unless the same (fact, kind, detail) exists.

    Check-then-insert with no unique constraint: de-duplication rests on the
    callers being serialized (the maintenance advisory lock, or the delivery
    row lock for the retry-cap alert), not on the schema.
    """

    digest = _detail_digest(detail)
    existing = await db.scalar(
        select(BillingFactReconciliationAlert.id).where(
            BillingFactReconciliationAlert.outbox_event_id == outbox_event_id,
            BillingFactReconciliationAlert.kind == kind,
            BillingFactReconciliationAlert.detail_sha256 == digest,
        )
    )
    if existing is None:
        db.add(
            BillingFactReconciliationAlert(
                id=uuid4(),
                outbox_event_id=outbox_event_id,
                kind=kind,
                detail_sha256=digest,
                observed_at=observed_at,
            )
        )


class BillingFactViolation(ValueError):
    """A closed producer/consumer contract invariant was not met."""


class PartialCreditLine(BillingFactViolation):
    """A partial settlement would make a credit-bearing line ambiguous."""


class AmbiguousLineAllocation(BillingFactViolation):
    """Aggregate provider money cannot be attributed to individual lines."""


@dataclass(frozen=True, slots=True)
class PublishedFact:
    """One immutable message that may safely be re-presented to a consumer."""

    outbox_event_id: UUID
    event_id: UUID
    payload: dict[str, object]


PublishOutcome = Literal["presented", "failed", "exhausted", "skipped", "not_ready"]


@dataclass(frozen=True, slots=True)
class FactRetryPolicy:
    """Bounded re-presentation: exponential backoff, then a terminal block.

    ``max_attempts`` counts presentations without an append-only ack.  After
    the last one the consumer still gets ``backoff(max_attempts)`` to ack
    before the delivery becomes ``blocked``; the fact itself is never deleted.
    """

    max_attempts: int
    base_backoff: timedelta = timedelta(seconds=60)
    max_backoff: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_backoff <= timedelta(0) or self.max_backoff < self.base_backoff:
            raise ValueError("retry backoff must be positive and ordered")

    def backoff(self, attempts: int) -> timedelta:
        """Wait after the ``attempts``-th presentation before the next claim."""

        if attempts <= 0:
            return timedelta(0)
        exponent = min(attempts, self.max_attempts, 32) - 1
        return min(self.base_backoff * (1 << exponent), self.max_backoff)


@dataclass(frozen=True, slots=True)
class PublishAttempt:
    """Outcome of one claimed delivery; ``failure`` is an exception class only."""

    event_id: UUID
    outcome: PublishOutcome
    attempt_count: int
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class _Lease:
    """Phase-1 evidence: the attempt committed before the fact is presented.

    ``attempt_count``/``leased_at`` are the values this publisher wrote; phase
    2 records the outcome only while the row still carries both.
    """

    event_id: UUID
    attempt_count: int
    leased_at: datetime
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class FactBacklog:
    """Global delivery backlog read by every worker loop (never per process)."""

    unacked: int
    stale: int
    blocked: int


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Current producer/consumer proof state; any issue blocks rollout."""

    backlog: int
    gaps: int
    collisions: int
    schema_mismatches: int
    digest_mismatches: int

    @property
    def rollout_blocked(self) -> bool:
        return any(
            (
                self.backlog,
                self.gaps,
                self.collisions,
                self.schema_mismatches,
                self.digest_mismatches,
            )
        )


@dataclass(frozen=True, slots=True)
class ReconciliationPage:
    """One bounded reconciliation pass and the keyset cursor for the next one.

    ``next_after`` is ``None`` when the open set was exhausted, so the next
    page wraps to the beginning and every open fact is revisited each cycle.
    """

    result: ReconciliationResult
    next_after: UUID | None


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """Return the exact byte representation used for payload hashes."""

    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def payload_digest(value: Mapping[str, object]) -> str:
    """Return a lowercase SHA-256 for a canonical fact object."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def billing_fact_bundle_digest(
    manifest: Path | None = None,
) -> str:
    """Hash the exact, versioned manifest bytes rather than a reconstructed list.

    The default is the copy packaged under ``src/contracts`` (the runtime image
    never ships ``tests/``, issue #137); it is byte-identical to the fixture
    manifest the bundle verifier checks, which a contract pin test enforces.
    """

    path = manifest or (
        Path(__file__).resolve().parents[2] / "src/contracts/billing_fact_v1/SHA256SUMS"
    )
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BillingFactViolation(
            "billing fact bundle manifest is unavailable"
        ) from exc


def billing_fact_schema_digest(schema: Path | None = None) -> str:
    """Hash the deployed billing fact schema bytes used by every producer."""

    path = schema or (
        Path(__file__).resolve().parents[2]
        / "src/contracts/billing_fact_v1.schema.json"
    )
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BillingFactViolation("billing fact schema is unavailable") from exc


def authoritative_billing_fact_schema_digest(environment: str) -> str:
    """Return the deployed schema digest used by capability authority.

    Production must receive this digest from the deployed artifact/secret
    contract; it may not silently derive authority from a mutable checkout
    source tree.  Non-production fixtures may use the checked-in schema when
    no deployment manifest is supplied.
    """

    configured = os.environ.get("BILLING_FACT_SCHEMA_DIGEST", "").strip()
    if not configured:
        try:
            from src.config.settings import settings

            configured = settings.billing_fact_schema_digest.strip()
        except (AttributeError, ImportError):
            configured = ""
    if environment == "production" and not configured:
        raise BillingFactViolation("authoritative billing fact schema is unavailable")
    expected = billing_fact_schema_digest()
    if configured and configured != expected:
        raise BillingFactViolation("authoritative billing fact schema is stale")
    return configured or expected


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or int(value) < minimum:
        raise BillingFactViolation(f"invalid {field}")
    return int(value)


def _text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise BillingFactViolation(f"invalid {field}")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BillingFactViolation(f"invalid {field}")
    return cast(Mapping[str, object], value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # PostgreSQL returns TIMESTAMPTZ aware.  SQLite intentionally loses
        # that annotation in focused tests, while its server default is still
        # UTC; restore the known storage convention at this adapter boundary.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _require_uuid(value: object, field: str) -> UUID:
    if not isinstance(value, str):
        raise BillingFactViolation(f"invalid {field}")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise BillingFactViolation(f"invalid {field}") from exc
    if str(parsed) != value.lower():
        raise BillingFactViolation(f"invalid {field}")
    return parsed


def _fact_datetime(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise BillingFactViolation(f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BillingFactViolation(f"invalid {field}") from exc
    if parsed.tzinfo is None:
        raise BillingFactViolation(f"invalid {field}")


def _fact_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise BillingFactViolation(f"invalid {field}")
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("non-canonical date")
        return parsed
    except ValueError as exc:
        raise BillingFactViolation(f"invalid {field}") from exc


def _reject_pii(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or key.lower().replace("_", "") in _FORBIDDEN_KEYS
            ):
                raise BillingFactViolation("fact contains forbidden field")
            _reject_pii(item)
    elif isinstance(value, list):
        for item in value:
            _reject_pii(item)


def _line_state(*, settled: int, reversed_cents: int, restored: int) -> str:
    if settled == 0:
        return "pending"
    if restored > 0:
        return "restored"
    if reversed_cents > 0:
        return "reversed"
    return "settled"


def _allocate(cumulative: int, amounts: list[int]) -> list[int]:
    """Copy only line-safe cumulative totals; never synthesize a split."""

    total = sum(amounts)
    if len(amounts) > 1:
        if cumulative == 0:
            return [0 for _ in amounts]
        if cumulative != total:
            raise AmbiguousLineAllocation(
                "provider aggregate cannot identify multi-line allocation"
            )
        return list(amounts)

    if cumulative > total:
        raise BillingFactViolation("cumulative total exceeds line amounts")
    return [cumulative] if amounts else []


def _copy_lines(
    snapshot: Mapping[str, object],
    *,
    settled_cents: int,
    reversed_cents: int,
    restored_cents: int,
) -> list[dict[str, object]]:
    raw_lines = snapshot.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise BillingFactViolation("pricing snapshot has no lines")
    source_lines = [_mapping(value, "pricing line") for value in raw_lines]
    source_lines.sort(
        key=lambda line: _text(line.get("line_id"), "line_id", maximum=100)
    )
    line_ids = [
        _text(line.get("line_id"), "line_id", maximum=100) for line in source_lines
    ]
    if len(set(line_ids)) != len(line_ids):
        raise BillingFactViolation("pricing snapshot has duplicate line ids")
    amounts: list[int] = []
    copied: list[dict[str, object]] = []
    for line in source_lines:
        line_id = _text(line.get("line_id"), "line_id", maximum=100)
        offer_id = _text(line.get("offer_id"), "line offer_id", maximum=100)
        kind = _text(line.get("kind"), "line kind", maximum=30)
        if kind not in _LINE_KINDS:
            raise BillingFactViolation("unknown line kind")
        quantity = _integer(line.get("quantity"), "line quantity", minimum=1)
        unit_amount = _integer(line.get("unit_amount_cents"), "line unit amount")
        amount = _integer(line.get("line_amount_cents"), "line amount")
        grant = _integer(line.get(_CREDIT_FIELD), "line credit amount")
        if amount != quantity * unit_amount:
            raise BillingFactViolation("line price equation does not hold")
        if kind in {"additional_clinic", "tax"} and grant != 0:
            raise BillingFactViolation("non-credit line grants credits")
        amounts.append(amount)
        copied.append(
            {
                "line_id": line_id,
                "offer_id": offer_id,
                "kind": kind,
                "quantity": quantity,
                "unit_amount_cents": unit_amount,
                "line_amount_cents": amount,
                _CREDIT_FIELD: grant,
            }
        )
    total_amount = sum(amounts)
    settled = _allocate(settled_cents, amounts)
    # The provider refund/readback is payment-level.  For multiple lines it
    # proves only a whole aggregate refund/restoration, never a line split.
    reversed_values = _allocate(reversed_cents, amounts)
    restored = _allocate(restored_cents, amounts)
    if len(amounts) > 1 and settled_cents not in (0, total_amount):
        raise AmbiguousLineAllocation(
            "provider aggregate cannot identify multi-line settlement"
        )
    if len(amounts) > 1 and reversed_cents not in (0, settled_cents):
        raise AmbiguousLineAllocation(
            "provider aggregate cannot identify multi-line refund"
        )
    if len(amounts) > 1 and restored_cents not in (0, reversed_cents):
        raise AmbiguousLineAllocation(
            "provider aggregate cannot identify multi-line restoration"
        )
    for index, line in enumerate(copied):
        line_settled = settled[index]
        line_reversed = reversed_values[index]
        line_restored = restored[index]
        if not 0 <= line_restored <= line_reversed <= line_settled <= amounts[index]:
            raise BillingFactViolation("line cumulative totals are invalid")
        line["settled_cents"] = line_settled
        line["reversed_cents"] = line_reversed
        line["restored_cents"] = line_restored
        line["financial_state"] = _line_state(
            settled=line_settled,
            reversed_cents=line_reversed,
            restored=line_restored,
        )
    return copied


def _validate_offer_snapshot(
    snapshot: Mapping[str, object], *, offer_id: str, catalog_version_id: str
) -> None:
    """Fail closed on stale or malformed persisted catalog credit amounts."""

    if catalog_version_id != CATALOG_VERSION_ID:
        raise BillingFactViolation("unsupported billing catalog snapshot")
    offer = BILLING_CATALOG.get(offer_id)
    if offer is None or snapshot.get("offer_id") != offer_id:
        raise BillingFactViolation("pricing snapshot offer is not in the catalog")
    if snapshot.get("catalog_version_id") != catalog_version_id:
        raise BillingFactViolation("pricing snapshot catalog version mismatch")
    expected_grant = offer.credits * 1_000
    if snapshot.get(_CREDIT_FIELD) != expected_grant:
        raise BillingFactViolation("pricing snapshot credit amount is stale")
    allowed_keys = (
        {
            "catalog_version_id",
            "offer_id",
            "amount_cents",
            _CREDIT_FIELD,
            "payment_method",
            "lines",
        }
        if offer.cycle == "DETACHED"
        else {
            "catalog_version_id",
            "offer_id",
            "amount_cents",
            _CREDIT_FIELD,
            "included_clinics",
            "additional_clinics",
            "billing_cycle",
            "payment_method",
            "lines",
            "coupon_code",
        }
    )
    required_keys = allowed_keys - {"coupon_code", "additional_clinics"}
    if not required_keys.issubset(snapshot) or set(snapshot) - allowed_keys:
        raise BillingFactViolation("pricing snapshot fields are not closed")
    raw_lines = snapshot.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise BillingFactViolation("pricing snapshot has no lines")
    lines = [_mapping(item, "pricing line") for item in raw_lines]
    kinds = [line.get("kind") for line in lines]
    grants = [line.get(_CREDIT_FIELD) for line in lines]
    line_amounts = [
        _integer(line.get("line_amount_cents"), "line amount") for line in lines
    ]
    amount = _integer(snapshot.get("amount_cents"), "snapshot amount", minimum=1)
    if any(line.get("offer_id") != offer_id for line in lines):
        raise BillingFactViolation("pricing line offer differs from catalog offer")
    if (
        any(
            line_amount
            != _integer(line.get("quantity"), "line quantity", minimum=1)
            * _integer(line.get("unit_amount_cents"), "line unit amount")
            for line, line_amount in zip(lines, line_amounts)
        )
        or sum(line_amounts) != amount
    ):
        raise BillingFactViolation("pricing line totals differ from snapshot amount")
    if any(
        line.get("settled_cents") != 0
        or line.get("reversed_cents") != 0
        or line.get("restored_cents") != 0
        or line.get("line_financial_state") != "pending"
        for line in lines
    ):
        raise BillingFactViolation("pricing snapshot has non-pending line state")
    if "DETACHED" == offer.cycle:
        if (
            len(lines) != 1
            or kinds != ["topup"]
            or grants != [expected_grant]
            or lines[0].get("offer_id") != offer_id
            or lines[0].get("unit_amount_cents") != offer.amount_cents
            or lines[0].get("quantity") != 1
            or amount != offer.amount_cents
            or snapshot.get("payment_method") not in {"credit_card", "pix"}
        ):
            raise BillingFactViolation("top-up pricing snapshot differs from catalog")
    else:
        plan_lines = [line for line in lines if line.get("kind") == "plan"]
        additional_lines = [
            line for line in lines if line.get("kind") == "additional_clinic"
        ]
        if (
            len(plan_lines) != 1
            or len(additional_lines) > 1
            or len(plan_lines) + len(additional_lines) != len(lines)
            or plan_lines[0].get("offer_id") != offer_id
            or plan_lines[0].get("quantity") != 1
            or plan_lines[0].get(_CREDIT_FIELD) != expected_grant
            or snapshot.get("included_clinics") != offer.included_clinics
            or snapshot.get("credit_grant_mc") != expected_grant
            or snapshot.get("billing_cycle") != "monthly"
            or snapshot.get("payment_method") != "credit_card"
        ):
            raise BillingFactViolation(
                "subscription pricing snapshot differs from catalog"
            )
        additional_quantity = _integer(
            snapshot.get("additional_clinics", 0), "additional clinic quantity"
        )
        gross = (
            offer.amount_cents
            + additional_quantity * offer.additional_clinic_amount_cents
        )
        matching_discount = next(
            (
                percent
                for percent in range(101)
                if offer.amount_cents - offer.amount_cents * percent // 100
                == plan_lines[0].get("unit_amount_cents")
                and (
                    not additional_lines
                    or offer.additional_clinic_amount_cents
                    - offer.additional_clinic_amount_cents * percent // 100
                    == additional_lines[0].get("unit_amount_cents")
                )
            ),
            None,
        )
        coupon_code = snapshot.get("coupon_code")
        if (
            matching_discount is None
            or (coupon_code is None and matching_discount != 0)
            or (
                coupon_code is not None
                and (not isinstance(coupon_code, str) or not coupon_code)
            )
        ):
            raise BillingFactViolation("subscription discount does not match catalog")
        if (additional_quantity > 0) != bool(additional_lines):
            raise BillingFactViolation("subscription add-on quantity is inconsistent")
        expected_total = gross - gross * matching_discount // 100
        if amount != expected_total:
            raise BillingFactViolation(
                "subscription discount total differs from catalog"
            )
        if additional_lines:
            addon = additional_lines[0]
            if (
                addon.get("offer_id") != offer_id
                or addon.get(_CREDIT_FIELD) != 0
                or addon.get("quantity") != snapshot.get("additional_clinics")
            ):
                raise BillingFactViolation(
                    "subscription add-on snapshot differs from catalog"
                )


def validate_fact(value: Mapping[str, object]) -> None:
    """Validate all semantic equations not expressible in JSON Schema."""

    _reject_pii(value)
    aggregate_kind = value.get("aggregate_kind")
    if aggregate_kind not in {"subscription_cycle", "topup_order"}:
        raise BillingFactViolation("invalid aggregate kind")
    expected_fields = set(_FACT_ROOT)
    if aggregate_kind == "subscription_cycle":
        expected_fields.add("subscription_cycle")
    if set(value) != expected_fields:
        raise BillingFactViolation("fact fields are not closed")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("producer") != PRODUCER
    ):
        raise BillingFactViolation("invalid producer envelope")
    _require_uuid(value.get("event_id"), "event_id")
    _require_uuid(value.get("builder_id"), "builder_id")
    _require_uuid(value.get("aggregate_id"), "aggregate_id")
    subscription_id = value.get("subscription_id")
    order_id = value.get("order_id")
    if aggregate_kind == "subscription_cycle":
        _require_uuid(subscription_id, "subscription_id")
        if order_id is not None:
            raise BillingFactViolation("subscription fact has an order id")
    else:
        if subscription_id is not None:
            raise BillingFactViolation("top-up fact has a subscription id")
        _require_uuid(order_id, "order_id")
        if value.get("aggregate_id") != order_id:
            raise BillingFactViolation("top-up aggregate id must equal order id")
    _fact_datetime(value.get("occurred_at"), "occurred_at")
    _fact_datetime(value.get("produced_at"), "produced_at")
    paid_at_value = value.get("paid_at")
    paid_at = None if paid_at_value is None else _fact_date(paid_at_value, "paid_at")
    event_type = value.get("event_type")
    expected_prefix = (
        "subscription.cycle_"
        if aggregate_kind == "subscription_cycle"
        else "topup.payment_"
    )
    if (
        not isinstance(event_type, str)
        or event_type not in _EVENT_TYPES.values()
        or not event_type.startswith(expected_prefix)
    ):
        raise BillingFactViolation("event type does not match aggregate")
    _text(value.get("payment_id"), "payment_id", maximum=160)
    _text(value.get("catalog_version_id"), "catalog_version_id", maximum=80)
    _text(value.get("offer_id"), "offer_id", maximum=100)
    if value.get("currency") != "BRL":
        raise BillingFactViolation("invalid currency")
    revision = _integer(
        value.get("financial_revision"), "financial revision", minimum=1
    )
    if revision <= 0:
        raise BillingFactViolation("financial revision must be positive")
    amount = _integer(value.get("total_amount_cents"), "total amount", minimum=1)
    settled = _integer(value.get("settled_cents"), "settled")
    reversed_cents = _integer(value.get("reversed_cents"), "reversed")
    restored = _integer(value.get("restored_cents"), "restored")
    grant = _integer(value.get(_CREDIT_FIELD), "credit amount")
    if (settled > 0) != (paid_at is not None):
        raise BillingFactViolation("paid_at must match settlement presence")
    if not 0 <= restored <= reversed_cents <= settled <= amount:
        raise BillingFactViolation("fact cumulative totals are invalid")
    raw_lines = value.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise BillingFactViolation("invalid lines")
    lines = [_mapping(line, "fact line") for line in raw_lines]
    ids: list[str] = []
    total_amount = total_settled = total_reversed = total_restored = total_grant = 0
    for line in lines:
        if set(line) != _LINE_ROOT:
            raise BillingFactViolation("line fields are not closed")
        line_id = _text(line.get("line_id"), "line_id", maximum=100)
        ids.append(line_id)
        _text(line.get("offer_id"), "line offer_id", maximum=100)
        if line.get("offer_id") != value.get("offer_id"):
            raise BillingFactViolation("line offer differs from fact offer")
        kind = line.get("kind")
        if kind not in _LINE_KINDS:
            raise BillingFactViolation("invalid line kind")
        quantity = _integer(line.get("quantity"), "line quantity", minimum=1)
        unit_amount = _integer(line.get("unit_amount_cents"), "line unit")
        line_amount = _integer(line.get("line_amount_cents"), "line amount")
        line_grant = _integer(line.get(_CREDIT_FIELD), "line credit amount")
        line_settled = _integer(line.get("settled_cents"), "line settled")
        line_reversed = _integer(line.get("reversed_cents"), "line reversed")
        line_restored = _integer(line.get("restored_cents"), "line restored")
        if line_amount != quantity * unit_amount:
            raise BillingFactViolation("line price equation does not hold")
        if not 0 <= line_restored <= line_reversed <= line_settled <= line_amount:
            raise BillingFactViolation("line cumulative totals are invalid")
        if kind in {"additional_clinic", "tax"} and line_grant != 0:
            raise BillingFactViolation("non-credit line grants credits")
        if line.get("financial_state") not in _LINE_STATES:
            raise BillingFactViolation("invalid line financial state")
        expected_line_state = _line_state(
            settled=line_settled,
            reversed_cents=line_reversed,
            restored=line_restored,
        )
        if line.get("financial_state") != expected_line_state:
            raise BillingFactViolation("line financial state disagrees with totals")
        if (
            line_grant > 0
            and line_settled > 0
            and (paid_at is None or line_settled != line_amount)
        ):
            raise BillingFactViolation("credit grant requires full paid settlement")
        total_amount += line_amount
        total_settled += line_settled
        total_reversed += line_reversed
        total_restored += line_restored
        total_grant += line_grant
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise BillingFactViolation("lines are not deterministically ordered")
    if (total_amount, total_settled, total_reversed, total_restored, total_grant) != (
        amount,
        settled,
        reversed_cents,
        restored,
        grant,
    ):
        raise BillingFactViolation("fact totals do not equal lines")
    if aggregate_kind == "subscription_cycle":
        cycle = _mapping(value.get("subscription_cycle"), "subscription cycle")
        if set(cycle) != {"cycle_start", "cycle_end"}:
            raise BillingFactViolation("subscription cycle fields are not closed")
        if _fact_date(cycle.get("cycle_start"), "cycle_start") >= _fact_date(
            cycle.get("cycle_end"), "cycle_end"
        ):
            raise BillingFactViolation("subscription cycle is invalid")


def _next_month(value: date) -> date:
    month = 1 if value.month == 12 else value.month + 1
    year = value.year + 1 if value.month == 12 else value.year
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _aggregate_state(*, settled: int, reversed_cents: int, restored: int) -> str:
    return _line_state(
        settled=settled, reversed_cents=reversed_cents, restored=restored
    )


class BillingFactProducer:
    """Build and persist one fact, its retry row, and its bundle handoff atomically."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        now: Callable[[], datetime] | None = None,
        bundle_digest: str | None = None,
        deployment_digest: str = _DEFAULT_DEPLOYMENT_DIGEST,
    ) -> None:
        self._db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._bundle_digest = bundle_digest
        self._deployment_digest = self._valid_digest(
            deployment_digest, "deployment digest"
        )

    @staticmethod
    def _valid_digest(value: str, field: str) -> str:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise BillingFactViolation(f"invalid {field}")
        return value

    async def record_effect(
        self,
        effect: BillingPaymentEffect,
        projection: BillingPaymentProjection,
        *,
        cycle_start: date | None = None,
    ) -> BillingOutboxEvent | None:
        """Persist a new cumulative snapshot or return the prior replay result.

        The caller owns the surrounding webhook transaction.  The effect has
        already been inserted under its idempotency key; this method inserts
        the outbox, delivery, and handoff before that transaction can commit.
        """

        existing = await self._db.scalar(
            select(BillingOutboxEvent)
            .where(BillingOutboxEvent.payment_effect_id == effect.id)
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            return existing
        if projection.settled_cents > 0 and projection.paid_at is None:
            raise BillingFactViolation("settled projection lacks provider payment date")
        if projection.aggregate_kind == "topup":
            return await self._record_topup(effect, projection)
        if projection.aggregate_kind == "subscription":
            return await self._record_subscription(
                effect, projection, cycle_start=cycle_start
            )
        raise BillingFactViolation("unknown payment aggregate")

    async def _record_topup(
        self, effect: BillingPaymentEffect, projection: BillingPaymentProjection
    ) -> BillingOutboxEvent | None:
        if projection.topup_order_id is None:
            raise BillingFactViolation("top-up projection is missing its order")
        order = await self._db.scalar(
            select(BillingTopupOrder)
            .where(BillingTopupOrder.id == projection.topup_order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None or order.builder_id != projection.builder_id:
            raise BillingFactViolation("top-up aggregate is unavailable")
        snapshot = _mapping(order.pricing_snapshot, "top-up pricing snapshot")
        _validate_offer_snapshot(
            snapshot,
            offer_id=order.offer_id,
            catalog_version_id=order.catalog_version_id,
        )
        if _integer(snapshot.get("amount_cents"), "snapshot amount", minimum=1) != int(
            order.amount_cents
        ) or _integer(snapshot.get(_CREDIT_FIELD), "snapshot credit amount") != int(
            order.credit_grant_mc
        ):
            raise BillingFactViolation(
                "top-up aggregate differs from its pricing snapshot"
            )
        try:
            lines = _copy_lines(
                snapshot,
                settled_cents=int(projection.settled_cents),
                reversed_cents=int(projection.reversed_cents),
                restored_cents=int(projection.restored_cents),
            )
            if effect.effect_kind == "settlement":
                self._reject_partial_credit_settlement(lines)
        except PartialCreditLine:
            order.state = "quarantined"
            order.financial_snapshot = self._financial_snapshot(
                lines, projection=projection
            )
            await self._append_alert(
                kind="partial_credit_settlement",
                outbox_event_id=None,
                detail=f"effect:{effect.id}",
            )
            await self._db.flush()
            return None
        except AmbiguousLineAllocation:
            order.state = "quarantined"
            order.settled_cents = int(projection.settled_cents)
            order.reversed_cents = int(projection.reversed_cents)
            order.restored_cents = int(projection.restored_cents)
            await self._append_alert(
                kind="ambiguous_line_allocation",
                outbox_event_id=None,
                detail=f"effect:{effect.id}",
            )
            await self._db.flush()
            return None
        revision = int(order.financial_revision) + 1
        order.financial_revision = revision
        order.settled_cents = int(projection.settled_cents)
        order.reversed_cents = int(projection.reversed_cents)
        order.restored_cents = int(projection.restored_cents)
        order.financial_snapshot = self._financial_snapshot(
            lines, projection=projection
        )
        order.state = self._topup_state(projection)
        return await self._persist_fact(
            effect=effect,
            projection=projection,
            aggregate_kind="topup_order",
            aggregate_id=order.id,
            subscription_id=None,
            topup_order_id=order.id,
            subscription_cycle_id=None,
            catalog_version_id=order.catalog_version_id,
            offer_id=order.offer_id,
            total_amount_cents=int(order.amount_cents),
            lines=lines,
            revision=revision,
            subscription_cycle=None,
            paid_at=projection.paid_at,
        )

    async def _record_subscription(
        self,
        effect: BillingPaymentEffect,
        projection: BillingPaymentProjection,
        *,
        cycle_start: date | None,
    ) -> BillingOutboxEvent | None:
        if projection.subscription_id is None:
            raise BillingFactViolation(
                "subscription projection is missing its subscription"
            )
        cycle = await self._cycle_for_projection(projection, cycle_start=cycle_start)
        snapshot = _mapping(cycle.pricing_snapshot, "subscription pricing snapshot")
        _validate_offer_snapshot(
            snapshot,
            offer_id=cycle.offer_id,
            catalog_version_id=cycle.catalog_version_id,
        )
        if _integer(snapshot.get("amount_cents"), "snapshot amount", minimum=1) != int(
            cycle.amount_cents
        ) or _integer(snapshot.get(_CREDIT_FIELD), "snapshot credit amount") != int(
            cycle.credit_grant_mc
        ):
            raise BillingFactViolation(
                "subscription cycle differs from its pricing snapshot"
            )
        try:
            lines = _copy_lines(
                snapshot,
                settled_cents=int(projection.settled_cents),
                reversed_cents=int(projection.reversed_cents),
                restored_cents=int(projection.restored_cents),
            )
            if effect.effect_kind == "settlement":
                self._reject_partial_credit_settlement(lines)
        except PartialCreditLine:
            cycle.state = "quarantined"
            cycle.financial_snapshot = self._financial_snapshot(
                lines, projection=projection
            )
            await self._append_alert(
                kind="partial_credit_settlement",
                outbox_event_id=None,
                detail=f"effect:{effect.id}",
            )
            await self._db.flush()
            return None
        except AmbiguousLineAllocation:
            cycle.state = "quarantined"
            cycle.settled_cents = int(projection.settled_cents)
            cycle.reversed_cents = int(projection.reversed_cents)
            cycle.restored_cents = int(projection.restored_cents)
            await self._append_alert(
                kind="ambiguous_line_allocation",
                outbox_event_id=None,
                detail=f"effect:{effect.id}",
            )
            await self._db.flush()
            return None
        revision = int(cycle.financial_revision) + 1
        cycle.financial_revision = revision
        cycle.settled_cents = int(projection.settled_cents)
        cycle.reversed_cents = int(projection.reversed_cents)
        cycle.restored_cents = int(projection.restored_cents)
        cycle.financial_snapshot = self._financial_snapshot(
            lines, projection=projection
        )
        cycle.state = _aggregate_state(
            settled=cycle.settled_cents,
            reversed_cents=cycle.reversed_cents,
            restored=cycle.restored_cents,
        )
        return await self._persist_fact(
            effect=effect,
            projection=projection,
            aggregate_kind="subscription_cycle",
            aggregate_id=cycle.id,
            subscription_id=cycle.subscription_id,
            topup_order_id=None,
            subscription_cycle_id=cycle.id,
            catalog_version_id=cycle.catalog_version_id,
            offer_id=cycle.offer_id,
            total_amount_cents=int(cycle.amount_cents),
            lines=lines,
            revision=revision,
            subscription_cycle={
                "cycle_start": cycle.cycle_start.isoformat(),
                "cycle_end": cycle.cycle_end.isoformat(),
            },
            paid_at=projection.paid_at,
        )

    async def _cycle_for_projection(
        self,
        projection: BillingPaymentProjection,
        *,
        cycle_start: date | None,
    ) -> BillingSubscriptionCycle:
        if projection.subscription_cycle_id is not None:
            cycle = await self._db.scalar(
                select(BillingSubscriptionCycle)
                .where(BillingSubscriptionCycle.id == projection.subscription_cycle_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if cycle is None:
                raise BillingFactViolation("subscription cycle is unavailable")
            return cycle
        existing = await self._db.scalar(
            select(BillingSubscriptionCycle)
            .where(
                BillingSubscriptionCycle.provider == projection.provider,
                BillingSubscriptionCycle.payment_id == projection.payment_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            projection.subscription_cycle_id = existing.id
            return existing
        if projection.checkout_session_id is None or projection.subscription_id is None:
            raise BillingFactViolation(
                "subscription cycle lacks an originating snapshot"
            )
        checkout = await self._db.scalar(
            select(BillingCheckoutSession)
            .where(BillingCheckoutSession.id == projection.checkout_session_id)
            .execution_options(populate_existing=True)
        )
        if checkout is None or checkout.builder_id != projection.builder_id:
            raise BillingFactViolation("subscription pricing snapshot is unavailable")
        start = cycle_start or _utc(projection.provider_created_at).date()
        cycle_values: dict[str, object] = {
            "id": uuid4(),
            "builder_id": projection.builder_id,
            "subscription_id": projection.subscription_id,
            "provider": projection.provider,
            "payment_id": projection.payment_id,
            "catalog_version_id": checkout.catalog_version_id,
            "offer_id": checkout.offer_id,
            "cycle_start": start,
            "cycle_end": _next_month(start),
            "amount_cents": int(projection.amount_cents),
            "pricing_snapshot": dict(checkout.pricing_snapshot),
            "financial_snapshot": {},
        }
        cycle_values[_CREDIT_FIELD] = _integer(
            _mapping(checkout.pricing_snapshot, "subscription pricing snapshot").get(
                _CREDIT_FIELD
            ),
            "subscription credit amount",
        )
        cycle = BillingSubscriptionCycle(**cycle_values)
        self._db.add(cycle)
        await self._db.flush()
        projection.subscription_cycle_id = cycle.id
        return cycle

    @staticmethod
    def _reject_partial_credit_settlement(lines: list[dict[str, object]]) -> None:
        for line in lines:
            amount = _integer(line["line_amount_cents"], "line amount")
            settled = _integer(line["settled_cents"], "line settled")
            grant = _integer(line[_CREDIT_FIELD], "line credit amount")
            if grant > 0 and 0 < settled < amount:
                raise PartialCreditLine("partial settlement of credit-bearing line")

    @staticmethod
    def _financial_snapshot(
        lines: list[dict[str, object]], *, projection: BillingPaymentProjection
    ) -> dict[str, object]:
        return {
            "total_amount_cents": int(projection.amount_cents),
            "settled_cents": int(projection.settled_cents),
            "reversed_cents": int(projection.reversed_cents),
            "restored_cents": int(projection.restored_cents),
            _CREDIT_FIELD: sum(
                _integer(line[_CREDIT_FIELD], "line credit amount") for line in lines
            ),
            "lines": lines,
        }

    @staticmethod
    def _topup_state(projection: BillingPaymentProjection) -> str:
        settled = int(projection.settled_cents)
        reversed_cents = int(projection.reversed_cents)
        restored = int(projection.restored_cents)
        if restored > 0:
            return "restored"
        if reversed_cents == settled and reversed_cents > 0:
            return "reversed"
        if reversed_cents > 0:
            return "partially_reversed"
        return "settled" if settled else "pending"

    async def _persist_fact(
        self,
        *,
        effect: BillingPaymentEffect,
        projection: BillingPaymentProjection,
        aggregate_kind: str,
        aggregate_id: UUID,
        subscription_id: UUID | None,
        topup_order_id: UUID | None,
        subscription_cycle_id: UUID | None,
        catalog_version_id: str,
        offer_id: str,
        total_amount_cents: int,
        lines: list[dict[str, object]],
        revision: int,
        subscription_cycle: dict[str, object] | None,
        paid_at: date | None,
    ) -> BillingOutboxEvent:
        event_type = _EVENT_TYPES.get((aggregate_kind, effect.effect_kind))
        if event_type is None:
            raise BillingFactViolation("effect cannot produce a billing fact")
        event_id = uuid5(
            _EVENT_NAMESPACE, f"{aggregate_kind}:{aggregate_id}:{revision}"
        )
        produced_at = _utc(self._now())
        occurred_at = _utc(effect.created_at)
        fact: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(event_id),
            "event_type": event_type,
            "producer": PRODUCER,
            "occurred_at": _timestamp(occurred_at),
            "produced_at": _timestamp(produced_at),
            "builder_id": str(projection.builder_id),
            "aggregate_kind": aggregate_kind,
            "aggregate_id": str(aggregate_id),
            "subscription_id": str(subscription_id) if subscription_id else None,
            "order_id": str(topup_order_id) if topup_order_id else None,
            "payment_id": projection.payment_id,
            "paid_at": paid_at.isoformat() if paid_at is not None else None,
            "catalog_version_id": catalog_version_id,
            "offer_id": offer_id,
            "currency": "BRL",
            "financial_revision": revision,
            "total_amount_cents": total_amount_cents,
            "settled_cents": int(projection.settled_cents),
            "reversed_cents": int(projection.reversed_cents),
            "restored_cents": int(projection.restored_cents),
            _CREDIT_FIELD: sum(
                _integer(line[_CREDIT_FIELD], "line credit amount") for line in lines
            ),
            "lines": lines,
        }
        if subscription_cycle is not None:
            fact["subscription_cycle"] = subscription_cycle
        validate_fact(fact)
        bundle = self._valid_digest(
            self._bundle_digest or billing_fact_bundle_digest(), "bundle digest"
        )
        outbox = BillingOutboxEvent(
            id=event_id,
            builder_id=projection.builder_id,
            schema_version=SCHEMA_VERSION,
            event_type=event_type,
            aggregate_kind=aggregate_kind,
            aggregate_id=aggregate_id,
            subscription_id=subscription_id,
            topup_order_id=topup_order_id,
            subscription_cycle_id=subscription_cycle_id,
            payment_effect_id=effect.id,
            financial_revision=revision,
            payload=fact,
            payload_sha256=payload_digest(fact),
            bundle_digest=bundle,
            deployment_digest=self._deployment_digest,
        )
        delivery = BillingFactDelivery(
            id=uuid4(), outbox_event_id=outbox.id, event_id=event_id, state="pending"
        )
        handoff = BillingFactHandoff(
            id=uuid4(),
            outbox_event_id=outbox.id,
            delivery_id=delivery.id,
            schema_version=SCHEMA_VERSION,
            bundle_digest=bundle,
            deployment_digest=self._deployment_digest,
            handed_off_at=produced_at,
        )
        # No relationship() links these mappers, so one flush would let the
        # unit of work order them by class name (deliveries before the outbox)
        # and PostgreSQL refuses the non-deferrable foreign keys.  Insert the
        # parents explicitly: outbox, then delivery, then handoff.
        self._db.add(outbox)
        await self._db.flush()
        self._db.add(delivery)
        await self._db.flush()
        self._db.add(handoff)
        await self._db.flush()
        return outbox

    async def _append_alert(
        self, *, kind: str, outbox_event_id: UUID | None, detail: str
    ) -> None:
        await _append_alert_row(
            self._db,
            kind=kind,
            outbox_event_id=outbox_event_id,
            detail=detail,
            observed_at=_utc(self._now()),
        )


class BillingFactPublisher:
    """Re-present immutable rows until the consumer's append-only ack is read."""

    def __init__(self, db: AsyncSession, *, now: Callable[[], datetime] | None = None):
        self._db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def pending(self, *, limit: int = 100) -> tuple[PublishedFact, ...]:
        """Legacy unbounded read of presentable rows; see :meth:`publish_pending`."""

        require_presentation_allowed()
        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = await self._db.execute(
            select(BillingFactDelivery, BillingOutboxEvent)
            .join(
                BillingOutboxEvent,
                BillingOutboxEvent.id == BillingFactDelivery.outbox_event_id,
            )
            .where(BillingFactDelivery.state.in_(("pending", "awaiting_ack")))
            .order_by(BillingOutboxEvent.created_at, BillingOutboxEvent.id)
            .limit(limit)
        )
        return tuple(
            PublishedFact(
                outbox_event_id=outbox.id,
                event_id=delivery.event_id,
                payload=dict(outbox.payload),
            )
            for delivery, outbox in rows
        )

    async def publish_pending(
        self,
        transport: Callable[[dict[str, object]], Awaitable[None]],
        *,
        limit: int = 100,
    ) -> tuple[PublishedFact, ...]:
        """Legacy bulk send; caller owns commit policy.  NOT a delivery path.

        Warning: this bypasses everything :meth:`publish_next` enforces: no
        ``SKIP LOCKED`` claim, no backoff, no ``max_attempts`` cap, no ack
        re-read and no transport timeout.  It has no production caller and
        raises while ``PRESENTATION_PRECONDITIONS_OPEN`` is true.
        """

        require_presentation_allowed()
        published: list[PublishedFact] = []
        for item in await self.pending(limit=limit):
            try:
                await transport(item.payload)
            except Exception:
                await self._record_attempt(item.event_id, delivered=False)
            else:
                await self._record_attempt(item.event_id, delivered=True)
                published.append(item)
        return tuple(published)

    async def publish_next(
        self,
        transport: Callable[[dict[str, object]], Awaitable[None]],
        *,
        policy: FactRetryPolicy,
        timeout: float,
    ) -> PublishAttempt | None:
        """Lease, present and record at most one due fact, in two phases.

        **Phase 1** claims the oldest due delivery ``FOR UPDATE OF
        billing_fact_deliveries SKIP LOCKED``, re-reads its ack, and either
        blocks a spent delivery or counts the attempt (``attempt_count + 1``,
        ``last_attempt_at = now``) and COMMITS that before the consumer sees
        the fact.  The committed attempt is the lease: no row lock is held
        while the consumer runs, and no worker claims the fact again until
        its backoff has passed, at least ``policy.base_backoff``, which must
        exceed ``timeout`` (refused otherwise).  **Phase 2** re-locks the row
        and records the outcome (``awaiting_ack`` or ``pending``), then
        commits.  :class:`TransportNotReady` is no attempt: phase 2 gives the
        leased count back and keeps ``last_attempt_at``, so the fact is not
        due again before the backoff and the worker cannot spin on it.

        A crash, kill or cancellation between the phases leaves exactly the
        leased attempt counted and the previous state: the fact is presented
        again after the backoff and ``max_attempts`` still bounds it (a
        replayed fact is a no-op for the consumer).  Phase 2 writes nothing
        when the delivery became terminal or the lease is no longer this
        call's (a presentation that outlived it was re-leased elsewhere).

        This method owns both commits, so ``db`` must have no transaction
        open.  A fact whose ack was committed before the post-lock re-read
        below is not presented (the reconciler converges it); an ack
        committed later cannot stop the presentation in flight.  When
        ``policy`` is spent the delivery becomes ``blocked`` with alert
        evidence and no presentation; the fact is retained and a later valid
        ack still converges it.  ``None`` means nothing is due.  Wall-clock
        skew between workers shortens the lease by the same amount.
        """

        require_presentation_allowed()
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if timedelta(seconds=timeout) >= policy.base_backoff:
            raise ValueError("timeout must be shorter than the retry base backoff")
        if self._db.in_transaction():
            raise RuntimeError(
                "publish_next commits its own phases: call it with no open "
                "transaction"
            )
        lease = await self._lease_next(policy)
        if not isinstance(lease, _Lease):
            return lease
        failure: str | None = None
        try:
            await asyncio.wait_for(transport(lease.payload), timeout)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                # A real cancellation of this task: the leased attempt stays
                # counted, so the fact is presented again after the backoff.
                raise
            # A stray CancelledError raised by the consumer code itself is a
            # failed presentation, so the backoff and the cap still apply.
            failure = "CancelledError"
        except TransportNotReady:
            return await self._record_outcome(lease, outcome="not_ready")
        except Exception as exc:
            failure = type(exc).__name__
        return await self._record_outcome(
            lease, outcome="presented" if failure is None else "failed", failure=failure
        )

    async def _lease_next(
        self, policy: FactRetryPolicy
    ) -> PublishAttempt | _Lease | None:
        """Phase 1: claim one due delivery and commit its attempt (the lease)."""

        row = (
            await self._db.execute(self._due_claim(policy, _utc(self._now())))
        ).first()
        if row is None:
            await self._db.rollback()
            return None
        delivery, outbox = row
        # Fresh statement, fresh READ COMMITTED snapshot: an ack committed
        # after the claim query started still prevents a re-presentation.
        if (
            await self._db.scalar(
                select(BillingFactAck.id).where(
                    BillingFactAck.outbox_event_id == outbox.id
                )
            )
            is not None
        ):
            skipped = PublishAttempt(
                delivery.event_id, "skipped", int(delivery.attempt_count)
            )
            await self._db.rollback()  # nothing to write; releases the lock
            return skipped
        if delivery.attempt_count >= policy.max_attempts:
            delivery.state = "blocked"
            await _append_alert_row(
                self._db,
                kind="backlog",
                outbox_event_id=outbox.id,
                detail=RETRY_EXHAUSTED_DETAIL,
                observed_at=_utc(self._now()),
            )
            exhausted = PublishAttempt(
                delivery.event_id, "exhausted", int(delivery.attempt_count)
            )
            await self._db.commit()
            return exhausted
        lease = _Lease(
            event_id=delivery.event_id,
            attempt_count=int(delivery.attempt_count) + 1,
            leased_at=_utc(self._now()),
            # A deep copy: a consumer can never alias the persisted payload.
            payload=copy.deepcopy(outbox.payload),
        )
        delivery.attempt_count = lease.attempt_count
        delivery.last_attempt_at = lease.leased_at
        await self._db.commit()  # counted BEFORE the consumer can see it
        return lease

    async def _record_outcome(
        self,
        lease: _Lease,
        *,
        outcome: Literal["presented", "failed", "not_ready"],
        failure: str | None = None,
    ) -> PublishAttempt:
        """Phase 2: record the presentation's outcome on the leased row."""

        delivery = await self._db.scalar(
            select(BillingFactDelivery)
            .where(BillingFactDelivery.event_id == lease.event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        attempt_count = lease.attempt_count
        if (
            delivery is not None
            and delivery.state in _PRESENTABLE
            and delivery.attempt_count == lease.attempt_count
            and delivery.last_attempt_at is not None
            and _utc(delivery.last_attempt_at) == lease.leased_at
        ):
            if outcome == "not_ready":
                # Not an attempt: give the count back, keep last_attempt_at
                # (and the state) so the fact waits its backoff again.
                delivery.attempt_count = lease.attempt_count - 1
            else:
                delivery.state = "awaiting_ack" if outcome == "presented" else "pending"
            attempt_count = int(delivery.attempt_count)
        await self._db.commit()
        return PublishAttempt(lease.event_id, outcome, attempt_count, failure)

    @staticmethod
    def _due_claim(
        policy: FactRetryPolicy, now: datetime
    ) -> Select[tuple[BillingFactDelivery, BillingOutboxEvent]]:
        """One oldest presentable, un-acked, due delivery, locked SKIP LOCKED.

        Backoff is a closed ``CASE`` over ``attempt_count`` with bound cutoffs,
        so PostgreSQL re-checks it against the latest row version when a
        concurrent worker committed an attempt between snapshot and lock.
        Only a delivery without ``last_attempt_at`` (never tried) is due at
        once: a count of 0 with ``last_attempt_at`` set (a not-ready attempt
        given back) waits ``base_backoff``, as after a first presentation.
        """

        def cutoff(wait: timedelta) -> BindParameter[datetime]:
            return literal(now - wait, DateTime(timezone=True))

        due_before = case(
            {
                0: cutoff(policy.base_backoff),
                **{
                    attempts: cutoff(policy.backoff(attempts))
                    for attempts in range(1, policy.max_attempts + 1)
                },
            },
            value=BillingFactDelivery.attempt_count,
            else_=cutoff(policy.backoff(policy.max_attempts)),
        )
        return (
            select(BillingFactDelivery, BillingOutboxEvent)
            .join(
                BillingOutboxEvent,
                BillingOutboxEvent.id == BillingFactDelivery.outbox_event_id,
            )
            .where(
                BillingFactDelivery.state.in_(_PRESENTABLE),
                ~select(BillingFactAck.id)
                .where(
                    BillingFactAck.outbox_event_id
                    == BillingFactDelivery.outbox_event_id
                )
                .exists(),
                or_(
                    BillingFactDelivery.last_attempt_at.is_(None),
                    BillingFactDelivery.last_attempt_at <= due_before,
                ),
            )
            .order_by(BillingOutboxEvent.created_at, BillingOutboxEvent.id)
            .limit(1)
            .with_for_update(of=BillingFactDelivery, skip_locked=True)
            .execution_options(populate_existing=True)
        )

    async def _record_attempt(self, event_id: UUID, *, delivered: bool) -> None:
        """Legacy single-phase record, after the send: ``publish_pending`` only."""

        delivery = await self._db.scalar(
            select(BillingFactDelivery)
            .where(BillingFactDelivery.event_id == event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if delivery is None or delivery.state in {"acknowledged", "blocked"}:
            return
        delivery.attempt_count += 1
        delivery.last_attempt_at = _utc(self._now())
        delivery.state = "awaiting_ack" if delivered else "pending"
        await self._db.flush()


class BillingFactReconciler:
    """Read consumer acknowledgements and retain evidence for every mismatch."""

    def __init__(self, db: AsyncSession, *, now: Callable[[], datetime] | None = None):
        self._db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def reconcile(self) -> ReconciliationResult:
        """Verify every fact ever produced (full proof pass, unbounded)."""

        tally: Counter[str] = Counter()
        await self._alert_missing_facts(tally, limit=None)
        outboxes = list(
            (
                await self._db.execute(
                    select(BillingOutboxEvent).order_by(
                        BillingOutboxEvent.created_at, BillingOutboxEvent.id
                    )
                )
            ).scalars()
        )
        for outbox in outboxes:
            delivery = await self._db.scalar(
                select(BillingFactDelivery)
                .where(BillingFactDelivery.outbox_event_id == outbox.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            tally[await self._reconcile_fact(outbox, delivery)] += 1
        await self._db.flush()
        return self._result(tally)

    async def reconcile_open(
        self, *, limit: int, after: UUID | None = None
    ) -> ReconciliationPage:
        """Verify one bounded page of the open (non-``acknowledged``) facts.

        ``acknowledged`` is terminal and its inputs (outbox, handoff, ack) are
        immutable in PostgreSQL, so only open facts can still change.  Pages
        are keyed by outbox id and wrap; a delivery a publisher is presenting
        right now is skipped (``SKIP LOCKED``) and revisited next cycle.
        """

        if limit <= 0:
            raise ValueError("limit must be positive")
        tally: Counter[str] = Counter()
        await self._alert_missing_facts(tally, limit=limit)
        orphaned = await self._db.execute(
            select(BillingOutboxEvent)
            .outerjoin(
                BillingFactDelivery,
                BillingFactDelivery.outbox_event_id == BillingOutboxEvent.id,
            )
            .where(BillingFactDelivery.id.is_(None))
            .order_by(BillingOutboxEvent.id)
            .limit(limit)
        )
        for outbox in list(orphaned.scalars()):
            tally[await self._reconcile_fact(outbox, None)] += 1
        page_query = (
            select(BillingFactDelivery.id, BillingOutboxEvent.id)
            .join(
                BillingOutboxEvent,
                BillingOutboxEvent.id == BillingFactDelivery.outbox_event_id,
            )
            .where(BillingFactDelivery.state != "acknowledged")
        )
        if after is not None:
            page_query = page_query.where(BillingOutboxEvent.id > after)
        page = list(
            (
                await self._db.execute(
                    page_query.order_by(BillingOutboxEvent.id).limit(limit)
                )
            ).all()
        )
        for delivery_id, outbox_id in page:
            delivery = await self._db.scalar(
                select(BillingFactDelivery)
                .where(
                    BillingFactDelivery.id == delivery_id,
                    BillingFactDelivery.state != "acknowledged",
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            fact = await self._db.get(
                BillingOutboxEvent, outbox_id, populate_existing=True
            )
            if delivery is None or fact is None:
                continue
            tally[await self._reconcile_fact(fact, delivery)] += 1
        await self._db.flush()
        return ReconciliationPage(
            result=self._result(tally),
            next_after=page[-1][1] if len(page) == limit else None,
        )

    async def escalate_overdue(self, *, stale_before: datetime, limit: int) -> int:
        """Append one durable ``ack overdue`` alert per fact unacked too long.

        Only facts without that alert are selected, so a bounded page always
        progresses through the whole overdue set; replays add nothing.  A
        fact that already has a valid ack is not overdue even before the
        reconciler's page reaches it and marks it ``acknowledged``.
        """

        if limit <= 0:
            raise ValueError("limit must be positive")
        overdue_digest = _detail_digest(ACK_OVERDUE_DETAIL)
        rows = await self._db.execute(
            select(BillingFactDelivery.outbox_event_id)
            .where(
                BillingFactDelivery.state.in_(_PRESENTABLE),
                BillingFactDelivery.created_at <= stale_before,
                ~_valid_ack_exists(),
                ~select(BillingFactReconciliationAlert.id)
                .where(
                    BillingFactReconciliationAlert.outbox_event_id
                    == BillingFactDelivery.outbox_event_id,
                    BillingFactReconciliationAlert.kind == "backlog",
                    BillingFactReconciliationAlert.detail_sha256 == overdue_digest,
                )
                .exists(),
            )
            .order_by(
                BillingFactDelivery.created_at, BillingFactDelivery.outbox_event_id
            )
            .limit(limit)
        )
        overdue = [outbox_event_id for (outbox_event_id,) in rows]
        for outbox_event_id in overdue:
            await self._alert(
                kind="backlog",
                outbox_event_id=outbox_event_id,
                detail=ACK_OVERDUE_DETAIL,
            )
        await self._db.flush()
        return len(overdue)

    async def backlog(self, *, stale_before: datetime) -> FactBacklog:
        """Read-only global counts; safe for every worker loop to compute.

        ``unacked``/``stale`` exclude open rows that already carry a valid ack
        (converged on the reconciler's next page), so an acknowledged fact
        never raises the stale alert.
        """

        open_state = BillingFactDelivery.state.in_(_PRESENTABLE) & ~_valid_ack_exists()
        unacked, stale, blocked = (
            await self._db.execute(
                select(
                    func.coalesce(func.sum(case((open_state, 1), else_=0)), 0),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    open_state
                                    & (BillingFactDelivery.created_at <= stale_before),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case((BillingFactDelivery.state == "blocked", 1), else_=0)
                        ),
                        0,
                    ),
                )
            )
        ).one()
        return FactBacklog(unacked=int(unacked), stale=int(stale), blocked=int(blocked))

    async def _alert_missing_facts(
        self, tally: Counter[str], *, limit: int | None
    ) -> None:
        query = (
            select(BillingPaymentEffect.id)
            .outerjoin(
                BillingOutboxEvent,
                BillingOutboxEvent.payment_effect_id == BillingPaymentEffect.id,
            )
            .where(
                BillingPaymentEffect.provider == "asaas",
                BillingOutboxEvent.id.is_(None),
            )
        )
        if limit is not None:
            # Known limit: a gap stays a gap until repaired, so a bounded run
            # re-alerts the same first page and gaps beyond it are not
            # alerted until earlier ones are resolved (the full ``reconcile``
            # pass sees all of them).  The orphan page below has the same
            # limit.  See docs/runbooks/asaas-billing-reconciliation.md.
            query = query.order_by(BillingPaymentEffect.id).limit(limit)
        for (effect_id,) in await self._db.execute(query):
            tally["gap"] += 1
            await self._alert(
                kind="gap", outbox_event_id=None, detail=f"effect:{effect_id}"
            )

    async def _reconcile_fact(
        self, outbox: BillingOutboxEvent, delivery: BillingFactDelivery | None
    ) -> str:
        """Verify one fact against its lineage and ack; return the tally key."""

        handoff = await self._db.scalar(
            select(BillingFactHandoff)
            .where(BillingFactHandoff.outbox_event_id == outbox.id)
            .execution_options(populate_existing=True)
        )
        if delivery is None or handoff is None or outbox.payment_effect_id is None:
            await self._alert(
                kind="gap", outbox_event_id=outbox.id, detail="missing lineage"
            )
            return "gap"
        ack = await self._db.scalar(
            select(BillingFactAck)
            .where(BillingFactAck.outbox_event_id == outbox.id)
            .execution_options(populate_existing=True)
        )
        if ack is None:
            await self._alert(
                kind="backlog", outbox_event_id=outbox.id, detail=ACK_MISSING_DETAIL
            )
            return "backlog"
        if ack.schema_version != ACK_SCHEMA_VERSION:
            delivery.state = "blocked"
            await self._alert(
                kind="schema_mismatch",
                outbox_event_id=outbox.id,
                detail="ack schema",
            )
            return "schema_mismatch"
        if ack.event_id != delivery.event_id or ack.outcome == "rejected":
            delivery.state = "blocked"
            await self._alert(
                kind="collision", outbox_event_id=outbox.id, detail="ack collision"
            )
            return "collision"
        if (
            ack.fact_sha256 != outbox.payload_sha256
            or ack.bundle_digest != handoff.bundle_digest
            or ack.deployment_digest != handoff.deployment_digest
        ):
            delivery.state = "blocked"
            await self._alert(
                kind="digest_mismatch",
                outbox_event_id=outbox.id,
                detail="ack digest",
            )
            return "digest_mismatch"
        delivery.state = "acknowledged"
        delivery.acknowledged_at = ack.acknowledged_at
        return "acknowledged"

    @staticmethod
    def _result(tally: Counter[str]) -> ReconciliationResult:
        return ReconciliationResult(
            backlog=tally["backlog"],
            gaps=tally["gap"],
            collisions=tally["collision"],
            schema_mismatches=tally["schema_mismatch"],
            digest_mismatches=tally["digest_mismatch"],
        )

    async def _alert(
        self, *, kind: str, outbox_event_id: UUID | None, detail: str
    ) -> None:
        await _append_alert_row(
            self._db,
            kind=kind,
            outbox_event_id=outbox_event_id,
            detail=detail,
            observed_at=_utc(self._now()),
        )
