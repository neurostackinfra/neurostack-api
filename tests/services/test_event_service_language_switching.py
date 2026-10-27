"""Per-turn language orchestration at the EventService boundary."""

from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.api.schemas.events import EventPayload, EventRequest, EventType
from src.core.pipeline import PipelineInput, PipelineResult
from src.models.session import CognitiveStateData, Session
from src.services.conversation_language import ENGLISH_PROMPT, PORTUGUESE_PROMPT
from src.services.event_service import EventService


def _request(
    tenant_id: UUID,
    session_id: UUID,
    text: str,
    *,
    untrusted_language: str = "pt-BR",
) -> EventRequest:
    return EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(
            content=text,
            channel="whatsapp",
            user_id="5511999999999",
            language=untrusted_language,
        ),
    )


def _row(
    tenant_id: UUID,
    session_id: UUID,
    state: CognitiveStateData,
) -> Session:
    return Session(
        id=session_id,
        tenant_id=tenant_id,
        user_id="5511999999999",
        channel="whatsapp",
        cognitive_state=state.to_dict(),
        session_metadata={},
        message_count=0,
        last_activity_at=datetime.now(timezone.utc),
    )


def _result(
    input_data: PipelineInput,
    *,
    safety: bool = False,
    handoff: bool = False,
    scheduling: bool = False,
    deterministic_response: str | None = None,
) -> PipelineResult:
    return PipelineResult(
        tenant_id=input_data.tenant_id,
        session_id=input_data.session_id,
        intent="agendamento" if scheduling else "duvida_generica",
        intent_category="scheduling" if scheduling else "question",
        funnel_stage="lead_novo",
        action="handoff" if handoff else "respond",
        confidence=0.95,
        safety_concern=safety,
        safety_category="clinical_risk" if safety else "none",
        needs_rag=False,
        handoff_required=handoff,
        handoff_reason="user_request" if handoff else None,
        system_prompt="system",
        instructions="instructions",
        deterministic_response=deterministic_response,
        mode="handoff" if handoff else ("safety" if safety else "normal"),
        state_patch={"slots": {}, "flags": {}},
    )


class _PipelineStub:
    def __init__(self, **result_options: Any) -> None:
        self.inputs: list[PipelineInput] = []
        self._result_options = result_options

    async def process(
        self, input_data: PipelineInput, *, context: Any = None
    ) -> PipelineResult:
        self.inputs.append(input_data)
        return _result(input_data, **self._result_options)


async def _run_turn(
    *,
    state: CognitiveStateData,
    text: str,
    configured_language: object,
    untrusted_language: str,
    **result_options: Any,
) -> tuple[PipelineResult, PipelineInput, CognitiveStateData]:
    tenant_id = uuid4()
    session_id = uuid4()
    row = _row(tenant_id, session_id, state)
    pipeline = _PipelineStub(**result_options)
    service = EventService(pipeline=cast(Any, pipeline))
    setattr(
        service,
        "_load_clinic_config",
        AsyncMock(
            return_value={"business_rules": {"agent_language": configured_language}}
        ),
    )
    setattr(service, "_lock_session_state", AsyncMock(return_value=(row, state)))
    setattr(service, "_load_message_history", AsyncMock(return_value=[]))
    db = AsyncMock()

    result, _, _ = await service.run_pipeline(
        _request(
            tenant_id,
            session_id,
            text,
            untrusted_language=untrusted_language,
        ),
        session=db,
    )

    db.flush.assert_awaited_once()
    return result, pipeline.inputs[0], row.get_cognitive_state()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "text", "candidate", "expected_prompt"),
    [
        (
            "pt-BR",
            "Hello, I would like to schedule an appointment tomorrow please.",
            "en",
            ENGLISH_PROMPT,
        ),
        (
            "en",
            "Olá, eu gostaria de agendar uma consulta amanhã por favor.",
            "pt-BR",
            PORTUGUESE_PROMPT,
        ),
    ],
)
@pytest.mark.asyncio
async def test_proposal_uses_candidate_for_canonical_question_only(
    active: str,
    text: str,
    candidate: str,
    expected_prompt: str,
) -> None:
    result, pipeline_input, persisted = await _run_turn(
        state=CognitiveStateData(active_language=active),
        text=text,
        configured_language=active,
        untrusted_language=candidate,
    )

    assert pipeline_input.detected_language == candidate
    assert pipeline_input.analysis_language == candidate
    assert pipeline_input.active_language == active
    assert result.response_language == candidate
    assert result.deterministic_response == expected_prompt
    assert persisted.active_language == active
    assert persisted.pending_language == candidate


