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
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.events import EventPayload, EventRequest
from src.core.lgpd_utils import resolve_privacy_link
from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant as TenantModel
from src.repositories import SessionRepository, get_db_session_dependency
from src.services.event_service import get_event_service
from src.services.meta_service import (
    download_meta_media,
    extract_meta_message_id,
    resolve_meta_media,
    send_meta_cloud_text,
)
from src.services.uazapi_service import send_uazapi_text
from src.services.zapi_service import mark_conversation_unread, mark_message_read, send_evolution_text, send_text_message

logger = logging.getLogger(__name__)

router = APIRouter()

# Track message IDs sent by the AI so we can ignore their fromMe=true echo.
# Uses Redis for persistence across container restarts and multiple workers.
# Falls back to an in-process LRU set when Redis is unavailable.
_SENT_ID_PREFIX = "ai_sent:"
_SENT_ID_TTL = 300  # 5 minutes — enough for Z-API echo to arrive

# In-memory fallback: bounded set of (message_id, expiry_epoch) tuples.
# Used when Redis is down so bot echoes don't trigger false human-takeover.
import time as _time
from collections import OrderedDict as _OrderedDict
_INMEM_SENT: "_OrderedDict[str, float]" = _OrderedDict()
_INMEM_MAX = 500  # cap at 500 entries to bound memory


