"""``PAYMENT_CONFIRMED`` settles; ``PAYMENT_RECEIVED`` is an inert confirmation.

Owner decision (2026-10-18, confirmed-settles DESIGN-DELTA): a card capture
(``CONFIRMED``) is commercially paid; access and credits start there.  The
funds-available receipt (``RECEIVED``, ~D+32 for a card) must never create a
second effect, cycle, fact revision or Transaction, and never moves
``paid_at``.  ``paid_at`` is the first strict ``YYYY-MM-DD`` of
``confirmedDate``, ``paymentDate``, ``clientPaymentDate`` that is not later
than the São Paulo day of the observation (the envelope ``dateCreated`` of the
earliest ``PAYMENT_CONFIRMED`` delivery of that payment, else the envelope
itself for an event and the readback clock for a read).  Nothing is inferred
from an event or payment timestamp: no usable date fails closed.

Fixture provenance (``tests/fixtures/asaas``): the card ``CONFIRMED`` shape
(``confirmedDate = clientPaymentDate =`` capture day, ``paymentDate`` null,
``creditDate = estimatedCreditDate =`` +32) and the refunded read are the
ASAAS sandbox capture of 2026-10-18.  The card ``RECEIVED`` field values are
UNOBSERVED (the sandbox payment never reached RECEIVED); its fixture follows
the official docs sample with ``paymentDate`` moved to the funds day.

The real services and repository SQL run on SQLite; PostgreSQL-only proofs
(triggers, canary slot, concurrency) live in
``tests/integration/test_asaas_confirmed_settlement_postgres.py``.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from src.models.billing import (
    BillingCheckoutSession,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingPaymentProjection,
    BillingRolloutControl,
    BillingSubscription,
    BillingSubscriptionCycle,
    BillingWebhookDelivery,
)
from src.models.builder import Builder
from src.models.coupon import Coupon
from src.models.transaction import Transaction
from src.services.billing_outbox import canonical_json_bytes
from src.services.billing_read_contract import read_current_subscription
from src.services.billing_webhook import (
    WebhookEnvelope,
    _FinancialState,
    parse_envelope,
)
from tests.services import test_billing_webhook as webhook_suite
from tests.services.test_billing_webhook import (
    _body,
    _card_read,
    _documented_id,
    _event,
    _metric,
    _payment_read,
    _World,
)

# The webhook suite's SQLite world (real services, in-memory provider).
world = webhook_suite.world

ASAAS_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "asaas"
CAPTURE_DAY = date(2026, 10, 11)


def _drift() -> float:
    return _metric("neurostack_billing_alerts_total", kind="drift")


def _from_fixture(
    relative: str,
    *,
    event_id: str | None = None,
    when: str | None = None,
    **payment: object,
) -> WebhookEnvelope:
    """A provider-shaped envelope from a fixture, with explicit overrides."""

    payload = json.loads((ASAAS_FIXTURES / relative).read_text(), parse_float=Decimal)
    payload = copy.deepcopy(payload)
    payload["id"] = event_id or _documented_id()
    if when is not None:
        payload["dateCreated"] = when
    payload["payment"]["externalReference"] = None
    payload["payment"].update(payment)
    return parse_envelope(_body(payload), payload)


async def _card_subscription(
    world: _World, *, coupon: str | None = None
) -> dict[str, Any]:
    """A linked (``active_linked``) Plus card subscription; payment fields."""

    if coupon is not None:
        await world.add(Coupon(code=coupon, discount_percent=10, uses_count=0))
    checkout = await world.subscription_session(coupon=coupon)
    value = Decimal(checkout.amount_cents).scaleb(-2)
    created = await world.receive(
        _event(
            "SUBSCRIPTION_CREATED",
            id="sub_life_0001",
            externalReference=str(checkout.id),
            value=value,
        )
    )
    assert created.outcome == "processed"
    return {
        "id": "pay_card_1",
        "subscription": "sub_life_0001",
        "checkoutSession": checkout.external_checkout_id,
        "value": value,
        "billingType": "CREDIT_CARD",
        "dueDate": "2026-10-11",
    }


async def _topup(world: _World, payment_id: str = "pay_topup_card") -> dict[str, Any]:
    checkout, _order = await world.topup_session()
    return {
        "id": payment_id,
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
        "value": Decimal("100.00"),
    }


async def _counts(world: _World) -> tuple[int, int, int, int]:
    """(effects, facts, Transactions, cycles)."""

    return (
        await world.count(BillingPaymentEffect),
        await world.count(BillingOutboxEvent),
        await world.count(Transaction),
        await world.count(BillingSubscriptionCycle),
    )


async def _subscription_status(world: _World) -> str:
    (row,) = await world.rows(BillingSubscription)
    return str(row.status)


async def _projection(world: _World, payment_id: str) -> BillingPaymentProjection:
    (row,) = await world.rows(
        BillingPaymentProjection, BillingPaymentProjection.payment_id == payment_id
    )
    return row


async def _readback(
    world: _World, delivery_id: Any, *, clock: datetime | None = None
) -> str:
    async with world.sessions() as session:
        return await world.readback(
            session, clock=(lambda: clock) if clock is not None else None
        ).process_delivery(delivery_id)


# ---------------------------------------------------------------------------
# The rule: first settle-class evidence settles, the second is inert.
# ---------------------------------------------------------------------------


async def test_card_confirmed_settles_once_and_the_late_received_is_inert(
    world: _World,
) -> None:
    common = await _card_subscription(world, coupon="WELCOME10")
    drift = _drift()
    confirmed = await world.receive(
        _from_fixture("webhook/payment_event.json", **common)
    )

    assert confirmed.outcome == "processed"
    projection = await _projection(world, "pay_card_1")
    assert (projection.status, projection.settled_cents) == ("settled", 89_910)
    assert projection.paid_at == CAPTURE_DAY
    (effect,) = await world.rows(BillingPaymentEffect)
    assert (effect.effect_kind, effect.source_revision, effect.delivery_id) == (
        "settlement",
        "settlement:v1",
        confirmed.delivery_id,
    )
    (transaction,) = await world.rows(Transaction)
    assert (transaction.event_type, transaction.coupon_code, transaction.plan) == (
        "PAYMENT_CONFIRMED",
        "WELCOME10",
        "plus",
    )
    (coupon,) = await world.rows(Coupon)
    assert coupon.uses_count == 1
    assert await _subscription_status(world) == "active"
    (builder,) = await world.rows(Builder, Builder.id == world.builder_id)
    assert (builder.subscription_plan, builder.subscription_status) == (
        "plus",
        "active",
    )
    (cycle,) = await world.rows(BillingSubscriptionCycle)
    assert (cycle.cycle_start, cycle.cycle_end, cycle.financial_revision) == (
        date(2026, 10, 11),
        date(2026, 11, 11),
        1,
    )
    (fact,) = await world.rows(BillingOutboxEvent)
    assert (fact.financial_revision, fact.payload["paid_at"]) == (1, "2026-10-11")

    # Funds available 32 days later: confirmation only.
    received = await world.receive(
        _from_fixture("payment/card_received_event.json", **common)
    )
    assert received.outcome == "processed"
    assert await _counts(world) == (1, 1, 1, 1)
    projection = await _projection(world, "pay_card_1")
    assert (projection.status, projection.paid_at) == ("settled", CAPTURE_DAY)
    (cycle,) = await world.rows(BillingSubscriptionCycle)
    assert cycle.financial_revision == 1
    (coupon,) = await world.rows(Coupon)
    assert coupon.uses_count == 1
    assert _drift() == drift
    (delivery,) = await world.rows(
        BillingWebhookDelivery, BillingWebhookDelivery.id == received.delivery_id
    )
    assert delivery.status == "processed"


async def test_received_first_then_an_older_confirmed_is_inert(world: _World) -> None:
    common = await _card_subscription(world)
    received = await world.receive(
        _from_fixture("payment/card_received_event.json", **common)
    )
    assert received.outcome == "processed"
    # confirmedDate wins over the funds-day paymentDate: same paid_at as
    # the CONFIRMED-first order.
    assert (await _projection(world, "pay_card_1")).paid_at == CAPTURE_DAY
    confirmed = await world.receive(
        _from_fixture("webhook/payment_event.json", **common)
    )
    assert confirmed.outcome == "processed"
    assert await _counts(world) == (1, 1, 1, 1)
    projection = await _projection(world, "pay_card_1")
    assert (projection.status, projection.paid_at) == ("settled", CAPTURE_DAY)
    (effect,) = await world.rows(BillingPaymentEffect)
    assert effect.delivery_id == received.delivery_id


async def test_confirmed_with_a_divergent_value_blocks_with_drift(
    world: _World,
) -> None:
    common = await _card_subscription(world)
    drift = _drift()
    outcome = await world.receive(
        _event("PAYMENT_CONFIRMED", **{**common, "value": Decimal("899.10")})
    )
    assert outcome.outcome == "readback_required"
    assert _drift() == drift + 1
    assert await _counts(world) == (0, 0, 0, 0)
    assert await _subscription_status(world) == "active_linked"


async def test_settled_payment_ignores_later_receipt_dates(world: _World) -> None:
    """``paid_at`` is write-once: a later settle-class event with a missing
    or different date is processed without drift (old rule: quarantine)."""

    common = await _topup(world, "pay_write_once")
    await world.receive(_event("PAYMENT_RECEIVED", **common))
    drift = _drift()
    for dates in (
        {"paymentDate": None},
        {"paymentDate": "2026-10-10"},
        {"paymentDate": None, "confirmedDate": "2026-10-09"},
        {"paymentDate": None, "clientPaymentDate": "2026-10-08"},
    ):
        later = await world.receive(
            _event("PAYMENT_RECEIVED", when="2026-10-11 12:00:00", **common, **dates)
        )
        assert later.outcome == "processed", dates
        # Checked after every event: a later CONFIRMED carrying the original
        # capture date must not be able to mask an overwrite.
        assert (await _projection(world, "pay_write_once")).paid_at == CAPTURE_DAY
        confirmed = await world.receive(
            _event("PAYMENT_CONFIRMED", when="2026-10-11 12:00:01", **common, **dates)
        )
        assert confirmed.outcome == "processed", dates
        assert (await _projection(world, "pay_write_once")).paid_at == CAPTURE_DAY
    projection = await _projection(world, "pay_write_once")
    assert projection.paid_at == CAPTURE_DAY
    assert await world.count(BillingPaymentEffect) == 1
    assert await world.count(BillingOutboxEvent) == 1
    assert _drift() == drift


async def test_confirmed_readback_on_a_settled_payment_converges(
    world: _World,
) -> None:
    common = await _topup(world, "pay_read_confirmed")
    await world.receive(_event("PAYMENT_RECEIVED", **common))
    updated = await world.receive(
        _event("PAYMENT_UPDATED", when="2026-10-12 09:00:00", **common)
    )
    assert updated.outcome == "readback_required"
    world.provider.payments["pay_read_confirmed"] = _card_read(
        "pay_read_confirmed",
        value_cents=10_000,
        subscription_id=None,
        checkout_id=common["checkoutSession"],
    )
    drift = _drift()
    assert await _readback(world, updated.delivery_id) == "converged"
    projection = await _projection(world, "pay_read_confirmed")
    assert (projection.status, projection.paid_at) == ("settled", CAPTURE_DAY)
    assert await world.count(BillingPaymentEffect) == 1
    assert _drift() == drift
    # A later read with *different* dates converges too: ``paid_at`` is
    # write-once and never compared (old rule: drift).
    world.provider.payments["pay_read_confirmed"] = _payment_read(
        "pay_read_confirmed",
        status="CONFIRMED",
        value_cents=10_000,
        checkout_id=common["checkoutSession"],
        payment_date=date(2026, 10, 10),
        confirmed_date=date(2026, 10, 10),
    )
    async with world.sessions() as session:
        assert (
            await world.readback(session).readback_payment(
                "pay_read_confirmed", delivery_id=updated.delivery_id
            )
            == "converged"
        )
    assert (await _projection(world, "pay_read_confirmed")).paid_at == CAPTURE_DAY
    assert _drift() == drift


@pytest.mark.parametrize("settled_first", [True, False], ids=["settled", "unsettled"])
async def test_a_read_refunding_more_than_the_charge_is_drift(
    world: _World, settled_first: bool
) -> None:
    common = await _topup(world, "pay_over_refund")
    if settled_first:
        await world.receive(_event("PAYMENT_CONFIRMED", **common))
    partial = await world.receive(
        _event("PAYMENT_PARTIALLY_REFUNDED", when="2026-10-12 09:00:00", **common)
    )
    world.provider.payments["pay_over_refund"] = _card_read(
        "pay_over_refund",
        status="CONFIRMED",
        refunded_cents=20_000,
        value_cents=10_000,
        subscription_id=None,
        checkout_id=common["checkoutSession"],
    )
    drift = _drift()
    assert await _readback(world, partial.delivery_id) == "drift"
    assert _drift() == drift + 1
    projection = await _projection(world, "pay_over_refund")
    assert (projection.settled_cents, projection.reversed_cents) == (
        10_000 if settled_first else 0,
        0,
    )
    assert projection.status != "refunded_before_settlement"


async def test_legacy_settlement_without_paid_at_is_repaired_from_the_chain(
    world: _World,
) -> None:
    """Pre-035 rows: the read repairs ``paid_at`` with the same chain and
    bound (capture date first; a funds-day ``paymentDate`` is refused)."""

    common = await _topup(world, "pay_legacy_chain")
    await world.receive(_event("PAYMENT_RECEIVED", **common))
    async with world.sessions() as session:
        await session.execute(
            update(BillingPaymentProjection)
            .where(BillingPaymentProjection.payment_id == "pay_legacy_chain")
            .values(paid_at=None)
        )
        await session.commit()
    refunded = await world.receive(
        _event("PAYMENT_REFUNDED", when="2026-10-11 11:00:00", **common)
    )
    assert refunded.outcome == "readback_required"
    world.provider.payments["pay_legacy_chain"] = _payment_read(
        "pay_legacy_chain",
        status="REFUNDED",
        value_cents=10_000,
        refunded_cents=10_000,
        checkout_id=common["checkoutSession"],
        payment_date=date(2026, 10, 12),
        confirmed_date=date(2026, 10, 10),
    )
    assert await _readback(world, refunded.delivery_id) == "converged"
    projection = await _projection(world, "pay_legacy_chain")
    assert (projection.paid_at, projection.reversed_cents) == (
        date(2026, 10, 10),
        10_000,
    )


async def test_confirmed_and_received_share_the_settlement_revision(
    world: _World,
) -> None:
    card = await _topup(world, "pay_rev_card")
    await world.receive(_event("PAYMENT_CONFIRMED", **card))
    pix_checkout, _order = await world.topup_session(
        external_checkout_id="chk_topup_000002"
    )
    pix = {**card, "id": "pay_rev_pix", "checkoutSession": "chk_topup_000002"}
    await world.receive(_event("PAYMENT_RECEIVED", **pix))
    del pix_checkout
    effects = await world.rows(BillingPaymentEffect)
    assert sorted((e.payment_id, e.source_revision) for e in effects) == [
        ("pay_rev_card", "settlement:v1"),
        ("pay_rev_pix", "settlement:v1"),
    ]
    facts = {
        fact.payload["payment_id"]: dict(fact.payload)
        for fact in await world.rows(BillingOutboxEvent)
    }

    def normalized(payload: dict[str, Any]) -> bytes:
        varying = (
            "occurred_at",
            "produced_at",
            "event_id",
            "aggregate_id",
            "order_id",
            "payment_id",
        )
        return canonical_json_bytes(
            {k: v for k, v in payload.items() if k not in varying}
        )

    # The CONFIRMED- and RECEIVED-triggered facts are byte-identical modulo
    # time fields and identities: the contract cannot tell them apart.
    assert normalized(facts["pay_rev_card"]) == normalized(facts["pay_rev_pix"])


# ---------------------------------------------------------------------------
# paid_at: chain, bound and anchor.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confirmed", "payment", "client", "bound", "expected"),
    [
        # confirmedDate first: RECEIVED-first equals CONFIRMED-first.
        ((10, 11), (11, 12), (10, 11), (11, 12), (10, 11)),
        ((10, 11), (10, 9), (10, 8), (10, 11), (10, 11)),
        # then clientPaymentDate (the capture day on the sandbox evidence),
        # then paymentDate ("data de liquidação", the card funds day).
        (None, (10, 10), (10, 9), (10, 11), (10, 9)),
        (None, None, (10, 9), (10, 11), (10, 9)),
        # RECEIVED-first without confirmedDate: the funds day never wins over
        # the capture day, even when both are inside the bound (review L-1).
        (None, (11, 12), (10, 11), (11, 12), (10, 11)),
        (None, (10, 10), None, (10, 11), (10, 10)),
        # a candidate later than the bound is skipped, never clamped.
        ((10, 12), (10, 10), None, (10, 11), (10, 10)),
        ((10, 12), (11, 12), (10, 11), (10, 11), (10, 11)),
        # equal to the bound is accepted (+0 days, inclusive).
        ((10, 11), None, None, (10, 11), (10, 11)),
        # nothing usable: no settlement (fail closed).
        ((10, 12), (11, 12), (10, 12), (10, 11), None),
        (None, None, None, (10, 11), None),
    ],
)
def test_paid_at_chain_precedence_and_bound(
    confirmed: tuple[int, int] | None,
    payment: tuple[int, int] | None,
    client: tuple[int, int] | None,
    bound: tuple[int, int],
    expected: tuple[int, int] | None,
) -> None:
    from src.services.billing_webhook import PaymentFacts, resolve_paid_at

    def day(value: tuple[int, int] | None) -> date | None:
        return None if value is None else date(2026, *value)

    facts = PaymentFacts(
        payment_id="pay_chain",
        subscription_id=None,
        checkout_id=None,
        external_reference=None,
        value_cents=10_000,
        billing_type="credit_card",
        payment_date=day(payment),
        confirmed_date=day(confirmed),
        client_payment_date=day(client),
    )
    assert resolve_paid_at(facts, bound=date(2026, *bound)) == day(expected)


@pytest.mark.parametrize(
    ("when", "capture", "expected"),
    [
        # 23:30 in São Paulo is already 02:30 UTC of the next day: the bound
        # is the São Paulo day, so the next-day capture date is refused.
        ("2026-10-11 23:30:00", "2026-10-12", None),
        ("2026-10-11 23:30:00", "2026-10-11", date(2026, 10, 11)),
        ("2026-10-12 00:30:00", "2026-10-12", date(2026, 10, 12)),
    ],
)
async def test_paid_at_bound_is_the_sao_paulo_day_of_the_envelope(
    world: _World, when: str, capture: str, expected: date | None
) -> None:
    common = await _topup(world, "pay_bound_day")
    outcome = await world.receive(
        _event(
            "PAYMENT_CONFIRMED",
            when=when,
            confirmedDate=capture,
            clientPaymentDate=capture,
            **common,
        )
    )
    projection = await _projection(world, "pay_bound_day")
    if expected is None:
        assert outcome.outcome == "readback_required"
        assert (projection.settled_cents, projection.paid_at) == (0, None)
        assert await world.count(BillingPaymentEffect) == 0
    else:
        assert outcome.outcome == "processed"
        assert projection.paid_at == expected


@pytest.mark.parametrize(
    ("payment_created", "capture", "settles"),
    [
        # A payment-level dateCreated after the event would admit a future
        # capture date; one before it (renewals are created ahead of their
        # due date) would refuse a same-day capture.  Neither is the bound.
        ("2026-10-20", "2026-10-15", False),
        ("2026-09-20", "2026-10-11", True),
    ],
)
async def test_payment_level_date_created_is_never_the_bound(
    world: _World, payment_created: str, capture: str, settles: bool
) -> None:
    common = await _topup(world, "pay_payment_created")
    outcome = await world.receive(
        _event(
            "PAYMENT_CONFIRMED",
            when="2026-10-11 10:00:00",
            dateCreated=payment_created,
            confirmedDate=capture,
            clientPaymentDate=capture,
            **common,
        )
    )
    assert outcome.outcome == ("processed" if settles else "readback_required")
    assert await world.count(BillingPaymentEffect) == (1 if settles else 0)


async def test_received_is_bounded_by_the_earliest_confirmed_delivery(
    world: _World,
) -> None:
    common = await _topup(world, "pay_anchor")
    blocked = {**common, "value": Decimal("1000.00")}  # drift: no settlement
    for when in ("2026-10-11 09:00:00", "2026-10-20 09:00:00"):
        outcome = await world.receive(_event("PAYMENT_CONFIRMED", when=when, **blocked))
        assert outcome.outcome == "readback_required"
    # Funds-day paymentDate and a mid-window clientPaymentDate are both later
    # than the earliest capture (10-11): no date may be taken.
    late = await world.receive(
        _event(
            "PAYMENT_RECEIVED",
            when="2026-11-12 08:00:00",
            paymentDate="2026-11-12",
            clientPaymentDate="2026-10-15",
            **common,
        )
    )
    assert late.outcome == "readback_required"
    assert await world.count(BillingPaymentEffect) == 0
    # The capture date itself is admissible.
    settled = await world.receive(
        _event(
            "PAYMENT_RECEIVED",
            when="2026-11-12 08:00:01",
            paymentDate="2026-11-12",
            confirmedDate="2026-10-11",
            **common,
        )
    )
    assert settled.outcome == "processed"
    assert (await _projection(world, "pay_anchor")).paid_at == CAPTURE_DAY


async def test_another_builders_delivery_never_anchors_the_bound(
    world: _World,
) -> None:
    """Tenant isolation, defence in depth: a CONFIRMED delivery row bound to
    another builder (unreachable through exact correlation) is never this
    payment's anchor."""

    common = await _topup(world, "pay_foreign_anchor")
    foreign = uuid4()
    await world.add(Builder(id=foreign, name="Foreign Builder"))
    await world.add(
        BillingWebhookDelivery(
            id=uuid4(),
            builder_id=foreign,
            provider="asaas",
            event_id=f"sha256:{'0' * 64}",
            event_name="PAYMENT_CONFIRMED",
            provider_created_at=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
            resource_kind="payment",
            resource_id="pay_foreign_anchor",
            body_bytes=2,
            body_sha256="0" * 64,
            status="processed",
        )
    )
    outcome = await world.receive(
        _event(
            "PAYMENT_RECEIVED",
            when="2026-11-12 08:00:00",
            confirmedDate="2026-10-05",
            **common,
        )
    )
    assert outcome.outcome == "processed"
    assert (await _projection(world, "pay_foreign_anchor")).paid_at == date(2026, 10, 5)


