-- SELECT-only readback after migration 037 (window 037).
--
-- scripts/asaas_migration_window.py (render-post-read --window 037) runs it
-- in a read task, inside a REPEATABLE READ, READ ONLY transaction that is
-- always rolled back; the same task reads alembic_version, which must be 037.
-- docs/runbooks/asaas-deployment.md, "Migration 037": verify alembic_version
-- == 037 and the four tables are gone.  Every row must read gone = true.
SELECT relation, to_regclass(relation) IS NULL AS gone
FROM unnest(ARRAY['public.billing_migrations', 'public.billing_migration_receipts',
                  'public.billing_migration_absence_reads', 'public.billing_roster_control'])
     AS relation
ORDER BY relation;
