-- SELECT-only readback before the 033-035 migration window (window 035).
--
-- scripts/asaas_migration_window.py (render-pre-read --window 035) runs it in
-- a read task, inside a REPEATABLE READ, READ ONLY transaction that is always
-- rolled back, right before the window; the same task reads alembic_version,
-- which must be 032.  Every count below must be 0: they are the counts the
-- SELECT-only read of 2026-10-17 found at zero (Group 10 phase 0 evidence in
-- the asaas-payment-gateway wish).  The last row counts the counted tables
-- with row-level security enabled or forced (that evidence found none): with
-- RLS a count could hide rows from the reading role, so it must be 0 too.
-- Any other value stops the window: return to the owner.  Every name is
-- schema-qualified so no other schema on the search path can shadow it.
-- docs/runbooks/asaas-deployment.md (gate 5) shows this query;
-- tests/scripts/test_asaas_migration_window.py keeps the two equal.
SELECT 'transactions' AS check_name, count(*) AS found FROM public.transactions
UNION ALL SELECT 'billing_subscriptions', count(*) FROM public.billing_subscriptions
UNION ALL SELECT 'billing_checkout_sessions', count(*) FROM public.billing_checkout_sessions
UNION ALL SELECT 'billing_webhook_deliveries', count(*) FROM public.billing_webhook_deliveries
UNION ALL SELECT 'billing_migrations', count(*) FROM public.billing_migrations
UNION ALL SELECT 'billing_migration_receipts', count(*) FROM public.billing_migration_receipts
UNION ALL SELECT 'billing_migration_absence_reads', count(*) FROM public.billing_migration_absence_reads
UNION ALL SELECT 'roster revision not 0', count(*) FROM public.billing_roster_control WHERE billing_roster_revision <> 0
UNION ALL SELECT 'rollout not disabled', count(*) FROM public.billing_rollout_control WHERE state <> 'disabled' OR topup_commercial_enabled
UNION ALL SELECT 'row level security on counted tables', count(*) FROM pg_catalog.pg_class WHERE (relrowsecurity OR relforcerowsecurity) AND oid IN ('public.transactions'::regclass, 'public.billing_subscriptions'::regclass, 'public.billing_checkout_sessions'::regclass, 'public.billing_webhook_deliveries'::regclass, 'public.billing_migrations'::regclass, 'public.billing_migration_receipts'::regclass, 'public.billing_migration_absence_reads'::regclass, 'public.billing_roster_control'::regclass, 'public.billing_rollout_control'::regclass);
