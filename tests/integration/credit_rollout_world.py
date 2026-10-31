"""A PostgreSQL world for the Group 7 rollout suites (helpers, not tests).

Each proof gets a fresh database copied from one migrated template (the
``template``/``clean`` fixtures of the ASAAS fact-worker suite).  ASAAS rows
(subscriptions, cycles, orders, outbox facts, acks, alerts, payment effects)
belong to the producer; they are planted here directly, the cycle/order rows
with ``session_replication_role = replica`` because their deferred
"revision needs an outbox fact" trigger would otherwise demand the whole
producer lineage.  Credit rows are written with every trigger on, in the
shape the fact consumer writes them (effect, lot, grant entry, binding).

Dates are relative to the real São Paulo civil date so the request-path
proofs (which read the wall clock) hold on any day.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.services.billing_outbox import PRODUCER
from src.services.credit_ledger import start_of_civil_day
from tests.integration import test_billing_fact_worker_postgres as worker_suite
from tests.integration.test_billing_fact_worker_postgres import _Harness

template = worker_suite.template
clean = worker_suite.clean

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
SHA = "a" * 64
PLANT = "SET LOCAL session_replication_role = replica"
OPEN_ACTIVE = ("active", "active_linked", "readback_required")


def civil_today(instant: datetime | None = None) -> date:
    return (instant or datetime.now(timezone.utc)).astimezone(SAO_PAULO).date()


async def execute(harness: _Harness, sql: str, **params: Any) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(text(sql), params)


async def plant(harness: _Harness, sql: str, **params: Any) -> None:
    """One producer-owned row, its triggers off (constraints still apply)."""

    async with harness.engine.begin() as connection:
        await connection.execute(text(PLANT))
        await connection.execute(text(sql), params)


async def scalar(harness: _Harness, sql: str, **params: Any) -> Any:
    async with harness.engine.connect() as connection:
        return await connection.scalar(text(sql), params)


async def make_builder(harness: _Harness) -> UUID:
    builder_id = uuid4()
    await execute(
        harness,
        "INSERT INTO builders (id, name) VALUES (:id, 'Rollout builder')",
        id=builder_id,
    )
    await execute(
        harness,
        "INSERT INTO credit_accounts (id, builder_id) VALUES (:id, :builder)",
        id=uuid4(),
        builder=builder_id,
    )
    return builder_id


async def make_tenant(harness: _Harness, builder_id: UUID | None) -> UUID:
    tenant_id = uuid4()
    await execute(
        harness,
        "INSERT INTO tenants (id, name, slug, api_key_hash, is_active, builder_id) "
        "VALUES (:id, 'Rollout clinic', :slug, :hash, true, :builder)",
        id=tenant_id,
        slug=f"rollout-{tenant_id.hex}",
        hash=f"hash-{tenant_id.hex}",
        builder=builder_id,
    )
    return tenant_id


async def make_subscription(
    harness: _Harness, builder_id: UUID, *, status: str = "active"
) -> UUID:
    subscription_id = uuid4()
    await plant(
        harness,
        """
        INSERT INTO billing_subscriptions (
            id, builder_id, provider, external_subscription_id, is_active, plan,
            billing_cycle, status
        ) VALUES (:id, :builder, 'asaas', :external, :active, 'plus', 'monthly',
                  :status)
        """,
        id=subscription_id,
        builder=builder_id,
        external=f"sub_{subscription_id.hex[:12]}",
        active=status in OPEN_ACTIVE,
        status=status,
    )
    return subscription_id


async def set_subscription_status(
    harness: _Harness, subscription_id: UUID, status: str
) -> None:
    await execute(
        harness,
        "UPDATE billing_subscriptions SET status = :status, is_active = :active "
        "WHERE id = :id",
        id=subscription_id,
        status=status,
        active=status in OPEN_ACTIVE,
    )


async def make_cycle(
    harness: _Harness,
    builder_id: UUID,
    subscription_id: UUID,
    *,
    start: date,
    end: date,
    revision: int = 1,
    state: str = "settled",
) -> UUID:
    cycle_id = uuid4()
    await plant(
        harness,
        """
        INSERT INTO billing_subscription_cycles (
            id, builder_id, subscription_id, payment_id, catalog_version_id,
            offer_id, cycle_start, cycle_end, amount_cents, credit_grant_mc,
            pricing_snapshot, financial_snapshot, state, financial_revision,
            settled_cents
        ) VALUES (
            :id, :builder, :subscription, :payment, 'catalog-v1', 'plan_plus_v1',
            :start, :end, 99900, 5000000, '{}'::jsonb, '{}'::jsonb, :state,
            :revision, 99900
        )
        """,
        id=cycle_id,
        builder=builder_id,
        subscription=subscription_id,
        payment=f"pay_{cycle_id.hex[:16]}",
        start=start,
        end=end,
        state=state,
        revision=revision,
    )
    return cycle_id


async def set_cycle_revision(harness: _Harness, cycle_id: UUID, revision: int) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(text(PLANT))
        await connection.execute(
            text(
                "UPDATE billing_subscription_cycles SET financial_revision = :r "
                "WHERE id = :id"
            ),
            {"r": revision, "id": cycle_id},
        )


async def _account(harness: _Harness, builder_id: UUID) -> UUID:
    return UUID(
        str(
            await scalar(
                harness,
                "SELECT id FROM credit_accounts WHERE builder_id = :b",
                b=builder_id,
            )
        )
    )


@dataclass(frozen=True)
class Grant:
    effect_id: str
    lot_id: UUID
    binding_id: UUID | None


async def make_monthly_grant(
    harness: _Harness,
    *,
    builder_id: UUID,
    subscription_id: UUID,
    cycle_id: UUID,
    start: date,
    end: date,
    revision: int = 1,
    status: str = "active",
    binding_start: date | None = None,
    binding_end: date | None = None,
    binding_created_at: datetime | None = None,
    with_binding: bool = True,
) -> Grant:
    """What the fact consumer writes for a settled cycle: grant effect,
    monthly lot, its ``grant`` entry and the cycle's binding."""

    account = await _account(harness, builder_id)
    lot_start, lot_end = binding_start or start, binding_end or end
    effect_id = f"cycle:{cycle_id}:grant"
    lot_id = uuid4()
    await execute(
        harness,
        "INSERT INTO credit_effects (id, builder_id, effect_id, effect_kind) "
        "VALUES (:id, :builder, :effect, 'grant')",
        id=uuid4(),
        builder=builder_id,
        effect=effect_id,
    )
    await execute(
        harness,
        """
        INSERT INTO credit_lots (
            id, builder_id, account_id, lot_kind, subscription_id, cycle_start,
            cycle_end, payment_id, paid_at, granted_mc, expires_at, grant_effect_id
        ) VALUES (
            :id, :builder, :account, 'monthly', :subscription, :start, :end,
            :payment, :start, 5000000, :expires, :effect
        )
        """,
        id=lot_id,
        builder=builder_id,
        account=account,
        subscription=subscription_id,
        start=lot_start,
        end=lot_end,
        payment=f"pay_{cycle_id.hex[:16]}",
        expires=start_of_civil_day(lot_end),
        effect=effect_id,
    )
    await execute(
        harness,
        """
        INSERT INTO credit_ledger_entries (
            id, builder_id, account_id, effect_id, lot_id, entry_type, amount_mc,
            occurred_at
        ) VALUES (:id, :builder, :account, :effect, :lot, 'grant', 5000000, :at)
        """,
        id=uuid4(),
        builder=builder_id,
        account=account,
        effect=effect_id,
        lot=lot_id,
        at=start_of_civil_day(lot_start),
    )
    binding_id: UUID | None = None
    if with_binding:
        binding_id = uuid4()
        created = binding_created_at or start_of_civil_day(lot_start)
        await execute(
            harness,
            """
            INSERT INTO credit_enforcement_binding (
                id, builder_id, subscription_id, subscription_cycle_id, cycle_start,
                cycle_end, financial_revision, grant_effect_id, status, created_at,
                updated_at
            ) VALUES (
                :id, :builder, :subscription, :cycle, :start, :end, :revision,
                :effect, :status, :created, :created
            )
            """,
            id=binding_id,
            builder=builder_id,
            subscription=subscription_id,
            cycle=cycle_id,
            start=lot_start,
            end=lot_end,
            revision=revision,
            effect=effect_id,
            status=status,
            created=created,
        )
    return Grant(effect_id=effect_id, lot_id=lot_id, binding_id=binding_id)


