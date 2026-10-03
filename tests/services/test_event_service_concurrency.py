"""Transaction and failure boundaries for serialized event processing."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.webhook import _run_pipeline_transactionally
from src.api.schemas.events import EventPayload, EventRequest, EventType
from src.core.pipeline import PipelineInput, PipelineResult
from src.models.session import CognitiveStateData, Session
from src.services.event_service import EventService


def _request() -> EventRequest:
    return EventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content="short input", channel="whatsapp"),
    )


def _result(input_data: PipelineInput) -> PipelineResult:
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


@pytest.mark.asyncio
async def test_lock_precedes_pipeline_and_existing_state_flush() -> None:
    request = _request()
    state = CognitiveStateData(turn_count=7, active_language="pt-BR")
    row = Session(
        id=request.session_id,
        tenant_id=request.tenant_id,
        channel="whatsapp",
        cognitive_state=state.to_dict(),
        session_metadata={},
        message_count=0,
        last_activity_at=datetime.now(timezone.utc),
    )
    calls: list[str] = []

    class Pipeline:
        async def process(self, input_data: PipelineInput) -> PipelineResult:
            calls.append("pipeline")
            assert input_data.turn_count == 7
            return _result(input_data)

    service = EventService(pipeline=cast(Any, Pipeline()))

    async def lock(
        *_args: object, **_kwargs: object
    ) -> tuple[Session, CognitiveStateData]:
        calls.append("lock")
        return row, state

    async def persist(*_args: object, **kwargs: object) -> None:
        calls.append("persist")
        assert kwargs["session_row"] is row
        assert kwargs["cognitive"] is state

    setattr(service, "_load_clinic_config", AsyncMock(return_value={}))
    setattr(service, "_lock_session_state", lock)
    setattr(service, "_load_message_history", AsyncMock(return_value=[]))
    setattr(service, "_persist_session_state", persist)

    await service.run_pipeline(request, session=AsyncMock())

    assert calls == ["lock", "pipeline", "persist"]


@pytest.mark.asyncio
async def test_webhook_helper_rolls_back_pipeline_failure_without_finish() -> None:
    request = _request()
    error = RuntimeError("pipeline failed")
    service = SimpleNamespace(
        run_pipeline=AsyncMock(side_effect=error),
        finish_response=AsyncMock(),
    )
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    with pytest.raises(RuntimeError, match="pipeline failed"):
        await _run_pipeline_transactionally(
            service=service,
            event_request=request,
            db=cast(AsyncSession, db),
        )

    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    service.finish_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_helper_rolls_back_commit_failure_without_finish() -> None:
    request = _request()
    phase: tuple[Any, dict[str, object], list[dict[str, str]]] = (
        SimpleNamespace(),
        {},
        [],
    )
    service = SimpleNamespace(
        run_pipeline=AsyncMock(return_value=phase),
        finish_response=AsyncMock(),
    )
    db = SimpleNamespace(
        commit=AsyncMock(side_effect=RuntimeError("commit failed")),
        rollback=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await _run_pipeline_transactionally(
            service=service,
            event_request=request,
            db=cast(AsyncSession, db),
        )

    db.rollback.assert_awaited_once()
    service.finish_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_helper_does_not_rollback_post_commit_failure() -> None:
    request = _request()
    phase: tuple[Any, dict[str, object], list[dict[str, str]]] = (
        SimpleNamespace(),
        {},
        [],
    )
    service = SimpleNamespace(
        run_pipeline=AsyncMock(return_value=phase),
        finish_response=AsyncMock(side_effect=RuntimeError("egress failed")),
    )
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    with pytest.raises(RuntimeError, match="egress failed"):
        await _run_pipeline_transactionally(
            service=service,
            event_request=request,
            db=cast(AsyncSession, db),
        )

    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    service.finish_response.assert_awaited_once_with(
        request,
        phase[0],
        phase[1],
        phase[2],
    )
