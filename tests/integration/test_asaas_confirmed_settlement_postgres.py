"""Real PostgreSQL proofs that PAYMENT_CONFIRMED settles and RECEIVED is inert.

Owner decision 2026-10-18 (confirmed-settles DESIGN-DELTA).  Every row is
written by the real ``BillingWebhookService``/``BillingReadbackService``
against migrations 026-035 (triggers, CHECKs, unique keys, the canary slot);
concurrency claims use separate connections and prove each waiter is blocked
on a lock before the first commits.  Card capture payloads follow the ASAAS
sandbox capture (``confirmedDate = clientPaymentDate =`` capture day,
``paymentDate`` null); the RECEIVED card field values are unobserved and
follow the docs sample with ``paymentDate`` moved to the funds day.
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.api.routes import billing
from src.services.asaas import PaymentReadback, _payment_readback
from src.services.billing_webhook import BillingReadbackService, BillingWebhookService
from tests.integration import test_asaas_webhook_postgres as webhook_suite
from tests.integration.test_asaas_billing_postgres import (
    _create_child,
    _drop_child,
    _run_alembic,
    _validated_admin_url,
)
from tests.integration.test_asaas_checkout_service_postgres import (
    _builder,
    _configure,
    _Flag,
    _Harness,
    _Provider,
    _service,
    _transition,
)
from tests.integration.test_asaas_webhook_postgres import (
    _all,
    _brl,
    _deliver,
    _envelope,
    _external_checkout,
    _payload,
    _payment,
    _pid,
    _read,
    _Reads,
    _scalar,
    _subscription,
    _subscription_created,
    _topup,
    _wait_until_blocked,
)

pytestmark = pytest.mark.integration

# This module's own disposable database (module-scoped fixture).
harness = webhook_suite.harness

CAPTURE_DAY = date(2026, 10, 11)


def _card(
    event: str,
    payment_id: str,
    *,
    subscription: str,
    cents: int,
    when: str,
    due: str = "2026-10-11",
    checkout: str | None = None,
    funds_day: str | None = None,
    capture_day: str | None = None,
    capture_dates: bool = True,
) -> dict[str, Any]:
    payload = _payment(
        event,
        payment_id,
        checkout=checkout,
        subscription=subscription,
        value=_brl(cents),
        when=when,
        payment_date=funds_day,
        capture_dates=capture_dates,
    )
    payload["payment"]["dueDate"] = due
    if capture_day is not None:
        payload["payment"]["confirmedDate"] = capture_day
        payload["payment"]["clientPaymentDate"] = capture_day
    return payload


def _card_read(
    payment_id: str,
    *,
    subscription: str,
    checkout: str | None,
    cents: int,
    status: str = "CONFIRMED",
    refunded: int = 0,
    capture_day: date | None = CAPTURE_DAY,
    payment_day: date | None = None,
) -> PaymentReadback:
    return PaymentReadback(
        payment_id,
        status,
        False,
        cents,
        subscription,
        checkout,
        None,
        "CREDIT_CARD",
        refunded,
        due_date=CAPTURE_DAY,
        payment_date=payment_day,
        confirmed_date=capture_day,
        client_payment_date=capture_day,
    )


async def _readback(
    harness: Any, delivery_id: UUID, reads: _Reads, environment: str = "test"
) -> str:
    async with harness.session() as session:
        return await BillingReadbackService(
            session, client_factory=reads, environment=environment
        ).process_delivery(delivery_id)


async def _summary_period_end(harness: Any, builder_id: UUID) -> Any:
    async with harness.session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        response = await billing.billing_summary(builder_id, session)
        await session.rollback()
    assert response.subscription is not None
    return response.subscription.current_period_end


async def _counts(harness: Any, builder_id: UUID) -> tuple[int, ...]:
    """(effects, facts, Transactions, cycles) of one builder."""

    (row,) = await _all(
        harness,
        "SELECT (SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b), "
        "(SELECT count(*) FROM billing_outbox WHERE builder_id = :b), "
        "(SELECT count(*) FROM transactions WHERE builder_id = :b), "
        "(SELECT count(*) FROM billing_subscription_cycles WHERE builder_id = :b)",
        b=builder_id,
    )
    return tuple(row)


async def _linked(
    harness: Any, monkeypatch: pytest.MonkeyPatch, label: str, sub: str
) -> tuple[UUID, str, int]:
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, label
    )
    created = await _deliver(
        harness, _subscription_created(session_id, sub, cents), "test"
    )
    assert created.outcome == "processed"
    return builder_id, checkout_id, cents


async def _subscription_status(harness: Any, builder_id: UUID) -> str:
    return str(
        await _scalar(
            harness,
            "SELECT status FROM billing_subscriptions WHERE builder_id = :b",
            b=builder_id,
        )
    )


# ---------------------------------------------------------------------------
# Concurrency: CONFIRMED, RECEIVED and a second CONFIRMED (new event id).
# ---------------------------------------------------------------------------


async def test_three_way_race_settles_once_on_the_capture(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "race 3")
    payment = "pay_pg_race3"
    confirmed = _envelope(
        _payment(
            "PAYMENT_CONFIRMED",
            payment,
            checkout=checkout_id,
            when="2026-10-11 12:00:00",
        )
    )
    received = _envelope(
        _payment(
            "PAYMENT_RECEIVED",
            payment,
            checkout=checkout_id,
            when="2026-11-12 08:00:00",
            payment_date="2026-11-12",
        )
    )
    again = _envelope(
        _payment(
            "PAYMENT_CONFIRMED",
            payment,
            checkout=checkout_id,
            when="2026-10-11 12:00:00",
        )
    )
    assert len({confirmed.event_id, received.event_id, again.event_id}) == 3
    first, second, third = harness.session(), harness.session(), harness.session()
    try:
        held = await BillingWebhookService(first, environment="test")._receive(
            confirmed
        )
        waiters = []
        for session, envelope in ((second, received), (third, again)):
            pid = await _pid(session)
            waiters.append(
                asyncio.create_task(
                    BillingWebhookService(session, environment="test").receive(envelope)
                )
            )
            assert await _wait_until_blocked(harness, pid), "waiter did not block"
        assert not any(task.done() for task in waiters)
        await first.commit()
        late = [await asyncio.wait_for(task, timeout=30) for task in waiters]
    finally:
        for session in (first, second, third):
            await session.close()
    assert [o.outcome for o in (held, *late)] == ["processed"] * 3
    (effect,) = await _all(
        harness,
        "SELECT source_revision, delivery_id FROM billing_payment_effects "
        "WHERE builder_id = :b",
        b=builder_id,
    )
    assert tuple(effect) == ("settlement:v1", held.delivery_id)
    (row,) = await _all(
        harness,
        "SELECT p.status, p.paid_at, (SELECT count(*) FROM transactions "
        "WHERE builder_id = :b), (SELECT count(*) FROM billing_outbox "
        "WHERE builder_id = :b), (SELECT count(*) FROM billing_webhook_deliveries "
        "WHERE builder_id = :b AND resource_id = :p AND status = 'processed') "
        "FROM billing_payment_projections p WHERE p.payment_id = :p",
        b=builder_id,
        p=payment,
    )
    assert tuple(row) == ("settled", CAPTURE_DAY, 1, 1, 3)


async def test_received_first_then_an_older_capture_is_inert_on_postgres(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = "sub_pg_recv_first"
    builder_id, checkout_id, cents = await _linked(
        harness, monkeypatch, "received first", sub
    )
    pay = "pay_pg_recv_first"
    received = await _deliver(
        harness,
        _card(
            "PAYMENT_RECEIVED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-11-12 08:00:00",
            funds_day="2026-11-12",
            capture_day="2026-10-11",
        ),
        "test",
    )
    assert received.outcome == "processed"
    older = await _deliver(
        harness,
        _card(
            "PAYMENT_CONFIRMED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-10-11 09:00:00",
        ),
        "test",
    )
    assert older.outcome == "processed"
    assert await _counts(harness, builder_id) == (1, 1, 1, 1)
    (row,) = await _all(
        harness,
        "SELECT p.status, p.paid_at, c.cycle_start, c.financial_revision, "
        "e.delivery_id FROM billing_payment_projections p "
        "JOIN billing_subscription_cycles c ON c.id = p.subscription_cycle_id "
        "JOIN billing_payment_effects e ON e.projection_id = p.id "
        "WHERE p.payment_id = :p",
        p=pay,
    )
    assert tuple(row) == (
        "settled",
        CAPTURE_DAY,
        CAPTURE_DAY,
        1,
        received.delivery_id,
    )
    assert await _subscription_status(harness, builder_id) == "active"


# ---------------------------------------------------------------------------
# Canary: the lifetime slot is consumed by the capture; the receipt is inert.
# ---------------------------------------------------------------------------


async def test_canary_capture_consumes_the_slot_and_the_receipt_stays_inert(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "staging"
    _configure(monkeypatch, environment, _Flag())
    builder_id = await _builder(harness, "canary capture")
    await _transition(harness, environment, "canary_allowlist", builder_id)
    async with harness.session() as session:
        view = await _service(session, _Provider(harness).client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )
    checkout_id = await _external_checkout(harness, view.checkout_session_id)
    sub, pay = "sub_pg_canary_cap", "pay_pg_canary_cap"
    await _deliver(
        harness,
        _subscription_created(view.checkout_session_id, sub, 99_900),
        environment,
    )

    async def slot_count() -> int:
        return int(
            await _scalar(
                harness,
                "SELECT canary_settlement_count FROM billing_rollout_control "
                "WHERE environment = :env",
                env=environment,
            )
        )

    # A capture without a usable date projects nothing and activates nothing
    # (the DB pins canary projection writes to an active Plus row).
    undated = await _deliver(
        harness,
        _card(
            "PAYMENT_CONFIRMED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=99_900,
            when="2026-10-11 09:00:00",
            capture_dates=False,
        ),
        environment,
    )
    assert undated.outcome == "readback_required"
    assert await _subscription_status(harness, builder_id) == "active_linked"
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM billing_payment_projections WHERE builder_id = :b",
            b=builder_id,
        )
        == 0
    )
    assert await slot_count() == 0
    # The exact read of the capture settles into the slot.
    reads = _Reads(
        payments={
            pay: _card_read(pay, subscription=sub, checkout=checkout_id, cents=99_900)
        }
    )
    assert (
        await _readback(harness, undated.delivery_id, reads, environment) == "converged"
    )
    assert await slot_count() == 1
    assert await _subscription_status(harness, builder_id) == "active"
    # SUBSCRIPTION_UPDATED, then the funds receipt: nothing moves.
    await _deliver(harness, _payload("SUBSCRIPTION_UPDATED", {"id": sub}), environment)
    received = await _deliver(
        harness,
        _card(
            "PAYMENT_RECEIVED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=99_900,
            when="2026-11-12 08:00:00",
            funds_day="2026-11-12",
            capture_day="2026-10-11",
        ),
        environment,
    )
    assert received.outcome == "processed"
    assert await slot_count() == 1
    (row,) = await _all(
        harness,
        "SELECT p.status, p.canary_slot, p.paid_at, e.canary_slot, "
        "c.canary_subscription_effect_id = e.id FROM billing_payment_projections p "
        "JOIN billing_payment_effects e ON e.projection_id = p.id "
        "JOIN billing_rollout_control c ON c.environment = :env "
        "WHERE p.payment_id = :p",
        p=pay,
        env=environment,
    )
    assert tuple(row) == ("settled", "subscription", CAPTURE_DAY, "subscription", True)
    assert await _counts(harness, builder_id) == (1, 1, 1, 1)


async def test_canary_event_capture_then_subscription_updated_then_receipt(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA M6: CONFIRMED (event) -> SUBSCRIPTION_UPDATED -> RECEIVED."""

    environment = "production"
    _configure(monkeypatch, environment, _Flag())
    builder_id = await _builder(harness, "canary event capture")
    await _transition(harness, environment, "canary_allowlist", builder_id)
    async with harness.session() as session:
        view = await _service(session, _Provider(harness).client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )
    checkout_id = await _external_checkout(harness, view.checkout_session_id)
    sub, pay = "sub_pg_canary_evt", "pay_pg_canary_evt"
    await _deliver(
        harness,
        _subscription_created(view.checkout_session_id, sub, 99_900),
        environment,
    )

    async def slot() -> tuple[Any, ...]:
        (row,) = await _all(
            harness,
            "SELECT c.canary_settlement_count, s.status FROM billing_rollout_control c "
            "JOIN billing_subscriptions s ON s.builder_id = :b "
            "WHERE c.environment = :env",
            b=builder_id,
            env=environment,
        )
        return tuple(row)

    captured = await _deliver(
        harness,
        _card(
            "PAYMENT_CONFIRMED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=99_900,
            when="2026-10-11 09:00:00",
        ),
        environment,
    )
    assert captured.outcome == "processed"
    assert await slot() == (1, "active")
    updated = await _deliver(
        harness, _payload("SUBSCRIPTION_UPDATED", {"id": sub}), environment
    )
    assert updated.outcome == "readback_required"
    assert await slot() == (1, "readback_required")
    received = await _deliver(
        harness,
        _card(
            "PAYMENT_RECEIVED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=99_900,
            when="2026-11-12 08:00:00",
            funds_day="2026-11-12",
            capture_day="2026-10-11",
        ),
        environment,
    )
    assert received.outcome == "processed"
    assert await slot() == (1, "readback_required")
    assert await _counts(harness, builder_id) == (1, 1, 1, 1)
    (effect,) = await _all(
        harness,
        "SELECT delivery_id, canary_slot FROM billing_payment_effects "
        "WHERE builder_id = :b",
        b=builder_id,
    )
    assert tuple(effect) == (captured.delivery_id, "subscription")


