"""PostgreSQL regression for #145: a phase 1 handoff must not wait on its own lock.

``EventService.run_pipeline`` holds ``SELECT ... FOR UPDATE`` on the
``sessions`` row until its caller commits. A synchronous handoff in that
window inserts ``handoff_records`` and the ``handoff.request`` event, and both
FKs to ``sessions`` take ``FOR KEY SHARE``: written from a second connection
they wait on the request's own lock with no timeout.

These tests drive the real composition through the WhatsApp adapter helper
``_run_pipeline_transactionally``: the real ``run_pipeline`` lock, the real
``CognitivePipeline.process`` (only the SRM decision is fixed to
``handoff_required=True``), the real ``HandoffService`` and ``EventStore`` on a
migrated PostgreSQL. Every turn runs under ``asyncio.timeout``, so the
self-wait fails fast instead of hanging. The webhook dispatcher records what a
fresh connection sees at the moment it would send.
"""

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
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from neurostack_core.srm import SRMDecision
from src.api.routes.webhook import _run_pipeline_transactionally
from src.api.schemas.events import CanonicalEventRequest, EventPayload, EventType
from src.config.settings import settings
from src.core.pipeline import CognitivePipeline, PipelineInput, PipelineResult
from src.handoff.models import (
    HandoffReason,
    HandoffRequest,
    WebhookConfig,
    WebhookDelivery,
    WebhookStatus,
)
from src.handoff.summarizer import ConversationSummarizer
from src.handoff.webhook import WebhookDispatcher
from src.interaction_context import (
    InteractionOrigin,
    ResolvedInteractionContext,
    build_interaction_context,
)
from src.memory.event_store import EventStore
from src.models.session import Session
from src.models.tenant import Tenant
from src.services.event_service import EventService
from src.services.handoff_service import (
    HandoffService,
    dispatch_committed_handoff_webhooks,
)

_ROOT = Path(__file__).resolve().parents[2]
_CHILD_PATTERN = re.compile(r"^neurostack_handoff_phase1_[0-9a-f]{12}$")
_TURN_TIMEOUT_SECONDS = 10
_WEBHOOK_URL = "https://clinic.test/handoff"
SessionMaker = async_sessionmaker[AsyncSession]


# ============================================
# Disposable migrated database
# ============================================


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


async def _run_alembic(database_url: URL) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(
        hide_password=False
    ).replace("%", "%%")
    executable = Path(sys.executable).with_name("alembic")
    if not executable.is_file():
        raise RuntimeError("Alembic executable is unavailable beside the test Python")
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "upgrade",
        "head",
        cwd=_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


@pytest.fixture(scope="module")
async def phase1_database(alembic_head: str) -> AsyncIterator[SessionMaker]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")

    admin_url = _validated_admin_url(raw_admin_url)
    child_name = f"neurostack_handoff_phase1_{uuid4().hex[:12]}"
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
                        sa.text(
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

        await _run_alembic(child_url)
        child_engine = create_async_engine(child_url, poolclass=NullPool)
        async with child_engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                )
                == alembic_head
            )
        yield async_sessionmaker(
            child_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    finally:
        if child_engine is not None:
            await child_engine.dispose()
        if created:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    sa.text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database AND pid <> pg_backend_pid()"
                    ),
                    {"database": child_name},
                )
                quoted_name = connection.dialect.identifier_preparer.quote(child_name)
                await connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        await admin_engine.dispose()


# ============================================
# Observation from a fresh connection
# ============================================


@dataclass(frozen=True)
class _Snapshot:
    """What a connection other than the phase 1 one can see."""

    handoff_ids: tuple[UUID, ...]
    request_event_ids: tuple[UUID, ...]


_HANDOFF_IDS = sa.text(
    "SELECT id FROM handoff_records WHERE session_id = :session_id ORDER BY id"
)
_REQUEST_EVENT_IDS = sa.text(
    "SELECT id FROM events WHERE session_id = :session_id "
    "AND event_type = 'handoff.request' ORDER BY id"
)


