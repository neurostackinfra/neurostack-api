"""The caller-session seam of ``HandoffService.initiate_handoff`` (#145).

Pipeline phase 1 holds ``FOR UPDATE`` on the ``sessions`` row. A synchronous
handoff written from another connection waits on that lock, so with
``session=`` the record and ``handoff.request`` join the caller's transaction
and the creation webhook waits for ``dispatch_committed_handoff_webhooks``
after the caller's commit. Without ``session`` nothing changes; the existing
suites in ``tests/handoff/test_service*.py`` pin that path.

The real lock behaviour is proved on PostgreSQL in
``tests/integration/test_handoff_phase1_lock_postgres.py``.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from neurostack_core.srm import SRMDecision
from src.core.exceptions import EventStoreError, HandoffServiceError
from src.core.pipeline import CognitivePipeline, PipelineInput, PipelineResult
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
from src.interaction_context import (
    InteractionContextError,
    InteractionOrigin,
    ResolvedInteractionContext,
    build_interaction_context,
)
from src.memory.event_store import EventData, EventType
from src.models.session import CognitiveStateData
from src.models.session import Session as SessionRow
from src.services.event_service import EventService
from src.services.handoff_service import (
    HandoffPersistenceError,
    HandoffPostCommitError,
    HandoffService,
    dispatch_committed_handoff_webhooks,
)
from tests.handoff.test_service import (
    _FakeHandoffRepository,
    _FakeHandoffStore,
    _FakeSession,
)

_WEBHOOK_URL = "https://clinic.test/handoff"


class _CallerSession:
    """The caller's open transaction: only the caller may end it."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.expunged: list[Any] = []
        self.ended: list[str] = []

    def expunge(self, instance: Any) -> None:
        self.expunged.append(instance)

    async def commit(self) -> None:
        self.ended.append("commit")

    async def rollback(self) -> None:
        self.ended.append("rollback")

    async def close(self) -> None:
        self.ended.append("close")


