"""Migration 037 (Group 10 E6, "remover objetos legados") on real PostgreSQL.

Owner decisions (asaas-payment-gateway wish, 2026-10-17/18): the downgrade is
real and restores 036 from DDL extracted from a fresh 036 database, never
typed by hand; the upgrade refuses, changing nothing, while any legacy row or
anything the downgrade could not put back exists.

* fidelity: the frozen file equals what ``scripts/alembic_frozen_ddl.py``
  extracts from a brand-new 036 database (byte for byte, the extraction
  leaving that database as it was); 037 removes exactly the four legacy
  tables and ten functions and changes nothing else; 037 -> 036 gives back
  the brand-new 036 catalog exactly (column order included, because whole
  tables come back), with working guards; 035 -> 037, 032 -> 037 on a clean
  database and a re-upgrade after each round trip land on the same 037
  catalog;
* preflight: every category refuses the upgrade alone, with its own line in
  one exact message, and leaves the catalog untouched; all of them are
  reported together in order; the non-legacy rows next to each boundary
  (roster at 0, ASAAS rows, an unexpired readiness receipt) pass and survive;
* readback parity: the operator's read-only readback
  (``scripts/migration_037_readback.sql``), run as the migration role, shows
  exactly the refusal lines the migration then raises -- in every refusal
  test here, in non-superuser role setups with and without default
  privileges -- and only ``ok`` where the migration applies;
* downgrade: needs no data; refuses only an object whose name the restore
  needs, or a revision other than 037;
* lock wait: with a plain read held open on any table either preflight
  locks, each direction gives up after its 5 s ``lock_timeout`` with SQLSTATE
  55P03 on the preflight itself and leaves the catalog untouched;
* privileges: owners, ACLs and default privileges of a non-default setup
  (application role, default privileges, a function with EXECUTE revoked
  from PUBLIC, objects owned by another role) compare equal after the round
  trip.

Every database here is a child of the smoke cluster, created and dropped by
this module; nothing touches a shared or production database.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from scripts.alembic_frozen_ddl import FROZEN_037_PATH, extract_037, render
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import (
    _PLANT,
    _alembic,
    _alembic_while_reading,
    _assert_035_restored,
    _assert_lock_timeout_in_the_preflight,
    _catalog,
    _Cluster,
    _execute,
    _migrate,
    _readiness,
    _refusal,
    _scalar,
)

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _ROOT
    / "alembic"
    / "versions"
    / "20261020_000001_037_drop_legacy_billing_migration_objects.py"
)
_TABLES = (
    "billing_roster_control",
    "billing_migrations",
    "billing_migration_receipts",
    "billing_migration_absence_reads",
)
_FUNCTIONS = (
    "billing_migration_snapshot_is_valid(jsonb,text,text,text,text,text,bigint)",
    "lock_billing_roster_statement()",
    "validate_billing_migration_receipt()",
    "validate_billing_migration_receipt_commit()",
    "protect_bound_billing_migration_target()",
    "protect_billing_roster_control()",
    "validate_billing_provider_evidence()",
    "billing_evidence_timestamp(timestamp with time zone)",
    "billing_provider_evidence_digest(text,text,timestamp with time zone,text,"
    "uuid,timestamp with time zone,text,uuid,text,text)",
    "validate_billing_migration_absence_read()",
)
_BUILDER = UUID("00000000-0000-4000-8000-0000000e6037")
_REFUSED = "migration 037 refused (nothing was changed): "
_DOWNGRADE_REFUSED = "downgrade of migration 037 refused (nothing was changed): "
_UPGRADE_LOCKED = (
    *_TABLES,
    "builders",
    "billing_subscriptions",
    "billing_checkout_sessions",
)
_DOWNGRADE_LOCKED = ("builders", "billing_subscriptions", "billing_checkout_sessions")


def _load_migration() -> ModuleType:
    """The migration module itself (its preflight SQL, run directly below).

    In-process, the repository's ``alembic/`` directory may shadow the
    package; ``op`` is only needed as a name for the module to import.
    """

    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        setattr(alembic_package, "op", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("migration_037", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION = _load_migration()


@dataclass(frozen=True)
class _Baseline:
    cluster: _Cluster
    template: str  # a brand-new 036 database (plus one builder row)
    catalog_036: dict[str, Any]
    catalog_037: dict[str, Any]


def _rows(catalog: dict[str, Any], key: str) -> set[tuple[Any, ...]]:
    return set(catalog[key])


# ---------------------------------------------------------------------------
# Module fixtures: one brand-new 036 template and the canonical 037 catalog.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def baseline() -> AsyncIterator[_Baseline]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    try:
        template = await cluster.fresh()
        await _migrate(cluster.url(template), "upgrade", "036")
        await _execute(
            cluster,
            template,
            "INSERT INTO builders (id, name) VALUES (:id, 'G10 E6 builder')",
            id=_BUILDER,
        )
        catalog_036 = await _catalog(cluster, template)
        probe = await cluster.clone(template)
        await _migrate(cluster.url(probe), "upgrade", "037")
        catalog_037 = await _catalog(cluster, probe)
        await cluster.drop(probe)
        yield _Baseline(cluster, template, catalog_036, catalog_037)
    finally:
        for name in list(reversed(cluster.created)):
            await cluster.drop(name)
        await admin.dispose()


@pytest.fixture
async def at_036(baseline: _Baseline) -> AsyncIterator[str]:
    """A disposable copy of the brand-new 036 template."""

    name = await baseline.cluster.clone(baseline.template)
    try:
        yield name
    finally:
        await baseline.cluster.drop(name)


@pytest.fixture
async def at_037(baseline: _Baseline, at_036: str) -> str:
    await _migrate(baseline.cluster.url(at_036), "upgrade", "037")
    return at_036


@pytest.fixture
async def roles(baseline: _Baseline) -> AsyncIterator[tuple[str, str]]:
    """Two throwaway cluster roles; dropped after every database that
    granted them anything is gone (the per-test database fixtures are
    finalized first, being requested after this one)."""

    suffix = uuid4().hex[:12]
    names = (f"g10e6_app_{suffix}", f"g10e6_owner_{suffix}")
    async with baseline.cluster.admin.connect() as connection:
        for role in names:
            await connection.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN')
    try:
        yield names
    finally:
        async with baseline.cluster.admin.connect() as connection:
            for role in names:
                await connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')


# ---------------------------------------------------------------------------
# Fidelity and shape.
# ---------------------------------------------------------------------------


async def test_frozen_ddl_is_exactly_what_a_fresh_036_catalog_yields(
    baseline: _Baseline,
) -> None:
    """Not typed by hand: re-extracting from a brand-new 036 database gives
    the frozen file byte for byte, and leaves that database as it was (the
    CHECK read-back proof runs on a temporary table that is rolled back)."""

    cluster = baseline.cluster
    engine = create_async_engine(cluster.url(baseline.template), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            document = await connection.run_sync(extract_037)
    finally:
        await engine.dispose()
    assert render(document) == FROZEN_037_PATH.read_text(encoding="ascii")
    assert await _catalog(cluster, baseline.template) == baseline.catalog_036


async def test_037_drops_exactly_the_legacy_objects(baseline: _Baseline) -> None:
    before, after = baseline.catalog_036, baseline.catalog_037
    legacy = set(_TABLES)

    def on_legacy(row: tuple[Any, ...]) -> bool:
        return row[0] in legacy

    assert _rows(before, "relations") - _rows(after, "relations") == {
        row for row in before["relations"] if on_legacy(row)
    }
    assert {row[0] for row in before["relations"] if on_legacy(row)} == legacy
    assert _rows(after, "relations") <= _rows(before, "relations")
    for key in ("columns", "constraints", "indexes", "triggers"):
        dropped = _rows(before, key) - _rows(after, key)
        assert dropped == {row for row in before[key] if on_legacy(row)}, key
        assert dropped, key
        assert _rows(after, key) <= _rows(before, key), key
    assert len([row for row in before["constraints"] if on_legacy(row)]) == 39
    assert len([row for row in before["triggers"] if on_legacy(row)]) == 12
    functions_before = {row[0]: row for row in before["functions"]}
    functions_after = {row[0]: row for row in after["functions"]}
    assert set(functions_before) - set(functions_after) == set(_FUNCTIONS)
    assert set(functions_after) <= set(functions_before)
    # Every surviving function is untouched: definition, ACL, owner,
    # SECURITY DEFINER, configuration, comment.
    for signature, row in functions_after.items():
        assert row == functions_before[signature], signature
    for key in (
        "types",
        "sequences",
        "views",
        "rules",
        "policies",
        "extensions",
        "default_privileges",
        "schema",
    ):
        assert after[key] == before[key], key
    assert {
        table: order
        for table, order in before["column_order"].items()
        if table not in legacy
    } == after["column_order"]
    assert after["alembic_version"] == [("037",)]


async def test_round_trip_restores_a_brand_new_036_and_reupgrades_identically(
    baseline: _Baseline, at_036: str
) -> None:
    cluster = baseline.cluster
    url = cluster.url(at_036)
    assert await _readback(cluster, at_036) == ""
    await _migrate(url, "upgrade", "037")
    assert await _catalog(cluster, at_036) == baseline.catalog_037
    await _migrate(url, "downgrade", "036")
    restored = await _catalog(cluster, at_036)
    # Whole tables come back, so even the column order is the brand-new one.
    assert restored == baseline.catalog_036
    frozen = json.loads(FROZEN_037_PATH.read_text(encoding="ascii"))
    functions = {row[0]: row[2] for row in restored["functions"]}
    for entry in frozen["functions"]:
        assert functions[entry["signature"]] == entry["statement"]
    seed = await _scalar(
        cluster,
        at_036,
        "SELECT array_agg(ARRAY[singleton::text, billing_roster_revision::text]) "
        "FROM billing_roster_control",
    )
    assert seed == [["true", "0"]]
    for table in _TABLES[1:]:
        assert await _scalar(cluster, at_036, f"SELECT count(*) FROM {table}") == 0
    await _migrate(url, "upgrade", "037")
    assert await _catalog(cluster, at_036) == baseline.catalog_037


async def test_upgrade_035_to_037_and_down_to_035(baseline: _Baseline) -> None:
    """035 -> 037 in one run; 037 -> 036 -> 035 back; 037 again."""

    cluster = baseline.cluster
    name = await cluster.fresh()
    try:
        url = cluster.url(name)
        await _migrate(url, "upgrade", "035")
        await _execute(
            cluster,
            name,
            "INSERT INTO builders (id, name) VALUES (:id, 'G10 E6 builder')",
            id=_BUILDER,
        )
        catalog_035 = await _catalog(cluster, name)
        await _migrate(url, "upgrade", "037")
        assert await _catalog(cluster, name) == baseline.catalog_037
        await _migrate(url, "downgrade", "036")
        assert await _catalog(cluster, name) == baseline.catalog_036
        await _migrate(url, "downgrade", "035")
        _assert_035_restored(await _catalog(cluster, name), catalog_035)
        await _migrate(url, "upgrade", "037")
        assert await _catalog(cluster, name) == baseline.catalog_037
    finally:
        await cluster.drop(name)


async def test_upgrade_032_to_037_on_a_clean_database(baseline: _Baseline) -> None:
    """The production path on a clean cluster database: 032 (production's
    revision on 2026-10-17, one builder), then 033-037 in one run."""

    cluster = baseline.cluster
    name = await cluster.fresh()
    try:
        url = cluster.url(name)
        await _migrate(url, "upgrade", "032")
        await _execute(
            cluster,
            name,
            "INSERT INTO builders (id, name) VALUES (:id, 'G10 E6 builder')",
            id=_BUILDER,
        )
        await _migrate(url, "upgrade", "037")
        assert await _catalog(cluster, name) == baseline.catalog_037
        await _migrate(url, "downgrade", "036")
        assert await _catalog(cluster, name) == baseline.catalog_036
        await _migrate(url, "upgrade", "037")
        assert await _catalog(cluster, name) == baseline.catalog_037
        assert await _scalar(cluster, name, "SELECT count(*) FROM builders") == 1
    finally:
        await cluster.drop(name)


async def test_the_restored_036_guards_work(baseline: _Baseline, at_037: str) -> None:
    """Behaviour, not only the catalog: after 037 -> 036 the restored CHECK
    and its helper refuse a malformed saga snapshot, the roster history
    trigger refuses a jump and a delete, and the restored provider-evidence
    digest computes the same value as on a brand-new 036 database."""

    cluster = baseline.cluster
    await _migrate(cluster.url(at_037), "downgrade", "036")
    subscription = uuid4()
    await _execute(
        cluster,
        at_037,
        "INSERT INTO billing_subscriptions (id, builder_id, provider, "
        "external_subscription_id, is_active, plan, billing_cycle, status) "
        "VALUES (:id, :builder, NULL, 'g10-e6-guard', false, 'start', "
        "'monthly', 'provider_unknown')",
        id=subscription,
        builder=_BUILDER,
    )
    refusals = {
        "ck_billing_migrations_snapshot_allowlist": (
            "INSERT INTO billing_migrations (id, builder_id, subscription_id, "
            "renewal_date, open_by_date, cutover_by_date, "
            "target_catalog_version_id, target_offer_id, target_plan, "
            "target_amount_cents, target_snapshot, source_roster_revision) "
            "VALUES (gen_random_uuid(), :builder, :subscription, "
            "DATE '2026-12-10', DATE '2026-12-03', DATE '2026-12-08', "
            "'catalog-v1', 'plan_plus_monthly_v1', 'plus', 99900, "
            '\'{"catalog_version_id": "catalog-v1"}\'::jsonb, 1)'
        ),
        "billing roster revision is saga-owned": (
            "UPDATE billing_roster_control SET billing_roster_revision = 2 "
            "WHERE singleton = true"
        ),
        "billing financial history is not deletable": (
            "DELETE FROM billing_roster_control"
        ),
    }
    for expected, statement in refusals.items():
        with pytest.raises(DBAPIError, match=expected):
            await _execute(
                cluster, at_037, statement, builder=_BUILDER, subscription=subscription
            )
    digest = (
        "SELECT billing_provider_evidence_digest('asaas.api.v3', 'evt-1', "
        "TIMESTAMPTZ '2026-10-20 12:00:00.5+00', 'sub-1', "
        "'00000000-0000-4000-8000-000000000001', "
        "TIMESTAMPTZ '2026-10-20 11:00:00+00', 'op', "
        "'00000000-0000-4000-8000-000000000002', 'asaas', repeat('a', 64))"
    )
    restored = await _scalar(cluster, at_037, digest)
    assert isinstance(restored, str) and len(restored) == 64
    assert restored == await _scalar(cluster, baseline.template, digest)


# ---------------------------------------------------------------------------
# Upgrade preflight.
# ---------------------------------------------------------------------------

# A saga snapshot billing_migration_snapshot_is_valid() accepts: the CHECK
# still applies under session_replication_role = replica.
_SNAPSHOT = json.dumps(
    {
        "catalog_version_id": "catalog-v1",
        "offer_id": "plan_plus_monthly_v1",
        "plan": "plus",
        "billing_cycle": "monthly",
        "coupon_code": None,
        "amount_cents": 99900,
        "credit_grant_mc": 5000,
        "included_clinics": 1,
        "additional_clinics": 0,
        "lines": [
            {
                "line_id": "plan",
                "offer_id": "plan_plus_monthly_v1",
                "kind": "plan",
                "quantity": 1,
                "unit_amount_cents": 99900,
                "line_amount_cents": 99900,
                "credit_grant_mc": 5000,
                "settled_cents": 0,
                "reversed_cents": 0,
                "restored_cents": 0,
                "line_financial_state": "pending",
            }
        ],
    },
    separators=(",", ":"),
)
_MIGRATION_ROW = (
    "INSERT INTO billing_migrations (id, builder_id, subscription_id, "
    "renewal_date, open_by_date, cutover_by_date, target_catalog_version_id, "
    "target_offer_id, target_plan, target_amount_cents, target_snapshot, "
    "source_roster_revision) VALUES (gen_random_uuid(), :builder, "
    "gen_random_uuid(), DATE '2026-12-10', DATE '2026-12-03', "
    "DATE '2026-12-08', 'catalog-v1', 'plan_plus_monthly_v1', 'plus', 99900, "
    "CAST(:snapshot AS jsonb), 1)"
)
_RECEIPT_ROW = (
    "INSERT INTO billing_migration_receipts (id, builder_id, migration_id, "
    "operation_id, receipt_kind, status, content_sha256, actor_user_id, "
    "observed_at) VALUES (gen_random_uuid(), :builder, gen_random_uuid(), "
    "gen_random_uuid(), 'roster_import', 'observed', repeat('a', 64), "
    "gen_random_uuid(), now())"
)
_ABSENCE_ROW = (
    "INSERT INTO billing_migration_absence_reads (id, builder_id, "
    "migration_id, operation_id, read_no, convergence_limit_at, provider, "
    "external_reference, provider_event_id, provider_authority, "
    "provider_key_id, provider_operation, provider_signature, "
    "provider_observed_at, provider_expires_at, provider_snapshot_digest, "
    "provider_evidence_digest, status, actor_user_id, observed_at) VALUES "
    "(gen_random_uuid(), :builder, gen_random_uuid(), gen_random_uuid(), 1, "
    "now() - interval '2 hours', 'asaas', 'sub-g10', 'evt-g10', "
    "'asaas.api.v3', 'key-g10', 'subscription.migration.absence', "
    "repeat('a', 128), now() - interval '1 hour', now() + interval '1 hour', "
    "repeat('b', 64), repeat('c', 64), 'absent', gen_random_uuid(), now())"
)

# category -> (planting statements, the one refusal line it must produce)
_LEGACY: dict[str, tuple[tuple[str, ...], str]] = {
    "billing_migrations": (
        (_PLANT, _MIGRATION_ROW),
        "billing_migrations has 1 row(s)",
    ),
    "billing_migration_receipts": (
        (_PLANT, _RECEIPT_ROW),
        "billing_migration_receipts has 1 row(s)",
    ),
    "billing_migration_absence_reads": (
        (_PLANT, _ABSENCE_ROW),
        "billing_migration_absence_reads has 1 row(s)",
    ),
    "roster_revision_advanced": (
        (
            "UPDATE billing_roster_control SET billing_roster_revision = 1 "
            "WHERE singleton = true",
        ),
        "billing_roster_control has 1 row(s) whose revision is not 0",
    ),
    "missing_function": (
        ("DROP FUNCTION billing_evidence_timestamp(timestamp with time zone)",),
        "functions 037 drops do not exist: "
        "public.billing_evidence_timestamp(timestamp with time zone)",
    ),
    "unexpected_index": (
        ("CREATE INDEX g10_e6_index ON billing_migrations (state)",),
        "unexpected objects depend on the dropped objects: index public.g10_e6_index",
    ),
    "function_reading_a_dropped_table": (
        (
            "CREATE FUNCTION g10_e6_reader() RETURNS bigint LANGUAGE plpgsql "
            "AS $$ BEGIN RETURN (SELECT count(*) FROM billing_migrations); END $$",
        ),
        "functions outside 037's scope name a dropped object: g10_e6_reader()",
    ),
    "function_calling_a_dropped_function": (
        (
            "CREATE FUNCTION g10_e6_caller() RETURNS text LANGUAGE plpgsql "
            "AS $$ BEGIN RETURN billing_evidence_timestamp(now()); END $$",
        ),
        "functions outside 037's scope name a dropped object: g10_e6_caller()",
    ),
    "function_body_in_capitals": (
        (
            "CREATE FUNCTION g10_e6_shout() RETURNS bigint LANGUAGE plpgsql "
            "AS $$ BEGIN RETURN (SELECT count(*) FROM BILLING_MIGRATIONS); END $$",
        ),
        "functions outside 037's scope name a dropped object: g10_e6_shout()",
    ),
    "function_body_in_mixed_case": (
        (
            "CREATE FUNCTION g10_e6_mixed() RETURNS boolean LANGUAGE plpgsql "
            "AS $$ BEGIN PERFORM 1 FROM Billing_Roster_Control; RETURN true; END $$",
        ),
        "functions outside 037's scope name a dropped object: g10_e6_mixed()",
    ),
    "function_in_another_schema": (
        (
            "CREATE SCHEMA g10_e6_other",
            "CREATE FUNCTION g10_e6_other.reader() RETURNS bigint LANGUAGE plpgsql "
            "AS $$ BEGIN RETURN (SELECT count(*) FROM public.billing_migrations); "
            "END $$",
        ),
        "functions outside 037's scope name a dropped object: g10_e6_other.reader()",
    ),
    "same_name_in_another_schema": (
        (
            "CREATE SCHEMA g10_e6_other",
            "CREATE FUNCTION g10_e6_other.lock_billing_roster_statement() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "PERFORM 1 FROM public.billing_roster_control; RETURN NULL; END $$",
        ),
        "functions outside 037's scope name a dropped object: "
        "g10_e6_other.lock_billing_roster_statement()",
    ),
    "extra_column": (
        ("ALTER TABLE billing_roster_control ADD COLUMN g10_e6_note text",),
        "columns the downgrade would not restore: billing_roster_control.g10_e6_note",
    ),
    "comment": (
        ("COMMENT ON TABLE billing_migrations IS 'kept by hand'",),
        "comments the downgrade would not restore: table public.billing_migrations",
    ),
}

# A comment on each kind of dropped object: (statement, pg_identify_object).
_COMMENTS: dict[str, tuple[str, str]] = {
    "column": (
        "COMMENT ON COLUMN billing_roster_control.singleton IS 'kept'",
        "table column public.billing_roster_control.singleton",
    ),
    "constraint": (
        "COMMENT ON CONSTRAINT ck_billing_roster_control_revision "
        "ON billing_roster_control IS 'kept'",
        "table constraint ck_billing_roster_control_revision "
        "on public.billing_roster_control",
    ),
    "key_index": (
        "COMMENT ON INDEX billing_migrations_pkey IS 'kept'",
        "index public.billing_migrations_pkey",
    ),
    "plain_index": (
        "COMMENT ON INDEX ix_billing_migrations_builder_id IS 'kept'",
        "index public.ix_billing_migrations_builder_id",
    ),
    "trigger": (
        "COMMENT ON TRIGGER trg_billing_roster_control_history "
        "ON billing_roster_control IS 'kept'",
        "trigger trg_billing_roster_control_history on public.billing_roster_control",
    ),
    "function": (
        "COMMENT ON FUNCTION billing_evidence_timestamp(timestamptz) IS 'kept'",
        "function public.billing_evidence_timestamp(timestamp with time zone)",
    ),
    "row_type": (
        "COMMENT ON TYPE billing_migrations IS 'kept'",
        "type public.billing_migrations",
    ),
}

# Every kind of catalog dependent DROP would remove silently or that would
# make it fail, each alone: (statements, pg_identify_object of the dependent).
_DEPENDENTS: dict[str, tuple[tuple[str, ...], str]] = {
    "view": (
        (
            "CREATE VIEW g10_e6_view AS "
            "SELECT billing_roster_revision FROM billing_roster_control",
        ),
        'rule "_RETURN" on public.g10_e6_view',
    ),
    "foreign_key": (
        ("CREATE TABLE g10_e6_ref (id uuid REFERENCES billing_migrations (id))",),
        "table constraint g10_e6_ref_id_fkey on public.g10_e6_ref",
    ),
    "live_trigger": (
        (
            "CREATE TRIGGER g10_e6_fence BEFORE UPDATE ON billing_subscriptions "
            "FOR EACH STATEMENT EXECUTE FUNCTION lock_billing_roster_statement()",
        ),
        "trigger g10_e6_fence on public.billing_subscriptions",
    ),
    "live_check": (
        (
            "CREATE TABLE g10_e6_check (s jsonb CHECK ("
            "billing_migration_snapshot_is_valid(s, 'a', 'b', 'c', 'd', NULL, 1)))",
        ),
        "table constraint g10_e6_check_s_check on public.g10_e6_check",
    ),
    "policy": (
        ("CREATE POLICY g10_e6_policy ON billing_migrations USING (true)",),
        "policy g10_e6_policy on public.billing_migrations",
    ),
    "statistics": (
        (
            "CREATE STATISTICS g10_e6_stats ON state, target_plan "
            "FROM billing_migrations",
        ),
        "statistics object public.g10_e6_stats",
    ),
    "row_type_argument": (
        (
            "CREATE FUNCTION g10_e6_rowtype(billing_roster_control) "
            "RETURNS boolean LANGUAGE sql AS 'SELECT true'",
        ),
        "function public.g10_e6_rowtype(public.billing_roster_control)",
    ),
    "owned_sequence": (
        ("CREATE SEQUENCE g10_e6_seq OWNED BY billing_migrations.attempt_count",),
        "sequence public.g10_e6_seq",
    ),
    "extra_trigger": (
        (
            "CREATE TRIGGER g10_e6_extra BEFORE INSERT ON billing_migrations "
            "FOR EACH ROW EXECUTE FUNCTION reject_immutable_billing_row()",
        ),
        "trigger g10_e6_extra on public.billing_migrations",
    ),
    "rule": (
        (
            "CREATE RULE g10_e6_rule AS ON INSERT TO billing_roster_control "
            "DO ALSO NOTIFY g10_e6",
        ),
        "rule g10_e6_rule on public.billing_roster_control",
    ),
    "array_column": (
        ("CREATE TABLE g10_e6_arr (x billing_migrations[])",),
        "table public.g10_e6_arr",
    ),
    "domain_over_row_type": (
        ("CREATE DOMAIN g10_e6_domain AS billing_roster_control",),
        "type public.g10_e6_domain",
    ),
}


async def _plant(cluster: _Cluster, name: str, statements: tuple[str, ...]) -> None:
    await _execute(cluster, name, *statements, builder=_BUILDER, snapshot=_SNAPSHOT)


_READBACK = (_ROOT / "scripts" / "migration_037_readback.sql").read_text(
    encoding="ascii"
)


async def _readback(cluster: _Cluster, name: str, user: str | None = None) -> str:
    """The committed readback, run exactly as the runbook says: connected as
    the migration role, in a READ ONLY transaction.  Returns the refusal
    lines it predicts, joined as the migration joins them ('' = all ok)."""

    url = cluster.url(name) if user is None else cluster.url(name).set(username=user)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            read_only = await connection.exec_driver_sql("SHOW transaction_read_only")
            assert read_only.scalar_one() == "on"
            rows = (await connection.exec_driver_sql(_READBACK)).all()
            await connection.rollback()
    finally:
        await engine.dispose()
    assert [row[0] for row in rows] == list(range(1, 12))
    return "; ".join(row[2] for row in rows if row[2] != "ok")


async def _assert_refused_and_unchanged(
    baseline: _Baseline, name: str, expected: str, *, user: str | None = None
) -> None:
    """The readback predicts the refusal line for line, then the migration
    refuses with exactly that message and changes nothing."""

    assert expected.startswith(_REFUSED)
    assert await _readback(baseline.cluster, name, user) == expected[len(_REFUSED) :]
    before = await _catalog(baseline.cluster, name)
    url = baseline.cluster.url(name)
    if user is not None:
        url = url.set(username=user)
    code, output = await _alembic(url, "upgrade", "037")
    assert code != 0, output
    assert _refusal(output, _REFUSED) == expected
    assert await _catalog(baseline.cluster, name) == before
    assert before["alembic_version"] == [("036",)]


@pytest.mark.parametrize("category", sorted(_LEGACY))
async def test_each_category_alone_refuses_the_upgrade(
    baseline: _Baseline, at_036: str, category: str
) -> None:
    statements, line = _LEGACY[category]
    await _plant(baseline.cluster, at_036, statements)
    await _assert_refused_and_unchanged(baseline, at_036, _REFUSED + line)


@pytest.mark.parametrize("kind", sorted(_DEPENDENTS))
async def test_each_kind_of_dependent_refuses_the_upgrade(
    baseline: _Baseline, at_036: str, kind: str
) -> None:
    statements, identity = _DEPENDENTS[kind]
    await _plant(baseline.cluster, at_036, statements)
    await _assert_refused_and_unchanged(
        baseline,
        at_036,
        _REFUSED + f"unexpected objects depend on the dropped objects: {identity}",
    )


@pytest.mark.parametrize("kind", sorted(_COMMENTS))
async def test_a_comment_on_any_dropped_object_refuses_the_upgrade(
    baseline: _Baseline, at_036: str, kind: str
) -> None:
    statement, identity = _COMMENTS[kind]
    await _plant(baseline.cluster, at_036, (statement,))
    await _assert_refused_and_unchanged(
        baseline,
        at_036,
        _REFUSED + f"comments the downgrade would not restore: {identity}",
    )


async def test_a_raw_billing_migrations_insert_still_possible_at_036_is_caught(
    baseline: _Baseline, at_036: str
) -> None:
    """The E5 review's gap: at 036 billing_migrations has no INSERT trigger
    and accepts a raw row (no replica role, every constraint checked).  037
    counts it again instead of trusting 036's preflight."""

    subscription = uuid4()
    await _execute(
        baseline.cluster,
        at_036,
        "INSERT INTO billing_subscriptions (id, builder_id, provider, "
        "external_subscription_id, is_active, plan, billing_cycle, status) "
        "VALUES (:subscription, :builder, NULL, 'g10-e6-raw', false, 'start', "
        "'monthly', 'provider_unknown')",
        "INSERT INTO billing_migrations (id, builder_id, subscription_id, "
        "renewal_date, open_by_date, cutover_by_date, target_catalog_version_id, "
        "target_offer_id, target_plan, target_billing_cycle, target_amount_cents, "
        "target_snapshot, source_roster_revision) VALUES (gen_random_uuid(), "
        ":builder, :subscription, DATE '2026-12-10', DATE '2026-12-03', "
        "DATE '2026-12-08', 'catalog-v1', 'plan_plus_monthly_v1', 'plus', "
        "'monthly', 99900, CAST(:snapshot AS jsonb), 1)",
        builder=_BUILDER,
        subscription=subscription,
        snapshot=_SNAPSHOT,
    )
    await _assert_refused_and_unchanged(
        baseline, at_036, _REFUSED + "billing_migrations has 1 row(s)"
    )


