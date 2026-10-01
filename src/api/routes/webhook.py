"""
NeuroStack API — WhatsApp Webhook Receivers
Receives WhatsApp messages from Z-API and Evolution API, processes them
through the cognitive pipeline, and sends the LLM response back.
"""

import hashlib
import hmac
import logging
import re
import time as _time
from collections import OrderedDict as _OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.rate_limit import (
    check_webhook_rate_limit,
    webhook_rate_limit_response,
)
from src.api.schemas.events import EventPayload, EventRequest, EventType
from src.config.settings import settings
from src.core.lgpd_utils import resolve_privacy_link
from src.handoff.pause_policy import resolve_handoff_pause
from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant as TenantModel
from src.repositories import SessionRepository, get_db_session_dependency
from src.services.api_credentials import OPERATIONAL_MULTIMODAL_PROVIDERS
from src.services.event_service import get_event_service
from src.services.meta_service import (
    download_meta_media_audio,
    extract_meta_message_id,
    resolve_meta_media,
    send_meta_cloud_audio,
    send_meta_cloud_text,
)
from src.services.safe_egress import UnsafeEgressError, safe_download_audio
from src.services.tts_service import synthesize_speech
from src.services.uazapi_service import (
    UazapiMediaDownloadError,
    download_uazapi_media,
    send_uazapi_audio,
    send_uazapi_text,
)
from src.services.zapi_service import (
    mark_conversation_unread,
    mark_message_read,
    send_evolution_audio,
    send_evolution_text,
    send_text_message,
    send_zapi_audio,
)

# ── Constants for webhook authenticity ──
# Z-API capability token: 32 random bytes encoded as base64url without
# padding == 43 ASCII characters from [A-Za-z0-9_-].
ZAPI_CAPABILITY_TOKEN_LEN = 43
ZAPI_CAPABILITY_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")

# Meta webhook HMAC header (case-insensitive).
META_SIGNATURE_HEADER = "X-Hub-Signature-256"

# STT providers with no runtime adapter: they must skip before any
# download / request / reply / handoff / session mutation happens.
UNSUPPORTED_STT_PROVIDERS = frozenset({"google_stt", "deepgram_stt", "microsoft_stt"})

# Replay claim retention: 31 days covers Z-API's documented 30-day media
# availability plus one extra day of dedup guarantee.
REPLAY_TTL_SECONDS = 31 * 24 * 60 * 60  # 2_678_400

# ── Audio fallback chain (STT → multimodal → text reroute) ──
#
# Failure reasons returned by ``_transcribe_audio_chain``. The empty string
# means "a usable transcript was produced"; the two non-empty values are the
# only causes that reach the patient, and they map 1:1 onto the two message
# variants below. They are deliberately distinct (Decision 6): telling a
# clinic that never configured speech-to-text that we are "temporarily
# unable" would be false.
STT_CHAIN_OK = ""
STT_CHAIN_NO_CAPABILITY = "no_capability"
STT_CHAIN_FAILED = "transcription_failed"

# Reroute notice throttle (Decision 12): one notice per patient per 6h
# window, keyed by tenant + phone on both channels.
_STT_FALLBACK_NOTICE_PREFIX = "stt_fallback_notice:"
_STT_FALLBACK_NOTICE_TTL = 21600  # 6 hours

# Patient-facing reroute copy. Two variants, both in PT and EN, resolved
# through ``_get_agent_language`` exactly like the long-audio handoff text.
_STT_NEVER_CONFIGURED_MSG_PT = (
    "Recebi seu áudio! 🎙️ Ainda não consigo ouvir mensagens de voz por aqui. "
    "Você pode me escrever o que precisa? Assim consigo te ajudar agora "
    "mesmo. 😊"
)

_STT_NEVER_CONFIGURED_MSG_EN = (
    "I got your voice message! 🎙️ I'm not able to listen to voice messages "
    "here yet. Could you type what you need? That way I can help you right "
    "away. 😊"
)

_STT_TEMPORARY_FAILURE_MSG_PT = (
    "Recebi seu áudio! 🎙️ Não consegui ouvir sua mensagem agora. Você pode "
    "me escrever o que precisa? Assim consigo te ajudar sem demora. 😊"
)

_STT_TEMPORARY_FAILURE_MSG_EN = (
    "I got your voice message! 🎙️ I couldn't listen to it right now. Could "
    "you type what you need? That way I can help you without delay. 😊"
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Track message IDs sent by the AI so we can ignore their fromMe=true echo.
# Uses Redis for persistence across container restarts and multiple workers.
# Falls back to an in-process LRU set when Redis is unavailable.
_SENT_ID_PREFIX = "ai_sent:"
_INCOMING_ID_PREFIX = "incoming_msg:"
_SENT_ID_TTL = 300  # 5 minutes — enough for Z-API echo to arrive
_INCOMING_ID_TTL = 900  # 15 minutes — providers may retry duplicate webhooks

# In-memory fallback: bounded set of (message_id, expiry_epoch) tuples.
# Used when Redis is down so bot echoes don't trigger false human-takeover.

_INMEM_SENT: "_OrderedDict[str, float]" = _OrderedDict()
_INMEM_INCOMING: "_OrderedDict[str, float]" = _OrderedDict()
# Reroute notices already delivered inside the current 6h window. Same
# Redis-with-in-memory-fallback shape as _INMEM_SENT; tests that clear the
# other two stores must clear this one too or the notice suppression leaks
# from one test into the next.
_INMEM_STT_NOTICE: "_OrderedDict[str, float]" = _OrderedDict()
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


def _get_agent_language(clinic_config: Optional[dict[str, Any]]) -> str:
    """Return the configured agent language for the clinic, defaulting to pt-BR."""
    if not clinic_config:
        return "pt-BR"
    business = clinic_config.get("business_rules") or {}
    return cast(str, business.get("agent_language") or "pt-BR")


async def _send_reply_with_optional_tts(
    *,
    credentials: dict[str, Any],
    text: str,
    language_code: str,
    channel: str,
    phone: str,
    input_was_audio: bool,
    send_audio: Callable[[bytes, str], Awaitable[dict[str, Any]]],
    send_text: Callable[[], Awaitable[dict[str, Any]]],
    force_text: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Deliver an agent reply as audio when enabled, otherwise as text.

    TTS is deliberately best-effort: an incomplete configuration, provider
    outage or channel upload failure falls back to the original text so the
    patient never loses the agent's response.

    When ``force_text`` is True the reply is always sent as text and TTS is
    skipped entirely, regardless of the clinic's ``tts_enabled`` setting.
    Callers use this to protect deterministic responses (e.g. scheduling
    links) whose text form must stay clickable for the patient.
    """

    reply_policy = credentials.get("tts_reply_policy") or "audio_input"
    policy_allows_audio = reply_policy == "all_inputs" or (
        reply_policy == "audio_input" and input_was_audio
    )
    if (
        force_text
        or not _is_enabled(credentials.get("tts_enabled"))
        or not policy_allows_audio
    ):
        return await send_text(), False

    try:
        audio = await synthesize_speech(
            text,
            credentials,
            language_code=language_code,
        )
    except Exception as exc:
        error_code = getattr(exc, "code", type(exc).__name__)
        logger.warning(
            "%s TTS synthesis failed for %s (reason=%s); using text fallback",
            channel,
            phone,
            error_code,
        )
        return await send_text(), False

    try:
        return await send_audio(audio.content, audio.mime_type), True
    except Exception as exc:
        error_code = getattr(exc, "code", type(exc).__name__)
        logger.warning(
            "%s audio delivery failed for %s (reason=%s); using text fallback",
            channel,
            phone,
            error_code,
        )
        return await send_text(), False


def _get_redis() -> Any:
    """Get Redis client (lazy import to avoid circular deps)."""
    try:
        import redis

        from src.config.settings import settings

        redis_from_url = cast(Callable[..., Any], redis.from_url)
        return redis_from_url(settings.redis_url, decode_responses=True)
    except Exception:
        return None


def _track_sent_message(zapi_response: dict[str, Any]) -> None:
    """Store the messageId from a Z-API send response (Redis + in-memory fallback).

    Always writes to the in-process dict so echoes are recognised even if Redis
    is temporarily down between _track and _is_ai_sent_message calls.
    """
    mid = (
        zapi_response.get("messageId")
        or zapi_response.get("zapiMessageId")
        or zapi_response.get("MessageID")  # wuzapi capitalisation
        or zapi_response.get("messageid")  # Uazapi commercial
    )
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


def _stt_fallback_notice_key(tenant_id: UUID, phone: str) -> str:
    """Decision 12's notice key: ``stt_fallback_notice:{tenant_id}:{phone}``."""
    return f"{_STT_FALLBACK_NOTICE_PREFIX}{tenant_id}:{phone}"


def _stt_fallback_notice_pending(tenant_id: UUID, phone: str) -> bool:
    """Whether this patient may receive a reroute notice right now.

    Mirrors :func:`_is_ai_sent_message`: in-memory first (always available),
    then Redis (authoritative across workers). Unlike that function this one
    does **not** consume the entry — the window has to keep suppressing until
    it expires on its own.
    """
    key = _stt_fallback_notice_key(tenant_id, phone)
    expiry = _INMEM_STT_NOTICE.get(key)
    if expiry is not None:
        if _time.monotonic() < expiry:
            return False
        _INMEM_STT_NOTICE.pop(key, None)

    r = _get_redis()
    if r:
        try:
            if r.exists(key):
                return False
        except Exception:
            pass  # Redis failure is non-fatal — in-memory already answered
    return True


def _mark_stt_fallback_notice_sent(tenant_id: UUID, phone: str) -> None:
    """Open a 6h suppression window after a reroute notice was delivered.

    Called only once the send succeeded, so a provider outage does not cost
    the patient six hours of silence.
    """
    key = _stt_fallback_notice_key(tenant_id, phone)
    _INMEM_STT_NOTICE[key] = _time.monotonic() + _STT_FALLBACK_NOTICE_TTL
    while len(_INMEM_STT_NOTICE) > _INMEM_MAX:
        _INMEM_STT_NOTICE.popitem(last=False)

    r = _get_redis()
    if r:
        try:
            r.setex(key, _STT_FALLBACK_NOTICE_TTL, "1")
        except Exception:
            pass  # Redis failure is non-fatal — in-memory bounds the window


class ReplayStoreUnavailable(RuntimeError):
    """Raised when the replay store cannot be consulted in production.

    Production requires the Redis-backed 31-day dedup window because we
    run multiple Uvicorn workers; when Redis is unavailable the webhook
    must fail closed with HTTP 503, never quietly fall back to the
    per-process in-memory store (which would only cover one worker).
    """


def _claim_incoming_message(
    source: str,
    tenant_id: UUID,
    message_id: str,
) -> bool:
    """Return True only for the first delivery of an inbound provider message.

    Uses Redis ``SET NX EX 2678400`` (31 days) as the authoritative claim
    store shared across workers. When Redis is unavailable in production
    the caller must translate the raised :class:`ReplayStoreUnavailable`
    into an HTTP 503 fail-closed response.

    In test / development environments with a single worker, an in-memory
    fallback is permitted so tests keep running without a live Redis; it
    is explicitly not durable and not distributed.
    """
    if not message_id:
        return True

    key = f"{source}:{tenant_id}:{message_id}"
    r = _get_redis()
    if r:
        try:
            claimed = r.set(
                f"{_INCOMING_ID_PREFIX}{key}",
                "1",
                nx=True,
                ex=REPLAY_TTL_SECONDS,
            )
            return bool(claimed)
        except Exception as exc:
            if settings.is_production:
                raise ReplayStoreUnavailable(str(exc)) from exc
            # non-production single-worker fallback: fall through to memory
    else:
        if settings.is_production:
            raise ReplayStoreUnavailable("redis_unavailable")

    now = _time.monotonic()
    expiry = _INMEM_INCOMING.get(key)
    if expiry and now < expiry:
        return False
    if expiry:
        _INMEM_INCOMING.pop(key, None)

    _INMEM_INCOMING[key] = now + REPLAY_TTL_SECONDS
    while len(_INMEM_INCOMING) > _INMEM_MAX:
        _INMEM_INCOMING.popitem(last=False)
    return True


# ─────────────────────────────────────────────────────────────────────
# Authenticity primitives (Z-API capability + Meta HMAC)
# ─────────────────────────────────────────────────────────────────────


def _is_canonical_zapi_token(raw_token: str) -> bool:
    """Reject any non-canonical Z-API capability token form.

    The wire format is exactly 43 ASCII characters from ``[A-Za-z0-9_-]``
    (32 random bytes base64url-encoded without padding). Percent-encoded,
    padded, Unicode, wrong-length, and decoded-only tokens all fail here
    before we ever touch the database or Redis.
    """

    if not isinstance(raw_token, str):
        return False
    if len(raw_token) != ZAPI_CAPABILITY_TOKEN_LEN:
        return False
    # Explicitly reject any percent-encoded byte — these would round-trip
    # to a 43-char string only after decoding, which the wish forbids.
    if "%" in raw_token:
        return False
    return bool(ZAPI_CAPABILITY_TOKEN_PATTERN.fullmatch(raw_token))


def _zapi_token_digest(raw_token: str) -> str:
    """Return the lowercase-hex SHA-256 of the exact raw token bytes."""

    return hashlib.sha256(raw_token.encode("ascii")).hexdigest()


async def _resolve_tenant_by_zapi_digest(
    digest: str,
    session: AsyncSession,
) -> Optional[tuple[UUID, dict[str, Any]]]:
    """Return the single tenant + credentials matching the token digest.

    Zero or multiple matches yield ``None`` — the caller translates that
    into a generic 401 without leaking which case triggered.
    """

    stmt = select(
        ClinicConfigTable.tenant_id,
        ClinicConfigTable.api_credentials,
    ).where(
        ClinicConfigTable.api_credentials["zapi_inbound_token_digest"].astext == digest,
    )
    result = await session.execute(stmt)
    rows = result.all()
    if len(rows) != 1:
        return None
    tenant_id, creds = rows[0]
    return tenant_id, dict(creds or {})


def _extract_meta_message_bearing_phone_ids(body: dict[str, Any]) -> list[str]:
    """Collect every ``phone_number_id`` whose change carries at least one message.

    Only ``changes`` whose ``field == "messages"`` and whose ``value.messages``
    is a non-empty list are considered. Status-only entries never contribute
    to the tenant binding, so the wish's "exactly one message-bearing phone
    id" rule stays independent of delivery/read receipts.
    """

    seen: list[str] = []
    entries = body.get("entry") or []
    if not isinstance(entries, list):
        return seen
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []) or []:
            if not isinstance(change, dict):
                continue
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            messages = value.get("messages") or []
            if not isinstance(messages, list) or not messages:
                continue
            metadata = value.get("metadata") or {}
            pnid = str((metadata or {}).get("phone_number_id") or "").strip()
            if pnid and pnid not in seen:
                seen.append(pnid)
    return seen


async def _resolve_tenant_by_meta_phone_number_id(
    phone_number_id: str,
    session: AsyncSession,
) -> Optional[tuple[UUID, dict[str, Any]]]:
    """Return the single tenant + credentials that own a Meta phone id."""

    stmt = select(
        ClinicConfigTable.tenant_id,
        ClinicConfigTable.api_credentials,
    ).where(
        ClinicConfigTable.api_credentials["meta_phone_number_id"].astext
        == phone_number_id,
    )
    result = await session.execute(stmt)
    rows = result.all()
    if len(rows) != 1:
        return None
    tenant_id, creds = rows[0]
    return tenant_id, dict(creds or {})


def _verify_meta_signature(
    signature_header: Optional[str],
    body_bytes: bytes,
    app_secret: str,
) -> bool:
    """Return True iff ``X-Hub-Signature-256`` matches the raw body HMAC.

    The comparison uses :func:`hmac.compare_digest` so no timing leak
    reveals partial matches. Any malformed header, missing algorithm
    prefix, or non-hex digest returns ``False``.
    """

    if not signature_header or not isinstance(signature_header, str):
        return False
    if not app_secret or not isinstance(app_secret, str):
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    provided = signature_header[len(prefix) :].strip().lower()
    if len(provided) != 64:
        return False
    try:
        int(provided, 16)
    except ValueError:
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), body_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