async def test_received_without_any_confirmed_delivery_uses_its_own_day(
    world: _World,
) -> None:
    common = await _topup(world, "pay_pix_late")
    outcome = await world.receive(
        _event("PAYMENT_RECEIVED", when="2026-11-12 08:00:00", **common)
    )
    assert outcome.outcome == "processed"
    assert (await _projection(world, "pay_pix_late")).paid_at == date(2026, 11, 12)


@pytest.mark.parametrize(
    ("confirmed", "client", "expected"),
    [
        # Review probe p_c: no confirmedDate, capture day as clientPaymentDate,
        # funds day as paymentDate -> the capture day.
        (None, "2026-10-11", date(2026, 10, 11)),
        ("2026-10-11", "2026-10-11", date(2026, 10, 11)),
        # Documented accepted case (owner decision 2026-10-18, runbook "A card
        # receipt seen before its capture"): the funds day is the only date
        # the provider sent, so it becomes paid_at; never refused.
        (None, None, date(2026, 11, 12)),
    ],
    ids=["client-capture-day", "confirmed-capture-day", "funds-day-only"],
)
async def test_card_received_first_prefers_the_capture_day_over_the_funds_day(
    world: _World, confirmed: str | None, client: str | None, expected: date
) -> None:
    common = await _topup(world, "pay_received_first_card")
    outcome = await world.receive(
        _event(
            "PAYMENT_RECEIVED",
            when="2026-11-12 08:00:00",
            billingType="CREDIT_CARD",
            confirmedDate=confirmed,
            clientPaymentDate=client,
            paymentDate="2026-11-12",
            creditDate="2026-11-12",
            **common,
        )
    )
    assert outcome.outcome == "processed"
    assert (await _projection(world, "pay_received_first_card")).paid_at == expected


