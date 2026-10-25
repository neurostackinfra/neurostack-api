-- SELECT-only post-deploy rollout readback ("Rollout fechado", asaas-payment-gateway).
--
-- scripts/asaas_rollout_readback.py (render) runs it on the read family
-- (neurostack-staging-consulta, application role) inside a REPEATABLE READ,
-- READ ONLY transaction of a session with default_transaction_read_only=on;
-- the transaction is never committed.  The same task reads alembic_version,
-- which must be 037.  One row per fact, three text columns; verify expects
-- exactly this table of 25 rows, no row missing and none added (judged by
-- check_name and subject: the order below is for reading only):
--   rollout state                      each of the four environments   disabled
--   rollout topup_commercial_enabled   each of the four environments   false
--   legacy table                       each table migration 037 drops  absent
--   legacy function                    each function 037 drops         absent
--   billing_subscriptions rows         (empty)                         0
--   billing_checkout_sessions rows     (empty)                         0
--   row level security on read tables  (empty)                         0
-- The last row counts the three read tables with row-level security enabled
-- or forced: with RLS the reading role could see fewer rows than exist.
-- It shows the state at the moment of the read, not its history.
-- Every name is schema-qualified; to_regclass and to_regprocedure only look
-- a name up.  tests/scripts/test_asaas_rollout_readback.py keeps the legacy
-- names equal to migration 037's DROPPED_TABLES and DROPPED_FUNCTIONS.
SELECT check_name, subject, value
FROM (
    SELECT 1 AS part, 'rollout state' AS check_name, environment::text AS subject, state::text AS value
    FROM public.billing_rollout_control
    UNION ALL
    SELECT 2, 'rollout topup_commercial_enabled', environment::text, topup_commercial_enabled::text
    FROM public.billing_rollout_control
    UNION ALL
    SELECT 3, 'legacy table', relation, CASE WHEN to_regclass(relation) IS NULL THEN 'absent' ELSE 'present' END
    FROM unnest(ARRAY['public.billing_migration_absence_reads', 'public.billing_migration_receipts',
                      'public.billing_migrations', 'public.billing_roster_control']::text[])
         AS legacy_table(relation)
    UNION ALL
    SELECT 4, 'legacy function', signature, CASE WHEN to_regprocedure(signature) IS NULL THEN 'absent' ELSE 'present' END
    FROM unnest(ARRAY['public.billing_migration_snapshot_is_valid(jsonb,text,text,text,text,text,bigint)',
                      'public.lock_billing_roster_statement()',
                      'public.validate_billing_migration_receipt()',
                      'public.validate_billing_migration_receipt_commit()',
                      'public.protect_bound_billing_migration_target()',
                      'public.protect_billing_roster_control()',
                      'public.validate_billing_provider_evidence()',
                      'public.billing_evidence_timestamp(timestamp with time zone)',
                      'public.billing_provider_evidence_digest(text,text,timestamp with time zone,text,uuid,timestamp with time zone,text,uuid,text,text)',
                      'public.validate_billing_migration_absence_read()']::text[])
         AS legacy_function(signature)
    UNION ALL
    SELECT 5, 'billing_subscriptions rows', '', count(*)::text
    FROM public.billing_subscriptions
    UNION ALL
    SELECT 6, 'billing_checkout_sessions rows', '', count(*)::text
    FROM public.billing_checkout_sessions
    UNION ALL
    SELECT 7, 'row level security on read tables', '', count(*)::text
    FROM pg_catalog.pg_class
    WHERE (relrowsecurity OR relforcerowsecurity)
      AND oid IN ('public.billing_rollout_control'::regclass, 'public.billing_subscriptions'::regclass,
                  'public.billing_checkout_sessions'::regclass)
) AS facts
ORDER BY part, subject COLLATE "C";