def _stt_canonical_provider(raw_provider: Optional[str]) -> str:
    """Resolve the canonical STT provider id, defaulting to ``outros``.

    Group 2's ``api_credentials`` module owns the allowlist; here we only
    normalise empty/None values to the ``outros`` default that preserves
    the historical pipeline.
    """

    if not raw_provider or not isinstance(raw_provider, str):
        return "outros"
    return raw_provider.strip() or "outros"


def _stt_skip_envelope(provider: str) -> dict[str, Any]:
    """Return the exact response body for an unsupported STT provider."""

    return {"ok": True, "skipped": "stt_provider_unsupported", "provider": provider}


# ── DB-backed AI message deduplication (cross-worker safe) ───────────


def _session_has_ai_msg_id(session: Any, message_id: str) -> bool:
    """Return True (and consume the entry) if message_id is a recent bot-sent ID.

    Stored in session.session_metadata["ai_msg_ids"] as [[id, monotonic_ts], ...].
    This is shared across all Uvicorn workers via PostgreSQL.

    The mapped attribute is ``session_metadata`` — ``Session.metadata`` is
    SQLAlchemy's declarative ``MetaData`` object, and reading it here raised
    ``TypeError: 'MetaData' object is not iterable`` for every fromMe message
    that had an active session.
    """
    meta = dict(getattr(session, "session_metadata", None) or {})
    entries: list[Any] = meta.get("ai_msg_ids", [])
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
        session.session_metadata = meta
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
        meta = dict(getattr(sess, "session_metadata", None) or {})
        entries: list[Any] = meta.get("ai_msg_ids", [])
        now = _time.monotonic()
        # Prune expired, cap at 20
        entries = [[m, t] for m, t in entries if now - t < _SENT_ID_TTL]
        entries.append([message_id, now])
        entries = entries[-20:]
        meta["ai_msg_ids"] = entries
        sess.session_metadata = meta
        await db.flush()
    except Exception as _exc:
        logger.warning("_store_ai_msg_id_to_db failed (non-fatal): %s", _exc)


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
    return (
        raw.lstrip("+")
        .replace("-", "")
        .replace(" ", "")
        .replace("(", "")
        .replace(")", "")
    )


def _extract_uazapi_text_value(value: Any) -> Optional[str]:
    """Return Uazapi text when the field is a string or a nested text object."""
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, dict):
        nested_text = value.get("text")
        if isinstance(nested_text, str):
            return nested_text if nested_text else None
    return None


def _extract_uazapi_message_text(msg: dict[str, Any]) -> Optional[str]:
    """Defensive text extraction for Uazapi ``text`` / ``content`` variants."""
    direct_text = _extract_uazapi_text_value(msg.get("text"))
    if direct_text is not None:
        return direct_text
    return _extract_uazapi_text_value(msg.get("content"))


def _is_uazapi_group_chat(chat_jid: str) -> bool:
    """Group conversations are out of scope for the Uazapi text pipeline."""
    return isinstance(chat_jid, str) and chat_jid.endswith("@g.us")


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
    lgpd: dict[str, Any],
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


def _extract_normalised_send_id(send_result: Any) -> str:
    """Return the outbound message id in the shape the provider's echo carries.

    - Reads ``messageid`` / ``MessageID`` / ``messageId`` / ``zapiMessageId``
      / ``id`` (in that order) — the union of every key the four channels
      have ever returned.
    - Normalises composite ids of the form ``owner:MSGID`` produced by
      Uazapi's ``id`` field by stripping everything up to the last ``:``.
      The pure ``messageid`` value (what actually comes back on the echo)
      is what dedup layers need to store.
    - Returns ``""`` when nothing usable was found. Never raises — callers
      use this on fire-and-forget tracking paths.

    Fixes the divergence flagged by Decisions 4 and 11 in ``uazapi-parity``:
    before this helper Uazapi could store ``owner:MSGID`` (never matches the
    echo) and Evolution stored ``id`` in one dedup layer while
    ``_track_sent_message`` never read that key at all.
    """
    if not isinstance(send_result, dict):
        return ""
    for key in ("messageid", "MessageID", "messageId", "zapiMessageId", "id"):
        candidate = send_result.get(key)
        if isinstance(candidate, str) and candidate:
            # Uazapi's ``id`` arrives as ``owner:MSGID``; the echo only
            # carries the tail ``MSGID`` in ``messageid``. Strip once, at
            # the last ``:``, so we always store the same value the echo
            # will produce.
            return candidate.rsplit(":", 1)[-1] if ":" in candidate else candidate
    return ""


async def _send_privacy_notice(
    *,
    channel_label: str,
    is_new: bool,
    lgpd_cfg: dict[str, Any],
    clinic_config: Optional[dict[str, Any]],
    tenant_id: UUID,
    session_id: UUID,
    to_phone: str,
    db: AsyncSession,
    send: Callable[[str], Awaitable[dict[str, Any]]],
    id_extractor: Callable[[dict[str, Any]], str] = _extract_normalised_send_id,
) -> None:
    """Send the LGPD privacy notice once, on the very first turn of a session.

    Encapsulates the block that four channels (Z-API, Evolution, Uazapi, Meta)
    would otherwise repeat verbatim:

    - Bail early when the session already exists or ``lgpd_enabled`` is off.
    - Build the localised message via ``_build_lgpd_message`` +
      ``_get_agent_language``.
    - Invoke the channel-specific ``send`` callable with the ready-to-ship
      copy; the caller closes over the provider credentials so this helper
      never learns about any of them.
    - Track the outbound id (``_track_sent_message`` + ``_store_ai_msg_id_to_db``)
      using the **normalised** value from ``id_extractor`` — the same value on
      both dedup layers, satisfying Decisions 4 and 11 of ``uazapi-parity``.
    - Commit immediately (Decision 2) so the id survives even if the pipeline
      that runs after crashes: without the commit the echo of the notice
      arrives inside a window where nothing in the DB matches.
    - Any exception is logged and swallowed. The privacy notice is
      never allowed to break the patient's actual reply.
    """
    if not is_new:
        return
    if not _is_enabled(lgpd_cfg.get("lgpd_enabled")):
        return
    try:
        clinic_name = (clinic_config or {}).get("identity", {}).get("clinic_name", "")
        privacy_msg = _build_lgpd_message(
            lgpd_cfg,
            clinic_id=str(tenant_id),
            clinic_name=clinic_name,
            agent_language=_get_agent_language(clinic_config),
        )
        send_result = await send(privacy_msg)
        mid = id_extractor(send_result or {})
        if mid:
            _track_sent_message({"messageid": mid})
            await _store_ai_msg_id_to_db(session_id, mid, db)
            await db.commit()
        logger.info(
            "%s LGPD privacy message sent to %s",
            channel_label,
            to_phone,
        )
    except Exception as exc:
        logger.warning(
            "%s LGPD send failed for %s — %s",
            channel_label,
            to_phone,
            exc,
        )


