"""The summary read contract through the real webhook/readback path.

Group 6b ``current_period_end`` and Group 6b-2 ``payment_state`` and the
cancelled paid window: real Plus subscriptions are captured (card
``PAYMENT_CONFIRMED``), received, renewed, reversed, restored and ended by the
real ``BillingWebhookService``/``BillingReadbackService`` on PostgreSQL, and
the summary is read through the real route inside a ``READ ONLY`` transaction
after every step, at injected instants (never the host clock).  The harness
(its own disposable child database) is the webhook suite's, so the cycle rows
are exactly what production code writes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from src.api.routes import billing
from src.api.schemas.billing import BillingSummaryResponse
from src.repositories.billing import BillingRepository
from src.services import billing_read_contract as read_contract_module
from src.services.asaas import PaymentReadback, SubscriptionReadback
from src.services.billing_webhook import BillingReadbackService
from tests.integration import test_asaas_webhook_postgres as webhook_suite
from tests.integration.test_asaas_checkout_service_postgres import (
    _builder,
    _configure,
    _Flag,
    _Provider,
    _service,
)
from tests.integration.test_asaas_webhook_postgres import (
    _all,
    _brl,
    _deliver,
    _external_checkout,
    _general,
    _payload,
    _payment,
    _read,
    _Reads,
    _scalar,
    _subscription,
    _subscription_created,
)

pytestmark = pytest.mark.integration

# The webhook suite's module-scoped disposable database.
harness = webhook_suite.harness

# Injected instants (never the host clock).  São Paulo is UTC-3.
_MID_CYCLE = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)  # 10-20 12:00
_LAST_PAID_DAY_LATE = datetime(2026, 11, 12, 2, 30, tzinfo=timezone.utc)  # 11-11
_NEXT_DAY = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)  # 11-12 00:00


def _at(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    monkeypatch.setattr(read_contract_module, "_utc_now", lambda: instant)


def _card(
    event: str,
    payment_id: str,
    *,
    subscription: str,
    cents: int,
    when: str,
    due: str,
    checkout: str | None = None,
    funds_day: str | None = None,
) -> dict[str, Any]:
    """A card payment event shaped like the ASAAS sandbox capture: a
    ``PAYMENT_CONFIRMED`` carries ``confirmedDate = clientPaymentDate =`` the
    capture day (``when``) and no ``paymentDate``; ``dueDate`` is the cycle
    start."""

    payload = _payment(
        event,
        payment_id,
        checkout=checkout,
        subscription=subscription,
        value=_brl(cents),
        when=when,
        payment_date=funds_day,
    )
    payload["payment"]["dueDate"] = due
    return payload


async def _cycles(harness: Any, builder_id: UUID) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in await _all(
            harness,
            "SELECT payment_id, state, cycle_start, cycle_end, settled_cents, "
            "reversed_cents, restored_cents FROM billing_subscription_cycles "
            "WHERE builder_id = :b ORDER BY cycle_start, payment_id",
            b=builder_id,
        )
    ]


async def _settlements(harness: Any, builder_id: UUID) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in await _all(
            harness,
            "SELECT payment_id, effect_kind, source_revision "
            "FROM billing_payment_effects WHERE builder_id = :b "
            "ORDER BY payment_id, effect_kind",
            b=builder_id,
        )
    ]


def _shown(summary: BillingSummaryResponse) -> tuple[Any, ...]:
    assert summary.subscription is not None
    return (
        summary.subscription.status,
        summary.subscription.current_period_end,
        summary.subscription.payment_state,
    )


async def _summary(harness: Any, builder_id: UUID) -> BillingSummaryResponse:
    async with harness.session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        response = await billing.billing_summary(builder_id, session)
        await session.rollback()
    return response


async def _cycle(harness: Any, builder_id: UUID) -> Any:
    (cycle,) = await _all(
        harness,
        "SELECT state, settled_cents, reversed_cents, restored_cents, cycle_end "
        "FROM billing_subscription_cycles WHERE builder_id = :b",
        b=builder_id,
    )
    return cycle


async def _readback(harness: Any, delivery_id: UUID, reads: _Reads) -> None:
    async with harness.session() as session:
        await BillingReadbackService(
            session, client_factory=reads, environment="test"
        ).process_delivery(delivery_id)


async def test_period_end_follows_settle_reverse_restore_on_the_real_path(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "G6b restored cycle"
    )
    _at(monkeypatch, _MID_CYCLE)
    sub = "sub_g6b_restored"
    pay = "pay_g6b_restored"
    await _deliver(harness, _subscription_created(session_id, sub, cents), "test")

    def payment(event: str) -> dict[str, Any]:
        return _payment(
            event, pay, checkout=checkout_id, subscription=sub, value=_brl(cents)
        )

    # Settled: the paid-through date is the cycle end.
    await _deliver(harness, payment("PAYMENT_RECEIVED"), "test")
    cycle = await _cycle(harness, builder_id)
    assert (cycle.state, cycle.settled_cents) == ("settled", cents)
    end = cycle.cycle_end
    assert end == date(2026, 11, 11)
    settled = await _summary(harness, builder_id)
    assert settled.subscription is not None
    assert settled.subscription.current_period_end == end
    assert settled.subscription.payment_state == "paid"

    # Reversed (refund): no fully-paid cycle remains, so ``null``.
    refunded = await _deliver(harness, payment("PAYMENT_REFUNDED"), "test")
    if refunded.outcome == "readback_required":
        await _readback(
            harness,
            refunded.delivery_id,
            _Reads(payments={pay: _read(pay, "REFUNDED", cents, refunded=cents)}),
        )
    cycle = await _cycle(harness, builder_id)
    assert (cycle.state, cycle.reversed_cents, cycle.restored_cents) == (
        "reversed",
        cents,
        0,
    )
    reversed_summary = await _summary(harness, builder_id)
    assert reversed_summary.subscription is not None
    assert reversed_summary.subscription.current_period_end is None
    # ``status`` stays ``active`` after a refund; ``payment_state`` says so.
    assert reversed_summary.subscription.status == "active"
    assert reversed_summary.subscription.payment_state == "reversed"

    # Restored (refund denied): fully paid again, so the cycle end is back.
    denied = await _deliver(harness, payment("PAYMENT_REFUND_DENIED"), "test")
    await _readback(
        harness,
        denied.delivery_id,
        _Reads(payments={pay: _read(pay, "RECEIVED", cents, refunded=0)}),
    )
    cycle = await _cycle(harness, builder_id)
    assert cycle.state == "restored"
    assert cycle.settled_cents == cycle.reversed_cents == cycle.restored_cents
    assert cycle.settled_cents == cents
    restored = await _summary(harness, builder_id)
    assert restored.subscription is not None
    assert restored.subscription.current_period_end == date(2026, 11, 11)
    assert restored.subscription.payment_state == "paid"
    # No renewal arrived: overdue from the next São Paulo day.
    _at(monkeypatch, _NEXT_DAY)
    overdue = await _summary(harness, builder_id)
    assert overdue.subscription is not None
    assert overdue.subscription.payment_state == "overdue"


async def _converge(harness: Any, subscription_id: UUID, reads: _Reads) -> str:
    async with harness.session() as session:
        return await BillingReadbackService(
            session, client_factory=reads, environment="test"
        ).readback_subscription(subscription_id)


async def _ended_after_payment(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    sub: str,
    *,
    event: str,
    remote_status: str,
    deleted: bool,
) -> tuple[UUID, UUID]:
    """A paid Plus subscription ended at the provider on the real path."""

    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, label
    )
    await _deliver(harness, _subscription_created(session_id, sub, cents), "test")
    # Paid by the card capture alone (no RECEIVED yet, as for ~32 days on a
    # real card): the cancelled window must exist without the funds receipt.
    captured = await _deliver(
        harness,
        _card(
            "PAYMENT_CONFIRMED",
            f"pay_{sub}",
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-10-11 10:00:00",
            due="2026-10-11",
        ),
        "test",
    )
    assert captured.outcome == "processed"
    subscription_id = await _scalar(
        harness,
        "SELECT id FROM billing_subscriptions WHERE builder_id = :b",
        b=builder_id,
    )
    ended = await _deliver(harness, _payload(event, {"id": sub}), "test")
    assert ended.outcome == "readback_required"
    # Before the readback converges: pending provider evidence.
    _at(monkeypatch, _MID_CYCLE)
    pending = await _summary(harness, builder_id)
    assert pending.subscription is not None
    assert (pending.subscription.status, pending.subscription.payment_state) == (
        "non_billable_pending_readback",
        "pending",
    )
    remote = SubscriptionReadback(
        sub, remote_status, deleted, "MONTHLY", cents, date(2026, 11, 11), None
    )
    reads = _Reads(subscriptions={sub: remote}, charges={sub: ()})
    assert await _converge(harness, subscription_id, reads) == "converged"
    return builder_id, subscription_id


async def test_provider_deleted_subscription_keeps_its_paid_period_on_the_real_path(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner A2 on the real webhook/readback path: SUBSCRIPTION_DELETED then a
    converged ``deleted: true`` readback writes ``cancelled``; the summary keeps
    it through the last paid São Paulo day, then ``null``."""

    builder_id, subscription_id = await _ended_after_payment(
        harness,
        monkeypatch,
        "G6b-2 deleted after payment",
        "sub_g6b2_deleted",
        event="SUBSCRIPTION_DELETED",
        remote_status="INACTIVE",
        deleted=True,
    )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_subscriptions WHERE id = :i",
            i=subscription_id,
        )
        == "cancelled"
    )
    for instant in (_MID_CYCLE, _LAST_PAID_DAY_LATE):
        _at(monkeypatch, instant)
        summary = await _summary(harness, builder_id)
        assert summary.subscription is not None
        assert summary.subscription.model_dump(mode="json") == {
            "offer_id": "plan_plus_monthly_v1",
            "catalog_version_id": "payment-2026-10-03",
            "status": "cancelled",
            "amount_cents": summary.subscription.amount_cents,
            "current_period_end": "2026-11-11",
            "payment_state": "paid",
        }
    _at(monkeypatch, _NEXT_DAY)
    assert (await _summary(harness, builder_id)).subscription is None
    # Eligibility is untouched by the paid window: nothing blocks a new plan.
    async with harness.session() as session:
        assert (
            await BillingRepository(session).subscription_checkout_blocker(
                builder_id=builder_id
            )
            is None
        )


