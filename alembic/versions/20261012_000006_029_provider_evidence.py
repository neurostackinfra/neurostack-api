"""bind migration provider flips to immutable ASAAS evidence envelopes

Revision ID: 029
Revises: 028
Create Date: 2026-10-12 00:00:06
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "029"
down_revision: Union[str, None] = "028"
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
            ALTER TABLE billing_migration_receipts
                ADD COLUMN provider_event_id VARCHAR(160) NULL,
                ADD COLUMN provider_authority VARCHAR(80) NULL,
                ADD COLUMN provider_key_id VARCHAR(80) NULL,
                ADD COLUMN provider_operation VARCHAR(80) NULL,
                ADD COLUMN provider_signature VARCHAR(128) NULL,
                ADD COLUMN provider_observed_at TIMESTAMPTZ NULL,
                ADD COLUMN provider_expires_at TIMESTAMPTZ NULL,
                ADD COLUMN provider_snapshot_digest VARCHAR(64) NULL,
                ADD COLUMN provider_evidence_digest VARCHAR(64) NULL
            """,
            """
            ALTER TABLE billing_migration_receipts
                DROP CONSTRAINT ck_billing_migration_receipts_saga_shape,
                ADD CONSTRAINT ck_billing_migration_receipts_saga_shape CHECK (
                    (receipt_kind NOT IN ('cancel_observation', 'provider_flip',
                                         'recreate_authorized'))
                    OR (receipt_kind = 'cancel_observation'
                        AND provider = 'abacatepay'
                        AND external_reference IS NOT NULL
                        AND status = 'cancelled')
                    OR (receipt_kind IN ('provider_flip', 'recreate_authorized')
                        AND provider = 'asaas'
                        AND external_reference IS NOT NULL
                        AND ((receipt_kind = 'provider_flip' AND status = 'verified')
                             OR (receipt_kind = 'recreate_authorized' AND status = 'absent'))
                        AND provider_event_id IS NOT NULL
                        AND provider_key_id IS NOT NULL
                        AND provider_operation IS NOT NULL
                        AND provider_authority = 'asaas.api.v3'
                        AND provider_signature IS NOT NULL
                        AND provider_observed_at IS NOT NULL
                        AND provider_expires_at IS NOT NULL
                        AND provider_snapshot_digest IS NOT NULL
                        AND provider_evidence_digest IS NOT NULL)
                ),
                ADD CONSTRAINT ck_billing_migration_receipts_provider_expiry CHECK (
                    provider_expires_at IS NULL
                    OR provider_observed_at IS NULL
                    OR provider_expires_at > provider_observed_at
                ),
                ADD CONSTRAINT ck_billing_migration_receipts_provider_evidence CHECK (
                    provider_event_id IS NULL
                    OR (
                        provider_event_id ~ '^[A-Za-z0-9._:-]{1,160}$'
                        AND provider_authority IS NOT NULL
                        AND provider_key_id ~ '^[A-Za-z0-9._:-]{1,80}$'
                        AND provider_operation IN (
                            'subscription.migration.provider_flip',
                            'subscription.migration.absence')
                        AND provider_signature ~ '^[0-9a-f]{128}$'
                        AND provider_snapshot_digest ~ '^[0-9a-f]{64}$'
                        AND provider_evidence_digest ~ '^[0-9a-f]{64}$'
                    )
                )
            """,
            """
            ALTER TABLE billing_migration_receipts
                ADD CONSTRAINT uq_billing_migration_receipts_provider_event
                UNIQUE (provider, provider_event_id)
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_provider_evidence()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE migration billing_migrations%ROWTYPE;
                    expected_snapshot_digest TEXT;
                    expected_evidence_digest TEXT;
            BEGIN
                IF NEW.receipt_kind NOT IN ('provider_flip', 'recreate_authorized') THEN
                    RETURN NEW;
                END IF;
                SELECT * INTO migration FROM billing_migrations
                WHERE id = NEW.migration_id AND builder_id = NEW.builder_id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'provider evidence migration target missing';
                END IF;
                expected_snapshot_digest := encode(sha256(convert_to(
                    jsonb_build_object(
                        'migration_id', migration.id,
                        'builder_id', migration.builder_id,
                        'subscription_id', migration.subscription_id,
                        'operation_id', migration.operation_id,
                        'source_roster_revision', migration.source_roster_revision,
                        'target_provider', migration.target_provider,
                        'target_external_subscription_id',
                            migration.target_external_subscription_id,
                        'target_catalog_version_id', migration.target_catalog_version_id,
                        'target_offer_id', migration.target_offer_id,
                        'target_plan', migration.target_plan,
                        'target_billing_cycle', migration.target_billing_cycle,
                        'target_coupon_code', migration.target_coupon_code,
                        'target_amount_cents', migration.target_amount_cents,
                        'target_snapshot', migration.target_snapshot
                    )::text, 'UTF8')), 'hex');
                expected_evidence_digest := encode(sha256(convert_to(
                    jsonb_build_object(
                        'authority', NEW.provider_authority,
                        'event_id', NEW.provider_event_id,
                        'expires_at', NEW.provider_expires_at,
                        'external_reference', NEW.external_reference,
                        'migration_id', NEW.migration_id,
                        'observed_at', NEW.provider_observed_at,
                        'operation_id', NEW.operation_id,
                        'provider', NEW.provider,
                        'operation', NEW.provider_operation,
                        'snapshot_digest', NEW.provider_snapshot_digest
                    )::text, 'UTF8')), 'hex');
                IF migration.operation_id IS DISTINCT FROM NEW.operation_id
                   OR migration.target_provider IS DISTINCT FROM NEW.provider
                   OR NEW.external_reference IS DISTINCT FROM
                       COALESCE(migration.target_external_subscription_id,
                                'migration:' || migration.id::text)
                   OR NEW.provider_authority IS DISTINCT FROM 'asaas.api.v3'
                   OR NEW.provider_key_id IS NULL
                   OR NEW.provider_operation NOT IN (
                       'subscription.migration.provider_flip',
                       'subscription.migration.absence')
                   OR NEW.provider_event_id IS NULL
                   OR NEW.provider_observed_at IS NULL
                   OR NEW.provider_expires_at IS NULL
                   OR NEW.provider_observed_at < migration.created_at
                   OR NEW.provider_observed_at > NEW.observed_at
                   OR NEW.provider_expires_at <= NEW.provider_observed_at
                   OR NEW.provider_expires_at <= clock_timestamp()
                   OR NEW.provider_snapshot_digest IS DISTINCT FROM
                       expected_snapshot_digest
                   OR NEW.provider_evidence_digest IS DISTINCT FROM
                       expected_evidence_digest
                   OR (NEW.receipt_kind = 'provider_flip'
                       AND (migration.state IS DISTINCT FROM 'legacy_cancelled'
                            OR NEW.provider_operation IS DISTINCT FROM
                               'subscription.migration.provider_flip'))
                   OR (NEW.receipt_kind = 'recreate_authorized'
                       AND (migration.state NOT IN ('recreate_pending', 'legacy_cancelled')
                            OR NEW.provider_operation IS DISTINCT FROM
                               'subscription.migration.absence'))
                THEN
                    RAISE EXCEPTION 'provider evidence envelope is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_provider_evidence_validate
            BEFORE INSERT ON billing_migration_receipts
            FOR EACH ROW EXECUTE FUNCTION validate_billing_provider_evidence()
            """,
            """
            DROP TRIGGER IF EXISTS trg_billing_migration_receipt_validate
                ON billing_migration_receipts
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_migration_receipt()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE migration billing_migrations%ROWTYPE;
                    subscription billing_subscriptions%ROWTYPE;
                    current_revision BIGINT;
                    expected_digest TEXT;
            BEGIN
                PERFORM pg_advisory_xact_lock(3486705124568449331);
                SELECT billing_roster_revision INTO current_revision
                FROM billing_roster_control WHERE singleton = true FOR UPDATE;
                SELECT * INTO migration FROM billing_migrations
                WHERE id = NEW.migration_id AND builder_id = NEW.builder_id FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'migration receipt target missing'; END IF;
                SELECT * INTO subscription FROM billing_subscriptions
                WHERE id = migration.subscription_id
                  AND builder_id = migration.builder_id FOR UPDATE;
                IF NEW.receipt_kind IN ('provider_flip', 'recreate_authorized')
                   AND migration.operation_id IS DISTINCT FROM NEW.operation_id THEN
                    RAISE EXCEPTION 'migration receipt operation mismatch';
                END IF;
                IF NEW.receipt_kind = 'cancel_observation' THEN
                    expected_digest := encode(sha256(convert_to(jsonb_build_object(
                      'migration_id', migration.id, 'builder_id', migration.builder_id,
                      'subscription_id', migration.subscription_id,
                      'operation_id', migration.operation_id,
                      'legacy_external_subscription_id', subscription.external_subscription_id,
                      'provider', 'abacatepay', 'status', 'cancelled',
                      'observed_at', NEW.observed_at)::text,'UTF8')),'hex');
                    IF migration.state IS DISTINCT FROM 'legacy_cancel_pending'
                       OR subscription.provider IS NOT NULL
                       OR subscription.status IS DISTINCT FROM 'legacy_cancel_pending'
                       OR NEW.provider IS DISTINCT FROM 'abacatepay'
                       OR NEW.status IS DISTINCT FROM 'cancelled'
                       OR NEW.external_reference IS DISTINCT FROM subscription.external_subscription_id
                       OR NEW.observed_at < migration.created_at
                       OR NEW.observed_at > clock_timestamp()
                       OR NEW.content_sha256 IS DISTINCT FROM expected_digest THEN
                        RAISE EXCEPTION 'invalid cancellation observation';
                    END IF;
                END IF;
                IF NEW.receipt_kind IN ('provider_flip', 'recreate_authorized')
                   AND (
                       migration.source_roster_revision <> subscription.roster_revision
                       OR migration.source_roster_revision <> current_revision
                   )
                   AND NEW.receipt_kind = 'provider_flip' THEN
                    RAISE EXCEPTION 'migration receipt source revision is stale';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_migration_receipt_validate
            BEFORE INSERT ON billing_migration_receipts
            FOR EACH ROW EXECUTE FUNCTION validate_billing_migration_receipt()
            """,
        )
    )