class _BatchEventStore:
    """Records batches; the own-connection ``append`` must stay unused."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.batches: list[tuple[list[EventData], Any]] = []
        self.single_appends: list[EventData] = []

    async def append_batch(
        self, events: list[EventData], *, session: Any = None
    ) -> list[EventData]:
        if self.error is not None:
            raise self.error
        self.batches.append((list(events), session))
        return events

    async def append(self, event: EventData) -> EventData:
        self.single_appends.append(event)
        return event

    async def get_recent_messages(
        self, tenant_id: UUID, session_id: UUID, limit: int
    ) -> list[EventData]:
        return []


class _RecordingDispatcher(WebhookDispatcher):
    def __init__(self) -> None:
        super().__init__(default_timeout=5, max_retries=1, retry_delay=0)
        self.sent: list[Any] = []

    async def send(self, tenant_id: UUID, payload: Any) -> WebhookDelivery:
        self.sent.append(payload)
        return WebhookDelivery(
            handoff_id=payload.handoff_id,
            tenant_id=tenant_id,
            url=_WEBHOOK_URL,
            status=WebhookStatus.SENT,
            attempt=1,
            response_status=204,
        )


@dataclass
class _Harness:
    tenant_id: UUID
    session_id: UUID
    store: _FakeHandoffStore
    events: _BatchEventStore
    dispatcher: _RecordingDispatcher
    service: HandoffService
    opened: list[_FakeSession] = field(default_factory=list)
    repository_sessions: list[Any] = field(default_factory=list)

    def request(self) -> HandoffRequest:
        return HandoffRequest(
            tenant_id=self.tenant_id,
            session_id=self.session_id,
            reason=HandoffReason.USER_REQUEST,
            recent_messages=[{"role": "user", "content": "Quero falar com alguém"}],
            key_info={"numero_whatsapp": "5511999990145"},
        )

    def context(
        self, origin: InteractionOrigin = InteractionOrigin.PRODUCTION
    ) -> ResolvedInteractionContext:
        return build_interaction_context(
            effective_tenant_id=self.tenant_id,
            session_id=self.session_id,
            origin=origin,
            event_type="message.in",
        )


def _harness(
    *,
    configured: bool = True,
    event_error: Exception | None = None,
) -> _Harness:
    tenant_id, session_id = uuid4(), uuid4()
    store = _FakeHandoffStore()
    events = _BatchEventStore(event_error)
    dispatcher = _RecordingDispatcher()
    if configured:
        dispatcher.set_config(tenant_id, WebhookConfig(url=_WEBHOOK_URL, enabled=True))
    opened: list[_FakeSession] = []
    repository_sessions: list[Any] = []

    def open_session() -> _FakeSession:
        session: _FakeSession = cast(Any, _FakeSession)(store)
        opened.append(session)
        return session

    def repository_factory(session: Any) -> _FakeHandoffRepository:
        repository_sessions.append(session)
        return _FakeHandoffRepository(store)

    service = HandoffService(
        event_store=events,
        summarizer=ConversationSummarizer(),
        webhook_dispatcher=dispatcher,
        session_factory=cast(Any, open_session),
        repository_factory=cast(Any, repository_factory),
    )
    return _Harness(
        tenant_id=tenant_id,
        session_id=session_id,
        store=store,
        events=events,
        dispatcher=dispatcher,
        service=service,
        opened=opened,
        repository_sessions=repository_sessions,
    )


async def _initiate(harness: _Harness, caller: _CallerSession) -> Any:
    return await harness.service.initiate_handoff(
        harness.request(),
        context=harness.context(),
        session=cast(AsyncSession, caller),
    )


def _assert_nothing_written(harness: _Harness, caller: _CallerSession) -> None:
    assert harness.store.records == {}
    assert harness.events.batches == []
    assert harness.events.single_appends == []
    assert harness.dispatcher.sent == []
    assert caller.info == {}
    assert caller.ended == []
    assert harness.opened == []


# ============================================
# The seam
# ============================================


def test_seam_keeps_request_positional_and_context_and_session_keyword_only() -> None:
    parameters = inspect.signature(HandoffService.initiate_handoff).parameters
    assert list(parameters) == ["self", "request", "context", "session"]
    assert parameters["request"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name in ("context", "session"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is None


# ============================================
# Writes join the caller's transaction
# ============================================


@pytest.mark.asyncio
async def test_writes_join_the_caller_session_and_wait_to_announce() -> None:
    harness = _harness()
    caller = _CallerSession()
    context = harness.context()

    response = await harness.service.initiate_handoff(
        harness.request(),
        context=context,
        session=cast(AsyncSession, caller),
    )

    # One unit of work: the caller's. No second session, no commit or
    # rollback of the caller's transaction, no webhook yet.
    assert harness.opened == []
    assert harness.repository_sessions == [caller]
    assert caller.ended == []
    assert harness.store.order == []
    assert harness.dispatcher.sent == []
    record = harness.store.records[response.id]
    assert caller.expunged == [record]

    batch, batch_session = harness.events.batches[0]
    assert len(harness.events.batches) == 1
    assert batch_session is caller
    assert harness.events.single_appends == []
    (event,) = batch
    assert event.event_type == EventType.HANDOFF_REQUEST.value
    assert event.tenant_id == harness.tenant_id
    assert event.session_id == harness.session_id
    assert event.interaction_origin is InteractionOrigin.PRODUCTION
    assert event.event_metadata == {"handoff_id": str(record.id)}
    rendered = event.model_dump_json()
    assert "interaction_event_id" not in rendered
    assert str(context.interaction_event_id) not in rendered

    assert response.webhook_sent is False
    assert response.webhook_status is WebhookStatus.PENDING
    assert response.status is HandoffStatus.PENDING
    assert response.user_message


@pytest.mark.asyncio
async def test_unconfigured_tenant_has_no_webhook_status_and_sends_nothing() -> None:
    harness = _harness(configured=False)
    caller = _CallerSession()

    response = await _initiate(harness, caller)
    await dispatch_committed_handoff_webhooks(cast(AsyncSession, caller))

    assert response.webhook_status is None
    assert harness.dispatcher.sent == []
    assert "bookkeeping" not in harness.store.order


# ============================================
# Failures before any effect, rollback left to the caller
# ============================================


@pytest.mark.asyncio
async def test_context_is_refused_before_anything_touches_the_caller_session() -> None:
    harness = _harness()
    caller = _CallerSession()
    foreign = build_interaction_context(
        effective_tenant_id=harness.tenant_id,
        session_id=uuid4(),
        origin=InteractionOrigin.PRODUCTION,
        event_type="message.in",
    )

    with pytest.raises(InteractionContextError):
        await harness.service.initiate_handoff(
            harness.request(), context=foreign, session=cast(AsyncSession, caller)
        )
    with pytest.raises(TypeError):
        await harness.service.initiate_handoff(
            harness.request(),
            context=cast(Any, object()),
            session=cast(AsyncSession, caller),
        )

    assert harness.repository_sessions == []
    _assert_nothing_written(harness, caller)


@pytest.mark.asyncio
async def test_session_outside_the_tenant_is_refused_before_any_write() -> None:
    harness = _harness()
    harness.store.session_is_owned = False
    caller = _CallerSession()

    with pytest.raises(HandoffServiceError, match="handoff_session_not_found"):
        await _initiate(harness, caller)

    _assert_nothing_written(harness, caller)


@pytest.mark.asyncio
async def test_record_failure_leaves_the_rollback_to_the_caller() -> None:
    harness = _harness()
    harness.store.fail_insert = True
    caller = _CallerSession()

    with pytest.raises(HandoffPersistenceError):
        await _initiate(harness, caller)

    _assert_nothing_written(harness, caller)


@pytest.mark.asyncio
async def test_event_failure_is_a_persistence_error_inside_the_transaction() -> None:
    harness = _harness(event_error=EventStoreError("fk violation"))
    caller = _CallerSession()

    with pytest.raises(HandoffPersistenceError) as raised:
        await _initiate(harness, caller)

    # Not "committed but the event failed": the caller's rollback removes
    # the record written just before, so the pair stays atomic.
    assert not isinstance(raised.value, HandoffPostCommitError)
    assert caller.ended == []
    assert caller.info == {}
    assert harness.dispatcher.sent == []


# ============================================
# Post-commit dispatch
# ============================================


@pytest.mark.asyncio
async def test_post_commit_dispatch_sends_the_creation_webhook_exactly_once() -> None:
    harness = _harness()
    caller = _CallerSession()
    response = await _initiate(harness, caller)

    await dispatch_committed_handoff_webhooks(cast(AsyncSession, caller))
    await dispatch_committed_handoff_webhooks(cast(AsyncSession, caller))

    (payload,) = harness.dispatcher.sent
    assert payload.event_type == "handoff.pending"
    assert payload.handoff_id == response.id
    assert payload.tenant_id == harness.tenant_id
    assert payload.session_id == harness.session_id
    assert payload.status is HandoffStatus.PENDING
    assert payload.reason is HandoffReason.USER_REQUEST
    assert payload.recent_messages == [
        {"role": "user", "content": "Quero falar com alguém"}
    ]
    record = harness.store.records[response.id]
    assert record.webhook_sent is True
    assert record.webhook_response == {
        "status": WebhookStatus.SENT.value,
        "attempt": 1,
        "response_status": 204,
    }
    # The delivery bookkeeping commits in its own session, after the caller's.
    assert harness.store.order == ["bookkeeping", "commit"]
    assert caller.info == {}


@pytest.mark.asyncio
async def test_creation_payload_matches_the_session_free_path() -> None:
    joined = _harness()
    caller = _CallerSession()
    joined_response = await _initiate(joined, caller)
    await dispatch_committed_handoff_webhooks(cast(AsyncSession, caller))

    alone = _harness()
    alone.tenant_id, alone.session_id = joined.tenant_id, joined.session_id
    alone.dispatcher.set_config(
        joined.tenant_id, WebhookConfig(url=_WEBHOOK_URL, enabled=True)
    )
    alone_response = await alone.service.initiate_handoff(
        alone.request(), context=alone.context()
    )

    volatile = {"handoff_id", "timestamp"}
    (joined_payload,) = joined.dispatcher.sent
    (alone_payload,) = alone.dispatcher.sent
    assert joined_payload.model_dump(exclude=volatile) == alone_payload.model_dump(
        exclude=volatile
    )
    assert joined_response.summary.summary_text == alone_response.summary.summary_text


@pytest.mark.asyncio
async def test_dispatch_drops_a_queued_webhook_whose_record_never_committed() -> None:
    harness = _harness()
    caller = _CallerSession()
    await _initiate(harness, caller)
    harness.store.records.clear()  # the caller rolled back

    await dispatch_committed_handoff_webhooks(cast(AsyncSession, caller))

    assert harness.dispatcher.sent == []
    assert "bookkeeping" not in harness.store.order
    assert caller.info == {}


@pytest.mark.asyncio
async def test_dispatch_runs_every_queued_webhook_then_raises_first() -> None:
    harness = _harness()
    caller = _CallerSession()
    await _initiate(harness, caller)
    await _initiate(harness, caller)
    harness.store.fail_bookkeeping = True

    with pytest.raises(HandoffPostCommitError):
        await dispatch_committed_handoff_webhooks(cast(AsyncSession, caller))

    assert len(harness.dispatcher.sent) == 2
    assert harness.store.order == ["bookkeeping", "bookkeeping"]
    assert caller.info == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session",
    [SimpleNamespace(), AsyncMock(), MagicMock()],
    ids=["no-info", "async-mock", "mock"],
)
async def test_objects_without_a_session_info_dict_carry_no_queue(
    session: Any,
) -> None:
    await dispatch_committed_handoff_webhooks(cast(AsyncSession, session))


# ============================================
# Phase 1 bridge: EventService -> pipeline -> handoff
# ============================================


def _handoff_decision() -> SRMDecision:
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


@pytest.mark.asyncio
async def test_pipeline_forwards_the_phase1_session_to_the_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, session_id = uuid4(), uuid4()
    handoff_service = MagicMock()
    handoff_service.initiate_handoff = AsyncMock(return_value=None)
    pipeline = CognitivePipeline(handoff_service=handoff_service)
    monkeypatch.setattr(
        pipeline, "_run_srm", AsyncMock(return_value=_handoff_decision())
    )
    context = build_interaction_context(
        effective_tenant_id=tenant_id,
        session_id=session_id,
        origin=InteractionOrigin.SIMULATOR,
        event_type="message.in",
    )
    phase1 = cast(AsyncSession, object())

    await pipeline.process(
        PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Quero falar com uma pessoa",
            channel="whatsapp",
        ),
        context=context,
        session=phase1,
    )

    call = handoff_service.initiate_handoff.await_args
    assert len(call.args) == 1
    assert set(call.kwargs) == {"context", "session"}
    assert call.kwargs["context"] is context
    assert call.kwargs["session"] is phase1


@pytest.mark.asyncio
async def test_run_pipeline_hands_its_locked_session_to_the_pipeline() -> None:
    from src.api.schemas.events import CanonicalEventRequest, EventPayload
    from src.api.schemas.events import EventType as RequestEventType

    request = CanonicalEventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=RequestEventType.MESSAGE_IN,
        payload=EventPayload(content="Quero falar com alguém", channel="whatsapp"),
    )
    state = CognitiveStateData()
    row = SessionRow(
        id=request.session_id,
        tenant_id=request.tenant_id,
        channel="whatsapp",
        cognitive_state=state.to_dict(),
        session_metadata={},
        message_count=0,
        last_activity_at=datetime.now(timezone.utc),
    )
    seen: dict[str, Any] = {}

    class _Pipeline:
        async def process(
            self,
            input_data: PipelineInput,
            *,
            context: Any = None,
            session: Any = None,
        ) -> PipelineResult:
            seen["session"] = session
            return PipelineResult(
                tenant_id=input_data.tenant_id,
                session_id=input_data.session_id,
                intent="duvida",
                intent_category="question",
                funnel_stage="lead_novo",
                action="respond",
                confidence=0.9,
                safety_concern=False,
                safety_category="none",
                needs_rag=False,
                handoff_required=False,
                system_prompt="system",
                instructions="instructions",
                state_patch={"slots": {}, "flags": {}},
            )

    service = EventService(pipeline=cast(Any, _Pipeline()))
    setattr(service, "_lock_session_state", AsyncMock(return_value=(row, state)))
    setattr(service, "_load_clinic_config", AsyncMock(return_value={}))
    setattr(service, "_load_message_history", AsyncMock(return_value=[]))
    setattr(service, "_persist_session_state", AsyncMock(return_value=None))
    phase1 = AsyncMock()

    await service.run_pipeline(request, session=phase1)

    assert seen["session"] is phase1