async def test_readback_bound_uses_the_confirmed_anchor_before_the_clock(
    world: _World,
) -> None:
    common = await _topup(world, "pay_read_anchor")
    blocked = await world.receive(
        _event(
            "PAYMENT_CONFIRMED",
            when="2026-10-11 09:00:00",
            **{**common, "value": Decimal("1000.00")},
        )
    )
    clock = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)
    world.provider.payments["pay_read_anchor"] = _payment_read(
        "pay_read_anchor",
        status="RECEIVED",
        value_cents=10_000,
        checkout_id=common["checkoutSession"],
        payment_date=date(2026, 11, 12),
    )
    assert await _readback(world, blocked.delivery_id, clock=clock) == "drift"
    assert await world.count(BillingPaymentEffect) == 0
    world.provider.payments["pay_read_anchor"] = _card_read(
        "pay_read_anchor",
        status="RECEIVED",
        value_cents=10_000,
        subscription_id=None,
        checkout_id=common["checkoutSession"],
    )
    assert await _readback(world, blocked.delivery_id, clock=clock) == "converged"
    assert (await _projection(world, "pay_read_anchor")).paid_at == CAPTURE_DAY


async def test_readback_without_confirmed_delivery_is_bounded_by_its_clock(
    world: _World,
) -> None:
    common = await _topup(world, "pay_read_clock")
    pending = await world.receive(
        _event("PAYMENT_REFUND_IN_PROGRESS", when="2026-11-12 09:00:00", **common)
    )
    assert pending.outcome == "readback_required"
    world.provider.payments["pay_read_clock"] = _payment_read(
        "pay_read_clock",
        status="RECEIVED",
        value_cents=10_000,
        checkout_id=common["checkoutSession"],
        payment_date=date(2026, 11, 12),
    )
    early = datetime(2026, 11, 11, 15, tzinfo=timezone.utc)
    assert await _readback(world, pending.delivery_id, clock=early) == "drift"
    late = datetime(2026, 11, 12, 15, tzinfo=timezone.utc)
    assert await _readback(world, pending.delivery_id, clock=late) == "converged"
    assert (await _projection(world, "pay_read_clock")).paid_at == date(2026, 11, 12)


