"""Proofs of ``python -m src.ops.credit_charge_mark status`` (cobranca-credito-
por-builder Group 1, the bridge release).

Without a database: the argument contract (every malformed invocation is
``unconfigured``/``usage`` before any session is opened), the closed result
vocabulary and exit codes, the switch-off path doing no database work, the
printable provider vocabulary, the module's boundary (its name, what it never
names or imports) and that nothing in ``src`` but the bridge's own files names
the marks table, and the real ``python -m`` entry point printing exactly one
stdout line.

On SQLite (``create_all`` plus an ``alembic_version`` row, so the real switch
reads the revision): ``status`` with the marks table absent (046: the gate is
closed and the table is never touched), empty (047), and with mark and unmark
events inserted by SQL; the effective instant and its origin (global first,
then a latest ``mark``, else none); the linked tenants with and without a
provider; an unknown builder; and that no credential key or value other than
``whatsapp_provider`` reaches the output or the log, every statement being a
SELECT and every mention of ``api_credentials`` the one ``->>`` key read.
``tests/integration/test_credit_enforcement_marks_migration_postgres.py``
repeats ``status`` on PostgreSQL at 046 and at 047.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import DateTime, String, bindparam, event, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.config.settings import settings
from src.models.base import Base
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.content_retention import CreditLifecycleEpisode
from src.models.credit import (
    ENFORCEMENT_STATE_ID,
    CreditAccount,
    CreditDebt,
    CreditEffect,
    CreditEnforcementState,
    CreditLedgerEntry,
)
from src.models.tenant import Tenant
from src.ops import credit_charge_mark as ops
from src.repositories.credit import BUILDER_TENANT_PROVIDERS_SQL
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import credit_switch

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "ops" / "credit_charge_mark.py"
BUILDER = UUID("4a2e1d5f-6b7c-4d8e-9f0a-1b2c3d4e5f60")
ACTOR = UUID("5b3f2e60-7c8d-4e9f-8a1b-2c3d4e5f6071")
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
# Credential values a tenant may hold; none may ever be printed or logged.
SECRETS = (
    "zapi-token-NEVER-PRINT-71c9",
    "zapi-client-token-NEVER-PRINT-55aa",
    "meta-permanent-token-NEVER-PRINT-0b1e",
    "sk-llm-key-NEVER-PRINT-9f3d",
    "provider-value-that-is-a-secret-NEVER-PRINT",
)
CREDENTIAL_KEYS = (
    "zapi_instance_id",
    "zapi_token",
    "zapi_client_token",
    "meta_permanent_token",
    "llm_api_key",
)


class _Untouchable:
    """A session factory that fails the test when used."""

    def __call__(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("no database work was allowed here")


async def _invoke(
    argv: list[str], session_factory: Any = None
) -> tuple[int, dict[str, Any], str]:
    out, err = io.StringIO(), io.StringIO()
    code = await ops.amain(
        argv,
        session_factory=_Untouchable() if session_factory is None else session_factory,
        stdout=out,
        stderr=err,
    )
    (line,) = out.getvalue().splitlines()
    document = json.loads(line)
    assert document["exit_code"] == code
    return code, document, out.getvalue() + err.getvalue()


def _status(builder: UUID = BUILDER) -> list[str]:
    return ["status", "--builder-id", str(builder)]


# ---------------------------------------------------------------------------
# Argument contract and result vocabulary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--help"],
        ["status"],
        ["status", "--builder-id"],
        ["status", "--builder-id", str(BUILDER).upper()],
        ["status", "--builder-id", "{" + str(BUILDER) + "}"],
        ["status", "--builder-id", "none"],
        ["status", "--builder-id", str(BUILDER), "extra"],
        ["status", "--builder", str(BUILDER)],
        ["mark", "--builder-id", str(BUILDER)],
        ["unmark", "--builder-id", str(BUILDER)],
    ],
    ids=lambda argv: " ".join(argv)[:60] or "empty",
)
async def test_a_malformed_invocation_is_usage_before_any_work(
    argv: list[str],
) -> None:
    code, document, _ = await _invoke(argv)
    assert code == ops.EXIT_CODES["unconfigured"] == 30
    assert document["verdict"] == "unconfigured"
    assert document["reasons"] == ["usage"]
    assert document["command"] is None


def test_the_bridge_has_only_the_read_only_status_command() -> None:
    assert ops.COMMANDS == ("status",)
    args = ops.parse_arguments(_status())
    assert (args.command, args.builder_id) == ("status", BUILDER)


def test_the_exit_codes_are_the_published_literals() -> None:
    assert dict(ops.EXIT_CODES) == {
        "ok": 0,
        "refused": 20,
        "switch_off": 21,
        "unconfigured": 30,
        "error": 50,
    }


def test_the_result_vocabulary_is_closed() -> None:
    with pytest.raises(ValueError, match="verdict"):
        ops.RunResult(command="status", verdict="armed")
    with pytest.raises(ValueError, match="command"):
        ops.RunResult(command="mark", verdict="refused")
    with pytest.raises(ValueError, match="reason"):
        ops.RunResult(command="status", verdict="refused", reasons=("other",))
    with pytest.raises(ValueError, match="success"):
        ops.RunResult(command=None, verdict="ok")


async def test_with_the_configuration_off_nothing_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        credit_switch._SWITCH, "_reading", credit_switch._SWITCH._reading
    )
    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    code, document, _ = await _invoke(_status())
    assert code == ops.EXIT_CODES["switch_off"] == 21
    assert document["verdict"] == "switch_off"
    assert document["reasons"] == ["credit_switch_off"]
    assert document["switch"] == {
        "effective": False,
        "reason": "config_disabled",
        "revision": None,
    }
    assert document["marks"] is None and document["tenants"] is None


@pytest.mark.parametrize(
    ("raw", "printed"),
    [
        (None, None),
        ("zapi", "zapi"),
        ("meta_cloud", "meta_cloud"),
        ("uazapi", "uazapi"),
        ("evolution", "evolution"),
        ("meta", "unrecognized"),
        ("ZAPI", "unrecognized"),
        ("", "unrecognized"),
        (SECRETS[-1], "unrecognized"),
        ('{"zapi_token": "x"}', "unrecognized"),
    ],
)
def test_only_a_known_provider_name_is_printed(
    raw: str | None, printed: str | None
) -> None:
    assert ops.provider_label(raw) == printed


def test_the_enforcement_origin_is_global_then_mark_then_none() -> None:
    from src.repositories.credit import EnforcementMarkRow

    def row(kind: str, at: datetime) -> EnforcementMarkRow:
        return EnforcementMarkRow(uuid4(), BUILDER, kind, at, ACTOR, "r")

    later = T0 + timedelta(hours=1)
    assert ops.enforcement_document(None, None)["origin"] == "none"
    assert ops.enforcement_document(None, row("unmark", T0)) == {
        "global_at": None,
        "effective_at": None,
        "origin": "none",
    }
    assert ops.enforcement_document(None, row("mark", later)) == {
        "global_at": None,
        "effective_at": later.isoformat(),
        "origin": "mark",
    }
    assert ops.enforcement_document(T0, row("mark", later)) == {
        "global_at": T0.isoformat(),
        "effective_at": T0.isoformat(),
        "origin": "global",
    }


# ---------------------------------------------------------------------------
# The module's boundary (no database).
# ---------------------------------------------------------------------------


def test_the_module_never_names_the_arming_task_or_its_writers() -> None:
    """The L10 fence (tests/integration/test_credit_enforcement_rollout_fence.py)
    stays unchanged: this module is not named like the arming task, never
    names its writers or its module, and builds no route."""

    assert "credit_enforcement" not in MODULE.name
    source = MODULE.read_text(encoding="utf-8")
    for forbidden in ("arm_enforcement", "accept_cutoff", "src.ops.credit_enforcement"):
        assert forbidden not in source, forbidden
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any(name.startswith(("fastapi", "starlette")) for name in imported)
    assert not any("route" in name.lower() for name in imported)


# The bridge's files that may name the marks table: its model (and the
# package export), the two plain reads, the revision gate and this command.
_MARK_FILES = frozenset(
    {
        "src/models/credit.py",
        "src/models/__init__.py",
        "src/repositories/credit.py",
        "src/services/credit_switch.py",
        "src/ops/credit_charge_mark.py",
    }
)
_MARK_NAMES = re.compile(
    r"credit_enforcement_marks|CreditEnforcementMark\b|latest_enforcement_mark"
    r"|enforcement_mark_gate_open|ENFORCEMENT_MARK_SCHEMA_MIN_REVISION"
)
_MARK_WRITES = re.compile(
    r"INSERT\s+INTO\s+credit_enforcement_marks|UPDATE\s+credit_enforcement_marks"
    r"|DELETE\s+FROM\s+credit_enforcement_marks|insert\(\s*CreditEnforcementMark"
    r"|(?<!class )CreditEnforcementMark\(",
    re.IGNORECASE,
)


def test_nothing_but_the_bridge_files_names_the_marks_and_nothing_writes_them() -> None:
    """Deliverable 2: no code other than ``status`` reads or writes the table
    in this release (its readers are the bridge's own files)."""

    named = set()
    for path in sorted((ROOT / "src").rglob("*.py")):
        text_ = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT).as_posix()
        if _MARK_NAMES.search(text_):
            named.add(relative)
        assert _MARK_WRITES.search(text_) is None, relative
    assert named == _MARK_FILES
    callers = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if re.search(
            r"\.latest_enforcement_mark\(|(?<!def )\benforcement_mark_gate_open\(",
            path.read_text(encoding="utf-8"),
        )
    }
    assert callers == {"src/ops/credit_charge_mark.py"}