def _is_enabled(value: Any) -> bool:
    """Safely evaluate a config flag that may be stored as bool, 'yes'/'no', or '1'/'0'.

    Returns True only for: True, 1, 'yes', 'true', '1' (case-insensitive).
    Returns False for everything else, including 'no', 'false', '0', None, ''.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "true", "1")
    return False


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
    """Store the messageId from a Z-API send response (Redis + in-memory fallback).

    Always writes to the in-process dict so echoes are recognised even if Redis
    is temporarily down between _track and _is_ai_sent_message calls.
    """
    mid = zapi_response.get("messageId") or zapi_response.get("zapiMessageId")
    if not mid:
        return

    # Always store in-memory (cheap, bounded, zero-latency fallback)
    expiry = _time.monotonic() + _SENT_ID_TTL
    _INMEM_SENT[mid] = expiry
    while len(_INMEM_SENT) > _INMEM_MAX:
        _INMEM_SENT.popitem(last=False)

    # Also persist to Redis for multi-worker / multi-container deployments
    r = _get_redis()
    if r:
        try:
            r.setex(f"{_SENT_ID_PREFIX}{mid}", _SENT_ID_TTL, "1")
        except Exception:
            pass  # Redis failure is non-fatal — in-memory handles the echo


def _is_ai_sent_message(message_id: str) -> bool:
    """Check if a messageId was sent by the AI (Redis with in-memory fallback).

    Checks in-memory first (fastest, always available), then Redis (authoritative
    for multi-worker setups). Consumes the entry so repeated echoes are rejected.
    """
    # Check in-memory first (fastest path, works when Redis is down)
    if message_id in _INMEM_SENT:
        expiry = _INMEM_SENT.pop(message_id)
        if _time.monotonic() < expiry:
            # Also clean up Redis if available (avoid stale key)
            r = _get_redis()
            if r:
                try:
                    r.delete(f"{_SENT_ID_PREFIX}{message_id}")
                except Exception:
                    pass
            return True
        # Expired in-memory entry — continue to Redis check

    # Check Redis (authoritative for other workers/containers)
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


# ── DB-backed AI message deduplication (cross-worker safe) ───────────

def _session_has_ai_msg_id(session: Any, message_id: str) -> bool:
    """Return True (and consume the entry) if message_id is a recent bot-sent ID.

    Stored in session.metadata["ai_msg_ids"] as [[id, monotonic_ts], ...].
    This is shared across all Uvicorn workers via PostgreSQL.
    """
    meta = dict(session.metadata or {})
    entries: list = meta.get("ai_msg_ids", [])
    if not entries:
        return False
    now = _time.monotonic()
    found = False
    remaining = []
    for entry in entries:
        try:
            mid, ts = entry[0], entry[1]
        except (IndexError, TypeError):
            continue
        if mid == message_id and now - ts < _SENT_ID_TTL:
            found = True  # consume — don't add to remaining
        elif now - ts < _SENT_ID_TTL:
            remaining.append(entry)  # still valid, keep
        # expired entries are silently dropped
    if found:
        meta["ai_msg_ids"] = remaining
        session.metadata = meta
    return found


async def _store_ai_msg_id_to_db(
    session_id: "UUID",
    message_id: str,
    db: AsyncSession,
) -> None:
    """Persist a bot-sent messageId into session.metadata for cross-worker dedup.

    Non-fatal: silently ignores any DB error so send failures don't abort the
    webhook handler.
    """
    if not message_id:
        return
    try:
        from src.repositories.session import SessionRepository as _SR
        repo = _SR(db)
        sess = await repo.get_by_id(session_id)
        if not sess:
            return
        meta = dict(sess.metadata or {})
        entries: list = meta.get("ai_msg_ids", [])
        now = _time.monotonic()
        # Prune expired, cap at 20
        entries = [[m, t] for m, t in entries if now - t < _SENT_ID_TTL]
        entries.append([message_id, now])
        entries = entries[-20:]
        meta["ai_msg_ids"] = entries
        sess.metadata = meta
        await db.flush()
    except Exception as _exc:
        logger.debug("_store_ai_msg_id_to_db failed (non-fatal): %s", _exc)


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


_DEFAULT_LGPD_MESSAGE_EN = (
    "Hello! Welcome to {{clinic_name}}. 🏥 To ensure the best care for your health "
    "and safety, our virtual assistant processes your data in accordance with "
    "applicable privacy laws. By continuing this conversation, you agree to the "
    "use of your data for scheduling and service guidance purposes. "
    "📄 Read our full Privacy Policy here: {{lgpd_link}} How can I help you today?"
)


def _build_lgpd_message(
    lgpd: dict,
    clinic_id: str,
    clinic_name: str = "",
    agent_language: str = "pt-BR",
) -> str:
    """Build the LGPD opening message with the resolved privacy link and clinic name.

    - Uses `lgpd_message` (PT) or `lgpd_message_en` (EN) from config based on
      ``agent_language``; falls back to a sensible default per language.
    - If `lgpd_custom_link` is set, uses it; otherwise builds
      `{PUBLIC_BASE_URL}/privacidade/{clinic_id}`.
    - Replaces `{{lgpd_link}}` and `{{clinic_name}}` / `[Nome da Clínica]` placeholders.
    """
    if agent_language == "en":
        message = (lgpd or {}).get("lgpd_message_en") or _DEFAULT_LGPD_MESSAGE_EN
    else:
        message = (lgpd or {}).get("lgpd_message") or _DEFAULT_LGPD_MESSAGE
    link = resolve_privacy_link(lgpd or {}, clinic_id)
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


async def _check_bot_active(
    tenant_id: UUID,
    phone: str,
    db: AsyncSession,
) -> Optional[dict]:
    """Return a skip-dict when bot_active=False, or None to continue normally.

    When the AI is paused (bot_active=False), all incoming messages are silently
    dropped — no reply is sent to the patient.  The human team handles the
    conversation directly.
    """
    result = await db.execute(
        select(TenantModel.bot_active).where(TenantModel.id == tenant_id)
    )
    bot_active = result.scalar_one_or_none()
    if bot_active is None or bot_active:
        return None  # Bot is on — continue normally

    logger.info(
        "bot_paused_skip",
        extra={"tenant_id": str(tenant_id), "phone": phone},
    )
    return {"ok": True, "skipped": "bot_paused"}


async def _handle_clinic_message(
    tenant_id: UUID,
    phone: str,
    message_text: str,
    db: AsyncSession,
    message_id: Optional[str] = None,
) -> dict:
    """Handle fromMe=true messages (clinic/secretary).

    Never enters pipeline/SRM. Checks for trigger message or human takeover.

    message_id: Z-API/Evolution messageId from the webhook payload. When
    provided, it is checked against the session's ai_msg_ids metadata (set
    by _store_ai_msg_id_to_db) so bot-sent echoes are silently skipped
    even across multiple Uvicorn workers.
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

    # ── Cross-worker bot-echo dedup (DB-backed, shared across all workers) ──
    # _session_has_ai_msg_id checks session.metadata["ai_msg_ids"] written by
    # _store_ai_msg_id_to_db after each outbound message. This is the
    # authoritative store — it works even when Redis is down and even when the
    # fromMe echo is routed to a different Uvicorn worker than the one that
    # sent the original message.
    if message_id and _session_has_ai_msg_id(session, message_id):
        logger.debug(
            "fromMe_echo_skipped_db",
            extra={"session_id": str(session.id), "message_id": message_id},
        )
        # Persist the consumed entry list back to the DB
        await db.flush()
        return {"ok": True, "skipped": "ai_echo_db"}

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

    standby_mode = str(handoff_cfg.get("standby_mode", "active")).lower()
    handoff_period = str(handoff_cfg.get("handoff_period", "permanent")).lower()
    if standby_mode == "inactive":
        fallback_hours = int(handoff_cfg.get("handoff_inactive_default_hours", 24))
        session.paused_until = datetime.now(timezone.utc) + timedelta(hours=fallback_hours)
    elif handoff_period == "cycle":
        cycle_days = int(handoff_cfg.get("handoff_cycle_days", 7))
        session.paused_until = datetime.now(timezone.utc) + timedelta(days=cycle_days)
    else:
        session.paused_until = None  # permanent — espera frase mestra

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

    # ── extract payload identifiers ──
    text_obj = body.get("text")
    audio_obj = body.get("audio")

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

    # ── load clinic_config early so audio/text handling can use credentials ──
    service = get_event_service()
    clinic_config = await service._load_clinic_config(tenant_id)

    # ── extract message text (from text payload or audio transcription) ──
    message_text = ""
    if text_obj and isinstance(text_obj, dict):
        message_text = (text_obj.get("message") or "").strip()

    # Handle audio/voice messages
    if audio_obj and not message_text:
        audio_url = audio_obj.get("url") or audio_obj.get("audioUrl")
        mimetype = audio_obj.get("mimeType") or "audio/ogg"
        if audio_url and clinic_config:
            creds = (clinic_config or {}).get("api_credentials", {}) or {}
            whisper_key = creds.get("whisper_api_key")
            stt_provider = (creds.get("stt_provider") or "outros").strip()
            stt_api_url = (creds.get("stt_api_url") or "").strip()
            # Per-provider model defaults — avoid sending Groq model names to OpenAI
            _STT_DEFAULT_MODELS: dict[str, str] = {
                "openai_whisper":    "whisper-1",
                "elevenlabs_scribe": "scribe_v1",
            }
            whisper_model = (
                creds.get("whisper_model")
                or _STT_DEFAULT_MODELS.get(stt_provider, "whisper-large-v3-turbo")
            )
            agent_lang = _get_agent_language(clinic_config)
            lang_hint = "en" if agent_lang == "en" else "pt"
            if whisper_key:
                z_inst = (creds.get("zapi_instance_id") or "").strip()
                z_tok = (creds.get("zapi_token") or "").strip()
                z_ct = (creds.get("zapi_client_token") or "").strip()

                # Duration-based limit: long audios trigger human handoff instead of transcription
                audio_max_seconds = int((creds or {}).get("audio_max_seconds") or 60)
                audio_seconds = int(audio_obj.get("seconds") or 0)
                if audio_seconds > audio_max_seconds:
                    if z_inst and z_tok:
                        if agent_lang == "en":
                            handoff_msg = (
                                "I received your voice message! 🎙️ Since it's a bit longer, I'll forward it to our team, "
                                "who will listen and get back to you shortly. 😊"
                            )
                        else:
                            handoff_msg = (
                                "Recebi seu áudio! 🎙️ Como ele é mais longo, vou encaminhar para a nossa equipe, "
                                "que vai ouvir e te responder com atenção. 😊"
                            )
                        await send_text_message(z_inst, z_tok, phone, handoff_msg, client_token=z_ct)
                        # Mark conversation as unread so the clinic team sees the pending audio
                        await mark_conversation_unread(z_inst, z_tok, phone, client_token=z_ct)
                    logger.info(
                        "Audio too long (%ds > %ds limit), skipping transcription for %s",
                        audio_seconds, audio_max_seconds, phone,
                    )
                    return {"ok": True, "skipped": "audio_too_long"}

                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=30) as hx:
                        audio_resp = await hx.get(audio_url)
                        audio_resp.raise_for_status()
                    audio_bytes = audio_resp.content
                    logger.info(
                        "Audio received for transcription: size=%d bytes, mimetype=%s",
                        len(audio_bytes), mimetype,
                    )
                    from src.services.groq_service import (
                        GROQ_API_BASE,
                        OPENAI_API_BASE,
                        transcribe_audio,
                    )
                    # Resolve STT base URL: use the saved URL (set by the UI for
                    # any provider), fall back to known defaults, then Groq as
                    # last resort for legacy configs without a saved URL.
                    if stt_api_url:
                        stt_base_url = stt_api_url
                    elif stt_provider == "openai_whisper":
                        stt_base_url = OPENAI_API_BASE
                    elif stt_provider == "elevenlabs_scribe":
                        stt_base_url = "https://api.elevenlabs.io/v1"
                    else:
                        stt_base_url = GROQ_API_BASE  # outros / google_stt / legacy
                    if stt_provider in ("elevenlabs_scribe", "google_stt"):
                        logger.warning(
                            "STT provider %r is not yet fully implemented in runtime — "
                            "transcription will attempt the configured endpoint but may fail.",
                            stt_provider,
                        )
                    message_text = await transcribe_audio(
                        api_key=whisper_key,
                        audio_bytes=audio_bytes,
                        mimetype=mimetype,
                        model=whisper_model,
                        language=lang_hint,
                        base_url=stt_base_url,
                    )
                except Exception as exc:
                    logger.warning("Audio transcription failed: %s", exc)
                    return {"ok": True, "skipped": "transcription_failed"}
            else:
                return {"ok": True, "skipped": "no_whisper_key"}

    if not text_obj and not audio_obj:
        return {"ok": True, "skipped": "non-text"}
    if not message_text:
        return {"ok": True, "skipped": "empty"}

    # ── fromMe=true: check if AI-sent echo or real clinic message ──
    if body.get("fromMe", False):
        webhook_msg_id = body.get("messageId", "")
        # Fast path: in-memory + Redis check (same worker, Redis available)
        if webhook_msg_id and _is_ai_sent_message(webhook_msg_id):
            return {"ok": True, "skipped": "ai_echo"}
        # When phone is in @lid format, resolve real phone from session metadata
        effective_phone = phone
        if "@lid" in raw_phone:
            effective_phone = await _resolve_phone_from_lid(tenant_id, raw_phone, db)
            if not effective_phone:
                logger.warning("zapi_lid_unresolved", extra={"lid": raw_phone, "tenant_id": str(tenant_id)})
                return {"ok": True, "skipped": "lid_unresolved"}
        # Pass message_id for DB-backed cross-worker dedup inside _handle_clinic_message
        result = await _handle_clinic_message(
            tenant_id, effective_phone, message_text, db, message_id=webhook_msg_id
        )
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

    # ── bot_active check — if AI is paused, send offline msg once then skip ──
    bot_skip = await _check_bot_active(tenant_id, phone, db)
    if bot_skip:
        await db.commit()
        return JSONResponse(status_code=202, content=bot_skip)

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
    # (service + clinic_config were loaded earlier so audio handling could use them)

    # ── send LGPD privacy notice as the very first message of a new session ──
    if is_new:
        lgpd_cfg = (clinic_config or {}).get("lgpd") or {}
        if _is_enabled(lgpd_cfg.get("lgpd_enabled")):
            creds_lgpd = await _load_tenant_credentials(tenant_id, db)
            z_inst = creds_lgpd.get("zapi_instance_id", "").strip()
            z_tok = creds_lgpd.get("zapi_token", "").strip()
            z_ct = creds_lgpd.get("zapi_client_token", "").strip()
            if z_inst and z_tok:
                try:
                    _clinic_name = (clinic_config or {}).get("identity", {}).get("clinic_name", "")
                    privacy_msg = _build_lgpd_message(lgpd_cfg, clinic_id=str(tenant_id), clinic_name=_clinic_name, agent_language=_get_agent_language(clinic_config))
                    send_result = await send_text_message(
                        z_inst, z_tok, phone, privacy_msg, client_token=z_ct,
                    )
                    _track_sent_message(send_result)
                    # DB-backed dedup: store LGPD message ID so its fromMe echo is
                    # recognised as a bot echo across all Uvicorn workers
                    _lgpd_mid = send_result.get("messageId") or send_result.get("zapiMessageId", "")
                    if _lgpd_mid:
                        await _store_ai_msg_id_to_db(session_id, _lgpd_mid, db)
                    logger.info("Z-API LGPD privacy message sent to %s", phone)
                except Exception as exc:
                    logger.warning("Z-API LGPD send failed for %s — %s", phone, exc)
    _event_metadata: dict = {
        "phone": phone,
        "senderName": sender_name,
        "messageId": body.get("messageId", ""),
        "source": "zapi",
    }
    if audio_obj and message_text:
        _event_metadata["whisper_model"] = creds.get("whisper_model") or "whisper-large-v3-turbo"  # type: ignore[possibly-undefined]
        _event_metadata["audio_seconds"] = audio_obj.get("seconds") or 0
        _event_metadata["is_audio"] = True
    event_request = EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type="message.in",
        payload=EventPayload(
            content=message_text,
            channel="whatsapp",
            user_id=phone,
            language=_get_agent_language(clinic_config),
            metadata=_event_metadata,
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
        # DB-backed dedup: store reply message ID so its fromMe echo is recognised
        # as a bot echo across all Uvicorn workers (works even when Redis is down)
        _reply_mid = send_result.get("messageId") or send_result.get("zapiMessageId", "")
        if _reply_mid:
            await _store_ai_msg_id_to_db(session_id, _reply_mid, db)
            await db.commit()  # commit the metadata update immediately
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

    # ── load clinic_config early (needed for bot_active offline_message + LGPD) ──
    service = get_event_service()
    clinic_config = await service._load_clinic_config(tenant_id)

    # ── fromMe=true: check if AI-sent echo or real clinic message ──
    if is_from_me:
        webhook_msg_id = key.get("id", "")
        # Fast path: in-memory + Redis check (same worker, Redis available)
        if webhook_msg_id and _is_ai_sent_message(webhook_msg_id):
            return {"ok": True, "skipped": "ai_echo"}
        # Pass message_id for DB-backed cross-worker dedup inside _handle_clinic_message
        result = await _handle_clinic_message(
            tenant_id, phone, message_text, db, message_id=webhook_msg_id
        )
        await db.commit()
        return result

    # ── bot_active check — if AI is paused, send offline msg once then skip ──
    bot_skip = await _check_bot_active(tenant_id, phone, db)
    if bot_skip:
        await db.commit()
        return JSONResponse(status_code=202, content=bot_skip)

    # ── pause check for patient messages ──
    pause_result = await _check_pause_state(tenant_id, phone, db)
    if pause_result:
        await db.commit()
        return JSONResponse(status_code=202, content=pause_result)

    # ── get or create a session for this phone ──
    session_id, is_new = await _get_or_create_session(tenant_id, phone, db)
    await db.commit()

    # ── send LGPD privacy notice as the very first message of a new session ──
    if is_new:
        lgpd_cfg = (clinic_config or {}).get("lgpd") or {}
        if _is_enabled(lgpd_cfg.get("lgpd_enabled")):
            creds_lgpd = await _load_tenant_credentials(tenant_id, db)
            e_url = creds_lgpd.get("evolution_api_url", "")
            e_name = creds_lgpd.get("evolution_instance_name", "")
            e_key = creds_lgpd.get("evolution_api_key", "")
            if e_url and e_name and e_key:
                try:
                    _clinic_name = (clinic_config or {}).get("identity", {}).get("clinic_name", "")
                    privacy_msg = _build_lgpd_message(lgpd_cfg, clinic_id=str(tenant_id), clinic_name=_clinic_name, agent_language=_get_agent_language(clinic_config))
                    send_result = await send_evolution_text(
                        e_url, e_name, e_key, phone, privacy_msg,
                    )
                    _track_sent_message(send_result)
                    # DB-backed dedup: store LGPD message ID for cross-worker echo detection
                    _lgpd_mid = send_result.get("messageId") or send_result.get("id", "")
                    if _lgpd_mid:
                        await _store_ai_msg_id_to_db(session_id, _lgpd_mid, db)
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
        # DB-backed dedup: store reply message ID for cross-worker echo detection
        _reply_mid = send_result.get("messageId") or send_result.get("id", "")
        if _reply_mid:
            await _store_ai_msg_id_to_db(session_id, _reply_mid, db)
            await db.commit()  # commit the metadata update immediately
        logger.info("Evolution reply sent to %s (%d chars)", phone, len(suggested))
    except Exception:
        logger.exception("Evolution send failed for %s", phone)
        return {"ok": True, "replied": False, "reason": "send_failed"}

    return {"ok": True, "replied": True}


# ── Uazapi webhook endpoint ─────────────────────────────────────────

@router.post(
    "/uazapi",
    summary="Uazapi Webhook",
    description=(
        "Receives incoming WhatsApp messages from a self-hosted Uazapi "
        "(wuzapi) gateway and responds automatically. Tenant is resolved by "
        "``uazapi_instance_name`` (the ``instance`` field on the payload)."
    ),
    tags=["Webhook"],
    status_code=200,
)
async def uazapi_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
):
    """
    Uazapi sends a POST for every incoming message.

    Expected payload (text message):
    {
      "instance": "my-instance",
      "event": "message",
      "data": {
        "from": "5511999999999@s.whatsapp.net",
        "body": "Olá",
        "type": "text",
        "id": "3EB0..."
      }
    }
    """
    body: dict[str, Any] = await request.json()

    # ── extract payload identifiers ──
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return {"ok": True, "skipped": "invalid_data"}

    msg_type = str(data.get("type") or "")
    instance_name = str(body.get("instance") or "")
    raw_from = str(data.get("from") or "")
    from_phone = _normalise_phone(
        raw_from.split("@")[0] if "@" in raw_from else raw_from
    )
    message_text = str(data.get("body") or "")
    msg_id = str(data.get("id") or "")

    if not instance_name or not from_phone:
        return {"ok": True, "skipped": "missing_fields"}

    # Accept "text", "chat", and empty type (some wuzapi variants omit it for
    # plain text messages). Anything else is treated as non-text.
    if msg_type not in ("text", "chat", "") or not message_text:
        return {"ok": True, "skipped": "non-text"}

    # ── resolve tenant from instance_name ──
    tenant_id = await _resolve_tenant_by_credential(
        "uazapi_instance_name", instance_name, db,
    )
    if not tenant_id:
        logger.warning(
            "Uazapi webhook: no tenant for instance %s", instance_name,
        )
        return {"ok": True, "skipped": "unknown_instance"}

    # ── echo dedup (fromMe / self-sent) ──
    if msg_id and _is_ai_sent_message(msg_id):
        return {"ok": True, "skipped": "ai_echo"}

    # ── load clinic_config (needed for agent language + downstream checks) ──
    service = get_event_service()
    clinic_config = await service._load_clinic_config(tenant_id)

    # ── bot_active check — if AI is paused, drop silently ──
    bot_skip = await _check_bot_active(tenant_id, from_phone, db)
    if bot_skip:
        await db.commit()
        return JSONResponse(status_code=202, content=bot_skip)

    # ── pause check for patient messages ──
    pause_result = await _check_pause_state(tenant_id, from_phone, db)
    if pause_result:
        await db.commit()
        return JSONResponse(status_code=202, content=pause_result)

    # ── get or create a session for this phone ──
    session_id, _is_new = await _get_or_create_session(tenant_id, from_phone, db)
    await db.commit()

    logger.info(
        "Uazapi webhook: msg %s from %s (tenant %s)",
        msg_id, from_phone, tenant_id,
    )

    event_request = EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type="message.in",
        payload=EventPayload(
            content=message_text,
            channel="whatsapp",
            user_id=from_phone,
            language=_get_agent_language(clinic_config),
            metadata={
                "phone": from_phone,
                "messageId": msg_id,
                "source": "uazapi",
            },
        ),
    )

    response = await service.process_event(event_request, session=db)
    await db.commit()

    suggested = (
        response.prompt.suggested_response
        if response.prompt and response.prompt.suggested_response
        else None
    )

    if not suggested:
        logger.info("Uazapi webhook: no suggested_response for %s", from_phone)
        return {"ok": True, "replied": False}

    creds = await _load_tenant_credentials(tenant_id, db)
    u_url = (creds.get("uazapi_api_url") or "").strip()
    u_token = (creds.get("uazapi_token") or "").strip()

    if not u_url or not u_token:
        logger.warning("Uazapi credentials missing for tenant %s", tenant_id)
        return {"ok": True, "replied": False, "reason": "no_credentials"}

    try:
        send_result = await send_uazapi_text(u_url, u_token, from_phone, suggested)
        _track_sent_message(send_result)
        # DB-backed dedup: store reply message ID for cross-worker echo detection
        reply_mid = send_result.get("MessageID") or send_result.get("id", "")
        if reply_mid:
            await _store_ai_msg_id_to_db(session_id, reply_mid, db)
            await db.commit()  # commit metadata update immediately
        logger.info(
            "Uazapi reply sent to %s (%d chars)", from_phone, len(suggested),
        )
    except Exception:
        logger.exception("Uazapi send failed for %s", from_phone)
        return {"ok": True, "replied": False, "reason": "send_failed"}

    return {"ok": True, "replied": True}