async def _acl(cluster: _Cluster, name: str, sql: str) -> str:
    value = await _scalar(cluster, name, sql)
    return "default" if value is None else str(value)


@pytest.mark.parametrize(
    "case",
    ["table_owner", "table_grant", "column_grant", "function_revoke", "function_owner"],
)
async def test_owner_or_privileges_the_downgrade_would_not_recreate_refuse(
    baseline: _Baseline,
    roles: tuple[str, str],
    at_036: str,
    case: str,
) -> None:
    cluster = baseline.cluster
    app, owner = roles
    statement, item = {
        "table_owner": (
            f'ALTER TABLE billing_migrations OWNER TO "{owner}"',
            "table billing_migrations (owner {owner}, acl {acl})",
        ),
        "table_grant": (
            f'GRANT SELECT ON billing_roster_control TO "{app}"',
            "table billing_roster_control (owner postgres, acl {acl})",
        ),
        "column_grant": (
            f'GRANT SELECT (state) ON billing_migrations TO "{app}"',
            "table billing_migrations (owner postgres, acl {acl})",
        ),
        "function_revoke": (
            "REVOKE EXECUTE ON FUNCTION lock_billing_roster_statement() FROM PUBLIC",
            "function lock_billing_roster_statement() (owner postgres, acl {acl})",
        ),
        "function_owner": (
            f'ALTER FUNCTION lock_billing_roster_statement() OWNER TO "{owner}"',
            "function lock_billing_roster_statement() (owner {owner}, acl {acl})",
        ),
    }[case]
    await _plant(cluster, at_036, (statement,))
    table = "billing_roster_control" if case == "table_grant" else "billing_migrations"
    acl = await _acl(
        cluster,
        at_036,
        (
            "SELECT proacl::text FROM pg_proc "
            "WHERE oid = 'lock_billing_roster_statement()'::regprocedure"
            if case.startswith("function")
            else f"SELECT relacl::text FROM pg_class WHERE oid = '{table}'::regclass"
        ),
    )
    await _assert_refused_and_unchanged(
        baseline,
        at_036,
        _REFUSED
        + "owners or privileges the downgrade would not recreate as postgres: "
        + item.format(owner=owner, acl=acl),
    )


