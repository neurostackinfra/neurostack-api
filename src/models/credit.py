"""Credit ledger, metering, ingress and enforcement rows.

Migration 038 (``alembic/versions/20261026_000001_038_credit_system.py``) is
the schema authority; these mappers mirror it column for column, constraint
for constraint (``tests/integration/test_credit_orm_ddl_drift_postgres.py``).
The fact inbox lives in :mod:`src.models.credit_fact_inbox`; the consumer's
ack is the existing ASAAS model ``BillingFactAck``.

Rules the schema enforces (see the migration docstring):

* history rows (accounts, effects, lots, ledger, debts, debt origins,
  settlements, readiness, observational effects) are append-only by trigger;
  a correction is a new row;
* history keeps ``builder_id``/``tenant_id`` without a foreign key to
  ``builders``/``tenants``; only ``credit_ingress_claims`` and
  ``credit_response_outbox`` reference ``tenants`` (``ON DELETE CASCADE``);
* composite ``(builder_id, ...)`` foreign keys keep every credit reference
  inside one builder, and a trigger checks ``builder_id`` against the
  tenant's current builder on write.

No mapper here declares ``relationship()``, and no existing model gains one
to a credit table.  Amounts are integer millicredits (1 credit = 1,000 mc).
Nothing in this module reads ``tests/`` at import.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin

SHA256_PATTERN: Final = "^[0-9a-f]{64}$"
EFFECT_ID_MAX_LENGTH: Final = 512
LOT_KINDS: Final = ("monthly", "topup")
LEDGER_ENTRY_TYPES: Final = (
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
INGRESS_SOURCES: Final = ("events", "zapi", "meta", "uazapi", "evolution")
INGRESS_MODES: Final = ("observational", "enforced")
OBSERVATIONAL_EFFECT_KINDS: Final = ("trace", "rag", "media", "egress", "maintenance")
OBSERVATIONAL_ORIGINS: Final = ("production", "simulator", "unknown")
BINDING_STATUSES: Final = ("active", "reversed")
READINESS_SCHEMA_VERSION: Final = "credit_capability_ready.v1"
READINESS_ENVIRONMENTS: Final = ("development", "staging", "production", "test")
ENFORCEMENT_STATE_ID: Final = 1


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _postgresql_check(expression: str, *, name: str) -> CheckConstraint:
    """PostgreSQL-only validation (regex) that SQLite tests cannot evaluate."""

    return CheckConstraint(expression, name=name).ddl_if(dialect="postgresql")


def _sha256(column: str) -> str:
    return f"{column} ~ '{SHA256_PATTERN}'"


def _nullable_sha256(column: str) -> str:
    return f"({column} IS NULL OR {_sha256(column)})"


class CreditAccount(Base):
    """One credit account per builder; balances are derived from the ledger."""

    __tablename__ = "credit_accounts"
    __table_args__ = (
        UniqueConstraint("builder_id", name="uq_credit_accounts_builder"),
        UniqueConstraint("builder_id", "id", name="uq_credit_accounts_builder_id_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditEffect(Base):
    """Idempotency boundary of one financial effect (``UNIQUE(builder_id,
    effect_id)``); ``decision_digest`` survives the deletion of its tenant."""

    __tablename__ = "credit_effects"
    __table_args__ = (
        CheckConstraint("length(effect_id) > 0", name="ck_credit_effects_effect_id"),
        CheckConstraint(
            "length(effect_kind) > 0", name="ck_credit_effects_effect_kind"
        ),
        _postgresql_check(
            _nullable_sha256("decision_digest"),
            name="ck_credit_effects_decision_digest",
        ),
        ForeignKeyConstraint(
            ["builder_id"],
            ["credit_accounts.builder_id"],
            ondelete="RESTRICT",
            name="fk_credit_effects_account",
        ),
        UniqueConstraint(
            "builder_id", "effect_id", name="uq_credit_effects_builder_effect"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    effect_id: Mapped[str] = mapped_column(String(EFFECT_ID_MAX_LENGTH), nullable=False)
    effect_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    decision_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditLot(Base):
    """A grant of millicredits with its own expiry: one monthly lot per
    subscription cycle, one top-up lot per order."""

    __tablename__ = "credit_lots"
    __table_args__ = (
        CheckConstraint(_in("lot_kind", LOT_KINDS), name="ck_credit_lots_kind"),
        CheckConstraint(
            "(lot_kind = 'monthly' AND subscription_id IS NOT NULL "
            "AND cycle_start IS NOT NULL AND cycle_end IS NOT NULL "
            "AND cycle_start < cycle_end AND order_id IS NULL) "
            "OR (lot_kind = 'topup' AND order_id IS NOT NULL "
            "AND subscription_id IS NULL AND cycle_start IS NULL "
            "AND cycle_end IS NULL)",
            name="ck_credit_lots_source",
        ),
        CheckConstraint("granted_mc > 0", name="ck_credit_lots_granted_mc"),
        ForeignKeyConstraint(
            ["builder_id", "account_id"],
            ["credit_accounts.builder_id", "credit_accounts.id"],
            ondelete="RESTRICT",
            name="fk_credit_lots_account",
        ),
        ForeignKeyConstraint(
            ["builder_id", "grant_effect_id"],
            ["credit_effects.builder_id", "credit_effects.effect_id"],
            ondelete="RESTRICT",
            name="fk_credit_lots_grant_effect",
        ),
        UniqueConstraint(
            "subscription_id",
            "cycle_start",
            "cycle_end",
            name="uq_credit_lots_monthly_cycle",
        ),
        UniqueConstraint("order_id", name="uq_credit_lots_topup_order"),
        UniqueConstraint("builder_id", "id", name="uq_credit_lots_builder_id_id"),
        Index("ix_credit_lots_builder_expires", "builder_id", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    lot_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subscription_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    cycle_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    cycle_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    order_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    payment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    paid_at: Mapped[date] = mapped_column(Date, nullable=False)
    granted_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    grant_effect_id: Mapped[str] = mapped_column(
        String(EFFECT_ID_MAX_LENGTH), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditLedgerEntry(Base):
    """One append-only movement.  ``amount_mc`` is always positive; the
    ``entry_type`` gives its direction (``src.repositories.credit``)."""

    __tablename__ = "credit_ledger_entries"
    __table_args__ = (
        CheckConstraint(
            _in("entry_type", LEDGER_ENTRY_TYPES),
            name="ck_credit_ledger_entries_type",
        ),
        CheckConstraint("amount_mc > 0", name="ck_credit_ledger_entries_amount"),
        CheckConstraint(
            "lot_id IS NOT NULL OR entry_type IN ('debt', 'debt_release')",
            name="ck_credit_ledger_entries_lot",
        ),
        ForeignKeyConstraint(
            ["builder_id", "account_id"],
            ["credit_accounts.builder_id", "credit_accounts.id"],
            ondelete="RESTRICT",
            name="fk_credit_ledger_entries_account",
        ),
        ForeignKeyConstraint(
            ["builder_id", "effect_id"],
            ["credit_effects.builder_id", "credit_effects.effect_id"],
            ondelete="RESTRICT",
            name="fk_credit_ledger_entries_effect",
        ),
        ForeignKeyConstraint(
            ["builder_id", "lot_id"],
            ["credit_lots.builder_id", "credit_lots.id"],
            ondelete="RESTRICT",
            name="fk_credit_ledger_entries_lot",
        ),
        UniqueConstraint(
            "builder_id", "id", name="uq_credit_ledger_entries_builder_id_id"
        ),
        Index("ix_credit_ledger_entries_builder_lot", "builder_id", "lot_id"),
        Index("ix_credit_ledger_entries_builder_effect", "builder_id", "effect_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    effect_id: Mapped[str] = mapped_column(String(EFFECT_ID_MAX_LENGTH), nullable=False)
    lot_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    amount_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditDebt(Base):
    """Consumption that capacity no longer covers, with its provenance."""

    __tablename__ = "credit_debts"
    __table_args__ = (
        CheckConstraint(
            "length(debt_source_kind) > 0", name="ck_credit_debts_source_kind"
        ),
        CheckConstraint("amount_mc > 0", name="ck_credit_debts_amount"),
        ForeignKeyConstraint(
            ["builder_id", "account_id"],
            ["credit_accounts.builder_id", "credit_accounts.id"],
            ondelete="RESTRICT",
            name="fk_credit_debts_account",
        ),
        ForeignKeyConstraint(
            ["builder_id", "debt_source_effect_id"],
            ["credit_effects.builder_id", "credit_effects.effect_id"],
            ondelete="RESTRICT",
            name="fk_credit_debts_source_effect",
        ),
        ForeignKeyConstraint(
            ["builder_id", "original_grant_lot_id"],
            ["credit_lots.builder_id", "credit_lots.id"],
            ondelete="RESTRICT",
            name="fk_credit_debts_original_lot",
        ),
        UniqueConstraint("builder_id", "id", name="uq_credit_debts_builder_id_id"),
        Index("ix_credit_debts_builder_order", "builder_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    debt_source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    debt_source_effect_id: Mapped[str] = mapped_column(
        String(EFFECT_ID_MAX_LENGTH), nullable=False
    )
    original_grant_lot_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    amount_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditDebtOrigin(Base):
    """The consumption entries a debt came from (same builder)."""

    __tablename__ = "credit_debt_origins"
    __table_args__ = (
        ForeignKeyConstraint(
            ["builder_id", "debt_id"],
            ["credit_debts.builder_id", "credit_debts.id"],
            ondelete="RESTRICT",
            name="fk_credit_debt_origins_debt",
        ),
        ForeignKeyConstraint(
            ["builder_id", "consumption_entry_id"],
            ["credit_ledger_entries.builder_id", "credit_ledger_entries.id"],
            ondelete="RESTRICT",
            name="fk_credit_debt_origins_consumption",
        ),
    )

    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    debt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    consumption_entry_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True
    )


class CreditDebtSettlement(Base):
    """Which grant lot paid which debt, in ``(debt.created_at, debt.id)``
    order; restoration returns exactly this amount to this lot."""

    __tablename__ = "credit_debt_settlements"
    __table_args__ = (
        CheckConstraint("amount_mc > 0", name="ck_credit_debt_settlements_amount"),
        ForeignKeyConstraint(
            ["builder_id", "debt_id"],
            ["credit_debts.builder_id", "credit_debts.id"],
            ondelete="RESTRICT",
            name="fk_credit_debt_settlements_debt",
        ),
        ForeignKeyConstraint(
            ["builder_id", "settling_grant_lot_id"],
            ["credit_lots.builder_id", "credit_lots.id"],
            ondelete="RESTRICT",
            name="fk_credit_debt_settlements_lot",
        ),
        ForeignKeyConstraint(
            ["builder_id", "effect_id"],
            ["credit_effects.builder_id", "credit_effects.effect_id"],
            ondelete="RESTRICT",
            name="fk_credit_debt_settlements_effect",
        ),
        Index("ix_credit_debt_settlements_builder_debt", "builder_id", "debt_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    debt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    settling_grant_lot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    amount_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effect_id: Mapped[str] = mapped_column(String(EFFECT_ID_MAX_LENGTH), nullable=False)


class CreditCapabilityReadiness(Base):
    """Append-only readiness evaluation (code readiness, never authority).

    The calculated fields are filled now; the five nullable digests and
    ``operation_id`` are filled only by a row written at the rollout opening
    from the ASAAS receipts.
    """

    __tablename__ = "credit_capability_readiness"
    __table_args__ = (
        CheckConstraint(
            f"schema_version = '{READINESS_SCHEMA_VERSION}'",
            name="ck_credit_capability_readiness_schema_version",
        ),
        CheckConstraint(
            _in("environment", READINESS_ENVIRONMENTS),
            name="ck_credit_capability_readiness_environment",
        ),
        CheckConstraint(
            "inbox_high_watermark >= 0",
            name="ck_credit_capability_readiness_watermark",
        ),
        _postgresql_check(
            " AND ".join(
                (
                    _sha256("billing_fact_schema_digest"),
                    _nullable_sha256("deployment_digest"),
                    _nullable_sha256("credit_deployment_digest"),
                    _nullable_sha256("producer_compatibility_digest"),
                    _nullable_sha256("consumer_compatibility_digest"),
                )
            ),
            name="ck_credit_capability_readiness_digests",
        ),
        Index(
            "ix_credit_capability_readiness_environment_created",
            "environment",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    billing_fact_schema_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    inbox_high_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reconciliation_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    readback_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    has_open_gap: Mapped[bool] = mapped_column(Boolean, nullable=False)
    has_backlog: Mapped[bool] = mapped_column(Boolean, nullable=False)
    has_alert: Mapped[bool] = mapped_column(Boolean, nullable=False)
    operation_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credit_deployment_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    producer_compatibility_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    consumer_compatibility_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditIngressClaim(Base):
    """Durable ingress claim, deduplicated per tenant (a standalone tenant has
    no builder, and a reparent keeps the tenant)."""

    __tablename__ = "credit_ingress_claims"
    __table_args__ = (
        CheckConstraint(
            _in("source", INGRESS_SOURCES), name="ck_credit_ingress_claims_source"
        ),
        CheckConstraint(
            "(source = 'events' AND idempotency_key IS NOT NULL "
            "AND provider_message_id IS NULL) "
            "OR (source <> 'events' AND provider_message_id IS NOT NULL "
            "AND idempotency_key IS NULL)",
            name="ck_credit_ingress_claims_key",
        ),
        CheckConstraint(
            "length(principal_id) > 0 "
            "AND (idempotency_key IS NULL OR length(idempotency_key) > 0) "
            "AND (provider_message_id IS NULL OR length(provider_message_id) > 0)",
            name="ck_credit_ingress_claims_identity",
        ),
        CheckConstraint(
            "decision_version > 0 "
            "AND (session_version IS NULL OR session_version >= 0)",
            name="ck_credit_ingress_claims_versions",
        ),
        CheckConstraint(
            _in("mode", INGRESS_MODES), name="ck_credit_ingress_claims_mode"
        ),
        _postgresql_check(
            _sha256("payload_sha256"), name="ck_credit_ingress_claims_payload_sha256"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_credit_ingress_claims_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "principal_id",
            "idempotency_key",
            name="uq_credit_ingress_claims_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "source",
            "provider_message_id",
            name="uq_credit_ingress_claims_provider_message",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    builder_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interaction_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False)
    session_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditResponseOutbox(Base):
    """Response/telemetry outbox row, published only after its commit."""

    __tablename__ = "credit_response_outbox"
    __table_args__ = (
        CheckConstraint(
            "length(effect_id) > 0", name="ck_credit_response_outbox_effect_id"
        ),
        CheckConstraint("length(state) > 0", name="ck_credit_response_outbox_state"),
        CheckConstraint("attempts >= 0", name="ck_credit_response_outbox_attempts"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_credit_response_outbox_tenant",
        ),
        UniqueConstraint(
            "tenant_id", "effect_id", name="uq_credit_response_outbox_effect"
        ),
        Index("ix_credit_response_outbox_state_created", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    effect_id: Mapped[str] = mapped_column(String(EFFECT_ID_MAX_LENGTH), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditEnforcementState(Base):
    """The single row (``id = 1``) carrying ``credit_enforcement_at``; written
    only by the Group 7 service, never seeded by the migration."""

    __tablename__ = "credit_enforcement_state"
    __table_args__ = (
        CheckConstraint(
            f"id = {ENFORCEMENT_STATE_ID}", name="ck_credit_enforcement_state_singleton"
        ),
        CheckConstraint(
            "credit_enforcement_at IS NULL "
            "OR (set_by IS NOT NULL AND set_at IS NOT NULL)",
            name="ck_credit_enforcement_state_set",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    credit_enforcement_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    set_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CreditEnforcementBinding(Base, TimestampMixin):
    """Subscription-cycle binding of the monthly allowance, written by the
    fact consumer in the grant's transaction and updated (``status``,
    ``financial_revision``, ``updated_at``) on every applied cycle fact.  No
    foreign key to the ASAAS tables."""

    __tablename__ = "credit_enforcement_binding"
    __table_args__ = (
        CheckConstraint(
            _in("status", BINDING_STATUSES), name="ck_credit_enforcement_binding_status"
        ),
        CheckConstraint(
            "cycle_start < cycle_end", name="ck_credit_enforcement_binding_cycle"
        ),
        CheckConstraint(
            "financial_revision > 0", name="ck_credit_enforcement_binding_revision"
        ),
        ForeignKeyConstraint(
            ["builder_id", "grant_effect_id"],
            ["credit_effects.builder_id", "credit_effects.effect_id"],
            ondelete="RESTRICT",
            name="fk_credit_enforcement_binding_grant_effect",
        ),
        UniqueConstraint(
            "subscription_cycle_id", name="uq_credit_enforcement_binding_cycle"
        ),
        UniqueConstraint(
            "subscription_id",
            "cycle_start",
            "cycle_end",
            name="uq_credit_enforcement_binding_subscription_cycle",
        ),
        Index("ix_credit_enforcement_binding_builder", "builder_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    subscription_cycle_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    cycle_start: Mapped[date] = mapped_column(Date, nullable=False)
    cycle_end: Mapped[date] = mapped_column(Date, nullable=False)
    financial_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    grant_effect_id: Mapped[str] = mapped_column(
        String(EFFECT_ID_MAX_LENGTH), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)


class CreditObservationalEffect(Base):
    """Metering recorded before ``credit_enforcement_at``: what would have
    been debited, keyed per tenant (``trace:{tenant_id}:...``)."""

    __tablename__ = "credit_observational_effects"
    __table_args__ = (
        CheckConstraint(
            _in("effect_kind", OBSERVATIONAL_EFFECT_KINDS),
            name="ck_credit_observational_effects_kind",
        ),
        CheckConstraint(
            f"origin IS NULL OR {_in('origin', OBSERVATIONAL_ORIGINS)}",
            name="ck_credit_observational_effects_origin",
        ),
        CheckConstraint(
            "length(effect_id) > 0", name="ck_credit_observational_effects_effect_id"
        ),
        CheckConstraint(
            "would_debit_mc >= 0", name="ck_credit_observational_effects_would_debit"
        ),
        _postgresql_check(
            _nullable_sha256("decision_digest"),
            name="ck_credit_observational_effects_decision_digest",
        ),
        UniqueConstraint(
            "tenant_id", "effect_id", name="uq_credit_observational_effects_effect"
        ),
        Index("ix_credit_observational_effects_recorded", "recorded_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    builder_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    effect_id: Mapped[str] = mapped_column(String(EFFECT_ID_MAX_LENGTH), nullable=False)
    effect_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    trace_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    origin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    authenticated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    would_debit_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
