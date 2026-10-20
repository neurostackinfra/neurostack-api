"""Migration 036 (Group 10 E5, "desacoplar") on real PostgreSQL.

Owner decisions (asaas-payment-gateway wish, 2026-10-17/18): the downgrade is
real and restores 035 from DDL extracted from a fresh 035 database, never
typed by hand; the upgrade refuses while any legacy row or unexpired readiness
receipt exists; the downgrade refuses rather than fabricate roster evidence.

* fidelity: the frozen file equals what ``scripts/alembic_frozen_ddl.py``
  extracts from a brand-new 035 database (byte for byte); upgrade 036 then
  downgrade 035 gives back that brand-new 035 catalog -- functions, triggers,
  constraints, indexes, columns -- except the ordinal position of the restored
  columns, pinned exactly; 032 -> 036 on a clean database and a re-upgrade
  after the round trip both land on the same 036 catalog;
* preflight: every legacy-row category refuses the upgrade alone, with its own
  line in one exact message, and leaves the database untouched; the
  non-legacy values next to each boundary pass;
* downgrade: refused (unchanged) while subscription or readiness rows exist;
  checkout rows come back with a NULL ``migration_id`` under the restored
  checks and triggers;
* lock wait: with a plain read held open on any table the preflight locks,
  each direction gives up after its 5 s ``lock_timeout`` with SQLSTATE 55P03
  on the preflight itself and leaves the catalog untouched;
* grants: owners, ACLs, default privileges and SECURITY DEFINER of a
  non-default setup compare equal after the round trip.

Every database here is a child of the smoke cluster, created and dropped by
this module; nothing touches a shared or production database.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from scripts.alembic_frozen_ddl import FROZEN_036_PATH, extract, render
from tests.integration.test_asaas_billing_postgres import (
    _CHILD_PATTERN,
    _create_child,
    _drop_child,
    _validated_admin_url,
)

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_RESTORED_COLUMNS = {
    "billing_checkout_sessions": "migration_id",
    "billing_rollout_readiness_receipts": "roster_revision",
    "billing_subscriptions": "roster_revision",
}
_BUILDER = UUID("00000000-0000-4000-8000-0000000e5036")
_REFUSED = "migration 036 refused (nothing was changed): "
_DOWNGRADE_REFUSED = "downgrade of migration 036 refused (nothing was changed): "


# ---------------------------------------------------------------------------
# Alembic and database plumbing.
# ---------------------------------------------------------------------------


async def _spawn_alembic(
    url: URL, command: str, revision: str
) -> asyncio.subprocess.Process:
    """Start ``alembic <command> <revision>`` against *url*.

    PYTHONPATH is dropped and the installed alembic is imported before the
    repository root joins sys.path: the repository's ``alembic/`` directory
    would otherwise shadow the package.
    """

    environment = os.environ.copy()
    environment["DATABASE_URL"] = url.render_as_string(hide_password=False)
    environment.pop("PYTHONPATH", None)
    runner = """
import sys
from alembic import command
from alembic.config import Config

