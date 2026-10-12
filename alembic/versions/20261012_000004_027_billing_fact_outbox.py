"""add canonical billing fact aggregates and delivery evidence

Revision ID: 027
Revises: 026
Create Date: 2026-10-12 00:00:01
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "027"
down_revision: Union[str, None] = "026"
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
            CREATE TABLE billing_subscription_cycles (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL REFERENCES builders(id) ON DELETE RESTRICT,
                subscription_id UUID NOT NULL,
                provider VARCHAR(20) NOT NULL DEFAULT 'asaas',
                payment_id VARCHAR(160) NOT NULL,
                catalog_version_id VARCHAR(80) NOT NULL,
                offer_id VARCHAR(100) NOT NULL,
                cycle_start DATE NOT NULL,
                cycle_end DATE NOT NULL,
                amount_cents BIGINT NOT NULL,
                credit_grant_mc BIGINT NOT NULL,
                pricing_snapshot JSONB NOT NULL,
                financial_snapshot JSONB NOT NULL,
                state VARCHAR(20) NOT NULL DEFAULT 'pending',
                financial_revision BIGINT NOT NULL DEFAULT 0,
                settled_cents BIGINT NOT NULL DEFAULT 0,
                reversed_cents BIGINT NOT NULL DEFAULT 0,
                restored_cents BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_subscription_cycles_provider CHECK (
                    provider = 'asaas'
                ),
                CONSTRAINT ck_billing_subscription_cycles_state CHECK (
                    state IN ('pending', 'settled', 'reversed', 'restored', 'quarantined')
                ),
                CONSTRAINT ck_billing_subscription_cycles_financial_revision CHECK (
                    financial_revision >= 0
                ),
                CONSTRAINT ck_billing_subscription_cycles_positive_amounts CHECK (
                    amount_cents > 0 AND credit_grant_mc >= 0
                ),
                CONSTRAINT ck_billing_subscription_cycles_financial_totals CHECK (
                    settled_cents >= 0 AND reversed_cents >= 0 AND restored_cents >= 0
                    AND restored_cents <= reversed_cents
                    AND reversed_cents <= settled_cents
                    AND settled_cents <= amount_cents
                ),
                CONSTRAINT ck_billing_subscription_cycles_dates CHECK (cycle_start < cycle_end),
                CONSTRAINT fk_billing_subscription_cycles_builder_subscription
                    FOREIGN KEY (builder_id, subscription_id)
                    REFERENCES billing_subscriptions(builder_id, id) ON DELETE RESTRICT,
                CONSTRAINT uq_billing_subscription_cycles_provider_payment
                    UNIQUE (provider, payment_id),
                CONSTRAINT uq_billing_subscription_cycles_builder_id_id
                    UNIQUE (builder_id, id)
            )
            """,
            """
            CREATE INDEX ix_billing_subscription_cycles_builder_id
            ON billing_subscription_cycles (builder_id)
            """,
            """
            ALTER TABLE billing_topup_orders
                ADD COLUMN financial_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
            """,
            """
            ALTER TABLE billing_payment_projections
                ADD COLUMN subscription_cycle_id UUID NULL,
                ADD CONSTRAINT fk_billing_payment_projections_builder_cycle
                    FOREIGN KEY (builder_id, subscription_cycle_id)
                    REFERENCES billing_subscription_cycles(builder_id, id)
                    ON DELETE RESTRICT
            """,
            """
            ALTER TABLE billing_outbox
                ADD COLUMN subscription_cycle_id UUID NULL,
                ADD COLUMN payment_effect_id UUID NULL,
                ADD COLUMN bundle_digest VARCHAR(64) NULL,
                ADD COLUMN deployment_digest VARCHAR(64) NULL
            """,
            """
            ALTER TABLE billing_outbox
                DROP CONSTRAINT ck_billing_outbox_aggregate_target,
                ADD CONSTRAINT ck_billing_outbox_aggregate_target CHECK (
                    (aggregate_kind = 'subscription_cycle'
                        AND subscription_id IS NOT NULL
                        AND topup_order_id IS NULL
                        AND ((subscription_cycle_id IS NOT NULL
                              AND aggregate_id = subscription_cycle_id)
                             OR (subscription_cycle_id IS NULL
                                 AND aggregate_id = subscription_id)))
                    OR (aggregate_kind = 'topup_order'
                        AND topup_order_id IS NOT NULL
                        AND subscription_id IS NULL
                        AND subscription_cycle_id IS NULL
                        AND aggregate_id = topup_order_id)
                ),
                ADD CONSTRAINT fk_billing_outbox_builder_cycle
                    FOREIGN KEY (builder_id, subscription_cycle_id)
                    REFERENCES billing_subscription_cycles(builder_id, id)
                    ON DELETE RESTRICT,
                ADD CONSTRAINT fk_billing_outbox_payment_effect
                    FOREIGN KEY (payment_effect_id)
                    REFERENCES billing_payment_effects(id) ON DELETE RESTRICT,
                ADD CONSTRAINT uq_billing_outbox_payment_effect
                    UNIQUE (payment_effect_id)
            """,
            """
            CREATE TABLE billing_fact_deliveries (
                id UUID PRIMARY KEY,
                outbox_event_id UUID NOT NULL,
                event_id UUID NOT NULL,
                state VARCHAR(20) NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TIMESTAMPTZ NULL,
                acknowledged_at TIMESTAMPTZ NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_fact_deliveries_state CHECK (
                    state IN ('pending', 'awaiting_ack', 'acknowledged', 'blocked')
                ),
                CONSTRAINT ck_billing_fact_deliveries_attempt_count CHECK (attempt_count >= 0),
                CONSTRAINT fk_billing_fact_deliveries_outbox
                    FOREIGN KEY (outbox_event_id) REFERENCES billing_outbox(id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_fact_deliveries_outbox UNIQUE (outbox_event_id),
                CONSTRAINT uq_billing_fact_deliveries_event_id UNIQUE (event_id)
            )
            """,
            """
            CREATE TABLE billing_fact_handoffs (
                id UUID PRIMARY KEY,
                outbox_event_id UUID NOT NULL,
                delivery_id UUID NOT NULL,
                schema_version VARCHAR(40) NOT NULL,
                bundle_digest VARCHAR(64) NOT NULL,
                deployment_digest VARCHAR(64) NOT NULL,
                handed_off_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT ck_billing_fact_handoffs_schema_version CHECK (
                    schema_version = 'billing.fact.v1'
                ),
                CONSTRAINT ck_billing_fact_handoffs_digests CHECK (
                    bundle_digest ~ '^[0-9a-f]{64}$'
                    AND deployment_digest ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT fk_billing_fact_handoffs_outbox
                    FOREIGN KEY (outbox_event_id) REFERENCES billing_outbox(id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_fact_handoffs_delivery
                    FOREIGN KEY (delivery_id) REFERENCES billing_fact_deliveries(id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_fact_handoffs_outbox UNIQUE (outbox_event_id),
                CONSTRAINT uq_billing_fact_handoffs_delivery UNIQUE (delivery_id)
            )
            """,
            """
            CREATE TABLE billing_fact_acks (
                id UUID PRIMARY KEY,
                outbox_event_id UUID NOT NULL,
                event_id UUID NOT NULL,
                schema_version VARCHAR(40) NOT NULL,
                outcome VARCHAR(20) NOT NULL,
                inbox_event_id UUID NOT NULL,
                fact_sha256 VARCHAR(64) NOT NULL,
                bundle_digest VARCHAR(64) NOT NULL,
                deployment_digest VARCHAR(64) NOT NULL,
                effects_digest VARCHAR(64) NOT NULL,
                acknowledged_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_fact_acks_schema_version CHECK (
                    schema_version = 'billing.fact.ack.v1'
                ),
                CONSTRAINT ck_billing_fact_acks_outcome CHECK (
                    outcome IN ('accepted', 'rejected')
                ),
                CONSTRAINT ck_billing_fact_acks_digests CHECK (
                    fact_sha256 ~ '^[0-9a-f]{64}$'
                    AND bundle_digest ~ '^[0-9a-f]{64}$'
                    AND deployment_digest ~ '^[0-9a-f]{64}$'
                    AND effects_digest ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT fk_billing_fact_acks_outbox
                    FOREIGN KEY (outbox_event_id) REFERENCES billing_outbox(id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_fact_acks_outbox UNIQUE (outbox_event_id),
                CONSTRAINT uq_billing_fact_acks_event_id UNIQUE (event_id)
            )
            """,
            """
            CREATE TABLE billing_fact_reconciliation_alerts (
                id UUID PRIMARY KEY,
                outbox_event_id UUID NULL,
                kind VARCHAR(40) NOT NULL,
                detail_sha256 VARCHAR(64) NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_fact_reconciliation_alerts_kind CHECK (
                    kind IN ('backlog', 'collision', 'schema_mismatch',
                             'digest_mismatch', 'gap', 'partial_credit_settlement')
                ),
                CONSTRAINT ck_billing_fact_reconciliation_alerts_digest CHECK (
                    detail_sha256 ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT fk_billing_fact_reconciliation_alerts_outbox
                    FOREIGN KEY (outbox_event_id) REFERENCES billing_outbox(id)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TRIGGER trg_billing_fact_handoffs_immutable
            BEFORE UPDATE OR DELETE ON billing_fact_handoffs
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
            """
            CREATE TRIGGER trg_billing_fact_acks_immutable
            BEFORE UPDATE OR DELETE ON billing_fact_acks
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
            """
            CREATE TRIGGER trg_billing_fact_reconciliation_alerts_immutable
            BEFORE UPDATE OR DELETE ON billing_fact_reconciliation_alerts
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_fact_aggregate_commit()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF NEW.financial_revision > 0 AND NOT EXISTS (
                    SELECT 1 FROM billing_outbox
                    WHERE aggregate_kind = TG_ARGV[0]
                      AND aggregate_id = NEW.id
                      AND financial_revision = NEW.financial_revision
                      AND payment_effect_id IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'financial aggregate revision requires outbox fact'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NULL;
            END;
            $function$
            """,
            """
            CREATE CONSTRAINT TRIGGER trg_billing_topup_fact_atomic
            AFTER INSERT OR UPDATE ON billing_topup_orders
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_billing_fact_aggregate_commit('topup_order')
            """,
            """
            CREATE CONSTRAINT TRIGGER trg_billing_subscription_cycle_fact_atomic
            AFTER INSERT OR UPDATE ON billing_subscription_cycles
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_billing_fact_aggregate_commit('subscription_cycle')
            """,
        )
    )