def test_the_provider_read_names_only_the_whatsapp_provider_key() -> None:
    assert BUILDER_TENANT_PROVIDERS_SQL == (
        "SELECT t.id AS tenant_id, "
        "c.api_credentials ->> 'whatsapp_provider' AS whatsapp_provider "
        "FROM tenants AS t "
        "LEFT JOIN clinic_configs AS c ON c.tenant_id = t.id "
        "WHERE t.builder_id = :builder_id "
        "ORDER BY t.id"
    )
    for path in (ROOT / "src/repositories/credit.py", MODULE):
        source = path.read_text(encoding="utf-8")
        # No ORM access to the credentials, and every SQL use is the one key.
        assert "ClinicConfigTable" not in source, path.name
        assert ".api_credentials[" not in source, path.name
        uses = [
            m.start() for m in re.finditer(r"(?<![\w.])c\.api_credentials\b", source)
        ]
        assert len(uses) == (1 if path.name == "credit.py" else 0), path.name
        for start in uses:
            assert source.startswith(
                "c.api_credentials ->> 'whatsapp_provider' AS whatsapp_provider", start
            ), path.name


# ---------------------------------------------------------------------------
# The real entry point.
# ---------------------------------------------------------------------------


def _subprocess(tmp_path: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """``python -m src.ops.credit_charge_mark`` from a directory with no
    ``.env`` (``src`` linked in) and a database URL nothing listens on."""

    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "src").symlink_to(ROOT / "src", target_is_directory=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ENVIRONMENT": "staging",
        "SECRET_KEY": "s" * 48,
        "DATABASE_URL": "postgresql+asyncpg://nobody@127.0.0.1:9/none",
        "CREDIT_HOT_PATH_ENABLED": "false",
    }
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return subprocess.run(
        [sys.executable, "-m", "src.ops.credit_charge_mark", *argv],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize(
    ("argv", "verdict"),
    [(["status"], "unconfigured"), (_status(), "switch_off")],
)
def test_the_entry_point_prints_one_document(
    tmp_path: Path, argv: list[str], verdict: str
) -> None:
    result = _subprocess(tmp_path, argv)
    assert result.returncode == ops.EXIT_CODES[verdict], result.stderr[-2000:]
    (line,) = result.stdout.splitlines()
    document = json.loads(line)
    assert document["schema"] == ops.SCHEMA
    assert document["verdict"] == verdict
    logs = [
        json.loads(raw)
        for raw in result.stderr.splitlines()
        if raw.startswith("{") and json.loads(raw).get("event") == ops.LOG_EVENT
    ]
    assert len(logs) == 1 and logs[0]["verdict"] == verdict


# ---------------------------------------------------------------------------
# ``status`` on SQLite: the real switch reads alembic_version.
# ---------------------------------------------------------------------------


@dataclass
class _World:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    statements: list[str]


@pytest.fixture
async def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_World]:
    _patch_jsonb_for_sqlite()
    monkeypatch.setattr(
        credit_switch._SWITCH, "_reading", credit_switch._SWITCH._reading
    )
    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'marks.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
    sessions = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with sessions() as session, session.begin():
        session.add(Builder(id=BUILDER, name="Marked", is_active=True, status="active"))
    statements: list[str] = []
    try:
        yield _World(engine=engine, sessions=sessions, statements=statements)
    finally:
        await engine.dispose()


