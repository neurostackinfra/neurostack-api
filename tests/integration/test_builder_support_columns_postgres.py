"""PostgreSQL migration smoke for builder.support_email and tenants.contact_phone."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.builder import Builder
from src.models.tenant import Tenant

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _ROOT
    / "alembic"
    / "versions"
    / "20260906_000001_019_builder_support_email_and_clinic_phone.py"
)
_ADMIN_DATABASES = {"postgres"}
_CHILD_PATTERN = re.compile(r"^neurostack_io_smoke_[0-9a-f]{12}$")


def _same_database(left: URL, right: URL) -> bool:
    return (
        left.host,
        left.port,
        left.username,
        left.database,
    ) == (
        right.host,
        right.port,
        right.username,
        right.database,
    )


def _validated_admin_url(raw_url: str, application_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("smoke URL must use PostgreSQL")
    try:
        is_loopback = ipaddress.ip_address(url.host or "").is_loopback
    except ValueError:
        is_loopback = (url.host or "").lower() == "localhost"
    if not is_loopback:
        raise ValueError("smoke URL host must be loopback")
    if _same_database(url, make_url(application_url)):
        raise ValueError("smoke URL must not be the application database")
    if url.database not in _ADMIN_DATABASES:
        raise ValueError("smoke URL must use an allowlisted administrative database")
    return url.set(drivername="postgresql+asyncpg")


def _validated_child_name(name: str) -> str:
    if _CHILD_PATTERN.fullmatch(name) is None:
        raise ValueError("unsafe smoke child database name")
    return name


def _require_createdb(can_create_database: bool) -> None:
    if not can_create_database:
        raise PermissionError("smoke database user must have CREATEDB")


async def _run_alembic(database_url: URL, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
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


def test_migration_declares_expected_revision_chain() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "019"' in source
    assert 'down_revision: Union[str, None] = "018"' in source


def test_migration_and_orm_share_the_support_email_contract() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    column = Builder.__table__.c.support_email

    # ORM must expose the column exactly as the migration declares it.
    assert column.name == "support_email"
    assert column.type.length == 255
    assert column.nullable is True
    assert column.server_default is None

    upgrade_source, downgrade_source = source.split("def downgrade() -> None:", 1)

    # Upgrade must add the column on the builders table with matching type/nullability
    # and must NOT set a server_default (any accidental default would rewrite existing
    # rows and violate the "no data change" acceptance criterion).
    assert '"builders"' in upgrade_source
    assert '"support_email"' in upgrade_source
    assert "sa.String(length=255)" in upgrade_source
    assert "nullable=True" in upgrade_source
    assert "server_default" not in upgrade_source

    # Downgrade must drop the column symmetrically.
    assert 'op.drop_column("builders", "support_email")' in downgrade_source


def test_migration_and_orm_share_the_contact_phone_contract() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    column = Tenant.__table__.c.contact_phone

    assert column.name == "contact_phone"
    assert column.type.length == 32
    assert column.nullable is True
    assert column.server_default is None

    upgrade_source, downgrade_source = source.split("def downgrade() -> None:", 1)

    assert '"tenants"' in upgrade_source
    assert '"contact_phone"' in upgrade_source
    assert "sa.String(length=32)" in upgrade_source
    # No server_default anywhere in upgrade (checked once above for both columns,
    # but re-asserted here so a future edit that adds a default to only the tenant
    # column still fails this test in isolation).
    assert "server_default" not in upgrade_source

    assert 'op.drop_column("tenants", "contact_phone")' in downgrade_source


@pytest.mark.integration
@pytest.mark.asyncio
async def test_builder_support_columns_postgres_smoke() -> None:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url, settings.database_url)
    child_name = _validated_child_name(f"neurostack_io_smoke_{uuid4().hex[:12]}")
    child_url = admin_url.set(database=child_name)
    admin_engine: AsyncEngine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    child_engine: AsyncEngine | None = None
    created_database: str | None = None

    try:
        async with admin_engine.connect() as connection:
            can_create = bool(
                (
                    await connection.execute(
                        text(
                            "SELECT rolcreatedb OR rolsuper "
                            "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
            )
            _require_createdb(can_create)
            quoted_name = connection.dialect.identifier_preparer.quote(child_name)
            await connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
            created_database = child_name

        # Bring the child up to 018 (state before this migration).
        await _run_alembic(child_url, "upgrade", "018")

        # Seed one builder + one tenant BEFORE applying 019 so we can prove the
        # upgrade does not rewrite existing rows.
        child_engine = create_async_engine(child_url, poolclass=NullPool)
        builder_id = uuid4()
        tenant_id = uuid4()
        async with child_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO builders (id, name, status, is_active) "
                    "VALUES (:id, 'Smoke builder', 'active', true)"
                ),
                {"id": builder_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenants "
                    "(id, name, slug, api_key_hash, is_active, config, builder_id) "
                    "VALUES (:id, 'Smoke clinic', :slug, :key, true, '{}', :builder_id)"
                ),
                {
                    "id": tenant_id,
                    "slug": f"smoke-{tenant_id}",
                    "key": f"smoke-{tenant_id}",
                    "builder_id": builder_id,
                },
            )
        await child_engine.dispose()
        child_engine = None

        # Apply 019.
        await _run_alembic(child_url, "upgrade", "019")

        child_engine = create_async_engine(child_url, poolclass=NullPool)
        async with child_engine.connect() as connection:
            # Columns exist with the expected physical shape.
            builder_column = (
                await connection.execute(
                    text(
                        "SELECT data_type, character_maximum_length, is_nullable, "
                        "column_default FROM information_schema.columns "
                        "WHERE table_name = 'builders' "
                        "AND column_name = 'support_email'"
                    )
                )
            ).one()
            assert builder_column.data_type == "character varying"
            assert builder_column.character_maximum_length == 255
            assert builder_column.is_nullable == "YES"
            assert builder_column.column_default is None

            tenant_column = (
                await connection.execute(
                    text(
                        "SELECT data_type, character_maximum_length, is_nullable, "
                        "column_default FROM information_schema.columns "
                        "WHERE table_name = 'tenants' "
                        "AND column_name = 'contact_phone'"
                    )
                )
            ).one()
            assert tenant_column.data_type == "character varying"
            assert tenant_column.character_maximum_length == 32
            assert tenant_column.is_nullable == "YES"
            assert tenant_column.column_default is None

            # Pre-existing rows retain NULL (no server_default backfill).
            assert (
                await connection.execute(
                    text(
                        "SELECT support_email FROM builders WHERE id = :id"
                    ),
                    {"id": builder_id},
                )
            ).scalar_one() is None
            assert (
                await connection.execute(
                    text(
                        "SELECT contact_phone FROM tenants WHERE id = :id"
                    ),
                    {"id": tenant_id},
                )
            ).scalar_one() is None

        # Write and read back through raw SQL to prove the column accepts data.
        async with child_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE builders SET support_email = :email WHERE id = :id"
                ),
                {"email": "support@example.com", "id": builder_id},
            )
            await connection.execute(
                text(
                    "UPDATE tenants SET contact_phone = :phone WHERE id = :id"
                ),
                {"phone": "+55 11 91234-5678", "id": tenant_id},
            )
        async with child_engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT support_email FROM builders WHERE id = :id"),
                    {"id": builder_id},
                )
            ).scalar_one() == "support@example.com"
            assert (
                await connection.execute(
                    text("SELECT contact_phone FROM tenants WHERE id = :id"),
                    {"id": tenant_id},
                )
            ).scalar_one() == "+55 11 91234-5678"
        await child_engine.dispose()
        child_engine = None

        # Downgrade drops both columns cleanly.
        await _run_alembic(child_url, "downgrade", "018")

        child_engine = create_async_engine(child_url, poolclass=NullPool)
        async with child_engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'builders' "
                        "AND column_name = 'support_email'"
                    )
                )
            ).scalar_one() == 0
            assert (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'tenants' "
                        "AND column_name = 'contact_phone'"
                    )
                )
            ).scalar_one() == 0
    finally:
        if child_engine is not None:
            await child_engine.dispose()
        if created_database is not None:
            assert created_database == child_name
            async with admin_engine.connect() as connection:
                quoted_name = connection.dialect.identifier_preparer.quote(
                    created_database
                )
                await connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        await admin_engine.dispose()