sys.path.insert(0, sys.argv[3])
config = Config(sys.argv[1])
config.set_main_option("script_location", sys.argv[2])
getattr(command, sys.argv[4])(config, sys.argv[5])
"""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        runner,
        str(_ROOT / "alembic.ini"),
        str(_ROOT / "alembic"),
        str(_ROOT),
        command,
        revision,
        cwd=Path("/tmp"),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def _alembic(url: URL, command: str, revision: str) -> tuple[int, str]:
    """Run ``alembic <command> <revision>``; return (exit code, output)."""

    process = await _spawn_alembic(url, command, revision)
    output, _ = await process.communicate()
    return int(process.returncode or 0), output.decode(errors="replace")


async def _migrate(url: URL, command: str, revision: str) -> None:
    code, output = await _alembic(url, command, revision)
    assert code == 0, output


def _refusal(output: str, prefix: str) -> str:
    """The refusal asyncpg raised (SQLSTATE 55000), as alembic printed it.

    Only the exception lines count: the echoed DO block also contains the
    prefix (followed by its ``%`` placeholder).
    """

    lines = [
        line.rstrip()
        for line in output.splitlines()
        if "ObjectNotInPrerequisiteStateError" in line and prefix in line
    ]
    assert lines, output
    messages = {line[line.index(prefix) :] for line in lines}
    assert len(messages) == 1, messages
    return messages.pop()


@dataclass
class _Cluster:
    admin_url: URL
    admin: AsyncEngine
    created: list[str]

    def url(self, name: str) -> URL:
        return self.admin_url.set(database=name)

    async def fresh(self) -> str:
        name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
        await _create_child(self.admin, name)
        self.created.append(name)
        return name

    async def clone(self, template: str) -> str:
        name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
        assert _CHILD_PATTERN.fullmatch(name) and _CHILD_PATTERN.fullmatch(template)
        async with self.admin.connect() as connection:
            await connection.exec_driver_sql(
                f'CREATE DATABASE "{name}" TEMPLATE "{template}"'
            )
        self.created.append(name)
        return name

    async def drop(self, name: str) -> None:
        await _drop_child(self.admin, name)
        self.created.remove(name)


@dataclass(frozen=True)
class _Baseline:
    cluster: _Cluster
    template: str  # a brand-new 035 database (plus one builder row)
    catalog_035: dict[str, Any]
    catalog_036: dict[str, Any]


async def _execute(
    cluster: _Cluster, name: str, *statements: str, **params: Any
) -> None:
    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _scalar(cluster: _Cluster, name: str, sql: str, **params: Any) -> Any:
    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(sql), params)).scalar()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Catalog fingerprint of the public schema.
# ---------------------------------------------------------------------------

_CATALOG_QUERIES = {
    "relations": """
        SELECT c.relname, c.relkind::text, c.relpersistence::text,
               c.relrowsecurity, c.relforcerowsecurity,
               coalesce(array_to_string(c.reloptions, ','), ''),
               coalesce(c.relacl::text, ''), c.relowner::regrole::text,
               coalesce(obj_description(c.oid, 'pg_class'), '')
        FROM pg_class AS c
        WHERE c.relnamespace = 'public'::regnamespace
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        ORDER BY 1
    """,
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
               a.attnotnull, coalesce(pg_get_expr(d.adbin, d.adrelid), ''),
               a.attidentity::text, a.attgenerated::text,
               coalesce(coll.collname, ''),
               coalesce(col_description(c.oid, a.attnum), ''),
               coalesce(a.attacl::text, '')
        FROM pg_attribute AS a
        JOIN pg_class AS c ON c.oid = a.attrelid
        LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        LEFT JOIN pg_collation AS coll
          ON coll.oid = a.attcollation AND a.attcollation <> 0
        WHERE c.relnamespace = 'public'::regnamespace
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY 1, 2
    """,
    "constraints": """
        SELECT con.conrelid::regclass::text, con.conname, con.contype::text,
               pg_get_constraintdef(con.oid), con.condeferrable,
               con.condeferred, con.convalidated,
               coalesce(obj_description(con.oid, 'pg_constraint'), '')
        FROM pg_constraint AS con
        WHERE con.connamespace = 'public'::regnamespace
        ORDER BY 1, 2
    """,
    "indexes": """
        SELECT tablename, indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public' ORDER BY 1, 2
    """,
    "triggers": """
        SELECT t.tgrelid::regclass::text, t.tgname, pg_get_triggerdef(t.oid),
               t.tgenabled::text,
               coalesce(obj_description(t.oid, 'pg_trigger'), '')
        FROM pg_trigger AS t
        JOIN pg_class AS c ON c.oid = t.tgrelid
        WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal
        ORDER BY 1, 2
    """,
    "functions": """
        SELECT p.oid::regprocedure::text, p.prokind::text,
               CASE WHEN p.prokind IN ('f', 'p')
                    THEN pg_get_functiondef(p.oid) ELSE '' END,
               coalesce(p.proacl::text, ''), p.proowner::regrole::text,
               p.prosecdef, coalesce(p.proconfig::text, ''),
               coalesce(obj_description(p.oid, 'pg_proc'), '')
        FROM pg_proc AS p
        WHERE p.pronamespace = 'public'::regnamespace
        ORDER BY 1
    """,
    "types": """
        SELECT t.typname, t.typtype::text,
               coalesce((SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
                         FROM pg_enum AS e WHERE e.enumtypid = t.oid), '')
        FROM pg_type AS t
        WHERE t.typnamespace = 'public'::regnamespace
          AND t.typtype IN ('e', 'd', 'r')
        ORDER BY 1
    """,
    "sequences": """
        SELECT sequencename, data_type::text, start_value, increment_by,
               max_value, min_value, cycle
        FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1
    """,
    "views": """
        SELECT viewname, definition FROM pg_views
        WHERE schemaname = 'public' ORDER BY 1
    """,
    "rules": """
        SELECT tablename, rulename, definition FROM pg_rules
        WHERE schemaname = 'public' ORDER BY 1, 2
    """,
    "policies": """
        SELECT tablename, policyname, cmd, coalesce(qual, ''),
               coalesce(with_check, '')
        FROM pg_policies WHERE schemaname = 'public' ORDER BY 1, 2
    """,
    "extensions": "SELECT extname, extversion FROM pg_extension ORDER BY 1",
    "default_privileges": """
        SELECT defaclrole::regrole::text,
               coalesce(defaclnamespace::regnamespace::text, ''),
               defaclobjtype::text, defaclacl::text
        FROM pg_default_acl ORDER BY 1, 2, 3
    """,
    "schema": """
        SELECT nspname, nspowner::regrole::text, coalesce(nspacl::text, '')
        FROM pg_namespace WHERE nspname = 'public'
    """,
    "alembic_version": "SELECT version_num FROM alembic_version",
    # Physical column order (attnum), compared separately: a real downgrade
    # appends what it re-adds.
    "column_order": """
        SELECT c.relname, array_agg(a.attname::text ORDER BY a.attnum)
        FROM pg_attribute AS a
        JOIN pg_class AS c ON c.oid = a.attrelid
        WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
          AND a.attnum > 0 AND NOT a.attisdropped
        GROUP BY c.relname ORDER BY 1
    """,
}