async def _at(world: _World, revision: str) -> None:
    """``alembic_version`` at *revision*; below 047 the marks table is
    dropped, so any read of it would fail the command."""

    async with world.engine.begin() as connection:
        await connection.execute(text("DELETE FROM alembic_version"))
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"),
            {"revision": revision},
        )
        if revision < "047":
            await connection.execute(
                text("DROP TABLE IF EXISTS credit_enforcement_marks")
            )


_INSERT_MARK = text(
    "INSERT INTO credit_enforcement_marks "
    "(id, builder_id, kind, occurred_at, actor_user_id, reason) "
    "VALUES (:id, :builder_id, :kind, :occurred_at, :actor_user_id, :reason)"
).bindparams(
    bindparam("id", type_=PG_UUID(as_uuid=True)),
    bindparam("builder_id", type_=PG_UUID(as_uuid=True)),
    bindparam("kind", type_=String()),
    bindparam("occurred_at", type_=DateTime(timezone=True)),
    bindparam("actor_user_id", type_=PG_UUID(as_uuid=True)),
    bindparam("reason", type_=String()),
)


async def _mark(world: _World, kind: str, at: datetime, reason: str) -> UUID:
    mark_id = uuid4()
    async with world.engine.begin() as connection:
        await connection.execute(
            _INSERT_MARK,
            {
                "id": mark_id,
                "builder_id": BUILDER,
                "kind": kind,
                "occurred_at": at,
                "actor_user_id": ACTOR,
                "reason": reason,
            },
        )
    return mark_id


