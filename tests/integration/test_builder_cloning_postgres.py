"""PostgreSQL concurrency and atomicity evidence for clinic cloning."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError, OperationalError, StatementError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.api.middleware.auth import TenantContext
from src.api.routes import builder as builder_routes
from src.api.routes import media_assets as media_route
from src.api.routes.config import patch_config
from src.api.schemas.builder import ClinicCloneRequest, ClinicCloneResponse
from src.api.schemas.config import ConfigPatchRequest
from src.api.schemas.media_assets import MediaAssetPatchRequest
from src.config.settings import settings
from src.handoff.models import HandoffReason, HandoffRequest
from src.handoff.webhook import WebhookDispatcher
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.handoff import HandoffRecord
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.session import Session
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.services import media_assets as media_service
from src.services.billing_usage_service import (
    PlanUsageLimits,
    get_builder_billing_usage,
)
from src.services.clinic_clone_service import MEDIA_ASSET_CLONE_FIELDS
from src.services.handoff_service import HandoffService
from src.services.media_assets import media_payload_sha256, media_storage_advisory_key
from tests.integration.test_media_assets_postgres import _route_request, _UploadRequest

_ROOT = Path(__file__).resolve().parents[2]
_ADMIN_DATABASES = {"postgres"}
_CHILD_PATTERN = re.compile(r"^neurostack_clone_smoke_[0-9a-f]{12}$")
_SOURCE_ERROR = {"error": "clinic_clone_source_not_found"}
_INVALID_ERROR = {"error": "clinic_clone_invalid"}
_CAP_ERROR = {"error": "clinic_clone_media_storage_cap_exceeded"}
_PERSISTENCE_ERROR = {"error": "clinic_clone_persistence_failed"}
_CONTENTS = ("Olá, visão 👁️", "漢字 e café com açúcar")
_MEDIA_PAYLOADS = (
    b"%PDF-1.7\n" + "Olá 👁️ mídia".encode() + b"\x00\xff" * 4,
    b"\x89PNG\r\n\x1a\n" + b"p" * 23,
    b"%PDF-1.7\n" + b"z" * 31,
)
_SOURCE_MEDIA_TIMESTAMP = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_TASK_TIMEOUT_SECONDS = 15
_CLEANUP_TIMEOUT_SECONDS = 5


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
    user_id: UUID
    source_id: UUID
    source_name: str

    @property
    def context(self) -> TenantContext:
        return TenantContext(
            tenant_id=self.auth_tenant_id,
            user_id=self.user_id,
            role=UserRole.builder.value,
            builder_id=self.builder_id,
        )


class _RecordingSession(AsyncSession):
    """Run real statements while retaining which ORM roots were read."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.read_entities: list[type[Any] | None] = []

    async def execute(self, statement: Any, params: Any = None, **kwargs: Any) -> Any:
        descriptions = getattr(statement, "column_descriptions", ())
        entity = descriptions[0].get("entity") if descriptions else None
        self.read_entities.append(entity)
        return await super().execute(statement, params, **kwargs)


