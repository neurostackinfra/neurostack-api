"""Group 3 state machine on SQLite: envelope, correlation, effects, convergence.

The real ``BillingWebhookService``/``BillingReadbackService`` and repository
SQL run here; PostgreSQL-only behaviour (advisory locks, triggers, CHECKs
guarded by ``ddl_if``, real concurrency) is proven in
``tests/integration/test_asaas_webhook_postgres.py``.  No network: every
provider read is an in-memory fake.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
from prometheus_client import REGISTRY
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.billing import (
    BillingCheckoutSession,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingPaymentProjection,
    BillingRolloutControl,
    BillingRosterControl,
    BillingSubscription,
    BillingTopupOrder,
    BillingWebhookDelivery,
)
from src.models.builder import Builder
from src.models.coupon import Coupon
from src.models.transaction import Transaction
from src.repositories.billing import (
    BillingCanaryBudgetExceeded,
    BillingCapabilityRejected,
    BillingRepository,
    BillingStateUnavailable,
    BillingTransitionRejected,
)
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import billing_checkout as checkout_module
from src.services.asaas import (
    PaymentReadback,
    ProviderRecord,
    SubscriptionReadback,
    WebhookConfig,
)
from src.services.billing_catalog import BILLING_CATALOG
from src.services.billing_checkout import (
    CHECKOUT_CONVERGENCE_LIMIT,
    CONVERGENCE_READ_SPACING,
    BillingCheckoutService,
    subscription_pricing_snapshot,
    topup_pricing_snapshot,
)
from src.services.billing_webhook import (
    CHECKOUT_EVENT_STATES,
    PAYMENT_EVENT_STATUSES,
    SUBSCRIPTION_EVENT_STATUSES,
    WEBHOOK_EVENTS,
    AsaasWebhookProvisioner,
    BillingReadbackService,
    BillingWebhookService,
    WebhookEnvelope,
    WebhookEnvelopeRejected,
    WebhookNotReady,
    WebhookProcessingUnavailable,
    WebhookReadiness,
    WebhookReadinessGate,
    _FinancialState,
    classify_failure,
    compare_webhook,
    desired_webhook_settings,
    event_key,
    ordering_key,
    parse_envelope,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "asaas" / "webhook"
ENVIRONMENT = "test"
NOW = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(), parse_float=Decimal)


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, default=str).encode()


def _event(
    name: str,
    *,
    event_id: str | None = None,
    when: str = "2026-10-11 10:00:00",
    **resource: object,
) -> WebhookEnvelope:
    family = name.split("_", 1)[0].lower()
    template = {
        "checkout": "checkout_event.json",
        "subscription": "subscription_event.json",
        "payment": "payment_event.json",
    }.get(family, "unknown_event.json")
    payload = copy.deepcopy(_fixture(template))
    payload["event"] = name
    payload["id"] = event_id or _documented_id()
    payload["dateCreated"] = when
    if family in ("checkout", "subscription", "payment"):
        # The template's externalReference is a foreign UUID; it must be
        # chosen explicitly by each test (a foreign one is a conflict).
        payload[family]["externalReference"] = None
        for key, value in resource.items():
            payload[family][key] = value
    return parse_envelope(_body(payload), payload)


def _documented_id() -> str:
    """The provider's documented id shape: ``evt_<32 hex>&<digits>``."""

    return f"evt_{uuid4().hex}&{uuid4().int % 10**9}"


def _metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@dataclass
class _Provider:
    """In-memory exact-ID provider reads (no network)."""

    payments: dict[str, PaymentReadback] = field(default_factory=dict)
    subscriptions: dict[str, SubscriptionReadback] = field(default_factory=dict)
    subscription_charges: dict[str, tuple[PaymentReadback, ...]] = field(
        default_factory=dict
    )
    by_reference_subscriptions: dict[UUID, tuple[ProviderRecord, ...]] = field(
        default_factory=dict
    )
    by_reference_payments: dict[UUID, tuple[ProviderRecord, ...]] = field(
        default_factory=dict
    )
    by_checkout_payments: dict[str, tuple[ProviderRecord, ...]] = field(
        default_factory=dict
    )
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    def __call__(self) -> "_Provider":
        return self

    def _check(self, call: str) -> None:
        self.calls.append(call)
        if self.fail:
            raise RuntimeError("provider unavailable")

    async def get_payment(self, payment_id: str) -> PaymentReadback:
        self._check("get_payment")
        return self.payments[payment_id]

    async def get_subscription(self, subscription_id: str) -> SubscriptionReadback:
        self._check("get_subscription")
        return self.subscriptions[subscription_id]

    async def list_payments_by_subscription(
        self, subscription_id: str
    ) -> tuple[PaymentReadback, ...]:
        self._check("list_payments_by_subscription")
        return self.subscription_charges.get(subscription_id, ())

    async def list_subscriptions_by_external_reference(
        self, reference: UUID
    ) -> tuple[ProviderRecord, ...]:
        self._check("list_subscriptions_by_external_reference")
        return self.by_reference_subscriptions.get(reference, ())

    async def list_payments_by_external_reference(
        self, reference: UUID
    ) -> tuple[ProviderRecord, ...]:
        self._check("list_payments_by_external_reference")
        return self.by_reference_payments.get(reference, ())

    async def list_payments_by_checkout(
        self, checkout_id: str
    ) -> tuple[ProviderRecord, ...]:
        self._check("list_payments_by_checkout")
        return self.by_checkout_payments.get(checkout_id, ())


def _payment_read(
    payment_id: str,
    *,
    status: str,
    value_cents: int,
    refunded_cents: int | None = 0,
    deleted: bool = False,
    subscription_id: str | None = None,
    checkout_id: str | None = None,
) -> PaymentReadback:
    return PaymentReadback(
        payment_id=payment_id,
        status=status,
        deleted=deleted,
        value_cents=value_cents,
        subscription_id=subscription_id,
        checkout_id=checkout_id,
        external_reference=None,
        billing_type="PIX",
        refunded_cents=refunded_cents,
    )


@dataclass
class _World:
    sessions: async_sessionmaker[AsyncSession]
    builder_id: UUID
    provider: _Provider

    async def add(self, *rows: object) -> None:
        async with self.sessions() as session:
            session.add_all(rows)
            await session.commit()

    async def receive(self, envelope: WebhookEnvelope) -> Any:
        async with self.sessions() as session:
            return await BillingWebhookService(
                session, environment=ENVIRONMENT, clock=lambda: NOW
            ).receive(envelope)

    def readback(
        self, session: AsyncSession, *, clock: Any = None
    ) -> BillingReadbackService:
        return BillingReadbackService(
            session,
            client_factory=self.provider,
            environment=ENVIRONMENT,
            clock=clock or (lambda: NOW),
        )

    async def rows(self, model: type[Any], *where: Any) -> list[Any]:
        async with self.sessions() as session:
            return list((await session.execute(select(model).where(*where))).scalars())

    async def count(self, model: type[Any]) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(select(func.count()).select_from(model)) or 0
            )

    async def topup_session(
        self,
        *,
        state: str = "active",
        external_checkout_id: str | None = "chk_topup_000001",
        builder_id: UUID | None = None,
    ) -> tuple[BillingCheckoutSession, BillingTopupOrder]:
        owner = builder_id or self.builder_id
        snapshot = topup_pricing_snapshot(
            BILLING_CATALOG["package_credits_500_v1"], payment_method="pix"
        )
        order = BillingTopupOrder(
            id=uuid4(),
            builder_id=owner,
            provider="asaas",
            catalog_version_id="payment-2026-10-03",
            offer_id="package_credits_500_v1",
            payment_method="pix",
            amount_cents=10_000,
            credit_grant_mc=500,
            pricing_snapshot=snapshot,
            state="pending",
        )
        checkout = BillingCheckoutSession(
            id=uuid4(),
            builder_id=owner,
            intent_kind="topup",
            intent_id=order.id,
            migration_id=None,
            topup_order_id=order.id,
            attempt_no=1,
            provider="asaas",
            external_checkout_id=external_checkout_id,
            state=state,
            catalog_version_id="payment-2026-10-03",
            offer_id="package_credits_500_v1",
            amount_cents=10_000,
            pricing_snapshot=snapshot,
            reconciliation_attempts=0,
        )
        await self.add(order)
        await self.add(checkout)
        return checkout, order

    async def subscription_session(
        self,
        *,
        state: str = "active",
        external_checkout_id: str | None = "chk_sub_00000001",
        coupon: str | None = None,
        builder_id: UUID | None = None,
    ) -> BillingCheckoutSession:
        snapshot = subscription_pricing_snapshot(
            BILLING_CATALOG["plan_plus_monthly_v1"],
            active_clinics=1,
            coupon_code=coupon,
            coupon_discount_percent=10 if coupon else 0,
        )
        checkout = BillingCheckoutSession(
            id=uuid4(),
            builder_id=builder_id or self.builder_id,
            intent_kind="subscription",
            intent_id=uuid4(),
            migration_id=None,
            topup_order_id=None,
            attempt_no=1,
            provider="asaas",
            external_checkout_id=external_checkout_id,
            state=state,
            catalog_version_id="payment-2026-10-03",
            offer_id="plan_plus_monthly_v1",
            amount_cents=int(snapshot["amount_cents"]),  # type: ignore[call-overload]
            pricing_snapshot=snapshot,
            next_due_date=date(2026, 10, 11),
            reconciliation_attempts=0,
        )
        await self.add(checkout)
        return checkout


