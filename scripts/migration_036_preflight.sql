-- SELECT-only preflight before migration 036 (window 036).
--
-- The query of docs/runbooks/asaas-deployment.md, "Migration 036", section
-- "Preflight (SELECT only, immediately before the run)", byte for byte below
-- these comment lines (tests/scripts/test_asaas_migration_window.py keeps the
-- two equal).  scripts/asaas_migration_window.py (render-pre-read --window
-- 036) runs it in a read task, inside a REPEATABLE READ, READ ONLY
-- transaction that is always rolled back; the same task reads
-- alembic_version, which must be 035.  Every count must be 0, or stop and
-- return to the owner.  The last row counts the counted tables with
-- row-level security enabled or forced (a count under RLS could hide rows
-- from the reading role); every name is schema-qualified.  The migration
-- runs the same row checks itself under ACCESS EXCLUSIVE locks, plus one
-- this query does not: no catalog object other than the five listed
-- constraints may depend on a dropped column.
SELECT 'billing_migrations' AS check, count(*) FROM public.billing_migrations
UNION ALL SELECT 'billing_migration_receipts', count(*) FROM public.billing_migration_receipts
UNION ALL SELECT 'billing_migration_absence_reads', count(*) FROM public.billing_migration_absence_reads
UNION ALL SELECT 'roster revision not 0', count(*) FROM public.billing_roster_control WHERE billing_roster_revision <> 0
UNION ALL SELECT 'subscriptions not asaas', count(*) FROM public.billing_subscriptions WHERE provider IS DISTINCT FROM 'asaas'
UNION ALL SELECT 'subscriptions roster_revision not 1', count(*) FROM public.billing_subscriptions WHERE roster_revision <> 1
UNION ALL SELECT 'checkouts with migration_id', count(*) FROM public.billing_checkout_sessions WHERE migration_id IS NOT NULL
UNION ALL SELECT 'readiness roster_revision not 0', count(*) FROM public.billing_rollout_readiness_receipts WHERE roster_revision <> 0
UNION ALL SELECT 'unexpired readiness', count(*) FROM public.billing_rollout_readiness_receipts WHERE expires_at > now()
UNION ALL SELECT 'row level security on counted tables', count(*) FROM pg_catalog.pg_class WHERE (relrowsecurity OR relforcerowsecurity) AND oid IN ('public.billing_migrations'::regclass, 'public.billing_migration_receipts'::regclass, 'public.billing_migration_absence_reads'::regclass, 'public.billing_roster_control'::regclass, 'public.billing_subscriptions'::regclass, 'public.billing_checkout_sessions'::regclass, 'public.billing_rollout_readiness_receipts'::regclass);