async def _load_tenant_credentials(
    tenant_id: UUID,
    db: AsyncSession,
) -> dict[str, Any]:
    """Load api_credentials for a tenant."""
    stmt = select(ClinicConfigTable.api_credentials).where(
        ClinicConfigTable.tenant_id == tenant_id,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() or {}


async def _load_handoff_config(
    tenant_id: UUID,
    db: AsyncSession,
) -> Any:
    """Load handoff config bucket for a tenant."""
    stmt = select(ClinicConfigTable.handoff).where(
        ClinicConfigTable.tenant_id == tenant_id,
    )
    result = await db.execute(stmt)
    loaded = result.scalar_one_or_none()
    return loaded if loaded is not None else {}


async def _check_pause_state(
    tenant_id: UUID,
    phone: str,
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> Optional[dict[str, Any]]:
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

    current_time = now or datetime.now(timezone.utc)

    # Lazy unpause: paused_until expired. SQLite test/dev storage may return a
    # naive UTC datetime even though PostgreSQL preserves timezone metadata.
    deadline = session.paused_until
    comparable_deadline = deadline
    if (
        comparable_deadline is not None
        and comparable_deadline.tzinfo is None
        and current_time.tzinfo is not None
    ):
        comparable_deadline = comparable_deadline.replace(tzinfo=current_time.tzinfo)
    if comparable_deadline and comparable_deadline <= current_time:
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
            "paused_until": (
                session.paused_until.isoformat()
                if session.paused_until
                else "permanent"
            ),
        },
    )
    return {"ok": True, "skipped": "session_paused"}


async def _check_bot_active(
    tenant_id: UUID,
    phone: str,
    db: AsyncSession,
) -> Optional[dict[str, Any]]:
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
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
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

    handoff_bucket = await _load_handoff_config(tenant_id, db)
    handoff_cfg = handoff_bucket if isinstance(handoff_bucket, dict) else {}
    current_time = now or datetime.now(timezone.utc)
    import re as _re

    def _normalize_for_match(s: str) -> str:
        """Normalize: lowercase, collapse whitespace, remove spaces around punctuation."""
        s = " ".join(s.strip().lower().split())
        s = _re.sub(r"\s*([,;:.!?])\s*", r"\1", s)
        return s

    trigger_msg = _normalize_for_match(
        str(
            handoff_cfg.get(
                "trigger_message",
                "Precisando de algo mais,estamos a disposição.",
            )
        )
    )
    normalized_text = _normalize_for_match(message_text)

    # Check if message contains the trigger phrase (flexible match, punctuation-tolerant)
    if trigger_msg in normalized_text:
        if session.is_paused:
            raw_reactivation_hours = handoff_cfg.get("reactivation_delay_hours", 1)
            fallback_reason: str | None = None
            if isinstance(raw_reactivation_hours, bool) or not isinstance(
                raw_reactivation_hours, (int, str)
            ):
                reactivation_hours = 1
                fallback_reason = "invalid_duration"
            else:
                try:
                    reactivation_hours = int(raw_reactivation_hours)
                    if reactivation_hours <= 0:
                        reactivation_hours = 1
                        fallback_reason = "invalid_duration"
                except ValueError:
                    reactivation_hours = 1
                    fallback_reason = "invalid_duration"
            try:
                reactivates_at = current_time + timedelta(hours=reactivation_hours)
            except OverflowError:
                reactivation_hours = 1
                reactivates_at = current_time + timedelta(hours=reactivation_hours)
                fallback_reason = "unrepresentable_duration"
            if fallback_reason is not None:
                logger.warning(
                    "handoff_reactivation_config_tolerated",
                    extra={
                        "tenant_id": str(tenant_id),
                        "session_id": str(session.id),
                        "field": "reactivation_delay_hours",
                        "reason": fallback_reason,
                    },
                )
            session.paused_until = reactivates_at
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

    # Not trigger message — resolve the shared human-takeover pause policy.
    decision = resolve_handoff_pause(handoff_bucket, current_time)
    for notice in decision.notices:
        logger.warning(
            "handoff_pause_policy_tolerated",
            extra={
                "tenant_id": str(tenant_id),
                "session_id": str(session.id),
                "field": notice.field,
                "reason": notice.reason,
            },
        )
    if not decision.should_pause:
        return {"ok": True, "skipped": "handoff_inactive"}

    if session.is_paused:
        return {"ok": True, "action": "already_paused"}

    # Human takeover: pause session (no mark-unread — secretary is in conversation)
    session.is_paused = True
    session.paused_until = decision.paused_until

    await db.flush()
    logger.info(
        "session_paused",
        extra={
            "event": "session_paused",
            "reason": "human_takeover",
            "session_id": str(session.id),
            "tenant_id": str(tenant_id),
            "phone": phone,
            "paused_until": (
                session.paused_until.isoformat() if session.paused_until else None
            ),
        },
    )
    return {"ok": True, "action": "human_takeover"}


# ─────────────────────────────────────────────────────────────────────
# Audio fallback chain — shared by Z-API and Meta Cloud API
#
# One chain, two channels (Decision 9): speech-to-text, then a multimodal
# chat model, then — when neither produced a transcript — a text reroute
# asking the patient to type. Parity is about the message the patient gets
# and the cause it was chosen for, not about the internal ordering each
# channel uses to reach the chain.
# ─────────────────────────────────────────────────────────────────────

# Per-provider STT model defaults — never send a Groq model name to OpenAI.
_STT_DEFAULT_MODELS: dict[str, str] = {
    "openai_whisper": "whisper-1",
}


def _resolve_stt_model(credentials: dict[str, Any]) -> str:
    """Canonical-first STT model name with a per-provider default."""
    provider = _stt_canonical_provider(credentials.get("stt_provider"))
    model = credentials.get("stt_model_name") or credentials.get("whisper_model")
    return str(model or _STT_DEFAULT_MODELS.get(provider, "whisper-large-v3-turbo"))


def _multimodal_operational(credentials: dict[str, Any]) -> bool:
    """Decision 14's "multimodal operante" predicate.

    Complete multimodal credentials **and** a provider proven to transcribe
    the OGG/Opus WhatsApp actually delivers. Mirrors ``multimodal_complete``
    in :mod:`src.services.api_credentials`; only the provider frozenset is
    imported, so this stays a local read of a shared constant rather than a
    shared helper that two concurrent groups would both be editing.
    """
    if credentials.get("multimodal_provider") not in OPERATIONAL_MULTIMODAL_PROVIDERS:
        return False
    return all(
        bool(credentials.get(field))
        for field in (
            "multimodal_api_url",
            "multimodal_api_key",
            "multimodal_model_name",
        )
    )


async def _stt_transcribe_audio(
    audio_bytes: bytes,
    mime_type: str,
    creds: dict[str, Any],
    agent_language: str,
) -> Optional[str]:
    """First rung: the dedicated speech-to-text provider.

    Returns the transcript (possibly empty for silent or unintelligible
    audio), or ``None`` when no STT key is configured at all. Raises the
    underlying exception on transcription failure so the chain can log it
    with a stack trace and move to the next rung.

    Key resolution is canonical-first, matching ``resolve_stt_credentials``:
    a legacy ``whisper_api_key`` is only a fallback for rows written before
    ``stt_api_key`` existed.
    """
    stt_provider = _stt_canonical_provider(creds.get("stt_provider"))
    whisper_key = (creds.get("stt_api_key") or "").strip() or (
        creds.get("whisper_api_key") or ""
    ).strip()
    lang_hint = "en" if agent_language == "en" else "pt"

    if stt_provider in {"elevenlabs_scribe", "openrouter"}:
        api_url = creds.get("stt_api_url")
        api_key = creds.get("stt_api_key")
        model_name = creds.get("stt_model_name")
        if not all(
            isinstance(value, str) and bool(value.strip())
            for value in (api_url, api_key, model_name)
        ):
            from src.services.stt_service import SttTranscriptionError

            raise SttTranscriptionError("stt_configuration_invalid")

        from src.services.stt_service import transcribe_audio as transcribe_fixed_stt

        return await transcribe_fixed_stt(
            provider=stt_provider,
            api_url=cast(str, api_url),
            api_key=cast(str, api_key),
            audio_bytes=audio_bytes,
            mimetype=mime_type,
            model_name=cast(str, model_name),
            language=lang_hint,
        )

    if not whisper_key:
        return None

    from src.services.groq_service import (
        GROQ_API_BASE,
        OPENAI_API_BASE,
        transcribe_audio,
    )

    stt_api_url = (creds.get("stt_api_url") or "").strip()

    # Resolve STT base URL: use the saved URL (set by the UI for any
    # provider), fall back to known defaults, then Groq as last resort for
    # legacy configs without a saved URL.
    if stt_api_url:
        stt_base_url = stt_api_url
    elif stt_provider == "openai_whisper":
        stt_base_url = OPENAI_API_BASE
    else:
        stt_base_url = GROQ_API_BASE

    return await transcribe_audio(
        api_key=whisper_key,
        audio_bytes=audio_bytes,
        mimetype=mime_type,
        model=_resolve_stt_model(creds),
        language=lang_hint,
        base_url=stt_base_url,
    )


async def _multimodal_transcribe_audio(
    audio_bytes: bytes,
    mime_type: str,
    creds: dict[str, Any],
) -> str:
    """Second rung: a multimodal chat model reading the audio directly.

    WhatsApp's OGG/Opus is passed through untouched — OpenRouter accepts it
    verbatim, so no transcoding step exists anywhere in this chain.
    """
    from src.services.multimodal_service import transcribe_audio_multimodal

    return await transcribe_audio_multimodal(
        api_key=str(creds.get("multimodal_api_key") or ""),
        audio_bytes=audio_bytes,
        mimetype=mime_type,
        model=str(creds.get("multimodal_model_name") or ""),
        base_url=str(creds.get("multimodal_api_url") or ""),
    )