async def _tenant(
    world: _World,
    *,
    credentials: dict[str, Any] | None,
    builder: UUID | None = BUILDER,
) -> UUID:
    tenant_id = uuid4()
    async with world.sessions() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                name=f"Clinic {tenant_id.hex[:6]}",
                api_key_hash=uuid4().hex,
                builder_id=builder,
            )
        )
        await session.flush()
        if credentials is not None:
            session.add(
                ClinicConfigTable(tenant_id=tenant_id, api_credentials=credentials)
            )
    return tenant_id


async def _run(
    world: _World, builder: UUID = BUILDER
) -> tuple[int, dict[str, Any], str]:
    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        world.statements.append(statement)

    event.listen(world.engine.sync_engine, "before_cursor_execute", record)
    try:
        return await _invoke(_status(builder), session_factory=world.sessions)
    finally:
        event.remove(world.engine.sync_engine, "before_cursor_execute", record)


def _assert_read_only(world: _World) -> None:
    assert world.statements
    for statement in world.statements:
        assert statement.lstrip().upper().startswith("SELECT"), statement[:80]
        for found in re.finditer(r"api_credentials", statement):
            assert statement.startswith(
                "api_credentials ->> 'whatsapp_provider' AS whatsapp_provider",
                found.start(),
            ), statement