@pytest.fixture
async def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_World]:
    _patch_jsonb_for_sqlite()

    async def no_advisory_lock(self: BillingRepository, environment: str) -> None:
        del self, environment  # PostgreSQL-only primitive

    monkeypatch.setattr(BillingRepository, "_lock_environment", no_advisory_lock)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'billing.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    builder_id = uuid4()
    async with sessions() as session:
        session.add_all(
            [
                Builder(id=builder_id, name="Fixture Builder"),
                BillingRolloutControl(environment=ENVIRONMENT, state="disabled"),
                BillingRosterControl(singleton=True, billing_roster_revision=0),
            ]
        )
        await session.commit()
    try:
        yield _World(sessions=sessions, builder_id=builder_id, provider=_Provider())
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Closed allowlist and envelope.
# ---------------------------------------------------------------------------


def test_allowlist_is_the_design_set_plus_the_boleto_only_readback_event() -> None:
    """DESIGN's 26 names plus ``PAYMENT_REFUND_DENIED`` (official, boleto-only,
    readback-only): 27 subscribed events."""
    assert dict(CHECKOUT_EVENT_STATES) == {
        "CHECKOUT_CREATED": "active",
        "CHECKOUT_PAID": "paid_pending_subscription",
        "CHECKOUT_CANCELED": "terminal_pending_readback",
        "CHECKOUT_EXPIRED": "terminal_pending_readback",
    }
    assert dict(SUBSCRIPTION_EVENT_STATUSES) == {
        "SUBSCRIPTION_CREATED": "active_linked",
        "SUBSCRIPTION_UPDATED": "readback_required",
        "SUBSCRIPTION_INACTIVATED": "non_billable_pending_readback",
        "SUBSCRIPTION_DELETED": "non_billable_pending_readback",
    }
    assert PAYMENT_EVENT_STATUSES["PAYMENT_CONFIRMED"] == "settled"
    assert PAYMENT_EVENT_STATUSES["PAYMENT_RECEIVED"] == "settled"
    assert PAYMENT_EVENT_STATUSES["PAYMENT_RESTORED"] == "readback_required"
    assert PAYMENT_EVENT_STATUSES["PAYMENT_AWAITING_CHARGEBACK_REVERSAL"] == (
        "restoration_pending"
    )
    assert PAYMENT_EVENT_STATUSES["PAYMENT_REFUND_DENIED"] == "readback_required"
    assert len(PAYMENT_EVENT_STATUSES) == 19
    assert len(WEBHOOK_EVENTS) == 27
    assert not any("SPLIT" in name for name in WEBHOOK_EVENTS)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.pop("id"),
        lambda p: p.update(id=""),
        lambda p: p.update(id="evt_\u0001ctl&1"),
        lambda p: p.update(id="evt_" + "a" * 600),
        lambda p: p.update(id="evt_\ud800&1"),
        lambda p: p.update(id=123),
        lambda p: p.pop("event"),
        lambda p: p.update(event="payment_confirmed"),
        lambda p: p.pop("dateCreated"),
        lambda p: p.update(dateCreated="yesterday"),
    ],
    ids=[
        "no-id",
        "empty-id",
        "control-char-id",
        "overlong-id",
        "lone-surrogate-id",
        "numeric-id",
        "no-event",
        "lowercase-event",
        "no-date",
        "bad-date",
    ],
)
def test_event_without_valid_identity_is_rejected(mutation: Any) -> None:
    payload = _fixture("payment_event.json")
    mutation(payload)
    with pytest.raises(WebhookEnvelopeRejected):
        parse_envelope(_body(payload), payload)


def test_envelope_keeps_only_allowlisted_identifiers() -> None:
    envelope = _event("PAYMENT_CONFIRMED", event_id="evt_fixture_1")

    assert envelope.event_id == event_key("evt_fixture_1")
    assert envelope.family == "payment"
    assert envelope.payment is not None
    assert envelope.payment.payment_id == "pay_000000000000001"
    assert envelope.payment.value_cents == 99_900
    # São Paulo provider time (UTC-03:00) normalized to UTC.
    assert envelope.provider_created_at == datetime(
        2026, 10, 11, 13, 0, tzinfo=timezone.utc
    )
    rendered = repr(envelope)
    for private in (
        "cus_fixture_private",
        "tok_fixture_private",
        "4444",
        "acc_fixture",
    ):
        assert private not in rendered


def test_ordering_key_orders_time_then_lifecycle_rank() -> None:
    early = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
    late = early + timedelta(seconds=1)
    assert ordering_key(early, "reversed", "z") < ordering_key(late, "pending", "a")
    assert ordering_key(early, "pending", "z") < ordering_key(early, "settled", "a")


# ---------------------------------------------------------------------------
# Unknown, duplicate and uncorrelated deliveries.
# ---------------------------------------------------------------------------


async def test_unknown_event_persists_only_the_minimal_envelope(world: _World) -> None:
    before = _metric("neurostack_billing_alerts_total", kind="unknown_event")
    payload = _fixture("unknown_event.json")
    body = _body(payload)
    outcome = await world.receive(parse_envelope(body, payload))

    assert outcome.outcome == "unknown_event"
    assert outcome.readback is False
    (delivery,) = await world.rows(BillingWebhookDelivery)
    assert delivery.status == "unknown"
    assert delivery.event_name == "INVOICE_CREATED"
    assert delivery.resource_kind is None and delivery.resource_id is None
    assert delivery.builder_id is None
    assert delivery.body_bytes == len(body)
    assert len(delivery.body_sha256) == 64
    for model in (BillingPaymentProjection, BillingPaymentEffect, Transaction):
        assert await world.count(model) == 0
    assert (
        _metric("neurostack_billing_alerts_total", kind="unknown_event") == before + 1
    )


async def test_unknown_event_correlates_resource_only_when_already_internal(
    world: _World,
) -> None:
    checkout, _order = await world.topup_session()
    await world.receive(
        _event(
            "PAYMENT_CREATED",
            id="pay_000000000000001",
            subscription=None,
            checkoutSession=checkout.external_checkout_id,
            value=Decimal("100.00"),
        )
    )
    payload = _fixture("unknown_event.json")
    outcome = await world.receive(parse_envelope(_body(payload), payload))

    assert outcome.readback is True
    delivery = (
        await world.rows(
            BillingWebhookDelivery,
            BillingWebhookDelivery.event_name == "INVOICE_CREATED",
        )
    )[0]
    assert (delivery.resource_kind, delivery.resource_id) == (
        "payment",
        "pay_000000000000001",
    )
    assert delivery.builder_id == world.builder_id


async def test_duplicate_delivery_is_a_noop(world: _World) -> None:
    checkout, _order = await world.topup_session()
    envelope = _event(
        "PAYMENT_CONFIRMED",
        event_id="evt_dup_1",
        id="pay_dup_1",
        subscription=None,
        checkoutSession=checkout.external_checkout_id,
        value=Decimal("100.00"),
    )
    first = await world.receive(envelope)
    second = await world.receive(envelope)

    assert (first.outcome, second.outcome) == ("processed", "duplicate")
    assert await world.count(BillingWebhookDelivery) == 1
    assert await world.count(BillingPaymentEffect) == 1
    assert await world.count(Transaction) == 1


async def test_known_event_without_exact_entity_blocks_and_alerts(
    world: _World,
) -> None:
    await world.subscription_session()
    before = _metric("neurostack_billing_alerts_total", kind="uncorrelated_entity")
    outcome = await world.receive(
        _event(
            "PAYMENT_CONFIRMED",
            id="pay_orphan_1",
            subscription="sub_unknown",
            checkoutSession="chk_unknown",
        )
    )

    assert outcome.outcome == "uncorrelated"
    (delivery,) = await world.rows(BillingWebhookDelivery)
    assert delivery.status == "readback_required"
    assert delivery.resource_kind is None and delivery.builder_id is None
    assert await world.count(BillingPaymentProjection) == 0
    assert await world.count(BillingPaymentEffect) == 0
    assert (
        _metric("neurostack_billing_alerts_total", kind="uncorrelated_entity")
        == before + 1
    )


async def test_payload_can_never_choose_or_mix_builders(world: _World) -> None:
    other_builder = uuid4()
    await world.add(Builder(id=other_builder, name="Other Builder"))
    checkout, _order = await world.topup_session(external_checkout_id="chk_mine_0001")
    foreign = await world.subscription_session(
        builder_id=other_builder, external_checkout_id="chk_foreign_01"
    )
    await world.receive(
        _event(
            "SUBSCRIPTION_CREATED",
            id="sub_foreign_01",
            externalReference=str(foreign.id),
            value=Decimal("999.00"),
        )
    )
    # checkoutSession resolves to builder A while subscription is builder B.
    outcome = await world.receive(
        _event(
            "PAYMENT_CONFIRMED",
            id="pay_mixed_1",
            subscription="sub_foreign_01",
            checkoutSession=checkout.external_checkout_id,
        )
    )

    assert outcome.outcome == "uncorrelated"
    assert await world.count(BillingPaymentEffect) == 0
    assert await world.count(Transaction) == 0


