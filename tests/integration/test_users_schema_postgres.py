"""PostgreSQL smoke for migrations 024 (users.role enum) and 025 (lower(email)).

Runs the real migration chain on a disposable child database: from 023 with a
VARCHAR role column, both upgrades refuse bad data without changing anything,
then convert the column, add the case-insensitive email index and downgrade.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.user import User, UserRole

_ROOT = Path(__file__).resolve().parents[2]
_CHILD_PATTERN = re.compile(r"^neurostack_users_schema_[0-9a-f]{12}$")
_LOCAL_SOCKET_DIRECTORIES = {"/run/postgresql", "/var/run/postgresql"}


def _database_identity(url: URL) -> tuple[str, int, str | None]:
    query_host = url.query.get("host")
    query_port = url.query.get("port")
    return (
        str(query_host or url.host or "").lower(),
        int(str(query_port or url.port or 5432)),
        url.database,
    )


def _validated_admin_url(raw_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("smoke URL must use PostgreSQL")
    if url.database != "postgres":
        raise ValueError("smoke URL must use the postgres administrative database")
    if url.query:
        if url.host is not None or set(url.query) - {"host", "port"}:
            raise ValueError("smoke URL has unsafe connection parameters")
        if str(url.query.get("host", "")) not in _LOCAL_SOCKET_DIRECTORIES:
            raise ValueError("smoke URL must use an allowlisted local socket")
        if str(url.query.get("port", "5432")) != "5432":
            raise ValueError("smoke URL must use the local PostgreSQL port")
    else:
        try:
            is_loopback = ipaddress.ip_address(url.host or "").is_loopback
        except ValueError:
            is_loopback = (url.host or "").lower() == "localhost"
        if not is_loopback or (url.port or 5432) != 5432:
            raise ValueError("smoke URL must use local PostgreSQL")
    if _database_identity(url) == _database_identity(make_url(settings.database_url)):
        raise ValueError("smoke URL must not be the application database")
    return url.set(drivername="postgresql+asyncpg")


async def _alembic(database_url: URL, *args: str) -> tuple[int, str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(
        hide_password=False
    ).replace("%", "%%")
    executable = Path(sys.executable).with_name("alembic")
    if not executable.is_file():
        raise RuntimeError("Alembic executable is unavailable beside the test Python")
    process = await asyncio.create_subprocess_exec(
        str(executable),
        *args,
        cwd=_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode is not None
    return process.returncode, output.decode()


@pytest.fixture(scope="module")
async def child_database_url() -> AsyncIterator[URL]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url)
    child_name = f"neurostack_users_schema_{uuid4().hex[:12]}"
    if _CHILD_PATTERN.fullmatch(child_name) is None:
        raise ValueError("unsafe users schema child database name")
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    created_database = False
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
        yield admin_url.set(database=child_name)
    finally:
        try:
            if created_database:
                async with admin_engine.connect() as connection:
                    quoted_name = connection.dialect.identifier_preparer.quote(
                        child_name
                    )
                    await connection.exec_driver_sql(
                        f"DROP DATABASE {quoted_name} WITH (FORCE)"
                    )
        finally:
            await admin_engine.dispose()


async def _state(engine: AsyncEngine) -> tuple[str, str, int]:
    async with engine.connect() as connection:
        column_type = await connection.scalar(
            sa.text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'role'"
            )
        )
        version = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
        index_count = await connection.scalar(
            sa.text(
                "SELECT count(*) FROM pg_indexes "
                "WHERE indexname = 'uq_users_email_lower'"
            )
        )
    return str(column_type), str(version), int(index_count or 0)


async def _execute(engine: AsyncEngine, statement: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(sa.text(statement))


async def test_role_enum_and_email_index_migrations(
    child_database_url: URL, alembic_head: str
) -> None:
    code, output = await _alembic(child_database_url, "upgrade", "023")
    assert code == 0, output
    engine = create_async_engine(child_database_url, poolclass=NullPool)
    try:
        await _execute(
            engine,
            "INSERT INTO tenants (id, name, api_key_hash, config, is_active) "
            "VALUES (gen_random_uuid(), 'smoke', 'smoke-hash', '{}', true)",
        )
        for email, role in [
            ("admin@example.com", "clinic_admin"),
            ("root@example.com", "super_admin"),
            ("investor@example.com", "investor"),
            ("builder@example.com", "builder"),
            ("dup@example.com", "clinic_admin"),
            ("Dup@example.com", "clinic_admin"),
        ]:
            await _execute(
                engine,
                "INSERT INTO users (id, tenant_id, email, password_hash, role, "
                "is_active) SELECT gen_random_uuid(), id, "
                f"'{email}', 'x', '{role}', true FROM tenants",
            )
        assert await _state(engine) == ("varchar", "023", 0)

        # A case-only duplicate aborts 025, and the single transaction also
        # undoes 024: nothing changes.
        code, output = await _alembic(child_database_url, "upgrade", "head")
        assert code != 0
        assert "belong to more than one user when case is ignored" in output
        assert "dup@example.com" not in output.lower()  # emails are never printed
        assert await _state(engine) == ("varchar", "023", 0)

        # A role outside the enum aborts 024 the same way.
        await _execute(engine, "DELETE FROM users WHERE email = 'Dup@example.com'")
        await _execute(
            engine, "UPDATE users SET role = 'ghost' WHERE email = 'dup@example.com'"
        )
        code, output = await _alembic(child_database_url, "upgrade", "head")
        assert code != 0
        assert "outside the userrole enum" in output
        assert await _state(engine) == ("varchar", "023", 0)

        await _execute(engine, "DELETE FROM users WHERE email = 'dup@example.com'")
        code, output = await _alembic(child_database_url, "upgrade", "head")
        assert code == 0, output
        assert await _state(engine) == ("userrole", alembic_head, 1)

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            builders = (
                await session.execute(
                    sa.select(User.email).where(User.role == UserRole.builder)
                )
            ).scalars()
            assert list(builders) == ["builder@example.com"]
        with pytest.raises(IntegrityError, match="uq_users_email_lower"):
            await _execute(
                engine,
                "INSERT INTO users (id, tenant_id, email, password_hash, role, "
                "is_active) SELECT gen_random_uuid(), id, 'ADMIN@example.com', "
                "'x', 'clinic_admin', true FROM tenants",
            )

        code, output = await _alembic(child_database_url, "downgrade", "023")
        assert code == 0, output
        assert await _state(engine) == ("varchar", "023", 0)
        async with engine.connect() as connection:
            roles = (
                await connection.execute(
                    sa.text("SELECT role FROM users ORDER BY email")
                )
            ).scalars()
            assert list(roles) == ["clinic_admin", "builder", "investor", "super_admin"]
    finally:
        await engine.dispose()
