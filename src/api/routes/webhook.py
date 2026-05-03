"""
NeuroStack API — WhatsApp Webhook Receivers
Receives WhatsApp messages from Z-API and Evolution API, processes them
through the cognitive pipeline, and sends the LLM response back.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.events import EventPayload, EventRequest
from src.models.clinic_config_table import ClinicConfigTable
from src.repositories import SessionRepository, get_db_session_dependency
from src.services.event_service import get_event_service
from src.services.zapi_service import send_evolution_text, send_text_message

logger = logging.getLogger(__name__)

router = APIRouter()

# Track message IDs sent by the AI so we can ignore their fromMe=true echo.
# Uses Redis for persistence across container restarts and multiple workers.
_SENT_ID_PREFIX = "ai_sent:"
_SENT_ID_TTL = 300  # 5 minutes — enough for Z-API echo to arrive


def _get_redis():
    """Get Redis client (lazy import to avoid circular deps)."""
    try:
        from src.config.settings import settings
        import redis
        return redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        return None


def _track_sent_message(zapi_response: dict) -> None:
    """Store the messageId from a Z-API send response in Redis."""
    mid = zapi_response.get("messageId") or zapi_response.get("zapiMessageId")
    if not mid:
        return
    r = _get_redis()
    if r:
        try:
            r.setex(f"{_SENT_ID_PREFIX}{mid}", _SENT_ID_TTL, "1")
        except Exception:
            pass


def _is_ai_sent_message(message_id: str) -> bool:
    """Check if a messageId was sent by the AI (stored in Redis)."""
    r = _get_redis()
    if r:
        try:
            key = f"{_SENT_ID_PREFIX}{message_id}"
            if r.exists(key):
                r.delete(key)
                return True
        except Exception:
            pass
    return False


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


async def _load_handoff_config(
    tenant_id: UUID,
    db: AsyncSession,
) -> dict:
    """Load handoff config bucket for a tenant."""
    stmt = select(ClinicConfigTable.handoff).where(
        ClinicConfigTable.tenant_id == tenant_id,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() or {}


async def _check_pause_state(
    tenant_id: UUID,
    phone: str,
    db: AsyncSession,
) -> Optional[dict]:
    """Check if session is paused. Returns action dict or None to continue.

    Implements lazy unpause: if paused_until has expired, unpauses the session.
    """
    repo = SessionRepository(db)
    sessions = await repo.find_by_user(
        tenant_id=tenant_id,
        user_id=phone,
        active_only=True,
        limit=1,
    )
    if not sessions:
        return None

    session = sessions[0]
    if not session.is_paused:
        return None

    now = datetime.now(timezone.utc)

    # Lazy unpause: paused_until expired
    if session.paused_until and session.paused_until <= now:
        session.is_paused = False
        session.paused_until = None
        await db.flush()
        logger.info(
            "session_auto_unpause",
            extra={
                "session_id": str(session.id),
                "tenant_id": str(tenant_id),
                "phone": phone,
            },
        )
        return None  # Continue processing

    # Still paused
    logger.info(
        "message_skipped_paused",
        extra={
            "session_id": str(session.id),
            "tenant_id": str(tenant_id),
            "phone": phone,
            "paused_until": session.paused_until.isoformat() if session.paused_until else "permanent",
        },
    )
    return {"ok": True, "skipped": "session_paused"}


async def _handle_clinic_message(
    tenant_id: UUID,
    phone: str,
    message_text: str,
    db: AsyncSession,
) -> dict:
    """Handle fromMe=true messages (clinic/secretary).

    Never enters pipeline/SRM. Checks for trigger message or human takeover.
    """
    repo = SessionRepository(db)
    sessions = await repo.find_by_user(
        tenant_id=tenant_id,
        user_id=phone,
        active_only=True,
        limit=1,
    )
    if not sessions:
        return {"ok": True, "action": "no_session"}

    session = sessions[0]
    handoff_cfg = await _load_handoff_config(tenant_id, db)

    handoff_active = str(handoff_cfg.get("handoff_active", "yes")).lower()
    trigger_msg = " ".join(str(handoff_cfg.get(
        "trigger_message",
        "Precisando de algo mais,estamos a disposição.",
    )).strip().lower().split())
    reactivation_hours = int(handoff_cfg.get("reactivation_delay_hours", 1))

    normalized_text = " ".join(message_text.strip().lower().split())

    # Check if message contains the trigger phrase (flexible match, space-tolerant)
    if trigger_msg in normalized_text:
        if session.is_paused:
            session.paused_until = datetime.now(timezone.utc) + timedelta(hours=reactivation_hours)
            await db.flush()
            logger.info(
                "trigger_reactivation",
                extra={
                    "event": "trigger_reactivation",
                    "session_id": str(session.id),
                    "tenant_id": str(tenant_id),
                    "phone": phone,
                    "reactivates_at": session.paused_until.isoformat(),
                },
            )
            return {"ok": True, "action": "trigger_reactivation"}
        return {"ok": True, "action": "already_active"}

    # Not trigger message — check for human takeover
    if handoff_active != "yes":
        return {"ok": True, "skipped": "handoff_inactive"}

    if session.is_paused:
        return {"ok": True, "action": "already_paused"}

    # Human takeover: pause session (no mark-unread — secretary is in conversation)
    session.is_paused = True

    handoff_period = str(handoff_cfg.get("handoff_period", "permanent")).lower()
    if handoff_period == "cycle":
        cycle_days = int(handoff_cfg.get("handoff_cycle_days", 7))
        session.paused_until = datetime.now(timezone.utc) + timedelta(days=cycle_days)
    else:
        session.paused_until = None

    await db.flush()
    logger.info(
        "session_paused",
        extra={
            "event": "session_paused",
            "reason": "human_takeover",
            "session_id": str(session.id),
            "tenant_id": str(tenant_id),
            "phone": phone,
            "paused_until": session.paused_until.isoformat() if session.paused_until else None,
        },
    )
    return {"ok": True, "action": "human_takeover"}


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

    # ── extract text from payload ──
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

    # ── fromMe=true: check if AI-sent echo or real clinic message ──
    if body.get("fromMe", False):
        webhook_msg_id = body.get("messageId", "")
        if webhook_msg_id and _is_ai_sent_message(webhook_msg_id):
            return {"ok": True, "skipped": "ai_echo"}
        result = await _handle_clinic_message(tenant_id, phone, message_text, db)
        await db.commit()
        return result

    # ── pause check for patient messages ──
    pause_result = await _check_pause_state(tenant_id, phone, db)
    if pause_result:
        await db.commit()
        return JSONResponse(status_code=202, content=pause_result)

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
    await db.commit()

    # ── send LLM response back via Z-API ──
    suggested = (
        response.prompt.suggested_response
        if response.prompt and response.prompt.suggested_response
        else None
    )

    if not suggested:
        logger.info("Z-API webhook: no suggested_response for %s", phone)
        return {"ok": True, "replied": False, "reason": "no_llm_response"}

    logger.info("Z-API webhook: LLM generated %d chars for %s", len(suggested), phone)

    creds = await _load_tenant_credentials(tenant_id, db)
    zapi_token = creds.get("zapi_token", "").strip()
    zapi_inst = creds.get("zapi_instance_id", "").strip()
    zapi_client_token = creds.get("zapi_client_token", "").strip()

    if not zapi_token or not zapi_inst:
        logger.warning("Z-API credentials missing for tenant %s", tenant_id)
        return {"ok": True, "replied": False, "reason": "no_credentials"}

    try:
        send_result = await send_text_message(zapi_inst, zapi_token, phone, suggested, client_token=zapi_client_token)
        _track_sent_message(send_result)
        logger.info("Z-API reply sent to %s (%d chars)", phone, len(suggested))
    except Exception as exc:
        logger.exception("Z-API send failed for %s — %s", phone, exc)
        return {"ok": True, "replied": False, "reason": "send_failed", "error": str(exc)}

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
    is_from_me = key.get("fromMe", False)

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

    # ── fromMe=true: check if AI-sent echo or real clinic message ──
    if is_from_me:
        webhook_msg_id = key.get("id", "")
        if webhook_msg_id and _is_ai_sent_message(webhook_msg_id):
            return {"ok": True, "skipped": "ai_echo"}
        result = await _handle_clinic_message(tenant_id, phone, message_text, db)
        await db.commit()
        return result

    # ── pause check for patient messages ──
    pause_result = await _check_pause_state(tenant_id, phone, db)
    if pause_result:
        await db.commit()
        return JSONResponse(status_code=202, content=pause_result)

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
    await db.commit()

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
        send_result = await send_evolution_text(evol_url, evol_name, evol_key, phone, suggested)
        _track_sent_message(send_result)
        logger.info("Evolution reply sent to %s (%d chars)", phone, len(suggested))
    except Exception:
        logger.exception("Evolution send failed for %s", phone)
        return {"ok": True, "replied": False, "reason": "send_failed"}

    return {"ok": True, "replied": True}
