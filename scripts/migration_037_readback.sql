-- Read-only readback before migration 037 (Group 10 E6), generated from the
-- migration by scripts/migration_037_readback.py; never edit by hand.
--
-- Run it connected as the role that will run the migration, inside a READ
-- ONLY transaction, immediately before the window:
--   BEGIN READ ONLY; <this query>; ROLLBACK;
-- One row per check of the migration's preflight, in its order: verdict
-- 'ok', or the exact line the migration's refusal would carry.  Every
-- verdict 'ok' = the preflight passes for this state of the database.  It
-- reads alembic_version and the four legacy tables, so it must run as the
-- migration role (as another role the privilege row answers for that role,
-- and a role without SELECT on those tables gets "permission denied").  It
-- holds ordinary read locks on them while its transaction is open: never
-- leave that transaction open (the migration's ACCESS EXCLUSIVE lock would
-- wait on it and give up with 55P03).  The state can change before the run.
-- Where the migration creates and drops a probe table and function to read
-- what the role's new objects get, this computes that from pg_default_acl.
-- At revision 035 three rows are red (revision, catalog dependents, function
-- sources); at 037 it stops with: relation "billing_migrations" does not
-- exist.
WITH scope AS (
    SELECT ARRAY(SELECT to_regclass(name)::oid FROM unnest(ARRAY['public.billing_roster_control', 'public.billing_migrations', 'public.billing_migration_receipts', 'public.billing_migration_absence_reads']::text[]) AS name
                 WHERE to_regclass(name) IS NOT NULL) AS tables,
           ARRAY(SELECT to_regprocedure(name)::oid FROM unnest(ARRAY['public.billing_migration_snapshot_is_valid(jsonb,text,text,text,text,text,bigint)', 'public.lock_billing_roster_statement()', 'public.validate_billing_migration_receipt()', 'public.validate_billing_migration_receipt_commit()', 'public.protect_bound_billing_migration_target()', 'public.protect_billing_roster_control()', 'public.validate_billing_provider_evidence()', 'public.billing_evidence_timestamp(timestamp with time zone)', 'public.billing_provider_evidence_digest(text,text,timestamp with time zone,text,uuid,timestamp with time zone,text,uuid,text,text)', 'public.validate_billing_migration_absence_read()']::text[]) AS name
                 WHERE to_regprocedure(name) IS NOT NULL) AS functions,
           (SELECT string_agg(name, ', ' ORDER BY position)
            FROM unnest(ARRAY['public.billing_migration_snapshot_is_valid(jsonb,text,text,text,text,text,bigint)', 'public.lock_billing_roster_statement()', 'public.validate_billing_migration_receipt()', 'public.validate_billing_migration_receipt_commit()', 'public.protect_bound_billing_migration_target()', 'public.protect_billing_roster_control()', 'public.validate_billing_provider_evidence()', 'public.billing_evidence_timestamp(timestamp with time zone)', 'public.billing_provider_evidence_digest(text,text,timestamp with time zone,text,uuid,timestamp with time zone,text,uuid,text,text)', 'public.validate_billing_migration_absence_read()']::text[]) WITH ORDINALITY AS listed(name, position)
            WHERE to_regprocedure(name) IS NULL) AS missing_functions
),
scope_types AS (
    SELECT ARRAY(
        SELECT reltype FROM pg_class WHERE oid = ANY(scope.tables)
        UNION ALL
        SELECT typarray FROM pg_type WHERE oid IN (
            SELECT reltype FROM pg_class WHERE oid = ANY(scope.tables))
    ) AS types
    FROM scope
)
SELECT check_no, check_name, coalesce(problem, 'ok') AS verdict
FROM scope, scope_types, LATERAL (VALUES
    (1, 'revision', (SELECT format('alembic_version is %s, not 036', coalesce(string_agg(version_num, ', ' ORDER BY version_num), 'empty')) FROM alembic_version HAVING string_agg(version_num, ', ' ORDER BY version_num) IS DISTINCT FROM '036')),
    (2, 'billing_migrations rows', (SELECT format('billing_migrations has %s row(s)', count(*)) FROM billing_migrations HAVING count(*) > 0)),
    (3, 'billing_migration_receipts rows', (SELECT format('billing_migration_receipts has %s row(s)', count(*)) FROM billing_migration_receipts HAVING count(*) > 0)),
    (4, 'billing_migration_absence_reads rows', (SELECT format('billing_migration_absence_reads has %s row(s)', count(*)) FROM billing_migration_absence_reads HAVING count(*) > 0)),
    (5, 'roster revision', (SELECT format('billing_roster_control has %s row(s) whose revision is not 0', count(*)) FROM billing_roster_control WHERE billing_roster_revision <> 0 HAVING count(*) > 0)),
    (6, 'functions to drop exist', 'functions 037 drops do not exist: ' || scope.missing_functions),
    (7, 'catalog dependents', (SELECT 'unexpected objects depend on the dropped objects: '
            || string_agg(DISTINCT dependent, ', ' ORDER BY dependent)
        FROM (
            SELECT (pg_identify_object(dep.classid, dep.objid, 0)).type || ' '
                   || (pg_identify_object(dep.classid, dep.objid, 0)).identity
                   AS dependent
            FROM pg_depend AS dep
            WHERE dep.deptype IN ('n', 'a')
              AND ((dep.refclassid = 'pg_class'::regclass
                    AND dep.refobjid = ANY(scope.tables))
                   OR (dep.refclassid = 'pg_proc'::regclass
                       AND dep.refobjid = ANY(scope.functions))
                   OR (dep.refclassid = 'pg_type'::regclass
                       AND dep.refobjid = ANY(scope_types.types)))
              AND NOT (dep.classid = 'pg_class'::regclass
                       AND dep.objid = ANY(scope.tables))
              AND NOT (dep.classid = 'pg_proc'::regclass
                       AND dep.objid = ANY(scope.functions))
              AND NOT (dep.classid = 'pg_type'::regclass
                       AND dep.objid = ANY(scope_types.types))
        ) AS dependents
        WHERE dependent <> ALL (ARRAY['default value for public.billing_migration_absence_reads.created_at', 'default value for public.billing_migration_receipts.created_at', 'default value for public.billing_migrations.attempt_count', 'default value for public.billing_migrations.created_at', 'default value for public.billing_migrations.state', 'default value for public.billing_migrations.target_billing_cycle', 'default value for public.billing_migrations.target_provider', 'default value for public.billing_migrations.updated_at', 'default value for public.billing_roster_control.billing_roster_revision', 'default value for public.billing_roster_control.singleton', 'default value for public.billing_roster_control.updated_at', 'index public.ix_billing_migration_receipts_builder_id', 'index public.ix_billing_migrations_builder_id', 'table constraint billing_migration_absence_reads_pkey on public.billing_migration_absence_reads', 'table constraint billing_migration_receipts_pkey on public.billing_migration_receipts', 'table constraint billing_migrations_builder_id_fkey on public.billing_migrations', 'table constraint billing_migrations_pkey on public.billing_migrations', 'table constraint billing_roster_control_pkey on public.billing_roster_control', 'table constraint ck_billing_migration_absence_reads_evidence on public.billing_migration_absence_reads', 'table constraint ck_billing_migration_absence_reads_read_no on public.billing_migration_absence_reads', 'table constraint ck_billing_migration_absence_reads_shape on public.billing_migration_absence_reads', 'table constraint ck_billing_migration_absence_reads_times on public.billing_migration_absence_reads', 'table constraint ck_billing_migration_receipts_external_reference on public.billing_migration_receipts', 'table constraint ck_billing_migration_receipts_kind on public.billing_migration_receipts', 'table constraint ck_billing_migration_receipts_provider on public.billing_migration_receipts', 'table constraint ck_billing_migration_receipts_provider_evidence on public.billing_migration_receipts', 'table constraint ck_billing_migration_receipts_provider_expiry on public.billing_migration_receipts', 'table constraint ck_billing_migration_receipts_saga_shape on public.billing_migration_receipts', 'table constraint ck_billing_migration_receipts_sha256 on public.billing_migration_receipts', 'table constraint ck_billing_migration_receipts_status on public.billing_migration_receipts', 'table constraint ck_billing_migrations_attempt_count on public.billing_migrations', 'table constraint ck_billing_migrations_cutoffs on public.billing_migrations', 'table constraint ck_billing_migrations_roster_revision on public.billing_migrations', 'table constraint ck_billing_migrations_snapshot_allowlist on public.billing_migrations', 'table constraint ck_billing_migrations_state on public.billing_migrations', 'table constraint ck_billing_migrations_target_amount on public.billing_migrations', 'table constraint ck_billing_migrations_target_cycle on public.billing_migrations', 'table constraint ck_billing_migrations_target_provider on public.billing_migrations', 'table constraint ck_billing_roster_control_revision on public.billing_roster_control', 'table constraint ck_billing_roster_control_singleton on public.billing_roster_control', 'table constraint fk_billing_migration_absence_reads_checkout on public.billing_migration_absence_reads', 'table constraint fk_billing_migration_absence_reads_migration on public.billing_migration_absence_reads', 'table constraint fk_billing_migration_receipts_builder_migration on public.billing_migration_receipts', 'table constraint fk_billing_migrations_builder_subscription on public.billing_migrations', 'table constraint trg_billing_migration_receipt_commit on public.billing_migration_receipts', 'table constraint uq_billing_migration_absence_reads_event on public.billing_migration_absence_reads', 'table constraint uq_billing_migration_absence_reads_slot on public.billing_migration_absence_reads', 'table constraint uq_billing_migration_receipts_operation_kind on public.billing_migration_receipts', 'table constraint uq_billing_migration_receipts_provider_event on public.billing_migration_receipts', 'table constraint uq_billing_migrations_builder_id_id on public.billing_migrations', 'table constraint uq_billing_migrations_builder_subscription on public.billing_migrations', 'table constraint uq_billing_migrations_target_external on public.billing_migrations', 'trigger trg_billing_migration_absence_read_validate on public.billing_migration_absence_reads', 'trigger trg_billing_migration_absence_reads_immutable on public.billing_migration_absence_reads', 'trigger trg_billing_migration_absence_reads_roster_fence on public.billing_migration_absence_reads', 'trigger trg_billing_migration_receipt_commit on public.billing_migration_receipts', 'trigger trg_billing_migration_receipt_validate on public.billing_migration_receipts', 'trigger trg_billing_migration_receipts_immutable on public.billing_migration_receipts', 'trigger trg_billing_migration_receipts_roster_fence on public.billing_migration_receipts', 'trigger trg_billing_migration_target_immutable on public.billing_migrations', 'trigger trg_billing_migrations_roster_fence on public.billing_migrations', 'trigger trg_billing_provider_evidence_validate on public.billing_migration_receipts', 'trigger trg_billing_roster_control_fence on public.billing_roster_control', 'trigger trg_billing_roster_control_history on public.billing_roster_control']::text[]))),
    (8, 'function sources', (SELECT 'functions outside 037''s scope name a dropped object: '
            || string_agg(p.oid::regprocedure::text, ', '
                          ORDER BY p.oid::regprocedure::text)
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND p.oid <> ALL (scope.functions)
          AND p.prosrc ~* '\m(billing_roster_control|billing_migrations|billing_migration_receipts|billing_migration_absence_reads|billing_migration_snapshot_is_valid|lock_billing_roster_statement|validate_billing_migration_receipt|validate_billing_migration_receipt_commit|protect_bound_billing_migration_target|protect_billing_roster_control|validate_billing_provider_evidence|billing_evidence_timestamp|billing_provider_evidence_digest|validate_billing_migration_absence_read)\M')),
    (9, 'columns', (SELECT 'columns the downgrade would not restore: '
            || string_agg(column_name, ', ' ORDER BY column_name)
        FROM (
            SELECT c.relname || '.' || a.attname AS column_name
            FROM pg_attribute AS a
            JOIN pg_class AS c ON c.oid = a.attrelid
            WHERE a.attrelid = ANY(scope.tables)
              AND a.attnum > 0 AND NOT a.attisdropped
        ) AS columns
        WHERE column_name <> ALL (ARRAY['billing_roster_control.singleton', 'billing_roster_control.billing_roster_revision', 'billing_roster_control.updated_at', 'billing_migrations.id', 'billing_migrations.builder_id', 'billing_migrations.subscription_id', 'billing_migrations.state', 'billing_migrations.renewal_date', 'billing_migrations.open_by_date', 'billing_migrations.cutover_by_date', 'billing_migrations.target_provider', 'billing_migrations.target_external_subscription_id', 'billing_migrations.target_catalog_version_id', 'billing_migrations.target_offer_id', 'billing_migrations.target_plan', 'billing_migrations.target_billing_cycle', 'billing_migrations.target_coupon_code', 'billing_migrations.target_amount_cents', 'billing_migrations.target_snapshot', 'billing_migrations.operation_id', 'billing_migrations.attempt_count', 'billing_migrations.source_roster_revision', 'billing_migrations.legacy_cancelled_at', 'billing_migrations.created_at', 'billing_migrations.updated_at', 'billing_migration_receipts.id', 'billing_migration_receipts.builder_id', 'billing_migration_receipts.migration_id', 'billing_migration_receipts.operation_id', 'billing_migration_receipts.receipt_kind', 'billing_migration_receipts.provider', 'billing_migration_receipts.external_reference', 'billing_migration_receipts.status', 'billing_migration_receipts.content_sha256', 'billing_migration_receipts.actor_user_id', 'billing_migration_receipts.observed_at', 'billing_migration_receipts.created_at', 'billing_migration_receipts.provider_event_id', 'billing_migration_receipts.provider_authority', 'billing_migration_receipts.provider_key_id', 'billing_migration_receipts.provider_operation', 'billing_migration_receipts.provider_signature', 'billing_migration_receipts.provider_observed_at', 'billing_migration_receipts.provider_expires_at', 'billing_migration_receipts.provider_snapshot_digest', 'billing_migration_receipts.provider_evidence_digest', 'billing_migration_absence_reads.id', 'billing_migration_absence_reads.builder_id', 'billing_migration_absence_reads.migration_id', 'billing_migration_absence_reads.operation_id', 'billing_migration_absence_reads.read_no', 'billing_migration_absence_reads.prior_checkout_session_id', 'billing_migration_absence_reads.convergence_limit_at', 'billing_migration_absence_reads.provider', 'billing_migration_absence_reads.external_reference', 'billing_migration_absence_reads.provider_event_id', 'billing_migration_absence_reads.provider_authority', 'billing_migration_absence_reads.provider_key_id', 'billing_migration_absence_reads.provider_operation', 'billing_migration_absence_reads.provider_signature', 'billing_migration_absence_reads.provider_observed_at', 'billing_migration_absence_reads.provider_expires_at', 'billing_migration_absence_reads.provider_snapshot_digest', 'billing_migration_absence_reads.provider_evidence_digest', 'billing_migration_absence_reads.status', 'billing_migration_absence_reads.actor_user_id', 'billing_migration_absence_reads.observed_at', 'billing_migration_absence_reads.created_at']::text[]))),
    (10, 'owners and privileges', (SELECT format(
                'owners or privileges the downgrade would not recreate as %s: %s',
                current_user, string_agg(item, ', ' ORDER BY item))
        FROM (
            SELECT format('table %s (owner %s, acl %s)', c.relname,
                          c.relowner::regrole, coalesce(c.relacl::text, 'default'))
                   AS item
            FROM pg_class AS c
            WHERE c.oid = ANY(scope.tables)
              AND (c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = current_user)
                   OR ARRAY(SELECT format('%s>%s:%s:%s', grantor, grantee, privilege_type, bool_or(is_grantable)) FROM aclexplode(nullif(coalesce(c.relacl, acldefault('r', c.relowner)), '{}'::aclitem[])) GROUP BY grantor, grantee, privilege_type ORDER BY 1) <> (SELECT CASE WHEN cardinality(merged.privileges) = 0 THEN ARRAY(SELECT format('%s>%s:%s:%s', grantor, grantee, privilege_type, bool_or(is_grantable)) FROM aclexplode(nullif(acldefault('r', (SELECT oid FROM pg_roles WHERE rolname = current_user)), '{}'::aclitem[])) GROUP BY grantor, grantee, privilege_type ORDER BY 1) ELSE merged.privileges END FROM (SELECT ARRAY(SELECT format('%s>%s:%s:%s', grantor, grantee, privilege_type, bool_or(is_grantable)) FROM (SELECT * FROM aclexplode(nullif(coalesce((SELECT defaclacl FROM pg_default_acl WHERE defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) AND defaclnamespace = 0 AND defaclobjtype = 'r'), acldefault('r', (SELECT oid FROM pg_roles WHERE rolname = current_user))), '{}'::aclitem[])) UNION ALL SELECT * FROM aclexplode(nullif((SELECT defaclacl FROM pg_default_acl WHERE defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) AND defaclnamespace = 'public'::regnamespace AND defaclobjtype = 'r'), '{}'::aclitem[]))) AS granted GROUP BY grantor, grantee, privilege_type ORDER BY 1) AS privileges) AS merged)
                   OR EXISTS (SELECT 1 FROM pg_attribute AS a
                              WHERE a.attrelid = c.oid AND a.attnum > 0
                                AND a.attacl IS NOT NULL))
            UNION ALL
            SELECT format('function %s (owner %s, acl %s)', p.oid::regprocedure,
                          p.proowner::regrole, coalesce(p.proacl::text, 'default'))
            FROM pg_proc AS p
            WHERE p.oid = ANY(scope.functions)
              AND (p.proowner <> (SELECT oid FROM pg_roles WHERE rolname = current_user)
                   OR ARRAY(SELECT format('%s>%s:%s:%s', grantor, grantee, privilege_type, bool_or(is_grantable)) FROM aclexplode(nullif(coalesce(p.proacl, acldefault('f', p.proowner)), '{}'::aclitem[])) GROUP BY grantor, grantee, privilege_type ORDER BY 1) <> (SELECT CASE WHEN cardinality(merged.privileges) = 0 THEN ARRAY(SELECT format('%s>%s:%s:%s', grantor, grantee, privilege_type, bool_or(is_grantable)) FROM aclexplode(nullif(acldefault('f', (SELECT oid FROM pg_roles WHERE rolname = current_user)), '{}'::aclitem[])) GROUP BY grantor, grantee, privilege_type ORDER BY 1) ELSE merged.privileges END FROM (SELECT ARRAY(SELECT format('%s>%s:%s:%s', grantor, grantee, privilege_type, bool_or(is_grantable)) FROM (SELECT * FROM aclexplode(nullif(coalesce((SELECT defaclacl FROM pg_default_acl WHERE defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) AND defaclnamespace = 0 AND defaclobjtype = 'f'), acldefault('f', (SELECT oid FROM pg_roles WHERE rolname = current_user))), '{}'::aclitem[])) UNION ALL SELECT * FROM aclexplode(nullif((SELECT defaclacl FROM pg_default_acl WHERE defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) AND defaclnamespace = 'public'::regnamespace AND defaclobjtype = 'f'), '{}'::aclitem[]))) AS granted GROUP BY grantor, grantee, privilege_type ORDER BY 1) AS privileges) AS merged))
        ) AS differing
        HAVING count(*) > 0)),
    (11, 'comments', (SELECT 'comments the downgrade would not restore: '
            || string_agg(described, ', ' ORDER BY described)
        FROM (
            SELECT (pg_identify_object(d.classoid, d.objoid, d.objsubid)).type
                   || ' '
                   || (pg_identify_object(d.classoid, d.objoid, d.objsubid)).identity
                   AS described
            FROM pg_description AS d
            WHERE (d.classoid = 'pg_class'::regclass
                   AND (d.objoid = ANY(scope.tables)
                        OR d.objoid IN (SELECT indexrelid FROM pg_index
                                        WHERE indrelid = ANY(scope.tables))))
               OR (d.classoid = 'pg_proc'::regclass
                   AND d.objoid = ANY(scope.functions))
               OR (d.classoid = 'pg_type'::regclass
                   AND d.objoid = ANY(scope_types.types))
               OR (d.classoid = 'pg_constraint'::regclass
                   AND d.objoid IN (SELECT oid FROM pg_constraint
                                    WHERE conrelid = ANY(scope.tables)))
               OR (d.classoid = 'pg_trigger'::regclass
                   AND d.objoid IN (SELECT oid FROM pg_trigger
                                    WHERE tgrelid = ANY(scope.tables)))
        ) AS comments))
) AS checks(check_no, check_name, problem)
ORDER BY check_no;
