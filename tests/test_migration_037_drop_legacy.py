"""Offline contract of migration 037 (Group 10 E6, "remover objetos legados").

PostgreSQL behaviour (preflight, round trips, fidelity against a brand-new
036 database, lock wait, privileges) is proven in
``tests/integration/test_migration_037_postgres.py``; these checks need no
database: the frozen 036 DDL is the pinned file and matches the migration's
scope, every statement passes the migration's bind-parameter guard, a changed
byte is refused before any SQL in both directions, nothing cascades, the
preflights lock exactly the tables each direction touches before anything
else, and the offline SQL carries the preflight first and the frozen
statements in order.  The ORM no longer maps the dropped tables.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable

import pytest
import sqlalchemy as sa

from scripts.alembic_frozen_ddl import FROZEN_037_PATH, SCOPE_037, render
from tests.alembic_graph import current_head, migration_revisions
from tests.scripts.test_production_dockerfile_frontend import (
    _copies,
    _stage,
    docker_ignores,
)

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = (
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
    "billing_migration_snapshot_is_valid",
    "lock_billing_roster_statement",
    "validate_billing_migration_receipt",
    "validate_billing_migration_receipt_commit",
    "protect_bound_billing_migration_target",
    "protect_billing_roster_control",
    "validate_billing_provider_evidence",
    "billing_evidence_timestamp",
    "billing_provider_evidence_digest",
    "validate_billing_migration_absence_read",
)
_UPGRADE_LOCKED = {
    *_TABLES,
    "builders",
    "billing_subscriptions",
    "billing_checkout_sessions",
}
_DOWNGRADE_LOCKED = {"builders", "billing_subscriptions", "billing_checkout_sessions"}
_READBACK = _ROOT / "scripts" / "migration_037_readback.sql"
_LOCK_STATEMENT = re.compile(
    r"\bLOCK TABLE (?P<tables>[a-z_,\s]+?)\s+IN (?P<mode>[A-Z ]+?) MODE;"
)


def _load() -> ModuleType:
    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        setattr(alembic_package, "op", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("migration_037", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load()


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return json.loads(FROZEN_037_PATH.read_bytes())


def test_037_revises_036_and_038_follows_it(migration: ModuleType) -> None:
    assert (migration.revision, migration.down_revision) == ("037", "036")
    revisions = migration_revisions()
    assert revisions[_MIGRATION.name] == ("037", "036")
    # neurostack-credit-system Group 1a: 038 (the additive credit schema) is
    # the only child of 037.
    assert [rev for rev, parent in revisions.values() if parent == "037"] == ["038"]
    assert current_head() == "038"


def test_scope_is_the_four_legacy_tables_and_their_ten_functions(
    migration: ModuleType,
) -> None:
    """The drop set, spelled out once more by hand: the migration's
    constants, the extractor's scope and this list must agree."""

    assert migration.DROPPED_TABLES == _TABLES == SCOPE_037.tables
    names = tuple(signature.split("(")[0] for signature in migration.DROPPED_FUNCTIONS)
    assert names == _FUNCTIONS == SCOPE_037.functions
    assert SCOPE_037.rows == (
        ("billing_roster_control", ("singleton", "billing_roster_revision")),
    )
    assert migration.REFERENCED_TABLES == (
        "builders",
        "billing_subscriptions",
        "billing_checkout_sessions",
    )
    # Live helpers other tables still use are not dropped.
    for live in ("billing_snapshot_has_no_pii", "reject_immutable_billing_row"):
        assert all(live not in signature for signature in migration.DROPPED_FUNCTIONS)