# ---------------------------------------------------------------------------
# Q-C / M1: activation only on the branch that settles.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_name", ["PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"])
async def test_settle_class_event_without_a_date_never_activates(
    world: _World, event_name: str
) -> None:
    common = await _card_subscription(world)
    outcome = await world.receive(
        _event(
            event_name,
            paymentDate=None,
            confirmedDate=None,
            clientPaymentDate=None,
            **common,
        )
    )
    assert outcome.outcome == "readback_required"
    assert await _subscription_status(world) == "active_linked"
    assert await _counts(world) == (0, 0, 0, 0)
    world.provider.payments["pay_card_1"] = _card_read(
        "pay_card_1", checkout_id=common["checkoutSession"]
    )
    assert await _readback(world, outcome.delivery_id) == "converged"
    assert await _subscription_status(world) == "active"
    assert await _counts(world) == (1, 1, 1, 1)


async def test_confirmed_on_a_deleted_payment_never_activates(world: _World) -> None:
    common = await _card_subscription(world)
    await world.receive(_event("PAYMENT_CREATED", when="2026-10-11 09:00:00", **common))
    await world.receive(_event("PAYMENT_DELETED", when="2026-10-11 09:01:00", **common))
    outcome = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 09:02:00", **common)
    )
    assert outcome.outcome == "readback_required"
    assert await _subscription_status(world) == "active_linked"
    assert (await _projection(world, "pay_card_1")).status == "deleted"
    assert await _counts(world) == (0, 0, 0, 0)


