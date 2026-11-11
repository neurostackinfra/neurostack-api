-- SELECT-only preflight before migration 047 (credit enforcement marks).
--
-- Migration 047 has no window in scripts/asaas_migration_window.py: the real
-- production applies it with its own one-off migrations task (alembic upgrade
-- to the image's head) after the bridge image serves.  Run this read right
-- before that task, as the application role, inside a REPEATABLE READ, READ
-- ONLY transaction that is rolled back.  Migration 047 only creates one table
-- (no foreign key) and reuses the history trigger function of 038, so the
-- start state is the 046 schema exactly where it touches it, and every count
-- must be 0, or stop and return to the owner:
--
-- * 'alembic_version not exactly 046' is 0 only when alembic_version holds
--   exactly one row, 046 (the task's upgrade applies 047 alone);
-- * '047 tables present' counts the tables 047 creates (CREATED_TABLES) that
--   already exist (to_regclass, so an absent table reads 0 instead of failing
--   the read);
-- * '047 prerequisites missing' is 1 minus whether what 047 needs exists:
--   the function public.reject_credit_history_mutation() (its append-only
--   trigger).
--
-- The rows count catalog objects and the one alembic_version row, never
-- table rows, so row-level security cannot hide one.  Every name is
-- schema-qualified.  docs/runbooks/asaas-deployment.md ("Migration 047")
-- shows this query byte for byte below these comment lines, and
-- tests/integration/test_credit_enforcement_marks_migration_postgres.py keeps
-- the two equal, every list equal to the migration's constants, and reads 0
-- with it at 046 and not 0 at 047.
SELECT 'alembic_version not exactly 046' AS check_name, CASE WHEN count(*) = 1 AND min(version_num) = '046' THEN 0 ELSE 1 END AS found FROM alembic_version
UNION ALL SELECT '047 tables present', count(*) FROM unnest(ARRAY['public.credit_enforcement_marks']) AS relation WHERE to_regclass(relation) IS NOT NULL
UNION ALL SELECT '047 prerequisites missing', CASE WHEN to_regprocedure('public.reject_credit_history_mutation()') IS NULL THEN 1 ELSE 0 END;