async def _catalog(cluster: _Cluster, name: str) -> dict[str, Any]:
    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            catalog: dict[str, Any] = {}
            for key, sql in _CATALOG_QUERIES.items():
                rows = (await connection.execute(text(sql))).all()
                catalog[key] = [tuple(row) for row in rows]
            catalog["column_order"] = {
                table: list(columns) for table, columns in catalog["column_order"]
            }
            return catalog
    finally:
        await engine.dispose()


def _without_order(catalog: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in catalog.items() if key != "column_order"}


def _assert_035_restored(restored: dict[str, Any], fresh: dict[str, Any]) -> None:
    """Equal to a brand-new 035 catalog except where the restored columns sit."""

    assert _without_order(restored) == _without_order(fresh)
    for table, order in fresh["column_order"].items():
        column = _RESTORED_COLUMNS.get(table)
        expected = (
            order if column is None else [*(c for c in order if c != column), column]
        )
        assert restored["column_order"][table] == expected, table


# ---------------------------------------------------------------------------
# Module fixtures: one brand-new 035 template and the canonical 036 catalog.
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
        await _migrate(cluster.url(template), "upgrade", "035")
        await _execute(
            cluster,
            template,
            "INSERT INTO builders (id, name) VALUES (:id, 'G10 E5 builder')",
            id=_BUILDER,
        )
        catalog_035 = await _catalog(cluster, template)
        probe = await cluster.clone(template)
        await _migrate(cluster.url(probe), "upgrade", "036")
        catalog_036 = await _catalog(cluster, probe)
        await cluster.drop(probe)
        yield _Baseline(cluster, template, catalog_035, catalog_036)
    finally:
        for name in list(reversed(cluster.created)):
            await cluster.drop(name)
        await admin.dispose()


@pytest.fixture
async def at_035(baseline: _Baseline) -> AsyncIterator[str]:
    """A disposable copy of the brand-new 035 template."""

    name = await baseline.cluster.clone(baseline.template)
    try:
        yield name
    finally:
        await baseline.cluster.drop(name)


# ---------------------------------------------------------------------------
# Fidelity.
# ---------------------------------------------------------------------------