# ---------------------------------------------------------------------------
# Renewals and the read contract.
# ---------------------------------------------------------------------------


async def test_renewals_n_and_n_plus_one_then_n_received_on_postgres(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = "sub_pg_renewals"
    builder_id, checkout_id, cents = await _linked(
        harness, monkeypatch, "renewals", sub
    )
    steps = (
        _card(
            "PAYMENT_CONFIRMED",
            "pay_pg_ren_n",
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-10-11 09:00:00",
        ),
        _card(
            "PAYMENT_CONFIRMED",
            "pay_pg_ren_n1",
            subscription=sub,
            cents=cents,
            when="2026-11-11 09:00:00",
            due="2026-11-11",
        ),
        _card(
            "PAYMENT_RECEIVED",
            "pay_pg_ren_n",
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-11-12 08:00:00",
            funds_day="2026-11-12",
            capture_day="2026-10-11",
        ),
    )
    for payload in steps:
        outcome = await _deliver(harness, payload, "test")
        assert outcome.outcome == "processed", payload["event"]
    cycles = await _all(
        harness,
        "SELECT c.payment_id, c.cycle_start, c.cycle_end, c.financial_revision, "
        "c.state, p.paid_at FROM billing_subscription_cycles c "
        "JOIN billing_payment_projections p ON p.subscription_cycle_id = c.id "
        "WHERE c.builder_id = :b ORDER BY c.cycle_start",
        b=builder_id,
    )
    assert [tuple(c) for c in cycles] == [
        (
            "pay_pg_ren_n",
            date(2026, 10, 11),
            date(2026, 11, 11),
            1,
            "settled",
            CAPTURE_DAY,
        ),
        (
            "pay_pg_ren_n1",
            date(2026, 11, 11),
            date(2026, 12, 11),
            1,
            "settled",
            date(2026, 11, 11),
        ),
    ]
    assert await _counts(harness, builder_id) == (2, 2, 2, 2)
    assert await _summary_period_end(harness, builder_id) == date(2026, 12, 11)


async def test_refund_after_capture_before_receipt_on_postgres(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = "sub_pg_window_refund"
    builder_id, checkout_id, cents = await _linked(
        harness, monkeypatch, "window refund", sub
    )
    pay = "pay_pg_window_refund"

    def card(event: str, when: str, **extra: Any) -> dict[str, Any]:
        return _card(
            event,
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when=when,
            **extra,
        )

    await _deliver(harness, card("PAYMENT_CONFIRMED", "2026-10-11 09:00:00"), "test")
    assert await _summary_period_end(harness, builder_id) == date(2026, 11, 11)
    refunded = await _deliver(
        harness, card("PAYMENT_REFUNDED", "2026-10-11 18:06:07"), "test"
    )
    assert refunded.outcome == "processed"
    late = await _deliver(
        harness,
        card(
            "PAYMENT_RECEIVED",
            "2026-11-12 08:00:00",
            funds_day="2026-11-12",
            capture_day="2026-10-11",
        ),
        "test",
    )
    # L-3: a receipt after the refund contradicts the reversed label: read.
    assert late.outcome == "readback_required"
    reads = _Reads(
        payments={
            pay: _card_read(
                pay,
                subscription=sub,
                checkout=checkout_id,
                cents=cents,
                status="REFUNDED",
                refunded=cents,
            )
        }
    )
    assert await _readback(harness, late.delivery_id, reads) == "converged"
    effects = await _all(
        harness,
        "SELECT effect_kind, source_revision, amount_cents FROM billing_payment_effects "
        "WHERE builder_id = :b ORDER BY effect_kind",
        b=builder_id,
    )
    assert [tuple(e) for e in effects] == [
        ("reversal", f"reversed_cents:{cents}", cents),
        ("settlement", "settlement:v1", cents),
    ]
    (row,) = await _all(
        harness,
        "SELECT p.status, p.reversed_cents, p.pre_settlement_refund_cents, p.paid_at, "
        "c.state, c.financial_revision FROM billing_payment_projections p "
        "JOIN billing_subscription_cycles c ON c.id = p.subscription_cycle_id "
        "WHERE p.payment_id = :p",
        p=pay,
    )
    assert tuple(row) == ("reversed", cents, None, CAPTURE_DAY, "reversed", 2)
    facts = await _all(
        harness,
        "SELECT financial_revision, payload ->> 'paid_at' FROM billing_outbox "
        "WHERE builder_id = :b ORDER BY financial_revision",
        b=builder_id,
    )
    assert [tuple(f) for f in facts] == [(1, "2026-10-11"), (2, "2026-10-11")]
    assert await _summary_period_end(harness, builder_id) is None


# ---------------------------------------------------------------------------
# Owner decision Q-A / reviewer F1: CONFIRMED read with DONE refunds.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "driver", ["confirmed_value_drift", "confirmed_without_date", "partial_refund"]
)
@pytest.mark.parametrize("dated", [True, False], ids=["capture-dates", "no-date"])
async def test_q_a_confirmed_read_with_refunds_settles_then_reverses_on_postgres(
    harness: Any, monkeypatch: pytest.MonkeyPatch, driver: str, dated: bool
) -> None:
    sub = f"sub_pg_qa_{driver[:12]}_{int(dated)}"
    builder_id, checkout_id, cents = await _linked(
        harness, monkeypatch, f"Q-A {driver} {dated}", sub
    )
    pay = f"pay_pg_qa_{driver[:12]}_{int(dated)}"
    when = "2026-10-11 09:00:00"
    if driver == "confirmed_value_drift":
        payload = _card(
            "PAYMENT_CONFIRMED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents + 100,
            when=when,
        )
    elif driver == "confirmed_without_date":
        payload = _card(
            "PAYMENT_CONFIRMED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when=when,
            capture_dates=False,
        )
    else:
        payload = _card(
            "PAYMENT_PARTIALLY_REFUNDED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-10-12 09:00:00",
        )
    first = await _deliver(harness, payload, "test")
    assert first.outcome == "readback_required"
    reads = _Reads(
        payments={
            pay: _card_read(
                pay,
                subscription=sub,
                checkout=checkout_id,
                cents=cents,
                refunded=10_000,
                capture_day=CAPTURE_DAY if dated else None,
            )
        }
    )
    outcome = await _readback(harness, first.delivery_id, reads)
    effects = await _all(
        harness,
        "SELECT effect_kind, source_revision, amount_cents FROM billing_payment_effects "
        "WHERE builder_id = :b ORDER BY effect_kind",
        b=builder_id,
    )
    projections = await _all(
        harness,
        "SELECT status, settled_cents, reversed_cents, paid_at, "
        "pre_settlement_refund_cents FROM billing_payment_projections "
        "WHERE payment_id = :p",
        p=pay,
    )
    if dated:
        assert outcome == "converged"
        assert [tuple(e) for e in effects] == [
            ("reversal", "reversed_cents:10000", 10_000),
            ("settlement", "settlement:v1", cents),
        ]
        assert [tuple(p) for p in projections] == [
            ("partially_reversed", cents, 10_000, CAPTURE_DAY, None)
        ]
        assert await _subscription_status(harness, builder_id) == "active"
        (cycle,) = await _all(
            harness,
            "SELECT state, financial_revision FROM billing_subscription_cycles "
            "WHERE builder_id = :b",
            b=builder_id,
        )
        # Policy B: any reversal makes the cycle ``reversed`` (a partially
        # refunded capture does not count as paid in this window either).
        assert tuple(cycle) == ("reversed", 2)
        assert await _summary_period_end(harness, builder_id) is None
    else:
        assert outcome == "drift"
        assert effects == []
        assert all(p[0] != "refunded_before_settlement" for p in projections)
        assert await _subscription_status(harness, builder_id) == "active_linked"


# ---------------------------------------------------------------------------
# Review fix loop 1, M-2: canary refund-first orderings on fresh databases.
# The canary budget is one lifetime subscription per environment, so every
# ordering runs on its own clone of one migrated (head) template database.
# ---------------------------------------------------------------------------

_SP = ZoneInfo("America/Sao_Paulo")
_REVERSAL_CLASS = (
    "PAYMENT_REFUND_IN_PROGRESS",
    "PAYMENT_PARTIALLY_REFUNDED",
    "PAYMENT_REFUNDED",
    "PAYMENT_CHARGEBACK_REQUESTED",
    "PAYMENT_CHARGEBACK_DISPUTE",
    "PAYMENT_AWAITING_CHARGEBACK_REVERSAL",
)
_SETTLE_CLASS = ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED")


@dataclass
class _Template:
    admin: AsyncEngine
    admin_url: URL
    name: str


@pytest.fixture(scope="module")
async def canary_template() -> AsyncIterator[_Template]:
    raw = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    await _create_child(admin, name)
    try:
        await _run_alembic(admin_url.set(database=name), "upgrade", "head")
        yield _Template(admin=admin, admin_url=admin_url, name=name)
    finally:
        await _drop_child(admin, name)
        await admin.dispose()


@asynccontextmanager
async def _clone(template: _Template) -> AsyncIterator[_Harness]:
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    async with template.admin.connect() as connection:
        await connection.exec_driver_sql(
            f'CREATE DATABASE "{name}" TEMPLATE "{template.name}"'
        )
    engine = create_async_engine(
        template.admin_url.set(database=name), poolclass=NullPool
    )
    try:
        yield _Harness(
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
        )
    finally:
        await engine.dispose()
        await _drop_child(template.admin, name)


def _utc(when: str) -> datetime:
    local = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_SP)
    return local.astimezone(timezone.utc)