async def test_status_with_the_marks_table_absent(world: _World) -> None:
    await _at(world, "046")
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["verdict"] == "ok" and document["reasons"] == []
    assert document["switch"] == {
        "effective": True,
        "reason": "schema_ready",
        "revision": "046",
    }
    assert document["marks"] == {
        "gate_open": False,
        "summary": "marks table absent",
        "latest": None,
    }
    # The rest is still read.
    assert document["builder"] == {"exists": True, "active": True}
    assert document["enforcement"] == {
        "global_at": None,
        "effective_at": None,
        "origin": "none",
    }
    assert document["lifecycle"] == {
        "open_episode": None,
        "open_debt_mc": 0,
        "open_debts": 0,
    }
    assert document["tenants"] == []
    assert set(document["coverage"]) == {"usable", "reconciliation_failure"}
    assert not any("credit_enforcement_marks" in s for s in world.statements)
    _assert_read_only(world)


async def test_status_with_the_marks_table_empty(world: _World) -> None:
    await _at(world, "047")
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["switch"]["revision"] == "047"
    assert document["marks"] == {
        "gate_open": True,
        "summary": "no marks",
        "latest": None,
    }
    assert document["enforcement"]["origin"] == "none"
    assert any("credit_enforcement_marks" in s for s in world.statements)
    _assert_read_only(world)


async def test_status_reads_the_latest_of_a_mark_and_an_unmark(world: _World) -> None:
    await _at(world, "047")
    marked = T0 - timedelta(days=2)
    await _mark(world, "mark", marked, "piloto de cobrança")
    code, document, _ = await _run(world)
    assert code == 0, document
    latest = document["marks"]["latest"]
    assert document["marks"]["summary"] == "latest mark"
    assert latest["kind"] == "mark"
    assert latest["actor_user_id"] == str(ACTOR)
    assert latest["reason"] == "piloto de cobrança"
    assert datetime.fromisoformat(latest["occurred_at"]) == marked
    assert document["enforcement"]["origin"] == "mark"
    assert datetime.fromisoformat(document["enforcement"]["effective_at"]) == marked

    unmarked_id = await _mark(world, "unmark", T0 - timedelta(days=1), "fim do piloto")
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["marks"]["summary"] == "latest unmark"
    assert document["marks"]["latest"]["id"] == str(unmarked_id)
    assert document["marks"]["latest"]["reason"] == "fim do piloto"
    assert document["enforcement"] == {
        "global_at": None,
        "effective_at": None,
        "origin": "none",
    }

    # mark -> unmark -> mark: the latest event wins, at its own instant.
    remarked = T0 - timedelta(hours=1)
    await _mark(world, "mark", remarked, "de novo")
    code, document, _ = await _run(world)
    assert document["marks"]["summary"] == "latest mark"
    assert datetime.fromisoformat(document["enforcement"]["effective_at"]) == remarked
    _assert_read_only(world)


async def test_the_global_instant_wins_over_a_mark(world: _World) -> None:
    await _at(world, "047")
    await _mark(world, "mark", T0, "piloto")
    armed = T0 - timedelta(days=10)
    async with world.sessions() as session, session.begin():
        session.add(
            CreditEnforcementState(
                id=ENFORCEMENT_STATE_ID,
                credit_enforcement_at=armed,
                set_by="test",
                set_at=armed,
            )
        )
    code, document, _ = await _run(world)
    assert code == 0, document
    assert document["enforcement"]["origin"] == "global"
    assert datetime.fromisoformat(document["enforcement"]["global_at"]) == armed
    assert datetime.fromisoformat(document["enforcement"]["effective_at"]) == armed
    assert document["marks"]["summary"] == "latest mark"


async def test_the_linked_tenants_with_and_without_a_provider(world: _World) -> None:
    await _at(world, "047")
    zapi = await _tenant(world, credentials={"whatsapp_provider": "zapi"})
    meta = await _tenant(world, credentials={"whatsapp_provider": "meta_cloud"})
    no_key = await _tenant(world, credentials={"llm_provider": "openai"})
    no_config = await _tenant(world, credentials=None)
    await _tenant(world, credentials={"whatsapp_provider": "zapi"}, builder=None)
    code, document, _ = await _run(world)
    assert code == 0, document
    expected = {
        str(zapi): "zapi",
        str(meta): "meta_cloud",
        str(no_key): None,
        str(no_config): None,
    }
    assert document["tenants"] == [
        {"tenant_id": tenant_id, "whatsapp_provider": expected[tenant_id]}
        for tenant_id in sorted(expected, key=UUID)
    ]
    _assert_read_only(world)