async def test_conflicting_checkout_identifiers_are_never_resolved_by_preference(
    world: _World,
) -> None:
    first, _ = await world.topup_session(external_checkout_id="chk_first_0001")
    second, _ = await world.topup_session(external_checkout_id="chk_second_001")
    outcome = await world.receive(
        _event(
            "CHECKOUT_PAID",
            id=first.external_checkout_id,
            externalReference=str(second.id),
        )
    )

    assert outcome.outcome == "uncorrelated"
    (stored_first,) = await world.rows(
        BillingCheckoutSession, BillingCheckoutSession.id == first.id
    )
    assert stored_first.state == "active"


# ---------------------------------------------------------------------------
# Top-up settlement, ordering and idempotency.
# ---------------------------------------------------------------------------


async def test_topup_settlement_uses_snapshot_and_runs_once(world: _World) -> None:
    checkout, order = await world.topup_session()
    common = {
        "id": "pay_topup_1",
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
        "value": Decimal("100.00"),
        "billingType": "PIX",
    }
    await world.receive(_event("CHECKOUT_PAID", id=checkout.external_checkout_id))
    await world.receive(_event("PAYMENT_CREATED", when="2026-10-11 10:00:00", **common))
    confirmed = await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 10:00:05", **common)
    )
    received = await world.receive(
        _event("PAYMENT_RECEIVED", when="2026-10-11 10:00:09", **common)
    )

    assert (confirmed.outcome, received.outcome) == ("processed", "processed")
    (session_row,) = await world.rows(
        BillingCheckoutSession, BillingCheckoutSession.id == checkout.id
    )
    assert session_row.state == "completed"
    (projection,) = await world.rows(BillingPaymentProjection)
    assert (projection.status, projection.settled_cents) == ("settled", 10_000)
    assert projection.aggregate_kind == "topup"
    assert projection.topup_order_id == order.id
    (effect,) = await world.rows(BillingPaymentEffect)
    assert (effect.effect_kind, effect.source_revision, effect.amount_cents) == (
        "settlement",
        "settlement:v1",
        10_000,
    )
    (outbox,) = await world.rows(BillingOutboxEvent)
    assert (outbox.event_type, outbox.payment_effect_id) == (
        "topup.payment_settled.v1",
        effect.id,
    )
    (transaction,) = await world.rows(Transaction)
    assert transaction.provider == "asaas"
    assert transaction.provider_payment_id == "pay_topup_1"
    assert transaction.billing_payment_effect_id == effect.id
    assert (transaction.amount_cents, transaction.plan, transaction.method) == (
        10_000,
        "topup",
        "pix",
    )
    (stored_order,) = await world.rows(BillingTopupOrder)
    assert (stored_order.state, stored_order.settled_cents) == ("settled", 10_000)
    (builder,) = await world.rows(Builder, Builder.id == world.builder_id)
    assert builder.subscription_status == "trial"  # top-up never changes the plan


async def test_old_event_never_regresses_the_projection(world: _World) -> None:
    checkout, _order = await world.topup_session()
    common = {
        "id": "pay_order_1",
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
        "value": Decimal("100.00"),
    }
    await world.receive(
        _event("PAYMENT_RECEIVED", when="2026-10-11 10:00:09", **common)
    )
    await world.receive(_event("PAYMENT_CREATED", when="2026-10-11 10:00:00", **common))
    await world.receive(_event("PAYMENT_OVERDUE", when="2026-10-11 10:00:01", **common))
    await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 10:00:05", **common)
    )

    (projection,) = await world.rows(BillingPaymentProjection)
    assert projection.status == "settled"
    assert await world.count(BillingPaymentEffect) == 1
    assert await world.count(Transaction) == 1


async def test_provider_amount_drift_blocks_settlement(world: _World) -> None:
    checkout, _order = await world.topup_session()
    before = _metric("neurostack_billing_alerts_total", kind="drift")
    outcome = await world.receive(
        _event(
            "PAYMENT_CONFIRMED",
            id="pay_drift_1",
            subscription=None,
            checkoutSession=checkout.external_checkout_id,
            value=Decimal("1.00"),
        )
    )

    assert outcome.outcome == "readback_required"
    assert await world.count(BillingPaymentEffect) == 0
    assert await world.count(Transaction) == 0
    assert _metric("neurostack_billing_alerts_total", kind="drift") == before + 1


# ---------------------------------------------------------------------------
# Subscription handoff, settlement, coupon.
# ---------------------------------------------------------------------------


async def test_subscription_created_hands_off_and_settlement_activates_once(
    world: _World,
) -> None:
    await world.add(Coupon(code="WELCOME10", discount_percent=10, uses_count=0))
    checkout = await world.subscription_session(coupon="WELCOME10")
    amount = checkout.amount_cents
    value = Decimal(amount).scaleb(-2)
    created = await world.receive(
        _event(
            "SUBSCRIPTION_CREATED",
            id="sub_handoff_01",
            externalReference=str(checkout.id),
            value=value,
        )
    )
    assert created.outcome == "processed"
    (row,) = await world.rows(BillingSubscription)
    assert row.id == checkout.intent_id
    assert (row.status, row.is_active, row.plan) == ("active_linked", True, "plus")
    assert row.coupon_code == "WELCOME10"
    assert row.amount_cents == amount
    assert row.roster_revision == 1
    (session_row,) = await world.rows(BillingCheckoutSession)
    assert session_row.state == "completed"
    assert session_row.resulting_external_subscription_id == "sub_handoff_01"

    common = {
        "id": "pay_cycle_1",
        "subscription": "sub_handoff_01",
        "checkoutSession": checkout.external_checkout_id,
        "value": value,
    }
    await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 10:00:05", **common)
    )
    await world.receive(
        _event("PAYMENT_RECEIVED", when="2026-10-11 10:00:06", **common)
    )
    # Next cycle: a new payment of the same subscription.
    await world.receive(
        _event(
            "PAYMENT_CONFIRMED",
            when="2026-11-11 10:00:05",
            id="pay_cycle_2",
            subscription="sub_handoff_01",
            checkoutSession=None,
            value=value,
        )
    )

    (row,) = await world.rows(BillingSubscription)
    assert row.status == "active"
    effects = await world.rows(BillingPaymentEffect)
    assert sorted(e.payment_id for e in effects) == ["pay_cycle_1", "pay_cycle_2"]
    assert {e.source_revision for e in effects} == {"settlement:v1"}
    transactions = await world.rows(Transaction)
    assert len(transactions) == 2
    assert {t.plan for t in transactions} == {"plus"}
    assert sorted(t.coupon_code or "" for t in transactions) == ["", "WELCOME10"]
    (coupon,) = await world.rows(Coupon)
    assert coupon.uses_count == 1  # exactly once per checkout session
    (builder,) = await world.rows(Builder, Builder.id == world.builder_id)
    assert (builder.subscription_plan, builder.subscription_status) == (
        "plus",
        "active",
    )
    assert builder.billing_cycle == "monthly"


async def test_payment_pairing_checkout_and_subscription_links_exactly(
    world: _World,
) -> None:
    checkout = await world.subscription_session()
    outcome = await world.receive(
        _event(
            "PAYMENT_CONFIRMED",
            id="pay_link_1",
            subscription="sub_linked_by_payment",
            checkoutSession=checkout.external_checkout_id,
        )
    )

    assert outcome.outcome == "processed"
    (row,) = await world.rows(BillingSubscription)
    assert (row.id, row.external_subscription_id, row.status) == (
        checkout.intent_id,
        "sub_linked_by_payment",
        "active",
    )
    assert await world.count(BillingPaymentEffect) == 1


async def test_subscription_created_with_drift_links_but_requires_readback(
    world: _World,
) -> None:
    checkout = await world.subscription_session()
    outcome = await world.receive(
        _event(
            "SUBSCRIPTION_CREATED",
            id="sub_drift_01",
            externalReference=str(checkout.id),
            value=Decimal("1.00"),
        )
    )

    assert outcome.outcome == "readback_required"
    (row,) = await world.rows(BillingSubscription)
    assert (row.status, row.is_active) == ("readback_required", True)


async def test_second_recurring_subscription_is_never_bound(world: _World) -> None:
    await world.add(
        BillingSubscription(
            id=uuid4(),
            builder_id=world.builder_id,
            provider="abacatepay",
            external_subscription_id="sub_legacy_live",
            is_active=True,
            plan="start",
            billing_cycle="monthly",
            status="active",
            roster_revision=1,
        )
    )
    checkout = await world.subscription_session()
    outcome = await world.receive(
        _event(
            "SUBSCRIPTION_CREATED",
            id="sub_second_01",
            externalReference=str(checkout.id),
            value=Decimal("999.00"),
        )
    )

    assert outcome.outcome == "uncorrelated"
    assert [
        r.external_subscription_id for r in await world.rows(BillingSubscription)
    ] == ["sub_legacy_live"]


# ---------------------------------------------------------------------------
# Subscription lifecycle and M-D (terminal only after a converged read).
# ---------------------------------------------------------------------------


async def _linked_subscription(world: _World) -> BillingSubscription:
    checkout = await world.subscription_session()
    await world.receive(
        _event(
            "SUBSCRIPTION_CREATED",
            id="sub_life_0001",
            externalReference=str(checkout.id),
            value=Decimal("999.00"),
        )
    )
    (row,) = await world.rows(BillingSubscription)
    return row


