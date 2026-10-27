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


def _make_result(
    request: EventRequest,
    slots: dict,
    *,
    context_episode_id: str | None = None,
) -> PipelineResult:
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
        context_episode_id=context_episode_id,
    )


async def _appended_events(request: EventRequest, result: PipelineResult) -> list:
    store = SimpleNamespace(append_batch=AsyncMock())
    with patch(
        "src.services.event_service.get_event_store",
        AsyncMock(return_value=store),
    ):
        service = EventService(pipeline=object())
        await service._save_events_to_store(
            request, result, suggested_response="Você tem convênio ou é particular?"
        )
    store.append_batch.assert_awaited_once()
    return list(store.append_batch.await_args.args[0])


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


class TestContextEpisodeIdEventTagging:
    """Wish ``handoff-contextual-resumption`` Group 3 deliverable 1 /
    AC A1 — ``message.in`` and ``prompt.generated`` carry the current
    ``context_episode_id`` in ``event_metadata`` without dropping any
    existing field.
    """

    @pytest.mark.asyncio
    async def test_message_in_metadata_tags_context_episode_id(self) -> None:
        request = _make_request("oi")
        episode = "episode-abc-123"
        result = _make_result(request, {}, context_episode_id=episode)

        events = await _appended_events(request, result)
        message_in = next(e for e in events if e.event_type == "message.in")
        md = message_in.event_metadata

        assert md["context_episode_id"] == episode
        # Existing fields must still be present — the tag is ADDED, not
        # a replacement of the audit metadata.
        for existing in (
            "processing_time_ms",
            "mode",
            "safety_concern",
            "handoff_required",
            "intent",
            "funnel_stage",
            "llm_model",
            "llm_usage",
            "llm_attempts",
            "llm_usage_total",
        ):
            assert existing in md, f"existing metadata field {existing!r} dropped"

    @pytest.mark.asyncio
    async def test_prompt_generated_metadata_tags_context_episode_id(self) -> None:
        request = _make_request("oi")
        episode = "episode-xyz-999"
        result = _make_result(request, {}, context_episode_id=episode)

        events = await _appended_events(request, result)
        prompt_ev = next(e for e in events if e.event_type == "prompt.generated")
        md = prompt_ev.event_metadata

        assert md["context_episode_id"] == episode
        # Existing prompt.generated metadata fields untouched.
        for existing in ("processing_time_ms", "processed_at", "stage_timings"):
            assert existing in md, f"existing metadata field {existing!r} dropped"

    @pytest.mark.asyncio
    async def test_legacy_untagged_result_persists_none_tag(self) -> None:
        """Legacy sessions (no reactivation yet) persist ``None`` for the
        tag — the audit trail is intact, just un-filterable."""
        request = _make_request("oi")
        result = _make_result(request, {})  # context_episode_id=None by default

        events = await _appended_events(request, result)
        message_in = next(e for e in events if e.event_type == "message.in")
        prompt_ev = next(e for e in events if e.event_type == "prompt.generated")

        assert message_in.event_metadata["context_episode_id"] is None
        assert prompt_ev.event_metadata["context_episode_id"] is None
