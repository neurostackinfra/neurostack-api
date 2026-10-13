"""Canonical, ledger-free publication of ASAAS financial facts.

This module has one authority: turn an already-persisted Group 3 payment
effect plus its cumulative cents into a closed ``billing.fact.v1`` envelope.
It neither grants entitlement nor interprets a projection status as money.
Every retry reuses the immutable outbox row and therefore its event id.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Final, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
        "payment_id",
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


class BillingFactViolation(ValueError):
    """A closed producer/consumer contract invariant was not met."""


class PartialCreditLine(BillingFactViolation):
    """A partial settlement would make a credit-bearing line ambiguous."""


@dataclass(frozen=True, slots=True)
class PublishedFact:
    """One immutable message that may safely be re-presented to a consumer."""

    outbox_event_id: UUID
    event_id: UUID
    payload: dict[str, object]


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
    """Hash the exact, versioned manifest bytes rather than a reconstructed list."""

    path = manifest or (
        Path(__file__).resolve().parents[2]
        / "tests/fixtures/contracts/billing_fact_v1/SHA256SUMS"
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
        raise BillingFactViolation(
            "billing fact schema is unavailable"
        ) from exc


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
        return date.fromisoformat(value)
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
    """Allocate a cumulative payment deterministically over ordered lines."""

    remaining = cumulative
    allocated: list[int] = []
    for amount in amounts:
        portion = min(remaining, amount)
        allocated.append(portion)
        remaining -= portion
    if remaining:
        raise BillingFactViolation("cumulative total exceeds line amounts")
    return allocated


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
    settled = _allocate(settled_cents, amounts)
    reversed_values = _allocate(reversed_cents, amounts)
    restored = _allocate(restored_cents, amounts)
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
    _fact_datetime(value.get("occurred_at"), "occurred_at")
    _fact_datetime(value.get("produced_at"), "produced_at")
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
        if line.get("financial_state") != _line_state(
            settled=line_settled,
            reversed_cents=line_reversed,
            restored=line_restored,
        ):
            raise BillingFactViolation("line financial state disagrees with totals")
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
        try:
            lines = _copy_lines(
                _mapping(order.pricing_snapshot, "top-up pricing snapshot"),
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
        try:
            lines = _copy_lines(
                _mapping(cycle.pricing_snapshot, "subscription pricing snapshot"),
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
            "payment_id": projection.payment_id,
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
        digest = hashlib.sha256(detail.encode("utf-8")).hexdigest()
        existing = await self._db.scalar(
            select(BillingFactReconciliationAlert.id).where(
                BillingFactReconciliationAlert.outbox_event_id == outbox_event_id,
                BillingFactReconciliationAlert.kind == kind,
                BillingFactReconciliationAlert.detail_sha256 == digest,
            )
        )
        if existing is None:
            self._db.add(
                BillingFactReconciliationAlert(
                    id=uuid4(),
                    outbox_event_id=outbox_event_id,
                    kind=kind,
                    detail_sha256=digest,
                    observed_at=_utc(self._now()),
                )
            )


class BillingFactPublisher:
    """Re-present immutable rows until the consumer's append-only ack is read."""

    def __init__(self, db: AsyncSession, *, now: Callable[[], datetime] | None = None):
        self._db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def pending(self, *, limit: int = 100) -> tuple[PublishedFact, ...]:
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
        """Send rows without inventing new event ids; caller owns commit policy."""

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

    async def _record_attempt(self, event_id: UUID, *, delivered: bool) -> None:
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
        backlog = gaps = collisions = schema_mismatches = digest_mismatches = 0
        missing_effect_rows = await self._db.execute(
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
        for (effect_id,) in missing_effect_rows:
            gaps += 1
            await self._alert(
                kind="gap", outbox_event_id=None, detail=f"effect:{effect_id}"
            )
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
            handoff = await self._db.scalar(
                select(BillingFactHandoff)
                .where(BillingFactHandoff.outbox_event_id == outbox.id)
                .execution_options(populate_existing=True)
            )
            if delivery is None or handoff is None or outbox.payment_effect_id is None:
                gaps += 1
                await self._alert(
                    kind="gap", outbox_event_id=outbox.id, detail="missing lineage"
                )
                continue
            ack = await self._db.scalar(
                select(BillingFactAck)
                .where(BillingFactAck.outbox_event_id == outbox.id)
                .execution_options(populate_existing=True)
            )
            if ack is None:
                backlog += 1
                await self._alert(
                    kind="backlog", outbox_event_id=outbox.id, detail="ack missing"
                )
                continue
            if ack.schema_version != ACK_SCHEMA_VERSION:
                schema_mismatches += 1
                delivery.state = "blocked"
                await self._alert(
                    kind="schema_mismatch",
                    outbox_event_id=outbox.id,
                    detail="ack schema",
                )
                continue
            if ack.event_id != delivery.event_id or ack.outcome == "rejected":
                collisions += 1
                delivery.state = "blocked"
                await self._alert(
                    kind="collision", outbox_event_id=outbox.id, detail="ack collision"
                )
                continue
            if (
                ack.fact_sha256 != outbox.payload_sha256
                or ack.bundle_digest != handoff.bundle_digest
                or ack.deployment_digest != handoff.deployment_digest
            ):
                digest_mismatches += 1
                delivery.state = "blocked"
                await self._alert(
                    kind="digest_mismatch",
                    outbox_event_id=outbox.id,
                    detail="ack digest",
                )
                continue
            delivery.state = "acknowledged"
            delivery.acknowledged_at = ack.acknowledged_at
        await self._db.flush()
        return ReconciliationResult(
            backlog=backlog,
            gaps=gaps,
            collisions=collisions,
            schema_mismatches=schema_mismatches,
            digest_mismatches=digest_mismatches,
        )

    async def _alert(
        self, *, kind: str, outbox_event_id: UUID | None, detail: str
    ) -> None:
        digest = hashlib.sha256(detail.encode("utf-8")).hexdigest()
        existing = await self._db.scalar(
            select(BillingFactReconciliationAlert.id).where(
                BillingFactReconciliationAlert.outbox_event_id == outbox_event_id,
                BillingFactReconciliationAlert.kind == kind,
                BillingFactReconciliationAlert.detail_sha256 == digest,
            )
        )
        if existing is None:
            self._db.add(
                BillingFactReconciliationAlert(
                    id=uuid4(),
                    outbox_event_id=outbox_event_id,
                    kind=kind,
                    detail_sha256=digest,
                    observed_at=_utc(self._now()),
                )
            )