def _remote_subscription(**overrides: Any) -> SubscriptionReadback:
    values: dict[str, Any] = {
        "subscription_id": "sub_life_0001",
        "status": "ACTIVE",
        "deleted": False,
        "cycle": "MONTHLY",
        "value_cents": 99_900,
        "next_due_date": date(2026, 11, 11),
        "external_reference": None,
    }
    values.update(overrides)
    return SubscriptionReadback(**values)


async def test_terminal_subscription_releases_retry_only_after_converged_read(
    world: _World,
) -> None:
    row = await _linked_subscription(world)
    outcome = await world.receive(_event("SUBSCRIPTION_DELETED", id="sub_life_0001"))
    assert outcome.outcome == "readback_required"
    (stored,) = await world.rows(BillingSubscription)
    assert (stored.status, stored.is_active) == ("non_billable_pending_readback", False)

    async def blocker() -> str | None:
        async with world.sessions() as session:
            return await BillingRepository(session).subscription_checkout_blocker(
                builder_id=world.builder_id
            )

    assert await blocker() == "asaas_subscription"
    world.provider.subscriptions["sub_life_0001"] = _remote_subscription(deleted=True)
    world.provider.subscription_charges["sub_life_0001"] = (
        _payment_read("pay_pending", status="PENDING", value_cents=99_900),
    )
    async with world.sessions() as session:
        assert await world.readback(session).readback_subscription(row.id) == "pending"
    assert await blocker() == "asaas_subscription"

    world.provider.subscription_charges["sub_life_0001"] = (
        _payment_read("pay_done", status="RECEIVED", value_cents=99_900),
    )
    async with world.sessions() as session:
        assert (
            await world.readback(session).readback_subscription(row.id) == "converged"
        )
    (stored,) = await world.rows(BillingSubscription)
    assert (stored.status, stored.is_active) == ("cancelled", False)
    # The subscription no longer blocks; the completed session is not live.
    assert await blocker() is None


async def test_failed_readback_keeps_the_subscription_blocking(world: _World) -> None:
    row = await _linked_subscription(world)
    await world.receive(_event("SUBSCRIPTION_INACTIVATED", id="sub_life_0001"))
    world.provider.fail = True
    before = _metric("neurostack_billing_alerts_total", kind="readback_failure")
    async with world.sessions() as session:
        assert await world.readback(session).readback_subscription(row.id) == "failed"
    (stored,) = await world.rows(BillingSubscription)
    assert stored.status == "non_billable_pending_readback"
    assert _metric("neurostack_billing_alerts_total", kind="readback_failure") == (
        before + 1
    )


async def test_updated_subscription_converges_or_flags_drift(world: _World) -> None:
    row = await _linked_subscription(world)
    await world.receive(_event("SUBSCRIPTION_UPDATED", id="sub_life_0001"))
    (stored,) = await world.rows(BillingSubscription)
    assert stored.status == "readback_required"

    world.provider.subscriptions["sub_life_0001"] = _remote_subscription(
        value_cents=50_000
    )
    world.provider.subscription_charges["sub_life_0001"] = ()
    async with world.sessions() as session:
        assert await world.readback(session).readback_subscription(row.id) == "drift"
    (stored,) = await world.rows(BillingSubscription)
    assert stored.status == "readback_required"

    world.provider.subscriptions["sub_life_0001"] = _remote_subscription()
    async with world.sessions() as session:
        assert (
            await world.readback(session).readback_subscription(row.id) == "converged"
        )
    (stored,) = await world.rows(BillingSubscription)
    assert stored.status == "active_linked"


async def test_old_update_never_reactivates_an_ended_subscription(
    world: _World,
) -> None:
    await _linked_subscription(world)
    await world.receive(
        _event("SUBSCRIPTION_DELETED", id="sub_life_0001", when="2026-10-11 12:00:00")
    )
    await world.receive(
        _event("SUBSCRIPTION_UPDATED", id="sub_life_0001", when="2026-10-11 11:00:00")
    )
    (stored,) = await world.rows(BillingSubscription)
    assert (stored.status, stored.is_active) == ("non_billable_pending_readback", False)


# ---------------------------------------------------------------------------
# Reversal and restoration effects.
# ---------------------------------------------------------------------------


async def _settled_topup(
    world: _World, payment_id: str = "pay_rev_1"
) -> dict[str, Any]:
    checkout, _order = await world.topup_session()
    common: dict[str, Any] = {
        "id": payment_id,
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
        "value": Decimal("100.00"),
    }
    await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 10:00:00", **common)
    )
    return common


async def test_full_refund_records_one_cumulative_reversal(world: _World) -> None:
    common = await _settled_topup(world)
    await world.receive(
        _event("PAYMENT_REFUNDED", when="2026-10-11 11:00:00", **common)
    )
    await world.receive(
        _event("PAYMENT_REFUNDED", when="2026-10-11 11:00:01", **common)
    )

    reversals = await world.rows(
        BillingPaymentEffect, BillingPaymentEffect.effect_kind == "reversal"
    )
    assert [(r.source_revision, r.amount_cents) for r in reversals] == [
        ("reversed_cents:10000", 10_000)
    ]
    (projection,) = await world.rows(BillingPaymentProjection)
    assert (projection.status, projection.reversed_cents) == ("reversed", 10_000)
    (order,) = await world.rows(BillingTopupOrder)
    assert (order.state, order.reversed_cents) == ("reversed", 10_000)
    assert await world.count(Transaction) == 1  # one Transaction per payment


async def test_partial_refund_is_priced_only_by_exact_readback(world: _World) -> None:
    common = await _settled_topup(world)
    partial = await world.receive(
        _event("PAYMENT_PARTIALLY_REFUNDED", when="2026-10-11 11:00:00", **common)
    )
    assert partial.outcome == "readback_required"
    assert await world.count(BillingPaymentEffect) == 1  # settlement only

    payload = _fixture("payment_readback_partial.json")
    from src.services.asaas import _payment_readback

    payload["id"] = "pay_rev_1"
    world.provider.payments["pay_rev_1"] = _payment_readback(payload)
    async with world.sessions() as session:
        outcome = await world.readback(session).process_delivery(partial.delivery_id)
    assert outcome == "converged"
    await world.receive(
        _event("PAYMENT_REFUNDED", when="2026-10-11 12:00:00", **common)
    )

    reversals = await world.rows(
        BillingPaymentEffect, BillingPaymentEffect.effect_kind == "reversal"
    )
    assert sorted((r.source_revision, r.amount_cents) for r in reversals) == [
        ("reversed_cents:10000", 7_000),
        ("reversed_cents:3000", 3_000),
    ]
    (delivery,) = await world.rows(
        BillingWebhookDelivery, BillingWebhookDelivery.id == partial.delivery_id
    )
    assert delivery.status == "processed"


async def test_reversal_before_settlement_waits_for_readback(world: _World) -> None:
    checkout, _order = await world.topup_session()
    outcome = await world.receive(
        _event(
            "PAYMENT_REFUNDED",
            id="pay_early_refund",
            subscription=None,
            checkoutSession=checkout.external_checkout_id,
            value=Decimal("100.00"),
        )
    )
    assert outcome.outcome == "readback_required"
    assert await world.count(BillingPaymentEffect) == 0

    world.provider.payments["pay_early_refund"] = _payment_read(
        "pay_early_refund",
        status="REFUNDED",
        value_cents=10_000,
        refunded_cents=10_000,
        checkout_id=checkout.external_checkout_id,
    )
    async with world.sessions() as session:
        assert (
            await world.readback(session).process_delivery(outcome.delivery_id)
            == "converged"
        )
    effects = sorted(
        (e.effect_kind, e.source_revision)
        for e in await world.rows(BillingPaymentEffect)
    )
    assert effects == [
        ("reversal", "reversed_cents:10000"),
        ("settlement", "settlement:v1"),
    ]


async def test_restoration_epoch_increments_only_on_readback_proof(
    world: _World,
) -> None:
    checkout, _order = await world.topup_session()
    common = {
        "id": "pay_restore_1",
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
        "value": Decimal("100.00"),
    }
    await world.receive(_event("PAYMENT_CREATED", when="2026-10-11 10:00:00", **common))
    await world.receive(_event("PAYMENT_DELETED", when="2026-10-11 10:01:00", **common))
    restored = await world.receive(
        _event("PAYMENT_RESTORED", when="2026-10-11 10:02:00", **common)
    )
    assert restored.outcome == "readback_required"
    assert await world.count(BillingPaymentEffect) == 0

    world.provider.payments["pay_restore_1"] = _payment_read(
        "pay_restore_1", status="PENDING", value_cents=10_000
    )
    async with world.sessions() as session:
        await world.readback(session).process_delivery(restored.delivery_id)
    async with world.sessions() as session:
        # A second read of the same live state proves nothing new.
        await world.readback(session).readback_payment(
            "pay_restore_1", delivery_id=restored.delivery_id
        )

    (projection,) = await world.rows(BillingPaymentProjection)
    assert (projection.status, projection.restoration_epoch) == ("pending", 1)
    (effect,) = await world.rows(BillingPaymentEffect)
    assert (effect.effect_kind, effect.source_revision, effect.amount_cents) == (
        "restoration",
        "restoration_epoch:1",
        0,
    )


