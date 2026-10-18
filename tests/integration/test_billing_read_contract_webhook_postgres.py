"""``current_period_end`` through the real webhook/readback path (Group 6b).

A real Plus subscription is settled, reversed and restored by the real
``BillingWebhookService``/``BillingReadbackService`` on PostgreSQL, and the
summary is read through the real route inside a ``READ ONLY`` transaction after
every step.  The harness (its own disposable child database) is the webhook
suite's, so the cycle rows are exactly what production code writes.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from src.api.routes import billing
from src.api.schemas.billing import BillingSummaryResponse
from src.services.billing_webhook import BillingReadbackService
from tests.integration import test_asaas_webhook_postgres as webhook_suite
from tests.integration.test_asaas_webhook_postgres import (
    _all,
    _brl,
    _deliver,
    _payment,
    _read,
    _Reads,
    _subscription,
    _subscription_created,
)

pytestmark = pytest.mark.integration

# The webhook suite's module-scoped disposable database.
harness = webhook_suite.harness


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