async def _snapshot(db: AsyncSession, session_id: UUID) -> _Snapshot:
    parameters = {"session_id": session_id}
    return _Snapshot(
        handoff_ids=tuple((await db.execute(_HANDOFF_IDS, parameters)).scalars()),
        request_event_ids=tuple(
            (await db.execute(_REQUEST_EVENT_IDS, parameters)).scalars()
        ),
    )


async def _observe(factory: SessionMaker, session_id: UUID) -> _Snapshot:
    """Plain MVCC reads: they never wait on the phase 1 row lock."""
    async with factory() as observer:
        return await _snapshot(observer, session_id)


class _RecordingDispatcher(WebhookDispatcher):
    """A configured dispatcher that records, never sends, each webhook."""

    def __init__(self, factory: SessionMaker, session_id: UUID) -> None:
        super().__init__(default_timeout=5, max_retries=1, retry_delay=0)
        self._factory = factory
        self._session_id = session_id
        self.sent: list[tuple[Any, _Snapshot]] = []

    async def send(self, tenant_id: UUID, payload: Any) -> WebhookDelivery:
        self.sent.append((payload, await _observe(self._factory, self._session_id)))
        return WebhookDelivery(
            handoff_id=payload.handoff_id,
            tenant_id=tenant_id,
            url=_WEBHOOK_URL,
            status=WebhookStatus.SENT,
            attempt=1,
            response_status=204,
        )


# ============================================
# Real composition with a fixed handoff decision
# ============================================


class _HandoffTurnPipeline(CognitivePipeline):
    """The real pipeline, with only the SRM decision fixed to a handoff."""

    async def _run_srm(self, input_data: PipelineInput) -> SRMDecision:
        return SRMDecision.model_validate(
            {
                "intent": "atendimento_humano",
                "intent_category": "information",
                "funnel_stage": "em_qualificacao",
                "action": "handoff_administrativo",
                "confidence": 0.99,
                "handoff_required": True,
                "handoff_reason": "user_request",
            }
        )


class _ObservedEventService(EventService):
    """The real phase 1 with observation points; phase 2 is not under test."""

    def __init__(
        self,
        pipeline: CognitivePipeline,
        factory: SessionMaker,
        session_id: UUID,
        dispatcher: _RecordingDispatcher,
        *,
        fail_state_flush: bool = False,
    ) -> None:
        super().__init__(pipeline=pipeline)
        self._factory = factory
        self._session_id = session_id
        self._dispatcher = dispatcher
        self._fail_state_flush = fail_state_flush
        self.inside_phase1: _Snapshot | None = None
        self.outside_phase1: _Snapshot | None = None
        self.sent_during_phase1: int | None = None
        self.at_finish: _Snapshot | None = None
        self.sent_before_finish: int | None = None
        self.result: PipelineResult | None = None

    async def _load_clinic_config(self, tenant_id: UUID) -> dict[str, Any]:
        return {}

    async def _persist_session_state(self, *args: Any, **kwargs: Any) -> None:
        # The handoff is already written; observe the open transaction from
        # inside (its own session) and outside (a fresh connection).
        phase1 = args[3]
        assert isinstance(phase1, AsyncSession)
        self.inside_phase1 = await _snapshot(phase1, self._session_id)
        self.outside_phase1 = await _observe(self._factory, self._session_id)
        self.sent_during_phase1 = len(self._dispatcher.sent)
        if self._fail_state_flush:
            raise RuntimeError("phase 1 state flush failed")
        await super()._persist_session_state(*args, **kwargs)

    async def finish_response(
        self,
        request: CanonicalEventRequest,
        result: PipelineResult,
        clinic_config: Any,
        message_history: Any,
        *,
        context: ResolvedInteractionContext | None = None,
    ) -> Any:
        self.at_finish = await _observe(self._factory, self._session_id)
        self.sent_before_finish = len(self._dispatcher.sent)
        self.result = result
        return result