def test_frozen_file_is_pinned_rendered_and_outside_versions(
    migration: ModuleType, document: dict[str, Any]
) -> None:
    raw = FROZEN_037_PATH.read_bytes()
    assert migration.FROZEN_036 == FROZEN_037_PATH
    assert hashlib.sha256(raw).hexdigest() == migration.FROZEN_036_SHA256
    assert FROZEN_037_PATH.parent == _ROOT / "alembic" / "frozen"
    # The exact bytes the extractor renders (stable order, ASCII, newline).
    assert render(document) == raw.decode("ascii")
    assert document["revision"] == "036"
    assert [entry["table"] for entry in document["tables"]] == list(_TABLES)
    assert [entry["signature"] for entry in document["functions"]] == list(
        migration.DROPPED_FUNCTIONS
    )
    assert [entry["name"] for entry in document["functions"]] == list(_FUNCTIONS)
    counts = {kind: len(document[kind]) for kind in migration._KINDS}
    assert counts == {
        "tables": 4,
        "functions": 10,
        "constraints": 38,
        "indexes": 2,
        "rows": 1,
        "triggers": 12,
    }
    # Constraints restore in scope-table order, foreign keys last.
    foreign = [
        entry["statement"].find(" FOREIGN KEY ") > 0
        for entry in document["constraints"]
    ]
    assert foreign == sorted(foreign) and sum(foreign) == 5
    assert document["rows"] == [
        {
            "table": "billing_roster_control",
            "statement": "INSERT INTO public.billing_roster_control "
            "(singleton, billing_roster_revision) VALUES ('true', '0')",
        }
    ]
    # The legacy receipt kinds live only in the dropped CHECK.
    (kinds,) = (
        entry["statement"]
        for entry in document["constraints"]
        if entry["name"] == "ck_billing_migration_receipts_kind"
    )
    for kind in ("roster_import", "roster_cutover", "roster_disposal"):
        assert f"'{kind}'" in kinds


def test_frozen_statements_restore_in_order_and_pass_the_bind_guard(
    migration: ModuleType, document: dict[str, Any]
) -> None:
    statements = migration.frozen_036_statements()
    expected = [
        entry["statement"]
        for kind in (
            "tables",
            "functions",
            "constraints",
            "indexes",
            "rows",
            "triggers",
        )
        for entry in document[kind]
    ]
    assert list(statements) == expected
    assert len(statements) == 67
    for statement in statements:
        assert not sa.text(statement)._bindparams, statement[:80]
    # Tables precede the PL/pgSQL functions that declare their %ROWTYPE.
    first_function = statements.index(document["functions"][0]["statement"])
    assert all(
        statements.index(entry["statement"]) < first_function
        for entry in document["tables"]
    )
    timestamp = statements.index(document["functions"][7]["statement"])
    digest = statements.index(document["functions"][8]["statement"])
    assert "billing_evidence_timestamp(" in document["functions"][7]["signature"]
    assert timestamp < digest


_TAMPERS: dict[str, Callable[[bytes], bytes]] = {
    "one_character": lambda raw: raw.replace(
        b"billing_roster_revision >= 0", b"billing_roster_revision >= 1", 1
    ),
    "truncated": lambda raw: raw[:-1],
    "trailing_byte": lambda raw: raw + b" ",
}


