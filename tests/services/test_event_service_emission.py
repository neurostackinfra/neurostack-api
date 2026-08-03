"""Emission test for the ns.atendimento_tipo_set event (convenio-particular-triage).

EventService._save_events_to_store writes an ns.atendimento_tipo_set event
(carrying the trigger message) whenever the SRM state patch sets the
atendimento_tipo slot. This lives on a DB-bound persistence path, so we drive
the method directly with get_event_store patched and capture the appended
EventData objects.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.api.schemas.events import EventPayload, EventRequest, EventType
from src.core.pipeline import PipelineResult
from src.services.event_service import EventService


def _make_request(content: str) -> EventRequest:
    return EventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content=content, channel="whatsapp", user_id="user-1"),
    )


def _make_result(request: EventRequest, slots: dict) -> PipelineResult:
    return PipelineResult(
        tenant_id=request.tenant_id,
        session_id=request.session_id,
        intent="convenio_particular",
        intent_category="scheduling",
        funnel_stage="consideration",
        action="collect_info",
        confidence=0.9,
        safety_concern=False,
        safety_category="none",
        needs_rag=False,
        handoff_required=False,
        system_prompt="sys",
        instructions="inst",
        mode="normal",
        state_patch={"slots": slots},
    )


async def _appended_events(request: EventRequest, result: PipelineResult) -> list:
    store = SimpleNamespace(append=AsyncMock())
    with patch(
        "src.services.event_service.get_event_store",
        AsyncMock(return_value=store),
    ):
        service = EventService(pipeline=object())
        await service._save_events_to_store(
            request, result, suggested_response="Você tem convênio ou é particular?"
        )
    return [call.args[0] for call in store.append.await_args_list]


def _tipo_events(events: list) -> list:
    return [e for e in events if e.event_type == "ns.atendimento_tipo_set"]


class TestAtendimentoTipoSetEvent:
    @pytest.mark.asyncio
    async def test_emitted_with_trigger_message_when_slot_set(self):
        request = _make_request("vai ser pelo convênio")
        result = _make_result(request, {"atendimento_tipo": "convenio"})

        tipo = _tipo_events(await _appended_events(request, result))

        assert len(tipo) == 1
        ev = tipo[0]
        assert ev.event_data["atendimento_tipo"] == "convenio"
        assert ev.event_data["trigger_message"] == "vai ser pelo convênio"
        assert ev.tenant_id == request.tenant_id
        assert ev.session_id == request.session_id

    @pytest.mark.asyncio
    async def test_particular_value_carried_through(self):
        request = _make_request("é particular")
        result = _make_result(request, {"atendimento_tipo": "particular"})

        tipo = _tipo_events(await _appended_events(request, result))

        assert len(tipo) == 1
        assert tipo[0].event_data["atendimento_tipo"] == "particular"

    @pytest.mark.asyncio
    async def test_not_emitted_when_slot_absent(self):
        request = _make_request("quero agendar")
        result = _make_result(request, {})  # patch without atendimento_tipo

        tipo = _tipo_events(await _appended_events(request, result))

        assert tipo == []