@dataclass(frozen=True)
class _Turn:
    tenant_id: UUID
    session_id: UUID
    request: CanonicalEventRequest
    context: ResolvedInteractionContext
    dispatcher: _RecordingDispatcher
    service: _ObservedEventService


async def _seed(factory: SessionMaker) -> tuple[UUID, UUID]:
    tenant_id, session_id = uuid4(), uuid4()
    async with factory() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Phase 1 handoff tenant",
                slug=f"phase1-handoff-{tenant_id.hex}",
                api_key_hash=f"phase1-handoff-{tenant_id.hex}",
                config={},
            )
        )
        await db.flush()
        db.add(
            Session(
                id=session_id,
                tenant_id=tenant_id,
                user_id="5511999990145",
                channel="whatsapp",
                cognitive_state={},
                session_metadata={},
            )
        )
        await db.commit()
    return tenant_id, session_id


async def _turn(factory: SessionMaker, *, fail_state_flush: bool = False) -> _Turn:
    tenant_id, session_id = await _seed(factory)
    dispatcher = _RecordingDispatcher(factory, session_id)
    dispatcher.set_config(tenant_id, WebhookConfig(url=_WEBHOOK_URL, enabled=True))
    handoff = HandoffService(
        event_store=EventStore(session_factory=factory),
        summarizer=ConversationSummarizer(),
        webhook_dispatcher=dispatcher,
        session_factory=factory,
    )
    service = _ObservedEventService(
        _HandoffTurnPipeline(handoff_service=handoff),
        factory,
        session_id,
        dispatcher,
        fail_state_flush=fail_state_flush,
    )
    request = CanonicalEventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(
            content="Quero falar com uma pessoa da equipe",
            channel="whatsapp",
            user_id="5511999990145",
        ),
    )
    context = build_interaction_context(
        effective_tenant_id=tenant_id,
        session_id=session_id,
        origin=InteractionOrigin.PRODUCTION,
        event_type=EventType.MESSAGE_IN.value,
    )
    return _Turn(tenant_id, session_id, request, context, dispatcher, service)


