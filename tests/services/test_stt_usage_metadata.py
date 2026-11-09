"""A transcribed audio's usage metadata reaches the usage cards.

The webhook helper, the event bundle and the trace stats share three metadata
keys by name only (``whisper_model``, ``is_audio``, ``audio_seconds``). This
pins them end to end, so renaming them on any side fails here instead of
silently emptying the "🎙️" line of the clinic and builder usage cards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import AsyncClient

from src.api.routes.webhook import _stt_usage_metadata
from src.api.schemas.events import CanonicalEventRequest, EventPayload, EventType
from src.config.settings import settings
from src.core.pipeline import PipelineResult
from src.interaction_context import InteractionOrigin, build_interaction_context
from src.models.event import Event
from src.models.tenant import Tenant
from src.repositories import get_db_session
from src.services.event_service import build_interaction_event_bundle


def _clinic_headers(tenant_id: UUID) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "email": "stt-usage@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _message_in(
    tenant_id: Any, metadata: dict[str, Any], content: str = "quero remarcar"
) -> Event:
    """The stored ``message.in`` row of one interaction, as the event bundle
    builds it from the webhook's payload metadata."""

    request = CanonicalEventRequest(
        tenant_id=tenant_id,
        session_id=uuid4(),
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(
            content=content,
            channel="whatsapp",
            user_id="5511900001234",
            metadata=metadata,
        ),
    )
    context = build_interaction_context(
        effective_tenant_id=tenant_id,
        session_id=request.session_id,
        origin=InteractionOrigin.PRODUCTION,
        event_type=request.event_type,
    )
    result = PipelineResult(
        tenant_id=tenant_id,
        session_id=request.session_id,
        intent="agendar",
        intent_category="scheduling",
        funnel_stage="em_qualificacao",
        action="provide_info",
        confidence=0.9,
        safety_concern=False,
        safety_category="none",
        needs_rag=False,
        handoff_required=False,
        system_prompt="sys",
        instructions="inst",
        mode="normal",
        state_patch={"slots": {}},
    )
    (event,) = [
        row
        for row in build_interaction_event_bundle(request, result, context)
        if row.event_type == "message.in"
    ]
    return Event(
        tenant_id=tenant_id,
        session_id=event.session_id,
        event_type=event.event_type,
        event_data=event.event_data,
        event_metadata=event.event_metadata,
    )


@pytest.mark.asyncio
async def test_transcribed_audio_is_counted_by_model_and_seconds(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    credentials = {"stt_provider": "openai_whisper", "stt_api_key": "sk-test"}
    events = [
        _message_in(
            tenant_id,
            {"source": "uazapi", **_stt_usage_metadata(credentials, audio_seconds=37)},
        ),
        # A Meta audio without a measured duration records 0 seconds and is
        # still counted, as one second.
        _message_in(
            tenant_id,
            {
                "source": "meta_cloud",
                **_stt_usage_metadata(credentials, audio_seconds=0),
            },
        ),
        # A text message carries none of the keys and adds no audio line.
        _message_in(tenant_id, {"source": "meta_cloud"}, content="oi"),
    ]
    session = get_db_session()
    try:
        session.add(
            Tenant(
                id=tenant_id,
                name=f"STT usage {tenant_id.hex[:8]}",
                slug=f"stt-usage-{tenant_id.hex}",
                api_key_hash=f"stt-usage-{tenant_id.hex}",
                is_active=True,
                bot_active=True,
                plan_type="pilot",
                config={},
            )
        )
        session.add_all(events)
        await session.commit()
    finally:
        await session.close()

    response = await async_client.get(
        "/v1/traces/stats", headers=_clinic_headers(tenant_id)
    )

    assert response.status_code == 200, response.text
    audio_lines = [
        line
        for line in response.json()["model_breakdown"]
        if line["model"].startswith("🎙️")
    ]
    assert audio_lines == [{"model": "🎙️ whisper-1", "tokens": 38}]
