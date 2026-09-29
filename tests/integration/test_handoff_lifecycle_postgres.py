"""Real PostgreSQL evidence for handoff lifecycle CAS and restart safety."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.handoff.models import (
    HandoffReason,
    HandoffRequest,
    HandoffStatus,
    WebhookConfig,
    WebhookDelivery,
    WebhookStatus,
)
from src.handoff.webhook import WebhookDispatcher
from src.memory.event_store import EventData
from src.models.clinic_config_table import ClinicConfigTable
from src.models.handoff import HandoffRecord
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.handoff import HandoffRecordRepository
from src.services.handoff_service import (
    HANDOFF_OPERATIONS,
    HandoffConflictError,
    HandoffService,
)

_ROOT = Path(__file__).resolve().parents[2]
_CHILD_PATTERN = re.compile(r"^neurostack_handoff_smoke_[0-9a-f]{12}$")
_TASK_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    def session(self) -> AsyncSession:
        return self.sessions()


@dataclass(frozen=True)
class _Graph:
    tenant_id: UUID
    session_id: UUID


class _EventStoreSpy:
    def __init__(self) -> None:
        self.appended: list[EventData] = []
        self.recent_calls: list[tuple[UUID, UUID, int]] = []

    async def append(self, event: EventData) -> EventData:
        self.appended.append(event)
        return event

    async def get_recent_messages(
        self,
        tenant_id: UUID,
        session_id: UUID,
        limit: int,
    ) -> list[EventData]:
        self.recent_calls.append((tenant_id, session_id, limit))
        return []


def _same_database(left: URL, right: URL) -> bool:
    return (left.host, left.port, left.database) == (
        right.host,
        right.port,
        right.database,
    )


def _validated_admin_url(raw_url: str) -> URL:
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
    if url.database != "postgres":
        raise ValueError("smoke URL must use the postgres administrative database")
    if _same_database(url, make_url(settings.database_url)):
        raise ValueError("smoke URL must not be the application database")
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
    assert process.returncode == 0, output.decode()


@pytest.fixture(scope="module")
async def postgres_harness() -> AsyncIterator[_Harness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url)
    child_name = f"neurostack_handoff_smoke_{uuid4().hex[:12]}"
    if _CHILD_PATTERN.fullmatch(child_name) is None:
        raise ValueError("unsafe smoke child database name")
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
                (
                    await connection.execute(
                        text(
                            "SELECT rolcreatedb OR rolsuper FROM pg_roles "
                            "WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
            )
            if not can_create:
                raise PermissionError("smoke database user must have CREATEDB")
            quoted_name = connection.dialect.identifier_preparer.quote(child_name)
            await connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
            created = True

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
                quoted_name = connection.dialect.identifier_preparer.quote(child_name)
                await connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        await admin_engine.dispose()


async def _seed_graph(
    harness: _Harness,
    *,
    configured_minutes: object = 7,
) -> _Graph:
    graph = _Graph(tenant_id=uuid4(), session_id=uuid4())
    async with harness.session() as session:
        session.add(
            Tenant(
                id=graph.tenant_id,
                name="Handoff smoke tenant",
                slug=f"handoff-smoke-{graph.tenant_id.hex}",
                api_key_hash=f"handoff-smoke-hash-{graph.tenant_id.hex}",
                config={},
            )
        )
        session.add(
            Session(
                id=graph.session_id,
                tenant_id=graph.tenant_id,
                channel="web",
                cognitive_state={},
                session_metadata={},
            )
        )
        session.add(
            ClinicConfigTable(
                tenant_id=graph.tenant_id,
                version=1,
                persona={},
                scheduling={},
                pricing={},
                post_care={},
                handoff={"meta_minutos_atendimento": configured_minutes},
                identity={},
                operations={},
                business_rules={},
                lgpd={},
                professionals=[],
                services=[],
                api_credentials={},
            )
        )
        await session.commit()
    return graph


async def _seed_pending(harness: _Harness, graph: _Graph) -> UUID:
    handoff_id = uuid4()
    async with harness.session() as session:
        session.add(
            HandoffRecord(
                id=handoff_id,
                tenant_id=graph.tenant_id,
                session_id=graph.session_id,
                reason="user_request",
                summary="summary",
                context={"key_info": {}},
                urgency="normal",
                status="pending",
                webhook_sent=False,
                sla_target_seconds=420,
                lifecycle_version=0,
            )
        )
        await session.commit()
    return handoff_id


async def _backend_pid(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")))


async def _await_service_waiters(
    harness: _Harness,
    *,
    blocker_pid: int,
    expected: int,
) -> tuple[int, ...]:
    async def inspect() -> tuple[int, ...]:
        async with harness.session() as observer:
            for _ in range(1_000):
                rows = (
                    await observer.execute(
                        text(
                            "SELECT pid, pg_blocking_pids(pid) "
                            "FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND pid <> pg_backend_pid() "
                            "AND state = 'active' "
                            "AND query LIKE 'UPDATE handoff_records%'"
                        )
                    )
                ).all()
                blocked_rows = [
                    (int(pid), tuple(blockers or ()))
                    for pid, blockers in rows
                    if blockers
                ]
                waiters = tuple(pid for pid, _ in blocked_rows)
                if len(waiters) >= expected and any(
                    blocker_pid in blockers for _, blockers in blocked_rows
                ):
                    return waiters
                await asyncio.sleep(0)
        raise AssertionError("CAS never waited on the expected PostgreSQL row lock")

    return await asyncio.wait_for(inspect(), timeout=10)


def _configured_dispatcher(
    tenant_id: UUID,
) -> tuple[WebhookDispatcher, list[Any]]:
    dispatcher = WebhookDispatcher()
    sent_payloads: list[Any] = []
    dispatcher.set_config(
        tenant_id,
        WebhookConfig(url="https://example.test/handoff", enabled=True),
    )

    async def send(
        actual_tenant_id: UUID,
        payload: Any,
    ) -> WebhookDelivery:
        sent_payloads.append(payload)
        return WebhookDelivery(
            handoff_id=payload.handoff_id,
            tenant_id=actual_tenant_id,
            url="https://example.test/handoff",
            status=WebhookStatus.SENT,
            attempt=1,
            response_status=204,
        )

    dispatcher.send = send  # type: ignore[method-assign]
    return dispatcher, sent_payloads


@pytest.mark.integration
@pytest.mark.asyncio
async def test_instances_a_b_c_share_canonical_state_and_sla_snapshot(
    postgres_harness: _Harness,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(harness)

    service_a = HandoffService(
        event_store=None,
        webhook_dispatcher=WebhookDispatcher(),
        session_factory=harness.session,
    )
    service_b = HandoffService(
        event_store=None,
        webhook_dispatcher=WebhookDispatcher(),
        session_factory=harness.session,
    )
    service_c = HandoffService(
        event_store=None,
        webhook_dispatcher=WebhookDispatcher(),
        session_factory=harness.session,
    )

    created = await service_a.initiate_handoff(
        HandoffRequest(
            tenant_id=graph.tenant_id,
            session_id=graph.session_id,
            reason=HandoffReason.USER_REQUEST,
            recent_messages=[{"role": "user", "content": "not persisted in JSONB"}],
        )
    )
    confirmed = await service_b.confirm_handoff(
        created.id,
        tenant_id=graph.tenant_id,
    )
    reread = await service_c.get_handoff(created.id, graph.tenant_id)

    assert confirmed.status is HandoffStatus.IN_PROGRESS
    assert reread.status is HandoffStatus.IN_PROGRESS
    assert reread.data_hora_saida == confirmed.data_hora_saida
    assert reread.sla_target_seconds == 420
    assert reread.recent_messages == []
    assert "lifecycle_version" not in reread.model_dump()
    async with harness.session() as observer:
        row = await observer.get(HandoffRecord, created.id)
        assert row is not None
        assert row.lifecycle_version == 1
        assert row.sla_target_seconds == 420
        assert "recent_messages" not in row.context


async def _run_confirmation_first_service_race(
    harness: _Harness,
    graph: _Graph,
    handoff_id: UUID,
    competing_mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, BaseException, _EventStoreSpy, list[Any], list[UUID]]:
    event_store = _EventStoreSpy()
    dispatcher, sent_payloads = _configured_dispatcher(graph.tenant_id)
    bookkeeping_calls: list[UUID] = []
    original_bookkeeping = HandoffRecordRepository.record_webhook_delivery

    async def record_webhook_delivery(
        repository: HandoffRecordRepository,
        **kwargs: Any,
    ) -> bool:
        bookkeeping_calls.append(kwargs["handoff_id"])
        return await original_bookkeeping(repository, **kwargs)

    monkeypatch.setattr(
        HandoffRecordRepository,
        "record_webhook_delivery",
        record_webhook_delivery,
    )
    first_service = HandoffService(
        event_store=event_store,
        webhook_dispatcher=dispatcher,
        session_factory=harness.session,
    )
    second_service = HandoffService(
        event_store=event_store,
        webhook_dispatcher=dispatcher,
        session_factory=harness.session,
    )
    blocker = harness.session()
    first_task: asyncio.Task[Any] | None = None
    second_task: asyncio.Task[Any] | None = None
    success_before = HANDOFF_OPERATIONS.labels(
        operation="update",
        status=HandoffStatus.IN_PROGRESS.value,
    )._value.get()
    conflict_before = HANDOFF_OPERATIONS.labels(
        operation="update",
        status="conflict",
    )._value.get()
    try:
        await blocker.execute(
            text("SELECT id FROM handoff_records WHERE id = :id FOR UPDATE"),
            {"id": handoff_id},
        )
        blocker_pid = await _backend_pid(blocker)
        first_task = asyncio.create_task(
            first_service.confirm_handoff(
                handoff_id,
                agent_id="winner",
                tenant_id=graph.tenant_id,
            )
        )
        await _await_service_waiters(
            harness,
            blocker_pid=blocker_pid,
            expected=1,
        )
        if competing_mutation == "confirm":
            competitor = second_service.confirm_handoff(
                handoff_id,
                agent_id="loser",
                tenant_id=graph.tenant_id,
            )
        elif competing_mutation == "cancel":
            competitor = second_service.cancel_handoff(
                handoff_id,
                notes="loser",
                tenant_id=graph.tenant_id,
            )
        else:
            competitor = second_service.timeout_handoff(
                handoff_id,
                tenant_id=graph.tenant_id,
            )
        second_task = asyncio.create_task(competitor)
        await _await_service_waiters(
            harness,
            blocker_pid=blocker_pid,
            expected=2,
        )
        await blocker.commit()
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first_task, second_task, return_exceptions=True),
            timeout=_TASK_TIMEOUT_SECONDS,
        )
    finally:
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
        pending = [
            task
            for task in (first_task, second_task)
            if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await blocker.rollback()
        await blocker.close()

    assert not isinstance(first_result, BaseException)
    assert isinstance(second_result, HandoffConflictError)
    assert (
        HANDOFF_OPERATIONS.labels(
            operation="update",
            status=HandoffStatus.IN_PROGRESS.value,
        )._value.get()
        - success_before
        == 1
    )
    assert (
        HANDOFF_OPERATIONS.labels(
            operation="update",
            status="conflict",
        )._value.get()
        - conflict_before
        == 1
    )
    assert len(sent_payloads) == 1
    assert bookkeeping_calls == [handoff_id]
    # Confirmation has no dedicated EventType; no synthetic status event is made.
    assert event_store.appended == []
    # Only the winning transition projects recent messages; the loser never does.
    assert event_store.recent_calls == [
        (graph.tenant_id, graph.session_id, 5),
    ]
    return (
        first_result,
        second_result,
        event_store,
        sent_payloads,
        bookkeeping_calls,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_double_confirmation_has_one_service_winner_after_lock_wait(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(harness)
    handoff_id = await _seed_pending(harness, graph)
    winner, _, _, _, _ = await _run_confirmation_first_service_race(
        harness,
        graph,
        handoff_id,
        "confirm",
        monkeypatch,
    )
    async with harness.session() as observer:
        row = await observer.get(HandoffRecord, handoff_id)
        assert row is not None
        assert row.status == "in_progress"
        assert row.assigned_to == "winner"
        assert row.resolved_at is None
        assert row.lifecycle_version == 1
        assert row.data_hora_saida == winner.data_hora_saida
        assert row.webhook_sent is True
        assert row.webhook_response == {
            "status": "sent",
            "attempt": 1,
            "response_status": 204,
        }


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("competing_mutation", ["cancel", "timeout"])
async def test_confirmation_beats_terminal_service_competitor_without_reapply(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    competing_mutation: str,
) -> None:
    harness = postgres_harness
    graph = await _seed_graph(harness)
    handoff_id = await _seed_pending(harness, graph)
    winner, _, _, _, _ = await _run_confirmation_first_service_race(
        harness,
        graph,
        handoff_id,
        competing_mutation,
        monkeypatch,
    )
    async with harness.session() as observer:
        row = await observer.get(HandoffRecord, handoff_id)
        assert row is not None
        assert row.status == "in_progress"
        assert row.assigned_to == "winner"
        assert row.resolved_at is None
        assert row.resolution_notes is None
        assert row.lifecycle_version == 1
        assert row.data_hora_saida == winner.data_hora_saida
        assert row.webhook_sent is True