# ── Meta Cloud API webhook endpoints ────────────────────────────────

async def _resolve_tenant_by_meta_verify_token(
    token: str,
    session: AsyncSession,
) -> Optional[UUID]:
    """Find the tenant whose meta_verify_token matches ``token``.

    The Meta handshake carries only ``hub.verify_token`` (no phone_number_id),
    so we must locate the clinic that generated this token during config save.
    Each clinic gets a UUID-hex verify token from ``config.py`` — collisions
    are astronomically unlikely.
    """
    if not token:
        return None
    stmt = select(ClinicConfigTable.tenant_id).where(
        ClinicConfigTable.api_credentials["meta_verify_token"].astext == token,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


def _track_meta_sent_message(send_result: dict) -> None:
    """Adapter around ``_track_sent_message`` for Meta's payload shape.

    Meta returns the outbound id at ``messages[0].id`` rather than the
    top-level ``messageId`` that Z-API/Evolution use. We rewrap so the
    existing in-memory + Redis dedup helpers keep working unchanged.
    """
    mid = extract_meta_message_id(send_result)
    if mid:
        _track_sent_message({"messageId": mid})


# ── Meta Cloud API — media (audio / files) handling helpers ──────────

# Message types that route through the audio-transcription path (Whisper)
_META_AUDIO_TYPES = frozenset({"audio", "voice"})

# Message types that trigger the "file received" acknowledgement flow.
# By design we never download or analyse the contents (see WISH.md: risk of
# opining on a sensitive document like a medical exam without clinical
# context is unsafe by default).
_META_FILE_TYPES = frozenset({"image", "document", "video", "sticker"})

# Rough Opus voice-note bitrate. Meta's webhook + media resolve endpoint do
# not expose duration, so we cap by size at ~16 KB/s to reject multi-minute
# audios before spending network / STT budget on them.
_META_AUDIO_APPROX_BYTES_PER_SECOND = 16000

# Keywords that mark a "forward it to your team" reply after the fixed
# file-acknowledgement message was sent. Matched as substrings on the
# lowercased text, so short variants ("encaminh", "envia") catch conjugated
# forms without a full NLP pass.
_META_FORWARD_KEYWORDS: tuple[str, ...] = (
    "encaminh", "encaminha", "encaminhar",
    "envia", "enviar", "manda", "mandar",
    "equipe", "atendent", "human",
    "forward", "team", "please forward", "send it",
)

_FILE_ACK_MSG_PT = (
    "Olá{name_suffix}! Percebi que você enviou um arquivo. No momento, não "
    "consigo acessar nem analisar o conteúdo por aqui. Você prefere me "
    "descrever como posso ajudar? Pode escrever ou enviar um áudio, se "
    "for mais fácil. Ou prefere que eu encaminhe o arquivo para a nossa "
    "equipe avaliar e retornar para você?"
)

_FILE_ACK_MSG_EN = (
    "Hi{name_suffix}! I noticed you sent a file. Right now I can't access "
    "or analyze its contents here. Would you rather describe how I can "
    "help? You can type or send a voice message, whichever is easier. Or "
    "would you like me to forward the file to our team so they can review "
    "and get back to you?"
)

_AUDIO_TOO_LONG_MSG_PT = (
    "Recebi seu áudio! 🎙️ Como ele é mais longo, vou encaminhar para a "
    "nossa equipe, que vai ouvir e te responder com atenção. 😊"
)

_AUDIO_TOO_LONG_MSG_EN = (
    "I received your voice message! 🎙️ Since it's a bit longer, I'll "
    "forward it to our team, who will listen and get back to you shortly. 😊"
)

_FILE_FORWARDED_MSG_PT = (
    "Perfeito! Já encaminhei seu arquivo para a nossa equipe. Em breve "
    "alguém entra em contato com você. 🙂"
)

_FILE_FORWARDED_MSG_EN = (
    "Got it! I've forwarded your file to our team. Someone will follow up "
    "with you shortly. 🙂"
)


def _build_meta_file_ack_message(lead_name: str, agent_language: str) -> str:
    """Fixed acknowledgement when a patient sends a file/attachment via Meta.

    Personalises with the lead's first name when we already know it (from
    ``cognitive_state.slots.lead_name``). Never analyses or opines about the
    file's content — offers two options: describe the need in text/audio,
    or forward the file to the human team.
    """
    suffix = f", {lead_name.split()[0]}" if lead_name and lead_name.strip() else ""
    template = _FILE_ACK_MSG_EN if agent_language == "en" else _FILE_ACK_MSG_PT
    return template.format(name_suffix=suffix)


def _detect_forward_intent(text: str) -> bool:
    """Return True when the patient's text signals "forward to your team".

    Called only when the session has ``pending_file_review=true``, so a
    generic "sim/ok/please" here is understood in that narrow context. Any
    reply that does not match falls back to the normal text pipeline (the
    patient is describing their need instead).
    """
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(kw in normalized for kw in _META_FORWARD_KEYWORDS)


async def _meta_set_pending_file_review(
    session_id: UUID,
    db: AsyncSession,
) -> None:
    """Mark the session as awaiting a decision on a received file/attachment.

    Uses the SQLAlchemy ORM (load → mutate → flush) so the same code works
    on PostgreSQL in production and SQLite in tests without dialect-specific
    JSONB syntax.
    """
    from src.models.session import Session as SessionModel
    result = await db.execute(
        select(SessionModel).where(SessionModel.id == session_id)
    )
    sess = result.scalars().first()
    if not sess:
        return
    meta = dict(sess.session_metadata or {})
    meta["pending_file_review"] = True
    sess.session_metadata = meta
    await db.flush()


async def _meta_get_pending_file_review(
    session_id: UUID,
    db: AsyncSession,
) -> bool:
    """Check whether the session is awaiting a decision on a received file."""
    from src.models.session import Session as SessionModel
    result = await db.execute(
        select(SessionModel.session_metadata).where(
            SessionModel.id == session_id,
        )
    )
    meta = result.scalars().first() or {}
    return bool(meta.get("pending_file_review"))


async def _meta_clear_pending_file_review(
    session_id: UUID,
    db: AsyncSession,
) -> None:
    """Clear the pending-file-review flag on the session."""
    from src.models.session import Session as SessionModel
    result = await db.execute(
        select(SessionModel).where(SessionModel.id == session_id)
    )
    sess = result.scalars().first()
    if not sess:
        return
    meta = dict(sess.session_metadata or {})
    if "pending_file_review" in meta:
        del meta["pending_file_review"]
        sess.session_metadata = meta
        await db.flush()


async def _meta_load_lead_name(session_id: UUID, db: AsyncSession) -> str:
    """Read ``cognitive_state.slots.lead_name`` from a session, or "" if unset."""
    from src.models.session import Session as SessionModel
    result = await db.execute(
        select(SessionModel.cognitive_state).where(
            SessionModel.id == session_id,
        )
    )
    cs = result.scalars().first() or {}
    slots = cs.get("slots") or {}
    name = slots.get("lead_name")
    return str(name) if name else ""


async def _meta_initiate_file_handoff(
    tenant_id: UUID,
    session_id: UUID,
    from_phone: str,
    note: str,
) -> None:
    """Initiate a human handoff signalling that a file/audio is pending review.

    Failures are logged at exception level (matching the "send failed"
    parity requirement) but never re-raised — the webhook should still
    return 200 so Meta does not retry.
    """
    from src.handoff.models import HandoffReason, HandoffRequest, HandoffUrgency
    from src.services.handoff_service import get_handoff_service

    handoff_service = get_handoff_service()
    request = HandoffRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        reason=HandoffReason.USER_REQUEST,
        urgency=HandoffUrgency.NORMAL,
        key_info={
            "numero_whatsapp": from_phone,
            "phone": from_phone,
            "file_review_note": note,
        },
    )
    try:
        await handoff_service.initiate_handoff(request)
        logger.info(
            "Meta file handoff initiated for %s (session=%s, note=%s)",
            from_phone, session_id, note,
        )
    except Exception as exc:
        logger.exception(
            "Meta file handoff init failed for %s (session=%s) — %s",
            from_phone, session_id, exc,
        )


