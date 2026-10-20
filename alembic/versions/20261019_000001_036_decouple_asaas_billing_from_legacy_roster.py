"""decouple ASAAS billing from the legacy roster and migration saga

Revision ID: 036
Revises: 035
Create Date: 2026-10-19 00:00:01

Group 10 phase E5 ("desacoplar"; owner decisions of 2026-10-17 and
2026-10-18 in the asaas-payment-gateway wish).  Only the decoupling:

* drop the roster statement trigger ``trg_billing_subscriptions_roster_fence``
  (``lock_billing_roster_statement()`` stays for the legacy tables' own
  triggers until 037 drops them);
* rewrite the four canary trigger functions so none reads ``billing_migrations``,
  the roster or the dropped columns;
* drop ``billing_subscriptions.roster_revision``,
  ``billing_rollout_readiness_receipts.roster_revision`` and
  ``billing_checkout_sessions.migration_id`` with every constraint on them, and
  re-add the two checkout CHECKs (``ck_billing_checkout_sessions_intent_target``
  and ``ck_billing_checkout_sessions_canary_exact``) without ``migration_id``.

Not included (owner: "sem os endurecimentos opcionais"): provider NOT NULL,
tighter status/cycle/provider CHECKs (``transactions`` included) and a unique
open subscription per builder.  Non-ASAAS subscription rows (``abacatepay`` or
NULL provider) stay representable; the rewritten guard keeps their snapshot
immutable and still refuses a self-asserted legacy cancellation.

Preflight (upgrade): refuses, changing nothing, while any legacy row or any
unexpired rollout readiness receipt exists; see ``_UPGRADE_PREFLIGHT``.

Lock wait (both directions): the first statement is ``LOCK_TIMEOUT``
(``SET LOCAL lock_timeout = '5s'``, the value of migrations 024 and 025).  The
preflight's ``LOCK TABLE ... IN ACCESS EXCLUSIVE MODE`` waits behind any open
transaction that touched one of those tables, and while it waits every new
read of that table queues behind it.  Five seconds bounds that stall for the
application and is long enough for any ordinary billing statement, which the
window (rollout ``disabled``) keeps rare anyway; a longer wait means a stuck
transaction, which the operator must see rather than queue behind.  A timeout
raises SQLSTATE 55P03 inside the migration transaction, so nothing changed:
wait for the blocking transaction to end and run the migration again.

Downgrade (real, owner decision): restores the 035 columns, constraints,
functions and trigger from ``alembic/frozen/036_downgrade_restores_035.json``,
which ``scripts/alembic_frozen_ddl.py`` extracted from a fresh 035 database and
whose SHA-256 is pinned below.  It refuses while any ``billing_subscriptions``
or ``billing_rollout_readiness_receipts`` row exists: restoring their NOT NULL
``roster_revision`` would fabricate roster evidence.  Restored columns are
appended, so their ordinal position differs from a fresh 035 database.
"""

import hashlib
import json
from pathlib import Path
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels = None
depends_on = None

FROZEN_035 = (
    Path(__file__).resolve().parents[1] / "frozen" / "036_downgrade_restores_035.json"
)
FROZEN_035_SHA256 = "0999933fa0784cb4c8cfb4aa8c4fbbab82651beddcfc4c546aa3c1f2e3cf5aa3"
# First statement of both directions; see "Lock wait" in the module docstring.
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"

# Columns whose 035 values would be lost (or would have to be invented).
_DROPPED_COLUMNS = (
    "billing_checkout_sessions.migration_id",
    "billing_rollout_readiness_receipts.roster_revision",
    "billing_subscriptions.roster_revision",
)
# Exactly the catalog objects that depend on those columns at 035.
_DROPPED_CONSTRAINTS = (
    "ck_billing_checkout_sessions_canary_exact",
    "ck_billing_checkout_sessions_intent_target",
    "ck_billing_rollout_readiness_roster_revision",
    "ck_billing_subscriptions_roster_revision",
    "fk_billing_checkout_sessions_builder_migration",
)


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            bind_names = ", ".join(sorted(ddl._bindparams))
            raise ValueError(
                f"raw migration DDL contains bind parameters: {bind_names}"
            )
        op.execute(ddl)


