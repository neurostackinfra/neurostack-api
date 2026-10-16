"""Contract tests for migration 021's handoff SLA columns and constraints."""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.handoff import HandoffRecord

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20260928_000001_021_add_handoff_sla_lifecycle.py"
)
_ROOT = Path(__file__).resolve().parents[2]
_ADMIN_DATABASES = {"postgres"}
_LOCAL_SOCKET_DIRECTORIES = {"/run/postgresql", "/var/run/postgresql"}
_LOCAL_POSTGRES_PORTS = {5432, 55432}
_CHILD_PATTERN = re.compile(r"^neurostack_handoff_sla_smoke_[0-9a-f]{12}$")


class OperationRecorder:
    def __init__(self) -> None:
        self.added_columns: list[tuple[str, sa.Column[Any]]] = []
        self.created_constraints: list[tuple[str, str, str, dict[str, Any]]] = []
        self.executed_statements: list[str] = []
        self.executed_in_autocommit: list[bool] = []
        self.transaction_events: list[str] = []
        self.constraints_at_autocommit_start: list[int] = []
        self.dropped_constraints: list[tuple[str, str, str | None]] = []
        self.dropped_columns: list[tuple[str, str]] = []
        self._in_autocommit = False

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.added_columns.append((table_name, column))

    def create_check_constraint(
        self,
        name: str,
        table_name: str,
        condition: str,
        **dialect_kwargs: Any,
    ) -> None:
        self.created_constraints.append((name, table_name, condition, dialect_kwargs))

    def execute(self, statement: Any) -> None:
        self.executed_statements.append(str(statement))
        self.executed_in_autocommit.append(self._in_autocommit)

    def get_context(self) -> OperationRecorder:
        return self

    @contextmanager
    def autocommit_block(self) -> Iterator[None]:
        self.constraints_at_autocommit_start.append(len(self.created_constraints))
        self.transaction_events.append("begin_autocommit")
        self._in_autocommit = True
        try:
            yield
        finally:
            self._in_autocommit = False
            self.transaction_events.append("end_autocommit")

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
    setattr(migration, "op", recorder)
    migration.upgrade()
    return recorder


