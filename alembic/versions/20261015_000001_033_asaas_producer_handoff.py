"""persist canonical ASAAS producer handoff receipts

Revision ID: 033
Revises: 032
Create Date: 2026-10-15 00:00:01
"""

from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "033"
down_revision: Union[str, None] = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_producer_build_attestations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_build_attestations_digest",
        ),
        sa.UniqueConstraint(
            "id", "payload_digest", name="uq_billing_producer_attestation_digest"
        ),
    )
    op.create_table(
        "billing_producer_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("grant_kind", sa.String(20), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("operational_role", sa.String(40), nullable=False),
        sa.Column("cluster_arn", sa.String(255), nullable=False),
        sa.Column("service_arn", sa.String(255), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "verification_receipt_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claims_signature", sa.String(256), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "grant_kind IN ('assignment', 'authorization')",
            name="ck_billing_producer_grants_kind",
        ),
        sa.CheckConstraint(
            "action IN ('create_billing_producer_deploy_receipt', "
            "'verify_billing_producer_deploy_receipt', 'read_billing_producer_pair')",
            name="ck_billing_producer_grants_action",
        ),
        sa.CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_grants_environment"
        ),
        sa.CheckConstraint(
            "operational_role = 'production-serving'",
            name="ck_billing_producer_grants_role",
        ),
        sa.CheckConstraint(
            "cluster_arn = 'arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging'",
            name="ck_billing_producer_grants_cluster",
        ),
        sa.CheckConstraint(
            "service_arn = 'arn:aws:ecs:us-east-1:388460142431:service/"
            "neurostack-staging/neurostack-staging-app'",
            name="ck_billing_producer_grants_service",
        ),
        sa.CheckConstraint(
            "length(scope_digest) = 64 AND scope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_grants_digest",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_billing_producer_grants_ttl"
        ),
        sa.CheckConstraint(
            "action <> 'read_billing_producer_pair' OR "
            "expires_at <= issued_at + interval '5 minutes'",
            name="ck_billing_producer_preflight_grant_max_ttl",
        ),
        sa.CheckConstraint(
            "(action = 'read_billing_producer_pair' AND receipt_id IS NULL "
            "AND verification_receipt_id IS NULL) OR "
            "(action <> 'read_billing_producer_pair' AND receipt_id IS NOT NULL "
            "AND verification_receipt_id IS NOT NULL)",
            name="ck_billing_producer_grant_receipt_scope",
        ),
        sa.UniqueConstraint(
            "id", "operation_id", name="uq_billing_producer_grant_operation"
        ),
    )
    op.create_index(
        "ix_billing_producer_grants_scope",
        "billing_producer_grants",
        ["operation_id", "grant_kind", "action"],
    )

    op.create_table(
        "billing_producer_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_type", sa.String(48), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("operational_role", sa.String(40), nullable=False),
        sa.Column("cluster_arn", sa.String(255), nullable=False),
        sa.Column("service_arn", sa.String(255), nullable=False),
        sa.Column("deployment_id", sa.String(160), nullable=False),
        sa.Column("task_set_id", sa.String(255), nullable=False),
        sa.Column("config_generation", sa.String(128), nullable=False),
        sa.Column("compatibility_generation", sa.String(128), nullable=False),
        sa.Column("app_digest", sa.String(71), nullable=False),
        sa.Column("bundle_digest", sa.String(64), nullable=False),
        sa.Column("readback_digest", sa.String(64), nullable=False),
        sa.Column(
            "build_attestation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("build_attestation_digest", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("tree_sha", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=False),
        sa.Column(
            "attestor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "verifier_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "supersedes_receipt_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("supersedes_receipt_digest", sa.String(64), nullable=True),
        sa.Column(
            "supersedes_verification_receipt_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "supersedes_verification_receipt_digest", sa.String(64), nullable=True
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(256), nullable=False),
        sa.CheckConstraint(
            "receipt_type IN ('billing_producer_deployed.v1', "
            "'billing_producer_deploy_verified.v1')",
            name="ck_billing_producer_receipts_type",
        ),
        sa.CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_receipts_environment"
        ),
        sa.CheckConstraint(
            "operational_role = 'production-serving'",
            name="ck_billing_producer_receipts_role",
        ),
        sa.CheckConstraint(
            "cluster_arn = 'arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging'",
            name="ck_billing_producer_receipts_cluster",
        ),
        sa.CheckConstraint(
            "service_arn = 'arn:aws:ecs:us-east-1:388460142431:service/"
            "neurostack-staging/neurostack-staging-app'",
            name="ck_billing_producer_receipts_service",
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_digest",
        ),
        sa.CheckConstraint(
            "app_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_app_digest",
        ),
        sa.CheckConstraint(
            "bundle_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_bundle_digest",
        ),
        sa.CheckConstraint(
            "readback_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_readback_digest",
        ),
        sa.CheckConstraint(
            "build_attestation_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_attestation_digest",
        ),
        sa.CheckConstraint(
            "commit_sha ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_receipts_commit_sha",
        ),
        sa.CheckConstraint(
            "tree_sha ~ '^[0-9a-f]{64}$'", name="ck_billing_producer_receipts_tree_sha"
        ),
        sa.CheckConstraint(
            "(supersedes_receipt_id IS NULL AND supersedes_receipt_digest IS NULL "
            "AND supersedes_verification_receipt_id IS NULL "
            "AND supersedes_verification_receipt_digest IS NULL) OR "
            "(supersedes_receipt_id IS NOT NULL AND supersedes_receipt_digest ~ '^[0-9a-f]{64}$' "
            "AND supersedes_verification_receipt_id IS NOT NULL "
            "AND supersedes_verification_receipt_digest ~ '^[0-9a-f]{64}$')",
            name="ck_billing_producer_receipts_supersedes_pair",
        ),
        sa.CheckConstraint(
            "(receipt_type = 'billing_producer_deployed.v1' AND verifier_user_id IS NULL) OR "
            "(receipt_type = 'billing_producer_deploy_verified.v1' AND verifier_user_id IS NOT NULL)",
            name="ck_billing_producer_receipts_actor_shape",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_billing_producer_receipts_ttl"
        ),
        sa.CheckConstraint(
            "expires_at <= issued_at + interval '7 days'",
            name="ck_billing_producer_receipts_max_ttl",
        ),
        sa.UniqueConstraint(
            "id", "operation_id", name="uq_billing_producer_receipt_operation"
        ),
        sa.UniqueConstraint(
            "operation_id",
            "receipt_type",
            "id",
            name="uq_billing_producer_receipt_kind_id",
        ),
        sa.ForeignKeyConstraint(
            ["build_attestation_id", "build_attestation_digest"],
            [
                "billing_producer_build_attestations.id",
                "billing_producer_build_attestations.payload_digest",
            ],
            ondelete="RESTRICT",
            name="fk_billing_producer_receipt_build_attestation",
        ),
    )
    op.create_index(
        "ix_billing_producer_receipts_operation",
        "billing_producer_receipts",
        ["operation_id", "receipt_type"],
    )

    op.create_table(
        "billing_producer_active_pairs",
        sa.Column("environment", sa.String(20), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_digest", sa.String(64), nullable=False),
        sa.Column(
            "verification_receipt_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("verification_receipt_digest", sa.String(64), nullable=False),
        sa.Column("deployment_fingerprint", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_active_environment"
        ),
        sa.CheckConstraint("revision > 0", name="ck_billing_producer_active_revision"),
        sa.ForeignKeyConstraint(
            ["receipt_id", "operation_id"],
            ["billing_producer_receipts.id", "billing_producer_receipts.operation_id"],
            name="fk_billing_producer_active_deploy_receipt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["verification_receipt_id", "operation_id"],
            ["billing_producer_receipts.id", "billing_producer_receipts.operation_id"],
            name="fk_billing_producer_active_verified_receipt",
            ondelete="RESTRICT",
        ),
    )

    op.execute("""
        CREATE FUNCTION reject_billing_producer_receipt_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
            RAISE EXCEPTION 'billing producer receipts are append-only';
        END;
        $function$
        """)
    op.execute("""
        CREATE TRIGGER trg_billing_producer_receipts_immutable
        BEFORE UPDATE OR DELETE ON billing_producer_receipts
        FOR EACH ROW EXECUTE FUNCTION reject_billing_producer_receipt_mutation()
        """)
    op.execute("""
        CREATE TRIGGER trg_billing_producer_build_attestations_immutable
        BEFORE UPDATE OR DELETE ON billing_producer_build_attestations
        FOR EACH ROW EXECUTE FUNCTION reject_billing_producer_receipt_mutation()
        """)
    op.execute("""
        CREATE FUNCTION validate_billing_producer_grant_state()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
            IF ROW(NEW.id, NEW.grant_kind, NEW.actor_user_id, NEW.operation_id,
                   NEW.environment, NEW.operational_role, NEW.cluster_arn,
                   NEW.service_arn, NEW.action, NEW.receipt_id,
                   NEW.verification_receipt_id, NEW.scope_digest, NEW.issued_at,
                   NEW.expires_at, NEW.claims_signature)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.grant_kind, OLD.actor_user_id, OLD.operation_id,
                   OLD.environment, OLD.operational_role, OLD.cluster_arn,
                   OLD.service_arn, OLD.action, OLD.receipt_id,
                   OLD.verification_receipt_id, OLD.scope_digest, OLD.issued_at,
                   OLD.expires_at, OLD.claims_signature) THEN
                RAISE EXCEPTION 'billing producer grant claims are immutable';
            END IF;
            IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
                RAISE EXCEPTION 'billing producer grant revocation is immutable';
            END IF;
            IF OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at THEN
                RAISE EXCEPTION 'billing producer grant consumption is immutable';
            END IF;
            IF NEW.revoked_at IS NOT NULL AND OLD.revoked_at IS NULL
               AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at THEN
                RAISE EXCEPTION 'grant cannot be revoked and consumed in one update';
            END IF;
            RETURN NEW;
        END;
        $function$
        """)
    op.execute("""
        CREATE TRIGGER trg_billing_producer_grants_state
        BEFORE UPDATE ON billing_producer_grants
        FOR EACH ROW EXECUTE FUNCTION validate_billing_producer_grant_state()
        """)
    op.execute("""
        CREATE FUNCTION validate_billing_producer_active_pair()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
            deploy_row billing_producer_receipts%ROWTYPE;
            verified_row billing_producer_receipts%ROWTYPE;
        BEGIN
            SELECT * INTO deploy_row FROM billing_producer_receipts
             WHERE id = NEW.receipt_id AND operation_id = NEW.operation_id;
            SELECT * INTO verified_row FROM billing_producer_receipts
             WHERE id = NEW.verification_receipt_id AND operation_id = NEW.operation_id;
            IF deploy_row.id IS NULL OR verified_row.id IS NULL
               OR deploy_row.receipt_type <> 'billing_producer_deployed.v1'
               OR verified_row.receipt_type <> 'billing_producer_deploy_verified.v1'
               OR deploy_row.environment <> NEW.environment
               OR verified_row.environment <> NEW.environment
               OR deploy_row.operational_role <> 'production-serving'
               OR verified_row.operational_role <> deploy_row.operational_role
               OR deploy_row.cluster_arn <> 'arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging'
               OR verified_row.cluster_arn <> deploy_row.cluster_arn
               OR deploy_row.service_arn <> 'arn:aws:ecs:us-east-1:388460142431:service/neurostack-staging/neurostack-staging-app'
               OR verified_row.service_arn <> deploy_row.service_arn
               OR deploy_row.payload_digest <> NEW.receipt_digest
               OR verified_row.payload_digest <> NEW.verification_receipt_digest
               OR deploy_row.build_attestation_id <> verified_row.build_attestation_id
               OR deploy_row.build_attestation_digest <> verified_row.build_attestation_digest
               OR deploy_row.commit_sha <> verified_row.commit_sha
               OR deploy_row.tree_sha <> verified_row.tree_sha
               OR deploy_row.workflow_id <> verified_row.workflow_id
               OR deploy_row.supersedes_receipt_id IS DISTINCT FROM verified_row.supersedes_receipt_id
               OR deploy_row.supersedes_receipt_digest IS DISTINCT FROM verified_row.supersedes_receipt_digest
               OR deploy_row.supersedes_verification_receipt_id IS DISTINCT FROM verified_row.supersedes_verification_receipt_id
               OR deploy_row.supersedes_verification_receipt_digest IS DISTINCT FROM verified_row.supersedes_verification_receipt_digest
               OR deploy_row.deployment_id <> verified_row.deployment_id
               OR deploy_row.task_set_id <> verified_row.task_set_id
               OR deploy_row.config_generation <> verified_row.config_generation
               OR deploy_row.compatibility_generation <> verified_row.compatibility_generation
               OR deploy_row.app_digest <> verified_row.app_digest
               OR deploy_row.bundle_digest <> verified_row.bundle_digest
               OR deploy_row.readback_digest <> verified_row.readback_digest
               OR verified_row.verifier_user_id IS NULL
               OR deploy_row.attestor_user_id = verified_row.verifier_user_id
               OR deploy_row.payload->>'operation_id' IS DISTINCT FROM NEW.operation_id::text
               OR deploy_row.payload->>'receipt_id' IS DISTINCT FROM deploy_row.id::text
               OR deploy_row.payload->>'verification_receipt_id' IS DISTINCT FROM verified_row.id::text
               OR deploy_row.payload->>'receipt_type' IS DISTINCT FROM deploy_row.receipt_type
               OR deploy_row.payload->>'environment' IS DISTINCT FROM deploy_row.environment
               OR deploy_row.payload->>'operational_role' IS DISTINCT FROM deploy_row.operational_role
               OR deploy_row.payload->>'readback_digest' IS DISTINCT FROM deploy_row.readback_digest
               OR deploy_row.payload->>'build_attestation_id' IS DISTINCT FROM deploy_row.build_attestation_id::text
               OR deploy_row.payload->>'build_attestation_digest' IS DISTINCT FROM deploy_row.build_attestation_digest
               OR deploy_row.payload->>'commit_sha' IS DISTINCT FROM deploy_row.commit_sha
               OR deploy_row.payload->>'tree_sha' IS DISTINCT FROM deploy_row.tree_sha
               OR deploy_row.payload->>'workflow_id' IS DISTINCT FROM deploy_row.workflow_id
               OR verified_row.payload->>'operation_id' IS DISTINCT FROM NEW.operation_id::text
               OR verified_row.payload->>'receipt_id' IS DISTINCT FROM verified_row.id::text
               OR verified_row.payload->>'receipt_type' IS DISTINCT FROM verified_row.receipt_type
               OR verified_row.payload->>'source_receipt_id' IS DISTINCT FROM deploy_row.id::text
               OR verified_row.payload->>'source_receipt_digest' IS DISTINCT FROM deploy_row.payload_digest
               OR verified_row.payload->>'readback_digest' IS DISTINCT FROM deploy_row.readback_digest
               OR verified_row.payload->>'build_attestation_id' IS DISTINCT FROM deploy_row.build_attestation_id::text
               OR verified_row.payload->>'build_attestation_digest' IS DISTINCT FROM deploy_row.build_attestation_digest
               OR verified_row.payload->>'commit_sha' IS DISTINCT FROM deploy_row.commit_sha
               OR verified_row.payload->>'tree_sha' IS DISTINCT FROM deploy_row.tree_sha
               OR verified_row.payload->>'workflow_id' IS DISTINCT FROM deploy_row.workflow_id
               OR verified_row.payload->>'attestor_user_id' IS DISTINCT FROM deploy_row.attestor_user_id::text
               OR verified_row.payload->>'verifier_user_id' IS DISTINCT FROM verified_row.verifier_user_id::text
               OR deploy_row.expires_at <= clock_timestamp()
               OR verified_row.expires_at <= clock_timestamp() THEN
                RAISE EXCEPTION 'active producer pair failed receipt verification';
            END IF;
            IF TG_OP = 'UPDATE' AND NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'active producer pair revision must advance exactly once';
            END IF;
            IF TG_OP = 'INSERT' AND NEW.revision <> 1 THEN
                RAISE EXCEPTION 'initial producer pair revision must be one';
            END IF;
            RETURN NEW;
        END;
        $function$
        """)
    op.execute("""
        CREATE TRIGGER trg_billing_producer_active_pair_validate
        BEFORE INSERT OR UPDATE ON billing_producer_active_pairs
        FOR EACH ROW EXECUTE FUNCTION validate_billing_producer_active_pair()
        """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_billing_producer_active_pair_validate ON billing_producer_active_pairs"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_billing_producer_active_pair()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_billing_producer_grants_state ON billing_producer_grants"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_billing_producer_grant_state()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_billing_producer_receipts_immutable ON billing_producer_receipts"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_billing_producer_build_attestations_immutable ON billing_producer_build_attestations"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_billing_producer_receipt_mutation()")
    op.drop_table("billing_producer_active_pairs")
    op.drop_index(
        "ix_billing_producer_receipts_operation", table_name="billing_producer_receipts"
    )
    op.drop_table("billing_producer_receipts")
    op.drop_index(
        "ix_billing_producer_grants_scope", table_name="billing_producer_grants"
    )
    op.drop_table("billing_producer_grants")
    op.drop_table("billing_producer_build_attestations")
