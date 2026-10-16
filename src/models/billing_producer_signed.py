"""Canonical JCS/KMS-signed ASAAS producer artifacts and one-use events."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class BillingProducerV1Grant(Base):
    """Immutable signed assignment or authorization; status lives in events."""

    __tablename__ = "billing_producer_v1_grants"
    __table_args__ = (
        CheckConstraint(
            "grant_kind IN ('assignment', 'authorization')",
            name="ck_billing_producer_v1_grants_kind",
        ),
        CheckConstraint(
            "action IN ('read_billing_producer_pair', "
            "'create_billing_producer_deploy_receipt', "
            "'verify_billing_producer_deploy_receipt')",
            name="ck_billing_producer_v1_grants_action",
        ),
        CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_v1_grants_environment"
        ),
        CheckConstraint(
            "operational_role = 'production-serving'",
            name="ck_billing_producer_v1_grants_role",
        ),
        CheckConstraint(
            "length(envelope_digest) = 64 AND envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_grants_envelope_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_grants_payload_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(scope_digest) = 64 AND scope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_grants_scope_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "expires_at > issued_at AND expires_at <= issued_at + interval '10 minutes'",
            name="ck_billing_producer_v1_grants_ttl",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(action = 'read_billing_producer_pair' AND receipt_id IS NULL "
            "AND verification_receipt_id IS NULL) OR "
            "(action <> 'read_billing_producer_pair' AND receipt_id IS NOT NULL "
            "AND verification_receipt_id IS NOT NULL "
            "AND receipt_id <> verification_receipt_id)",
            name="ck_billing_producer_v1_grants_receipt_ids",
        ),
        CheckConstraint(
            "(grant_kind = 'assignment' AND assignment_id IS NULL "
            "AND assignment_digest IS NULL AND approver_user_id IS NULL) OR "
            "(grant_kind = 'authorization' AND assignment_id IS NOT NULL "
            "AND assignment_digest IS NOT NULL AND approver_user_id IS NOT NULL)",
            name="ck_billing_producer_v1_grants_assignment_link",
        ),
        CheckConstraint(
            "issuer_user_id <> executor_user_id AND "
            "(approver_user_id IS NULL OR "
            "(approver_user_id <> issuer_user_id AND "
            "approver_user_id <> executor_user_id))",
            name="ck_billing_producer_v1_grants_distinct_actors",
        ),
        UniqueConstraint(
            "id", "envelope_digest", name="uq_billing_producer_v1_grant_digest"
        ),
        ForeignKeyConstraint(
            ["assignment_id", "assignment_digest"],
            [
                "billing_producer_v1_grants.id",
                "billing_producer_v1_grants.envelope_digest",
            ],
            name="fk_billing_producer_v1_grant_assignment",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_billing_producer_v1_grants_scope",
            "operation_id",
            "grant_kind",
            "action",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    grant_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    issuer_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    executor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approver_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    assignment_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    assignment_digest: Mapped[str | None] = mapped_column(String(64))
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    operational_role: Mapped[str] = mapped_column(String(40), nullable=False)
    cluster_arn: Mapped[str] = mapped_column(String(255), nullable=False)
    service_arn: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    receipt_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    verification_receipt_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BillingProducerV1GrantConsumption(Base):
    """Append-only, unique consumption event for one signed grant."""

    __tablename__ = "billing_producer_v1_grant_consumptions"
    __table_args__ = (
        UniqueConstraint("grant_id", name="uq_billing_producer_v1_consumption_grant"),
        ForeignKeyConstraint(
            ["grant_id", "grant_digest"],
            [
                "billing_producer_v1_grants.id",
                "billing_producer_v1_grants.envelope_digest",
            ],
            name="fk_billing_producer_v1_consumption_grant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(grant_digest) = 64 AND grant_digest ~ '^[0-9a-f]{64}$' "
            "AND length(result_digest) = 64 AND result_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_consumption_digests",
        ).ddl_if(dialect="postgresql"),
        Index("ix_billing_producer_v1_consumptions_operation", "operation_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    grant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    grant_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    executor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BillingProducerV1GrantRevocation(Base):
    """Append-only revocation artifact and event, without grant-row mutation."""

    __tablename__ = "billing_producer_v1_grant_revocations"
    __table_args__ = (
        UniqueConstraint("grant_id", name="uq_billing_producer_v1_revocation_grant"),
        ForeignKeyConstraint(
            ["grant_id", "grant_digest"],
            [
                "billing_producer_v1_grants.id",
                "billing_producer_v1_grants.envelope_digest",
            ],
            name="fk_billing_producer_v1_revocation_grant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(grant_digest) = 64 AND grant_digest ~ '^[0-9a-f]{64}$' "
            "AND length(envelope_digest) = 64 AND envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_revocation_digests",
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    grant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    grant_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BillingProducerV1BuildAttestation(Base):
    """Immutable CI-signed build provenance; registering human is audit metadata."""

    __tablename__ = "billing_producer_v1_build_attestations"
    __table_args__ = (
        CheckConstraint(
            "length(envelope_digest) = 64 AND envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_attestation_envelope_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_attestation_payload_digest",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "id", "envelope_digest", name="uq_billing_producer_v1_attestation_digest"
        ),
        Index("ix_billing_producer_v1_attestation_operation", "operation_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    registered_by_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class BillingProducerV1Receipt(Base):
    """Immutable canonical deploy or independent verification receipt."""

    __tablename__ = "billing_producer_v1_receipts"
    __table_args__ = (
        CheckConstraint(
            "receipt_type IN ('billing_producer_deployed.v1', "
            "'billing_producer_deploy_verified.v1')",
            name="ck_billing_producer_v1_receipts_type",
        ),
        CheckConstraint(
            "environment = 'staging'",
            name="ck_billing_producer_v1_receipts_environment",
        ),
        CheckConstraint(
            "operational_role = 'production-serving'",
            name="ck_billing_producer_v1_receipts_role",
        ),
        CheckConstraint(
            "length(envelope_digest) = 64 AND envelope_digest ~ '^[0-9a-f]{64}$' "
            "AND length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_receipts_digests",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "expires_at > issued_at AND expires_at <= issued_at + interval '7 days'",
            name="ck_billing_producer_v1_receipts_ttl",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "id",
            "operation_id",
            "envelope_digest",
            name="uq_billing_producer_v1_receipt_link",
        ),
        ForeignKeyConstraint(
            ["build_attestation_id", "build_attestation_digest"],
            [
                "billing_producer_v1_build_attestations.id",
                "billing_producer_v1_build_attestations.envelope_digest",
            ],
            name="fk_billing_producer_v1_receipt_attestation",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_billing_producer_v1_receipts_operation", "operation_id", "receipt_type"
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    receipt_type: Mapped[str] = mapped_column(String(48), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    operational_role: Mapped[str] = mapped_column(String(40), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    task_set_id: Mapped[str] = mapped_column(String(255), nullable=False)
    config_generation: Mapped[str] = mapped_column(String(128), nullable=False)
    compatibility_generation: Mapped[str] = mapped_column(String(128), nullable=False)
    app_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    bundle_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    readback_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    build_attestation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    build_attestation_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    executor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    verifier_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BillingProducerV1ActivePair(Base):
    """Single updatable pointer; all referenced signed evidence is append-only."""

    __tablename__ = "billing_producer_v1_active_pairs"
    __table_args__ = (
        CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_v1_pair_environment"
        ),
        CheckConstraint("revision > 0", name="ck_billing_producer_v1_pair_revision"),
        ForeignKeyConstraint(
            ["receipt_id", "operation_id", "receipt_digest"],
            [
                "billing_producer_v1_receipts.id",
                "billing_producer_v1_receipts.operation_id",
                "billing_producer_v1_receipts.envelope_digest",
            ],
            name="fk_billing_producer_v1_pair_receipt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["verification_receipt_id", "operation_id", "verification_receipt_digest"],
            [
                "billing_producer_v1_receipts.id",
                "billing_producer_v1_receipts.operation_id",
                "billing_producer_v1_receipts.envelope_digest",
            ],
            name="fk_billing_producer_v1_pair_verification",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("environment", name="uq_billing_producer_v1_pair_environment"),
    )

    environment: Mapped[str] = mapped_column(String(20), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    receipt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    verification_receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