async def _transcribe_audio_chain(
    audio_bytes: Optional[bytes],
    credentials: dict[str, Any],
    *,
    mime_type: str,
    agent_language: str,
    channel: str,
) -> tuple[str, str]:
    """Run the audio fallback chain. Returns ``(transcript, failure_reason)``.

    ``failure_reason`` is :data:`STT_CHAIN_OK` (the empty string) exactly
    when ``transcript`` is usable. Otherwise the transcript is empty and the
    reason is one of :data:`STT_CHAIN_NO_CAPABILITY` (nothing was ever
    configured — Decision 6's honest variant) or :data:`STT_CHAIN_FAILED`
    (something was configured and did not deliver a transcript just now).

    ``audio_bytes=None`` is a legitimate input, not an error: it is the
    no-capability path where the caller deliberately downloaded nothing. It
    keeps the message choice in the same place on both channels instead of
    forking inside the helper on one and outside it on the other.

    An empty transcript is a failure, not a success: ``transcribe_audio``
    returns ``""`` for silent or unintelligible audio, and letting that
    through would drop the patient into a silent skip with no fallback.
    """
    stt_provider = _stt_canonical_provider(credentials.get("stt_provider"))
    has_stt = stt_provider in {"elevenlabs_scribe", "openrouter"} or bool(
        (credentials.get("stt_api_key") or "").strip()
        or (credentials.get("whisper_api_key") or "").strip()
    )
    has_multimodal = _multimodal_operational(credentials)

    if not has_stt and not has_multimodal:
        return "", STT_CHAIN_NO_CAPABILITY

    if audio_bytes is None:
        # A configured clinic always downloads before reaching the chain;
        # arriving here without bytes means the media never materialised.
        logger.warning("%s audio fallback chain reached with no bytes", channel)
        return "", STT_CHAIN_FAILED

    if has_stt:
        try:
            stt_text = await _stt_transcribe_audio(
                audio_bytes=audio_bytes,
                mime_type=mime_type,
                creds=credentials,
                agent_language=agent_language,
            )
        except Exception as exc:
            logger.exception(
                "%s audio transcription failed at the STT rung — %s", channel, exc
            )
        else:
            if stt_text and stt_text.strip():
                return stt_text.strip(), STT_CHAIN_OK
            logger.info("%s STT rung returned an empty transcript", channel)

    if has_multimodal:
        try:
            mm_text = await _multimodal_transcribe_audio(
                audio_bytes=audio_bytes,
                mime_type=mime_type,
                creds=credentials,
            )
        except Exception as exc:
            logger.exception(
                "%s audio transcription failed at the multimodal rung — %s",
                channel,
                exc,
            )
        else:
            from src.services.multimodal_service import NO_SPEECH_MARKER

            cleaned = mm_text.strip()
            # The adapter's prompt asks for this exact marker when the audio
            # carries no intelligible speech. Forwarding it would make the
            # marker itself the patient's message; treat it as "no usable
            # transcript" so the text reroute takes over instead.
            if cleaned and cleaned != NO_SPEECH_MARKER:
                return cleaned, STT_CHAIN_OK
            logger.info("%s multimodal rung returned no intelligible speech", channel)

    return "", STT_CHAIN_FAILED


def _stt_fallback_message(failure_reason: str, agent_language: str) -> str:
    """Pick the reroute copy for a chain failure, in the agent's language."""
    if failure_reason == STT_CHAIN_NO_CAPABILITY:
        return (
            _STT_NEVER_CONFIGURED_MSG_EN
            if agent_language == "en"
            else _STT_NEVER_CONFIGURED_MSG_PT
        )
    return (
        _STT_TEMPORARY_FAILURE_MSG_EN
        if agent_language == "en"
        else _STT_TEMPORARY_FAILURE_MSG_PT
    )


async def _send_zapi_stt_fallback(
    *,
    tenant_id: UUID,
    phone: str,
    z_inst: str,
    z_tok: str,
    z_ct: str,
    agent_language: str,
    failure_reason: str,
) -> dict[str, Any]:
    """Deliver the Z-API text reroute and mark the conversation unread.

    No handoff and no session mutation: the patient is being asked to type,
    not escalated. The unread mark is a Z-API-only affordance so the clinic
    sees there is a voice note it may want to listen to.
    """
    message = _stt_fallback_message(failure_reason, agent_language)

    if not z_inst or not z_tok:
        logger.warning(
            "Z-API STT fallback undeliverable for %s — send credentials missing",
            phone,
        )
        return {"ok": True, "replied": False, "action": "stt_fallback_undeliverable"}

    if not _stt_fallback_notice_pending(tenant_id, phone):
        logger.info("Z-API STT fallback suppressed for %s (6h window open)", phone)
        return {"ok": True, "replied": False, "action": "stt_fallback_suppressed"}

    try:
        send_result = await send_text_message(
            z_inst, z_tok, phone, message, client_token=z_ct
        )
        _track_sent_message(send_result)
    except Exception as exc:
        logger.warning("Z-API STT fallback send failed for %s — %s", phone, exc)
        return {"ok": True, "replied": False, "action": "stt_fallback_send_failed"}

    _mark_stt_fallback_notice_sent(tenant_id, phone)

    try:
        await mark_conversation_unread(z_inst, z_tok, phone, client_token=z_ct)
    except Exception as exc:
        logger.warning("Z-API STT fallback mark_unread failed for %s — %s", phone, exc)

    logger.info("Z-API STT fallback sent to %s (reason=%s)", phone, failure_reason)
    return {"ok": True, "replied": True, "action": "stt_fallback"}


async def _send_meta_stt_fallback(
    *,
    tenant_id: UUID,
    from_phone: str,
    meta_pnid: str,
    meta_token: str,
    agent_language: str,
    failure_reason: str,
) -> dict[str, Any]:
    """Deliver the Meta Cloud API text reroute — the complete effect there.

    The Cloud API has no unread primitive, so unlike Z-API this sends the
    message and nothing else: no handoff, no session mutation.
    """
    message = _stt_fallback_message(failure_reason, agent_language)

    # ``meta_token`` needs no handling: its absence already exited with
    # ``{"skipped": "no_credentials"}`` before the audio was ever resolved.
    if not meta_pnid:
        logger.warning(
            "Meta STT fallback undeliverable for %s — phone_number_id missing",
            from_phone,
        )
        return {"ok": True, "replied": False, "action": "stt_fallback_undeliverable"}

    if not _stt_fallback_notice_pending(tenant_id, from_phone):
        logger.info("Meta STT fallback suppressed for %s (6h window open)", from_phone)
        return {"ok": True, "replied": False, "action": "stt_fallback_suppressed"}

    try:
        send_result = await send_meta_cloud_text(
            meta_pnid,
            meta_token,
            from_phone,
            message,
        )
        _track_meta_sent_message(send_result)
    except Exception as exc:
        logger.warning("Meta STT fallback send failed for %s — %s", from_phone, exc)
        return {"ok": True, "replied": False, "action": "stt_fallback_send_failed"}

    _mark_stt_fallback_notice_sent(tenant_id, from_phone)

    logger.info("Meta STT fallback sent to %s (reason=%s)", from_phone, failure_reason)
    return {"ok": True, "replied": True, "action": "stt_fallback"}


async def _send_uazapi_stt_fallback(
    *,
    tenant_id: UUID,
    phone: str,
    u_url: str,
    u_token: str,
    agent_language: str,
    failure_reason: str,
) -> dict[str, Any]:
    """Deliver the Uazapi text reroute — parity with Z-API/Meta fallbacks.

    Uazapi has no ``mark_unread`` primitive confirmed in the spec, so this
    only sends the reroute text. No handoff, no session mutation, no DB
    echo storage — parity with the Z-API and Meta variants (both of which
    also skip DB storage; only the main AI reply persists ids).
    """
    message = _stt_fallback_message(failure_reason, agent_language)

    if not u_url or not u_token:
        logger.warning(
            "Uazapi STT fallback undeliverable for %s — credentials missing",
            phone,
        )
        return {"ok": True, "replied": False, "action": "stt_fallback_undeliverable"}

    if not _stt_fallback_notice_pending(tenant_id, phone):
        logger.info("Uazapi STT fallback suppressed for %s (6h window open)", phone)
        return {"ok": True, "replied": False, "action": "stt_fallback_suppressed"}

    try:
        send_result = await send_uazapi_text(u_url, u_token, phone, message)
        _track_sent_message(send_result)
    except Exception as exc:
        logger.warning("Uazapi STT fallback send failed for %s — %s", phone, exc)
        return {"ok": True, "replied": False, "action": "stt_fallback_send_failed"}

    _mark_stt_fallback_notice_sent(tenant_id, phone)

    logger.info("Uazapi STT fallback sent to %s (reason=%s)", phone, failure_reason)
    return {"ok": True, "replied": True, "action": "stt_fallback"}


# ── Z-API webhook endpoint ──────────────────────────────────────────


@router.post(
    "/zapi",
    summary="Z-API Legacy Webhook (deprecated)",
    description=(
        "Legacy Z-API webhook without capability authentication. "
        "Returns 200 with ``{'skipped': 'legacy'}`` and does not perform any "
        "downstream processing — clinics must register the "
        "``/zapi/{capability_token}`` URL instead."
    ),
    tags=["Webhook"],
    status_code=200,
)
async def zapi_webhook_legacy(request: Request) -> dict[str, Any]:
    """Legacy Z-API path returns a 200 sentinel with zero downstream effects.

    We deliberately consume the body so upstream retries stop; nothing about
    the payload is inspected, logged, or forwarded.
    """

    try:
        await request.body()
    except Exception:  # pragma: no cover — body reads are cheap in tests
        pass
    return {"ok": True, "skipped": "legacy"}


@router.post(
    "/zapi/{capability_token}",
    summary="Z-API Webhook (capability URL)",
    description=(
        "Receives incoming WhatsApp messages from Z-API through the "
        "capability-authenticated URL registered by the clinic. The "
        "canonical token is 43 base64url characters — non-canonical forms "
        "are rejected before any database or Redis lookup."
    ),
    tags=["Webhook"],
    response_model=None,
    status_code=200,
)
async def zapi_webhook(
    capability_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
) -> Response | dict[str, Any]:
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
    # ── 1) Canonical token validation (before any DB / Redis lookup) ──
    if not _is_canonical_zapi_token(capability_token):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )

    # ── 2) Read raw body once so authenticity stays bound to the exact bytes ──
    raw_body = await request.body()
    try:
        import json as _json

        body: dict[str, Any] = _json.loads(raw_body or b"{}")
    except Exception:
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )

    # ── 3) Digest lookup — exactly one tenant must match ──
    digest = _zapi_token_digest(capability_token)
    resolved = await _resolve_tenant_by_zapi_digest(digest, db)
    if resolved is None:
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )
    tenant_id, tenant_creds = resolved

    # ── 4) Body instanceId must match the resolved tenant ──
    body_instance_id = str(body.get("instanceId") or "").strip()
    expected_instance_id = str(tenant_creds.get("zapi_instance_id") or "").strip()
    if not body_instance_id or body_instance_id != expected_instance_id:
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )

    # ── 5) Rate limit (after auth, before replay) ──
    rate_decision = check_webhook_rate_limit(str(tenant_id), "zapi")
    if not rate_decision.allowed:
        return webhook_rate_limit_response(rate_decision)

    # ── 6) messageId presence + replay claim ──
    webhook_msg_id = str(body.get("messageId") or "").strip()
    if not webhook_msg_id:
        return {"ok": True, "skipped": "missing_message_id"}
    try:
        first_delivery = _claim_incoming_message("zapi", tenant_id, webhook_msg_id)
    except ReplayStoreUnavailable:
        return JSONResponse(
            status_code=503,
            content={"error": "replay_store_unavailable"},
        )
    if not first_delivery:
        return {"ok": True, "skipped": "duplicate_message"}

    # ── downstream processing (session / media / reply / handoff) ──
    return await _process_zapi_authenticated_body(
        body=body,
        tenant_id=tenant_id,
        db=db,
    )