def downgrade() -> None:
    _execute(
        (
            """
            DROP TRIGGER IF EXISTS trg_billing_provider_evidence_validate
                ON billing_migration_receipts
            """,
            "DROP FUNCTION IF EXISTS validate_billing_provider_evidence()",
            """
            ALTER TABLE billing_migration_receipts
                DROP CONSTRAINT uq_billing_migration_receipts_provider_event,
                DROP CONSTRAINT ck_billing_migration_receipts_provider_evidence,
                DROP CONSTRAINT ck_billing_migration_receipts_provider_expiry,
                DROP CONSTRAINT ck_billing_migration_receipts_saga_shape,
                ADD CONSTRAINT ck_billing_migration_receipts_saga_shape CHECK (
                    (receipt_kind NOT IN ('cancel_observation', 'provider_flip'))
                    OR (receipt_kind = 'cancel_observation'
                        AND provider = 'abacatepay'
                        AND external_reference IS NOT NULL
                        AND status = 'cancelled')
                    OR (receipt_kind = 'provider_flip'
                        AND provider = 'asaas'
                        AND external_reference IS NOT NULL
                        AND status = 'verified')
                )
            """,
            """
            ALTER TABLE billing_migration_receipts
                DROP COLUMN provider_event_id,
                DROP COLUMN provider_authority,
                DROP COLUMN provider_signature,
                DROP COLUMN provider_key_id,
                DROP COLUMN provider_operation,
                DROP COLUMN provider_observed_at,
                DROP COLUMN provider_expires_at,
                DROP COLUMN provider_snapshot_digest,
                DROP COLUMN provider_evidence_digest
            """,
        )
    )