# ---------------------------------------------------------------------------
# Checkout convergence and M-C.
# ---------------------------------------------------------------------------


async def test_canceled_checkout_frees_only_after_converged_empty_read(
    world: _World,
) -> None:
    checkout = await world.subscription_session()
    outcome = await world.receive(
        _event("CHECKOUT_CANCELED", id=checkout.external_checkout_id)
    )
    assert outcome.outcome == "readback_required"
    (row,) = await world.rows(BillingCheckoutSession)
    assert row.state == "terminal_pending_readback"

    world.provider.by_checkout_payments[str(checkout.external_checkout_id)] = (
        ProviderRecord("pay_live", None, "PENDING", False),
    )
    async with world.sessions() as session:
        assert await world.readback(session).process_delivery(outcome.delivery_id) == (
            "pending"
        )
    (row,) = await world.rows(BillingCheckoutSession)
    assert row.state == "terminal_pending_readback"

    world.provider.by_checkout_payments[str(checkout.external_checkout_id)] = ()
    async with world.sessions() as session:
        assert await world.readback(session).process_delivery(outcome.delivery_id) == (
            "converged"
        )
    (row,) = await world.rows(BillingCheckoutSession)
    assert row.state == "cancelled"


async def test_canceled_checkout_with_subscription_links_instead_of_freeing(
    world: _World,
) -> None:
    checkout = await world.subscription_session()
    outcome = await world.receive(
        _event("CHECKOUT_EXPIRED", id=checkout.external_checkout_id)
    )
    world.provider.by_reference_subscriptions[checkout.id] = (
        ProviderRecord("sub_found_0001", str(checkout.id), "ACTIVE", False),
    )
    async with world.sessions() as session:
        assert await world.readback(session).process_delivery(outcome.delivery_id) == (
            "converged"
        )
    (row,) = await world.rows(BillingCheckoutSession)
    assert (row.state, row.resulting_external_subscription_id) == (
        "completed",
        "sub_found_0001",
    )


async def _reconcile(world: _World, checkout_id: UUID, now: datetime) -> str:
    async with world.sessions() as session:
        view = await BillingCheckoutService(
            session, client_factory=world.provider, clock=lambda: now
        ).reconcile_checkout(
            builder_id=world.builder_id, checkout_session_id=checkout_id
        )
    return view.state


async def _created_at(world: _World, checkout_id: UUID) -> datetime:
    (row,) = await world.rows(
        BillingCheckoutSession, BillingCheckoutSession.id == checkout_id
    )
    created = row.created_at
    return created if created.tzinfo else created.replace(tzinfo=timezone.utc)


async def test_reconciliation_abandons_only_after_two_spaced_empty_reads(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checkout_module.settings, "asaas_auto_abandon_enabled", True)
    checkout = await world.subscription_session(
        state="reconciliation_required", external_checkout_id=None
    )
    created = await _created_at(world, checkout.id)
    before_limit = created + timedelta(minutes=10)
    after_limit = created + CHECKOUT_CONVERGENCE_LIMIT + timedelta(minutes=1)

    assert (
        await _reconcile(world, checkout.id, before_limit) == "reconciliation_required"
    )
    assert (
        await _reconcile(world, checkout.id, after_limit) == "reconciliation_required"
    )
    # Too soon after the first post-limit read: does not count.
    assert (
        await _reconcile(world, checkout.id, after_limit + timedelta(minutes=1))
        == "reconciliation_required"
    )
    assert (
        await _reconcile(
            world,
            checkout.id,
            after_limit + CONVERGENCE_READ_SPACING + timedelta(seconds=1),
        )
        == "abandoned"
    )
    (row,) = await world.rows(BillingCheckoutSession)
    assert row.reconciliation_attempts == 3


async def test_reconciliation_with_provider_records_never_abandons(
    world: _World,
) -> None:
    checkout = await world.subscription_session(
        state="reconciliation_required", external_checkout_id=None
    )
    world.provider.by_reference_payments[checkout.id] = (
        ProviderRecord("pay_seen", str(checkout.id), "PENDING", False),
    )
    created = await _created_at(world, checkout.id)
    later = created + CHECKOUT_CONVERGENCE_LIMIT + timedelta(hours=1)
    assert await _reconcile(world, checkout.id, later) == "terminal_pending_readback"


async def test_orphaned_creating_attempt_converges_safely(world: _World) -> None:
    checkout = await world.subscription_session(
        state="creating", external_checkout_id=None
    )
    created = await _created_at(world, checkout.id)

    assert (
        await _reconcile(world, checkout.id, created + timedelta(minutes=1))
        == "creating"
    )
    assert world.provider.calls == []  # an in-flight request is never read over
    assert (
        await _reconcile(world, checkout.id, created + timedelta(minutes=6))
        == "reconciliation_required"
    )


async def test_backlog_sweep_converges_ambiguous_attempts_without_the_builder(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checkout_module.settings, "asaas_auto_abandon_enabled", True)
    checkout = await world.subscription_session(
        state="creating", external_checkout_id=None
    )
    created = await _created_at(world, checkout.id)
    start = created + CHECKOUT_CONVERGENCE_LIMIT + timedelta(minutes=1)
    for offset in (timedelta(0), CONVERGENCE_READ_SPACING + timedelta(seconds=1)):
        async with world.sessions() as session:
            await world.readback(
                session, clock=lambda: start + offset
            ).reconcile_backlog()
    (row,) = await world.rows(BillingCheckoutSession)
    assert row.state == "abandoned"


# ---------------------------------------------------------------------------
# Provider webhook provisioning, drift and the checkout readiness gate.
# ---------------------------------------------------------------------------

_TOKEN = SecretStr("t" * 40)
_DESIRED = desired_webhook_settings(
    environment=ENVIRONMENT,
    url="https://api.example.test/v1/billing/webhook/asaas",
    email="ops@example.test",
)


def _config(**overrides: Any) -> WebhookConfig:
    values: dict[str, Any] = {
        "webhook_id": "0b7e9c3a-1111-4222-8333-944455556666",
        "name": _DESIRED.name,
        "url": _DESIRED.url,
        "enabled": True,
        "interrupted": False,
        "api_version": 3,
        "has_auth_token": True,
        "send_type": "SEQUENTIALLY",
        "events": _DESIRED.events,
        "penalized_requests_count": 0,
    }
    values.update(overrides)
    return WebhookConfig(**values)


@dataclass
class _WebhookApi:
    configs: list[WebhookConfig]
    created: list[dict[str, object]] = field(default_factory=list)
    updated: list[dict[str, object]] = field(default_factory=list)
    fail: bool = False

    async def list_webhooks(self) -> tuple[WebhookConfig, ...]:
        if self.fail:
            raise RuntimeError("down")
        return tuple(self.configs)

    async def get_webhook(self, webhook_id: str) -> WebhookConfig:
        return next(c for c in self.configs if c.webhook_id == webhook_id)

    async def create_webhook(
        self, desired: Any, *, auth_token: SecretStr
    ) -> WebhookConfig:
        self.created.append(desired.payload(auth_token=auth_token, create=True))
        self.configs.append(_config())
        return self.configs[-1]

    async def update_webhook(
        self, webhook_id: str, desired: Any, *, auth_token: SecretStr
    ) -> WebhookConfig:
        self.updated.append(desired.payload(auth_token=auth_token, create=False))
        self.configs = [_config()]
        return self.configs[0]


def test_desired_webhook_subscribes_exactly_the_allowlist() -> None:
    assert set(_DESIRED.events) == WEBHOOK_EVENTS
    assert _DESIRED.send_type == "SEQUENTIALLY" and _DESIRED.api_version == 3
    payload = _DESIRED.payload(auth_token=_TOKEN, create=True)
    assert payload["enabled"] is True and payload["interrupted"] is False
    assert "SecretStr" not in repr(_DESIRED) and "t" * 40 not in repr(_DESIRED)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"url": "https://evil.example.test/v1/billing/webhook/asaas"}, "url"),
        ({"name": "other"}, "name"),
        ({"api_version": 2}, "api_version"),
        ({"send_type": "NON_SEQUENTIALLY"}, "send_type"),
        ({"enabled": False}, "disabled"),
        ({"interrupted": True}, "interrupted"),
        ({"events": ("PAYMENT_CONFIRMED",)}, "events"),
        ({"has_auth_token": False}, "token_absent"),
    ],
)
async def test_readback_detects_each_drift(
    overrides: dict[str, Any], reason: str
) -> None:
    api = _WebhookApi([_config(**overrides)])
    verdict = await AsaasWebhookProvisioner(api, _DESIRED, auth_token=_TOKEN).readback()  # type: ignore[arg-type]
    assert verdict.ready is False
    assert reason in verdict.reasons
    assert compare_webhook(_config(), _DESIRED) == ()


async def test_readback_requires_local_token_and_unambiguous_config() -> None:
    api = _WebhookApi([_config()])
    assert (
        await AsaasWebhookProvisioner(api, _DESIRED, auth_token=SecretStr("")).readback()  # type: ignore[arg-type]
    ).reasons == ("token_unconfigured",)
    api.configs.append(_config(webhook_id="second-id"))
    assert (
        await AsaasWebhookProvisioner(api, _DESIRED, auth_token=_TOKEN).readback()  # type: ignore[arg-type]
    ).reasons == ("ambiguous",)
    api.fail = True
    assert (
        await AsaasWebhookProvisioner(api, _DESIRED, auth_token=_TOKEN).readback()  # type: ignore[arg-type]
    ).reasons == ("readback_failed",)


