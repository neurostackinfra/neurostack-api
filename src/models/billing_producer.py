"""Append-only evidence and one-use grants for the producer handoff."""

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
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class BillingProducerGrant(Base):
    """Signed assignment or authorization; only consumption/revocation may change."""

    __tablename__ = "billing_producer_grants"
    __table_args__ = (
        CheckConstraint(
            "grant_kind IN ('assignment', 'authorization')",
            name="ck_billing_producer_grants_kind",
        ),
        CheckConstraint(
            "action IN ('create_billing_producer_deploy_receipt', "
            "'verify_billing_producer_deploy_receipt', 'read_billing_producer_pair')",
            name="ck_billing_producer_grants_action",
        ),
        CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_grants_environment"
        ),
        CheckConstraint(
            "operational_role = 'production-serving'",
            name="ck_billing_producer_grants_role",
        ),
        CheckConstraint(
            "cluster_arn = 'arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging'",
            name="ck_billing_producer_grants_cluster",
        ),
        CheckConstraint(
            "service_arn = 'arn:aws:ecs:us-east-1:388460142431:service/"
            "neurostack-staging/neurostack-staging-app'",
            name="ck_billing_producer_grants_service",
        ),
        CheckConstraint(
            "length(scope_digest) = 64 AND scope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_grants_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "expires_at > issued_at", name="ck_billing_producer_grants_ttl"
        ),
        CheckConstraint(
            "action <> 'read_billing_producer_pair' OR "
            "expires_at <= issued_at + interval '5 minutes'",
            name="ck_billing_producer_preflight_grant_max_ttl",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(action = 'read_billing_producer_pair' AND receipt_id IS NULL "
            "AND verification_receipt_id IS NULL) OR "
            "(action <> 'read_billing_producer_pair' AND receipt_id IS NOT NULL "
            "AND verification_receipt_id IS NOT NULL)",
            name="ck_billing_producer_grant_receipt_scope",
        ),
        UniqueConstraint(
            "id", "operation_id", name="uq_billing_producer_grant_operation"
        ),
        Index(
            "ix_billing_producer_grants_scope", "operation_id", "grant_kind", "action"
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    grant_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    operational_role: Mapped[str] = mapped_column(String(40), nullable=False)
    cluster_arn: Mapped[str] = mapped_column(String(255), nullable=False)
    service_arn: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    receipt_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    verification_receipt_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    claims_signature: Mapped[str] = mapped_column(String(256), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingProducerBuildAttestation(Base):
    """Immutable signed CI provenance already verified before producer handoff."""

    __tablename__ = "billing_producer_build_attestations"
    __table_args__ = (
        CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_build_attestations_digest",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "id", "payload_digest", name="uq_billing_producer_attestation_digest"
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class BillingProducerReceipt(Base):
    """Immutable, signed deploy or independent verification evidence."""

    __tablename__ = "billing_producer_receipts"
    __table_args__ = (
        CheckConstraint(
            "receipt_type IN ('billing_producer_deployed.v1', "
            "'billing_producer_deploy_verified.v1')",
            name="ck_billing_producer_receipts_type",
        ),
        CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_receipts_environment"
        ),
        CheckConstraint(
            "operational_role = 'production-serving'",
            name="ck_billing_producer_receipts_role",
        ),
        CheckConstraint(
            "cluster_arn = 'arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging'",
            name="ck_billing_producer_receipts_cluster",
        ),
        CheckConstraint(
            "service_arn = 'arn:aws:ecs:us-east-1:388460142431:service/"
            "neurostack-staging/neurostack-staging-app'",
            name="ck_billing_producer_receipts_service",
        ),
        CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "app_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_app_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "bundle_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_bundle_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "readback_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_readback_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "build_attestation_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_attestation_digest",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "commit_sha ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_commit_sha",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "tree_sha ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_tree_sha",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(supersedes_receipt_id IS NULL AND supersedes_receipt_digest IS NULL "
            "AND supersedes_verification_receipt_id IS NULL "
            "AND supersedes_verification_receipt_digest IS NULL) OR "
            "(supersedes_receipt_id IS NOT NULL AND supersedes_receipt_digest ~ '^[0-9a-f]{64}$' "
            "AND supersedes_verification_receipt_id IS NOT NULL "
            "AND supersedes_verification_receipt_digest ~ '^[0-9a-f]{64}$')",
            name="ck_billing_producer_receipts_supersedes_pair",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(receipt_type = 'billing_producer_deployed.v1' AND verifier_user_id IS NULL) OR "
            "(receipt_type = 'billing_producer_deploy_verified.v1' AND verifier_user_id IS NOT NULL)",
            name="ck_billing_producer_receipts_actor_shape",
        ),
        CheckConstraint(
            "expires_at > issued_at", name="ck_billing_producer_receipts_ttl"
        ),
        CheckConstraint(
            "expires_at <= issued_at + interval '7 days'",
            name="ck_billing_producer_receipts_max_ttl",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "id", "operation_id", name="uq_billing_producer_receipt_operation"
        ),
        UniqueConstraint(
            "operation_id",
            "receipt_type",
            "id",
            name="uq_billing_producer_receipt_kind_id",
        ),
        Index("ix_billing_producer_receipts_operation", "operation_id", "receipt_type"),
        ForeignKeyConstraint(
            ["build_attestation_id", "build_attestation_digest"],
            [
                "billing_producer_build_attestations.id",
                "billing_producer_build_attestations.payload_digest",
            ],
            ondelete="RESTRICT",
            name="fk_billing_producer_receipt_build_attestation",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    receipt_type: Mapped[str] = mapped_column(String(48), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    operational_role: Mapped[str] = mapped_column(String(40), nullable=False)
    cluster_arn: Mapped[str] = mapped_column(String(255), nullable=False)
    service_arn: Mapped[str] = mapped_column(String(255), nullable=False)
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
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    tree_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attestor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    verifier_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    supersedes_receipt_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    supersedes_receipt_digest: Mapped[str | None] = mapped_column(String(64))
    supersedes_verification_receipt_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )
    supersedes_verification_receipt_digest: Mapped[str | None] = mapped_column(
        String(64)
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(256), nullable=False)


class BillingProducerActivePair(Base):
    """CAS-protected pointer to the sole active verified pair for staging."""

    __tablename__ = "billing_producer_active_pairs"
    __table_args__ = (
        CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_active_environment"
        ),
        CheckConstraint("revision > 0", name="ck_billing_producer_active_revision"),
        UniqueConstraint("environment", name="uq_billing_producer_active_environment"),
        ForeignKeyConstraint(
            ["receipt_id", "operation_id"],
            ["billing_producer_receipts.id", "billing_producer_receipts.operation_id"],
            ondelete="RESTRICT",
            name="fk_billing_producer_active_deploy_receipt",
        ),
        ForeignKeyConstraint(
            ["verification_receipt_id", "operation_id"],
            ["billing_producer_receipts.id", "billing_producer_receipts.operation_id"],
            ondelete="RESTRICT",
            name="fk_billing_producer_active_verified_receipt",
        ),
    )

    environment: Mapped[str] = mapped_column(String(20), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    receipt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    verification_receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(
        default=1, nullable=False, server_default=text("1")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
