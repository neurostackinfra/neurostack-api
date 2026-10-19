"""Real PostgreSQL proofs of the Group 3 webhook/readback *service* path.

Checkout sessions are created through ``BillingCheckoutService`` (Group 2,
in-process ``httpx.MockTransport``), and every Group 3 row is written by
``BillingWebhookService``/``BillingReadbackService`` with the real repository
SQL, triggers, CHECKs and unique indexes of migration 026.  Concurrency
claims use genuinely separate connections and prove the second backend is
actually waiting on a lock (``pg_stat_activity.wait_event_type = 'Lock'``)
before the first commits.  No network: provider reads are in-memory fakes.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.coupon import Coupon
from src.repositories.billing import BillingRepository, billing_advisory_key
from src.services.asaas import (
    PaymentReadback,
    ProviderRecord,
    SubscriptionReadback,
    encode_json,
)
from src.services.billing_catalog import BILLING_CATALOG
from src.services.billing_checkout import (
    CHECKOUT_CONVERGENCE_LIMIT,
    CONVERGENCE_READ_SPACING,
    BillingCheckoutService,
    subscription_pricing_snapshot,
)
from src.services.billing_webhook import (
    BillingReadbackService,
    BillingWebhookService,
    WebhookProcessingUnavailable,
    _FinancialState,
    event_key,
    parse_envelope,
)
from tests.integration.test_asaas_billing_postgres import (
    _create_child,
    _drop_child,
    _evidence,
    _run_alembic,
    _validated_admin_url,
)
from tests.integration.test_asaas_checkout_service_postgres import (
    _CLOCK,
    _builder,
    _configure,
    _Flag,
    _Harness,
    _open_topup_gate,
    _Provider,
    _service,
    _transition,
)

pytestmark = pytest.mark.integration

_SECONDS = itertools.count(1)


@pytest.fixture(scope="module")
async def harness() -> AsyncIterator[_Harness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    engine = None
    created = False
    try:
        await _create_child(admin_engine, name)
        created = True
        url = admin_url.set(database=name)
        await _run_alembic(url, "upgrade", "head")
        engine = create_async_engine(url, poolclass=NullPool)
        yield _Harness(
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
        )
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            await _drop_child(admin_engine, name)
        await admin_engine.dispose()


_READY: set[tuple[int, str]] = set()


async def _general(harness: _Harness, environment: str) -> None:
    """Per-database ``disabled -> canary -> general`` with the top-up gate open."""

    key = (id(harness), environment)
    if key in _READY:
        return
    placeholder = await _builder(harness, f"{environment} placeholder canary")
    await _transition(harness, environment, "canary_allowlist", placeholder)
    await _transition(harness, environment, "general")
    await _open_topup_gate(harness, environment)
    _READY.add(key)


def _documented_id() -> str:
    """The provider's documented event id shape: ``evt_<32 hex>&<digits>``."""

    return f"evt_{uuid4().hex}&{uuid4().int % 10**9}"


def _when() -> str:
    instant = datetime(2026, 10, 11, 10, 0) + timedelta(seconds=next(_SECONDS))
    return instant.strftime("%Y-%m-%d %H:%M:%S")


def _payload(
    event: str,
    resource: dict[str, Any],
    *,
    event_id: str | None = None,
    when: str | None = None,
) -> dict[str, Any]:
    family = event.split("_", 1)[0].lower()
    return {
        "id": event_id or _documented_id(),
        "event": event,
        "dateCreated": when or _when(),
        # Extra/PII fields that must never be persisted.
        "account": {"id": "acc_private", "ownerId": "own_private"},
        family: {
            "customer": "cus_private",
            "customerData": {"name": "Private Person", "email": "p@example.test"},
            **resource,
        },
    }


def _payment(
    event: str,
    payment_id: str,
    *,
    checkout: str | None = None,
    subscription: str | None = None,
    value: str = "100.00",
    when: str | None = None,
    event_id: str | None = None,
    payment_date: str | None = None,
    capture_dates: bool = True,
) -> dict[str, Any]:
    when = when or _when()
    if payment_date is None and event == "PAYMENT_RECEIVED":
        payment_date = "2026-10-11"
    resource = {
        "object": "payment",
        "id": payment_id,
        "subscription": subscription,
        "checkoutSession": checkout,
        "value": Decimal(value),
        "billingType": "CREDIT_CARD" if subscription else "PIX",
        "externalReference": None,
        "creditCard": {"creditCardToken": "tok_private"},
    }
    if payment_date is not None:
        resource["paymentDate"] = payment_date
    if event == "PAYMENT_CONFIRMED" and capture_dates:
        # ASAAS sandbox 2026-10-18: a card capture carries confirmedDate =
        # clientPaymentDate = the capture day and paymentDate = null.
        resource.update(
            confirmedDate=when[:10], clientPaymentDate=when[:10], paymentDate=None
        )
    return _payload(
        event,
        resource,
        when=when,
        event_id=event_id,
    )


def _envelope(payload: dict[str, Any]) -> Any:
    # Exact two-place Decimal money as JSON numbers, then the boundary's parse.
    body = encode_json(payload)
    return parse_envelope(body, json.loads(body, parse_float=Decimal))


async def _deliver(harness: _Harness, payload: dict[str, Any], environment: str) -> Any:
    async with harness.session() as session:
        return await BillingWebhookService(session, environment=environment).receive(
            _envelope(payload)
        )


@dataclass
class _Reads:
    """In-memory exact-ID provider reads."""

    payments: dict[str, PaymentReadback] = field(default_factory=dict)
    subscriptions: dict[str, SubscriptionReadback] = field(default_factory=dict)
    charges: dict[str, tuple[PaymentReadback, ...]] = field(default_factory=dict)
    by_reference: dict[UUID, tuple[ProviderRecord, ...]] = field(default_factory=dict)

    def __call__(self) -> "_Reads":
        return self

    async def get_payment(self, payment_id: str) -> PaymentReadback:
        return self.payments[payment_id]

    async def get_subscription(self, subscription_id: str) -> SubscriptionReadback:
        return self.subscriptions[subscription_id]

    async def list_payments_by_subscription(
        self, sid: str
    ) -> tuple[PaymentReadback, ...]:
        return self.charges.get(sid, ())

    async def list_subscriptions_by_external_reference(
        self, reference: UUID
    ) -> tuple[ProviderRecord, ...]:
        return self.by_reference.get(reference, ())

    async def list_payments_by_external_reference(
        self, _reference: UUID
    ) -> tuple[ProviderRecord, ...]:
        return ()

    async def list_payments_by_checkout(
        self, _checkout: str
    ) -> tuple[ProviderRecord, ...]:
        return ()


def _read(
    payment_id: str,
    status: str,
    cents: int,
    refunded: int = 0,
    deleted: bool = False,
    *,
    omit_payment_date: bool = False,
    include_payment_date: bool = False,
    capture_date: date | None = None,
) -> PaymentReadback:
    capture: dict[str, Any] = {}
    if capture_date is not None:
        # Sandbox-shaped card read: capture dates set (paymentDate as given).
        capture = {"confirmed_date": capture_date, "client_payment_date": capture_date}
    return PaymentReadback(
        payment_id,
        status,
        deleted,
        cents,
        None,
        None,
        None,
        "PIX",
        refunded,
        payment_date=(
            date(2026, 10, 11)
            if not omit_payment_date
            and (
                include_payment_date
                or status in {"RECEIVED", "CHARGEBACK_DISPUTE", "CHARGEBACK_REQUESTED"}
            )
            else None
        ),
        **capture,
    )


async def _scalar(harness: _Harness, sql: str, **params: Any) -> Any:
    async with harness.session() as session:
        return (await session.execute(text(sql), params)).scalar()


async def _all(harness: _Harness, sql: str, **params: Any) -> list[Any]:
    async with harness.session() as session:
        return list((await session.execute(text(sql), params)).all())


async def _external_checkout(harness: _Harness, session_id: UUID) -> str:
    value = await _scalar(
        harness,
        "SELECT external_checkout_id FROM billing_checkout_sessions WHERE id = :id",
        id=session_id,
    )
    assert isinstance(value, str)
    return value


async def _wait_until_blocked(
    harness: _Harness, pid: int, timeout: float = 15.0
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with harness.session() as observer:
        while loop.time() < deadline:
            wait = (
                await observer.execute(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": pid},
                )
            ).scalar()
            await observer.rollback()  # fresh statistics snapshot per poll
            if wait == "Lock":
                return True
            await asyncio.sleep(0.05)
    return False


async def _pid(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _topup(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    environment: str = "test",
) -> tuple[UUID, UUID, str]:
    _configure(monkeypatch, environment, _Flag())
    await _general(harness, environment)
    builder_id = await _builder(harness, label)
    provider = _Provider(harness)
    async with harness.session() as session:
        view = await _service(session, provider.client).create_checkout(
            builder_id=builder_id,
            offer_id="package_credits_500_v1",
            payment_method="pix",
        )
    assert view.state == "active"
    return (
        builder_id,
        view.checkout_session_id,
        await _external_checkout(harness, view.checkout_session_id),
    )


async def _subscription(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    *,
    coupon: str | None = None,
    environment: str = "test",
) -> tuple[UUID, UUID, str, int]:
    _configure(monkeypatch, environment, _Flag())
    await _general(harness, environment)
    builder_id = await _builder(harness, label)
    provider = _Provider(harness)
    async with harness.session() as session:
        view = await _service(session, provider.client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
            coupon_code=coupon,
        )
    amount = await _scalar(
        harness,
        "SELECT amount_cents FROM billing_checkout_sessions WHERE id = :id",
        id=view.checkout_session_id,
    )
    return (
        builder_id,
        view.checkout_session_id,
        await _external_checkout(harness, view.checkout_session_id),
        int(amount),
    )


def _brl(cents: int) -> str:
    return str(Decimal(cents).scaleb(-2))


def _subscription_created(session_id: UUID, sub_id: str, cents: int) -> dict[str, Any]:
    return _payload(
        "SUBSCRIPTION_CREATED",
        {
            "object": "subscription",
            "id": sub_id,
            "value": Decimal(_brl(cents)),
            "cycle": "MONTHLY",
            "nextDueDate": _CLOCK.astimezone(timezone(timedelta(hours=-3)))
            .date()
            .isoformat(),
            "status": "ACTIVE",
            "deleted": False,
            "externalReference": str(session_id),
        },
    )


# ---------------------------------------------------------------------------
# Top-up lifecycle, unknown envelope and distinct idempotency constraints.
# ---------------------------------------------------------------------------


async def test_topup_settles_once_and_reverses_cumulatively(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, checkout_id = await _topup(harness, monkeypatch, "topup T")
    payment = "pay_pg_topup_1"
    assert (
        await _deliver(harness, _payload("CHECKOUT_PAID", {"id": checkout_id}), "test")
    ).outcome == "processed"
    await _deliver(
        harness, _payment("PAYMENT_CREATED", payment, checkout=checkout_id), "test"
    )
    await _deliver(
        harness, _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id), "test"
    )
    await _deliver(
        harness, _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id), "test"
    )
    partial = await _deliver(
        harness,
        _payment("PAYMENT_PARTIALLY_REFUNDED", payment, checkout=checkout_id),
        "test",
    )
    reads = _Reads(
        payments={payment: _read(payment, "RECEIVED", 10_000, refunded=3_000)}
    )
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).process_delivery(partial.delivery_id)
            == "converged"
        )
    await _deliver(
        harness, _payment("PAYMENT_REFUNDED", payment, checkout=checkout_id), "test"
    )

    assert (
        await _scalar(
            harness,
            "SELECT state FROM billing_checkout_sessions WHERE id = :id",
            id=session_id,
        )
        == "completed"
    )
    effects = await _all(
        harness,
        "SELECT effect_kind, source_revision, amount_cents FROM billing_payment_effects "
        "WHERE builder_id = :b ORDER BY created_at, source_revision",
        b=builder_id,
    )
    assert sorted(tuple(e) for e in effects) == [
        ("reversal", "reversed_cents:10000", 7_000),
        ("reversal", "reversed_cents:3000", 3_000),
        ("settlement", "settlement:v1", 10_000),
    ]
    (tx,) = await _all(
        harness,
        "SELECT t.amount_cents, t.plan, t.provider, t.provider_payment_id, "
        "e.effect_kind FROM transactions t JOIN billing_payment_effects e "
        "ON e.id = t.billing_payment_effect_id WHERE t.builder_id = :b",
        b=builder_id,
    )
    assert tuple(tx) == (10_000, "topup", "asaas", payment, "settlement")
    (order,) = await _all(
        harness,
        "SELECT state, settled_cents, reversed_cents FROM billing_topup_orders "
        "WHERE builder_id = :b",
        b=builder_id,
    )
    assert tuple(order) == ("reversed", 10_000, 10_000)


