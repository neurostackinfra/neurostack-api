"""PostgreSQL evidence for media constraints and the two-phase capacity guard."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from starlette.requests import Request
from starlette.types import Message

from src.api.middleware.auth import TenantContext
from src.api.middleware.media_limits import MediaConcurrencyLimiters
from src.api.routes import media_assets as media_route
from src.api.schemas.media_assets import MediaAssetPatchRequest
from src.config.settings import settings
from src.models.builder import Builder
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.services import media_assets as media_service
from src.services.media_assets import (
    MediaOwnerTransition,
    MediaStorageCapacityExceeded,
    MediaStorageGuardError,
    detach_builder_media_owners_for_delete,
    lock_builder_users_before_parent,
    lock_clinic_for_delete,
    lock_tenant_users_before_parent,
    media_payload_sha256,
    media_storage_advisory_key,
    media_storage_capacity_guard,
    transition_tenant_media_owner,
)

_ROOT = Path(__file__).resolve().parents[2]
_CHILD_PATTERN = re.compile(r"^neurostack_media_smoke_[0-9a-f]{12}$")
_TASK_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    def session(self) -> AsyncSession:
        return self.sessions()


@dataclass(frozen=True)
class _Graph:
    builder_id: UUID
    foreign_builder_id: UUID
    auth_tenant_id: UUID
    actor_user_id: UUID
    source_tenant_id: UUID
    destination_tenant_id: UUID


def _validated_admin_url(raw_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("POSTGRES_SMOKE_DATABASE_URL must use PostgreSQL")
    if url.query:
        raise ValueError("POSTGRES_SMOKE_DATABASE_URL must not contain query overrides")
    try:
        is_loopback = ipaddress.ip_address(url.host or "").is_loopback
    except ValueError:
        is_loopback = (url.host or "").lower() == "localhost"
    if not is_loopback:
        raise ValueError("POSTGRES_SMOKE_DATABASE_URL must use a loopback host")
    application_url = make_url(settings.database_url)
    if (url.host, url.port, url.database) == (
        application_url.host,
        application_url.port,
        application_url.database,
    ):
        raise ValueError("smoke URL must not target the application database")
    if url.database != "postgres":
        raise ValueError("smoke URL must target the postgres administrative database")
    return URL.create(
        drivername="postgresql+asyncpg",
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database=url.database,
    )


async def _run_alembic(database_url: URL, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    executable = Path(sys.executable).with_name("alembic")
    if not executable.is_file():
        raise RuntimeError("Alembic executable is unavailable beside the test Python")
    process = await asyncio.create_subprocess_exec(
        str(executable),
        *arguments,
        cwd=_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode(errors="replace")


async def _table_exists(engine: AsyncEngine) -> bool:
    async with engine.connect() as connection:
        return bool(
            await connection.scalar(
                text("SELECT to_regclass('public.media_assets') IS NOT NULL")
            )
        )


@pytest.fixture(scope="module")
async def postgres_harness() -> AsyncIterator[_Harness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.fail(
            "POSTGRES_SMOKE_DATABASE_URL is required for media integration tests",
            pytrace=False,
        )

    admin_url = _validated_admin_url(raw_admin_url)
    child_name = f"neurostack_media_smoke_{uuid4().hex[:12]}"
    assert _CHILD_PATTERN.fullmatch(child_name)
    child_url = admin_url.set(database=child_name)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    child_engine: AsyncEngine | None = None
    created = False
    try:
        async with admin_engine.connect() as connection:
            can_create = bool(
                await connection.scalar(
                    text(
                        "SELECT rolcreatedb OR rolsuper "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            )
            if not can_create:
                raise PermissionError("smoke database user must have CREATEDB")
            quoted = connection.dialect.identifier_preparer.quote(child_name)
            await connection.exec_driver_sql(f"CREATE DATABASE {quoted}")
            created = True

        await _run_alembic(child_url, "upgrade", "head")
        child_engine = create_async_engine(child_url, poolclass=NullPool)
        assert await _table_exists(child_engine)
        await child_engine.dispose()
        child_engine = None

        await _run_alembic(child_url, "downgrade", "021")
        downgrade_probe = create_async_engine(child_url, poolclass=NullPool)
        try:
            assert not await _table_exists(downgrade_probe)
        finally:
            await downgrade_probe.dispose()
        await _run_alembic(child_url, "upgrade", "head")

        child_engine = create_async_engine(child_url, poolclass=NullPool)
        assert await _table_exists(child_engine)
        yield _Harness(
            engine=child_engine,
            sessions=async_sessionmaker(
                child_engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            ),
        )
    finally:
        if child_engine is not None:
            await child_engine.dispose()
        if created:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database AND pid <> pg_backend_pid()"
                    ),
                    {"database": child_name},
                )
                quoted = connection.dialect.identifier_preparer.quote(child_name)
                await connection.exec_driver_sql(f"DROP DATABASE {quoted}")
        await admin_engine.dispose()


def _asset(
    tenant_id: UUID,
    payload: bytes,
    *,
    display_name: str = "Asset",
    original_filename: str = "asset.pdf",
    category: str = "pdf",
    mime_type: str = "application/pdf",
    size_bytes: int | None = None,
    sha256: bytes | None = None,
    usage_trigger: str | None = None,
) -> MediaAsset:
    return MediaAsset(
        tenant_id=tenant_id,
        display_name=display_name,
        original_filename=original_filename,
        category=category,
        mime_type=mime_type,
        size_bytes=len(payload) if size_bytes is None else size_bytes,
        sha256=media_payload_sha256(payload) if sha256 is None else sha256,
        payload=payload,
        usage_trigger=usage_trigger,
        auto_suggest=False,
    )


async def _seed_graph(
    harness: _Harness,
    *,
    source_payloads: tuple[bytes, ...] = (b"source-one", b"source-two"),
) -> _Graph:
    graph = _Graph(
        builder_id=uuid4(),
        foreign_builder_id=uuid4(),
        auth_tenant_id=uuid4(),
        actor_user_id=uuid4(),
        source_tenant_id=uuid4(),
        destination_tenant_id=uuid4(),
    )
    async with harness.session() as session:
        session.add_all(
            [
                Builder(id=graph.builder_id, name="Media Builder"),
                Builder(id=graph.foreign_builder_id, name="Foreign Builder"),
                Tenant(
                    id=graph.auth_tenant_id,
                    name="Builder Login",
                    slug=f"media-auth-{graph.auth_tenant_id.hex}",
                    api_key_hash=f"media-auth-{graph.auth_tenant_id.hex}",
                    plan_type="admin",
                    config={},
                ),
                Tenant(
                    id=graph.source_tenant_id,
                    name="Media Source",
                    slug=f"media-source-{graph.source_tenant_id.hex}",
                    api_key_hash=f"media-source-{graph.source_tenant_id.hex}",
                    plan_type="clinic",
                    builder_id=graph.builder_id,
                    config={},
                ),
                Tenant(
                    id=graph.destination_tenant_id,
                    name="Media Destination",
                    slug=f"media-destination-{graph.destination_tenant_id.hex}",
                    api_key_hash=f"media-destination-{graph.destination_tenant_id.hex}",
                    plan_type="clinic",
                    builder_id=graph.builder_id,
                    config={},
                ),
                User(
                    id=graph.actor_user_id,
                    tenant_id=graph.auth_tenant_id,
                    email=f"media-builder-{graph.actor_user_id.hex}@test.local",
                    password_hash="unused",
                    role=UserRole.builder,
                    builder_id=graph.builder_id,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                _asset(
                    graph.source_tenant_id,
                    payload,
                    display_name=f"Source {index}",
                    original_filename=f"source-{index}.pdf",
                )
                for index, payload in enumerate(source_payloads)
            ]
        )
        await session.commit()
    return graph


@pytest.mark.asyncio
async def test_migration_constraints_reject_inconsistent_rows(
    postgres_harness: _Harness,
) -> None:
    tenant_id = uuid4()
    async with postgres_harness.session() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="Constraint Tenant",
                slug=f"constraint-{tenant_id.hex}",
                api_key_hash=f"constraint-{tenant_id.hex}",
                config={},
            )
        )
        await session.commit()

    invalid_assets = [
        _asset(tenant_id, b"x", size_bytes=2),
        _asset(tenant_id, b"x", size_bytes=0),
        _asset(tenant_id, b"x", size_bytes=50_000_001),
        _asset(tenant_id, b"x", sha256=b"short"),
        _asset(tenant_id, b"x", category="audio", mime_type="image/png"),
        _asset(tenant_id, b"x", display_name=""),
        _asset(tenant_id, b"x", original_filename="x" * 256),
        _asset(tenant_id, b"x", usage_trigger="x" * 501),
        _asset(tenant_id, b"x", display_name="line\nbreak"),
        _asset(tenant_id, b"x", display_name="nul\x00byte"),
    ]
    for invalid in invalid_assets:
        async with postgres_harness.session() as session:
            session.add(invalid)
            with pytest.raises(SQLAlchemyError):
                await session.commit()
            await session.rollback()

    async with postgres_harness.session() as session:
        assert (
            await session.scalar(
                select(func.count(MediaAsset.id)).where(
                    MediaAsset.tenant_id == tenant_id
                )
            )
        ) == 0


@pytest.mark.asyncio
async def test_clone_compatible_consumer_observes_canonical_lock_order(
    postgres_harness: _Harness,
) -> None:
    graph = await _seed_graph(postgres_harness)
    statements: list[str] = []

    def record_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(
        postgres_harness.engine.sync_engine,
        "before_cursor_execute",
        record_statement,
    )
    try:
        async with postgres_harness.session() as session:
            destination = await session.get(Tenant, graph.destination_tenant_id)
            assert destination is not None
            statements.clear()
            async with media_storage_capacity_guard(
                session,
                graph.actor_user_id,
                destination,
            ) as guard:
                assert guard.advisory_key == media_storage_advisory_key(
                    graph.builder_id
                )
                source_ids = tuple(
                    (
                        await session.execute(
                            select(MediaAsset.id)
                            .where(MediaAsset.tenant_id == graph.source_tenant_id)
                            .order_by(MediaAsset.id)
                            .with_for_update(read=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(source_ids) == 2
                source_sizes = tuple(
                    (
                        await session.execute(
                            select(MediaAsset.size_bytes)
                            .where(MediaAsset.id.in_(source_ids))
                            .order_by(MediaAsset.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                additional_bytes = sum(source_sizes)
                projection = await guard.validate_projection(additional_bytes)
                assert projection.owner_id == graph.builder_id
                assert projection.additional_bytes == additional_bytes

                sources = (
                    (
                        await session.execute(
                            select(MediaAsset)
                            .where(MediaAsset.id.in_(source_ids))
                            .order_by(MediaAsset.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                session.add_all(
                    [
                        _asset(
                            graph.destination_tenant_id,
                            source.payload,
                            display_name=source.display_name,
                            original_filename=source.original_filename,
                            category=source.category,
                            mime_type=source.mime_type,
                            usage_trigger=source.usage_trigger,
                        )
                        for source in sources
                    ]
                )
                await session.flush()
            await session.commit()
    finally:
        event.remove(
            postgres_harness.engine.sync_engine,
            "before_cursor_execute",
            record_statement,
        )

    def statement_index(fragment: str, *, after: int = -1) -> int:
        return next(
            index
            for index, statement in enumerate(statements)
            if index > after and fragment in statement
        )

    user_lock = statement_index("from users")
    assert "for key share" in statements[user_lock]
    builder_lock = statement_index("from builders", after=user_lock)
    assert "for update" in statements[builder_lock]
    advisory_lock = statement_index("pg_advisory_xact_lock", after=builder_lock)
    tenant_lock = statement_index("from tenants", after=advisory_lock)
    assert "for update" in statements[tenant_lock]
    source_lock = statement_index("for share", after=tenant_lock)
    assert "media_assets.id" in statements[source_lock]
    assert "payload" not in statements[source_lock]
    source_size = statement_index("media_assets.size_bytes", after=source_lock)
    usage = statement_index("sum(media_assets.size_bytes)", after=source_size)
    payload_read = statement_index("media_assets.payload", after=usage)
    insert = statement_index("insert into media_assets", after=payload_read)
    assert user_lock < builder_lock < advisory_lock < tenant_lock
    assert tenant_lock < source_lock < source_size < usage < payload_read < insert
    assert sum("sum(media_assets.size_bytes)" in sql for sql in statements) == 1


@pytest.mark.asyncio
async def test_two_sessions_serialize_capacity_without_deadlock(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = await _seed_graph(postgres_harness, source_payloads=(b"s" * 60,))
    monkeypatch.setattr(media_service, "MEDIA_STORAGE_CAP_BYTES", 100)
    first_inserted = asyncio.Event()
    release_first = asyncio.Event()
    advisory_keys: list[int] = []

    async def upload_known_bytes() -> str:
        async with postgres_harness.session() as session:
            destination = await session.get(Tenant, graph.destination_tenant_id)
            assert destination is not None
            try:
                async with media_storage_capacity_guard(
                    session,
                    graph.actor_user_id,
                    destination,
                ) as guard:
                    advisory_keys.append(guard.advisory_key)
                    await guard.validate_projection(30)
                    session.add(
                        _asset(
                            graph.destination_tenant_id,
                            b"u" * 30,
                            display_name="upload",
                            original_filename="upload.pdf",
                        )
                    )
                    await session.flush()
                    first_inserted.set()
                    await release_first.wait()
                await session.commit()
                return "inserted"
            except MediaStorageCapacityExceeded:
                await session.rollback()
                return "cap"

    async def clone_unknown_bytes() -> str:
        async with postgres_harness.session() as session:
            destination = await session.get(Tenant, graph.destination_tenant_id)
            assert destination is not None
            try:
                async with media_storage_capacity_guard(
                    session,
                    graph.actor_user_id,
                    destination,
                ) as guard:
                    advisory_keys.append(guard.advisory_key)
                    source_ids = tuple(
                        (
                            await session.execute(
                                select(MediaAsset.id)
                                .where(MediaAsset.tenant_id == graph.source_tenant_id)
                                .order_by(MediaAsset.id)
                                .with_for_update(read=True)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    source_sizes = tuple(
                        (
                            await session.execute(
                                select(MediaAsset.size_bytes)
                                .where(MediaAsset.id.in_(source_ids))
                                .order_by(MediaAsset.id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    await guard.validate_projection(sum(source_sizes))
                await session.commit()
                return "inserted"
            except MediaStorageCapacityExceeded:
                await session.rollback()
                return "cap"

    first_task = asyncio.create_task(upload_known_bytes())
    await asyncio.wait_for(first_inserted.wait(), timeout=_TASK_TIMEOUT_SECONDS)
    second_task = asyncio.create_task(clone_unknown_bytes())
    await asyncio.sleep(0.1)
    assert not second_task.done()
    release_first.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(first_task, second_task),
        timeout=_TASK_TIMEOUT_SECONDS,
    )
    assert outcomes == ["inserted", "cap"]
    expected_key = media_storage_advisory_key(graph.builder_id)
    assert advisory_keys == [expected_key, expected_key]
    async with postgres_harness.session() as session:
        assert (
            await session.scalar(
                select(func.count(MediaAsset.id)).where(
                    MediaAsset.tenant_id == graph.destination_tenant_id
                )
            )
        ) == 1
        assert (
            await session.scalar(
                select(func.sum(MediaAsset.size_bytes))
                .select_from(MediaAsset)
                .join(Tenant, Tenant.id == MediaAsset.tenant_id)
                .where(Tenant.builder_id == graph.builder_id)
            )
        ) == 90


@pytest.mark.asyncio
async def test_owner_transition_uses_canonical_lock_and_projection_order(
    postgres_harness: _Harness,
) -> None:
    graph = await _seed_graph(postgres_harness, source_payloads=(b"move",))
    async with postgres_harness.session() as session:
        await session.execute(
            update(Tenant)
            .where(Tenant.id == graph.source_tenant_id)
            .values(builder_id=None)
        )
        await session.commit()

    statements: list[str] = []

    def record_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(
        postgres_harness.engine.sync_engine,
        "before_cursor_execute",
        record_statement,
    )
    try:
        async with postgres_harness.session() as session:
            await transition_tenant_media_owner(
                session,
                MediaOwnerTransition(
                    tenant_id=graph.source_tenant_id,
                    expected_builder_id=None,
                    destination_builder_id=graph.builder_id,
                ),
                actor_user_id=graph.actor_user_id,
            )
            await session.commit()
    finally:
        event.remove(
            postgres_harness.engine.sync_engine,
            "before_cursor_execute",
            record_statement,
        )

    user_lock = next(i for i, sql in enumerate(statements) if "from users" in sql)
    builder_lock = next(
        i
        for i, sql in enumerate(statements)
        if i > user_lock and "from builders" in sql
    )
    advisory_locks = [
        i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql
    ]
    tenant_lock = next(
        i
        for i, sql in enumerate(statements)
        if i > advisory_locks[-1] and "from tenants" in sql and "for update" in sql
    )
    usage_queries = [
        i for i, sql in enumerate(statements) if "sum(media_assets.size_bytes)" in sql
    ]
    tenant_update = next(
        i for i, sql in enumerate(statements) if sql.startswith("update tenants")
    )
    assert "for update" in statements[user_lock]
    assert "for update" in statements[builder_lock]
    assert len(advisory_locks) == 2
    assert user_lock < builder_lock < advisory_locks[0] < advisory_locks[1]
    assert advisory_locks[-1] < tenant_lock < min(usage_queries) < tenant_update


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["link", "unlink", "delete"])
async def test_owner_mutations_serialize_with_guard_without_deadlock(
    postgres_harness: _Harness,
    mutation: str,
) -> None:
    graph = await _seed_graph(postgres_harness, source_payloads=())
    if mutation == "link":
        async with postgres_harness.session() as session:
            await session.execute(
                update(Tenant)
                .where(Tenant.id == graph.source_tenant_id)
                .values(builder_id=None)
            )
            await session.commit()
        guarded_tenant_id = graph.destination_tenant_id
    else:
        guarded_tenant_id = graph.source_tenant_id

    guard_acquired = asyncio.Event()
    release_guard = asyncio.Event()

    async def hold_guard() -> None:
        async with postgres_harness.session() as session:
            guarded = await session.get(Tenant, guarded_tenant_id)
            assert guarded is not None
            async with media_storage_capacity_guard(
                session,
                graph.actor_user_id,
                guarded,
            ) as guard:
                await guard.validate_projection(0)
                guard_acquired.set()
                await release_guard.wait()
            await session.commit()

    async def mutate_owner() -> None:
        async with postgres_harness.session() as session:
            if mutation == "link":
                await transition_tenant_media_owner(
                    session,
                    MediaOwnerTransition(
                        tenant_id=graph.source_tenant_id,
                        expected_builder_id=None,
                        destination_builder_id=graph.builder_id,
                    ),
                    actor_user_id=graph.actor_user_id,
                )
            elif mutation == "unlink":
                await transition_tenant_media_owner(
                    session,
                    MediaOwnerTransition(
                        tenant_id=graph.source_tenant_id,
                        expected_builder_id=graph.builder_id,
                        destination_builder_id=None,
                    ),
                    actor_user_id=graph.actor_user_id,
                )
            else:
                await detach_builder_media_owners_for_delete(
                    session,
                    graph.builder_id,
                    actor_user_id=graph.actor_user_id,
                )
                builder = await session.get(Builder, graph.builder_id)
                assert builder is not None
                await session.delete(builder)
            await session.commit()

    holder = asyncio.create_task(hold_guard())
    await asyncio.wait_for(guard_acquired.wait(), timeout=_TASK_TIMEOUT_SECONDS)
    mutation_task = asyncio.create_task(mutate_owner())
    await asyncio.sleep(0.1)
    assert not mutation_task.done()
    release_guard.set()
    await asyncio.wait_for(
        asyncio.gather(holder, mutation_task),
        timeout=_TASK_TIMEOUT_SECONDS,
    )

    async with postgres_harness.session() as session:
        source = await session.get(Tenant, graph.source_tenant_id)
        assert source is not None
        if mutation == "link":
            assert source.builder_id == graph.builder_id
        else:
            assert source.builder_id is None
        if mutation == "delete":
            assert await session.get(Builder, graph.builder_id) is None


@pytest.mark.asyncio
async def test_guard_and_clinic_delete_complete_without_deadlock(
    postgres_harness: _Harness,
) -> None:
    graph = await _seed_graph(postgres_harness, source_payloads=(b"delete-me",))
    guard_acquired = asyncio.Event()
    release_guard = asyncio.Event()

    async def hold_guard() -> None:
        async with postgres_harness.session() as session:
            source = await session.get(Tenant, graph.source_tenant_id)
            assert source is not None
            async with media_storage_capacity_guard(
                session,
                graph.actor_user_id,
                source,
            ) as guard:
                await guard.validate_projection(0)
                guard_acquired.set()
                await release_guard.wait()
            await session.commit()

    async def delete_clinic() -> None:
        async with postgres_harness.session() as session:
            source = await lock_clinic_for_delete(
                session,
                graph.source_tenant_id,
                actor_user_id=graph.actor_user_id,
            )
            assert source is not None
            await session.delete(source)
            await session.commit()

    holder = asyncio.create_task(hold_guard())
    await asyncio.wait_for(guard_acquired.wait(), timeout=_TASK_TIMEOUT_SECONDS)
    delete_task = asyncio.create_task(delete_clinic())
    await asyncio.sleep(0.1)
    assert not delete_task.done()
    release_guard.set()
    await asyncio.wait_for(
        asyncio.gather(holder, delete_task),
        timeout=_TASK_TIMEOUT_SECONDS,
    )
    async with postgres_harness.session() as session:
        assert await session.get(Tenant, graph.source_tenant_id) is None
        assert (
            await session.scalar(
                select(func.count(MediaAsset.id)).where(
                    MediaAsset.tenant_id == graph.source_tenant_id
                )
            )
        ) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_kind", ["builder", "clinic"])
async def test_owner_transition_and_parent_user_update_complete_without_deadlock(
    postgres_harness: _Harness,
    parent_kind: str,
) -> None:
    graph = await _seed_graph(postgres_harness, source_payloads=())
    clinic_user_id = uuid4()
    async with postgres_harness.session() as session:
        session.add(
            User(
                id=clinic_user_id,
                tenant_id=graph.source_tenant_id,
                email=f"clinic-{clinic_user_id.hex}@test.local",
                password_hash="unused",
                role=UserRole.clinic_admin,
            )
        )
        await session.commit()

    transition_acquired = asyncio.Event()
    release_transition = asyncio.Event()

    async def transition_owner() -> None:
        async with postgres_harness.session() as session:
            await transition_tenant_media_owner(
                session,
                MediaOwnerTransition(
                    tenant_id=graph.source_tenant_id,
                    expected_builder_id=graph.builder_id,
                    destination_builder_id=None,
                ),
                actor_user_id=graph.actor_user_id,
            )
            transition_acquired.set()
            await release_transition.wait()
            await session.commit()

    async def update_parent_and_user() -> None:
        async with postgres_harness.session() as session:
            if parent_kind == "builder":
                builder = await lock_builder_users_before_parent(
                    session,
                    graph.builder_id,
                )
                assert builder is not None
                user = await session.get(User, graph.actor_user_id)
                assert user is not None
                builder.name = "Updated Builder"
                user.email = f"updated-builder-{graph.actor_user_id.hex}@test.local"
            else:
                tenant = await lock_tenant_users_before_parent(
                    session,
                    graph.source_tenant_id,
                )
                assert tenant is not None
                user = await session.get(User, clinic_user_id)
                assert user is not None
                tenant.name = "Updated Clinic"
                user.email = f"updated-clinic-{clinic_user_id.hex}@test.local"
            await session.commit()

    transition_task = asyncio.create_task(transition_owner())
    await asyncio.wait_for(
        transition_acquired.wait(),
        timeout=_TASK_TIMEOUT_SECONDS,
    )
    update_task = asyncio.create_task(update_parent_and_user())
    await asyncio.sleep(0.1)
    assert not update_task.done()
    release_transition.set()
    await asyncio.wait_for(
        asyncio.gather(transition_task, update_task),
        timeout=_TASK_TIMEOUT_SECONDS,
    )

    async with postgres_harness.session() as session:
        source = await session.get(Tenant, graph.source_tenant_id)
        assert source is not None and source.builder_id is None
        if parent_kind == "builder":
            builder = await session.get(Builder, graph.builder_id)
            user = await session.get(User, graph.actor_user_id)
            assert builder is not None and builder.name == "Updated Builder"
            assert user is not None and user.email.startswith("updated-builder-")
        else:
            user = await session.get(User, clinic_user_id)
            assert source.name == "Updated Clinic"
            assert user is not None and user.email.startswith("updated-clinic-")


@pytest.mark.asyncio
async def test_link_projects_destination_owner_before_moving_tenant(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = await _seed_graph(
        postgres_harness,
        source_payloads=(b"s" * 30,),
    )
    monkeypatch.setattr(media_service, "MEDIA_STORAGE_CAP_BYTES", 100)
    async with postgres_harness.session() as session:
        await session.execute(
            update(Tenant)
            .where(Tenant.id == graph.source_tenant_id)
            .values(builder_id=None)
        )
        session.add(_asset(graph.destination_tenant_id, b"d" * 80))
        await session.commit()

    async with postgres_harness.session() as session:
        with pytest.raises(MediaStorageCapacityExceeded):
            await transition_tenant_media_owner(
                session,
                MediaOwnerTransition(
                    tenant_id=graph.source_tenant_id,
                    expected_builder_id=None,
                    destination_builder_id=graph.builder_id,
                ),
                actor_user_id=graph.actor_user_id,
            )
        await session.rollback()

    async with postgres_harness.session() as session:
        source = await session.get(Tenant, graph.source_tenant_id)
        assert source is not None and source.builder_id is None


@pytest.mark.asyncio
async def test_tenant_delete_uses_database_cascade_without_loading_payload(
    postgres_harness: _Harness,
) -> None:
    tenant_id = uuid4()
    async with postgres_harness.session() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="Cascade Tenant",
                slug=f"cascade-{tenant_id.hex}",
                api_key_hash=f"cascade-{tenant_id.hex}",
                config={},
            )
        )
        await session.flush()
        session.add(_asset(tenant_id, b"database-cascade"))
        await session.commit()

    statements: list[str] = []

    def record_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(
        postgres_harness.engine.sync_engine,
        "before_cursor_execute",
        record_statement,
    )
    try:
        async with postgres_harness.session() as session:
            tenant = await session.get(Tenant, tenant_id)
            assert tenant is not None
            statements.clear()
            await session.delete(tenant)
            await session.commit()
    finally:
        event.remove(
            postgres_harness.engine.sync_engine,
            "before_cursor_execute",
            record_statement,
        )

    assert not any(
        statement.startswith("select")
        and "media_assets" in statement
        and "payload" in statement
        for statement in statements
    )
    async with postgres_harness.session() as session:
        assert (
            await session.scalar(
                select(func.count(MediaAsset.id)).where(
                    MediaAsset.tenant_id == tenant_id
                )
            )
        ) == 0


@pytest.mark.asyncio
async def test_cancellation_rolls_back_and_releases_all_guard_locks(
    postgres_harness: _Harness,
) -> None:
    graph = await _seed_graph(postgres_harness, source_payloads=())
    acquired = asyncio.Event()
    wait_forever = asyncio.Event()

    async def hold_guard() -> None:
        async with postgres_harness.session() as session:
            destination = await session.get(Tenant, graph.destination_tenant_id)
            assert destination is not None
            async with media_storage_capacity_guard(
                session,
                graph.actor_user_id,
                destination,
            ) as guard:
                await guard.validate_projection(0)
                acquired.set()
                await wait_forever.wait()

    holder = asyncio.create_task(hold_guard())
    await asyncio.wait_for(acquired.wait(), timeout=_TASK_TIMEOUT_SECONDS)
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    async def reacquire() -> None:
        async with postgres_harness.session() as session:
            destination = await session.get(Tenant, graph.destination_tenant_id)
            assert destination is not None
            async with media_storage_capacity_guard(
                session,
                graph.actor_user_id,
                destination,
            ) as guard:
                await guard.validate_projection(0)
            await session.rollback()

    await asyncio.wait_for(reacquire(), timeout=_TASK_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_caught_cap_error_still_forces_guard_rollback(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = await _seed_graph(postgres_harness, source_payloads=())
    monkeypatch.setattr(media_service, "MEDIA_STORAGE_CAP_BYTES", 0)
    async with postgres_harness.session() as session:
        destination = await session.get(Tenant, graph.destination_tenant_id)
        assert destination is not None
        with pytest.raises(MediaStorageGuardError, match="not approved"):
            async with media_storage_capacity_guard(
                session,
                graph.actor_user_id,
                destination,
            ) as guard:
                with pytest.raises(MediaStorageCapacityExceeded):
                    await guard.validate_projection(1)
        assert not session.in_transaction()


@pytest.mark.asyncio
async def test_owner_change_between_resolution_and_revalidation_rolls_back(
    postgres_harness: _Harness,
) -> None:
    graph = await _seed_graph(postgres_harness, source_payloads=())
    async with postgres_harness.session() as guarded_session:
        stale_destination = await guarded_session.get(
            Tenant,
            graph.destination_tenant_id,
        )
        assert stale_destination is not None
        async with postgres_harness.session() as mutating_session:
            await mutating_session.execute(
                update(Tenant)
                .where(Tenant.id == graph.destination_tenant_id)
                .values(builder_id=graph.foreign_builder_id)
            )
            await mutating_session.commit()

        with pytest.raises(MediaStorageGuardError, match="owner changed"):
            async with media_storage_capacity_guard(
                guarded_session,
                graph.actor_user_id,
                stale_destination,
            ):
                raise AssertionError("unreachable")
        assert not guarded_session.in_transaction()


@pytest.mark.asyncio
async def test_userless_standalone_tenant_uses_its_own_owner(
    postgres_harness: _Harness,
) -> None:
    tenant_id = uuid4()
    async with postgres_harness.session() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="Userless Clinic",
                slug=f"userless-{tenant_id.hex}",
                api_key_hash=f"userless-{tenant_id.hex}",
                plan_type="clinic",
                config={},
            )
        )
        await session.commit()

    async with postgres_harness.session() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        async with media_storage_capacity_guard(session, None, tenant) as guard:
            projection = await guard.validate_projection(0)
            assert projection.owner_id == tenant_id
            assert projection.used_bytes == 0
        await session.rollback()


def _route_request() -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(
            media_concurrency=MediaConcurrencyLimiters(uploads=4, downloads=8)
        )
    )
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/media-assets",
            "raw_path": b"/v1/media-assets",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "app": app,
        }
    )


async def _consume_route_response(response: Any) -> bytes:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await response(_route_request().scope, receive, send)
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "is_active", "plan_type", "allowed"),
    [
        (None, True, "clinic", True),
        (UserRole.clinic_admin, True, "clinic", True),
        (UserRole.super_admin, True, "clinic", False),
        (UserRole.builder, True, "clinic", False),
        (UserRole.investor, True, "clinic", False),
        (None, False, "clinic", False),
        (None, True, "admin", False),
    ],
)
async def test_media_route_postgres_target_authorization_matrix(
    postgres_harness: _Harness,
    role: UserRole | None,
    is_active: bool,
    plan_type: str,
    allowed: bool,
) -> None:
    tenant_id = uuid4()
    async with postgres_harness.session() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name=f"Authorization {tenant_id.hex}",
                slug=f"authorization-{tenant_id.hex}",
                api_key_hash=f"authorization-{tenant_id.hex}",
                is_active=is_active,
                plan_type=plan_type,
                config={},
            )
        )
        await session.flush()
        if role is not None:
            session.add(
                User(
                    tenant_id=tenant_id,
                    email=f"authorization-{tenant_id.hex}@test.local",
                    password_hash="unused",
                    role=role,
                )
            )
        await session.commit()

    async with postgres_harness.session() as session:
        if allowed:
            target = await media_route._authorized_target(
                _route_request(),
                TenantContext(tenant_id, api_key="tenant-scoped"),
                None,
                session,
            )
            assert target.id == tenant_id
        else:
            with pytest.raises(HTTPException) as caught:
                await media_route._authorized_target(
                    _route_request(),
                    TenantContext(tenant_id, api_key="tenant-scoped"),
                    None,
                    session,
                )
            assert caught.value.status_code == 403
            assert getattr(caught.value, "detail") == {"code": "media_tenant_forbidden"}


@pytest.mark.asyncio
async def test_builder_own_mixed_tenant_claim_tracks_owner_relink(
    postgres_harness: _Harness,
) -> None:
    tenant_id = uuid4()
    original_builder_id = uuid4()
    foreign_builder_id = uuid4()
    async with postgres_harness.session() as session:
        session.add_all(
            [
                Builder(id=original_builder_id, name="Original auth builder"),
                Builder(id=foreign_builder_id, name="Relinked auth builder"),
                Tenant(
                    id=tenant_id,
                    name="Mixed own clinic",
                    slug=f"mixed-own-{tenant_id.hex}",
                    api_key_hash=f"mixed-own-{tenant_id.hex}",
                    builder_id=original_builder_id,
                    is_active=True,
                    plan_type="clinic",
                    config={},
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                User(
                    tenant_id=tenant_id,
                    email=f"mixed-builder-{tenant_id.hex}@test.local",
                    password_hash="unused",
                    role=UserRole.builder,
                    builder_id=original_builder_id,
                ),
                User(
                    tenant_id=tenant_id,
                    email=f"mixed-clinic-{tenant_id.hex}@test.local",
                    password_hash="unused",
                    role=UserRole.clinic_admin,
                ),
            ]
        )
        await session.commit()

    async with postgres_harness.session() as session:
        matching = await media_route._authorized_target(
            _route_request(),
            TenantContext(
                tenant_id,
                role="builder",
                builder_id=original_builder_id,
            ),
            None,
            session,
        )
        assert matching.id == tenant_id

    for claim in (None, foreign_builder_id):
        async with postgres_harness.session() as session:
            with pytest.raises(HTTPException) as denied:
                await media_route._authorized_target(
                    _route_request(),
                    TenantContext(tenant_id, role="builder", builder_id=claim),
                    None,
                    session,
                )
            assert denied.value.status_code == 403

    async with postgres_harness.session() as session:
        await transition_tenant_media_owner(
            session,
            MediaOwnerTransition(
                tenant_id=tenant_id,
                expected_builder_id=original_builder_id,
                destination_builder_id=foreign_builder_id,
            ),
        )
        await session.commit()

    async with postgres_harness.session() as session:
        with pytest.raises(HTTPException) as stale_claim:
            await media_route._authorized_target(
                _route_request(),
                TenantContext(
                    tenant_id,
                    role="builder",
                    builder_id=original_builder_id,
                ),
                None,
                session,
            )
        assert stale_claim.value.status_code == 403

    async with postgres_harness.session() as session:
        relinked = await media_route._authorized_target(
            _route_request(),
            TenantContext(
                tenant_id,
                role="builder",
                builder_id=foreign_builder_id,
            ),
            None,
            session,
        )
        assert relinked.id == tenant_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revocation", "operation"),
    [
        (revocation, operation)
        for revocation in ("transfer", "unlink", "deactivate", "reclassify")
        for operation in ("list", "download", "patch", "delete")
    ],
)
async def test_g5_media_access_is_revalidated_after_concurrent_revocation(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
    operation: str,
) -> None:
    tenant_id = uuid4()
    builder_id = uuid4()
    foreign_builder_id = uuid4()
    clinic_user_id = uuid4()
    asset = _asset(tenant_id, b"secret-payload", display_name="Original metadata")
    async with postgres_harness.session() as session:
        session.add_all(
            [
                Builder(id=builder_id, name=f"Owner {tenant_id.hex}"),
                Builder(id=foreign_builder_id, name=f"Foreign {tenant_id.hex}"),
                Tenant(
                    id=tenant_id,
                    name="Concurrent auth clinic",
                    slug=f"concurrent-auth-{tenant_id.hex}",
                    api_key_hash=f"concurrent-auth-{tenant_id.hex}",
                    builder_id=builder_id,
                    is_active=True,
                    plan_type="clinic",
                    config={},
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                User(
                    tenant_id=tenant_id,
                    email=f"concurrent-builder-{tenant_id.hex}@test.local",
                    password_hash="unused",
                    role=UserRole.builder,
                    builder_id=builder_id,
                ),
                User(
                    id=clinic_user_id,
                    tenant_id=tenant_id,
                    email=f"concurrent-clinic-{tenant_id.hex}@test.local",
                    password_hash="unused",
                    role=UserRole.clinic_admin,
                ),
            ]
        )
        await session.flush()
        session.add(asset)
        await session.commit()

    async def revoke() -> None:
        async with postgres_harness.session() as revoker:
            if revocation in {"transfer", "unlink"}:
                await transition_tenant_media_owner(
                    revoker,
                    MediaOwnerTransition(
                        tenant_id=tenant_id,
                        expected_builder_id=builder_id,
                        destination_builder_id=(
                            foreign_builder_id if revocation == "transfer" else None
                        ),
                    ),
                )
            elif revocation == "deactivate":
                await revoker.execute(
                    update(Tenant).where(Tenant.id == tenant_id).values(is_active=False)
                )
            else:
                await revoker.execute(
                    update(User)
                    .where(User.id == clinic_user_id)
                    .values(role=UserRole.investor)
                )
            await revoker.commit()

    original_authorize = media_route._authorized_target
    revoked = False

    async def authorize_then_revoke(*args: Any, **kwargs: Any) -> Tenant:
        nonlocal revoked
        target = await original_authorize(*args, **kwargs)
        if not revoked:
            await revoke()
            revoked = True
        return target

    monkeypatch.setattr(media_route, "_authorized_target", authorize_then_revoke)
    tenant = TenantContext(tenant_id, role="builder", builder_id=builder_id)
    request = _route_request()
    async with postgres_harness.session() as session:
        if operation == "list":
            response = await media_route.list_media_assets(
                request=request,
                tenant_id=None,
                cursor=None,
                page_size=25,
                tenant=tenant,
                session=session,
            )
            assert response.items == []
        else:
            with pytest.raises(HTTPException) as denied:
                if operation == "download":
                    await media_route.download_media_asset(
                        media_id=asset.id,
                        request=request,
                        tenant_id=None,
                        tenant=tenant,
                        session=session,
                    )
                elif operation == "patch":
                    await media_route.patch_media_asset(
                        media_id=asset.id,
                        patch=MediaAssetPatchRequest(display_name="Forbidden change"),
                        request=request,
                        tenant_id=None,
                        tenant=tenant,
                        session=session,
                    )
                else:
                    await media_route.delete_media_asset(
                        media_id=asset.id,
                        request=request,
                        tenant_id=None,
                        tenant=tenant,
                        session=session,
                    )
            assert denied.value.status_code == 404

    async with postgres_harness.session() as session:
        persisted = await session.get(MediaAsset, asset.id)
        assert persisted is not None
        assert persisted.display_name == "Original metadata"
        assert persisted.payload == b"secret-payload"


@pytest.mark.asyncio
async def test_patch_revalidates_again_between_metadata_read_and_update(
    postgres_harness: _Harness,
) -> None:
    tenant_id = uuid4()
    asset = _asset(tenant_id, b"patch-race", display_name="Before race")
    async with postgres_harness.session() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="PATCH revalidation clinic",
                slug=f"patch-revalidation-{tenant_id.hex}",
                api_key_hash=f"patch-revalidation-{tenant_id.hex}",
                is_active=True,
                plan_type="clinic",
                config={},
            )
        )
        await session.flush()
        session.add(asset)
        await session.commit()

    class RevokeAfterMetadataRead:
        def __init__(self, session: AsyncSession) -> None:
            self.session = session
            self.revoked = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.session, name)

        async def execute(self, statement: Any) -> Any:
            result = await self.session.execute(statement)
            rendered = str(statement).lower()
            if (
                not self.revoked
                and "media_assets.display_name" in rendered
                and "for update" in rendered
                and not rendered.lstrip().startswith("update")
            ):
                async with postgres_harness.session() as revoker:
                    await revoker.execute(
                        update(Tenant)
                        .where(Tenant.id == tenant_id)
                        .values(is_active=False)
                    )
                    await revoker.commit()
                self.revoked = True
            return result

    async with postgres_harness.session() as raw_session:
        route_session = RevokeAfterMetadataRead(raw_session)
        with pytest.raises(HTTPException) as denied:
            await media_route.patch_media_asset(
                media_id=asset.id,
                patch=MediaAssetPatchRequest(display_name="After race"),
                request=_route_request(),
                tenant_id=None,
                tenant=TenantContext(tenant_id, api_key="tenant-scoped"),
                session=route_session,  # type: ignore[arg-type]
            )
        assert denied.value.status_code == 404
        assert route_session.revoked is True

    async with postgres_harness.session() as session:
        persisted = await session.get(MediaAsset, asset.id)
        assert persisted is not None
        assert persisted.display_name == "Before race"


@pytest.mark.asyncio
async def test_media_route_postgres_keyset_patch_download_delete_roundtrip(
    postgres_harness: _Harness,
) -> None:
    tenant_id = uuid4()
    foreign_tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    assets = [
        _asset(
            tenant_id,
            f"payload-{index}".encode(),
            display_name=f"Asset {index}",
            original_filename=f"ativo-{index}-ç.pdf",
            usage_trigger=f"trigger {index}",
        )
        for index in range(4)
    ]
    for index, asset in enumerate(assets):
        asset.created_at = now - timedelta(seconds=index)
        asset.updated_at = asset.created_at
    foreign_asset = _asset(foreign_tenant_id, b"foreign")
    async with postgres_harness.session() as session:
        session.add_all(
            [
                Tenant(
                    id=tenant_id,
                    name="CRUD Clinic",
                    slug=f"crud-{tenant_id.hex}",
                    api_key_hash=f"crud-{tenant_id.hex}",
                    plan_type="clinic",
                    config={},
                ),
                Tenant(
                    id=foreign_tenant_id,
                    name="Foreign CRUD Clinic",
                    slug=f"crud-{foreign_tenant_id.hex}",
                    api_key_hash=f"crud-{foreign_tenant_id.hex}",
                    plan_type="clinic",
                    config={},
                ),
            ]
        )
        await session.flush()
        session.add_all([*assets, foreign_asset])
        await session.commit()

    tenant = TenantContext(tenant_id, api_key="tenant-scoped")
    request = _route_request()
    async with postgres_harness.session() as session:
        first = await media_route.list_media_assets(
            request=request,
            tenant_id=None,
            cursor=None,
            page_size=2,
            tenant=tenant,
            session=session,
        )
        assert [item.id for item in first.items] == [assets[0].id, assets[1].id]
        assert first.has_more is True
        assert first.next_cursor is not None
        assert all(item.tenant_id == tenant_id for item in first.items)

    inserted_between_pages = _asset(
        tenant_id,
        b"newer",
        display_name="Inserted after page one",
    )
    inserted_between_pages.created_at = now + timedelta(seconds=1)
    inserted_between_pages.updated_at = inserted_between_pages.created_at
    async with postgres_harness.session() as session:
        session.add(inserted_between_pages)
        await session.delete(await session.get(MediaAsset, assets[2].id))
        await session.commit()

    async with postgres_harness.session() as session:
        second = await media_route.list_media_assets(
            request=request,
            tenant_id=None,
            cursor=first.next_cursor,
            page_size=2,
            tenant=tenant,
            session=session,
        )
        assert [item.id for item in second.items] == [assets[3].id]
        assert inserted_between_pages.id not in {item.id for item in second.items}
        assert not (
            {item.id for item in first.items} & {item.id for item in second.items}
        )
        assert second.has_more is False

    async with postgres_harness.session() as session:
        patched = await media_route.patch_media_asset(
            media_id=assets[1].id,
            patch=MediaAssetPatchRequest(
                display_name="  Nome preservado  ",
                original_filename="relatório final ç.pdf",
                usage_trigger=None,
                auto_suggest=True,
            ),
            request=request,
            tenant_id=None,
            tenant=tenant,
            session=session,
        )
        assert patched.display_name == "  Nome preservado  "
        assert patched.original_filename == "relatório final ç.pdf"
        assert patched.usage_trigger is None
        assert patched.auto_suggest is True

    async with postgres_harness.session() as session:
        response = await media_route.download_media_asset(
            media_id=assets[1].id,
            request=request,
            tenant_id=None,
            tenant=tenant,
            session=session,
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (
            "filename*=UTF-8''relat%C3%B3rio%20final%20%C3%A7.pdf"
            in response.headers["content-disposition"]
        )
        assert await _consume_route_response(response) == b"payload-1"
        assert request.app.state.media_concurrency.downloads.active == 0

    async with postgres_harness.session() as session:
        with pytest.raises(HTTPException) as cross_tenant:
            await media_route.patch_media_asset(
                media_id=foreign_asset.id,
                patch=MediaAssetPatchRequest(display_name="must not change"),
                request=request,
                tenant_id=None,
                tenant=tenant,
                session=session,
            )
        assert cross_tenant.value.status_code == 404
        assert getattr(cross_tenant.value, "detail") == {
            "code": "media_asset_not_found"
        }

    async with postgres_harness.session() as session:
        delete_response = await media_route.delete_media_asset(
            media_id=assets[1].id,
            request=request,
            tenant_id=None,
            tenant=tenant,
            session=session,
        )
        assert delete_response.status_code == 204
    async with postgres_harness.session() as session:
        assert await session.get(MediaAsset, assets[1].id) is None
        assert await session.get(MediaAsset, foreign_asset.id) is not None