async def test_the_revision_must_be_exactly_036(
    baseline: _Baseline, at_036: str
) -> None:
    """Alembic itself only runs 037 from 036; the preflight also refuses the
    SQL run out of band (``--sql``) on another revision.  Run directly."""

    cluster = baseline.cluster
    preflight = _MIGRATION.upgrade_preflight(_MIGRATION.frozen_036())
    await _execute(cluster, at_036, "UPDATE alembic_version SET version_num = '035'")
    assert await _readback(cluster, at_036) == "alembic_version is 035, not 036"
    before = await _catalog(cluster, at_036)
    engine = create_async_engine(cluster.url(at_036), poolclass=NullPool)
    try:
        with pytest.raises(DBAPIError) as raised:
            async with engine.begin() as connection:
                await connection.execute(text(preflight))
    finally:
        await engine.dispose()
    cause = raised.value.orig.__cause__  # type: ignore[union-attr]
    assert getattr(cause, "sqlstate", None) == "55000"
    assert str(cause) == _REFUSED + "alembic_version is 035, not 036"
    assert await _catalog(cluster, at_036) == before


async def test_all_categories_are_reported_together_in_order(
    baseline: _Baseline, roles: tuple[str, str], at_036: str
) -> None:
    cluster = baseline.cluster
    app, _ = roles
    order = (
        "billing_migrations",
        "billing_migration_receipts",
        "billing_migration_absence_reads",
        "roster_revision_advanced",
        "missing_function",
        "unexpected_index",
        "function_reading_a_dropped_table",
        "extra_column",
    )
    for category in order:
        await _plant(cluster, at_036, _LEGACY[category][0])
    await _plant(
        cluster,
        at_036,
        (
            f'GRANT SELECT ON billing_roster_control TO "{app}"',
            "COMMENT ON TABLE billing_migrations IS 'kept by hand'",
            "COMMENT ON FUNCTION lock_billing_roster_statement() IS 'kept'",
        ),
    )
    acl = await _acl(
        cluster,
        at_036,
        "SELECT relacl::text FROM pg_class "
        "WHERE oid = 'billing_roster_control'::regclass",
    )
    lines = [_LEGACY[category][1] for category in order] + [
        "owners or privileges the downgrade would not recreate as postgres: "
        f"table billing_roster_control (owner postgres, acl {acl})",
        "comments the downgrade would not restore: "
        "function public.lock_billing_roster_statement(), "
        "table public.billing_migrations",
    ]
    await _assert_refused_and_unchanged(baseline, at_036, _REFUSED + "; ".join(lines))


