"""add ASAAS billing persistence and financial invariants

Revision ID: 026
Revises: 025
Create Date: 2026-10-10 00:00:01
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "026"
down_revision: Union[str, None] = "025"
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
            CREATE OR REPLACE FUNCTION billing_snapshot_has_no_pii(value JSONB)
            RETURNS BOOLEAN
            LANGUAGE sql
            IMMUTABLE
            STRICT
            AS $function$
                WITH RECURSIVE nodes(value) AS (
                    SELECT value
                    UNION ALL
                    SELECT child.value
                    FROM nodes
                    CROSS JOIN LATERAL (
                        SELECT object_value AS value
                        FROM jsonb_each(
                            CASE jsonb_typeof(nodes.value)
                                WHEN 'object' THEN nodes.value
                                ELSE '{}'::jsonb
                            END
                        ) AS object_child(object_key, object_value)
                        UNION ALL
                        SELECT array_value
                        FROM jsonb_array_elements(
                            CASE jsonb_typeof(nodes.value)
                                WHEN 'array' THEN nodes.value
                                ELSE '[]'::jsonb
                            END
                        ) AS array_child(array_value)
                    ) AS child
                )
                SELECT NOT EXISTS (
                    SELECT 1
                    FROM nodes
                    CROSS JOIN LATERAL jsonb_object_keys(
                        CASE jsonb_typeof(nodes.value)
                            WHEN 'object' THEN nodes.value
                            ELSE '{}'::jsonb
                        END
                    ) AS object_key
                    WHERE lower(object_key) IN (
                        'customer', 'email', 'document', 'document_number',
                        'cpf', 'cnpj', 'phone', 'address', 'name'
                    )
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_each(value) AS root_field(key, item)
                    WHERE key <> 'lines'
                      AND jsonb_typeof(item) IN ('object', 'array')
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_each(value) AS root_field(key, item)
                    WHERE key IN (
                            'catalog_version_id', 'offer_id', 'plan',
                            'billing_cycle', 'payment_method')
                      AND (jsonb_typeof(item) <> 'string'
                           OR item #>> '{}' = '')
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_each(value) AS root_field(key, item)
                    WHERE key = 'coupon_code'
                      AND jsonb_typeof(item) NOT IN ('string', 'null')
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_each(value) AS root_field(key, item)
                    WHERE key IN (
                            'amount_cents', 'credit_grant_mc',
                            'included_clinics', 'additional_clinics')
                      AND (jsonb_typeof(item) <> 'number'
                           OR item::text !~ '^[0-9]+$')
                )
                AND (
                    NOT value ? 'lines'
                    OR (
                        jsonb_typeof(value -> 'lines') = 'array'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(value -> 'lines') AS entry(line)
                            WHERE jsonb_typeof(line) <> 'object'
                               OR line - ARRAY[
                                    'line_id', 'offer_id', 'kind', 'quantity',
                                    'unit_amount_cents', 'line_amount_cents',
                                    'credit_grant_mc', 'settled_cents',
                                    'reversed_cents', 'restored_cents',
                                    'line_financial_state'
                                  ] <> '{}'::jsonb
                               OR EXISTS (
                                    SELECT 1 FROM jsonb_each(line) AS field(key, item)
                                    WHERE jsonb_typeof(item) IN ('object', 'array')
                               )
                               OR (line ? 'line_id' AND (
                                    jsonb_typeof(line -> 'line_id') <> 'string'
                                    OR line ->> 'line_id' !~ '^[A-Za-z0-9._:-]+$'))
                               OR (line ? 'offer_id' AND (
                                    jsonb_typeof(line -> 'offer_id') <> 'string'
                                    OR line ->> 'offer_id' !~ '^[A-Za-z0-9._:-]+$'))
                               OR (line ? 'kind' AND (
                                    jsonb_typeof(line -> 'kind') <> 'string'
                                    OR line ->> 'kind' NOT IN (
                                        'plan', 'additional_clinic', 'topup', 'tax')))
                               OR (line ? 'line_financial_state' AND (
                                    jsonb_typeof(line -> 'line_financial_state')
                                        <> 'string'
                                    OR line ->> 'line_financial_state' NOT IN (
                                        'pending', 'settled', 'partially_reversed',
                                        'reversed', 'restored')))
                               OR EXISTS (
                                    SELECT 1
                                    FROM jsonb_each(line) AS number_field(key, item)
                                    WHERE key IN (
                                        'quantity', 'unit_amount_cents',
                                        'line_amount_cents', 'credit_grant_mc',
                                        'settled_cents', 'reversed_cents',
                                        'restored_cents')
                                      AND (jsonb_typeof(item) <> 'number'
                                           OR item::text !~ '^[0-9]+$')
                               )
                               OR (line ? 'quantity'
                                   AND (line ->> 'quantity')::numeric <= 0)
                               OR (
                                    line ?& ARRAY[
                                        'quantity', 'unit_amount_cents',
                                        'line_amount_cents']
                                    AND (line ->> 'line_amount_cents')::numeric
                                        <> (line ->> 'quantity')::numeric
                                           * (line ->> 'unit_amount_cents')::numeric
                               )
                               OR (
                                    line ?& ARRAY[
                                        'line_amount_cents', 'settled_cents',
                                        'reversed_cents', 'restored_cents']
                                    AND NOT (
                                        (line ->> 'restored_cents')::numeric
                                            <= (line ->> 'reversed_cents')::numeric
                                        AND (line ->> 'reversed_cents')::numeric
                                            <= (line ->> 'settled_cents')::numeric
                                        AND (line ->> 'settled_cents')::numeric
                                            <= (line ->> 'line_amount_cents')::numeric)
                               )
                               OR (
                                    line ?& ARRAY['kind', 'credit_grant_mc']
                                    AND line ->> 'kind' IN ('additional_clinic', 'tax')
                                    AND (line ->> 'credit_grant_mc')::numeric <> 0
                               )
                        )
                    )
                )
            $function$
            """,
            """
            CREATE OR REPLACE FUNCTION billing_migration_snapshot_is_valid(
                value JSONB, catalog_id TEXT, offer_id TEXT, plan_name TEXT,
                cycle_name TEXT, coupon TEXT, amount BIGINT
            ) RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $function$
                SELECT jsonb_typeof(value) = 'object'
                  AND value ?& ARRAY['catalog_version_id','offer_id','plan',
                    'billing_cycle','coupon_code','amount_cents','credit_grant_mc',
                    'included_clinics','additional_clinics','lines']
                  AND value - ARRAY['catalog_version_id','offer_id','plan',
                    'billing_cycle','coupon_code','amount_cents','credit_grant_mc',
                    'included_clinics','additional_clinics','lines'] = '{}'::jsonb
                  AND billing_snapshot_has_no_pii(value)
                  AND value ->> 'catalog_version_id' = catalog_id
                  AND value ->> 'offer_id' = offer_id
                  AND value ->> 'plan' = plan_name
                  AND value ->> 'billing_cycle' = cycle_name
                  AND value ->> 'coupon_code' IS NOT DISTINCT FROM coupon
                  AND jsonb_typeof(value -> 'coupon_code') IN ('string','null')
                  AND (value ->> 'amount_cents')::bigint = amount
                  AND (value ->> 'credit_grant_mc')::bigint >= 0
                  AND (value ->> 'included_clinics')::bigint >= 0
                  AND (value ->> 'additional_clinics')::bigint >= 0
                  AND jsonb_typeof(value -> 'lines') = 'array'
                  AND jsonb_array_length(value -> 'lines') > 0
                  AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(value -> 'lines') e(line)
                    WHERE jsonb_typeof(line) <> 'object'
                       OR NOT line ?& ARRAY['line_id','offer_id','kind','quantity',
                         'unit_amount_cents','line_amount_cents','credit_grant_mc',
                         'settled_cents','reversed_cents','restored_cents',
                         'line_financial_state']
                       OR line - ARRAY['line_id','offer_id','kind','quantity',
                         'unit_amount_cents','line_amount_cents','credit_grant_mc',
                         'settled_cents','reversed_cents','restored_cents',
                         'line_financial_state'] <> '{}'::jsonb
                  )
                  AND (SELECT sum((line ->> 'line_amount_cents')::bigint)
                       FROM jsonb_array_elements(value -> 'lines') e(line)) = amount
                  AND (SELECT sum((line ->> 'credit_grant_mc')::bigint)
                       FROM jsonb_array_elements(value -> 'lines') e(line)) =
                      (value ->> 'credit_grant_mc')::bigint
            $function$
            """,
            """
            CREATE TABLE billing_topup_capability_receipts (
                id UUID PRIMARY KEY,
                environment VARCHAR(20) NOT NULL,
                schema_version VARCHAR(40) NOT NULL,
                request_id UUID NOT NULL,
                credit_deployment_digest VARCHAR(100) NOT NULL,
                billing_fact_schema_digest VARCHAR(64) NOT NULL,
                producer_compatibility_digest VARCHAR(64) NOT NULL,
                consumer_compatibility_digest VARCHAR(64) NOT NULL,
                inbox_high_watermark BIGINT NOT NULL,
                reconciliation_receipt_id UUID NOT NULL,
                approved_by UUID NOT NULL,
                approved_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                reconciliation_complete BOOLEAN NOT NULL,
                readback_complete BOOLEAN NOT NULL,
                has_open_gap BOOLEAN NOT NULL,
                has_backlog BOOLEAN NOT NULL,
                has_alert BOOLEAN NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_topup_capability_environment CHECK (
                    environment IN ('development', 'staging', 'production', 'test')
                ),
                CONSTRAINT ck_billing_topup_capability_schema CHECK (
                    schema_version = 'credit_capability_ready.v1'
                ),
                CONSTRAINT ck_billing_topup_capability_watermark CHECK (
                    inbox_high_watermark >= 0
                ),
                CONSTRAINT ck_billing_topup_capability_digests CHECK (
                    billing_fact_schema_digest ~ '^[0-9a-f]{64}$'
                    AND producer_compatibility_digest ~ '^[0-9a-f]{64}$'
                    AND consumer_compatibility_digest ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT ck_billing_topup_capability_expiry CHECK (
                    expires_at > approved_at
                ),
                CONSTRAINT uq_billing_topup_capability_environment_request
                    UNIQUE (environment, request_id),
                CONSTRAINT uq_billing_topup_capability_environment_id
                    UNIQUE (environment, id)
            )
            """,
            """
            CREATE TABLE billing_rollout_control (
                environment VARCHAR(20) PRIMARY KEY,
                state VARCHAR(30) NOT NULL DEFAULT 'disabled',
                canary_builder_id UUID NULL REFERENCES builders(id) ON DELETE RESTRICT,
                rollout_revision BIGINT NOT NULL DEFAULT 0,
                rollout_receipt_id UUID NULL,
                topup_commercial_enabled BOOLEAN NOT NULL DEFAULT false,
                topup_revision BIGINT NOT NULL DEFAULT 0,
                topup_receipt_id UUID NULL,
                active_topup_capability_id UUID NULL,
                canary_checkout_limit INTEGER NOT NULL DEFAULT 2,
                canary_settlement_limit INTEGER NOT NULL DEFAULT 2,
                canary_amount_limit_cents BIGINT NOT NULL DEFAULT 109900,
                canary_checkout_count INTEGER NOT NULL DEFAULT 0,
                canary_settlement_count INTEGER NOT NULL DEFAULT 0,
                canary_amount_reserved_cents BIGINT NOT NULL DEFAULT 0,
                canary_subscription_checkout_id UUID NULL,
                canary_topup_checkout_id UUID NULL,
                canary_subscription_effect_id UUID NULL,
                canary_topup_effect_id UUID NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_rollout_control_environment CHECK (
                    environment IN ('development', 'staging', 'production', 'test')
                ),
                CONSTRAINT ck_billing_rollout_control_state CHECK (
                    state IN ('disabled', 'canary_allowlist', 'general')
                ),
                CONSTRAINT ck_billing_rollout_control_revisions CHECK (
                    rollout_revision >= 0 AND topup_revision >= 0
                ),
                CONSTRAINT ck_billing_rollout_control_receipt_pointers CHECK (
                    ((rollout_revision = 0 AND rollout_receipt_id IS NULL)
                        OR (rollout_revision > 0 AND rollout_receipt_id IS NOT NULL))
                    AND ((topup_revision = 0 AND topup_receipt_id IS NULL)
                        OR (topup_revision > 0 AND topup_receipt_id IS NOT NULL))
                ),
                CONSTRAINT ck_billing_rollout_control_canary_builder CHECK (
                    (state = 'canary_allowlist' AND canary_builder_id IS NOT NULL)
                    OR (state <> 'canary_allowlist' AND canary_builder_id IS NULL)
                ),
                CONSTRAINT ck_billing_rollout_control_canary_budget CHECK (
                    canary_checkout_limit = 2
                    AND canary_settlement_limit = 2
                    AND canary_amount_limit_cents = 109900
                ),
                CONSTRAINT ck_billing_rollout_control_canary_usage CHECK (
                    canary_checkout_count =
                        CAST(canary_subscription_checkout_id IS NOT NULL AS INTEGER)
                        + CAST(canary_topup_checkout_id IS NOT NULL AS INTEGER)
                    AND canary_settlement_count =
                        CAST(canary_subscription_effect_id IS NOT NULL AS INTEGER)
                        + CAST(canary_topup_effect_id IS NOT NULL AS INTEGER)
                    AND canary_amount_reserved_cents =
                        CASE WHEN canary_subscription_checkout_id IS NULL
                            THEN 0 ELSE 99900 END
                        + CASE WHEN canary_topup_checkout_id IS NULL
                            THEN 0 ELSE 10000 END
                    AND (canary_subscription_effect_id IS NULL
                        OR canary_subscription_checkout_id IS NOT NULL)
                    AND (canary_topup_effect_id IS NULL
                        OR canary_topup_checkout_id IS NOT NULL)
                ),
                CONSTRAINT ck_billing_rollout_control_topup_capability CHECK (
                    (topup_commercial_enabled = false
                        AND active_topup_capability_id IS NULL)
                    OR (topup_commercial_enabled = true
                        AND active_topup_capability_id IS NOT NULL)
                ),
                CONSTRAINT fk_billing_rollout_control_topup_capability
                    FOREIGN KEY (environment, active_topup_capability_id)
                    REFERENCES billing_topup_capability_receipts(environment, id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_rollout_control_rollout_receipt
                    UNIQUE (rollout_receipt_id),
                CONSTRAINT uq_billing_rollout_control_topup_receipt
                    UNIQUE (topup_receipt_id)
            )
            """,
            """
            CREATE TABLE billing_roster_control (
                singleton BOOLEAN PRIMARY KEY DEFAULT true,
                billing_roster_revision BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_roster_control_singleton CHECK (
                    singleton = true
                ),
                CONSTRAINT ck_billing_roster_control_revision CHECK (
                    billing_roster_revision >= 0
                )
            )
            """,
            """
            CREATE TABLE billing_rollout_receipts (
                id UUID PRIMARY KEY,
                environment VARCHAR(20) NOT NULL,
                receipt_kind VARCHAR(40) NOT NULL,
                nonce UUID NOT NULL,
                deployment_digest VARCHAR(64) NOT NULL,
                from_state VARCHAR(30) NOT NULL,
                to_state VARCHAR(30) NOT NULL,
                expected_revision BIGINT NOT NULL,
                resulting_revision BIGINT NOT NULL,
                capability_receipt_id UUID NULL,
                actor_user_id UUID NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_rollout_receipts_environment CHECK (
                    environment IN ('development', 'staging', 'production', 'test')
                ),
                CONSTRAINT ck_billing_rollout_receipts_kind CHECK (
                    receipt_kind IN (
                        'rollout_transition', 'topup_gate_transition'
                    )
                ),
                CONSTRAINT ck_billing_rollout_receipts_states CHECK (
                    (receipt_kind = 'rollout_transition'
                        AND from_state IN (
                            'disabled', 'canary_allowlist', 'general'
                        )
                        AND to_state IN (
                            'disabled', 'canary_allowlist', 'general'
                        )
                        AND capability_receipt_id IS NULL)
                    OR (receipt_kind = 'topup_gate_transition'
                        AND from_state IN ('topup_disabled', 'topup_enabled')
                        AND to_state IN ('topup_disabled', 'topup_enabled')
                        AND from_state <> to_state
                        AND (
                            (to_state = 'topup_enabled'
                                AND capability_receipt_id IS NOT NULL)
                            OR (to_state = 'topup_disabled'
                                AND capability_receipt_id IS NULL)
                        ))
                ),
                CONSTRAINT ck_billing_rollout_receipts_revision CHECK (
                    resulting_revision = expected_revision + 1
                ),
                CONSTRAINT ck_billing_rollout_receipts_deployment_digest CHECK (
                    deployment_digest ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT uq_billing_rollout_receipts_nonce UNIQUE (nonce),
                CONSTRAINT uq_billing_rollout_receipts_revision UNIQUE (
                    environment, receipt_kind, resulting_revision
                ),
                CONSTRAINT fk_billing_rollout_receipts_capability
                    FOREIGN KEY (environment, capability_receipt_id)
                    REFERENCES billing_topup_capability_receipts(environment, id)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE UNIQUE INDEX uq_billing_rollout_receipts_capability_consumption
            ON billing_rollout_receipts(environment, capability_receipt_id)
            WHERE capability_receipt_id IS NOT NULL
            """,
            """
            ALTER TABLE billing_rollout_control
                ADD CONSTRAINT fk_billing_rollout_control_rollout_receipt
                    FOREIGN KEY (rollout_receipt_id)
                    REFERENCES billing_rollout_receipts(id) ON DELETE RESTRICT,
                ADD CONSTRAINT fk_billing_rollout_control_topup_receipt
                    FOREIGN KEY (topup_receipt_id)
                    REFERENCES billing_rollout_receipts(id) ON DELETE RESTRICT
            """,
            """
            CREATE TABLE billing_subscriptions (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL REFERENCES builders(id) ON DELETE RESTRICT,
                provider VARCHAR(20) NULL,
                external_subscription_id VARCHAR(160) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT true,
                plan VARCHAR(100) NOT NULL,
                billing_cycle VARCHAR(20) NOT NULL DEFAULT 'monthly',
                coupon_code VARCHAR(50) NULL,
                catalog_version_id VARCHAR(80) NULL,
                offer_id VARCHAR(100) NULL,
                amount_cents BIGINT NULL,
                status VARCHAR(40) NOT NULL DEFAULT 'active',
                roster_revision BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_subscriptions_provider CHECK (
                    provider IS NULL OR provider IN ('abacatepay', 'asaas')
                ),
                CONSTRAINT ck_billing_subscriptions_external_id_length CHECK (
                    char_length(external_subscription_id) BETWEEN 1 AND 160
                    AND external_subscription_id ~ '^[A-Za-z0-9._:-]+$'
                ),
                CONSTRAINT ck_billing_subscriptions_plan_length CHECK (
                    length(plan) BETWEEN 1 AND 100
                ),
                CONSTRAINT ck_billing_subscriptions_cycle CHECK (
                    (provider = 'asaas' AND billing_cycle = 'monthly')
                    OR (provider = 'abacatepay'
                        AND billing_cycle IN ('monthly', 'annual'))
                    OR (provider IS NULL
                        AND billing_cycle IN ('monthly', 'annual'))
                ),
                CONSTRAINT ck_billing_subscriptions_status CHECK (
                    status IN (
                        'active', 'inactive', 'cancelled',
                        'active_linked', 'readback_required',
                        'non_billable_pending_readback',
                        'legacy_cancel_pending', 'legacy_cancelled',
                        'provider_unknown', 'manual_intervention'
                    )
                ),
                CONSTRAINT ck_billing_subscriptions_active_status CHECK (
                    (status IN ('active', 'active_linked', 'readback_required')
                        AND is_active = true)
                    OR (status NOT IN (
                            'active', 'active_linked', 'readback_required'
                        ) AND is_active = false)
                ),
                CONSTRAINT ck_billing_subscriptions_unknown_provider CHECK (
                    (provider IS NULL AND is_active = false
                        AND status IN (
                            'legacy_cancel_pending', 'legacy_cancelled',
                            'provider_unknown', 'manual_intervention'
                        ))
                    OR (provider IS NOT NULL AND status NOT IN (
                        'legacy_cancel_pending', 'legacy_cancelled',
                        'provider_unknown', 'manual_intervention'
                    ))
                ),
                CONSTRAINT ck_billing_subscriptions_amount_cents CHECK (
                    amount_cents IS NULL OR amount_cents >= 0
                ),
                CONSTRAINT ck_billing_subscriptions_roster_revision CHECK (
                    roster_revision > 0
                ),
                CONSTRAINT uq_billing_subscriptions_provider_external UNIQUE (
                    provider, external_subscription_id
                ),
                CONSTRAINT uq_billing_subscriptions_builder_id_id UNIQUE (
                    builder_id, id
                ),
                CONSTRAINT uq_billing_subscriptions_builder_provider_external UNIQUE (
                    builder_id, provider, external_subscription_id
                )
            )
            """,
            """
            CREATE TABLE billing_migrations (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL REFERENCES builders(id) ON DELETE RESTRICT,
                subscription_id UUID NOT NULL,
                state VARCHAR(40) NOT NULL DEFAULT 'pending',
                renewal_date DATE NOT NULL,
                open_by_date DATE NOT NULL,
                cutover_by_date DATE NOT NULL,
                target_provider VARCHAR(20) NOT NULL DEFAULT 'asaas',
                target_external_subscription_id VARCHAR(160) NULL,
                target_catalog_version_id VARCHAR(80) NOT NULL,
                target_offer_id VARCHAR(100) NOT NULL,
                target_plan VARCHAR(100) NOT NULL,
                target_billing_cycle VARCHAR(20) NOT NULL DEFAULT 'monthly',
                target_coupon_code VARCHAR(50) NULL,
                target_amount_cents BIGINT NOT NULL,
                target_snapshot JSONB NOT NULL,
                operation_id UUID NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                source_roster_revision BIGINT NOT NULL,
                legacy_cancelled_at TIMESTAMPTZ NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_migrations_state CHECK (
                    state IN (
                        'pending', 'in_progress', 'legacy_cancel_pending',
                        'legacy_cancelled', 'recreate_pending', 'migrated',
                        'manual_intervention'
                    )
                ),
                CONSTRAINT ck_billing_migrations_target_provider CHECK (
                    target_provider = 'asaas'
                ),
                CONSTRAINT ck_billing_migrations_target_cycle CHECK (
                    target_billing_cycle = 'monthly'
                ),
                CONSTRAINT ck_billing_migrations_target_amount CHECK (
                    target_amount_cents >= 0
                ),
                CONSTRAINT ck_billing_migrations_attempt_count CHECK (
                    attempt_count >= 0
                ),
                CONSTRAINT ck_billing_migrations_cutoffs CHECK (
                    open_by_date <= cutover_by_date
                    AND cutover_by_date <= renewal_date
                ),
                CONSTRAINT ck_billing_migrations_roster_revision CHECK (
                    source_roster_revision > 0
                ),
                CONSTRAINT ck_billing_migrations_snapshot_allowlist CHECK (
                    billing_migration_snapshot_is_valid(
                        target_snapshot, target_catalog_version_id,
                        target_offer_id, target_plan, target_billing_cycle,
                        target_coupon_code, target_amount_cents)
                ),
                CONSTRAINT fk_billing_migrations_builder_subscription
                    FOREIGN KEY (builder_id, subscription_id)
                    REFERENCES billing_subscriptions(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_migrations_builder_subscription UNIQUE (
                    builder_id, subscription_id
                ),
                CONSTRAINT uq_billing_migrations_target_external UNIQUE (
                    target_provider, target_external_subscription_id
                ),
                CONSTRAINT uq_billing_migrations_builder_id_id UNIQUE (
                    builder_id, id
                )
            )
            """,
            """
            CREATE TABLE billing_topup_orders (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL REFERENCES builders(id) ON DELETE RESTRICT,
                provider VARCHAR(20) NOT NULL DEFAULT 'asaas',
                external_order_id VARCHAR(160) NULL,
                catalog_version_id VARCHAR(80) NOT NULL,
                offer_id VARCHAR(100) NOT NULL,
                payment_method VARCHAR(20) NOT NULL,
                amount_cents BIGINT NOT NULL,
                credit_grant_mc BIGINT NOT NULL,
                pricing_snapshot JSONB NOT NULL,
                state VARCHAR(40) NOT NULL DEFAULT 'pending',
                financial_revision BIGINT NOT NULL DEFAULT 0,
                settled_cents BIGINT NOT NULL DEFAULT 0,
                reversed_cents BIGINT NOT NULL DEFAULT 0,
                restored_cents BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_topup_orders_provider CHECK (
                    provider = 'asaas'
                ),
                CONSTRAINT ck_billing_topup_orders_payment_method CHECK (
                    payment_method IN ('credit_card', 'pix')
                ),
                CONSTRAINT ck_billing_topup_orders_state CHECK (
                    state IN (
                        'pending', 'settled', 'partially_reversed', 'reversed',
                        'restored', 'failed', 'cancelled', 'quarantined'
                    )
                ),
                CONSTRAINT ck_billing_topup_orders_positive_amounts CHECK (
                    amount_cents > 0 AND credit_grant_mc > 0
                ),
                CONSTRAINT ck_billing_topup_orders_financial_revision CHECK (
                    financial_revision >= 0
                ),
                CONSTRAINT ck_billing_topup_orders_financial_totals CHECK (
                    settled_cents >= 0 AND reversed_cents >= 0
                    AND restored_cents >= 0
                    AND restored_cents <= reversed_cents
                    AND reversed_cents <= settled_cents
                    AND settled_cents <= amount_cents
                ),
                CONSTRAINT ck_billing_topup_orders_snapshot_allowlist CHECK (
                    jsonb_typeof(pricing_snapshot) = 'object'
                    AND pricing_snapshot - ARRAY[
                        'catalog_version_id', 'offer_id', 'amount_cents',
                        'credit_grant_mc', 'payment_method', 'lines'
                    ] = '{}'::jsonb
                    AND billing_snapshot_has_no_pii(pricing_snapshot)
                    AND (NOT pricing_snapshot ? 'catalog_version_id'
                        OR pricing_snapshot ->> 'catalog_version_id' =
                            catalog_version_id)
                    AND (NOT pricing_snapshot ? 'offer_id'
                        OR pricing_snapshot ->> 'offer_id' = offer_id)
                    AND (NOT pricing_snapshot ? 'amount_cents'
                        OR (pricing_snapshot ->> 'amount_cents')::bigint =
                            amount_cents)
                    AND (NOT pricing_snapshot ? 'credit_grant_mc'
                        OR (pricing_snapshot ->> 'credit_grant_mc')::bigint =
                            credit_grant_mc)
                    AND (NOT pricing_snapshot ? 'payment_method'
                        OR pricing_snapshot ->> 'payment_method' = payment_method)
                ),
                CONSTRAINT uq_billing_topup_orders_provider_external UNIQUE (
                    provider, external_order_id
                ),
                CONSTRAINT uq_billing_topup_orders_builder_id_id UNIQUE (
                    builder_id, id
                )
            )
            """,
            """
            CREATE TABLE billing_checkout_sessions (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL REFERENCES builders(id) ON DELETE RESTRICT,
                intent_kind VARCHAR(20) NOT NULL,
                intent_id UUID NOT NULL,
                migration_id UUID NULL,
                topup_order_id UUID NULL,
                attempt_no INTEGER NOT NULL,
                provider VARCHAR(20) NOT NULL DEFAULT 'asaas',
                external_checkout_id VARCHAR(160) NULL,
                resulting_external_subscription_id VARCHAR(160) NULL,
                state VARCHAR(40) NOT NULL DEFAULT 'creating',
                catalog_version_id VARCHAR(80) NOT NULL,
                offer_id VARCHAR(100) NOT NULL,
                amount_cents BIGINT NOT NULL,
                pricing_snapshot JSONB NOT NULL,
                canary_environment VARCHAR(20) NULL,
                canary_slot VARCHAR(20) NULL,
                next_due_date DATE NULL,
                reconciliation_attempts INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_checkout_sessions_intent_kind CHECK (
                    intent_kind IN ('subscription', 'topup')
                ),
                CONSTRAINT ck_billing_checkout_sessions_provider CHECK (
                    provider = 'asaas'
                ),
                CONSTRAINT ck_billing_checkout_sessions_external_ids CHECK (
                    (external_checkout_id IS NULL
                        OR external_checkout_id ~ '^[A-Za-z0-9._:-]+$')
                    AND (resulting_external_subscription_id IS NULL
                        OR (intent_kind = 'subscription'
                            AND resulting_external_subscription_id ~
                                '^[A-Za-z0-9._:-]+$'))
                ),
                CONSTRAINT ck_billing_checkout_sessions_state CHECK (
                    state IN (
                        'creating', 'active', 'paid_pending_subscription',
                        'reconciliation_required', 'terminal_pending_readback',
                        'completed', 'abandoned', 'cancelled', 'expired', 'failed'
                    )
                ),
                CONSTRAINT ck_billing_checkout_sessions_attempt_amount CHECK (
                    attempt_no > 0 AND amount_cents > 0
                    AND reconciliation_attempts >= 0
                ),
                CONSTRAINT ck_billing_checkout_sessions_intent_target CHECK (
                    (intent_kind = 'subscription' AND topup_order_id IS NULL
                        AND (migration_id IS NULL OR intent_id = migration_id))
                    OR (intent_kind = 'topup' AND topup_order_id IS NOT NULL
                        AND migration_id IS NULL AND intent_id = topup_order_id)
                ),
                CONSTRAINT ck_billing_checkout_sessions_snapshot_allowlist CHECK (
                    jsonb_typeof(pricing_snapshot) = 'object'
                    AND pricing_snapshot - ARRAY[
                        'catalog_version_id', 'offer_id', 'amount_cents',
                        'credit_grant_mc', 'included_clinics',
                        'additional_clinics', 'billing_cycle', 'coupon_code',
                        'payment_method', 'lines'
                    ] = '{}'::jsonb
                    AND billing_snapshot_has_no_pii(pricing_snapshot)
                    AND (NOT pricing_snapshot ? 'catalog_version_id'
                        OR pricing_snapshot ->> 'catalog_version_id' =
                            catalog_version_id)
                    AND (NOT pricing_snapshot ? 'offer_id'
                        OR pricing_snapshot ->> 'offer_id' = offer_id)
                    AND (NOT pricing_snapshot ? 'amount_cents'
                        OR (pricing_snapshot ->> 'amount_cents')::bigint =
                            amount_cents)
                ),
                CONSTRAINT ck_billing_checkout_sessions_canary_pair CHECK (
                    (canary_environment IS NULL AND canary_slot IS NULL)
                    OR (canary_environment IS NOT NULL
                        AND canary_environment IN (
                            'development', 'staging', 'production', 'test')
                        AND canary_slot IN ('subscription', 'topup'))
                ),
                CONSTRAINT ck_billing_checkout_sessions_canary_exact CHECK (
                    canary_slot IS NULL OR (
                        provider = 'asaas' AND attempt_no = 1
                        AND catalog_version_id = 'payment-2026-10-03'
                        AND (
                            (canary_slot = 'subscription'
                                AND intent_kind = 'subscription'
                                AND migration_id IS NULL
                                AND topup_order_id IS NULL
                                AND next_due_date IS NOT NULL
                                AND offer_id = 'plan_plus_monthly_v1'
                                AND amount_cents = 99900
                                AND pricing_snapshot ->> 'catalog_version_id' =
                                    'payment-2026-10-03'
                                AND pricing_snapshot ->> 'offer_id' =
                                    'plan_plus_monthly_v1'
                                AND pricing_snapshot ->> 'amount_cents' = '99900'
                                AND pricing_snapshot ->> 'payment_method' =
                                    'credit_card'
                                AND pricing_snapshot ->> 'billing_cycle' =
                                    'monthly'
                                AND pricing_snapshot = jsonb_build_object(
                                    'catalog_version_id', 'payment-2026-10-03',
                                    'offer_id', 'plan_plus_monthly_v1',
                                    'amount_cents', 99900,
                                    'credit_grant_mc', 5000,
                                    'included_clinics', 1,
                                    'billing_cycle', 'monthly',
                                    'payment_method', 'credit_card',
                                    'lines', jsonb_build_array(
                                        jsonb_build_object(
                                            'line_id', 'plan',
                                            'offer_id', 'plan_plus_monthly_v1',
                                            'kind', 'plan',
                                            'quantity', 1,
                                            'unit_amount_cents', 99900,
                                            'line_amount_cents', 99900,
                                            'credit_grant_mc', 5000,
                                            'settled_cents', 0,
                                            'reversed_cents', 0,
                                            'restored_cents', 0,
                                            'line_financial_state', 'pending'
                                        )
                                    )
                                ))
                            OR (canary_slot = 'topup'
                                AND intent_kind = 'topup'
                                AND migration_id IS NULL
                                AND topup_order_id IS NOT NULL
                                AND intent_id = topup_order_id
                                AND offer_id = 'package_credits_500_v1'
                                AND amount_cents = 10000
                                AND pricing_snapshot ->> 'catalog_version_id' =
                                    'payment-2026-10-03'
                                AND pricing_snapshot ->> 'offer_id' =
                                    'package_credits_500_v1'
                                AND pricing_snapshot ->> 'amount_cents' = '10000'
                                AND pricing_snapshot ->> 'credit_grant_mc' = '500')
                        )
                    )
                ),
                CONSTRAINT fk_billing_checkout_sessions_builder_migration
                    FOREIGN KEY (builder_id, migration_id)
                    REFERENCES billing_migrations(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_checkout_sessions_builder_topup
                    FOREIGN KEY (builder_id, topup_order_id)
                    REFERENCES billing_topup_orders(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_checkout_sessions_result_subscription
                    FOREIGN KEY (
                        builder_id, provider, resulting_external_subscription_id
                    )
                    REFERENCES billing_subscriptions(
                        builder_id, provider, external_subscription_id
                    ) ON DELETE RESTRICT,
                CONSTRAINT uq_billing_checkout_sessions_provider_external UNIQUE (
                    provider, external_checkout_id
                ),
                CONSTRAINT uq_billing_checkout_sessions_attempt UNIQUE (
                    builder_id, intent_kind, intent_id, attempt_no
                ),
                CONSTRAINT uq_billing_checkout_sessions_builder_id_id UNIQUE (
                    builder_id, id
                ),
                CONSTRAINT uq_billing_checkout_sessions_canary_identity UNIQUE (
                    builder_id, id, canary_environment, canary_slot
                )
            )
            """,
            """
            CREATE UNIQUE INDEX uq_billing_checkout_sessions_live_intent
            ON billing_checkout_sessions(builder_id, intent_kind, intent_id)
            WHERE state IN (
                'creating', 'active', 'paid_pending_subscription',
                'reconciliation_required', 'terminal_pending_readback'
            )
            """,
            """
            CREATE UNIQUE INDEX uq_billing_checkout_sessions_canary_slot
            ON billing_checkout_sessions(canary_environment, canary_slot)
            WHERE canary_environment IS NOT NULL
            """,
            """
            CREATE TABLE billing_migration_receipts (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL,
                migration_id UUID NOT NULL,
                operation_id UUID NOT NULL,
                receipt_kind VARCHAR(40) NOT NULL,
                provider VARCHAR(20) NULL,
                external_reference VARCHAR(160) NULL,
                status VARCHAR(30) NOT NULL,
                content_sha256 VARCHAR(64) NOT NULL,
                actor_user_id UUID NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_migration_receipts_kind CHECK (
                    receipt_kind IN (
                        'roster_import', 'cancel_observation', 'provider_delete',
                        'recreate_authorized', 'provider_flip', 'roster_cutover',
                        'roster_disposal'
                    )
                ),
                CONSTRAINT ck_billing_migration_receipts_provider CHECK (
                    provider IS NULL OR provider IN ('abacatepay', 'asaas')
                ),
                CONSTRAINT ck_billing_migration_receipts_status CHECK (
                    status IN (
                        'observed', 'active', 'cancelled', 'absent', 'verified',
                        'failed', 'inconclusive', 'consumed'
                    )
                ),
                CONSTRAINT ck_billing_migration_receipts_sha256 CHECK (
                    content_sha256 ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT ck_billing_migration_receipts_external_reference CHECK (
                    external_reference IS NULL
                    OR external_reference ~ '^[A-Za-z0-9._:-]+$'
                ),
                CONSTRAINT ck_billing_migration_receipts_saga_shape CHECK (
                    (receipt_kind NOT IN ('cancel_observation', 'provider_flip'))
                    OR (receipt_kind = 'cancel_observation'
                        AND provider = 'abacatepay'
                        AND external_reference IS NOT NULL
                        AND status = 'cancelled')
                    OR (receipt_kind = 'provider_flip'
                        AND provider = 'asaas'
                        AND external_reference IS NOT NULL
                        AND status = 'verified')
                ),
                CONSTRAINT fk_billing_migration_receipts_builder_migration
                    FOREIGN KEY (builder_id, migration_id)
                    REFERENCES billing_migrations(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_migration_receipts_operation_kind UNIQUE (
                    migration_id, operation_id, receipt_kind
                )
            )
            """,
            """
            CREATE TABLE billing_webhook_deliveries (
                id UUID PRIMARY KEY,
                builder_id UUID NULL REFERENCES builders(id) ON DELETE RESTRICT,
                provider VARCHAR(20) NOT NULL,
                event_id VARCHAR(160) NOT NULL,
                event_name VARCHAR(100) NOT NULL,
                provider_created_at TIMESTAMPTZ NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                resource_kind VARCHAR(30) NULL,
                resource_id VARCHAR(160) NULL,
                body_bytes INTEGER NOT NULL,
                body_sha256 VARCHAR(64) NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'received',
                CONSTRAINT ck_billing_webhook_deliveries_provider CHECK (
                    provider IN ('abacatepay', 'asaas')
                ),
                CONSTRAINT ck_billing_webhook_deliveries_event_fields CHECK (
                    event_id ~ '^[A-Za-z0-9._:-]+$'
                    AND event_name ~ '^[A-Z][A-Z0-9_]{0,99}$'
                ),
                CONSTRAINT ck_billing_webhook_deliveries_resource_kind CHECK (
                    resource_kind IS NULL OR resource_kind IN (
                        'checkout_session', 'subscription', 'topup_order', 'payment'
                    )
                ),
                CONSTRAINT ck_billing_webhook_deliveries_resource_id CHECK (
                    resource_id IS NULL OR resource_id ~ '^[A-Za-z0-9._:-]+$'
                ),
                CONSTRAINT ck_billing_webhook_deliveries_body_bytes CHECK (
                    body_bytes BETWEEN 2 AND 262144
                ),
                CONSTRAINT ck_billing_webhook_deliveries_sha256 CHECK (
                    body_sha256 ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT ck_billing_webhook_deliveries_status CHECK (
                    status IN (
                        'received', 'processed', 'unknown', 'readback_required'
                    )
                ),
                CONSTRAINT uq_billing_webhook_deliveries_event UNIQUE (
                    provider, event_id
                ),
                CONSTRAINT uq_billing_webhook_deliveries_builder_id_id UNIQUE (
                    builder_id, id
                )
            )
            """,
            """
            CREATE TABLE billing_payment_projections (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL REFERENCES builders(id) ON DELETE RESTRICT,
                provider VARCHAR(20) NOT NULL,
                payment_id VARCHAR(160) NOT NULL,
                aggregate_kind VARCHAR(20) NOT NULL,
                subscription_id UUID NULL,
                topup_order_id UUID NULL,
                checkout_session_id UUID NULL,
                canary_environment VARCHAR(20) NULL,
                canary_slot VARCHAR(20) NULL,
                status VARCHAR(40) NOT NULL,
                amount_cents BIGINT NOT NULL,
                settled_cents BIGINT NOT NULL DEFAULT 0,
                reversed_cents BIGINT NOT NULL DEFAULT 0,
                restored_cents BIGINT NOT NULL DEFAULT 0,
                restoration_epoch INTEGER NOT NULL DEFAULT 0,
                provider_created_at TIMESTAMPTZ NOT NULL,
                ordering_key VARCHAR(200) NOT NULL,
                last_event_id VARCHAR(160) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_payment_projections_provider CHECK (
                    provider IN ('abacatepay', 'asaas')
                ),
                CONSTRAINT ck_billing_payment_projections_payment_id CHECK (
                    payment_id ~ '^[A-Za-z0-9._:-]+$'
                ),
                CONSTRAINT ck_billing_payment_projections_aggregate_kind CHECK (
                    aggregate_kind IN ('subscription', 'topup')
                ),
                CONSTRAINT ck_billing_payment_projections_aggregate_target CHECK (
                    (aggregate_kind = 'subscription'
                        AND subscription_id IS NOT NULL
                        AND topup_order_id IS NULL)
                    OR (aggregate_kind = 'topup'
                        AND topup_order_id IS NOT NULL
                        AND subscription_id IS NULL)
                ),
                CONSTRAINT ck_billing_payment_projections_status CHECK (
                    status IN (
                        'pending', 'failed', 'overdue', 'settled',
                        'reversal_pending', 'partially_reversed', 'reversed',
                        'chargeback_requested', 'chargeback_dispute',
                        'restoration_pending', 'deleted', 'readback_required',
                        'restored'
                    )
                ),
                CONSTRAINT ck_billing_payment_projections_totals CHECK (
                    amount_cents >= 0 AND settled_cents >= 0
                    AND reversed_cents >= 0 AND restored_cents >= 0
                    AND restored_cents <= reversed_cents
                    AND reversed_cents <= settled_cents
                    AND settled_cents <= amount_cents
                ),
                CONSTRAINT ck_billing_payment_projections_restoration_epoch CHECK (
                    restoration_epoch >= 0
                ),
                CONSTRAINT ck_billing_payment_projections_canary_pair CHECK (
                    (canary_environment IS NULL AND canary_slot IS NULL)
                    OR (canary_environment IS NOT NULL
                        AND canary_slot IN ('subscription', 'topup'))
                ),
                CONSTRAINT ck_billing_payment_projections_canary_exact CHECK (
                    canary_slot IS NULL OR (
                        provider = 'asaas' AND checkout_session_id IS NOT NULL
                        AND (
                            (canary_slot = 'subscription'
                                AND aggregate_kind = 'subscription'
                                AND subscription_id IS NOT NULL
                                AND topup_order_id IS NULL
                                AND amount_cents = 99900)
                            OR (canary_slot = 'topup'
                                AND aggregate_kind = 'topup'
                                AND topup_order_id IS NOT NULL
                                AND subscription_id IS NULL
                                AND amount_cents = 10000)
                        )
                    )
                ),
                CONSTRAINT fk_billing_payment_projections_builder_subscription
                    FOREIGN KEY (builder_id, subscription_id)
                    REFERENCES billing_subscriptions(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_payment_projections_builder_topup
                    FOREIGN KEY (builder_id, topup_order_id)
                    REFERENCES billing_topup_orders(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_payment_projections_builder_checkout
                    FOREIGN KEY (builder_id, checkout_session_id)
                    REFERENCES billing_checkout_sessions(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_payment_projections_canary_checkout
                    FOREIGN KEY (
                        builder_id, checkout_session_id,
                        canary_environment, canary_slot
                    )
                    REFERENCES billing_checkout_sessions(
                        builder_id, id, canary_environment, canary_slot
                    )
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_payment_projections_payment UNIQUE (
                    provider, payment_id
                ),
                CONSTRAINT uq_billing_payment_projections_builder_id_id UNIQUE (
                    builder_id, id
                ),
                CONSTRAINT uq_billing_payment_projections_identity UNIQUE (
                    builder_id, id, provider, payment_id
                ),
                CONSTRAINT uq_billing_payment_projections_canary_identity UNIQUE (
                    builder_id, id, provider, payment_id,
                    canary_environment, canary_slot
                )
            )
            """,
            """
            CREATE UNIQUE INDEX uq_billing_payment_projections_canary_slot
            ON billing_payment_projections(canary_environment, canary_slot)
            WHERE canary_environment IS NOT NULL
            """,
            """
            CREATE TABLE billing_payment_effects (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL,
                projection_id UUID NOT NULL,
                delivery_id UUID NOT NULL,
                provider VARCHAR(20) NOT NULL,
                payment_id VARCHAR(160) NOT NULL,
                effect_kind VARCHAR(20) NOT NULL,
                source_revision VARCHAR(100) NOT NULL,
                amount_cents BIGINT NOT NULL,
                canary_environment VARCHAR(20) NULL,
                canary_slot VARCHAR(20) NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_payment_effects_provider CHECK (
                    provider IN ('abacatepay', 'asaas')
                ),
                CONSTRAINT ck_billing_payment_effects_kind CHECK (
                    effect_kind IN ('settlement', 'reversal', 'restoration')
                ),
                CONSTRAINT ck_billing_payment_effects_amount CHECK (
                    amount_cents >= 0
                ),
                CONSTRAINT ck_billing_payment_effects_canary_pair CHECK (
                    (canary_environment IS NULL AND canary_slot IS NULL)
                    OR (canary_environment IS NOT NULL
                        AND canary_slot IN ('subscription', 'topup'))
                ),
                CONSTRAINT ck_billing_payment_effects_canary_exact CHECK (
                    canary_slot IS NULL OR (
                        effect_kind = 'settlement'
                        AND source_revision = 'settlement:v1'
                        AND ((canary_slot = 'subscription'
                                AND amount_cents = 99900)
                            OR (canary_slot = 'topup'
                                AND amount_cents = 10000))
                    )
                ),
                CONSTRAINT ck_billing_payment_effects_source_revision CHECK (
                    char_length(source_revision) BETWEEN 1 AND 100
                    AND source_revision !~ '[[\\:cntrl\\:]]'
                ),
                CONSTRAINT fk_billing_payment_effects_builder_projection
                    FOREIGN KEY (
                        builder_id, projection_id, provider, payment_id
                    )
                    REFERENCES billing_payment_projections(
                        builder_id, id, provider, payment_id
                    )
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_payment_effects_canary_projection
                    FOREIGN KEY (
                        builder_id, projection_id, provider, payment_id,
                        canary_environment, canary_slot
                    )
                    REFERENCES billing_payment_projections(
                        builder_id, id, provider, payment_id,
                        canary_environment, canary_slot
                    )
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_payment_effects_builder_delivery
                    FOREIGN KEY (builder_id, delivery_id)
                    REFERENCES billing_webhook_deliveries(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_payment_effects_idempotency UNIQUE (
                    provider, payment_id, effect_kind, source_revision
                ),
                CONSTRAINT uq_billing_payment_effects_builder_id_id UNIQUE (
                    builder_id, id
                ),
                CONSTRAINT uq_billing_payment_effects_identity UNIQUE (
                    builder_id, id, provider, payment_id
                )
            )
            """,
            """
            CREATE UNIQUE INDEX uq_billing_payment_effects_canary_slot
            ON billing_payment_effects(canary_environment, canary_slot)
            WHERE canary_environment IS NOT NULL
              AND effect_kind = 'settlement'
            """,
            """
            ALTER TABLE transactions
                ADD COLUMN provider VARCHAR(20) NULL,
                ADD COLUMN provider_payment_id VARCHAR(160) NULL,
                ADD COLUMN billing_payment_effect_id UUID NULL,
                ADD CONSTRAINT ck_transactions_provider_payment_pair CHECK (
                    (provider IS NULL AND provider_payment_id IS NULL)
                    OR (provider IN ('abacatepay', 'asaas')
                        AND provider_payment_id IS NOT NULL)
                ),
                ADD CONSTRAINT ck_transactions_billing_effect_identity CHECK (
                    (billing_payment_effect_id IS NULL
                        AND provider IS NULL AND provider_payment_id IS NULL)
                    OR (billing_payment_effect_id IS NOT NULL
                        AND provider IS NOT NULL
                        AND provider_payment_id IS NOT NULL)
                ),
                ADD CONSTRAINT fk_transactions_billing_effect_identity
                    FOREIGN KEY (
                        builder_id, billing_payment_effect_id,
                        provider, provider_payment_id
                    )
                    REFERENCES billing_payment_effects(
                        builder_id, id, provider, payment_id
                    )
                    ON DELETE RESTRICT,
                ADD CONSTRAINT uq_transactions_provider_payment UNIQUE (
                    provider, provider_payment_id
                ),
                ADD CONSTRAINT uq_transactions_billing_payment_effect UNIQUE (
                    billing_payment_effect_id
                )
            """,
            """
            ALTER TABLE billing_rollout_control
                ADD CONSTRAINT fk_billing_rollout_subscription_checkout
                    FOREIGN KEY (canary_subscription_checkout_id)
                    REFERENCES billing_checkout_sessions(id) ON DELETE RESTRICT,
                ADD CONSTRAINT fk_billing_rollout_topup_checkout
                    FOREIGN KEY (canary_topup_checkout_id)
                    REFERENCES billing_checkout_sessions(id) ON DELETE RESTRICT,
                ADD CONSTRAINT fk_billing_rollout_subscription_effect
                    FOREIGN KEY (canary_subscription_effect_id)
                    REFERENCES billing_payment_effects(id) ON DELETE RESTRICT,
                ADD CONSTRAINT fk_billing_rollout_topup_effect
                    FOREIGN KEY (canary_topup_effect_id)
                    REFERENCES billing_payment_effects(id) ON DELETE RESTRICT
            """,
            """
            CREATE TABLE billing_outbox (
                id UUID PRIMARY KEY,
                builder_id UUID NOT NULL REFERENCES builders(id) ON DELETE RESTRICT,
                schema_version VARCHAR(40) NOT NULL,
                event_type VARCHAR(60) NOT NULL,
                aggregate_kind VARCHAR(30) NOT NULL,
                aggregate_id UUID NOT NULL,
                subscription_id UUID NULL,
                topup_order_id UUID NULL,
                financial_revision BIGINT NOT NULL,
                payload JSONB NOT NULL,
                payload_sha256 VARCHAR(64) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_billing_outbox_schema_version CHECK (
                    schema_version = 'billing.fact.v1'
                ),
                CONSTRAINT ck_billing_outbox_event_type CHECK (
                    event_type IN (
                        'subscription.cycle_settled.v1',
                        'subscription.cycle_reversed.v1',
                        'subscription.cycle_restored.v1',
                        'topup.payment_settled.v1',
                        'topup.payment_reversed.v1',
                        'topup.payment_restored.v1'
                    )
                ),
                CONSTRAINT ck_billing_outbox_aggregate_kind CHECK (
                    aggregate_kind IN ('subscription_cycle', 'topup_order')
                ),
                CONSTRAINT ck_billing_outbox_financial_revision CHECK (
                    financial_revision > 0
                ),
                CONSTRAINT ck_billing_outbox_aggregate_target CHECK (
                    (aggregate_kind = 'subscription_cycle'
                        AND subscription_id IS NOT NULL
                        AND topup_order_id IS NULL
                        AND aggregate_id = subscription_id)
                    OR (aggregate_kind = 'topup_order'
                        AND topup_order_id IS NOT NULL
                        AND subscription_id IS NULL
                        AND aggregate_id = topup_order_id)
                ),
                CONSTRAINT ck_billing_outbox_payload_sha256 CHECK (
                    payload_sha256 ~ '^[0-9a-f]{64}$'
                ),
                CONSTRAINT fk_billing_outbox_builder_subscription
                    FOREIGN KEY (builder_id, subscription_id)
                    REFERENCES billing_subscriptions(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_billing_outbox_builder_topup
                    FOREIGN KEY (builder_id, topup_order_id)
                    REFERENCES billing_topup_orders(builder_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT uq_billing_outbox_aggregate_revision UNIQUE (
                    aggregate_kind, aggregate_id, financial_revision
                )
            )
            """,
            """
            CREATE OR REPLACE FUNCTION reject_immutable_billing_row()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
                    USING ERRCODE = 'integrity_constraint_violation';
            END;
            $function$
            """,
            """
            CREATE OR REPLACE FUNCTION lock_billing_roster_statement()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                PERFORM pg_advisory_xact_lock(3486705124568449331);
                PERFORM 1 FROM billing_roster_control
                WHERE singleton = true FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'global billing roster control is unavailable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NULL;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_subscriptions_roster_fence
            BEFORE UPDATE OR DELETE ON billing_subscriptions
            FOR EACH STATEMENT EXECUTE FUNCTION lock_billing_roster_statement()
            """,
            """
            CREATE TRIGGER trg_billing_migrations_roster_fence
            BEFORE UPDATE OR DELETE ON billing_migrations
            FOR EACH STATEMENT EXECUTE FUNCTION lock_billing_roster_statement()
            """,
            """
            CREATE TRIGGER trg_billing_migration_receipts_roster_fence
            BEFORE INSERT OR UPDATE OR DELETE ON billing_migration_receipts
            FOR EACH STATEMENT EXECUTE FUNCTION lock_billing_roster_statement()
            """,
            """
            CREATE TRIGGER trg_billing_roster_control_fence
            BEFORE UPDATE OR DELETE ON billing_roster_control
            FOR EACH STATEMENT EXECUTE FUNCTION lock_billing_roster_statement()
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
                WHERE id = NEW.migration_id AND builder_id = NEW.builder_id
                FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'migration receipt target missing'; END IF;
                SELECT * INTO subscription FROM billing_subscriptions
                WHERE id = migration.subscription_id
                  AND builder_id = migration.builder_id FOR UPDATE;
                IF (NEW.receipt_kind = 'provider_flip'
                    OR (NEW.receipt_kind = 'cancel_observation'
                        AND NEW.status = 'cancelled'))
                   AND (migration.source_roster_revision <>
                            subscription.roster_revision
                        OR migration.source_roster_revision <>
                            current_revision) THEN
                    RAISE EXCEPTION 'migration receipt source revision is stale';
                END IF;
                IF NEW.receipt_kind IN ('cancel_observation', 'provider_flip')
                   AND NEW.operation_id IS DISTINCT FROM migration.operation_id THEN
                    RAISE EXCEPTION 'migration receipt operation mismatch';
                END IF;
                IF NEW.receipt_kind = 'cancel_observation' THEN
                    expected_digest := encode(sha256(convert_to(jsonb_build_object(
                      'migration_id', migration.id,
                      'builder_id', migration.builder_id,
                      'subscription_id', migration.subscription_id,
                      'operation_id', migration.operation_id,
                      'legacy_external_subscription_id',
                          subscription.external_subscription_id,
                      'provider', 'abacatepay', 'status', 'cancelled',
                      'observed_at', NEW.observed_at)::text,'UTF8')),'hex');
                    IF migration.state IS DISTINCT FROM 'legacy_cancel_pending'
                       OR subscription.provider IS NOT NULL
                       OR subscription.status IS DISTINCT FROM 'legacy_cancel_pending'
                       OR NEW.provider IS DISTINCT FROM 'abacatepay'
                       OR NEW.status IS DISTINCT FROM 'cancelled'
                       OR NEW.external_reference IS DISTINCT FROM
                          subscription.external_subscription_id
                       OR NEW.observed_at < migration.created_at
                       OR NEW.observed_at > clock_timestamp()
                       OR NEW.content_sha256 IS DISTINCT FROM expected_digest THEN
                        RAISE EXCEPTION 'invalid cancellation observation';
                    END IF;
                ELSIF NEW.receipt_kind = 'provider_flip' THEN
                    expected_digest := encode(sha256(convert_to(jsonb_build_object(
                      'migration_id', migration.id, 'builder_id', migration.builder_id,
                      'subscription_id', migration.subscription_id,
                      'operation_id', migration.operation_id,
                      'source_roster_revision', migration.source_roster_revision,
                      'target_provider', migration.target_provider,
                      'target_external_subscription_id', migration.target_external_subscription_id,
                      'target_catalog_version_id', migration.target_catalog_version_id,
                      'target_offer_id', migration.target_offer_id,
                      'target_plan', migration.target_plan,
                      'target_billing_cycle', migration.target_billing_cycle,
                      'target_coupon_code', migration.target_coupon_code,
                      'target_amount_cents', migration.target_amount_cents,
                      'target_snapshot', migration.target_snapshot)::text,'UTF8')),'hex');
                    IF migration.state IS DISTINCT FROM 'legacy_cancelled'
                       OR subscription.provider IS NOT NULL
                       OR subscription.status IS DISTINCT FROM 'legacy_cancelled'
                       OR NEW.provider IS DISTINCT FROM 'asaas'
                       OR NEW.status IS DISTINCT FROM 'verified'
                       OR NEW.external_reference IS DISTINCT FROM
                          migration.target_external_subscription_id
                       OR NEW.observed_at < migration.created_at
                       OR NEW.observed_at > clock_timestamp()
                       OR NEW.content_sha256 IS DISTINCT FROM expected_digest THEN
                        RAISE EXCEPTION 'invalid provider flip receipt';
                    END IF;
                END IF;
                RETURN NEW;
            END $function$
            """,
            """
            CREATE TRIGGER trg_billing_migration_receipt_validate
            BEFORE INSERT ON billing_migration_receipts
            FOR EACH ROW EXECUTE FUNCTION validate_billing_migration_receipt()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_migration_receipt_commit()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE migration billing_migrations%ROWTYPE;
                    subscription billing_subscriptions%ROWTYPE;
                    current_revision BIGINT;
            BEGIN
                IF NEW.receipt_kind NOT IN ('cancel_observation', 'provider_flip') THEN
                    RETURN NULL;
                END IF;
                SELECT * INTO migration FROM billing_migrations
                WHERE id = NEW.migration_id AND builder_id = NEW.builder_id;
                SELECT * INTO subscription FROM billing_subscriptions
                WHERE id = migration.subscription_id
                  AND builder_id = migration.builder_id;
                SELECT billing_roster_revision INTO current_revision
                FROM billing_roster_control WHERE singleton = true;
                IF NEW.receipt_kind = 'cancel_observation' AND (
                    migration.state NOT IN ('legacy_cancelled', 'migrated')
                    OR migration.legacy_cancelled_at IS DISTINCT FROM NEW.observed_at
                    OR (migration.state = 'legacy_cancelled' AND (
                        subscription.provider IS NOT NULL
                        OR subscription.status IS DISTINCT FROM 'legacy_cancelled'
                        OR subscription.roster_revision IS DISTINCT FROM
                           migration.source_roster_revision
                    ))
                    OR (migration.state = 'migrated' AND (
                        subscription.provider IS DISTINCT FROM 'asaas'
                        OR subscription.roster_revision IS DISTINCT FROM
                           migration.source_roster_revision + 1
                    ))
                ) THEN
                    RAISE EXCEPTION 'cancellation receipt requires converged saga at commit'
                        USING ERRCODE = 'integrity_constraint_violation';
                ELSIF NEW.receipt_kind = 'provider_flip' AND (
                    migration.state IS DISTINCT FROM 'migrated'
                    OR subscription.provider IS DISTINCT FROM 'asaas'
                    OR subscription.external_subscription_id IS DISTINCT FROM
                       migration.target_external_subscription_id
                    OR subscription.catalog_version_id IS DISTINCT FROM
                       migration.target_catalog_version_id
                    OR subscription.offer_id IS DISTINCT FROM migration.target_offer_id
                    OR subscription.plan IS DISTINCT FROM migration.target_plan
                    OR subscription.billing_cycle IS DISTINCT FROM
                       migration.target_billing_cycle
                    OR subscription.coupon_code IS DISTINCT FROM
                       migration.target_coupon_code
                    OR subscription.amount_cents IS DISTINCT FROM
                       migration.target_amount_cents
                    OR subscription.roster_revision IS DISTINCT FROM
                       migration.source_roster_revision + 1
                    OR current_revision IS DISTINCT FROM subscription.roster_revision
                ) THEN
                    RAISE EXCEPTION 'provider flip receipt requires migrated saga at commit'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NULL;
            END;
            $function$
            """,
            """
            CREATE CONSTRAINT TRIGGER trg_billing_migration_receipt_commit
            AFTER INSERT ON billing_migration_receipts
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_billing_migration_receipt_commit()
            """,
            """
            CREATE TRIGGER trg_billing_migration_receipts_immutable
            BEFORE UPDATE OR DELETE ON billing_migration_receipts
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
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
            """
            CREATE TRIGGER trg_billing_migration_target_immutable
            BEFORE UPDATE ON billing_migrations
            FOR EACH ROW EXECUTE FUNCTION protect_bound_billing_migration_target()
            """,
            """
            CREATE TRIGGER trg_billing_rollout_receipts_immutable
            BEFORE UPDATE OR DELETE ON billing_rollout_receipts
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
            """
            CREATE TRIGGER trg_billing_topup_capability_receipts_immutable
            BEFORE UPDATE OR DELETE ON billing_topup_capability_receipts
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
            """
            CREATE TRIGGER trg_billing_outbox_immutable
            BEFORE UPDATE OR DELETE ON billing_outbox
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_capability_lifetime()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF NEW.approved_at > clock_timestamp()
                   OR NEW.expires_at <= clock_timestamp() THEN
                    RAISE EXCEPTION 'capability must be currently approved'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_topup_capability_lifetime
            BEFORE INSERT ON billing_topup_capability_receipts
            FOR EACH ROW EXECUTE FUNCTION validate_billing_capability_lifetime()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_canary_checkout()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE
                active_canary_environment VARCHAR(20);
            BEGIN
                PERFORM pg_advisory_xact_lock(hashtextextended(
                    'billing-canary-builder:' || NEW.builder_id::text, 0
                ));
                SELECT environment INTO active_canary_environment
                FROM billing_rollout_control
                WHERE state = 'canary_allowlist'
                  AND canary_builder_id = NEW.builder_id
                ORDER BY environment
                LIMIT 1;
                IF NEW.state IN (
                       'creating', 'active', 'paid_pending_subscription',
                       'reconciliation_required', 'terminal_pending_readback'
                   ) AND active_canary_environment IS NOT NULL
                   AND (
                       NEW.canary_environment IS DISTINCT FROM
                           active_canary_environment
                       OR NEW.canary_slot IS NULL
                   ) THEN
                    RAISE EXCEPTION 'active canary checkout provenance is required'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF TG_OP = 'INSERT' AND NEW.canary_environment IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM billing_rollout_control
                       WHERE environment = NEW.canary_environment
                         AND state = 'canary_allowlist'
                         AND canary_builder_id = NEW.builder_id
                   ) THEN
                    RAISE EXCEPTION 'canary checkout is not DB-authorized'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF TG_OP = 'INSERT' AND NEW.canary_environment IS NOT NULL
                   AND NEW.state <> 'creating' THEN
                    RAISE EXCEPTION 'canary checkout must begin creating'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF TG_OP = 'UPDATE' AND (
                    OLD.canary_environment IS DISTINCT FROM NEW.canary_environment
                    OR OLD.canary_slot IS DISTINCT FROM NEW.canary_slot
                ) THEN
                    RAISE EXCEPTION 'canary checkout provenance is immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF TG_OP = 'UPDATE'
                   AND OLD.external_checkout_id IS NOT NULL
                   AND OLD.external_checkout_id IS DISTINCT FROM
                       NEW.external_checkout_id THEN
                    RAISE EXCEPTION 'provider checkout identity is write-once'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF TG_OP = 'UPDATE'
                   AND OLD.resulting_external_subscription_id IS NOT NULL
                   AND OLD.resulting_external_subscription_id IS DISTINCT FROM
                       NEW.resulting_external_subscription_id THEN
                    RAISE EXCEPTION 'provider subscription result is write-once'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF TG_OP = 'UPDATE' AND OLD.canary_environment IS NOT NULL
                   AND ROW(
                        OLD.id, OLD.builder_id, OLD.intent_kind, OLD.intent_id,
                        OLD.migration_id, OLD.topup_order_id, OLD.attempt_no,
                        OLD.provider, OLD.catalog_version_id, OLD.offer_id,
                        OLD.amount_cents, OLD.pricing_snapshot, OLD.next_due_date,
                        OLD.canary_environment, OLD.canary_slot
                   ) IS DISTINCT FROM ROW(
                        NEW.id, NEW.builder_id, NEW.intent_kind, NEW.intent_id,
                        NEW.migration_id, NEW.topup_order_id, NEW.attempt_no,
                        NEW.provider, NEW.catalog_version_id, NEW.offer_id,
                        NEW.amount_cents, NEW.pricing_snapshot, NEW.next_due_date,
                        NEW.canary_environment, NEW.canary_slot
                   ) THEN
                    RAISE EXCEPTION 'canary checkout identity is immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_canary_checkout_validate
            BEFORE INSERT OR UPDATE ON billing_checkout_sessions
            FOR EACH ROW EXECUTE FUNCTION validate_billing_canary_checkout()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_canary_checkout_slot()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF NEW.canary_environment IS NOT NULL AND NOT EXISTS (
                    SELECT 1
                    FROM billing_rollout_control AS control
                    WHERE control.environment = NEW.canary_environment
                      AND (
                          (NEW.canary_slot = 'subscription'
                           AND control.canary_subscription_checkout_id = NEW.id)
                          OR (NEW.canary_slot = 'topup'
                              AND control.canary_topup_checkout_id = NEW.id)
                      )
                ) THEN
                    RAISE EXCEPTION 'canary checkout is outside its lifetime slot'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NULL;
            END;
            $function$
            """,
            """
            CREATE CONSTRAINT TRIGGER trg_billing_canary_checkout_slot
            AFTER INSERT OR UPDATE ON billing_checkout_sessions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_billing_canary_checkout_slot()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_canary_projection()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE
                checkout_environment VARCHAR(20);
                checkout_slot VARCHAR(20);
                checkout_topup_order_id UUID;
                checkout_intent_id UUID;
                checkout_migration_id UUID;
                checkout_external_checkout_id VARCHAR(160);
                checkout_result_subscription_id VARCHAR(160);
                historical_checkout_id UUID;
                historical_environment VARCHAR(20);
                historical_slot VARCHAR(20);
                active_canary_environment VARCHAR(20);
            BEGIN
                IF TG_OP = 'UPDATE'
                   AND (OLD.canary_environment IS NOT NULL
                        OR NEW.canary_environment IS NOT NULL)
                   AND ROW(
                        OLD.id, OLD.builder_id, OLD.provider, OLD.payment_id,
                        OLD.aggregate_kind, OLD.subscription_id,
                        OLD.topup_order_id, OLD.checkout_session_id,
                        OLD.amount_cents, OLD.canary_environment, OLD.canary_slot
                   ) IS DISTINCT FROM ROW(
                        NEW.id, NEW.builder_id, NEW.provider, NEW.payment_id,
                        NEW.aggregate_kind, NEW.subscription_id,
                        NEW.topup_order_id, NEW.checkout_session_id,
                        NEW.amount_cents, NEW.canary_environment, NEW.canary_slot
                   ) THEN
                    RAISE EXCEPTION 'canary projection identity is immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.checkout_session_id IS NOT NULL THEN
                    SELECT canary_environment, canary_slot, topup_order_id,
                           intent_id, migration_id, external_checkout_id,
                           resulting_external_subscription_id
                    INTO checkout_environment, checkout_slot,
                         checkout_topup_order_id, checkout_intent_id,
                         checkout_migration_id, checkout_external_checkout_id,
                         checkout_result_subscription_id
                    FROM billing_checkout_sessions
                    WHERE builder_id = NEW.builder_id
                      AND id = NEW.checkout_session_id;
                END IF;
                IF NEW.aggregate_kind = 'subscription' THEN
                    SELECT checkout.id, checkout.canary_environment,
                           checkout.canary_slot
                    INTO historical_checkout_id, historical_environment,
                         historical_slot
                    FROM billing_checkout_sessions AS checkout
                    LEFT JOIN billing_migrations AS migration
                      ON migration.builder_id = checkout.builder_id
                     AND migration.id = checkout.migration_id
                    WHERE checkout.builder_id = NEW.builder_id
                      AND checkout.canary_slot = 'subscription'
                      AND (checkout.intent_id = NEW.subscription_id
                           OR migration.subscription_id = NEW.subscription_id)
                    ORDER BY checkout.created_at, checkout.id
                    LIMIT 1;
                ELSE
                    SELECT checkout.id, checkout.canary_environment,
                           checkout.canary_slot
                    INTO historical_checkout_id, historical_environment,
                         historical_slot
                    FROM billing_checkout_sessions AS checkout
                    WHERE checkout.builder_id = NEW.builder_id
                      AND checkout.canary_slot = 'topup'
                      AND checkout.topup_order_id = NEW.topup_order_id
                    ORDER BY checkout.created_at, checkout.id
                    LIMIT 1;
                END IF;
                IF historical_checkout_id IS NOT NULL
                   AND (NEW.checkout_session_id
                            IS DISTINCT FROM historical_checkout_id
                        OR NEW.canary_environment
                            IS DISTINCT FROM historical_environment
                        OR NEW.canary_slot IS DISTINCT FROM historical_slot) THEN
                    RAISE EXCEPTION 'historical canary lineage is required'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF checkout_environment IS NOT NULL THEN
                    IF checkout_external_checkout_id IS NULL THEN
                        RAISE EXCEPTION 'provider checkout identity is required'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF NEW.canary_environment IS DISTINCT FROM checkout_environment
                       OR NEW.canary_slot IS DISTINCT FROM checkout_slot THEN
                        RAISE EXCEPTION 'canary projection provenance is required'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF checkout_slot = 'topup'
                       AND NEW.topup_order_id IS DISTINCT FROM checkout_topup_order_id
                    THEN
                        RAISE EXCEPTION 'canary topup projection target mismatch'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF checkout_slot = 'subscription'
                       AND NEW.subscription_id IS DISTINCT FROM checkout_intent_id
                       AND NOT EXISTS (
                           SELECT 1 FROM billing_migrations
                           WHERE id = checkout_migration_id
                             AND builder_id = NEW.builder_id
                             AND subscription_id = NEW.subscription_id
                       ) THEN
                        RAISE EXCEPTION 'canary subscription projection target mismatch'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF checkout_slot = 'subscription' AND NOT EXISTS (
                        SELECT 1 FROM billing_subscriptions
                        WHERE id = NEW.subscription_id
                          AND builder_id = NEW.builder_id
                          AND provider = 'asaas'
                          AND is_active
                          AND plan = 'plus'
                          AND billing_cycle = 'monthly'
                          AND coupon_code IS NULL
                          AND catalog_version_id = 'payment-2026-10-03'
                          AND offer_id = 'plan_plus_monthly_v1'
                          AND amount_cents = 99900
                          AND status = 'active'
                    ) THEN
                        RAISE EXCEPTION 'canonical Plus subscription is required'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF checkout_slot = 'subscription' AND NOT EXISTS (
                        SELECT 1 FROM billing_subscriptions
                        WHERE id = NEW.subscription_id
                          AND builder_id = NEW.builder_id
                          AND external_subscription_id =
                              checkout_result_subscription_id
                    ) THEN
                        RAISE EXCEPTION 'provider subscription lineage is required'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                ELSIF NEW.canary_environment IS NOT NULL THEN
                    RAISE EXCEPTION 'canary projection requires canary checkout'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT environment INTO active_canary_environment
                FROM billing_rollout_control
                WHERE state = 'canary_allowlist'
                  AND canary_builder_id = NEW.builder_id
                ORDER BY environment
                LIMIT 1;
                IF TG_OP = 'INSERT' AND NEW.provider = 'asaas'
                   AND active_canary_environment IS NOT NULL
                   AND NEW.canary_environment IS NULL THEN
                    RAISE EXCEPTION 'active canary projection provenance is required'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_canary_projection_validate
            BEFORE INSERT OR UPDATE ON billing_payment_projections
            FOR EACH ROW EXECUTE FUNCTION validate_billing_canary_projection()
            """,
            """
            CREATE OR REPLACE FUNCTION protect_billing_canary_subscription()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE
                approved_migration_id UUID;
                saga_migration_id UUID;
                current_roster_revision BIGINT;
                resulting_roster_revision BIGINT;
                cancellation_observed_at TIMESTAMPTZ;
            BEGIN
                IF TG_OP = 'UPDATE'
                   AND OLD.provider = 'asaas'
                   AND ROW(
                       OLD.provider, OLD.external_subscription_id, OLD.plan,
                       OLD.billing_cycle, OLD.coupon_code,
                       OLD.catalog_version_id, OLD.offer_id,
                       OLD.amount_cents, OLD.roster_revision
                   ) IS DISTINCT FROM ROW(
                       NEW.provider, NEW.external_subscription_id, NEW.plan,
                       NEW.billing_cycle, NEW.coupon_code,
                       NEW.catalog_version_id, NEW.offer_id,
                       NEW.amount_cents, NEW.roster_revision
                   ) THEN
                    RAISE EXCEPTION 'provider subscription snapshot is write-once'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF TG_OP = 'UPDATE'
                   AND OLD.provider IS NULL
                   AND NEW.provider IS NULL
                   AND OLD.status IS DISTINCT FROM NEW.status
                   AND NEW.status = 'legacy_cancelled'
                THEN
                    IF OLD.status <> 'legacy_cancel_pending'
                       OR OLD.is_active OR NEW.is_active
                       OR ROW(OLD.external_subscription_id, OLD.plan,
                              OLD.billing_cycle, OLD.coupon_code,
                              OLD.catalog_version_id, OLD.offer_id,
                              OLD.amount_cents, OLD.roster_revision)
                          IS DISTINCT FROM
                          ROW(NEW.external_subscription_id, NEW.plan,
                              NEW.billing_cycle, NEW.coupon_code,
                              NEW.catalog_version_id, NEW.offer_id,
                              NEW.amount_cents, NEW.roster_revision)
                    THEN
                        RAISE EXCEPTION 'legacy cancellation cannot be self-asserted';
                    END IF;
                    PERFORM pg_advisory_xact_lock(3486705124568449331);
                    SELECT billing_roster_revision INTO current_roster_revision
                    FROM billing_roster_control WHERE singleton = true FOR UPDATE;
                    IF OLD.roster_revision <> current_roster_revision THEN
                        RAISE EXCEPTION 'legacy cancellation source is stale';
                    END IF;
                    SELECT migration.id, receipt.observed_at
                    INTO saga_migration_id, cancellation_observed_at
                    FROM billing_migrations migration
                    JOIN billing_migration_receipts receipt
                      ON receipt.migration_id = migration.id
                     AND receipt.builder_id = migration.builder_id
                     AND receipt.operation_id = migration.operation_id
                     AND receipt.receipt_kind = 'cancel_observation'
                     AND receipt.provider = 'abacatepay'
                     AND receipt.status = 'cancelled'
                     AND receipt.external_reference =
                         OLD.external_subscription_id
                    WHERE migration.builder_id = OLD.builder_id
                      AND migration.subscription_id = OLD.id
                      AND migration.state = 'legacy_cancel_pending'
                      AND migration.source_roster_revision =
                          current_roster_revision
                    FOR UPDATE OF migration;
                    IF saga_migration_id IS NULL THEN
                        RAISE EXCEPTION 'exact cancellation observation required';
                    END IF;
                    UPDATE billing_migrations
                    SET state = 'legacy_cancelled',
                        legacy_cancelled_at = cancellation_observed_at,
                        updated_at = now()
                    WHERE id = saga_migration_id
                      AND state = 'legacy_cancel_pending';
                ELSIF TG_OP = 'UPDATE'
                   AND OLD.provider IS NULL
                   AND OLD.status IS DISTINCT FROM NEW.status
                   AND NEW.status = 'legacy_cancelled'
                THEN
                    RAISE EXCEPTION 'legacy cancellation cannot be self-asserted';
                END IF;
                IF TG_OP = 'UPDATE'
                   AND OLD.provider IS NULL AND NEW.provider IS NULL
                   AND OLD.status = 'legacy_cancelled'
                   AND NEW.status = 'legacy_cancelled'
                   AND OLD.roster_revision IS DISTINCT FROM NEW.roster_revision
                   AND ROW(OLD.external_subscription_id, OLD.plan,
                           OLD.billing_cycle, OLD.coupon_code,
                           OLD.catalog_version_id, OLD.offer_id, OLD.amount_cents)
                       IS NOT DISTINCT FROM
                       ROW(NEW.external_subscription_id, NEW.plan,
                           NEW.billing_cycle, NEW.coupon_code,
                           NEW.catalog_version_id, NEW.offer_id, NEW.amount_cents)
                THEN
                    PERFORM pg_advisory_xact_lock(3486705124568449331);
                    SELECT billing_roster_revision INTO current_roster_revision
                    FROM billing_roster_control WHERE singleton = true FOR UPDATE;
                    IF NEW.roster_revision <> current_roster_revision THEN
                        RAISE EXCEPTION 'legacy migration rebase is stale';
                    END IF;
                    UPDATE billing_migrations
                    SET source_roster_revision = current_roster_revision,
                        updated_at = now()
                    WHERE builder_id = OLD.builder_id
                      AND subscription_id = OLD.id
                      AND state = 'legacy_cancelled'
                      AND source_roster_revision = OLD.roster_revision
                      AND NOT EXISTS (
                        SELECT 1 FROM billing_migration_receipts receipt
                        WHERE receipt.migration_id = billing_migrations.id
                          AND receipt.receipt_kind = 'provider_flip')
                      AND EXISTS (
                        SELECT 1 FROM billing_migration_receipts receipt
                        WHERE receipt.migration_id = billing_migrations.id
                          AND receipt.builder_id = billing_migrations.builder_id
                          AND receipt.operation_id = billing_migrations.operation_id
                          AND receipt.receipt_kind = 'cancel_observation'
                          AND receipt.provider = 'abacatepay'
                          AND receipt.status = 'cancelled'
                          AND receipt.external_reference =
                              OLD.external_subscription_id
                          AND receipt.observed_at = legacy_cancelled_at
                          AND receipt.content_sha256 = encode(
                            sha256(convert_to(jsonb_build_object(
                              'migration_id', billing_migrations.id,
                              'builder_id', billing_migrations.builder_id,
                              'subscription_id',
                                billing_migrations.subscription_id,
                              'operation_id', billing_migrations.operation_id,
                              'legacy_external_subscription_id',
                                OLD.external_subscription_id,
                              'provider', 'abacatepay',
                              'status', 'cancelled',
                              'observed_at', receipt.observed_at
                            )::text, 'UTF8')), 'hex'));
                    IF NOT FOUND THEN RAISE EXCEPTION 'legacy rebase denied'; END IF;
                    RETURN NEW;
                END IF;
                IF TG_OP = 'UPDATE'
                   AND OLD.provider IN ('abacatepay')
                   AND NEW.provider IS NULL
                   AND ROW(
                       OLD.external_subscription_id, OLD.plan,
                       OLD.billing_cycle, OLD.coupon_code,
                       OLD.catalog_version_id, OLD.offer_id,
                       OLD.amount_cents
                   ) IS NOT DISTINCT FROM ROW(
                       NEW.external_subscription_id, NEW.plan,
                       NEW.billing_cycle, NEW.coupon_code,
                       NEW.catalog_version_id, NEW.offer_id,
                       NEW.amount_cents
                   )
                   AND NEW.is_active = false
                   AND NEW.status = 'legacy_cancel_pending'
                THEN
                    PERFORM pg_advisory_xact_lock(3486705124568449331);
                    SELECT billing_roster_revision INTO current_roster_revision
                    FROM billing_roster_control WHERE singleton = true FOR UPDATE;
                    IF OLD.roster_revision <> current_roster_revision
                       OR NEW.roster_revision <> current_roster_revision + 1 THEN
                        RAISE EXCEPTION 'legacy cancellation roster revision is stale';
                    END IF;
                    UPDATE billing_migrations
                    SET state = 'legacy_cancel_pending',
                        source_roster_revision = NEW.roster_revision,
                        updated_at = now()
                    WHERE builder_id = OLD.builder_id
                      AND subscription_id = OLD.id
                      AND state IN ('in_progress', 'legacy_cancel_pending')
                      AND source_roster_revision = OLD.roster_revision
                    RETURNING id INTO saga_migration_id;
                    IF saga_migration_id IS NULL THEN
                        RAISE EXCEPTION 'legacy cancellation migration required';
                    END IF;
                    UPDATE billing_roster_control
                    SET billing_roster_revision = billing_roster_revision + 1,
                        updated_at = now()
                    WHERE singleton = true
                      AND billing_roster_revision = current_roster_revision;
                ELSIF TG_OP = 'UPDATE'
                   AND OLD.provider IS DISTINCT FROM 'asaas'
                   AND ROW(
                       OLD.provider, OLD.external_subscription_id, OLD.plan,
                       OLD.billing_cycle, OLD.coupon_code,
                       OLD.catalog_version_id, OLD.offer_id,
                       OLD.amount_cents, OLD.roster_revision
                   ) IS DISTINCT FROM ROW(
                       NEW.provider, NEW.external_subscription_id, NEW.plan,
                       NEW.billing_cycle, NEW.coupon_code,
                       NEW.catalog_version_id, NEW.offer_id,
                       NEW.amount_cents, NEW.roster_revision
                   )
                THEN
                    IF OLD.provider IS NOT NULL
                       OR OLD.status <> 'legacy_cancelled'
                       OR OLD.is_active
                       OR NEW.provider <> 'asaas'
                    THEN
                        RAISE EXCEPTION 'subscription snapshot mutation requires legacy flip'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;

                    PERFORM pg_advisory_xact_lock(3486705124568449331);
                    SELECT billing_roster_revision
                    INTO current_roster_revision
                    FROM billing_roster_control
                    WHERE singleton = true
                    FOR UPDATE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'global billing roster control is unavailable'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF NEW.roster_revision <> current_roster_revision + 1 THEN
                        RAISE EXCEPTION 'provider flip roster revision is stale'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF OLD.roster_revision <> current_roster_revision THEN
                        RAISE EXCEPTION 'provider flip source revision is stale'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;

                    SELECT migration.id
                    INTO approved_migration_id
                    FROM billing_migrations AS migration
                    JOIN billing_migration_receipts AS receipt
                      ON receipt.builder_id = migration.builder_id
                     AND receipt.migration_id = migration.id
                     AND receipt.operation_id = migration.operation_id
                     AND receipt.receipt_kind = 'provider_flip'
                     AND receipt.provider = 'asaas'
                     AND receipt.external_reference =
                         migration.target_external_subscription_id
                     AND receipt.status = 'verified'
                     AND receipt.content_sha256 = encode(
                         sha256(convert_to(jsonb_build_object(
                             'migration_id', migration.id,
                             'builder_id', migration.builder_id,
                             'subscription_id', migration.subscription_id,
                             'operation_id', migration.operation_id,
                             'source_roster_revision',
                                 migration.source_roster_revision,
                             'target_provider', migration.target_provider,
                             'target_external_subscription_id',
                                 migration.target_external_subscription_id,
                             'target_catalog_version_id',
                                 migration.target_catalog_version_id,
                             'target_offer_id', migration.target_offer_id,
                             'target_plan', migration.target_plan,
                             'target_billing_cycle',
                                 migration.target_billing_cycle,
                             'target_coupon_code',
                                 migration.target_coupon_code,
                             'target_amount_cents',
                                 migration.target_amount_cents,
                             'target_snapshot', migration.target_snapshot
                         )::text, 'UTF8')),
                         'hex'
                     )
                    WHERE migration.builder_id = OLD.builder_id
                      AND migration.subscription_id = OLD.id
                      AND migration.state = 'legacy_cancelled'
                      AND migration.operation_id IS NOT NULL
                      AND migration.source_roster_revision = OLD.roster_revision
                      AND migration.source_roster_revision =
                          current_roster_revision
                      AND migration.target_provider = NEW.provider
                      AND migration.target_external_subscription_id =
                          NEW.external_subscription_id
                      AND migration.target_catalog_version_id =
                          NEW.catalog_version_id
                      AND migration.target_offer_id = NEW.offer_id
                      AND migration.target_plan = NEW.plan
                      AND migration.target_billing_cycle = NEW.billing_cycle
                      AND migration.target_coupon_code IS NOT DISTINCT FROM
                          NEW.coupon_code
                      AND migration.target_amount_cents = NEW.amount_cents
                      AND EXISTS (
                        SELECT 1 FROM billing_migration_receipts cancellation
                        WHERE cancellation.migration_id = migration.id
                          AND cancellation.builder_id = migration.builder_id
                          AND cancellation.operation_id = migration.operation_id
                          AND cancellation.receipt_kind = 'cancel_observation'
                          AND cancellation.provider = 'abacatepay'
                          AND cancellation.status = 'cancelled'
                          AND cancellation.external_reference =
                              OLD.external_subscription_id
                          AND cancellation.observed_at =
                              migration.legacy_cancelled_at
                          AND cancellation.content_sha256 = encode(
                            sha256(convert_to(jsonb_build_object(
                              'migration_id', migration.id,
                              'builder_id', migration.builder_id,
                              'subscription_id', migration.subscription_id,
                              'operation_id', migration.operation_id,
                              'legacy_external_subscription_id',
                                OLD.external_subscription_id,
                              'provider', 'abacatepay',
                              'status', 'cancelled',
                              'observed_at', cancellation.observed_at
                            )::text, 'UTF8')), 'hex')
                      )
                    FOR UPDATE OF migration;
                    IF approved_migration_id IS NULL THEN
                        RAISE EXCEPTION 'ASAAS provider flip requires exact bound target'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;

                    UPDATE billing_roster_control
                    SET billing_roster_revision = billing_roster_revision + 1,
                        updated_at = now()
                    WHERE singleton = true
                      AND billing_roster_revision = current_roster_revision
                    RETURNING billing_roster_revision
                    INTO resulting_roster_revision;
                    IF resulting_roster_revision IS DISTINCT FROM
                        NEW.roster_revision
                    THEN
                        RAISE EXCEPTION 'global billing roster advance failed'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;

                    UPDATE billing_migrations
                    SET state = 'migrated', updated_at = now()
                    WHERE id = approved_migration_id
                      AND state = 'legacy_cancelled'
                      AND source_roster_revision = current_roster_revision
                      AND target_provider = NEW.provider
                      AND target_external_subscription_id =
                          NEW.external_subscription_id
                      AND target_catalog_version_id = NEW.catalog_version_id
                      AND target_offer_id = NEW.offer_id
                      AND target_plan = NEW.plan
                      AND target_billing_cycle = NEW.billing_cycle
                      AND target_coupon_code IS NOT DISTINCT FROM NEW.coupon_code
                      AND target_amount_cents = NEW.amount_cents;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'provider flip migration was already consumed'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                IF EXISTS (
                    SELECT 1 FROM billing_payment_projections
                    WHERE builder_id = OLD.builder_id
                      AND subscription_id = OLD.id
                      AND canary_slot = 'subscription'
                ) THEN
                    IF TG_OP = 'DELETE' OR ROW(
                        OLD.id, OLD.builder_id, OLD.provider,
                        OLD.external_subscription_id, OLD.plan,
                        OLD.billing_cycle, OLD.coupon_code,
                        OLD.catalog_version_id, OLD.offer_id,
                        OLD.amount_cents, OLD.roster_revision
                    ) IS DISTINCT FROM ROW(
                        NEW.id, NEW.builder_id, NEW.provider,
                        NEW.external_subscription_id, NEW.plan,
                        NEW.billing_cycle, NEW.coupon_code,
                        NEW.catalog_version_id, NEW.offer_id,
                        NEW.amount_cents, NEW.roster_revision
                    ) THEN
                        RAISE EXCEPTION 'canary subscription snapshot is immutable'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF OLD.is_active = false AND NEW.is_active = true THEN
                        RAISE EXCEPTION 'canary subscription cannot reactivate'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF OLD.status IS DISTINCT FROM NEW.status
                       AND NOT (
                           (OLD.status IN ('active', 'active_linked')
                            AND NEW.status IN (
                                'active_linked', 'inactive', 'cancelled',
                                'readback_required',
                                'non_billable_pending_readback'))
                           OR (OLD.status = 'readback_required'
                               AND NEW.status IN (
                                   'active', 'active_linked', 'inactive', 'cancelled',
                                   'non_billable_pending_readback'))
                           OR (OLD.status = 'non_billable_pending_readback'
                               AND NEW.status IN ('inactive', 'cancelled'))
                           OR (OLD.status = 'inactive'
                               AND NEW.status = 'cancelled')
                       ) THEN
                        RAISE EXCEPTION 'canary subscription lifecycle regressed'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_canary_subscription_immutable
            BEFORE UPDATE OR DELETE ON billing_subscriptions
            FOR EACH ROW EXECUTE FUNCTION protect_billing_canary_subscription()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_canary_effect()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE
                projection_environment VARCHAR(20);
                projection_slot VARCHAR(20);
                projection_status VARCHAR(40);
                projection_amount BIGINT;
                projection_settled BIGINT;
                projection_reversed BIGINT;
                projection_restored BIGINT;
                projection_subscription_id UUID;
                projection_topup_order_id UUID;
            BEGIN
                IF TG_OP = 'UPDATE' AND OLD.canary_environment IS NOT NULL THEN
                    RAISE EXCEPTION 'canary settlement effect is immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT canary_environment, canary_slot, status, amount_cents,
                       settled_cents, reversed_cents, restored_cents,
                       subscription_id, topup_order_id
                INTO projection_environment, projection_slot, projection_status,
                     projection_amount, projection_settled,
                     projection_reversed, projection_restored,
                     projection_subscription_id, projection_topup_order_id
                FROM billing_payment_projections
                WHERE builder_id = NEW.builder_id
                  AND id = NEW.projection_id
                  AND provider = NEW.provider
                  AND payment_id = NEW.payment_id;
                IF NEW.effect_kind = 'settlement'
                   AND projection_environment IS NULL
                   AND EXISTS (
                       SELECT 1
                       FROM billing_checkout_sessions AS checkout
                       LEFT JOIN billing_migrations AS migration
                         ON migration.builder_id = checkout.builder_id
                        AND migration.id = checkout.migration_id
                       WHERE checkout.builder_id = NEW.builder_id
                         AND checkout.canary_environment IS NOT NULL
                         AND (
                             (checkout.canary_slot = 'subscription'
                              AND (checkout.intent_id =
                                      projection_subscription_id
                                   OR migration.subscription_id =
                                      projection_subscription_id))
                             OR (checkout.canary_slot = 'topup'
                                 AND checkout.topup_order_id =
                                     projection_topup_order_id)
                         )
                   ) THEN
                    RAISE EXCEPTION 'historical canary settlement lineage is required'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.effect_kind = 'settlement'
                   AND projection_environment IS NOT NULL THEN
                    IF NEW.canary_environment
                           IS DISTINCT FROM projection_environment
                       OR NEW.canary_slot IS DISTINCT FROM projection_slot THEN
                        RAISE EXCEPTION 'canary settlement provenance is required'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF projection_status <> 'settled'
                       OR projection_settled <> projection_amount
                       OR projection_reversed <> 0
                       OR projection_restored <> 0
                       OR NEW.amount_cents <> projection_amount
                       OR NEW.source_revision <> 'settlement:v1' THEN
                        RAISE EXCEPTION 'canary settlement is not fully settled'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                ELSIF NEW.canary_environment IS NOT NULL THEN
                    RAISE EXCEPTION 'canary tags are settlement-only'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_canary_effect_validate
            BEFORE INSERT OR UPDATE ON billing_payment_effects
            FOR EACH ROW EXECUTE FUNCTION validate_billing_canary_effect()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_rollout_gate()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            DECLARE
                capability_is_valid BOOLEAN;
                slot_builder_id UUID;
            BEGIN
                IF TG_OP = 'INSERT' AND (
                    NEW.canary_subscription_checkout_id IS NOT NULL
                    OR NEW.canary_topup_checkout_id IS NOT NULL
                    OR NEW.canary_subscription_effect_id IS NOT NULL
                    OR NEW.canary_topup_effect_id IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'canary slots must be assigned under lock'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF TG_OP = 'UPDATE'
                   AND (
                       OLD.state IS DISTINCT FROM NEW.state
                       OR OLD.rollout_revision IS DISTINCT FROM NEW.rollout_revision
                       OR OLD.canary_builder_id IS DISTINCT FROM NEW.canary_builder_id
                       OR OLD.rollout_receipt_id IS DISTINCT FROM
                           NEW.rollout_receipt_id
                   )
                   AND (
                       OLD.topup_commercial_enabled
                           IS DISTINCT FROM NEW.topup_commercial_enabled
                       OR OLD.topup_revision IS DISTINCT FROM NEW.topup_revision
                       OR OLD.active_topup_capability_id
                           IS DISTINCT FROM NEW.active_topup_capability_id
                       OR OLD.topup_receipt_id IS DISTINCT FROM
                           NEW.topup_receipt_id
                   ) THEN
                    RAISE EXCEPTION 'rollout and topup transitions must be independent'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;

                IF NEW.topup_commercial_enabled THEN
                    SELECT EXISTS (
                        SELECT 1
                        FROM billing_topup_capability_receipts AS receipt
                        WHERE receipt.environment = NEW.environment
                          AND receipt.id = NEW.active_topup_capability_id
                          AND receipt.schema_version = 'credit_capability_ready.v1'
                          AND receipt.approved_at <= clock_timestamp()
                          AND receipt.expires_at > clock_timestamp()
                          AND receipt.reconciliation_complete
                          AND receipt.readback_complete
                          AND NOT receipt.has_open_gap
                          AND NOT receipt.has_backlog
                          AND NOT receipt.has_alert
                    ) INTO capability_is_valid;
                    IF NOT capability_is_valid THEN
                        RAISE EXCEPTION 'valid topup capability is required'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;

                IF TG_OP = 'UPDATE' AND (
                    OLD.canary_subscription_checkout_id IS DISTINCT FROM
                        NEW.canary_subscription_checkout_id
                    OR OLD.canary_topup_checkout_id IS DISTINCT FROM
                        NEW.canary_topup_checkout_id
                ) AND (NEW.state <> 'canary_allowlist'
                       OR NEW.canary_builder_id IS NULL) THEN
                    RAISE EXCEPTION 'canary slots require active allowlist'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;

                IF TG_OP = 'UPDATE' AND
                   OLD.canary_subscription_checkout_id IS DISTINCT FROM
                       NEW.canary_subscription_checkout_id THEN
                    IF OLD.canary_subscription_checkout_id IS NOT NULL
                       OR NEW.canary_subscription_checkout_id IS NULL THEN
                        RAISE EXCEPTION 'subscription checkout slot is lifetime'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    SELECT builder_id INTO slot_builder_id
                    FROM billing_checkout_sessions
                    WHERE id = NEW.canary_subscription_checkout_id
                      AND canary_environment = NEW.environment
                      AND canary_slot = 'subscription';
                    IF slot_builder_id IS DISTINCT FROM NEW.canary_builder_id THEN
                        RAISE EXCEPTION 'subscription checkout slot mismatch'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                IF TG_OP = 'UPDATE' AND
                   OLD.canary_topup_checkout_id IS DISTINCT FROM
                       NEW.canary_topup_checkout_id THEN
                    IF OLD.canary_topup_checkout_id IS NOT NULL
                       OR NEW.canary_topup_checkout_id IS NULL THEN
                        RAISE EXCEPTION 'topup checkout slot is lifetime'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    SELECT builder_id INTO slot_builder_id
                    FROM billing_checkout_sessions
                    WHERE id = NEW.canary_topup_checkout_id
                      AND canary_environment = NEW.environment
                      AND canary_slot = 'topup';
                    IF slot_builder_id IS DISTINCT FROM NEW.canary_builder_id THEN
                        RAISE EXCEPTION 'topup checkout slot mismatch'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                IF TG_OP = 'UPDATE' AND
                   OLD.canary_subscription_effect_id IS DISTINCT FROM
                       NEW.canary_subscription_effect_id THEN
                    IF OLD.canary_subscription_effect_id IS NOT NULL
                       OR NEW.canary_subscription_effect_id IS NULL THEN
                        RAISE EXCEPTION 'subscription settlement slot is lifetime'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    SELECT effect.builder_id INTO slot_builder_id
                    FROM billing_payment_effects AS effect
                    JOIN billing_payment_projections AS projection
                      ON projection.builder_id = effect.builder_id
                     AND projection.id = effect.projection_id
                     AND projection.provider = effect.provider
                     AND projection.payment_id = effect.payment_id
                    WHERE effect.id = NEW.canary_subscription_effect_id
                      AND effect.canary_environment = NEW.environment
                      AND effect.canary_slot = 'subscription'
                      AND projection.checkout_session_id =
                          NEW.canary_subscription_checkout_id;
                    IF slot_builder_id IS NULL THEN
                        RAISE EXCEPTION 'subscription settlement slot mismatch'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                IF TG_OP = 'UPDATE' AND
                   OLD.canary_topup_effect_id IS DISTINCT FROM
                       NEW.canary_topup_effect_id THEN
                    IF OLD.canary_topup_effect_id IS NOT NULL
                       OR NEW.canary_topup_effect_id IS NULL THEN
                        RAISE EXCEPTION 'topup settlement slot is lifetime'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    SELECT effect.builder_id INTO slot_builder_id
                    FROM billing_payment_effects AS effect
                    JOIN billing_payment_projections AS projection
                      ON projection.builder_id = effect.builder_id
                     AND projection.id = effect.projection_id
                     AND projection.provider = effect.provider
                     AND projection.payment_id = effect.payment_id
                    WHERE effect.id = NEW.canary_topup_effect_id
                      AND effect.canary_environment = NEW.environment
                      AND effect.canary_slot = 'topup'
                      AND projection.checkout_session_id =
                          NEW.canary_topup_checkout_id;
                    IF slot_builder_id IS NULL THEN
                        RAISE EXCEPTION 'topup settlement slot mismatch'
                        USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                IF TG_OP = 'UPDATE' AND NEW.state = 'canary_allowlist' THEN
                    PERFORM pg_advisory_xact_lock(hashtextextended(
                        'billing-canary-builder:' || NEW.canary_builder_id::text, 0
                    ));
                    IF EXISTS (
                        SELECT 1 FROM billing_checkout_sessions
                        WHERE builder_id = NEW.canary_builder_id
                          AND state IN (
                              'creating', 'active', 'paid_pending_subscription',
                              'reconciliation_required',
                              'terminal_pending_readback'
                          )
                          AND canary_environment IS NULL
                    ) THEN
                        RAISE EXCEPTION 'untagged live checkout blocks canary opening'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF NEW.canary_subscription_checkout_id IS NOT NULL THEN
                        SELECT builder_id INTO slot_builder_id
                        FROM billing_checkout_sessions
                        WHERE id = NEW.canary_subscription_checkout_id;
                        IF slot_builder_id IS DISTINCT FROM NEW.canary_builder_id THEN
                            RAISE EXCEPTION 'canary reactivation builder mismatch'
                                USING ERRCODE = 'integrity_constraint_violation';
                        END IF;
                    END IF;
                    IF NEW.canary_topup_checkout_id IS NOT NULL THEN
                        SELECT builder_id INTO slot_builder_id
                        FROM billing_checkout_sessions
                        WHERE id = NEW.canary_topup_checkout_id;
                        IF slot_builder_id IS DISTINCT FROM NEW.canary_builder_id THEN
                            RAISE EXCEPTION 'canary reactivation builder mismatch'
                                USING ERRCODE = 'integrity_constraint_violation';
                        END IF;
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_rollout_gate_validate
            BEFORE INSERT OR UPDATE ON billing_rollout_control
            FOR EACH ROW EXECUTE FUNCTION validate_billing_rollout_gate()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_control_receipt()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF OLD.state IS DISTINCT FROM NEW.state
                   OR OLD.rollout_revision IS DISTINCT FROM NEW.rollout_revision
                   OR OLD.canary_builder_id IS DISTINCT FROM NEW.canary_builder_id
                   OR OLD.rollout_receipt_id IS DISTINCT FROM NEW.rollout_receipt_id
                THEN
                    IF NEW.rollout_revision <> OLD.rollout_revision + 1
                       OR NEW.rollout_receipt_id IS NULL
                       OR NEW.rollout_receipt_id IS NOT DISTINCT FROM
                           OLD.rollout_receipt_id
                       OR NOT (
                           (OLD.state = 'disabled'
                            AND NEW.state = 'canary_allowlist')
                           OR (OLD.state = 'canary_allowlist'
                               AND NEW.state = 'general')
                           OR (OLD.state <> 'disabled'
                               AND NEW.state = 'disabled')
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM billing_rollout_receipts
                           WHERE id = NEW.rollout_receipt_id
                             AND environment = NEW.environment
                             AND receipt_kind = 'rollout_transition'
                             AND from_state = OLD.state
                             AND to_state = NEW.state
                             AND expected_revision = OLD.rollout_revision
                             AND resulting_revision = NEW.rollout_revision
                             AND capability_receipt_id IS NULL
                             AND expires_at > clock_timestamp()
                       ) THEN
                        RAISE EXCEPTION 'rollout transition receipt is required'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                IF OLD.topup_commercial_enabled IS DISTINCT FROM
                       NEW.topup_commercial_enabled
                   OR OLD.topup_revision IS DISTINCT FROM NEW.topup_revision
                   OR OLD.active_topup_capability_id IS DISTINCT FROM
                       NEW.active_topup_capability_id
                   OR OLD.topup_receipt_id IS DISTINCT FROM NEW.topup_receipt_id
                THEN
                    IF NEW.topup_revision <> OLD.topup_revision + 1
                       OR NEW.topup_receipt_id IS NULL
                       OR NEW.topup_receipt_id IS NOT DISTINCT FROM
                           OLD.topup_receipt_id
                       OR OLD.topup_commercial_enabled =
                           NEW.topup_commercial_enabled
                       OR NOT EXISTS (
                           SELECT 1 FROM billing_rollout_receipts
                           WHERE id = NEW.topup_receipt_id
                             AND environment = NEW.environment
                             AND receipt_kind = 'topup_gate_transition'
                             AND from_state = CASE
                                 WHEN OLD.topup_commercial_enabled
                                 THEN 'topup_enabled'
                                 ELSE 'topup_disabled'
                             END
                             AND to_state = CASE
                                 WHEN NEW.topup_commercial_enabled
                                 THEN 'topup_enabled'
                                 ELSE 'topup_disabled'
                             END
                             AND expected_revision = OLD.topup_revision
                             AND resulting_revision = NEW.topup_revision
                             AND capability_receipt_id IS NOT DISTINCT FROM
                                 NEW.active_topup_capability_id
                             AND expires_at > clock_timestamp()
                       ) THEN
                        RAISE EXCEPTION 'topup transition receipt is required'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                RETURN NULL;
            END;
            $function$
            """,
            """
            CREATE CONSTRAINT TRIGGER trg_billing_control_receipt
            AFTER UPDATE ON billing_rollout_control
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_billing_control_receipt()
            """,
            """
            CREATE OR REPLACE FUNCTION validate_billing_receipt_control()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF NEW.expires_at <= clock_timestamp() THEN
                    RAISE EXCEPTION 'transition receipt expired before commit'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.receipt_kind = 'rollout_transition' THEN
                    IF NOT EXISTS (
                        SELECT 1 FROM billing_rollout_control
                        WHERE environment = NEW.environment
                          AND state = NEW.to_state
                          AND rollout_revision = NEW.resulting_revision
                          AND rollout_receipt_id = NEW.id
                    ) THEN
                        RAISE EXCEPTION 'rollout receipt has no final control state'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                ELSE
                    IF NOT EXISTS (
                        SELECT 1 FROM billing_rollout_control
                        WHERE environment = NEW.environment
                          AND topup_revision = NEW.resulting_revision
                          AND topup_receipt_id = NEW.id
                          AND topup_commercial_enabled =
                              (NEW.to_state = 'topup_enabled')
                          AND active_topup_capability_id IS NOT DISTINCT FROM
                              NEW.capability_receipt_id
                    ) THEN
                        RAISE EXCEPTION 'topup receipt has no final control state'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    IF NEW.to_state = 'topup_enabled' AND NOT EXISTS (
                        SELECT 1
                        FROM billing_topup_capability_receipts AS capability
                        WHERE capability.environment = NEW.environment
                          AND capability.id = NEW.capability_receipt_id
                          AND capability.schema_version =
                              'credit_capability_ready.v1'
                          AND capability.approved_at <= clock_timestamp()
                          AND capability.expires_at > clock_timestamp()
                          AND capability.reconciliation_complete
                          AND capability.readback_complete
                          AND NOT capability.has_open_gap
                          AND NOT capability.has_backlog
                          AND NOT capability.has_alert
                    ) THEN
                        RAISE EXCEPTION 'topup capability is invalid at commit'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                END IF;
                RETURN NULL;
            END;
            $function$
            """,
            """
            CREATE CONSTRAINT TRIGGER trg_billing_receipt_control
            AFTER INSERT ON billing_rollout_receipts
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_billing_receipt_control()
            """,
            """
            CREATE OR REPLACE FUNCTION protect_billing_financial_history()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                RAISE EXCEPTION 'billing financial history is not deletable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END;
            $function$
            """,
            """
            CREATE OR REPLACE FUNCTION protect_billing_rollout_control()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'billing financial history is not deletable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.rollout_revision < OLD.rollout_revision
                   OR NEW.topup_revision < OLD.topup_revision THEN
                    RAISE EXCEPTION 'billing control revision cannot reset'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE OR REPLACE FUNCTION protect_billing_roster_control()
            RETURNS trigger LANGUAGE plpgsql AS $function$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'billing financial history is not deletable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.billing_roster_revision < OLD.billing_roster_revision THEN
                    RAISE EXCEPTION 'billing roster revision cannot reset'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.billing_roster_revision IS DISTINCT FROM
                       OLD.billing_roster_revision
                   AND pg_trigger_depth() = 1
                   AND NOT (
                       OLD.billing_roster_revision = 0
                       AND NEW.billing_roster_revision = 1
                   ) THEN
                    RAISE EXCEPTION 'billing roster revision is saga-owned'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $function$
            """,
            """
            CREATE TRIGGER trg_billing_rollout_control_history
            BEFORE UPDATE OR DELETE ON billing_rollout_control
            FOR EACH ROW EXECUTE FUNCTION protect_billing_rollout_control()
            """,
            """
            CREATE TRIGGER trg_billing_roster_control_history
            BEFORE UPDATE OR DELETE ON billing_roster_control
            FOR EACH ROW EXECUTE FUNCTION protect_billing_roster_control()
            """,
            """
            CREATE TRIGGER trg_billing_canary_checkout_delete
            BEFORE DELETE ON billing_checkout_sessions
            FOR EACH ROW WHEN (OLD.canary_environment IS NOT NULL)
            EXECUTE FUNCTION protect_billing_financial_history()
            """,
            """
            CREATE TRIGGER trg_billing_canary_projection_delete
            BEFORE DELETE ON billing_payment_projections
            FOR EACH ROW WHEN (OLD.canary_environment IS NOT NULL)
            EXECUTE FUNCTION protect_billing_financial_history()
            """,
            """
            CREATE TRIGGER trg_billing_canary_effect_delete
            BEFORE DELETE ON billing_payment_effects
            FOR EACH ROW WHEN (OLD.canary_environment IS NOT NULL)
            EXECUTE FUNCTION protect_billing_financial_history()
            """,
            """
            INSERT INTO billing_rollout_control (
                environment, state, rollout_revision,
                topup_commercial_enabled, topup_revision
            )
            SELECT environment, 'disabled', 0, false, 0
            FROM unnest(ARRAY[
                'development', 'staging', 'production', 'test'
            ]) AS environment
            """,
            """
            INSERT INTO billing_roster_control (
                singleton, billing_roster_revision
            )
            VALUES (true, 0)
            """,
        )
    )

    op.create_index(
        "ix_billing_subscriptions_builder_id",
        "billing_subscriptions",
        ["builder_id"],
    )
    op.create_index(
        "ix_billing_migrations_builder_id", "billing_migrations", ["builder_id"]
    )
    op.create_index(
        "ix_billing_topup_orders_builder_id",
        "billing_topup_orders",
        ["builder_id"],
    )
    op.create_index(
        "ix_billing_checkout_sessions_builder_id",
        "billing_checkout_sessions",
        ["builder_id"],
    )
    op.create_index(
        "ix_billing_migration_receipts_builder_id",
        "billing_migration_receipts",
        ["builder_id"],
    )
    op.create_index(
        "ix_billing_webhook_deliveries_builder_id",
        "billing_webhook_deliveries",
        ["builder_id"],
    )
    op.create_index(
        "ix_billing_payment_projections_builder_id",
        "billing_payment_projections",
        ["builder_id"],
    )
    op.create_index(
        "ix_billing_payment_effects_builder_id",
        "billing_payment_effects",
        ["builder_id"],
    )
    op.create_index("ix_billing_outbox_builder_id", "billing_outbox", ["builder_id"])


def downgrade() -> None:
    _execute(
        (
            "DROP TRIGGER IF EXISTS trg_billing_receipt_control "
            "ON billing_rollout_receipts",
            "DROP FUNCTION IF EXISTS validate_billing_receipt_control()",
            "DROP TRIGGER IF EXISTS trg_billing_control_receipt "
            "ON billing_rollout_control",
            "DROP FUNCTION IF EXISTS validate_billing_control_receipt()",
            "DROP TRIGGER IF EXISTS trg_billing_canary_effect_delete "
            "ON billing_payment_effects",
            "DROP TRIGGER IF EXISTS trg_billing_canary_projection_delete "
            "ON billing_payment_projections",
            "DROP TRIGGER IF EXISTS trg_billing_canary_checkout_delete "
            "ON billing_checkout_sessions",
            "DROP TRIGGER IF EXISTS trg_billing_roster_control_history "
            "ON billing_roster_control",
            "DROP TRIGGER IF EXISTS trg_billing_rollout_control_history "
            "ON billing_rollout_control",
            "DROP FUNCTION IF EXISTS protect_billing_roster_control()",
            "DROP FUNCTION IF EXISTS protect_billing_rollout_control()",
            "DROP FUNCTION IF EXISTS protect_billing_financial_history()",
            "DROP TRIGGER IF EXISTS trg_billing_rollout_gate_validate "
            "ON billing_rollout_control",
            "DROP FUNCTION IF EXISTS validate_billing_rollout_gate()",
            "DROP TRIGGER IF EXISTS trg_billing_canary_effect_validate "
            "ON billing_payment_effects",
            "DROP FUNCTION IF EXISTS validate_billing_canary_effect()",
            "DROP TRIGGER IF EXISTS trg_billing_canary_projection_validate "
            "ON billing_payment_projections",
            "DROP FUNCTION IF EXISTS validate_billing_canary_projection()",
            "DROP TRIGGER IF EXISTS trg_billing_canary_subscription_immutable "
            "ON billing_subscriptions",
            "DROP FUNCTION IF EXISTS protect_billing_canary_subscription()",
            "DROP TRIGGER IF EXISTS trg_billing_canary_checkout_validate "
            "ON billing_checkout_sessions",
            "DROP TRIGGER IF EXISTS trg_billing_canary_checkout_slot "
            "ON billing_checkout_sessions",
            "DROP FUNCTION IF EXISTS validate_billing_canary_checkout_slot()",
            "DROP FUNCTION IF EXISTS validate_billing_canary_checkout()",
            "DROP TRIGGER IF EXISTS trg_billing_topup_capability_lifetime "
            "ON billing_topup_capability_receipts",
            "DROP FUNCTION IF EXISTS validate_billing_capability_lifetime()",
            "DROP TRIGGER IF EXISTS trg_billing_outbox_immutable ON billing_outbox",
            "DROP TRIGGER IF EXISTS trg_billing_topup_capability_receipts_immutable "
            "ON billing_topup_capability_receipts",
            "DROP TRIGGER IF EXISTS trg_billing_rollout_receipts_immutable "
            "ON billing_rollout_receipts",
            "DROP TRIGGER IF EXISTS trg_billing_migration_receipts_immutable "
            "ON billing_migration_receipts",
            "DROP TRIGGER IF EXISTS trg_billing_migration_receipt_commit "
            "ON billing_migration_receipts",
            "DROP FUNCTION IF EXISTS validate_billing_migration_receipt_commit()",
            "DROP TRIGGER IF EXISTS trg_billing_migration_receipt_validate "
            "ON billing_migration_receipts",
            "DROP FUNCTION IF EXISTS validate_billing_migration_receipt()",
            "DROP TRIGGER IF EXISTS trg_billing_migration_receipts_roster_fence "
            "ON billing_migration_receipts",
            "DROP TRIGGER IF EXISTS trg_billing_migrations_roster_fence "
            "ON billing_migrations",
            "DROP TRIGGER IF EXISTS trg_billing_subscriptions_roster_fence "
            "ON billing_subscriptions",
            "DROP TRIGGER IF EXISTS trg_billing_roster_control_fence "
            "ON billing_roster_control",
            "DROP FUNCTION IF EXISTS lock_billing_roster_statement()",
            "DROP TRIGGER IF EXISTS trg_billing_migration_target_immutable "
            "ON billing_migrations",
            "DROP FUNCTION IF EXISTS protect_bound_billing_migration_target()",
            "DROP FUNCTION IF EXISTS reject_immutable_billing_row()",
            "DROP TABLE billing_outbox",
            """
            ALTER TABLE billing_rollout_control
                DROP CONSTRAINT fk_billing_rollout_control_topup_receipt,
                DROP CONSTRAINT fk_billing_rollout_control_rollout_receipt,
                DROP CONSTRAINT fk_billing_rollout_topup_effect,
                DROP CONSTRAINT fk_billing_rollout_subscription_effect,
                DROP CONSTRAINT fk_billing_rollout_topup_checkout,
                DROP CONSTRAINT fk_billing_rollout_subscription_checkout
            """,
            """
            ALTER TABLE transactions
                DROP CONSTRAINT uq_transactions_billing_payment_effect,
                DROP CONSTRAINT uq_transactions_provider_payment,
                DROP CONSTRAINT fk_transactions_billing_effect_identity,
                DROP CONSTRAINT ck_transactions_billing_effect_identity,
                DROP CONSTRAINT ck_transactions_provider_payment_pair,
                DROP COLUMN billing_payment_effect_id,
                DROP COLUMN provider_payment_id,
                DROP COLUMN provider
            """,
            "DROP TABLE billing_payment_effects",
            "DROP TABLE billing_payment_projections",
            "DROP TABLE billing_webhook_deliveries",
            "DROP TABLE billing_migration_receipts",
            "DROP TABLE billing_checkout_sessions",
            "DROP TABLE billing_topup_orders",
            "DROP TABLE billing_migrations",
            "DROP TABLE billing_subscriptions",
            "DROP TABLE billing_rollout_receipts",
            "DROP TABLE billing_roster_control",
            "DROP TABLE billing_rollout_control",
            "DROP TABLE billing_topup_capability_receipts",
            "DROP FUNCTION IF EXISTS billing_snapshot_has_no_pii(JSONB)",
            "DROP FUNCTION IF EXISTS billing_migration_snapshot_is_valid("
            "JSONB, TEXT, TEXT, TEXT, TEXT, TEXT, BIGINT)",
        )
    )
