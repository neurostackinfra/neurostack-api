"""
NeuroStack API — WhatsApp Webhook Receivers
Receives WhatsApp messages from Z-API and Evolution API, processes them
through the cognitive pipeline, and sends the LLM response back.
"""

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.events import EventPayload, EventRequest
from src.models.clinic_config_table import ClinicConfigTable
from src.repositories import SessionRepository, get_db_session_dependency
from src.services.event_service import get_event_service
from src.services.zapi_service import send_evolution_text, send_text_message

logger = logging.getLogger(__name__)

router = APIRouter()


# ── helpers ──────────────────────────────────────────────────────────

async def _resolve_tenant_by_credential(
    key: str,
    value: str,
    session: AsyncSession,
) -> Optional[UUID]:
    """Find the tenant whose api_credentials[key] matches value."""
    stmt = select(ClinicConfigTable.tenant_id).where(
        ClinicConfigTable.api_credentials[key].astext == value,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _normalise_phone(raw: str) -> str:
    """Strip non-digits and leading '+' so we get a clean E.164-ish number."""
    return raw.lstrip("+").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")


async def _get_or_create_session(
    tenant_id: UUID,
    phone: str,
    db: AsyncSession,
) -> UUID:
    """Return an active session for this phone, or create one (race-safe)."""
    repo = SessionRepository(db)
    sessions = await repo.find_by_user(
        tenant_id=tenant_id,
        user_id=phone,
        active_only=True,
        limit=1,
    )
    if sessions:
        return sessions[0].id

    from src.models.session import CognitiveStateData
    initial_state = CognitiveStateData(
        funnel_stage="lead_novo",
        intent_history=[],
        slots={},
        flags={},
        turn_count=0,
        safety_triggers=0,
    )
    try:
        new_session = await repo.create(
            tenant_id=tenant_id,
            user_id=phone,
            channel="whatsapp",
            cognitive_state=initial_state.to_dict(),
            session_metadata={"phone": phone, "source": "webhook"},
        )
        await db.flush()
        return new_session.id
    except Exception:
        # Race condition: another request created the session concurrently
        await db.rollback()
        sessions = await repo.find_by_user(
            tenant_id=tenant_id,
            user_id=phone,
            active_only=True,
            limit=1,
        )
        if sessions:
            return sessions[0].id
        raise


async def _load_tenant_credentials(
    tenant_id: UUID,
    db: AsyncSession,
) -> dict:
    """Load api_credentials for a tenant."""
    stmt = select(ClinicConfigTable.api_credentials).where(
        ClinicConfigTable.tenant_id == tenant_id,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() or {}


# ── Z-API webhook endpoint ──────────────────────────────────────────

@router.post(
    "/zapi",
    summary="Z-API Webhook",
    description="Receives incoming WhatsApp messages from Z-API and responds automatically.",
    tags=["Webhook"],
    status_code=200,
)
async def zapi_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
):
    """
    Z-API sends a POST for every incoming message.

    Expected payload (text message):
    {
      "instanceId": "...",
      "phone": "5511999999999",
      "messageId": "...",
      "fromMe": false,
      "text": { "message": "Olá" },
      "momment": 1711900000000,
      "senderName": "João"
    }
    """
    body: dict[str, Any] = await request.json()

    # ── ignore outgoing / non-text / status events ──
    if body.get("fromMe", False):
        return {"ok": True, "skipped": "outgoing"}

    text_obj = body.get("text")
    if not text_obj or not isinstance(text_obj, dict):
        return {"ok": True, "skipped": "non-text"}

    message_text = text_obj.get("message", "").strip()
    if not message_text:
        return {"ok": True, "skipped": "empty"}

    instance_id = body.get("instanceId", "")
    phone = _normalise_phone(body.get("phone", ""))
    sender_name = body.get("senderName", "")

    if not instance_id or not phone:
        return {"ok": True, "skipped": "missing_fields"}

    # ── resolve tenant from instance_id ──
    tenant_id = await _resolve_tenant_by_credential("zapi_instance_id", instance_id, db)
    if not tenant_id:
        logger.warning("Z-API webhook: no tenant for instance %s", instance_id)
        return {"ok": True, "skipped": "unknown_instance"}

    # ── get or create a session for this phone ──
    session_id = await _get_or_create_session(tenant_id, phone, db)
    await db.commit()

    # ── process through NeuroStack pipeline ──
    event_request = EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type="message.in",
        payload=EventPayload(
            content=message_text,
            channel="whatsapp",
            user_id=phone,
            metadata={
                "phone": phone,
                "senderName": sender_name,
                "messageId": body.get("messageId", ""),
                "source": "zapi",
            },
        ),
    )

    service = get_event_service()
    response = await service.process_event(event_request, session=db)

    # ── send LLM response back via Z-API ──
    suggested = (
        response.prompt.suggested_response
        if response.prompt and response.prompt.suggested_response
        else None
    )

    if not suggested:
        logger.info("Z-API webhook: no suggested_response for %s", phone)
        return {"ok": True, "replied": False}

    creds = await _load_tenant_credentials(tenant_id, db)
    zapi_token = creds.get("zapi_token", "")
    zapi_inst = creds.get("zapi_instance_id", "")

    if not zapi_token or not zapi_inst:
        logger.warning("Z-API credentials missing for tenant %s", tenant_id)
        return {"ok": True, "replied": False, "reason": "no_credentials"}

    try:
        await send_text_message(zapi_inst, zapi_token, phone, suggested)
        logger.info("Z-API reply sent to %s (%d chars)", phone, len(suggested))
    except Exception:
        logger.exception("Z-API send failed for %s", phone)
        return {"ok": True, "replied": False, "reason": "send_failed"}

    return {"ok": True, "replied": True}