async def test_non_legacy_rows_next_to_each_boundary_pass_and_survive(
    baseline: _Baseline, at_036: str
) -> None:
    """The roster singleton at revision 0, an ASAAS subscription, a checkout
    and an UNEXPIRED readiness receipt: 037 applies (it changes no live
    column, so no receipt changes meaning) and keeps every row; the
    downgrade needs none of them either."""

    cluster = baseline.cluster
    await _plant(
        cluster,
        at_036,
        (
            "INSERT INTO billing_subscriptions (id, builder_id, provider, "
            "external_subscription_id, is_active, plan, billing_cycle, status) "
            "VALUES (gen_random_uuid(), :builder, 'asaas', 'sub-g10-e6', true, "
            "'plus', 'monthly', 'active')",
            "INSERT INTO billing_checkout_sessions (id, builder_id, intent_kind, "
            "intent_id, attempt_no, catalog_version_id, offer_id, amount_cents, "
            "pricing_snapshot) VALUES (gen_random_uuid(), :builder, "
            "'subscription', gen_random_uuid(), 1, 'catalog-v1', "
            "'plan_plus_monthly_v1', 99900, '{}'::jsonb)",
            _PLANT,
            _readiness("smoke", None, "now() + interval '1 hour'"),
        ),
    )
    counts = (
        "SELECT ARRAY[(SELECT count(*) FROM billing_subscriptions), "
        "(SELECT count(*) FROM billing_checkout_sessions), "
        "(SELECT count(*) FROM billing_rollout_readiness_receipts)]"
    )
    url = cluster.url(at_036)
    assert await _readback(cluster, at_036) == ""
    await _migrate(url, "upgrade", "037")
    assert list(await _scalar(cluster, at_036, counts)) == [1, 1, 1]
    await _migrate(url, "downgrade", "036")
    assert list(await _scalar(cluster, at_036, counts)) == [1, 1, 1]
    assert await _scalar(cluster, at_036, "SELECT count(*) FROM builders") == 1


