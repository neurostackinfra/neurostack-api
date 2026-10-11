"""Contract tests for migration 024, which aligns ``users.role`` with the enum.

Postgres behaviour (VARCHAR to enum, no-op on an enum column, refusal on
unknown values) is proven by a disposable Postgres run; these tests pin the
decisions the migration makes from what it reads.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20261011_000001_024_align_users_role_enum.py"
)
ENUM_LABELS = ["builder", "clinic_admin", "investor", "super_admin"]


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalar_one(self) -> Any:
        assert len(self.rows) == 1
        return self.rows[0]

    def scalars(self) -> list[Any]:
        return self.rows


class FakeDatabase:
    def __init__(self, column_type: str, roles: list[str]) -> None:
        self.column_type = column_type
        self.roles = roles
        self.executed: list[str] = []

    def execute(self, clause: Any) -> FakeResult:
        sql = str(clause)
        if "information_schema.columns" in sql:
            return FakeResult([self.column_type])
        if "pg_enum" in sql:
            return FakeResult(ENUM_LABELS)
        if "DISTINCT role" in sql:
            return FakeResult(sorted(set(self.roles)))
        raise AssertionError(f"unexpected query: {sql}")


def _load(database: FakeDatabase | None, *, offline: bool = False) -> ModuleType:
    import alembic as alembic_package

    for name in ("op", "context"):
        if not hasattr(alembic_package, name):
            setattr(alembic_package, name, SimpleNamespace())
    spec = importlib.util.spec_from_file_location("users_role_enum_024", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    executed: list[str] = [] if database is None else database.executed
    module.op = SimpleNamespace(  # type: ignore[attr-defined]
        get_bind=lambda: database, execute=executed.append
    )
    module.context = SimpleNamespace(  # type: ignore[attr-defined]
        is_offline_mode=lambda: offline
    )
    module.executed = executed  # type: ignore[attr-defined]
    return module


def test_revision_extends_migration_023() -> None:
    migration = _load(None)
    assert migration.revision == "024"
    assert migration.down_revision == "023"


def test_upgrade_converts_varchar_column() -> None:
    database = FakeDatabase("varchar", ["builder", "super_admin"])
    _load(database).upgrade()
    assert database.executed == [
        "ALTER TABLE users ALTER COLUMN role TYPE userrole USING role::userrole"
    ]


def test_upgrade_leaves_enum_column_untouched() -> None:
    database = FakeDatabase("userrole", ["builder"])
    _load(database).upgrade()
    assert database.executed == []


def test_upgrade_refuses_values_outside_the_enum() -> None:
    database = FakeDatabase("varchar", ["builder", "ghost"])
    with pytest.raises(RuntimeError, match=r"\['ghost'\]"):
        _load(database).upgrade()
    assert database.executed == []


def test_offline_upgrade_and_downgrade_emit_the_alter() -> None:
    migration = _load(None, offline=True)
    migration.upgrade()
    migration.downgrade()
    assert migration.executed == [
        "ALTER TABLE users ALTER COLUMN role TYPE userrole USING role::userrole",
        "ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(50) USING role::text",
    ]


@pytest.mark.parametrize(
    ("column_type", "expected"),
    [
        (
            "userrole",
            ["ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(50) USING role::text"],
        ),
        ("varchar", []),
    ],
)
def test_downgrade_restores_varchar_only_from_the_enum(
    column_type: str, expected: list[str]
) -> None:
    database = FakeDatabase(column_type, ["builder"])
    _load(database).downgrade()
    assert database.executed == expected