_CAPTURE = {
    "status": "CONFIRMED",
    "confirmedDate": "2026-10-11",
    "clientPaymentDate": "2026-10-11",
    "paymentDate": None,
    "creditDate": "2026-11-12",
    "estimatedCreditDate": "2026-11-12",
}
_FULL_REFUND = {
    "status": "REFUNDED",
    "refunds": [{"status": "DONE", "value": Decimal("999.00")}],
}
# Provider timelines (name, event, envelope time, provider state update),
# mirroring the reviewer's matrix C02/C03/C05/C12/C14 for the canary Plus.
_TIMELINES: dict[str, list[tuple[str, str, str, dict[str, Any]]]] = {
    "C02": [
        ("CREATED", "PAYMENT_CREATED", "2026-10-11 09:00:00", {}),
        ("CONFIRMED", "PAYMENT_CONFIRMED", "2026-10-11 10:00:00", _CAPTURE),
        ("REFUNDED", "PAYMENT_REFUNDED", "2026-10-11 15:00:00", _FULL_REFUND),
    ],
    "C03": [
        ("CREATED", "PAYMENT_CREATED", "2026-10-11 09:00:00", {}),
        ("CONFIRMED", "PAYMENT_CONFIRMED", "2026-10-11 10:00:00", _CAPTURE),
        (
            "RECEIVED",
            "PAYMENT_RECEIVED",
            "2026-11-12 08:00:00",
            {"status": "RECEIVED", "paymentDate": "2026-11-12"},
        ),
        ("REFUNDED", "PAYMENT_REFUNDED", "2026-11-17 11:00:00", _FULL_REFUND),
    ],
    "C05": [
        ("CONFIRMED", "PAYMENT_CONFIRMED", "2026-10-11 10:00:00", _CAPTURE),
        (
            "CB_REQUESTED",
            "PAYMENT_CHARGEBACK_REQUESTED",
            "2026-10-16 10:00:00",
            {"status": "CHARGEBACK_REQUESTED"},
        ),
        (
            "CB_DISPUTE",
            "PAYMENT_CHARGEBACK_DISPUTE",
            "2026-10-21 10:00:00",
            {"status": "CHARGEBACK_DISPUTE"},
        ),
        (
            "CB_REVERSAL",
            "PAYMENT_AWAITING_CHARGEBACK_REVERSAL",
            "2026-10-31 10:00:00",
            {"status": "AWAITING_CHARGEBACK_REVERSAL"},
        ),
        ("WON", "PAYMENT_CONFIRMED", "2026-11-05 10:00:00", {"status": "CONFIRMED"}),
    ],
    "C12": [
        ("CONFIRMED", "PAYMENT_CONFIRMED", "2026-10-11 10:00:00", _CAPTURE),
        (
            "PARTIAL",
            "PAYMENT_PARTIALLY_REFUNDED",
            "2026-10-13 08:00:00",
            {"refunds": [{"status": "DONE", "value": Decimal("30.00")}]},
        ),
    ],
    "C14": [
        ("CONFIRMED", "PAYMENT_CONFIRMED", "2026-10-11 10:00:00", _CAPTURE),
        (
            "IN_PROGRESS",
            "PAYMENT_REFUND_IN_PROGRESS",
            "2026-10-12 10:00:00",
            {"status": "REFUND_IN_PROGRESS"},
        ),
        ("REFUNDED", "PAYMENT_REFUNDED", "2026-10-13 10:00:00", _FULL_REFUND),
    ],
}
# The reviewer's refund-first canary orderings (SUB = SUBSCRIPTION_CREATED),
# plus probes p_p (refund, read, then the older capture) and p_r (partial).
_ORDERINGS: dict[str, tuple[str, list[str]]] = {
    "C02-perm1": ("C02", ["CREATED", "SUB", "REFUNDED", "CONFIRMED"]),
    "C02-rev": ("C02", ["REFUNDED", "CONFIRMED", "CREATED", "SUB"]),
    "C02-p_p": ("C02", ["SUB", "REFUNDED", "CONFIRMED"]),
    "C03-rev": ("C03", ["REFUNDED", "RECEIVED", "CONFIRMED", "CREATED", "SUB"]),
    "C05-first_last": (
        "C05",
        ["CB_REQUESTED", "CB_DISPUTE", "CB_REVERSAL", "WON", "CONFIRMED", "SUB"],
    ),
    "C12-p_r": ("C12", ["SUB", "PARTIAL", "CONFIRMED"]),
    "C14-first_last": ("C14", ["IN_PROGRESS", "REFUNDED", "CONFIRMED", "SUB"]),
    "C14-perm1": ("C14", ["IN_PROGRESS", "SUB", "REFUNDED", "CONFIRMED"]),
    "C14-rev": ("C14", ["REFUNDED", "IN_PROGRESS", "CONFIRMED", "SUB"]),
}