class _SourcePauseSession(_RecordingSession):
    """Pause after PostgreSQL grants the source row lock."""

    def __init__(
        self,
        *args: Any,
        source_locked: asyncio.Event,
        resume: asyncio.Event,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.source_locked = source_locked
        self.resume = resume
        self._did_pause = False

    async def execute(self, statement: Any, params: Any = None, **kwargs: Any) -> Any:
        result = await super().execute(statement, params, **kwargs)
        descriptions = getattr(statement, "column_descriptions", ())
        entity = descriptions[0].get("entity") if descriptions else None
        is_locked_source = (
            entity is Tenant and getattr(statement, "_for_update_arg", None) is not None
        )
        if is_locked_source and not self._did_pause:
            self._did_pause = True
            self.source_locked.set()
            await self.resume.wait()
        return result


class _InjectedBoundaryFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class _SourceRowSnapshot:
    config_ids: frozenset[UUID]
    document_ids: frozenset[UUID]


def _same_database(left: URL, right: URL) -> bool:
    return (
        left.host,
        left.port,
        left.database,
    ) == (
        right.host,
        right.port,
        right.database,
    )


def _validated_admin_url(raw_url: str, application_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("smoke URL must use PostgreSQL")
    if url.query:
        raise ValueError("smoke URL must not use query parameters")
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
    return URL.create(
        drivername="postgresql+asyncpg",
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database=url.database,
    )


def _validated_child_name(name: str) -> str:
    if _CHILD_PATTERN.fullmatch(name) is None:
        raise ValueError("unsafe smoke child database name")
    return name


@pytest.mark.parametrize(
    "query",
    [
        "host=attacker.invalid",
        "port=6432",
        "database=application",
    ],
)
def test_admin_url_rejects_connection_query_overrides(query: str) -> None:
    with pytest.raises(ValueError, match="query parameters"):
        _validated_admin_url(
            f"postgresql://admin:secret@localhost/postgres?{query}",
            "postgresql://application:secret@localhost/application",
        )


def test_application_database_identity_ignores_username() -> None:
    with pytest.raises(ValueError, match="application database"):
        _validated_admin_url(
            "postgresql://admin:secret@localhost/postgres",
            "postgresql://application:other-secret@localhost/postgres",
        )


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


@pytest.fixture(scope="module")
async def postgres_harness() -> AsyncIterator[_Harness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        # Critical concurrency evidence fails closed; it is never skipped.
        pytest.fail(
            "POSTGRES_SMOKE_DATABASE_URL is required for clinic clone integration "
            "tests",
            pytrace=False,
        )

    admin_url = _validated_admin_url(raw_admin_url, settings.database_url)
    child_name = _validated_child_name(f"neurostack_clone_smoke_{uuid4().hex[:12]}")
    child_url = admin_url.set(database=child_name)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    child_engine: AsyncEngine | None = None
    created_database = False
    unexpected_backends: list[dict[str, Any]] = []
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
            if not can_create:
                raise PermissionError("smoke database user must have CREATEDB")
            quoted_name = connection.dialect.identifier_preparer.quote(child_name)
            await connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
            created_database = True

        await _run_alembic(child_url, "upgrade", "head")
        child_engine = create_async_engine(child_url, poolclass=NullPool)
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
        try:
            if child_engine is not None:
                await child_engine.dispose()
            if created_database:
                async with admin_engine.connect() as connection:
                    unexpected_backends = [
                        dict(row)
                        for row in (
                            await connection.execute(
                                text(
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
                        text(
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


def _source_media_asset(source_id: UUID, index: int, payload: bytes) -> MediaAsset:
    is_png = payload.startswith(b"\x89PNG")
    return MediaAsset(
        tenant_id=source_id,
        display_name=f"  Mídia {index} 👁️  ",
        original_filename=f"mídia-{index}.{'png' if is_png else 'pdf'}",
        category="image" if is_png else "pdf",
        mime_type="image/png" if is_png else "application/pdf",
        size_bytes=len(payload),
        sha256=media_payload_sha256(payload),
        payload=payload,
        usage_trigger=f"gatilho {index}" if index % 2 == 0 else None,
        auto_suggest=index % 2 == 0,
        created_at=_SOURCE_MEDIA_TIMESTAMP,
        updated_at=_SOURCE_MEDIA_TIMESTAMP,
    )


async def _seed_graph(
    harness: _Harness,
    *,
    source_name: str = "Clínica Raiz",
    contents: tuple[str, ...] = _CONTENTS,
    media_payloads: tuple[bytes, ...] = (),
) -> _Graph:
    graph = _Graph(
        builder_id=uuid4(),
        foreign_builder_id=uuid4(),
        auth_tenant_id=uuid4(),
        user_id=uuid4(),
        source_id=uuid4(),
        source_name=source_name,
    )
    async with harness.session() as session:
        session.add_all(
            [
                Builder(
                    id=graph.builder_id,
                    name=f"Clone Builder {graph.builder_id.hex[:8]}",
                    status="active",
                    is_active=True,
                ),
                Builder(
                    id=graph.foreign_builder_id,
                    name=f"Foreign Builder {graph.foreign_builder_id.hex[:8]}",
                    status="active",
                    is_active=True,
                ),
                Tenant(
                    id=graph.auth_tenant_id,
                    name="Builder login tenant",
                    slug=f"clone-auth-{graph.auth_tenant_id.hex}",
                    api_key_hash=f"auth-hash-{graph.auth_tenant_id.hex}",
                    is_active=True,
                    plan_type="admin",
                    config={},
                ),
                Tenant(
                    id=graph.source_id,
                    name=source_name,
                    slug=f"clone-source-{graph.source_id.hex}",
                    api_key_hash=f"source-hash-{graph.source_id.hex}",
                    is_active=True,
                    plan_type="clinic",
                    builder_id=graph.builder_id,
                    config={},
                ),
            ]
        )
        session.add(
            User(
                id=graph.user_id,
                tenant_id=graph.auth_tenant_id,
                email=f"clone-builder-{graph.user_id.hex}@test.local",
                password_hash="unused",
                role=UserRole.builder,
                builder_id=graph.builder_id,
                is_active=True,
            )
        )
        session.add(
            ClinicConfigTable(
                tenant_id=graph.source_id,
                version=1,
                persona={"agent_display_name": "Lia"},
                scheduling={},
                pricing={},
                post_care={},
                handoff={},
                identity={
                    "clinic_name": source_name,
                    "clinic_history": "História íntegra",
                },
                operations={},
                business_rules={},
                lgpd={},
                professionals=[{"name": "Dra. Íris"}],
                services=[{"name": "Avaliação"}],
                api_credentials={},
            )
        )
        session.add_all(
            [
                KnowledgeDocument(
                    tenant_id=graph.source_id,
                    title=f"Documento {index}",
                    content=content,
                    content_type="text",
                    document_metadata={"order": index},
                    vector_id=f"source-vector-{index}",
                    is_indexed=True,
                    chunk_count=3,
                    size_bytes=999_999,
                )
                for index, content in enumerate(contents)
            ]
        )
        await session.flush()
        session.add_all(
            [
                _source_media_asset(graph.source_id, index, payload)
                for index, payload in enumerate(media_payloads)
            ]
        )
        await session.commit()
    return graph


async def _clone(
    session: AsyncSession,
    graph: _Graph,
    *,
    source_id: UUID | None = None,
    include_rag: bool = False,
    include_media: bool = False,
) -> ClinicCloneResponse:
    return await builder_routes.clone_clinic(
        source_id=str(source_id or graph.source_id),
        clone_request=ClinicCloneRequest(
            include_rag=include_rag,
            include_media=include_media,
        ),
        tenant=graph.context,
        session=session,
    )


def _new_session(
    harness: _Harness,
    session_type: type[AsyncSession] = AsyncSession,
    **kwargs: Any,
) -> AsyncSession:
    return session_type(
        bind=harness.engine,
        expire_on_commit=False,
        autoflush=False,
        **kwargs,
    )


def _stub_fast_unique_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        builder_routes.pwd_context,
        "hash",
        lambda token: f"safe-{token}",
    )


async def _cancel_and_gather(*tasks: asyncio.Task[Any] | None) -> None:
    created_tasks = [task for task in tasks if task is not None]
    for task in created_tasks:
        if not task.done():
            task.cancel()
    if created_tasks:
        await asyncio.wait_for(
            asyncio.gather(*created_tasks, return_exceptions=True),
            timeout=_CLEANUP_TIMEOUT_SECONDS,
        )


async def _wait_for_task(task: asyncio.Task[Any]) -> Any:
    return await asyncio.wait_for(task, timeout=_TASK_TIMEOUT_SECONDS)


async def _wait_for_tasks(*tasks: asyncio.Task[Any]) -> list[Any]:
    return list(
        await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=_TASK_TIMEOUT_SECONDS,
        )
    )


async def _backend_pid(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")))


async def _lock_row(session: AsyncSession, model: Any, row_id: UUID) -> None:
    result = await session.execute(
        select(model).where(model.id == row_id).with_for_update()
    )
    assert result.scalar_one() is not None


async def _await_blocked(
    harness: _Harness,
    blocked_pid: int,
    blocker_pid: int,
) -> None:
    async def inspect() -> None:
        async with harness.session() as inspector:
            for _ in range(1_000):
                blockers = await inspector.scalar(
                    text("SELECT pg_blocking_pids(:pid)"),
                    {"pid": blocked_pid},
                )
                if blocker_pid in (blockers or []):
                    return
        raise AssertionError(
            f"backend {blocked_pid} never blocked behind backend {blocker_pid}"
        )

    await asyncio.wait_for(inspect(), timeout=10)


async def _assert_row_lock_state(
    harness: _Harness,
    model: Any,
    row_id: UUID,
    *,
    locked: bool,
) -> None:
    async with harness.session() as probe:
        try:
            await probe.execute(
                select(model).where(model.id == row_id).with_for_update(nowait=True)
            )
        except DBAPIError as error:
            await probe.rollback()
            assert locked is True
            assert getattr(error.orig, "sqlstate", None) == "55P03"
        else:
            await probe.rollback()
            assert locked is False


async def _mutate_source(
    session: AsyncSession,
    graph: _Graph,
    mutation: str,
) -> None:
    if mutation == "delete":
        await session.execute(delete(Tenant).where(Tenant.id == graph.source_id))
    else:
        builder_id = None if mutation == "unlink" else graph.foreign_builder_id
        await session.execute(
            update(Tenant)
            .where(Tenant.id == graph.source_id)
            .values(builder_id=builder_id)
        )
    await session.commit()


async def _assert_no_clone_residuals(harness: _Harness, graph: _Graph) -> None:
    clone_ids = select(Tenant.id).where(
        Tenant.builder_id == graph.builder_id,
        Tenant.id != graph.source_id,
    )
    async with harness.session() as observer:
        tenant_count = await observer.scalar(
            select(func.count(Tenant.id)).where(Tenant.id.in_(clone_ids))
        )
        config_count = await observer.scalar(
            select(func.count(ClinicConfigTable.id)).where(
                ClinicConfigTable.tenant_id.in_(clone_ids)
            )
        )
        document_count = await observer.scalar(
            select(func.count(KnowledgeDocument.id)).where(
                KnowledgeDocument.tenant_id.in_(clone_ids)
            )
        )
        media_count = await observer.scalar(
            select(func.count(MediaAsset.id)).where(MediaAsset.tenant_id.in_(clone_ids))
        )
    assert (tenant_count, config_count, document_count, media_count) == (0, 0, 0, 0)


async def _source_row_snapshot(
    harness: _Harness,
    graph: _Graph,
) -> _SourceRowSnapshot:
    async with harness.session() as observer:
        config_ids = frozenset(
            (
                await observer.execute(
                    select(ClinicConfigTable.id).where(
                        ClinicConfigTable.tenant_id == graph.source_id
                    )
                )
            )
            .scalars()
            .all()
        )
        document_ids = frozenset(
            (
                await observer.execute(
                    select(KnowledgeDocument.id).where(
                        KnowledgeDocument.tenant_id == graph.source_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return _SourceRowSnapshot(
        config_ids=config_ids,
        document_ids=document_ids,
    )


async def _assert_boundary_rollback(
    harness: _Harness,
    graph: _Graph,
    source_snapshot: _SourceRowSnapshot,
    attempted_ids: dict[type[Any], set[UUID]],
) -> None:
    async with harness.session() as observer:
        attempted_tenants = await observer.scalar(
            select(func.count(Tenant.id)).where(Tenant.id.in_(attempted_ids[Tenant]))
        )
        attempted_configs = await observer.scalar(
            select(func.count(ClinicConfigTable.id)).where(
                ClinicConfigTable.id.in_(attempted_ids[ClinicConfigTable])
            )
        )
        attempted_documents = await observer.scalar(
            select(func.count(KnowledgeDocument.id)).where(
                KnowledgeDocument.id.in_(attempted_ids[KnowledgeDocument])
            )
        )
        source = await observer.get(Tenant, graph.source_id)
        current_config_ids = frozenset(
            (
                await observer.execute(
                    select(ClinicConfigTable.id).where(
                        ClinicConfigTable.tenant_id == graph.source_id
                    )
                )
            )
            .scalars()
            .all()
        )
        current_document_ids = frozenset(
            (
                await observer.execute(
                    select(KnowledgeDocument.id).where(
                        KnowledgeDocument.tenant_id == graph.source_id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert (attempted_tenants, attempted_configs, attempted_documents) == (0, 0, 0)
    assert source is not None and source.builder_id == graph.builder_id
    assert current_config_ids == source_snapshot.config_ids
    assert current_document_ids == source_snapshot.document_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_clones_and_clone_of_clone_keep_one_root(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(harness)
    _stub_fast_unique_hash(monkeypatch)
    existing_copy_id = uuid4()
    async with harness.session() as seed:
        seed.add(
            Tenant(
                id=existing_copy_id,
                name="Clínica Raiz cópia 4",
                slug=f"existing-copy-{existing_copy_id.hex}",
                api_key_hash=f"existing-copy-hash-{existing_copy_id.hex}",
                is_active=False,
                plan_type="clinic",
                builder_id=graph.builder_id,
                config={},
            )
        )
        await seed.commit()

    source_locked = asyncio.Event()
    resume = asyncio.Event()
    first = _new_session(
        harness,
        _SourcePauseSession,
        source_locked=source_locked,
        resume=resume,
    )
    second = harness.session()
    first_task: asyncio.Task[Any] | None = None
    second_task: asyncio.Task[Any] | None = None
    try:
        first_pid = await _backend_pid(first)
        first_task = asyncio.create_task(_clone(first, graph, include_rag=True))
        await asyncio.wait_for(source_locked.wait(), timeout=10)

        second_pid = await _backend_pid(second)
        second_task = asyncio.create_task(_clone(second, graph, include_rag=True))
        await _await_blocked(harness, second_pid, first_pid)
        resume.set()
        first_response, second_response = await _wait_for_tasks(
            first_task,
            second_task,
        )
    finally:
        resume.set()
        try:
            await _cancel_and_gather(first_task, second_task)
        finally:
            await first.close()
            await second.close()

    responses = (first_response, second_response)
    assert {response.name for response in responses} == {
        "Clínica Raiz cópia 5",
        "Clínica Raiz cópia 6",
    }

    clone_ids = {response.id for response in responses}
    async with harness.session() as observer:
        clones = list(
            (await observer.execute(select(Tenant).where(Tenant.id.in_(clone_ids))))
            .scalars()
            .all()
        )
        assert len({clone.slug for clone in clones}) == 2
        for clone in clones:
            assert clone.slug == (
                f"clinica-raiz-copia-{clone.name.rsplit(' ', 1)[1]}-{clone.id.hex}"
            )

    source_clone_id = min(responses, key=lambda response: response.name).id
    async with harness.session() as third:
        third_response = await _clone(
            third, graph, source_id=source_clone_id, include_rag=True
        )
    assert third_response.name == "Clínica Raiz cópia 7"

    async with harness.session() as observer:
        third_tenant = await observer.get(Tenant, third_response.id)
        assert third_tenant is not None
        assert third_tenant.slug == f"clinica-raiz-copia-7-{third_response.id.hex}"
        third_config = (
            await observer.execute(
                select(ClinicConfigTable).where(
                    ClinicConfigTable.tenant_id == third_response.id
                )
            )
        ).scalar_one()
        third_documents = list(
            (
                await observer.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.tenant_id == third_response.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert third_config.identity["clinic_name"] == "Clínica Raiz cópia 7"
    assert {document.content for document in third_documents} == set(_CONTENTS)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_row_locks_enforce_user_builder_source_order(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = postgres_harness
    _stub_fast_unique_hash(monkeypatch)

    builder_graph = await _seed_graph(harness, source_name="Ordem Builder")
    blocker = harness.session()
    clone = harness.session()
    clone_task: asyncio.Task[Any] | None = None
    try:
        await _lock_row(blocker, Builder, builder_graph.builder_id)
        blocker_pid = await _backend_pid(blocker)
        clone_pid = await _backend_pid(clone)
        clone_task = asyncio.create_task(_clone(clone, builder_graph, include_rag=True))
        await _await_blocked(harness, clone_pid, blocker_pid)
        await _assert_row_lock_state(harness, User, builder_graph.user_id, locked=True)
        await _assert_row_lock_state(
            harness, Tenant, builder_graph.source_id, locked=False
        )
        await blocker.rollback()
        await _wait_for_task(clone_task)
    finally:
        await blocker.rollback()
        try:
            await _cancel_and_gather(clone_task)
        finally:
            await blocker.close()
            await clone.close()

    source_graph = await _seed_graph(harness, source_name="Ordem Source")
    blocker = harness.session()
    clone = harness.session()
    clone_task = None
    try:
        await _lock_row(blocker, Tenant, source_graph.source_id)
        blocker_pid = await _backend_pid(blocker)
        clone_pid = await _backend_pid(clone)
        clone_task = asyncio.create_task(_clone(clone, source_graph, include_rag=True))
        await _await_blocked(harness, clone_pid, blocker_pid)
        await _assert_row_lock_state(harness, User, source_graph.user_id, locked=True)
        await _assert_row_lock_state(
            harness, Builder, source_graph.builder_id, locked=True
        )
        await blocker.rollback()
        await _wait_for_task(clone_task)
    finally:
        await blocker.rollback()
        try:
            await _cancel_and_gather(clone_task)
        finally:
            await blocker.close()
            await clone.close()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["unlink", "reassign", "delete"])
async def test_source_mutation_before_source_lock_is_404_without_clone_reads(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(harness, source_name=f"Before {mutation}")
    _stub_fast_unique_hash(monkeypatch)

    blocker = harness.session()
    clone = _new_session(harness, _RecordingSession)
    clone_task: asyncio.Task[Any] | None = None
    try:
        await _lock_row(blocker, Builder, graph.builder_id)
        blocker_pid = await _backend_pid(blocker)
        clone_pid = await _backend_pid(clone)
        clone_task = asyncio.create_task(_clone(clone, graph, include_rag=True))
        await _await_blocked(harness, clone_pid, blocker_pid)
        async with harness.session() as mutator:
            await _mutate_source(mutator, graph, mutation)
        await blocker.rollback()

        with pytest.raises(HTTPException) as raised:
            await _wait_for_task(clone_task)
        assert raised.value.status_code == 404
        assert raised.value.detail == _SOURCE_ERROR
        assert ClinicConfigTable not in clone.read_entities
        assert KnowledgeDocument not in clone.read_entities
    finally:
        await blocker.rollback()
        try:
            await _cancel_and_gather(clone_task)
        finally:
            await blocker.close()
            await clone.close()

    await _assert_no_clone_residuals(harness, graph)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["unlink", "reassign", "delete"])
async def test_source_mutation_after_source_lock_waits_for_complete_snapshot(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(harness, source_name=f"After {mutation}")
    _stub_fast_unique_hash(monkeypatch)

    source_locked = asyncio.Event()
    resume = asyncio.Event()
    clone = _new_session(
        harness,
        _SourcePauseSession,
        source_locked=source_locked,
        resume=resume,
    )
    mutator = harness.session()
    clone_task: asyncio.Task[Any] | None = None
    mutation_task: asyncio.Task[Any] | None = None
    try:
        clone_pid = await _backend_pid(clone)
        clone_task = asyncio.create_task(_clone(clone, graph, include_rag=True))
        await asyncio.wait_for(source_locked.wait(), timeout=10)

        mutator_pid = await _backend_pid(mutator)
        mutation_task = asyncio.create_task(_mutate_source(mutator, graph, mutation))
        await _await_blocked(harness, mutator_pid, clone_pid)
        assert mutation_task.done() is False

        resume.set()
        response, _ = await _wait_for_tasks(clone_task, mutation_task)
    finally:
        resume.set()
        try:
            await _cancel_and_gather(clone_task, mutation_task)
        finally:
            await clone.close()
            await mutator.close()

    async with harness.session() as observer:
        clone_tenant = await observer.get(Tenant, response.id)
        clone_config = (
            await observer.execute(
                select(ClinicConfigTable).where(
                    ClinicConfigTable.tenant_id == response.id
                )
            )
        ).scalar_one()
        clone_documents = list(
            (
                await observer.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.tenant_id == response.id
                    )
                )
            )
            .scalars()
            .all()
        )
        source = await observer.get(Tenant, graph.source_id)

    assert clone_tenant is not None
    assert clone_tenant.builder_id == graph.builder_id
    assert clone_config.identity["clinic_history"] == "História íntegra"
    assert clone_config.professionals == [{"name": "Dra. Íris"}]
    assert clone_config.services == [{"name": "Avaliação"}]
    assert {document.content for document in clone_documents} == set(_CONTENTS)
    assert len(clone_documents) == len(_CONTENTS)
    if mutation == "delete":
        assert source is None
    elif mutation == "unlink":
        assert source is not None and source.builder_id is None
    else:
        assert source is not None and source.builder_id == graph.foreign_builder_id


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    [Tenant, ClinicConfigTable, KnowledgeDocument],
    ids=["tenant", "config", "knowledge"],
)
async def test_failure_after_each_insert_boundary_leaves_zero_residual_rows(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    boundary: type[Any],
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(harness, source_name=f"Rollback {boundary.__name__}")
    _stub_fast_unique_hash(monkeypatch)
    source_snapshot = await _source_row_snapshot(harness, graph)
    attempted_ids: dict[type[Any], set[UUID]] = {
        Tenant: set(),
        ClinicConfigTable: set(),
        KnowledgeDocument: set(),
    }

    def capture_attempted_insert(
        _mapper: Any,
        _connection: Any,
        target: Any,
    ) -> None:
        attempted_ids[type(target)].add(target.id)

    def fail_after_insert(_mapper: Any, _connection: Any, target: Any) -> None:
        if boundary is not Tenant or target.id != graph.source_id:
            raise _InjectedBoundaryFailure(boundary.__name__)

    for model in attempted_ids:
        event.listen(model, "before_insert", capture_attempted_insert)
    event.listen(boundary, "after_insert", fail_after_insert)
    try:
        async with harness.session() as clone:
            with pytest.raises(_InjectedBoundaryFailure, match=boundary.__name__):
                await _clone(clone, graph, include_rag=True)
    finally:
        event.remove(boundary, "after_insert", fail_after_insert)
        for model in attempted_ids:
            event.remove(model, "before_insert", capture_attempted_insert)

    assert attempted_ids[boundary]
    await _assert_boundary_rollback(
        harness,
        graph,
        source_snapshot,
        attempted_ids,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clone_billing_delta_uses_recalculated_multibyte_utf8_bytes(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(harness, source_name="Cobrança UTF-8")
    _stub_fast_unique_hash(monkeypatch)
    actual_bytes = sum(len(content.encode("utf-8")) for content in _CONTENTS)

    async with harness.session() as session:
        source_ids = list(
            (
                await session.execute(
                    select(KnowledgeDocument.id)
                    .where(KnowledgeDocument.tenant_id == graph.source_id)
                    .order_by(KnowledgeDocument.id)
                )
            )
            .scalars()
            .all()
        )
        stale_sizes = (1, 2)
        for document_id, stale_size in zip(source_ids, stale_sizes, strict=True):
            await session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id == document_id)
                .values(size_bytes=stale_size)
            )
        await session.commit()

    limits = PlanUsageLimits(
        monthly_traces=1_000,
        rag_storage_mb=500,
        media_storage_mb=500,
    )
    async with harness.session() as billing:
        before = await get_builder_billing_usage(
            billing,
            graph.builder_id,
            "start",
            limits,
        )
    assert before["storage"]["rag"]["used_bytes"] == sum(stale_sizes)
    assert actual_bytes != sum(stale_sizes)

    async with harness.session() as clone:
        response = await _clone(clone, graph, include_rag=True)

    async with harness.session() as billing:
        after = await get_builder_billing_usage(
            billing,
            graph.builder_id,
            "start",
            limits,
        )
        cloned_sizes = list(
            (
                await billing.execute(
                    select(KnowledgeDocument.size_bytes).where(
                        KnowledgeDocument.tenant_id == response.id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert (
        after["storage"]["rag"]["used_bytes"] - before["storage"]["rag"]["used_bytes"]
        == actual_bytes
    )
    assert sum(cloned_sizes) == actual_bytes
    assert sorted(cloned_sizes) == sorted(
        len(content.encode("utf-8")) for content in _CONTENTS
    )


# ============================================
# Selective clone: flags, preview, media guard and failure evidence
# ============================================


def _record_statements(
    engine: AsyncEngine,
) -> tuple[list[str], list[Any], Callable[..., None]]:
    statements: list[str] = []
    parameters: list[Any] = []

    def record(
        _connection: Any,
        _cursor: Any,
        statement: str,
        statement_parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))
        parameters.append(statement_parameters)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    return statements, parameters, record


def _stop_recording(engine: AsyncEngine, listener: Callable[..., None]) -> None:
    event.remove(engine.sync_engine, "before_cursor_execute", listener)


def _statement_index(
    statements: list[str],
    fragment: str,
    *,
    after: int = -1,
    also: str | None = None,
) -> int:
    return next(
        index
        for index, statement in enumerate(statements)
        if index > after
        and fragment in statement
        and (also is None or also in statement)
    )


async def _media_rows(harness: _Harness, tenant_id: UUID) -> list[MediaAsset]:
    async with harness.session() as observer:
        return list(
            (
                await observer.execute(
                    select(MediaAsset)
                    .where(MediaAsset.tenant_id == tenant_id)
                    .order_by(MediaAsset.id)
                )
            )
            .scalars()
            .all()
        )


def _media_signature(rows: list[MediaAsset]) -> list[tuple[Any, ...]]:
    return [
        (row.id, *(getattr(row, field) for field in MEDIA_ASSET_CLONE_FIELDS))
        for row in rows
    ]


async def _owner_media_bytes(harness: _Harness, graph: _Graph) -> int:
    async with harness.session() as observer:
        return int(
            await observer.scalar(
                select(func.coalesce(func.sum(MediaAsset.size_bytes), 0))
                .select_from(MediaAsset)
                .join(Tenant, Tenant.id == MediaAsset.tenant_id)
                .where(Tenant.builder_id == graph.builder_id)
            )
        )


_SOURCE_SLA_TARGET_MINUTES = 15  # non-default, so a leaked copy is visible


async def _set_source_sla_target(harness: _Harness, graph: _Graph) -> None:
    async with harness.session() as seed:
        await seed.execute(
            update(ClinicConfigTable)
            .where(ClinicConfigTable.tenant_id == graph.source_id)
            .values(
                handoff={
                    "default_mode": "crm_task_create",
                    "meta_minutos_atendimento": _SOURCE_SLA_TARGET_MINUTES,
                }
            )
        )
        await seed.commit()


async def _assert_clone_omits_sla_target(
    harness: _Harness, graph: _Graph, clone_id: UUID
) -> None:
    """The destination JSONB never holds an explicit tenant-local SLA target."""
    async with harness.session() as observer:
        has_key = dict(
            (
                await observer.execute(
                    select(
                        ClinicConfigTable.tenant_id,
                        ClinicConfigTable.handoff.has_key("meta_minutos_atendimento"),
                    ).where(
                        ClinicConfigTable.tenant_id.in_([graph.source_id, clone_id])
                    )
                )
            ).all()
        )
        clone_handoff = await observer.scalar(
            select(ClinicConfigTable.handoff).where(
                ClinicConfigTable.tenant_id == clone_id
            )
        )
    assert has_key == {graph.source_id: True, clone_id: False}
    assert clone_handoff["default_mode"] == "crm_task_create"


async def _assert_owner_locks_released(
    harness: _Harness,
    graph: _Graph,
    media_ids: list[UUID],
) -> None:
    async with harness.session() as probe:
        acquired = await probe.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": media_storage_advisory_key(graph.builder_id)},
        )
        await probe.rollback()
    assert acquired is True
    await _assert_row_lock_state(harness, Tenant, graph.source_id, locked=False)
    for media_id in media_ids:
        await _assert_row_lock_state(harness, MediaAsset, media_id, locked=False)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_rag", "include_media"),
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["config-only", "rag-only", "media-only", "rag-and-media"],
)
async def test_each_flag_combination_copies_exactly_the_selected_sets(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    include_rag: bool,
    include_media: bool,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(
        harness,
        source_name=f"Combinação {int(include_rag)}{int(include_media)}",
        media_payloads=_MEDIA_PAYLOADS,
    )
    await _set_source_sla_target(harness, graph)
    _stub_fast_unique_hash(monkeypatch)
    if not include_media:

        def forbidden_guard(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("non-media clone entered the media storage guard")

        monkeypatch.setattr(
            builder_routes, "media_storage_capacity_guard", forbidden_guard
        )
    source_media = await _media_rows(harness, graph.source_id)
    statements, _, listener = _record_statements(harness.engine)
    try:
        async with harness.session() as clone:
            response = await _clone(
                clone,
                graph,
                include_rag=include_rag,
                include_media=include_media,
            )
    finally:
        _stop_recording(harness.engine, listener)

    assert response.is_active is False
    assert response.copied_rag_documents == (len(_CONTENTS) if include_rag else 0)
    assert response.copied_media_assets == (
        len(_MEDIA_PAYLOADS) if include_media else 0
    )
    if not include_media:
        # Configuration-only and RAG-only clones never take the owner advisory
        # lock nor touch media_assets at all.
        assert not any("pg_advisory" in sql for sql in statements)
        assert not any("media_assets" in sql for sql in statements)
    if not include_rag:
        assert not any("knowledge_documents" in sql for sql in statements)

    async with harness.session() as observer:
        clone_tenant = await observer.get(Tenant, response.id)
        clone_config = (
            await observer.execute(
                select(ClinicConfigTable).where(
                    ClinicConfigTable.tenant_id == response.id
                )
            )
        ).scalar_one()
        clone_documents = list(
            (
                await observer.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.tenant_id == response.id
                    )
                )
            )
            .scalars()
            .all()
        )
        clone_users = await observer.scalar(
            select(func.count(User.id)).where(User.tenant_id == response.id)
        )
    assert clone_tenant is not None and clone_tenant.is_active is False
    assert clone_users == 0
    assert clone_config.identity["clinic_history"] == "História íntegra"
    assert clone_config.professionals == [{"name": "Dra. Íris"}]
    assert clone_config.api_credentials == {}
    assert clone_config.scheduling["scheduling_link_enabled"] is False
    assert clone_config.scheduling["scheduling_link_url"] is None
    await _assert_clone_omits_sla_target(harness, graph, response.id)
    if include_rag:
        assert sorted(document.content for document in clone_documents) == sorted(
            _CONTENTS
        )
        for document in clone_documents:
            assert document.vector_id is None
            assert document.is_indexed is False
            assert document.chunk_count == 0
            assert document.size_bytes == len(document.content.encode("utf-8"))
    else:
        assert clone_documents == []

    clone_media = await _media_rows(harness, response.id)
    if include_media:
        copies = {copy.sha256: copy for copy in clone_media}
        assert len(copies) == len(source_media) == len(clone_media)
        for source in source_media:
            copy = copies[source.sha256]
            assert copy.id != source.id
            assert copy.tenant_id == response.id
            for column in MEDIA_ASSET_CLONE_FIELDS:
                assert getattr(copy, column) == getattr(source, column)
            assert copy.created_at > _SOURCE_MEDIA_TIMESTAMP
            assert copy.updated_at > _SOURCE_MEDIA_TIMESTAMP
    else:
        assert clone_media == []
    assert _media_signature(await _media_rows(harness, graph.source_id)) == (
        _media_signature(source_media)
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_media_clone_follows_the_canonical_lock_and_query_order(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(
        harness,
        source_name="Ordem Mídias",
        media_payloads=_MEDIA_PAYLOADS,
    )
    _stub_fast_unique_hash(monkeypatch)
    stabilized_ids = [row.id for row in await _media_rows(harness, graph.source_id)]
    statements, parameters, listener = _record_statements(harness.engine)
    try:
        async with harness.session() as clone:
            response = await _clone(clone, graph, include_rag=True, include_media=True)
    finally:
        _stop_recording(harness.engine, listener)

    assert response.copied_media_assets == len(_MEDIA_PAYLOADS)
    user_lock = _statement_index(statements, "from users", also="for update")
    builder_lock = _statement_index(
        statements, "from builders", after=user_lock, also="for update"
    )
    resolution = _statement_index(statements, "from tenants", after=builder_lock)
    assert "for update" not in statements[resolution]
    guard_user = _statement_index(statements, "for key share", after=resolution)
    assert "from users" in statements[guard_user]
    advisory = _statement_index(statements, "pg_advisory_xact_lock", after=guard_user)
    assert media_storage_advisory_key(graph.builder_id) in tuple(parameters[advisory])
    assert sum("pg_advisory_xact_lock" in sql for sql in statements) == 1
    tenant_lock = _statement_index(
        statements, "from tenants", after=advisory, also="for update"
    )
    source_set = _statement_index(statements, "for share", after=tenant_lock)
    assert "from media_assets" in statements[source_set]
    assert "media_assets.size_bytes" in statements[source_set]
    assert "order by media_assets.id" in statements[source_set]
    assert "payload" not in statements[source_set]
    usage = _statement_index(
        statements, "sum(media_assets.size_bytes)", after=source_set
    )
    # No source size (or any media) query precedes the owner advisory lock and
    # the usage is measured exactly once, after the full set is share-locked.
    assert not any("media_assets" in sql for sql in statements[:advisory])
    assert sum("sum(media_assets.size_bytes)" in sql for sql in statements) == 1

    payload_reads = [
        index
        for index, sql in enumerate(statements)
        if sql.startswith("select") and "media_assets.payload" in sql
    ]
    media_inserts = [
        index
        for index, sql in enumerate(statements)
        if sql.startswith("insert into media_assets")
    ]
    assert len(payload_reads) == len(media_inserts) == len(_MEDIA_PAYLOADS)
    assert user_lock < builder_lock < advisory < tenant_lock < source_set < usage
    assert usage < payload_reads[0]
    for read, insert, next_read in zip(
        payload_reads,
        media_inserts,
        [*payload_reads[1:], len(statements)],
        strict=True,
    ):
        # One locked payload is fetched and inserted before the next is read.
        assert read < insert < next_read
        assert "media_assets.id = " in statements[read]
        assert "media_assets.tenant_id = " in statements[read]
    assert [tuple(parameters[read]) for read in payload_reads] == [
        (media_id, graph.source_id) for media_id in stabilized_ids
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preview_aggregates_are_exact_zero_safe_and_payload_free(
    postgres_harness: _Harness,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(
        harness,
        source_name="Prévia",
        media_payloads=_MEDIA_PAYLOADS,
    )
    empty = await _seed_graph(harness, source_name="Prévia vazia", contents=())
    statements, _, listener = _record_statements(harness.engine)
    try:
        async with harness.session() as session:
            populated = await builder_routes.preview_clinic_clone(
                source_id=str(graph.source_id),
                tenant=graph.context,
                session=session,
            )
        async with harness.session() as session:
            empty_preview = await builder_routes.preview_clinic_clone(
                source_id=str(empty.source_id),
                tenant=empty.context,
                session=session,
            )
    finally:
        _stop_recording(harness.engine, listener)

    utf8_bytes = sum(len(content.encode("utf-8")) for content in _CONTENTS)
    assert populated.document_count == len(_CONTENTS)
    assert populated.total_bytes == utf8_bytes
    assert utf8_bytes != sum(len(content) for content in _CONTENTS)
    assert populated.media_asset_count == len(_MEDIA_PAYLOADS)
    assert populated.media_total_bytes == sum(len(item) for item in _MEDIA_PAYLOADS)
    assert (
        empty_preview.document_count,
        empty_preview.total_bytes,
        empty_preview.media_asset_count,
        empty_preview.media_total_bytes,
    ) == (0, 0, 0, 0)
    aggregates = [
        sql
        for sql in statements
        if "from knowledge_documents" in sql or "from media_assets" in sql
    ]
    assert len(aggregates) == 4
    for sql in aggregates:
        assert "coalesce(" in sql
        assert "as bigint" in sql
        assert "payload" not in sql
        assert "knowledge_documents.content" not in sql.replace(
            "octet_length(knowledge_documents.content)", ""
        )
    assert not any("pg_advisory" in sql for sql in statements)
    assert not any("for update" in sql or "for share" in sql for sql in statements)


class _MediaSetPauseSession(_RecordingSession):
    """Pause right after PostgreSQL granted FOR SHARE on the source media set."""

    def __init__(
        self,
        *args: Any,
        stabilized: asyncio.Event,
        resume: asyncio.Event,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.stabilized = stabilized
        self.resume = resume
        self._did_pause = False

    async def execute(self, statement: Any, params: Any = None, **kwargs: Any) -> Any:
        result = await super().execute(statement, params, **kwargs)
        descriptions = getattr(statement, "column_descriptions", ())
        entity = descriptions[0].get("entity") if descriptions else None
        for_update = getattr(statement, "_for_update_arg", None)
        if (
            entity is MediaAsset
            and for_update is not None
            and for_update.read
            and not self._did_pause
        ):
            self._did_pause = True
            self.stabilized.set()
            await self.resume.wait()
        return result


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["patch_first_delete_last", "delete_first_patch_last"]
)
async def test_patch_and_delete_of_first_and_last_source_media_wait_for_the_clone(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(
        harness,
        source_name=f"Estável {scenario}",
        media_payloads=_MEDIA_PAYLOADS,
    )
    _stub_fast_unique_hash(monkeypatch)
    before = await _media_rows(harness, graph.source_id)
    first, last = before[0], before[-1]
    patched, deleted = (
        (first, last) if scenario == "patch_first_delete_last" else (last, first)
    )
    source_context = TenantContext(graph.source_id, api_key="tenant-scoped")
    stabilized = asyncio.Event()
    resume = asyncio.Event()
    clone = _new_session(
        harness,
        _MediaSetPauseSession,
        stabilized=stabilized,
        resume=resume,
    )
    patcher = harness.session()
    deleter = harness.session()
    clone_task: asyncio.Task[Any] | None = None
    patch_task: asyncio.Task[Any] | None = None
    delete_task: asyncio.Task[Any] | None = None
    try:
        clone_pid = await _backend_pid(clone)
        clone_task = asyncio.create_task(
            _clone(clone, graph, include_rag=True, include_media=True)
        )
        await asyncio.wait_for(stabilized.wait(), timeout=10)

        patch_pid = await _backend_pid(patcher)
        delete_pid = await _backend_pid(deleter)
        patch_task = asyncio.create_task(
            media_route.patch_media_asset(
                media_id=patched.id,
                patch=MediaAssetPatchRequest(display_name="PATCHED during clone"),
                request=_route_request(),
                tenant_id=None,
                tenant=source_context,
                session=patcher,
            )
        )
        delete_task = asyncio.create_task(
            media_route.delete_media_asset(
                media_id=deleted.id,
                request=_route_request(),
                tenant_id=None,
                tenant=source_context,
                session=deleter,
            )
        )
        await _await_blocked(harness, patch_pid, clone_pid)
        await _await_blocked(harness, delete_pid, clone_pid)
        assert patch_task.done() is False
        assert delete_task.done() is False

        resume.set()
        response, patch_response, delete_response = await _wait_for_tasks(
            clone_task,
            patch_task,
            delete_task,
        )
    finally:
        resume.set()
        try:
            await _cancel_and_gather(clone_task, patch_task, delete_task)
        finally:
            await clone.close()
            await patcher.close()
            await deleter.close()

    assert response.copied_media_assets == len(before)
    assert patch_response.display_name == "PATCHED during clone"
    assert delete_response.status_code == 204
    copies = {copy.sha256: copy for copy in await _media_rows(harness, response.id)}
    assert len(copies) == len(before)
    for source in before:
        copy = copies[source.sha256]
        assert copy.id != source.id
        for column in MEDIA_ASSET_CLONE_FIELDS:
            assert getattr(copy, column) == getattr(source, column)
    assert "PATCHED during clone" not in {copy.display_name for copy in copies.values()}
    after = await _media_rows(harness, graph.source_id)
    assert {row.id for row in after} == {row.id for row in before} - {deleted.id}
    assert next(row for row in after if row.id == patched.id).display_name == (
        "PATCHED during clone"
    )


class _HoldingGuard:
    """Delegate to the real guard, recording its key; optionally hold its locks.

    Holding happens on a successful exit, i.e. after the caller inserted and
    flushed its media rows and before it commits, so every lock it took (owner
    advisory, Tenant, FK KEY SHARE, FOR SHARE) is held while the peer runs.
    """

    def __init__(
        self,
        inner: Any,
        *,
        keys: list[int],
        hold: tuple[asyncio.Event, asyncio.Event] | None,
    ) -> None:
        self._inner = inner
        self._keys = keys
        self._hold = hold

    async def __aenter__(self) -> _HoldingGuard:
        await self._inner.__aenter__()
        self._keys.append(self._inner.advisory_key)
        return self

    async def validate_projection(self, additional_bytes: int) -> Any:
        return await self._inner.validate_projection(additional_bytes)

    async def __aexit__(self, *exc_info: Any) -> bool:
        if exc_info[0] is None and self._hold is not None:
            held, release = self._hold
            held.set()
            await release.wait()
        return bool(await self._inner.__aexit__(*exc_info))


def _holding_guard_factory(
    real_factory: Callable[..., Any],
    *,
    keys: list[int],
    hold: tuple[asyncio.Event, asyncio.Event] | None,
) -> Callable[..., _HoldingGuard]:
    def factory(*args: Any, **kwargs: Any) -> _HoldingGuard:
        return _HoldingGuard(real_factory(*args, **kwargs), keys=keys, hold=hold)

    return factory


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("cap_mode", ["tight", "roomy"])
@pytest.mark.parametrize("first", ["clone", "upload"])
async def test_upload_and_media_clone_serialize_on_the_owner_cap_without_deadlock(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    cap_mode: str,
) -> None:
    harness = postgres_harness
    source_payloads = (b"%PDF-1.7\n" + b"a" * 31, b"%PDF-1.7\n" + b"b" * 31)
    upload_payload = b"%PDF-1.7\n" + b"u" * 21
    source_bytes = sum(len(payload) for payload in source_payloads)
    # Tight: whichever operation runs second would push the owner over the cap.
    cap = 2 * source_bytes + len(upload_payload) - 1 if cap_mode == "tight" else 10_000
    monkeypatch.setattr(media_service, "MEDIA_STORAGE_CAP_BYTES", cap)
    graph = await _seed_graph(
        harness,
        source_name=f"Corrida {first} {cap_mode}",
        media_payloads=source_payloads,
    )
    _stub_fast_unique_hash(monkeypatch)
    held = asyncio.Event()
    release = asyncio.Event()
    clone_keys: list[int] = []
    upload_keys: list[int] = []
    monkeypatch.setattr(
        builder_routes,
        "media_storage_capacity_guard",
        _holding_guard_factory(
            builder_routes.media_storage_capacity_guard,
            keys=clone_keys,
            hold=(held, release) if first == "clone" else None,
        ),
    )
    monkeypatch.setattr(
        media_route,
        "media_storage_capacity_guard",
        _holding_guard_factory(
            media_route.media_storage_capacity_guard,
            keys=upload_keys,
            hold=(held, release) if first == "upload" else None,
        ),
    )

    async def run_clone(session: AsyncSession) -> Any:
        try:
            return await _clone(session, graph, include_media=True)
        except HTTPException as error:
            return error.detail

    async def run_upload(session: AsyncSession) -> str:
        try:
            await media_route.upload_media_asset(
                request=_UploadRequest(upload_payload, "upload-race"),  # type: ignore[arg-type]
                tenant_id=graph.source_id,
                tenant=graph.context,
                session=session,
            )
        except HTTPException as error:
            return str(getattr(error, "detail")["code"])
        return "created"

    clone_session = harness.session()
    upload_session = harness.session()
    first_task: asyncio.Task[Any] | None = None
    second_task: asyncio.Task[Any] | None = None
    try:
        clone_pid = await _backend_pid(clone_session)
        upload_pid = await _backend_pid(upload_session)
        if first == "clone":
            first_task = asyncio.create_task(run_clone(clone_session))
            await asyncio.wait_for(held.wait(), timeout=10)
            second_task = asyncio.create_task(run_upload(upload_session))
            await _await_blocked(harness, upload_pid, clone_pid)
        else:
            first_task = asyncio.create_task(run_upload(upload_session))
            await asyncio.wait_for(held.wait(), timeout=10)
            second_task = asyncio.create_task(run_clone(clone_session))
            await _await_blocked(harness, clone_pid, upload_pid)
        assert second_task.done() is False
        release.set()
        first_outcome, second_outcome = await _wait_for_tasks(first_task, second_task)
    finally:
        release.set()
        try:
            await _cancel_and_gather(first_task, second_task)
        finally:
            await clone_session.close()
            await upload_session.close()

    clone_outcome, upload_outcome = (
        (first_outcome, second_outcome)
        if first == "clone"
        else (second_outcome, first_outcome)
    )
    expected_key = media_storage_advisory_key(graph.builder_id)
    assert clone_keys == [expected_key]
    assert upload_keys == [expected_key]
    assert await _owner_media_bytes(harness, graph) <= cap

    upload_sha = media_payload_sha256(upload_payload)
    if cap_mode == "tight" and first == "clone":
        assert isinstance(clone_outcome, ClinicCloneResponse)
        assert clone_outcome.copied_media_assets == len(source_payloads)
        assert upload_outcome == "media_storage_cap_exceeded"
    elif cap_mode == "tight":
        assert upload_outcome == "created"
        assert clone_outcome == _CAP_ERROR
        await _assert_no_clone_residuals(harness, graph)
    else:
        assert upload_outcome == "created"
        assert isinstance(clone_outcome, ClinicCloneResponse)
        copied = {row.sha256 for row in await _media_rows(harness, clone_outcome.id)}
        # The clone copies exactly the set it stabilized: a later upload is not
        # part of it, an earlier committed upload is.
        expected = {media_payload_sha256(payload) for payload in source_payloads}
        if first == "upload":
            expected.add(upload_sha)
        assert copied == expected
        assert clone_outcome.copied_media_assets == len(expected)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["unlink", "reassign", "delete"])
async def test_media_branch_source_mutation_before_guard_lock_is_404(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(
        harness,
        source_name=f"Guarda {mutation}",
        media_payloads=_MEDIA_PAYLOADS,
    )
    _stub_fast_unique_hash(monkeypatch)
    blocker = harness.session()
    clone = _new_session(harness, _RecordingSession)
    clone_task: asyncio.Task[Any] | None = None
    try:
        await _lock_row(blocker, Tenant, graph.source_id)
        blocker_pid = await _backend_pid(blocker)
        clone_pid = await _backend_pid(clone)
        clone_task = asyncio.create_task(
            _clone(clone, graph, include_rag=True, include_media=True)
        )
        await _await_blocked(harness, clone_pid, blocker_pid)
        async with harness.session() as inspector:
            advisory_locks = await inspector.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND granted AND pid = :pid"
                ),
                {"pid": clone_pid},
            )
        # The clone already holds the owner advisory lock while it waits for
        # the source Tenant row: advisory strictly precedes the Tenant lock.
        assert advisory_locks == 1
        await _mutate_source(blocker, graph, mutation)

        with pytest.raises(HTTPException) as raised:
            await _wait_for_task(clone_task)
        assert raised.value.status_code == 404
        assert raised.value.detail == _SOURCE_ERROR
        assert MediaAsset not in clone.read_entities
        assert ClinicConfigTable not in clone.read_entities
        assert KnowledgeDocument not in clone.read_entities
    finally:
        await blocker.rollback()
        try:
            await _cancel_and_gather(clone_task)
        finally:
            await blocker.close()
            await clone.close()

    await _assert_no_clone_residuals(harness, graph)


class _FailingSession(AsyncSession):
    """Fail the Nth flush or the final commit with a driver-level error."""

    def __init__(
        self,
        *args: Any,
        fail_flush_at: int | None = None,
        fail_commit: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fail_flush_at = fail_flush_at
        self.fail_commit = fail_commit
        self.flushes = 0

    async def flush(self, objects: Any = None) -> None:
        self.flushes += 1
        if self.flushes == self.fail_flush_at:
            raise StatementError(
                "flush failed",
                "INSERT INTO media_assets",
                {},
                RuntimeError("driver failure during flush"),
            )
        await super().flush(objects)

    async def commit(self) -> None:
        if self.fail_commit:
            raise OperationalError(
                "COMMIT",
                {},
                RuntimeError("connection lost during commit"),
            )
        await super().commit()


_FAILURE_OUTCOMES: dict[str, tuple[int, dict[str, str]] | None] = {
    "config_build": (409, _INVALID_ERROR),
    "config_insert": None,
    "rag_insert": None,
    "first_media": None,
    "last_media": None,
    "media_flush": (500, _PERSISTENCE_ERROR),
    "commit": (500, _PERSISTENCE_ERROR),
    "cap": (409, _CAP_ERROR),
}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", list(_FAILURE_OUTCOMES))
async def test_failure_at_every_stage_reverts_tenant_config_rag_and_media(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(
        harness,
        source_name=f"Falha {boundary}",
        media_payloads=_MEDIA_PAYLOADS,
    )
    _stub_fast_unique_hash(monkeypatch)
    source_snapshot = await _source_row_snapshot(harness, graph)
    source_media = await _media_rows(harness, graph.source_id)
    source_bytes = sum(row.size_bytes for row in source_media)
    attempted: dict[type[Any], set[UUID]] = {
        Tenant: set(),
        ClinicConfigTable: set(),
        KnowledgeDocument: set(),
        MediaAsset: set(),
    }
    media_inserts = 0

    def capture_attempted_insert(_mapper: Any, _connection: Any, target: Any) -> None:
        attempted[type(target)].add(target.id)

    def fail_after_insert(_mapper: Any, _connection: Any, target: Any) -> None:
        nonlocal media_inserts
        if isinstance(target, MediaAsset):
            media_inserts += 1
            failing = 1 if boundary == "first_media" else len(_MEDIA_PAYLOADS)
            if media_inserts == failing:
                raise _InjectedBoundaryFailure(boundary)
            return
        raise _InjectedBoundaryFailure(boundary)

    listeners: list[tuple[type[Any], str, Callable[..., None]]] = [
        (model, "before_insert", capture_attempted_insert) for model in attempted
    ]
    boundary_models: dict[str, type[Any]] = {
        "config_insert": ClinicConfigTable,
        "rag_insert": KnowledgeDocument,
        "first_media": MediaAsset,
        "last_media": MediaAsset,
    }
    if boundary in boundary_models:
        listeners.append((boundary_models[boundary], "after_insert", fail_after_insert))
    if boundary == "config_build":

        def failing_config_build(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError("CONFIG_BUILD_SENTINEL")

        monkeypatch.setattr(
            builder_routes, "build_clinic_config_clone", failing_config_build
        )
    if boundary == "cap":
        monkeypatch.setattr(
            media_service, "MEDIA_STORAGE_CAP_BYTES", 2 * source_bytes - 1
        )
    session_options: dict[str, Any] = {}
    if boundary == "media_flush":
        # Flush 1 persists tenant/config/RAG; one flush per media copy follows.
        session_options["fail_flush_at"] = 1 + len(_MEDIA_PAYLOADS)
    if boundary == "commit":
        session_options["fail_commit"] = True

    for model, identifier, listener in listeners:
        event.listen(model, identifier, listener)
    try:
        async with _new_session(harness, _FailingSession, **session_options) as clone:
            expected = _FAILURE_OUTCOMES[boundary]
            if expected is None:
                with pytest.raises(_InjectedBoundaryFailure, match=boundary):
                    await _clone(clone, graph, include_rag=True, include_media=True)
            else:
                with pytest.raises(HTTPException) as raised:
                    await _clone(clone, graph, include_rag=True, include_media=True)
                assert (raised.value.status_code, raised.value.detail) == expected
                assert "SENTINEL" not in str(raised.value.detail)
    finally:
        for model, identifier, listener in listeners:
            event.remove(model, identifier, listener)

    attempts = tuple(len(ids) for ids in attempted.values())
    if boundary in {"config_insert", "rag_insert"}:
        # Tenant, configuration and RAG share one flush whose table order is
        # the unit of work's: the failing table was reached, media never was.
        assert attempts[0] == 1
        assert attempts[1 if boundary == "config_insert" else 2] >= 1
        assert attempts[3] == 0
    else:
        assert (
            attempts
            == {
                "config_build": (0, 0, 0, 0),
                "first_media": (1, 1, len(_CONTENTS), 1),
                "last_media": (1, 1, len(_CONTENTS), len(_MEDIA_PAYLOADS)),
                "media_flush": (1, 1, len(_CONTENTS), len(_MEDIA_PAYLOADS) - 1),
                "commit": (1, 1, len(_CONTENTS), len(_MEDIA_PAYLOADS)),
                "cap": (0, 0, 0, 0),
            }[boundary]
        )
    async with harness.session() as observer:
        for model, ids in attempted.items():
            if ids:
                remaining = await observer.scalar(
                    select(func.count(model.id)).where(model.id.in_(ids))
                )
                assert remaining == 0, model.__name__
    await _assert_no_clone_residuals(harness, graph)
    await _assert_boundary_rollback(
        harness,
        graph,
        source_snapshot,
        {
            Tenant: attempted[Tenant],
            ClinicConfigTable: attempted[ClinicConfigTable],
            KnowledgeDocument: attempted[KnowledgeDocument],
        },
    )
    assert _media_signature(await _media_rows(harness, graph.source_id)) == (
        _media_signature(source_media)
    )
    await _assert_owner_locks_released(harness, graph, [row.id for row in source_media])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_builder_clone_omits_sla_target_and_preserves_runtime_snapshots(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(harness, source_name="SLA Clone Source")
    _stub_fast_unique_hash(monkeypatch)

    async with harness.session() as seed:
        source_config = (
            await seed.execute(
                select(ClinicConfigTable).where(
                    ClinicConfigTable.tenant_id == graph.source_id
                )
            )
        ).scalar_one()
        source_config.handoff = {"meta_minutos_atendimento": 23}
        await seed.commit()

    async with harness.session() as clone_session:
        clone = await _clone(clone_session, graph)

    first_session_id = uuid4()
    second_session_id = uuid4()
    async with harness.session() as seed:
        destination_handoff = (
            await seed.execute(
                select(ClinicConfigTable.handoff).where(
                    ClinicConfigTable.tenant_id == clone.id
                )
            )
        ).scalar_one()
        assert "meta_minutos_atendimento" not in destination_handoff
        seed.add_all(
            [
                Session(
                    id=first_session_id,
                    tenant_id=clone.id,
                    user_id=f"clone-sla-{first_session_id}",
                    channel="test",
                    cognitive_state={},
                    session_metadata={},
                ),
                Session(
                    id=second_session_id,
                    tenant_id=clone.id,
                    user_id=f"clone-sla-{second_session_id}",
                    channel="test",
                    cognitive_state={},
                    session_metadata={},
                ),
            ]
        )
        await seed.commit()

    service = HandoffService(
        event_store=None,
        webhook_dispatcher=WebhookDispatcher(),
        session_factory=harness.session,
    )
    first = await service.initiate_handoff(
        HandoffRequest(
            tenant_id=clone.id,
            session_id=first_session_id,
            reason=HandoffReason.USER_REQUEST,
            recent_messages=[{"role": "user", "content": "primeiro"}],
        )
    )

    async def no_cache(_tenant_id: UUID) -> None:
        return None

    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache",
        no_cache,
    )
    clinic_admin = TenantContext(
        tenant_id=clone.id,
        user_id=uuid4(),
        role="clinic_admin",
    )
    async with harness.session() as config_session:
        patched = await patch_config(
            request=SimpleNamespace(query_params={}),
            body=ConfigPatchRequest(meta_minutos_atendimento=17),
            tenant=clinic_admin,
            session=config_session,
        )
    assert patched.meta_minutos_atendimento == 17

    second = await service.initiate_handoff(
        HandoffRequest(
            tenant_id=clone.id,
            session_id=second_session_id,
            reason=HandoffReason.USER_REQUEST,
            recent_messages=[{"role": "user", "content": "segundo"}],
        )
    )

    async with harness.session() as observer:
        first_record = await observer.get(HandoffRecord, first.id)
        second_record = await observer.get(HandoffRecord, second.id)

    assert first_record is not None
    assert second_record is not None
    assert first_record.sla_target_seconds == 600
    assert second_record.sla_target_seconds == 17 * 60
