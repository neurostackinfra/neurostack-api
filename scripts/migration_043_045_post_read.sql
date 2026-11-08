-- SELECT-only readback after the media bridge migration (window 045).
--
-- scripts/asaas_migration_window.py (render-post-read --window 045) runs it
-- in a read task, as the application role, inside a REPEATABLE READ, READ
-- ONLY transaction that is always rolled back; the same task reads
-- alembic_version, which must be 045.  The migrations carry no GRANT: the
-- application role reaches a new table through the migration role's default
-- privileges, and a new column through its grants on the table.  So, after
-- the run, each row asks whether the role this read runs as (the DML
-- application role of the read family) holds every privilege the behaviour
-- release needs on one object: SELECT, INSERT, UPDATE and DELETE on
-- media_deliveries; SELECT, INSERT and UPDATE on the column
-- media_assets.trigger_intents.  One call per privilege, joined with AND:
-- has_table_privilege with a list of privileges is true when ANY of them is
-- held.  Each object is looked up by OID, so a table or column that does not
-- exist reads NULL instead of failing the read.  Every row must read
-- held = true, or stop and return to the owner before the behaviour release.
-- docs/runbooks/asaas-deployment.md ("Migration 045").
SELECT 'dml on public.media_deliveries' AS check_name, has_table_privilege(current_user, to_regclass('public.media_deliveries'), 'SELECT') AND has_table_privilege(current_user, to_regclass('public.media_deliveries'), 'INSERT') AND has_table_privilege(current_user, to_regclass('public.media_deliveries'), 'UPDATE') AND has_table_privilege(current_user, to_regclass('public.media_deliveries'), 'DELETE') AS held
UNION ALL SELECT 'dml on public.media_assets.trigger_intents', has_column_privilege(current_user, to_regclass('public.media_assets'), column_number, 'SELECT') AND has_column_privilege(current_user, to_regclass('public.media_assets'), column_number, 'INSERT') AND has_column_privilege(current_user, to_regclass('public.media_assets'), column_number, 'UPDATE') FROM (SELECT (SELECT attribute.attnum FROM pg_catalog.pg_attribute AS attribute WHERE attribute.attrelid = to_regclass('public.media_assets') AND attribute.attname = 'trigger_intents' AND attribute.attnum > 0 AND NOT attribute.attisdropped) AS column_number) AS added;
