"""
NeuroStack API — WhatsApp Webhook Receivers
Receives WhatsApp messages from Z-API and Evolution API, processes them
through the cognitive pipeline, and sends the LLM response back.
"""

import logging
import os
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
from src.services.zapi_service import mark_conversation_unread, mark_message_read, send_evolution_text, send_text_message

logger = logging.getLogger(__name__)

router = APIRouter()

# Track message IDs sent by the AI so we can ignore their fromMe=true echo.
# Uses Redis for persistence across container restarts and multiple workers.
_SENT_ID_PREFIX = "ai_sent:"
_SENT_ID_TTL = 300  # 5 minutes — enough for Z-API echo to arrive


def _get_agent_language(clinic_config: Optional[dict]) -> str:
    """Return the configured agent language for the clinic, defaulting to pt-BR."""
    if not clinic_config:
        return "pt-BR"
    business = clinic_config.get("business_rules") or {}
    return business.get("agent_language") or "pt-BR"


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
    return result.scalars().first()


def _normalise_phone(raw: str) -> str:
    """Strip non-digits and leading '+' so we get a clean E.164-ish number."""
    return raw.lstrip("+").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")


async def _resolve_phone_from_lid(
    tenant_id: UUID,
    lid: str,
    db: AsyncSession,
) -> Optional[str]:
    """Resolve a real phone number from a WhatsApp LID using session metadata."""
    from sqlalchemy import text as sa_text
    result = await db.execute(
        sa_text(
            "SELECT user_id FROM sessions "
            "WHERE tenant_id = :tid AND metadata->>'chat_lid' = :lid "
            "AND is_active = true "
            "ORDER BY last_activity_at DESC LIMIT 1"
        ),
        {"tid": str(tenant_id), "lid": lid},
    )
    row = result.first()
    return row[0] if row else None


async def _save_lid_mapping(
    tenant_id: UUID,
    phone: str,
    chat_lid: str,
    db: AsyncSession,
) -> None:
    """Save chatLid in session metadata so fromMe=true messages can resolve the real phone."""
    from sqlalchemy import text as sa_text
    await db.execute(
        sa_text(
            "UPDATE sessions SET metadata = jsonb_set(COALESCE(metadata, '{}'), '{chat_lid}', to_jsonb(CAST(:lid AS text))) "
            "WHERE tenant_id = :tid AND user_id = :phone AND is_active = true"
        ),
        {"tid": str(tenant_id), "phone": phone, "lid": chat_lid},
    )


async def _get_or_create_session(
    tenant_id: UUID,
    phone: str,
    db: AsyncSession,
) -> tuple[UUID, bool]:
    """Return an active session for this phone, or create one (race-safe).

    Returns:
        Tuple of (session_id, is_new). `is_new=True` when the session was just
        created by this call; `False` when an existing session was found
        (including the race-recovery path).
    """
    repo = SessionRepository(db)
    sessions = await repo.find_by_user(
        tenant_id=tenant_id,
        user_id=phone,
        active_only=True,
        limit=1,
    )
    if sessions:
        return sessions[0].id, False

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
        return new_session.id, True
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
            return sessions[0].id, False
        raise


_DEFAULT_LGPD_MESSAGE = (
    "Olá! Bem-vindo(a) ao atendimento da [Nome da Clínica]. 🏥 "
    "Para garantir o melhor cuidado com a sua saúde e segurança, nossa "
    "assistente virtual processa seus dados de acordo com a Lei Geral de "
    "Proteção de Dados (LGPD). Ao continuar esta conversa, você concorda "
    "com o uso dos seus dados para fins de agendamento e orientações sobre "
    "serviços. 📄 Leia nossa Política de Privacidade completa aqui: "
    "{{lgpd_link}} Como posso te ajudar hoje?"
)


