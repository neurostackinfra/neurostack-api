"""Contract tests for migration 022's ``tenants.audit_extended_access`` column."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from src.models.tenant import Tenant

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20261007_000001_022_add_tenant_audit_extended_access.py"
)


class OperationRecorder:
    def __init__(self) -> None:
        self.added_columns: list[tuple[str, sa.Column[Any]]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.added_columns.append((table_name, column))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))


def _load_migration() -> ModuleType:
    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        alembic_package.op = SimpleNamespace()  # type: ignore[attr-defined]

    spec = importlib.util.spec_from_file_location(
        "tenant_audit_extended_access_migration_022", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migration() -> ModuleType:
    return _load_migration()


def _run(migration: ModuleType, step: str) -> OperationRecorder:
    recorder = OperationRecorder()
    setattr(migration, "op", recorder)
    getattr(migration, step)()
    return recorder


def _compiled_default(column: sa.Column[Any]) -> str:
    default = column.server_default
    assert isinstance(default, sa.DefaultClause)
    arg = default.arg
    if isinstance(arg, str):
        return arg
    return str(cast(sa.ClauseElement, arg).compile(dialect=postgresql.dialect()))


def test_revision_extends_migration_021(migration: ModuleType) -> None:
    assert migration.revision == "022"
    assert migration.down_revision == "021"
    assert migration.branch_labels is None
    assert migration.depends_on is None


def test_upgrade_adds_not_null_false_default_boolean(migration: ModuleType) -> None:
    recorder = _run(migration, "upgrade")

    assert [(table, column.name) for table, column in recorder.added_columns] == [
        ("tenants", "audit_extended_access")
    ]
    column = recorder.added_columns[0][1]
    assert isinstance(column.type, sa.Boolean)
    assert column.nullable is False
    assert _compiled_default(column) == "false"
    assert recorder.dropped_columns == []


def test_upgrade_backfills_existing_rows_as_false(migration: ModuleType) -> None:
    recorder = _run(migration, "upgrade")
    metadata = sa.MetaData()
    table = sa.Table(
        "tenants",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        *[column for _, column in recorder.added_columns],
    )
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(sa.insert(table).values(id=1))
        stored = connection.execute(sa.select(table.c.audit_extended_access)).scalar()

    assert stored is False


def test_downgrade_drops_the_column(migration: ModuleType) -> None:
    recorder = _run(migration, "downgrade")

    assert recorder.dropped_columns == [("tenants", "audit_extended_access")]
    assert recorder.added_columns == []


def test_orm_column_matches_the_migration_contract() -> None:
    table = cast(sa.Table, Tenant.__table__)
    column = table.c.audit_extended_access

    assert isinstance(column.type, sa.Boolean)
    assert column.nullable is False
    assert _compiled_default(column) == "false"
    assert column.default is not None
    assert column.default.arg is False  # type: ignore[attr-defined]
