"""PostgreSQL evidence for the handoff SLA snapshot migration."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.handoff import HandoffRecord

_ROOT = Path(__file__).resolve().parents[2]
_ADMIN_DATABASES = {"postgres"}
_LOCAL_SOCKET_DIRECTORIES = {"/run/postgresql", "/var/run/postgresql"}
_CHILD_PATTERN = re.compile(r"^neurostack_handoff_sla_smoke_[0-9a-f]{12}$")


def _database_identity(url: URL) -> tuple[str, int, str | None]:
    query_host = url.query.get("host")
    query_port = url.query.get("port")
    host = str(query_host or url.host or "").lower()
    port = int(query_port or url.port or 5432)
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
        if str(url.query.get("port", "5432")) != "5432":
            raise ValueError("smoke URL must use the local PostgreSQL port")
    else:
        try:
            is_loopback = ipaddress.ip_address(url.host or "").is_loopback
        except ValueError:
            is_loopback = (url.host or "").lower() == "localhost"
        if not is_loopback:
            raise ValueError("smoke URL host must be loopback")
        if (url.port or 5432) != 5432:
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
    return dict(
        connection.execute(
            sa.text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conrelid = 'handoff_records'::regclass "
                "AND conname IN "
                "('ck_handoff_records_sla_target_seconds_range', "
                "'ck_handoff_records_lifecycle_version_nonnegative')"
            )
        ).all()
    )


@pytest.mark.parametrize(
    ("raw_url", "message"),
    [
        ("postgresql://db.example.com/postgres", "loopback"),
        ("postgresql://localhost/application", "administrative database"),
        ("postgresql://localhost/neurostack", "administrative database"),
        ("postgresql://localhost:6432/postgres", "local PostgreSQL port"),
        ("postgresql:///postgres?host=/tmp", "local socket"),
        ("postgresql:///postgres?host=/run/postgresql&sslmode=disable", "parameters"),
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
    table = HandoffRecord.__table__

    assert table.c.sla_target_seconds.nullable is True
    assert table.c.updated_at.nullable is True
    assert table.c.lifecycle_version.nullable is False
    assert str(table.c.lifecycle_version.server_default.arg) == "0"
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_handoff_records_sla_target_seconds_range",
        "ck_handoff_records_lifecycle_version_nonnegative",
    }