async def _canary_subscription(
    world: _World, *, settleable: bool = False
) -> dict[str, Any]:
    """The linked Plus subscription as the environment's canary checkout.

    ``settleable`` also binds the checkout to the rollout control's canary
    slot, so a canary settlement can be reserved (SQLite has no triggers).
    """

    common = await _card_subscription(world)
    async with world.sessions() as session:
        await session.execute(
            update(BillingCheckoutSession).values(
                canary_environment="test", canary_slot="subscription"
            )
        )
        if settleable:
            checkout_id = await session.scalar(select(BillingCheckoutSession.id))
            await session.execute(
                update(BillingRolloutControl)
                .where(BillingRolloutControl.environment == "test")
                .values(
                    canary_subscription_checkout_id=checkout_id,
                    canary_checkout_count=1,
                    canary_amount_reserved_cents=99_900,
                )
            )
        await session.commit()
    return common


async def _canary_count(world: _World) -> int:
    (control,) = await world.rows(
        BillingRolloutControl, BillingRolloutControl.environment == "test"
    )
    return int(control.canary_settlement_count)


async def _builder_plan(world: _World) -> str | None:
    (builder,) = await world.rows(Builder, Builder.id == world.builder_id)
    return builder.subscription_plan


_REVERSAL_CLASS_EVENTS_EXPECTED = [
    "PAYMENT_REFUND_IN_PROGRESS",
    "PAYMENT_PARTIALLY_REFUNDED",
    "PAYMENT_REFUNDED",
    "PAYMENT_CHARGEBACK_REQUESTED",
    "PAYMENT_CHARGEBACK_DISPUTE",
    "PAYMENT_AWAITING_CHARGEBACK_REVERSAL",
]


def test_m2_reversal_class_is_derived_from_the_allowlist() -> None:
    from src.services.billing_webhook import _REVERSAL_CLASS_EVENTS

    assert sorted(_REVERSAL_CLASS_EVENTS) == sorted(_REVERSAL_CLASS_EVENTS_EXPECTED)


@pytest.mark.parametrize("reversal_event", _REVERSAL_CLASS_EVENTS_EXPECTED)
@pytest.mark.parametrize("settle_event", ["PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"])
@pytest.mark.parametrize(
    "settle_when",
    ["2026-10-11 10:00:00", "2026-10-11 16:00:00"],
    ids=["older", "newer"],
)
async def test_m2_canary_capture_after_an_open_reversal_waits_for_a_read(
    world: _World, reversal_event: str, settle_event: str, settle_when: str
) -> None:
    """Review M-2: the canary refund marker cannot be stored, so a capture
    event must not settle (and spend the slot) while a reversal-class
    delivery of that payment is still open: only a read may decide."""

    common = await _canary_subscription(world, settleable=True)
    first = await world.receive(
        _event(reversal_event, when="2026-10-11 15:00:00", **common)
    )
    assert first.outcome == "readback_required"
    late = await world.receive(_event(settle_event, when=settle_when, **common))
    assert late.outcome == "readback_required"
    assert await world.count(BillingPaymentProjection) == 0
    assert await _counts(world) == (0, 0, 0, 0)
    assert await _subscription_status(world) == "active_linked"
    assert await _builder_plan(world) != "plus"
    assert await _canary_count(world) == 0


async def test_m2_a_processed_reversal_delivery_no_longer_blocks_the_capture(
    world: _World,
) -> None:
    common = await _canary_subscription(world, settleable=True)
    first = await world.receive(
        _event("PAYMENT_REFUND_IN_PROGRESS", when="2026-10-11 15:00:00", **common)
    )
    async with world.sessions() as session:
        await session.execute(
            update(BillingWebhookDelivery)
            .where(BillingWebhookDelivery.id == first.delivery_id)
            .values(status="processed")
        )
        await session.commit()
    captured = await world.receive(_event("PAYMENT_CONFIRMED", **common))
    assert captured.outcome == "processed"
    assert await _counts(world) == (1, 1, 1, 1)
    assert await _canary_count(world) == 1
    assert await _subscription_status(world) == "active"


async def test_m2_another_payments_open_refund_does_not_block_this_capture(
    world: _World,
) -> None:
    """The guard is per payment: an unresolved refund of another charge of
    the same canary builder never holds this capture back."""

    common = await _canary_subscription(world, settleable=True)
    other = await world.receive(
        _event(
            "PAYMENT_REFUND_IN_PROGRESS",
            when="2026-10-11 09:00:00",
            **{**common, "id": "pay_card_other"},
        )
    )
    assert other.outcome == "readback_required"
    captured = await world.receive(_event("PAYMENT_CONFIRMED", **common))
    assert captured.outcome == "processed"
    assert await _canary_count(world) == 1
    assert (await _projection(world, "pay_card_1")).status == "settled"


async def test_m2_the_exact_read_still_settles_a_canary_capture(world: _World) -> None:
    """The guard only defers to a read: a read showing the capture (and no
    completed refund) settles it into the slot."""

    common = await _canary_subscription(world, settleable=True)
    await world.receive(
        _event("PAYMENT_REFUND_IN_PROGRESS", when="2026-10-11 15:00:00", **common)
    )
    late = await world.receive(_event("PAYMENT_CONFIRMED", **common))
    assert late.outcome == "readback_required"
    world.provider.payments["pay_card_1"] = _card_read(
        "pay_card_1", checkout_id=common["checkoutSession"]
    )
    assert await _readback(world, late.delivery_id) == "converged"
    assert await _counts(world) == (1, 1, 1, 1)
    assert await _canary_count(world) == 1
    assert await _subscription_status(world) == "active"


async def test_m2_guard_is_canary_only(world: _World) -> None:
    """Off the canary, the refund-first row is projected (readback_required)
    and a newer capture settles it; the next read reverses it."""

    common = await _card_subscription(world)
    first = await world.receive(
        _event("PAYMENT_REFUND_IN_PROGRESS", when="2026-10-11 09:00:00", **common)
    )
    assert first.outcome == "readback_required"
    captured = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 10:00:00", **common)
    )
    assert captured.outcome == "processed"
    assert await _counts(world) == (1, 1, 1, 1)


async def test_m2_guard_is_canary_only_when_the_payment_has_no_row(
    world: _World,
) -> None:
    """Review D09: an open refund delivery that never projected a row (its
    value diverged) does not hold a non-canary capture back."""

    common = await _card_subscription(world)
    blocked = await world.receive(
        _event(
            "PAYMENT_REFUNDED",
            when="2026-10-11 09:00:00",
            **{**common, "value": Decimal("1000.00")},
        )
    )
    assert blocked.outcome == "readback_required"
    assert await world.count(BillingPaymentProjection) == 0
    captured = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 10:00:00", **common)
    )
    assert captured.outcome == "processed"
    assert await _counts(world) == (1, 1, 1, 1)


async def test_canary_first_write_without_a_date_projects_nothing(
    world: _World,
) -> None:
    """The DB pins every canary projection write to an ``active`` Plus row;
    an event that will not settle must neither activate nor project."""

    common = await _canary_subscription(world)
    outcome = await world.receive(
        _event(
            "PAYMENT_CONFIRMED", confirmedDate=None, clientPaymentDate=None, **common
        )
    )
    assert outcome.outcome == "readback_required"
    assert await _subscription_status(world) == "active_linked"
    assert await world.count(BillingPaymentProjection) == 0


