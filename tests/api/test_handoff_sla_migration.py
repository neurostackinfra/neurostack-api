"""Contract tests for migration 021's handoff SLA columns and constraints."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20260928_000001_021_add_handoff_sla_lifecycle.py"
)


class OperationRecorder:
    def __init__(self) -> None:
        self.added_columns: list[tuple[str, sa.Column[Any]]] = []
        self.created_constraints: list[tuple[str, str, str]] = []
        self.dropped_constraints: list[tuple[str, str, str | None]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.added_columns.append((table_name, column))

    def create_check_constraint(
        self, name: str, table_name: str, condition: str
    ) -> None:
        self.created_constraints.append((name, table_name, condition))

    def drop_constraint(
        self, name: str, table_name: str, type_: str | None = None
    ) -> None:
        self.dropped_constraints.append((name, table_name, type_))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))


def _load_migration() -> ModuleType:
    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        alembic_package.op = SimpleNamespace()  # type: ignore[attr-defined]

    spec = importlib.util.spec_from_file_location(
        "handoff_sla_migration_021", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migration() -> ModuleType:
    return _load_migration()


def _upgrade(migration: ModuleType) -> OperationRecorder:
    recorder = OperationRecorder()
    migration.op = recorder
    migration.upgrade()
    return recorder


def _constraint_table(recorder: OperationRecorder) -> tuple[sa.Engine, sa.Table]:
    metadata = sa.MetaData()
    added_columns = [column for _, column in recorder.added_columns]
    constraints = [
        sa.CheckConstraint(condition, name=name)
        for name, _, condition in recorder.created_constraints
    ]
    table = sa.Table(
        "handoff_records",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        *added_columns,
        *constraints,
    )
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    return engine, table


def test_revision_extends_the_current_head(migration: ModuleType) -> None:
    assert migration.revision == "021"
    assert migration.down_revision == "020"


def test_upgrade_adds_nullable_snapshots_and_internal_version(
    migration: ModuleType,
) -> None:
    recorder = _upgrade(migration)
    columns = {column.name: column for _, column in recorder.added_columns}

    assert {table for table, _ in recorder.added_columns} == {"handoff_records"}
    assert set(columns) == {"sla_target_seconds", "updated_at", "lifecycle_version"}
    assert isinstance(columns["sla_target_seconds"].type, sa.Integer)
    assert columns["sla_target_seconds"].nullable is True
    assert columns["sla_target_seconds"].server_default is None
    assert isinstance(columns["updated_at"].type, sa.DateTime)
    assert columns["updated_at"].type.timezone is True
    assert columns["updated_at"].nullable is True
    assert columns["updated_at"].server_default is None
    assert columns["lifecycle_version"].nullable is False
    assert str(columns["lifecycle_version"].server_default.arg) == "0"
    assert {name for name, _, _ in recorder.created_constraints} == {
        "ck_handoff_records_sla_target_seconds_range",
        "ck_handoff_records_lifecycle_version_nonnegative",
    }


def test_upgrade_constraints_accept_boundaries_and_preserve_legacy_nulls(
    migration: ModuleType,
) -> None:
    recorder = _upgrade(migration)
    engine, table = _constraint_table(recorder)

    with engine.begin() as connection:
        connection.execute(table.insert().values(id=1))
        connection.execute(table.insert().values(id=2, sla_target_seconds=60))
        connection.execute(table.insert().values(id=3, sla_target_seconds=7200))

    with engine.connect() as connection:
        legacy = connection.execute(sa.select(table).where(table.c.id == 1)).one()
    assert legacy.sla_target_seconds is None
    assert legacy.updated_at is None
    assert legacy.lifecycle_version == 0


@pytest.mark.parametrize(
    "values",
    [
        {"sla_target_seconds": 59},
        {"sla_target_seconds": 7201},
        {"lifecycle_version": -1},
    ],
)
def test_upgrade_constraints_reject_out_of_contract_values(
    migration: ModuleType, values: dict[str, int]
) -> None:
    recorder = _upgrade(migration)
    engine, table = _constraint_table(recorder)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(table.insert().values(id=1, **values))


def test_downgrade_removes_constraints_and_all_three_columns(
    migration: ModuleType,
) -> None:
    recorder = OperationRecorder()
    migration.op = recorder

    migration.downgrade()

    assert recorder.dropped_constraints == [
        (
            "ck_handoff_records_lifecycle_version_nonnegative",
            "handoff_records",
            "check",
        ),
        (
            "ck_handoff_records_sla_target_seconds_range",
            "handoff_records",
            "check",
        ),
    ]
    assert recorder.dropped_columns == [
        ("handoff_records", "lifecycle_version"),
        ("handoff_records", "updated_at"),
        ("handoff_records", "sla_target_seconds"),
    ]