def _sql_list(values: tuple[str, ...]) -> str:
    return "ARRAY[" + ", ".join(f"'{value}'" for value in values) + "]::text[]"


# A legacy row is a row of the migration saga, a row whose roster/migration
# column carries anything but the constant non-legacy code writes, or a
# non-ASAAS subscription:
#   * any row of billing_migrations, billing_migration_receipts or
#     billing_migration_absence_reads;
#   * billing_roster_control with billing_roster_revision <> 0;
#   * billing_subscriptions with provider IS DISTINCT FROM 'asaas'
#     (abacatepay or NULL), or with roster_revision <> 1 (ASAAS rows were
#     always written with max(revision, 1) = 1, the constant since E4);
#   * billing_checkout_sessions with migration_id IS NOT NULL;
#   * billing_rollout_readiness_receipts with roster_revision <> 0 (the
#     constant since E3; the roster revision is 0 when nothing above exists).
# Also refused: any unexpired readiness receipt (its content digest lost
# roster_revision in E3), and any catalog object other than the five listed
# constraints that depends on a dropped column (DROP COLUMN would drop it
# silently and the downgrade could not restore it).  ``transactions`` and
# ``billing_webhook_deliveries`` are not read: 036 does not touch them.
_UPGRADE_PREFLIGHT = f"""
DO $preflight$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
    found BIGINT;
    dependents TEXT;
BEGIN
    LOCK TABLE billing_subscriptions, billing_checkout_sessions,
        billing_rollout_readiness_receipts, billing_migrations,
        billing_migration_receipts, billing_migration_absence_reads,
        billing_roster_control
        IN ACCESS EXCLUSIVE MODE;
    SELECT count(*) INTO found FROM billing_migrations;
    IF found > 0 THEN
        problems := problems || format('billing_migrations has %s row(s)', found);
    END IF;
    SELECT count(*) INTO found FROM billing_migration_receipts;
    IF found > 0 THEN
        problems := problems
            || format('billing_migration_receipts has %s row(s)', found);
    END IF;
    SELECT count(*) INTO found FROM billing_migration_absence_reads;
    IF found > 0 THEN
        problems := problems
            || format('billing_migration_absence_reads has %s row(s)', found);
    END IF;
    SELECT count(*) INTO found FROM billing_roster_control
    WHERE billing_roster_revision <> 0;
    IF found > 0 THEN
        problems := problems || format(
            'billing_roster_control has %s row(s) whose revision is not 0', found);
    END IF;
    SELECT count(*) INTO found FROM billing_subscriptions
    WHERE provider IS DISTINCT FROM 'asaas';
    IF found > 0 THEN
        problems := problems || format(
            'billing_subscriptions has %s row(s) whose provider is not asaas',
            found);
    END IF;
    SELECT count(*) INTO found FROM billing_subscriptions
    WHERE roster_revision <> 1;
    IF found > 0 THEN
        problems := problems || format(
            'billing_subscriptions has %s row(s) whose roster_revision is not 1',
            found);
    END IF;
    SELECT count(*) INTO found FROM billing_checkout_sessions
    WHERE migration_id IS NOT NULL;
    IF found > 0 THEN
        problems := problems || format(
            'billing_checkout_sessions has %s row(s) with a migration_id', found);
    END IF;
    SELECT count(*) INTO found FROM billing_rollout_readiness_receipts
    WHERE roster_revision <> 0;
    IF found > 0 THEN
        problems := problems || format(
            'billing_rollout_readiness_receipts has %s row(s) whose '
            'roster_revision is not 0', found);
    END IF;
    SELECT count(*) INTO found FROM billing_rollout_readiness_receipts
    WHERE expires_at > now();
    IF found > 0 THEN
        problems := problems || format(
            'billing_rollout_readiness_receipts has %s unexpired row(s)', found);
    END IF;
    SELECT string_agg(DISTINCT dependent, ', ' ORDER BY dependent)
    INTO dependents
    FROM (
        SELECT pg_describe_object(dep.classid, dep.objid, dep.objsubid)
            AS dependent
        FROM pg_depend AS dep
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = dep.refobjid
         AND attribute.attnum = dep.refobjsubid
        WHERE dep.refclassid = 'pg_class'::regclass
          AND dep.refobjsubid > 0
          AND dep.deptype IN ('n', 'a')
          AND dep.refobjid::regclass::text || '.' || attribute.attname
              = ANY({_sql_list(_DROPPED_COLUMNS)})
          AND NOT (
              dep.classid = 'pg_constraint'::regclass
              AND (SELECT conname FROM pg_constraint WHERE oid = dep.objid)
                  = ANY({_sql_list(_DROPPED_CONSTRAINTS)})
          )
    ) AS unexpected;
    IF dependents IS NOT NULL THEN
        problems := problems || format(
            'unexpected objects depend on the dropped columns: %s', dependents);
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'migration 036 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""

_PROTECT_SUBSCRIPTION = """
CREATE OR REPLACE FUNCTION protect_billing_canary_subscription()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.provider = 'asaas'
       AND ROW(
           OLD.provider, OLD.external_subscription_id, OLD.plan,
           OLD.billing_cycle, OLD.coupon_code,
           OLD.catalog_version_id, OLD.offer_id, OLD.amount_cents
       ) IS DISTINCT FROM ROW(
           NEW.provider, NEW.external_subscription_id, NEW.plan,
           NEW.billing_cycle, NEW.coupon_code,
           NEW.catalog_version_id, NEW.offer_id, NEW.amount_cents
       ) THEN
        RAISE EXCEPTION 'provider subscription snapshot is write-once'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- Non-ASAAS rows (abacatepay or NULL provider) stay representable, but
    -- the legacy saga that could cancel, rebase or flip them is gone (036):
    -- a self-asserted legacy cancellation is still refused and their
    -- snapshot, provider included, can no longer change.
    IF TG_OP = 'UPDATE'
       AND OLD.provider IS NULL
       AND OLD.status IS DISTINCT FROM NEW.status
       AND NEW.status = 'legacy_cancelled'
    THEN
        RAISE EXCEPTION 'legacy cancellation cannot be self-asserted';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.provider IS DISTINCT FROM 'asaas'
       AND ROW(
           OLD.provider, OLD.external_subscription_id, OLD.plan,
           OLD.billing_cycle, OLD.coupon_code,
           OLD.catalog_version_id, OLD.offer_id, OLD.amount_cents
       ) IS DISTINCT FROM ROW(
           NEW.provider, NEW.external_subscription_id, NEW.plan,
           NEW.billing_cycle, NEW.coupon_code,
           NEW.catalog_version_id, NEW.offer_id, NEW.amount_cents
       ) THEN
        RAISE EXCEPTION 'legacy subscription snapshot is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
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
            OLD.catalog_version_id, OLD.offer_id, OLD.amount_cents
        ) IS DISTINCT FROM ROW(
            NEW.id, NEW.builder_id, NEW.provider,
            NEW.external_subscription_id, NEW.plan,
            NEW.billing_cycle, NEW.coupon_code,
            NEW.catalog_version_id, NEW.offer_id, NEW.amount_cents
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
"""

_VALIDATE_CHECKOUT = """
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
            OLD.topup_order_id, OLD.attempt_no,
            OLD.provider, OLD.catalog_version_id, OLD.offer_id,
            OLD.amount_cents, OLD.pricing_snapshot, OLD.next_due_date,
            OLD.canary_environment, OLD.canary_slot
       ) IS DISTINCT FROM ROW(
            NEW.id, NEW.builder_id, NEW.intent_kind, NEW.intent_id,
            NEW.topup_order_id, NEW.attempt_no,
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
"""

_VALIDATE_PROJECTION = """
CREATE OR REPLACE FUNCTION validate_billing_canary_projection()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    checkout_environment VARCHAR(20);
    checkout_slot VARCHAR(20);
    checkout_topup_order_id UUID;
    checkout_intent_id UUID;
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
               intent_id, external_checkout_id,
               resulting_external_subscription_id
        INTO checkout_environment, checkout_slot,
             checkout_topup_order_id, checkout_intent_id,
             checkout_external_checkout_id,
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
        WHERE checkout.builder_id = NEW.builder_id
          AND checkout.canary_slot = 'subscription'
          AND checkout.intent_id = NEW.subscription_id
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
           AND NEW.subscription_id IS DISTINCT FROM checkout_intent_id THEN
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
"""

_VALIDATE_EFFECT = """
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
           WHERE checkout.builder_id = NEW.builder_id
             AND checkout.canary_environment IS NOT NULL
             AND (
                 (checkout.canary_slot = 'subscription'
                  AND checkout.intent_id = projection_subscription_id)
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
"""


def _canary_subscription_snapshot(grant: int) -> str:
    return (
        "pricing_snapshot = jsonb_build_object("
        "'catalog_version_id', 'payment-2026-10-03', "
        "'offer_id', 'plan_plus_monthly_v1', 'amount_cents', 99900, "
        f"'credit_grant_mc', {grant}, 'included_clinics', 1, "
        "'billing_cycle', 'monthly', 'payment_method', 'credit_card', "
        "'lines', jsonb_build_array(jsonb_build_object("
        "'line_id', 'plan', 'offer_id', 'plan_plus_monthly_v1', "
        "'kind', 'plan', 'quantity', 1, 'unit_amount_cents', 99900, "
        f"'line_amount_cents', 99900, 'credit_grant_mc', {grant}, "
        "'settled_cents', 0, 'reversed_cents', 0, 'restored_cents', 0, "
        "'line_financial_state', 'pending')))"
    )


# 035's CHECK (both credit-grant snapshots stay valid) minus the two
# ``migration_id IS NULL`` terms.
CANARY_EXACT_CHECK = (
    "canary_slot IS NULL OR (provider = 'asaas' AND attempt_no = 1 AND "
    "catalog_version_id = 'payment-2026-10-03' AND "
    "((canary_slot = 'subscription' AND intent_kind = 'subscription' "
    "AND topup_order_id IS NULL "
    "AND next_due_date IS NOT NULL AND offer_id = 'plan_plus_monthly_v1' "
    "AND amount_cents = 99900 AND "
    "pricing_snapshot ->> 'catalog_version_id' = 'payment-2026-10-03' AND "
    "pricing_snapshot ->> 'offer_id' = 'plan_plus_monthly_v1' AND "
    "pricing_snapshot ->> 'amount_cents' = '99900' AND "
    "pricing_snapshot ->> 'payment_method' = 'credit_card' AND "
    "pricing_snapshot ->> 'billing_cycle' = 'monthly' AND "
    f"({_canary_subscription_snapshot(5_000_000)} OR "
    f"{_canary_subscription_snapshot(5_000)})) OR "
    "(canary_slot = 'topup' AND intent_kind = 'topup' "
    "AND topup_order_id IS NOT NULL "
    "AND intent_id = topup_order_id "
    "AND offer_id = 'package_credits_500_v1' AND amount_cents = 10000 "
    "AND pricing_snapshot ->> 'catalog_version_id' = 'payment-2026-10-03' "
    "AND pricing_snapshot ->> 'offer_id' = 'package_credits_500_v1' "
    "AND pricing_snapshot ->> 'amount_cents' = '10000' "
    "AND pricing_snapshot ->> 'credit_grant_mc' = '500000')))"
)
INTENT_TARGET_CHECK = (
    "(intent_kind = 'subscription' AND topup_order_id IS NULL) OR "
    "(intent_kind = 'topup' AND topup_order_id IS NOT NULL "
    "AND intent_id = topup_order_id)"
)

_UPGRADE = (
    _PROTECT_SUBSCRIPTION,
    _VALIDATE_CHECKOUT,
    _VALIDATE_PROJECTION,
    _VALIDATE_EFFECT,
    "DROP TRIGGER trg_billing_subscriptions_roster_fence ON billing_subscriptions",
    f"""
    ALTER TABLE billing_checkout_sessions
        DROP CONSTRAINT ck_billing_checkout_sessions_canary_exact,
        DROP CONSTRAINT ck_billing_checkout_sessions_intent_target,
        DROP CONSTRAINT fk_billing_checkout_sessions_builder_migration,
        DROP COLUMN migration_id,
        ADD CONSTRAINT ck_billing_checkout_sessions_intent_target
            CHECK ({INTENT_TARGET_CHECK}),
        ADD CONSTRAINT ck_billing_checkout_sessions_canary_exact
            CHECK ({CANARY_EXACT_CHECK})
    """,
    """
    ALTER TABLE billing_subscriptions
        DROP CONSTRAINT ck_billing_subscriptions_roster_revision,
        DROP COLUMN roster_revision
    """,
    """
    ALTER TABLE billing_rollout_readiness_receipts
        DROP CONSTRAINT ck_billing_rollout_readiness_roster_revision,
        DROP COLUMN roster_revision
    """,
)

_DOWNGRADE_PREFLIGHT = """
DO $preflight$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
    found BIGINT;
BEGIN
    LOCK TABLE billing_subscriptions, billing_checkout_sessions,
        billing_rollout_readiness_receipts
        IN ACCESS EXCLUSIVE MODE;
    SELECT count(*) INTO found FROM billing_subscriptions;
    IF found > 0 THEN
        problems := problems || format(
            'billing_subscriptions has %s row(s): restoring their '
            'roster_revision would fabricate roster evidence', found);
    END IF;
    SELECT count(*) INTO found FROM billing_rollout_readiness_receipts;
    IF found > 0 THEN
        problems := problems || format(
            'billing_rollout_readiness_receipts has %s row(s): restoring their '
            'roster_revision would fabricate roster evidence', found);
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'downgrade of migration 036 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""

_DOWNGRADE_DROP_036 = (
    """
    ALTER TABLE billing_checkout_sessions
        DROP CONSTRAINT ck_billing_checkout_sessions_canary_exact,
        DROP CONSTRAINT ck_billing_checkout_sessions_intent_target
    """,
)


def frozen_035_statements(raw: bytes | None = None) -> tuple[str, ...]:
    """The frozen 035 DDL, in restore order, after the pinned digest check."""

    content = FROZEN_035.read_bytes() if raw is None else raw
    digest = hashlib.sha256(content).hexdigest()
    if digest != FROZEN_035_SHA256:
        raise RuntimeError(
            f"{FROZEN_035.name} has SHA-256 {digest}, migration 036 pins "
            f"{FROZEN_035_SHA256}; the frozen 035 DDL was changed"
        )
    document = json.loads(content)
    if document.get("revision") != "035":
        raise RuntimeError(f"{FROZEN_035.name} does not freeze revision 035")
    return tuple(
        entry["statement"]
        for kind in ("columns", "constraints", "functions", "triggers")
        for entry in document[kind]
    )


def upgrade() -> None:
    _execute((LOCK_TIMEOUT, _UPGRADE_PREFLIGHT, *_UPGRADE))


def downgrade() -> None:
    restore = frozen_035_statements()
    _execute((LOCK_TIMEOUT, _DOWNGRADE_PREFLIGHT, *_DOWNGRADE_DROP_036, *restore))
