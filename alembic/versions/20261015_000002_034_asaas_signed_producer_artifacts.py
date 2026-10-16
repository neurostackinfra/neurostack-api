"""add canonical signed ASAAS producer v1 artifacts and events

Revision ID: 034
Revises: 033
Create Date: 2026-10-15 00:00:02
"""

from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_producer_v1_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("grant_kind", sa.String(20), nullable=False),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("envelope_digest", sa.String(64), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column(
            "issuer_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "executor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "approver_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assignment_digest", sa.String(64), nullable=True),
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
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "grant_kind IN ('assignment', 'authorization')",
            name="ck_billing_producer_v1_grants_kind",
        ),
        sa.CheckConstraint(
            "action IN ('read_billing_producer_pair', 'create_billing_producer_deploy_receipt', 'verify_billing_producer_deploy_receipt')",
            name="ck_billing_producer_v1_grants_action",
        ),
        sa.CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_v1_grants_environment"
        ),
        sa.CheckConstraint(
            "operational_role = 'production-serving'",
            name="ck_billing_producer_v1_grants_role",
        ),
        sa.CheckConstraint(
            "length(envelope_digest) = 64 AND envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_grants_envelope_digest",
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_grants_payload_digest",
        ),
        sa.CheckConstraint(
            "length(scope_digest) = 64 AND scope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_grants_scope_digest",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at AND expires_at <= issued_at + interval '10 minutes'",
            name="ck_billing_producer_v1_grants_ttl",
        ),
        sa.CheckConstraint(
            "(action = 'read_billing_producer_pair' AND receipt_id IS NULL AND verification_receipt_id IS NULL) OR (action <> 'read_billing_producer_pair' AND receipt_id IS NOT NULL AND verification_receipt_id IS NOT NULL AND receipt_id <> verification_receipt_id)",
            name="ck_billing_producer_v1_grants_receipt_ids",
        ),
        sa.CheckConstraint(
            "(grant_kind = 'assignment' AND assignment_id IS NULL AND assignment_digest IS NULL AND approver_user_id IS NULL) OR (grant_kind = 'authorization' AND assignment_id IS NOT NULL AND assignment_digest IS NOT NULL AND approver_user_id IS NOT NULL)",
            name="ck_billing_producer_v1_grants_assignment_link",
        ),
        sa.CheckConstraint(
            "issuer_user_id <> executor_user_id AND (approver_user_id IS NULL OR (approver_user_id <> issuer_user_id AND approver_user_id <> executor_user_id))",
            name="ck_billing_producer_v1_grants_distinct_actors",
        ),
        sa.UniqueConstraint(
            "id", "envelope_digest", name="uq_billing_producer_v1_grant_digest"
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id", "assignment_digest"],
            [
                "billing_producer_v1_grants.id",
                "billing_producer_v1_grants.envelope_digest",
            ],
            name="fk_billing_producer_v1_grant_assignment",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_billing_producer_v1_grants_scope",
        "billing_producer_v1_grants",
        ["operation_id", "grant_kind", "action"],
    )

    op.create_table(
        "billing_producer_v1_grant_consumptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grant_digest", sa.String(64), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "executor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "grant_id", name="uq_billing_producer_v1_consumption_grant"
        ),
        sa.ForeignKeyConstraint(
            ["grant_id", "grant_digest"],
            [
                "billing_producer_v1_grants.id",
                "billing_producer_v1_grants.envelope_digest",
            ],
            name="fk_billing_producer_v1_consumption_grant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(grant_digest) = 64 AND grant_digest ~ '^[0-9a-f]{64}$' AND length(result_digest) = 64 AND result_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_consumption_digests",
        ),
    )
    op.create_index(
        "ix_billing_producer_v1_consumptions_operation",
        "billing_producer_v1_grant_consumptions",
        ["operation_id"],
    )

    op.create_table(
        "billing_producer_v1_grant_revocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grant_digest", sa.String(64), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(40), nullable=False),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("envelope_digest", sa.String(64), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("grant_id", name="uq_billing_producer_v1_revocation_grant"),
        sa.ForeignKeyConstraint(
            ["grant_id", "grant_digest"],
            [
                "billing_producer_v1_grants.id",
                "billing_producer_v1_grants.envelope_digest",
            ],
            name="fk_billing_producer_v1_revocation_grant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(grant_digest) = 64 AND grant_digest ~ '^[0-9a-f]{64}$' AND length(envelope_digest) = 64 AND envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_revocation_digests",
        ),
    )

    op.create_table(
        "billing_producer_v1_build_attestations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("envelope_digest", sa.String(64), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column(
            "registered_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "length(envelope_digest) = 64 AND envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_attestation_envelope_digest",
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_attestation_payload_digest",
        ),
        sa.UniqueConstraint(
            "id", "envelope_digest", name="uq_billing_producer_v1_attestation_digest"
        ),
    )
    op.create_index(
        "ix_billing_producer_v1_attestation_operation",
        "billing_producer_v1_build_attestations",
        ["operation_id"],
    )

    op.create_table(
        "billing_producer_v1_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_type", sa.String(48), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("operational_role", sa.String(40), nullable=False),
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
        sa.Column(
            "executor_user_id",
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
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("envelope_digest", sa.String(64), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "receipt_type IN ('billing_producer_deployed.v1', 'billing_producer_deploy_verified.v1')",
            name="ck_billing_producer_v1_receipts_type",
        ),
        sa.CheckConstraint(
            "environment = 'staging'",
            name="ck_billing_producer_v1_receipts_environment",
        ),
        sa.CheckConstraint(
            "operational_role = 'production-serving'",
            name="ck_billing_producer_v1_receipts_role",
        ),
        sa.CheckConstraint(
            "length(envelope_digest) = 64 AND envelope_digest ~ '^[0-9a-f]{64}$' AND length(payload_digest) = 64 AND payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_billing_producer_v1_receipts_digests",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at AND expires_at <= issued_at + interval '7 days'",
            name="ck_billing_producer_v1_receipts_ttl",
        ),
        sa.CheckConstraint(
            "(receipt_type = 'billing_producer_deployed.v1' AND verifier_user_id IS NULL) OR (receipt_type = 'billing_producer_deploy_verified.v1' AND verifier_user_id IS NOT NULL)",
            name="ck_billing_producer_v1_receipts_actor_shape",
        ),
        sa.UniqueConstraint(
            "id",
            "operation_id",
            "envelope_digest",
            name="uq_billing_producer_v1_receipt_link",
        ),
        sa.ForeignKeyConstraint(
            ["build_attestation_id", "build_attestation_digest"],
            [
                "billing_producer_v1_build_attestations.id",
                "billing_producer_v1_build_attestations.envelope_digest",
            ],
            name="fk_billing_producer_v1_receipt_attestation",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_billing_producer_v1_receipts_operation",
        "billing_producer_v1_receipts",
        ["operation_id", "receipt_type"],
    )

    op.create_table(
        "billing_producer_v1_active_pairs",
        sa.Column("environment", sa.String(20), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_digest", sa.String(64), nullable=False),
        sa.Column(
            "verification_receipt_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("verification_receipt_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "environment = 'staging'", name="ck_billing_producer_v1_pair_environment"
        ),
        sa.CheckConstraint("revision > 0", name="ck_billing_producer_v1_pair_revision"),
        sa.ForeignKeyConstraint(
            ["receipt_id", "operation_id", "receipt_digest"],
            [
                "billing_producer_v1_receipts.id",
                "billing_producer_v1_receipts.operation_id",
                "billing_producer_v1_receipts.envelope_digest",
            ],
            name="fk_billing_producer_v1_pair_receipt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["verification_receipt_id", "operation_id", "verification_receipt_digest"],
            [
                "billing_producer_v1_receipts.id",
                "billing_producer_v1_receipts.operation_id",
                "billing_producer_v1_receipts.envelope_digest",
            ],
            name="fk_billing_producer_v1_pair_verification",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "environment", name="uq_billing_producer_v1_pair_environment"
        ),
    )

    op.execute("""
        CREATE FUNCTION reject_billing_producer_v1_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
            RAISE EXCEPTION 'canonical billing producer artifacts are append-only';
        END;
        $function$
        """)
    for table in (
        "billing_producer_v1_grants",
        "billing_producer_v1_grant_consumptions",
        "billing_producer_v1_grant_revocations",
        "billing_producer_v1_build_attestations",
        "billing_producer_v1_receipts",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_billing_producer_v1_mutation()"
        )

    op.execute("""
        CREATE FUNCTION validate_billing_producer_v1_grant_event()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE grant_row billing_producer_v1_grants%ROWTYPE;
        BEGIN
            SELECT * INTO grant_row FROM billing_producer_v1_grants
             WHERE id = NEW.grant_id AND envelope_digest = NEW.grant_digest
               AND operation_id = NEW.operation_id FOR UPDATE;
            IF grant_row.id IS NULL THEN
                RAISE EXCEPTION 'grant event does not match immutable grant';
            END IF;
            IF TG_TABLE_NAME = 'billing_producer_v1_grant_consumptions' THEN
                IF NEW.executor_user_id <> grant_row.executor_user_id
                   OR NEW.consumed_at < grant_row.issued_at
                   OR NEW.consumed_at >= grant_row.expires_at THEN
                    RAISE EXCEPTION 'consumption actor or time does not match grant';
                END IF;
            ELSIF TG_TABLE_NAME = 'billing_producer_v1_grant_revocations' THEN
                IF NEW.revoked_at < grant_row.issued_at THEN
                    RAISE EXCEPTION 'revocation time precedes grant';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $function$
        """)
    op.execute(
        "CREATE TRIGGER trg_billing_producer_v1_consumption_validate "
        "BEFORE INSERT ON billing_producer_v1_grant_consumptions FOR EACH ROW "
        "EXECUTE FUNCTION validate_billing_producer_v1_grant_event()"
    )
    op.execute(
        "CREATE TRIGGER trg_billing_producer_v1_revocation_validate "
        "BEFORE INSERT ON billing_producer_v1_grant_revocations FOR EACH ROW "
        "EXECUTE FUNCTION validate_billing_producer_v1_grant_event()"
    )

    op.execute("""
        CREATE FUNCTION validate_billing_producer_v1_active_pair()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
            deploy_row billing_producer_v1_receipts%ROWTYPE;
            verify_row billing_producer_v1_receipts%ROWTYPE;
        BEGIN
            SELECT * INTO deploy_row FROM billing_producer_v1_receipts
             WHERE id = NEW.receipt_id AND operation_id = NEW.operation_id
               AND envelope_digest = NEW.receipt_digest;
            SELECT * INTO verify_row FROM billing_producer_v1_receipts
             WHERE id = NEW.verification_receipt_id AND operation_id = NEW.operation_id
               AND envelope_digest = NEW.verification_receipt_digest;
            IF deploy_row.id IS NULL OR verify_row.id IS NULL
               OR deploy_row.receipt_type <> 'billing_producer_deployed.v1'
               OR verify_row.receipt_type <> 'billing_producer_deploy_verified.v1'
               OR deploy_row.environment <> NEW.environment
               OR verify_row.environment <> NEW.environment
               OR deploy_row.operational_role <> 'production-serving'
               OR verify_row.operational_role <> deploy_row.operational_role
               OR deploy_row.deployment_id <> verify_row.deployment_id
               OR deploy_row.task_set_id <> verify_row.task_set_id
               OR deploy_row.config_generation <> verify_row.config_generation
               OR deploy_row.compatibility_generation <> verify_row.compatibility_generation
               OR deploy_row.app_digest <> verify_row.app_digest
               OR deploy_row.bundle_digest <> verify_row.bundle_digest
               OR deploy_row.readback_digest <> verify_row.readback_digest
               OR deploy_row.build_attestation_id <> verify_row.build_attestation_id
               OR deploy_row.build_attestation_digest <> verify_row.build_attestation_digest
               OR deploy_row.executor_user_id = verify_row.verifier_user_id
               OR verify_row.verifier_user_id IS NULL
               OR deploy_row.expires_at <= clock_timestamp()
               OR verify_row.expires_at <= clock_timestamp() THEN
                RAISE EXCEPTION 'canonical active pair failed verified receipt binding';
            END IF;
            IF TG_OP = 'INSERT' AND NEW.revision <> 1 THEN
                RAISE EXCEPTION 'initial canonical pair revision must be one';
            END IF;
            IF TG_OP = 'UPDATE' AND NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'canonical pair revision must advance exactly once';
            END IF;
            RETURN NEW;
        END;
        $function$
        """)
    op.execute(
        "CREATE TRIGGER trg_billing_producer_v1_active_pair_validate "
        "BEFORE INSERT OR UPDATE ON billing_producer_v1_active_pairs FOR EACH ROW "
        "EXECUTE FUNCTION validate_billing_producer_v1_active_pair()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_billing_producer_v1_active_pair_validate ON billing_producer_v1_active_pairs"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_billing_producer_v1_active_pair()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_billing_producer_v1_revocation_validate ON billing_producer_v1_grant_revocations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_billing_producer_v1_consumption_validate ON billing_producer_v1_grant_consumptions"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_billing_producer_v1_grant_event()")
    for table in (
        "billing_producer_v1_grants",
        "billing_producer_v1_grant_consumptions",
        "billing_producer_v1_grant_revocations",
        "billing_producer_v1_build_attestations",
        "billing_producer_v1_receipts",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_billing_producer_v1_mutation()")
    op.drop_table("billing_producer_v1_active_pairs")
    op.drop_index(
        "ix_billing_producer_v1_receipts_operation",
        table_name="billing_producer_v1_receipts",
    )
    op.drop_table("billing_producer_v1_receipts")
    op.drop_index(
        "ix_billing_producer_v1_attestation_operation",
        table_name="billing_producer_v1_build_attestations",
    )
    op.drop_table("billing_producer_v1_build_attestations")
    op.drop_table("billing_producer_v1_grant_revocations")
    op.drop_index(
        "ix_billing_producer_v1_consumptions_operation",
        table_name="billing_producer_v1_grant_consumptions",
    )
    op.drop_table("billing_producer_v1_grant_consumptions")
    op.drop_index(
        "ix_billing_producer_v1_grants_scope", table_name="billing_producer_v1_grants"
    )
    op.drop_table("billing_producer_v1_grants")