# ── Evolution API webhook endpoint ───────────────────────────────────

@router.post(
    "/evolution",
    summary="Evolution API Webhook",
    description="Receives incoming WhatsApp messages from Evolution API and responds automatically.",
    tags=["Webhook"],
    status_code=200,
)
async def evolution_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
):
    """
    Evolution API sends a POST for every event.

    Expected payload (messages.upsert):
    {
      "event": "messages.upsert",
      "instance": "my-instance",
      "data": {
        "key": { "remoteJid": "5511999999999@s.whatsapp.net", "fromMe": false, "id": "..." },
        "message": { "conversation": "Olá" },
        "pushName": "João"
      }
    }
    """
    body: dict[str, Any] = await request.json()

    # ── only handle messages.upsert ──
    event_type = body.get("event", "")
    if event_type != "messages.upsert":
        return {"ok": True, "skipped": "non_message_event"}

    data = body.get("data", {})
    if not isinstance(data, dict):
        return {"ok": True, "skipped": "invalid_data"}

    key = data.get("key", {})

    # ── ignore outgoing ──
    if key.get("fromMe", False):
        return {"ok": True, "skipped": "outgoing"}

    # ── extract message text ──
    msg_obj = data.get("message", {})
    message_text = (
        msg_obj.get("conversation")
        or msg_obj.get("extendedTextMessage", {}).get("text", "")
    ).strip() if isinstance(msg_obj, dict) else ""

    if not message_text:
        return {"ok": True, "skipped": "non-text"}

    # ── extract phone from remoteJid ──
    remote_jid = key.get("remoteJid", "")
    phone = _normalise_phone(remote_jid.split("@")[0]) if "@" in remote_jid else ""
    sender_name = data.get("pushName", "")
    instance_name = body.get("instance", "")

    if not instance_name or not phone:
        return {"ok": True, "skipped": "missing_fields"}

    # ── resolve tenant from instance_name ──
    tenant_id = await _resolve_tenant_by_credential("evolution_instance_name", instance_name, db)
    if not tenant_id:
        logger.warning("Evolution webhook: no tenant for instance %s", instance_name)
        return {"ok": True, "skipped": "unknown_instance"}

    # ── get or create a session for this phone ──
    session_id = await _get_or_create_session(tenant_id, phone, db)
    await db.commit()

    # ── process through NeuroStack pipeline ──
    event_request = EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type="message.in",
        payload=EventPayload(
            content=message_text,
            channel="whatsapp",
            user_id=phone,
            metadata={
                "phone": phone,
                "pushName": sender_name,
                "messageId": key.get("id", ""),
                "source": "evolution",
            },
        ),
    )

    service = get_event_service()
    response = await service.process_event(event_request, session=db)

    # ── send LLM response back via Evolution API ──
    suggested = (
        response.prompt.suggested_response
        if response.prompt and response.prompt.suggested_response
        else None
    )

    if not suggested:
        logger.info("Evolution webhook: no suggested_response for %s", phone)
        return {"ok": True, "replied": False}

    creds = await _load_tenant_credentials(tenant_id, db)
    evol_url = creds.get("evolution_api_url", "")
    evol_name = creds.get("evolution_instance_name", "")
    evol_key = creds.get("evolution_api_key", "")

    if not evol_url or not evol_name or not evol_key:
        logger.warning("Evolution credentials missing for tenant %s", tenant_id)
        return {"ok": True, "replied": False, "reason": "no_credentials"}

    try:
        await send_evolution_text(evol_url, evol_name, evol_key, phone, suggested)
        logger.info("Evolution reply sent to %s (%d chars)", phone, len(suggested))
    except Exception:
        logger.exception("Evolution send failed for %s", phone)
        return {"ok": True, "replied": False, "reason": "send_failed"}

    return {"ok": True, "replied": True}
