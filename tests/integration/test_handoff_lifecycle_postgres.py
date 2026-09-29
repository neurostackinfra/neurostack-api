"""Real PostgreSQL evidence for the canonical handoff lifecycle."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.core.exceptions import HandoffNotFoundError
from src.handoff.models import (
    HandoffReason,
    HandoffRequest,
    HandoffStatus,
    WebhookConfig,
    WebhookDelivery,
    WebhookStatus,
)
from src.handoff.summarizer import ConversationSummarizer
from src.handoff.webhook import WebhookDispatcher
from src.memory.event_store import EventData, EventStore, EventType
from src.models.clinic_config_table import ClinicConfigTable
from src.models.handoff import HandoffRecord
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.handoff import HandoffRecordRepository
from src.services.handoff_service import (
    HANDOFF_OPERATIONS,
    HandoffLifecycleError,
    HandoffService,
)
from tests.api.test_handoff_sla_migration import _run_alembic, _validated_admin_url

_CHILD_PATTERN = re.compile(r"^neurostack_handoff_lc_[0-9a-f]{12}$")
SessionMaker = async_sessionmaker[AsyncSession]


def _validated_child_name(name: str) -> str:
    if _CHILD_PATTERN.fullmatch(name) is None:
        raise ValueError("unsafe lifecycle smoke child database name")
    return name


@pytest.fixture(scope="module")
async def postgres_lifecycle_database() -> (
    AsyncIterator[tuple[AsyncEngine, SessionMaker]]
):
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url, settings.database_url)
    child_name = _validated_child_name(f"neurostack_handoff_lc_{uuid4().hex[:12]}")
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

        await _run_alembic(child_url, "upgrade", "head")
        child_engine = create_async_engine(child_url, poolclass=NullPool)
        factory = async_sessionmaker(child_engine, expire_on_commit=False)
        async with child_engine.connect() as connection:
            version = await connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
        assert version == "021"
        yield child_engine, factory
    finally:
        if child_engine is not None:
            await child_engine.dispose()
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


async def _seed_identity(
    factory: SessionMaker,
    *,
    tenant_id: UUID,
    session_id: UUID,
) -> None:
    async with factory() as database:
        async with database.begin():
            database.add(
                Tenant(
                    id=tenant_id,
                    name=f"Lifecycle {tenant_id}",
                    slug=f"lifecycle-{tenant_id}",
                    api_key_hash=f"lifecycle-{tenant_id}",
                    config={},
                )
            )
            database.add(
                Session(
                    id=session_id,
                    tenant_id=tenant_id,
                    channel="test",
                    cognitive_state={},
                    session_metadata={},
                )
            )
            database.add(
                ClinicConfigTable(
                    tenant_id=tenant_id,
                    handoff={"meta_minutos_atendimento": 10},
                )
            )


async def _seed_pending_handoff(
    factory: SessionMaker,
    *,
    tenant_id: UUID,
    session_id: UUID,
) -> UUID:
    handoff_id = uuid4()
    async with factory() as database:
        async with database.begin():
            database.add(
                HandoffRecord(
                    id=handoff_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    reason=HandoffReason.USER_REQUEST.value,
                    summary="PostgreSQL race",
                    context={"context_version": 1, "key_info": {}},
                    urgency="normal",
                    status=HandoffStatus.PENDING.value,
                    sla_target_seconds=600,
                )
            )
    return handoff_id


def _event_store_with_messages(
    tenant_id: UUID,
    session_id: UUID,
) -> MagicMock:
    store = MagicMock(spec=EventStore)
    store.append = AsyncMock()
    store.get_recent_messages = AsyncMock(
        return_value=[
            EventData(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=(
                    EventType.MESSAGE_IN.value
                    if index % 2 == 0
                    else EventType.MESSAGE_OUT.value
                ),
                event_data={
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"message-{index}",
                },
                event_metadata={},
            )
            for index in range(6)
        ]
    )
    return store


def _dispatcher_with_delivery() -> WebhookDispatcher:
    dispatcher = WebhookDispatcher()

    async def _delivery(tenant_id: UUID, payload: Any) -> WebhookDelivery:
        return WebhookDelivery(
            handoff_id=payload.handoff_id,
            tenant_id=tenant_id,
            url="https://example.test/handoff",
            status=WebhookStatus.SENT,
            response_status=200,
            response_body="ok",
        )

    dispatcher.send = AsyncMock(side_effect=_delivery)  # type: ignore[method-assign]
    return dispatcher


def _service(
    factory: SessionMaker,
    *,
    event_store: EventStore | None = None,
    dispatcher: WebhookDispatcher | None = None,
) -> HandoffService:
    return HandoffService(
        event_store=event_store,
        summarizer=ConversationSummarizer(),
        webhook_dispatcher=dispatcher or WebhookDispatcher(),
        session_factory=factory,
    )


async def test_restart_round_trip_uses_postgres_as_the_only_state(
    postgres_lifecycle_database: tuple[AsyncEngine, SessionMaker],
) -> None:
    _, factory = postgres_lifecycle_database
    tenant_id = uuid4()
    session_id = uuid4()
    other_tenant_id = uuid4()
    other_session_id = uuid4()
    await _seed_identity(factory, tenant_id=tenant_id, session_id=session_id)
    await _seed_identity(
        factory,
        tenant_id=other_tenant_id,
        session_id=other_session_id,
    )
    event_store = _event_store_with_messages(tenant_id, session_id)

    service_a = _service(factory, event_store=event_store)
    created = await service_a.initiate_handoff(
        HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
            key_info={"patient_reference": "expected"},
            recent_messages=[{"role": "user", "content": "do-not-copy"}],
        )
    )

    service_b = _service(factory, event_store=event_store)
    detail_b = await service_b.get_handoff(created.id, tenant_id=tenant_id)
    accepted = await service_b.accept_handoff(
        created.id,
        agent_id="agent-postgres",
        tenant_id=tenant_id,
    )

    service_c = _service(factory, event_store=event_store)
    confirmed = await service_c.confirm_handoff(created.id, tenant_id=tenant_id)

    assert detail_b.key_info["patient_reference"] == "expected"
    assert [message["content"] for message in detail_b.recent_messages] == [
        "message-1",
        "message-2",
        "message-3",
        "message-4",
        "message-5",
    ]
    assert accepted.status == HandoffStatus.ACCEPTED
    assert confirmed.status == HandoffStatus.IN_PROGRESS
    assert confirmed.data_hora_saida is not None
    assert confirmed.updated_at is not None
    event_store.get_recent_messages.assert_awaited_with(
        tenant_id,
        session_id,
        limit=5,
    )

    async with factory() as verification_session:
        persisted = await HandoffRecordRepository(verification_session).get_for_tenant(
            tenant_id,
            created.id,
        )
        assert persisted is not None
        assert persisted.sla_target_seconds == 600
        assert persisted.lifecycle_version == 2
        assert "recent_messages" not in persisted.context
        assert persisted.data_hora_saida is not None
        assert persisted.updated_at is not None

    with pytest.raises(HandoffNotFoundError):
        await service_c.get_handoff(created.id, tenant_id=other_tenant_id)


async def _run_bounded_race(
    monkeypatch: pytest.MonkeyPatch,
    operations: tuple[Callable[[], Awaitable[Any]], Callable[[], Awaitable[Any]]],
) -> list[Any]:
    original = HandoffRecordRepository.compare_and_set
    all_read = asyncio.Event()
    arrivals = 0

    async def _gated_compare_and_set(
        repository: HandoffRecordRepository,
        **kwargs: Any,
    ) -> Any:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            all_read.set()
        await asyncio.wait_for(all_read.wait(), timeout=3)
        return await original(repository, **kwargs)

    monkeypatch.setattr(
        HandoffRecordRepository,
        "compare_and_set",
        _gated_compare_and_set,
    )
    return await asyncio.wait_for(
        asyncio.gather(
            operations[0](),
            operations[1](),
            return_exceptions=True,
        ),
        timeout=8,
    )


def _assert_one_winner_one_conflict(results: list[Any]) -> None:
    successes = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [
        result
        for result in results
        if isinstance(result, HandoffLifecycleError)
        and result.code == "handoff_conflict"
    ]
    assert len(successes) == 1, results
    assert len(conflicts) == 1, results


async def test_two_confirmations_have_one_winner_and_one_effect_set(
    postgres_lifecycle_database: tuple[AsyncEngine, SessionMaker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = postgres_lifecycle_database
    tenant_id = uuid4()
    session_id = uuid4()
    await _seed_identity(factory, tenant_id=tenant_id, session_id=session_id)
    handoff_id = await _seed_pending_handoff(
        factory,
        tenant_id=tenant_id,
        session_id=session_id,
    )
    event_store = _event_store_with_messages(tenant_id, session_id)
    dispatcher = _dispatcher_with_delivery()
    dispatcher.set_config(tenant_id, WebhookConfig(url="https://example.test/handoff"))
    service = _service(factory, event_store=event_store, dispatcher=dispatcher)
    metric = HANDOFF_OPERATIONS.labels(operation="update", status="in_progress")
    metric_before = metric._value.get()

    results = await _run_bounded_race(
        monkeypatch,
        (
            lambda: service.confirm_handoff(handoff_id, tenant_id=tenant_id),
            lambda: service.confirm_handoff(handoff_id, tenant_id=tenant_id),
        ),
    )
    _assert_one_winner_one_conflict(results)

    async with factory() as third_session:
        record = await HandoffRecordRepository(third_session).get_for_tenant(
            tenant_id,
            handoff_id,
        )
        assert record is not None
        assert record.status == HandoffStatus.IN_PROGRESS.value
        assert record.lifecycle_version == 1
        assert record.data_hora_saida is not None
        assert record.updated_at == record.data_hora_saida
        assert record.resolved_at is None
        assert record.webhook_sent is True
        assert record.context["webhook_status"] == WebhookStatus.SENT.value
    assert dispatcher.send.await_count == 1  # type: ignore[attr-defined]
    assert event_store.append.await_count == 0
    event_store.get_recent_messages.assert_awaited_once_with(
        tenant_id,
        session_id,
        limit=5,
    )
    assert metric._value.get() - metric_before == 1


@pytest.mark.parametrize("terminal_operation", ["cancel", "timeout"])
async def test_confirmation_racing_terminal_has_one_coherent_winner(
    postgres_lifecycle_database: tuple[AsyncEngine, SessionMaker],
    monkeypatch: pytest.MonkeyPatch,
    terminal_operation: str,
) -> None:
    _, factory = postgres_lifecycle_database
    tenant_id = uuid4()
    session_id = uuid4()
    await _seed_identity(factory, tenant_id=tenant_id, session_id=session_id)
    handoff_id = await _seed_pending_handoff(
        factory,
        tenant_id=tenant_id,
        session_id=session_id,
    )
    event_store = _event_store_with_messages(tenant_id, session_id)
    dispatcher = _dispatcher_with_delivery()
    dispatcher.set_config(tenant_id, WebhookConfig(url="https://example.test/handoff"))
    service = _service(factory, event_store=event_store, dispatcher=dispatcher)
    metric_statuses = (
        "in_progress",
        "cancelled" if terminal_operation == "cancel" else "timeout",
    )
    metrics = [
        HANDOFF_OPERATIONS.labels(operation="update", status=status)
        for status in metric_statuses
    ]
    metrics_before = sum(metric._value.get() for metric in metrics)
    terminal = (
        (lambda: service.cancel_handoff(handoff_id, tenant_id=tenant_id))
        if terminal_operation == "cancel"
        else (lambda: service.timeout_handoff(handoff_id, tenant_id=tenant_id))
    )

    results = await _run_bounded_race(
        monkeypatch,
        (
            lambda: service.confirm_handoff(handoff_id, tenant_id=tenant_id),
            terminal,
        ),
    )
    _assert_one_winner_one_conflict(results)

    async with factory() as third_session:
        record = await HandoffRecordRepository(third_session).get_for_tenant(
            tenant_id,
            handoff_id,
        )
        assert record is not None
        assert record.lifecycle_version == 1
        assert record.updated_at is not None
        if record.status == HandoffStatus.IN_PROGRESS.value:
            assert record.data_hora_saida is not None
            assert record.updated_at == record.data_hora_saida
            assert record.resolved_at is None
            assert event_store.append.await_count == 0
        else:
            assert record.status == terminal_operation + (
                "led" if terminal_operation == "cancel" else ""
            )
            assert record.data_hora_saida is None
            assert record.resolved_at == record.updated_at
            expected_events = 1 if terminal_operation == "timeout" else 0
            assert event_store.append.await_count == expected_events
        assert record.webhook_sent is True
    assert dispatcher.send.await_count == 1  # type: ignore[attr-defined]
    event_store.get_recent_messages.assert_awaited_once_with(
        tenant_id,
        session_id,
        limit=5,
    )
    assert sum(metric._value.get() for metric in metrics) - metrics_before == 1