async def test_provider_inactivated_subscription_is_null_on_the_real_path(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``inactive`` (provider INACTIVE/EXPIRED, not deleted) is not mapped to
    the cancelled paid window: ``null`` even inside the paid period."""

    builder_id, subscription_id = await _ended_after_payment(
        harness,
        monkeypatch,
        "G6b-2 inactivated after payment",
        "sub_g6b2_inactive",
        event="SUBSCRIPTION_INACTIVATED",
        remote_status="INACTIVE",
        deleted=False,
    )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_subscriptions WHERE id = :i",
            i=subscription_id,
        )
        == "inactive"
    )
    _at(monkeypatch, _MID_CYCLE)
    assert (await _summary(harness, builder_id)).subscription is None


# -- card settlement timing (owner decision "CONFIRMED counts as paid") --------
#
# Re-verification of the 2026-10-18 review findings HIGH-1/HIGH-2 of the first
# 6b-2 attempt, which were caused by the webhook treating PAYMENT_CONFIRMED as
# "not yet paid": a card is CONFIRMED at capture and RECEIVED only ~D+32.


async def test_card_capture_alone_is_paid_and_the_receipt_is_inert_on_the_real_path(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "G6b-2 card capture"
    )
    sub = "sub_g6b2_capture"
    pay = "pay_g6b2_capture"
    await _deliver(harness, _subscription_created(session_id, sub, cents), "test")
    _at(monkeypatch, _MID_CYCLE)
    # Linked, nothing charged yet: the first payment is pending.
    assert _shown(await _summary(harness, builder_id)) == (
        "active_linked",
        None,
        "pending",
    )

    captured = await _deliver(
        harness,
        _card(
            "PAYMENT_CONFIRMED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-10-11 10:00:00",
            due="2026-10-11",
        ),
        "test",
    )
    assert captured.outcome == "processed"
    after_capture = await _cycles(harness, builder_id)
    assert after_capture == [
        (pay, "settled", date(2026, 10, 11), date(2026, 11, 11), cents, 0, 0)
    ]
    paid = ("active", date(2026, 11, 11), "paid")
    for instant in (_MID_CYCLE, _LAST_PAID_DAY_LATE):
        _at(monkeypatch, instant)
        assert _shown(await _summary(harness, builder_id)) == paid

    # ~D+32 the funds receipt arrives: inert (same settlement, same cycle).
    received = await _deliver(
        harness,
        _card(
            "PAYMENT_RECEIVED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-11-12 09:00:00",
            due="2026-10-11",
            funds_day="2026-11-12",
        ),
        "test",
    )
    assert received.outcome == "processed"
    assert await _cycles(harness, builder_id) == after_capture
    assert await _settlements(harness, builder_id) == [
        (pay, "settlement", "settlement:v1")
    ]
    _at(monkeypatch, _LAST_PAID_DAY_LATE)
    assert _shown(await _summary(harness, builder_id)) == paid
    # No renewal was charged: overdue from the next São Paulo day on.
    _at(monkeypatch, _NEXT_DAY)
    assert _shown(await _summary(harness, builder_id)) == (
        "active",
        date(2026, 11, 11),
        "overdue",
    )


async def test_card_renewal_capture_moves_the_period_and_stays_paid_on_the_real_path(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "G6b-2 card renewal"
    )
    sub = "sub_g6b2_renewal"
    first = "pay_g6b2_renewal_1"
    renewal = "pay_g6b2_renewal_2"
    await _deliver(harness, _subscription_created(session_id, sub, cents), "test")
    await _deliver(
        harness,
        _card(
            "PAYMENT_CONFIRMED",
            first,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-10-11 10:00:00",
            due="2026-10-11",
        ),
        "test",
    )
    # Control: without the renewal the next São Paulo day is overdue.
    _at(monkeypatch, _NEXT_DAY)
    assert _shown(await _summary(harness, builder_id)) == (
        "active",
        date(2026, 11, 11),
        "overdue",
    )

    # The renewal is captured on its due date; its funds arrive only ~D+32.
    captured = await _deliver(
        harness,
        _card(
            "PAYMENT_CONFIRMED",
            renewal,
            subscription=sub,
            cents=cents,
            when="2026-11-11 09:00:00",
            due="2026-11-11",
        ),
        "test",
    )
    assert captured.outcome == "processed"
    renewed = ("active", date(2026, 12, 11), "paid")
    for instant in (
        _NEXT_DAY,
        datetime(2026, 12, 12, 2, 59, 59, tzinfo=timezone.utc),  # 12-11 23:59:59
    ):
        _at(monkeypatch, instant)
        assert _shown(await _summary(harness, builder_id)) == renewed
    _at(monkeypatch, datetime(2026, 12, 12, 3, 0, tzinfo=timezone.utc))
    assert _shown(await _summary(harness, builder_id)) == (
        "active",
        date(2026, 12, 11),
        "overdue",
    )

    received = await _deliver(
        harness,
        _card(
            "PAYMENT_RECEIVED",
            renewal,
            subscription=sub,
            cents=cents,
            when="2026-12-13 09:00:00",
            due="2026-11-11",
            funds_day="2026-12-13",
        ),
        "test",
    )
    assert received.outcome == "processed"
    assert await _cycles(harness, builder_id) == [
        (first, "settled", date(2026, 10, 11), date(2026, 11, 11), cents, 0, 0),
        (renewal, "settled", date(2026, 11, 11), date(2026, 12, 11), cents, 0, 0),
    ]
    assert await _settlements(harness, builder_id) == [
        (first, "settlement", "settlement:v1"),
        (renewal, "settlement", "settlement:v1"),
    ]
    _at(monkeypatch, _NEXT_DAY)
    assert _shown(await _summary(harness, builder_id)) == renewed


async def test_partially_refunded_capture_read_is_reversed_on_the_real_path(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner Q-A: a CONFIRMED read carrying a completed partial refund on a
    payment not yet settled here settles, then reverses; owner B: a partially
    reversed cycle is not paid, so the summary says ``reversed`` and has no
    paid-through date."""

    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "G6b-2 Q-A partial"
    )
    sub = "sub_g6b2_qa_partial"
    pay = "pay_g6b2_qa_partial"
    await _deliver(harness, _subscription_created(session_id, sub, cents), "test")
    partial = await _deliver(
        harness,
        _card(
            "PAYMENT_PARTIALLY_REFUNDED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-10-12 09:00:00",
            due="2026-10-11",
        ),
        "test",
    )
    assert partial.outcome == "readback_required"
    _at(monkeypatch, _MID_CYCLE)
    assert _shown(await _summary(harness, builder_id)) == (
        "active_linked",
        None,
        "pending",
    )
    capture_day = date(2026, 10, 11)
    read = PaymentReadback(
        pay,
        "CONFIRMED",
        False,
        cents,
        sub,
        checkout_id,
        None,
        "CREDIT_CARD",
        10_000,
        due_date=capture_day,
        confirmed_date=capture_day,
        client_payment_date=capture_day,
    )
    await _readback(harness, partial.delivery_id, _Reads(payments={pay: read}))
    assert await _cycles(harness, builder_id) == [
        (pay, "reversed", capture_day, date(2026, 11, 11), cents, 10_000, 0)
    ]
    assert _shown(await _summary(harness, builder_id)) == ("active", None, "reversed")