async def test_unknown_event_persists_only_the_minimal_envelope(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(
        "INVOICE_CREATED", {"id": "inv_private", "value": Decimal("9.99")}
    )
    payload["invoice"] = payload.pop("invoice")
    outcome = await _deliver(harness, payload, "test")
    assert outcome.outcome == "unknown_event"
    (row,) = await _all(
        harness,
        "SELECT status, builder_id, resource_kind, resource_id, body_bytes, "
        "length(body_sha256) FROM billing_webhook_deliveries WHERE event_id = :e",
        e=event_key(payload["id"]),
    )
    assert tuple(row)[:4] == ("unknown", None, None, None)
    assert row[5] == 64
    columns = await _all(
        harness,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'billing_webhook_deliveries' ORDER BY column_name",
    )
    assert [c[0] for c in columns] == sorted(
        [
            "id",
            "builder_id",
            "provider",
            "event_id",
            "event_name",
            "provider_created_at",
            "received_at",
            "resource_kind",
            "resource_id",
            "body_bytes",
            "body_sha256",
            "status",
        ]
    )


async def test_each_idempotency_key_is_its_own_postgres_constraint(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(
        harness, monkeypatch, "constraints"
    )
    payment = "pay_pg_constraint"
    confirmed = _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id)
    await _deliver(harness, confirmed, "test")
    (effect,) = await _all(
        harness,
        "SELECT id, projection_id, delivery_id FROM billing_payment_effects "
        "WHERE payment_id = :p",
        p=payment,
    )
    probes = [
        (
            "uq_billing_webhook_deliveries_event",
            "INSERT INTO billing_webhook_deliveries (id, provider, event_id, event_name, "
            "provider_created_at, body_bytes, body_sha256, status) SELECT gen_random_uuid(), "
            "provider, event_id, event_name, provider_created_at, body_bytes, body_sha256, "
            "status FROM billing_webhook_deliveries WHERE event_id = :e",
            {"e": event_key(confirmed["id"])},
        ),
        (
            "uq_billing_payment_projections_payment",
            "INSERT INTO billing_payment_projections (id, builder_id, provider, payment_id, "
            "aggregate_kind, topup_order_id, status, amount_cents, provider_created_at, "
            "ordering_key, last_event_id) SELECT gen_random_uuid(), builder_id, provider, "
            "payment_id, aggregate_kind, topup_order_id, status, amount_cents, "
            "provider_created_at, ordering_key, last_event_id FROM "
            "billing_payment_projections WHERE payment_id = :p",
            {"p": payment},
        ),
        (
            "uq_billing_payment_effects_idempotency",
            "INSERT INTO billing_payment_effects (id, builder_id, projection_id, delivery_id, "
            "provider, payment_id, effect_kind, source_revision, amount_cents) VALUES "
            "(gen_random_uuid(), :b, :proj, :d, 'asaas', :p, 'settlement', "
            "'settlement:v1', 10000)",
            {"b": builder_id, "proj": effect[1], "d": effect[2], "p": payment},
        ),
        (
            "uq_transactions_provider_payment",
            "INSERT INTO transactions (id, builder_id, builder_name, amount_cents, method, "
            "cycle, plan, provider, provider_payment_id, billing_payment_effect_id, "
            "event_type, created_at, updated_at) VALUES (gen_random_uuid(), :b, 'x', 1, "
            "'pix', 'detached', 'topup', 'asaas', :p, :eid, 'PAYMENT_RECEIVED', now(), now())",
            {"b": builder_id, "p": payment, "eid": effect[0]},
        ),
    ]
    for constraint, sql, params in probes:
        async with harness.session() as session:
            async with session.begin():
                with pytest.raises(DBAPIError, match=constraint):
                    await session.execute(text(sql), params)


# ---------------------------------------------------------------------------
# Concurrency on separate connections.
# ---------------------------------------------------------------------------


async def test_concurrent_identical_deliveries_wait_and_apply_once(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "dup race")
    envelope = _envelope(
        _payment("PAYMENT_RECEIVED", "pay_pg_dup", checkout=checkout_id)
    )
    first = harness.session()
    second = harness.session()
    try:
        held = await BillingWebhookService(first, environment="test")._receive(envelope)
        pid = await _pid(second)
        task = asyncio.create_task(
            BillingWebhookService(second, environment="test").receive(envelope)
        )
        assert await _wait_until_blocked(harness, pid), "second delivery did not wait"
        assert not task.done()
        await first.commit()
        late = await asyncio.wait_for(task, timeout=30)
    finally:
        await first.close()
        await second.close()
    assert (held.outcome, late.outcome) == ("processed", "duplicate")
    counts = await _all(
        harness,
        "SELECT (SELECT count(*) FROM billing_webhook_deliveries WHERE event_id = :e), "
        "(SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b), "
        "(SELECT count(*) FROM transactions WHERE builder_id = :b)",
        e=envelope.event_id,
        b=builder_id,
    )
    assert tuple(counts[0]) == (1, 1, 1)


async def test_concurrent_confirmed_and_received_share_one_settlement(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "pair race")
    confirmed = _envelope(
        _payment("PAYMENT_CONFIRMED", "pay_pg_pair", checkout=checkout_id)
    )
    received = _envelope(
        _payment("PAYMENT_RECEIVED", "pay_pg_pair", checkout=checkout_id)
    )
    first = harness.session()
    second = harness.session()
    try:
        held = await BillingWebhookService(first, environment="test")._receive(
            confirmed
        )
        pid = await _pid(second)
        task = asyncio.create_task(
            BillingWebhookService(second, environment="test").receive(received)
        )
        assert await _wait_until_blocked(harness, pid), "second event did not wait"
        await first.commit()
        late = await asyncio.wait_for(task, timeout=30)
    finally:
        await first.close()
        await second.close()
    assert (held.outcome, late.outcome) == ("processed", "processed")
    # The capture settled; the receipt that waited on it is inert.
    rows = await _all(
        harness,
        "SELECT effect_kind, source_revision, delivery_id FROM billing_payment_effects "
        "WHERE builder_id = :b",
        b=builder_id,
    )
    assert [tuple(r) for r in rows] == [
        ("settlement", "settlement:v1", held.delivery_id)
    ]
    assert await _scalar(
        harness,
        "SELECT paid_at FROM billing_payment_projections WHERE payment_id = 'pay_pg_pair'",
    ) == date(2026, 10, 11)
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM transactions WHERE builder_id = :b",
            b=builder_id,
        )
        == 1
    )


async def test_out_of_order_events_never_regress_on_postgres(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "ordering")
    payment = "pay_pg_order"
    await _deliver(
        harness,
        _payment(
            "PAYMENT_RECEIVED",
            payment,
            checkout=checkout_id,
            when="2026-10-11 12:00:09",
        ),
        "test",
    )
    for event, when in (
        ("PAYMENT_CREATED", "2026-10-11 12:00:00"),
        ("PAYMENT_OVERDUE", "2026-10-11 12:00:01"),
        ("PAYMENT_CONFIRMED", "2026-10-11 12:00:05"),
    ):
        await _deliver(
            harness, _payment(event, payment, checkout=checkout_id, when=when), "test"
        )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_payment_projections WHERE payment_id = :p",
            p=payment,
        )
        == "settled"
    )
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b",
            b=builder_id,
        )
        == 1
    )