async def test_frozen_ddl_is_exactly_what_a_fresh_035_catalog_yields(
    baseline: _Baseline,
) -> None:
    """Not typed by hand: re-extracting from a brand-new 035 database gives
    the frozen file byte for byte."""

    engine = create_async_engine(
        baseline.cluster.url(baseline.template), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            document = await connection.run_sync(extract)
    finally:
        await engine.dispose()
    assert render(document) == FROZEN_036_PATH.read_text(encoding="ascii")
    assert [entry["name"] for entry in document["constraints"]] == [
        "ck_billing_checkout_sessions_canary_exact",
        "ck_billing_checkout_sessions_intent_target",
        "fk_billing_checkout_sessions_builder_migration",
        "ck_billing_rollout_readiness_roster_revision",
        "ck_billing_subscriptions_roster_revision",
    ]


async def test_036_has_the_decoupled_shape(baseline: _Baseline) -> None:
    before, after = baseline.catalog_035, baseline.catalog_036
    dropped_columns = {(row[0], row[1]) for row in before["columns"]} - {
        (row[0], row[1]) for row in after["columns"]
    }
    assert dropped_columns == set(_RESTORED_COLUMNS.items())
    assert {(row[0], row[1]) for row in after["columns"]} <= {
        (row[0], row[1]) for row in before["columns"]
    }
    constraints_035 = {row[1]: row[3] for row in before["constraints"]}
    constraints_036 = {row[1]: row[3] for row in after["constraints"]}
    assert set(constraints_035) - set(constraints_036) == {
        "ck_billing_rollout_readiness_roster_revision",
        "ck_billing_subscriptions_roster_revision",
        "fk_billing_checkout_sessions_builder_migration",
    }
    assert set(constraints_036) == set(constraints_035) - {
        "ck_billing_rollout_readiness_roster_revision",
        "ck_billing_subscriptions_roster_revision",
        "fk_billing_checkout_sessions_builder_migration",
    }
    changed = {
        name
        for name in constraints_036
        if constraints_036[name] != constraints_035[name]
    }
    assert changed == {
        "ck_billing_checkout_sessions_canary_exact",
        "ck_billing_checkout_sessions_intent_target",
    }
    assert constraints_036["ck_billing_checkout_sessions_intent_target"] == (
        "CHECK (((((intent_kind)::text = 'subscription'::text) AND "
        "(topup_order_id IS NULL)) OR (((intent_kind)::text = 'topup'::text) "
        "AND (topup_order_id IS NOT NULL) AND (intent_id = topup_order_id))))"
    )
    # canary_exact: the 035 definition minus exactly its two migration terms.
    assert constraints_036["ck_billing_checkout_sessions_canary_exact"] == (
        constraints_035["ck_billing_checkout_sessions_canary_exact"].replace(
            " AND (migration_id IS NULL)", ""
        )
    )
    assert (
        "migration_id"
        not in constraints_036["ck_billing_checkout_sessions_canary_exact"]
    )
    triggers_035 = {row[1] for row in before["triggers"]}
    triggers_036 = {row[1] for row in after["triggers"]}
    assert triggers_035 - triggers_036 == {"trg_billing_subscriptions_roster_fence"}
    assert triggers_036 <= triggers_035
    functions_035 = {row[0]: row[2] for row in before["functions"]}
    functions_036 = {row[0]: row[2] for row in after["functions"]}
    assert set(functions_036) == set(functions_035)
    rewritten = {
        "protect_billing_canary_subscription()",
        "validate_billing_canary_checkout()",
        "validate_billing_canary_effect()",
        "validate_billing_canary_projection()",
    }
    assert {
        name for name in functions_036 if functions_036[name] != functions_035[name]
    } == rewritten
    for name in rewritten:
        for legacy in ("billing_migration", "roster", "migration_id"):
            assert legacy not in functions_036[name], (name, legacy)
    # Everything else is untouched.
    for key in ("relations", "indexes", "types", "sequences", "views", "policies"):
        assert after[key] == before[key], key
    assert after["alembic_version"] == [("036",)]


async def test_round_trip_restores_a_brand_new_035_and_reupgrades_identically(
    baseline: _Baseline, at_035: str
) -> None:
    cluster = baseline.cluster
    url = cluster.url(at_035)
    await _migrate(url, "upgrade", "036")
    assert await _catalog(cluster, at_035) == baseline.catalog_036
    await _migrate(url, "downgrade", "035")
    restored = await _catalog(cluster, at_035)
    _assert_035_restored(restored, baseline.catalog_035)
    # The restored functions/trigger/constraints are the frozen ones verbatim.
    frozen = json.loads(FROZEN_036_PATH.read_text(encoding="ascii"))
    functions = {row[0]: row[2] for row in restored["functions"]}
    for entry in frozen["functions"]:
        assert functions[f"{entry['name']}()"] == entry["statement"]
    await _migrate(url, "upgrade", "036")
    assert await _catalog(cluster, at_035) == baseline.catalog_036


async def test_upgrade_032_to_036_on_a_clean_database(baseline: _Baseline) -> None:
    """The production path on a clean cluster database: 032 (production's
    revision on 2026-10-17, one builder), then 033-036 in one run."""

    cluster = baseline.cluster
    name = await cluster.fresh()
    try:
        url = cluster.url(name)
        await _migrate(url, "upgrade", "032")
        await _execute(
            cluster,
            name,
            "INSERT INTO builders (id, name) VALUES (:id, 'G10 E5 builder')",
            id=_BUILDER,
        )
        await _migrate(url, "upgrade", "036")
        assert await _catalog(cluster, name) == baseline.catalog_036
        await _migrate(url, "downgrade", "035")
        _assert_035_restored(await _catalog(cluster, name), baseline.catalog_035)
        await _migrate(url, "upgrade", "036")
        assert await _catalog(cluster, name) == baseline.catalog_036
        assert await _scalar(cluster, name, "SELECT count(*) FROM builders") == 1
    finally:
        await cluster.drop(name)


# ---------------------------------------------------------------------------
# Upgrade preflight: legacy rows and unexpired readiness receipts.
# ---------------------------------------------------------------------------

_PLANT = "SET LOCAL session_replication_role = replica"
_MIGRATION_SNAPSHOT = json.dumps(
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


def _subscription(provider: str, revision: int, status: str = "active") -> str:
    active = "true" if status == "active" else "false"
    return (
        "INSERT INTO billing_subscriptions (id, builder_id, provider, "
        "external_subscription_id, is_active, plan, billing_cycle, status, "
        f"roster_revision) VALUES (gen_random_uuid(), :builder, {provider}, "
        f"'g10-' || md5(random()::text), {active}, 'plus', 'monthly', "
        f"'{status}', {revision})"
    )


def _readiness(kind: str, revision: int | None, expires: str) -> str:
    """A readiness row (FKs off); ``revision`` None at 036 (no such column)."""

    column, value = (
        ("", "") if revision is None else (", roster_revision", f", {revision}")
    )
    return (
        "INSERT INTO billing_rollout_readiness_receipts (id, environment, "
        "receipt_kind, rollout_revision, status, content_sha256, operation_id, "
        "operation_receipt_id, provider_receipt_id, rule_digest, bundle_digest, "
        f"app_digest, observed_at, expires_at{column}) VALUES ("
        f"gen_random_uuid(), 'test', '{kind}', 1, 'passed', repeat('a', 64), "
        "gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
        "repeat('b', 64), repeat('c', 64), repeat('d', 64), "
        f"now() - interval '2 hours', {expires}{value})"
    )


# category -> (planting statements, the one refusal line it must produce)
_LEGACY: dict[str, tuple[tuple[str, ...], str]] = {
    "billing_migrations": (
        (
            _PLANT,
            "INSERT INTO billing_migrations (id, builder_id, subscription_id, "
            "renewal_date, open_by_date, cutover_by_date, "
            "target_catalog_version_id, target_offer_id, target_plan, "
            "target_amount_cents, target_snapshot, source_roster_revision) "
            "VALUES (gen_random_uuid(), :builder, gen_random_uuid(), "
            "DATE '2026-12-10', DATE '2026-12-03', DATE '2026-12-08', "
            "'catalog-v1', 'plan_plus_monthly_v1', 'plus', 99900, "
            "CAST(:snapshot AS jsonb), 1)",
        ),
        "billing_migrations has 1 row(s)",
    ),
    "billing_migration_receipts": (
        (
            _PLANT,
            "INSERT INTO billing_migration_receipts (id, builder_id, "
            "migration_id, operation_id, receipt_kind, status, content_sha256, "
            "actor_user_id, observed_at) VALUES (gen_random_uuid(), :builder, "
            "gen_random_uuid(), gen_random_uuid(), 'roster_import', 'observed', "
            "repeat('a', 64), gen_random_uuid(), now())",
        ),
        "billing_migration_receipts has 1 row(s)",
    ),
    "billing_migration_absence_reads": (
        (
            _PLANT,
            "INSERT INTO billing_migration_absence_reads (id, builder_id, "
            "migration_id, operation_id, read_no, convergence_limit_at, "
            "provider, external_reference, provider_event_id, "
            "provider_authority, provider_key_id, provider_operation, "
            "provider_signature, provider_observed_at, provider_expires_at, "
            "provider_snapshot_digest, provider_evidence_digest, status, "
            "actor_user_id, observed_at) VALUES (gen_random_uuid(), :builder, "
            "gen_random_uuid(), gen_random_uuid(), 1, now() - interval '2 hours', "
            "'asaas', 'sub-g10', 'evt-g10', 'asaas.api.v3', 'key-g10', "
            "'subscription.migration.absence', repeat('a', 128), "
            "now() - interval '1 hour', now() + interval '1 hour', "
            "repeat('b', 64), repeat('c', 64), 'absent', gen_random_uuid(), now())",
        ),
        "billing_migration_absence_reads has 1 row(s)",
    ),
    "roster_revision_advanced": (
        (
            "UPDATE billing_roster_control SET billing_roster_revision = 1 "
            "WHERE singleton = true",
        ),
        "billing_roster_control has 1 row(s) whose revision is not 0",
    ),
    "abacatepay_subscription": (
        (_subscription("'abacatepay'", 1),),
        "billing_subscriptions has 1 row(s) whose provider is not asaas",
    ),
    "null_provider_subscription": (
        (_subscription("NULL", 1, status="legacy_cancel_pending"),),
        "billing_subscriptions has 1 row(s) whose provider is not asaas",
    ),
    "subscription_roster_revision": (
        (_subscription("'asaas'", 2),),
        "billing_subscriptions has 1 row(s) whose roster_revision is not 1",
    ),
    "checkout_migration_link": (
        (
            _PLANT,
            "INSERT INTO billing_checkout_sessions (id, builder_id, intent_kind, "
            "intent_id, migration_id, attempt_no, catalog_version_id, offer_id, "
            "amount_cents, pricing_snapshot) VALUES (gen_random_uuid(), "
            "CAST(:builder AS uuid), 'subscription', "
            "CAST(:builder AS uuid), CAST(:builder AS uuid), 1, 'catalog-v1', "
            "'plan_plus_monthly_v1', 99900, '{}'::jsonb)",
        ),
        "billing_checkout_sessions has 1 row(s) with a migration_id",
    ),
    "readiness_roster_revision": (
        (_PLANT, _readiness("backlog", 3, "now() - interval '1 hour'")),
        "billing_rollout_readiness_receipts has 1 row(s) whose roster_revision "
        "is not 0",
    ),
    "unexpired_readiness": (
        (_PLANT, _readiness("smoke", 0, "now() + interval '1 hour'")),
        "billing_rollout_readiness_receipts has 1 unexpired row(s)",
    ),
    "unexpected_dependent": (
        (
            "CREATE INDEX g10_unexpected_idx "
            "ON billing_checkout_sessions (migration_id)",
        ),
        "unexpected objects depend on the dropped columns: index " "g10_unexpected_idx",
    ),
}


async def _plant(cluster: _Cluster, name: str, statements: tuple[str, ...]) -> None:
    await _execute(
        cluster, name, *statements, builder=_BUILDER, snapshot=_MIGRATION_SNAPSHOT
    )


async def _assert_refused_and_unchanged(
    baseline: _Baseline, name: str, expected: str, *, unchanged: dict[str, Any]
) -> None:
    code, output = await _alembic(baseline.cluster.url(name), "upgrade", "036")
    assert code != 0, output
    assert _refusal(output, _REFUSED) == expected
    assert await _catalog(baseline.cluster, name) == unchanged


@pytest.mark.parametrize("category", sorted(_LEGACY))
async def test_each_legacy_category_alone_refuses_the_upgrade(
    baseline: _Baseline, at_035: str, category: str
) -> None:
    statements, line = _LEGACY[category]
    await _plant(baseline.cluster, at_035, statements)
    before = await _catalog(baseline.cluster, at_035)
    await _assert_refused_and_unchanged(
        baseline, at_035, _REFUSED + line, unchanged=before
    )
    assert before["alembic_version"] == [("035",)]


async def test_all_categories_are_reported_together_in_order(
    baseline: _Baseline, at_035: str
) -> None:
    order = (
        "billing_migrations",
        "billing_migration_receipts",
        "billing_migration_absence_reads",
        "roster_revision_advanced",
        "abacatepay_subscription",
        "subscription_roster_revision",
        "checkout_migration_link",
        "readiness_roster_revision",
        "unexpired_readiness",
        "unexpected_dependent",
    )
    for category in order:
        await _plant(baseline.cluster, at_035, _LEGACY[category][0])
    before = await _catalog(baseline.cluster, at_035)
    lines = [_LEGACY[category][1] for category in order]
    await _assert_refused_and_unchanged(
        baseline, at_035, _REFUSED + "; ".join(lines), unchanged=before
    )
    # Two non-ASAAS rows are counted once each.
    await _plant(baseline.cluster, at_035, _LEGACY["null_provider_subscription"][0])
    lines[4] = "billing_subscriptions has 2 row(s) whose provider is not asaas"
    code, output = await _alembic(baseline.cluster.url(at_035), "upgrade", "036")
    assert code != 0
    assert _refusal(output, _REFUSED) == _REFUSED + "; ".join(lines)


async def test_non_legacy_rows_next_to_each_boundary_pass_and_survive(
    baseline: _Baseline, at_035: str
) -> None:
    """An ASAAS subscription at roster revision 1, a checkout without a
    migration link, an expired readiness receipt at 0 and the roster at 0:
    036 applies and keeps every row; the subscription and readiness rows then
    block the downgrade (below)."""

    cluster = baseline.cluster
    await _plant(
        cluster,
        at_035,
        (
            _subscription("'asaas'", 1),
            _PLANT,
            _readiness("readiness", 0, "now() - interval '1 second'"),
            "INSERT INTO billing_checkout_sessions (id, builder_id, intent_kind, "
            "intent_id, attempt_no, catalog_version_id, offer_id, amount_cents, "
            "pricing_snapshot) VALUES (gen_random_uuid(), :builder, "
            "'subscription', gen_random_uuid(), 1, 'catalog-v1', "
            "'plan_plus_monthly_v1', 99900, '{}'::jsonb)",
        ),
    )
    await _migrate(cluster.url(at_035), "upgrade", "036")
    counts = await _scalar(
        cluster,
        at_035,
        "SELECT ARRAY[(SELECT count(*) FROM billing_subscriptions), "
        "(SELECT count(*) FROM billing_rollout_readiness_receipts), "
        "(SELECT count(*) FROM billing_checkout_sessions)]",
    )
    assert list(counts) == [1, 1, 1]


# ---------------------------------------------------------------------------
# Downgrade: refuse instead of fabricating roster evidence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "lines"),
    [
        (
            ("subscription",),
            [
                "billing_subscriptions has 1 row(s): restoring their roster_revision "
                "would fabricate roster evidence"
            ],
        ),
        (
            ("readiness",),
            [
                "billing_rollout_readiness_receipts has 1 row(s): restoring their "
                "roster_revision would fabricate roster evidence"
            ],
        ),
        (
            ("subscription", "readiness"),
            [
                "billing_subscriptions has 1 row(s): restoring their roster_revision "
                "would fabricate roster evidence",
                "billing_rollout_readiness_receipts has 1 row(s): restoring their "
                "roster_revision would fabricate roster evidence",
            ],
        ),
    ],
    ids=["subscription", "readiness", "both"],
)
async def test_downgrade_refuses_rows_that_would_need_invented_roster_values(
    baseline: _Baseline, at_035: str, rows: tuple[str, ...], lines: list[str]
) -> None:
    cluster = baseline.cluster
    url = cluster.url(at_035)
    await _migrate(url, "upgrade", "036")
    statements: list[str] = []
    if "subscription" in rows:
        statements.append(
            "INSERT INTO billing_subscriptions (id, builder_id, provider, "
            "external_subscription_id, is_active, plan, billing_cycle, status) "
            "VALUES (gen_random_uuid(), :builder, 'asaas', 'sub-g10-036', true, "
            "'plus', 'monthly', 'active')"
        )
    if "readiness" in rows:
        statements += [_PLANT, _readiness("smoke", None, "now() - interval '1 hour'")]
    await _plant(cluster, at_035, tuple(statements))
    before = await _catalog(cluster, at_035)
    code, output = await _alembic(url, "downgrade", "035")
    assert code != 0, output
    assert _refusal(output, _DOWNGRADE_REFUSED) == _DOWNGRADE_REFUSED + "; ".join(lines)
    assert await _catalog(cluster, at_035) == before
    assert before["alembic_version"] == [("036",)]


async def test_downgrade_keeps_checkouts_with_a_null_migration_link(
    baseline: _Baseline, at_035: str
) -> None:
    """Checkout rows need no invented value: they return with a NULL
    ``migration_id`` (no saga link, the truth for every 036 row), satisfy the
    restored CHECKs and FK, and the restored 035 triggers accept updates."""

    cluster = baseline.cluster
    url = cluster.url(at_035)
    await _migrate(url, "upgrade", "036")
    checkout = uuid4()
    await _execute(
        cluster,
        at_035,
        "INSERT INTO billing_checkout_sessions (id, builder_id, intent_kind, "
        "intent_id, attempt_no, catalog_version_id, offer_id, amount_cents, "
        "pricing_snapshot) VALUES (:id, :builder, 'subscription', "
        "gen_random_uuid(), 1, 'catalog-v1', 'plan_plus_monthly_v1', 99900, "
        "'{}'::jsonb)",
        id=checkout,
        builder=_BUILDER,
    )
    await _migrate(url, "downgrade", "035")
    _assert_035_restored(await _catalog(cluster, at_035), baseline.catalog_035)
    await _execute(
        cluster,
        at_035,
        "UPDATE billing_checkout_sessions SET state = 'failed' WHERE id = :id",
        id=checkout,
    )
    row = await _scalar(
        cluster,
        at_035,
        "SELECT ARRAY[state, coalesce(migration_id::text, 'NULL')] "
        "FROM billing_checkout_sessions WHERE id = :id",
        id=checkout,
    )
    assert list(row) == ["failed", "NULL"]


# ---------------------------------------------------------------------------
# Lock wait: SET LOCAL lock_timeout = '5s' (as migrations 024 and 025).
# ---------------------------------------------------------------------------

_UPGRADE_LOCKED = (
    "billing_subscriptions",
    "billing_checkout_sessions",
    "billing_rollout_readiness_receipts",
    "billing_migrations",
    "billing_migration_receipts",
    "billing_migration_absence_reads",
    "billing_roster_control",
)
_DOWNGRADE_LOCKED = _UPGRADE_LOCKED[:3]
# Far above alembic's start-up plus the 5 s timeout, far below "forever":
# a run without a working lock_timeout is reported, never left hanging.
_LOCK_BOUND_SECONDS = 60.0


async def _alembic_while_reading(
    cluster: _Cluster, name: str, table: str, command: str, revision: str
) -> tuple[int | None, str]:
    """Run alembic while another session holds a plain read of *table*.

    The reader keeps an ACCESS SHARE lock (an idle ``SELECT`` inside an open
    transaction).  Returns ``(None, output)`` when the run did not finish
    within the bound; it is then let finish after the reader ends.
    """

    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    connection = await engine.connect()
    process: asyncio.subprocess.Process | None = None
    code: int | None = None
    output = b""
    try:
        await connection.execute(text(f"SELECT count(*) FROM {table}"))
        process = await _spawn_alembic(cluster.url(name), command, revision)
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=_LOCK_BOUND_SECONDS
            )
            code = int(process.returncode or 0)
        except asyncio.TimeoutError:
            code = None
    finally:
        await connection.rollback()
        await connection.close()
        await engine.dispose()
    if code is None and process is not None:
        output, _ = await process.communicate()
    return code, output.decode(errors="replace")