async def _meta_transcribe_audio(
    audio_bytes: bytes,
    mime_type: str,
    creds: dict,
    agent_language: str,
) -> Optional[str]:
    """Transcribe Meta audio bytes reusing the same Whisper pipeline as Z-API.

    Returns the transcribed text, or ``None`` when no ``whisper_api_key`` is
    configured. Raises the underlying exception on transcription failure so
    the caller can log it via ``logger.exception``.
    """
    whisper_key = (creds.get("whisper_api_key") or "").strip()
    if not whisper_key:
        return None

    from src.services.groq_service import (
        GROQ_API_BASE,
        OPENAI_API_BASE,
        transcribe_audio,
    )

    stt_provider = (creds.get("stt_provider") or "outros").strip()
    stt_api_url = (creds.get("stt_api_url") or "").strip()
    _STT_DEFAULT_MODELS: dict[str, str] = {
        "openai_whisper": "whisper-1",
        "elevenlabs_scribe": "scribe_v1",
    }
    whisper_model = (
        creds.get("whisper_model")
        or _STT_DEFAULT_MODELS.get(stt_provider, "whisper-large-v3-turbo")
    )
    lang_hint = "en" if agent_language == "en" else "pt"

    if stt_api_url:
        stt_base_url = stt_api_url
    elif stt_provider == "openai_whisper":
        stt_base_url = OPENAI_API_BASE
    elif stt_provider == "elevenlabs_scribe":
        stt_base_url = "https://api.elevenlabs.io/v1"
    else:
        stt_base_url = GROQ_API_BASE

    return await transcribe_audio(
        api_key=whisper_key,
        audio_bytes=audio_bytes,
        mimetype=mime_type,
        model=whisper_model,
        language=lang_hint,
        base_url=stt_base_url,
    )