async def test_capture_first_then_late_receipt_never_regresses_on_postgres(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse order: CONFIRMED settles, then a RECEIVED at D+32 with the
    funds day as ``paymentDate`` is inert and keeps the capture day."""

    builder_id, _session, checkout_id = await _topup(
        harness, monkeypatch, "ordering reverse"
    )
    payment = "pay_pg_order_reverse"
    for event, when, extra in (
        ("PAYMENT_CREATED", "2026-10-11 11:00:00", None),
        ("PAYMENT_CONFIRMED", "2026-10-11 11:00:05", None),
        ("PAYMENT_RECEIVED", "2026-11-12 08:00:00", "2026-11-12"),
    ):
        outcome = await _deliver(
            harness,
            _payment(
                event, payment, checkout=checkout_id, when=when, payment_date=extra
            ),
            "test",
        )
        assert outcome.outcome == "processed", event
    (row,) = await _all(
        harness,
        "SELECT status, paid_at, (SELECT count(*) FROM billing_payment_effects "
        "WHERE builder_id = :b), (SELECT count(*) FROM billing_outbox "
        "WHERE builder_id = :b) FROM billing_payment_projections WHERE payment_id = :p",
        b=builder_id,
        p=payment,
    )
    assert tuple(row) == ("settled", date(2026, 10, 11), 1, 1)


# ---------------------------------------------------------------------------
# Subscription: M-A handoff lock, coupon in the settlement transaction, M-D.
# ---------------------------------------------------------------------------


async def test_handoff_holds_the_rollout_lock_against_eligibility(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, _checkout, cents = await _subscription(
        harness, monkeypatch, "handoff M-A"
    )
    first = harness.session()
    second = harness.session()

    async def eligibility() -> str | None:
        repository = BillingRepository(second)
        await repository.lock_rollout_control(environment="test")
        blocker = await repository.subscription_checkout_blocker(builder_id=builder_id)
        await second.rollback()
        return blocker

    try:
        held = await BillingWebhookService(first, environment="test")._receive(
            _envelope(_subscription_created(session_id, "sub_pg_handoff", cents))
        )
        assert held.outcome == "processed"
        pid = await _pid(second)
        task = asyncio.create_task(eligibility())
        assert await _wait_until_blocked(harness, pid), "eligibility did not wait"
        assert not task.done()
        await first.commit()
        blocker = await asyncio.wait_for(task, timeout=30)
    finally:
        await first.close()
        await second.close()
    # The decision is taken only after the handoff committed: the new
    # recurring row blocks, never a gap between session and subscription.
    assert blocker == "asaas_subscription"
    (row,) = await _all(
        harness,
        "SELECT s.id = c.intent_id, s.status, s.is_active, s.roster_revision, c.state "
        "FROM billing_subscriptions s JOIN billing_checkout_sessions c "
        "ON c.resulting_external_subscription_id = s.external_subscription_id "
        "WHERE s.builder_id = :b",
        b=builder_id,
    )
    assert tuple(row) == (True, "active_linked", True, 1, "completed")


async def test_coupon_is_consumed_once_inside_the_settlement_transaction(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with harness.session() as session:
        session.add(Coupon(code="PGWELCOME", discount_percent=10, uses_count=0))
        await session.commit()
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "coupon", coupon="PGWELCOME"
    )
    await _deliver(
        harness, _subscription_created(session_id, "sub_pg_coupon", cents), "test"
    )
    confirmed = _payment(
        "PAYMENT_RECEIVED",
        "pay_pg_coupon_1",
        checkout=checkout_id,
        subscription="sub_pg_coupon",
        value=_brl(cents),
    )

    async def failing_hook(
        self: Any, effect: Any, projection: Any, **_fact_context: Any
    ) -> None:
        # Group 4 passes the fact context (``cycle_start``) to this hook.
        raise RuntimeError("simulated failure at the Group 4 outbox insertion point")

    original = _FinancialState._financial_fact_hook
    monkeypatch.setattr(_FinancialState, "_financial_fact_hook", failing_hook)
    with pytest.raises(WebhookProcessingUnavailable):
        await _deliver(harness, confirmed, "test")
    monkeypatch.setattr(_FinancialState, "_financial_fact_hook", original)
    # Atomic: no delivery, effect, Transaction or coupon use survived.
    snapshot = await _all(
        harness,
        "SELECT (SELECT count(*) FROM billing_webhook_deliveries WHERE event_id = :e), "
        "(SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b), "
        "(SELECT count(*) FROM transactions WHERE builder_id = :b), "
        "(SELECT uses_count FROM coupons WHERE code = 'PGWELCOME')",
        e=event_key(confirmed["id"]),
        b=builder_id,
    )
    assert tuple(snapshot[0]) == (0, 0, 0, 0)

    await _deliver(harness, confirmed, "test")  # provider retry
    await _deliver(
        harness,
        _payment(
            "PAYMENT_RECEIVED",
            "pay_pg_coupon_1",
            checkout=checkout_id,
            subscription="sub_pg_coupon",
            value=_brl(cents),
        ),
        "test",
    )
    await _deliver(
        harness,
        _payment(
            "PAYMENT_RECEIVED",
            "pay_pg_coupon_2",
            subscription="sub_pg_coupon",
            value=_brl(cents),
        ),
        "test",
    )
    rows = await _all(
        harness,
        "SELECT provider_payment_id, coupon_code, plan, amount_cents FROM transactions "
        "WHERE builder_id = :b ORDER BY provider_payment_id",
        b=builder_id,
    )
    assert [tuple(r) for r in rows] == [
        ("pay_pg_coupon_1", "PGWELCOME", "plus", cents),
        ("pay_pg_coupon_2", None, "plus", cents),
    ]
    assert (
        await _scalar(
            harness, "SELECT uses_count FROM coupons WHERE code = 'PGWELCOME'"
        )
        == 1
    )
    builder = await _all(
        harness,
        "SELECT subscription_plan, subscription_status, billing_cycle FROM builders "
        "WHERE id = :b",
        b=builder_id,
    )
    assert tuple(builder[0]) == ("plus", "active", "monthly")


async def test_terminal_subscription_frees_only_after_converged_read(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, _checkout, cents = await _subscription(
        harness, monkeypatch, "terminal M-D"
    )
    await _deliver(
        harness, _subscription_created(session_id, "sub_pg_end", cents), "test"
    )
    ended = await _deliver(
        harness,
        _payload("SUBSCRIPTION_DELETED", {"id": "sub_pg_end", "deleted": True}),
        "test",
    )
    subscription_id = await _scalar(
        harness,
        "SELECT id FROM billing_subscriptions WHERE builder_id = :b",
        b=builder_id,
    )

    async def blocker() -> str | None:
        async with harness.session() as session:
            return await BillingRepository(session).subscription_checkout_blocker(
                builder_id=builder_id
            )

    assert ended.outcome == "readback_required"
    assert await blocker() == "asaas_subscription"
    remote = SubscriptionReadback(
        "sub_pg_end", "INACTIVE", True, "MONTHLY", cents, date(2026, 11, 11), None
    )
    reads = _Reads(
        subscriptions={"sub_pg_end": remote},
        charges={"sub_pg_end": (_read("pay_open", "PENDING", cents),)},
    )
    async with harness.session() as session:
        outcome = await BillingReadbackService(
            session, client_factory=reads, environment="test"
        ).readback_subscription(subscription_id)
    assert outcome == "pending" and await blocker() == "asaas_subscription"
    reads.charges["sub_pg_end"] = ()
    async with harness.session() as session:
        outcome = await BillingReadbackService(
            session, client_factory=reads, environment="test"
        ).readback_subscription(subscription_id)
    assert outcome == "converged"
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_subscriptions WHERE id = :i",
            i=subscription_id,
        )
        == "cancelled"
    )
    assert await blocker() is None


# ---------------------------------------------------------------------------
# Canary: settlement binds its lifetime slot; DB-refused writes quarantine.
# ---------------------------------------------------------------------------


async def test_canary_subscription_settles_into_its_slot_and_quarantines_refusals(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = "development"
    _configure(monkeypatch, environment, _Flag())
    builder_id = await _builder(harness, "canary C")
    await _transition(harness, environment, "canary_allowlist", builder_id)
    provider = _Provider(harness)
    async with harness.session() as session:
        view = await _service(session, provider.client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )
    session_id = view.checkout_session_id
    checkout_id = await _external_checkout(harness, session_id)
    await _deliver(
        harness, _subscription_created(session_id, "sub_pg_canary", 99_900), environment
    )
    created = await _deliver(
        harness,
        _payment(
            "PAYMENT_CREATED",
            "pay_pg_canary",
            checkout=checkout_id,
            subscription="sub_pg_canary",
            value="999.00",
        ),
        environment,
    )
    assert created.outcome == "processed"
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM billing_payment_projections " "WHERE builder_id = :b",
            b=builder_id,
        )
        == 0
    )
    for event in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"):
        outcome = await _deliver(
            harness,
            _payment(
                event,
                "pay_pg_canary",
                checkout=checkout_id,
                subscription="sub_pg_canary",
                value="999.00",
            ),
            environment,
        )
        assert outcome.outcome == "processed", event
        # The capture consumes the lifetime slot; the receipt changes nothing.
        assert (
            await _scalar(
                harness,
                "SELECT canary_settlement_count FROM billing_rollout_control "
                "WHERE environment = :env",
                env=environment,
            )
            == 1
        ), event
    (control,) = await _all(
        harness,
        "SELECT c.canary_settlement_count, c.canary_subscription_effect_id = e.id, "
        "e.canary_slot, p.canary_slot, s.status FROM billing_rollout_control c "
        "JOIN billing_payment_effects e ON e.builder_id = :b "
        "JOIN billing_payment_projections p ON p.id = e.projection_id "
        "JOIN billing_subscriptions s ON s.id = p.subscription_id "
        "WHERE c.environment = :env",
        b=builder_id,
        env=environment,
    )
    assert tuple(control) == (1, True, "subscription", "subscription", "active")

    # A renewal of the canary subscription is a deterministic refusal (the DB
    # admits one canary settlement per slot): quarantined with 200, never a
    # 503 retry loop that would interrupt the provider's sequential queue.
    renewal = await _deliver(
        harness,
        _payment(
            "PAYMENT_RECEIVED",
            "pay_pg_canary_renewal",
            subscription="sub_pg_canary",
            value="999.00",
        ),
        environment,
    )
    assert renewal.outcome == "quarantined"
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_webhook_deliveries WHERE id = :d",
            d=renewal.delivery_id,
        )
        == "readback_required"
    )
    assert (
        await _scalar(
            harness,
            "SELECT canary_settlement_count FROM billing_rollout_control "
            "WHERE environment = :env",
            env=environment,
        )
        == 1
    )

    # Cancel, converge, then a refund event: the Group 1 trigger pins every
    # canary projection write to an ``active`` Plus row, so the service must
    # quarantine (200, minimal envelope, alert) instead of looping the
    # provider into retries.
    await _deliver(
        harness, _payload("SUBSCRIPTION_DELETED", {"id": "sub_pg_canary"}), environment
    )
    subscription_id = await _scalar(
        harness,
        "SELECT id FROM billing_subscriptions WHERE builder_id = :b",
        b=builder_id,
    )
    reads = _Reads(
        subscriptions={
            "sub_pg_canary": SubscriptionReadback(
                "sub_pg_canary",
                "INACTIVE",
                True,
                "MONTHLY",
                99_900,
                date(2026, 11, 11),
                None,
            )
        }
    )
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment=environment
            ).readback_subscription(subscription_id)
            == "converged"
        )
    refunded = await _deliver(
        harness,
        _payment(
            "PAYMENT_REFUNDED",
            "pay_pg_canary",
            checkout=checkout_id,
            subscription="sub_pg_canary",
            value="999.00",
        ),
        environment,
    )
    assert refunded.outcome == "quarantined"
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_webhook_deliveries WHERE id = :d",
            d=refunded.delivery_id,
        )
        == "readback_required"
    )
    async with harness.session() as session:
        async with session.begin():
            with pytest.raises(
                DBAPIError, match="canonical Plus subscription is required"
            ):
                await session.execute(
                    text(
                        "UPDATE billing_payment_projections SET status = 'reversed', "
                        "reversed_cents = 99900 WHERE payment_id = 'pay_pg_canary'"
                    )
                )


# ---------------------------------------------------------------------------
# M-C: ambiguous attempts converge; lockout is never permanent.
# ---------------------------------------------------------------------------


async def test_ambiguous_attempts_converge_to_abandoned_after_two_empty_reads(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, "test", _Flag())
    await _general(harness, "test")
    builder_id = await _builder(harness, "M-C")
    snapshot = subscription_pricing_snapshot(
        BILLING_CATALOG["plan_pro_monthly_v1"], active_clinics=0
    )
    session_id = uuid4()
    async with harness.session() as session:
        await BillingRepository(session).lock_rollout_control(environment="test")
        await BillingRepository(session).reserve_general_subscription_checkout(
            environment="test",
            builder_id=builder_id,
            intent_id=uuid4(),
            checkout_session_id=session_id,
            attempt_no=1,
            catalog_version_id="payment-2026-10-03",
            offer_id="plan_pro_monthly_v1",
            amount_cents=int(snapshot["amount_cents"]),  # type: ignore[call-overload]
            pricing_snapshot=snapshot,
            next_due_date=date(2026, 10, 11),
        )
        await session.commit()  # then the process "crashes": orphaned creating
    created_at = await _scalar(
        harness,
        "SELECT created_at FROM billing_checkout_sessions WHERE id = :i",
        i=session_id,
    )
    reads = _Reads()

    async def reconcile(now: datetime) -> str:
        async with harness.session() as session:
            view = await BillingCheckoutService(
                session, client_factory=reads, clock=lambda: now
            ).reconcile_checkout(builder_id=builder_id, checkout_session_id=session_id)
        return view.state

    async def blocker() -> str | None:
        async with harness.session() as session:
            return await BillingRepository(session).subscription_checkout_blocker(
                builder_id=builder_id
            )

    assert await blocker() == "live_subscription_checkout"
    assert (
        await reconcile(created_at + timedelta(minutes=6)) == "reconciliation_required"
    )
    first = created_at + CHECKOUT_CONVERGENCE_LIMIT + timedelta(minutes=1)
    assert await reconcile(first) == "reconciliation_required"
    assert await reconcile(first + timedelta(minutes=1)) == "reconciliation_required"
    second = first + CONVERGENCE_READ_SPACING + timedelta(seconds=1)
    # Dormant by default (M-C): the read runs, nothing is abandoned.
    assert await reconcile(second) == "reconciliation_required"
    assert await blocker() == "live_subscription_checkout"
    monkeypatch.setattr(settings, "asaas_auto_abandon_enabled", True)
    assert await reconcile(
        second + CONVERGENCE_READ_SPACING + timedelta(seconds=1)
    ) == ("abandoned")
    assert await blocker() is None
    assert (
        await _scalar(
            harness,
            "SELECT reconciliation_attempts FROM billing_checkout_sessions WHERE id = :i",
            i=session_id,
        )
        == 4
    )


# ---------------------------------------------------------------------------
# Systematic pass: every value Group 3 writes, through the service, on PG.
# ---------------------------------------------------------------------------


async def test_every_status_group3_writes_is_accepted_by_postgres(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout = await _topup(harness, monkeypatch, "status sweep")

    async def send(event: str, payment: str) -> Any:
        return await _deliver(
            harness, _payment(event, payment, checkout=checkout), "test"
        )

    async def readback(outcome: Any, payment: str, read: PaymentReadback) -> None:
        reads = _Reads(payments={payment: read})
        async with harness.session() as session:
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).process_delivery(outcome.delivery_id)

    await send("PAYMENT_CREATED", "pay_s_pending")
    await send("PAYMENT_REPROVED_BY_RISK_ANALYSIS", "pay_s_failed")
    await send("PAYMENT_CREATED", "pay_s_overdue")
    await send("PAYMENT_OVERDUE", "pay_s_overdue")
    for payment, follow in (
        ("pay_s_rp", "PAYMENT_REFUND_IN_PROGRESS"),
        ("pay_s_cbr", "PAYMENT_CHARGEBACK_REQUESTED"),
        ("pay_s_cbd", "PAYMENT_CHARGEBACK_DISPUTE"),
        ("pay_s_rsp", "PAYMENT_AWAITING_CHARGEBACK_REVERSAL"),
        ("pay_s_full", "PAYMENT_REFUNDED"),
    ):
        await send("PAYMENT_RECEIVED", payment)
        await send(follow, payment)
    await send("PAYMENT_RECEIVED", "pay_s_partial")
    partial = await send("PAYMENT_PARTIALLY_REFUNDED", "pay_s_partial")
    await readback(
        partial, "pay_s_partial", _read("pay_s_partial", "RECEIVED", 10_000, 2_500)
    )
    await send("PAYMENT_CREATED", "pay_s_deleted")
    await send("PAYMENT_DELETED", "pay_s_deleted")
    await send("PAYMENT_CREATED", "pay_s_restored")
    await send("PAYMENT_DELETED", "pay_s_restored")
    restored = await send("PAYMENT_RESTORED", "pay_s_restored")
    await readback(
        restored, "pay_s_restored", _read("pay_s_restored", "RECEIVED", 10_000)
    )
    await send("PAYMENT_REFUNDED", "pay_s_readback")  # reversal before settlement

    statuses = {
        r[0]
        for r in await _all(
            harness, "SELECT DISTINCT status FROM billing_payment_projections"
        )
    }
    assert statuses >= {
        "pending",
        "failed",
        "overdue",
        "settled",
        "reversal_pending",
        "partially_reversed",
        "reversed",
        "chargeback_requested",
        "chargeback_dispute",
        "restoration_pending",
        "deleted",
        "readback_required",
        "restored",
    }
    kinds = {
        tuple(r)
        for r in await _all(
            harness,
            "SELECT DISTINCT effect_kind, split_part(source_revision, ':', 1) "
            "FROM billing_payment_effects",
        )
    }
    assert kinds >= {
        ("settlement", "settlement"),
        ("reversal", "reversed_cents"),
        ("restoration", "restoration_epoch"),
    }
    deliveries = {
        r[0]
        for r in await _all(
            harness, "SELECT DISTINCT status FROM billing_webhook_deliveries"
        )
    }
    assert deliveries >= {"processed", "unknown", "readback_required"}
    subscriptions = {
        r[0]
        for r in await _all(
            harness, "SELECT DISTINCT status FROM billing_subscriptions"
        )
    }
    assert subscriptions >= {"active_linked", "active", "cancelled"}
    sessions = {
        r[0]
        for r in await _all(
            harness, "SELECT DISTINCT state FROM billing_checkout_sessions"
        )
    }
    # reconciliation_required is asserted where M-C writes it (above).
    assert sessions >= {"completed", "abandoned"}
    orders = {
        r[0]
        for r in await _all(
            harness,
            "SELECT DISTINCT state FROM billing_topup_orders WHERE builder_id = :b",
            b=builder_id,
        )
    }
    assert orders & {"settled", "partially_reversed", "reversed", "restored"}


async def test_session_and_subscription_values_through_events_and_readbacks(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # reconciliation_required -> active (CHECKOUT_CREATED), paid_pending_subscription,
    # terminal_pending_readback -> cancelled / expired, readback_required and
    # non_billable_pending_readback -> inactive: all written by the services.
    builder_a, session_a, checkout_a, cents = await _subscription(
        harness, monkeypatch, "S-A"
    )
    await _deliver(harness, _payload("CHECKOUT_PAID", {"id": checkout_a}), "test")
    assert (
        await _scalar(
            harness,
            "SELECT state FROM billing_checkout_sessions WHERE id = :i",
            i=session_a,
        )
        == "paid_pending_subscription"
    )
    await _deliver(
        harness, _subscription_created(session_a, "sub_pg_values", cents), "test"
    )
    updated = await _deliver(
        harness, _payload("SUBSCRIPTION_UPDATED", {"id": "sub_pg_values"}), "test"
    )
    assert updated.outcome == "readback_required"
    subscription_id = await _scalar(
        harness,
        "SELECT id FROM billing_subscriptions WHERE builder_id = :b",
        b=builder_a,
    )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_subscriptions WHERE id = :i",
            i=subscription_id,
        )
        == "readback_required"
    )
    await _deliver(
        harness, _payload("SUBSCRIPTION_INACTIVATED", {"id": "sub_pg_values"}), "test"
    )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_subscriptions WHERE id = :i",
            i=subscription_id,
        )
        == "non_billable_pending_readback"
    )
    reads = _Reads(
        subscriptions={
            "sub_pg_values": SubscriptionReadback(
                "sub_pg_values",
                "INACTIVE",
                False,
                "MONTHLY",
                cents,
                date(2026, 11, 11),
                None,
            )
        }
    )
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).readback_subscription(subscription_id)
            == "converged"
        )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_subscriptions WHERE id = :i",
            i=subscription_id,
        )
        == "inactive"
    )

    for event, expected in (
        ("CHECKOUT_CANCELED", "cancelled"),
        ("CHECKOUT_EXPIRED", "expired"),
    ):
        _b, session_id, checkout_id = await _topup(
            harness, monkeypatch, f"T-{expected}"
        )
        outcome = await _deliver(harness, _payload(event, {"id": checkout_id}), "test")
        assert (
            await _scalar(
                harness,
                "SELECT state FROM billing_checkout_sessions " "WHERE id = :i",
                i=session_id,
            )
            == "terminal_pending_readback"
        )
        async with harness.session() as session:
            assert (
                await BillingReadbackService(
                    session, client_factory=_Reads(), environment="test"
                ).process_delivery(outcome.delivery_id)
                == "converged"
            )
        assert (
            await _scalar(
                harness,
                "SELECT state FROM billing_checkout_sessions " "WHERE id = :i",
                i=session_id,
            )
            == expected
        )

    # reconciliation_required -> active through CHECKOUT_CREATED.
    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _configure(monkeypatch, "test", _Flag())
    builder_r = await _builder(harness, "R")
    from src.services.asaas import AsaasClient
    from tests.integration.test_asaas_checkout_service_postgres import _API_KEY

    def timing_out() -> AsaasClient:
        from pydantic import SecretStr

        return AsaasClient(
            api_key=SecretStr(_API_KEY),
            user_agent="NeuroStack/test",
            transport=httpx.MockTransport(timeout_handler),
        )

    async with harness.session() as session:
        with pytest.raises(Exception):
            await _service(session, timing_out).create_checkout(
                builder_id=builder_r,
                offer_id="package_credits_500_v1",
                payment_method="pix",
            )
    (session_r,) = await _all(
        harness,
        "SELECT id, state FROM billing_checkout_sessions " "WHERE builder_id = :b",
        b=builder_r,
    )
    assert session_r[1] == "reconciliation_required"
    await _deliver(
        harness,
        _payload(
            "CHECKOUT_CREATED",
            {"id": "chk_late_link_0001", "externalReference": str(session_r[0])},
        ),
        "test",
    )
    assert tuple(
        (
            await _all(
                harness,
                "SELECT state, external_checkout_id FROM "
                "billing_checkout_sessions WHERE id = :i",
                i=session_r[0],
            )
        )[0]
    ) == ("active", "chk_late_link_0001")


# ---------------------------------------------------------------------------
# Fix loop 1: documented ids, M-4 lock-free reads, backlog sweep, extra
# service-path rows.  Every row below is written by the services on PG.
# ---------------------------------------------------------------------------


async def test_documented_event_ids_dedupe_and_distinguish_on_postgres(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "C-1 ids")
    documented = "evt_05b708f961d739ea7eba7e4db318f621&368604920"
    sibling = "evt_05b708f961d739ea7eba7e4db318f621&368604921"
    paid = await _deliver(
        harness,
        _payload("CHECKOUT_PAID", {"id": checkout_id}, event_id=documented),
        "test",
    )
    again = await _deliver(
        harness,
        _payload("CHECKOUT_PAID", {"id": checkout_id}, event_id=documented),
        "test",
    )
    created = await _deliver(
        harness,
        _payment(
            "PAYMENT_CREATED", "pay_pg_doc", checkout=checkout_id, event_id=sibling
        ),
        "test",
    )
    assert (paid.outcome, again.outcome, created.outcome) == (
        "processed",
        "duplicate",
        "processed",
    )
    rows = await _all(
        harness,
        "SELECT event_id FROM billing_webhook_deliveries WHERE builder_id = :b "
        "ORDER BY event_id",
        b=builder_id,
    )
    assert sorted(r[0] for r in rows) == sorted(
        [event_key(documented), event_key(sibling)]
    )
    projection = await _all(
        harness,
        "SELECT last_event_id, ordering_key FROM billing_payment_projections "
        "WHERE payment_id = 'pay_pg_doc'",
    )
    assert projection[0][0] == event_key(sibling)
    assert projection[0][1].endswith(event_key(sibling))


def _timing_out_client() -> Any:
    from pydantic import SecretStr

    from src.services.asaas import AsaasClient
    from tests.integration.test_asaas_checkout_service_postgres import _API_KEY

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    return AsaasClient(
        api_key=SecretStr(_API_KEY),
        user_agent="NeuroStack/test",
        transport=httpx.MockTransport(handler),
    )


async def _ambiguous_session(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, label: str
) -> tuple[UUID, UUID, datetime]:
    _configure(monkeypatch, "test", _Flag())
    await _general(harness, "test")
    builder_id = await _builder(harness, label)
    async with harness.session() as session:
        with pytest.raises(Exception):
            await _service(session, _timing_out_client).create_checkout(
                builder_id=builder_id,
                offer_id="package_credits_500_v1",
                payment_method="pix",
            )
    (row,) = await _all(
        harness,
        "SELECT id, state, created_at FROM billing_checkout_sessions WHERE builder_id = :b",
        b=builder_id,
    )
    assert row[1] == "reconciliation_required"
    return builder_id, row[0], row[2]


async def test_slow_provider_read_never_blocks_a_webhook_for_the_same_session(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, created_at = await _ambiguous_session(
        harness, monkeypatch, "M-4"
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowReads(_Reads):
        async def list_subscriptions_by_external_reference(
            self, reference: UUID
        ) -> tuple[ProviderRecord, ...]:
            entered.set()
            await release.wait()
            return ()

    reconciler = harness.session()
    task = asyncio.create_task(
        BillingCheckoutService(
            reconciler,
            client_factory=SlowReads(),
            clock=lambda: created_at + timedelta(minutes=10),
        ).reconcile_checkout(builder_id=builder_id, checkout_session_id=session_id)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        outcome = await asyncio.wait_for(
            _deliver(
                harness,
                _payload(
                    "CHECKOUT_CREATED",
                    {"id": "chk_pg_meanwhile", "externalReference": str(session_id)},
                ),
                "test",
            ),
            timeout=10,
        )
        assert outcome.outcome == "processed"
        release.set()
        view = await asyncio.wait_for(task, timeout=10)
    finally:
        release.set()
        await reconciler.close()
    assert view.state == "active"
    (row,) = await _all(
        harness,
        "SELECT state, reconciliation_attempts, external_checkout_id "
        "FROM billing_checkout_sessions WHERE id = :i",
        i=session_id,
    )
    assert tuple(row) == ("active", 1, "chk_pg_meanwhile")


async def test_backlog_sweep_converges_real_rows(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _b1, cancelled_session, cancelled_checkout = await _topup(
        harness, monkeypatch, "sweep cancel"
    )
    await _deliver(
        harness, _payload("CHECKOUT_CANCELED", {"id": cancelled_checkout}), "test"
    )
    _b2, _s2, partial_checkout = await _topup(harness, monkeypatch, "sweep partial")
    payment = "pay_pg_sweep"
    await _deliver(
        harness,
        _payment("PAYMENT_CONFIRMED", payment, checkout=partial_checkout),
        "test",
    )
    partial = await _deliver(
        harness,
        _payment("PAYMENT_PARTIALLY_REFUNDED", payment, checkout=partial_checkout),
        "test",
    )
    reads = _Reads(
        payments={payment: _read(payment, "RECEIVED", 10_000, refunded=2_500)}
    )
    async with harness.session() as session:
        await BillingReadbackService(
            session, client_factory=reads, environment="test"
        ).reconcile_backlog(limit=500)
    assert (
        await _scalar(
            harness,
            "SELECT state FROM billing_checkout_sessions WHERE id = :i",
            i=cancelled_session,
        )
        == "cancelled"
    )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_webhook_deliveries WHERE id = :d",
            d=partial.delivery_id,
        )
        == "processed"
    )
    assert (
        await _scalar(
            harness,
            "SELECT amount_cents FROM billing_payment_effects WHERE payment_id = :p "
            "AND source_revision = 'reversed_cents:2500'",
            p=payment,
        )
        == 2_500
    )


async def test_paid_checkout_hands_off_through_readback_with_auto_abandon_off(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "readback handoff"
    )
    await _deliver(harness, _payload("CHECKOUT_PAID", {"id": checkout_id}), "test")
    reads = _Reads(
        by_reference={
            session_id: (
                ProviderRecord("sub_pg_readback", str(session_id), "ACTIVE", False),
            )
        }
    )
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).readback_checkout(session_id)
            == "converged"
        )
    (row,) = await _all(
        harness,
        "SELECT c.state, s.status, s.id = c.intent_id FROM billing_checkout_sessions c "
        "JOIN billing_subscriptions s ON s.external_subscription_id = "
        "c.resulting_external_subscription_id WHERE c.id = :i",
        i=session_id,
    )
    assert tuple(row) == ("completed", "active_linked", True)


async def test_subscription_readback_converges_back_to_active(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "back to active"
    )
    await _deliver(
        harness, _subscription_created(session_id, "sub_pg_back", cents), "test"
    )
    await _deliver(
        harness,
        _payment(
            "PAYMENT_CONFIRMED",
            "pay_pg_back",
            checkout=checkout_id,
            subscription="sub_pg_back",
            value=_brl(cents),
        ),
        "test",
    )
    await _deliver(
        harness, _payload("SUBSCRIPTION_UPDATED", {"id": "sub_pg_back"}), "test"
    )
    subscription_id = await _scalar(
        harness,
        "SELECT id FROM billing_subscriptions WHERE builder_id = :b",
        b=builder_id,
    )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_subscriptions WHERE id = :i",
            i=subscription_id,
        )
        == "readback_required"
    )
    reads = _Reads(
        subscriptions={
            "sub_pg_back": SubscriptionReadback(
                "sub_pg_back",
                "ACTIVE",
                False,
                "MONTHLY",
                cents,
                date(2026, 11, 11),
                None,
            )
        }
    )
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).readback_subscription(subscription_id)
            == "converged"
        )
    # The card capture settled the first charge, so the converged row is
    # ``active`` (before 2026-10-18 a CONFIRMED left it ``active_linked``).
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_subscriptions WHERE id = :i",
            i=subscription_id,
        )
        == "active"
    )


async def test_refund_denied_restores_a_non_zero_amount_by_readback(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "restore")
    payment = "pay_pg_restore"
    await _deliver(
        harness, _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id), "test"
    )
    await _deliver(
        harness, _payment("PAYMENT_REFUNDED", payment, checkout=checkout_id), "test"
    )
    denied = await _deliver(
        harness,
        _payment("PAYMENT_REFUND_DENIED", payment, checkout=checkout_id),
        "test",
    )
    assert denied.outcome == "readback_required"
    reads = _Reads(payments={payment: _read(payment, "RECEIVED", 10_000, refunded=0)})
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).process_delivery(denied.delivery_id)
            == "converged"
        )
    (projection,) = await _all(
        harness,
        "SELECT status, settled_cents, reversed_cents, restored_cents, restoration_epoch "
        "FROM billing_payment_projections WHERE payment_id = :p",
        p=payment,
    )
    assert tuple(projection) == ("restored", 10_000, 10_000, 10_000, 1)
    (effect,) = await _all(
        harness,
        "SELECT source_revision, amount_cents FROM billing_payment_effects "
        "WHERE payment_id = :p AND effect_kind = 'restoration'",
        p=payment,
    )
    assert tuple(effect) == ("restoration_epoch:1", 10_000)
    (order,) = await _all(
        harness,
        "SELECT state, restored_cents FROM billing_topup_orders WHERE builder_id = :b",
        b=builder_id,
    )
    assert tuple(order) == ("restored", 10_000)


async def test_chargeback_status_converges_by_readback(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builder_id, _session, checkout_id = await _topup(
        harness, monkeypatch, "chargeback"
    )
    payment = "pay_pg_chargeback"
    await _deliver(
        harness, _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id), "test"
    )
    requested = await _deliver(
        harness,
        _payment("PAYMENT_CHARGEBACK_REQUESTED", payment, checkout=checkout_id),
        "test",
    )
    reads = _Reads(payments={payment: _read(payment, "CHARGEBACK_DISPUTE", 10_000)})
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).readback_payment(payment, delivery_id=requested.delivery_id)
            == "converged"
        )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_payment_projections WHERE payment_id = :p",
            p=payment,
        )
        == "chargeback_dispute"
    )


@pytest.mark.parametrize(
    ("event_name", "provider_status", "refunded_cents"),
    [
        ("PAYMENT_PARTIALLY_REFUNDED", "RECEIVED", 3_000),
        ("PAYMENT_CHARGEBACK_REQUESTED", "CHARGEBACK_DISPUTE", 0),
    ],
)
async def test_post_settlement_readback_without_payment_date_preserves_paid_at(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    event_name: str,
    provider_status: str,
    refunded_cents: int,
) -> None:
    _builder_id, _session, checkout_id = await _topup(
        harness, monkeypatch, f"readback without date {event_name}"
    )
    payment = f"pay_pg_no_date_{event_name.lower()}"
    await _deliver(
        harness, _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id), "test"
    )
    outcome = await _deliver(
        harness, _payment(event_name, payment, checkout=checkout_id), "test"
    )
    reads = _Reads(
        payments={
            payment: _read(
                payment,
                provider_status,
                10_000,
                refunded=refunded_cents,
                omit_payment_date=True,
            )
        }
    )
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).readback_payment(payment, delivery_id=outcome.delivery_id)
            == "converged"
        )
    (projection,) = await _all(
        harness,
        "SELECT status, paid_at FROM billing_payment_projections WHERE payment_id = :p",
        p=payment,
    )
    assert projection[1] == date(2026, 10, 11)


async def test_legacy_settlement_without_paid_at_requires_readback_before_fact(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(
        harness, monkeypatch, "legacy paid_at repair"
    )
    payment = "pay_pg_legacy_paid_at"
    await _deliver(
        harness, _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id), "test"
    )
    async with harness.session() as session:
        await session.execute(
            text(
                "UPDATE billing_payment_projections SET paid_at = NULL "
                "WHERE payment_id = :p"
            ),
            {"p": payment},
        )
        await session.commit()
    refunded = await _deliver(
        harness, _payment("PAYMENT_REFUNDED", payment, checkout=checkout_id), "test"
    )
    assert refunded.outcome == "readback_required"
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM billing_payment_effects "
            "WHERE payment_id = :p AND effect_kind = 'reversal'",
            p=payment,
        )
        == 0
    )
    reads = _Reads(
        payments={
            payment: _read(
                payment,
                "REFUNDED",
                10_000,
                refunded=10_000,
                include_payment_date=True,
            )
        }
    )
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).process_delivery(refunded.delivery_id)
            == "converged"
        )
    (projection,) = await _all(
        harness,
        "SELECT paid_at, reversed_cents FROM billing_payment_projections "
        "WHERE payment_id = :p",
        p=payment,
    )
    assert tuple(projection) == (date(2026, 10, 11), 10_000)


async def test_subscription_with_additional_clinics_settles_its_snapshot_amount(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, "test", _Flag())
    await _general(harness, "test")
    builder_id = await _builder(harness, "clinics")
    async with harness.session() as session:
        async with session.begin():
            for index in range(3):
                await session.execute(
                    text(
                        "INSERT INTO tenants (id, name, api_key_hash, is_active, "
                        "builder_id, plan_type, config) VALUES (:id, :name, :hash, true, "
                        ":b, 'pilot', '{}'::jsonb)"
                    ),
                    {
                        "id": uuid4(),
                        "name": f"clinic {index}",
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
        "SELECT amount_cents, pricing_snapshot ->> 'additional_clinics', external_checkout_id "
        "FROM billing_checkout_sessions WHERE id = :i",
        i=view.checkout_session_id,
    )
    cents, additional, checkout_id = snapshot
    assert (cents, additional) == (99_900 + 2 * 20_000, "2")
    await _deliver(
        harness,
        _subscription_created(view.checkout_session_id, "sub_pg_clinics", cents),
        "test",
    )
    await _deliver(
        harness,
        _payment(
            "PAYMENT_RECEIVED",
            "pay_pg_clinics",
            checkout=checkout_id,
            subscription="sub_pg_clinics",
            value=_brl(cents),
        ),
        "test",
    )
    (tx,) = await _all(
        harness,
        "SELECT amount_cents, plan FROM transactions WHERE builder_id = :b",
        b=builder_id,
    )
    assert tuple(tx) == (139_900, "plus")


# ---------------------------------------------------------------------------
# Fix loop 2: MEDIUM-A (restoration by cumulative cents) and MEDIUM-B
# (service-path holes).  Documented event ids, services only.
# ---------------------------------------------------------------------------


async def test_restoration_survives_an_absent_value_event_on_postgres(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "M-A")
    payment = "pay_pg_ma"
    await _deliver(
        harness, _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id), "test"
    )
    await _deliver(
        harness, _payment("PAYMENT_REFUNDED", payment, checkout=checkout_id), "test"
    )
    absent = _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id)
    absent["payment"]["value"] = None
    assert (await _deliver(harness, absent, "test")).outcome == "readback_required"
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_payment_projections WHERE payment_id = :p",
            p=payment,
        )
        == "reversed"
    )
    # A status-only event legitimately relabels the row; the reversed money is
    # still in the cumulative columns, which is what restoration reads.
    await _deliver(
        harness,
        _payment("PAYMENT_CHARGEBACK_REQUESTED", payment, checkout=checkout_id),
        "test",
    )
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_payment_projections WHERE payment_id = :p",
            p=payment,
        )
        == "chargeback_requested"
    )
    denied = await _deliver(
        harness,
        _payment("PAYMENT_REFUND_DENIED", payment, checkout=checkout_id),
        "test",
    )
    reads = _Reads(payments={payment: _read(payment, "RECEIVED", 10_000, refunded=0)})

    async def restorations() -> list[tuple[Any, ...]]:
        rows = await _all(
            harness,
            "SELECT source_revision, amount_cents FROM billing_payment_effects "
            "WHERE payment_id = :p AND effect_kind = 'restoration'",
            p=payment,
        )
        return [tuple(r) for r in rows]

    for _replay in range(2):
        async with harness.session() as session:
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).readback_payment(payment, delivery_id=denied.delivery_id)
        # The FIRST exact read restores; a replay never duplicates it.
        assert await restorations() == [("restoration_epoch:1", 10_000)]
    (row,) = await _all(
        harness,
        "SELECT p.status, p.restored_cents, o.state, o.restored_cents "
        "FROM billing_payment_projections p JOIN billing_topup_orders o "
        "ON o.id = p.topup_order_id WHERE p.payment_id = :p",
        p=payment,
    )
    assert tuple(row) == ("restored", 10_000, "restored", 10_000)
    del builder_id


async def test_payment_first_handoff_and_settlement_commit_as_one_transaction(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with harness.session() as session:
        session.add(Coupon(code="PGFIRST", discount_percent=10, uses_count=0))
        await session.commit()
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "payment-first", coupon="PGFIRST"
    )
    confirmed = _payment(
        "PAYMENT_RECEIVED",
        "pay_pg_first",
        checkout=checkout_id,
        subscription="sub_pg_first",
        value=_brl(cents),
    )

    seen: dict[str, Any] = {}

    async def failing_hook(
        self: Any, effect: Any, projection: Any, **_fact_context: Any
    ) -> None:
        # Group 4 passes the fact context (``cycle_start``) to this hook.
        # Inside the same transaction: prove handoff, effect, Transaction and
        # coupon were all written before the injected failure fires.
        (row,) = (
            await self._db.execute(
                text(
                    "SELECT (SELECT count(*) FROM billing_subscriptions "
                    "WHERE builder_id = :b), "
                    "(SELECT count(*) FROM billing_payment_effects "
                    "WHERE builder_id = :b), "
                    "(SELECT count(*) FROM transactions WHERE builder_id = :b "
                    "AND coupon_code = 'PGFIRST'), "
                    "(SELECT uses_count FROM coupons WHERE code = 'PGFIRST')"
                ),
                {"b": builder_id},
            )
        ).all()
        seen["before_failure"] = tuple(row)
        raise RuntimeError("injected after handoff, effect and Transaction")

    original = _FinancialState._financial_fact_hook
    monkeypatch.setattr(_FinancialState, "_financial_fact_hook", failing_hook)
    with pytest.raises(
        WebhookProcessingUnavailable, match="billing_webhook_unavailable"
    ) as exc_info:
        await _deliver(harness, confirmed, "test")
    monkeypatch.setattr(_FinancialState, "_financial_fact_hook", original)
    assert exc_info.value.failure_class == "unexpected"
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == (
        "injected after handoff, effect and Transaction"
    )
    assert seen["before_failure"] == (1, 1, 1, 1)
    (nothing,) = await _all(
        harness,
        "SELECT (SELECT count(*) FROM billing_subscriptions WHERE builder_id = :b), "
        "(SELECT state FROM billing_checkout_sessions WHERE id = :s), "
        "(SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b), "
        "(SELECT count(*) FROM transactions WHERE builder_id = :b), "
        "(SELECT uses_count FROM coupons WHERE code = 'PGFIRST'), "
        "(SELECT count(*) FROM billing_webhook_deliveries WHERE event_id = :e)",
        b=builder_id,
        s=session_id,
        e=event_key(confirmed["id"]),
    )
    assert tuple(nothing) == (0, "active", 0, 0, 0, 0)

    assert (await _deliver(harness, confirmed, "test")).outcome == "processed"
    await _deliver(
        harness,
        _payment(
            "PAYMENT_RECEIVED",
            "pay_pg_first",
            checkout=checkout_id,
            subscription="sub_pg_first",
            value=_brl(cents),
        ),
        "test",
    )
    (row,) = await _all(
        harness,
        "SELECT s.id = c.intent_id, s.status, s.coupon_code, c.state, "
        "(SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b), "
        "(SELECT coupon_code FROM transactions WHERE builder_id = :b), "
        "(SELECT uses_count FROM coupons WHERE code = 'PGFIRST'), "
        "(SELECT subscription_plan || '/' || subscription_status FROM builders "
        "WHERE id = :b) "
        "FROM billing_subscriptions s JOIN billing_checkout_sessions c "
        "ON c.resulting_external_subscription_id = s.external_subscription_id "
        "WHERE s.builder_id = :b",
        b=builder_id,
    )
    assert tuple(row) == (
        True,
        "active",
        "PGFIRST",
        "completed",
        1,
        "PGFIRST",
        1,
        "plus/active",
    )


async def test_already_applied_from_a_real_asyncpg_unique_violation(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheus_client import REGISTRY

    _builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "backstop")
    payment = "pay_pg_backstop"
    await _deliver(
        harness, _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id), "test"
    )
    # A writer that cannot see the committed effect (the race the unique key
    # backstops): the projection looks unsettled and the lookup misses.
    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE billing_payment_projections SET settled_cents = 0, "
                    "status = 'pending' WHERE payment_id = :p"
                ),
                {"p": payment},
            )

    async def unseen(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(BillingRepository, "find_payment_effect", unseen)
    labels = {"failure": "integrity", "disposition": "already_applied"}
    before = (
        REGISTRY.get_sample_value("neurostack_billing_webhook_failures_total", labels)
        or 0
    )
    drift = (
        REGISTRY.get_sample_value("neurostack_billing_alerts_total", {"kind": "drift"})
        or 0
    )
    received = _payment("PAYMENT_RECEIVED", payment, checkout=checkout_id)
    outcome = await _deliver(harness, received, "test")
    assert outcome.outcome == "processed"
    assert (
        REGISTRY.get_sample_value("neurostack_billing_webhook_failures_total", labels)
        == before + 1
    )
    assert (
        REGISTRY.get_sample_value("neurostack_billing_alerts_total", {"kind": "drift"})
        or 0
    ) == drift
    assert (
        await _scalar(
            harness,
            "SELECT status FROM billing_webhook_deliveries WHERE event_id = :e",
            e=event_key(received["id"]),
        )
        == "processed"
    )
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM billing_payment_effects WHERE payment_id = :p",
            p=payment,
        )
        == 1
    )


async def test_quarantine_write_failure_is_503_with_nothing_reserved(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import BackgroundTasks, HTTPException

    from src.api.routes import billing as billing_route
    from src.repositories.billing import BillingTransitionRejected

    _builder_id, _session, checkout_id = await _topup(harness, monkeypatch, "q-503")

    async def refuse(*_args: object, **_kwargs: object) -> str:
        raise BillingTransitionRejected("deterministic refusal")

    calls: list[int] = []
    original = BillingRepository.reserve_webhook_delivery

    async def reserve(self: Any, values: dict[str, object]) -> bool:
        calls.append(1)
        if len(calls) == 2:  # the quarantine write
            raise RuntimeError("injected quarantine write failure")
        return await original(self, values)

    monkeypatch.setattr(_FinancialState, "apply_payment", refuse)
    monkeypatch.setattr(BillingRepository, "reserve_webhook_delivery", reserve)
    payload = _payment("PAYMENT_CONFIRMED", "pay_pg_q503", checkout=checkout_id)
    body = encode_json(payload)

    class Request:
        scope = {
            "state": {
                "asaas_webhook_body": body,
                "asaas_webhook_payload": json.loads(body, parse_float=Decimal),
            }
        }

    async with harness.session() as session:
        with pytest.raises(HTTPException) as exc_info:
            await billing_route.asaas_webhook(Request(), BackgroundTasks(), session)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 503
    assert len(calls) == 2
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM billing_webhook_deliveries WHERE event_id = :e",
            e=event_key(payload["id"]),
        )
        == 0
    )


async def test_checkout_expired_frees_a_subscription_attempt_on_postgres(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, checkout_id, _cents = await _subscription(
        harness, monkeypatch, "expired"
    )
    expired = await _deliver(
        harness, _payload("CHECKOUT_EXPIRED", {"id": checkout_id}), "test"
    )
    assert expired.outcome == "readback_required"
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=_Reads(), environment="test"
            ).process_delivery(expired.delivery_id)
            == "converged"
        )
    assert (
        await _scalar(
            harness,
            "SELECT state FROM billing_checkout_sessions WHERE id = :i",
            i=session_id,
        )
        == "expired"
    )
    async with harness.session() as session:
        assert (
            await BillingRepository(session).subscription_checkout_blocker(
                builder_id=builder_id
            )
            is None
        )


async def test_readback_payment_hands_off_and_settles_on_postgres(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "readback handoff payment"
    )
    paid = await _deliver(
        harness, _payload("CHECKOUT_PAID", {"id": checkout_id}), "test"
    )
    reads = _Reads(
        payments={
            "pay_pg_rb_first": PaymentReadback(
                "pay_pg_rb_first",
                "RECEIVED",
                False,
                cents,
                "sub_pg_rb_first",
                checkout_id,
                None,
                "CREDIT_CARD",
                0,
                payment_date=date(2026, 10, 11),
            )
        }
    )
    async with harness.session() as session:
        assert (
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).readback_payment("pay_pg_rb_first", delivery_id=paid.delivery_id)
            == "converged"
        )
    (row,) = await _all(
        harness,
        "SELECT s.id = c.intent_id, s.status, c.state, "
        "(SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b "
        "AND effect_kind = 'settlement'), "
        "(SELECT amount_cents FROM transactions WHERE builder_id = :b) "
        "FROM billing_subscriptions s JOIN billing_checkout_sessions c "
        "ON c.resulting_external_subscription_id = s.external_subscription_id "
        "WHERE c.id = :s",
        b=builder_id,
        s=session_id,
    )
    assert tuple(row) == (True, "active", "completed", 1, cents)


# ---------------------------------------------------------------------------
# Fix loop 3 (MEDIUM-E): deleted never-settled payment read back live with a
# completed refund -> settled + reversed; the totals CHECK never fires.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "refunded", "expected", "label"),
    [
        (
            "RECEIVED",
            3_000,
            [
                ("reversal", "reversed_cents:3000", 3_000),
                ("settlement", "settlement:v1", 10_000),
            ],
            "partially_reversed",
        ),
        (
            "PENDING",
            3_000,
            [],
            "refunded_before_settlement",
        ),
        (
            "REFUNDED",
            10_000,
            [],
            "refunded_before_settlement",
        ),
    ],
)
async def test_deleted_payment_with_refunds_settles_and_reverses_on_postgres(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    refunded: int,
    expected: list[tuple[Any, ...]],
    label: str,
) -> None:
    builder_id, _session, checkout_id = await _topup(
        harness, monkeypatch, f"M-E {status}"
    )
    payment = f"pay_pg_me_{status.lower()}"
    await _deliver(
        harness, _payment("PAYMENT_CREATED", payment, checkout=checkout_id), "test"
    )
    await _deliver(
        harness, _payment("PAYMENT_DELETED", payment, checkout=checkout_id), "test"
    )
    restored = await _deliver(
        harness, _payment("PAYMENT_RESTORED", payment, checkout=checkout_id), "test"
    )
    assert restored.outcome == "readback_required"
    reads = _Reads(
        payments={payment: _read(payment, status, 10_000, refunded=refunded)}
    )
    observed_at = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    for replay in range(2):
        async with harness.session() as session:
            outcome = await BillingReadbackService(
                session,
                client_factory=reads,
                environment="test",
                clock=lambda: observed_at,
            ).readback_payment(payment, delivery_id=restored.delivery_id)
        assert outcome == "converged"
        effects = await _all(
            harness,
            "SELECT effect_kind, source_revision, amount_cents "
            "FROM billing_payment_effects WHERE payment_id = :p "
            "ORDER BY effect_kind, source_revision",
            p=payment,
        )
        assert [tuple(e) for e in effects] == expected
        (row,) = await _all(
            harness,
            "SELECT p.status, p.settled_cents, p.reversed_cents, p.restored_cents, "
            "p.restoration_epoch, o.state, o.settled_cents, o.reversed_cents "
            ", p.pre_settlement_refund_cents, p.pre_settlement_refund_status, "
            "p.pre_settlement_refund_observed_at "
            "FROM billing_payment_projections p JOIN billing_topup_orders o "
            "ON o.id = p.topup_order_id WHERE p.payment_id = :p",
            p=payment,
        )
        if status == "RECEIVED":
            assert tuple(row) == (
                label,
                10_000,
                refunded,
                0,
                0,
                label,
                10_000,
                refunded,
                None,
                None,
                None,
            ), f"replay {replay}"
        else:
            assert tuple(row) == (
                label,
                0,
                0,
                0,
                0,
                "pending",
                0,
                0,
                refunded,
                status,
                observed_at,
            ), f"replay {replay}"
    assert await _scalar(
        harness,
        "SELECT count(*) FROM transactions WHERE builder_id = :b",
        b=builder_id,
    ) == (1 if status == "RECEIVED" else 0)


# ---------------------------------------------------------------------------
# Group 10 E4: no webhook or readback path takes the legacy roster fence.
#
# The four former call sites -- the handoff, the target rows of a payment of
# a subscription, an event on an existing subscription and the subscription
# readback -- took the global roster advisory key and the
# billing_roster_control row right after the environment rollout lock.  Each
# runs under that rollout lock, which already serializes them within one
# environment.  The global fence also serialized handoffs ACROSS environments;
# without it one environment per database is a requirement (two environments
# sharing a database could leave a builder with two active subscriptions; see
# the billing_webhook module docstring and the deployment runbook).  What is
# left of the fence is the database's statement trigger on UPDATE/DELETE of
# billing_subscriptions (migration 026), which migration 036 (E5) removes.
# ---------------------------------------------------------------------------

# Inventory for Group 10 E5/E6, same convention as the characterization
# module (tests/integration/test_asaas_subscription_triggers_postgres.py):
# pytest node names, checked by test_break_inventory_names_only_collected_tests
# below.  A test may appear in both lists when it breaks for both reasons.
#
# 036 drops billing_subscriptions.roster_revision (these tests select it) and
# the roster statement trigger.  From 036 on the held-fence test only runs its
# "no trigger" branch; its trigger branch becomes unreachable.
TESTS_THAT_BREAK_AFTER_036: tuple[str, ...] = (
    "test_handoff_holds_the_rollout_lock_against_eligibility",
    "test_e4_former_roster_call_sites_commit_without_the_fence",
    "test_e4_a_held_roster_fence_blocks_only_a_subscription_update",
    "test_e4_creation_payment_cancellation_and_transition_queue_on_rollout",
)
# 037 drops billing_roster_control, which the held-fence test locks.
TESTS_THAT_BREAK_AFTER_037: tuple[str, ...] = (
    "test_e4_a_held_roster_fence_blocks_only_a_subscription_update",
)


async def test_break_inventory_names_only_collected_tests(
    request: pytest.FixtureRequest,
) -> None:
    """Every inventory entry is a real node name of this module (not stale)."""

    module = request.node.parent
    assert isinstance(module, pytest.Module)
    collected = {item.name for item in module.collect()}
    for listed in (TESTS_THAT_BREAK_AFTER_036, TESTS_THAT_BREAK_AFTER_037):
        assert len(set(listed)) == len(listed)
        assert sorted(set(listed) - collected) == []


# The key the 026 triggers take; it has no Python mirror any more.
_LEGACY_ROSTER_FENCE_KEY = 3486705124568449331
_FENCE_TRIGGER = (
    "SELECT count(*) FROM pg_trigger "
    "WHERE tgname = 'trg_billing_subscriptions_roster_fence' AND NOT tgisinternal"
)
# Long enough for any lock-free path on a slow runner; a path that waits on
# the held fence (the pre-E4 code) fails here instead of hanging.
_FENCE_FREE_SECONDS = 15.0


@dataclass(frozen=True)
class _Lock:
    locktype: str
    relation: str | None
    advisory: int | None
    granted: bool


def _bigint_key(high: int, low: int) -> int:
    """The bigint advisory key that pg_locks splits into classid/objid."""

    key = (high << 32) | low
    return key - 2**64 if key >= 2**63 else key


async def _locks_of(session: AsyncSession, pid: int | None = None) -> list[_Lock]:
    """The locks of backend *pid* (default: *session*'s own backend)."""

    rows = (
        await session.execute(
            text(
                "SELECT locktype, CASE WHEN locktype = 'relation' "
                "THEN relation::regclass::text END, classid::bigint, "
                "objid::bigint, objsubid, granted FROM pg_locks "
                "WHERE pid = COALESCE(CAST(:pid AS integer), pg_backend_pid())"
            ),
            {"pid": pid},
        )
    ).all()
    return [
        _Lock(
            locktype=str(locktype),
            relation=relation,
            advisory=(
                _bigint_key(int(high), int(low))
                if locktype == "advisory" and subid == 1
                else None
            ),
            granted=bool(granted),
        )
        for locktype, relation, high, low, subid, granted in rows
    ]


def _roster_locks(locks: list[_Lock]) -> list[_Lock]:
    return [
        lock
        for lock in locks
        if lock.advisory == _LEGACY_ROSTER_FENCE_KEY
        or (lock.relation or "").startswith("billing_roster_control")
    ]


@asynccontextmanager
async def _locks_at_commit(session: AsyncSession) -> AsyncIterator[list[list[_Lock]]]:
    """Record *session*'s own locks right before each of its commits."""

    snapshots: list[list[_Lock]] = []
    commit = session.commit

    async def recording_commit() -> None:
        snapshots.append(await _locks_of(session))
        await commit()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session, "commit", recording_commit)
        yield snapshots


def _assert_rollout_without_roster(locks: list[_Lock], environment: str) -> None:
    # Recorded inside the path's transaction: the rollout lock is held ...
    assert billing_advisory_key(environment) in {
        lock.advisory for lock in locks if lock.granted
    }
    assert "billing_rollout_control" in {lock.relation for lock in locks}
    # ... and nothing of the roster: neither its key nor its control row.
    assert _roster_locks(locks) == []


async def _waiting(harness: _Harness, pid: int) -> list[_Lock]:
    """Poll until backend *pid* waits on a lock; return all of its locks."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    while True:
        async with harness.session() as probe:
            locks = await _locks_of(probe, pid)
        if any(not lock.granted for lock in locks):
            return locks
        assert loop.time() < deadline, f"backend {pid} never waited on a lock"
        await asyncio.sleep(0.02)


def _awaited(locks: list[_Lock]) -> list[tuple[str, int | None]]:
    return [(lock.locktype, lock.advisory) for lock in locks if not lock.granted]


async def test_e4_former_roster_call_sites_commit_without_the_fence(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each former call site, through every public entry point that reaches
    it (deliveries and the subscription, payment and checkout readbacks),
    commits holding the rollout lock and nothing of the roster.  Before E4
    each one held the roster key and the roster row lock at this point."""

    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "E4 no roster fence"
    )
    external = "sub_pg_e4_no_fence"

    async def deliver(payload: dict[str, Any]) -> tuple[Any, list[_Lock]]:
        async with harness.session() as session:
            async with _locks_at_commit(session) as snapshots:
                outcome = await BillingWebhookService(
                    session, environment="test"
                ).receive(_envelope(payload))
        (locks,) = snapshots
        return outcome, locks

    # 1. The handoff creates the subscription row (former first call site).
    created, locks = await deliver(_subscription_created(session_id, external, cents))
    assert created.outcome == "processed"
    _assert_rollout_without_roster(locks, "test")
    # 2. A payment of that subscription locks its target rows (second site).
    pending, locks = await deliver(
        _payment(
            "PAYMENT_CREATED",
            "pay_pg_e4_no_fence",
            checkout=checkout_id,
            subscription=external,
            value=_brl(cents),
        )
    )
    assert pending.outcome == "processed"
    _assert_rollout_without_roster(locks, "test")
    # 3. An event on the existing subscription row (third site).
    again, locks = await deliver(_subscription_created(session_id, external, cents))
    assert again.outcome == "processed"
    _assert_rollout_without_roster(locks, "test")
    # 4. The subscription readback (fourth site), converged with no write.
    subscription_id, next_due = tuple(
        (
            await _all(
                harness,
                "SELECT s.id, c.next_due_date FROM billing_subscriptions s "
                "JOIN billing_checkout_sessions c ON c.intent_id = s.id "
                "WHERE s.builder_id = :b",
                b=builder_id,
            )
        )[0]
    )
    reads = _Reads(
        subscriptions={
            external: SubscriptionReadback(
                external, "ACTIVE", False, "MONTHLY", cents, next_due, str(session_id)
            )
        }
    )
    async with harness.session() as session:
        async with _locks_at_commit(session) as snapshots:
            converged = await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).readback_subscription(subscription_id)
    assert converged == "converged"
    (locks,) = snapshots
    _assert_rollout_without_roster(locks, "test")
    # 5. The payment readback reaches the second site under its own lock.
    reads.payments["pay_pg_e4_no_fence"] = _read("pay_pg_e4_no_fence", "PENDING", cents)
    async with harness.session() as session:
        async with _locks_at_commit(session) as snapshots:
            await BillingReadbackService(
                session, client_factory=reads, environment="test"
            ).readback_payment("pay_pg_e4_no_fence", delivery_id=pending.delivery_id)
    (locks,) = snapshots
    _assert_rollout_without_roster(locks, "test")

    (row,) = await _all(
        harness,
        "SELECT status, is_active, roster_revision, "
        "(SELECT count(*) FROM billing_payment_projections WHERE builder_id = :b) "
        "FROM billing_subscriptions WHERE builder_id = :b",
        b=builder_id,
    )
    # The legacy column holds the constant the CHECK (> 0) needs until 036.
    assert tuple(row) == ("active_linked", True, 1, 1)

    # 6. The checkout readback reaches the first site (a readback handoff).
    other_builder, other_session, other_checkout, _cents = await _subscription(
        harness, monkeypatch, "E4 no roster fence, readback handoff"
    )
    paid = await _deliver(
        harness, _payload("CHECKOUT_PAID", {"id": other_checkout}), "test"
    )
    assert paid.outcome == "processed"
    handoff_reads = _Reads(
        by_reference={
            other_session: (
                ProviderRecord(
                    "sub_pg_e4_no_fence_rb", str(other_session), "ACTIVE", False
                ),
            )
        }
    )
    async with harness.session() as session:
        async with _locks_at_commit(session) as snapshots:
            linked = await BillingReadbackService(
                session, client_factory=handoff_reads, environment="test"
            ).readback_checkout(other_session)
    assert linked == "converged"
    (locks,) = snapshots
    _assert_rollout_without_roster(locks, "test")
    assert (
        await _scalar(
            harness,
            "SELECT roster_revision FROM billing_subscriptions WHERE builder_id = :b",
            b=other_builder,
        )
        == 1
    )


