"""canonical provider-evidence digest and two-read recreate absence evidence

The provider-evidence digest enforced since 029/030 was computed from
``jsonb_build_object(...)::text``.  That text depends on JSONB key ordering,
JSONB whitespace and, for ``timestamptz`` values, on the session ``TimeZone``,
so neither the Python verifier nor an external signer could reproduce it.
This revision replaces it with one explicit canonical form shared by Python
(``provider_evidence_digest``) and PostgreSQL: compact, key-sorted JSON over
values that CHECK constraints restrict to ``[A-Za-z0-9._:-]``, timestamps in
UTC ISO-8601 exactly as ``datetime.isoformat()`` renders them.

It also adds ``billing_migration_absence_reads``: the two provider-signed
empty ASAAS reads that must precede a ``recreate_authorized`` receipt and the
``recreate_pending -> legacy_cancelled`` reopen (DESIGN decision 15).

Revision ID: 032
Revises: 031
Create Date: 2026-10-12 00:00:09
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "032"
down_revision: Union[str, None] = "031"
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


_MIGRATION_SNAPSHOT_DIGEST = """
    encode(sha256(convert_to(jsonb_build_object(
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
    )::text, 'UTF8')), 'hex')
"""


def upgrade() -> None:
    _execute(
        (
            """
            CREATE OR REPLACE FUNCTION billing_evidence_timestamp(value TIMESTAMPTZ)
            RETURNS TEXT LANGUAGE sql STABLE STRICT AS $function$
                SELECT to_char(value AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
                    || CASE
                           WHEN to_char(value AT TIME ZONE 'UTC', 'US') = '000000'
                           THEN ''
                           ELSE '.' || to_char(value AT TIME ZONE 'UTC', 'US')
                       END
                    || '+00:00'
            $function$
            """,
            """
            CREATE OR REPLACE FUNCTION billing_provider_evidence_digest(
                authority TEXT,
                event_id TEXT,
                expires_at TIMESTAMPTZ,
                external_reference TEXT,
                migration_id UUID,
                observed_at TIMESTAMPTZ,
                operation TEXT,
                operation_id UUID,
                provider TEXT,
                snapshot_digest TEXT
            ) RETURNS TEXT LANGUAGE sql STABLE STRICT AS $function$
                SELECT CASE
                    WHEN authority !~ '^[A-Za-z0-9._:-]+$'
                      OR event_id !~ '^[A-Za-z0-9._:-]+$'
                      OR external_reference !~ '^[A-Za-z0-9._:-]+$'
                      OR operation !~ '^[A-Za-z0-9._:-]+$'
                      OR provider !~ '^[A-Za-z0-9._:-]+$'
                      OR snapshot_digest !~ '^[0-9a-f]{64}$'
                    THEN NULL
                    ELSE encode(sha256(convert_to(
                        '{"authority":"' || authority
                        || '","event_id":"' || event_id
                        || '","expires_at":"' || billing_evidence_timestamp(expires_at)
                        || '","external_reference":"' || external_reference
                        || '","migration_id":"' || migration_id::text
                        || '","observed_at":"' || billing_evidence_timestamp(observed_at)
                        || '","operation":"' || operation
                        || '","operation_id":"' || operation_id::text
                        || '","provider":"' || provider
                        || '","snapshot_digest":"' || snapshot_digest
                        || '"}', 'UTF8')), 'hex')
                END
            $function$
            """,
            """
            CREATE TABLE billing_migration_absence_reads (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL,
                migration_id UUID NOT NULL,
                operation_id UUID NOT NULL,
                read_no INTEGER NOT NULL,
                prior_checkout_session_id UUID NULL,
                convergence_limit_at TIMESTAMPTZ NOT NULL,
                provider VARCHAR(20) NOT NULL,
                external_reference VARCHAR(160) NOT NULL,
                provider_event_id VARCHAR(160) NOT NULL,
                provider_authority VARCHAR(80) NOT NULL,
                provider_key_id VARCHAR(80) NOT NULL,
                provider_operation VARCHAR(80) NOT NULL,
                provider_signature VARCHAR(128) NOT NULL,
                provider_observed_at TIMESTAMPTZ NOT NULL,
                provider_expires_at TIMESTAMPTZ NOT NULL,
                provider_snapshot_digest VARCHAR(64) NOT NULL,
                provider_evidence_digest VARCHAR(64) NOT NULL,
                status VARCHAR(20) NOT NULL,
                actor_user_id UUID NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_migration_absence_reads_read_no CHECK (
                    read_no IN (1, 2)
                ),
                CONSTRAINT ck_billing_migration_absence_reads_shape CHECK (
                    provider = 'asaas' AND status = 'absent'
                    AND provider_authority = 'asaas.api.v3'
                    AND provider_operation = 'subscription.migration.absence'
                ),
                CONSTRAINT ck_billing_migration_absence_reads_evidence CHECK (
                    provider_event_id ~ '^[A-Za-z0-9._:-]{1,160}$'
                    AND external_reference ~ '^[A-Za-z0-9._:-]{1,160}$'
                    AND provider_key_id ~ '^[A-Za-z0-9._:-]{1,80}$'
                    AND provider_signature ~ '^[0-9a-f]{128}$'
                    AND provider_snapshot_digest ~ '^[0-9a-f]{64}$'
                    AND provider_evidence_digest ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT ck_billing_migration_absence_reads_times CHECK (
                    provider_expires_at > provider_observed_at
                    AND provider_observed_at > convergence_limit_at
                    AND provider_observed_at <= observed_at
                ),
                CONSTRAINT fk_billing_migration_absence_reads_migration
                    FOREIGN KEY (builder_id, migration_id)
                    REFERENCES billing_migrations(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_migration_absence_reads_checkout
                    FOREIGN KEY (builder_id, prior_checkout_session_id)
                    REFERENCES billing_checkout_sessions(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_migration_absence_reads_slot UNIQUE (
                    migration_id, operation_id, read_no
                ),
                CONSTRAINT uq_billing_migration_absence_reads_event UNIQUE (
                    provider, provider_event_id
                )
            )
            """,
            f"""
            CREATE OR REPLACE FUNCTION validate_billing_migration_absence_read()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE migration billing_migrations%ROWTYPE;
                    subscription billing_subscriptions%ROWTYPE;
                    latest_attempt billing_checkout_sessions%ROWTYPE;
                    first_read billing_migration_absence_reads%ROWTYPE;
                    convergence_base TIMESTAMPTZ;
            BEGIN
                SELECT * INTO migration FROM billing_migrations
                WHERE id = NEW.migration_id AND builder_id = NEW.builder_id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'absence read migration target missing'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT * INTO subscription FROM billing_subscriptions
                WHERE id = migration.subscription_id
                  AND builder_id = migration.builder_id
                FOR UPDATE;
                SELECT * INTO latest_attempt FROM billing_checkout_sessions
                WHERE migration_id = migration.id
                  AND builder_id = migration.builder_id
                ORDER BY attempt_no DESC
                LIMIT 1;
                convergence_base := GREATEST(
                    migration.legacy_cancelled_at,
                    COALESCE(latest_attempt.created_at, migration.legacy_cancelled_at)
                );
                -- 90 minutes mirrors CHECKOUT_CONVERGENCE_LIMIT
                -- (ASAAS Checkout expiry 60 min + 30 min); the backend derives
                -- the exact limit, the database refuses anything earlier.
                IF migration.state NOT IN ('legacy_cancelled', 'recreate_pending')
                   OR migration.operation_id IS DISTINCT FROM NEW.operation_id
                   OR migration.legacy_cancelled_at IS NULL
                   OR subscription.id IS NULL
                   OR subscription.provider IS NOT NULL
                   OR subscription.is_active IS NOT FALSE
                   OR subscription.status IS DISTINCT FROM 'legacy_cancelled'
                   OR NEW.external_reference IS DISTINCT FROM
                       COALESCE(migration.target_external_subscription_id,
                                'migration:' || migration.id::text)
                   OR NEW.provider_snapshot_digest IS DISTINCT FROM
                       {_MIGRATION_SNAPSHOT_DIGEST}
                   OR NEW.provider_evidence_digest IS DISTINCT FROM
                       billing_provider_evidence_digest(
                           NEW.provider_authority, NEW.provider_event_id,
                           NEW.provider_expires_at, NEW.external_reference,
                           NEW.migration_id, NEW.provider_observed_at,
                           NEW.provider_operation, NEW.operation_id,
                           NEW.provider, NEW.provider_snapshot_digest)
                   OR NEW.provider_observed_at < migration.created_at
                   OR NEW.provider_expires_at <= clock_timestamp()
                   OR NEW.observed_at > clock_timestamp()
                   OR NEW.prior_checkout_session_id IS DISTINCT FROM latest_attempt.id
                   OR NEW.convergence_limit_at <
                       convergence_base + interval '90 minutes'
                   OR EXISTS (
                       SELECT 1 FROM billing_checkout_sessions live
                       WHERE live.migration_id = migration.id
                         AND live.builder_id = migration.builder_id
                         AND live.state IN (
                             'creating', 'active', 'paid_pending_subscription',
                             'reconciliation_required', 'terminal_pending_readback')
                   )
                   OR EXISTS (
                       SELECT 1 FROM billing_migration_receipts receipt
                       WHERE receipt.provider = NEW.provider
                         AND receipt.provider_event_id = NEW.provider_event_id
                   )
                THEN
                    RAISE EXCEPTION 'absence read evidence is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.read_no = 2 THEN
                    SELECT * INTO first_read FROM billing_migration_absence_reads
                    WHERE migration_id = NEW.migration_id
                      AND builder_id = NEW.builder_id
                      AND operation_id = NEW.operation_id
                      AND read_no = 1;
                    -- 5 minutes mirrors CONVERGENCE_READ_SPACING.
                    IF NOT FOUND
                       OR first_read.provider_event_id = NEW.provider_event_id
                       OR NEW.provider_observed_at <
                           first_read.provider_observed_at + interval '5 minutes'
                       OR first_read.prior_checkout_session_id IS DISTINCT FROM
                           NEW.prior_checkout_session_id
                       OR first_read.convergence_limit_at IS DISTINCT FROM
                           NEW.convergence_limit_at
                    THEN
                        RAISE EXCEPTION 'second absence read must follow a distinct first read'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_migration_absence_read_validate
            BEFORE INSERT ON billing_migration_absence_reads
            FOR EACH ROW EXECUTE FUNCTION validate_billing_migration_absence_read()
            """,
            """
            CREATE TRIGGER trg_billing_migration_absence_reads_immutable
            BEFORE UPDATE OR DELETE ON billing_migration_absence_reads
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
            """
            CREATE TRIGGER trg_billing_migration_absence_reads_roster_fence
            BEFORE INSERT OR UPDATE OR DELETE ON billing_migration_absence_reads
            FOR EACH STATEMENT EXECUTE FUNCTION lock_billing_roster_statement()
            """,
            f"""
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
                WHERE id = NEW.migration_id AND builder_id = NEW.builder_id FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'provider evidence migration target missing';
                END IF;
                expected_snapshot_digest := {_MIGRATION_SNAPSHOT_DIGEST};
                expected_evidence_digest := billing_provider_evidence_digest(
                    NEW.provider_authority, NEW.provider_event_id,
                    NEW.provider_expires_at, NEW.external_reference,
                    NEW.migration_id, NEW.provider_observed_at,
                    NEW.provider_operation, NEW.operation_id,
                    NEW.provider, NEW.provider_snapshot_digest);
                IF migration.operation_id IS DISTINCT FROM NEW.operation_id
                   OR migration.target_provider IS DISTINCT FROM NEW.provider
                   OR NEW.external_reference IS DISTINCT FROM
                       COALESCE(migration.target_external_subscription_id,
                                'migration:' || migration.id::text)
                   OR NEW.provider_authority IS DISTINCT FROM 'asaas.api.v3'
                   OR NEW.provider_key_id IS NULL
                   OR NEW.provider_event_id IS NULL
                   OR NEW.provider_observed_at IS NULL
                   OR NEW.provider_expires_at IS NULL
                   OR NEW.provider_observed_at < migration.created_at
                   OR NEW.provider_observed_at > NEW.observed_at
                   OR NEW.provider_expires_at <= NEW.provider_observed_at
                   OR NEW.provider_expires_at <= clock_timestamp()
                   OR NEW.provider_snapshot_digest IS DISTINCT FROM expected_snapshot_digest
                   OR NEW.provider_evidence_digest IS DISTINCT FROM expected_evidence_digest
                   OR (NEW.receipt_kind = 'provider_flip' AND (
                       migration.state IS DISTINCT FROM 'legacy_cancelled'
                       OR NEW.provider_operation IS DISTINCT FROM
                          'subscription.migration.provider_flip'
                       OR EXISTS (
                           SELECT 1 FROM billing_migration_absence_reads absence
                           WHERE absence.provider = NEW.provider
                             AND absence.provider_event_id = NEW.provider_event_id
                       )))
                   OR (NEW.receipt_kind = 'recreate_authorized' AND (
                       migration.state NOT IN ('recreate_pending', 'legacy_cancelled')
                       OR NEW.provider_operation IS DISTINCT FROM
                          'subscription.migration.absence'
                       OR NOT EXISTS (
                           SELECT 1
                           FROM billing_migration_absence_reads second_read
                           JOIN billing_migration_absence_reads first_read
                             ON first_read.migration_id = second_read.migration_id
                            AND first_read.builder_id = second_read.builder_id
                            AND first_read.operation_id = second_read.operation_id
                            AND first_read.read_no = 1
                            AND first_read.provider_event_id
                                <> second_read.provider_event_id
                            AND first_read.provider_observed_at
                                < second_read.provider_observed_at
                           WHERE second_read.migration_id = NEW.migration_id
                             AND second_read.builder_id = NEW.builder_id
                             AND second_read.operation_id = NEW.operation_id
                             AND second_read.read_no = 2
                             AND second_read.provider_event_id = NEW.provider_event_id
                             AND second_read.provider_evidence_digest
                                 = NEW.provider_evidence_digest
                             AND second_read.provider_observed_at
                                 = NEW.provider_observed_at
                             AND second_read.provider_expires_at
                                 = NEW.provider_expires_at
                       )))
                THEN
                    RAISE EXCEPTION 'provider evidence envelope is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
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
                           SELECT 1
                           FROM billing_migration_receipts receipt
                           JOIN billing_migration_absence_reads second_read
                             ON second_read.migration_id = receipt.migration_id
                            AND second_read.builder_id = receipt.builder_id
                            AND second_read.operation_id = receipt.operation_id
                            AND second_read.read_no = 2
                            AND second_read.provider_event_id
                                = receipt.provider_event_id
                            AND second_read.provider_evidence_digest
                                = receipt.provider_evidence_digest
                           JOIN billing_migration_absence_reads first_read
                             ON first_read.migration_id = receipt.migration_id
                            AND first_read.builder_id = receipt.builder_id
                            AND first_read.operation_id = receipt.operation_id
                            AND first_read.read_no = 1
                            AND first_read.provider_event_id
                                <> second_read.provider_event_id
                            AND first_read.provider_observed_at
                                < second_read.provider_observed_at
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
            # Revision 028's protection (single recreate_authorized receipt).
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
            # Revision 030's provider-evidence validation.
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
                WHERE id = NEW.migration_id AND builder_id = NEW.builder_id FOR UPDATE;
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
                   OR NEW.provider_event_id IS NULL
                   OR NEW.provider_observed_at IS NULL
                   OR NEW.provider_expires_at IS NULL
                   OR NEW.provider_observed_at < migration.created_at
                   OR NEW.provider_observed_at > NEW.observed_at
                   OR NEW.provider_expires_at <= NEW.provider_observed_at
                   OR NEW.provider_expires_at <= clock_timestamp()
                   OR NEW.provider_snapshot_digest IS DISTINCT FROM expected_snapshot_digest
                   OR NEW.provider_evidence_digest IS DISTINCT FROM expected_evidence_digest
                   OR (NEW.receipt_kind = 'provider_flip' AND (
                       migration.state IS DISTINCT FROM 'legacy_cancelled'
                       OR NEW.provider_operation IS DISTINCT FROM
                          'subscription.migration.provider_flip'))
                   OR (NEW.receipt_kind = 'recreate_authorized' AND (
                       migration.state NOT IN ('recreate_pending', 'legacy_cancelled')
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
            "DROP TRIGGER IF EXISTS trg_billing_migration_absence_reads_roster_fence "
            "ON billing_migration_absence_reads",
            "DROP TRIGGER IF EXISTS trg_billing_migration_absence_reads_immutable "
            "ON billing_migration_absence_reads",
            "DROP TRIGGER IF EXISTS trg_billing_migration_absence_read_validate "
            "ON billing_migration_absence_reads",
            "DROP FUNCTION IF EXISTS validate_billing_migration_absence_read()",
            "DROP TABLE billing_migration_absence_reads",
            "DROP FUNCTION IF EXISTS billing_provider_evidence_digest("
            "TEXT, TEXT, TIMESTAMPTZ, TEXT, UUID, TIMESTAMPTZ, TEXT, UUID, TEXT, TEXT)",
            "DROP FUNCTION IF EXISTS billing_evidence_timestamp(TIMESTAMPTZ)",
        )
    )