# ---------------------------------------------------------------------------
# Downgrade: no data needed; refuses names the restore needs.
# ---------------------------------------------------------------------------


async def test_downgrade_refuses_objects_whose_names_the_restore_needs(
    baseline: _Baseline, at_037: str
) -> None:
    cluster = baseline.cluster
    await _plant(
        cluster,
        at_037,
        (
            "CREATE TABLE billing_roster_control (note text)",
            "CREATE TYPE billing_migrations AS ENUM ('x')",
            "CREATE INDEX ix_billing_migrations_builder_id ON builders (name)",
            "CREATE FUNCTION lock_billing_roster_statement() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$",
        ),
    )
    before = await _catalog(cluster, at_037)
    code, output = await _alembic(cluster.url(at_037), "downgrade", "036")
    assert code != 0, output
    assert _refusal(output, _DOWNGRADE_REFUSED) == (
        _DOWNGRADE_REFUSED + "objects the restore would create already exist: "
        "function lock_billing_roster_statement(), "
        "relation billing_roster_control, "
        "relation ix_billing_migrations_builder_id, "
        "type billing_migrations"
    )
    assert await _catalog(cluster, at_037) == before
    assert before["alembic_version"] == [("037",)]


async def test_downgrade_revision_must_be_exactly_037(
    baseline: _Baseline, at_037: str
) -> None:
    cluster = baseline.cluster
    preflight = _MIGRATION.downgrade_preflight(_MIGRATION.frozen_036())
    await _execute(cluster, at_037, "UPDATE alembic_version SET version_num = '036'")
    before = await _catalog(cluster, at_037)
    engine = create_async_engine(cluster.url(at_037), poolclass=NullPool)
    try:
        with pytest.raises(DBAPIError) as raised:
            async with engine.begin() as connection:
                await connection.execute(text(preflight))
    finally:
        await engine.dispose()
    cause = raised.value.orig.__cause__  # type: ignore[union-attr]
    assert getattr(cause, "sqlstate", None) == "55000"
    assert str(cause) == _DOWNGRADE_REFUSED + "alembic_version is 036, not 037"
    assert await _catalog(cluster, at_037) == before