async def test_reconcile_creates_or_converges_then_reads_back() -> None:
    absent = _WebhookApi([])
    verdict = await AsaasWebhookProvisioner(absent, _DESIRED, auth_token=_TOKEN).reconcile()  # type: ignore[arg-type]
    assert verdict.ready is True
    assert absent.created[0]["apiVersion"] == 3 and absent.created[0]["email"]
    drifted = _WebhookApi([_config(enabled=False)])
    verdict = await AsaasWebhookProvisioner(drifted, _DESIRED, auth_token=_TOKEN).reconcile()  # type: ignore[arg-type]
    assert verdict.ready is True
    assert "apiVersion" not in drifted.updated[0]


async def test_token_smoke_requires_401_then_400_and_persists_nothing() -> None:
    seen: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("asaas-access-token", "")
        status = 400 if token == _TOKEN.get_secret_value() else 401
        seen.append((str(request.url), status))
        return httpx.Response(status)

    provisioner = AsaasWebhookProvisioner(_WebhookApi([]), _DESIRED, auth_token=_TOKEN)  # type: ignore[arg-type]
    assert await provisioner.smoke(httpx.MockTransport(handler)) is True
    assert [status for _url, status in seen] == [401, 400]
    assert (
        await provisioner.smoke(httpx.MockTransport(lambda r: httpx.Response(200)))
        is False
    )


async def test_readiness_gate_is_fail_closed_and_expires() -> None:
    clock = [100.0]
    gate = WebhookReadinessGate(
        soft_ttl=10, hard_ttl=20, retry_interval=5, monotonic=lambda: clock[0]
    )
    with pytest.raises(WebhookNotReady):
        gate.require_ready()
    gate.record(WebhookReadiness(True, ()))
    gate.require_ready()
    clock[0] += 25
    with pytest.raises(WebhookNotReady):
        gate.require_ready()
    gate.record(WebhookReadiness(False, ("url",)))
    with pytest.raises(WebhookNotReady):
        gate.require_ready()
    assert _metric("neurostack_billing_webhook_config_ready") == 0


async def test_readiness_gate_refreshes_in_background_without_blocking() -> None:
    calls: list[int] = []

    async def refresh() -> WebhookReadiness:
        calls.append(1)
        return WebhookReadiness(True, ())

    gate = WebhookReadinessGate()
    with pytest.raises(WebhookNotReady):
        gate.require_ready(refresh)  # this request still fails closed
    assert gate._task is not None
    await gate._task
    assert calls == [1]
    gate.require_ready(refresh)  # fresh: no second refresh scheduled
    assert calls == [1]


def test_checkout_service_reconcile_constants_follow_provider_expiry() -> None:
    from src.services.asaas import ASAAS_CHECKOUT_MINUTES_TO_EXPIRE

    assert CHECKOUT_CONVERGENCE_LIMIT > timedelta(
        minutes=ASAAS_CHECKOUT_MINUTES_TO_EXPIRE
    )
    assert checkout_module.ORPHANED_CREATING_AFTER >= timedelta(minutes=1)


async def test_restoration_to_paid_also_records_the_settlement(world: _World) -> None:
    checkout, _order = await world.topup_session()
    common = {
        "id": "pay_restore_paid",
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
        "value": Decimal("100.00"),
    }
    await world.receive(_event("PAYMENT_CREATED", when="2026-10-11 10:00:00", **common))
    await world.receive(_event("PAYMENT_DELETED", when="2026-10-11 10:01:00", **common))
    restored = await world.receive(
        _event("PAYMENT_RESTORED", when="2026-10-11 10:02:00", **common)
    )
    world.provider.payments["pay_restore_paid"] = _payment_read(
        "pay_restore_paid", status="RECEIVED", value_cents=10_000
    )
    async with world.sessions() as session:
        assert (
            await world.readback(session).process_delivery(restored.delivery_id)
            == "converged"
        )

    (projection,) = await world.rows(BillingPaymentProjection)
    assert (projection.status, projection.settled_cents) == ("restored", 10_000)
    effects = sorted(
        (e.effect_kind, e.source_revision, e.amount_cents)
        for e in await world.rows(BillingPaymentEffect)
    )
    assert effects == [
        ("restoration", "restoration_epoch:1", 0),
        ("settlement", "settlement:v1", 10_000),
    ]
    assert await world.count(Transaction) == 1


async def test_settled_money_never_restores_to_pending(world: _World) -> None:
    common = await _settled_topup(world, payment_id="pay_no_unsettle")
    refunded = await world.receive(
        _event("PAYMENT_REFUNDED", when="2026-10-11 11:00:00", **common)
    )
    world.provider.payments["pay_no_unsettle"] = _payment_read(
        "pay_no_unsettle", status="PENDING", value_cents=10_000
    )
    async with world.sessions() as session:
        outcome = await world.readback(session).readback_payment(
            "pay_no_unsettle", delivery_id=refunded.delivery_id
        )
    assert outcome == "drift"
    (projection,) = await world.rows(BillingPaymentProjection)
    assert (projection.status, projection.restoration_epoch) == ("reversed", 0)


async def test_late_older_status_never_overwrites_a_newer_one(world: _World) -> None:
    checkout, _order = await world.topup_session()
    common = {
        "id": "pay_status_order",
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
        "value": Decimal("100.00"),
    }
    await world.receive(_event("PAYMENT_OVERDUE", when="2026-10-11 10:00:05", **common))
    await world.receive(_event("PAYMENT_CREATED", when="2026-10-11 10:00:00", **common))
    (projection,) = await world.rows(BillingPaymentProjection)
    assert projection.status == "overdue"

    await world.receive(
        _event("PAYMENT_CONFIRMED", when="2026-10-11 10:00:06", **common)
    )
    await world.receive(
        _event("PAYMENT_CHARGEBACK_DISPUTE", when="2026-10-11 10:00:09", **common)
    )
    await world.receive(
        _event("PAYMENT_CHARGEBACK_REQUESTED", when="2026-10-11 10:00:08", **common)
    )
    (projection,) = await world.rows(BillingPaymentProjection)
    assert projection.status == "chargeback_dispute"


# ---------------------------------------------------------------------------
# C-1: the provider's documented event id shape (``evt_<hex>&<digits>``).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture", ["checkout_event.json", "subscription_event.json", "payment_event.json"]
)
def test_documented_event_ids_are_accepted_and_stored_as_a_derived_key(
    fixture: str,
) -> None:
    payload = _fixture(fixture)
    raw = payload["id"]
    assert "&" in raw and raw.startswith("evt_")
    envelope = parse_envelope(_body(payload), payload)
    assert envelope.event_id == event_key(raw)
    assert envelope.event_id.startswith("sha256:") and len(envelope.event_id) == 71
    assert set(envelope.event_id) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    )
    assert raw not in repr(envelope)


async def test_documented_id_redelivery_deduplicates_and_suffix_distinguishes(
    world: _World,
) -> None:
    checkout, _order = await world.topup_session()
    common = {
        "id": "pay_documented_1",
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
        "value": Decimal("100.00"),
    }
    raw = "evt_05b708f961d739ea7eba7e4db318f621&368604920"
    first = await world.receive(_event("PAYMENT_CREATED", event_id=raw, **common))
    again = await world.receive(_event("PAYMENT_CREATED", event_id=raw, **common))
    other = await world.receive(
        _event(
            "PAYMENT_CONFIRMED",
            event_id="evt_05b708f961d739ea7eba7e4db318f621&368604921",
            **common,
        )
    )

    assert (first.outcome, again.outcome, other.outcome) == (
        "processed",
        "duplicate",
        "processed",
    )
    keys = sorted(d.event_id for d in await world.rows(BillingWebhookDelivery))
    assert keys == sorted(
        [event_key(raw), event_key("evt_05b708f961d739ea7eba7e4db318f621&368604921")]
    )
    (projection,) = await world.rows(BillingPaymentProjection)
    assert projection.last_event_id.startswith("sha256:")
    assert raw not in projection.ordering_key


async def test_unicode_event_id_is_hashed_not_rejected(world: _World) -> None:
    payload = _fixture("unknown_event.json")
    payload["id"] = "evt_ação_€&1"
    outcome = await world.receive(parse_envelope(_body(payload), payload))
    assert outcome.outcome == "unknown_event"
    (delivery,) = await world.rows(BillingWebhookDelivery)
    assert delivery.event_id == event_key("evt_ação_€&1")