@pytest.mark.parametrize("tamper", sorted(_TAMPERS))
def test_a_changed_frozen_byte_is_refused_before_any_sql(
    migration: ModuleType, tamper: str
) -> None:
    raw = FROZEN_037_PATH.read_bytes()
    changed = _TAMPERS[tamper](raw)
    assert changed != raw
    with pytest.raises(RuntimeError, match="the frozen 036 DDL was changed"):
        migration.frozen_036_statements(changed)
    with pytest.raises(RuntimeError, match="the frozen 036 DDL was changed"):
        migration.frozen_036(changed)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_a_changed_frozen_file_stops_either_direction_before_any_statement(
    migration: ModuleType, direction: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both directions read the file: the downgrade restores it, the upgrade
    derives the allowlist of what it may drop from it."""

    executed: list[str] = []
    monkeypatch.setattr(
        migration, "op", SimpleNamespace(execute=lambda c: executed.append(str(c)))
    )
    monkeypatch.setattr(migration, "FROZEN_036_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="the frozen 036 DDL was changed"):
        getattr(migration, direction)()
    assert executed == []


def _executed(migration: ModuleType, direction: str) -> list[str]:
    """The statements ``upgrade()``/``downgrade()`` hand to ``op.execute``."""

    executed: list[str] = []
    original = migration.op
    migration.op = SimpleNamespace(execute=lambda clause: executed.append(str(clause)))
    try:
        getattr(migration, direction)()
    finally:
        migration.op = original
    return executed


def test_upgrade_drops_exactly_the_scope_and_rewrites_nothing(
    migration: ModuleType,
) -> None:
    executed = _executed(migration, "upgrade")
    assert len(executed) == 4
    assert executed[2] == (
        "DROP TABLE billing_migration_absence_reads, billing_migration_receipts, "
        "billing_migrations, billing_roster_control"
    )
    assert executed[3] == "DROP FUNCTION " + ", ".join(
        reversed(migration.DROPPED_FUNCTIONS)
    )
    for statement in executed:
        assert not sa.text(statement)._bindparams, statement[:80]
        # 037 rewrites no surviving function or constraint.
        assert "CREATE OR REPLACE" not in statement
        assert "ALTER TABLE" not in statement


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_no_statement_cascades(migration: ModuleType, direction: str) -> None:
    """Defense in depth behind the dependents preflight: without CASCADE an
    object nobody foresaw that depends on a dropped table or function makes
    the statement fail (nothing changed) instead of vanishing."""

    for statement in _executed(migration, direction):
        assert not re.search(r"\bCASCADE\b", statement, re.IGNORECASE), statement[:120]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_lock_timeout_of_five_seconds_is_the_first_statement(
    migration: ModuleType, direction: str
) -> None:
    """Precedent: migrations 024, 025 and 036.  It must precede the
    preflight's LOCK TABLE, or the wait it bounds would already be unbounded."""

    assert migration.LOCK_TIMEOUT == "SET LOCAL lock_timeout = '5s'"
    executed = _executed(migration, direction)
    assert executed[0] == migration.LOCK_TIMEOUT
    assert executed.count(migration.LOCK_TIMEOUT) == 1
    assert "DO $preflight$" in executed[1]
    assert all("lock_timeout" not in statement for statement in executed[1:])
    assert all("DO $preflight$" not in statement for statement in executed[2:])


@pytest.mark.parametrize(
    ("direction", "tables"),
    [("upgrade", _UPGRADE_LOCKED), ("downgrade", _DOWNGRADE_LOCKED)],
)
def test_preflight_locks_its_tables_in_access_exclusive_mode_first(
    migration: ModuleType, direction: str, tables: set[str]
) -> None:
    source = _executed(migration, direction)[1]
    (lock,) = _LOCK_STATEMENT.finditer(source)
    assert {name.strip() for name in lock["tables"].split(",")} == tables
    assert lock["mode"] == "ACCESS EXCLUSIVE"
    # The lock is the first statement of the block: every check runs under it.
    body = source[source.index("BEGIN") + len("BEGIN") :]
    assert body.lstrip().startswith("LOCK TABLE")


def test_upgrade_preflight_checks_every_category_in_one_refusal(
    migration: ModuleType,
) -> None:
    source = _executed(migration, "upgrade")[1]
    lines = [
        "'alembic_version is %s, not 036'",
        "'billing_migrations has %s row(s)'",
        "'billing_migration_receipts has %s row(s)'",
        "'billing_migration_absence_reads has %s row(s)'",
        "'billing_roster_control has %s row(s) whose revision is not 0'",
        "'functions 037 drops do not exist: '",
        "'unexpected objects depend on the dropped objects: '",
        "'functions outside 037''s scope name a dropped object: '",
        "'columns the downgrade would not restore: '",
        "'owners or privileges the downgrade would not recreate as %s: %s'",
        "'comments the downgrade would not restore: '",
    ]
    positions = [source.index(line) for line in lines]
    assert positions == sorted(positions)
    raise_at = source.index("RAISE EXCEPTION 'migration 037 refused (nothing")
    assert positions[-1] < raise_at
    assert source.count("RAISE EXCEPTION") == 1
    assert "USING ERRCODE = 'object_not_in_prerequisite_state'" in source
    # No readiness-receipt check: 037 changes no live column (unlike 036).
    assert "readiness" not in source
    # The probe runs before the checks (under the lock), the checks read it.
    probe = source.index("EXECUTE format('CREATE TABLE public.%I ()', probe);")
    assert source.index("LOCK TABLE") < probe < positions[0]
    # Function sources are matched case-insensitively.
    assert "p.prosrc ~* " in source and "p.prosrc ~ " not in source


def test_the_readback_file_is_what_the_migration_renders(migration: ModuleType) -> None:
    """The operator's readback (scripts/migration_037_readback.sql) is
    generated from the migration; regenerate it with
    ``python -m scripts.migration_037_readback --output <file>``."""

    rendered = migration.readback_sql(migration.frozen_036())
    assert _READBACK.read_text(encoding="ascii") == rendered
    assert not sa.text(rendered)._bindparams
    from scripts.migration_037_readback import READBACK_PATH, render

    assert READBACK_PATH == _READBACK and render() == rendered


def test_readback_and_preflight_share_every_check_but_the_probe(
    migration: ModuleType, document: dict[str, Any]
) -> None:
    """Both are built from ``_checks``: every check expression reads the same
    in both, except the privilege comparison, whose expected access lists
    come from the probe in the migration and from pg_default_acl in the
    readback.  The readback is a single SELECT: no DDL, DML or lock."""

    preflight = migration.upgrade_preflight(document)
    readback = migration.readback_sql(document)
    names = [
        name
        for name, _ in migration._checks(
            document, role="R", table_acl="T", function_acl="F"
        )
    ]
    assert names == [
        "revision",
        "billing_migrations rows",
        "billing_migration_receipts rows",
        "billing_migration_absence_reads rows",
        "roster revision",
        "functions to drop exist",
        "catalog dependents",
        "function sources",
        "columns",
        "owners and privileges",
        "comments",
    ]
    probe_checks = migration._checks(
        document,
        role="probe_owner",
        table_acl=migration._acl_set(
            "coalesce(probe_table_acl, acldefault('r', probe_owner))"
        ),
        function_acl=migration._acl_set(
            "coalesce(probe_function_acl, acldefault('f', probe_owner))"
        ),
    )
    readback_checks = migration._checks(
        document,
        role=migration._CURRENT_ROLE,
        table_acl=migration._default_acl_set("r"),
        function_acl=migration._default_acl_set("f"),
    )
    for (name, in_preflight), (_, in_readback) in zip(probe_checks, readback_checks):
        assert in_preflight in preflight and in_readback in readback, name
        if name != "owners and privileges":
            assert in_preflight == in_readback, name
    assert migration._scope_ctes() in preflight and migration._scope_ctes() in readback
    body = "\n".join(
        line for line in readback.splitlines() if not line.startswith("--")
    )
    assert body.lstrip().startswith("WITH scope AS (")
    assert body.count(";") == 1 and body.rstrip().endswith(";")
    for keyword in (
        "CREATE",
        "DROP",
        "INSERT",
        "UPDATE",
        "DELETE",
        "LOCK",
        "EXECUTE",
        "ALTER",
        "GRANT",
    ):
        assert not re.search(rf"\b{keyword}\b", body), keyword
    assert "pg_default_acl" in body and "acldefault(" in body


def test_the_lock_list_is_every_table_the_drops_touch(
    migration: ModuleType, document: dict[str, Any]
) -> None:
    """Dropping a foreign key changes the RI triggers of the table it
    references, so the upgrade locks the dropped tables plus every live table
    the frozen foreign keys reference; the downgrade (re-adding those keys)
    locks those live tables."""

    referenced = {
        match
        for entry in document["constraints"]
        for match in re.findall(r" REFERENCES (\w+)\(", entry["statement"])
    }
    live = referenced - set(migration.DROPPED_TABLES)
    assert live == set(migration.REFERENCED_TABLES)
    assert set(migration.UPGRADE_LOCKED) == {*migration.DROPPED_TABLES, *live}
    assert set(migration.DOWNGRADE_LOCKED) == live


def test_dependents_allowlist_is_exactly_what_the_downgrade_restores(
    migration: ModuleType, document: dict[str, Any]
) -> None:
    expected = migration.expected_dependents(document)
    assert len(expected) == len(set(expected)) == 64
    kinds = {}
    for identity in expected:
        kind = identity.split(" ")[0]
        kinds[kind] = kinds.get(kind, 0) + 1
    # 38 constraints + the constraint trigger's own row; 12 triggers; 2 plain
    # indexes; 11 column defaults (3 + 6 + 1 + 1).
    assert kinds == {"table": 39, "trigger": 12, "index": 2, "default": 11}
    assert (
        "table constraint trg_billing_migration_receipt_commit on "
        "public.billing_migration_receipts"
    ) in expected
    source = _executed(migration, "upgrade")[1]
    for identity in expected:
        assert f"'{identity}'" in source
    columns = migration.restored_columns(document)
    assert len(columns) == 3 + 22 + 21 + 22
    for column in columns:
        assert f"'{column}'" in source


def test_dropped_names_pattern_names_every_table_and_function(
    migration: ModuleType,
) -> None:
    pattern = migration._DROPPED_NAMES_PATTERN
    assert pattern.startswith(r"\m(") and pattern.endswith(r")\M")
    names = pattern[len(r"\m(") : -len(r")\M")].split("|")
    assert sorted(names) == sorted((*_TABLES, *_FUNCTIONS))


def test_downgrade_preflight_checks_revision_and_name_collisions(
    migration: ModuleType, document: dict[str, Any]
) -> None:
    source = _executed(migration, "downgrade")[1]
    assert "'alembic_version is %s, not 037'" in source
    assert "'objects the restore would create already exist: %s'" in source
    assert source.count("RAISE EXCEPTION") == 1
    relations = migration.restored_relations(document)
    # 4 tables, 11 key indexes (4 primary + 7 unique), 2 plain indexes.
    assert len(relations) == len(set(relations)) == 17
    for relation in relations:
        assert f"'{relation}'" in source
    for entry in document["functions"]:
        assert f"'public.{entry['signature']}'" in source


def test_the_frozen_file_ships_in_the_production_image() -> None:
    """Both directions read it at run time: a .dockerignore rule or a narrower
    COPY would make a production migration fail with FileNotFoundError."""

    relative = FROZEN_037_PATH.relative_to(_ROOT).as_posix()
    for path in (relative, "alembic/frozen", _MIGRATION.relative_to(_ROOT).as_posix()):
        assert not docker_ignores(path), path
    copies = [
        (copy.sources, copy.destination) for copy in _copies(_stage("production"))
    ]
    assert (["alembic"], "./alembic") in copies


def test_the_orm_no_longer_maps_the_dropped_tables() -> None:
    import src.models as models
    from src.models import BillingSubscription, Builder
    from src.models.base import Base

    for name in (
        "BillingMigration",
        "BillingMigrationReceipt",
        "BillingMigrationAbsenceRead",
        "BillingRosterControl",
    ):
        assert not hasattr(models, name), name
        assert name not in models.__all__, name
    assert not set(_TABLES) & set(Base.metadata.tables)
    # A relationship to a dropped table would be loaded by an ORM delete of
    # the parent (delete_builder) and fail on a 037 database.
    assert "billing_migrations" not in Builder.__mapper__.relationships
    assert "migrations" not in BillingSubscription.__mapper__.relationships


def _offline(*arguments: str) -> str:
    alembic_executable = Path(sys.executable).with_name("alembic")
    assert alembic_executable.is_file()
    environment = os.environ.copy()
    environment["DATABASE_URL"] = (
        "postgresql+asyncpg://offline:offline@localhost/offline"
    )
    # The repository's alembic/ directory must not shadow the package.
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(alembic_executable), *arguments, "--sql"],
        cwd=_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_offline_upgrade_runs_the_preflight_before_any_change() -> None:
    sql = _offline("upgrade", "036:037")
    timeout = sql.index("SET LOCAL lock_timeout = '5s';")
    lock = sql.index("LOCK TABLE billing_roster_control")
    preflight = sql.index("migration 037 refused (nothing was changed)")
    positions = [
        sql.index(marker)
        for marker in (
            "DROP TABLE billing_migration_absence_reads",
            "DROP FUNCTION validate_billing_migration_absence_read()",
            "UPDATE alembic_version SET version_num='037'",
        )
    ]
    assert timeout < lock < preflight < positions[0]
    assert positions == sorted(positions)