async def test_e4_a_held_roster_fence_blocks_only_a_subscription_update(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction holding the roster fence exactly as the 026 triggers take
    it (the global key, then the control row) no longer blocks the handoff, a
    payment of the subscription, a redelivered event or the readback; before
    E4 each of them waited on it.  A deletion UPDATEs billing_subscriptions,
    so it still meets the database's statement fence until migration 036 --
    only after its rollout lock and its subscription row lock -- and applies
    once the holder ends."""

    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "E4 fence held"
    )
    external = "sub_pg_e4_fence_held"
    fence_trigger = int(await _scalar(harness, _FENCE_TRIGGER)) == 1
    holder = harness.session()
    writer = harness.session()
    try:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _LEGACY_ROSTER_FENCE_KEY},
        )
        await holder.execute(
            text(
                "SELECT 1 FROM billing_roster_control WHERE singleton = true "
                "FOR UPDATE"
            )
        )

        async def unblocked(call: Awaitable[Any]) -> Any:
            return await asyncio.wait_for(call, timeout=_FENCE_FREE_SECONDS)

        created = await unblocked(
            _deliver(
                harness, _subscription_created(session_id, external, cents), "test"
            )
        )
        assert created.outcome == "processed"
        pending = await unblocked(
            _deliver(
                harness,
                _payment(
                    "PAYMENT_CREATED",
                    "pay_pg_e4_fence_held",
                    checkout=checkout_id,
                    subscription=external,
                    value=_brl(cents),
                ),
                "test",
            )
        )
        assert pending.outcome == "processed"
        again = await unblocked(
            _deliver(
                harness, _subscription_created(session_id, external, cents), "test"
            )
        )
        assert again.outcome == "processed"
        subscription_id, next_due = tuple(
            (
                await _all(
                    harness,
                    "SELECT s.id, c.next_due_date FROM billing_subscriptions s "
                    "JOIN billing_checkout_sessions c ON c.intent_id = s.id "
                    "WHERE s.builder_id = :b",
                    b=builder_id,
                )
            )[0]
        )
        reads = _Reads(
            subscriptions={
                external: SubscriptionReadback(
                    external,
                    "ACTIVE",
                    False,
                    "MONTHLY",
                    cents,
                    next_due,
                    str(session_id),
                )
            }
        )

        async def readback() -> str:
            async with harness.session() as session:
                return await BillingReadbackService(
                    session, client_factory=reads, environment="test"
                ).readback_subscription(subscription_id)

        assert await unblocked(readback()) == "converged"

        pid = await _pid(writer)
        deletion = asyncio.create_task(
            BillingWebhookService(writer, environment="test").receive(
                _envelope(
                    _payload("SUBSCRIPTION_DELETED", {"id": external, "deleted": True})
                )
            )
        )
        if fence_trigger:
            locks = await _waiting(harness, pid)
            # It waits on the trigger's key alone, already holding the
            # rollout lock and the subscription it is updating.
            assert _awaited(locks) == [("advisory", _LEGACY_ROSTER_FENCE_KEY)]
            assert billing_advisory_key("test") in {
                lock.advisory for lock in locks if lock.granted
            }
            assert {"billing_rollout_control", "billing_subscriptions"} <= {
                lock.relation for lock in locks if lock.granted
            }
            assert not deletion.done()
        else:
            assert (await unblocked(deletion)).outcome == "readback_required"
        await holder.rollback()
        deleted = await asyncio.wait_for(deletion, timeout=30)
    finally:
        await holder.close()
        await writer.close()
    assert deleted.outcome == "readback_required"
    (row,) = await _all(
        harness,
        "SELECT status, is_active, roster_revision FROM billing_subscriptions "
        "WHERE builder_id = :b",
        b=builder_id,
    )
    assert tuple(row) == ("non_billable_pending_readback", False, 1)


async def test_e4_creation_payment_cancellation_and_transition_queue_on_rollout(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the environment's rollout lock held, a handoff, a settling payment
    of the same subscription, its cancellation and a rollout transition each
    queue on exactly the rollout advisory key, holding no checkout or
    subscription row and no roster fence.  Released, they apply one at a time
    in arrival order (the cancellation sees the settled row) and the end
    state is consistent: no deadlock, no lost update."""

    environment = "staging"
    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, "E4 race", environment=environment
    )
    external = "sub_pg_e4_race"
    payment_id = "pay_pg_e4_race"
    pids: dict[str, int] = {}

    def delivery(name: str, payload: dict[str, Any]) -> Callable[[], Awaitable[Any]]:
        async def run() -> Any:
            async with harness.session() as session:
                # Same transaction as the delivery: the pid stays its backend.
                pids[name] = await _pid(session)
                return await BillingWebhookService(
                    session, environment=environment
                ).receive(_envelope(payload))

        return run

    async def transition() -> str:
        async with harness.session() as session:
            pids["transition"] = await _pid(session)
            repository = BillingRepository(session)
            control = await repository.lock_rollout_control(environment=environment)
            control = await repository.transition_rollout(
                environment=environment,
                expected_revision=control.rollout_revision,
                to_state="disabled",
                evidence=_evidence(),
            )
            state = str(control.state)
            await session.commit()
            return state

    contenders: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
        (
            "handoff",
            delivery("handoff", _subscription_created(session_id, external, cents)),
        ),
        (
            "payment",
            delivery(
                "payment",
                _payment(
                    "PAYMENT_CONFIRMED",
                    payment_id,
                    checkout=checkout_id,
                    subscription=external,
                    value=_brl(cents),
                ),
            ),
        ),
        (
            "cancellation",
            delivery(
                "cancellation",
                _payload("SUBSCRIPTION_DELETED", {"id": external, "deleted": True}),
            ),
        ),
        ("transition", transition),
    ]
    rollout_key = billing_advisory_key(environment)
    tasks: list[asyncio.Task[Any]] = []
    holder = harness.session()
    try:
        await BillingRepository(holder).lock_rollout_control(environment=environment)
        for name, run in contenders:
            tasks.append(asyncio.create_task(run()))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30
            while name not in pids:
                assert loop.time() < deadline, f"{name} never started"
                await asyncio.sleep(0.01)
            locks = await _waiting(harness, pids[name])
            # Queued on the rollout key alone, in arrival order, holding no
            # roster fence (only its own delivery row, if any).
            assert _awaited(locks) == [("advisory", rollout_key)], name
            assert _roster_locks(locks) == [], name
            assert not tasks[-1].done(), name
        # None of them holds the checkout row its path will lock.
        async with harness.session() as probe:
            rows = (
                await probe.execute(
                    text(
                        "SELECT id FROM billing_checkout_sessions WHERE id = :id "
                        "FOR UPDATE NOWAIT"
                    ),
                    {"id": session_id},
                )
            ).all()
            assert len(rows) == 1
            await probe.rollback()
        await holder.rollback()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=60
        )
    finally:
        await holder.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        # This test leaves the environment disabled for the rest of the module.
        _READY.discard((id(harness), environment))
    for (name, _run), result in zip(contenders, results, strict=True):
        assert not isinstance(result, BaseException), (name, result)
    handoff, payment, cancellation, state = results
    assert (handoff.outcome, payment.outcome, cancellation.outcome, state) == (
        "processed",
        "processed",
        "readback_required",
        "disabled",
    )
    (row,) = await _all(
        harness,
        "SELECT (SELECT count(*) FROM billing_subscriptions WHERE builder_id = :b), "
        "s.status, s.is_active, s.roster_revision, c.state, p.status, "
        "p.settled_cents, "
        "(SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b), "
        "(SELECT count(*) FROM transactions WHERE builder_id = :b) "
        "FROM billing_subscriptions s "
        "JOIN billing_checkout_sessions c ON c.id = :s "
        "JOIN billing_payment_projections p ON p.subscription_id = s.id "
        "WHERE s.builder_id = :b",
        b=builder_id,
        s=session_id,
    )
    assert tuple(row) == (
        1,
        "non_billable_pending_readback",
        False,
        1,
        "completed",
        "settled",
        cents,
        1,
        1,
    )