@pytest.mark.parametrize("mode", ["end", "immediate"])
@pytest.mark.parametrize("ordering", list(_ORDERINGS))
async def test_m2_canary_refund_first_orderings_never_grant_from_an_event(
    canary_template: _Template,
    monkeypatch: pytest.MonkeyPatch,
    ordering: str,
    mode: str,
) -> None:
    """No settle-class EVENT settles a canary payment while one of its
    reversal-class deliveries is open; the final money is the provider's
    truth, and a refund-first payment never spends the canary slot unless a
    read proved the capture first."""

    timeline_name, order = _ORDERINGS[ordering]
    timeline = _TIMELINES[timeline_name]
    environment = "development"
    async with _clone(canary_template) as h:
        _configure(monkeypatch, environment, _Flag())
        builder_id = await _builder(h, f"m2 {ordering} {mode}")
        await _transition(h, environment, "canary_allowlist", builder_id)
        async with h.session() as session:
            view = await _service(session, _Provider(h).client).create_checkout(
                builder_id=builder_id,
                offer_id="plan_plus_monthly_v1",
                payment_method="credit_card",
            )
        checkout_id = await _external_checkout(h, view.checkout_session_id)
        sub, pay = "sub_pg_m2_matrix", "pay_pg_m2_matrix"
        state: dict[str, Any] = {
            "object": "payment",
            "id": pay,
            "subscription": sub,
            "checkoutSession": checkout_id,
            "value": Decimal("999.00"),
            "billingType": "CREDIT_CARD",
            "externalReference": None,
            "status": "PENDING",
            "dueDate": "2026-10-11",
            "confirmedDate": None,
            "paymentDate": None,
            "clientPaymentDate": None,
            "refunds": None,
            "deleted": False,
        }
        snapshots: dict[str, dict[str, Any]] = {}
        index: dict[str, int] = {}
        for position, (label, _event, _when, update) in enumerate(timeline):
            state = {**state, **update}
            snapshots[label] = dict(state)
            index[label] = position
        final_label = timeline[-1][0]
        reads = _Reads()

        async def read_round(label: str, clock: datetime) -> None:
            reads.payments[pay] = _payment_readback(snapshots[label])
            pending = await _all(
                h,
                "SELECT id FROM billing_webhook_deliveries WHERE builder_id = :b "
                "AND status IN ('readback_required', 'unknown') "
                "AND resource_kind IS NOT NULL ORDER BY received_at, id",
                b=builder_id,
            )
            for (delivery_id,) in pending:
                async with h.session() as session:
                    await BillingReadbackService(
                        session,
                        client_factory=reads,
                        environment=environment,
                        clock=lambda: clock,
                    ).process_delivery(delivery_id)

        async def grant() -> tuple[Any, ...]:
            (row,) = await _all(
                h,
                "SELECT (SELECT canary_settlement_count FROM billing_rollout_control "
                "WHERE environment = :env), "
                "(SELECT status FROM billing_subscriptions WHERE builder_id = :b), "
                "(SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b "
                "AND effect_kind = 'settlement')",
                env=environment,
                b=builder_id,
            )
            return tuple(row)

        reversal_names = ", ".join(f"'{name}'" for name in _REVERSAL_CLASS)
        latest = -1
        guarded = 0
        for label in order:
            if label == "SUB":
                payload = _subscription_created(view.checkout_session_id, sub, 99_900)
                await _deliver(h, payload, environment)
                continue
            _label, event, when, _update = timeline[index[label]]
            open_reversals = 0
            if event in _SETTLE_CLASS:
                open_reversals = int(
                    await _scalar(
                        h,
                        "SELECT count(*) FROM billing_webhook_deliveries "
                        "WHERE resource_id = :p AND status <> 'processed' "
                        f"AND event_name IN ({reversal_names})",
                        p=pay,
                    )
                )
            before = await grant()
            outcome = await _deliver(
                h,
                {
                    "id": webhook_suite._documented_id(),
                    "event": event,
                    "dateCreated": when,
                    "payment": snapshots[label],
                },
                environment,
            )
            if open_reversals:
                guarded += 1
                assert outcome.outcome == "readback_required", (label, ordering)
                assert await grant() == before, (label, ordering)
            latest = max(latest, index[label])
            if mode == "immediate":
                seen = timeline[latest]
                await read_round(seen[0], _utc(seen[2]) + timedelta(minutes=1))
        end_clock = max(_utc(step[2]) for step in timeline) + timedelta(hours=1)
        for _round in range(4):
            await read_round(final_label, end_clock)
        canary_count, sub_status, settlements = await grant()
        projections = await _all(
            h,
            "SELECT status, settled_cents, reversed_cents, paid_at "
            "FROM billing_payment_projections WHERE payment_id = :p",
            p=pay,
        )
        transactions = await _scalar(
            h, "SELECT count(*) FROM transactions WHERE builder_id = :b", b=builder_id
        )
        plan = await _scalar(
            h, "SELECT subscription_plan FROM builders WHERE id = :b", b=builder_id
        )
    settled = projections[0][1] if projections else 0
    reversed_cents = projections[0][2] if projections else 0
    assert settlements <= 1 and transactions == settlements
    assert canary_count == (1 if settled else 0)
    if timeline_name == "C05":  # won dispute: the capture stands
        assert (settled, reversed_cents, sub_status) == (99_900, 0, "active")
    elif timeline_name == "C12":  # capture with a partial refund (policy B)
        assert (settled, reversed_cents) == (99_900, 3_000)
    elif mode == "end" or settled == 0:
        # Every read saw the completed refund: the canary marker cannot be
        # stored, nothing was granted and the slot is unspent.
        assert (projections, canary_count, sub_status) == ([], 0, "active_linked")
        assert plan != "plus"
    else:
        # An earlier read proved the capture (exact provider evidence), then
        # the refund reversed it in full.
        assert (settled, reversed_cents) == (99_900, 99_900)
    if mode == "end" and order.index(final_label) < order.index("CONFIRMED"):
        # With every read deferred, the late capture met an open reversal.
        assert guarded >= 1, "the ordering never exercised the guard"


