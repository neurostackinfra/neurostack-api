"""PostgreSQL migration and EventStore smoke for interaction provenance."""

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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.core.interaction_context import InteractionOrigin
from src.memory.event_store import EventData, EventStore, EventType
from src.models.event import Event

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _ROOT / "alembic" / "versions" / "20260904_000001_018_add_interaction_origin.py"
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


def test_migration_down_revision_matches_preflight_head() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "018"' in source
    assert 'down_revision: Union[str, None] = "017"' in source


@pytest.mark.asyncio
async def test_alembic_smoke_runner_uses_the_installed_cli() -> None:
    await _run_alembic(
        make_url("postgresql+asyncpg://unused:unused@localhost/unused"),
        "heads",
    )


def test_migration_and_orm_share_the_origin_contract() -> None:
    migration = _MIGRATION.read_text(encoding="utf-8")
    column = Event.__table__.c.interaction_origin
    constraints = {constraint.name for constraint in Event.__table__.constraints}
    indexes = {index.name for index in Event.__table__.indexes}

    assert column.nullable is False
    assert column.type.length == 20
    assert str(column.server_default.arg) == "unknown"
    assert "ck_events_interaction_origin" in constraints
    assert "idx_events_tenant_origin_type_created" in indexes
    assert "interaction_origin IN ('production', 'simulator', 'unknown')" in migration
    assert "idx_events_tenant_origin_type_created" in migration
    assert "prevent_events_interaction_origin_change" in migration


def test_migration_has_conservative_backfill_and_symmetric_downgrade() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    upgrade_source, downgrade_source = source.split("def downgrade() -> None:", 1)

    assert "'unknown'" in upgrade_source
    assert "channel" not in upgrade_source
    assert "metadata" not in upgrade_source
    assert "DROP TRIGGER IF EXISTS" in downgrade_source
    assert "DROP FUNCTION IF EXISTS" in downgrade_source
    assert "op.drop_index(_INDEX_NAME" in downgrade_source
    assert "op.drop_constraint(_CHECK_NAME" in downgrade_source
    assert 'op.drop_column("events", "interaction_origin")' in downgrade_source


@pytest.mark.parametrize(
    "raw_url",
    [
        "postgresql+asyncpg://user:pass@example.com/postgres",
        "sqlite+aiosqlite:///:memory:",
        "postgresql+asyncpg://user:pass@localhost/not_admin",
    ],
)
def test_smoke_refuses_unsafe_admin_urls(raw_url: str) -> None:
    with pytest.raises(ValueError):
        _validated_admin_url(
            raw_url,
            "postgresql+asyncpg://app:pass@localhost/application",
        )


def test_smoke_refuses_application_database() -> None:
    application_url = "postgresql+asyncpg://user:pass@localhost/postgres"
    with pytest.raises(ValueError, match="application database"):
        _validated_admin_url(application_url, application_url)


@pytest.mark.parametrize(
    "name",
    ["postgres", "neurostack", "neurostack_io_smoke_bad-name", "smoke_deadbeef0000"],
)
def test_smoke_refuses_child_name_outside_internal_pattern(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        _validated_child_name(name)


def test_smoke_refuses_user_without_createdb() -> None:
    with pytest.raises(PermissionError, match="CREATEDB"):
        _require_createdb(False)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_interaction_origin_postgres_smoke() -> None:
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

        await _run_alembic(child_url, "upgrade", "017")
        child_engine = create_async_engine(child_url, poolclass=NullPool)
        tenant_id = uuid4()
        session_id = uuid4()
        legacy_event_id = uuid4()
        async with child_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants "
                    "(id, name, slug, api_key_hash, is_active, config) "
                    "VALUES (:id, 'Smoke tenant', :slug, :key, true, '{}')"
                ),
                {
                    "id": tenant_id,
                    "slug": f"smoke-{tenant_id}",
                    "key": f"smoke-{tenant_id}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, tenant_id, channel, cognitive_state, metadata, is_active, message_count) "
                    "VALUES (:id, :tenant_id, 'test', '{}', '{}', true, 0)"
                ),
                {"id": session_id, "tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO events "
                    "(id, tenant_id, session_id, event_type, event_data, metadata) "
                    "VALUES (:id, :tenant_id, :session_id, 'message.in', '{}', '{}')"
                ),
                {
                    "id": legacy_event_id,
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                },
            )
        await child_engine.dispose()
        child_engine = None

        await _run_alembic(child_url, "upgrade", "018")
        child_engine = create_async_engine(child_url, poolclass=NullPool)
        async with child_engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT interaction_origin FROM events WHERE id = :event_id"),
                    {"event_id": legacy_event_id},
                )
            ).scalar_one() == "unknown"
            indexes = (
                (
                    await connection.execute(
                        text(
                            "SELECT indexdef FROM pg_indexes "
                            "WHERE schemaname = current_schema() "
                            "AND indexname = 'idx_events_tenant_origin_type_created'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(indexes) == 1
            assert "created_at DESC" in indexes[0]

            transaction = connection.get_transaction()
            assert transaction is not None
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "UPDATE events SET interaction_origin = 'production' "
                        "WHERE id = :event_id"
                    ),
                    {"event_id": legacy_event_id},
                )
            await transaction.rollback()

        async with child_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE events SET metadata = '{\"checked\": true}' WHERE id = :id"
                ),
                {"id": legacy_event_id},
            )
        async with child_engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO events "
                        "(tenant_id, session_id, event_type, interaction_origin, event_data) "
                        "VALUES (:tenant_id, :session_id, 'message.in', 'forged', '{}')"
                    ),
                    {"tenant_id": tenant_id, "session_id": session_id},
                )
            await transaction.rollback()

        store = EventStore(
            session_factory=async_sessionmaker(child_engine, expire_on_commit=False)
        )
        events = [
            EventData(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=EventType.MESSAGE_IN.value,
                interaction_origin=origin,
            )
            for origin in InteractionOrigin
        ]
        await store.append(events[0])
        await store.append_batch(events[1:])
        for event in events:
            fetched = await store.get_event(event.id)
            assert fetched is not None
            assert fetched.interaction_origin is event.interaction_origin
        assert {
            event.interaction_origin
            for event in (await store.query_by_session(tenant_id, session_id)).events
        }.issuperset(set(InteractionOrigin))
        assert {
            event.interaction_origin
            for event in (await store.query_by_tenant(tenant_id)).events
        }.issuperset(set(InteractionOrigin))
        await _run_alembic(child_url, "downgrade", "017")
        async with child_engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'events' "
                        "AND column_name = 'interaction_origin'"
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