async def test_e4_identical_subscription_deliveries_hand_off_once(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two deliveries of one SUBSCRIPTION_CREATED: the second waits on the
    first's idempotency key (the unique delivery index), before any rollout
    or roster lock, and is a duplicate once the first commits."""

    builder_id, session_id, _checkout, cents = await _subscription(
        harness, monkeypatch, "E4 duplicate handoff"
    )
    envelope = _envelope(_subscription_created(session_id, "sub_pg_e4_dup", cents))
    first = harness.session()
    second = harness.session()
    try:
        held = await BillingWebhookService(first, environment="test")._receive(envelope)
        pid = await _pid(second)
        task = asyncio.create_task(
            BillingWebhookService(second, environment="test").receive(envelope)
        )
        locks = await _waiting(harness, pid)
        assert _awaited(locks) == [("transactionid", None)]
        assert {lock.advisory for lock in locks} == {None}
        assert not task.done()
        await first.commit()
        late = await asyncio.wait_for(task, timeout=30)
    finally:
        await first.close()
        await second.close()
    assert (held.outcome, late.outcome) == ("processed", "duplicate")
    (row,) = await _all(
        harness,
        "SELECT (SELECT count(*) FROM billing_webhook_deliveries WHERE event_id = :e), "
        "(SELECT count(*) FROM billing_subscriptions WHERE builder_id = :b), "
        "(SELECT state FROM billing_checkout_sessions WHERE id = :s)",
        e=envelope.event_id,
        b=builder_id,
        s=session_id,
    )
    assert tuple(row) == (1, 1, "completed")


async def _rollout_waiter(
    harness: _Harness, holder_pid: int, environment: str
) -> tuple[int, list[_Lock]]:
    """Poll until another backend waits on *environment*'s rollout key.

    A readback runs several transactions and, under NullPool, each may get a
    new backend, so the waiter is found by the lock it waits on; returns its
    pid and all of its locks.
    """

    key = billing_advisory_key(environment)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    while True:
        async with harness.session() as probe:
            pid = await probe.scalar(
                text(
                    "SELECT lock.pid FROM pg_locks AS lock "
                    "JOIN pg_database AS db ON db.oid = lock.database "
                    "WHERE db.datname = current_database() "
                    "AND lock.locktype = 'advisory' AND NOT lock.granted "
                    "AND lock.objsubid = 1 AND lock.classid::bigint = :high "
                    "AND lock.objid::bigint = :low AND lock.pid <> :holder"
                ),
                {
                    "high": (key >> 32) & 0xFFFFFFFF,
                    "low": key & 0xFFFFFFFF,
                    "holder": holder_pid,
                },
            )
            if pid is not None:
                return int(pid), await _locks_of(probe, int(pid))
        assert loop.time() < deadline, "no backend queued on the rollout lock"
        await asyncio.sleep(0.02)


async def _assert_rows_unlocked(harness: _Harness, rows: dict[str, Any]) -> None:
    """Every named row is free: ``FOR UPDATE NOWAIT`` would raise otherwise."""

    for table, row_id in rows.items():
        async with harness.session() as probe:
            found = (
                await probe.execute(
                    text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": row_id},
                )
            ).all()
            assert len(found) == 1, table
            await probe.rollback()


@pytest.mark.parametrize("readback", ["subscription", "payment", "checkout"])
async def test_e4_readbacks_take_the_rollout_lock_before_any_billing_row(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, readback: str
) -> None:
    """Each readback's locked transaction takes the environment rollout lock
    before any billing row.  With the lock held elsewhere it waits on exactly
    the rollout key while holding no builder, checkout, subscription or
    projection row and nothing of the roster (the payment readback holds only
    its own delivery row, locked first by design); released, it converges."""

    builder_id, session_id, checkout_id, cents = await _subscription(
        harness, monkeypatch, f"E4 readback order {readback}"
    )
    external = f"sub_pg_e4_order_{readback}"
    payment_id = f"pay_pg_e4_order_{readback}"
    reads = _Reads()
    rows: dict[str, Any] = {
        "builders": builder_id,
        "billing_checkout_sessions": session_id,
    }
    if readback == "checkout":
        paid = await _deliver(
            harness, _payload("CHECKOUT_PAID", {"id": checkout_id}), "test"
        )
        assert paid.outcome == "processed"
        reads.by_reference[session_id] = (
            ProviderRecord(external, str(session_id), "ACTIVE", False),
        )
    else:
        created = await _deliver(
            harness, _subscription_created(session_id, external, cents), "test"
        )
        assert created.outcome == "processed"
        subscription_id, next_due = tuple(
            (
                await _all(
                    harness,
                    "SELECT s.id, c.next_due_date FROM billing_subscriptions s "
                    "JOIN billing_checkout_sessions c ON c.intent_id = s.id "
                    "WHERE s.builder_id = :b",
                    b=builder_id,
                )
            )[0]
        )
        rows["billing_subscriptions"] = subscription_id
        reads.subscriptions[external] = SubscriptionReadback(
            external, "ACTIVE", False, "MONTHLY", cents, next_due, str(session_id)
        )
        pending = await _deliver(
            harness,
            _payment(
                "PAYMENT_CREATED",
                payment_id,
                checkout=checkout_id,
                subscription=external,
                value=_brl(cents),
            ),
            "test",
        )
        assert pending.outcome == "processed"
        rows["billing_payment_projections"] = await _scalar(
            harness,
            "SELECT id FROM billing_payment_projections WHERE payment_id = :p",
            p=payment_id,
        )
        reads.payments[payment_id] = _read(payment_id, "PENDING", cents)

    async def run() -> str:
        async with harness.session() as session:
            service = BillingReadbackService(
                session, client_factory=reads, environment="test"
            )
            if readback == "subscription":
                return await service.readback_subscription(
                    rows["billing_subscriptions"]
                )
            if readback == "payment":
                return await service.readback_payment(
                    payment_id, delivery_id=pending.delivery_id
                )
            return await service.readback_checkout(session_id)

    holder = harness.session()
    task: asyncio.Task[str] | None = None
    try:
        holder_pid = await _pid(holder)
        await BillingRepository(holder).lock_rollout_control(environment="test")
        task = asyncio.create_task(run())
        _pid_waiting, locks = await _rollout_waiter(harness, holder_pid, "test")
        assert _awaited(locks) == [("advisory", billing_advisory_key("test"))]
        assert _roster_locks(locks) == []
        await _assert_rows_unlocked(harness, rows)
        assert not task.done()
        await holder.rollback()
        outcome = await asyncio.wait_for(task, timeout=30)
    finally:
        await holder.close()
        if task is not None and not task.done():
            task.cancel()
    assert outcome == "converged"
