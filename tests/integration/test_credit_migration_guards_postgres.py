"""Migration 038 (neurostack-credit-system Group 1a) on real PostgreSQL.

* invariance by existing object: every object that existed at 037 -- table,
  column (type, nullability, default, identity, collation, ACL), constraint,
  index, user trigger, function, type, sequence, view, rule, policy,
  extension, default privilege, schema ACL and the physical column order --
  is identical after ``upgrade 037 -> 038``; the only difference on an
  existing object is the four internal RI triggers PostgreSQL adds on
  ``tenants`` for the two new foreign keys, named below (``_TENANTS_RI``);
  everything else 038 adds is a ``credit_*`` object or one of its four
  functions;
* downgrade guard: a single row in ANY 038 table (mutable ones and
  ``credit_enforcement_state`` included) or in ``billing_fact_acks`` refuses
  the downgrade with SQLSTATE 55000 and one exact message, changing nothing
  -- table by table and all together;
* round trip: with everything empty, ``downgrade 037`` gives back the 037
  catalog exactly (internal triggers included) and a re-upgrade lands on the
  same 038 catalog;
* lock wait: both directions start with ``SET LOCAL lock_timeout = '5s'``; a
  session holding a conflicting lock makes the migration give up with
  SQLSTATE 55P03, changing nothing.

Rows are planted under ``session_replication_role = replica`` (as the 036/037
suites do) so that each probe puts exactly one row in exactly one table.
Every database is a child of the smoke cluster, created and dropped here.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.test_asaas_billing_postgres import (
    _sqlstate,
    _validated_admin_url,
)
from tests.integration.test_migration_036_postgres import (
    _PLANT,
    _alembic,
    _alembic_while_reading,
    _assert_lock_timeout_in_the_preflight,
    _catalog,
    _Cluster,
    _execute,
    _migrate,
    _refusal,
    _scalar,
    _spawn_alembic,
)

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _ROOT / "alembic" / "versions" / "20261026_000001_038_credit_system.py"
)


def _load_migration() -> ModuleType:
    """The migration module itself (its preflight SQL, run directly).

    In-process, the repository's ``alembic/`` directory may shadow the
    package; ``op`` is only needed as a name for the module to import.
    """

    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        setattr(alembic_package, "op", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("migration_038", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION = _load_migration()
_DOWNGRADE_REFUSED = "downgrade of migration 038 refused (nothing was changed): "
_CREDIT_TABLES = (
    "credit_accounts",
    "credit_effects",
    "credit_lots",
    "credit_ledger_entries",
    "credit_debts",
    "credit_debt_origins",
    "credit_debt_settlements",
    "credit_fact_inbox",
    "credit_revision_gaps",
    "credit_capability_readiness",
    "credit_ingress_claims",
    "credit_response_outbox",
    "credit_enforcement_state",
    "credit_enforcement_binding",
    "credit_observational_effects",
)
_CREDIT_FUNCTIONS = {
    "reject_credit_history_mutation()",
    "check_credit_tenant_builder()",
    "guard_credit_revision_gap()",
    "guard_credit_enforcement_binding()",
}
# The only change 038 makes to an existing object: PostgreSQL's own RI action
# triggers on the referenced table, two per foreign key (ON DELETE CASCADE,
# ON UPDATE NO ACTION).  Keyed by (table, constraint, function); their names
# are RI_ConstraintTrigger_a_<oid>.
_TENANTS_RI = Counter(
    {
        ("tenants", "fk_credit_ingress_claims_tenant", "RI_FKey_cascade_del"): 1,
        ("tenants", "fk_credit_ingress_claims_tenant", "RI_FKey_noaction_upd"): 1,
        ("tenants", "fk_credit_response_outbox_tenant", "RI_FKey_cascade_del"): 1,
        ("tenants", "fk_credit_response_outbox_tenant", "RI_FKey_noaction_upd"): 1,
    }
)
_INTERNAL_TRIGGERS = """
    SELECT t.tgrelid::regclass::text, coalesce(con.conname, ''),
           p.proname::text, t.tgtype, t.tgenabled::text, t.tgdeferrable,
           t.tginitdeferred, t.tgname
    FROM pg_trigger AS t
    JOIN pg_class AS c ON c.oid = t.tgrelid
    JOIN pg_proc AS p ON p.oid = t.tgfoid
    LEFT JOIN pg_constraint AS con ON con.oid = t.tgconstraint
    WHERE c.relnamespace = 'public'::regnamespace AND t.tgisinternal