async def reverse_binding(harness: _Harness, binding_id: UUID, revision: int) -> None:
    """The consumer's update after a full chargeback of the plan line."""

    await execute(
        harness,
        "UPDATE credit_enforcement_binding SET status = 'reversed', "
        "financial_revision = :r WHERE id = :id",
        r=revision,
        id=binding_id,
    )


async def outbox_fact(
    harness: _Harness,
    *,
    builder_id: UUID,
    order_id: UUID,
    revision: int = 1,
    ack: str | None = "accepted",
    inbox: bool = True,
) -> UUID:
    """A top-up fact of the producer, with the consumer's inbox row and ack."""

    outbox_id = uuid4()
    event_type = "topup.payment_settled.v1"
    await plant(
        harness,
        """
        INSERT INTO billing_outbox (
            id, builder_id, schema_version, event_type, aggregate_kind,
            aggregate_id, topup_order_id, financial_revision, payload,
            payload_sha256
        ) VALUES (
            :id, :builder, 'billing.fact.v1', :event_type, 'topup_order', :order,
            :order, :revision, CAST(:payload AS JSONB), :sha
        )
        """,
        id=outbox_id,
        builder=builder_id,
        event_type=event_type,
        order=order_id,
        revision=revision,
        payload=json.dumps({"probe": str(outbox_id)}),
        sha=SHA,
    )
    inbox_id = uuid4()
    if inbox:
        await execute(
            harness,
            """
            INSERT INTO credit_fact_inbox (
                id, producer, event_id, schema_version, aggregate_kind,
                aggregate_id, financial_revision, fact_sha256, bundle_digest,
                schema_digest, outcome
            ) VALUES (
                :id, :producer, :event, 'billing.fact.v1', 'topup_order', :order,
                :revision, :sha, :sha, :sha, :outcome
            )
            """,
            id=inbox_id,
            producer=PRODUCER,
            event=outbox_id,
            order=order_id,
            revision=revision,
            sha=SHA,
            outcome=ack or "accepted",
        )
    if ack is not None:
        await plant(
            harness,
            """
            INSERT INTO billing_fact_acks (
                id, outbox_event_id, event_id, schema_version, outcome,
                inbox_event_id, fact_sha256, bundle_digest, deployment_digest,
                effects_digest, acknowledged_at
            ) VALUES (
                :id, :outbox, :outbox, 'billing.fact.ack.v1', :outcome, :inbox,
                :sha, :sha, :sha, :sha, now()
            )
            """,
            id=uuid4(),
            outbox=outbox_id,
            outcome=ack,
            inbox=inbox_id,
            sha=SHA,
        )
    return outbox_id