async def test_no_credential_but_the_provider_reaches_the_output_or_the_log(
    world: _World,
) -> None:
    await _at(world, "047")
    await _tenant(
        world,
        credentials={
            "whatsapp_provider": "zapi",
            "zapi_instance_id": "instance-77",
            "zapi_token": SECRETS[0],
            "zapi_client_token": SECRETS[1],
            "llm_api_key": SECRETS[3],
        },
    )
    await _tenant(
        world,
        credentials={
            "whatsapp_provider": "meta_cloud",
            "meta_permanent_token": SECRETS[2],
        },
    )
    # A credential stored under the provider key itself is not a provider.
    await _tenant(world, credentials={"whatsapp_provider": SECRETS[4]})
    code, document, printed = await _run(world)
    assert code == 0, document
    assert sorted(str(row["whatsapp_provider"]) for row in document["tenants"]) == [
        "meta_cloud",
        "unrecognized",
        "zapi",
    ]
    for secret in (*SECRETS, "instance-77"):
        assert secret not in printed, secret
    for key in CREDENTIAL_KEYS:
        assert key not in printed, key
    assert "api_credentials" not in printed
    _assert_read_only(world)


async def test_status_reports_the_open_episode_and_the_open_debt(
    world: _World,
) -> None:
    """The lifecycle part reads the stored rows as the credit read routes do:
    an open exhaustion episode and a 5-credit debt (its row and its ``debt``
    ledger entry) of the builder."""

    await _at(world, "047")
    account, effect = uuid4(), f"debit:test:{uuid4()}"
    exhausted = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=5)
    async with world.sessions() as session, session.begin():
        session.add(CreditAccount(id=account, builder_id=BUILDER))
        await session.flush()
        session.add(
            CreditEffect(builder_id=BUILDER, effect_id=effect, effect_kind="debit")
        )
        await session.flush()
        session.add_all(
            [
                CreditLedgerEntry(
                    builder_id=BUILDER,
                    account_id=account,
                    effect_id=effect,
                    lot_id=None,
                    entry_type="debt",
                    amount_mc=5_000,
                    occurred_at=exhausted,
                ),
                CreditDebt(
                    builder_id=BUILDER,
                    account_id=account,
                    debt_source_kind="debit",
                    debt_source_effect_id=effect,
                    original_grant_lot_id=None,
                    amount_mc=5_000,
                    created_at=exhausted,
                ),
                CreditLifecycleEpisode(
                    builder_id=BUILDER,
                    account_id=account,
                    opened_effect_id=effect,
                    exhausted_at=exhausted,
                    suspension_due_at=exhausted + timedelta(hours=72),
                    deletion_due_at=exhausted + timedelta(days=30),
                ),
            ]
        )
    code, document, _ = await _run(world)
    assert code == 0, document
    lifecycle = document["lifecycle"]
    assert (lifecycle["open_debt_mc"], lifecycle["open_debts"]) == (5_000, 1)
    episode = lifecycle["open_episode"]
    assert datetime.fromisoformat(episode["exhausted_at"]) == exhausted
    assert datetime.fromisoformat(episode["suspension_due_at"]) == exhausted + (
        timedelta(hours=72)
    )
    _assert_read_only(world)


async def test_an_unknown_builder_is_refused(world: _World) -> None:
    await _at(world, "047")
    code, document, _ = await _run(world, builder=uuid4())
    assert code == ops.EXIT_CODES["refused"] == 20
    assert document["verdict"] == "refused"
    assert document["reasons"] == ["builder_not_found"]
    assert document["marks"] is None and document["tenants"] is None
    _assert_read_only(world)