# ---------------------------------------------------------------------------
# MEDIUM 3: transient failures retry, deterministic ones never loop the queue.
# ---------------------------------------------------------------------------


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (IntegrityError("x", {}, Exception("uq")), ("integrity", True)),
        (DataError("x", {}, Exception("too long")), ("data_error", True)),
        (BillingCanaryBudgetExceeded("slot"), ("canary_budget_exceeded", True)),
        (BillingTransitionRejected("no"), ("transition_rejected", True)),
        (BillingCapabilityRejected("no"), ("capability_rejected", True)),
        (BillingStateUnavailable("no"), ("state_unavailable", True)),
        (OperationalError("x", {}, _PgError("40001")), ("serialization", False)),
        (OperationalError("x", {}, _PgError("40P01")), ("serialization", False)),
        (OperationalError("x", {}, _PgError("55P03")), ("lock_timeout", False)),
        (OperationalError("x", {}, _PgError("57014")), ("lock_timeout", False)),
        (OperationalError("x", {}, _PgError("08006")), ("database", False)),
        (TimeoutError(), ("timeout", False)),
        (ConnectionResetError(), ("connection", False)),
        (KeyError("bug"), ("unexpected", False)),
    ],
)
def test_failure_classification_is_closed_and_fail_safe(
    exc: BaseException, expected: tuple[str, bool]
) -> None:
    assert classify_failure(exc) == expected


async def _topup_confirmed(world: _World) -> WebhookEnvelope:
    checkout, _order = await world.topup_session()
    return _event(
        "PAYMENT_CONFIRMED",
        id="pay_failure_1",
        subscription=None,
        checkoutSession=checkout.external_checkout_id,
        value=Decimal("100.00"),
    )