# ---------------------------------------------------------------------------
# Review probes p_c, p_s, p_t on the module database.
# ---------------------------------------------------------------------------


_RUNBOOK = (
    Path(__file__).resolve().parents[2]
    / "docs/runbooks/asaas-billing-reconciliation.md"
)


def _runbook_sql(heading: str) -> str:
    """The one SQL block of a runbook subsection (the doc stays executable)."""

    section = _RUNBOOK.read_text(encoding="utf-8").split(heading, 1)[1]
    (sql,) = re.findall(r"```sql\n(.*?)```", section.split("\n### ", 1)[0], re.S)
    return sql


@pytest.mark.parametrize(
    ("confirmed", "client", "expected"),
    [
        (None, "2026-10-11", CAPTURE_DAY),
        ("2026-10-11", "2026-10-11", CAPTURE_DAY),
        # Documented accepted case (owner decision 2026-10-18): the funds day
        # is the only date sent, so it becomes paid_at; never refused.
        (None, None, date(2026, 11, 12)),
    ],
    ids=["client-capture-day", "confirmed-capture-day", "funds-day-only"],
)
async def test_p_c_received_first_card_dates_on_postgres(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    confirmed: str | None,
    client: str | None,
    expected: date,
) -> None:
    _builder_id, _session, checkout_id = await _topup(
        harness, monkeypatch, f"p_c {confirmed} {client}"
    )
    pay = f"pay_pg_pc_{uuid4().hex[:10]}"
    payload = _payment(
        "PAYMENT_RECEIVED",
        pay,
        checkout=checkout_id,
        when="2026-11-12 08:00:00",
        payment_date="2026-11-12",
    )
    payload["payment"].update(
        billingType="CREDIT_CARD",
        confirmedDate=confirmed,
        clientPaymentDate=client,
        creditDate="2026-11-12",
    )
    assert (await _deliver(harness, payload, "test")).outcome == "processed"
    assert (
        await _scalar(
            harness,
            "SELECT paid_at FROM billing_payment_projections WHERE payment_id = :p",
            p=pay,
        )
        == expected
    )
    # The runbook's read-only detection: with no PAYMENT_CONFIRMED delivery
    # every settled card payment is a candidate to check in the panel; once
    # the late capture shows up (inert; paid_at never moves), only the
    # funds-day case stays listed.
    detection = _runbook_sql("### A card receipt seen before its capture")
    funds_day = expected != CAPTURE_DAY
    listed = {row[0] for row in await _all(harness, detection)}
    assert pay in listed
    capture = _payment(
        "PAYMENT_CONFIRMED", pay, checkout=checkout_id, when="2026-10-11 10:00:00"
    )
    capture["payment"]["billingType"] = "CREDIT_CARD"
    assert (await _deliver(harness, capture, "test")).outcome == "processed"
    rows = {row[0]: row for row in await _all(harness, detection)}
    assert (pay in rows) is funds_day
    if funds_day:
        assert rows[pay][2:] == (expected, CAPTURE_DAY)


