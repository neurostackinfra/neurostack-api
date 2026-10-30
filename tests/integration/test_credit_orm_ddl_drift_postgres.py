"""ORM x DDL drift of the credit tables (neurostack-credit-system Group 1a).

After ``alembic upgrade head`` on a fresh child database, every ``credit_*``
table the migrations created equals what ``Base.metadata`` builds from the
credit models in a scratch schema: columns (type, nullability, server default,
identity), primary key, foreign keys (target, columns, ON DELETE/ON UPDATE),
unique constraints, CHECK constraints (name and PostgreSQL's own rendering)
and plain indexes.  Both sides are reflected from PostgreSQL, so a difference
is a real drift between migration 038 and ``src/models/credit*.py``.

Needs ``POSTGRES_SMOKE_DATABASE_URL`` (a disposable loopback cluster, database
``postgres``); without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import RelationshipProperty
from sqlalchemy.pool import NullPool

import src.models  # noqa: F401  (registers every mapper on Base.metadata)
from src.models.base import Base
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

_PREFIX = "credit_"
_EXPECTED_TABLES = {
    "credit_accounts",
    "credit_capability_readiness",
    "credit_debt_origins",
    "credit_debt_settlements",
    "credit_debts",
    "credit_effects",
    "credit_enforcement_binding",
    "credit_enforcement_state",
    "credit_fact_inbox",
    "credit_ingress_claims",
    "credit_ledger_entries",
    "credit_lots",
    "credit_observational_effects",
    "credit_response_outbox",
    "credit_revision_gaps",
    # Group 4a, migration 039.
    "credit_inventory_versions",
    "credit_allocations",
    "credit_egress_deliveries",
}


def _credit_tables() -> list[str]:
    return sorted(name for name in Base.metadata.tables if name.startswith(_PREFIX))


def _reflect(sync_connection: Any, *, schema: str) -> dict[str, Any]:
    inspector = inspect(sync_connection)
    contract: dict[str, Any] = {}
    for table in _credit_tables():
        columns = {
            column["name"]: (
                str(column["type"].compile(dialect=sync_connection.dialect)),
                bool(column["nullable"]),
                column.get("default"),
                (
                    None
                    if not column.get("identity")
                    else tuple(
                        sorted(
                            (key, str(value))
                            for key, value in column["identity"].items()
                        )
                    )
                ),
            )
            for column in inspector.get_columns(table, schema=schema)
        }
        primary_key = inspector.get_pk_constraint(table, schema=schema)
        foreign_keys = sorted(
            (
                item["name"],
                tuple(item["constrained_columns"]),
                item["referred_table"],
                tuple(item["referred_columns"]),
                (item.get("options") or {}).get("ondelete"),
                (item.get("options") or {}).get("onupdate"),
            )
            for item in inspector.get_foreign_keys(table, schema=schema)
        )
        uniques = sorted(
            (item["name"], tuple(item["column_names"]))
            for item in inspector.get_unique_constraints(table, schema=schema)
        )
        checks = sorted(
            (item["name"], item["sqltext"])
            for item in inspector.get_check_constraints(table, schema=schema)
        )
        indexes = sorted(
            (item["name"], tuple(item["column_names"]), bool(item["unique"]))
            for item in inspector.get_indexes(table, schema=schema)
            if not item.get("duplicates_constraint")
        )
        contract[table] = {
            "columns": columns,
            "primary_key": (
                primary_key["name"],
                tuple(primary_key["constrained_columns"]),
            ),
            "foreign_keys": foreign_keys,
            "uniques": uniques,
            "checks": checks,
            "indexes": indexes,
        }
    return contract


def _build_and_reflect_orm(sync_connection: Any) -> dict[str, Any]:
    schema = f"credit_orm_{uuid4().hex[:12]}"
    quoted = sync_connection.dialect.identifier_preparer.quote(schema)
    sync_connection.exec_driver_sql(f"CREATE SCHEMA {quoted}")
    # Unqualified names resolve in the scratch schema first: the credit
    # tables reference each other there and ``tenants`` in public.
    sync_connection.exec_driver_sql(f"SET LOCAL search_path TO {quoted}, public")
    try:
        for table in Base.metadata.sorted_tables:
            if table.name.startswith(_PREFIX):
                table.create(sync_connection)
        return _reflect(sync_connection, schema=schema)
    finally:
        sync_connection.exec_driver_sql(f"DROP SCHEMA {quoted} CASCADE")


@pytest.fixture(scope="module")
async def migrated() -> AsyncIterator[AsyncEngine]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    engine: AsyncEngine | None = None
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        engine = create_async_engine(cluster.url(name), poolclass=NullPool)
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


async def test_the_migrated_credit_tables_are_exactly_the_mapped_ones(
    migrated: AsyncEngine,
) -> None:
    async with migrated.connect() as connection:
        version = await connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        tables = (
            (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename LIKE 'credit\\_%' "
                        "ORDER BY 1"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert version == "039"
    assert set(tables) == set(_credit_tables()) == _EXPECTED_TABLES


async def test_credit_tables_match_the_orm_metadata(migrated: AsyncEngine) -> None:
    async with migrated.connect() as connection:
        actual = await connection.run_sync(
            lambda sync_connection: _reflect(sync_connection, schema="public")
        )
        expected = await connection.run_sync(_build_and_reflect_orm)
        await connection.rollback()

    assert sorted(actual) == sorted(expected) == sorted(_EXPECTED_TABLES)
    for table in sorted(expected):
        for aspect in (
            "columns",
            "primary_key",
            "foreign_keys",
            "uniques",
            "checks",
            "indexes",
        ):
            assert actual[table][aspect] == expected[table][aspect], (table, aspect)


async def test_drift_check_is_not_vacuous(migrated: AsyncEngine) -> None:
    """The comparison sees what matters: identity, composite foreign keys to
    credit tables, the CASCADE keys to tenants and PostgreSQL-only CHECKs."""

    async with migrated.connect() as connection:
        actual = await connection.run_sync(
            lambda sync_connection: _reflect(sync_connection, schema="public")
        )
    inbox_seq = actual["credit_fact_inbox"]["columns"]["inbox_seq"]
    assert inbox_seq[0] == "BIGINT" and inbox_seq[1] is False
    assert inbox_seq[3] is not None and ("always", "True") in inbox_seq[3]
    assert (
        "fk_credit_ledger_entries_lot",
        ("builder_id", "lot_id"),
        "credit_lots",
        ("builder_id", "id"),
        "RESTRICT",
        None,
    ) in actual["credit_ledger_entries"]["foreign_keys"]
    assert actual["credit_ingress_claims"]["foreign_keys"] == [
        (
            "fk_credit_ingress_claims_tenant",
            ("tenant_id",),
            "tenants",
            ("id",),
            "CASCADE",
            None,
        )
    ]
    assert actual["credit_response_outbox"]["foreign_keys"][0][2:5] == (
        "tenants",
        ("id",),
        "CASCADE",
    )
    # History has no foreign key to builders or tenants.
    for table in _EXPECTED_TABLES - {
        "credit_ingress_claims",
        "credit_response_outbox",
    }:
        assert not {fk[2] for fk in actual[table]["foreign_keys"]} & {
            "builders",
            "tenants",
        }, table
    checks = dict(actual["credit_fact_inbox"]["checks"])
    assert "^[0-9a-f]{64}$" in checks["ck_credit_fact_inbox_digests"]
    assert actual["credit_enforcement_state"]["columns"]["id"][2] is None


def test_no_existing_model_relates_to_a_credit_table() -> None:
    for mapper in Base.registry.mappers:
        for prop in mapper.attrs:
            if isinstance(prop, RelationshipProperty):
                target = prop.mapper.local_table.name
                source = mapper.local_table.name
                assert not str(target).startswith(_PREFIX), (source, prop.key)
                assert not str(source).startswith(_PREFIX), (source, prop.key)