"""
_SHA = "'" + "a" * 64 + "'"
# One minimal row per guarded table, valid for its CHECKs (foreign keys and
# triggers are off while planting, so each row is alone in its table).
_ROWS = {
    "credit_accounts": (
        "INSERT INTO credit_accounts (id, builder_id) "
        "VALUES (gen_random_uuid(), gen_random_uuid())"
    ),
    "credit_effects": (
        "INSERT INTO credit_effects (id, builder_id, effect_id, effect_kind) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), 'probe-grant', 'grant')"
    ),
    "credit_lots": (
        "INSERT INTO credit_lots (id, builder_id, account_id, lot_kind, order_id, "
        "payment_id, paid_at, granted_mc, expires_at, grant_effect_id) VALUES "
        "(gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'topup', "
        "gen_random_uuid(), 'pay_probe', DATE '2026-10-26', 500000, "
        "TIMESTAMPTZ '2027-04-26 03:00:00+00', 'probe-grant')"
    ),
    "credit_ledger_entries": (
        "INSERT INTO credit_ledger_entries (id, builder_id, account_id, effect_id, "
        "entry_type, amount_mc, occurred_at) VALUES (gen_random_uuid(), "
        "gen_random_uuid(), gen_random_uuid(), 'probe-trace', 'debt', 1000, now())"
    ),
    "credit_debts": (
        "INSERT INTO credit_debts (id, builder_id, account_id, debt_source_kind, "
        "debt_source_effect_id, amount_mc) VALUES (gen_random_uuid(), "
        "gen_random_uuid(), gen_random_uuid(), 'consumption', 'probe-trace', 1000)"
    ),
    "credit_debt_origins": (
        "INSERT INTO credit_debt_origins (builder_id, debt_id, consumption_entry_id) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid())"
    ),
    "credit_debt_settlements": (
        "INSERT INTO credit_debt_settlements (id, builder_id, debt_id, "
        "settling_grant_lot_id, amount_mc, effect_id) VALUES (gen_random_uuid(), "
        "gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 1000, "
        "'probe-settlement')"
    ),
    "credit_fact_inbox": (
        "INSERT INTO credit_fact_inbox (id, producer, event_id, schema_version, "
        "aggregate_kind, aggregate_id, financial_revision, fact_sha256, "
        "bundle_digest, schema_digest, outcome) VALUES (gen_random_uuid(), "
        "'asaas-payment-gateway', gen_random_uuid(), 'billing.fact.v1', "
        f"'topup_order', gen_random_uuid(), 1, {_SHA}, {_SHA}, {_SHA}, 'accepted')"
    ),
    "credit_revision_gaps": (
        "INSERT INTO credit_revision_gaps (aggregate_kind, aggregate_id, "
        "first_missing_revision) VALUES ('topup_order', gen_random_uuid(), 2)"
    ),
    "credit_capability_readiness": (
        "INSERT INTO credit_capability_readiness (id, schema_version, environment, "
        "billing_fact_schema_digest, inbox_high_watermark, reconciliation_complete, "
        "readback_complete, has_open_gap, has_backlog, has_alert) VALUES "
        f"(gen_random_uuid(), 'credit_capability_ready.v1', 'test', {_SHA}, 0, "
        "false, false, false, false, false)"
    ),
    "credit_ingress_claims": (
        "INSERT INTO credit_ingress_claims (id, tenant_id, source, principal_id, "
        "idempotency_key, interaction_id, payload_sha256, decision_version, mode) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), 'events', 'probe-principal', "
        f"'probe-key', gen_random_uuid(), {_SHA}, 1, 'observational')"
    ),
    "credit_response_outbox": (
        "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, payload) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), 'probe-trace', '{}'::jsonb)"
    ),
    "credit_enforcement_state": "INSERT INTO credit_enforcement_state (id) VALUES (1)",
    "credit_enforcement_binding": (
        "INSERT INTO credit_enforcement_binding (id, builder_id, subscription_id, "
        "subscription_cycle_id, cycle_start, cycle_end, financial_revision, "
        "grant_effect_id, status) VALUES (gen_random_uuid(), gen_random_uuid(), "
        "gen_random_uuid(), gen_random_uuid(), DATE '2026-10-01', "
        "DATE '2026-11-01', 1, 'probe-grant', 'active')"
    ),
    "credit_observational_effects": (
        "INSERT INTO credit_observational_effects (id, tenant_id, effect_id, "
        "effect_kind, would_debit_mc) VALUES (gen_random_uuid(), gen_random_uuid(), "
        "'probe-trace', 'trace', 1000)"
    ),
    "billing_fact_acks": (
        "INSERT INTO billing_fact_acks (id, outbox_event_id, event_id, "
        "schema_version, outcome, inbox_event_id, fact_sha256, bundle_digest, "
        "deployment_digest, effects_digest, acknowledged_at) VALUES "
        "(gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
        f"'billing.fact.ack.v1', 'accepted', gen_random_uuid(), {_SHA}, {_SHA}, "
        f"{_SHA}, {_SHA}, now())"
    ),
}
_GUARDED = (*_CREDIT_TABLES, "billing_fact_acks")


@dataclass(frozen=True)
class _Baseline:
    cluster: _Cluster
    template_037: str
    template_038: str
    catalog_037: dict[str, Any]
    catalog_038: dict[str, Any]
    internal_037: list[tuple[Any, ...]]
    internal_038: list[tuple[Any, ...]]


async def _internal_triggers(cluster: _Cluster, name: str) -> list[tuple[Any, ...]]:
    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text(_INTERNAL_TRIGGERS))).all()
            return [tuple(row) for row in rows]
    finally:
        await engine.dispose()


def _shape(rows: list[tuple[Any, ...]]) -> Counter[tuple[Any, ...]]:
    """Internal triggers without their OID-derived names."""

    return Counter(row[:-1] for row in rows)


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
        template_037 = await cluster.fresh()
        await _migrate(cluster.url(template_037), "upgrade", "037")
        # A builder and a tenant under it: existing tables with rows.
        await _execute(
            cluster,
            template_037,
            "INSERT INTO builders (id, name) VALUES "
            "('00000000-0000-4000-8000-000000c0038a', 'G1a builder')",
            "INSERT INTO tenants (id, name, api_key_hash, is_active, builder_id) "
            "VALUES ('00000000-0000-4000-8000-000000c0038b', 'G1a clinic', "
            "'g1a-clinic-hash', true, '00000000-0000-4000-8000-000000c0038a')",
        )
        catalog_037 = await _catalog(cluster, template_037)
        internal_037 = await _internal_triggers(cluster, template_037)
        template_038 = await cluster.clone(template_037)
        await _migrate(cluster.url(template_038), "upgrade", "038")
        catalog_038 = await _catalog(cluster, template_038)
        internal_038 = await _internal_triggers(cluster, template_038)
        yield _Baseline(
            cluster,
            template_037,
            template_038,
            catalog_037,
            catalog_038,
            internal_037,
            internal_038,
        )
    finally:
        for name in list(reversed(cluster.created)):
            await cluster.drop(name)
        await admin.dispose()


@pytest.fixture
async def at_038(baseline: _Baseline) -> AsyncIterator[str]:
    name = await baseline.cluster.clone(baseline.template_038)
    try:
        yield name
    finally:
        await baseline.cluster.drop(name)


@pytest.fixture
async def at_037(baseline: _Baseline) -> AsyncIterator[str]:
    name = await baseline.cluster.clone(baseline.template_037)
    try:
        yield name
    finally:
        await baseline.cluster.drop(name)


# ---------------------------------------------------------------------------
# Invariance by existing object.
# ---------------------------------------------------------------------------

# Identity columns of each _catalog() row kind (the rest is its definition).
_KEYS = {
    "relations": 1,
    "columns": 2,
    "constraints": 2,
    "indexes": 2,
    "triggers": 2,
    "functions": 1,
    "types": 1,
    "sequences": 1,
    "views": 1,
    "rules": 2,
    "policies": 2,
    "extensions": 1,
    "default_privileges": 3,
    "schema": 1,
}


def _keyed(rows: list[tuple[Any, ...]], width: int) -> dict[tuple[Any, ...], Any]:
    keyed = {tuple(row[:width]): row for row in rows}
    assert len(keyed) == len(rows)
    return keyed


def _is_credit_object(kind: str, key: tuple[Any, ...]) -> bool:
    if kind == "functions":
        return str(key[0]) in _CREDIT_FUNCTIONS
    if kind == "sequences" or kind == "relations":
        return str(key[0]).startswith("credit_")
    if kind in ("columns", "constraints", "indexes", "triggers"):
        return str(key[0]).startswith("credit_")
    return False


async def test_upgrade_changes_no_existing_object(baseline: _Baseline) -> None:
    before, after = baseline.catalog_037, baseline.catalog_038
    assert set(before) == set(after) == {*_KEYS, "alembic_version", "column_order"}
    assert before["alembic_version"] == [("037",)]
    assert after["alembic_version"] == [("038",)]
    for kind, width in _KEYS.items():
        old = _keyed(before[kind], width)
        new = _keyed(after[kind], width)
        for key, row in old.items():
            assert new.get(key) == row, (kind, key)
        added = set(new) - set(old)
        assert all(_is_credit_object(kind, key) for key in added), (kind, added)
    for table, order in before["column_order"].items():
        assert after["column_order"][table] == order, table
    assert set(after["column_order"]) - set(before["column_order"]) == set(
        _CREDIT_TABLES
    )


async def test_what_038_adds_is_exactly_its_tables_and_functions(
    baseline: _Baseline,
) -> None:
    before = {row[0] for row in baseline.catalog_037["relations"] if row[1] == "r"}
    after = {row[0] for row in baseline.catalog_038["relations"] if row[1] == "r"}
    assert after - before == set(_CREDIT_TABLES)
    assert before <= after
    functions_before = {row[0] for row in baseline.catalog_037["functions"]}
    functions_after = {row[0] for row in baseline.catalog_038["functions"]}
    assert functions_after - functions_before == _CREDIT_FUNCTIONS
    sequences = {row[0] for row in baseline.catalog_038["sequences"]} - {
        row[0] for row in baseline.catalog_037["sequences"]
    }
    assert sequences == {"credit_fact_inbox_inbox_seq_seq"}
    # No user trigger lands on an existing table.
    new_triggers = {(row[0], row[1]) for row in baseline.catalog_038["triggers"]} - {
        (row[0], row[1]) for row in baseline.catalog_037["triggers"]
    }
    assert {table for table, _ in new_triggers} <= set(_CREDIT_TABLES)


async def test_the_only_existing_object_change_is_the_tenants_ri_triggers(
    baseline: _Baseline,
) -> None:
    existing = {row[0] for row in baseline.catalog_037["relations"]}
    before = _shape(baseline.internal_037)
    after_on_existing = _shape(
        [row for row in baseline.internal_038 if row[0] in existing]
    )
    added = after_on_existing - before
    assert before - after_on_existing == Counter()
    assert Counter(key[:3] for key in added.elements()) == _TENANTS_RI
    names = [
        row[-1]
        for row in baseline.internal_038
        if row[0] == "tenants"
        and row[1]
        in {"fk_credit_ingress_claims_tenant", "fk_credit_response_outbox_tenant"}
    ]
    assert len(names) == 4
    assert all(re.fullmatch(r"RI_ConstraintTrigger_a_[0-9]+", name) for name in names)
    # Each new foreign key also checks its own (new) referencing table.
    referencing = Counter(
        (row[0], row[1], row[2])
        for row in baseline.internal_038
        if row[1]
        in {"fk_credit_ingress_claims_tenant", "fk_credit_response_outbox_tenant"}
        and row[0] != "tenants"
    )
    assert referencing == Counter(
        {
            ("credit_ingress_claims", "fk_credit_ingress_claims_tenant", name): 1
            for name in ("RI_FKey_check_ins", "RI_FKey_check_upd")
        }
        | {
            ("credit_response_outbox", "fk_credit_response_outbox_tenant", name): 1
            for name in ("RI_FKey_check_ins", "RI_FKey_check_upd")
        }
    )
    # No FK from a credit table references any existing table but tenants.
    credit_fk_targets = {
        row[3].split("REFERENCES ", 1)[1].split("(", 1)[0]
        for row in baseline.catalog_038["constraints"]
        if row[0].startswith("credit_") and row[2] == "f"
    }
    assert credit_fk_targets == {
        "tenants",
        "credit_accounts",
        "credit_effects",
        "credit_lots",
        "credit_ledger_entries",
        "credit_debts",
    }


# ---------------------------------------------------------------------------
# Downgrade guard.
# ---------------------------------------------------------------------------


async def _plant(cluster: _Cluster, name: str, tables: tuple[str, ...]) -> None:
    await _execute(cluster, name, _PLANT, *(_ROWS[table] for table in tables))


async def _assert_downgrade_refused(
    cluster: _Cluster, name: str, tables: tuple[str, ...]
) -> None:
    before = await _catalog(cluster, name)
    internal = await _internal_triggers(cluster, name)
    code, output = await _alembic(cluster.url(name), "downgrade", "037")
    assert code != 0, output
    assert _refusal(output, _DOWNGRADE_REFUSED) == (
        f"{_DOWNGRADE_REFUSED}tables that have rows: {', '.join(tables)}"
    )
    assert await _catalog(cluster, name) == before
    assert await _internal_triggers(cluster, name) == internal
    assert before["alembic_version"] == [("038",)]
    for table in tables:
        assert await _scalar(cluster, name, f"SELECT count(*) FROM {table}") == 1


@pytest.mark.parametrize("table", _GUARDED)
async def test_one_row_in_any_guarded_table_refuses_the_downgrade(
    baseline: _Baseline, at_038: str, table: str
) -> None:
    await _plant(baseline.cluster, at_038, (table,))
    await _assert_downgrade_refused(baseline.cluster, at_038, (table,))


async def test_every_occupied_table_is_reported_together_in_order(
    baseline: _Baseline, at_038: str
) -> None:
    await _plant(baseline.cluster, at_038, _GUARDED)
    await _assert_downgrade_refused(baseline.cluster, at_038, _GUARDED)


async def test_the_preflight_refuses_a_revision_other_than_038(
    baseline: _Baseline, at_038: str
) -> None:
    """Alembic itself never runs 038's downgrade at another revision, so the
    preflight block is run directly, with ``alembic_version`` rewritten."""

    cluster = baseline.cluster
    await _execute(cluster, at_038, "UPDATE alembic_version SET version_num = '037'")
    before = await _catalog(cluster, at_038)
    engine = create_async_engine(cluster.url(at_038), poolclass=NullPool)
    try:
        with pytest.raises(DBAPIError) as info:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(_MIGRATION.LOCK_TIMEOUT)
                await connection.exec_driver_sql(_MIGRATION.downgrade_preflight())
    finally:
        await engine.dispose()
    assert _sqlstate(info.value) == "55000"
    assert f"{_DOWNGRADE_REFUSED}alembic_version is 037, not 038" in str(info.value)
    assert await _catalog(cluster, at_038) == before


async def test_empty_round_trip_restores_037_exactly_and_reupgrades(
    baseline: _Baseline, at_038: str
) -> None:
    cluster = baseline.cluster
    await _migrate(cluster.url(at_038), "downgrade", "037")
    assert await _catalog(cluster, at_038) == baseline.catalog_037
    assert _shape(await _internal_triggers(cluster, at_038)) == _shape(
        baseline.internal_037
    )
    await _migrate(cluster.url(at_038), "upgrade", "038")
    assert await _catalog(cluster, at_038) == baseline.catalog_038
    assert _shape(await _internal_triggers(cluster, at_038)) == _shape(
        baseline.internal_038
    )


async def test_existing_rows_survive_the_round_trip(
    baseline: _Baseline, at_038: str
) -> None:
    cluster = baseline.cluster
    await _migrate(cluster.url(at_038), "downgrade", "037")
    await _migrate(cluster.url(at_038), "upgrade", "038")
    assert (
        await _scalar(
            cluster,
            at_038,
            "SELECT count(*) FROM tenants WHERE builder_id = "
            "'00000000-0000-4000-8000-000000c0038a'",
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Lock wait.
# ---------------------------------------------------------------------------

_LOCK_BOUND_SECONDS = 60.0


@pytest.mark.parametrize(
    "table", ["tenants", "credit_ledger_entries", "credit_enforcement_state"]
)
async def test_downgrade_gives_up_after_the_lock_timeout_and_changes_nothing(
    baseline: _Baseline, at_038: str, table: str
) -> None:
    before = await _catalog(baseline.cluster, at_038)
    code, output = await _alembic_while_reading(
        baseline.cluster, at_038, table, "downgrade", "037"
    )
    _assert_lock_timeout_in_the_preflight(code, output)
    assert await _catalog(baseline.cluster, at_038) == before


async def _alembic_while_holding(
    cluster: _Cluster, name: str, statements: tuple[str, ...], revision: str
) -> tuple[int | None, str]:
    """Run alembic while another session keeps *statements* uncommitted."""

    command = "upgrade" if revision == "038" else "downgrade"
    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    connection = await engine.connect()
    process: asyncio.subprocess.Process | None = None
    code: int | None = None
    output = b""
    try:
        for statement in statements:
            await connection.execute(text(statement))
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


async def test_an_uncommitted_ack_blocks_the_downgrade_until_the_lock_timeout(
    baseline: _Baseline, at_038: str
) -> None:
    """``billing_fact_acks`` is locked in SHARE mode: an ack being written
    concurrently cannot slip in behind the emptiness check."""

    before = await _catalog(baseline.cluster, at_038)
    code, output = await _alembic_while_holding(
        baseline.cluster, at_038, (_PLANT, _ROWS["billing_fact_acks"]), "037"
    )
    _assert_lock_timeout_in_the_preflight(code, output)
    assert await _catalog(baseline.cluster, at_038) == before


async def test_upgrade_gives_up_after_the_lock_timeout_and_changes_nothing(
    baseline: _Baseline, at_037: str
) -> None:
    """Creating the foreign keys to ``tenants`` waits for a writer there."""

    before = await _catalog(baseline.cluster, at_037)
    code, output = await _alembic_while_holding(
        baseline.cluster,
        at_037,
        ("LOCK TABLE tenants IN ROW EXCLUSIVE MODE",),
        "038",
    )
    assert code is not None, "the migration waited past the bound: no lock timeout"
    assert code != 0, output
    assert "LockNotAvailableError" in output, output
    assert "canceling statement due to lock timeout" in output, output
    assert await _catalog(baseline.cluster, at_037) == before
    assert before["alembic_version"] == [("037",)]