# ---------------------------------------------------------------------------
# Lock wait: SET LOCAL lock_timeout = '5s' (as migrations 024, 025 and 036).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", _UPGRADE_LOCKED)
async def test_upgrade_gives_up_after_the_lock_timeout_and_changes_nothing(
    baseline: _Baseline, at_036: str, table: str
) -> None:
    before = await _catalog(baseline.cluster, at_036)
    code, output = await _alembic_while_reading(
        baseline.cluster, at_036, table, "upgrade", "037"
    )
    _assert_lock_timeout_in_the_preflight(code, output)
    assert await _catalog(baseline.cluster, at_036) == before
    assert before["alembic_version"] == [("036",)]


@pytest.mark.parametrize("table", _DOWNGRADE_LOCKED)
async def test_downgrade_gives_up_after_the_lock_timeout_and_changes_nothing(
    baseline: _Baseline, at_037: str, table: str
) -> None:
    before = await _catalog(baseline.cluster, at_037)
    code, output = await _alembic_while_reading(
        baseline.cluster, at_037, table, "downgrade", "036"
    )
    _assert_lock_timeout_in_the_preflight(code, output)
    assert await _catalog(baseline.cluster, at_037) == before
    assert before["alembic_version"] == [("037",)]


# ---------------------------------------------------------------------------
# Grants, owners and default privileges survive the round trip.
# ---------------------------------------------------------------------------