def _assert_lock_timeout_in_the_preflight(code: int | None, output: str) -> None:
    assert code is not None, "the migration waited past the bound: no lock timeout"
    assert code != 0, output
    assert "LockNotAvailableError" in output, output  # SQLSTATE 55P03
    assert "canceling statement due to lock timeout" in output, output
    # It gave up on the preflight's LOCK TABLE, before any change.
    statements = re.findall(r"\[SQL:\s*(\S+ \S+)", output)
    assert statements and set(statements) == {"DO $preflight$"}, statements


@pytest.mark.parametrize("table", _UPGRADE_LOCKED)
async def test_upgrade_gives_up_after_the_lock_timeout_and_changes_nothing(
    baseline: _Baseline, at_035: str, table: str
) -> None:
    before = await _catalog(baseline.cluster, at_035)
    code, output = await _alembic_while_reading(
        baseline.cluster, at_035, table, "upgrade", "036"
    )
    _assert_lock_timeout_in_the_preflight(code, output)
    assert await _catalog(baseline.cluster, at_035) == before
    assert before["alembic_version"] == [("035",)]


@pytest.mark.parametrize("table", _DOWNGRADE_LOCKED)
async def test_downgrade_gives_up_after_the_lock_timeout_and_changes_nothing(
    baseline: _Baseline, at_035: str, table: str
) -> None:
    await _migrate(baseline.cluster.url(at_035), "upgrade", "036")
    before = await _catalog(baseline.cluster, at_035)
    code, output = await _alembic_while_reading(
        baseline.cluster, at_035, table, "downgrade", "035"
    )
    _assert_lock_timeout_in_the_preflight(code, output)
    assert await _catalog(baseline.cluster, at_035) == before
    assert before["alembic_version"] == [("036",)]


