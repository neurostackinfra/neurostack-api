"""Credit ledger schema on real PostgreSQL (neurostack-credit-system Group 1a).

What SQLite cannot show, proven against migration 038 at head:

* append-only history: UPDATE and DELETE of accounts, effects, lots, ledger
  entries, debts, debt origins, settlements, inbox, readiness and
  observational effects fail (SQLSTATE 23000); a correction is a new row;
  revision gaps only close, once; the enforcement binding keeps its cycle
  identity and never regresses its revision;
* ``credit_enforcement_state`` holds at most one row (``id = 1``);
* claims deduplicate by tenant -- standalone (``builder_id`` NULL) and after a
  reparent -- and the tenant/builder trigger waits for a concurrent reparent;
* references across builders fail under direct SQL (composite foreign keys
  and the tenant/builder trigger);
* concurrency never creates a second account, lot, effect, inbox row, claim
  or outbox row; a second binding of one cycle or subscription period and a
  second observational effect of one tenant and effect id are refused; the
  account and lot locks serialize one builder only;
* lock order step 1 (the tenant row ``FOR SHARE`` before its claims and
  effects) is what keeps a concurrent clinic delete from deadlocking a
  credit transaction;
* the SQL-derived balance equals the reference fold of the same entries;
* deleting a clinic or a builder and reparenting a tenant work as on
  ``main`` with credit rows present: history stays, the deleted tenant's
  claims and outbox rows go by CASCADE, no append-only trigger interferes;
* the ack repository writes a ``billing_fact_acks`` row the untouched ASAAS
  reconciler accepts.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.api.middleware.auth import TenantContext
from src.api.routes import admin as admin_routes
from src.models.billing import BillingFactDelivery, BillingOutboxEvent
from src.models.builder import Builder
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository, LedgerMovement, fold_ledger
from src.repositories.credit_fact_ack import (
    EMPTY_EFFECTS_DIGEST,
    CreditFactAckRepository,
    effects_digest,
)
from src.services.billing_outbox import (
    BillingFactReconciler,
    billing_fact_bundle_digest,
    payload_digest,
)
from src.services.media_assets import (
    MediaOwnerTransition,
    transition_tenant_media_owner,
)
from tests.integration.test_asaas_billing_postgres import (
    _sqlstate,
    _validated_admin_url,
)
from tests.integration.test_billing_fact_worker_postgres import _seed_fact
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

_SHA = "a" * 64
_WAIT = 0.15  # seconds a blocked contender must still be waiting
_TIMEOUT = 15.0


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    def session(self) -> AsyncSession:
        return self.sessions()


@dataclass(frozen=True)
class _Builder:
    builder_id: UUID
    tenant_id: UUID
    account_id: UUID
    grant_effect_id: str
    lot_id: UUID


@pytest.fixture(scope="module")
async def postgres_harness() -> AsyncIterator[_Harness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    engine: AsyncEngine | None = None
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        engine = create_async_engine(cluster.url(name), poolclass=NullPool)
        yield _Harness(
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
        )
    finally:
        if engine is not None:
            await engine.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


# ---------------------------------------------------------------------------
# Seeding helpers (direct SQL, one committed transaction each).
# ---------------------------------------------------------------------------


async def _run(harness: _Harness, statement: str, **params: Any) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(text(statement), params)


async def _value(harness: _Harness, statement: str, **params: Any) -> Any:
    async with harness.engine.connect() as connection:
        return (await connection.execute(text(statement), params)).scalar()


async def _refused(
    harness: _Harness, statement: str, sqlstate: str, match: str, **params: Any
) -> None:
    with pytest.raises(DBAPIError, match=re.escape(match)) as info:
        await _run(harness, statement, **params)
    assert _sqlstate(info.value) == sqlstate, statement


async def _tenant(
    harness: _Harness, builder_id: UUID | None, *, tenant_id: UUID | None = None
) -> UUID:
    tenant_id = tenant_id or uuid4()
    async with harness.session() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="Credit Clinic",
                slug=f"credit-{tenant_id.hex}",
                api_key_hash=f"credit-{tenant_id.hex}",
                plan_type="clinic",
                builder_id=builder_id,
                config={},
            )
        )
        await session.commit()
    return tenant_id


async def _builder_row(harness: _Harness) -> UUID:
    builder_id = uuid4()
    async with harness.session() as session:
        session.add(Builder(id=builder_id, name="Credit Builder"))
        await session.commit()
    return builder_id


async def _effect(
    harness: _Harness,
    builder_id: UUID,
    effect_id: str,
    *,
    kind: str = "grant",
    tenant_id: UUID | None = None,
) -> None:
    await _run(
        harness,
        "INSERT INTO credit_effects (id, builder_id, tenant_id, effect_id, "
        "effect_kind) VALUES (:id, :builder, :tenant, :effect, :kind)",
        id=uuid4(),
        builder=builder_id,
        tenant=tenant_id,
        effect=effect_id,
        kind=kind,
    )


async def _topup_lot(
    harness: _Harness,
    builder_id: UUID,
    account_id: UUID,
    grant_effect_id: str,
    *,
    granted_mc: int = 500_000,
) -> UUID:
    lot_id = uuid4()
    await _run(
        harness,
        "INSERT INTO credit_lots (id, builder_id, account_id, lot_kind, order_id, "
        "payment_id, paid_at, granted_mc, expires_at, grant_effect_id) VALUES "
        "(:id, :builder, :account, 'topup', :order, :payment, DATE '2026-10-26', "
        ":granted, TIMESTAMPTZ '2027-04-26 03:00:00+00', :effect)",
        id=lot_id,
        builder=builder_id,
        account=account_id,
        order=uuid4(),
        payment=f"pay_{lot_id.hex}",
        granted=granted_mc,
        effect=grant_effect_id,
    )
    return lot_id


async def _entry(
    harness: _Harness,
    seeded: _Builder,
    entry_type: str,
    amount_mc: int,
    *,
    lot_id: UUID | None,
    effect_id: str | None = None,
) -> UUID:
    entry_id = uuid4()
    await _run(
        harness,
        "INSERT INTO credit_ledger_entries (id, builder_id, account_id, effect_id, "
        "lot_id, entry_type, amount_mc, occurred_at) VALUES (:id, :builder, "
        ":account, :effect, :lot, :type, :amount, now())",
        id=entry_id,
        builder=seeded.builder_id,
        account=seeded.account_id,
        effect=effect_id or seeded.grant_effect_id,
        lot=lot_id,
        type=entry_type,
        amount=amount_mc,
    )
    return entry_id


async def _seed(harness: _Harness, *, standalone_tenant: bool = False) -> _Builder:
    """A builder with a clinic, its account, a grant effect and a top-up lot
    whose grant entry is in the ledger."""

    builder_id = await _builder_row(harness)
    tenant_id = await _tenant(harness, None if standalone_tenant else builder_id)
    async with harness.session() as session:
        account_id = await CreditRepository(session).ensure_account(builder_id)
        await session.commit()
    grant = f"topup-grant-{uuid4().hex}"
    await _effect(harness, builder_id, grant)
    lot_id = await _topup_lot(harness, builder_id, account_id, grant)
    seeded = _Builder(builder_id, tenant_id, account_id, grant, lot_id)
    await _entry(harness, seeded, "grant", 500_000, lot_id=lot_id)
    return seeded


async def _claim(
    harness: _Harness,
    tenant_id: UUID,
    builder_id: UUID | None,
    *,
    key: str | None = "idem-1",
    source: str = "events",
    message: str | None = None,
    principal: str | None = None,
) -> None:
    await _run(
        harness,
        "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
        "principal_id, idempotency_key, provider_message_id, interaction_id, "
        "payload_sha256, decision_version, mode) VALUES (:id, :tenant, :builder, "
        ":source, :principal, :key, :message, :interaction, :sha, 1, "
        "'observational')",
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        source=source,
        principal=principal or f"api_key:{tenant_id}",
        key=key,
        message=message,
        interaction=uuid4(),
        sha=_SHA,
    )


async def _observational(
    harness: _Harness, tenant_id: UUID, builder_id: UUID | None, effect_id: str
) -> None:
    await _run(
        harness,
        "INSERT INTO credit_observational_effects (id, tenant_id, builder_id, "
        "effect_id, effect_kind, origin, authenticated, would_debit_mc) VALUES "
        "(:id, :tenant, :builder, :effect, 'trace', 'production', true, 1000)",
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        effect=effect_id,
    )


async def _outbox(harness: _Harness, tenant_id: UUID, effect_id: str) -> None:
    await _run(
        harness,
        "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload) "
        "VALUES (:id, :tenant, :effect, CAST('{}' AS JSONB))",
        id=uuid4(),
        tenant=tenant_id,
        effect=effect_id,
    )


async def _history(harness: _Harness, seeded: _Builder) -> dict[str, str]:
    """One row of every append-only table for *seeded*; returns, per table,
    a WHERE clause naming it."""

    debit = await _entry(harness, seeded, "debit", 1_000, lot_id=seeded.lot_id)
    debt_effect = f"trace:{seeded.tenant_id}:{uuid4()}:decision:1"
    await _effect(
        harness,
        seeded.builder_id,
        debt_effect,
        kind="trace",
        tenant_id=seeded.tenant_id,
    )
    debt_id = uuid4()
    await _run(
        harness,
        "INSERT INTO credit_debts (id, builder_id, account_id, debt_source_kind, "
        "debt_source_effect_id, amount_mc) VALUES (:id, :builder, :account, "
        "'consumption', :effect, 1000)",
        id=debt_id,
        builder=seeded.builder_id,
        account=seeded.account_id,
        effect=debt_effect,
    )
    await _run(
        harness,
        "INSERT INTO credit_debt_origins (builder_id, debt_id, consumption_entry_id) "
        "VALUES (:builder, :debt, :entry)",
        builder=seeded.builder_id,
        debt=debt_id,
        entry=debit,
    )
    settlement_id = uuid4()
    await _run(
        harness,
        "INSERT INTO credit_debt_settlements (id, builder_id, debt_id, "
        "settling_grant_lot_id, amount_mc, effect_id) VALUES (:id, :builder, "
        ":debt, :lot, 1000, :effect)",
        id=settlement_id,
        builder=seeded.builder_id,
        debt=debt_id,
        lot=seeded.lot_id,
        effect=seeded.grant_effect_id,
    )
    inbox_id = uuid4()
    await _run(
        harness,
        "INSERT INTO credit_fact_inbox (id, producer, event_id, schema_version, "
        "aggregate_kind, aggregate_id, financial_revision, fact_sha256, "
        "bundle_digest, schema_digest, outcome) VALUES (:id, "
        "'asaas-payment-gateway', :event, 'billing.fact.v1', 'topup_order', "
        ":aggregate, 1, :sha, :sha, :sha, 'accepted')",
        id=inbox_id,
        event=uuid4(),
        aggregate=uuid4(),
        sha=_SHA,
    )
    readiness_id = uuid4()
    await _run(
        harness,
        "INSERT INTO credit_capability_readiness (id, schema_version, environment, "
        "billing_fact_schema_digest, inbox_high_watermark, reconciliation_complete, "
        "readback_complete, has_open_gap, has_backlog, has_alert) VALUES (:id, "
        "'credit_capability_ready.v1', 'test', :sha, 0, false, false, false, "
        "false, false)",
        id=readiness_id,
        sha=_SHA,
    )
    observed = f"trace:{seeded.tenant_id}:{uuid4()}:decision:1"
    await _observational(harness, seeded.tenant_id, seeded.builder_id, observed)
    return {
        "credit_accounts": f"id = '{seeded.account_id}'",
        "credit_effects": f"builder_id = '{seeded.builder_id}'",
        "credit_lots": f"id = '{seeded.lot_id}'",
        "credit_ledger_entries": f"id = '{debit}'",
        "credit_debts": f"id = '{debt_id}'",
        "credit_debt_origins": f"debt_id = '{debt_id}'",
        "credit_debt_settlements": f"id = '{settlement_id}'",
        "credit_fact_inbox": f"id = '{inbox_id}'",
        "credit_capability_readiness": f"id = '{readiness_id}'",
        "credit_observational_effects": f"tenant_id = '{seeded.tenant_id}'",
    }


# ---------------------------------------------------------------------------
# Append-only history and guarded updates.
# ---------------------------------------------------------------------------

_APPEND_ONLY = (
    "credit_accounts",
    "credit_effects",
    "credit_lots",
    "credit_ledger_entries",
    "credit_debts",
    "credit_debt_origins",
    "credit_debt_settlements",
    "credit_fact_inbox",
    "credit_capability_readiness",
    "credit_observational_effects",
)
# A column every table has, set to itself: even a no-op UPDATE is refused.
_SET = {
    "credit_debt_origins": "builder_id = builder_id",
    "credit_debt_settlements": "amount_mc = amount_mc",
    "credit_fact_inbox": "outcome = outcome",
    "credit_capability_readiness": "has_alert = has_alert",
}


@pytest.mark.parametrize("table", _APPEND_ONLY)
async def test_history_refuses_update_and_delete(
    postgres_harness: _Harness, table: str
) -> None:
    seeded = await _seed(postgres_harness)
    where = (await _history(postgres_harness, seeded))[table]
    before = await _value(
        postgres_harness, f"SELECT count(*) FROM {table} WHERE {where}"
    )
    assert before >= 1
    assignment = _SET.get(table, "created_at = created_at")
    if table == "credit_observational_effects":
        assignment = "would_debit_mc = 0"
    await _refused(
        postgres_harness,
        f"UPDATE {table} SET {assignment} WHERE {where}",
        "23000",
        f"{table} is append-only",
    )
    await _refused(
        postgres_harness,
        f"DELETE FROM {table} WHERE {where}",
        "23000",
        f"{table} is append-only",
    )
    assert (
        await _value(postgres_harness, f"SELECT count(*) FROM {table} WHERE {where}")
        == before
    )


async def test_a_correction_is_a_new_ledger_entry(postgres_harness: _Harness) -> None:
    seeded = await _seed(postgres_harness)
    wrong = await _entry(postgres_harness, seeded, "debit", 5_000, lot_id=seeded.lot_id)
    await _refused(
        postgres_harness,
        "UPDATE credit_ledger_entries SET amount_mc = 1000 WHERE id = :id",
        "23000",
        "credit_ledger_entries is append-only",
        id=wrong,
    )
    correction = f"correction-{uuid4().hex}"
    await _effect(postgres_harness, seeded.builder_id, correction, kind="correction")
    await _entry(
        postgres_harness,
        seeded,
        "lot_regrant",
        4_000,
        lot_id=seeded.lot_id,
        effect_id=correction,
    )
    async with postgres_harness.session() as session:
        balance = await CreditRepository(session).derived_balance(seeded.builder_id)
    assert balance.lots == {seeded.lot_id: 500_000 - 5_000 + 4_000}


async def test_a_revision_gap_closes_once_and_is_never_deleted(
    postgres_harness: _Harness,
) -> None:
    aggregate = uuid4()
    key = "aggregate_kind = 'topup_order' AND aggregate_id = :aggregate"
    await _run(
        postgres_harness,
        "INSERT INTO credit_revision_gaps (aggregate_kind, aggregate_id, "
        "first_missing_revision) VALUES ('topup_order', :aggregate, 2)",
        aggregate=aggregate,
    )
    for assignment in ("first_missing_revision = 3", "opened_at = opened_at"):
        await _refused(
            postgres_harness,
            f"UPDATE credit_revision_gaps SET {assignment} WHERE {key}",
            "23000",
            "a credit revision gap only closes, once",
            aggregate=aggregate,
        )
    await _run(
        postgres_harness,
        f"UPDATE credit_revision_gaps SET closed_at = now() WHERE {key}",
        aggregate=aggregate,
    )
    await _refused(
        postgres_harness,
        f"UPDATE credit_revision_gaps SET closed_at = now() WHERE {key}",
        "23000",
        "a credit revision gap only closes, once",
        aggregate=aggregate,
    )
    await _refused(
        postgres_harness,
        f"DELETE FROM credit_revision_gaps WHERE {key}",
        "23000",
        "credit_revision_gaps rows are never deleted",
        aggregate=aggregate,
    )


async def test_the_binding_keeps_its_cycle_and_never_regresses(
    postgres_harness: _Harness,
) -> None:
    seeded = await _seed(postgres_harness)
    grant = f"cycle-grant-{uuid4().hex}"
    await _effect(postgres_harness, seeded.builder_id, grant)
    binding = uuid4()
    await _run(
        postgres_harness,
        "INSERT INTO credit_enforcement_binding (id, builder_id, subscription_id, "
        "subscription_cycle_id, cycle_start, cycle_end, financial_revision, "
        "grant_effect_id, status) VALUES (:id, :builder, :subscription, :cycle, "
        "DATE '2026-10-01', DATE '2026-11-01', 1, :grant, 'active')",
        id=binding,
        builder=seeded.builder_id,
        subscription=uuid4(),
        cycle=uuid4(),
        grant=grant,
    )
    # Every applied cycle fact moves the revision forward (and the status).
    await _run(
        postgres_harness,
        "UPDATE credit_enforcement_binding SET financial_revision = 2, "
        "status = 'reversed', updated_at = now() WHERE id = :id",
        id=binding,
    )
    await _run(
        postgres_harness,
        "UPDATE credit_enforcement_binding SET financial_revision = 3, "
        "status = 'active' WHERE id = :id",
        id=binding,
    )
    await _refused(
        postgres_harness,
        "UPDATE credit_enforcement_binding SET financial_revision = 2 WHERE id = :id",
        "23000",
        "never regresses its revision",
        id=binding,
    )
    for assignment in (
        "cycle_end = DATE '2026-11-02'",
        "subscription_cycle_id = gen_random_uuid()",
        f"grant_effect_id = '{seeded.grant_effect_id}'",
    ):
        await _refused(
            postgres_harness,
            f"UPDATE credit_enforcement_binding SET {assignment} WHERE id = :id",
            "23000",
            "keeps its cycle identity",
            id=binding,
        )
    await _refused(
        postgres_harness,
        "DELETE FROM credit_enforcement_binding WHERE id = :id",
        "23000",
        "credit_enforcement_binding rows are never deleted",
        id=binding,
    )
    status = await _value(
        postgres_harness,
        "SELECT status || ':' || financial_revision FROM credit_enforcement_binding "
        "WHERE id = :id",
        id=binding,
    )
    assert status == "active:3"


_BINDING = (
    "INSERT INTO credit_enforcement_binding (id, builder_id, subscription_id, "
    "subscription_cycle_id, cycle_start, cycle_end, financial_revision, "
    "grant_effect_id, status) VALUES (gen_random_uuid(), :builder, :subscription, "
    ":cycle, :start, :end, 1, :grant, 'active')"
)


async def test_one_binding_per_cycle_and_per_subscription_period(
    postgres_harness: _Harness,
) -> None:
    seeded = await _seed(postgres_harness)
    grant = f"cycle-grant-{uuid4().hex}"
    await _effect(postgres_harness, seeded.builder_id, grant)
    subscription, cycle = uuid4(), uuid4()
    period = {
        "start": datetime(2026, 10, 1).date(),
        "end": datetime(2026, 11, 1).date(),
    }
    common = {"builder": seeded.builder_id, "grant": grant}
    await _run(
        postgres_harness,
        _BINDING,
        **common,
        subscription=subscription,
        cycle=cycle,
        **period,
    )
    # The same subscription cycle row twice (another subscription period).
    await _refused(
        postgres_harness,
        _BINDING,
        "23505",
        "uq_credit_enforcement_binding_cycle",
        **common,
        subscription=uuid4(),
        cycle=cycle,
        start=datetime(2026, 11, 1).date(),
        end=datetime(2026, 12, 1).date(),
    )
    # The same subscription period under another cycle id.
    await _refused(
        postgres_harness,
        _BINDING,
        "23505",
        "uq_credit_enforcement_binding_subscription_cycle",
        **common,
        subscription=subscription,
        cycle=uuid4(),
        **period,
    )
    # The next period of the same subscription is a new binding.
    await _run(
        postgres_harness,
        _BINDING,
        **common,
        subscription=subscription,
        cycle=uuid4(),
        start=datetime(2026, 11, 1).date(),
        end=datetime(2026, 12, 1).date(),
    )
    assert (
        await _value(
            postgres_harness,
            "SELECT count(*) FROM credit_enforcement_binding "
            "WHERE subscription_id = :subscription",
            subscription=subscription,
        )
        == 2
    )


async def test_one_observational_effect_per_tenant_and_effect_id(
    postgres_harness: _Harness,
) -> None:
    """Retries never duplicate an observational row; the effect id is scoped
    by tenant, so another tenant may carry the same id."""

    mine = await _seed(postgres_harness)
    other = await _seed(postgres_harness)
    effect = f"trace:shared:{uuid4()}:decision:1"
    await _observational(postgres_harness, mine.tenant_id, mine.builder_id, effect)
    await _refused(
        postgres_harness,
        "INSERT INTO credit_observational_effects (id, tenant_id, builder_id, "
        "effect_id, effect_kind, would_debit_mc) VALUES (gen_random_uuid(), "
        ":tenant, :builder, :effect, 'trace', 0)",
        "23505",
        "uq_credit_observational_effects_effect",
        tenant=mine.tenant_id,
        builder=mine.builder_id,
        effect=effect,
    )
    await _observational(postgres_harness, other.tenant_id, other.builder_id, effect)
    rows = await _value(
        postgres_harness,
        "SELECT array_agg(tenant_id::text ORDER BY tenant_id::text) "
        "FROM credit_observational_effects WHERE effect_id = :effect",
        effect=effect,
    )
    assert sorted(rows) == sorted([str(mine.tenant_id), str(other.tenant_id)])


async def test_enforcement_state_holds_at_most_one_row(
    postgres_harness: _Harness,
) -> None:
    await _refused(
        postgres_harness,
        "INSERT INTO credit_enforcement_state (id) VALUES (2)",
        "23514",
        "ck_credit_enforcement_state_singleton",
    )
    await _refused(
        postgres_harness,
        "INSERT INTO credit_enforcement_state (id, credit_enforcement_at) "
        "VALUES (1, now())",
        "23514",
        "ck_credit_enforcement_state_set",
    )
    async with postgres_harness.session() as session:
        assert await CreditRepository(session).enforcement_at() is None
    await _run(postgres_harness, "INSERT INTO credit_enforcement_state (id) VALUES (1)")
    await _refused(
        postgres_harness,
        "INSERT INTO credit_enforcement_state (id) VALUES (1)",
        "23505",
        "credit_enforcement_state_pkey",
    )
    instant = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
    await _run(
        postgres_harness,
        "UPDATE credit_enforcement_state SET credit_enforcement_at = :at, "
        "set_by = 'user:group7-test', set_at = now() WHERE id = 1",
        at=instant,
    )
    async with postgres_harness.session() as session:
        assert await CreditRepository(session).enforcement_at() == instant
    assert (
        await _value(postgres_harness, "SELECT count(*) FROM credit_enforcement_state")
        == 1
    )
    # Leave the shared database as it was (the row is mutable state).
    await _run(postgres_harness, "DELETE FROM credit_enforcement_state")


# ---------------------------------------------------------------------------
# Claims: per-tenant dedupe and tenant/builder coherence.
# ---------------------------------------------------------------------------


async def test_standalone_claims_deduplicate_by_tenant(
    postgres_harness: _Harness,
) -> None:
    tenant_id = await _tenant(postgres_harness, None)
    await _claim(postgres_harness, tenant_id, None, key="idem-standalone")
    await _refused(
        postgres_harness,
        "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
        "principal_id, idempotency_key, interaction_id, payload_sha256, "
        "decision_version, mode) VALUES (gen_random_uuid(), :tenant, NULL, "
        "'events', :principal, 'idem-standalone', gen_random_uuid(), :sha, 1, "
        "'observational')",
        "23505",
        "uq_credit_ingress_claims_idempotency",
        tenant=tenant_id,
        principal=f"api_key:{tenant_id}",
        sha=_SHA,
    )
    await _claim(
        postgres_harness, tenant_id, None, key=None, source="zapi", message="msg-1"
    )
    with pytest.raises(IntegrityError, match="uq_credit_ingress_claims_provider"):
        await _claim(
            postgres_harness, tenant_id, None, key=None, source="zapi", message="msg-1"
        )
    # Another tenant may reuse the same key and message id.
    other = await _tenant(postgres_harness, None)
    await _claim(postgres_harness, other, None, key="idem-standalone")
    await _claim(
        postgres_harness, other, None, key=None, source="zapi", message="msg-1"
    )
    # The key matches the source: never both, never neither.
    with pytest.raises(IntegrityError, match="ck_credit_ingress_claims_key"):
        await _claim(postgres_harness, other, None, key=None)
    with pytest.raises(IntegrityError, match="ck_credit_ingress_claims_key"):
        await _claim(
            postgres_harness, other, None, key="idem-x", source="meta", message="m"
        )


async def test_a_reparented_tenant_keeps_deduplicating_by_tenant(
    postgres_harness: _Harness,
) -> None:
    first = await _builder_row(postgres_harness)
    second = await _builder_row(postgres_harness)
    tenant_id = await _tenant(postgres_harness, first)
    await _claim(postgres_harness, tenant_id, first, key="idem-reparent")
    async with postgres_harness.session() as session:
        await transition_tenant_media_owner(
            session,
            MediaOwnerTransition(
                tenant_id=tenant_id,
                expected_builder_id=first,
                destination_builder_id=second,
            ),
        )
        await session.commit()
    with pytest.raises(IntegrityError, match="uq_credit_ingress_claims_idempotency"):
        await _claim(postgres_harness, tenant_id, second, key="idem-reparent")
    # A claim still naming the old builder is incoherent now.
    await _refused(
        postgres_harness,
        "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
        "principal_id, idempotency_key, interaction_id, payload_sha256, "
        "decision_version, mode) VALUES (gen_random_uuid(), :tenant, :builder, "
        "'events', 'api_key:old', 'idem-new', gen_random_uuid(), :sha, 1, "
        "'observational')",
        "23000",
        "credit_ingress_claims row builder does not match its tenant's builder",
        tenant=tenant_id,
        builder=first,
        sha=_SHA,
    )
    await _claim(postgres_harness, tenant_id, second, key="idem-new")


async def test_the_coherence_trigger_waits_for_a_concurrent_reparent(
    postgres_harness: _Harness,
) -> None:
    first = await _builder_row(postgres_harness)
    second = await _builder_row(postgres_harness)
    tenant_id = await _tenant(postgres_harness, first)
    mover = postgres_harness.session()
    await mover.begin()
    try:
        await transition_tenant_media_owner(
            mover,
            MediaOwnerTransition(
                tenant_id=tenant_id,
                expected_builder_id=first,
                destination_builder_id=second,
            ),
        )
        await mover.flush()
        contender = asyncio.create_task(
            _claim(postgres_harness, tenant_id, first, key="idem-race")
        )
        done, _ = await asyncio.wait({contender}, timeout=_WAIT)
        assert not done, "the claim did not wait for the reparent"
        await mover.commit()
        with pytest.raises(DBAPIError, match="does not match its tenant"):
            await asyncio.wait_for(contender, timeout=_TIMEOUT)
    finally:
        if mover.in_transaction():
            await mover.rollback()
        await mover.close()
    await _claim(postgres_harness, tenant_id, second, key="idem-race")


# ---------------------------------------------------------------------------
# Cross-builder references.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Pair:
    mine: _Builder
    other: _Builder


@pytest.fixture
async def pair(postgres_harness: _Harness) -> _Pair:
    return _Pair(await _seed(postgres_harness), await _seed(postgres_harness))


_CROSS_BUILDER = {
    "lot_on_foreign_account": (
        "INSERT INTO credit_lots (id, builder_id, account_id, lot_kind, order_id, "
        "payment_id, paid_at, granted_mc, expires_at, grant_effect_id) VALUES "
        "(gen_random_uuid(), :mine_builder, :other_account, 'topup', "
        "gen_random_uuid(), 'pay-x', DATE '2026-10-26', 1000, now(), :mine_grant)",
        "23503",
        "fk_credit_lots_account",
    ),
    "lot_on_foreign_grant_effect": (
        "INSERT INTO credit_lots (id, builder_id, account_id, lot_kind, order_id, "
        "payment_id, paid_at, granted_mc, expires_at, grant_effect_id) VALUES "
        "(gen_random_uuid(), :mine_builder, :mine_account, 'topup', "
        "gen_random_uuid(), 'pay-x', DATE '2026-10-26', 1000, now(), :other_grant)",
        "23503",
        "fk_credit_lots_grant_effect",
    ),
    "entry_on_foreign_lot": (
        "INSERT INTO credit_ledger_entries (id, builder_id, account_id, effect_id, "
        "lot_id, entry_type, amount_mc, occurred_at) VALUES (gen_random_uuid(), "
        ":mine_builder, :mine_account, :mine_grant, :other_lot, 'debit', 1000, now())",
        "23503",
        "fk_credit_ledger_entries_lot",
    ),
    "entry_on_foreign_effect": (
        "INSERT INTO credit_ledger_entries (id, builder_id, account_id, effect_id, "
        "lot_id, entry_type, amount_mc, occurred_at) VALUES (gen_random_uuid(), "
        ":mine_builder, :mine_account, :other_grant, :mine_lot, 'debit', 1000, now())",
        "23503",
        "fk_credit_ledger_entries_effect",
    ),
    "entry_on_foreign_account": (
        "INSERT INTO credit_ledger_entries (id, builder_id, account_id, effect_id, "
        "lot_id, entry_type, amount_mc, occurred_at) VALUES (gen_random_uuid(), "
        ":mine_builder, :other_account, :mine_grant, :mine_lot, 'debit', 1000, now())",
        "23503",
        "fk_credit_ledger_entries_account",
    ),
    "debt_on_foreign_lot": (
        "INSERT INTO credit_debts (id, builder_id, account_id, debt_source_kind, "
        "debt_source_effect_id, original_grant_lot_id, amount_mc) VALUES "
        "(gen_random_uuid(), :mine_builder, :mine_account, 'reversal', "
        ":mine_grant, :other_lot, 1000)",
        "23503",
        "fk_credit_debts_original_lot",
    ),
    "settlement_from_foreign_lot": (
        "WITH debt AS (INSERT INTO credit_debts (id, builder_id, account_id, "
        "debt_source_kind, debt_source_effect_id, amount_mc) VALUES "
        "(gen_random_uuid(), :mine_builder, :mine_account, 'consumption', "
        ":mine_grant, 1000) RETURNING id) "
        "INSERT INTO credit_debt_settlements (id, builder_id, debt_id, "
        "settling_grant_lot_id, amount_mc, effect_id) SELECT gen_random_uuid(), "
        ":mine_builder, debt.id, :other_lot, 1000, :mine_grant FROM debt",
        "23503",
        "fk_credit_debt_settlements_lot",
    ),
    "origin_from_foreign_entry": (
        "WITH debt AS (INSERT INTO credit_debts (id, builder_id, account_id, "
        "debt_source_kind, debt_source_effect_id, amount_mc) VALUES "
        "(gen_random_uuid(), :mine_builder, :mine_account, 'consumption', "
        ":mine_grant, 1000) RETURNING id) "
        "INSERT INTO credit_debt_origins (builder_id, debt_id, consumption_entry_id) "
        "SELECT :mine_builder, debt.id, (SELECT id FROM credit_ledger_entries "
        "WHERE builder_id = :other_builder LIMIT 1) FROM debt",
        "23503",
        "fk_credit_debt_origins_consumption",
    ),
    "binding_on_foreign_grant": (
        "INSERT INTO credit_enforcement_binding (id, builder_id, subscription_id, "
        "subscription_cycle_id, cycle_start, cycle_end, financial_revision, "
        "grant_effect_id, status) VALUES (gen_random_uuid(), :mine_builder, "
        "gen_random_uuid(), gen_random_uuid(), DATE '2026-10-01', "
        "DATE '2026-11-01', 1, :other_grant, 'active')",
        "23503",
        "fk_credit_enforcement_binding_grant_effect",
    ),
    "effect_without_account": (
        "INSERT INTO credit_effects (id, builder_id, effect_id, effect_kind) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), 'orphan-effect', 'grant')",
        "23503",
        "fk_credit_effects_account",
    ),
    "effect_for_foreign_tenant": (
        "INSERT INTO credit_effects (id, builder_id, tenant_id, effect_id, "
        "effect_kind) VALUES (gen_random_uuid(), :mine_builder, :other_tenant, "
        "'trace-foreign', 'trace')",
        "23000",
        "credit_effects row builder does not match its tenant's builder",
    ),
    "claim_for_foreign_tenant": (
        "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
        "principal_id, idempotency_key, interaction_id, payload_sha256, "
        "decision_version, mode) VALUES (gen_random_uuid(), :other_tenant, "
        ":mine_builder, 'events', 'api_key:x', 'idem-x', gen_random_uuid(), "
        f"'{_SHA}', 1, 'observational')",
        "23000",
        "credit_ingress_claims row builder does not match its tenant's builder",
    ),
    "claim_hiding_its_builder": (
        "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
        "principal_id, idempotency_key, interaction_id, payload_sha256, "
        "decision_version, mode) VALUES (gen_random_uuid(), :mine_tenant, NULL, "
        "'events', 'api_key:x', 'idem-null', gen_random_uuid(), "
        f"'{_SHA}', 1, 'observational')",
        "23000",
        "credit_ingress_claims row builder does not match its tenant's builder",
    ),
    "claim_moved_to_foreign_builder": (
        "WITH claim AS (INSERT INTO credit_ingress_claims (id, tenant_id, "
        "builder_id, source, principal_id, idempotency_key, interaction_id, "
        "payload_sha256, decision_version, mode) VALUES (gen_random_uuid(), "
        ":mine_tenant, :mine_builder, 'events', 'api_key:y', 'idem-moved', "
        f"gen_random_uuid(), '{_SHA}', 1, 'observational') RETURNING id) "
        "SELECT 1 FROM claim; "
        "UPDATE credit_ingress_claims SET builder_id = :other_builder "
        "WHERE tenant_id = :mine_tenant AND idempotency_key = 'idem-moved'",
        "23000",
        "credit_ingress_claims row builder does not match its tenant's builder",
    ),
    "claim_for_missing_tenant": (
        "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
        "principal_id, idempotency_key, interaction_id, payload_sha256, "
        "decision_version, mode) VALUES (gen_random_uuid(), gen_random_uuid(), "
        "NULL, 'events', 'api_key:x', 'idem-x', gen_random_uuid(), "
        f"'{_SHA}', 1, 'observational')",
        "23503",
        "credit_ingress_claims row names a tenant that does not exist",
    ),
    "observation_for_foreign_tenant": (
        "INSERT INTO credit_observational_effects (id, tenant_id, builder_id, "
        "effect_id, effect_kind, would_debit_mc) VALUES (gen_random_uuid(), "
        ":other_tenant, :mine_builder, 'trace-x', 'trace', 1000)",
        "23000",
        "credit_observational_effects row builder does not match",
    ),
    "outbox_for_missing_tenant": (
        "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), 'trace-x', "
        "CAST('{}' AS JSONB))",
        "23503",
        "fk_credit_response_outbox_tenant",
    ),
}


@pytest.mark.parametrize("case", sorted(_CROSS_BUILDER))
async def test_references_across_builders_fail_under_direct_sql(
    postgres_harness: _Harness, pair: _Pair, case: str
) -> None:
    statement, sqlstate, match = _CROSS_BUILDER[case]
    params = {
        "mine_builder": pair.mine.builder_id,
        "mine_account": pair.mine.account_id,
        "mine_grant": pair.mine.grant_effect_id,
        "mine_lot": pair.mine.lot_id,
        "mine_tenant": pair.mine.tenant_id,
        "other_builder": pair.other.builder_id,
        "other_account": pair.other.account_id,
        "other_grant": pair.other.grant_effect_id,
        "other_lot": pair.other.lot_id,
        "other_tenant": pair.other.tenant_id,
    }
    used = {
        name: value
        for name, value in params.items()
        if re.search(rf":{name}\b", statement)
    }
    with pytest.raises(DBAPIError, match=re.escape(match)) as info:
        async with postgres_harness.engine.begin() as connection:
            for part in statement.split("; "):
                await connection.execute(
                    text(part),
                    {k: v for k, v in used.items() if re.search(rf":{k}\b", part)},
                )
    assert _sqlstate(info.value) == sqlstate


# ---------------------------------------------------------------------------
# Concurrency.
# ---------------------------------------------------------------------------


async def _one_wins(
    harness: _Harness, statement: str, first: dict[str, Any], second: dict[str, Any]
) -> None:
    """The second insert waits on the unique boundary, then fails."""

    session_one = harness.session()
    session_two = harness.session()
    await session_one.begin()
    await session_two.begin()
    try:
        await session_one.execute(text(statement), first)

        async def insert_second() -> None:
            try:
                await session_two.execute(text(statement), second)
                await session_two.commit()
            except BaseException:
                await session_two.rollback()
                raise

        contender = asyncio.create_task(insert_second())
        done, _ = await asyncio.wait({contender}, timeout=_WAIT)
        assert not done, "concurrent insert did not wait on the unique boundary"
        await session_one.commit()
        with pytest.raises(IntegrityError) as info:
            await asyncio.wait_for(contender, timeout=_TIMEOUT)
        assert _sqlstate(info.value) == "23505"
    finally:
        for session in (session_one, session_two):
            if session.in_transaction():
                await session.rollback()
            await session.close()


async def test_concurrent_account_creation_yields_one_account(
    postgres_harness: _Harness,
) -> None:
    builder_id = uuid4()
    session_one = postgres_harness.session()
    session_two = postgres_harness.session()
    await session_one.begin()
    await session_two.begin()
    try:
        first = await CreditRepository(session_one).ensure_account(builder_id)

        async def ensure_second() -> UUID:
            account = await CreditRepository(session_two).ensure_account(builder_id)
            await session_two.commit()
            return account

        contender = asyncio.create_task(ensure_second())
        done, _ = await asyncio.wait({contender}, timeout=_WAIT)
        assert not done, "the second creator did not wait on the builder key"
        await session_one.commit()
        second = await asyncio.wait_for(contender, timeout=_TIMEOUT)
    finally:
        for session in (session_one, session_two):
            if session.in_transaction():
                await session.rollback()
            await session.close()
    assert first == second
    assert (
        await _value(
            postgres_harness,
            "SELECT count(*) FROM credit_accounts WHERE builder_id = :builder",
            builder=builder_id,
        )
        == 1
    )


async def test_concurrent_duplicates_create_one_row_each(
    postgres_harness: _Harness,
) -> None:
    seeded = await _seed(postgres_harness)
    base = {
        "builder": seeded.builder_id,
        "account": seeded.account_id,
        "grant": seeded.grant_effect_id,
    }
    subscription = uuid4()
    order = uuid4()
    lot = (
        "INSERT INTO credit_lots (id, builder_id, account_id, lot_kind, "
        "subscription_id, cycle_start, cycle_end, order_id, payment_id, paid_at, "
        "granted_mc, expires_at, grant_effect_id) VALUES (:id, :builder, :account, "
        ":kind, :subscription, :start, :end, :order, 'pay-c', DATE '2026-10-01', "
        "5000000, now(), :grant)"
    )
    monthly = {
        **base,
        "kind": "monthly",
        "subscription": subscription,
        "start": datetime(2026, 10, 1).date(),
        "end": datetime(2026, 11, 1).date(),
        "order": None,
    }
    topup = {
        **base,
        "kind": "topup",
        "subscription": None,
        "start": None,
        "end": None,
        "order": order,
    }
    await _one_wins(
        postgres_harness, lot, {**monthly, "id": uuid4()}, {**monthly, "id": uuid4()}
    )
    await _one_wins(
        postgres_harness, lot, {**topup, "id": uuid4()}, {**topup, "id": uuid4()}
    )
    effect = (
        "INSERT INTO credit_effects (id, builder_id, effect_id, effect_kind) "
        "VALUES (:id, :builder, :effect, 'trace')"
    )
    same_effect = {"builder": seeded.builder_id, "effect": f"concurrent-{uuid4()}"}
    await _one_wins(
        postgres_harness,
        effect,
        {**same_effect, "id": uuid4()},
        {**same_effect, "id": uuid4()},
    )
    inbox = (
        "INSERT INTO credit_fact_inbox (id, producer, event_id, schema_version, "
        "aggregate_kind, aggregate_id, financial_revision, fact_sha256, "
        "bundle_digest, schema_digest, outcome) VALUES (:id, "
        "'asaas-payment-gateway', :event, 'billing.fact.v1', 'subscription_cycle', "
        ":aggregate, :revision, :sha, :sha, :sha, 'accepted')"
    )
    event = uuid4()
    aggregate = uuid4()
    # The same fact twice, and two facts claiming one revision.
    await _one_wins(
        postgres_harness,
        inbox,
        {
            "id": uuid4(),
            "event": event,
            "aggregate": aggregate,
            "revision": 1,
            "sha": _SHA,
        },
        {
            "id": uuid4(),
            "event": event,
            "aggregate": uuid4(),
            "revision": 1,
            "sha": _SHA,
        },
    )
    await _one_wins(
        postgres_harness,
        inbox,
        {
            "id": uuid4(),
            "event": uuid4(),
            "aggregate": aggregate,
            "revision": 2,
            "sha": _SHA,
        },
        {
            "id": uuid4(),
            "event": uuid4(),
            "aggregate": aggregate,
            "revision": 2,
            "sha": _SHA,
        },
    )
    outbox = (
        "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload) "
        "VALUES (:id, :tenant, :effect, CAST('{}' AS JSONB))"
    )
    same_outbox = {"tenant": seeded.tenant_id, "effect": f"concurrent-{uuid4()}"}
    await _one_wins(
        postgres_harness,
        outbox,
        {**same_outbox, "id": uuid4()},
        {**same_outbox, "id": uuid4()},
    )
    claim = (
        "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
        "principal_id, idempotency_key, interaction_id, payload_sha256, "
        "decision_version, mode) VALUES (:id, :tenant, :builder, 'events', "
        "'api_key:c', 'idem-concurrent', :interaction, :sha, 1, 'observational')"
    )
    same_claim = {"tenant": seeded.tenant_id, "builder": seeded.builder_id, "sha": _SHA}
    await _one_wins(
        postgres_harness,
        claim,
        {**same_claim, "id": uuid4(), "interaction": uuid4()},
        {**same_claim, "id": uuid4(), "interaction": uuid4()},
    )
    counts = await _value(
        postgres_harness,
        "SELECT ARRAY[(SELECT count(*) FROM credit_lots WHERE subscription_id = :s), "
        "(SELECT count(*) FROM credit_lots WHERE order_id = :o), "
        "(SELECT count(*) FROM credit_fact_inbox WHERE aggregate_id = :a), "
        "(SELECT count(*) FROM credit_response_outbox WHERE tenant_id = :t), "
        "(SELECT count(*) FROM credit_ingress_claims WHERE tenant_id = :t)]",
        s=subscription,
        o=order,
        a=aggregate,
        t=seeded.tenant_id,
    )
    assert list(counts) == [1, 1, 2, 1, 1]


async def test_the_account_lock_serializes_one_builder_only(
    postgres_harness: _Harness,
) -> None:
    mine = await _seed(postgres_harness)
    other = await _seed(postgres_harness)
    holder = postgres_harness.session()
    await holder.begin()
    try:
        assert await CreditRepository(holder).lock_account(mine.builder_id) is not None

        async def lock(builder_id: UUID) -> UUID:
            async with postgres_harness.session() as session, session.begin():
                account = await CreditRepository(session).lock_account(builder_id)
                assert account is not None
                return account.id

        same = asyncio.create_task(lock(mine.builder_id))
        assert await asyncio.wait_for(lock(other.builder_id), timeout=_TIMEOUT) == (
            other.account_id
        )
        done, _ = await asyncio.wait({same}, timeout=_WAIT)
        assert not done, "a second writer of the same builder did not wait"
        await holder.commit()
        assert await asyncio.wait_for(same, timeout=_TIMEOUT) == mine.account_id
    finally:
        if holder.in_transaction():
            await holder.rollback()
        await holder.close()


async def test_lock_lots_holds_the_builders_lots_in_id_order(
    postgres_harness: _Harness,
) -> None:
    mine = await _seed(postgres_harness)
    other = await _seed(postgres_harness)
    extra = [
        await _topup_lot(
            postgres_harness, mine.builder_id, mine.account_id, mine.grant_effect_id
        )
        for _ in range(3)
    ]
    holder = postgres_harness.session()
    await holder.begin()
    try:
        lots = await CreditRepository(holder).lock_lots(mine.builder_id)
        assert [lot.id for lot in lots] == sorted([mine.lot_id, *extra])
        assert other.lot_id not in {lot.id for lot in lots}

        async def lock(builder_id: UUID) -> list[UUID]:
            async with postgres_harness.session() as session, session.begin():
                locked = await CreditRepository(session).lock_lots(builder_id)
                return [lot.id for lot in locked]

        same = asyncio.create_task(lock(mine.builder_id))
        # Another builder's lots are not held.
        assert await asyncio.wait_for(lock(other.builder_id), timeout=_TIMEOUT) == [
            other.lot_id
        ]
        done, _ = await asyncio.wait({same}, timeout=_WAIT)
        assert not done, "a second writer locked lots the first one holds"
        await holder.commit()
        assert await asyncio.wait_for(same, timeout=_TIMEOUT) == sorted(
            [mine.lot_id, *extra]
        )
    finally:
        if holder.in_transaction():
            await holder.rollback()
        await holder.close()


# ---------------------------------------------------------------------------
# Derived balance.
# ---------------------------------------------------------------------------


async def test_derived_balance_of_a_fixed_history(postgres_harness: _Harness) -> None:
    seeded = await _seed(postgres_harness)  # top-up lot granted 500_000
    monthly_grant = f"cycle-grant-{uuid4().hex}"
    await _effect(postgres_harness, seeded.builder_id, monthly_grant)
    monthly = uuid4()
    await _run(
        postgres_harness,
        "INSERT INTO credit_lots (id, builder_id, account_id, lot_kind, "
        "subscription_id, cycle_start, cycle_end, payment_id, paid_at, granted_mc, "
        "expires_at, grant_effect_id) VALUES (:id, :builder, :account, 'monthly', "
        ":subscription, DATE '2026-10-01', DATE '2026-11-01', 'pay-m', "
        "DATE '2026-10-01', 5000000, TIMESTAMPTZ '2026-11-01 03:00:00+00', :effect)",
        id=monthly,
        builder=seeded.builder_id,
        account=seeded.account_id,
        subscription=uuid4(),
        effect=monthly_grant,
    )
    steps = (
        ("grant", 5_000_000, monthly),
        ("debit", 1_000, monthly),
        ("debit", 1_000, monthly),
        ("reversal", 200_000, seeded.lot_id),
        ("debt", 3_000, None),
        ("debt_settlement", 1_000, monthly),
        ("debt_release", 500, None),
        ("settlement_release", 1_000, monthly),
        ("lot_regrant", 100_000, seeded.lot_id),
        ("expiry", 4_998_000, monthly),
    )
    for entry_type, amount, lot in steps:
        await _entry(postgres_harness, seeded, entry_type, amount, lot_id=lot)
    async with postgres_harness.session() as session:
        balance = await CreditRepository(session).derived_balance(seeded.builder_id)
    assert balance.lots == {
        seeded.lot_id: 500_000 - 200_000 + 100_000,
        monthly: 5_000_000 - 2_000 - 1_000 + 1_000 - 4_998_000,
    }
    assert balance.debt_mc == 3_000 - 1_000 - 500
    assert balance.lots_total_mc == 400_000


async def test_derived_balance_equals_the_reference_fold(
    postgres_harness: _Harness,
) -> None:
    """Seeded pseudo-random histories of two builders: the SQL aggregate and
    :func:`fold_ledger` agree, and one builder never sees the other."""

    generator = random.Random(20261026)
    builders = [await _seed(postgres_harness) for _ in range(2)]
    histories: dict[UUID, list[LedgerMovement]] = {}
    for seeded in builders:
        lots = [seeded.lot_id] + [
            await _topup_lot(
                postgres_harness,
                seeded.builder_id,
                seeded.account_id,
                seeded.grant_effect_id,
            )
            for _ in range(2)
        ]
        movements = [LedgerMovement("grant", 500_000, seeded.lot_id)]
        for _ in range(40):
            entry_type = generator.choice(
                (
                    "grant",
                    "debit",
                    "expiry",
                    "reversal",
                    "debt",
                    "debt_settlement",
                    "debt_release",
                    "settlement_release",
                    "lot_regrant",
                )
            )
            amount = generator.randint(1, 50_000)
            lot: UUID | None = generator.choice(lots)
            if entry_type in ("debt", "debt_release") and generator.random() < 0.5:
                lot = None
            await _entry(postgres_harness, seeded, entry_type, amount, lot_id=lot)
            movements.append(LedgerMovement(entry_type, amount, lot))
        histories[seeded.builder_id] = movements
    async with postgres_harness.session() as session:
        repository = CreditRepository(session)
        for seeded in builders:
            derived = await repository.derived_balance(seeded.builder_id)
            reference = fold_ledger(histories[seeded.builder_id])
            assert derived == reference
            assert set(derived.lots) <= {
                movement.lot_id for movement in histories[seeded.builder_id]
            }
        assert (await repository.derived_balance(uuid4())).lots == {}


# ---------------------------------------------------------------------------
# Scoped reads.
# ---------------------------------------------------------------------------


async def test_reads_are_scoped_by_builder_and_tenant(
    postgres_harness: _Harness,
) -> None:
    mine = await _seed(postgres_harness)
    other = await _seed(postgres_harness)
    standalone = await _tenant(postgres_harness, None)
    await _claim(postgres_harness, mine.tenant_id, mine.builder_id, key="idem-scope")
    await _claim(
        postgres_harness,
        mine.tenant_id,
        mine.builder_id,
        key=None,
        source="meta",
        message="wamid-scope",
        principal=f"meta:{mine.tenant_id}",
    )
    effect = f"trace:{mine.tenant_id}:scope"
    await _observational(postgres_harness, mine.tenant_id, mine.builder_id, effect)
    async with postgres_harness.session() as session:
        repository = CreditRepository(session)
        scope = await repository.tenant_scope(mine.tenant_id)
        assert scope is not None and scope.builder_id == mine.builder_id
        alone = await repository.tenant_scope(standalone)
        assert alone is not None and alone.builder_id is None
        assert await repository.tenant_scope(uuid4()) is None
        principal = f"api_key:{mine.tenant_id}"
        assert await repository.get_idempotency_claim(
            mine.tenant_id, principal_id=principal, idempotency_key="idem-scope"
        )
        assert (
            await repository.get_idempotency_claim(
                other.tenant_id, principal_id=principal, idempotency_key="idem-scope"
            )
            is None
        )
        assert await repository.get_provider_claim(
            mine.tenant_id, source="meta", provider_message_id="wamid-scope"
        )
        assert (
            await repository.get_provider_claim(
                other.tenant_id, source="meta", provider_message_id="wamid-scope"
            )
            is None
        )
        assert await repository.get_observational_effect(mine.tenant_id, effect)
        assert (
            await repository.get_observational_effect(other.tenant_id, effect) is None
        )
        assert await repository.get_effect(mine.builder_id, mine.grant_effect_id)
        assert (
            await repository.get_effect(other.builder_id, mine.grant_effect_id) is None
        )
        watermark = await repository.inbox_high_watermark()
    highest = await _value(
        postgres_harness, "SELECT coalesce(max(inbox_seq), 0) FROM credit_fact_inbox"
    )
    assert watermark == highest


# ---------------------------------------------------------------------------
# Clinic and builder delete, reparent: as on main, with credit rows present.
# ---------------------------------------------------------------------------


def _super_admin() -> TenantContext:
    return TenantContext(uuid4(), role="super_admin", authentication_mode="jwt")


async def _credit_rows(harness: _Harness, seeded: _Builder) -> dict[str, int]:
    tables = {
        "claims": "SELECT count(*) FROM credit_ingress_claims WHERE tenant_id = :t",
        "outbox": "SELECT count(*) FROM credit_response_outbox WHERE tenant_id = :t",
        "observed": (
            "SELECT count(*) FROM credit_observational_effects WHERE tenant_id = :t"
        ),
        "effects": "SELECT count(*) FROM credit_effects WHERE builder_id = :b",
        "ledger": "SELECT count(*) FROM credit_ledger_entries WHERE builder_id = :b",
        "lots": "SELECT count(*) FROM credit_lots WHERE builder_id = :b",
        "accounts": "SELECT count(*) FROM credit_accounts WHERE builder_id = :b",
    }
    return {
        name: int(await _value(harness, sql, t=seeded.tenant_id, b=seeded.builder_id))
        for name, sql in tables.items()
    }


async def _with_tenant_rows(harness: _Harness) -> _Builder:
    seeded = await _seed(harness)
    await _history(harness, seeded)
    await _claim(harness, seeded.tenant_id, seeded.builder_id, key="idem-delete")
    await _outbox(harness, seeded.tenant_id, f"trace:{seeded.tenant_id}:delete")
    return seeded


async def test_clinic_delete_keeps_history_and_cascades_claims_and_outbox(
    postgres_harness: _Harness,
) -> None:
    seeded = await _with_tenant_rows(postgres_harness)
    before = await _credit_rows(postgres_harness, seeded)
    assert before["claims"] == before["outbox"] == 1
    async with postgres_harness.session() as session:
        response = await admin_routes.delete_clinic(
            str(seeded.tenant_id), tenant=_super_admin(), session=session
        )
    assert response == {"ok": True}
    after = await _credit_rows(postgres_harness, seeded)
    assert after == {**before, "claims": 0, "outbox": 0}
    async with postgres_harness.session() as session:
        assert await session.get(Tenant, seeded.tenant_id) is None


async def test_builder_delete_detaches_clinics_and_keeps_history(
    postgres_harness: _Harness,
) -> None:
    seeded = await _with_tenant_rows(postgres_harness)
    before = await _credit_rows(postgres_harness, seeded)
    async with postgres_harness.session() as session:
        response = await admin_routes.delete_builder(
            str(seeded.builder_id), tenant=_super_admin(), session=session
        )
    assert response == {"ok": True}
    async with postgres_harness.session() as session:
        assert await session.get(Builder, seeded.builder_id) is None
        tenant = await session.get(Tenant, seeded.tenant_id)
        assert tenant is not None and tenant.builder_id is None
    # The clinic survives (now standalone): its claims and outbox stay too.
    assert await _credit_rows(postgres_harness, seeded) == before


async def test_reparent_works_with_credit_rows_present(
    postgres_harness: _Harness,
) -> None:
    seeded = await _with_tenant_rows(postgres_harness)
    destination = await _builder_row(postgres_harness)
    before = await _credit_rows(postgres_harness, seeded)
    async with postgres_harness.session() as session:
        await transition_tenant_media_owner(
            session,
            MediaOwnerTransition(
                tenant_id=seeded.tenant_id,
                expected_builder_id=seeded.builder_id,
                destination_builder_id=destination,
            ),
        )
        await session.commit()
    async with postgres_harness.session() as session:
        tenant = await session.get(Tenant, seeded.tenant_id)
        assert tenant is not None and tenant.builder_id == destination
    assert await _credit_rows(postgres_harness, seeded) == before
    # Detach (destination NULL) works the same way.
    async with postgres_harness.session() as session:
        await transition_tenant_media_owner(
            session,
            MediaOwnerTransition(
                tenant_id=seeded.tenant_id,
                expected_builder_id=destination,
                destination_builder_id=None,
            ),
        )
        await session.commit()
    assert await _credit_rows(postgres_harness, seeded) == before


# ---------------------------------------------------------------------------
# Lock order step 1: the tenant row FOR SHARE before its claims and effects.
# ---------------------------------------------------------------------------

_TOUCH_CLAIM = (
    "UPDATE credit_ingress_claims SET mode = 'enforced' WHERE tenant_id = :tenant"
)
_EFFECT_OF_TENANT = (
    "INSERT INTO credit_effects (id, builder_id, tenant_id, effect_id, effect_kind) "
    "VALUES (gen_random_uuid(), :builder, :tenant, :effect, 'trace')"
)


async def _claimed_tenant(harness: _Harness) -> _Builder:
    seeded = await _seed(harness)
    await _claim(harness, seeded.tenant_id, seeded.builder_id, key="idem-order")
    return seeded


async def _close(*sessions: AsyncSession) -> None:
    for session in sessions:
        if session.in_transaction():
            await session.rollback()
        await session.close()


async def test_touching_a_claim_before_the_tenant_lock_deadlocks_a_clinic_delete(
    postgres_harness: _Harness,
) -> None:
    """The hazard step 1 exists for: claim row first, then an effect insert
    whose trigger wants the tenant row, against ``DELETE FROM tenants`` (tenant
    row first, then CASCADE into the claim).  PostgreSQL aborts one side."""

    seeded = await _claimed_tenant(postgres_harness)
    params = {"tenant": seeded.tenant_id, "builder": seeded.builder_id}
    credit, deleter = postgres_harness.session(), postgres_harness.session()
    await credit.begin()
    await deleter.begin()
    try:
        await credit.execute(text(_TOUCH_CLAIM), params)

        async def delete_tenant() -> None:
            await deleter.execute(
                text("DELETE FROM tenants WHERE id = :tenant"), params
            )
            await deleter.commit()

        async def insert_effect() -> None:
            await credit.execute(
                text(_EFFECT_OF_TENANT), {**params, "effect": f"trace-{uuid4()}"}
            )
            await credit.commit()

        deleting = asyncio.create_task(delete_tenant())
        done, _ = await asyncio.wait({deleting}, timeout=_WAIT)
        assert not done, "the delete did not wait on the locked claim"
        results = await asyncio.wait_for(
            asyncio.gather(deleting, insert_effect(), return_exceptions=True),
            timeout=_TIMEOUT,
        )
    finally:
        await _close(credit, deleter)
    deadlocks = [
        result
        for result in results
        if isinstance(result, DBAPIError) and _sqlstate(result) == "40P01"
    ]
    assert len(deadlocks) == 1, results


async def test_the_tenant_lock_first_lets_a_clinic_delete_wait_instead(
    postgres_harness: _Harness,
) -> None:
    seeded = await _claimed_tenant(postgres_harness)
    params = {"tenant": seeded.tenant_id, "builder": seeded.builder_id}
    effect = f"trace-{uuid4()}"
    credit, deleter = postgres_harness.session(), postgres_harness.session()
    await credit.begin()
    await deleter.begin()
    try:
        scope = await CreditRepository(credit).tenant_scope(seeded.tenant_id, lock=True)
        assert scope is not None and scope.builder_id == seeded.builder_id
        await credit.execute(text(_TOUCH_CLAIM), params)

        async def delete_tenant() -> None:
            await deleter.execute(
                text("DELETE FROM tenants WHERE id = :tenant"), params
            )
            await deleter.commit()

        deleting = asyncio.create_task(delete_tenant())
        done, _ = await asyncio.wait({deleting}, timeout=_WAIT)
        assert not done, "the delete did not wait on the tenant lock"
        await credit.execute(text(_EFFECT_OF_TENANT), {**params, "effect": effect})
        await credit.commit()
        await asyncio.wait_for(deleting, timeout=_TIMEOUT)
    finally:
        await _close(credit, deleter)
    counts = await _value(
        postgres_harness,
        "SELECT ARRAY[(SELECT count(*) FROM tenants WHERE id = :tenant), "
        "(SELECT count(*) FROM credit_ingress_claims WHERE tenant_id = :tenant), "
        "(SELECT count(*) FROM credit_effects WHERE effect_id = :effect)]",
        tenant=seeded.tenant_id,
        effect=effect,
    )
    # The clinic and its claim are gone; the committed effect (history) stays.
    assert list(counts) == [0, 0, 1]


async def test_the_tenant_lock_waits_for_a_delete_and_then_finds_no_tenant(
    postgres_harness: _Harness,
) -> None:
    seeded = await _claimed_tenant(postgres_harness)
    credit, deleter = postgres_harness.session(), postgres_harness.session()
    await deleter.begin()
    try:
        await deleter.execute(
            text("DELETE FROM tenants WHERE id = :tenant"),
            {"tenant": seeded.tenant_id},
        )

        async def scope() -> Any:
            async with credit.begin():
                return await CreditRepository(credit).tenant_scope(
                    seeded.tenant_id, lock=True
                )

        locking = asyncio.create_task(scope())
        done, _ = await asyncio.wait({locking}, timeout=_WAIT)
        assert not done, "the tenant lock did not wait for the delete"
        await deleter.commit()
        assert await asyncio.wait_for(locking, timeout=_TIMEOUT) is None
    finally:
        await _close(credit, deleter)


# ---------------------------------------------------------------------------
# The ack repository against the untouched ASAAS reconciler.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FactHarness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


async def _inbox_row(harness: _Harness, event_id: UUID) -> UUID:
    inbox_id = uuid4()
    await _run(
        harness,
        "INSERT INTO credit_fact_inbox (id, producer, event_id, schema_version, "
        "aggregate_kind, aggregate_id, financial_revision, fact_sha256, "
        "bundle_digest, schema_digest, outcome) VALUES (:id, "
        "'asaas-payment-gateway', :event, 'billing.fact.v1', 'topup_order', "
        ":aggregate, 1, :sha, :sha, :sha, 'accepted')",
        id=inbox_id,
        event=event_id,
        aggregate=uuid4(),
        sha=_SHA,
    )
    return inbox_id


@pytest.mark.parametrize("outcome", ["accepted", "rejected"])
async def test_the_ack_is_read_by_the_untouched_asaas_reconciler(
    postgres_harness: _Harness, outcome: str
) -> None:
    outbox_id = await _seed_fact(
        _FactHarness(postgres_harness.engine, postgres_harness.sessions)  # type: ignore[arg-type]
    )
    inbox_id = await _inbox_row(postgres_harness, outbox_id)
    effect_ids = ["topup:grant"] if outcome == "accepted" else []
    async with postgres_harness.session() as session, session.begin():
        outbox = await session.get(BillingOutboxEvent, outbox_id)
        assert outbox is not None
        acks = CreditFactAckRepository(session)
        handoff = await acks.resolve_handoff(outbox_id)
        assert handoff is not None and handoff.outbox_event_id == outbox_id
        assert await acks.resolve_handoff(uuid4()) is None
        ack = await acks.insert_ack(
            handoff=handoff,
            outcome=outcome,
            inbox_event_id=inbox_id,
            fact_sha256=payload_digest(outbox.payload),
            bundle_digest=billing_fact_bundle_digest(),
            effect_ids=effect_ids,
            acknowledged_at=datetime.now(timezone.utc),
        )
        assert ack.deployment_digest == handoff.deployment_digest
        assert ack.effects_digest == (
            effects_digest(effect_ids) if effect_ids else EMPTY_EFFECTS_DIGEST
        )
    async with postgres_harness.session() as session, session.begin():
        await BillingFactReconciler(session).reconcile()
    async with postgres_harness.session() as session:
        delivery = (
            await session.execute(
                select(BillingFactDelivery).where(
                    BillingFactDelivery.outbox_event_id == outbox_id
                )
            )
        ).scalar_one()
        stored = await CreditFactAckRepository(session).get_ack(outbox_id)
    assert stored is not None and stored.inbox_event_id == inbox_id
    assert delivery.state == ("acknowledged" if outcome == "accepted" else "blocked")
    await _refused(
        postgres_harness,
        "UPDATE billing_fact_acks SET outcome = outcome WHERE outbox_event_id = :id",
        "23000",
        "billing_fact_acks is append-only",
        id=outbox_id,
    )


async def test_the_ack_repository_refuses_effects_on_a_rejected_fact(
    postgres_harness: _Harness,
) -> None:
    outbox_id = await _seed_fact(
        _FactHarness(postgres_harness.engine, postgres_harness.sessions)  # type: ignore[arg-type]
    )
    async with postgres_harness.session() as session:
        acks = CreditFactAckRepository(session)
        handoff = await acks.resolve_handoff(outbox_id)
        assert handoff is not None
        common = {
            "handoff": handoff,
            "inbox_event_id": uuid4(),
            "fact_sha256": _SHA,
            "bundle_digest": _SHA,
            "acknowledged_at": datetime.now(timezone.utc),
        }
        with pytest.raises(ValueError, match="zero effects"):
            await acks.insert_ack(outcome="rejected", effect_ids=["x"], **common)
        with pytest.raises(ValueError, match="outcome"):
            await acks.insert_ack(outcome="ignored", effect_ids=[], **common)
        with pytest.raises(ValueError, match="fact_sha256"):
            await acks.insert_ack(
                outcome="accepted", effect_ids=[], **{**common, "fact_sha256": "A" * 64}
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            await acks.insert_ack(
                outcome="accepted",
                effect_ids=[],
                **{**common, "acknowledged_at": datetime.now() - timedelta(0)},
            )
        assert await session.scalar(
            select(func.count()).select_from(BillingFactDelivery)
        )
        await session.rollback()
    assert (
        await _value(
            postgres_harness,
            "SELECT count(*) FROM billing_fact_acks WHERE outbox_event_id = :id",
            id=outbox_id,
        )
        == 0
    )