# ============================================
# Regression tests
# ============================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_phase1_handoff_joins_the_locked_transaction_and_announces_after_commit(
    phase1_database: SessionMaker,
) -> None:
    factory = phase1_database
    turn = await _turn(factory)

    async with factory() as db:
        async with asyncio.timeout(_TURN_TIMEOUT_SECONDS):
            await _run_pipeline_transactionally(
                service=turn.service,
                event_request=turn.request,
                db=db,
                context=turn.context,
            )

    service = turn.service
    # Written inside the locked phase 1 transaction, invisible outside it and
    # not announced while it was open.
    assert service.inside_phase1 is not None
    assert len(service.inside_phase1.handoff_ids) == 1
    assert len(service.inside_phase1.request_event_ids) == 1
    assert service.outside_phase1 == _Snapshot((), ())
    assert service.sent_during_phase1 == 0

    # Exactly one webhook, after the commit and before the response I/O.
    assert len(turn.dispatcher.sent) == 1
    assert service.sent_before_finish == 1
    payload, seen_at_send = turn.dispatcher.sent[0]
    assert seen_at_send == service.inside_phase1
    assert service.at_finish == service.inside_phase1
    (handoff_id,) = service.inside_phase1.handoff_ids
    assert payload.handoff_id == handoff_id
    assert payload.session_id == turn.session_id
    assert service.result is not None
    assert service.result.handoff_summary is not None
    assert service.result.handoff_summary["id"] == str(handoff_id)

    async with factory() as observer:
        record = (
            await observer.execute(
                sa.text(
                    "SELECT tenant_id, webhook_sent, webhook_response "
                    "FROM handoff_records WHERE id = :id"
                ),
                {"id": handoff_id},
            )
        ).one()
        event = (
            await observer.execute(
                sa.text(
                    "SELECT tenant_id, interaction_origin, metadata "
                    "FROM events WHERE session_id = :session_id "
                    "AND event_type = 'handoff.request'"
                ),
                {"session_id": turn.session_id},
            )
        ).one()
        session_row = await observer.get(Session, turn.session_id)
    # The delivery bookkeeping committed after the send.
    assert record.tenant_id == turn.tenant_id
    assert record.webhook_sent is True
    assert record.webhook_response["status"] == WebhookStatus.SENT.value
    # handoff.request carries the trusted origin, tenant and session only.
    assert event.tenant_id == turn.tenant_id
    assert event.interaction_origin == InteractionOrigin.PRODUCTION.value
    assert event.metadata == {"handoff_id": str(handoff_id)}
    # The phase 1 state committed in the same transaction as the handoff.
    assert session_row is not None
    assert session_row.get_cognitive_state().turn_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_phase1_rollback_after_the_handoff_write_leaves_nothing_to_announce(
    phase1_database: SessionMaker,
) -> None:
    factory = phase1_database
    turn = await _turn(factory, fail_state_flush=True)

    async with factory() as db:
        async with asyncio.timeout(_TURN_TIMEOUT_SECONDS):
            with pytest.raises(RuntimeError, match="phase 1 state flush failed"):
                await _run_pipeline_transactionally(
                    service=turn.service,
                    event_request=turn.request,
                    db=db,
                    context=turn.context,
                )
            # Even a later dispatch on the rolled-back session announces
            # nothing: the queued record was never committed.
            await dispatch_committed_handoff_webhooks(db)

    service = turn.service
    # The handoff really was written in the transaction that rolled back.
    assert service.inside_phase1 is not None
    assert len(service.inside_phase1.handoff_ids) == 1
    assert len(service.inside_phase1.request_event_ids) == 1
    assert service.outside_phase1 == _Snapshot((), ())
    assert service.sent_during_phase1 == 0

    assert await _observe(factory, turn.session_id) == _Snapshot((), ())
    assert turn.dispatcher.sent == []
    assert service.at_finish is None
    async with factory() as observer:
        session_row = await observer.get(Session, turn.session_id)
    assert session_row is not None
    assert session_row.cognitive_state == {}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_phase1_handoff_created_at_is_the_insert_time_not_the_transaction_start(
    phase1_database: SessionMaker,
) -> None:
    factory = phase1_database
    turn = await _turn(factory)
    handoff = HandoffService(
        event_store=EventStore(session_factory=factory),
        summarizer=ConversationSummarizer(),
        webhook_dispatcher=turn.dispatcher,
        session_factory=factory,
    )

    async with factory() as db:
        transaction_start = (await db.execute(sa.text("SELECT now()"))).scalar_one()
        await asyncio.sleep(2)
        before_insert = (
            await db.execute(sa.text("SELECT clock_timestamp()"))
        ).scalar_one()
        response = await handoff.initiate_handoff(
            HandoffRequest(
                tenant_id=turn.tenant_id,
                session_id=turn.session_id,
                reason=HandoffReason.USER_REQUEST,
            ),
            context=turn.context,
            session=db,
        )
        after_insert = (
            await db.execute(sa.text("SELECT clock_timestamp()"))
        ).scalar_one()
        stored = (
            await db.execute(
                sa.text("SELECT created_at FROM handoff_records WHERE id = :id"),
                {"id": response.id},
            )
        ).scalar_one()
        await db.commit()
    await dispatch_committed_handoff_webhooks(db)

    # ``now()`` would have stamped the transaction start, 2 s earlier.
    assert (before_insert - transaction_start).total_seconds() >= 1.9
    assert before_insert <= stored <= after_insert
    assert response.created_at == stored
