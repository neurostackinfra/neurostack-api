-- SELECT-only preflight before the 038-042 migration window (window 042).
--
-- scripts/asaas_migration_window.py (render-pre-read --window 042) runs it in
-- a read task, as the application role, inside a REPEATABLE READ, READ ONLY
-- transaction that is always rolled back, right before the window; the same
-- task reads alembic_version, which must be 037.  Migrations 038 to 042 only
-- create objects, so the start state is "none of them exists yet": each row
-- counts the tables (CREATED_TABLES) or the functions (CREATED_FUNCTIONS,
-- exact signatures) one of those migrations creates that already exist, and
-- every count must be 0, or stop and return to the owner.  The rows count
-- catalog objects, not table rows, so row-level security cannot hide one.
-- Every name is schema-qualified.  docs/runbooks/asaas-deployment.md
-- ("Window 042: migrations 038-042") shows this query byte for byte below
-- these comment lines; tests/scripts/test_asaas_migration_window.py keeps
-- the two equal and every list equal to its migration's constants.
SELECT '038 tables present' AS check_name, count(*) AS found FROM unnest(ARRAY['public.credit_accounts', 'public.credit_effects', 'public.credit_lots', 'public.credit_ledger_entries', 'public.credit_debts', 'public.credit_debt_origins', 'public.credit_debt_settlements', 'public.credit_fact_inbox', 'public.credit_revision_gaps', 'public.credit_capability_readiness', 'public.credit_ingress_claims', 'public.credit_response_outbox', 'public.credit_enforcement_state', 'public.credit_enforcement_binding', 'public.credit_observational_effects']) AS relation WHERE to_regclass(relation) IS NOT NULL
UNION ALL SELECT '039 tables present', count(*) FROM unnest(ARRAY['public.credit_inventory_versions', 'public.credit_allocations', 'public.credit_egress_deliveries']) AS relation WHERE to_regclass(relation) IS NOT NULL
UNION ALL SELECT '040 tables present', count(*) FROM unnest(ARRAY['public.credit_object_copies', 'public.credit_object_copy_jobs', 'public.credit_qdrant_jobs']) AS relation WHERE to_regclass(relation) IS NOT NULL
UNION ALL SELECT '041 tables present', count(*) FROM unnest(ARRAY['public.credit_lifecycle_episodes', 'public.credit_lifecycle_transitions', 'public.credit_threshold_alerts', 'public.credit_operational_role_assignments', 'public.credit_operational_authorizations', 'public.credit_operational_authorization_receipts', 'public.credit_hold_generations', 'public.credit_hold_events', 'public.credit_purge_fences', 'public.credit_retention_tombstones', 'public.credit_purge_jobs', 'public.credit_purge_results', 'public.credit_purge_receipts']) AS relation WHERE to_regclass(relation) IS NOT NULL
UNION ALL SELECT '042 tables present', count(*) FROM unnest(ARRAY['public.credit_cutoff_acceptances']) AS relation WHERE to_regclass(relation) IS NOT NULL
UNION ALL SELECT '038 functions present', count(*) FROM unnest(ARRAY['public.reject_credit_history_mutation()', 'public.check_credit_tenant_builder()', 'public.guard_credit_revision_gap()', 'public.guard_credit_enforcement_binding()']) AS signature WHERE to_regprocedure(signature) IS NOT NULL
UNION ALL SELECT '041 functions present', count(*) FROM unnest(ARRAY['public.guard_credit_lifecycle_episode()', 'public.guard_credit_hold_generation()']) AS signature WHERE to_regprocedure(signature) IS NOT NULL
UNION ALL SELECT '042 functions present', count(*) FROM unnest(ARRAY['public.reject_credit_cutoff_acceptance_mutation()']) AS signature WHERE to_regprocedure(signature) IS NOT NULL;