def _build_lgpd_message(lgpd: dict, clinic_id: str, clinic_name: str = "") -> str:
    """Build the LGPD opening message with the resolved privacy link and clinic name.

    - Uses `lgpd_message` from config or a sensible default.
    - If `lgpd_custom_link` is set, uses it; otherwise builds
      `{PUBLIC_BASE_URL}/privacidade/{clinic_id}`.
    - Replaces `{{lgpd_link}}` and `{{clinic_name}}` / `[Nome da Clínica]` placeholders.
    """
    message = (lgpd or {}).get("lgpd_message") or _DEFAULT_LGPD_MESSAGE
    custom_link = ((lgpd or {}).get("lgpd_custom_link") or "").strip()
    if custom_link:
        link = custom_link
    else:
        base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        link = f"{base_url}/privacidade/{clinic_id}" if base_url else f"/privacidade/{clinic_id}"
    message = message.replace("{{lgpd_link}}", link)
    if clinic_name:
        message = message.replace("{{clinic_name}}", clinic_name)
        message = message.replace("[Nome da Clínica]", clinic_name)
    return message


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
    import re as _re
    def _normalize_for_match(s: str) -> str:
        """Normalize: lowercase, collapse whitespace, remove spaces around punctuation."""
        s = " ".join(s.strip().lower().split())
        s = _re.sub(r'\s*([,;:.!?])\s*', r'\1', s)
        return s

    trigger_msg = _normalize_for_match(str(handoff_cfg.get(
        "trigger_message",
        "Precisando de algo mais,estamos a disposição.",
    )))
    reactivation_hours = int(handoff_cfg.get("reactivation_delay_hours", 1))

    normalized_text = _normalize_for_match(message_text)

    # Check if message contains the trigger phrase (flexible match, punctuation-tolerant)
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
    raw_phone = body.get("phone", "")
    phone = _normalise_phone(raw_phone)
    chat_lid = body.get("chatLid", "")
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
        # When phone is in @lid format, resolve real phone from session metadata
        effective_phone = phone
        if "@lid" in raw_phone:
            effective_phone = await _resolve_phone_from_lid(tenant_id, raw_phone, db)
            if not effective_phone:
                logger.warning("zapi_lid_unresolved", extra={"lid": raw_phone, "tenant_id": str(tenant_id)})
                return {"ok": True, "skipped": "lid_unresolved"}
        result = await _handle_clinic_message(tenant_id, effective_phone, message_text, db)
        await db.commit()
        return result

    # ── mark message as read (blue checkmarks) ──
    try:
        _cr = await db.execute(
            text("SELECT api_credentials FROM clinic_configs WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        _creds = (_cr.first() or [None])[0] or {}
        _inst = _creds.get("zapi_instance_id", "")
        _tok = _creds.get("zapi_token", "")
        _ct = _creds.get("zapi_client_token", "")
        if _inst and _tok:
            await mark_message_read(_inst, _tok, phone, body.get("messageId", ""), _ct)
    except Exception:
        pass  # fire-and-forget

    # ── pause check for patient messages ──
    pause_result = await _check_pause_state(tenant_id, phone, db)
    if pause_result:
        await db.commit()
        return JSONResponse(status_code=202, content=pause_result)

    # ── get or create a session for this phone ──
    session_id, is_new = await _get_or_create_session(tenant_id, phone, db)

    # ── save chatLid mapping so fromMe=true can resolve the real phone ──
    if chat_lid and "@lid" in chat_lid:
        await _save_lid_mapping(tenant_id, phone, chat_lid, db)

    await db.commit()

    # ── process through NeuroStack pipeline ──
    service = get_event_service()
    clinic_config = await service._load_clinic_config(tenant_id)

    # ── send LGPD privacy notice as the very first message of a new session ──
    if is_new:
        lgpd_cfg = (clinic_config or {}).get("lgpd") or {}
        if lgpd_cfg.get("lgpd_enabled"):
            creds_lgpd = await _load_tenant_credentials(tenant_id, db)
            z_inst = creds_lgpd.get("zapi_instance_id", "").strip()
            z_tok = creds_lgpd.get("zapi_token", "").strip()
            z_ct = creds_lgpd.get("zapi_client_token", "").strip()
            if z_inst and z_tok:
                try:
                    _clinic_name = (clinic_config or {}).get("identity", {}).get("clinic_name", "")
                    privacy_msg = _build_lgpd_message(lgpd_cfg, clinic_id=str(tenant_id), clinic_name=_clinic_name)
                    send_result = await send_text_message(
                        z_inst, z_tok, phone, privacy_msg, client_token=z_ct,
                    )
                    _track_sent_message(send_result)
                    logger.info("Z-API LGPD privacy message sent to %s", phone)
                except Exception as exc:
                    logger.warning("Z-API LGPD send failed for %s — %s", phone, exc)
    event_request = EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type="message.in",
        payload=EventPayload(
            content=message_text,
            channel="whatsapp",
            user_id=phone,
            language=_get_agent_language(clinic_config),
            metadata={
                "phone": phone,
                "senderName": sender_name,
                "messageId": body.get("messageId", ""),
                "source": "zapi",
            },
        ),
    )

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

    # ── mark conversation as unread (green dot) after handoff message ──
    _handoff = response.handoff_summary or (response.decision and response.decision.handoff_required)
    logger.info(
        "Z-API handoff check for %s: handoff_summary=%s, decision.handoff_required=%s → mark_unread=%s",
        phone,
        bool(response.handoff_summary),
        response.decision.handoff_required if response.decision else None,
        bool(_handoff),
    )
    if _handoff:
        try:
            await mark_conversation_unread(zapi_inst, zapi_token, phone, client_token=zapi_client_token)
            logger.info("Z-API mark_unread SUCCESS after handoff for %s", phone)
        except Exception as exc:
            logger.warning("Z-API mark_unread FAILED for %s — %s", phone, exc)

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
    session_id, is_new = await _get_or_create_session(tenant_id, phone, db)
    await db.commit()

    # ── process through NeuroStack pipeline ──
    service = get_event_service()
    clinic_config = await service._load_clinic_config(tenant_id)

    # ── send LGPD privacy notice as the very first message of a new session ──
    if is_new:
        lgpd_cfg = (clinic_config or {}).get("lgpd") or {}
        if lgpd_cfg.get("lgpd_enabled"):
            creds_lgpd = await _load_tenant_credentials(tenant_id, db)
            e_url = creds_lgpd.get("evolution_api_url", "")
            e_name = creds_lgpd.get("evolution_instance_name", "")
            e_key = creds_lgpd.get("evolution_api_key", "")
            if e_url and e_name and e_key:
                try:
                    _clinic_name = (clinic_config or {}).get("identity", {}).get("clinic_name", "")
                    privacy_msg = _build_lgpd_message(lgpd_cfg, clinic_id=str(tenant_id), clinic_name=_clinic_name)
                    send_result = await send_evolution_text(
                        e_url, e_name, e_key, phone, privacy_msg,
                    )
                    _track_sent_message(send_result)
                    logger.info("Evolution LGPD privacy message sent to %s", phone)
                except Exception as exc:
                    logger.warning("Evolution LGPD send failed for %s — %s", phone, exc)

    event_request = EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type="message.in",
        payload=EventPayload(
            content=message_text,
            channel="whatsapp",
            user_id=phone,
            language=_get_agent_language(clinic_config),
            metadata={
                "phone": phone,
                "pushName": sender_name,
                "messageId": key.get("id", ""),
                "source": "evolution",
            },
        ),
    )

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
