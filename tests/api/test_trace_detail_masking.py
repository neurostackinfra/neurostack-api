"""Contract tests for card-number masking on GET /v1/traces/{trace_id}.

The trace detail response must not carry an unmasked carteirinha number in
any field outside the closed list (id, timestamp, session_id,
processing_time_ms, mode, selected_context), on both the legacy path
(context/prompt stored inline on the message.in event) and the current path
(prompt.generated event of the same session, recorded right after it).
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.config.settings import settings
from src.models.event import Event
from src.models.session import Session
from src.models.tenant import Tenant
from src.observability.pii import MASK_TOKEN
from src.repositories import get_db_session

CARD = "998877665544"
CARD_SPACED = "9988 7766 5544"
CLINIC_PHONE_SOURCE = {
    "origin": "config",
    "field": "telefone",
    "title": "telefone",
    "content": "Telefone da clínica 1133224455",
}


def _clinic_headers(tenant_id: UUID) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "email": "trace-masking@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _tenant(tenant_id: UUID) -> Tenant:
    return Tenant(
        id=tenant_id,
        name=f"Trace Masking Clinic {tenant_id.hex}",
        slug=f"trace-masking-{tenant_id.hex}",
        api_key_hash=f"trace-masking-hash-{tenant_id.hex}",
        is_active=True,
        bot_active=True,
        plan_type="pilot",
        config={},
    )


def _session(tenant_id: UUID, session_id: UUID) -> Session:
    return Session(
        id=session_id,
        tenant_id=tenant_id,
        channel="whatsapp",
        cognitive_state={},
        session_metadata={},
    )


async def _seed(*rows: Any) -> None:
    session = get_db_session()
    try:
        session.add_all(list(rows))
        await session.commit()
    finally:
        await session.close()


async def _stored_created_at(event_id: UUID) -> datetime:
    session = get_db_session()
    try:
        result = await session.execute(
            select(Event.created_at).where(Event.id == event_id)
        )
        return result.scalar_one()
    finally:
        await session.close()


async def _assert_masked_contract(
    response: Any,
    *,
    trace_id: UUID,
    session_id: UUID,
    processing_time_ms: int,
) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    raw = response.text
    assert CARD not in raw
    assert CARD_SPACED not in raw
    assert MASK_TOKEN in raw

    body: dict[str, Any] = response.json()
    assert body["id"] == str(trace_id)
    assert body["session_id"] == str(session_id)
    assert body["processing_time_ms"] == processing_time_ms
    # SQLite drops the offset: compare datetimes, not text.
    assert datetime.fromisoformat(body["timestamp"]) == await _stored_created_at(
        trace_id
    )
    return body


@pytest.mark.asyncio
async def test_legacy_inline_context_and_prompt_are_masked(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    session_id = uuid4()
    trace_id = uuid4()
    created_at = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    await _seed(
        _tenant(tenant_id),
        _session(tenant_id, session_id),
        Event(
            id=trace_id,
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="message.in",
            event_data={
                "content": f"minha carteirinha é {CARD}",
                "channel": "whatsapp",
                "decision": {
                    "intent": "agendar",
                    "confidence": 0.9,
                    "reasoning": f"paciente enviou {CARD}",
                },
                "context": {
                    "session_summary": f"Paciente enviou {CARD}",
                    "recent_messages": [{"role": "user", "content": CARD}],
                },
                "prompt": {
                    "system": f"Carteirinha do paciente: {CARD}",
                    "constraints": [f"não repita {CARD}"],
                },
            },
            event_metadata={
                "processing_time_ms": 87,
                "mode": "normal",
                "note": f"cartão {CARD_SPACED}",
            },
            created_at=created_at,
        ),
        # Decoy inside the 5 s lookup window: with an inline prompt the
        # legacy path must not read it.
        Event(
            id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="prompt.generated",
            event_data={
                "system_prompt": "ISCA system prompt",
                "context_summary": "ISCA context summary",
                "suggested_response": "ISCA suggested response",
                "context_sources": [
                    {
                        "origin": "config",
                        "field": "isca",
                        "title": "isca",
                        "content": "ISCA context source",
                    }
                ],
            },
            event_metadata={},
            created_at=created_at + timedelta(seconds=2),
        ),
    )

    response = await async_client.get(
        f"/v1/traces/{trace_id}", headers=_clinic_headers(tenant_id)
    )

    body = await _assert_masked_contract(
        response, trace_id=trace_id, session_id=session_id, processing_time_ms=87
    )
    assert body["srm_decision"]["reasoning"] == f"paciente enviou {MASK_TOKEN}"
    assert body["full_context"]["session_summary"] == f"Paciente enviou {MASK_TOKEN}"
    assert body["full_prompt"]["system"] == f"Carteirinha do paciente: {MASK_TOKEN}"
    assert body["event_metadata"]["note"] == f"cartão {MASK_TOKEN}"
    assert body["event_metadata"]["processing_time_ms"] == 87
    assert body["llm_response"] is None
    assert body["selected_context"] == []
    assert "ISCA" not in response.text


@pytest.mark.asyncio
async def test_prompt_generated_path_is_masked_and_accepts_text_context(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    session_id = uuid4()
    trace_id = uuid4()
    created_at = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    context_summary = f"Paciente informou a carteirinha {CARD} do convênio"
    await _seed(
        _tenant(tenant_id),
        _session(tenant_id, session_id),
        Event(
            id=trace_id,
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="message.in",
            event_data={
                "content": CARD,
                "channel": "whatsapp",
                "decision": {"intent": "agendar", "confidence": 0.9},
            },
            event_metadata={"processing_time_ms": 123, "mode": "normal"},
            created_at=created_at,
        ),
        Event(
            id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="prompt.generated",
            event_data={
                "system_prompt": f"Você atende o paciente da carteirinha {CARD}",
                "instructions": f"Confirme a carteirinha {CARD}",
                "context_summary": context_summary,
                "suggested_response": f"Recebemos a carteirinha {CARD}",
                "constraints": [f"Nunca repita {CARD_SPACED}"],
                "context_sources": [CLINIC_PHONE_SOURCE],
            },
            event_metadata={},
            created_at=created_at + timedelta(seconds=2),
        ),
    )

    response = await async_client.get(
        f"/v1/traces/{trace_id}", headers=_clinic_headers(tenant_id)
    )

    body = await _assert_masked_contract(
        response, trace_id=trace_id, session_id=session_id, processing_time_ms=123
    )
    assert body["selected_context"] == [CLINIC_PHONE_SOURCE]
    assert "1133224455" in response.text
    assert (
        body["full_context"]
        == f"Paciente informou a carteirinha {MASK_TOKEN} do convênio"
    )
    assert body["prompt_built"] == f"Você atende o paciente da carteirinha {MASK_TOKEN}"
    assert body["llm_response"] == f"Recebemos a carteirinha {MASK_TOKEN}"
    assert body["full_prompt"]["instructions"] == f"Confirme a carteirinha {MASK_TOKEN}"
    assert body["full_prompt"]["constraints"] == [f"Nunca repita {MASK_TOKEN}"]