async def test_p_s_stale_capture_after_a_refund_first_row_on_postgres(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = "sub_pg_p_s"
    builder_id, checkout_id, cents = await _linked(harness, monkeypatch, "p_s", sub)
    pay = "pay_pg_p_s"
    refunded_payload = _card(
        "PAYMENT_REFUNDED",
        pay,
        subscription=sub,
        checkout=checkout_id,
        cents=cents,
        when="2026-10-11 15:00:00",
    )
    refunded = await _deliver(harness, refunded_payload, "test")
    late = await _deliver(
        harness,
        _card(
            "PAYMENT_CONFIRMED",
            pay,
            subscription=sub,
            checkout=checkout_id,
            cents=cents,
            when="2026-10-11 10:00:00",
        ),
        "test",
    )
    assert (refunded.outcome, late.outcome) == (
        "readback_required",
        "readback_required",
    )
    assert await _counts(harness, builder_id) == (0, 0, 0, 0)
    assert await _subscription_status(harness, builder_id) == "active_linked"
    reads = _Reads(
        payments={
            pay: _card_read(
                pay,
                subscription=sub,
                checkout=checkout_id,
                cents=cents,
                status="REFUNDED",
                refunded=cents,
            )
        }
    )
    for delivery_id in (refunded.delivery_id, late.delivery_id):
        assert await _readback(harness, delivery_id, reads) == "converged"
    (row,) = await _all(
        harness,
        "SELECT status, settled_cents FROM billing_payment_projections "
        "WHERE payment_id = :p",
        p=pay,
    )
    assert tuple(row) == ("refunded_before_settlement", 0)
    assert await _counts(harness, builder_id) == (0, 0, 0, 0)
    assert await _subscription_status(harness, builder_id) == "active_linked"


async def test_p_t_settle_evidence_after_a_reversal_reads_back_on_postgres(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "p_t")
    pay = "pay_pg_p_t"

    def event(name: str, when: str, **dates: Any) -> dict[str, Any]:
        payload = _payment(name, pay, checkout=checkout_id, when=when)
        payload["payment"].update(billingType="CREDIT_CARD", **dates)
        return payload

    capture = {"confirmedDate": "2026-10-11", "clientPaymentDate": "2026-10-11"}
    await _deliver(harness, event("PAYMENT_CONFIRMED", "2026-10-11 10:00:00"), "test")
    await _deliver(harness, event("PAYMENT_REFUNDED", "2026-10-11 15:00:00"), "test")
    outcomes = [
        (await _deliver(harness, event(name, when, **dates), "test")).outcome
        for name, when, dates in (
            ("PAYMENT_CONFIRMED", "2026-10-11 10:00:00", capture),
            ("PAYMENT_CONFIRMED", "2026-10-11 16:00:00", capture),
            (
                "PAYMENT_RECEIVED",
                "2026-11-12 08:00:00",
                {**capture, "paymentDate": "2026-11-12"},
            ),
        )
    ]
    assert outcomes == ["processed", "readback_required", "readback_required"]
    reads = _Reads(payments={pay: _read(pay, "REFUNDED", 10_000, refunded=10_000)})
    pending = await _all(
        harness,
        "SELECT id FROM billing_webhook_deliveries WHERE resource_id = :p "
        "AND status = 'readback_required' ORDER BY received_at",
        p=pay,
    )
    assert len(pending) == 2
    for (delivery_id,) in pending:
        assert await _readback(harness, delivery_id, reads) == "converged"
    effects = await _all(
        harness,
        "SELECT effect_kind, source_revision FROM billing_payment_effects "
        "WHERE payment_id = :p ORDER BY effect_kind",
        p=pay,
    )
    assert [tuple(e) for e in effects] == [
        ("reversal", "reversed_cents:10000"),
        ("settlement", "settlement:v1"),
    ]
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_payment_projections WHERE payment_id = :p",
            p=pay,
        )
        == "reversed"
    )
    del builder_id