@router.get(
    "/meta",
    summary="Meta Cloud API Webhook Verification",
    description=(
        "Meta Cloud API webhook handshake. Meta calls this once per app to "
        "verify the URL owns the ``hub.verify_token`` it was configured with."
    ),
    tags=["Webhook"],
)
async def meta_webhook_verify(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
):
    """Meta Cloud API webhook verification handshake.

    Meta sends a GET like:
    ``GET /webhook/meta?hub.mode=subscribe&hub.verify_token=<TOKEN>&hub.challenge=<CHALLENGE>``

    We locate the clinic whose ``meta_verify_token`` matches and echo back the
    challenge as plain text. Any mismatch returns 403.
    """
    mode = request.query_params.get("hub.mode", "")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")

    if mode != "subscribe" or not token or not challenge:
        logger.warning(
            "Meta webhook verify rejected — missing/invalid handshake params",
        )
        return PlainTextResponse("forbidden", status_code=403)

    tenant_id = await _resolve_tenant_by_meta_verify_token(token, db)
    if not tenant_id:
        logger.warning("Meta webhook verify: no tenant for verify_token")
        return PlainTextResponse("forbidden", status_code=403)

    logger.info("Meta webhook verify succeeded for tenant %s", tenant_id)
    # Meta expects the challenge value returned as plain text (not JSON)
    return PlainTextResponse(challenge, status_code=200)