async def test_round_trip_keeps_grants_owners_and_default_privileges(
    baseline: _Baseline, roles: tuple[str, str], at_036: str
) -> None:
    """A DML-only application role granted on every table and function and
    by default privileges (as production's migration role is set up), a
    live function with EXECUTE revoked from PUBLIC, a live function and a
    live table owned by another role.  The dropped tables and functions come
    back with the ACL the default privileges give them, which is the one they
    had: 037 -> 036 returns exactly the catalog before 037."""

    cluster = baseline.cluster
    app, owner = roles
    await _plant(
        cluster,
        at_036,
        (
            f'GRANT USAGE ON SCHEMA public TO "{app}"',
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            f'TO "{app}"',
            f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO "{app}"',
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app}"',
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f'GRANT EXECUTE ON FUNCTIONS TO "{app}"',
            "REVOKE EXECUTE ON FUNCTION validate_billing_canary_effect() FROM PUBLIC",
            f'ALTER FUNCTION protect_billing_canary_subscription() OWNER TO "{owner}"',
            f'ALTER TABLE billing_checkout_sessions OWNER TO "{owner}"',
        ),
    )
    before = await _catalog(cluster, at_036)
    relations = {row[0]: row for row in before["relations"]}
    functions = {row[0]: row for row in before["functions"]}
    # Non-vacuous: the legacy objects really carry the application grants.
    for table in _TABLES:
        assert app in relations[table][6], table
    for signature in _FUNCTIONS:
        assert app in functions[signature][3], signature
    assert relations["billing_checkout_sessions"][7] == owner
    assert await _readback(cluster, at_036) == ""
    url = cluster.url(at_036)
    await _migrate(url, "upgrade", "037")
    upgraded = await _catalog(cluster, at_036)
    for key in ("default_privileges", "schema"):
        assert upgraded[key] == before[key], key
    assert {row for row in upgraded["relations"]} == {
        row for row in before["relations"] if row[0] not in _TABLES
    }
    await _migrate(url, "downgrade", "036")
    assert await _catalog(cluster, at_036) == before