async def test_deterministic_domain_rejection_is_quarantined_with_200(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = await _topup_confirmed(world)

    async def refuse(*_args: object, **_kwargs: object) -> str:
        raise BillingTransitionRejected("settled checkout transaction unavailable")

    monkeypatch.setattr(_FinancialState, "apply_payment", refuse)
    before = _metric(
        "neurostack_billing_webhook_failures_total",
        failure="transition_rejected",
        disposition="quarantined",
    )
    outcome = await world.receive(envelope)

    assert outcome.outcome == "quarantined"
    (delivery,) = await world.rows(BillingWebhookDelivery)
    assert delivery.status == "readback_required"
    assert await world.count(BillingPaymentProjection) == 0
    assert await world.count(BillingPaymentEffect) == 0
    assert (
        _metric(
            "neurostack_billing_webhook_failures_total",
            failure="transition_rejected",
            disposition="quarantined",
        )
        == before + 1
    )


async def test_missing_control_row_is_deterministic_not_a_retry_loop(
    world: _World,
) -> None:
    envelope = await _topup_confirmed(world)
    async with world.sessions() as session:
        await session.execute(
            BillingRolloutControl.__table__.delete()  # type: ignore[attr-defined]
        )
        await session.commit()
    outcome = await world.receive(envelope)
    assert outcome.outcome == "quarantined"
    assert await world.count(BillingPaymentEffect) == 0


async def test_transient_failure_retries_and_reserves_nothing(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = await _topup_confirmed(world)

    async def serialization(*_args: object, **_kwargs: object) -> str:
        raise OperationalError("x", {}, _PgError("40001"))

    monkeypatch.setattr(_FinancialState, "apply_payment", serialization)
    with pytest.raises(WebhookProcessingUnavailable) as exc_info:
        await world.receive(envelope)
    assert exc_info.value.failure_class == "serialization"
    assert await world.count(BillingWebhookDelivery) == 0


async def test_effect_backstop_hit_is_already_applied_not_drift(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = await _topup_confirmed(world)
    await world.receive(envelope)
    # Simulate a writer that lost the race: the effect exists, but this
    # transaction cannot see it before inserting.
    async with world.sessions() as session:
        projection = (
            await session.execute(select(BillingPaymentProjection))
        ).scalar_one()
        projection.settled_cents = 0
        projection.status = "pending"
        await session.commit()

    async def unseen(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(BillingRepository, "find_payment_effect", unseen)
    before_drift = _metric("neurostack_billing_alerts_total", kind="drift")
    before_quarantine = _metric("neurostack_billing_alerts_total", kind="quarantine")
    outcome = await world.receive(
        _event(
            "PAYMENT_RECEIVED",
            id="pay_failure_1",
            subscription=None,
            checkoutSession=envelope.payment.checkout_id,  # type: ignore[union-attr]
            value=Decimal("100.00"),
        )
    )
    assert outcome.outcome == "processed"
    assert await world.count(BillingPaymentEffect) == 1
    assert _metric("neurostack_billing_alerts_total", kind="drift") == before_drift
    assert (
        _metric("neurostack_billing_alerts_total", kind="quarantine")
        == before_quarantine
    )


async def test_settlement_without_an_exact_amount_waits_for_readback(
    world: _World,
) -> None:
    checkout, _order = await world.topup_session()
    common = {
        "id": "pay_no_value",
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
    }
    absent = await world.receive(_event("PAYMENT_CONFIRMED", value=None, **common))
    inexact = await world.receive(
        _event("PAYMENT_RECEIVED", value=Decimal("100.001"), **common)
    )
    assert (absent.outcome, inexact.outcome) == (
        "readback_required",
        "readback_required",
    )
    assert await world.count(BillingPaymentEffect) == 0

    world.provider.payments["pay_no_value"] = _payment_read(
        "pay_no_value",
        status="RECEIVED",
        value_cents=10_000,
        checkout_id=checkout.external_checkout_id,
    )
    async with world.sessions() as session:
        assert (
            await world.readback(session).process_delivery(absent.delivery_id)
            == "converged"
        )
    assert await world.count(BillingPaymentEffect) == 1
    assert await world.count(BillingOutboxEvent) == 1


# ---------------------------------------------------------------------------
# MEDIUM 2: automatic abandonment is dormant by default.
# MEDIUM 4: provider reads never run inside a transaction / under a row lock.
# ---------------------------------------------------------------------------


async def test_auto_abandon_is_dormant_by_default_and_keeps_blocking(
    world: _World,
) -> None:
    assert checkout_module.settings.asaas_auto_abandon_enabled is False
    checkout = await world.subscription_session(
        state="reconciliation_required", external_checkout_id=None
    )
    created = await _created_at(world, checkout.id)
    first = created + CHECKOUT_CONVERGENCE_LIMIT + timedelta(minutes=1)
    for step in range(4):
        state = await _reconcile(
            world,
            checkout.id,
            first + step * (CONVERGENCE_READ_SPACING + timedelta(seconds=1)),
        )
        assert state == "reconciliation_required"
    (row,) = await world.rows(BillingCheckoutSession)
    assert row.reconciliation_attempts == 4  # the exact-ID reads still ran
    async with world.sessions() as session:
        assert (
            await BillingRepository(session).subscription_checkout_blocker(
                builder_id=world.builder_id
            )
            == "live_subscription_checkout"
        )


async def test_dormant_setting_also_keeps_terminal_readback_from_abandoning(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = await world.subscription_session(state="terminal_pending_readback")
    created = await _created_at(world, checkout.id)
    late = created + CHECKOUT_CONVERGENCE_LIMIT + timedelta(hours=1)
    async with world.sessions() as session:
        outcome = await world.readback(session, clock=lambda: late).readback_checkout(
            checkout.id
        )
    assert outcome == "pending"
    monkeypatch.setattr(checkout_module.settings, "asaas_auto_abandon_enabled", True)
    async with world.sessions() as session:
        outcome = await world.readback(session, clock=lambda: late).readback_checkout(
            checkout.id
        )
    assert outcome == "converged"
    (row,) = await world.rows(BillingCheckoutSession)
    assert row.state == "abandoned"


async def test_reconcile_reads_the_provider_with_no_open_transaction(
    world: _World,
) -> None:
    checkout = await world.subscription_session(
        state="reconciliation_required", external_checkout_id=None
    )
    seen: list[bool] = []

    async with world.sessions() as session:

        class Client:
            async def list_subscriptions_by_external_reference(
                self, _reference: UUID
            ) -> tuple[ProviderRecord, ...]:
                seen.append(session.in_transaction())
                return ()

            async def list_payments_by_external_reference(
                self, _reference: UUID
            ) -> tuple[ProviderRecord, ...]:
                seen.append(session.in_transaction())
                return ()

        view = await BillingCheckoutService(
            session, client_factory=Client, clock=lambda: NOW
        ).reconcile_checkout(
            builder_id=world.builder_id, checkout_session_id=checkout.id
        )

    assert seen == [False, False]
    assert view.state == "reconciliation_required"


async def test_reconcile_is_a_noop_when_a_webhook_moved_the_session_meanwhile(
    world: _World,
) -> None:
    checkout = await world.subscription_session(
        state="reconciliation_required", external_checkout_id=None
    )

    async with world.sessions() as session:

        class Client:
            async def list_subscriptions_by_external_reference(
                self, _reference: UUID
            ) -> tuple[ProviderRecord, ...]:
                # A webhook links the checkout while the provider read runs.
                await world.receive(
                    _event(
                        "CHECKOUT_CREATED",
                        id="chk_linked_meanwhile",
                        externalReference=str(checkout.id),
                    )
                )
                return ()

            async def list_payments_by_external_reference(
                self, _reference: UUID
            ) -> tuple[ProviderRecord, ...]:
                return ()

        view = await BillingCheckoutService(
            session, client_factory=Client, clock=lambda: NOW
        ).reconcile_checkout(
            builder_id=world.builder_id, checkout_session_id=checkout.id
        )

    assert view.state == "active"
    (row,) = await world.rows(BillingCheckoutSession)
    assert (row.state, row.reconciliation_attempts) == ("active", 0)


# ---------------------------------------------------------------------------
# LOW: paginated readbacks stay under the 64 KiB response cap.
# ---------------------------------------------------------------------------


def _client(handler: Any) -> Any:
    from src.services.asaas import AsaasClient

    return AsaasClient(
        api_key=SecretStr("$aact_prod_fixture"),
        user_agent="NeuroStack/test",
        transport=httpx.MockTransport(handler),
    )


async def test_subscription_payments_are_read_page_by_page_under_the_cap() -> None:
    from src.services.asaas import ASAAS_MAX_RESPONSE_BYTES, ASAAS_PAGE_SIZE

    template = _fixture("payment_event.json")["payment"]
    offsets: list[str] = []
    bodies: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["subscription"] == "sub_paged_0001"
        assert request.url.params["limit"] == str(ASAAS_PAGE_SIZE)
        offset = int(request.url.params["offset"])
        offsets.append(str(offset))
        count = 3 if offset == 2 * ASAAS_PAGE_SIZE else ASAAS_PAGE_SIZE
        data = [
            {
                **template,
                "id": f"pay_{offset + i:05d}",
                "subscription": "sub_paged_0001",
            }
            for i in range(count)
        ]
        body = json.dumps(
            {"hasMore": offset < 2 * ASAAS_PAGE_SIZE, "data": data}, default=str
        ).encode()
        bodies.append(len(body))
        return httpx.Response(200, content=body)

    records = await _client(handler).list_payments_by_subscription("sub_paged_0001")
    assert len(records) == 2 * ASAAS_PAGE_SIZE + 3
    assert offsets == ["0", "10", "20"]
    assert max(bodies) < ASAAS_MAX_RESPONSE_BYTES // 2


async def test_unbounded_or_unfiltered_pages_prove_nothing() -> None:
    from src.services.asaas import AsaasOutcomeUnknown

    def endless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"hasMore": True, "data": [{"id": "pay_x", "subscription": "sub_e"}]},
        )

    with pytest.raises(AsaasOutcomeUnknown):
        await _client(endless).list_payments_by_subscription("sub_e")

    def foreign(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"hasMore": False, "data": [{"id": "pay_y", "subscription": "x"}]}
        )

    with pytest.raises(AsaasOutcomeUnknown):
        await _client(foreign).list_payments_by_subscription("sub_e")


# ---------------------------------------------------------------------------
# Fix loop 2, MEDIUM-A: a reversed state is decided by cumulative cents, so a
# later exact readback still restores after any event touched the row.
# ---------------------------------------------------------------------------


async def _refunded_topup(world: _World, payment_id: str) -> dict[str, Any]:
    common = await _settled_topup(world, payment_id=payment_id)
    await world.receive(
        _event("PAYMENT_REFUNDED", when="2026-10-11 11:00:00", **common)
    )
    (projection,) = await world.rows(BillingPaymentProjection)
    assert (projection.status, projection.reversed_cents) == ("reversed", 10_000)
    return common


async def _restorations(world: _World) -> list[tuple[str, int]]:
    return sorted(
        (e.source_revision, e.amount_cents)
        for e in await world.rows(
            BillingPaymentEffect, BillingPaymentEffect.effect_kind == "restoration"
        )
    )


async def test_absent_value_event_never_hides_a_later_restoration(
    world: _World,
) -> None:
    common = await _refunded_topup(world, "pay_probe_absent")
    absent = await world.receive(
        _event(
            "PAYMENT_RECEIVED", when="2026-10-11 11:30:00", **{**common, "value": None}
        )
    )
    assert absent.outcome == "readback_required"
    (projection,) = await world.rows(BillingPaymentProjection)
    assert projection.status == "reversed"  # never relabelled

    denied = await world.receive(
        _event("PAYMENT_REFUND_DENIED", when="2026-10-11 12:00:00", **common)
    )
    world.provider.payments["pay_probe_absent"] = _payment_read(
        "pay_probe_absent", status="RECEIVED", value_cents=10_000, refunded_cents=0
    )
    for _replay in range(2):
        async with world.sessions() as session:
            await world.readback(session).readback_payment(
                "pay_probe_absent", delivery_id=denied.delivery_id
            )
        # The first exact read restores; the replay never duplicates it.
        assert await _restorations(world) == [("restoration_epoch:1", 10_000)]
    (projection,) = await world.rows(BillingPaymentProjection)
    assert (
        projection.status,
        projection.restored_cents,
        projection.restoration_epoch,
    ) == (
        "restored",
        10_000,
        1,
    )


@pytest.mark.parametrize(
    "touch",
    ["PAYMENT_UPDATED", "PAYMENT_OVERDUE", "PAYMENT_CHARGEBACK_REQUESTED"],
)
async def test_status_events_on_a_reversed_row_never_hide_a_restoration(
    world: _World, touch: str
) -> None:
    payment_id = f"pay_touch_{touch.lower()}"
    common = await _refunded_topup(world, payment_id)
    touched = await world.receive(_event(touch, when="2026-10-11 11:30:00", **common))
    world.provider.payments[payment_id] = _payment_read(
        payment_id, status="RECEIVED", value_cents=10_000, refunded_cents=0
    )
    async with world.sessions() as session:
        await world.readback(session).readback_payment(
            payment_id, delivery_id=touched.delivery_id
        )
    assert await _restorations(world) == [("restoration_epoch:1", 10_000)]


async def test_rereading_an_unchanged_partial_refund_is_not_a_restoration(
    world: _World,
) -> None:
    common = await _settled_topup(world, payment_id="pay_partial_reread")
    partial = await world.receive(
        _event("PAYMENT_PARTIALLY_REFUNDED", when="2026-10-11 11:00:00", **common)
    )
    world.provider.payments["pay_partial_reread"] = _payment_read(
        "pay_partial_reread",
        status="RECEIVED",
        value_cents=10_000,
        refunded_cents=3_000,
    )
    for _replay in range(3):
        async with world.sessions() as session:
            await world.readback(session).readback_payment(
                "pay_partial_reread", delivery_id=partial.delivery_id
            )
    assert await _restorations(world) == []
    (projection,) = await world.rows(BillingPaymentProjection)
    assert (
        projection.status,
        projection.reversed_cents,
        projection.restoration_epoch,
    ) == (
        "partially_reversed",
        3_000,
        0,
    )


async def test_quarantine_alert_is_counted_once(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = await _topup_confirmed(world)

    async def refuse(*_args: object, **_kwargs: object) -> str:
        raise BillingTransitionRejected("refused")

    monkeypatch.setattr(_FinancialState, "apply_payment", refuse)
    before = _metric("neurostack_billing_alerts_total", kind="quarantine")
    assert (await world.receive(envelope)).outcome == "quarantined"
    assert _metric("neurostack_billing_alerts_total", kind="quarantine") == before + 1


# ---------------------------------------------------------------------------
# Fix loop 3 (MEDIUM-E): a deleted, never-settled payment read back live WITH
# a completed refund is settled and reversed, never "restored" by a negative
# delta (which the totals CHECK refuses on PostgreSQL).
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
            [
                ("reversal", "reversed_cents:3000", 3_000),
                ("settlement", "settlement:v1", 10_000),
            ],
            "partially_reversed",
        ),
        (
            "REFUNDED",
            10_000,
            [
                ("reversal", "reversed_cents:10000", 10_000),
                ("settlement", "settlement:v1", 10_000),
            ],
            "reversed",
        ),
    ],
)
async def test_deleted_payment_read_back_with_refunds_settles_then_reverses(
    world: _World, status: str, refunded: int, expected: list[Any], label: str
) -> None:
    checkout, _order = await world.topup_session()
    payment_id = f"pay_deleted_{status.lower()}"
    common = {
        "id": payment_id,
        "subscription": None,
        "checkoutSession": checkout.external_checkout_id,
        "value": Decimal("100.00"),
    }
    await world.receive(_event("PAYMENT_CREATED", when="2026-10-11 10:00:00", **common))
    await world.receive(_event("PAYMENT_DELETED", when="2026-10-11 10:01:00", **common))
    restored = await world.receive(
        _event("PAYMENT_RESTORED", when="2026-10-11 10:02:00", **common)
    )
    world.provider.payments[payment_id] = _payment_read(
        payment_id, status=status, value_cents=10_000, refunded_cents=refunded
    )
    for replay in range(2):
        async with world.sessions() as session:
            outcome = await world.readback(session).readback_payment(
                payment_id, delivery_id=restored.delivery_id
            )
        assert outcome == "converged"
        effects = sorted(
            (e.effect_kind, e.source_revision, e.amount_cents)
            for e in await world.rows(BillingPaymentEffect)
        )
        assert effects == expected  # first read applies; the replay is inert
        (projection,) = await world.rows(BillingPaymentProjection)
        # The migration's ck_billing_payment_projections_totals, in Python.
        assert (
            0
            <= projection.restored_cents
            <= projection.reversed_cents
            <= projection.settled_cents
            <= projection.amount_cents
        )
        assert projection.restoration_epoch == 0
        if replay == 0:
            # Label as at 417c8ca.  (A PENDING read carrying a DONE refund is
            # contradictory provider data; its replay relabels ``pending``
            # exactly like 417c8ca did — money and effects stay put.)
            assert projection.status == label
    assert await world.count(Transaction) == 1
