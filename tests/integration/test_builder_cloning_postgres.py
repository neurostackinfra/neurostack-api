"""PostgreSQL concurrency and atomicity evidence for clinic cloning."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.api.middleware.auth import TenantContext
from src.api.routes import builder as builder_routes
from src.api.routes.config import patch_config
from src.api.schemas.config import ConfigPatchRequest
from src.config.settings import settings
from src.handoff.models import HandoffReason, HandoffRequest
from src.handoff.webhook import WebhookDispatcher
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.handoff import HandoffRecord
from src.models.knowledge import KnowledgeDocument
from src.models.session import Session
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.services.billing_usage_service import (
    PlanUsageLimits,
    get_builder_billing_usage,
)
from src.services.handoff_service import HandoffService

_ROOT = Path(__file__).resolve().parents[2]
_ADMIN_DATABASES = {"postgres"}
_CHILD_PATTERN = re.compile(r"^neurostack_clone_smoke_[0-9a-f]{12}$")
_SOURCE_ERROR = {"error": "clinic_clone_source_not_found"}
_CONTENTS = ("Olá, visão 👁️", "漢字 e café com açúcar")
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
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

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


async def _seed_graph(
    harness: _Harness,
    *,
    source_name: str = "Clínica Raiz",
    contents: tuple[str, ...] = _CONTENTS,
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
        await session.commit()
    return graph


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
    assert (tenant_count, config_count, document_count) == (0, 0, 0)


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
        first_task = asyncio.create_task(
            builder_routes.clone_clinic(str(graph.source_id), graph.context, first)
        )
        await asyncio.wait_for(source_locked.wait(), timeout=10)

        second_pid = await _backend_pid(second)
        second_task = asyncio.create_task(
            builder_routes.clone_clinic(str(graph.source_id), graph.context, second)
        )
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
        third_response = await builder_routes.clone_clinic(
            str(source_clone_id),
            graph.context,
            third,
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
        clone_task = asyncio.create_task(
            builder_routes.clone_clinic(
                str(builder_graph.source_id),
                builder_graph.context,
                clone,
            )
        )
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
        clone_task = asyncio.create_task(
            builder_routes.clone_clinic(
                str(source_graph.source_id),
                source_graph.context,
                clone,
            )
        )
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
        clone_task = asyncio.create_task(
            builder_routes.clone_clinic(str(graph.source_id), graph.context, clone)
        )
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
        clone_task = asyncio.create_task(
            builder_routes.clone_clinic(str(graph.source_id), graph.context, clone)
        )
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
                await builder_routes.clone_clinic(
                    str(graph.source_id),
                    graph.context,
                    clone,
                )
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
        response = await builder_routes.clone_clinic(
            str(graph.source_id),
            graph.context,
            clone,
        )

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
        clone = await builder_routes.clone_clinic(
            str(graph.source_id),
            graph.context,
            clone_session,
        )

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