def _constraint_table(recorder: OperationRecorder) -> tuple[sa.Engine, sa.Table]:
    metadata = sa.MetaData()
    added_columns = [column for _, column in recorder.added_columns]
    constraints = [
        sa.CheckConstraint(condition, name=name)
        for name, _, condition, _ in recorder.created_constraints
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
    lifecycle_default = columns["lifecycle_version"].server_default
    assert lifecycle_default is not None
    assert str(getattr(lifecycle_default, "arg", lifecycle_default)) == "0"
    assert {name for name, _, _, _ in recorder.created_constraints} == {
        "ck_handoff_records_sla_target_seconds_range",
        "ck_handoff_records_lifecycle_version_nonnegative",
    }
    assert all(
        dialect_kwargs == {"postgresql_not_valid": True}
        for _, _, _, dialect_kwargs in recorder.created_constraints
    )
    assert recorder.executed_statements == [
        "ALTER TABLE handoff_records VALIDATE CONSTRAINT "
        "ck_handoff_records_sla_target_seconds_range",
        "ALTER TABLE handoff_records VALIDATE CONSTRAINT "
        "ck_handoff_records_lifecycle_version_nonnegative",
    ]
    assert recorder.executed_in_autocommit == [True, True]
    assert recorder.constraints_at_autocommit_start == [2]
    assert recorder.transaction_events == ["begin_autocommit", "end_autocommit"]


def test_offline_sql_commits_not_valid_constraints_before_validation() -> None:
    alembic_executable = Path(sys.executable).with_name("alembic")
    assert alembic_executable.is_file()
    environment = os.environ.copy()
    environment["DATABASE_URL"] = (
        "postgresql+asyncpg://offline:offline@localhost/offline"
    )
    completed = subprocess.run(
        [str(alembic_executable), "upgrade", "020:021", "--sql"],
        cwd=_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    sql = completed.stdout

    sla_not_valid = sql.index(
        "ADD CONSTRAINT ck_handoff_records_sla_target_seconds_range"
    )
    version_not_valid = sql.index(
        "ADD CONSTRAINT ck_handoff_records_lifecycle_version_nonnegative"
    )
    commit = sql.index("COMMIT;", version_not_valid)
    validate_sla = sql.index(
        "VALIDATE CONSTRAINT ck_handoff_records_sla_target_seconds_range"
    )
    validate_version = sql.index(
        "VALIDATE CONSTRAINT ck_handoff_records_lifecycle_version_nonnegative"
    )
    next_transaction = sql.index("BEGIN;", validate_version)

    assert (
        sla_not_valid
        < version_not_valid
        < commit
        < validate_sla
        < validate_version
        < next_transaction
    )


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
    setattr(migration, "op", recorder)

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


def _database_identity(url: URL) -> tuple[str, int, str | None]:
    query_host = url.query.get("host")
    query_port = url.query.get("port")
    host = str(query_host or url.host or "").lower()
    port = int(str(query_port or url.port or 5432))
    return host, port, url.database


def _validated_admin_url(raw_url: str, application_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("smoke URL must use PostgreSQL")
    if url.database not in _ADMIN_DATABASES:
        raise ValueError("smoke URL must use an allowlisted administrative database")

    if url.query:
        if url.host is not None or set(url.query) - {"host", "port"}:
            raise ValueError("smoke URL has unsafe connection parameters")
        socket_directory = str(url.query.get("host", ""))
        if socket_directory not in _LOCAL_SOCKET_DIRECTORIES:
            raise ValueError("smoke URL must use an allowlisted local socket")
        if int(str(url.query.get("port", "5432"))) not in _LOCAL_POSTGRES_PORTS:
            raise ValueError("smoke URL must use the local PostgreSQL port")
    else:
        try:
            is_loopback = ipaddress.ip_address(url.host or "").is_loopback
        except ValueError:
            is_loopback = (url.host or "").lower() == "localhost"
        if not is_loopback:
            raise ValueError("smoke URL host must be loopback")
        if (url.port or 5432) not in _LOCAL_POSTGRES_PORTS:
            raise ValueError("smoke URL must use the local PostgreSQL port")

    if _database_identity(url) == _database_identity(make_url(application_url)):
        raise ValueError("smoke URL must not be the application database")
    return url.set(drivername="postgresql+asyncpg")


def _validated_child_name(name: str) -> str:
    if _CHILD_PATTERN.fullmatch(name) is None:
        raise ValueError("unsafe smoke child database name")
    return name


async def _run_alembic(database_url: URL, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(
        hide_password=False
    ).replace("%", "%%")
    alembic_executable = Path(sys.executable).with_name("alembic")
    if not alembic_executable.is_file():
        raise RuntimeError("Alembic executable is unavailable beside the test Python")
    process = await asyncio.create_subprocess_exec(
        str(alembic_executable),
        *arguments,
        cwd=_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


@pytest.fixture(scope="module")
async def postgres_database() -> AsyncIterator[URL]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url, settings.database_url)
    child_name = _validated_child_name(
        f"neurostack_handoff_sla_smoke_{uuid4().hex[:12]}"
    )
    child_url = admin_url.set(database=child_name)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    created_database = False
    unexpected_backends: list[dict[str, Any]] = []
    try:
        async with admin_engine.connect() as connection:
            can_create = bool(
                (
                    await connection.execute(
                        sa.text(
                            "SELECT rolcreatedb OR rolsuper "
                            "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
            )
            if not can_create:
                raise PermissionError("smoke database user must have CREATEDB")
            quoted_name = connection.dialect.identifier_preparer.quote(child_name)
            await connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
            created_database = True
        yield child_url
    finally:
        try:
            if created_database:
                async with admin_engine.connect() as connection:
                    unexpected_backends = [
                        dict(row)
                        for row in (
                            await connection.execute(
                                sa.text(
                                    "SELECT pid, application_name, state "
                                    "FROM pg_stat_activity WHERE datname = :database "
                                    "AND pid <> pg_backend_pid() ORDER BY pid"
                                ),
                                {"database": child_name},
                            )
                        )
                        .mappings()
                        .all()
                    ]
                    await connection.execute(
                        sa.text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :database AND pid <> pg_backend_pid()"
                        ),
                        {"database": child_name},
                    )
                    quoted_name = connection.dialect.identifier_preparer.quote(
                        child_name
                    )
                    await connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        finally:
            await admin_engine.dispose()
        assert not unexpected_backends, (
            "unexpected child database sessions required termination: "
            f"{unexpected_backends}"
        )


def _inspect_handoff(connection: sa.Connection) -> dict[str, Any]:
    inspector = sa.inspect(connection)
    return {
        "columns": {
            column["name"]: column
            for column in inspector.get_columns("handoff_records")
        },
        "checks": {
            constraint["name"]
            for constraint in inspector.get_check_constraints("handoff_records")
        },
    }


def _inspect_check_validation(connection: sa.Connection) -> dict[str, bool]:
    return {
        str(row[0]): bool(row[1])
        for row in connection.execute(
            sa.text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conrelid = 'handoff_records'::regclass "
                "AND conname IN "
                "('ck_handoff_records_sla_target_seconds_range', "
                "'ck_handoff_records_lifecycle_version_nonnegative')"
            )
        ).all()
    }


@pytest.mark.parametrize(
    ("raw_url", "message"),
    [
        ("postgresql://db.example.com/postgres", "loopback"),
        ("postgresql://localhost/application", "administrative database"),
        ("postgresql://localhost/neurostack", "administrative database"),
        ("postgresql://localhost:6432/postgres", "local PostgreSQL port"),
        ("postgresql:///postgres?host=/tmp", "local socket"),
        (
            "postgresql:///postgres?host=/run/postgresql&sslmode=disable",
            "parameters",
        ),
        ("postgresql:///postgres?host=/run/postgresql&port=6432", "port"),
    ],
)
def test_admin_url_rejects_destructive_targets(
    raw_url: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _validated_admin_url(
            raw_url,
            "postgresql://localhost/application",
        )


def test_admin_url_rejects_application_database_even_with_different_user() -> None:
    with pytest.raises(ValueError, match="application database"):
        _validated_admin_url(
            "postgresql://admin@localhost/postgres",
            "postgresql://application@localhost/postgres",
        )


@pytest.mark.parametrize(
    "raw_url",
    [
        "postgresql://localhost/postgres",
        "postgresql:///postgres?host=/run/postgresql&port=5432",
        "postgresql://127.0.0.1:55432/postgres",
    ],
)
def test_admin_url_accepts_allowlisted_local_admin_targets(raw_url: str) -> None:
    validated = _validated_admin_url(
        raw_url,
        "postgresql://localhost/application",
    )

    assert validated.drivername == "postgresql+asyncpg"
    assert validated.database == "postgres"


def test_child_database_name_guard_accepts_generated_shape_only() -> None:
    valid = "neurostack_handoff_sla_smoke_0123456789ab"
    assert _validated_child_name(valid) == valid

    for invalid in (
        "neurostack_handoff_sla_smoke_short",
        "neurostack_handoff_sla_smoke_0123456789AB",
        "neurostack_handoff_sla_smoke_0123456789ab;drop",
        "other_0123456789ab",
    ):
        with pytest.raises(ValueError, match="unsafe smoke child database name"):
            _validated_child_name(invalid)


async def _seed_legacy_row(engine: AsyncEngine) -> str:
    tenant_id = str(uuid4())
    session_id = str(uuid4())
    handoff_id = str(uuid4())
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO tenants "
                "(id, name, slug, api_key_hash, is_active, config) "
                "VALUES (:id, 'SLA migration', :slug, :api_key_hash, true, '{}')"
            ),
            {"id": tenant_id, "slug": f"sla-{tenant_id}", "api_key_hash": tenant_id},
        )
        await connection.execute(
            sa.text(
                "INSERT INTO sessions "
                "(id, tenant_id, channel, cognitive_state, metadata, is_active, "
                "message_count) VALUES "
                "(:id, :tenant_id, 'test', '{}', '{}', true, 0)"
            ),
            {"id": session_id, "tenant_id": tenant_id},
        )
        await connection.execute(
            sa.text(
                "INSERT INTO handoff_records "
                "(id, tenant_id, session_id, reason, context, urgency, status, "
                "webhook_sent) VALUES "
                "(:id, :tenant_id, :session_id, 'migration-test', '{}', "
                "'normal', 'pending', false)"
            ),
            {"id": handoff_id, "tenant_id": tenant_id, "session_id": session_id},
        )
    return handoff_id


async def test_real_postgres_upgrade_and_downgrade_preserve_contract(
    postgres_database: URL,
) -> None:
    await _run_alembic(postgres_database, "upgrade", "020")
    engine = create_async_engine(postgres_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            before = await connection.run_sync(_inspect_handoff)
        handoff_id = await _seed_legacy_row(engine)

        await engine.dispose()
        await _run_alembic(postgres_database, "upgrade", "021")
        engine = create_async_engine(postgres_database, poolclass=NullPool)

        async with engine.connect() as connection:
            after = await connection.run_sync(_inspect_handoff)
            check_validation = await connection.run_sync(_inspect_check_validation)
            legacy = (
                await connection.execute(
                    sa.text(
                        "SELECT sla_target_seconds, updated_at, lifecycle_version "
                        "FROM handoff_records WHERE id = :id"
                    ),
                    {"id": handoff_id},
                )
            ).one()

        added_names = set(after["columns"]) - set(before["columns"])
        assert added_names == {
            "sla_target_seconds",
            "updated_at",
            "lifecycle_version",
        }
        assert after["columns"]["sla_target_seconds"]["nullable"] is True
        assert isinstance(after["columns"]["sla_target_seconds"]["type"], sa.Integer)
        assert after["columns"]["updated_at"]["nullable"] is True
        assert isinstance(after["columns"]["updated_at"]["type"], sa.DateTime)
        assert after["columns"]["updated_at"]["type"].timezone is True
        assert after["columns"]["lifecycle_version"]["nullable"] is False
        assert isinstance(after["columns"]["lifecycle_version"]["type"], sa.Integer)
        assert str(after["columns"]["lifecycle_version"]["default"]) == "0"
        assert after["checks"] >= {
            "ck_handoff_records_sla_target_seconds_range",
            "ck_handoff_records_lifecycle_version_nonnegative",
        }
        assert check_validation == {
            "ck_handoff_records_lifecycle_version_nonnegative": True,
            "ck_handoff_records_sla_target_seconds_range": True,
        }
        assert tuple(legacy) == (None, None, 0)

        for target in (60, 7200, None):
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "UPDATE handoff_records SET sla_target_seconds = :target "
                        "WHERE id = :id"
                    ),
                    {"id": handoff_id, "target": target},
                )

        for statement, parameters in (
            (
                "UPDATE handoff_records SET sla_target_seconds = :target "
                "WHERE id = :id",
                {"id": handoff_id, "target": 59},
            ),
            (
                "UPDATE handoff_records SET sla_target_seconds = :target "
                "WHERE id = :id",
                {"id": handoff_id, "target": 7201},
            ),
            (
                "UPDATE handoff_records SET lifecycle_version = -1 WHERE id = :id",
                {"id": handoff_id},
            ),
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(sa.text(statement), parameters)

        await engine.dispose()
        await _run_alembic(postgres_database, "downgrade", "020")
        engine = create_async_engine(postgres_database, poolclass=NullPool)
        async with engine.connect() as connection:
            downgraded = await connection.run_sync(_inspect_handoff)
            preserved_id = (
                await connection.execute(
                    sa.text("SELECT id FROM handoff_records WHERE id = :id"),
                    {"id": handoff_id},
                )
            ).scalar_one()

        assert not {
            "sla_target_seconds",
            "updated_at",
            "lifecycle_version",
        } & set(downgraded["columns"])
        assert str(preserved_id) == handoff_id
    finally:
        await engine.dispose()


def test_orm_columns_and_constraints_match_the_migration_contract() -> None:
    table = cast(sa.Table, HandoffRecord.__table__)

    assert table.c.sla_target_seconds.nullable is True
    assert table.c.updated_at.nullable is True
    assert table.c.lifecycle_version.nullable is False
    lifecycle_default = table.c.lifecycle_version.server_default
    assert lifecycle_default is not None
    assert str(getattr(lifecycle_default, "arg", lifecycle_default)) == "0"
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_handoff_records_sla_target_seconds_range",
        "ck_handoff_records_lifecycle_version_nonnegative",
    }