async def make_topup(
    harness: _Harness,
    builder_id: UUID,
    *,
    accepted: bool = True,
    state: str = "settled",
    reversed_mc: int = 0,
    expires_at: datetime | None = None,
) -> UUID:
    """A settled top-up order, its fact (acked or not) and its granted lot."""

    order_id = uuid4()
    await plant(
        harness,
        """
        INSERT INTO billing_topup_orders (
            id, builder_id, payment_method, catalog_version_id, offer_id,
            amount_cents, credit_grant_mc, pricing_snapshot, state,
            financial_revision, settled_cents
        ) VALUES (
            :id, :builder, 'pix', 'catalog-v1', 'package_credits_500_v1', 10000,
            500000, '{}'::jsonb, :state, 1, 10000
        )
        """,
        id=order_id,
        builder=builder_id,
        state=state,
    )
    await outbox_fact(
        harness,
        builder_id=builder_id,
        order_id=order_id,
        ack="accepted" if accepted else None,
        inbox=accepted,
    )
    account = await _account(harness, builder_id)
    effect_id, lot_id = f"topup:{order_id}:grant", uuid4()
    await execute(
        harness,
        "INSERT INTO credit_effects (id, builder_id, effect_id, effect_kind) "
        "VALUES (:id, :builder, :effect, 'grant')",
        id=uuid4(),
        builder=builder_id,
        effect=effect_id,
    )
    await execute(
        harness,
        """
        INSERT INTO credit_lots (
            id, builder_id, account_id, lot_kind, order_id, payment_id, paid_at,
            granted_mc, expires_at, grant_effect_id
        ) VALUES (
            :id, :builder, :account, 'topup', :order, :payment, :paid, 500000,
            :expires, :effect
        )
        """,
        id=lot_id,
        builder=builder_id,
        account=account,
        order=order_id,
        payment=f"pay_{order_id.hex[:16]}",
        paid=civil_today() - timedelta(days=3),
        expires=expires_at or datetime.now(timezone.utc) + timedelta(days=150),
        effect=effect_id,
    )
    for entry_type, amount in (("grant", 500000), ("reversal", reversed_mc)):
        if amount:
            await execute(
                harness,
                """
                INSERT INTO credit_ledger_entries (
                    id, builder_id, account_id, effect_id, lot_id, entry_type,
                    amount_mc, occurred_at
                ) VALUES (:id, :builder, :account, :effect, :lot, :type, :amount,
                          now() - interval '1 day')
                """,
                id=uuid4(),
                builder=builder_id,
                account=account,
                effect=effect_id,
                lot=lot_id,
                type=entry_type,
                amount=amount,
            )
    return order_id