# ---------------------------------------------------------------------------
# Every table the statements touch is in the lock list.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_no_live_table_outside_the_lock_list_is_touched(
    baseline: _Baseline, at_036: str, direction: str
) -> None:
    """The statements of each direction, run in one transaction that is
    rolled back: the only live tables they lock are the ones the preflight
    locked first, in ACCESS EXCLUSIVE mode (plus the read of alembic_version)."""

    cluster = baseline.cluster
    document = _MIGRATION.frozen_036()
    if direction == "upgrade":
        statements = (
            _MIGRATION.LOCK_TIMEOUT,
            _MIGRATION.upgrade_preflight(document),
            *_MIGRATION.UPGRADE_DROPS,
        )
    else:
        await _migrate(cluster.url(at_036), "upgrade", "037")
        statements = (
            _MIGRATION.LOCK_TIMEOUT,
            _MIGRATION.downgrade_preflight(document),
            *_MIGRATION.restore_statements(document),
        )
    engine = create_async_engine(cluster.url(at_036), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            live = {
                row[0]
                for row in (
                    await connection.exec_driver_sql(
                        "SELECT relname FROM pg_class "
                        "WHERE relnamespace = 'public'::regnamespace "
                        "AND relkind IN ('r', 'p')"
                    )
                ).all()
            } - set(_TABLES)
            for statement in statements:
                await connection.exec_driver_sql(statement)
            locks = (
                await connection.exec_driver_sql(
                    "SELECT c.relname, l.mode FROM pg_locks AS l "
                    "JOIN pg_class AS c ON c.oid = l.relation "
                    "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' "
                    "AND c.relkind IN ('r', 'p')"
                )
            ).all()
            await connection.rollback()
    finally:
        await engine.dispose()
    touched = {name for name, _ in locks if name in live} - {"alembic_version"}
    exclusive = {name for name, mode in locks if mode == "AccessExclusiveLock"}
    expected = set(_DOWNGRADE_LOCKED)
    assert touched == expected
    assert expected <= exclusive
    assert {mode for name, mode in locks if name == "alembic_version"} == {
        "AccessShareLock"
    }


# ---------------------------------------------------------------------------
# Readback parity under real role setups (non-superuser owner).
# ---------------------------------------------------------------------------

# scenario -> (statements before the migrations, statements after 036,
#              who runs readback and migration, expected verdict)
# Statements run as the cluster superuser; {mig} and {app} are role names.
_ROLE_SETUPS: dict[str, tuple[tuple[str, ...], tuple[str, ...], str, str]] = {
    "plain_owner_no_grants": ((), (), "mig", "ok"),
    "table_grants_without_default_privileges": (
        (),
        (
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            'TO "{app}"',
        ),
        "mig",
        "tables",
    ),
    "schema_default_privileges_from_the_start": (
        (
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" IN SCHEMA public '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app}"',
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" IN SCHEMA public '
            'GRANT EXECUTE ON FUNCTIONS TO "{app}"',
        ),
        (),
        "mig",
        "ok",
    ),
    "global_default_privileges_from_the_start": (
        (
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" '
            'GRANT SELECT ON TABLES TO "{app}"',
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" '
            "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
        ),
        (),
        "mig",
        "ok",
    ),
    "schema_default_privileges_added_after_the_migrations": (
        (),
        (
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" IN SCHEMA public '
            'GRANT SELECT ON TABLES TO "{app}"',
        ),
        "mig",
        "tables",
    ),
    "public_execute_revoked_by_default_after_the_migrations": (
        (),
        (
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" '
            "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
        ),
        "mig",
        "functions",
    ),
    "migration_run_by_another_role": ((), (), "superuser", "tables"),
    # The same privilege in the global and the schema entries with different
    # grant options: PostgreSQL ORs them (SELECT WITH GRANT OPTION).
    "grant_option_merge_from_the_start": (
        (
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" '
            'GRANT SELECT ON TABLES TO "{app}"',
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" IN SCHEMA public '
            'GRANT SELECT ON TABLES TO "{app}" WITH GRANT OPTION',
        ),
        (),
        "mig",
        "ok",
    ),
    "grant_option_merge_after_plain_grants": (
        (),
        (
            'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{app}"',
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" '
            'GRANT SELECT ON TABLES TO "{app}" WITH GRANT OPTION',
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{mig}" IN SCHEMA public '
            'GRANT SELECT ON TABLES TO "{app}"',
        ),
        "mig",
        "tables",
    ),
}


@pytest.mark.parametrize("scenario", sorted(_ROLE_SETUPS))
async def test_readback_matches_the_migration_under_role_setups(
    baseline: _Baseline, scenario: str
) -> None:
    """A database built by a non-superuser owner role (as production's
    migration role is), with the application role granted in different ways.
    The readback, run as the role that runs the migration, is red exactly
    when the migration refuses, with the same lines; when both are green the
    round trip gives back the same catalog, access lists included."""

    cluster = baseline.cluster
    before_setup, after_setup, runner, verdict = _ROLE_SETUPS[scenario]
    suffix = uuid4().hex[:12]
    mig, app = f"g10e6_mig_{suffix}", f"g10e6_app_{suffix}"
    names = {"mig": mig, "app": app}
    async with cluster.admin.connect() as connection:
        await connection.exec_driver_sql(f'CREATE ROLE "{mig}" LOGIN')
        await connection.exec_driver_sql(f'CREATE ROLE "{app}" NOLOGIN')
    name = await cluster.fresh()
    try:
        async with cluster.admin.connect() as connection:
            await connection.exec_driver_sql(
                f'ALTER DATABASE "{name}" OWNER TO "{mig}"'
            )
        await _execute(cluster, name, *(s.format(**names) for s in before_setup))
        mig_url = cluster.url(name).set(username=mig)
        await _migrate(mig_url, "upgrade", "036")
        await _execute(
            cluster,
            name,
            "INSERT INTO builders (id, name) VALUES (:id, 'G10 E6 builder')",
            id=_BUILDER,
        )
        await _execute(cluster, name, *(s.format(**names) for s in after_setup))
        user = mig if runner == "mig" else None
        predicted = await _readback(cluster, name, user)
        if verdict == "ok":
            assert predicted == ""
            before = await _catalog(cluster, name)
            url = mig_url if user else cluster.url(name)
            await _migrate(url, "upgrade", "037")
            await _migrate(url, "downgrade", "036")
            assert await _catalog(cluster, name) == before
        else:
            role = mig if user else "postgres"
            assert predicted.startswith(
                f"owners or privileges the downgrade would not recreate as {role}: "
            ), predicted
            kind = "table" if verdict == "tables" else "function"
            assert f"{kind} " in predicted, predicted
            await _assert_refused_and_unchanged(
                baseline, name, _REFUSED + predicted, user=user
            )
    finally:
        await cluster.drop(name)
        async with cluster.admin.connect() as connection:
            for role in (app, mig):
                await connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')


# ---------------------------------------------------------------------------
# Empty default-privilege entries: PostgreSQL stores an empty merged list as
# NULL, i.e. the built-in default (superuser-owned database, run as postgres).
# ---------------------------------------------------------------------------

_LEGACY_TABLE_LIST = ", ".join(_TABLES)
_LEGACY_FUNCTION_LIST = ", ".join(_FUNCTIONS)

# scenario -> (statements, refused items or None for green); {app} = role.
_EMPTY_ENTRIES: dict[str, tuple[tuple[str, ...], set[str] | None]] = {
    "empty_global_table_entry": (
        ("ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM postgres",),
        None,
    ),
    "empty_global_table_entry_and_revoked_tables": (
        (
            "ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM postgres",
            f"REVOKE ALL ON {_LEGACY_TABLE_LIST} FROM postgres",
        ),
        {f"table {table} (owner postgres, acl {{}})" for table in _TABLES},
    ),
    "empty_global_function_entry": (
        ("ALTER DEFAULT PRIVILEGES REVOKE ALL ON FUNCTIONS FROM PUBLIC, postgres",),
        None,
    ),
    "empty_global_function_entry_and_revoked_functions": (
        (
            "ALTER DEFAULT PRIVILEGES REVOKE ALL ON FUNCTIONS FROM PUBLIC, postgres",
            f"REVOKE ALL ON FUNCTION {_LEGACY_FUNCTION_LIST} FROM PUBLIC, postgres",
        ),
        {
            f"function {signature} (owner postgres, acl {{}})"
            for signature in _FUNCTIONS
        },
    ),
    "empty_global_table_entry_with_a_schema_entry": (
        (
            "ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM postgres",
            'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO "{app}"',
            f"REVOKE ALL ON {_LEGACY_TABLE_LIST} FROM postgres",
            f'GRANT SELECT ON {_LEGACY_TABLE_LIST} TO "{{app}}"',
        ),
        None,
    ),
}


@pytest.mark.parametrize("scenario", sorted(_EMPTY_ENTRIES))
async def test_readback_matches_the_migration_with_empty_default_entries(
    baseline: _Baseline, roles: tuple[str, str], at_036: str, scenario: str
) -> None:
    """A global entry ``{}`` alone gives new objects the built-in default
    (stored NULL); with a schema entry the schema's privileges alone.  The
    readback expects what the migration's probe then observes, in both
    directions: green when the legacy objects have that list (and the round
    trip gives back the same catalog), red with the same lines when not."""

    cluster = baseline.cluster
    app, _ = roles
    statements, refused = _EMPTY_ENTRIES[scenario]
    await _plant(cluster, at_036, tuple(s.format(app=app) for s in statements))
    predicted = await _readback(cluster, at_036)
    if refused is None:
        assert predicted == ""
        before = await _catalog(cluster, at_036)
        url = cluster.url(at_036)
        await _migrate(url, "upgrade", "037")
        await _migrate(url, "downgrade", "036")
        assert await _catalog(cluster, at_036) == before
        return
    prefix = "owners or privileges the downgrade would not recreate as postgres: "
    assert predicted.startswith(prefix), predicted
    items = re.split(r"(?<=\)), (?=(?:table|function) )", predicted[len(prefix) :])
    assert set(items) == refused
    await _assert_refused_and_unchanged(baseline, at_036, _REFUSED + predicted)