async def test_canary_refund_read_before_any_settlement_projects_nothing(
    world: _World,
) -> None:
    """A completed refund read on an unprojected canary payment is the
    marker case: it never activates (and cannot be projected) -> drift."""

    common = await _canary_subscription(world)
    refunded = await world.receive(_event("PAYMENT_REFUNDED", **common))
    assert refunded.outcome == "readback_required"
    world.provider.payments["pay_card_1"] = _card_read(
        "pay_card_1",
        status="REFUNDED",
        refunded_cents=99_900,
        checkout_id=common["checkoutSession"],
    )
    drift = _drift()
    assert await _readback(world, refunded.delivery_id) == "drift"
    assert _drift() == drift + 1
    assert await _subscription_status(world) == "active_linked"
    assert await world.count(BillingPaymentProjection) == 0


async def test_canary_activation_without_settlement_is_rolled_back(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety net: if the pre-write settle decision were ever wrong, the
    whole transaction is refused (quarantine), never an activation alone."""

    common = await _canary_subscription(world)
    monkeypatch.setattr(
        _FinancialState, "_first_write_settles", staticmethod(lambda **_: True)
    )
    outcome = await world.receive(
        _event(
            "PAYMENT_CONFIRMED", confirmedDate=None, clientPaymentDate=None, **common
        )
    )
    assert outcome.outcome == "quarantined"
    assert await _subscription_status(world) == "active_linked"
    assert await world.count(BillingPaymentProjection) == 0


# ---------------------------------------------------------------------------
# The second event is not always inert: H-1, H-2, M2 and the marker.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label_event", "label", "read_status", "read_refunded", "final"),
    [
        # H-1: a won dispute ends in CONFIRMED/RECEIVED.
        (
            "PAYMENT_CHARGEBACK_REQUESTED",
            "chargeback_requested",
            "CONFIRMED",
            0,
            "settled",
        ),
        ("PAYMENT_CHARGEBACK_DISPUTE", "chargeback_dispute", "CONFIRMED", 0, "settled"),
        (
            "PAYMENT_AWAITING_CHARGEBACK_REVERSAL",
            "restoration_pending",
            "CONFIRMED",
            0,
            "settled",
        ),
        # L-3 (review probe p_t): settle evidence on a reversal label is never
        # inert either; the provider's current word decides the label.
        (
            "PAYMENT_REFUND_IN_PROGRESS",
            "reversal_pending",
            "REFUND_IN_PROGRESS",
            0,
            "reversal_pending",
        ),
        ("PAYMENT_REFUNDED", "reversed", "REFUNDED", 10_000, "reversed"),
    ],
)
@pytest.mark.parametrize("settle_event", ["PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"])
async def test_settle_evidence_on_a_disputed_or_reversed_label_requires_a_readback(
    world: _World,
    label_event: str,
    label: str,
    read_status: str,
    read_refunded: int,
    final: str,
    settle_event: str,
) -> None:
    common = await _topup(world, "pay_h1")
    await world.receive(
        _event("PAYMENT_RECEIVED", when="2026-10-11 09:00:00", **common)
    )
    await world.receive(_event(label_event, when="2026-10-15 09:00:00", **common))
    assert (await _projection(world, "pay_h1")).status == label
    effects = await world.count(BillingPaymentEffect)
    drift = _drift()
    stale = await world.receive(
        _event(settle_event, when="2026-10-11 09:00:01", **common)
    )
    assert stale.outcome == "processed"  # an old event never needs a read
    newer = await world.receive(
        _event(
            settle_event,
            when="2026-10-21 09:00:00",
            confirmedDate="2026-10-11",
            paymentDate="2026-10-11",
            **common,
        )
    )
    assert newer.outcome == "readback_required"
    assert _drift() == drift  # a read, not an alert
    assert (await _projection(world, "pay_h1")).status == label
    world.provider.payments["pay_h1"] = _card_read(
        "pay_h1",
        status=read_status,
        refunded_cents=read_refunded,
        value_cents=10_000,
        subscription_id=None,
        checkout_id=common["checkoutSession"],
    )
    assert await _readback(world, newer.delivery_id) == "converged"
    assert (await _projection(world, "pay_h1")).status == final
    assert await world.count(BillingPaymentEffect) == effects
    assert _drift() == drift


async def test_l3_settle_evidence_on_a_partially_reversed_row_requires_a_readback(
    world: _World,
) -> None:
    common = await _topup(world, "pay_l3_partial")
    await world.receive(
        _event("PAYMENT_RECEIVED", when="2026-10-11 09:00:00", **common)
    )
    partial = await world.receive(
        _event("PAYMENT_PARTIALLY_REFUNDED", when="2026-10-12 09:00:00", **common)
    )
    world.provider.payments["pay_l3_partial"] = _payment_read(
        "pay_l3_partial",
        status="RECEIVED",
        value_cents=10_000,
        refunded_cents=3_000,
        checkout_id=common["checkoutSession"],
    )
    assert await _readback(world, partial.delivery_id) == "converged"
    assert (await _projection(world, "pay_l3_partial")).status == "partially_reversed"
    drift = _drift()
    newer = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-20 09:00:00", **common)
    )
    assert newer.outcome == "readback_required"
    assert await _readback(world, newer.delivery_id) == "converged"
    projection = await _projection(world, "pay_l3_partial")
    assert (projection.status, projection.reversed_cents) == (
        "partially_reversed",
        3_000,
    )
    assert await world.count(BillingPaymentEffect) == 2
    assert _drift() == drift


@pytest.mark.parametrize(
    ("event_name", "needs_read"),
    [
        ("PAYMENT_REPROVED_BY_RISK_ANALYSIS", True),
        ("PAYMENT_CREDIT_CARD_CAPTURE_REFUSED", True),
        ("PAYMENT_OVERDUE", True),
        ("PAYMENT_CREATED", False),
        ("PAYMENT_AWAITING_RISK_ANALYSIS", False),
    ],
)
async def test_h2_newer_unpaid_status_on_settled_money_requires_a_readback(
    world: _World, event_name: str, needs_read: bool
) -> None:
    common = await _topup(world, "pay_h2")
    await world.receive(
        _event("PAYMENT_RECEIVED", when="2026-10-11 09:00:00", **common)
    )
    drift = _drift()
    stale = await world.receive(
        _event(event_name, when="2026-10-11 08:00:00", **common)
    )
    assert stale.outcome == "processed"
    assert _drift() == drift
    newer = await world.receive(
        _event(event_name, when="2026-10-11 10:00:00", **common)
    )
    assert newer.outcome == ("readback_required" if needs_read else "processed")
    assert _drift() == drift + (1 if needs_read else 0)
    projection = await _projection(world, "pay_h2")
    assert (projection.status, projection.settled_cents) == ("settled", 10_000)


@pytest.mark.parametrize(
    ("final_status", "refunded", "label", "effects"),
    [
        ("REFUNDED", 99_900, "reversed", 2),
        ("CONFIRMED", 0, "settled", 1),
    ],
)
async def test_h2_confirmed_then_reproved_converges_only_by_a_paid_or_refunded_read(
    world: _World, final_status: str, refunded: int, label: str, effects: int
) -> None:
    """M3: the named operator path for a grant whose capture the provider
    later reports as unpaid (runbook: asaas-billing-reconciliation)."""

    common = await _card_subscription(world)
    await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 09:00:00", **common)
    )
    reproved = await world.receive(
        _event(
            "PAYMENT_REPROVED_BY_RISK_ANALYSIS", when="2026-10-11 10:00:00", **common
        )
    )
    assert reproved.outcome == "readback_required"
    # The provider still shows the charge unpaid: money never un-settles, so
    # the read keeps alerting until an operator resolves it at ASAAS.
    world.provider.payments["pay_card_1"] = _payment_read(
        "pay_card_1",
        status="PENDING",
        value_cents=99_900,
        subscription_id="sub_life_0001",
        checkout_id=common["checkoutSession"],
        payment_date=None,
    )
    for _attempt in range(2):
        assert await _readback(world, reproved.delivery_id) == "drift"
    world.provider.payments["pay_card_1"] = _card_read(
        "pay_card_1",
        status=final_status,
        refunded_cents=refunded,
        checkout_id=common["checkoutSession"],
    )
    assert await _readback(world, reproved.delivery_id) == "converged"
    projection = await _projection(world, "pay_card_1")
    assert projection.status == label
    assert await world.count(BillingPaymentEffect) == effects


async def test_m2_stale_confirmed_never_settles_a_newer_reproved_payment(
    world: _World,
) -> None:
    common = await _card_subscription(world)
    reproved = await world.receive(
        _event(
            "PAYMENT_REPROVED_BY_RISK_ANALYSIS", when="2026-10-11 10:00:00", **common
        )
    )
    assert reproved.outcome == "processed"
    late = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 09:00:00", **common)
    )
    assert late.outcome == "readback_required"
    projection = await _projection(world, "pay_card_1")
    assert (projection.status, projection.settled_cents) == ("failed", 0)
    assert await _subscription_status(world) == "active_linked"
    assert await _counts(world) == (0, 0, 0, 0)
    # Only the provider's current word settles it.
    world.provider.payments["pay_card_1"] = _card_read(
        "pay_card_1", checkout_id=common["checkoutSession"]
    )
    assert await _readback(world, late.delivery_id) == "converged"
    assert await _subscription_status(world) == "active"
    assert await _counts(world) == (1, 1, 1, 1)


async def test_l2_stale_capture_never_settles_a_row_a_newer_refund_created(
    world: _World,
) -> None:
    """Review probe p_s: REFUNDED (15:00) arrives first and projects a
    ``readback_required`` row; the older CONFIRMED (10:00) must not grant."""

    common = await _card_subscription(world)
    refunded = await world.receive(
        _event("PAYMENT_REFUNDED", when="2026-10-11 15:00:00", **common)
    )
    assert refunded.outcome == "readback_required"
    assert (await _projection(world, "pay_card_1")).status == "readback_required"
    late = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 10:00:00", **common)
    )
    assert late.outcome == "readback_required"
    assert await _counts(world) == (0, 0, 0, 0)
    assert await _subscription_status(world) == "active_linked"
    assert await _builder_plan(world) != "plus"
    world.provider.payments["pay_card_1"] = _card_read(
        "pay_card_1",
        status="REFUNDED",
        refunded_cents=99_900,
        checkout_id=common["checkoutSession"],
    )
    for delivery_id in (refunded.delivery_id, late.delivery_id):
        assert await _readback(world, delivery_id) == "converged"
    projection = await _projection(world, "pay_card_1")
    assert (projection.status, projection.settled_cents) == (
        "refunded_before_settlement",
        0,
    )
    assert await _counts(world) == (0, 0, 0, 0)
    assert await _subscription_status(world) == "active_linked"


async def test_l2_control_a_newer_capture_settles_and_the_read_reverses(
    world: _World,
) -> None:
    common = await _card_subscription(world)
    refunded = await world.receive(
        _event("PAYMENT_REFUNDED", when="2026-10-11 15:00:00", **common)
    )
    newer = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 16:00:00", **common)
    )
    assert newer.outcome == "processed"
    world.provider.payments["pay_card_1"] = _card_read(
        "pay_card_1",
        status="REFUNDED",
        refunded_cents=99_900,
        checkout_id=common["checkoutSession"],
    )
    assert await _readback(world, refunded.delivery_id) == "converged"
    projection = await _projection(world, "pay_card_1")
    assert (projection.status, projection.reversed_cents) == ("reversed", 99_900)


async def test_stale_confirmed_after_a_newer_overdue_still_settles(
    world: _World,
) -> None:
    common = await _topup(world, "pay_overdue_then_paid")
    await world.receive(_event("PAYMENT_OVERDUE", when="2026-10-11 10:00:00", **common))
    late = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 09:00:00", **common)
    )
    assert late.outcome == "processed"
    assert await world.count(BillingPaymentEffect) == 1


async def test_pre_settlement_refund_marker_quarantines_a_late_confirmed(
    world: _World,
) -> None:
    common = await _topup(world, "pay_marker_confirmed")
    refunded = await world.receive(_event("PAYMENT_REFUNDED", **common))
    world.provider.payments["pay_marker_confirmed"] = _payment_read(
        "pay_marker_confirmed",
        status="REFUNDED",
        value_cents=10_000,
        refunded_cents=10_000,
        checkout_id=common["checkoutSession"],
        payment_date=None,
    )
    assert await _readback(world, refunded.delivery_id) == "converged"
    drift = _drift()
    late = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 10:30:00", **common)
    )
    assert late.outcome == "readback_required"
    assert _drift() == drift + 1
    projection = await _projection(world, "pay_marker_confirmed")
    assert (projection.status, projection.settled_cents) == (
        "refunded_before_settlement",
        0,
    )
    assert await world.count(BillingPaymentEffect) == 0


# ---------------------------------------------------------------------------
# Reversals in the capture -> receipt window.
# ---------------------------------------------------------------------------


async def test_refund_after_confirmed_before_received_is_a_normal_reversal(
    world: _World,
) -> None:
    common = await _card_subscription(world)
    await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 09:00:00", **common)
    )
    refunded = await world.receive(
        _event("PAYMENT_REFUNDED", when="2026-10-11 18:06:07", **common)
    )
    assert refunded.outcome == "processed"
    effects = sorted(
        (e.effect_kind, e.source_revision, e.amount_cents)
        for e in await world.rows(BillingPaymentEffect)
    )
    assert effects == [
        ("reversal", "reversed_cents:99900", 99_900),
        ("settlement", "settlement:v1", 99_900),
    ]
    projection = await _projection(world, "pay_card_1")
    assert (projection.status, projection.reversed_cents) == ("reversed", 99_900)
    assert projection.pre_settlement_refund_cents is None
    assert projection.paid_at == CAPTURE_DAY
    (cycle,) = await world.rows(BillingSubscriptionCycle)
    assert (cycle.state, cycle.financial_revision) == ("reversed", 2)
    facts = await world.rows(BillingOutboxEvent)
    assert sorted(f.financial_revision for f in facts) == [1, 2]
    assert {f.payload["paid_at"] for f in facts} == {"2026-10-11"}
    async with world.sessions() as session:
        current = await read_current_subscription(session, builder_id=world.builder_id)
    assert current is not None and current.current_period_end is None

    # L-3: funds arriving after a refund contradict the reversed label, so
    # the receipt needs the provider's word; the read changes no money.
    drift = _drift()
    late = await world.receive(
        _from_fixture("payment/card_received_event.json", **common)
    )
    assert late.outcome == "readback_required"
    assert _drift() == drift
    world.provider.payments["pay_card_1"] = _card_read(
        "pay_card_1",
        status="REFUNDED",
        refunded_cents=99_900,
        checkout_id=common["checkoutSession"],
    )
    assert await _readback(world, late.delivery_id) == "converged"
    assert await world.count(BillingPaymentEffect) == 2
    assert await world.count(BillingOutboxEvent) == 2
    assert (await _projection(world, "pay_card_1")).status == "reversed"


@pytest.mark.parametrize(
    "driver", ["confirmed_value_drift", "confirmed_without_date", "partial_refund"]
)
@pytest.mark.parametrize(
    ("dates", "settles"),
    [
        ({"confirmed_date": CAPTURE_DAY, "client_payment_date": CAPTURE_DAY}, True),
        ({"payment_date": CAPTURE_DAY}, True),
        ({}, False),
    ],
    ids=["sandbox-capture-dates", "payment-date-only", "no-date"],
)
@pytest.mark.parametrize(
    ("refunded", "label"),
    [(10_000, "partially_reversed"), (99_900, "reversed")],
)
async def test_q_a_confirmed_read_with_done_refunds_settles_then_reverses(
    world: _World,
    driver: str,
    dates: dict[str, date],
    settles: bool,
    refunded: int,
    label: str,
) -> None:
    """Owner decision Q-A / reviewer F1: a CONFIRMED read with completed
    refunds on an unsettled payment is a receipt, whichever delivery drives
    the read; without any usable date it fails closed (never the marker)."""

    common = await _card_subscription(world)
    if driver == "confirmed_value_drift":
        first = await world.receive(
            _event("PAYMENT_CONFIRMED", **{**common, "value": Decimal("1000.00")})
        )
    elif driver == "confirmed_without_date":
        first = await world.receive(
            _event(
                "PAYMENT_CONFIRMED",
                confirmedDate=None,
                clientPaymentDate=None,
                **common,
            )
        )
    else:
        first = await world.receive(
            _event("PAYMENT_PARTIALLY_REFUNDED", when="2026-10-12 09:00:00", **common)
        )
    assert first.outcome == "readback_required"
    world.provider.payments["pay_card_1"] = _payment_read(
        "pay_card_1",
        status="CONFIRMED",
        value_cents=99_900,
        refunded_cents=refunded,
        subscription_id="sub_life_0001",
        checkout_id=common["checkoutSession"],
        billing_type="CREDIT_CARD",
        **{"payment_date": None, **dates},
    )
    outcome = await _readback(world, first.delivery_id)
    projections = await world.rows(BillingPaymentProjection)
    effects = sorted(
        (e.effect_kind, e.source_revision, e.amount_cents)
        for e in await world.rows(BillingPaymentEffect)
    )
    if settles:
        assert outcome == "converged"
        (projection,) = projections
        assert (
            projection.status,
            projection.settled_cents,
            projection.reversed_cents,
            projection.paid_at,
            projection.pre_settlement_refund_cents,
        ) == (label, 99_900, refunded, CAPTURE_DAY, None)
        assert effects == [
            ("reversal", f"reversed_cents:{refunded}", refunded),
            ("settlement", "settlement:v1", 99_900),
        ]
        assert await _subscription_status(world) == "active"
        assert await world.count(Transaction) == 1
        assert await world.count(BillingOutboxEvent) == 2
    else:
        assert outcome == "drift"
        assert all(p.status != "refunded_before_settlement" for p in projections)
        assert all(p.settled_cents == 0 for p in projections)
        assert effects == []
        assert await _subscription_status(world) == "active_linked"


# ---------------------------------------------------------------------------
# Renewals: cycles are keyed by payment and never revised by RECEIVED.
# ---------------------------------------------------------------------------


async def test_renewals_n_and_n_plus_one_then_n_received(world: _World) -> None:
    common = await _card_subscription(world)
    renewal = {
        **common,
        "id": "pay_card_2",
        "checkoutSession": None,
        "dueDate": "2026-11-11",
    }
    await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 09:00:00", **common)
    )
    await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-11-11 09:00:00", **renewal)
    )
    late = await world.receive(
        _from_fixture("payment/card_received_event.json", **common)
    )
    assert late.outcome == "processed"
    cycles = sorted(
        (c.payment_id, c.cycle_start, c.cycle_end, c.financial_revision, c.state)
        for c in await world.rows(BillingSubscriptionCycle)
    )
    assert cycles == [
        ("pay_card_1", date(2026, 10, 11), date(2026, 11, 11), 1, "settled"),
        ("pay_card_2", date(2026, 11, 11), date(2026, 12, 11), 1, "settled"),
    ]
    assert await _counts(world) == (2, 2, 2, 2)
    assert {
        (await _projection(world, pid)).paid_at for pid in ("pay_card_1", "pay_card_2")
    } == {date(2026, 10, 11), date(2026, 11, 11)}
    async with world.sessions() as session:
        current = await read_current_subscription(session, builder_id=world.builder_id)
    assert current is not None
    assert (current.status, current.current_period_end) == (
        "active",
        date(2026, 12, 11),
    )


async def test_updated_in_the_window_reads_back_without_new_effects(
    world: _World,
) -> None:
    common = await _card_subscription(world)
    await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 09:00:00", **common)
    )
    updated = await world.receive(
        _event("PAYMENT_UPDATED", when="2026-10-20 09:00:00", **common)
    )
    assert updated.outcome == "readback_required"
    world.provider.payments["pay_card_1"] = _card_read(
        "pay_card_1", checkout_id=common["checkoutSession"]
    )
    assert await _readback(world, updated.delivery_id) == "converged"
    assert await _counts(world) == (1, 1, 1, 1)


def test_fixture_dates_match_the_sandbox_capture() -> None:
    """The fixtures carry the observed card shape (no funds date extracted)."""

    confirmed = _from_fixture("webhook/payment_event.json")
    assert confirmed.payment is not None
    assert (
        confirmed.payment.confirmed_date,
        confirmed.payment.client_payment_date,
        confirmed.payment.payment_date,
        confirmed.payment.due_date,
    ) == (CAPTURE_DAY, CAPTURE_DAY, None, CAPTURE_DAY)
    received = _from_fixture("payment/card_received_event.json")
    assert received.payment is not None
    assert (
        received.payment.confirmed_date,
        received.payment.payment_date,
    ) == (CAPTURE_DAY, CAPTURE_DAY + timedelta(days=32))
    from src.services.asaas import _payment_readback

    for name, refunded in (
        ("payment_response.json", 0),
        ("payment_refunded_response.json", 99_900),
    ):
        read = _payment_readback(
            json.loads(
                (ASAAS_FIXTURES / "readback" / name).read_text(), parse_float=Decimal
            )
        )
        assert (read.confirmed_date, read.payment_date, read.refunded_cents) == (
            CAPTURE_DAY,
            None,
            refunded,
        )
