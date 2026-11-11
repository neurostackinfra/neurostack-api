-- SELECT-only readback after migration 047 (credit enforcement marks).
--
-- Run it after the one-off migrations task that applies 047 (it has no window
-- in scripts/asaas_migration_window.py), as the application role, inside a
-- REPEATABLE READ, READ ONLY transaction that is always rolled back.  Every
-- row must read held = true, or stop and return to the owner before the
-- behaviour release (PR-B):
--
-- * 'alembic_version is exactly 047': one row, 047;
-- * 'append-only trigger on public.credit_enforcement_marks': the trigger
--   trg_credit_enforcement_marks_immutable exists on the table, enabled, not
--   internal, and runs public.reject_credit_history_mutation();
-- * 'select and insert on public.credit_enforcement_marks': the migrations
--   carry no GRANT, so the application role reaches the new table only
--   through the migration role's default privileges; the row asks whether
--   the role this read runs as (current_user: neurostack_app in production)
--   holds every privilege the mark command needs on it, SELECT and INSERT
--   (append-only by trigger, so neither UPDATE nor DELETE is needed).  One
--   call per privilege, joined with AND: has_table_privilege with a list of
--   privileges ('SELECT,INSERT') is true when ANY of them is held.
--
-- The table is looked up by OID (to_regclass), so a table that does not
-- exist reads NULL or false instead of failing the read.
-- docs/runbooks/asaas-deployment.md ("Migration 047") shows this query byte
-- for byte below these comment lines, and
-- tests/integration/test_credit_enforcement_marks_migration_postgres.py keeps
-- the two equal and reads it at 046 and at 047.
SELECT 'alembic_version is exactly 047' AS check_name, count(*) = 1 AND min(version_num) = '047' AS held FROM alembic_version
UNION ALL SELECT 'append-only trigger on public.credit_enforcement_marks', EXISTS (SELECT 1 FROM pg_catalog.pg_trigger AS trigger WHERE trigger.tgrelid = to_regclass('public.credit_enforcement_marks') AND trigger.tgname = 'trg_credit_enforcement_marks_immutable' AND NOT trigger.tgisinternal AND trigger.tgenabled = 'O' AND trigger.tgfoid = to_regprocedure('public.reject_credit_history_mutation()'))
UNION ALL SELECT 'select and insert on public.credit_enforcement_marks', has_table_privilege(current_user, to_regclass('public.credit_enforcement_marks'), 'SELECT') AND has_table_privilege(current_user, to_regclass('public.credit_enforcement_marks'), 'INSERT');
