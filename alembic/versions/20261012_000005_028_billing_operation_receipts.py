"""add signed operator assignments and single-use billing receipts

Revision ID: 028
Revises: 027
Create Date: 2026-10-12 00:00:05
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels = None
depends_on = None


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            bind_names = ", ".join(sorted(ddl._bindparams))
            raise ValueError(
                f"raw migration DDL contains bind parameters: {bind_names}"
            )
        op.execute(ddl)


def upgrade() -> None:
    _execute(
        (
            """
            CREATE TABLE billing_operation_assignments (
                id UUID PRIMARY KEY,
                operation_id UUID NOT NULL,
                environment VARCHAR(20) NOT NULL,
                scope VARCHAR(30) NOT NULL,
                role VARCHAR(20) NOT NULL,
                actor_user_id UUID NOT NULL,
                payload_digest VARCHAR(64) NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                active BOOLEAN NOT NULL DEFAULT true,
                consumed_at TIMESTAMPTZ NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_operation_assignments_environment CHECK (
                    environment IN ('development', 'staging', 'production', 'test')
                ),
                CONSTRAINT ck_billing_operation_assignments_scope CHECK (
                    scope IN ('migration', 'rollout', 'topup_capability', 'incident')
                ),
                CONSTRAINT ck_billing_operation_assignments_role CHECK (
                    role IN ('deployer', 'approver', 'reviewer')
                ),
                CONSTRAINT ck_billing_operation_assignments_expiry CHECK (
                    expires_at > created_at
                ),
                CONSTRAINT ck_billing_operation_assignments_digest CHECK (
                    payload_digest ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT uq_billing_operation_assignments_role UNIQUE (
                    operation_id, role
                ),
                CONSTRAINT uq_billing_operation_assignments_actor UNIQUE (
                    operation_id, actor_user_id
                )
            )
            """,
            """
            CREATE TABLE billing_operation_receipts (
                id UUID PRIMARY KEY,
                assignment_id UUID NOT NULL,
                operation_id UUID NOT NULL,
                environment VARCHAR(20) NOT NULL,
                scope VARCHAR(30) NOT NULL,
                role VARCHAR(20) NOT NULL,
                actor_user_id UUID NOT NULL,
                nonce UUID NOT NULL,
                payload_digest VARCHAR(64) NOT NULL,
                deployment_digest VARCHAR(64) NOT NULL,
                budget_digest VARCHAR(64) NOT NULL,
                from_state VARCHAR(40) NOT NULL,
                to_state VARCHAR(40) NOT NULL,
                evidence_type VARCHAR(30) NULL,
                request_id UUID NULL,
                inbox_high_watermark BIGINT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                signature_sha256 VARCHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_operation_receipts_environment CHECK (
                    environment IN ('development', 'staging', 'production', 'test')
                ),
                CONSTRAINT ck_billing_operation_receipts_scope CHECK (
                    scope IN ('migration', 'rollout', 'topup_capability', 'incident')
                ),
                CONSTRAINT ck_billing_operation_receipts_role CHECK (
                    role IN ('deployer', 'approver', 'reviewer')
                ),
                CONSTRAINT ck_billing_operation_receipts_evidence_type CHECK (
                    evidence_type IS NULL OR evidence_type IN (
                        'producer', 'credit', 'reconciliation', 'verification'
                    )
                ),
                CONSTRAINT ck_billing_operation_receipts_watermark CHECK (
                    inbox_high_watermark IS NULL OR inbox_high_watermark >= 0
                ),
                CONSTRAINT ck_billing_operation_receipts_expiry CHECK (
                    expires_at > created_at
                ),
                CONSTRAINT ck_billing_operation_receipts_digests CHECK (
                    payload_digest ~ '^[0-9a-f]{64}$'
                    AND deployment_digest ~ '^[0-9a-f]{64}$'
                    AND budget_digest ~ '^[0-9a-f]{64}$'
                    AND signature_sha256 ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT uq_billing_operation_receipts_nonce UNIQUE (nonce),
                CONSTRAINT uq_billing_operation_receipts_assignment UNIQUE (
                    assignment_id
                ),
                CONSTRAINT fk_billing_operation_receipts_assignment FOREIGN KEY (
                    assignment_id
                ) REFERENCES billing_operation_assignments(id) ON DELETE RESTRICT
            )
            """,
            """
            ALTER TABLE billing_operation_receipts
              ADD CONSTRAINT uq_billing_operation_receipts_environment_id
              UNIQUE (environment, id)
            """,
            """
            CREATE TABLE billing_rollout_readiness_receipts (
                id UUID PRIMARY KEY,
                environment VARCHAR(20) NOT NULL,
                receipt_kind VARCHAR(20) NOT NULL,
                rollout_revision BIGINT NOT NULL,
                status VARCHAR(20) NOT NULL,
                content_sha256 VARCHAR(64) NOT NULL,
                operation_id UUID NOT NULL,
                operation_receipt_id UUID NOT NULL,
                provider_receipt_id UUID NOT NULL,
                rule_digest VARCHAR(64) NOT NULL,
                bundle_digest VARCHAR(64) NOT NULL,
                app_digest VARCHAR(64) NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT ck_billing_rollout_readiness_environment CHECK (
                    environment IN ('development', 'staging', 'production', 'test')
                ),
                CONSTRAINT ck_billing_rollout_readiness_kind CHECK (
                    receipt_kind IN ('readiness', 'backlog', 'provisioning', 'smoke')
                ),
                CONSTRAINT ck_billing_rollout_readiness_status CHECK (status = 'passed'),
                CONSTRAINT ck_billing_rollout_readiness_digest CHECK (
                    content_sha256 ~ '^[0-9a-f]{64}$'
                    AND rule_digest ~ '^[0-9a-f]{64}$'
                    AND bundle_digest ~ '^[0-9a-f]{64}$'
                    AND app_digest ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT ck_billing_rollout_readiness_fresh CHECK (expires_at > observed_at),
                CONSTRAINT uq_billing_rollout_readiness_revision UNIQUE (
                    environment, receipt_kind, rollout_revision
                ),
                CONSTRAINT fk_billing_rollout_readiness_operation_receipt
                    FOREIGN KEY (environment, operation_receipt_id)
                    REFERENCES billing_operation_receipts(environment, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_rollout_readiness_provider_receipt
                    FOREIGN KEY (environment, provider_receipt_id)
                    REFERENCES billing_operation_receipts(environment, id)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE billing_topup_capability_bindings (
                id UUID PRIMARY KEY,
                environment VARCHAR(20) NOT NULL,
                capability_receipt_id UUID NOT NULL,
                producer_receipt_id UUID NOT NULL,
                credit_receipt_id UUID NOT NULL,
                reconciliation_receipt_id UUID NOT NULL,
                verification_receipt_id UUID NOT NULL,
                producer_digest VARCHAR(64) NOT NULL,
                credit_digest VARCHAR(64) NOT NULL,
                reconciliation_digest VARCHAR(64) NOT NULL,
                verification_digest VARCHAR(64) NOT NULL,
                producer_actor_id UUID NOT NULL,
                credit_actor_id UUID NOT NULL,
                reconciliation_actor_id UUID NOT NULL,
                verification_actor_id UUID NOT NULL,
                verified_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_topup_capability_bindings_environment CHECK (
                    environment IN ('development', 'staging', 'production', 'test')
                ),
                CONSTRAINT ck_billing_topup_capability_bindings_digests CHECK (
                    producer_digest ~ '^[0-9a-f]{64}$'
                    AND credit_digest ~ '^[0-9a-f]{64}$'
                    AND reconciliation_digest ~ '^[0-9a-f]{64}$'
                    AND verification_digest ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT fk_billing_topup_capability_bindings_capability
                    FOREIGN KEY (environment, capability_receipt_id)
                    REFERENCES billing_topup_capability_receipts(environment, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_topup_bindings_producer_receipt
                    FOREIGN KEY (environment, producer_receipt_id)
                    REFERENCES billing_operation_receipts(environment, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_topup_bindings_credit_receipt
                    FOREIGN KEY (environment, credit_receipt_id)
                    REFERENCES billing_operation_receipts(environment, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_topup_bindings_reconciliation_receipt
                    FOREIGN KEY (environment, reconciliation_receipt_id)
                    REFERENCES billing_operation_receipts(environment, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_topup_bindings_verification_receipt
                    FOREIGN KEY (environment, verification_receipt_id)
                    REFERENCES billing_operation_receipts(environment, id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_topup_capability_bindings_capability UNIQUE (
                    environment, capability_receipt_id
                ),
                CONSTRAINT uq_billing_topup_bindings_producer UNIQUE (
                    environment, producer_receipt_id
                ),
                CONSTRAINT uq_billing_topup_bindings_credit UNIQUE (
                    environment, credit_receipt_id
                ),
                CONSTRAINT uq_billing_topup_bindings_reconciliation UNIQUE (
                    environment, reconciliation_receipt_id
                ),
                CONSTRAINT uq_billing_topup_bindings_verification UNIQUE (
                    environment, verification_receipt_id
                )
            )
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_operation_receipt()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE assignment billing_operation_assignments%ROWTYPE;
            BEGIN
                SELECT * INTO assignment
                FROM billing_operation_assignments
                WHERE id = NEW.assignment_id
                FOR UPDATE;
                IF NOT FOUND
                   OR NOT assignment.active
                   OR assignment.consumed_at IS NOT NULL
                   OR assignment.expires_at <= clock_timestamp()
                   OR NEW.expires_at <= clock_timestamp()
                   OR assignment.operation_id IS DISTINCT FROM NEW.operation_id
                   OR assignment.environment IS DISTINCT FROM NEW.environment
                   OR assignment.scope IS DISTINCT FROM NEW.scope
                   OR assignment.role IS DISTINCT FROM NEW.role
                   OR assignment.actor_user_id IS DISTINCT FROM NEW.actor_user_id
                   OR assignment.payload_digest IS DISTINCT FROM NEW.payload_digest
                THEN
                    RAISE EXCEPTION 'billing operation receipt assignment is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                UPDATE billing_operation_assignments
                SET active = false, consumed_at = clock_timestamp(), updated_at = now()
                WHERE id = assignment.id AND active AND consumed_at IS NULL;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'billing operation assignment was already consumed'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_operation_receipt_assignment
            BEFORE INSERT ON billing_operation_receipts
            FOR EACH ROW EXECUTE FUNCTION validate_billing_operation_receipt()
            """,
            """
            CREATE OR REPLACE FUNCTION protect_billing_operation_assignment()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'billing operation assignments are not deletable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF ROW(
                    OLD.operation_id, OLD.environment, OLD.scope, OLD.role,
                    OLD.actor_user_id, OLD.payload_digest, OLD.expires_at
                ) IS DISTINCT FROM ROW(
                    NEW.operation_id, NEW.environment, NEW.scope, NEW.role,
                    NEW.actor_user_id, NEW.payload_digest, NEW.expires_at
                )
                   OR (OLD.active = false AND NEW.active = true)
                   OR (OLD.consumed_at IS NOT NULL
                       AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at)
                THEN
                    RAISE EXCEPTION 'billing operation assignment is immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_operation_assignment_history
            BEFORE UPDATE OR DELETE ON billing_operation_assignments
            FOR EACH ROW EXECUTE FUNCTION protect_billing_operation_assignment()
            """,
            """
            CREATE OR REPLACE FUNCTION protect_billing_operation_receipt()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'billing operation receipts are append-only'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF ROW(
                    OLD.id, OLD.assignment_id, OLD.operation_id, OLD.environment,
                    OLD.scope, OLD.role, OLD.actor_user_id, OLD.nonce,
                    OLD.payload_digest, OLD.deployment_digest, OLD.budget_digest,
                    OLD.from_state, OLD.to_state, OLD.evidence_type, OLD.request_id,
                    OLD.inbox_high_watermark, OLD.expires_at,
                    OLD.signature_sha256, OLD.created_at
                ) IS DISTINCT FROM ROW(
                    NEW.id, NEW.assignment_id, NEW.operation_id, NEW.environment,
                    NEW.scope, NEW.role, NEW.actor_user_id, NEW.nonce,
                    NEW.payload_digest, NEW.deployment_digest, NEW.budget_digest,
                    NEW.from_state, NEW.to_state, NEW.evidence_type, NEW.request_id,
                    NEW.inbox_high_watermark, NEW.expires_at,
                    NEW.signature_sha256, NEW.created_at
                )
                   OR OLD.applied_at IS NOT NULL
                   OR NEW.applied_at IS NULL
                THEN
                    RAISE EXCEPTION 'billing operation receipt is immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_operation_receipt_history
            BEFORE UPDATE OR DELETE ON billing_operation_receipts
            FOR EACH ROW EXECUTE FUNCTION protect_billing_operation_receipt()
            """,
            """
            CREATE TRIGGER trg_billing_topup_capability_binding_history
            BEFORE UPDATE OR DELETE ON billing_topup_capability_bindings
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
            """
            CREATE TRIGGER trg_billing_rollout_readiness_receipt_history
            BEFORE UPDATE OR DELETE ON billing_rollout_readiness_receipts
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
            """
            CREATE OR REPLACE FUNCTION protect_bound_billing_migration_target()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF OLD.state IS DISTINCT FROM NEW.state
                   AND NEW.state IN (
                       'legacy_cancel_pending', 'legacy_cancelled', 'migrated'
                   )
                   AND pg_trigger_depth() = 1
                   AND NOT (
                       OLD.state = 'recreate_pending'
                       AND NEW.state = 'legacy_cancelled'
                       AND EXISTS (
                           SELECT 1 FROM billing_migration_receipts receipt
                           WHERE receipt.migration_id = OLD.id
                             AND receipt.builder_id = OLD.builder_id
                             AND receipt.operation_id = OLD.operation_id
                             AND receipt.receipt_kind = 'recreate_authorized'
                             AND receipt.status = 'absent'
                       )
                   )
                THEN
                    RAISE EXCEPTION 'migration saga state is subscription-owned';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM billing_migration_receipts
                    WHERE migration_id = OLD.id
                      AND builder_id = OLD.builder_id
                      AND receipt_kind = 'provider_flip'
                ) AND ROW(
                    OLD.id, OLD.builder_id, OLD.subscription_id,
                    OLD.operation_id, OLD.source_roster_revision,
                    OLD.target_provider, OLD.target_external_subscription_id,
                    OLD.target_catalog_version_id, OLD.target_offer_id,
                    OLD.target_plan, OLD.target_billing_cycle,
                    OLD.target_coupon_code, OLD.target_amount_cents,
                    OLD.target_snapshot
                ) IS DISTINCT FROM ROW(
                    NEW.id, NEW.builder_id, NEW.subscription_id,
                    NEW.operation_id, NEW.source_roster_revision,
                    NEW.target_provider, NEW.target_external_subscription_id,
                    NEW.target_catalog_version_id, NEW.target_offer_id,
                    NEW.target_plan, NEW.target_billing_cycle,
                    NEW.target_coupon_code, NEW.target_amount_cents,
                    NEW.target_snapshot
                ) THEN
                    RAISE EXCEPTION 'receipt-bound migration target is immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
        )
    )


def downgrade() -> None:
    _execute(
        (
            "DROP TRIGGER IF EXISTS trg_billing_operation_receipt_history ON "
            "billing_operation_receipts",
            "DROP FUNCTION IF EXISTS protect_billing_operation_receipt()",
            "DROP TRIGGER IF EXISTS trg_billing_operation_assignment_history ON "
            "billing_operation_assignments",
            "DROP FUNCTION IF EXISTS protect_billing_operation_assignment()",
            "DROP TRIGGER IF EXISTS trg_billing_operation_receipt_assignment ON "
            "billing_operation_receipts",
            "DROP FUNCTION IF EXISTS validate_billing_operation_receipt()",
            "DROP TRIGGER IF EXISTS trg_billing_topup_capability_binding_history ON "
            "billing_topup_capability_bindings",
            "DROP TRIGGER IF EXISTS trg_billing_rollout_readiness_receipt_history ON "
            "billing_rollout_readiness_receipts",
            "DROP TABLE billing_topup_capability_bindings",
            "DROP TABLE billing_rollout_readiness_receipts",
            "DROP TABLE billing_operation_receipts",
            "DROP TABLE billing_operation_assignments",
            """
            CREATE OR REPLACE FUNCTION protect_bound_billing_migration_target()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF OLD.state IS DISTINCT FROM NEW.state
                   AND NEW.state IN (
                       'legacy_cancel_pending', 'legacy_cancelled', 'migrated')
                   AND pg_trigger_depth() = 1 THEN
                    RAISE EXCEPTION 'migration saga state is subscription-owned';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM billing_migration_receipts
                    WHERE migration_id = OLD.id
                      AND builder_id = OLD.builder_id
                      AND receipt_kind = 'provider_flip'
                ) AND ROW(
                    OLD.id, OLD.builder_id, OLD.subscription_id,
                    OLD.operation_id, OLD.source_roster_revision,
                    OLD.target_provider, OLD.target_external_subscription_id,
                    OLD.target_catalog_version_id, OLD.target_offer_id,
                    OLD.target_plan, OLD.target_billing_cycle,
                    OLD.target_coupon_code, OLD.target_amount_cents,
                    OLD.target_snapshot
                ) IS DISTINCT FROM ROW(
                    NEW.id, NEW.builder_id, NEW.subscription_id,
                    NEW.operation_id, NEW.source_roster_revision,
                    NEW.target_provider, NEW.target_external_subscription_id,
                    NEW.target_catalog_version_id, NEW.target_offer_id,
                    NEW.target_plan, NEW.target_billing_cycle,
                    NEW.target_coupon_code, NEW.target_amount_cents,
                    NEW.target_snapshot
                ) THEN
                    RAISE EXCEPTION 'receipt-bound migration target is immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
        )
    )
