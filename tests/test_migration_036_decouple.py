"""Offline contract of migration 036 (Group 10 E5, "desacoplar").

PostgreSQL behaviour (preflight, round trip, fidelity against a brand-new 035
database) is proven in ``tests/integration/test_migration_036_postgres.py``;
these checks need no database: the frozen 035 DDL is the pinned file, every
statement passes the migration's bind-parameter guard, a changed byte is
refused before any SQL, and the offline SQL of both directions carries the
preflight first and the frozen statements in order.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable

import pytest
import sqlalchemy as sa

from scripts.alembic_frozen_ddl import FROZEN_036_PATH, SCOPE_036, render
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
    / "20261019_000001_036_decouple_asaas_billing_from_legacy_roster.py"
)
_LEGACY_TERMS = ("billing_migration", "roster", "migration_id")
_HUNKS = _ROOT / "tests" / "fixtures" / "billing" / "migration_036_function_hunks.json"
_UPGRADE_LOCKED = {
    "billing_subscriptions",
    "billing_checkout_sessions",
    "billing_rollout_readiness_receipts",
    "billing_migrations",
    "billing_migration_receipts",
    "billing_migration_absence_reads",
    "billing_roster_control",
}
_DOWNGRADE_LOCKED = {
    "billing_subscriptions",
    "billing_checkout_sessions",
    "billing_rollout_readiness_receipts",
}
_LOCK_STATEMENT = re.compile(
    r"\bLOCK TABLE (?P<tables>[a-z_,\s]+?)\s+IN (?P<mode>[A-Z ]+?) MODE;"
)


def _load() -> ModuleType:
    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        setattr(alembic_package, "op", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("migration_036", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load()


def test_036_revises_035_and_037_follows_it(migration: ModuleType) -> None:
    assert (migration.revision, migration.down_revision) == ("036", "035")
    revisions = migration_revisions()
    assert revisions[_MIGRATION.name] == ("036", "035")
    # Group 10 E6: 037 (drop the legacy objects) is the only child of 036.
    assert [rev for rev, parent in revisions.values() if parent == "036"] == ["037"]
    # neurostack-credit-system Group 1a: 038 (credit schema) follows 037;
    # Group 4b: 040 (additional object copies) follows 039; Group 5: 041
    # (lifecycle, legal hold, purge and roles) follows 040.
    assert current_head() == "041"


def test_frozen_file_is_pinned_rendered_and_outside_versions(
    migration: ModuleType,
) -> None:
    raw = FROZEN_036_PATH.read_bytes()
    assert migration.FROZEN_035 == FROZEN_036_PATH
    assert hashlib.sha256(raw).hexdigest() == migration.FROZEN_035_SHA256
    assert FROZEN_036_PATH.parent == _ROOT / "alembic" / "frozen"
    document = json.loads(raw)
    # The exact bytes the extractor renders (stable order, ASCII, newline).
    assert render(document) == raw.decode("ascii")
    assert document["revision"] == "035"
    assert [(e["table"], e["column"]) for e in document["columns"]] == list(
        SCOPE_036.columns
    )
    assert [e["name"] for e in document["functions"]] == list(SCOPE_036.functions)
    assert [(e["table"], e["name"]) for e in document["triggers"]] == list(
        SCOPE_036.triggers
    )
    assert sorted(e["name"] for e in document["constraints"]) == sorted(
        migration._DROPPED_CONSTRAINTS
    )


def test_frozen_statements_restore_in_order_and_pass_the_bind_guard(
    migration: ModuleType,
) -> None:
    statements = migration.frozen_035_statements()
    document = json.loads(FROZEN_036_PATH.read_bytes())
    expected = [
        entry["statement"]
        for kind in ("columns", "constraints", "functions", "triggers")
        for entry in document[kind]
    ]
    assert list(statements) == expected
    assert len(statements) == 13
    for statement in statements:
        assert not sa.text(statement)._bindparams, statement[:80]
    # 035 still names the legacy objects; that is what the downgrade restores.
    assert any("migration_id" in statement for statement in statements)
    assert any("roster_revision" in statement for statement in statements)


_TAMPERS: dict[str, Callable[[bytes], bytes]] = {
    "one_operator": lambda raw: raw.replace(
        b"roster_revision > 0", b"roster_revision >= 0", 1
    ),
    "truncated": lambda raw: raw[:-1],
    "trailing_byte": lambda raw: raw + b" ",
}


@pytest.mark.parametrize("tamper", sorted(_TAMPERS))
def test_a_changed_frozen_byte_is_refused_before_any_sql(
    migration: ModuleType, tamper: str
) -> None:
    raw = FROZEN_036_PATH.read_bytes()
    changed = _TAMPERS[tamper](raw)
    assert changed != raw
    with pytest.raises(RuntimeError, match="the frozen 035 DDL was changed"):
        migration.frozen_035_statements(changed)


def test_upgrade_ddl_reads_no_legacy_object_and_passes_the_bind_guard(
    migration: ModuleType,
) -> None:
    for statement in (migration._UPGRADE_PREFLIGHT, *migration._UPGRADE):
        assert not sa.text(statement)._bindparams, statement[:80]
    for function in migration._UPGRADE[:4]:
        assert "CREATE OR REPLACE FUNCTION" in function
        for term in _LEGACY_TERMS:
            assert term not in function, term
    assert "migration_id" not in migration.CANARY_EXACT_CHECK
    assert "migration_id" not in migration.INTENT_TARGET_CHECK
    # Both credit-grant snapshots of 035 stay valid.
    assert "'credit_grant_mc', 5000000" in migration.CANARY_EXACT_CHECK
    assert "'credit_grant_mc', 5000," in migration.CANARY_EXACT_CHECK
    for statement in (migration._DOWNGRADE_PREFLIGHT, *migration._DOWNGRADE_DROP_036):
        assert not sa.text(statement)._bindparams, statement[:80]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_no_statement_cascades(migration: ModuleType, direction: str) -> None:
    """Defense in depth behind the dependents preflight: without CASCADE an
    object nobody foresaw that depends on a dropped column or constraint
    makes the statement fail (nothing changed) instead of vanishing.  At run
    time the preflight already refuses such objects, so only this contract
    can tell a CASCADE apart."""

    for statement in _executed(migration, direction):
        assert not re.search(r"\bCASCADE\b", statement, re.IGNORECASE), statement[:120]


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


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_lock_timeout_of_five_seconds_is_the_first_statement(
    migration: ModuleType, direction: str
) -> None:
    """Precedent: migrations 024 and 025.  It must precede the preflight's
    LOCK TABLE, or the wait it bounds would already be unbounded."""

    assert migration.LOCK_TIMEOUT == "SET LOCAL lock_timeout = '5s'"
    executed = _executed(migration, direction)
    assert executed[0] == migration.LOCK_TIMEOUT
    assert executed.count(migration.LOCK_TIMEOUT) == 1
    assert "DO $preflight$" in executed[1]
    assert all("lock_timeout" not in statement for statement in executed[1:])


@pytest.mark.parametrize(
    ("preflight", "tables"),
    [
        ("_UPGRADE_PREFLIGHT", _UPGRADE_LOCKED),
        ("_DOWNGRADE_PREFLIGHT", _DOWNGRADE_LOCKED),
    ],
    ids=["upgrade", "downgrade"],
)
def test_preflight_locks_its_tables_in_access_exclusive_mode_first(
    migration: ModuleType, preflight: str, tables: set[str]
) -> None:
    source = getattr(migration, preflight)
    (lock,) = _LOCK_STATEMENT.finditer(source)
    assert {name.strip() for name in lock["tables"].split(",")} == tables
    assert lock["mode"] == "ACCESS EXCLUSIVE"
    # The lock is the first statement of the block: every count runs under it.
    body = source[source.index("BEGIN") + len("BEGIN") :]
    assert body.lstrip().startswith("LOCK TABLE")


def _function_body(sql: str) -> list[str]:
    """Whitespace-normalised body lines between the ``$function$`` quotes."""

    start = sql.index("$function$") + len("$function$")
    end = sql.rindex("$function$")
    return [line.strip() for line in sql[start:end].splitlines() if line.strip()]


def test_each_rewritten_function_is_035_minus_exactly_the_pinned_hunks(
    migration: ModuleType,
) -> None:
    """Every line of a 036 body is a 035 line except the pinned hunks, so an
    edit anywhere else (a dropped topup lineage branch, a dropped
    ``projection_restored <> 0``) fails here.  The hunks remove only the
    legacy saga/roster/``migration_id`` parts and collapse the snapshot
    tuples; regenerate the fixture only for a reviewed rewrite."""

    rewritten = {
        "protect_billing_canary_subscription": migration._PROTECT_SUBSCRIPTION,
        "validate_billing_canary_checkout": migration._VALIDATE_CHECKOUT,
        "validate_billing_canary_projection": migration._VALIDATE_PROJECTION,
        "validate_billing_canary_effect": migration._VALIDATE_EFFECT,
    }
    frozen = {
        entry["name"]: entry["statement"]
        for entry in json.loads(FROZEN_036_PATH.read_bytes())["functions"]
    }
    pinned = json.loads(_HUNKS.read_text(encoding="ascii"))
    assert set(rewritten) == set(frozen) == set(pinned)
    for name, new_sql in rewritten.items():
        old, new = _function_body(frozen[name]), _function_body(new_sql)
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        hunks = [
            {"remove": old[i1:i2], "add": new[j1:j2]}
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        ]
        assert hunks == pinned[name], name
    # Lines the review called out stay guarded (present before and after).
    effect = _function_body(migration._VALIDATE_EFFECT)
    assert "OR projection_restored <> 0" in effect
    assert "OR (checkout.canary_slot = 'topup'" in effect
    assert "AND checkout.topup_order_id =" in effect


def test_the_frozen_file_ships_in_the_production_image() -> None:
    """The downgrade reads it at run time: a .dockerignore rule or a narrower
    COPY would make a production downgrade fail with FileNotFoundError."""

    relative = FROZEN_036_PATH.relative_to(_ROOT).as_posix()
    for path in (relative, "alembic/frozen", _MIGRATION.relative_to(_ROOT).as_posix()):
        assert not docker_ignores(path), path
    copies = [
        (copy.sources, copy.destination) for copy in _copies(_stage("production"))
    ]
    assert (["alembic"], "./alembic") in copies


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
    sql = _offline("upgrade", "035:036")
    timeout = sql.index("SET LOCAL lock_timeout = '5s';")
    lock = sql.index("LOCK TABLE billing_subscriptions")
    preflight = sql.index("migration 036 refused (nothing was changed)")
    positions = [
        sql.index(marker)
        for marker in (
            "CREATE OR REPLACE FUNCTION protect_billing_canary_subscription()",
            "CREATE OR REPLACE FUNCTION validate_billing_canary_checkout()",
            "CREATE OR REPLACE FUNCTION validate_billing_canary_projection()",
            "CREATE OR REPLACE FUNCTION validate_billing_canary_effect()",
            "DROP TRIGGER trg_billing_subscriptions_roster_fence",
            "DROP COLUMN migration_id",
            "DROP CONSTRAINT ck_billing_subscriptions_roster_revision",
            "DROP CONSTRAINT ck_billing_rollout_readiness_roster_revision",
            "UPDATE alembic_version SET version_num='036'",
        )
    ]
    assert timeout < lock < preflight < positions[0]
    assert positions == sorted(positions)


def test_offline_downgrade_refuses_first_then_restores_the_frozen_ddl(
    migration: ModuleType,
) -> None:
    sql = _offline("downgrade", "036:035")
    timeout = sql.index("SET LOCAL lock_timeout = '5s';")
    lock = sql.index("LOCK TABLE billing_subscriptions")
    preflight = sql.index("downgrade of migration 036 refused (nothing was changed)")
    cursor = sql.index("DROP CONSTRAINT ck_billing_checkout_sessions_canary_exact")
    assert timeout < lock < preflight < cursor
    for statement in migration.frozen_035_statements():
        found = sql.index(statement.strip(), cursor)
        assert found > cursor
        cursor = found
    assert cursor < sql.index("UPDATE alembic_version SET version_num='035'")