async def test_refund_or_chargeback_in_progress_stays_paid_until_completed(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status-only events on captured money (refund in progress, chargeback
    requested) change no cycle, so ``payment_state`` stays ``paid``; the
    completed refund makes it ``reversed`` and drops the paid-through date."""

    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "G6b-2 refund in progress"
    )
    sub = "sub_g6b2_in_progress"
    pay = "pay_g6b2_in_progress"
    await _deliver(harness, _subscription_created(session_id, sub, cents), "test")
    _at(monkeypatch, _MID_CYCLE)

    def event(name: str, when: str) -> dict[str, Any]:
        return _card(
            name,
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when=when,
            due="2026-10-11",
        )

    await _deliver(harness, event("PAYMENT_CONFIRMED", "2026-10-11 10:00:00"), "test")
    paid = ("active", date(2026, 11, 11), "paid")
    assert _shown(await _summary(harness, builder_id)) == paid
    captured = await _cycles(harness, builder_id)
    for name, when in (
        ("PAYMENT_REFUND_IN_PROGRESS", "2026-10-13 10:00:00"),
        ("PAYMENT_CHARGEBACK_REQUESTED", "2026-10-14 10:00:00"),
    ):
        outcome = await _deliver(harness, event(name, when), "test")
        assert outcome.outcome == "processed", name
        assert await _cycles(harness, builder_id) == captured, name
        assert _shown(await _summary(harness, builder_id)) == paid, name

    refunded = await _deliver(
        harness, event("PAYMENT_REFUNDED", "2026-10-15 10:00:00"), "test"
    )
    assert refunded.outcome == "processed"
    assert await _cycles(harness, builder_id) == [
        (pay, "reversed", date(2026, 10, 11), date(2026, 11, 11), cents, cents, 0)
    ]
    assert _shown(await _summary(harness, builder_id)) == ("active", None, "reversed")


def _capture_read(
    payment_id: str, *, subscription: str, checkout: str, cents: int, refunded: int
) -> PaymentReadback:
    """An exact card read: CONFIRMED, captured 2026-10-11, ``refunded`` the
    sum of the DONE refunds."""

    capture_day = date(2026, 10, 11)
    return PaymentReadback(
        payment_id,
        "CONFIRMED",
        False,
        cents,
        subscription,
        checkout,
        None,
        "CREDIT_CARD",
        refunded,
        due_date=capture_day,
        confirmed_date=capture_day,
        client_payment_date=capture_day,
    )


async def test_chargeback_steps_keep_the_summary_paid_known_gap(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins a KNOWN GAP (owner open item "chargeback exposure window"):
    reversed money comes only from the sum of DONE refunds, so no chargeback
    step reaches the cycle and the summary stays ``paid``.  A webhook change
    that reflects chargebacks must update this test and the contract text in
    ``docs/api-contracts.md`` together."""

    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "G6b-2 chargeback"
    )
    sub = "sub_g6b2_chargeback"
    pay = "pay_g6b2_chargeback"
    await _deliver(harness, _subscription_created(session_id, sub, cents), "test")
    _at(monkeypatch, _MID_CYCLE)

    def event(name: str, when: str) -> dict[str, Any]:
        return _card(
            name,
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when=when,
            due="2026-10-11",
        )

    await _deliver(harness, event("PAYMENT_CONFIRMED", "2026-10-11 10:00:00"), "test")
    paid = ("active", date(2026, 11, 11), "paid")
    captured = await _cycles(harness, builder_id)
    assert captured == [
        (pay, "settled", date(2026, 10, 11), date(2026, 11, 11), cents, 0, 0)
    ]
    deliveries = []
    for name, when in (
        ("PAYMENT_CHARGEBACK_REQUESTED", "2026-10-13 10:00:00"),
        ("PAYMENT_CHARGEBACK_DISPUTE", "2026-10-14 10:00:00"),
        ("PAYMENT_AWAITING_CHARGEBACK_REVERSAL", "2026-10-15 10:00:00"),
    ):
        outcome = await _deliver(harness, event(name, when), "test")
        assert outcome.outcome == "processed", name
        deliveries.append(outcome.delivery_id)
        assert await _cycles(harness, builder_id) == captured, name
        assert _shown(await _summary(harness, builder_id)) == paid, name

    # Exact reads in the chargeback statuses carry no DONE refund: still paid.
    for status in ("CHARGEBACK_DISPUTE", "CHARGEBACK_REQUESTED"):
        read = _capture_read(
            pay, subscription=sub, checkout=checkout_id, cents=cents, refunded=0
        )
        reads = _Reads(payments={pay: replace(read, status=status)})
        async with harness.session() as session:
            assert (
                await BillingReadbackService(
                    session, client_factory=reads, environment="test"
                ).readback_payment(pay, delivery_id=deliveries[0])
                == "converged"
            ), status
        assert await _cycles(harness, builder_id) == captured, status
        assert _shown(await _summary(harness, builder_id)) == paid, status
    assert await _settlements(harness, builder_id) == [
        (pay, "settlement", "settlement:v1")
    ]


