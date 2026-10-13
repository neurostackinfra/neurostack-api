"""Transactional Group 4 production, replay, and quarantine tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.billing import (
    BillingCheckoutSession,
    BillingFactAck,
    BillingFactDelivery,
    BillingFactHandoff,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingPaymentProjection,
    BillingSubscription,
    BillingSubscriptionCycle,
    BillingTopupOrder,
    BillingWebhookDelivery,
)
from src.models.builder import Builder
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services.billing_catalog import BILLING_CATALOG
from src.services.billing_checkout import (
    subscription_pricing_snapshot,
    topup_pricing_snapshot,
)
from src.services.billing_outbox import (
    ACK_SCHEMA_VERSION,
    BillingFactProducer,
    BillingFactPublisher,
    BillingFactReconciler,
    billing_fact_bundle_digest,
)


@pytest.fixture
async def session_factory(
    tmp_path: Path,
) -> async_sessionmaker[AsyncSession]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _settled_topup(
    session: AsyncSession,
    *,
    ordered: bool = False,
) -> tuple[BillingPaymentEffect, BillingPaymentProjection, BillingTopupOrder]:
    builder_id = uuid4()
    order_id = uuid4()
    projection_id = uuid4()
    delivery_id = uuid4()
    now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    snapshot = topup_pricing_snapshot(
        BILLING_CATALOG["package_credits_500_v1"], payment_method="pix"
    )
    order = BillingTopupOrder(
        id=order_id,
        builder_id=builder_id,
        catalog_version_id="payment-2026-10-03",
        offer_id="package_credits_500_v1",
        payment_method="pix",
        amount_cents=10_000,
        credit_grant_mc=500,
        pricing_snapshot=snapshot,
    )
    delivery = BillingWebhookDelivery(
        id=delivery_id,
        builder_id=builder_id,
        provider="asaas",
        event_id=f"event-{delivery_id}",
        event_name="PAYMENT_CONFIRMED",
        provider_created_at=now,
        body_bytes=2,
        body_sha256="a" * 64,
        status="processed",
    )
    projection = BillingPaymentProjection(
        id=projection_id,
        builder_id=builder_id,
        provider="asaas",
        payment_id="pay_outbox_fixture",
        aggregate_kind="topup",
        subscription_id=None,
        topup_order_id=order_id,
        checkout_session_id=None,
        status="settled",
        amount_cents=10_000,
        settled_cents=10_000,
        reversed_cents=0,
        restored_cents=0,
        restoration_epoch=0,
        provider_created_at=now,
        ordering_key="00000000000000001.00:fixture",
        last_event_id="fixture",
    )
    effect = BillingPaymentEffect(
        id=uuid4(),
        builder_id=builder_id,
        projection_id=projection_id,
        delivery_id=delivery_id,
        provider="asaas",
        payment_id="pay_outbox_fixture",
        effect_kind="settlement",
        source_revision="settlement:v1",
        amount_cents=10_000,
    )
    rows = (
        Builder(id=builder_id, name="Outbox Fixture"),
        order,
        delivery,
        projection,
        effect,
    )
    if ordered:
        # Parents first, so the fixture itself satisfies enforced foreign keys.
        for row in rows:
            session.add(row)
            await session.flush()
    else:
        session.add_all(rows)
        await session.flush()
    return effect, projection, order


async def _settled_subscription(
    session: AsyncSession,
) -> tuple[BillingPaymentEffect, BillingPaymentProjection]:
    builder_id = uuid4()
    subscription_id = uuid4()
    checkout_id = uuid4()
    projection_id = uuid4()
    delivery_id = uuid4()
    now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    snapshot = subscription_pricing_snapshot(
        BILLING_CATALOG["plan_plus_monthly_v1"], active_clinics=1
    )
    subscription = BillingSubscription(
        id=subscription_id,
        builder_id=builder_id,
        provider="asaas",
        external_subscription_id="sub_cycle_fixture",
        is_active=True,
        plan="plus",
        billing_cycle="monthly",
        catalog_version_id="payment-2026-10-03",
        offer_id="plan_plus_monthly_v1",
        amount_cents=99_900,
        status="active",
        roster_revision=1,
    )
    checkout = BillingCheckoutSession(
        id=checkout_id,
        builder_id=builder_id,
        intent_kind="subscription",
        intent_id=subscription_id,
        migration_id=None,
        topup_order_id=None,
        attempt_no=1,
        provider="asaas",
        external_checkout_id="checkout_cycle_fixture",
        resulting_external_subscription_id="sub_cycle_fixture",
        state="completed",
        catalog_version_id="payment-2026-10-03",
        offer_id="plan_plus_monthly_v1",
        amount_cents=99_900,
        pricing_snapshot=snapshot,
        next_due_date=date(2026, 10, 12),
    )
    delivery = BillingWebhookDelivery(
        id=delivery_id,
        builder_id=builder_id,
        provider="asaas",
        event_id=f"event-{delivery_id}",
        event_name="PAYMENT_CONFIRMED",
        provider_created_at=now,
        body_bytes=2,
        body_sha256="b" * 64,
        status="processed",
    )
    projection = BillingPaymentProjection(
        id=projection_id,
        builder_id=builder_id,
        provider="asaas",
        payment_id="pay_cycle_fixture",
        aggregate_kind="subscription",
        subscription_id=subscription_id,
        topup_order_id=None,
        checkout_session_id=checkout_id,
        status="settled",
        amount_cents=99_900,
        settled_cents=99_900,
        reversed_cents=0,
        restored_cents=0,
        restoration_epoch=0,
        provider_created_at=now,
        ordering_key="00000000000000001.00:fixture",
        last_event_id="fixture",
    )
    effect = BillingPaymentEffect(
        id=uuid4(),
        builder_id=builder_id,
        projection_id=projection_id,
        delivery_id=delivery_id,
        provider="asaas",
        payment_id="pay_cycle_fixture",
        effect_kind="settlement",
        source_revision="settlement:v1",
        amount_cents=99_900,
    )
    session.add_all(
        (
            Builder(id=builder_id, name="Subscription Fixture"),
            subscription,
            checkout,
            delivery,
            projection,
            effect,
        )
    )
    await session.flush()
    return effect, projection


async def test_effect_creates_one_atomic_outbox_delivery_and_handoff(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixed_now = datetime(2026, 10, 12, 12, 0, 1, tzinfo=timezone.utc)
    async with session_factory() as session:
        effect, projection, order = await _settled_topup(session)
        producer = BillingFactProducer(session, now=lambda: fixed_now)
        outbox = await producer.record_effect(effect, projection)
        assert outbox is not None
        event_id = outbox.id
        await session.commit()

    async with session_factory() as session:
        stored = await session.get(BillingOutboxEvent, event_id)
        assert stored is not None
        assert stored.financial_revision == 1
        assert stored.payload["event_id"] == str(event_id)
        assert stored.bundle_digest == billing_fact_bundle_digest()
        assert (
            await session.scalar(
                select(BillingFactDelivery.id).where(
                    BillingFactDelivery.outbox_event_id == event_id
                )
            )
            is not None
        )
        assert (
            await session.scalar(
                select(BillingFactHandoff.id).where(
                    BillingFactHandoff.outbox_event_id == event_id
                )
            )
            is not None
        )
        replay_effect = await session.get(BillingPaymentEffect, effect.id)
        replay_projection = await session.get(BillingPaymentProjection, projection.id)
        assert replay_effect is not None and replay_projection is not None
        replay = await BillingFactProducer(
            session, now=lambda: fixed_now
        ).record_effect(replay_effect, replay_projection)
        assert replay is not None and replay.id == event_id
        assert await session.scalar(select(BillingTopupOrder.financial_revision)) == 1


async def test_subscription_fact_creates_current_cycle_with_xor_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        effect, projection = await _settled_subscription(session)
        outbox = await BillingFactProducer(session).record_effect(
            effect, projection, cycle_start=date(2026, 10, 12)
        )
        assert outbox is not None
        assert outbox.aggregate_kind == "subscription_cycle"
        assert outbox.subscription_id == projection.subscription_id
        assert outbox.topup_order_id is None
        assert outbox.subscription_cycle_id is not None
        cycle = await session.get(
            BillingSubscriptionCycle, outbox.subscription_cycle_id
        )
        assert cycle is not None
        assert (cycle.cycle_start, cycle.cycle_end) == (
            date(2026, 10, 12),
            date(2026, 11, 12),
        )
        assert outbox.payload["subscription_cycle"] == {
            "cycle_start": "2026-10-12",
            "cycle_end": "2026-11-12",
        }
        await session.commit()


async def test_lost_ack_republishes_same_event_then_reconciles_read_only_ack(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        effect, projection, _order = await _settled_topup(session)
        outbox = await BillingFactProducer(session).record_effect(effect, projection)
        assert outbox is not None
        outbox_id = outbox.id
        await session.commit()

    sent: list[str] = []

    async def transport(payload: dict[str, object]) -> None:
        sent.append(str(payload["event_id"]))

    async with session_factory() as session:
        publisher = BillingFactPublisher(session)
        await publisher.publish_pending(transport)
        await publisher.publish_pending(transport)
        assert sent == [str(outbox_id), str(outbox_id)]
        delivery = await session.scalar(
            select(BillingFactDelivery).where(
                BillingFactDelivery.outbox_event_id == outbox_id
            )
        )
        stored = await session.get(BillingOutboxEvent, outbox_id)
        assert delivery is not None and stored is not None
        # This is deliberately a direct consumer-owned insert.  The producer
        # has no ack creation API and only reads the append-only row below.
        session.add(
            BillingFactAck(
                id=uuid4(),
                outbox_event_id=outbox_id,
                event_id=outbox_id,
                schema_version=ACK_SCHEMA_VERSION,
                outcome="accepted",
                inbox_event_id=uuid4(),
                fact_sha256=stored.payload_sha256,
                bundle_digest=str(stored.bundle_digest),
                deployment_digest=str(stored.deployment_digest),
                effects_digest="b" * 64,
                acknowledged_at=datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
            )
        )
        result = await BillingFactReconciler(session).reconcile()
        assert not result.rollout_blocked
        assert delivery.state == "acknowledged"


async def test_partial_credit_settlement_is_quarantined_without_a_fact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        effect, projection, order = await _settled_topup(session)
        projection.settled_cents = 5_000
        result = await BillingFactProducer(session).record_effect(effect, projection)
        assert result is None
        assert order.state == "quarantined"
        assert await session.scalar(select(BillingOutboxEvent.id)) is None


async def test_cumulative_reversal_restoration_and_zero_restoration_are_ordered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        settlement, projection, order = await _settled_topup(session)
        producer = BillingFactProducer(session)
        assert await producer.record_effect(settlement, projection) is not None

        # Facts deliberately derive from the effect and cumulative cents, never
        # the projection's informational status label.
        projection.status = "readback_required"
        projection.reversed_cents = 5_000
        reversal = BillingPaymentEffect(
            id=uuid4(),
            builder_id=projection.builder_id,
            projection_id=projection.id,
            delivery_id=settlement.delivery_id,
            provider="asaas",
            payment_id=projection.payment_id,
            effect_kind="reversal",
            source_revision="reversed_cents:5000",
            amount_cents=5_000,
        )
        session.add(reversal)
        await session.flush()
        assert await producer.record_effect(reversal, projection) is not None

        projection.status = "pending"
        projection.restored_cents = 2_500
        restoration = BillingPaymentEffect(
            id=uuid4(),
            builder_id=projection.builder_id,
            projection_id=projection.id,
            delivery_id=settlement.delivery_id,
            provider="asaas",
            payment_id=projection.payment_id,
            effect_kind="restoration",
            source_revision="restoration_epoch:1",
            amount_cents=2_500,
        )
        session.add(restoration)
        await session.flush()
        assert await producer.record_effect(restoration, projection) is not None

        zero_restoration = BillingPaymentEffect(
            id=uuid4(),
            builder_id=projection.builder_id,
            projection_id=projection.id,
            delivery_id=settlement.delivery_id,
            provider="asaas",
            payment_id=projection.payment_id,
            effect_kind="restoration",
            source_revision="restoration_epoch:2",
            amount_cents=0,
        )
        session.add(zero_restoration)
        await session.flush()
        assert await producer.record_effect(zero_restoration, projection) is not None

        facts = list(
            (
                await session.execute(
                    select(BillingOutboxEvent).order_by(
                        BillingOutboxEvent.financial_revision
                    )
                )
            ).scalars()
        )
        assert [fact.financial_revision for fact in facts] == [1, 2, 3, 4]
        assert [fact.event_type for fact in facts] == [
            "topup.payment_settled.v1",
            "topup.payment_reversed.v1",
            "topup.payment_restored.v1",
            "topup.payment_restored.v1",
        ]
        assert facts[-1].payload["reversed_cents"] == 5_000
        assert facts[-1].payload["restored_cents"] == 2_500
        assert order.state == "restored"
        assert order.financial_revision == 4


async def test_fact_rows_satisfy_enforced_foreign_keys_on_sqlite(
    tmp_path: Path,
) -> None:
    """Unit-tier guard for the outbox -> delivery -> handoff insert order.

    SQLite ignores foreign keys unless ``PRAGMA foreign_keys=ON``; with it on,
    a single flush that lets the unit of work insert ``billing_fact_deliveries``
    before ``billing_outbox`` fails here exactly as it does on PostgreSQL.
    """

    from sqlalchemy import event

    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            effect, projection, _order = await _settled_topup(session, ordered=True)
            outbox = await BillingFactProducer(session).record_effect(
                effect, projection
            )
            assert outbox is not None
            await session.commit()
            assert (
                await session.scalar(
                    select(BillingFactHandoff.delivery_id).where(
                        BillingFactHandoff.outbox_event_id == outbox.id
                    )
                )
                == await session.scalar(
                    select(BillingFactDelivery.id).where(
                        BillingFactDelivery.outbox_event_id == outbox.id
                    )
                )
            )
    finally:
        await engine.dispose()