def downgrade() -> None:
    _execute(
        (
            "DROP TRIGGER trg_billing_subscription_cycle_fact_atomic ON billing_subscription_cycles",
            "DROP TRIGGER trg_billing_topup_fact_atomic ON billing_topup_orders",
            "DROP FUNCTION validate_billing_fact_aggregate_commit()",
            "DROP TRIGGER trg_billing_fact_reconciliation_alerts_immutable ON billing_fact_reconciliation_alerts",
            "DROP TRIGGER trg_billing_fact_acks_immutable ON billing_fact_acks",
            "DROP TRIGGER trg_billing_fact_handoffs_immutable ON billing_fact_handoffs",
            "DROP TABLE billing_fact_reconciliation_alerts",
            "DROP TABLE billing_fact_acks",
            "DROP TABLE billing_fact_handoffs",
            "DROP TABLE billing_fact_deliveries",
            """
            ALTER TABLE billing_outbox
                DROP CONSTRAINT uq_billing_outbox_payment_effect,
                DROP CONSTRAINT fk_billing_outbox_payment_effect,
                DROP CONSTRAINT fk_billing_outbox_builder_cycle,
                DROP CONSTRAINT ck_billing_outbox_aggregate_target,
                ADD CONSTRAINT ck_billing_outbox_aggregate_target CHECK (
                    (aggregate_kind = 'subscription_cycle' AND subscription_id IS NOT NULL
                        AND topup_order_id IS NULL AND aggregate_id = subscription_id)
                    OR (aggregate_kind = 'topup_order' AND topup_order_id IS NOT NULL
                        AND subscription_id IS NULL AND aggregate_id = topup_order_id)
                ),
                DROP COLUMN deployment_digest,
                DROP COLUMN bundle_digest,
                DROP COLUMN payment_effect_id,
                DROP COLUMN subscription_cycle_id
            """,
            """
            ALTER TABLE billing_payment_projections
                DROP CONSTRAINT fk_billing_payment_projections_builder_cycle,
                DROP COLUMN subscription_cycle_id
            """,
            "ALTER TABLE billing_topup_orders DROP COLUMN financial_snapshot",
            "DROP INDEX ix_billing_subscription_cycles_builder_id",
            "DROP TABLE billing_subscription_cycles",
        )
    )