async def message_in(harness: _Harness, tenant_id: UUID, at: datetime) -> None:
    await execute(
        harness,
        "INSERT INTO events (id, tenant_id, event_type, event_data, created_at) "
        "VALUES (:id, :tenant, 'message.in', '{}'::jsonb, :at)",
        id=uuid4(),
        tenant=tenant_id,
        at=at,
    )


async def observation(
    harness: _Harness,
    tenant_id: UUID,
    builder_id: UUID | None,
    at: datetime,
    *,
    origin: str = "production",
    authenticated: bool = True,
) -> None:
    await execute(
        harness,
        """
        INSERT INTO credit_observational_effects (
            id, tenant_id, builder_id, effect_id, effect_kind, origin,
            authenticated, would_debit_mc, recorded_at
        ) VALUES (:id, :tenant, :builder, :effect, 'trace', :origin, :auth, 1000, :at)
        """,
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        effect=f"trace:{tenant_id}:{uuid4()}:decision:1",
        origin=origin,
        auth=authenticated,
        at=at,
    )


@dataclass(frozen=True)
class Subscribed:
    """A builder, its clinic and an exactly bound current cycle."""

    builder_id: UUID
    tenant_id: UUID
    subscription_id: UUID
    cycle_id: UUID
    grant: Grant
    start: date
    end: date


async def subscribed_builder(
    harness: _Harness,
    *,
    revision: int = 1,
    binding_revision: int | None = None,
    status: str = "active",
    today: date | None = None,
) -> Subscribed:
    day = today or civil_today()
    builder_id = await make_builder(harness)
    tenant_id = await make_tenant(harness, builder_id)
    subscription_id = await make_subscription(harness, builder_id, status=status)
    start, end = day - timedelta(days=10), day + timedelta(days=20)
    cycle_id = await make_cycle(
        harness, builder_id, subscription_id, start=start, end=end, revision=revision
    )
    grant = await make_monthly_grant(
        harness,
        builder_id=builder_id,
        subscription_id=subscription_id,
        cycle_id=cycle_id,
        start=start,
        end=end,
        revision=binding_revision or revision,
    )
    return Subscribed(
        builder_id, tenant_id, subscription_id, cycle_id, grant, start, end
    )


__all__ = [
    "Grant",
    "SAO_PAULO",
    "SHA",
    "Subscribed",
    "civil_today",
    "clean",
    "execute",
    "make_builder",
    "make_cycle",
    "make_monthly_grant",
    "make_subscription",
    "make_tenant",
    "make_topup",
    "message_in",
    "observation",
    "outbox_fact",
    "plant",
    "reverse_binding",
    "scalar",
    "set_cycle_revision",
    "set_subscription_status",
    "subscribed_builder",
    "template",
]
