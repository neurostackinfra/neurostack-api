"""Metadata and caller-owned persistence contracts for ASAAS billing."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import configure_mappers

import src.models as model_exports
from src.models.base import Base
from src.models.billing import (
    BillingCheckoutSession,
    BillingMigration,
    BillingMigrationReceipt,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingPaymentProjection,
    BillingRolloutControl,
    BillingRolloutReceipt,
    BillingRosterControl,
    BillingSubscription,
    BillingTopupCapabilityReceipt,
    BillingTopupOrder,
    BillingWebhookDelivery,
)
from src.models.builder import Builder
from src.models.transaction import Transaction
from src.repositories.billing import BillingRepository, billing_advisory_key
from src.repositories.database import _patch_jsonb_for_sqlite

_ROOT = Path(__file__).resolve().parents[2]

_BILLING_MODELS = (
    BillingSubscription,
    BillingMigration,
    BillingCheckoutSession,
    BillingMigrationReceipt,
    BillingWebhookDelivery,
    BillingPaymentProjection,
    BillingPaymentEffect,
    BillingRolloutControl,
    BillingRolloutReceipt,
    BillingRosterControl,
    BillingTopupCapabilityReceipt,
    BillingTopupOrder,
    BillingOutboxEvent,
)


def _constraint(table: object, name: str) -> object:
    constraints = getattr(table, "constraints")
    matches = [constraint for constraint in constraints if constraint.name == name]
    assert len(matches) == 1
    return matches[0]


def _check_sql(model: type[object], name: str) -> str:
    constraint = _constraint(model.__table__, name)
    assert isinstance(constraint, CheckConstraint)
    return str(constraint.sqltext)


def _unique_columns(model: type[object], name: str) -> tuple[str, ...]:
    constraint = _constraint(model.__table__, name)
    assert isinstance(constraint, UniqueConstraint)
    return tuple(column.name for column in constraint.columns)


def _fk_columns(
    model: type[object], name: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    constraint = _constraint(model.__table__, name)
    assert isinstance(constraint, ForeignKeyConstraint)
    local = tuple(column.name for column in constraint.columns)
    remote = tuple(element.target_fullname for element in constraint.elements)
    return local, remote


def test_all_billing_models_are_exported_and_mappers_are_unambiguous() -> None:
    configure_mappers()
    for model in _BILLING_MODELS:
        assert getattr(model_exports, model.__name__) is model


def test_group_zero_legacy_binding_is_physical_and_provider_unique() -> None:
    columns = BillingSubscription.__table__.columns
    assert {
        "provider",
        "external_subscription_id",
        "is_active",
        "builder_id",
        "plan",
        "billing_cycle",
        "coupon_code",
        "roster_revision",
    }.issubset(columns.keys())
    assert _unique_columns(
        BillingSubscription,
        "uq_billing_subscriptions_provider_external",
    ) == ("provider", "external_subscription_id")
    external_check = _check_sql(
        BillingSubscription,
        "ck_billing_subscriptions_external_id_length",
    )
    assert "A-Za-z0-9._:-" in external_check
    assert "roster_revision > 0" in _check_sql(
        BillingSubscription,
        "ck_billing_subscriptions_roster_revision",
    )


def test_asaas_target_snapshot_stays_separate_until_roster_flip() -> None:
    migration_columns = BillingMigration.__table__.columns
    assert {
        "target_provider",
        "target_external_subscription_id",
        "target_plan",
        "target_billing_cycle",
        "target_coupon_code",
        "target_snapshot",
        "source_roster_revision",
    }.issubset(migration_columns.keys())
    allowlist = _check_sql(
        BillingMigration,
        "ck_billing_migrations_snapshot_allowlist",
    )
    assert "billing_migration_snapshot_is_valid" in allowlist
    assert "customer" not in allowlist
    assert "email" not in allowlist


def test_legacy_cycle_and_unknown_provider_saga_are_representable() -> None:
    provider = BillingSubscription.__table__.columns.provider
    assert provider.nullable is True
    cycle = _check_sql(BillingSubscription, "ck_billing_subscriptions_cycle")
    assert "provider = 'abacatepay'" in cycle
    assert "'annual'" in cycle
    assert "provider = 'asaas' AND billing_cycle = 'monthly'" in cycle
    unknown = _check_sql(
        BillingSubscription,
        "ck_billing_subscriptions_unknown_provider",
    )
    assert "is_active = false" in unknown
    assert "legacy_cancel_pending" in unknown
    assert "provider IS NOT NULL AND status NOT IN" in unknown
    statuses = _check_sql(BillingSubscription, "ck_billing_subscriptions_status")
    assert "active_linked" in statuses
    assert "readback_required" in statuses
    assert "non_billable_pending_readback" in statuses
    active_status = _check_sql(
        BillingSubscription, "ck_billing_subscriptions_active_status"
    )
    assert "'active_linked', 'readback_required'" in active_status
    assert "is_active = false" in active_status


def test_subscription_checks_allow_sqlite_legacy_insert() -> None:
    engine = create_engine("sqlite:///:memory:")
    Builder.__table__.create(engine)
    BillingSubscription.__table__.create(engine)
    builder_id = uuid4()
    subscription_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            Builder.__table__.insert(),
            {"id": builder_id, "name": "SQLite Builder"},
        )
        connection.execute(
            BillingSubscription.__table__.insert(),
            {
                "id": subscription_id,
                "builder_id": builder_id,
                "provider": "abacatepay",
                "external_subscription_id": "legacy-annual",
                "is_active": True,
                "plan": "start",
                "billing_cycle": "annual",
                "status": "active",
                "roster_revision": 1,
            },
        )
    engine.dispose()


def test_checkout_session_has_one_live_attempt_per_builder_intent() -> None:
    columns = BillingCheckoutSession.__table__.columns
    assert "external_checkout_id" in columns
    assert "resulting_external_subscription_id" in columns
    index = next(
        item
        for item in BillingCheckoutSession.__table__.indexes
        if item.name == "uq_billing_checkout_sessions_live_intent"
    )
    assert isinstance(index, Index)
    assert index.unique is True
    assert tuple(column.name for column in index.columns) == (
        "builder_id",
        "intent_kind",
        "intent_id",
    )
    predicate = str(index.dialect_options["postgresql"]["where"])
    assert "reconciliation_required" in predicate
    assert "completed" not in predicate
    attempt_check = _check_sql(
        BillingCheckoutSession,
        "ck_billing_checkout_sessions_attempt_amount",
    )
    assert "reconciliation_attempts >= 0" in attempt_check


def test_three_idempotency_namespaces_are_independent() -> None:
    assert _unique_columns(
        BillingWebhookDelivery,
        "uq_billing_webhook_deliveries_event",
    ) == ("provider", "event_id")
    assert _unique_columns(
        BillingPaymentProjection,
        "uq_billing_payment_projections_payment",
    ) == ("provider", "payment_id")
    assert _unique_columns(
        BillingPaymentEffect,
        "uq_billing_payment_effects_idempotency",
    ) == ("provider", "payment_id", "effect_kind", "source_revision")


@pytest.mark.parametrize(
    ("model", "constraint_name", "local", "remote"),
    [
        (
            BillingMigration,
            "fk_billing_migrations_builder_subscription",
            ("builder_id", "subscription_id"),
            (
                "billing_subscriptions.builder_id",
                "billing_subscriptions.id",
            ),
        ),
        (
            BillingCheckoutSession,
            "fk_billing_checkout_sessions_builder_migration",
            ("builder_id", "migration_id"),
            ("billing_migrations.builder_id", "billing_migrations.id"),
        ),
        (
            BillingCheckoutSession,
            "fk_billing_checkout_sessions_result_subscription",
            ("builder_id", "provider", "resulting_external_subscription_id"),
            (
                "billing_subscriptions.builder_id",
                "billing_subscriptions.provider",
                "billing_subscriptions.external_subscription_id",
            ),
        ),
        (
            BillingPaymentEffect,
            "fk_billing_payment_effects_builder_projection",
            ("builder_id", "projection_id", "provider", "payment_id"),
            (
                "billing_payment_projections.builder_id",
                "billing_payment_projections.id",
                "billing_payment_projections.provider",
                "billing_payment_projections.payment_id",
            ),
        ),
        (
            BillingOutboxEvent,
            "fk_billing_outbox_builder_topup",
            ("builder_id", "topup_order_id"),
            ("billing_topup_orders.builder_id", "billing_topup_orders.id"),
        ),
    ],
)
def test_builder_scope_is_part_of_cross_aggregate_foreign_keys(
    model: type[object],
    constraint_name: str,
    local: tuple[str, ...],
    remote: tuple[str, ...],
) -> None:
    assert _fk_columns(model, constraint_name) == (local, remote)


def test_financial_checks_use_integer_cents_and_cumulative_bounds() -> None:
    projection_totals = _check_sql(
        BillingPaymentProjection,
        "ck_billing_payment_projections_totals",
    )
    assert "restored_cents <= reversed_cents" in projection_totals
    assert "reversed_cents <= settled_cents" in projection_totals
    assert "settled_cents <= amount_cents" in projection_totals
    topup_totals = _check_sql(
        BillingTopupOrder,
        "ck_billing_topup_orders_financial_totals",
    )
    assert "settled_cents <= amount_cents" in topup_totals


def test_external_envelopes_have_only_minimal_sanitized_columns() -> None:
    delivery_columns = set(BillingWebhookDelivery.__table__.columns.keys())
    assert {
        "provider",
        "event_id",
        "event_name",
        "resource_kind",
        "resource_id",
        "body_bytes",
        "body_sha256",
    }.issubset(delivery_columns)
    assert not delivery_columns.intersection(
        {"payload", "raw_payload", "headers", "email", "customer", "document"}
    )
    event_fields = _check_sql(
        BillingWebhookDelivery,
        "ck_billing_webhook_deliveries_event_fields",
    )
    assert "A-Z0-9_" in event_fields


def test_transaction_keeps_legacy_external_id_semantics() -> None:
    assert Transaction.__table__.columns.external_id.unique is not True
    assert not any(
        isinstance(constraint, UniqueConstraint)
        and tuple(column.name for column in constraint.columns) == ("external_id",)
        for constraint in Transaction.__table__.constraints
    )
    assert _unique_columns(
        Transaction,
        "uq_transactions_provider_payment",
    ) == ("provider", "provider_payment_id")
    assert _fk_columns(Transaction, "fk_transactions_billing_effect_identity") == (
        (
            "builder_id",
            "billing_payment_effect_id",
            "provider",
            "provider_payment_id",
        ),
        (
            "billing_payment_effects.builder_id",
            "billing_payment_effects.id",
            "billing_payment_effects.provider",
            "billing_payment_effects.payment_id",
        ),
    )
    identity = _check_sql(Transaction, "ck_transactions_billing_effect_identity")
    assert "billing_payment_effect_id IS NULL AND provider IS NULL" in identity
    assert "billing_payment_effect_id IS NOT NULL" in identity


def test_rollout_and_roster_have_independent_revision_fences() -> None:
    rollout_columns = BillingRolloutControl.__table__.columns
    assert "rollout_revision" in rollout_columns
    assert "topup_revision" in rollout_columns
    assert "topup_commercial_enabled" in rollout_columns
    assert "rollout_receipt_id" in rollout_columns
    assert "topup_receipt_id" in rollout_columns
    assert {
        "canary_checkout_count",
        "canary_settlement_count",
        "canary_amount_reserved_cents",
        "canary_subscription_checkout_id",
        "canary_topup_checkout_id",
        "canary_subscription_effect_id",
        "canary_topup_effect_id",
    }.issubset(rollout_columns.keys())
    roster_columns = BillingRosterControl.__table__.columns
    assert "billing_roster_revision" in roster_columns
    assert "singleton" in roster_columns
    assert "environment" not in roster_columns


def test_sqlite_create_all_skips_postgresql_canary_aggregate_check() -> None:
    exact = _constraint(
        BillingCheckoutSession.__table__,
        "ck_billing_checkout_sessions_canary_exact",
    )
    assert isinstance(exact, CheckConstraint)
    assert "jsonb_build_object" in str(exact.sqltext)

    _patch_jsonb_for_sqlite()
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            ddl = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'billing_checkout_sessions'"
            ).scalar_one()
    finally:
        engine.dispose()

    assert "ck_billing_checkout_sessions_canary_exact" not in ddl


def test_alias_identifiers_are_bonded_to_canonical_targets() -> None:
    checkout_target = _check_sql(
        BillingCheckoutSession, "ck_billing_checkout_sessions_intent_target"
    )
    assert "intent_id = migration_id" in checkout_target
    assert "intent_id = topup_order_id" in checkout_target
    assert _unique_columns(
        BillingPaymentProjection,
        "uq_billing_payment_projections_identity",
    ) == ("builder_id", "id", "provider", "payment_id")
    assert _unique_columns(
        BillingPaymentEffect,
        "uq_billing_payment_effects_identity",
    ) == ("builder_id", "id", "provider", "payment_id")
    outbox_target = _check_sql(BillingOutboxEvent, "ck_billing_outbox_aggregate_target")
    assert "aggregate_id = subscription_id" in outbox_target
    assert "aggregate_id = topup_order_id" in outbox_target


def test_snapshots_are_recursively_closed_and_capability_consumption_is_unique() -> (
    None
):
    for model, name in (
        (BillingTopupOrder, "ck_billing_topup_orders_snapshot_allowlist"),
        (BillingCheckoutSession, "ck_billing_checkout_sessions_snapshot_allowlist"),
    ):
        assert "billing_snapshot_has_no_pii" in _check_sql(model, name)
    migration_snapshot = _check_sql(
        BillingMigration, "ck_billing_migrations_snapshot_allowlist"
    )
    assert "billing_migration_snapshot_is_valid" in migration_snapshot
    assert "target_plan" in migration_snapshot
    assert "target_billing_cycle" in migration_snapshot
    assert "target_coupon_code" in migration_snapshot
    migration = (
        _ROOT / "alembic/versions/20261011_000003_026_asaas_billing.py"
    ).read_text()
    assert "'line_financial_state'" in migration
    assert "key <> 'lines'" in migration
    assert "jsonb_typeof(item) IN ('object', 'array')" in migration
    index = next(
        item
        for item in BillingRolloutReceipt.__table__.indexes
        if item.name == "uq_billing_rollout_receipts_capability_consumption"
    )
    assert index.unique is True
    assert "IS NOT NULL" in str(index.dialect_options["postgresql"]["where"])


def test_global_roster_fence_is_database_only() -> None:
    # Group 10 E4 removed the Python mirror of the key; the database fence
    # (removed by migration 036) still takes the key it was created with.
    import src.repositories.billing as repository

    expected_key = 3486705124568449331
    assert not hasattr(repository, "billing_roster_advisory_key")
    migration = (
        _ROOT / "alembic/versions/20261011_000003_026_asaas_billing.py"
    ).read_text()
    assert migration.count(f"pg_advisory_xact_lock({expected_key})") == 6
    for trigger in (
        "trg_billing_subscriptions_roster_fence",
        "trg_billing_migrations_roster_fence",
        "trg_billing_migration_receipts_roster_fence",
        "trg_billing_roster_control_fence",
    ):
        assert trigger in migration
    receipt_shape = _check_sql(
        BillingMigrationReceipt, "ck_billing_migration_receipts_saga_shape"
    )
    assert "provider = 'abacatepay'" in receipt_shape
    assert "provider = 'asaas'" in receipt_shape


def test_canary_plus_snapshot_and_transitive_provenance_are_closed() -> None:
    exact = _check_sql(
        BillingCheckoutSession, "ck_billing_checkout_sessions_canary_exact"
    )
    assert "'included_clinics', 1" in exact
    assert "'credit_grant_mc', 5000000" in exact
    assert "'credit_grant_mc', 5000" in exact
    assert "'line_financial_state', 'pending'" in exact
    migration = (
        _ROOT / "alembic/versions/20261011_000003_026_asaas_billing.py"
    ).read_text(encoding="utf-8")
    assert "active canary checkout provenance is required" in migration
    assert "canary checkout is outside its lifetime slot" in migration
    assert "canary subscription projection target mismatch" in migration
    assert "canonical Plus subscription is required" in migration
    assert "historical canary lineage is required" in migration
    assert "provider checkout identity is required" in migration
    assert "provider subscription lineage is required" in migration


def test_migration_035_preserves_legacy_canary_rows_and_guards_downgrade() -> None:
    migration_path = (
        _ROOT / "alembic/versions/20261015_000003_035_billing_payment_paid_at.py"
    )
    migration = migration_path.read_text(encoding="utf-8")
    assert "_canary_subscription_snapshot(5_000_000)" in migration
    assert "_canary_subscription_snapshot(5_000)" in migration
    assert "refunded_before_settlement" in migration
    assert "cannot downgrade 035" in migration
    assert "else [_canary_subscription_snapshot(5_000)]" in migration
    assert "pre_settlement_refund_cents" in migration
    assert "pre_settlement_refund_status" in migration
    assert "pre_settlement_refund_observed_at" in migration


def test_postgresql_model_constraints_match_migration_names() -> None:
    expected = {
        "ck_billing_migrations_snapshot_allowlist",
        "ck_billing_topup_orders_snapshot_allowlist",
        "ck_billing_checkout_sessions_snapshot_allowlist",
        "ck_billing_migration_receipts_external_reference",
        "ck_billing_webhook_deliveries_event_fields",
        "ck_billing_webhook_deliveries_resource_id",
        "ck_billing_payment_projections_payment_id",
    }
    metadata_names = {
        constraint.name
        for model in _BILLING_MODELS
        for constraint in model.__table__.constraints
    }
    assert expected.issubset(metadata_names)
    migration = (
        _ROOT / "alembic/versions/20261011_000003_026_asaas_billing.py"
    ).read_text(encoding="utf-8")
    for name in expected:
        assert name in migration


def test_append_only_triggers_and_topup_gate_are_in_revision() -> None:
    migration = (
        _ROOT / "alembic/versions/20261011_000003_026_asaas_billing.py"
    ).read_text(encoding="utf-8")
    for table in (
        "billing_migration_receipts",
        "billing_rollout_receipts",
        "billing_topup_capability_receipts",
        "billing_outbox",
    ):
        assert f"BEFORE UPDATE OR DELETE ON {table}" in migration
    assert "rollout and topup transitions must be independent" in migration
    assert "valid topup capability is required" in migration
    assert "BEFORE UPDATE OR DELETE ON billing_rollout_control" in migration
    assert "BEFORE UPDATE OR DELETE ON billing_roster_control" in migration
    assert "billing financial history is not deletable" in migration
    assert "billing control revision cannot reset" in migration
    assert "CREATE CONSTRAINT TRIGGER trg_billing_control_receipt" in migration
    assert "CREATE CONSTRAINT TRIGGER trg_billing_receipt_control" in migration
    assert "DEFERRABLE INITIALLY DEFERRED" in migration
    assert "rollout_receipt_id = NEW.id" in migration
    assert "topup_receipt_id = NEW.id" in migration
    assert "xmin" not in migration


def test_raw_migration_ddl_rejects_sqlalchemy_bind_parameters() -> None:
    migration = (
        _ROOT / "alembic/versions/20261011_000003_026_asaas_billing.py"
    ).read_text(encoding="utf-8")
    assert "if ddl._bindparams:" in migration
    assert "'[[:cntrl:]]'" not in migration
    assert r"'[[\\:cntrl\\:]]'" in migration


def test_legacy_init_script_defers_billing_to_alembic() -> None:
    bootstrap = (_ROOT / "scripts/init-db.sql").read_text(encoding="utf-8")
    assert "Alembic is the sole schema authority" in bootstrap
    assert "CREATE TABLE billing_" not in bootstrap
    assert "CREATE TABLE IF NOT EXISTS builders" not in bootstrap
    assert "CREATE TABLE" not in bootstrap
    assert "INSERT INTO" not in bootstrap


def test_development_compose_migrates_to_head_before_api() -> None:
    compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == (
        "service_healthy"
    )
    assert services["api"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ("development", 2206209053798267846),
        ("staging", -2227883711241673981),
        ("production", -288927985375474916),
        ("test", 1185205650830267814),
    ],
)
def test_advisory_lock_key_has_fixed_sha256_vectors(
    environment: str, expected: int
) -> None:
    assert billing_advisory_key(environment) == expected


@pytest.mark.asyncio
async def test_atomic_helpers_flush_without_owning_transaction() -> None:
    session = MagicMock()
    session.add_all = MagicMock()
    session.flush = AsyncMock()
    repository = BillingRepository(session)
    order = MagicMock(spec=BillingTopupOrder)
    event = MagicMock(spec=BillingOutboxEvent)

    await repository.record_topup_outbox(order=order, outbox_event=event)

    session.add_all.assert_called_once_with((order, event))
    session.flush.assert_awaited_once()
    assert not hasattr(session, "commit") or not session.commit.called
    assert not hasattr(session, "rollback") or not session.rollback.called


def test_uuid_defaults_are_local_and_not_provider_controlled() -> None:
    for model in _BILLING_MODELS:
        if "id" not in model.__table__.columns:
            continue
        default = model.__table__.columns.id.default
        assert default is not None
        assert default.is_callable
        assert default.arg.__name__ == "uuid4"
    assert uuid4() != uuid4()
