"""Migration 047, the credit enforcement marks bridge, on real PostgreSQL.

cobranca-credito-por-builder Group 1 (PR-A).  PostgreSQL only.  A module
template at ``046`` with two builders, and a second template upgraded from it
to ``047``; every test clones one of them.  Pinned here:

* the revision: ``047`` revises ``046`` and is the single head; it only
  creates (no ALTER or DROP of an existing table) and references no
  existing table;
* the upgrade adds exactly the table ``credit_enforcement_marks`` (columns,
  CHECKs, no foreign key, the ``(builder_id, occurred_at)`` index and the
  append-only trigger) and leaves every other object of the 046 catalog as
  it was;
* the locks (the no-window classification, Decision 14): the migration's own
  statements lock no relation that existed before them, and with Alembic's
  version bump the only lock on an existing relation is ROW EXCLUSIVE on
  ``alembic_version`` (and its key); no ACCESS EXCLUSIVE on any of them.  A
  reader, an open writer, or even an ACCESS EXCLUSIVE holder of ``builders``
  does not stop the upgrade;
* the rows: a mark and an unmark are recorded; each CHECK, the length bound
  and NOT NULL refuse their own bad row; UPDATE and DELETE are refused by the
  038 trigger (23000); with no foreign key (owner decision, review of Group
  1), a mark naming a builder that does not exist is admitted (the writer of
  PR-B checks the builder), and a marked builder is deleted, by SQL and by
  the ORM delete ``DELETE /v1/admin/builders/{id}`` issues, while its marks
  stay;
* the downgrade: an empty round trip restores the 046 catalog exactly; a
  mark refuses it with SQLSTATE 55000, changing nothing; another revision is
  refused; it gives up after the lock timeout behind a reader of the table
  it locks;
* the SQL of live step L2: ``scripts/migration_047_preflight.sql`` reads
  every count 0 only at 046 (as the application role too), and
  ``scripts/migration_047_post_read.sql``, run as ``current_user`` =
  ``neurostack_app`` (created here when the cluster lacks it), reads every
  row true at 047 only when that role holds SELECT and INSERT through the
  migration role's default privileges and the trigger is in place; both are
  byte for byte in the runbook;
* ``python -m src.ops.credit_charge_mark status`` at 046 (``marks table
  absent``, the rest still read, exit 0) and at 047 (empty, then a mark and
  an unmark inserted by SQL), with linked tenants with and without a
  provider, reading only ``api_credentials ->> 'whatsapp_provider'`` in a
  READ ONLY transaction, never printing a credential.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.models.base import Base
from tests.alembic_graph import heads, migration_revisions
from tests.integration.test_asaas_billing_postgres import (
    _sqlstate,
    _validated_admin_url,
)
from tests.integration.test_migration_036_postgres import (
    _LOCK_BOUND_SECONDS,
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
(_MIGRATION_PATH,) = (_ROOT / "alembic" / "versions").glob("*_047_*.py")
_RUNBOOK = _ROOT / "docs" / "runbooks" / "asaas-deployment.md"
_DOWNGRADE_REFUSED = "downgrade of migration 047 refused (nothing was changed): "
_APPEND_ONLY = "credit_enforcement_marks is append-only: correct it with a new row"
_APP_ROLE = "neurostack_app"

_BUILDER = UUID("00000000-0000-4000-8000-000000004701")
_OTHER_BUILDER = UUID("00000000-0000-4000-8000-000000004702")
_ACTOR = UUID("00000000-0000-4000-8000-000000004731")
_AT = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
_SECRET = "zapi-token-NEVER-PRINT-4747"


def _load_migration() -> ModuleType:
    """The migration module itself (its DDL and preflight, run directly).

    In-process, the repository's ``alembic/`` directory may shadow the
    package; ``op`` is only needed as a name for the module to import.
    """

    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        setattr(alembic_package, "op", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("migration_047", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION = _load_migration()

_INSERT_BUILDER = (
    "INSERT INTO builders (id, name, status, is_active) "
    "VALUES (:id, :builder_name, 'active', true)"
)
_INSERT_MARK = (
    "INSERT INTO credit_enforcement_marks "
    "(id, builder_id, kind, occurred_at, actor_user_id, reason) "
    "VALUES (:id, :builder, :kind, :at, :actor, :reason)"
)


def _mark(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid4(),
        "builder": _BUILDER,
        "kind": "mark",
        "at": _AT,
        "actor": _ACTOR,
        "reason": "piloto de cobrança",
    }
    row.update(changes)
    return row


def _refused(info: pytest.ExceptionInfo[DBAPIError], sqlstate: str, guard: str) -> None:
    assert _sqlstate(info.value) == sqlstate, info.value
    assert guard in str(info.value), info.value


def _without_marks(catalog: dict[str, Any]) -> dict[str, Any]:
    """The catalog without the rows of the table 047 adds (and without
    ``alembic_version``, which the upgrade moves)."""

    def foreign(row: tuple[Any, ...]) -> bool:
        return any(
            isinstance(value, str) and "credit_enforcement_marks" in value
            for value in row[:2]
        )

    kept: dict[str, Any] = {}
    for key, rows in catalog.items():
        if key == "alembic_version":
            continue
        if key == "column_order":
            kept[key] = {
                t: c for t, c in rows.items() if t != "credit_enforcement_marks"
            }
            continue
        kept[key] = [row for row in rows if not foreign(row)]
    return kept


# ---------------------------------------------------------------------------
# Module fixture: a 046 template with two builders, and a 047 template.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Baseline:
    cluster: _Cluster
    template_046: str
    template_047: str
    catalog_046: dict[str, Any]
    created_app_role: bool


async def _ensure_app_role(cluster: _Cluster) -> bool:
    """``neurostack_app`` (production's application role), NOLOGIN, created
    only when the throwaway cluster lacks it; True when created here."""

    async with cluster.admin.connect() as connection:
        exists = await connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
            {"role": _APP_ROLE},
        )
        if exists:
            return False
        await connection.exec_driver_sql(f'CREATE ROLE "{_APP_ROLE}" NOLOGIN')
        return True


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
    created_role = False
    try:
        created_role = await _ensure_app_role(cluster)
        template_046 = await cluster.fresh()
        await _migrate(cluster.url(template_046), "upgrade", "046")
        for builder, label in ((_BUILDER, "Marked"), (_OTHER_BUILDER, "Other")):
            await _execute(
                cluster, template_046, _INSERT_BUILDER, id=builder, builder_name=label
            )
        catalog_046 = await _catalog(cluster, template_046)
        template_047 = await cluster.clone(template_046)
        await _migrate(cluster.url(template_047), "upgrade", "047")
        yield _Baseline(cluster, template_046, template_047, catalog_046, created_role)
    finally:
        for name in list(reversed(cluster.created)):
            await cluster.drop(name)
        if created_role:
            async with admin.connect() as connection:
                await connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{_APP_ROLE}"')
        await admin.dispose()


@pytest.fixture
async def at_046(baseline: _Baseline) -> AsyncIterator[str]:
    name = await baseline.cluster.clone(baseline.template_046)
    try:
        yield name
    finally:
        await baseline.cluster.drop(name)


@pytest.fixture
async def at_047(baseline: _Baseline) -> AsyncIterator[str]:
    name = await baseline.cluster.clone(baseline.template_047)
    try:
        yield name
    finally:
        await baseline.cluster.drop(name)


# ---------------------------------------------------------------------------
# The revision and the DDL (no database).
# ---------------------------------------------------------------------------


def test_047_revises_046_and_is_the_single_head() -> None:
    assert (_MIGRATION.revision, _MIGRATION.down_revision) == ("047", "046")
    revisions = migration_revisions()
    assert revisions[_MIGRATION_PATH.name] == ("047", "046")
    assert heads(revisions) == ["047"]
    assert re.fullmatch(
        r"\d{8}_\d{6}_047_credit_enforcement_marks\.py", _MIGRATION_PATH.name
    )
    # A credit migration that deliberately keeps CREDIT_SCHEMA_MIN_REVISION
    # at 042 (tests/services/test_credit_switch.py names it).
    assert "_credit_" in _MIGRATION_PATH.name
    assert _MIGRATION.CREATED_TABLES == ("credit_enforcement_marks",)
    assert _MIGRATION.REFERENCED_TABLES == ()
    assert _MIGRATION.DOWNGRADE_LOCKED == ("credit_enforcement_marks",)
    assert _MIGRATION.LOCK_TIMEOUT == "SET LOCAL lock_timeout = '5s'"


def test_the_upgrade_only_creates_and_the_orm_shares_its_vocabulary() -> None:
    from src.models.credit import (
        ENFORCEMENT_MARK_KINDS,
        ENFORCEMENT_MARK_REASON_MAX_LENGTH,
    )

    for statement in (*_MIGRATION.TABLES, *_MIGRATION.TRIGGERS):
        sql = " ".join(statement.split())
        assert re.match(r"^CREATE (TABLE|INDEX|TRIGGER) ", sql), sql[:60]
        assert "ALTER " not in sql and "DROP " not in sql, sql[:60]
        assert not text(statement)._bindparams, sql[:60]
    for statement in _MIGRATION.TABLES:
        assert "REFERENCES" not in statement and "FOREIGN KEY" not in statement
    assert _MIGRATION.MARK_KINDS == ENFORCEMENT_MARK_KINDS
    assert _MIGRATION.REASON_MAX_LENGTH == ENFORCEMENT_MARK_REASON_MAX_LENGTH
    assert "credit_enforcement_marks" in Base.metadata.tables


# ---------------------------------------------------------------------------
# The upgrade: exactly one new table, nothing else changed.
# ---------------------------------------------------------------------------


async def test_the_upgrade_adds_only_the_marks_table(baseline: _Baseline) -> None:
    after = await _catalog(baseline.cluster, baseline.template_047)
    assert after["alembic_version"] == [("047",)]
    assert _without_marks(after) == _without_marks(baseline.catalog_046)
    added = {row[0] for row in after["relations"]} - {
        row[0] for row in baseline.catalog_046["relations"]
    }
    assert added == {"credit_enforcement_marks"}


async def test_credit_enforcement_marks_is_exactly_the_047_table(
    baseline: _Baseline,
) -> None:
    catalog = await _catalog(baseline.cluster, baseline.template_047)
    columns = [
        row[1:5] for row in catalog["columns"] if row[0] == "credit_enforcement_marks"
    ]
    assert catalog["column_order"]["credit_enforcement_marks"] == [
        "id",
        "builder_id",
        "kind",
        "occurred_at",
        "actor_user_id",
        "reason",
    ]
    assert sorted(columns) == sorted(
        [
            ("id", "uuid", True, ""),
            ("builder_id", "uuid", True, ""),
            ("kind", "character varying(20)", True, ""),
            ("occurred_at", "timestamp with time zone", True, ""),
            ("actor_user_id", "uuid", True, ""),
            ("reason", "character varying(500)", True, ""),
        ]
    )
    constraints = {
        row[1]: (row[2], row[3])
        for row in catalog["constraints"]
        if row[0] == "credit_enforcement_marks"
    }
    assert constraints == {
        "credit_enforcement_marks_pkey": ("p", "PRIMARY KEY (id)"),
        "ck_credit_enforcement_marks_kind": (
            "c",
            "CHECK (((kind)::text = ANY ((ARRAY['mark'::character varying, "
            "'unmark'::character varying])::text[])))",
        ),
        "ck_credit_enforcement_marks_reason": (
            "c",
            "CHECK ((length(TRIM(BOTH FROM reason)) > 0))",
        ),
    }
    indexes = {
        row[1]: row[2]
        for row in catalog["indexes"]
        if row[0] == "credit_enforcement_marks"
    }
    assert indexes == {
        "credit_enforcement_marks_pkey": (
            "CREATE UNIQUE INDEX credit_enforcement_marks_pkey "
            "ON public.credit_enforcement_marks USING btree (id)"
        ),
        "ix_credit_enforcement_marks_builder_occurred": (
            "CREATE INDEX ix_credit_enforcement_marks_builder_occurred "
            "ON public.credit_enforcement_marks USING btree (builder_id, occurred_at)"
        ),
    }
    triggers = [
        row for row in catalog["triggers"] if row[0] == "credit_enforcement_marks"
    ]
    assert [(row[1], row[2], row[3]) for row in triggers] == [
        (
            "trg_credit_enforcement_marks_immutable",
            "CREATE TRIGGER trg_credit_enforcement_marks_immutable BEFORE DELETE OR "
            "UPDATE ON public.credit_enforcement_marks FOR EACH ROW EXECUTE FUNCTION "
            "reject_credit_history_mutation()",
            "O",
        )
    ]


_LOCKS_SQL = """
SELECT c.relname, l.mode
FROM pg_locks AS l JOIN pg_class AS c ON c.oid = l.relation
WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation'
  AND c.relnamespace = 'public'::regnamespace
ORDER BY 1, 2
"""


async def test_the_upgrade_locks_no_existing_table(
    baseline: _Baseline, at_046: str
) -> None:
    """Decision 14's rule, measured: inside the upgrade's transaction the
    migration's own statements hold no lock on a relation of the 046 schema;
    with the version bump Alembic makes, the only one is the ROW EXCLUSIVE of
    ``alembic_version`` (and its key).  Every ACCESS EXCLUSIVE lock is on an
    object the upgrade creates."""

    existing = {row[0] for row in baseline.catalog_046["relations"]} | {
        row[1] for row in baseline.catalog_046["indexes"]
    }
    engine = create_async_engine(baseline.cluster.url(at_046), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            for statement in (
                _MIGRATION.LOCK_TIMEOUT,
                *_MIGRATION.TABLES,
                *_MIGRATION.TRIGGERS,
            ):
                await connection.exec_driver_sql(statement)
            own = [tuple(row) for row in (await connection.execute(text(_LOCKS_SQL)))]
            await connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num = '047'"
            )
            locks = [tuple(row) for row in (await connection.execute(text(_LOCKS_SQL)))]
            await transaction.rollback()
    finally:
        await engine.dispose()
    assert own and not {name for name, _mode in own} & existing
    on_existing = {(name, mode) for name, mode in locks if name in existing}
    assert on_existing == {
        ("alembic_version", "RowExclusiveLock"),
        ("alembic_version_pkc", "RowExclusiveLock"),
    }
    exclusive = {name for name, mode in locks if mode == "AccessExclusiveLock"}
    assert exclusive and not exclusive & existing
    assert {
        "credit_enforcement_marks",
        "ix_credit_enforcement_marks_builder_occurred",
    } <= exclusive
    assert (
        await _scalar(
            baseline.cluster, at_046, "SELECT version_num FROM alembic_version"
        )
        == "046"
    )


async def test_a_reader_of_builders_does_not_stop_the_upgrade(
    baseline: _Baseline, at_046: str
) -> None:
    code, output = await _alembic_while_reading(
        baseline.cluster, at_046, "builders", "upgrade", "047"
    )
    assert code == 0, output
    assert (
        await _scalar(
            baseline.cluster, at_046, "SELECT version_num FROM alembic_version"
        )
        == "047"
    )


@pytest.mark.parametrize(
    "holder",
    [
        "UPDATE builders SET name = name WHERE id = :id",
        "LOCK TABLE builders IN ACCESS EXCLUSIVE MODE",
    ],
    ids=["open-writer", "access-exclusive"],
)
async def test_a_writer_of_builders_does_not_stop_the_upgrade(
    baseline: _Baseline, at_046: str, holder: str
) -> None:
    """With no foreign key the upgrade never locks ``builders``: an open
    writer of it, and even a session holding ACCESS EXCLUSIVE on it, let the
    run finish well inside the 5 s lock timeout."""

    cluster = baseline.cluster
    engine = create_async_engine(cluster.url(at_046), poolclass=NullPool)
    connection = await engine.connect()
    code: int | None = None
    output = b""
    try:
        await connection.execute(text(holder), {"id": _BUILDER})
        process = await _spawn_alembic(cluster.url(at_046), "upgrade", "047")
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=_LOCK_BOUND_SECONDS
            )
            code = int(process.returncode or 0)
        except asyncio.TimeoutError:
            code = None
        # Read while the holder still holds its lock: the run did not wait.
    finally:
        await connection.rollback()
        await connection.close()
        await engine.dispose()
    if code is None:
        await process.communicate()
    assert code == 0, output.decode(errors="replace")
    assert "LockNotAvailableError" not in output.decode(errors="replace")
    assert (
        await _scalar(cluster, at_046, "SELECT version_num FROM alembic_version")
        == "047"
    )


# ---------------------------------------------------------------------------
# The rows.
# ---------------------------------------------------------------------------


async def test_a_mark_and_an_unmark_are_recorded(
    baseline: _Baseline, at_047: str
) -> None:
    cluster = baseline.cluster
    mark, unmark = _mark(), _mark(kind="unmark", at=_AT + timedelta(hours=1))
    await _execute(cluster, at_047, _INSERT_MARK, **mark)
    await _execute(cluster, at_047, _INSERT_MARK, **unmark)
    latest = await _scalar(
        cluster,
        at_047,
        "SELECT kind FROM credit_enforcement_marks WHERE builder_id = :builder "
        "ORDER BY occurred_at DESC LIMIT 1",
        builder=_BUILDER,
    )
    assert latest == "unmark"


@pytest.mark.parametrize(
    ("changes", "sqlstate", "guard"),
    [
        ({"kind": "suspend"}, "23514", "ck_credit_enforcement_marks_kind"),
        ({"kind": "MARK"}, "23514", "ck_credit_enforcement_marks_kind"),
        ({"reason": ""}, "23514", "ck_credit_enforcement_marks_reason"),
        ({"reason": "   "}, "23514", "ck_credit_enforcement_marks_reason"),
        ({"reason": "x" * 501}, "22001", "character varying(500)"),
        ({"reason": None}, "23502", "reason"),
        ({"at": None}, "23502", "occurred_at"),
        ({"actor": None}, "23502", "actor_user_id"),
    ],
    ids=[
        "kind",
        "kind-case",
        "reason-empty",
        "reason-blank",
        "reason-long",
        "reason-null",
        "occurred-null",
        "actor-null",
    ],
)
async def test_each_rule_refuses_its_own_bad_row(
    baseline: _Baseline,
    at_047: str,
    changes: dict[str, Any],
    sqlstate: str,
    guard: str,
) -> None:
    with pytest.raises(DBAPIError) as info:
        await _execute(baseline.cluster, at_047, _INSERT_MARK, **_mark(**changes))
    _refused(info, sqlstate, guard)
    assert (
        await _scalar(
            baseline.cluster, at_047, "SELECT count(*) FROM credit_enforcement_marks"
        )
        == 0
    )


async def test_the_reason_bound_is_500_characters(
    baseline: _Baseline, at_047: str
) -> None:
    await _execute(baseline.cluster, at_047, _INSERT_MARK, **_mark(reason="x" * 500))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE credit_enforcement_marks SET reason = 'outro motivo'",
        "UPDATE credit_enforcement_marks SET kind = 'unmark'",
        "UPDATE credit_enforcement_marks SET kind = kind",
        "DELETE FROM credit_enforcement_marks",
    ],
    ids=["update-reason", "update-kind", "update-same", "delete"],
)
async def test_marks_are_append_only(
    baseline: _Baseline, at_047: str, statement: str
) -> None:
    cluster = baseline.cluster
    row = _mark()
    await _execute(cluster, at_047, _INSERT_MARK, **row)
    with pytest.raises(DBAPIError, match=_APPEND_ONLY) as info:
        await _execute(cluster, at_047, statement)
    _refused(info, "23000", _APPEND_ONLY)
    assert (
        await _scalar(
            cluster,
            at_047,
            "SELECT kind || ':' || reason FROM credit_enforcement_marks WHERE id = :id",
            id=row["id"],
        )
        == "mark:piloto de cobrança"
    )


async def test_a_mark_of_a_builder_that_does_not_exist_is_admitted(
    baseline: _Baseline, at_047: str
) -> None:
    """No foreign key: the schema does not check the builder (the PR-B
    writer reads it ``FOR KEY SHARE`` in its own transaction)."""

    row = _mark(builder=uuid4())
    await _execute(baseline.cluster, at_047, _INSERT_MARK, **row)
    assert (
        await _scalar(
            baseline.cluster,
            at_047,
            "SELECT count(*) FROM credit_enforcement_marks WHERE builder_id = :b",
            b=row["builder"],
        )
        == 1
    )


async def test_a_marked_builder_is_deleted_and_its_marks_stay(
    baseline: _Baseline, at_047: str
) -> None:
    """A builder with marks is deleted, by SQL and by the ORM delete of
    ``Builder`` (the statement ``DELETE /v1/admin/builders/{id}`` ends with):
    its marks stay, as credit history does."""

    from src.models.builder import Builder

    cluster = baseline.cluster
    for builder in (_BUILDER, _OTHER_BUILDER):
        await _execute(cluster, at_047, _INSERT_MARK, **_mark(builder=builder))
        await _execute(
            cluster,
            at_047,
            _INSERT_MARK,
            **_mark(builder=builder, kind="unmark", at=_AT + timedelta(hours=1)),
        )
    await _execute(cluster, at_047, "DELETE FROM builders WHERE id = :id", id=_BUILDER)
    engine = create_async_engine(cluster.url(at_047), poolclass=NullPool)
    try:
        sessions = async_sessionmaker(engine, class_=AsyncSession)
        async with sessions() as session:
            builder = await session.get(Builder, _OTHER_BUILDER)
            assert builder is not None
            await session.delete(builder)
            await session.commit()
    finally:
        await engine.dispose()
    assert await _scalar(cluster, at_047, "SELECT count(*) FROM builders") == 0
    assert (
        await _scalar(cluster, at_047, "SELECT count(*) FROM credit_enforcement_marks")
        == 4
    )


# ---------------------------------------------------------------------------
# The downgrade.
# ---------------------------------------------------------------------------


async def test_the_empty_round_trip_restores_046_exactly(
    baseline: _Baseline, at_047: str
) -> None:
    cluster = baseline.cluster
    await _migrate(cluster.url(at_047), "downgrade", "046")
    assert await _catalog(cluster, at_047) == baseline.catalog_046
    await _migrate(cluster.url(at_047), "upgrade", "047")
    assert (await _catalog(cluster, at_047))["alembic_version"] == [("047",)]


@pytest.mark.parametrize("kind", ["mark", "unmark"])
async def test_any_mark_row_refuses_the_downgrade(
    baseline: _Baseline, at_047: str, kind: str
) -> None:
    cluster = baseline.cluster
    await _execute(cluster, at_047, _INSERT_MARK, **_mark(kind=kind))
    before = await _catalog(cluster, at_047)
    code, output = await _alembic(cluster.url(at_047), "downgrade", "046")
    assert code != 0, output
    assert _refusal(output, _DOWNGRADE_REFUSED) == (
        f"{_DOWNGRADE_REFUSED}credit enforcement marks exist"
    )
    assert await _catalog(cluster, at_047) == before
    assert before["alembic_version"] == [("047",)]


@pytest.mark.parametrize(
    "statement,listed",
    [
        ("UPDATE alembic_version SET version_num = '046'", "046"),
        ("DELETE FROM alembic_version", "empty"),
        ("INSERT INTO alembic_version VALUES ('046')", "046, 047"),
    ],
)
async def test_the_downgrade_preflight_refuses_another_revision(
    baseline: _Baseline, at_047: str, statement: str, listed: str
) -> None:
    cluster = baseline.cluster
    await _execute(cluster, at_047, statement)
    before = await _catalog(cluster, at_047)
    engine = create_async_engine(cluster.url(at_047), poolclass=NullPool)
    try:
        with pytest.raises(DBAPIError, match="downgrade of migration 047") as info:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(_MIGRATION.LOCK_TIMEOUT)
                await connection.exec_driver_sql(_MIGRATION.downgrade_preflight())
    finally:
        await engine.dispose()
    assert _sqlstate(info.value) == "55000"
    assert f"{_DOWNGRADE_REFUSED}alembic_version is {listed}, not 047" in str(
        info.value
    )
    assert await _catalog(cluster, at_047) == before


async def test_the_downgrade_gives_up_after_the_lock_timeout(
    baseline: _Baseline, at_047: str
) -> None:
    """A reader of the table the preflight locks holds ACCESS SHARE: the
    preflight's ACCESS EXCLUSIVE lock gives up (55P03) before anything
    changes."""

    before = await _catalog(baseline.cluster, at_047)
    code, output = await _alembic_while_reading(
        baseline.cluster, at_047, "credit_enforcement_marks", "downgrade", "046"
    )
    _assert_lock_timeout_in_the_preflight(code, output)
    assert await _catalog(baseline.cluster, at_047) == before


# ---------------------------------------------------------------------------
# Live step L2's reads.
# ---------------------------------------------------------------------------

_PREFLIGHT_PATH = _ROOT / "scripts" / "migration_047_preflight.sql"
_POST_READ_PATH = _ROOT / "scripts" / "migration_047_post_read.sql"


def _query(path: Path) -> str:
    """The SQL below the comment lines (what the runbook shows)."""

    lines = path.read_text(encoding="utf-8").splitlines()
    body = [line for line in lines if not line.startswith("--")]
    assert body and all(line.strip() for line in body), path.name
    return "\n".join(body)


def test_the_047_reads_are_byte_for_byte_in_the_runbook() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    start = runbook.index("## Migration 047:")
    section = runbook[start : runbook.index("\n## ", start + 1)]
    blocks = re.findall(r"```sql\n(.*?)\n```", section, re.S)
    assert blocks == [_query(_PREFLIGHT_PATH), _query(_POST_READ_PATH)]
    assert "pre-047-" in section


def test_the_preflight_lists_are_the_migration_constants() -> None:
    sql = _query(_PREFLIGHT_PATH)
    created = re.search(r"unnest\(ARRAY\[(.*?)\]\)", sql)
    assert created is not None
    assert re.findall(r"'public\.([a-z_]+)'", created.group(1)) == list(
        _MIGRATION.CREATED_TABLES
    )
    assert "public.builders" not in sql
    assert [f"public.{name}" for name in _MIGRATION.REFERENCED_TABLES] == re.findall(
        r"to_regclass\('(public\.[a-z_]+)'\) IS NULL", sql
    )
    assert "to_regprocedure('public.reject_credit_history_mutation()')" in sql


async def _read(
    cluster: _Cluster, name: str, sql: str, *, role: str | None = None
) -> list[tuple[Any, ...]]:
    """*sql* in a REPEATABLE READ, READ ONLY transaction, rolled back, as
    *role* when given (``SET LOCAL ROLE``: ``current_user`` is that role)."""

    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            if role is not None:
                await connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
                assert await connection.scalar(text("SELECT current_user")) == role
            rows = [tuple(row) for row in (await connection.exec_driver_sql(sql)).all()]
            await connection.rollback()
            return rows
    finally:
        await engine.dispose()


_PREFLIGHT_NAMES = [
    "alembic_version not exactly 046",
    "047 tables present",
    "047 prerequisites missing",
]
_POST_READ_NAMES = [
    "alembic_version is exactly 047",
    "append-only trigger on public.credit_enforcement_marks",
    "select and insert on public.credit_enforcement_marks",
]


async def _grant_app_reads(cluster: _Cluster, name: str, *privileges: str) -> None:
    """What production's application role has: SELECT on ``alembic_version``
    and, through the migration role's default privileges, *privileges* on
    every table the migrations create from now on."""

    owner = cluster.admin_url.username
    await _execute(
        cluster,
        name,
        f'GRANT USAGE ON SCHEMA public TO "{_APP_ROLE}"',
        f'GRANT SELECT ON alembic_version TO "{_APP_ROLE}"',
        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public '
        f'GRANT {", ".join(privileges)} ON TABLES TO "{_APP_ROLE}"',
    )


@pytest.mark.parametrize(
    ("statements", "counts"),
    [
        ((), (0, 0, 0)),
        (("INSERT INTO alembic_version VALUES ('047')",), (1, 0, 0)),
        (("UPDATE alembic_version SET version_num = '045'",), (1, 0, 0)),
        (
            ("CREATE TABLE credit_enforcement_marks (id UUID PRIMARY KEY)",),
            (0, 1, 0),
        ),
    ],
    ids=["at-046", "two-rows", "at-045", "table-by-hand"],
)
async def test_the_preflight_reads_zero_only_at_the_046_start_state(
    baseline: _Baseline,
    at_046: str,
    statements: tuple[str, ...],
    counts: tuple[int, int, int],
) -> None:
    cluster = baseline.cluster
    if statements:
        await _execute(cluster, at_046, *statements)
    sql = _PREFLIGHT_PATH.read_text(encoding="utf-8")
    rows = await _read(cluster, at_046, sql)
    assert [row[0] for row in rows] == _PREFLIGHT_NAMES
    assert tuple(row[1] for row in rows) == counts


async def test_the_preflight_reads_as_the_application_role_and_stops_at_047(
    baseline: _Baseline, at_046: str
) -> None:
    cluster = baseline.cluster
    sql = _PREFLIGHT_PATH.read_text(encoding="utf-8")
    await _grant_app_reads(cluster, at_046, "SELECT", "INSERT")
    rows = await _read(cluster, at_046, sql, role=_APP_ROLE)
    assert [(row[0], row[1]) for row in rows] == list(zip(_PREFLIGHT_NAMES, (0, 0, 0)))
    await _migrate(cluster.url(at_046), "upgrade", "047")
    rows = await _read(cluster, at_046, sql, role=_APP_ROLE)
    assert tuple(row[1] for row in rows) == (1, 1, 0)


@pytest.mark.parametrize(
    ("privileges", "after", "held"),
    [
        (("SELECT", "INSERT"), (), (True, True, True)),
        (("SELECT",), (), (True, True, False)),
        (("INSERT",), (), (True, True, False)),
        (
            ("SELECT", "INSERT"),
            (
                "ALTER TABLE credit_enforcement_marks DISABLE TRIGGER "
                "trg_credit_enforcement_marks_immutable",
            ),
            (True, False, True),
        ),
    ],
    ids=["held", "select-only", "insert-only", "trigger-disabled"],
)
async def test_the_post_read_as_current_user_holds_only_with_both_privileges(
    baseline: _Baseline,
    at_046: str,
    privileges: tuple[str, ...],
    after: tuple[str, ...],
    held: tuple[bool, bool, bool],
) -> None:
    cluster = baseline.cluster
    sql = _POST_READ_PATH.read_text(encoding="utf-8")
    await _grant_app_reads(cluster, at_046, *privileges)
    before = await _read(cluster, at_046, sql, role=_APP_ROLE)
    # At 046: the table is looked up by OID, so the read does not fail.
    assert [(row[0], row[1]) for row in before] == list(
        zip(_POST_READ_NAMES, (False, False, None))
    )
    await _migrate(cluster.url(at_046), "upgrade", "047")
    if after:
        await _execute(cluster, at_046, *after)
    rows = await _read(cluster, at_046, sql, role=_APP_ROLE)
    assert [row[0] for row in rows] == _POST_READ_NAMES
    assert tuple(row[1] for row in rows) == held


# ---------------------------------------------------------------------------
# ``status`` on PostgreSQL at 046 and at 047.
# ---------------------------------------------------------------------------


@dataclass
class _StatusRun:
    code: int
    document: dict[str, Any]
    printed: str
    statements: list[str]


async def _status(
    cluster: _Cluster, name: str, monkeypatch: pytest.MonkeyPatch
) -> _StatusRun:
    from src.config.settings import settings
    from src.ops import credit_charge_mark as ops
    from src.services import credit_switch

    monkeypatch.setattr(
        credit_switch._SWITCH, "_reading", credit_switch._SWITCH._reading
    )
    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    out, err = io.StringIO(), io.StringIO()
    try:
        code = await ops.amain(
            ["status", "--builder-id", str(_BUILDER)],
            session_factory=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
            stdout=out,
            stderr=err,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        await engine.dispose()
    (line,) = out.getvalue().splitlines()
    return _StatusRun(
        code, json.loads(line), out.getvalue() + err.getvalue(), statements
    )


def _assert_read_only(run: _StatusRun) -> None:
    """The switch read, then one READ ONLY transaction of SELECTs."""

    assert run.statements[0] == "SELECT version_num FROM alembic_version"
    body = run.statements[1:]
    assert body[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    for statement in body[1:]:
        assert statement.lstrip().upper().startswith("SELECT"), statement[:80]
        for found in re.finditer(r"api_credentials", statement):
            assert statement.startswith(
                "api_credentials ->> 'whatsapp_provider' AS whatsapp_provider",
                found.start(),
            ), statement


async def _tenant(
    cluster: _Cluster, name: str, credentials: dict[str, Any] | None
) -> UUID:
    tenant_id = uuid4()
    await _execute(
        cluster,
        name,
        "INSERT INTO tenants (id, name, api_key_hash, is_active, builder_id) "
        "VALUES (:id, :tenant_name, :hash, true, :builder)",
        id=tenant_id,
        tenant_name=f"Clinic {tenant_id.hex[:6]}",
        hash=uuid4().hex,
        builder=_BUILDER,
    )
    if credentials is not None:
        await _execute(
            cluster,
            name,
            "INSERT INTO clinic_configs (id, tenant_id, api_credentials) "
            "VALUES (:id, :tenant, CAST(:credentials AS JSONB))",
            id=uuid4(),
            tenant=tenant_id,
            credentials=json.dumps(credentials),
        )
    return tenant_id


async def test_status_at_046_reports_the_marks_table_absent(
    baseline: _Baseline, at_046: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await _status(baseline.cluster, at_046, monkeypatch)
    assert run.code == 0, run.document
    assert run.document["switch"] == {
        "effective": True,
        "reason": "schema_ready",
        "revision": "046",
    }
    assert run.document["marks"] == {
        "gate_open": False,
        "summary": "marks table absent",
        "latest": None,
    }
    assert run.document["builder"] == {"exists": True, "active": True}
    assert run.document["enforcement"]["origin"] == "none"
    assert run.document["lifecycle"]["open_debt_mc"] == 0
    assert run.document["coverage"] == {
        "usable": True,
        "reconciliation_failure": False,
    }
    assert not any("credit_enforcement_marks" in s for s in run.statements)
    _assert_read_only(run)


async def test_status_at_047_reads_the_marks_and_never_a_credential(
    baseline: _Baseline, at_047: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = baseline.cluster
    zapi = await _tenant(
        cluster,
        at_047,
        {"whatsapp_provider": "zapi", "zapi_token": _SECRET, "llm_api_key": _SECRET},
    )
    no_provider = await _tenant(cluster, at_047, {"llm_provider": "openai"})
    no_config = await _tenant(cluster, at_047, None)

    empty = await _status(cluster, at_047, monkeypatch)
    assert empty.code == 0, empty.document
    assert empty.document["switch"]["revision"] == "047"
    assert empty.document["marks"] == {
        "gate_open": True,
        "summary": "no marks",
        "latest": None,
    }
    expected = {str(zapi): "zapi", str(no_provider): None, str(no_config): None}
    assert empty.document["tenants"] == [
        {"tenant_id": tenant, "whatsapp_provider": expected[tenant]}
        for tenant in sorted(expected, key=UUID)
    ]
    _assert_read_only(empty)

    await _execute(cluster, at_047, _INSERT_MARK, **_mark(at=_AT))
    marked = await _status(cluster, at_047, monkeypatch)
    assert marked.document["marks"]["summary"] == "latest mark"
    assert marked.document["enforcement"]["origin"] == "mark"
    assert datetime.fromisoformat(marked.document["enforcement"]["effective_at"]) == _AT

    unmark = _mark(kind="unmark", at=_AT + timedelta(hours=2), reason="fim do piloto")
    await _execute(cluster, at_047, _INSERT_MARK, **unmark)
    unmarked = await _status(cluster, at_047, monkeypatch)
    assert unmarked.code == 0, unmarked.document
    assert unmarked.document["marks"]["latest"] == {
        "id": str(unmark["id"]),
        "kind": "unmark",
        "occurred_at": unmark["at"].isoformat(),
        "actor_user_id": str(_ACTOR),
        "reason": "fim do piloto",
    }
    assert unmarked.document["enforcement"] == {
        "global_at": None,
        "effective_at": None,
        "origin": "none",
    }
    for run in (empty, marked, unmarked):
        assert _SECRET not in run.printed
        for key in ("zapi_token", "llm_api_key", "llm_provider", "api_credentials"):
            assert key not in run.printed, key
        _assert_read_only(run)
    assert (
        await _scalar(cluster, at_047, "SELECT count(*) FROM credit_enforcement_marks")
        == 2
    )