async def test_partial_refund_of_a_multi_line_subscription_reads_pending(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plus for 3 active clinics is two lines (plan + 2 additional clinics,
    139,900 cents).  A completed partial refund cannot be split across the
    lines, so the outbox quarantines the cycle and the summary reads
    ``pending`` with no period end (not ``reversed``).  The classification is
    an open owner choice; this pins today's behaviour."""

    _configure(monkeypatch, "test", _Flag())
    await _general(harness, "test")
    builder_id = await _builder(harness, "G6b-2 multi-line partial")
    async with harness.session() as session:
        async with session.begin():
            for index in range(3):
                await session.execute(
                    text(
                        "INSERT INTO tenants (id, name, api_key_hash, is_active, "
                        "builder_id, plan_type, config) VALUES (:id, :name, "
                        ":hash, true, :b, 'pilot', '{}'::jsonb)"
                    ),
                    {
                        "id": uuid4(),
                        "name": f"G6b-2 clinic {index}",
                        "hash": f"hash-{uuid4().hex}",
                        "b": builder_id,
                    },
                )
    provider = _Provider(harness)
    async with harness.session() as session:
        view = await _service(session, provider.client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )
    (snapshot,) = await _all(
        harness,
        "SELECT amount_cents, pricing_snapshot ->> 'additional_clinics' "
        "FROM billing_checkout_sessions WHERE id = :i",
        i=view.checkout_session_id,
    )
    cents = int(snapshot[0])
    assert (cents, snapshot[1]) == (99_900 + 2 * 20_000, "2")
    checkout_id = await _external_checkout(harness, view.checkout_session_id)
    sub = "sub_g6b2_multi_line"
    pay = "pay_g6b2_multi_line"
    await _deliver(
        harness, _subscription_created(view.checkout_session_id, sub, cents), "test"
    )

    def event(name: str, when: str) -> dict[str, Any]:
        return _card(
            name,
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when=when,
            due="2026-10-11",
        )

    await _deliver(harness, event("PAYMENT_CONFIRMED", "2026-10-11 10:00:00"), "test")
    _at(monkeypatch, _MID_CYCLE)
    assert _shown(await _summary(harness, builder_id)) == (
        "active",
        date(2026, 11, 11),
        "paid",
    )
    partial = await _deliver(
        harness, event("PAYMENT_PARTIALLY_REFUNDED", "2026-10-12 10:00:00"), "test"
    )
    assert partial.outcome == "readback_required"
    read = _capture_read(
        pay, subscription=sub, checkout=checkout_id, cents=cents, refunded=10_000
    )
    await _readback(harness, partial.delivery_id, _Reads(payments={pay: read}))
    assert await _cycles(harness, builder_id) == [
        (pay, "quarantined", date(2026, 10, 11), date(2026, 11, 11), cents, 10_000, 0)
    ]
    assert _shown(await _summary(harness, builder_id)) == ("active", None, "pending")