@router.post(
    "/meta",
    summary="Meta Cloud API Webhook",
    description=(
        "Receives incoming WhatsApp messages via the Meta Cloud API and "
        "responds automatically. Tenant is resolved by ``phone_number_id``."
    ),
    tags=["Webhook"],
    status_code=200,
)
async def meta_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
):
    """
    Meta Cloud API sends a POST for every event (messages + statuses).

    Expected payload (text message):
    {
      "object": "whatsapp_business_account",
      "entry": [{
        "id": "<WABA_ID>",
        "changes": [{
          "field": "messages",
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "5511999999999",
              "phone_number_id": "<PHONE_NUMBER_ID>"
            },
            "contacts": [{"profile": {"name": "João"}, "wa_id": "5511..."}],
            "messages": [{
              "from": "5511999999999",
              "id": "wamid.HBgN...",
              "timestamp": "1711900000",
              "text": {"body": "Olá"},
              "type": "text"
            }]
          }
        }]
      }]
    }
    """
    body: dict[str, Any] = await request.json()

    if body.get("object") != "whatsapp_business_account":
        return {"ok": True, "skipped": "unknown_object"}

    entries = body.get("entry") or []
    if not entries:
        return {"ok": True, "skipped": "no_entry"}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []) or []:
            if not isinstance(change, dict):
                continue
            if change.get("field") != "messages":
                continue

            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "").strip()
            messages = value.get("messages") or []

            if not messages:
                # Pure status update (delivered/read/sent) — silently ignore
                return {"ok": True, "skipped": "status_only"}

            if not phone_number_id:
                return {"ok": True, "skipped": "missing_phone_number_id"}

            # ── resolve tenant from phone_number_id (unique across clinics) ──
            tenant_id = await _resolve_tenant_by_credential(
                "meta_phone_number_id", phone_number_id, db,
            )
            if not tenant_id:
                # Parity with Z-API/Evolution warning so ops can grep for the
                # same signal across all three channels
                logger.warning(
                    "Meta webhook: no tenant for phone_number_id %s",
                    phone_number_id,
                )
                return {"ok": True, "skipped": "unknown_phone_number_id"}

            # ── contact / sender metadata ──
            contacts = value.get("contacts") or []
            contact_name = ""
            if contacts and isinstance(contacts[0], dict):
                contact_name = (contacts[0].get("profile") or {}).get("name", "")

            # Only the first message is processed per webhook call (Meta
            # normally sends one per event) — matches Z-API/Evolution behavior
            msg = messages[0] if isinstance(messages[0], dict) else {}
            message_id = str(msg.get("id") or "")
            msg_type = str(msg.get("type") or "")
            from_phone = _normalise_phone(str(msg.get("from") or ""))

            # Log receipt with the Meta message id so a "patient never got a
            # reply" report can be traced through our logs without needing
            # the Meta dashboard
            logger.info(
                "Meta webhook received message id=%s type=%s from=%s tenant=%s",
                message_id, msg_type, from_phone, tenant_id,
            )

            if not from_phone:
                return {"ok": True, "skipped": "missing_from"}

            # ── AI echo dedup (in-memory + Redis fast path) ──
            # The Cloud API does not deliver our own outbound as "fromMe" the
            # way Z-API does, but we still consult the dedup store as a
            # safety net for future features and cross-worker consistency.
            if message_id and _is_ai_sent_message(message_id):
                return {"ok": True, "skipped": "ai_echo"}

            # ── bot_active check ──
            bot_skip = await _check_bot_active(tenant_id, from_phone, db)
            if bot_skip:
                await db.commit()
                return JSONResponse(status_code=202, content=bot_skip)

            # ── pause check for patient messages ──
            pause_result = await _check_pause_state(tenant_id, from_phone, db)
            if pause_result:
                await db.commit()
                return JSONResponse(status_code=202, content=pause_result)

            # ── get or create a session for this phone ──
            session_id, is_new = await _get_or_create_session(tenant_id, from_phone, db)
            await db.commit()

            # ── load clinic config + credentials ──
            service = get_event_service()
            clinic_config = await service._load_clinic_config(tenant_id)

            creds = await _load_tenant_credentials(tenant_id, db)
            meta_pnid = (creds.get("meta_phone_number_id") or "").strip()
            meta_token = (creds.get("meta_permanent_token") or "").strip()
            agent_lang = _get_agent_language(clinic_config)

            # ── dispatch by message type ──
            #
            # Group 4 adds two paths on top of the Group 2 text flow:
            #   • ``audio`` / ``voice`` — resolve media_id, cap by size to
            #     protect the STT budget, then transcribe via Whisper and
            #     fall through to the normal text pipeline.
            #   • ``image`` / ``document`` / ``video`` / ``sticker`` — never
            #     download the payload (safety: we won't opine on a medical
            #     document without clinical context). Send the fixed
            #     acknowledgement and flag the session so the next reply can
            #     branch into a real handoff or a normal description.
            message_text: str = ""

            if msg_type == "text":
                message_text = (msg.get("text") or {}).get("body", "").strip()
                if not message_text:
                    return {"ok": True, "skipped": "empty"}

                # If a previous file/attachment set pending_file_review, this
                # text is the patient's choice: forward to the team, or
                # describe their need.
                if await _meta_get_pending_file_review(session_id, db):
                    if _detect_forward_intent(message_text):
                        await _meta_initiate_file_handoff(
                            tenant_id, session_id, from_phone,
                            "file_pending_review",
                        )
                        await _meta_clear_pending_file_review(session_id, db)
                        await db.commit()
                        if meta_pnid and meta_token:
                            confirm_msg = (
                                _FILE_FORWARDED_MSG_EN if agent_lang == "en"
                                else _FILE_FORWARDED_MSG_PT
                            )
                            try:
                                send_result = await send_meta_cloud_text(
                                    meta_pnid, meta_token, from_phone, confirm_msg,
                                )
                                _track_meta_sent_message(send_result)
                                _fwd_mid = extract_meta_message_id(send_result)
                                if _fwd_mid:
                                    await _store_ai_msg_id_to_db(session_id, _fwd_mid, db)
                                    await db.commit()
                            except Exception as exc:
                                logger.exception(
                                    "Meta file-forward confirmation send failed for %s — %s",
                                    from_phone, exc,
                                )
                        return {
                            "ok": True,
                            "replied": True,
                            "action": "file_handoff",
                        }
                    # Not a forward — patient is describing their need.
                    # Clear the flag and continue with the normal pipeline.
                    await _meta_clear_pending_file_review(session_id, db)
                    await db.commit()

            elif msg_type in _META_AUDIO_TYPES:
                audio_obj = msg.get("audio") or msg.get("voice") or {}
                media_id = str(audio_obj.get("id") or "")
                mime_type = str(
                    audio_obj.get("mime_type") or "audio/ogg"
                )

                if not media_id:
                    return {"ok": True, "skipped": "missing_media_id"}
                if not meta_token:
                    logger.warning(
                        "Meta credentials missing for audio processing tenant %s",
                        tenant_id,
                    )
                    return {"ok": True, "skipped": "no_credentials"}

                # 1) Resolve media_id → signed URL (+ file_size / mime)
                try:
                    media_info = await resolve_meta_media(media_id, meta_token)
                except Exception as exc:
                    logger.warning(
                        "Meta media resolve failed for %s tenant=%s — %s",
                        media_id, tenant_id, exc,
                    )
                    return {"ok": True, "skipped": "media_resolve_failed"}

                media_url = str(media_info.get("url") or "")
                if not media_url:
                    logger.warning(
                        "Meta media resolve returned no url for %s tenant=%s",
                        media_id, tenant_id,
                    )
                    return {"ok": True, "skipped": "media_resolve_failed"}

                # 2) Enforce audio_max_seconds by approximate byte count.
                # Meta exposes neither duration nor download size in the
                # webhook, so we lean on the file_size the resolve endpoint
                # returns and treat > audio_max_seconds*16KB as "too long"
                # — this triggers a handoff, never a 500.
                audio_max_seconds = int((creds or {}).get("audio_max_seconds") or 60)
                file_size = int(media_info.get("file_size") or 0)
                max_bytes = audio_max_seconds * _META_AUDIO_APPROX_BYTES_PER_SECOND
                if file_size and file_size > max_bytes:
                    handoff_msg = (
                        _AUDIO_TOO_LONG_MSG_EN if agent_lang == "en"
                        else _AUDIO_TOO_LONG_MSG_PT
                    )
                    if meta_pnid and meta_token:
                        try:
                            await send_meta_cloud_text(
                                meta_pnid, meta_token, from_phone, handoff_msg,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Meta audio-too-long ack send failed for %s — %s",
                                from_phone, exc,
                            )
                    await _meta_initiate_file_handoff(
                        tenant_id, session_id, from_phone, "audio_too_long",
                    )
                    logger.info(
                        "Meta audio too long (%d bytes > ~%ds*%dB) for %s — handoff",
                        file_size, audio_max_seconds,
                        _META_AUDIO_APPROX_BYTES_PER_SECOND, from_phone,
                    )
                    return {"ok": True, "skipped": "audio_too_long"}

                # 3) Download bytes (authenticated GET)
                try:
                    audio_bytes = await download_meta_media(media_url, meta_token)
                except Exception as exc:
                    logger.warning(
                        "Meta audio download failed for %s tenant=%s — %s",
                        media_id, tenant_id, exc,
                    )
                    return {"ok": True, "skipped": "media_download_failed"}

                # 4) Transcribe via the same Whisper/STT client used by Z-API
                try:
                    message_text = await _meta_transcribe_audio(
                        audio_bytes=audio_bytes,
                        mime_type=mime_type,
                        creds=creds,
                        agent_language=agent_lang,
                    ) or ""
                except Exception as exc:
                    logger.exception(
                        "Meta audio transcription failed for %s — %s",
                        from_phone, exc,
                    )
                    return {"ok": True, "skipped": "transcription_failed"}

                if not message_text:
                    return {"ok": True, "skipped": "no_whisper_key"}

                # Falls through to the shared text pipeline below.

            elif msg_type in _META_FILE_TYPES:
                # File/attachment: never download, never analyse. Send the
                # fixed acknowledgement and mark the session as awaiting the
                # patient's choice (describe vs forward).
                lead_name = await _meta_load_lead_name(session_id, db)
                ack_msg = _build_meta_file_ack_message(lead_name, agent_lang)

                if not meta_pnid or not meta_token:
                    logger.warning(
                        "Meta credentials missing for file ack tenant %s",
                        tenant_id,
                    )
                    return {"ok": True, "replied": False, "reason": "no_credentials"}

                try:
                    send_result = await send_meta_cloud_text(
                        meta_pnid, meta_token, from_phone, ack_msg,
                    )
                    _track_meta_sent_message(send_result)
                    _ack_mid = extract_meta_message_id(send_result)
                    if _ack_mid:
                        await _store_ai_msg_id_to_db(session_id, _ack_mid, db)
                    await _meta_set_pending_file_review(session_id, db)
                    await db.commit()
                    logger.info(
                        "Meta file ack sent to %s (type=%s)", from_phone, msg_type,
                    )
                except Exception as exc:
                    logger.exception(
                        "Meta file ack send failed for %s — %s", from_phone, exc,
                    )
                    return {
                        "ok": True,
                        "replied": False,
                        "reason": "send_failed",
                        "error": str(exc),
                    }
                return {
                    "ok": True,
                    "replied": True,
                    "action": "file_ack",
                }

            else:
                # Unknown message type (contacts, location, reactions…) —
                # keep behaviour compatible with Group 2's non-text skip.
                return {"ok": True, "skipped": "non-text"}

            # ── send LGPD privacy notice on new sessions ──
            if is_new and meta_pnid and meta_token:
                lgpd_cfg = (clinic_config or {}).get("lgpd") or {}
                if _is_enabled(lgpd_cfg.get("lgpd_enabled")):
                    try:
                        _clinic_name = (clinic_config or {}).get("identity", {}).get(
                            "clinic_name", "",
                        )
                        privacy_msg = _build_lgpd_message(
                            lgpd_cfg,
                            clinic_id=str(tenant_id),
                            clinic_name=_clinic_name,
                            agent_language=_get_agent_language(clinic_config),
                        )
                        send_result = await send_meta_cloud_text(
                            meta_pnid, meta_token, from_phone, privacy_msg,
                        )
                        _track_meta_sent_message(send_result)
                        _lgpd_mid = extract_meta_message_id(send_result)
                        if _lgpd_mid:
                            await _store_ai_msg_id_to_db(session_id, _lgpd_mid, db)
                        logger.info("Meta LGPD privacy message sent to %s", from_phone)
                    except Exception as exc:
                        logger.warning(
                            "Meta LGPD send failed for %s — %s", from_phone, exc,
                        )

            # ── pipeline ──
            event_request = EventRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type="message.in",
                payload=EventPayload(
                    content=message_text,
                    channel="whatsapp",
                    user_id=from_phone,
                    language=_get_agent_language(clinic_config),
                    metadata={
                        "phone": from_phone,
                        "pushName": contact_name,
                        "messageId": message_id,
                        "source": "meta_cloud",
                    },
                ),
            )

            response = await service.process_event(event_request, session=db)
            await db.commit()

            suggested = (
                response.prompt.suggested_response
                if response.prompt and response.prompt.suggested_response
                else None
            )

            if not suggested:
                logger.info("Meta webhook: no suggested_response for %s", from_phone)
                return {"ok": True, "replied": False, "reason": "no_llm_response"}

            if not meta_pnid or not meta_token:
                logger.warning("Meta credentials missing for tenant %s", tenant_id)
                return {"ok": True, "replied": False, "reason": "no_credentials"}

            try:
                send_result = await send_meta_cloud_text(
                    meta_pnid, meta_token, from_phone, suggested,
                )
                _track_meta_sent_message(send_result)
                _reply_mid = extract_meta_message_id(send_result)
                if _reply_mid:
                    await _store_ai_msg_id_to_db(session_id, _reply_mid, db)
                    await db.commit()  # commit metadata update immediately
                logger.info(
                    "Meta reply sent to %s (%d chars)", from_phone, len(suggested),
                )
            except Exception as exc:
                logger.exception("Meta send failed for %s — %s", from_phone, exc)
                return {
                    "ok": True,
                    "replied": False,
                    "reason": "send_failed",
                    "error": str(exc),
                }

            return {"ok": True, "replied": True}

    return {"ok": True, "skipped": "no_message"}