# ---------------------------------------------------------------------------
# Grants, owners and SECURITY DEFINER survive the round trip.
# ---------------------------------------------------------------------------


async def test_round_trip_keeps_grants_owners_and_function_acls(
    baseline: _Baseline,
) -> None:
    """Non-default privileges make the fidelity comparison non-vacuous: a
    DML-only application role with default privileges, a function whose
    EXECUTE is revoked from PUBLIC, a function and a table owned by another
    role.  Owners, ACLs, default privileges and SECURITY DEFINER (off, as
    every migration creates them) compare equal after 036 -> 035."""

    cluster = baseline.cluster
    suffix = uuid4().hex[:12]
    app, owner = f"g10_app_{suffix}", f"g10_owner_{suffix}"
    name = await cluster.clone(baseline.template)
    try:
        await _execute(
            cluster,
            name,
            f'CREATE ROLE "{app}" NOLOGIN',
            f'CREATE ROLE "{owner}" NOLOGIN',
            f'GRANT USAGE ON SCHEMA public TO "{app}"',
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            f'TO "{app}"',
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app}"',
            "REVOKE EXECUTE ON FUNCTION validate_billing_canary_effect() FROM PUBLIC",
            f'GRANT EXECUTE ON FUNCTION validate_billing_canary_effect() TO "{app}"',
            f'ALTER FUNCTION protect_billing_canary_subscription() OWNER TO "{owner}"',
            f'ALTER TABLE billing_checkout_sessions OWNER TO "{owner}"',
        )
        before = await _catalog(cluster, name)
        functions = {row[0]: row for row in before["functions"]}
        assert app in functions["validate_billing_canary_effect()"][3]
        assert functions["protect_billing_canary_subscription()"][4] == owner
        url = cluster.url(name)
        await _migrate(url, "upgrade", "036")
        upgraded = await _catalog(cluster, name)
        for key in ("relations", "default_privileges", "schema"):
            assert upgraded[key] == before[key], key
        rewritten = {row[0]: row[3:] for row in upgraded["functions"]}
        for signature, row in functions.items():
            assert rewritten[signature] == row[3:], signature
        await _migrate(url, "downgrade", "035")
        _assert_035_restored(await _catalog(cluster, name), before)
    finally:
        await cluster.drop(name)
        async with cluster.admin.connect() as connection:
            for role in (app, owner):
                await connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