@pytest.mark.asyncio
async def test_canonical_confirmation_switches_analysis_output_and_state() -> None:
    result, pipeline_input, persisted = await _run_turn(
        state=CognitiveStateData(active_language="pt-BR", pending_language="en"),
        text="yes please",
        configured_language="pt-BR",
        untrusted_language="pt-BR",
    )

    assert pipeline_input.detected_language is None
    assert pipeline_input.analysis_language == "en"
    assert pipeline_input.active_language == "en"
    assert result.response_language == "en"
    assert result.deterministic_response is None
    assert persisted.active_language == "en"
    assert persisted.pending_language is None


@pytest.mark.asyncio
async def test_request_language_never_overrides_fail_closed_clinic_config() -> None:
    result, pipeline_input, persisted = await _run_turn(
        state=CognitiveStateData(),
        text="short input",
        configured_language={"language": "en"},
        untrusted_language="en",
    )

    assert pipeline_input.analysis_language == "pt-BR"
    assert pipeline_input.active_language == "pt-BR"
    assert result.response_language == "pt-BR"
    assert persisted.active_language == "pt-BR"


@pytest.mark.parametrize(
    ("active", "candidate_text", "unrelated_confirmation", "expected_prompt"),
    [
        (
            "pt-BR",
            "Hello, I need urgent help from your specialist team today.",
            "yes",
            ENGLISH_PROMPT,
        ),
        (
            "en",
            "Olá, preciso de ajuda urgente da sua equipe especializada hoje.",
            "sim",
            PORTUGUESE_PROMPT,
        ),
    ],
)
@pytest.mark.parametrize("preemption", ["safety", "handoff"])
@pytest.mark.asyncio
async def test_safety_or_team_preemption_leaves_no_invisible_pending_on_next_turn(
    active: str,
    candidate_text: str,
    unrelated_confirmation: str,
    expected_prompt: str,
    preemption: str,
) -> None:
    first_result, first_input, first_state = await _run_turn(
        state=CognitiveStateData(active_language=active),
        text=candidate_text,
        configured_language=active,
        untrusted_language="en" if active == "pt-BR" else "pt-BR",
        safety=preemption == "safety",
        handoff=preemption == "handoff",
    )

    assert first_input.analysis_language != active
    assert first_result.deterministic_response != expected_prompt
    assert first_result.response_language == active
    assert first_state.active_language == active
    assert first_state.pending_language is None

    second_result, second_input, second_state = await _run_turn(
        state=first_state,
        text=unrelated_confirmation,
        configured_language=active,
        untrusted_language="en" if active == "pt-BR" else "pt-BR",
    )

    assert second_input.active_language == active
    assert second_result.response_language == active
    assert second_state.active_language == active
    assert second_state.pending_language is None


@pytest.mark.asyncio
async def test_generic_scheduling_intent_does_not_discard_proposal() -> None:
    result, _, persisted = await _run_turn(
        state=CognitiveStateData(active_language="pt-BR"),
        text="Hello, I would like to schedule an appointment tomorrow please.",
        configured_language="pt-BR",
        untrusted_language="en",
        scheduling=True,
    )

    assert result.deterministic_response == ENGLISH_PROMPT
    assert result.response_language == "en"
    assert persisted.active_language == "pt-BR"
    assert persisted.pending_language == "en"


@pytest.mark.asyncio
async def test_existing_deterministic_winner_discards_proposal() -> None:
    result, _, persisted = await _run_turn(
        state=CognitiveStateData(active_language="pt-BR"),
        text="Hello, I would like to schedule an appointment tomorrow please.",
        configured_language="pt-BR",
        untrusted_language="en",
        deterministic_response="existing deterministic winner",
    )

    assert result.deterministic_response == "existing deterministic winner"
    assert result.response_language == "pt-BR"
    assert persisted.active_language == "pt-BR"
    assert persisted.pending_language is None


@pytest.mark.parametrize(
    ("active", "pending", "confirmation"),
    [("pt-BR", "en", "yes"), ("en", "pt-BR", "sim")],
)
@pytest.mark.asyncio
async def test_handoff_confirmation_pending_precedes_language_confirmation(
    active: str,
    pending: str,
    confirmation: str,
) -> None:
    result, pipeline_input, persisted = await _run_turn(
        state=CognitiveStateData(
            active_language=active,
            pending_language=pending,
            flags={"handoff_confirmation_pending": True},
        ),
        text=confirmation,
        configured_language=active,
        untrusted_language=pending,
    )

    assert pipeline_input.message == confirmation
    assert pipeline_input.flags["handoff_confirmation_pending"] is True
    assert pipeline_input.active_language == active
    assert pipeline_input.analysis_language == active
    assert result.response_language == active
    assert persisted.active_language == active
    assert persisted.pending_language is None
