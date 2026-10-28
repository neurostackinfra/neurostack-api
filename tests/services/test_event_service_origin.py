"""EventService carries ONE interaction context through both phases and the bundle.

Wish ``interaction-origin-training-safety`` Group 3: ``run_pipeline`` and
``finish_response`` take ``context`` keyword-only (``None`` goes through the
single ``unknown`` factory), nothing is keyed by the context before the
tenant-scoped session lock, the bundle is built by a pure function and written
with exactly one ``append_batch``, and only a committed batch exposes
``interaction_event_id``.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

import src.services.event_service as event_service_module
from src.api.schemas.events import (
    CanonicalEventRequest,
    ContextOutput,
    EventPayload,
    EventResponse,
    EventType,
    PromptOutput,
    SRMDecision,
    StatePatch,
)
from src.core.exceptions import EventStoreError
from src.core.pipeline import PipelineInput, PipelineResult
from src.interaction_context import (
    InteractionContextError,
    InteractionOrigin,
    ResolvedInteractionContext,
    build_interaction_context,
)
from src.models.event import Event
from src.models.session import CognitiveStateData, Session
from src.repositories import EventRepository
from src.services.event_service import EventService, SessionNotFoundError

# ============================================
# Helpers
# ============================================


def _request(
    event_type: EventType = EventType.MESSAGE_IN,
    *,
    content: str = "quero agendar",
) -> CanonicalEventRequest:
    return CanonicalEventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=event_type,
        payload=EventPayload(content=content, channel="whatsapp", user_id="u-1"),
    )


def _context(
    request: CanonicalEventRequest,
    origin: InteractionOrigin = InteractionOrigin.PRODUCTION,
) -> ResolvedInteractionContext:
    return build_interaction_context(
        effective_tenant_id=request.tenant_id,
        session_id=request.session_id,
        origin=origin,
        event_type=request.event_type,
    )


def _result(request: CanonicalEventRequest, **overrides: Any) -> PipelineResult:
    values: dict[str, Any] = {
        "tenant_id": request.tenant_id,
        "session_id": request.session_id,
        "intent": "agendamento",
        "intent_category": "scheduling",
        "funnel_stage": "em_qualificacao",
        "action": "collect_info",
        "confidence": 0.9,
        "safety_concern": False,
        "safety_category": "none",
        "needs_rag": False,
        "handoff_required": False,
        "system_prompt": "sys",
        "instructions": "inst",
    }
    values.update(overrides)
    return PipelineResult(**values)


def _response(result: PipelineResult) -> EventResponse:
    return EventResponse(
        event_id=result.event_id,
        decision=SRMDecision(
            intent=result.intent,
            funnel_stage=result.funnel_stage,
            action=result.action,
            confidence=result.confidence,
        ),
        state_patch=StatePatch(),
        prompt=PromptOutput(system="sys", instructions="inst"),
        context=ContextOutput(),
        processed_at=datetime.now(timezone.utc).isoformat(),
    )


class _RecordingPipeline:
    def __init__(self, **result_overrides: Any) -> None:
        self.contexts: list[Optional[ResolvedInteractionContext]] = []
        self.inputs: list[PipelineInput] = []
        self.result_overrides = result_overrides

    async def process(
        self,
        input_data: PipelineInput,
        *,
        context: Optional[ResolvedInteractionContext] = None,
        session: Any = None,
    ) -> PipelineResult:
        self.inputs.append(input_data)
        self.contexts.append(context)
        return PipelineResult(
            tenant_id=input_data.tenant_id,
            session_id=input_data.session_id,
            intent="agendamento",
            intent_category="scheduling",
            funnel_stage="em_qualificacao",
            action="collect_info",
            confidence=0.9,
            safety_concern=False,
            safety_category="none",
            needs_rag=False,
            handoff_required=False,
            system_prompt="sys",
            instructions="inst",
            **self.result_overrides,
        )


class _BatchStore:
    """Event Store double exposing ONLY ``append_batch``."""

    def __init__(self, error: Optional[BaseException] = None) -> None:
        self.batches: list[list[Any]] = []
        self.error = error

    async def append_batch(self, events: list[Any]) -> list[Any]:
        self.batches.append(list(events))
        if self.error is not None:
            raise self.error
        return list(events)


def _phase_one_service(
    monkeypatch: pytest.MonkeyPatch,
    request: CanonicalEventRequest,
    *,
    session_found: bool = True,
) -> tuple[EventService, _RecordingPipeline, list[str], dict[str, AsyncMock]]:
    pipeline = _RecordingPipeline()
    service = EventService(pipeline=pipeline)  # type: ignore[arg-type]
    order: list[str] = []
    row = Session(
        id=request.session_id,
        tenant_id=request.tenant_id,
        channel="whatsapp",
        cognitive_state={},
        session_metadata={},
        message_count=0,
    )

    def recorder(name: str, value: Any) -> AsyncMock:
        async def _side_effect(*_args: Any, **_kwargs: Any) -> Any:
            order.append(name)
            return value

        return AsyncMock(side_effect=_side_effect)

    mocks = {
        "_lock_session_state": recorder(
            "lock", (row if session_found else None, CognitiveStateData())
        ),
        "_load_clinic_config": recorder("config", None),
        "_load_message_history": recorder("history", []),
        "_persist_session_state": recorder("persist", None),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(service, name, mock)
    return service, pipeline, order, mocks


def _phase_two_service(
    monkeypatch: pytest.MonkeyPatch,
    result: PipelineResult,
    store: _BatchStore,
) -> tuple[EventService, AsyncMock, EventResponse]:
    service = EventService(pipeline=_RecordingPipeline())  # type: ignore[arg-type]
    response = _response(result)
    convert = AsyncMock(return_value=(response, {}, "", [], {}))
    monkeypatch.setattr(service, "_convert_pipeline_result_to_response", convert)
    monkeypatch.setattr(
        event_service_module, "get_event_store", AsyncMock(return_value=store)
    )
    return service, convert, response


# ============================================
# Context resolution and identity
# ============================================


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", [EventType.MESSAGE_IN, EventType.SESSION_START])
async def test_absent_context_goes_through_the_single_unknown_factory(
    monkeypatch: pytest.MonkeyPatch, event_type: EventType
) -> None:
    request = _request(event_type)
    service, pipeline, _order, _mocks = _phase_one_service(monkeypatch, request)
    factory_calls: list[dict[str, Any]] = []
    real_factory = event_service_module.unknown_interaction_context

    def spy(**kwargs: Any) -> ResolvedInteractionContext:
        factory_calls.append(kwargs)
        return real_factory(**kwargs)

    monkeypatch.setattr(event_service_module, "unknown_interaction_context", spy)

    await service.run_pipeline(request)

    assert factory_calls == [
        {
            "effective_tenant_id": request.tenant_id,
            "session_id": request.session_id,
            "event_type": event_type,
        }
    ]
    (context,) = pipeline.contexts
    assert type(context) is ResolvedInteractionContext
    assert context.origin is InteractionOrigin.UNKNOWN
    assert context.effective_tenant_id == request.tenant_id
    assert context.session_id == request.session_id
    assert (context.interaction_event_id is not None) == (
        event_type is EventType.MESSAGE_IN
    )


@pytest.mark.asyncio
async def test_legacy_process_event_shares_one_unknown_context_across_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    service, pipeline, _order, _mocks = _phase_one_service(monkeypatch, request)
    store = _BatchStore()
    monkeypatch.setattr(
        event_service_module, "get_event_store", AsyncMock(return_value=store)
    )
    convert_results: list[EventResponse] = []

    async def convert(result: PipelineResult, **_kwargs: Any) -> Any:
        convert_results.append(_response(result))
        return convert_results[-1], {}, "", [], {}

    monkeypatch.setattr(service, "_convert_pipeline_result_to_response", convert)
    seen: dict[str, Any] = {}
    real_run, real_finish = service.run_pipeline, service.finish_response

    async def run_spy(*args: Any, **kwargs: Any) -> Any:
        seen["run_pipeline"] = kwargs["context"]
        return await real_run(*args, **kwargs)

    async def finish_spy(*args: Any, **kwargs: Any) -> Any:
        seen["finish_response"] = kwargs["context"]
        return await real_finish(*args, **kwargs)

    monkeypatch.setattr(service, "run_pipeline", run_spy)
    monkeypatch.setattr(service, "finish_response", finish_spy)

    response = await service.process_event(request)

    context = seen["run_pipeline"]
    assert seen["finish_response"] is context
    assert pipeline.contexts == [context]
    assert context.origin is InteractionOrigin.UNKNOWN
    (batch,) = store.batches
    assert batch[0].id == context.interaction_event_id
    assert {event.interaction_origin for event in batch} == {InteractionOrigin.UNKNOWN}
    assert response.interaction_event_id == context.interaction_event_id


@pytest.mark.asyncio
async def test_the_same_context_object_reaches_pipeline_bundle_and_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    context = _context(request)
    service, pipeline, order, _mocks = _phase_one_service(monkeypatch, request)
    store = _BatchStore()
    bundle_contexts: list[Any] = []
    real_builder = event_service_module.build_interaction_event_bundle

    def bundle_spy(*args: Any, **kwargs: Any) -> Any:
        bundle_contexts.append(args[2])
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(
        event_service_module, "build_interaction_event_bundle", bundle_spy
    )
    monkeypatch.setattr(
        event_service_module, "get_event_store", AsyncMock(return_value=store)
    )
    monkeypatch.setattr(
        service,
        "_convert_pipeline_result_to_response",
        AsyncMock(
            side_effect=lambda result, **_kw: (_response(result), {}, "", [], {})
        ),
    )

    result, config, history = await service.run_pipeline(
        request, session=object(), context=context  # type: ignore[arg-type]
    )
    response = await service.finish_response(
        request, result, config, history, context=context
    )

    assert order == ["lock", "config", "history", "persist"]
    assert pipeline.contexts[0] is context
    assert bundle_contexts[0] is context
    assert response.interaction_event_id == context.interaction_event_id
    assert response.event_id == result.event_id


# ============================================
# Fail-closed checks before any effect
# ============================================


def _assert_no_phase_one_effect(
    order: list[str], pipeline: _RecordingPipeline, mocks: dict[str, AsyncMock]
) -> None:
    assert order == []
    assert pipeline.contexts == []
    for mock in mocks.values():
        mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("diverges", ["tenant", "session", "event_type"])
async def test_divergent_context_raises_the_fixed_error_before_any_effect(
    monkeypatch: pytest.MonkeyPatch, diverges: str
) -> None:
    request = _request()
    other = _request(
        EventType.SESSION_START if diverges == "event_type" else EventType.MESSAGE_IN
    )
    context = build_interaction_context(
        effective_tenant_id=(
            other.tenant_id if diverges == "tenant" else request.tenant_id
        ),
        session_id=other.session_id if diverges == "session" else request.session_id,
        origin=InteractionOrigin.PRODUCTION,
        event_type=other.event_type if diverges == "event_type" else "message.in",
    )
    service, pipeline, order, mocks = _phase_one_service(monkeypatch, request)

    with pytest.raises(InteractionContextError):
        await service.run_pipeline(
            request, session=object(), context=context  # type: ignore[arg-type]
        )
    _assert_no_phase_one_effect(order, pipeline, mocks)

    store = _BatchStore()
    finisher, convert, _response_ = _phase_two_service(
        monkeypatch, _result(request), store
    )
    with pytest.raises(InteractionContextError):
        await finisher.finish_response(
            request, _result(request), None, [], context=context
        )
    convert.assert_not_awaited()
    assert store.batches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", ["dict", "namespace", "json"])
async def test_non_context_is_rejected_before_any_effect(
    monkeypatch: pytest.MonkeyPatch, forged: str
) -> None:
    request = _request()
    fields_ = {
        "effective_tenant_id": request.tenant_id,
        "session_id": request.session_id,
        "origin": InteractionOrigin.PRODUCTION,
        "interaction_event_id": uuid4(),
    }
    candidate = {
        "dict": dict(fields_),
        "namespace": SimpleNamespace(**fields_),
        "json": '{"origin": "production"}',
    }[forged]
    service, pipeline, order, mocks = _phase_one_service(monkeypatch, request)

    with pytest.raises(TypeError):
        await service.run_pipeline(
            request, session=object(), context=candidate  # type: ignore[arg-type]
        )
    _assert_no_phase_one_effect(order, pipeline, mocks)

    store = _BatchStore()
    finisher, convert, _response_ = _phase_two_service(
        monkeypatch, _result(request), store
    )
    with pytest.raises(TypeError):
        await finisher.finish_response(
            request, _result(request), None, [], context=candidate  # type: ignore[arg-type]
        )
    convert.assert_not_awaited()
    assert store.batches == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin", [InteractionOrigin.PRODUCTION, InteractionOrigin.SIMULATOR]
)
async def test_session_less_run_refuses_a_trusted_origin(
    monkeypatch: pytest.MonkeyPatch, origin: InteractionOrigin
) -> None:
    request = _request()
    service, pipeline, order, mocks = _phase_one_service(monkeypatch, request)

    with pytest.raises(InteractionContextError):
        await service.run_pipeline(request, context=_context(request, origin))

    _assert_no_phase_one_effect(order, pipeline, mocks)


@pytest.mark.asyncio
async def test_session_less_run_accepts_an_unknown_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    context = _context(request, InteractionOrigin.UNKNOWN)
    service, pipeline, order, _mocks = _phase_one_service(monkeypatch, request)

    await service.run_pipeline(request, context=context)

    assert pipeline.contexts == [context]
    assert "lock" not in order


@pytest.mark.asyncio
async def test_nothing_is_keyed_by_the_context_before_the_lock_proves_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    context = _context(request)
    service, pipeline, order, mocks = _phase_one_service(
        monkeypatch, request, session_found=False
    )
    store = _BatchStore()
    monkeypatch.setattr(
        event_service_module, "get_event_store", AsyncMock(return_value=store)
    )
    session = object()

    with pytest.raises(SessionNotFoundError):
        await service.run_pipeline(
            request, session=session, context=context  # type: ignore[arg-type]
        )

    # The lock is the first and only effect, keyed by the request's tenant and
    # session (equal to the context's by the pre-lock check), never by the
    # interaction ID.
    assert order == ["lock"]
    mocks["_lock_session_state"].assert_awaited_once_with(
        request.tenant_id, request.session_id, session
    )
    for name in (
        "_load_clinic_config",
        "_load_message_history",
        "_persist_session_state",
    ):
        mocks[name].assert_not_awaited()
    assert pipeline.contexts == []
    assert store.batches == []


# ============================================
# Bundle: one batch, uniform origin, root ID
# ============================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        InteractionOrigin.PRODUCTION,
        InteractionOrigin.SIMULATOR,
        InteractionOrigin.UNKNOWN,
    ],
)
async def test_one_batch_with_uniform_origin_and_the_root_interaction_id(
    monkeypatch: pytest.MonkeyPatch, origin: InteractionOrigin
) -> None:
    request = _request()
    context = _context(request, origin)
    result = _result(
        request,
        handoff_required=True,
        handoff_reason="user_request",
        handoff_summary={"summary": "s", "urgency": "normal"},
        mode="handoff",
        state_patch={
            "slots": {"atendimento_tipo": "convenio"},
            "funnel_stage": "em_qualificacao",
        },
    )
    store = _BatchStore()
    service, _convert, _response_ = _phase_two_service(monkeypatch, result, store)

    response = await service.finish_response(request, result, None, [], context=context)

    (batch,) = store.batches
    assert [event.event_type for event in batch] == [
        "message.in",
        "srm.decision",
        "prompt.generated",
        "ns.atendimento_tipo_set",
        "ns.funnel_stage_changed",
        "ns.handoff_created",
    ]
    assert {event.interaction_origin for event in batch} == {origin}
    assert {event.tenant_id for event in batch} == {context.effective_tenant_id}
    assert {event.session_id for event in batch} == {context.session_id}
    assert batch[0].id == context.interaction_event_id
    assert batch[1].id == result.event_id == response.event_id
    assert len({event.id for event in batch}) == len(batch)
    assert response.interaction_event_id == context.interaction_event_id
    for event in batch:
        rendered = repr(event.event_data) + repr(event.event_metadata)
        assert "interaction_origin" not in rendered
        assert "ResolvedInteractionContext" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type", [EventType.SESSION_START, EventType.HANDOFF_COMPLETE]
)
async def test_non_message_in_keeps_its_type_and_returns_a_null_interaction_id(
    monkeypatch: pytest.MonkeyPatch, event_type: EventType
) -> None:
    request = _request(event_type)
    context = _context(request, InteractionOrigin.SIMULATOR)
    assert context.interaction_event_id is None
    result = _result(request)
    store = _BatchStore()
    service, _convert, _response_ = _phase_two_service(monkeypatch, result, store)

    response = await service.finish_response(request, result, None, [], context=context)

    (batch,) = store.batches
    assert batch[0].event_type == event_type.value
    assert isinstance(batch[0].id, UUID)
    assert {event.interaction_origin for event in batch} == {
        InteractionOrigin.SIMULATOR
    }
    assert response.interaction_event_id is None
    assert response.event_id == result.event_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["event-store-error", "unexpected-error", "store-unavailable"],
)
async def test_batch_failure_keeps_the_legacy_response_and_nulls_only_the_id(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    request = _request()
    context = _context(request)
    result = _result(request)
    store = _BatchStore(
        error=(
            EventStoreError("batch failed")
            if failure == "event-store-error"
            else RuntimeError("boom")
        )
    )
    service, _convert, prepared = _phase_two_service(monkeypatch, result, store)
    expected = prepared.model_dump(mode="json")
    if failure == "store-unavailable":
        monkeypatch.setattr(
            event_service_module,
            "get_event_store",
            AsyncMock(side_effect=EventStoreError("no pool")),
        )

    response = await service.finish_response(request, result, None, [], context=context)

    assert response.interaction_event_id is None
    assert response.event_id == result.event_id
    assert response.model_dump(mode="json") == expected
    assert str(context.interaction_event_id) not in response.model_dump_json()


@pytest.mark.asyncio
async def test_save_events_to_store_keeps_its_name_and_returns_the_persisted_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    context = _context(request)
    store = _BatchStore()
    monkeypatch.setattr(
        event_service_module, "get_event_store", AsyncMock(return_value=store)
    )
    service = EventService(pipeline=_RecordingPipeline())  # type: ignore[arg-type]

    persisted = await service._save_events_to_store(
        request, _result(request), context=context
    )

    assert persisted == context.interaction_event_id
    assert len(store.batches) == 1


def test_bundle_builder_is_pure_and_validates_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_lookup = AsyncMock(side_effect=AssertionError("builder touched the store"))
    monkeypatch.setattr(event_service_module, "get_event_store", store_lookup)
    request = _request()
    context = _context(request)

    events = event_service_module.build_interaction_event_bundle(
        request, _result(request), context
    )

    assert [event.event_type for event in events][:3] == [
        "message.in",
        "srm.decision",
        "prompt.generated",
    ]
    store_lookup.assert_not_called()
    with pytest.raises(InteractionContextError):
        event_service_module.build_interaction_event_bundle(
            _request(), _result(request), context
        )


# ============================================
# Read paths expose the persisted origin
# ============================================


def _stored_event(tenant_id: UUID, event_type: str, origin: Optional[str]) -> Event:
    return Event(
        id=uuid4(),
        tenant_id=tenant_id,
        session_id=uuid4(),
        event_type=event_type,
        interaction_origin=origin,
        event_data={"content": "x"},
        event_metadata={},
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("production", InteractionOrigin.PRODUCTION),
        ("simulator", InteractionOrigin.SIMULATOR),
        ("unknown", InteractionOrigin.UNKNOWN),
        (None, InteractionOrigin.UNKNOWN),
    ],
)
async def test_detail_uses_the_tenant_tuple_and_exposes_origin(
    monkeypatch: pytest.MonkeyPatch,
    stored: Optional[str],
    expected: InteractionOrigin,
) -> None:
    tenant_id = uuid4()
    row = _stored_event(tenant_id, "message.in", stored)
    lookup = AsyncMock(return_value=row)
    bare_lookup = AsyncMock(side_effect=AssertionError("bare event_id lookup"))
    monkeypatch.setattr(EventRepository, "get_for_tenant", lookup)
    monkeypatch.setattr(EventRepository, "get_by_id", bare_lookup)
    service = EventService(pipeline=_RecordingPipeline())  # type: ignore[arg-type]

    detail = await service.get_event(tenant_id, row.id, object())  # type: ignore[arg-type]

    lookup.assert_awaited_once_with(row.id, tenant_id)
    bare_lookup.assert_not_awaited()
    assert detail.interaction_origin is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("by_session", [True, False])
async def test_listing_exposes_origin_per_event(
    monkeypatch: pytest.MonkeyPatch, by_session: bool
) -> None:
    tenant_id = uuid4()
    rows = [
        _stored_event(tenant_id, "message.in", "production"),
        _stored_event(tenant_id, "srm.decision", "simulator"),
        _stored_event(tenant_id, "session.start", None),
    ]
    finder = AsyncMock(return_value=rows)
    monkeypatch.setattr(
        EventRepository, "find_by_session" if by_session else "find_by_tenant", finder
    )
    service = EventService(pipeline=_RecordingPipeline())  # type: ignore[arg-type]

    listed = await service.list_events(
        tenant_id=tenant_id,
        session=object(),  # type: ignore[arg-type]
        session_id=uuid4() if by_session else None,
    )

    assert finder.await_args.kwargs["tenant_id"] == tenant_id
    assert [item.interaction_origin for item in listed] == [
        InteractionOrigin.PRODUCTION,
        InteractionOrigin.SIMULATOR,
        InteractionOrigin.UNKNOWN,
    ]


# ============================================
# Existing constructors stay compatible
# ============================================


def test_existing_constructors_stay_compatible() -> None:
    from src.core.pipeline import CognitivePipeline
    from src.handoff.models import HandoffReason, HandoffRequest
    from src.services.handoff_service import HandoffService

    pipeline = _RecordingPipeline()
    positional = EventService(pipeline, None)  # type: ignore[arg-type]
    keyword = EventService(pipeline=pipeline, event_repo=None)  # type: ignore[arg-type]
    assert positional._pipeline is keyword._pipeline is pipeline
    handoff = HandoffService()
    assert CognitivePipeline(handoff_service=handoff)._handoff is handoff
    request = HandoffRequest(
        tenant_id=uuid4(), session_id=uuid4(), reason=HandoffReason.USER_REQUEST
    )
    assert "context" not in request.model_dump()