def test_offline_downgrade_refuses_first_then_restores_the_frozen_ddl(
    migration: ModuleType,
) -> None:
    sql = _offline("downgrade", "037:036")
    timeout = sql.index("SET LOCAL lock_timeout = '5s';")
    lock = sql.index("LOCK TABLE builders")
    preflight = sql.index("downgrade of migration 037 refused (nothing was changed)")
    cursor = preflight
    assert timeout < lock < preflight
    for statement in migration.frozen_036_statements():
        found = sql.index(statement.strip(), cursor)
        assert found > cursor
        cursor = found
    assert cursor < sql.index("UPDATE alembic_version SET version_num='036'")


def test_the_runbook_points_the_operator_at_the_generated_readback() -> None:
    runbook = (_ROOT / "docs" / "runbooks" / "asaas-deployment.md").read_text(
        encoding="utf-8"
    )
    section = runbook[
        runbook.index("## Migration 037") : runbook.index("## Production wiring")
    ]
    assert "scripts/migration_037_readback.sql" in section
    assert "connected as the role that\nwill run the migration" in section
    assert "BEGIN READ ONLY;" in section
    for state in ("55P03", "40P01"):
        assert state in section, state
    # The readback is the migration's own checks except the two listed.
    assert "predicates are the migration's own" not in section
    assert "migration-only" in section