async def _process_zapi_authenticated_body(
    *,
    body: dict[str, Any],
    tenant_id: UUID,
    db: AsyncSession,
) -> Response | dict[str, Any]:
    """Downstream Z-API processing for an already authenticated request.

    Callers must have verified: canonical token, digest match, ``instanceId``
    binding, rate limit, ``messageId`` presence and replay-store claim.
    """
    text_obj = body.get("text")
    audio_obj = body.get("audio")

    raw_phone = body.get("phone", "")
    phone = _normalise_phone(raw_phone)
    chat_lid = body.get("chatLid", "")
    sender_name = body.get("senderName", "")

    if not phone:
        return {"ok": True, "skipped": "missing_fields"}

    # ── load clinic_config early so audio/text handling can use credentials ──
    service = get_event_service()
    clinic_config = await service._load_clinic_config(tenant_id)

    # ``clinic_config`` is the cached *runtime projection* — by design it never
    # carries secrets (stt_api_key, zapi_token, zapi_client_token are stripped
    # by ``runtime_projection``). Every site in this request that needs a raw
    # credential must read the row directly; this memo makes that read happen
    # at most once per request without paying for it on early-skip paths.
    _raw_creds_memo: Optional[dict[str, Any]] = None

    async def _tenant_creds() -> dict[str, Any]:
        """Return the tenant's raw ``api_credentials``, loading it once."""
        nonlocal _raw_creds_memo
        if _raw_creds_memo is None:
            _raw_creds_memo = await _load_tenant_credentials(tenant_id, db)
        return _raw_creds_memo

    # ── extract message text (from text payload or audio transcription) ──
    message_text = ""
    if text_obj and isinstance(text_obj, dict):
        message_text = (text_obj.get("message") or "").strip()

    is_from_me = bool(body.get("fromMe", False))
    original_message_text = message_text
    patient_audio_input = (
        bool(audio_obj) and not original_message_text and not is_from_me
    )

    # A clinic staff voice note. It must never be downloaded, transcribed
    # (real STT cost on the clinic's own audio) or answered, but it still has
    # to reach the fromMe handling below so an active human takeover pauses
    # the AI — a secretary replying by voice is as much a takeover as one
    # replying by text.
    staff_audio = bool(audio_obj) and not message_text and is_from_me

    # Handle audio/voice messages.
    # ``not is_from_me`` sits in the block condition — not merely above the
    # download — so no branch inside the block (download, STT, handoff, reply)
    # is reachable at all for the clinic's own audio.
    #
    # The metadata block near the end of this handler reads ``whisper_model``
    # under a *different* guard (``audio_obj and message_text``), which is also
    # true for payloads carrying both text and audio, so it must stay bound.
    whisper_model: Optional[str] = None
    if audio_obj and not message_text and not is_from_me:
        audio_url = audio_obj.get("url") or audio_obj.get("audioUrl")
        mimetype = audio_obj.get("mimeType") or "audio/ogg"
        # Everything that follows is guarded by this *inner* condition, and
        # so is the fallback chain. That is deliberate: ``audio_obj`` present
        # with no ``audio_url``, or a tenant with no config row, is media that
        # never existed rather than a transcription failure, and it keeps
        # falling through to the ``{"skipped": "empty"}`` exit below with no
        # message sent — as does a *text* message with an empty body.
        if audio_url and clinic_config:
            # Raw read: the cached projection deliberately withholds
            # stt_api_key / zapi_token / zapi_client_token.
            creds = await _tenant_creds()
            # Canonical-first, matching ``resolve_stt_credentials``: a legacy
            # ``whisper_api_key`` is only a fallback for rows written before
            # ``stt_api_key`` existed.
            whisper_key = creds.get("stt_api_key") or creds.get("whisper_api_key")
            stt_provider = _stt_canonical_provider(creds.get("stt_provider"))

            # STT providers without a runtime adapter must NOT download the
            # audio, hit any STT endpoint, reply, hand off, or mutate the
            # session — return the exact skip envelope immediately. This gate
            # stays above everything below it, including the reroute: it is an
            # explicit carve-out from the "always answer" rule.
            if stt_provider in UNSUPPORTED_STT_PROVIDERS:
                return _stt_skip_envelope(stt_provider)

            whisper_model = _resolve_stt_model(creds)
            agent_lang = _get_agent_language(clinic_config)

            # Send tokens are read here, above the capability gate, so the
            # branch that downloads nothing can still deliver the reroute.
            z_inst = (creds.get("zapi_instance_id") or "").strip()
            z_tok = (creds.get("zapi_token") or "").strip()
            z_ct = (creds.get("zapi_client_token") or "").strip()

            # Duration-based limit: long audios trigger human handoff instead
            # of transcription. It runs above the capability gate too, because
            # Meta's long-audio handoff runs before anything is known about
            # STT; leaving it inside the gate would give the same 3-minute
            # audio a handoff on Meta and "please type" on Z-API. The duration
            # comes from the payload, so this costs no download.
            audio_max_seconds = int((creds or {}).get("audio_max_seconds") or 60)
            audio_seconds = int(audio_obj.get("seconds") or 0)
            if audio_seconds > audio_max_seconds:
                if z_inst and z_tok:
                    handoff_msg = (
                        _AUDIO_TOO_LONG_MSG_EN
                        if agent_lang == "en"
                        else _AUDIO_TOO_LONG_MSG_PT
                    )
                    await send_text_message(
                        z_inst, z_tok, phone, handoff_msg, client_token=z_ct
                    )
                    # Mark conversation as unread so the clinic team sees the pending audio
                    await mark_conversation_unread(
                        z_inst, z_tok, phone, client_token=z_ct
                    )
                logger.info(
                    "Audio too long (%ds > %ds limit), skipping transcription for %s",
                    audio_seconds,
                    audio_max_seconds,
                    phone,
                )
                return {"ok": True, "skipped": "audio_too_long"}

            # Capability gate: download when *either* rung of the chain can
            # use the bytes. With neither, nothing is downloaded at all and
            # the chain reports "never configured" from ``audio_bytes=None``.
            audio_bytes: Optional[bytes] = None
            effective_mime = mimetype
            if whisper_key or _multimodal_operational(creds):
                # Bound the streaming download by the same audio_max_seconds
                # window the config exposes; safe_download_audio enforces MIME,
                # host, and Content-Length policy uniformly.
                max_bytes = min(
                    audio_max_seconds * 16 * 1024,
                    10 * 1024 * 1024,
                )
                try:
                    download = await safe_download_audio(audio_url, max_bytes=max_bytes)
                except UnsafeEgressError as exc:
                    # Media acquisition failure never enters the chain: there
                    # is no transcription to reroute from, and this exit also
                    # covers hosts we refuse to talk to.
                    logger.warning(
                        "Z-API audio egress rejected: %s",
                        exc.code,
                    )
                    envelope = {
                        "unsafe_api_url": "unsafe_audio_url",
                        "audio_too_large": "audio_too_large",
                        "unsupported_audio_mime": "unsupported_audio_mime",
                        "audio_download_failed": "audio_download_failed",
                        "redirect_forbidden": "unsafe_audio_url",
                    }.get(exc.code, "audio_download_failed")
                    return {"ok": True, "skipped": envelope}
                audio_bytes = download.content
                effective_mime = download.mime_type or mimetype
                logger.info(
                    "Audio received for transcription: size=%d bytes, mimetype=%s",
                    len(audio_bytes),
                    effective_mime,
                )

            message_text, chain_failure = await _transcribe_audio_chain(
                audio_bytes,
                creds,
                mime_type=effective_mime,
                agent_language=agent_lang,
                channel="Z-API",
            )
            if chain_failure:
                return await _send_zapi_stt_fallback(
                    tenant_id=tenant_id,
                    phone=phone,
                    z_inst=z_inst,
                    z_tok=z_tok,
                    z_ct=z_ct,
                    agent_language=agent_lang,
                    failure_reason=chain_failure,
                )

    if not text_obj and not audio_obj:
        return {"ok": True, "skipped": "non-text"}
    # ``staff_audio`` is the one case allowed past the empty-text guard: the
    # audio was deliberately left untranscribed, and the fromMe block below
    # routes it to _handle_clinic_message with empty text — empty text cannot
    # match the trigger phrase, so it falls through to human takeover.
    if not message_text and not staff_audio:
        return {"ok": True, "skipped": "empty"}

    # ── fromMe=true: check if AI-sent echo or real clinic message ──
    if is_from_me:
        webhook_msg_id = body.get("messageId", "")
        # Fast path: in-memory + Redis check (same worker, Redis available)
        if webhook_msg_id and _is_ai_sent_message(webhook_msg_id):
            return {"ok": True, "skipped": "ai_echo"}
        # When phone is in @lid format, resolve real phone from session metadata
        effective_phone = phone
        if "@lid" in raw_phone:
            resolved_phone = await _resolve_phone_from_lid(tenant_id, raw_phone, db)
            if not resolved_phone:
                logger.warning(
                    "zapi_lid_unresolved",
                    extra={"lid": raw_phone, "tenant_id": str(tenant_id)},
                )
                return {"ok": True, "skipped": "lid_unresolved"}
            effective_phone = resolved_phone
        # Pass message_id for DB-backed cross-worker dedup inside _handle_clinic_message
        result = await _handle_clinic_message(
            tenant_id, effective_phone, message_text, db, message_id=webhook_msg_id
        )
        await db.commit()
        return result

    # ── mark message as read (blue checkmarks) ──
    try:
        _creds = await _tenant_creds()
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
        creds_lgpd = await _tenant_creds()
        z_inst = creds_lgpd.get("zapi_instance_id", "").strip()
        z_tok = creds_lgpd.get("zapi_token", "").strip()
        z_ct = creds_lgpd.get("zapi_client_token", "").strip()
        if z_inst and z_tok:

            async def _send_zapi_privacy(msg: str) -> dict[str, Any]:
                return await send_text_message(
                    z_inst,
                    z_tok,
                    phone,
                    msg,
                    client_token=z_ct,
                )

            await _send_privacy_notice(
                channel_label="Z-API",
                is_new=is_new,
                lgpd_cfg=lgpd_cfg,
                clinic_config=clinic_config,
                tenant_id=tenant_id,
                session_id=session_id,
                to_phone=phone,
                db=db,
                send=_send_zapi_privacy,
            )
    _event_metadata: dict[str, Any] = {
        "phone": phone,
        "senderName": sender_name,
        "messageId": body.get("messageId", ""),
        "source": "zapi",
    }
    if audio_obj and message_text:
        _audio_creds = await _tenant_creds()
        # Reuse the model the STT call actually used; the re-derivation below is
        # only for payloads that never entered the audio block (text+audio, or
        # audio without a url), where whisper_model is still None.
        _event_metadata["whisper_model"] = (
            whisper_model
            or _audio_creds.get("stt_model_name")
            or _audio_creds.get("whisper_model")
            or "whisper-large-v3-turbo"
        )
        _event_metadata["audio_seconds"] = audio_obj.get("seconds") or 0
        _event_metadata["is_audio"] = True
    event_request = EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type=EventType.MESSAGE_IN,
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

    creds = await _tenant_creds()
    zapi_token = creds.get("zapi_token", "").strip()
    zapi_inst = creds.get("zapi_instance_id", "").strip()
    zapi_client_token = creds.get("zapi_client_token", "").strip()

    if not zapi_token or not zapi_inst:
        logger.warning("Z-API credentials missing for tenant %s", tenant_id)
        return {"ok": True, "replied": False, "reason": "no_credentials"}

    try:
        send_result, sent_as_audio = await _send_reply_with_optional_tts(
            credentials=creds,
            text=suggested,
            language_code=_get_agent_language(clinic_config),
            channel="Z-API",
            phone=phone,
            input_was_audio=patient_audio_input,
            send_audio=lambda audio, mime: send_zapi_audio(
                zapi_inst,
                zapi_token,
                phone,
                audio,
                mime_type=mime,
                client_token=zapi_client_token,
            ),
            send_text=lambda: send_text_message(
                zapi_inst,
                zapi_token,
                phone,
                suggested,
                client_token=zapi_client_token,
            ),
            force_text=bool(
                response.prompt and getattr(response.prompt, "deterministic", False)
            ),
        )
        _track_sent_message(send_result)
        # DB-backed dedup: store reply message ID so its fromMe echo is recognised
        # as a bot echo across all Uvicorn workers (works even when Redis is down)
        _reply_mid = send_result.get("messageId") or send_result.get(
            "zapiMessageId", ""
        )
        if _reply_mid:
            await _store_ai_msg_id_to_db(session_id, _reply_mid, db)
            await db.commit()  # commit the metadata update immediately
        logger.info(
            "Z-API reply sent to %s (%d chars, format=%s)",
            phone,
            len(suggested),
            "audio" if sent_as_audio else "text",
        )
    except Exception as exc:
        logger.exception("Z-API send failed for %s — %s", phone, exc)
        return {
            "ok": True,
            "replied": False,
            "reason": "send_failed",
            "error": str(exc),
        }

    # ── mark conversation as unread (green dot) after handoff message ──
    _handoff = response.handoff_summary or (
        response.decision and response.decision.handoff_required
    )
    logger.info(
        "Z-API handoff check for %s: handoff_summary=%s, decision.handoff_required=%s → mark_unread=%s",
        phone,
        bool(response.handoff_summary),
        response.decision.handoff_required if response.decision else None,
        bool(_handoff),
    )
    if _handoff:
        try:
            await mark_conversation_unread(
                zapi_inst, zapi_token, phone, client_token=zapi_client_token
            )
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
    response_model=None,
    status_code=200,
)
async def evolution_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
) -> Response | dict[str, Any]:
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
        (
            msg_obj.get("conversation")
            or msg_obj.get("extendedTextMessage", {}).get("text", "")
        ).strip()
        if isinstance(msg_obj, dict)
        else ""
    )

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
    tenant_id = await _resolve_tenant_by_credential(
        "evolution_instance_name", instance_name, db
    )
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
        creds_lgpd = await _load_tenant_credentials(tenant_id, db)
        e_url = creds_lgpd.get("evolution_api_url", "")
        e_name = creds_lgpd.get("evolution_instance_name", "")
        e_key = creds_lgpd.get("evolution_api_key", "")
        if e_url and e_name and e_key:

            async def _send_evolution_privacy(msg: str) -> dict[str, Any]:
                return await send_evolution_text(
                    e_url,
                    e_name,
                    e_key,
                    phone,
                    msg,
                )

            await _send_privacy_notice(
                channel_label="Evolution",
                is_new=is_new,
                lgpd_cfg=lgpd_cfg,
                clinic_config=clinic_config,
                tenant_id=tenant_id,
                session_id=session_id,
                to_phone=phone,
                db=db,
                send=_send_evolution_privacy,
            )

    event_request = EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type=EventType.MESSAGE_IN,
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
        send_result, sent_as_audio = await _send_reply_with_optional_tts(
            credentials=creds,
            text=suggested,
            language_code=_get_agent_language(clinic_config),
            channel="Evolution",
            phone=phone,
            input_was_audio=False,
            send_audio=lambda audio, _mime: send_evolution_audio(
                evol_url, evol_name, evol_key, phone, audio
            ),
            send_text=lambda: send_evolution_text(
                evol_url, evol_name, evol_key, phone, suggested
            ),
            force_text=bool(
                response.prompt and getattr(response.prompt, "deterministic", False)
            ),
        )
        # Normalise the outbound id (Decisions 4 + 11): both dedup layers
        # receive the same value, and Evolution's ``id`` — the only key it
        # used to write to ``_store_ai_msg_id_to_db`` — no longer diverges
        # from what ``_track_sent_message`` looks for.
        _reply_mid = _extract_normalised_send_id(send_result)
        if _reply_mid:
            _track_sent_message({"messageid": _reply_mid})
            await _store_ai_msg_id_to_db(session_id, _reply_mid, db)
            await db.commit()  # commit the metadata update immediately
        else:
            _track_sent_message(send_result)
        logger.info(
            "Evolution reply sent to %s (%d chars, format=%s)",
            phone,
            len(suggested),
            "audio" if sent_as_audio else "text",
        )
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
    response_model=None,
    status_code=200,
)
async def uazapi_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
) -> Response | dict[str, Any]:
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

    # ── extract payload identifiers — Uazapi commercial payload schema ──
    # Uazapi uses top-level "message" and "chat" objects (not the wuzapi
    # "data" envelope). Field names confirmed from live payload inspection.
    # Real Uazapi payload uses top-level "message" and "chat" objects, not
    # the wuzapi "data" envelope we originally assumed.
    msg = body.get("message") or {}
    if not isinstance(msg, dict):
        return {"ok": True, "skipped": "invalid_data"}

    # instanceName (capital N) identifies the Uazapi instance / tenant
    instance_name = str(body.get("instanceName") or "")

    chat = body.get("chat") or {}
    chat_jid = str(chat.get("wa_chatid") or msg.get("chatid") or "")
    is_from_me = bool(msg.get("fromMe", False))
    # Decision 8: the provider's ``wasSentByApi`` flag is a *reinforcing*
    # dedup signal — cheap, id-independent — but never a substitute. It
    # covers the exact hole the id-based claim leaves open when a fromMe
    # echo arrives without a ``messageid``: without it, every re-delivery
    # of that same idless echo would slip through and pause the session.
    was_sent_by_api = bool(msg.get("wasSentByApi", False))

    # Counterparty resolution (Decision 3): when the clinic replies from
    # its own WhatsApp, ``sender_pn`` is the clinic's own number and
    # ``chat.wa_chatid`` / ``chatid`` is the patient we must pause. For
    # patient-originated messages we keep ``sender_pn`` as the primary
    # source with ``chat.wa_chatid`` as fallback, matching the pre-Group-3
    # behaviour.
    if is_from_me:
        raw_from = str(chat.get("wa_chatid") or msg.get("chatid") or "")
    else:
        raw_from = str(msg.get("sender_pn") or chat.get("wa_chatid") or "")
    from_phone = _normalise_phone(
        raw_from.split("@")[0] if "@" in raw_from else raw_from
    )

    # text / content both carry the message body depending on messageType
    message_text = _extract_uazapi_message_text(msg)
    msg_id = str(msg.get("messageid") or msg.get("id") or "")

    # messageType / type identify media (e.g. "Image", "Audio", "Video")
    msg_type = str(msg.get("type") or msg.get("messageType") or "")
    media_type = str(msg.get("mediaType") or "")

    logger.info(
        "Uazapi webhook: instance=%s from=%s type=%s mediaType=%s body_len=%d fromMe=%s",
        instance_name,
        raw_from,
        msg_type,
        media_type,
        len(message_text or ""),
        is_from_me,
    )

    if not instance_name or not from_phone:
        return {"ok": True, "skipped": "missing_fields"}

    if _is_uazapi_group_chat(chat_jid):
        logger.info("Uazapi webhook: skipping group chat %s", chat_jid)
        return {"ok": True, "skipped": "group_chat"}

    # Skip binary media — text messages arrive as "text" or "Conversation"
    _MEDIA_TYPES = {
        "image",
        "Image",
        "video",
        "Video",
        "audio",
        "Audio",
        "document",
        "Document",
        "sticker",
        "Sticker",
        "ptt",
    }
    is_media_message = (
        msg_type == "media" or msg_type in _MEDIA_TYPES or media_type in _MEDIA_TYPES
    )
    is_audio_media = (
        msg_type in {"audio", "Audio", "ptt"}
        or media_type in {"audio", "Audio", "ptt"}
        or bool(msg.get("ptt"))
    )

    # ── resolve tenant from instance_name ──
    tenant_id = await _resolve_tenant_by_credential(
        "uazapi_instance_name",
        instance_name,
        db,
    )
    if not tenant_id:
        logger.warning(
            "Uazapi webhook: no tenant for instance %s",
            instance_name,
        )
        return {"ok": True, "skipped": "unknown_instance"}

    # Media-gate carve-outs:
    #   * ``is_from_me and is_audio_media`` — staff voice note. Group 3 routes
    #     it through ``_handle_clinic_message`` so it can pause the session;
    #     the audio bytes are never downloaded or transcribed (real STT cost
    #     on the clinic's own voice, same rationale as Z-API).
    #   * ``is_audio_media and not is_from_me`` — patient voice note. Group 4
    #     transcribes it below (STT → multimodal → text reroute chain shared
    #     with Z-API and Meta). Without this carve-out patient audio would be
    #     silently dropped as ``non-text``.
    #
    # Any other media (image, video, document, sticker) still exits as
    # ``non-text`` — Z-API reference, not Meta's file-ack path (out of scope
    # for this wish).
    audio_is_transcribable = is_audio_media and not is_from_me
    if (
        is_media_message
        and not (is_from_me and is_audio_media)
        and not audio_is_transcribable
    ):
        logger.info(
            "Uazapi webhook: skipping non-text msg_type=%s mediaType=%s",
            msg_type,
            media_type,
        )
        return {"ok": True, "skipped": "non-text"}

    if (
        message_text is None
        and not (is_from_me and is_audio_media)
        and not audio_is_transcribable
    ):
        logger.info(
            "Uazapi webhook: skipping non-text missing textual content type=%s mediaType=%s",
            msg_type,
            media_type,
        )
        return {"ok": True, "skipped": "non-text"}

    # ── replay claim (Decision 9, lower bound) ──
    # The claim runs *before* the fromMe routing so a re-delivery of the
    # same echo is barred here — the id-based echo layers below consume
    # on match, so without this ordering the second delivery would slip
    # past them and pause the session. The claim is ``SET NX EX`` with a
    # 31-day retention: claim-and-hold, not consume.
    if msg_id and not _claim_incoming_message("uazapi", tenant_id, msg_id):
        logger.info(
            "Uazapi webhook: duplicate msg %s from %s skipped",
            msg_id,
            from_phone,
        )
        return {"ok": True, "skipped": "duplicate_message"}

    # ── fromMe=true: AI-sent echo or real clinic message ──
    # Routing lives between the claim (lower bound) and _check_pause_state
    # (upper bound): the reactivation branch inside _handle_clinic_message
    # only fires when the session is already paused, and _check_pause_state
    # returns 202 in exactly that state — routing after it would make the
    # trigger-phrase reactivation permanently unreachable.
    if is_from_me:
        # ``wasSentByApi`` (Decision 8) is the id-independent guard that
        # covers idless echoes; ``_is_ai_sent_message`` still handles the
        # id-based case (and consumes the entry so repeats can't leak).
        if was_sent_by_api:
            return {"ok": True, "skipped": "ai_echo"}
        if msg_id and _is_ai_sent_message(msg_id):
            return {"ok": True, "skipped": "ai_echo"}
        # Pass message_id so _handle_clinic_message can consult the
        # session's DB-backed ai_msg_ids list — this is how the LGPD
        # aviso echo is recognised across worker/container boundaries.
        result = await _handle_clinic_message(
            tenant_id, from_phone, message_text or "", db, message_id=msg_id
        )
        await db.commit()
        return result

    # ── load clinic_config (needed for agent language + downstream checks) ──
    service = get_event_service()
    clinic_config = await service._load_clinic_config(tenant_id)

    # Raw credentials memo: the audio path below needs Uazapi send tokens +
    # STT/multimodal fields, and the LGPD/reply path near the bottom needs
    # the same row. Load once and reuse — the cached ``clinic_config`` is a
    # runtime projection that strips ``stt_api_key``/``uazapi_token``, so we
    # must read the raw row for either use.
    _raw_creds_memo: Optional[dict[str, Any]] = None

    async def _tenant_creds() -> dict[str, Any]:
        nonlocal _raw_creds_memo
        if _raw_creds_memo is None:
            _raw_creds_memo = await _load_tenant_credentials(tenant_id, db)
        return _raw_creds_memo

    # ── patient audio: transcribe via STT → multimodal → text reroute ──
    # Mirrors the Z-API audio block (webhook.py: ``_process_zapi_authenticated_body``)
    # gate order: STT-provider-unsupported → duration → capability →
    # download → chain → reroute on failure. This block only runs for
    # patient audio (``is_audio_media and not is_from_me``); staff voice
    # notes were already routed above and returned before this point.
    if audio_is_transcribable:
        creds = await _tenant_creds()
        u_url = (creds.get("uazapi_api_url") or "").strip()
        u_token = (creds.get("uazapi_token") or "").strip()
        stt_provider = _stt_canonical_provider(creds.get("stt_provider"))
        agent_lang = _get_agent_language(clinic_config)

        # STT-provider-unsupported gate: same skip envelope as Z-API/Meta.
        # This gate is deliberate — some providers have no runtime adapter
        # and must not attempt download / transcription / reroute.
        if stt_provider in UNSUPPORTED_STT_PROVIDERS:
            return _stt_skip_envelope(stt_provider)

        # Duration gate: field name confirmed against the captured evidence
        # (media-payload.json: ``message.content.seconds`` is an int). This
        # runs above the capability gate so a 3-minute audio hands off
        # instead of getting "please type" on Uazapi — matching Z-API/Meta.
        content_obj = msg.get("content") or {}
        audio_seconds = 0
        if isinstance(content_obj, dict):
            audio_seconds = int(content_obj.get("seconds") or 0)
        audio_max_seconds = int(creds.get("audio_max_seconds") or 60)
        if audio_seconds > audio_max_seconds:
            if u_url and u_token:
                handoff_msg = (
                    _AUDIO_TOO_LONG_MSG_EN
                    if agent_lang == "en"
                    else _AUDIO_TOO_LONG_MSG_PT
                )
                try:
                    await send_uazapi_text(u_url, u_token, from_phone, handoff_msg)
                except Exception:
                    logger.exception(
                        "Uazapi long-audio handoff send failed for %s",
                        from_phone,
                    )
            logger.info(
                "Uazapi audio too long (%ds > %ds limit), skipping transcription for %s",
                audio_seconds,
                audio_max_seconds,
                from_phone,
            )
            return {"ok": True, "skipped": "audio_too_long"}

        # Capability gate: download only when at least one chain rung can
        # actually use the bytes. Matches Z-API — no bytes fetched from
        # WhatsApp / Uazapi when the tenant has neither STT nor multimodal.
        whisper_key = (
            creds.get("stt_api_key") or creds.get("whisper_api_key") or ""
        ).strip()
        audio_bytes: Optional[bytes] = None
        effective_mime = str(
            (content_obj.get("mimetype") if isinstance(content_obj, dict) else None)
            or "audio/ogg"
        )
        if whisper_key or _multimodal_operational(creds):
            try:
                audio_bytes, downloaded_mime = await download_uazapi_media(
                    u_url, u_token, msg_id
                )
            except UazapiMediaDownloadError as exc:
                # Media never materialised — Uazapi's download endpoint
                # rejected the request or returned garbage. There is no
                # transcription to reroute from; skip with a distinct label
                # so operators can grep for it in logs.
                logger.warning(
                    "Uazapi media download failed for %s — %s",
                    msg_id,
                    exc,
                )
                return {"ok": True, "skipped": "audio_download_failed"}
            if downloaded_mime:
                effective_mime = downloaded_mime
            logger.info(
                "Uazapi audio received for transcription: size=%d bytes, mimetype=%s",
                len(audio_bytes),
                effective_mime,
            )

        transcript, chain_failure = await _transcribe_audio_chain(
            audio_bytes,
            creds,
            mime_type=effective_mime,
            agent_language=agent_lang,
            channel="Uazapi",
        )
        if chain_failure:
            return await _send_uazapi_stt_fallback(
                tenant_id=tenant_id,
                phone=from_phone,
                u_url=u_url,
                u_token=u_token,
                agent_language=agent_lang,
                failure_reason=chain_failure,
            )
        # Success: feed the transcript into the normal pipeline. The
        # load-bearing ``assert message_text is not None`` below is now
        # satisfied on the patient-audio path.
        message_text = transcript

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
    session_id, is_new = await _get_or_create_session(tenant_id, from_phone, db)
    await db.commit()

    logger.info(
        "Uazapi webhook: msg %s from %s (tenant %s)",
        msg_id,
        from_phone,
        tenant_id,
    )

    # ── load tenant credentials early so the LGPD aviso can precede the
    # AI reply. Deliverable 5 of the ``uazapi-parity`` wish: the aviso is
    # the very first outbound message on a new session, so credentials
    # cannot wait until after the pipeline finishes. Reuses the same memo
    # the audio path (above) already populated on the transcribe branch.
    creds = await _tenant_creds()
    u_url = (creds.get("uazapi_api_url") or "").strip()
    u_token = (creds.get("uazapi_token") or "").strip()

    # ── send LGPD privacy notice as the very first message of a new session ──
    if is_new:
        lgpd_cfg = (clinic_config or {}).get("lgpd") or {}
        if u_url and u_token:

            async def _send_uazapi_privacy(msg: str) -> dict[str, Any]:
                return await send_uazapi_text(u_url, u_token, from_phone, msg)

            await _send_privacy_notice(
                channel_label="Uazapi",
                is_new=is_new,
                lgpd_cfg=lgpd_cfg,
                clinic_config=clinic_config,
                tenant_id=tenant_id,
                session_id=session_id,
                to_phone=from_phone,
                db=db,
                send=_send_uazapi_privacy,
            )

    assert message_text is not None
    event_request = EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type=EventType.MESSAGE_IN,
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

    if not u_url or not u_token:
        logger.warning("Uazapi credentials missing for tenant %s", tenant_id)
        return {"ok": True, "replied": False, "reason": "no_credentials"}

    try:
        send_result, sent_as_audio = await _send_reply_with_optional_tts(
            credentials=creds,
            text=suggested,
            language_code=_get_agent_language(clinic_config),
            channel="Uazapi",
            phone=from_phone,
            input_was_audio=audio_is_transcribable,
            send_audio=lambda audio, mime: send_uazapi_audio(
                u_url,
                u_token,
                from_phone,
                audio,
                mime_type=mime,
            ),
            send_text=lambda: send_uazapi_text(u_url, u_token, from_phone, suggested),
            force_text=bool(
                response.prompt and getattr(response.prompt, "deterministic", False)
            ),
        )
        # Normalise the outbound id (Decisions 4 + 11): Uazapi's ``id``
        # arrives as ``owner:MSGID`` and never matches the echo, which only
        # carries the tail. Both dedup layers now receive the same value.
        reply_mid = _extract_normalised_send_id(send_result)
        if reply_mid:
            _track_sent_message({"messageid": reply_mid})
            await _store_ai_msg_id_to_db(session_id, reply_mid, db)
            await db.commit()  # commit metadata update immediately
        else:
            _track_sent_message(send_result)
        logger.info(
            "Uazapi reply sent to %s (%d chars, format=%s)",
            from_phone,
            len(suggested),
            "audio" if sent_as_audio else "text",
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


def _track_meta_sent_message(send_result: dict[str, Any]) -> None:
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
    "encaminh",
    "encaminha",
    "encaminhar",
    "envia",
    "enviar",
    "manda",
    "mandar",
    "equipe",
    "atendent",
    "human",
    "forward",
    "team",
    "please forward",
    "send it",
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

    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
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

    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
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
) -> Any:
    """Initiate a human handoff signalling that a file/audio is pending review.

    PostgreSQL insertion is mandatory. Failures propagate to the route so it
    cannot clear state or acknowledge a handoff that does not exist.
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
        response = await handoff_service.initiate_handoff(request)
    except Exception:
        logger.exception(
            "Meta file handoff creation failed tenant=%s session=%s",
            tenant_id,
            session_id,
        )
        raise
    logger.info(
        "Meta file handoff initiated for %s (session=%s, note=%s)",
        from_phone,
        session_id,
        note,
    )
    return response


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
) -> PlainTextResponse:
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
    response_model=None,
    status_code=200,
)
async def meta_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session_dependency),
) -> Response | dict[str, Any]:
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
    # ── 1) Read the exact raw bytes BEFORE anything else so HMAC can bind
    # to them (json parsing does not preserve byte-for-byte). ──
    raw_body = await request.body()
    try:
        import json as _json

        body: dict[str, Any] = _json.loads(raw_body or b"{}")
    except Exception:
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )

    if body.get("object") != "whatsapp_business_account":
        return {"ok": True, "skipped": "unknown_object"}

    entries = body.get("entry") or []
    if not entries:
        return {"ok": True, "skipped": "no_entry"}

    # ── 2) Extract every phone_number_id whose change carries a message.
    # The wish requires exactly one distinct message-bearing phone id, and
    # the whole request must be resolved to that single tenant/app secret
    # BEFORE any session/media/reply/handoff/session mutation. ──
    message_bearing_phone_ids = _extract_meta_message_bearing_phone_ids(body)
    if not message_bearing_phone_ids:
        # No messages at all — this is a status-only or empty envelope. The
        # existing tests expect the specific ``status_only``/``no_entry``
        # sentinels for those cases, so preserve them.
        first_change = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes", []) or []:
                if not isinstance(change, dict):
                    continue
                if change.get("field") == "messages":
                    first_change = change
                    break
            if first_change is not None:
                break
        if first_change is None:
            return {"ok": True, "skipped": "no_entry"}
        value = first_change.get("value") or {}
        if not (value.get("metadata") or {}).get("phone_number_id"):
            return {"ok": True, "skipped": "missing_phone_number_id"}
        return {"ok": True, "skipped": "status_only"}

    if len(message_bearing_phone_ids) != 1:
        # Zero or multiple message-bearing phone ids — fail closed with a
        # generic error so multi-tenant confusion cannot bleed downstream.
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )

    phone_number_id = message_bearing_phone_ids[0]

    # ── 3) Resolve exactly one tenant + credentials for that phone id ──
    resolved = await _resolve_tenant_by_meta_phone_number_id(phone_number_id, db)
    if resolved is None:
        logger.warning(
            "Meta webhook: no tenant for phone_number_id %s",
            phone_number_id,
        )
        return {"ok": True, "skipped": "unknown_phone_number_id"}
    tenant_id, tenant_creds = resolved

    # ── 4) HMAC-SHA256 authenticity over the raw body bytes ──
    app_secret = str(tenant_creds.get("meta_app_secret") or "").strip()
    signature_header = request.headers.get(
        META_SIGNATURE_HEADER
    ) or request.headers.get(META_SIGNATURE_HEADER.lower())
    if not app_secret or not _verify_meta_signature(
        signature_header, raw_body, app_secret
    ):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )

    # ── 5) Rate limit (after auth, before replay/session/media/reply) ──
    rate_decision = check_webhook_rate_limit(str(tenant_id), "meta")
    if not rate_decision.allowed:
        return webhook_rate_limit_response(rate_decision)

    # ── 6) messageId presence + replay claim (before any downstream effect) ──
    # We need the first message id from the single message-bearing change.
    change_for_processing: dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []) or []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            if (
                str((metadata or {}).get("phone_number_id") or "").strip()
                != phone_number_id
            ):
                continue
            messages = value.get("messages") or []
            if not messages:
                continue
            change_for_processing = change
            break
        if change_for_processing:
            break

    value = change_for_processing.get("value") or {}
    messages = value.get("messages") or []
    msg = messages[0] if isinstance(messages[0], dict) else {}
    first_message_id = str(msg.get("id") or "").strip()
    if not first_message_id:
        return {"ok": True, "skipped": "missing_message_id"}

    # Preserve the authenticated Meta boundary, then reject providers without
    # adapters before replay state, bot/pause checks, session creation, media
    # resolution/download, reply, or handoff. ``tenant_creds`` is the same raw
    # row resolved together with the HMAC secret above.
    first_message_type = str(msg.get("type") or "")
    if first_message_type in _META_AUDIO_TYPES:
        early_stt_provider = _stt_canonical_provider(tenant_creds.get("stt_provider"))
        if early_stt_provider in UNSUPPORTED_STT_PROVIDERS:
            return _stt_skip_envelope(early_stt_provider)

    try:
        first_delivery = _claim_incoming_message("meta", tenant_id, first_message_id)
    except ReplayStoreUnavailable:
        return JSONResponse(
            status_code=503,
            content={"error": "replay_store_unavailable"},
        )
    if not first_delivery:
        return {"ok": True, "skipped": "duplicate_message"}

    # ── downstream processing (bound to the authenticated tenant/phone id) ──
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
            entry_pnid = str((metadata or {}).get("phone_number_id") or "").strip()
            # Any change referencing a different phone id under the same
            # (authenticated) body is a mixed-tenant attempt — reject the
            # whole request rather than partially processing it.
            if entry_pnid and entry_pnid != phone_number_id:
                return JSONResponse(
                    status_code=401,
                    content={"error": "invalid_webhook_auth"},
                )
            messages = value.get("messages") or []
            if not messages:
                continue

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
            input_was_audio = msg_type in _META_AUDIO_TYPES
            from_phone = _normalise_phone(str(msg.get("from") or ""))

            # Log receipt with the Meta message id so a "patient never got a
            # reply" report can be traced through our logs without needing
            # the Meta dashboard
            logger.info(
                "Meta webhook received message id=%s type=%s from=%s tenant=%s",
                message_id,
                msg_type,
                from_phone,
                tenant_id,
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
                            tenant_id,
                            session_id,
                            from_phone,
                            "file_pending_review",
                        )
                        await _meta_clear_pending_file_review(session_id, db)
                        await db.commit()
                        if meta_pnid and meta_token:
                            confirm_msg = (
                                _FILE_FORWARDED_MSG_EN
                                if agent_lang == "en"
                                else _FILE_FORWARDED_MSG_PT
                            )
                            try:
                                send_result = await send_meta_cloud_text(
                                    meta_pnid,
                                    meta_token,
                                    from_phone,
                                    confirm_msg,
                                )
                                _track_meta_sent_message(send_result)
                                _fwd_mid = extract_meta_message_id(send_result)
                                if _fwd_mid:
                                    await _store_ai_msg_id_to_db(
                                        session_id, _fwd_mid, db
                                    )
                                    await db.commit()
                            except Exception as exc:
                                logger.exception(
                                    "Meta file-forward confirmation send failed for %s — %s",
                                    from_phone,
                                    exc,
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
                mime_type = str(audio_obj.get("mime_type") or "audio/ogg")

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
                        media_id,
                        tenant_id,
                        exc,
                    )
                    return {"ok": True, "skipped": "media_resolve_failed"}

                media_url = str(media_info.get("url") or "")
                if not media_url:
                    logger.warning(
                        "Meta media resolve returned no url for %s tenant=%s",
                        media_id,
                        tenant_id,
                    )
                    return {"ok": True, "skipped": "media_resolve_failed"}

                # 2) Enforce audio_max_seconds by approximate byte count.
                # Meta exposes neither duration nor download size in the
                # webhook, so we lean on the file_size the resolve endpoint
                # returns and treat > audio_max_seconds*16KB as "too long"
                # — this triggers a handoff, never a 500.
                audio_max_seconds_value = (creds or {}).get("audio_max_seconds")
                if isinstance(audio_max_seconds_value, (str, int, float)):
                    audio_max_seconds = int(audio_max_seconds_value or 60)
                else:
                    audio_max_seconds = 60
                file_size_value = media_info.get("file_size")
                if isinstance(file_size_value, (str, int, float)):
                    file_size = int(file_size_value or 0)
                else:
                    file_size = 0
                max_bytes = audio_max_seconds * _META_AUDIO_APPROX_BYTES_PER_SECOND
                if file_size and file_size > max_bytes:
                    handoff_msg = (
                        _AUDIO_TOO_LONG_MSG_EN
                        if agent_lang == "en"
                        else _AUDIO_TOO_LONG_MSG_PT
                    )
                    await _meta_initiate_file_handoff(
                        tenant_id,
                        session_id,
                        from_phone,
                        "audio_too_long",
                    )
                    if meta_pnid and meta_token:
                        try:
                            await send_meta_cloud_text(
                                meta_pnid,
                                meta_token,
                                from_phone,
                                handoff_msg,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Meta audio-too-long ack send failed for %s — %s",
                                from_phone,
                                exc,
                            )
                    logger.info(
                        "Meta audio too long (%d bytes > ~%ds*%dB) for %s — handoff",
                        file_size,
                        audio_max_seconds,
                        _META_AUDIO_APPROX_BYTES_PER_SECOND,
                        from_phone,
                    )
                    return {"ok": True, "skipped": "audio_too_long"}

                # 3) Download bytes — streaming with MIME allowlist + byte cap
                # (max_bytes already computed for the size-check above)
                try:
                    audio_bytes = await download_meta_media_audio(
                        media_url, meta_token, max_bytes=max_bytes
                    )
                except Exception as exc:
                    logger.warning(
                        "Meta audio download failed for %s tenant=%s — %s",
                        media_id,
                        tenant_id,
                        exc,
                    )
                    return {"ok": True, "skipped": "media_download_failed"}

                # 4) Run the same fallback chain Z-API runs: STT, then a
                # multimodal model, then a text reroute. The ``(text, reason)``
                # pair is what separates "the key is missing" from "STT ran and
                # returned nothing" — the old ``or ""`` collapsed both into
                # ``no_whisper_key``.
                message_text, chain_failure = await _transcribe_audio_chain(
                    audio_bytes,
                    creds,
                    mime_type=mime_type,
                    agent_language=agent_lang,
                    channel="Meta",
                )
                if chain_failure:
                    return await _send_meta_stt_fallback(
                        tenant_id=tenant_id,
                        from_phone=from_phone,
                        meta_pnid=meta_pnid,
                        meta_token=meta_token,
                        agent_language=agent_lang,
                        failure_reason=chain_failure,
                    )

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
                        meta_pnid,
                        meta_token,
                        from_phone,
                        ack_msg,
                    )
                    _track_meta_sent_message(send_result)
                    _ack_mid = extract_meta_message_id(send_result)
                    if _ack_mid:
                        await _store_ai_msg_id_to_db(session_id, _ack_mid, db)
                    await _meta_set_pending_file_review(session_id, db)
                    await db.commit()
                    logger.info(
                        "Meta file ack sent to %s (type=%s)",
                        from_phone,
                        msg_type,
                    )
                except Exception as exc:
                    logger.exception(
                        "Meta file ack send failed for %s — %s",
                        from_phone,
                        exc,
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

                async def _send_meta_privacy(msg: str) -> dict[str, Any]:
                    return await send_meta_cloud_text(
                        meta_pnid,
                        meta_token,
                        from_phone,
                        msg,
                    )

                # Meta wraps the outbound id inside ``messages[0].id`` — use
                # ``extract_meta_message_id`` here instead of the default
                # top-level ``messageid`` lookup used by the other channels.
                await _send_privacy_notice(
                    channel_label="Meta",
                    is_new=is_new,
                    lgpd_cfg=lgpd_cfg,
                    clinic_config=clinic_config,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    to_phone=from_phone,
                    db=db,
                    send=_send_meta_privacy,
                    id_extractor=lambda r: extract_meta_message_id(r) or "",
                )

            # ── pipeline ──
            event_request = EventRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=EventType.MESSAGE_IN,
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
                send_result, sent_as_audio = await _send_reply_with_optional_tts(
                    credentials=creds,
                    text=suggested,
                    language_code=_get_agent_language(clinic_config),
                    channel="Meta",
                    phone=from_phone,
                    input_was_audio=input_was_audio,
                    send_audio=lambda audio, mime: send_meta_cloud_audio(
                        meta_pnid,
                        meta_token,
                        from_phone,
                        audio,
                        mime_type=mime,
                    ),
                    send_text=lambda: send_meta_cloud_text(
                        meta_pnid,
                        meta_token,
                        from_phone,
                        suggested,
                    ),
                    force_text=bool(
                        response.prompt
                        and getattr(response.prompt, "deterministic", False)
                    ),
                )
                _track_meta_sent_message(send_result)
                _reply_mid = extract_meta_message_id(send_result)
                if _reply_mid:
                    await _store_ai_msg_id_to_db(session_id, _reply_mid, db)
                    await db.commit()  # commit metadata update immediately
                logger.info(
                    "Meta reply sent to %s (%d chars, format=%s)",
                    from_phone,
                    len(suggested),
                    "audio" if sent_as_audio else "text",
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
