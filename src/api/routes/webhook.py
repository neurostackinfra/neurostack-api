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
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import (
    Any,
    Awaitable,
    Callable,
    Final,
    Literal,
    Optional,
    cast,
    get_args,
    overload,
)
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.rate_limit import (
    check_webhook_rate_limit,
    webhook_rate_limit_response,
)
from src.api.schemas.events import (
    CanonicalEventRequest,
    EventPayload,
    EventResponse,
    EventType,
)
from src.config.settings import settings
from src.core.lgpd_utils import resolve_privacy_link
from src.handoff.episode_context import (
    build_prior_episode_context,
    project_resume_state,
)
from src.handoff.pause_policy import resolve_handoff_pause, resolve_trigger_reactivation
from src.interaction_context import (
    InteractionOrigin,
    ResolvedInteractionContext,
    build_interaction_context,
    classify_interaction_origin,
)
from src.models.clinic_config_table import ClinicConfigTable
from src.models.event import Event as EventModel
from src.models.tenant import Tenant as TenantModel
from src.observability.metric_dimensions import (
    UazapiWebhookAuthMode,
    UazapiWebhookAuthResult,
)
from src.observability.metrics import UAZAPI_WEBHOOK_AUTH_TOTAL
from src.repositories import (
    EventRepository,
    SessionRepository,
    get_db_session_dependency,
)
from src.repositories.database import get_db_session_factory
from src.services import credit_metering, reply_media_runner
from src.services.api_credentials import (
    DEFAULT_AUDIO_MAX_SECONDS,
    OPERATIONAL_MULTIMODAL_PROVIDERS,
    MediaDeliveryProvider,
    media_delivery_provider,
)
from src.services.credit_metering import (
    REPLY_PAYLOAD_KIND,
    CreditEnforcementUnavailable,
    CreditReplyUndeliverable,
    InlineDelivery,
    ObservationalIngress,
    WebhookEnforcement,
    evolution_ingress,
    meta_ingress,
    record_meta_file_handoff,
    record_webhook_decision,
    uazapi_ingress,
    zapi_ingress,
)
from src.services.credit_switch import request_credit_switch
from src.services.egress_policy import (
    EgressDisabledError,
    require_uazapi_egress,
    uazapi_webhook_auth_enforced,
)
from src.services.event_service import get_event_service
from src.services.handoff_service import (
    dispatch_committed_handoff_webhooks,
    record_handoff_post_commit_dispatch_failure,
)
from src.services.media_assets import MEDIA_IMAGE_MAX_BYTES, ogg_opus_duration_seconds
from src.services.meta_service import (
    download_meta_media_audio,
    extract_meta_message_id,
    resolve_meta_media,
    send_meta_cloud_audio,
    send_meta_cloud_text,
)
from src.services.reply_media import PermitPool, RecordSentId, reply_media_channel
from src.services.reply_media_runner import ReplyMediaJob, cut_safe_sessions
from src.services.safe_egress import (
    IMAGE_MIME_ALLOWLIST,
    UnsafeEgressError,
    safe_download_audio,
    safe_download_image,
)
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

# ── Image description chain (whatsapp-image-understanding) ──
#
# Byte cap of an image the agent may describe (Decision 12): the same cap the
# credit system applies to media images. Above it the image never reaches the
# multimodal provider and, when the channel declares the size, is never even
# downloaded.
IMAGE_MAX_BYTES: Final = MEDIA_IMAGE_MAX_BYTES

# Reasons returned by ``_describe_image_chain``. The empty string means "a
# description was produced and composed"; the two non-empty values both send
# the patient down the file-acknowledgement flow (Decision 3), and are kept
# distinct so a handler can tell "nothing configured" from "it failed now".
IMAGE_CHAIN_OK = ""
IMAGE_CHAIN_NO_CAPABILITY = "no_capability"
IMAGE_CHAIN_FAILED = "description_failed"

# ``outcome`` of the ``image_description_attempt`` event. The chain emits the
# outcomes it decides itself; handlers emit the ones decided before the chain
# runs (``view_once``, a declared ``too_large``/``unsupported_mime``,
# ``download_failed``).
ImageDescriptionOutcome = Literal[
    "described",
    "no_capability",
    "too_large",
    "unsupported_mime",
    "view_once",
    "download_failed",
    "provider_failed",
    "no_content",
]
IMAGE_DESCRIPTION_OUTCOMES: frozenset[str] = frozenset(
    get_args(ImageDescriptionOutcome)
)

# Adapter error code -> event outcome; every other failure is
# ``provider_failed``.
_IMAGE_ERROR_OUTCOMES: dict[str, ImageDescriptionOutcome] = {
    "no_image_content": "no_content",
    "unsupported_image_mime": "unsupported_mime",
}

# ``safe_download_image`` error code -> event outcome, for the handlers that
# download before the chain runs; every other code (``unsafe_api_url``,
# ``redirect_forbidden``, ``image_download_failed``, unknown) is
# ``download_failed``.
_IMAGE_DOWNLOAD_ERROR_OUTCOMES: dict[str, ImageDescriptionOutcome] = {
    "image_too_large": "too_large",
    "unsupported_image_mime": "unsupported_mime",
}


def _image_download_outcome(code: str) -> ImageDescriptionOutcome:
    """Map a ``safe_download_image`` error code to its event outcome."""

    return _IMAGE_DOWNLOAD_ERROR_OUTCOMES.get(code, "download_failed")


# What a loggable MIME looks like (``type/subtype``, RFC 6838 characters,
# bounded). The MIME comes from the patient's payload, so anything else is
# logged as ``invalid`` rather than echoed into the log line.
_IMAGE_EVENT_MIME_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)

# Decision 10: how the description enters the pipeline, per agent language.
_IMAGE_DESCRIPTION_LABEL_PT = "Imagem enviada pelo paciente"
_IMAGE_DESCRIPTION_LABEL_EN = "Image sent by the patient"

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


@dataclass(frozen=True)
class _ReplyTarget:
    """Where an enforced webhook decision's reply goes (credit Group 3b-2):
    the channel, the patient's phone and whether the patient sent audio --
    the arguments of ``_send_reply_with_optional_tts`` the response does not
    carry.  Never a credential."""

    # "zapi" | "meta" | "uazapi" | "evolution" (the last two only under the
    # courtesy rule)
    channel: str
    phone: str
    input_was_audio: bool


# ── Agent media after the reply (envio-midia-agente Group 6) ─────────


def _route_media_provider(whatsapp_provider: str) -> MediaDeliveryProvider:
    provider = media_delivery_provider(whatsapp_provider)
    if provider is None:  # pragma: no cover - a broken map fails at import
        raise RuntimeError(f"no media delivery provider for {whatsapp_provider}")
    return provider


# The delivery provider of each webhook's replies: the route's own WhatsApp
# provider through the ONE map the Central de Mídias reads too
# (``media_delivery_provider``), so the screen, the selection and the
# delivery always name the same provider.  A server parameter, never the
# payload's (``channel`` is ``"whatsapp"`` on all four).
_ZAPI_MEDIA_PROVIDER: Final = _route_media_provider("zapi")
_EVOLUTION_MEDIA_PROVIDER: Final = _route_media_provider("evolution")
_UAZAPI_MEDIA_PROVIDER: Final = _route_media_provider("uazapi")
_META_MEDIA_PROVIDER: Final = _route_media_provider("meta_cloud")
# The enforced replies' channels (``_ReplyTarget.channel``) -> the same
# providers, for the file of a stored reply.
_CREDIT_MEDIA_PROVIDERS: Final[Mapping[str, MediaDeliveryProvider]] = MappingProxyType(
    {
        "zapi": _ZAPI_MEDIA_PROVIDER,
        "meta": _META_MEDIA_PROVIDER,
        "uazapi": _UAZAPI_MEDIA_PROVIDER,
        "evolution": _EVOLUTION_MEDIA_PROVIDER,
    }
)


@dataclass(frozen=True)
class _InlineMedia:
    """What one webhook turn needs to send its file after its text: the
    delivery provider -- given to the selection (``finish_response`` /
    ``generate_response``) AND bound by the delivery, the very same value --,
    the process's one ``agent_sends`` pool (the outbox publisher's too) and
    the credit switch the request captured at entry."""

    provider: MediaDeliveryProvider
    agent_sends: PermitPool
    credit_switch_on: Optional[bool]


def _inline_media(
    request: Optional[Request], provider: MediaDeliveryProvider
) -> Optional[_InlineMedia]:
    """The turn's media wiring, or ``None``: then the turn is offered no file
    at all (the selection gets no provider) -- without the application's
    ``agent_sends`` pool nothing could send it, and a reply never promises a
    file nothing sends."""

    if request is None:
        return None
    try:
        agent_sends = request.app.state.media_concurrency.agent_sends
    except Exception:
        return None
    if agent_sends is None:
        return None
    try:
        switch_on: Optional[bool] = request_credit_switch(request)
    except Exception:
        switch_on = None
    return _InlineMedia(provider, agent_sends, switch_on)


def _media_provider(media: Optional[_InlineMedia]) -> Optional[MediaDeliveryProvider]:
    return None if media is None else media.provider


def _turn_media_id(response: object) -> Optional[UUID]:
    """The file the turn chose (``EventResponse.attachment``), or ``None``; a
    response without the field (a suite's stand-in) chose none."""

    media_id = getattr(getattr(response, "attachment", None), "media_id", None)
    return media_id if isinstance(media_id, UUID) else None


def _media_send_id(provider: MediaDeliveryProvider, send_result: dict[str, Any]) -> str:
    """Track a message the media delivery sent (the file or its notice)
    exactly as the provider's text reply tracks its own, and return the id
    to store for the ``fromMe`` echo (``""``: none)."""

    if provider == "meta":
        _track_meta_sent_message(send_result)
        return extract_meta_message_id(send_result) or ""
    if provider == "zapi":
        _track_sent_message(send_result)
        mid = send_result.get("messageId") or send_result.get("zapiMessageId")
        return mid if isinstance(mid, str) else ""
    mid = _extract_normalised_send_id(send_result)
    if mid:
        _track_sent_message({"messageid": mid})
    else:
        _track_sent_message(send_result)
    return mid


def _reply_media_recorder(
    provider: MediaDeliveryProvider,
    session_id: UUID,
    session_factory: Callable[[], AsyncSession],
) -> RecordSentId:
    """The delivery's ``record_sent_id`` on an inline path: the id tracked
    (``_track_sent_message`` / ``_track_meta_sent_message``) and stored for
    the cross-worker echo (``_store_ai_msg_id_to_db``) on a session of its
    own, committed at once.  A cancellation propagates: the delivery bounds
    each record, and a cut-safe session invalidates a connection it cut."""

    async def record_sent_id(sent: Mapping[str, object]) -> None:
        mid = _media_send_id(provider, dict(sent))
        if not mid:
            return
        async with session_factory() as db:
            await _store_ai_msg_id_to_db(session_id, mid, db)
            await db.commit()

    return record_sent_id


async def _send_inline_reply_media(
    media: Optional[_InlineMedia],
    response: object,
    *,
    db: AsyncSession,
    credentials: Mapping[str, Any],
    phone: str,
    tenant_id: UUID,
    session_id: UUID,
) -> None:
    """After the provider accepted the reply's text (its 2xx): START the
    delivery of the turn's file, if it chose one, as a task of its own
    (:func:`reply_media_runner.start_reply_media`) and return at once -- the
    webhook answers as before.  A turn with no file returns here, with
    nothing else done.

    The channel is bound to the provider the selection was given
    (``media.provider``); credentials it refuses (``ValueError``) skip the
    file, logged -- never a 500 after the text went out.  The request's
    transaction is ended first: everything the path wrote is committed, and
    the request must hold no pooled connection while the delivery waits up
    to a minute for the process's permit.  Never raises but for a
    cancellation."""

    media_id = _turn_media_id(response)
    if media is None or media_id is None:
        return
    try:
        channel = reply_media_channel(media.provider, credentials, phone)
    except ValueError as error:
        logger.warning(
            "agent media not started for tenant %s session %s media %s (%s)",
            tenant_id,
            session_id,
            media_id,
            error,
        )
        return
    with suppress(Exception):
        await db.rollback()
    try:
        factory = get_db_session_factory()
    except Exception as error:
        logger.warning(
            "agent media not started for tenant %s session %s media %s (%s)",
            tenant_id,
            session_id,
            media_id,
            type(error).__name__,
        )
        return
    language = getattr(response, "response_language", None)
    reply_media_runner.start_reply_media(
        ReplyMediaJob(
            channel=channel,
            media_id=media_id,
            tenant_id=tenant_id,
            session_id=session_id,
            response_language=language if isinstance(language, str) else "pt-BR",
            record_sent_id=_reply_media_recorder(
                media.provider, session_id, cut_safe_sessions(factory)
            ),
            credit_switch_on=media.credit_switch_on,
        ),
        session_factory=factory,
        agent_sends=media.agent_sends,
    )


def _suggested_reply(response: EventResponse) -> Optional[str]:
    if response.prompt and response.prompt.suggested_response:
        return response.prompt.suggested_response
    return None


def _credit_reply_payload(
    target: _ReplyTarget,
    event_request: CanonicalEventRequest,
    response: EventResponse,
) -> Optional[dict[str, Any]]:
    """The stored reply of an enforced decision: the arguments main passes to
    ``_send_reply_with_optional_tts`` (and to its side effects) WITHOUT any
    token or credential -- the send re-reads ``clinic_configs``.  ``None``
    when there is nothing to send (main answers ``no_llm_response``)."""

    suggested = _suggested_reply(response)
    if not suggested:
        return None
    payload: dict[str, Any] = {
        "kind": REPLY_PAYLOAD_KIND,
        "channel": target.channel,
        "tenant_id": str(event_request.tenant_id),
        "session_id": str(event_request.session_id),
        "phone": target.phone,
        "text": suggested,
        "language_code": response.response_language,
        "input_was_audio": target.input_was_audio,
        "force_text": bool(
            response.prompt and getattr(response.prompt, "deterministic", False)
        ),
        "handoff": bool(
            response.handoff_summary
            or (response.decision and response.decision.handoff_required)
        ),
    }
    media_id = _turn_media_id(response)
    if media_id is not None:
        # envio-midia-agente: the turn's file -- all the outbox carries of it
        # (the delivery re-reads the asset); only when the turn chose one.
        payload["media_id"] = str(media_id)
    return payload


@dataclass(frozen=True)
class _PreparedReply:
    """A stored reply ready for its provider call (no credential is ever
    stored in the outbox: they are re-read from ``clinic_configs`` here)."""

    channel: str
    session_id: UUID
    phone: str
    text: str
    language_code: str
    input_was_audio: bool
    force_text: bool
    handoff: bool
    credentials: dict[str, Any]


# The channels of a stored reply (``_ReplyTarget.channel``) and their log
# labels -- Uazapi and Evolution only under the courtesy rule
# (debito-cortesia-automatico Groups 3 and 4).
_CREDIT_REPLY_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {"zapi": "Z-API", "meta": "Meta", "uazapi": "Uazapi", "evolution": "Evolution"}
)


class CreditReplySender:
    """The send adapter of a stored webhook reply (credit Group 3b-2), used
    inline by the route and by the response-outbox publisher (injected from
    the lifespan: the credit module never imports this route, LOW-13).

    Exactly main's steps after a decision, split so that only the provider
    call is under the send timeout (OD-5): :meth:`prepare` re-reads the
    tenant's raw credentials; :meth:`send` is ``_send_reply_with_optional_tts``
    with TTS and ``force_text`` (and its egress gate); :meth:`record` tracks
    the sent id (``_track_sent_message`` / ``_track_meta_sent_message``),
    stores it with ``_store_ai_msg_id_to_db`` -- so the ``fromMe`` echo is
    recognised as the bot's -- and, on Z-API, marks a handoff's conversation
    unread.  Each bookkeeping step only logs its failure.

    Uazapi (debito-cortesia-automatico Group 3, a reply charged under the
    courtesy rule): main's Uazapi reply -- ``send_uazapi_text`` /
    ``send_uazapi_audio`` with ``uazapi_api_url`` / ``uazapi_token``; a
    closed ``uazapi_egress_enabled`` refuses before the network
    (``uazapi_egress_disabled``, main's reason); the sent id normalised by
    ``_extract_normalised_send_id`` (``owner:MSGID`` -> ``MSGID``, what the
    echo carries) for both dedup layers, so the echo of a charged reply never
    reads as the clinic's message and never pauses the session; no unread
    mark (Uazapi has none).

    Evolution (debito-cortesia-automatico Group 4, a reply charged under the
    courtesy rule): main's Evolution reply -- ``send_evolution_text`` /
    ``send_evolution_audio`` with ``evolution_api_url`` /
    ``evolution_instance_name`` / ``evolution_api_key``, exactly the values
    main reads (no egress flag exists for Evolution); the sent id normalised
    by ``_extract_normalised_send_id`` (Evolution's ``key.id``) tracked and
    stored for both dedup layers, as main does, so an echo of a charged reply
    is the bot's (``ai_echo`` / ``ai_echo_db``) and never pauses the session;
    no unread mark (Evolution has none).
    """

    async def prepare(
        self, payload: Mapping[str, Any], db: AsyncSession
    ) -> _PreparedReply:
        channel = str(payload.get("channel") or "")
        try:
            tenant_id = UUID(str(payload["tenant_id"]))
            session_id = UUID(str(payload["session_id"]))
            phone = str(payload["phone"])
            text = str(payload["text"])
        except (KeyError, ValueError) as error:
            raise CreditReplyUndeliverable("payload_unusable") from error
        if (
            payload.get("kind") != REPLY_PAYLOAD_KIND
            or channel not in _CREDIT_REPLY_LABELS
            or not phone
            or not text
        ):
            raise CreditReplyUndeliverable("payload_unusable")
        creds = await _load_tenant_credentials(tenant_id, db)
        if channel == "zapi":
            ready = (creds.get("zapi_token") or "").strip() and (
                creds.get("zapi_instance_id") or ""
            ).strip()
        elif channel == "uazapi":
            ready = (creds.get("uazapi_api_url") or "").strip() and (
                creds.get("uazapi_token") or ""
            ).strip()
        elif channel == "evolution":
            # Main's check, on main's values: the three present, unstripped.
            ready = (
                creds.get("evolution_api_url")
                and creds.get("evolution_instance_name")
                and creds.get("evolution_api_key")
            )
        else:
            ready = (creds.get("meta_phone_number_id") or "").strip() and (
                creds.get("meta_permanent_token") or ""
            ).strip()
        if not ready:
            logger.warning(
                "%s credentials missing for tenant %s",
                _CREDIT_REPLY_LABELS[channel],
                tenant_id,
            )
            raise CreditReplyUndeliverable("no_credentials")
        return _PreparedReply(
            channel=channel,
            session_id=session_id,
            phone=phone,
            text=text,
            language_code=str(payload.get("language_code") or "pt-BR"),
            input_was_audio=bool(payload.get("input_was_audio")),
            force_text=bool(payload.get("force_text")),
            handoff=bool(payload.get("handoff")),
            credentials=creds,
        )

    async def send(self, prepared: _PreparedReply) -> dict[str, Any]:
        creds, phone, text = prepared.credentials, prepared.phone, prepared.text
        if prepared.channel == "zapi":
            zapi_token = (creds.get("zapi_token") or "").strip()
            zapi_inst = (creds.get("zapi_instance_id") or "").strip()
            zapi_client_token = (creds.get("zapi_client_token") or "").strip()
            send_result, sent_as_audio = await _send_reply_with_optional_tts(
                credentials=creds,
                text=text,
                language_code=prepared.language_code,
                channel="Z-API",
                phone=phone,
                input_was_audio=prepared.input_was_audio,
                send_audio=lambda audio, mime: send_zapi_audio(
                    zapi_inst,
                    zapi_token,
                    phone,
                    audio,
                    mime_type=mime,
                    client_token=zapi_client_token,
                ),
                send_text=lambda: send_text_message(
                    zapi_inst, zapi_token, phone, text, client_token=zapi_client_token
                ),
                force_text=prepared.force_text,
            )
        elif prepared.channel == "uazapi":
            u_url = (creds.get("uazapi_api_url") or "").strip()
            u_token = (creds.get("uazapi_token") or "").strip()
            try:
                send_result, sent_as_audio = await _send_reply_with_optional_tts(
                    credentials=creds,
                    text=text,
                    language_code=prepared.language_code,
                    channel="Uazapi",
                    phone=phone,
                    input_was_audio=prepared.input_was_audio,
                    send_audio=lambda audio, mime: send_uazapi_audio(
                        u_url, u_token, phone, audio, mime_type=mime
                    ),
                    send_text=lambda: send_uazapi_text(u_url, u_token, phone, text),
                    force_text=prepared.force_text,
                )
            except EgressDisabledError as closed:
                # Refused before the network boundary: nothing was sent
                # (main answers ``uazapi_egress_disabled``).
                raise CreditReplyUndeliverable(closed.code) from closed
        elif prepared.channel == "evolution":
            evol_url = creds.get("evolution_api_url", "")
            evol_name = creds.get("evolution_instance_name", "")
            evol_key = creds.get("evolution_api_key", "")
            send_result, sent_as_audio = await _send_reply_with_optional_tts(
                credentials=creds,
                text=text,
                language_code=prepared.language_code,
                channel="Evolution",
                phone=phone,
                input_was_audio=prepared.input_was_audio,
                send_audio=lambda audio, _mime: send_evolution_audio(
                    evol_url, evol_name, evol_key, phone, audio
                ),
                send_text=lambda: send_evolution_text(
                    evol_url, evol_name, evol_key, phone, text
                ),
                force_text=prepared.force_text,
            )
        else:
            meta_pnid = (creds.get("meta_phone_number_id") or "").strip()
            meta_token = (creds.get("meta_permanent_token") or "").strip()
            send_result, sent_as_audio = await _send_reply_with_optional_tts(
                credentials=creds,
                text=text,
                language_code=prepared.language_code,
                channel="Meta",
                phone=phone,
                input_was_audio=prepared.input_was_audio,
                send_audio=lambda audio, mime: send_meta_cloud_audio(
                    meta_pnid, meta_token, phone, audio, mime_type=mime
                ),
                send_text=lambda: send_meta_cloud_text(
                    meta_pnid, meta_token, phone, text
                ),
                force_text=prepared.force_text,
            )
        logger.info(
            "%s reply sent to %s (%d chars, format=%s)",
            _CREDIT_REPLY_LABELS[prepared.channel],
            phone,
            len(text),
            "audio" if sent_as_audio else "text",
        )
        return send_result

    async def record(
        self,
        prepared: _PreparedReply,
        send_result: dict[str, Any],
        db: AsyncSession,
    ) -> None:
        phone = prepared.phone
        reply_mid = ""
        try:
            if prepared.channel == "zapi":
                _track_sent_message(send_result)
                reply_mid = send_result.get("messageId") or send_result.get(
                    "zapiMessageId", ""
                )
            elif prepared.channel in ("uazapi", "evolution"):
                # Main's Uazapi and Evolution bookkeeping (Decisions 4 + 11 of
                # uazapi-parity): the normalised id for both layers
                # (Evolution's is ``key.id``).
                reply_mid = _extract_normalised_send_id(send_result)
                if reply_mid:
                    _track_sent_message({"messageid": reply_mid})
                else:
                    _track_sent_message(send_result)
            else:
                _track_meta_sent_message(send_result)
                reply_mid = extract_meta_message_id(send_result) or ""
            if reply_mid:
                await _store_ai_msg_id_to_db(prepared.session_id, reply_mid, db)
                await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning("reply id bookkeeping failed for %s — %s", phone, exc)
        if prepared.channel == "zapi" and prepared.handoff:
            creds = prepared.credentials
            try:
                await mark_conversation_unread(
                    (creds.get("zapi_instance_id") or "").strip(),
                    (creds.get("zapi_token") or "").strip(),
                    phone,
                    client_token=(creds.get("zapi_client_token") or "").strip(),
                )
                logger.info("Z-API mark_unread SUCCESS after handoff for %s", phone)
            except Exception as exc:
                logger.warning("Z-API mark_unread FAILED for %s — %s", phone, exc)

    def media_recorder(
        self,
        prepared: _PreparedReply,
        session_factory: Callable[[], AsyncSession],
    ) -> RecordSentId:
        """:meth:`record` as the file delivery's ``record_sent_id``
        (envio-midia-agente G6): the same tracking and stored echo id, for
        the file's and the notice's answers, each on a session of its own
        (``record`` commits).  ``handoff`` is off: that bookkeeping never
        marks the conversation unread.  ``record`` catches ``Exception`` only,
        so a cancellation -- the delivery's bound on each record -- is never
        swallowed, and the cut-safe session invalidates what it cut."""

        bookkeeping = replace(prepared, handoff=False)

        async def record_sent_id(sent: Mapping[str, object]) -> None:
            async with session_factory() as db:
                await self.record(bookkeeping, dict(sent), db)

        return record_sent_id

    def start_media(
        self,
        prepared: _PreparedReply,
        *,
        media_id: UUID,
        tenant_id: UUID,
        session_factory: Callable[[], AsyncSession],
        agent_sends: PermitPool,
    ) -> None:
        """Start the stored reply's file (``ReplySender.start_media``): the
        channel bound to the provider its turn's selection was given, the
        phone and the credentials of ``prepared`` (in memory: the row's are
        erased by now).  Returns at once; never raises."""

        provider = _CREDIT_MEDIA_PROVIDERS.get(prepared.channel)
        if provider is None:
            logger.warning("agent media not started: reply channel unknown")
            return
        try:
            channel = reply_media_channel(
                provider, prepared.credentials, prepared.phone
            )
        except ValueError as error:
            logger.warning(
                "agent media not started for tenant %s session %s media %s (%s)",
                tenant_id,
                prepared.session_id,
                media_id,
                error,
            )
            return
        reply_media_runner.start_reply_media(
            ReplyMediaJob(
                channel=channel,
                media_id=media_id,
                tenant_id=tenant_id,
                session_id=prepared.session_id,
                response_language=prepared.language_code,
                record_sent_id=self.media_recorder(
                    prepared, cut_safe_sessions(session_factory)
                ),
            ),
            session_factory=session_factory,
            agent_sends=agent_sends,
        )


CREDIT_REPLY_SENDER: Final = CreditReplySender()


def _inline_answer(delivery: InlineDelivery) -> dict[str, Any]:
    """The webhook body of an enforced decision after its inline send.

    Main's bodies for what main also answers (``replied``,
    ``no_credentials``, ``send_failed`` with its ``error``; a send timeout is
    ``send_failed`` / ``send_timeout``).  Two bodies are NOT main's, and only
    exist on the enforced path: ``send_queued`` -- the inline claim found no
    ``pending`` row: the publisher took it first, or the row is gone with
    its tenant (a concurrent clinic delete) -- and the OD-2 refusals
    (``session_changed``, ``bot_inactive``, ``session_gone``,
    ``reply_expired``, ``payload_unusable``): the reply was not sent and
    never will be.
    """

    if delivery.outcome == "delivered":
        return {"ok": True, "replied": True}
    if delivery.outcome == "not_claimed":
        return {"ok": True, "replied": False, "reason": "send_queued"}
    if delivery.reason == "send_failed":
        return {
            "ok": True,
            "replied": False,
            "reason": "send_failed",
            "error": delivery.error or "send_failed",
        }
    return {"ok": True, "replied": False, "reason": delivery.reason or "send_failed"}


async def _dispatch_phase1_handoffs(
    db: AsyncSession, event_request: CanonicalEventRequest
) -> None:
    """Announce the handoffs a committed phase 1 wrote, best effort (main's
    post-commit dispatch; after a rollback the queue is dropped unsent)."""

    try:
        await dispatch_committed_handoff_webhooks(db)
    except Exception as exc:
        record_handoff_post_commit_dispatch_failure(
            exc,
            owner="adapter",
            tenant_id=event_request.tenant_id,
            session_id=event_request.session_id,
        )


async def _run_enforced_webhook_decision(
    *,
    service: Any,
    event_request: CanonicalEventRequest,
    db: AsyncSession,
    context: ResolvedInteractionContext,
    enforcement: WebhookEnforcement,
    reply: _ReplyTarget,
    media: Optional[_InlineMedia] = None,
) -> dict[str, Any]:
    """The enforced branch of ``_run_pipeline_transactionally`` (credit Group
    3b-2): the claim (its own short transaction; a redelivery is ALWAYS
    ``duplicate_message``) -> phase 1 (the route session, the webhook marker
    LAST) -> the handoff announcement after its commit, as main -> the LLM
    outside every transaction (``generate_response``) -> the final
    transaction (bundle, one trace or ``trace_no_response``, the ``pending``
    reply, the final fence) -> the inline send, which claims the reply first.
    ``media`` (envio-midia-agente): the delivery provider for the selection,
    and the pool with which a delivered reply starts its file.
    Returns the webhook body."""

    leased = await credit_metering.acquire_webhook_claim(db, enforcement)
    if not isinstance(leased, credit_metering.ClaimLease):
        return dict(leased)

    async def run_pipeline() -> Any:
        return await service.run_pipeline(event_request, session=db, context=context)

    try:
        phase1 = await credit_metering.run_webhook_phase1(
            db,
            enforcement,
            leased,
            session_id=event_request.session_id,
            run_pipeline=run_pipeline,
        )
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        raise
    finally:
        await _dispatch_phase1_handoffs(db, event_request)
    if phase1 is None:  # the marker's fence did not hold: rolled back
        return dict(credit_metering.DUPLICATE_MESSAGE_SKIP)

    generated = await service.generate_response(
        event_request,
        phase1.result,
        phase1.clinic_config,
        phase1.message_history,
        context=context,
        media_delivery_provider=_media_provider(media),
    )
    response = cast(EventResponse, generated.response)

    async def write_bundle(credit_effect_id: Optional[str]) -> Optional[UUID]:
        written: Optional[UUID] = await service.write_interaction_bundle(
            event_request,
            phase1.result,
            generated,
            session=db,
            context=context,
            credit_effect_id=credit_effect_id,
        )
        return written

    try:
        final = await credit_metering.run_webhook_final(
            db,
            enforcement,
            leased,
            phase1,
            response=response,
            write_bundle=write_bundle,
            reply=_credit_reply_payload(reply, event_request, response),
        )
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        raise
    if final is None:  # the final fence did not hold: no bundle, no debit
        return dict(credit_metering.DUPLICATE_MESSAGE_SKIP)
    response.interaction_event_id = final.interaction_event_id
    if final.outbox_id is None:
        logger.info(
            "%s webhook: no suggested_response for %s", reply.channel, reply.phone
        )
        return {"ok": True, "replied": False, "reason": "no_llm_response"}
    delivery = await credit_metering.deliver_reply_inline(
        final.outbox_id,
        sender=CREDIT_REPLY_SENDER,
        agent_sends=None if media is None else media.agent_sends,
    )
    return _inline_answer(delivery)


@overload
async def _run_pipeline_transactionally(
    *,
    service: Any,
    event_request: CanonicalEventRequest,
    db: AsyncSession,
    context: ResolvedInteractionContext,
    metering: Optional[ObservationalIngress] = None,
    media: Optional[_InlineMedia] = None,
) -> EventResponse: ...


@overload
async def _run_pipeline_transactionally(
    *,
    service: Any,
    event_request: CanonicalEventRequest,
    db: AsyncSession,
    context: ResolvedInteractionContext,
    metering: Optional[ObservationalIngress] = None,
    enforcement: Optional[WebhookEnforcement],
    reply: _ReplyTarget,
    media: Optional[_InlineMedia] = None,
) -> EventResponse | dict[str, Any]: ...


async def _run_pipeline_transactionally(
    *,
    service: Any,
    event_request: CanonicalEventRequest,
    db: AsyncSession,
    context: ResolvedInteractionContext,
    metering: Optional[ObservationalIngress] = None,
    enforcement: Optional[WebhookEnforcement] = None,
    reply: Optional[_ReplyTarget] = None,
    media: Optional[_InlineMedia] = None,
) -> EventResponse | dict[str, Any]:
    """Run the locked phase, commit it, then start post-commit response I/O.

    Pipeline or commit failures roll back and never reach ``finish_response``.
    Once commit succeeds, failures from response generation are deliberately
    not followed by rollback because the language/state transition is already
    durable and the PostgreSQL row lock has been released.

    A handoff written in the locked phase is announced to the clinic only
    after that commit, before the response I/O. The announcement is best
    effort: its failure is logged and counted, never re-raised, so the reply
    is still produced.

    ``context`` is the adapter's single interaction context. The very same
    object goes, by keyword, to both phases. It is required: every caller
    classifies the origin and builds the context before calling.

    ``metering`` is the adapter's observational ingress (neurostack-credit-
    system Group 3a), ``None`` unless the request captured the credit switch
    on.  Its decision is recorded best-effort once ``finish_response`` has
    returned (both phases' transactions are over), before any reply is sent;
    it never changes the response.

    ``enforcement`` (credit Group 3b-2) is the Z-API/Meta pre-check's verdict
    for this message: with it, the decision runs the enforced branch
    (:func:`_run_enforced_webhook_decision`) -- claimed, charged with its
    bundle in one commit and its reply sent inline from the response
    outbox -- and the WEBHOOK BODY is returned instead of the response (the
    reply was already sent or queued).  Without it, ``main``'s path.

    ``media`` (envio-midia-agente Group 6) is the turn's media wiring: its
    provider goes to the selection of both branches, and the caller sends
    the turn's file with the same object after the text.  ``None``: the
    turn is offered no file.
    """
    if enforcement is not None:
        if reply is None:
            raise TypeError("an enforced webhook decision needs its reply target")
        return await _run_enforced_webhook_decision(
            service=service,
            event_request=event_request,
            db=db,
            context=context,
            enforcement=enforcement,
            reply=reply,
            media=media,
        )
    try:
        result, clinic_config, message_history = await service.run_pipeline(
            event_request,
            session=db,
            context=context,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    try:
        await dispatch_committed_handoff_webhooks(db)
    except Exception as exc:
        record_handoff_post_commit_dispatch_failure(
            exc,
            owner="adapter",
            tenant_id=event_request.tenant_id,
            session_id=event_request.session_id,
        )
    response = cast(
        EventResponse,
        await service.finish_response(
            event_request,
            result,
            clinic_config,
            message_history,
            context=context,
            media_delivery_provider=_media_provider(media),
        ),
    )
    await record_webhook_decision(metering, db, response)
    return response


def _meta_authenticated_origin(
    authenticated: object,
    canonical_tenant_id: UUID,
) -> Optional[InteractionOrigin]:
    """Origin of a Meta request, or ``None`` when its proof is not consistent.

    The only proof is the ``meta_hmac`` ``TenantContext`` that
    ``TenantIdentityMiddleware`` publishes after verifying the HMAC over the
    exact body. It must be consistent for the Group 3 classifier and bound to
    ``canonical_tenant_id``. Anything else yields ``None`` and the route
    answers 401: Meta is never downgraded to ``unknown``.
    """
    try:
        if getattr(authenticated, "authentication_mode", None) != "meta_hmac":
            return None
        if getattr(authenticated, "tenant_id", None) != canonical_tenant_id:
            return None
    except Exception:
        return None
    origin = classify_interaction_origin(authenticated)
    if origin is not InteractionOrigin.PRODUCTION:
        return None
    return origin


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
    except EgressDisabledError:
        raise
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
    - Falls back to ``key.id``, returned verbatim, when no top-level key
      matched. Evolution v2 documents its ``sendText`` /
      ``sendWhatsAppAudio`` response as the Baileys message
      (``{"key": {"remoteJid", "fromMe", "id"}, "message", "status", ...}``)
      with no top-level id (``openapi-v2.json``; ``prepareMessage`` in
      2.3.7 ``whatsapp.baileys.service.ts``), so that is where its outbound
      id lives. It is read last so every response that already yielded an
      id keeps yielding the same one. Defensive: Evolution reports its own
      API sends as ``send.message`` (2.3.7 L2545), which
      ``evolution_webhook`` ignores, and runs Baileys with
      ``emitOwnEvents: false`` (2.3.7 L658), so no echo of an API send is
      matched against this id today.
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
    message_key = send_result.get("key")
    if isinstance(message_key, dict):
        key_id = message_key.get("id")
        if isinstance(key_id, str) and key_id:
            return key_id
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
    except EgressDisabledError:
        raise
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


async def _load_ai_person_history(
    tenant_id: UUID,
    session_id: UUID,
    db: AsyncSession,
    *,
    limit: int = 20,
) -> list[dict[str, str]]:
    """Load the last ``limit`` ai/person events for a session as role/content dicts.

    Mirrors ``EventService._load_message_history`` (bounded to 20 by default,
    ``message.in`` -> ``user``, ``prompt.generated`` -> ``assistant``) so the
    pause/reactivation composers in this module see exactly the same input shape
    as the pipeline composer. Returns ``[]`` on any error so composition can
    fall back to the neutral episode context without failing the webhook.
    """
    try:
        stmt = (
            select(EventModel)
            .where(
                EventModel.session_id == session_id,
                EventModel.tenant_id == tenant_id,
                or_(
                    EventModel.event_type == "message.in",
                    EventModel.event_type == "prompt.generated",
                ),
            )
            .order_by(EventModel.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        events = list(result.scalars().all())
        events.reverse()  # chronological order
        history: list[dict[str, str]] = []
        for e in events:
            if (e.event_metadata or {}).get("paused_capture"):
                continue
            data = e.event_data or {}
            if e.event_type == "message.in":
                content = data.get("content", "")
                if content:
                    history.append({"role": "user", "content": content})
            elif e.event_type == "prompt.generated":
                resp = data.get("suggested_response", "")
                if resp:
                    history.append({"role": "assistant", "content": resp})
        return history
    except Exception as exc:
        logger.warning(
            "Failed to load ai/person history for pause composer",
            extra={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "error": str(exc),
            },
        )
        return []


async def _check_pause_state(
    tenant_id: UUID,
    phone: str,
    db: AsyncSession,
    *,
    now: datetime | None = None,
    message_text: Optional[str] = None,
    origin: InteractionOrigin = InteractionOrigin.UNKNOWN,
) -> Optional[dict[str, Any]]:
    """Check if session is paused. Returns action dict or None to continue.

    Implements lazy unpause: if paused_until has expired, unpauses the session.

    ``origin`` is the value the calling adapter classified after its own
    authenticity proof (``unknown`` by default, which keeps positional callers
    valid). When the session is still paused, the captured ``message.in`` is
    written through the interaction's single context: its ID is the context's
    ``interaction_event_id`` and its origin is ``origin``.
    """
    if not isinstance(origin, InteractionOrigin):
        raise TypeError("origin must be an InteractionOrigin")
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
        # Auto-reactivation CAS (wish deliverables 6 + 10/A1): project a fresh
        # resume state with a brand-new episode id and let
        # reactivate_effective_auto flip is_paused/paused_until and overwrite
        # cognitive_state in a single UPDATE. The episode id is generated once
        # here so retries inside the repository loop reuse the same uuid (A3).
        prior_state = session.get_cognitive_state()
        new_episode_id = uuid4()
        projected = project_resume_state(
            prior_state,
            context_episode_id=str(new_episode_id),
            prior_episode_context=getattr(prior_state, "prior_episode_context", None),
        )
        outcome = await repo.reactivate_effective_auto(
            tenant_id=tenant_id,
            session_id=session.id,
            new_cognitive_state=projected.to_dict(),
            new_episode_id=new_episode_id,
            now=current_time,
        )
        if outcome == "reactivated":
            # Winner: refresh identity-mapped attributes so downstream code in
            # the same request sees the post-CAS state (A1).
            await db.refresh(
                session,
                attribute_names=["cognitive_state", "is_paused", "paused_until"],
            )
            logger.info(
                "session_auto_unpause",
                extra={
                    "session_id": str(session.id),
                    "tenant_id": str(tenant_id),
                    "phone": phone,
                    "outcome": outcome,
                },
            )
            return None  # Continue processing
        # Non-winner (already_active / not_yet_expired / superseded_session /
        # not_found / reactivation_conflict): fall through to the still-paused
        # skip-response behavior below. Do not fail the webhook.
        logger.info(
            "session_auto_unpause_non_winner",
            extra={
                "session_id": str(session.id),
                "tenant_id": str(tenant_id),
                "field": "cognitive_state",
                "outcome": outcome,
            },
        )

    # Still paused: the paused session is this message's interaction.
    context = build_interaction_context(
        effective_tenant_id=tenant_id,
        session_id=session.id,
        origin=origin,
        event_type=EventType.MESSAGE_IN,
    )
    if message_text:
        try:
            await EventRepository(db).create(
                id=context.interaction_event_id,
                tenant_id=context.effective_tenant_id,
                session_id=context.session_id,
                event_type=EventType.MESSAGE_IN.value,
                interaction_origin=context.origin.value,
                event_data={
                    "content": message_text,
                    "channel": "whatsapp",
                    "user_id": phone,
                    "role": "user",
                },
                event_metadata={"paused_capture": True},
            )
        except Exception as exc:
            # Never fail the webhook over a best-effort audit write (D2).
            logger.warning(
                "Failed to persist paused-capture message event",
                extra={
                    "tenant_id": str(tenant_id),
                    "session_id": str(session.id),
                    "error": str(exc),
                },
            )
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

    reactivation = resolve_trigger_reactivation(handoff_cfg, current_time)
    trigger_msg = _normalize_for_match(reactivation.trigger_message)
    normalized_text = _normalize_for_match(message_text)

    # Check if message contains the trigger phrase (flexible match, punctuation-tolerant)
    if trigger_msg in normalized_text:
        if session.is_paused:
            for notice in reactivation.notices:
                logger.warning(
                    "handoff_reactivation_config_tolerated",
                    extra={
                        "tenant_id": str(tenant_id),
                        "session_id": str(session.id),
                        "field": notice.field,
                        "reason": notice.reason,
                    },
                )
            session.paused_until = reactivation.reactivates_at
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

    # Human takeover: compose the prior_episode_context from AI/person events
    # ONLY (no current message — this is a fromMe clinic message, never a
    # patient turn), merge it onto the current cognitive state, and route the
    # pause write through the single tenant-scoped CAS so an AI-handoff pause
    # racing against this takeover does not last-writer-wins (D13/A2).
    ai_person_events = await _load_ai_person_history(
        tenant_id, session.id, db, limit=20
    )
    prior_context = build_prior_episode_context(
        tenant_id=tenant_id,
        session_id=session.id,
        events=ai_person_events,
        current_message=None,
    )
    cognitive = session.get_cognitive_state()
    cognitive.prior_episode_context = prior_context

    outcome = await repo.pause_effective(
        tenant_id,
        session.id,
        cognitive_state=cognitive.to_dict(),
        paused_until=decision.paused_until,
    )

    if outcome == "paused":
        await db.refresh(
            session,
            attribute_names=["cognitive_state", "is_paused", "paused_until"],
        )
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

    # Non-winner outcomes (already_paused, superseded_session, not_found):
    # another writer already paused this row — surface the existing
    # already_paused action without failing the webhook, and never overwrite
    # the concurrent winner's cognitive_state.
    logger.info(
        "session_pause_cas_non_winner",
        extra={
            "session_id": str(session.id),
            "tenant_id": str(tenant_id),
            "field": "cognitive_state",
            "outcome": outcome,
        },
    )
    return {"ok": True, "action": "already_paused"}


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


def _stt_usage_metadata(
    credentials: dict[str, Any], *, audio_seconds: int
) -> dict[str, Any]:
    """The ``message.in`` metadata of a transcribed patient audio.

    The three keys ``event_service`` keeps from the webhook payload, read by
    the trace stats and the builder dashboard to count transcription seconds
    per model. Z-API builds the same keys inline, from its ``seconds``.
    """
    return {
        "whisper_model": _resolve_stt_model(credentials),
        "is_audio": True,
        "audio_seconds": audio_seconds,
    }


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
) -> Optional[str]:
    """First rung: the dedicated speech-to-text provider.

    Returns the transcript (possibly empty for silent or unintelligible
    audio), or ``None`` when no STT key is configured at all. Raises the
    underlying exception on transcription failure so the chain can log it
    with a stack trace and move to the next rung.

    Language hint is deliberately **omitted**: the audio turn must feed the
    same PT/EN decision machine the text turn feeds, with no STT-side
    translation. ElevenLabs autodetects natively; the OpenAI-compatible
    adapters pass no ``language`` so Whisper autodetects as well.

    Key resolution is canonical-first, matching ``resolve_stt_credentials``:
    a legacy ``whisper_api_key`` is only a fallback for rows written before
    ``stt_api_key`` existed.
    """
    stt_provider = _stt_canonical_provider(creds.get("stt_provider"))
    whisper_key = (creds.get("stt_api_key") or "").strip() or (
        creds.get("whisper_api_key") or ""
    ).strip()

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


def _image_event_mime(mime: Optional[str]) -> Optional[str]:
    """The MIME as logged: normalized, or ``invalid`` when not MIME-shaped."""
    normalized = (mime or "").split(";", 1)[0].strip().lower()
    if not normalized:
        return None
    if _IMAGE_EVENT_MIME_PATTERN.fullmatch(normalized) is None:
        return "invalid"
    return normalized


def _emit_image_description_attempt(
    *,
    channel: str,
    outcome: ImageDescriptionOutcome,
    mime: Optional[str],
    size_bytes: Optional[int],
    latency_ms: Optional[int],
) -> None:
    """Emit the structured ``image_description_attempt`` event (one per image).

    ``channel`` is the channel token (``uazapi``, ``zapi`` or ``meta``);
    ``outcome`` is one of :data:`IMAGE_DESCRIPTION_OUTCOMES`; ``size_bytes``
    and ``latency_ms`` are ``None`` when unknown (no download, no provider
    call). The signature has no slot for the image bytes or the description:
    neither ever reaches a log record. The fields are in the message (the
    production formatter prints only the message) and in ``extra``.
    """
    if outcome not in IMAGE_DESCRIPTION_OUTCOMES:
        logger.error(
            "image_description_attempt dropped: unknown outcome channel=%s",
            channel,
        )
        return
    logged_mime = _image_event_mime(mime)
    logger.info(
        "image_description_attempt channel=%s outcome=%s mime=%s "
        "size_bytes=%s latency_ms=%s",
        channel,
        outcome,
        logged_mime,
        size_bytes,
        latency_ms,
        extra={
            "event": "image_description_attempt",
            "channel": channel,
            "outcome": outcome,
            "mime": logged_mime,
            "size_bytes": size_bytes,
            "latency_ms": latency_ms,
        },
    )


def _compose_image_message(
    caption: Optional[str],
    description: str,
    agent_language: str,
) -> str:
    """Decision 10: the patient's caption, then the bracketed description.

    ``"<caption>\\n\\n[Imagem enviada pelo paciente: <description>]"``; just
    the bracketed line without a caption; ``[Image sent by the patient: ...]``
    when the agent language is ``en``. The main LLM can thus tell what the
    patient wrote from what was seen, and the context survives later turns.
    """
    label = (
        _IMAGE_DESCRIPTION_LABEL_EN
        if agent_language == "en"
        else _IMAGE_DESCRIPTION_LABEL_PT
    )
    marker = f"[{label}: {description.strip()}]"
    cleaned_caption = (caption or "").strip()
    if cleaned_caption:
        return f"{cleaned_caption}\n\n{marker}"
    return marker


async def _multimodal_describe_image(
    image_bytes: bytes,
    mime_type: str,
    creds: dict[str, Any],
    agent_language: str,
) -> str:
    """The provider call of the image chain: the tenant's OpenRouter bundle."""
    from src.services.multimodal_service import describe_image_multimodal

    return await describe_image_multimodal(
        api_key=str(creds.get("multimodal_api_key") or ""),
        image_bytes=image_bytes,
        mimetype=mime_type,
        model=str(creds.get("multimodal_model_name") or ""),
        base_url=str(creds.get("multimodal_api_url") or ""),
        agent_language=agent_language,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((_time.monotonic() - started) * 1000)))


async def _describe_image_chain(
    image_bytes: Optional[bytes],
    credentials: dict[str, Any],
    *,
    mime_type: str,
    caption: Optional[str],
    agent_language: str,
    channel: str,
) -> tuple[str, str]:
    """Describe a patient image. Returns ``(composed_text, reason)``.

    ``reason`` is :data:`IMAGE_CHAIN_OK` (the empty string) exactly when
    ``composed_text`` is usable: the caption plus the bracketed description
    (:func:`_compose_image_message`). Otherwise the text is empty and the
    reason is :data:`IMAGE_CHAIN_NO_CAPABILITY` (the tenant's multimodal
    bundle is not operational — no provider call) or
    :data:`IMAGE_CHAIN_FAILED`.

    Mirrors ``_transcribe_audio_chain``, with one rung: capability is
    ``_multimodal_operational`` (the OpenRouter bundle). Before any call the
    chain also refuses missing bytes (``download_failed``), an image above
    :data:`IMAGE_MAX_BYTES` (``too_large``) and a MIME outside the allowlist
    (``unsupported_mime``), so no handler can send those to the provider. An
    empty description or one carrying ``IMAGE_NO_CONTENT_MARKER`` is a
    failure, never a message.

    Emits exactly one ``image_description_attempt`` event (with ``channel``)
    for the outcome it decides. ``channel`` is the channel token
    (``uazapi``, ``zapi`` or ``meta``).
    """
    from src.services.multimodal_service import (
        IMAGE_NO_CONTENT_MARKER,
        MultimodalImageError,
    )

    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    size_bytes = len(image_bytes) if image_bytes is not None else None

    def emit(outcome: ImageDescriptionOutcome, latency_ms: Optional[int]) -> None:
        _emit_image_description_attempt(
            channel=channel,
            outcome=outcome,
            mime=mime,
            size_bytes=size_bytes,
            latency_ms=latency_ms,
        )

    if not _multimodal_operational(credentials):
        emit("no_capability", None)
        return "", IMAGE_CHAIN_NO_CAPABILITY
    if not image_bytes:
        logger.warning("%s image description chain reached with no bytes", channel)
        emit("download_failed", None)
        return "", IMAGE_CHAIN_FAILED
    if len(image_bytes) > IMAGE_MAX_BYTES:
        emit("too_large", None)
        return "", IMAGE_CHAIN_FAILED
    if mime not in IMAGE_MIME_ALLOWLIST:
        emit("unsupported_mime", None)
        return "", IMAGE_CHAIN_FAILED

    started = _time.monotonic()
    try:
        description = await _multimodal_describe_image(
            image_bytes, mime, credentials, agent_language
        )
    except MultimodalImageError as exc:
        # Expected provider outcomes: the stable code is the whole story.
        logger.warning(
            "%s image description failed at the multimodal provider code=%s",
            channel,
            exc.code,
        )
        emit(
            _IMAGE_ERROR_OUTCOMES.get(exc.code, "provider_failed"), _elapsed_ms(started)
        )
        return "", IMAGE_CHAIN_FAILED
    except Exception as exc:
        logger.exception(
            "%s image description failed at the multimodal provider — %s",
            channel,
            type(exc).__name__,
        )
        emit("provider_failed", _elapsed_ms(started))
        return "", IMAGE_CHAIN_FAILED

    latency_ms = _elapsed_ms(started)
    cleaned = (description or "").strip()
    if not cleaned or IMAGE_NO_CONTENT_MARKER in cleaned:
        emit("no_content", latency_ms)
        return "", IMAGE_CHAIN_FAILED
    emit("described", latency_ms)
    return _compose_image_message(caption, cleaned, agent_language), IMAGE_CHAIN_OK


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
    except EgressDisabledError:
        raise
    except Exception as exc:
        logger.warning("Uazapi STT fallback send failed for %s — %s", phone, exc)
        return {"ok": True, "replied": False, "action": "stt_fallback_send_failed"}

    _mark_stt_fallback_notice_sent(tenant_id, phone)

    logger.info("Uazapi STT fallback sent to %s (reason=%s)", phone, failure_reason)
    return {"ok": True, "replied": True, "action": "stt_fallback"}


# ── Z-API webhook endpoint ──────────────────────────────────────────

# Patient files on Z-API (whatsapp-image-understanding Group 3). Z-API names
# each media by its own key; the field names are the "Ao receber" return
# examples of the Z-API docs (``tests/fixtures/zapi/media-payloads.json``),
# not verified on a real payload.
ZapiFileKind = Literal["image", "document", "video"]
_ZAPI_FILE_KINDS: tuple[ZapiFileKind, ...] = ("image", "document", "video")


def _zapi_patient_file_kind(body: Mapping[str, Any]) -> Optional[ZapiFileKind]:
    """The file kind of a Z-API message, or ``None`` when it carries no file.

    A media object under ``image``, ``document`` or ``video`` -- one without
    a URL included: it is acknowledged, never silenced (Decision 12). A
    sticker is never a file and keeps the ``non-text`` exit. Group and
    channel chats never get here: ``_process_zapi_authenticated_body`` skips
    them first (``_is_zapi_group_or_channel``, ``group_chat``). ``fromMe``,
    text and audio are the caller's to exclude.
    """
    if body.get("sticker"):
        return None
    for kind in _ZAPI_FILE_KINDS:
        if isinstance(body.get(kind), Mapping):
            return kind
    return None


def _zapi_sent_message_id(send_result: Mapping[str, Any]) -> str:
    """The outbound id of a Z-API send, read like the AI reply's."""
    return str(send_result.get("messageId") or send_result.get("zapiMessageId") or "")


async def _describe_zapi_image(
    image: Mapping[str, Any],
    credentials: dict[str, Any],
    *,
    agent_language: str,
) -> str:
    """Download and describe one Z-API patient image (Decisions 9-13).

    Returns the Decision 10 message (``image.caption`` plus the bracketed
    description) when the image was described, and ``""`` when it goes down
    the file flow.

    Order: capability, view-once (``image.viewOnce is True``: neither
    downloaded nor described), the URL (``imageUrl``, else ``url``, read like
    the audio's; none is ``download_failed``), the declared ``mimeType``,
    then ``safe_download_image`` -- whose ``declared_mime`` covers a CDN that
    answers ``application/octet-stream`` -- and :func:`_describe_image_chain`
    on the effective MIME. Without capability nothing is downloaded and the
    chain itself reports ``no_capability``. The refusals decided here emit
    their own ``image_description_attempt`` with ``size_bytes=None`` (Z-API
    declares no size); after a download the chain emits the rest, so each
    image emits exactly one event.

    Nothing is claimed here (Decision 9): the replay claim at the top of
    ``zapi_webhook`` already covers the message. Logs carry a stable error
    code or an exception type, never the media URL.
    """
    caption_value = image.get("caption")
    caption = caption_value if isinstance(caption_value, str) else None
    declared_mime = str(image.get("mimeType") or "")
    image_bytes: Optional[bytes] = None
    effective_mime = declared_mime

    if _multimodal_operational(credentials):
        refused: Optional[ImageDescriptionOutcome] = None
        image_url = str(image.get("imageUrl") or image.get("url") or "").strip()
        normalized_mime = declared_mime.split(";", 1)[0].strip().lower()
        if image.get("viewOnce") is True:
            refused = "view_once"
        elif not image_url:
            logger.warning("Z-API image without a URL")
            refused = "download_failed"
        elif normalized_mime and normalized_mime not in IMAGE_MIME_ALLOWLIST:
            refused = "unsupported_mime"
        else:
            try:
                download = await safe_download_image(
                    image_url,
                    max_bytes=IMAGE_MAX_BYTES,
                    declared_mime=declared_mime or None,
                )
            except UnsafeEgressError as exc:
                logger.warning("Z-API image download failed code=%s", exc.code)
                refused = _image_download_outcome(exc.code)
            except Exception as exc:
                logger.warning("Z-API image download failed — %s", type(exc).__name__)
                refused = "download_failed"
            else:
                image_bytes = download.content
                effective_mime = download.mime_type
        if refused is not None:
            _emit_image_description_attempt(
                channel="zapi",
                outcome=refused,
                mime=declared_mime,
                size_bytes=None,
                latency_ms=None,
            )
            return ""

    composed, _reason = await _describe_image_chain(
        image_bytes,
        credentials,
        mime_type=effective_mime,
        caption=caption,
        agent_language=agent_language,
        channel="zapi",
    )
    return composed


async def _send_zapi_file_ack(
    *,
    session_id: UUID,
    phone: str,
    z_inst: str,
    z_tok: str,
    z_ct: str,
    agent_language: str,
    file_kind: ZapiFileKind,
    db: AsyncSession,
) -> dict[str, Any]:
    """Acknowledge a file the agent does not analyse (Decisions 3, 4, 7, 17, 18).

    Free: no claim and no effect in any credit world; a handoff only ever
    comes from the forward-on-request path. No acknowledgement while a
    pending review is active. Otherwise the owner's copy goes out through
    ``send_text_message``; its outbound id is tracked and stored like the AI
    reply's -- Z-API echoes the bot's own sends back as ``fromMe``, and an
    untracked echo would pause the session -- and only then is the pending
    review written and committed: a failed send leaves nothing pending.
    """
    if not await _file_ack_needed(session_id, db):
        logger.info(
            "Z-API file ack suppressed for %s (type=%s): review pending",
            phone,
            file_kind,
        )
        return {"ok": True, "replied": False, "action": "file_ack_suppressed"}
    if not z_inst or not z_tok:
        logger.warning("Z-API credentials missing for file ack to %s", phone)
        return {"ok": True, "replied": False, "reason": "no_credentials"}
    try:
        send_result = await send_text_message(
            z_inst,
            z_tok,
            phone,
            _build_meta_file_ack_message(agent_language),
            client_token=z_ct,
        )
    except Exception as exc:
        # ``send_text_message`` strips the token-bearing URL from HTTP errors.
        logger.warning("Z-API file ack send failed for %s — %s", phone, exc)
        return {"ok": True, "replied": False, "reason": "send_failed"}
    _track_sent_message(send_result)
    ack_mid = _zapi_sent_message_id(send_result)
    if ack_mid:
        await _store_ai_msg_id_to_db(session_id, ack_mid, db)
    await _meta_set_pending_file_review(session_id, db)
    await db.commit()
    logger.info("Z-API file ack sent to %s (type=%s)", phone, file_kind)
    return {"ok": True, "replied": True, "action": "file_ack"}


async def _send_zapi_handoff_notice(
    *,
    session_id: UUID,
    phone: str,
    z_inst: str,
    z_tok: str,
    z_ct: str,
    message: str,
    label: str,
    db: AsyncSession,
) -> None:
    """Send the fixed notice of an already committed file handoff.

    Like Meta, a notice that cannot go out is logged and the route still
    answers as if it had. A sent notice is tracked and stored like the AI
    reply, so its echo is an ``ai_echo`` and never reads as a human takeover.
    """
    if not z_inst or not z_tok:
        logger.warning("Z-API credentials missing for %s to %s", label, phone)
        return
    try:
        send_result = await send_text_message(
            z_inst, z_tok, phone, message, client_token=z_ct
        )
    except Exception as exc:
        logger.warning("Z-API %s send failed for %s — %s", label, phone, exc)
        return
    _track_sent_message(send_result)
    sent_mid = _zapi_sent_message_id(send_result)
    if sent_mid:
        await _store_ai_msg_id_to_db(session_id, sent_mid, db)
    await db.commit()


async def _send_zapi_file_forwarded(
    *,
    session_id: UUID,
    phone: str,
    z_inst: str,
    z_tok: str,
    z_ct: str,
    agent_language: str,
    db: AsyncSession,
) -> None:
    """Confirm a forward-on-request handoff to the patient (Decision 17)."""
    await _send_zapi_handoff_notice(
        session_id=session_id,
        phone=phone,
        z_inst=z_inst,
        z_tok=z_tok,
        z_ct=z_ct,
        message=(
            _FILE_FORWARDED_MSG_EN if agent_language == "en" else _FILE_FORWARDED_MSG_PT
        ),
        label="file-forward confirmation",
        db=db,
    )


async def _send_zapi_audio_too_long(
    *,
    session_id: UUID,
    phone: str,
    z_inst: str,
    z_tok: str,
    z_ct: str,
    agent_language: str,
    db: AsyncSession,
) -> None:
    """Tell the patient their long audio went to the team, then mark the
    conversation unread so the clinic sees it on the phone as well."""
    await _send_zapi_handoff_notice(
        session_id=session_id,
        phone=phone,
        z_inst=z_inst,
        z_tok=z_tok,
        z_ct=z_ct,
        message=(
            _AUDIO_TOO_LONG_MSG_EN if agent_language == "en" else _AUDIO_TOO_LONG_MSG_PT
        ),
        label="long-audio notice",
        db=db,
    )
    if z_inst and z_tok:
        await mark_conversation_unread(z_inst, z_tok, phone, client_token=z_ct)


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

    # Origin, classified once: the capability digest matched exactly one
    # tenant and the body is bound to its instance, so this is production.
    origin = InteractionOrigin.PRODUCTION

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
        origin=origin,
        request=request,
    )


_ZAPI_GROUP_OR_CHANNEL_PHONE_SUFFIXES = ("-group", "@g.us", "@newsletter")


def _is_zapi_group_or_channel(body: dict[str, Any]) -> bool:
    """Group and channel (newsletter) chats are out of scope for Z-API.

    Mirrors ``_is_uazapi_group_chat``. Z-API flags a group with
    ``"isGroup": true`` and a ``"<id>-group"`` phone, and a channel with
    ``"isNewsletter": true``. The phone suffix alone is enough, and the
    ``@g.us`` / ``@newsletter`` JID forms are accepted defensively, so an
    unflagged payload still never reaches the pipeline or a reply.
    """
    if body.get("isGroup") is True or body.get("isNewsletter") is True:
        return True
    raw_phone = body.get("phone")
    return isinstance(raw_phone, str) and raw_phone.strip().endswith(
        _ZAPI_GROUP_OR_CHANNEL_PHONE_SUFFIXES
    )


async def _process_zapi_authenticated_body(
    *,
    body: dict[str, Any],
    tenant_id: UUID,
    db: AsyncSession,
    origin: InteractionOrigin,
    request: Optional[Request] = None,
) -> Response | dict[str, Any]:
    """Downstream Z-API processing for an already authenticated request.

    Callers must have verified: canonical token, digest match, ``instanceId``
    binding, rate limit, ``messageId`` presence and replay-store claim.
    ``origin`` is the one the caller classified after that proof.
    ``request`` only carries the credit switch captured at entry (observational
    metering, neurostack-credit-system Group 3a); without it nothing is metered.
    """
    # Groups and channels exit before any side effect: no credential read,
    # no fromMe takeover (a clinic post in a group pauses nothing), no media
    # download or STT, no credit pre-check, no session and no reply.
    if _is_zapi_group_or_channel(body):
        logger.info("Z-API webhook: skipping group chat %s", body.get("phone"))
        return {"ok": True, "skipped": "group_chat"}

    text_obj = body.get("text")
    audio_obj = body.get("audio")

    raw_phone = body.get("phone", "")
    phone = _normalise_phone(raw_phone)
    chat_lid = body.get("chatLid", "")
    sender_name = body.get("senderName", "")

    if not phone:
        return {"ok": True, "skipped": "missing_fields"}

    is_from_me = bool(body.get("fromMe", False))

    # ── credit capture (debito-cortesia-automatico, DESIGN item 5) ──
    # A patient message's effective enforcement is read ONCE, before the
    # first thing a builder BLOCKED by its courtesy rule must not do: the
    # audio download, STT and fallback of a patient audio, else the read
    # receipt (owner decisions 11 and 16) -- so the paths that leave earlier
    # read nothing, as before.  The decision itself stays at the pre-check
    # below, on this capture (no second read).  The rule's D1 answers 503 at
    # once (whether the builder is blocked cannot be known); a global or
    # mark D1 is held and answered at the pre-check, where it was before.
    # Captured on the rule, ``blocked`` (``rule_blocked``): no download, no
    # STT, no fallback, no read receipt; the AI-off and pause gates still run
    # (a paused session keeps capturing the text, never a transcription) and
    # the pre-check answers ``credit_suspended``.
    early_capture: Optional[credit_metering.EarlyWebhookCapture] = None

    async def _capture_credit() -> Optional[JSONResponse]:
        nonlocal early_capture
        if early_capture is not None or request is None or is_from_me:
            return None
        try:
            early_capture = await credit_metering.capture_zapi(
                request, db, tenant_id=tenant_id, body=body
            )
        except CreditEnforcementUnavailable:
            # The rule's D1; like every 503 here it drops the message (main's
            # replay claim is already spent).
            return JSONResponse(
                status_code=503,
                content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
            )
        return None

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

    # A patient image, document or video (whatsapp-image-understanding,
    # Decisions 3, 8). It is the only media let through the ``non-text`` and
    # ``empty`` guards below, and it is described or acknowledged at the
    # insertion point after the LGPD notice. A sticker and the clinic's own
    # media (``fromMe``) are never files: they keep today's exits and never
    # reach ``_handle_clinic_message``, where a staff image would read as a
    # human takeover and pause the AI.
    patient_file_kind: Optional[ZapiFileKind] = (
        None if is_from_me or text_obj or audio_obj else _zapi_patient_file_kind(body)
    )

    # Handle audio/voice messages.
    # ``not is_from_me`` sits in the block condition — not merely above the
    # download — so no branch inside the block (download, STT, handoff, reply)
    # is reachable at all for the clinic's own audio.
    #
    # The metadata block near the end of this handler reads ``whisper_model``
    # under a *different* guard (``audio_obj and message_text``), which is also
    # true for payloads carrying both text and audio, so it must stay bound.
    whisper_model: Optional[str] = None
    # A patient audio above ``audio_max_seconds``: never downloaded or
    # transcribed, handed off to the team at the insertion point below.
    audio_too_long = False
    if patient_audio_input:
        unavailable = await _capture_credit()
        if unavailable is not None:
            return unavailable
    rule_blocked = early_capture is not None and early_capture.blocked
    if audio_obj and not message_text and not is_from_me and not rule_blocked:
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

            # Duration-based limit: a long audio goes to the clinic team
            # instead of being transcribed. It runs above the capability gate,
            # like Meta's: otherwise a 3-minute audio would get a handoff on
            # Meta and "please type" on Z-API. The duration comes from the
            # payload, so this costs no download. Only the decision is taken
            # here: the handoff and its notice wait below the AI-off, pause
            # and credit gates and the session, where Meta's run too.
            audio_max_seconds = int(
                (creds or {}).get("audio_max_seconds") or DEFAULT_AUDIO_MAX_SECONDS
            )
            audio_seconds = int(audio_obj.get("seconds") or 0)
            if audio_seconds > audio_max_seconds:
                audio_too_long = True
                logger.info(
                    "Audio too long (%ds > %ds limit), skipping transcription for %s",
                    audio_seconds,
                    audio_max_seconds,
                    phone,
                )
            else:
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
                        download = await safe_download_audio(
                            audio_url, max_bytes=max_bytes
                        )
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

    if not text_obj and not audio_obj and patient_file_kind is None:
        return {"ok": True, "skipped": "non-text"}
    # ``staff_audio`` is one case allowed past the empty-text guard: the
    # audio was deliberately left untranscribed, and the fromMe block below
    # routes it to _handle_clinic_message with empty text — empty text cannot
    # match the trigger phrase, so it falls through to human takeover. A
    # patient file is the other: it has no text of its own (a caption is the
    # file's, never the patient's message) and never takes the fromMe block.
    # A long patient audio is the third: it is handed off, never transcribed.
    # A rule-blocked patient audio is the fourth: never transcribed, it is
    # answered ``credit_suspended`` by the pre-check below.
    if (
        not message_text
        and not staff_audio
        and patient_file_kind is None
        and not audio_too_long
        and not (rule_blocked and patient_audio_input)
    ):
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
    # Never for a rule-blocked builder (owner decision 16): the message stays
    # unread on the clinic's phone and the patient does not see it read.
    unavailable = await _capture_credit()
    if unavailable is not None:
        return unavailable
    rule_blocked = early_capture is not None and early_capture.blocked
    if not rule_blocked:
        try:
            _creds = await _tenant_creds()
            _inst = _creds.get("zapi_instance_id", "")
            _tok = _creds.get("zapi_token", "")
            _ct = _creds.get("zapi_client_token", "")
            if _inst and _tok:
                await mark_message_read(
                    _inst, _tok, phone, body.get("messageId", ""), _ct
                )
        except Exception:
            pass  # fire-and-forget

    # ── bot_active check — if AI is paused, send offline msg once then skip ──
    bot_skip = await _check_bot_active(tenant_id, phone, db)
    if bot_skip:
        await db.commit()
        return JSONResponse(status_code=202, content=bot_skip)

    # ── pause check for patient messages ──
    pause_result = await _check_pause_state(
        tenant_id, phone, db, message_text=message_text, origin=origin
    )
    if pause_result:
        await db.commit()
        return JSONResponse(status_code=202, content=pause_result)

    # ── credit pre-check (neurostack-credit-system Group 3b-2) ──
    # Right after the pause check and before the session exists: under a
    # hold (or for a standalone tenant) once credits are enforced, a new
    # patient gets no session row and no LGPD notice.  It decides on the
    # capture taken above (``early_capture``: no second read).  Accepted per
    # channel (D4/OWNER-c) under the global and a mark: on Z-API the STT and
    # its fallback ran above and their fixed messages already reached the
    # patient, and the message was already marked read.  Under the courtesy
    # rule a BLOCKED builder skipped all of them (see ``rule_blocked``); a
    # charged one keeps that order.  ``audio_too_long`` is no longer among
    # them: its handoff and notice run below, so under a hold, as on Meta,
    # neither happens.
    enforcement: Optional[WebhookEnforcement] = None
    if request is not None:
        try:
            verdict = await credit_metering.precheck_zapi(
                request, db, tenant_id=tenant_id, body=body, early=early_capture
            )
        except CreditEnforcementUnavailable:
            # D1 latch.  In practice this DROPS the message: main's replay
            # claim (Redis) was already taken above, so the provider's retry
            # of this 503 answers ``duplicate_message``.
            return JSONResponse(
                status_code=503,
                content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
            )
        if verdict == "credit_pending":
            return dict(credit_metering.CREDIT_PENDING_SKIP)
        if verdict == "credit_suspended":
            # Group 5: the builder is suspended -- no session, no reply.
            return dict(credit_metering.CREDIT_SUSPENDED_SKIP)
        if isinstance(verdict, WebhookEnforcement):
            enforcement = verdict

    # ── get or create a session for this phone ──
    session_id, is_new = await _get_or_create_session(tenant_id, phone, db)
    context = build_interaction_context(
        effective_tenant_id=tenant_id,
        session_id=session_id,
        origin=origin,
        event_type=EventType.MESSAGE_IN,
    )

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

    # ── patient files (whatsapp-image-understanding, Decisions 3, 4, 7-14) ──
    # Here, after the AI-off and pause gates, the credit pre-check, the
    # session and the LGPD notice: nothing below runs with the AI off or a
    # paused session, and a new session's notice already went out. No claim
    # of its own: the replay claim at the top of ``zapi_webhook`` covers it.
    agent_lang = _get_agent_language(clinic_config)

    # ── long patient audio: hand off to the team, never transcribed ──
    # Same place and same decision as Meta's ``audio_too_long``: after the
    # AI-off and pause gates (a paused session is already with a human), the
    # credit pre-check, the session and the LGPD notice. The handoff is the
    # decision: no pipeline and no ``message.in``, metered like the file
    # forward below. A pending file review is left as it is, as on Meta.
    if audio_too_long:
        skipped = await _meta_file_handoff(
            tenant_id=tenant_id,
            session_id=session_id,
            from_phone=phone,
            note="audio_too_long",
            context=context,
            db=db,
            metering=(
                None
                if request is None or enforcement is not None
                else zapi_ingress(request, body=body, context=context)
            ),
            enforcement=enforcement,
            clear_pending_review=False,
        )
        if skipped is not None:
            return skipped
        audio_creds = await _tenant_creds()
        await _send_zapi_audio_too_long(
            session_id=session_id,
            phone=phone,
            z_inst=str(audio_creds.get("zapi_instance_id") or "").strip(),
            z_tok=str(audio_creds.get("zapi_token") or "").strip(),
            z_ct=str(audio_creds.get("zapi_client_token") or "").strip(),
            agent_language=agent_lang,
            db=db,
        )
        return {"ok": True, "skipped": "audio_too_long"}

    if patient_file_kind is None:
        # A textual message (text, or audio transcribed above) resolves an
        # active pending review. A file's caption never does: files never
        # take this branch. An expired or legacy flag counts as absent.
        if await _pending_file_review_active(session_id, db):
            if _detect_forward_intent(message_text):
                # Credit-metered like Meta (Decision 7): with the pre-check's
                # enforcement, one transaction in ``run_meta_file_handoff``
                # (claim ``zapi``, handoff, debit); without it, the Group 3a
                # writer on the Z-API ingress.
                skipped = await _meta_file_handoff(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    from_phone=phone,
                    note="file_pending_review",
                    context=context,
                    db=db,
                    metering=(
                        None
                        if request is None or enforcement is not None
                        else zapi_ingress(request, body=body, context=context)
                    ),
                    enforcement=enforcement,
                    clear_pending_review=True,
                )
                if skipped is not None:
                    return skipped
                forward_creds = await _tenant_creds()
                await _send_zapi_file_forwarded(
                    session_id=session_id,
                    phone=phone,
                    z_inst=str(forward_creds.get("zapi_instance_id") or "").strip(),
                    z_tok=str(forward_creds.get("zapi_token") or "").strip(),
                    z_ct=str(forward_creds.get("zapi_client_token") or "").strip(),
                    agent_language=agent_lang,
                    db=db,
                )
                return {"ok": True, "replied": True, "action": "file_handoff"}
            # Not a forward: the patient is telling what they need.
            await _meta_clear_pending_file_review(session_id, db)
            await db.commit()
    else:
        file_creds = await _tenant_creds()
        image_text = ""
        if patient_file_kind == "image":
            image_obj = body.get("image")
            image_text = await _describe_zapi_image(
                image_obj if isinstance(image_obj, Mapping) else {},
                file_creds,
                agent_language=agent_lang,
            )
        if not image_text:
            return await _send_zapi_file_ack(
                session_id=session_id,
                phone=phone,
                z_inst=str(file_creds.get("zapi_instance_id") or "").strip(),
                z_tok=str(file_creds.get("zapi_token") or "").strip(),
                z_ct=str(file_creds.get("zapi_client_token") or "").strip(),
                agent_language=agent_lang,
                file_kind=patient_file_kind,
                db=db,
            )
        # Described: the caption plus the description is the patient's
        # message (Decision 10), billed like any turn by the pipeline below.
        # An active pending review stays as it is (Decision 14); the next
        # textual message resolves it.
        message_text = image_text

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
    event_request = CanonicalEventRequest(
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

    media = _inline_media(request, _ZAPI_MEDIA_PROVIDER)
    try:
        outcome = await _run_pipeline_transactionally(
            service=service,
            event_request=event_request,
            db=db,
            context=context,
            metering=(
                None
                if request is None or enforcement is not None
                else zapi_ingress(request, body=body, context=context)
            ),
            enforcement=enforcement,
            reply=_ReplyTarget("zapi", phone, patient_audio_input),
            media=media,
        )
    except CreditEnforcementUnavailable:
        # cobranca-credito-por-builder: the enforced claim found the tenant
        # relinked since the pre-check -- nothing written; the pre-check's 503
        # (the message is lost: main's replay claim is spent).
        return JSONResponse(
            status_code=503,
            content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
        )
    if isinstance(outcome, dict):
        # Enforced: claimed, charged and its reply sent from the outbox.
        return outcome
    response = outcome

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
            language_code=response.response_language,
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

    # ── the turn's file, after the text's 2xx (envio-midia-agente) ──
    await _send_inline_reply_media(
        media,
        response,
        db=db,
        credentials=creds,
        phone=phone,
        tenant_id=tenant_id,
        session_id=session_id,
    )
    return {"ok": True, "replied": True}


# ── Evolution API webhook endpoint ───────────────────────────────────


# The id Baileys generates on the socket (``generateMessageIDV2``), identical
# at every Baileys Evolution pins. Phone-typed ids are Android 32-hex or iOS
# ``3A...`` and never match.
_EVOLUTION_SOCKET_MESSAGE_ID = re.compile(r"3EB0[0-9A-F]{18}")


def _is_evolution_call_auto_reply(data: dict[str, Any]) -> bool:
    """Whether a ``fromMe`` upsert is Evolution's own call auto-reply.

    When a call arrives and the instance has ``msgCall`` set, Evolution sends
    that text and re-emits the send as ``messages.upsert`` (2.2.0 to
    2.4.0-rc2; 2.3.7 ``whatsapp.baileys.service.ts:1890-1896``). The message
    is generated on the socket, so it carries Baileys' ``PENDING`` status
    (forwarded as ``data.status == "PENDING"``) and a socket id
    (``3EB0`` + 18 uppercase hex). Both must hold.

    A message decoded from the wire (typed on another device of the clinic's
    number) is stamped ``SERVER_ACK`` by Baileys. Evolution's socket runs
    with ``emitOwnEvents: false`` and posts its API sends as
    ``send.message``, so the re-emit is the only ``PENDING`` ``fromMe``
    upsert it produces, with two known exceptions:

    * Placeholder resend: when a message fails to decrypt, the phone's
      resend answer is re-emitted with a status the phone wrote, which
      Baileys does not normalise (rc.9 ``process-message.ts:339-358``). The id
      check closes this for phone-typed staff messages; a message from a
      web-based companion linked to the number (WhatsApp Web, or a
      Baileys-based CRM or another Evolution instance), whose id has the
      socket shape, could still be skipped.
    * A receipt merged into the still-buffered upsert overwrites ``PENDING``
      (rc.9 ``event-buffer.ts:444``). The check then misses and the message
      takes over, which is today's behaviour and harmless.

    Anything else, including a missing status or id, is staff, as before.
    """
    if data.get("status") != "PENDING":
        return False
    key = data.get("key")
    message_id = key.get("id") if isinstance(key, dict) else None
    return isinstance(message_id, str) and bool(
        _EVOLUTION_SOCKET_MESSAGE_ID.fullmatch(message_id)
    )


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

    Credits (debito-cortesia-automatico Group 4, DESIGN item 9): Evolution has
    no origin proof, so every callback is ``unknown`` and charged only under
    its builder's courtesy rule -- through the Z-API/Meta path, a blocked
    builder answered ``credit_suspended`` with nothing sent
    (``credit_metering.capture_evolution`` / ``precheck_evolution``); under
    the global, a mark or nothing the turn stays observational.  Every
    patient message reads the mode once, after the ``fromMe`` routing (the
    AI echo and the call auto-reply) and before the AI-off and pause gates
    (so a failed read never undoes the pause gate's lazy unpause); the
    decision waits for the pre-check after them, before the session.  There
    is no egress clause (Evolution has no egress flag), and the Evolution
    route sends no read receipt.

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
    # This also drops ``send.message``, the event Evolution emits for every
    # message sent through its API (the agent's replies included), so those
    # never reach the ``fromMe`` takeover below.
    event_type = body.get("event", "")
    if event_type != "messages.upsert":
        return {"ok": True, "skipped": "non_message_event"}

    data = body.get("data", {})
    if not isinstance(data, dict):
        return {"ok": True, "skipped": "invalid_data"}

    key = data.get("key", {})
    if not isinstance(key, dict):
        return {"ok": True, "skipped": "missing_message_id"}
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

    # Origin, classified once: an instance name is a lookup key, not a proof
    # of the sender's authenticity, so Evolution is never production.
    origin = InteractionOrigin.UNKNOWN

    # ── require and claim the provider message id before downstream effects ──
    webhook_msg_id = str(key.get("id") or "").strip()
    if not webhook_msg_id:
        return {"ok": True, "skipped": "missing_message_id"}
    try:
        first_delivery = _claim_incoming_message("evolution", tenant_id, webhook_msg_id)
    except ReplayStoreUnavailable:
        return JSONResponse(
            status_code=503,
            content={"error": "replay_store_unavailable"},
        )
    if not first_delivery:
        return {"ok": True, "skipped": "duplicate_message"}

    # ── load clinic_config early (needed for bot_active offline_message + LGPD) ──
    service = get_event_service()
    clinic_config = await service._load_clinic_config(tenant_id)

    # ── fromMe=true: check if AI-sent echo or real clinic message ──
    if is_from_me:
        # Fast path: in-memory + Redis check (same worker, Redis available)
        if webhook_msg_id and _is_ai_sent_message(webhook_msg_id):
            return {"ok": True, "skipped": "ai_echo"}
        # The instance's own call auto-reply is not staff typing: without
        # this it would pause the AI (no expiry under the default config).
        if _is_evolution_call_auto_reply(data):
            return {"ok": True, "skipped": "call_auto_reply"}
        # Pass message_id for DB-backed cross-worker dedup inside _handle_clinic_message
        result = await _handle_clinic_message(
            tenant_id, phone, message_text, db, message_id=webhook_msg_id
        )
        await db.commit()
        return result

    # ── credit capture (debito-cortesia-automatico Group 4, DESIGN item 9) ──
    # Every patient message's effective enforcement is read ONCE, here: after
    # the Redis replay claim and the fromMe routing (the AI echo and the call
    # auto-reply never read it), before the AI-off and pause gates -- a
    # failed read rolls the session back, and after the pause gate it would
    # undo the lazy unpause of an expired pause (flushed, not yet committed)
    # while the turn is still answered.  Nothing a blocked builder must not
    # do runs before the pre-check below (the route takes text only: no
    # download, STT or description exists here).  Evolution is ``unknown``
    # and charged only under the rule (``rule_only``): under the global, a
    # mark or nothing the capture is main's path.  The rule's D1 answers 503
    # at once (the message is lost: its Redis claim is spent, as on Z-API).
    early_capture: Optional[credit_metering.EarlyWebhookCapture] = None
    try:
        early_capture = await credit_metering.capture_evolution(
            request, db, tenant_id=tenant_id, data=data
        )
    except CreditEnforcementUnavailable:
        return JSONResponse(
            status_code=503,
            content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
        )

    # ── bot_active check — if AI is paused, send offline msg once then skip ──
    bot_skip = await _check_bot_active(tenant_id, phone, db)
    if bot_skip:
        await db.commit()
        return JSONResponse(status_code=202, content=bot_skip)

    # ── pause check for patient messages ──
    # A paused session keeps capturing the text, blocked or not.
    pause_result = await _check_pause_state(
        tenant_id, phone, db, message_text=message_text, origin=origin
    )
    if pause_result:
        await db.commit()
        return JSONResponse(status_code=202, content=pause_result)

    # ── credit pre-check (debito-cortesia-automatico Group 4, DESIGN item 9) ──
    # Right after the AI-off and pause gates and before the session, the LGPD
    # notice and every send: decided on the capture above (never read
    # again).  ``blocked`` is ``credit_suspended`` -- no session, no reply, no
    # LGPD notice --; ``charged`` is enforced: the PostgreSQL claim right
    # before phase 1, on top of the Redis claim taken where it always is.  No
    # egress clause (wish Decision 7).  Under the global, a mark or nothing:
    # main's path.  A 503 after the Redis claim loses the message, as on
    # Z-API and Meta.
    enforcement: Optional[WebhookEnforcement] = None
    try:
        verdict = await credit_metering.precheck_evolution(
            request, db, tenant_id=tenant_id, data=data, early=early_capture
        )
    except CreditEnforcementUnavailable:
        return JSONResponse(
            status_code=503,
            content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
        )
    if verdict == "credit_pending":
        return dict(credit_metering.CREDIT_PENDING_SKIP)
    if verdict == "credit_suspended":
        return dict(credit_metering.CREDIT_SUSPENDED_SKIP)
    if isinstance(verdict, WebhookEnforcement):
        enforcement = verdict

    # ── get or create a session for this phone ──
    session_id, is_new = await _get_or_create_session(tenant_id, phone, db)
    context = build_interaction_context(
        effective_tenant_id=tenant_id,
        session_id=session_id,
        origin=origin,
        event_type=EventType.MESSAGE_IN,
    )
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

    event_request = CanonicalEventRequest(
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
                "messageId": webhook_msg_id,
                "source": "evolution",
            },
        ),
    )

    media = _inline_media(request, _EVOLUTION_MEDIA_PROVIDER)
    try:
        outcome = await _run_pipeline_transactionally(
            service=service,
            event_request=event_request,
            db=db,
            context=context,
            metering=(
                None
                if enforcement is not None
                else evolution_ingress(request, data=data, context=context)
            ),
            enforcement=enforcement,
            reply=_ReplyTarget("evolution", phone, False),
            media=media,
        )
    except CreditEnforcementUnavailable:
        # The charged claim found the tenant relinked since the pre-check:
        # nothing written; the pre-check's 503 (the message is lost: the
        # Redis replay claim is spent).
        return JSONResponse(
            status_code=503,
            content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
        )
    if isinstance(outcome, dict):
        # Charged under the courtesy rule: claimed, charged and its reply
        # sent from the outbox (``CreditReplySender``, Evolution branch), its
        # file started after the text by the same sender.
        return outcome
    response = outcome

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
            language_code=response.response_language,
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
        # receive the same value. Evolution's documented send response
        # carries it only as ``key.id``. Tracking it is defensive: Evolution
        # reports this send as ``send.message``, skipped at the top of this
        # handler, not as a ``messages.upsert`` echo.
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

    # ── the turn's file, after the text's 2xx (envio-midia-agente) ──
    await _send_inline_reply_media(
        media,
        response,
        db=db,
        credentials=creds,
        phone=phone,
        tenant_id=tenant_id,
        session_id=session_id,
    )
    return {"ok": True, "replied": True}


# ── Uazapi patient files (whatsapp-image-understanding) ─────────────

# The file kinds a patient message can take on Uazapi. An image is described
# when it can be; a document, a video and an image that is not described get
# the file acknowledgement (Decision 3).
UazapiFileKind = Literal["image", "document", "video"]

# ``messageType`` names per kind, compared lowercased. Only ``ImageMessage``
# is proven by a captured payload (``media-payload.json``); the document and
# video names are not, so ``type``/``mediaType`` are accepted as well.
_UAZAPI_IMAGE_MESSAGE_TYPES = frozenset({"imagemessage"})
_UAZAPI_DOCUMENT_MESSAGE_TYPES = frozenset(
    {"documentmessage", "documentwithcaptionmessage"}
)
_UAZAPI_VIDEO_MESSAGE_TYPES = frozenset({"videomessage"})


def _uazapi_is_view_once(msg: Mapping[str, Any]) -> bool:
    """Decision 13: ``content.viewOnce == true`` or a ``*ViewOnce*`` type.

    Neither signal is verified against a real Uazapi payload, so both count.
    """
    content = msg.get("content")
    if isinstance(content, Mapping) and content.get("viewOnce") is True:
        return True
    return "viewonce" in str(msg.get("messageType") or "").lower()


def _uazapi_file_kind(msg: Mapping[str, Any]) -> Optional[UazapiFileKind]:
    """The file kind of a Uazapi message, or ``None`` when it is no file.

    Case-insensitive over ``type``, ``mediaType`` and ``messageType``. A
    sticker is never a file (it keeps the ``non-text`` exit). Audio is the
    caller's to exclude: it has its own path. A view-once message of no
    recognised kind still takes the image path, which refuses it before any
    download and acknowledges it (Decision 13), so it never ends in silence.
    """
    kinds = {
        str(msg.get("type") or "").strip().lower(),
        str(msg.get("mediaType") or "").strip().lower(),
    }
    message_type = str(msg.get("messageType") or "").strip().lower()
    if "sticker" in kinds or message_type == "stickermessage":
        return None
    if "image" in kinds or message_type in _UAZAPI_IMAGE_MESSAGE_TYPES:
        return "image"
    if "document" in kinds or message_type in _UAZAPI_DOCUMENT_MESSAGE_TYPES:
        return "document"
    if "video" in kinds or message_type in _UAZAPI_VIDEO_MESSAGE_TYPES:
        return "video"
    if _uazapi_is_view_once(msg):
        return "image"
    return None


def _uazapi_declared_size(value: Any) -> Optional[int]:
    """``content.fileLength`` as a byte count; ``None`` when not a count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _uazapi_image_caption(msg: Mapping[str, Any]) -> Optional[str]:
    """Decision 10: ``content.caption``, falling back to ``message.text``."""
    content = msg.get("content")
    if isinstance(content, Mapping):
        caption = content.get("caption")
        if isinstance(caption, str) and caption.strip():
            return caption
    return _extract_uazapi_text_value(msg.get("text"))


def _uazapi_egress_retry_response() -> JSONResponse:
    """The retryable 503 of a closed Uazapi egress, answered before any claim."""
    return JSONResponse(
        status_code=503,
        content={"ok": False, "error": "uazapi_egress_disabled", "retryable": True},
    )


async def _describe_uazapi_image(
    msg: Mapping[str, Any],
    credentials: dict[str, Any],
    *,
    msg_id: str,
    claim: Callable[[], Optional[dict[str, Any]]],
    agent_language: str,
) -> tuple[str, Response | dict[str, Any] | None]:
    """Gate, download, claim and describe one patient image (Decisions 9-13).

    Returns ``(composed_text, answer)``. A non-``None`` ``answer`` is returned
    by the route as is: the retryable 503 when the egress closes during the
    download (nothing claimed), or ``duplicate_message`` on a redelivery.
    Otherwise ``composed_text`` is the Decision 10 message when the image was
    described, and ``""`` when it goes down the file flow.

    Order: capability, view-once, declared ``fileLength``/``mimetype``,
    download, claim, :func:`_describe_image_chain`. Without capability nothing
    is downloaded and the chain itself reports ``no_capability``. The refusals
    decided here (view-once, a declared size or MIME off the limits, a failed
    download) emit their own ``image_description_attempt``; after a download
    the chain re-checks size and MIME on the effective MIME (downloaded, else
    declared) and emits the rest, so each image emits exactly one event.
    Pre-chain refusals report the declared ``content.fileLength`` as
    ``size_bytes`` (``None`` when absent or not a count).
    """
    content_value = msg.get("content")
    content: Mapping[str, Any] = (
        content_value if isinstance(content_value, Mapping) else {}
    )
    declared_mime = str(content.get("mimetype") or "")
    declared_size = _uazapi_declared_size(content.get("fileLength"))
    image_bytes: Optional[bytes] = None
    effective_mime = declared_mime

    if _multimodal_operational(credentials):
        refused: Optional[ImageDescriptionOutcome] = None
        normalized_mime = declared_mime.split(";", 1)[0].strip().lower()
        if _uazapi_is_view_once(msg):
            refused = "view_once"
        elif declared_size is not None and declared_size > IMAGE_MAX_BYTES:
            refused = "too_large"
        elif normalized_mime and normalized_mime not in IMAGE_MIME_ALLOWLIST:
            refused = "unsupported_mime"
        else:
            try:
                image_bytes, downloaded_mime = await download_uazapi_media(
                    str(credentials.get("uazapi_api_url") or "").strip(),
                    str(credentials.get("uazapi_token") or "").strip(),
                    msg_id,
                )
            except EgressDisabledError:
                return "", _uazapi_egress_retry_response()
            except UazapiMediaDownloadError as exc:
                logger.warning("Uazapi image download failed for %s — %s", msg_id, exc)
                refused = "download_failed"
            else:
                effective_mime = downloaded_mime or declared_mime
        if refused is not None:
            duplicate = claim()
            if duplicate is not None:
                return "", duplicate
            _emit_image_description_attempt(
                channel="uazapi",
                outcome=refused,
                mime=declared_mime,
                size_bytes=declared_size,
                latency_ms=None,
            )
            return "", None

    duplicate = claim()
    if duplicate is not None:
        return "", duplicate
    composed, _reason = await _describe_image_chain(
        image_bytes,
        credentials,
        mime_type=effective_mime,
        caption=_uazapi_image_caption(msg),
        agent_language=agent_language,
        channel="uazapi",
    )
    return composed, None


async def _send_uazapi_file_ack(
    *,
    session_id: UUID,
    phone: str,
    u_url: str,
    u_token: str,
    agent_language: str,
    file_kind: UazapiFileKind,
    db: AsyncSession,
) -> dict[str, Any]:
    """Acknowledge a file the agent does not analyse (Decisions 3, 4, 17, 18).

    No acknowledgement while a pending review is active. Otherwise the
    owner's copy goes out, the outbound id is tracked like the AI reply's (so
    its ``fromMe`` echo is an ``ai_echo``), and only then is the pending review
    written and committed: a failed send leaves nothing pending.
    """
    if not await _file_ack_needed(session_id, db):
        logger.info(
            "Uazapi file ack suppressed for %s (type=%s): review pending",
            phone,
            file_kind,
        )
        return {"ok": True, "replied": False, "action": "file_ack_suppressed"}
    if not u_url or not u_token:
        logger.warning("Uazapi credentials missing for file ack to %s", phone)
        return {"ok": True, "replied": False, "reason": "no_credentials"}
    try:
        send_result = await send_uazapi_text(
            u_url, u_token, phone, _build_meta_file_ack_message(agent_language)
        )
    except EgressDisabledError:
        return {"ok": True, "replied": False, "reason": "uazapi_egress_disabled"}
    except Exception:
        logger.exception("Uazapi file ack send failed for %s", phone)
        return {"ok": True, "replied": False, "reason": "send_failed"}
    ack_mid = _extract_normalised_send_id(send_result)
    if ack_mid:
        _track_sent_message({"messageid": ack_mid})
        await _store_ai_msg_id_to_db(session_id, ack_mid, db)
    else:
        _track_sent_message(send_result)
    await _meta_set_pending_file_review(session_id, db)
    await db.commit()
    logger.info("Uazapi file ack sent to %s (type=%s)", phone, file_kind)
    return {"ok": True, "replied": True, "action": "file_ack"}


async def _send_uazapi_handoff_notice(
    *,
    session_id: UUID,
    phone: str,
    u_url: str,
    u_token: str,
    message: str,
    label: str,
    db: AsyncSession,
) -> bool:
    """Send the fixed notice of an already committed file handoff.

    Like Meta, a notice that cannot go out is logged and the route still
    answers as if it had. A sent notice is tracked and stored like the AI
    reply, so its echo is an ``ai_echo``. Returns ``False`` only when the
    egress switch held the notice back, so a caller can say so.
    """
    if not u_url or not u_token:
        logger.warning("Uazapi credentials missing for %s to %s", label, phone)
        return True
    try:
        send_result = await send_uazapi_text(u_url, u_token, phone, message)
    except EgressDisabledError:
        logger.warning("Uazapi %s to %s suppressed: egress disabled", label, phone)
        return False
    except Exception:
        logger.exception("Uazapi %s send failed for %s", label, phone)
        return True
    sent_mid = _extract_normalised_send_id(send_result)
    if sent_mid:
        _track_sent_message({"messageid": sent_mid})
        await _store_ai_msg_id_to_db(session_id, sent_mid, db)
    else:
        _track_sent_message(send_result)
    await db.commit()
    return True


async def _send_uazapi_file_forwarded(
    *,
    session_id: UUID,
    phone: str,
    u_url: str,
    u_token: str,
    agent_language: str,
    db: AsyncSession,
) -> None:
    """Confirm a forward-on-request handoff to the patient (Decision 17)."""
    await _send_uazapi_handoff_notice(
        session_id=session_id,
        phone=phone,
        u_url=u_url,
        u_token=u_token,
        message=(
            _FILE_FORWARDED_MSG_EN if agent_language == "en" else _FILE_FORWARDED_MSG_PT
        ),
        label="file-forward confirmation",
        db=db,
    )


# ── Uazapi inbound authentication (uazapi-webhook-auth) ─────────────
#
# uazapi 2.6.0 puts the instance ``token`` in every callback envelope. The
# check compares it with the ``uazapi_token`` stored for every row of the
# envelope's ``instanceName``. The stored token never leaves
# ``_load_uazapi_auth_candidates`` (the candidates carry booleans only), and
# neither token nor any derivative of one reaches a log, a metric label, a
# response or an exception.

# The closed ``UazapiWebhookAuthResult`` and ``UazapiWebhookAuthMode`` are the
# metric's vocabularies (``src/observability/metric_dimensions.py``): one
# definition, imported here.
# The 401 body of an enforced refusal (the same for every failed result).
UAZAPI_WEBHOOK_AUTH_REFUSAL: Final[Mapping[str, str]] = MappingProxyType(
    {"error": "invalid_webhook_auth"}
)
_DEFAULT_URL_PORTS: Final[Mapping[str, int]] = MappingProxyType(
    {"http": 80, "https": 443}
)


@dataclass(frozen=True)
class _UazapiAuthCandidate:
    """One ``clinic_configs`` row of the callback's instance, as compared.

    Only booleans: the stored token is compared inside the loader and
    dropped there.
    """

    tenant_id: UUID
    token_configured: bool
    token_matches: bool
    provider_matches: bool
    base_url_matches: bool


@dataclass(frozen=True)
class _UazapiAuth:
    """The check's outcome; ``matched`` is set only for ``match``."""

    result: UazapiWebhookAuthResult
    matched: Optional[_UazapiAuthCandidate]
    candidates: tuple[_UazapiAuthCandidate, ...]


def _uazapi_comparable_token(value: object) -> Optional[str]:
    """A token as compared: a ``str`` that is not empty after ``strip()``.

    Anything else (absent, ``None``, an ``int``, a ``dict``, blank) is no
    token, so ``compare_digest("", "")`` can never make two missing tokens
    equal. The send path strips the stored token too, and it is not stripped
    when saved.
    """

    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _uazapi_tokens_equal(presented: Optional[str], stored: Optional[str]) -> bool:
    """Constant-time equality of two comparable tokens; never raises.

    Compared as UTF-8 bytes: ``hmac.compare_digest`` raises ``TypeError`` for
    a non-ASCII ``str``. A string UTF-8 cannot encode (a lone surrogate the
    JSON decoder lets through) equals nothing.
    """

    if presented is None or stored is None:
        return False
    try:
        return hmac.compare_digest(
            presented.strip().encode("utf-8"), stored.strip().encode("utf-8")
        )
    except Exception:
        return False


def _normalised_base_url(value: object) -> Optional[tuple[str, str, str]]:
    """``(scheme, host[:port], path)`` with case, default port and trailing
    slash normalised; ``None`` when it is not an absolute URL. Never raises."""

    try:
        if not isinstance(value, str):
            return None
        parts = urlsplit(value.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port
        if not scheme or not host:
            return None
        if ":" in host:
            host = f"[{host}]"
        if port is not None and port != _DEFAULT_URL_PORTS.get(scheme):
            host = f"{host}:{port}"
        return scheme, host, parts.path.rstrip("/")
    except Exception:
        return None


def _uazapi_base_urls_match(presented: object, stored: object) -> bool:
    """The envelope ``BaseUrl`` names the stored ``uazapi_api_url``."""

    left = _normalised_base_url(presented)
    return left is not None and left == _normalised_base_url(stored)


def _uazapi_auth_candidate(
    *,
    tenant_id: UUID,
    stored_token: object,
    stored_api_url: object,
    whatsapp_provider: object,
    envelope_token: object,
    envelope_base_url: object,
) -> _UazapiAuthCandidate:
    """Compare one stored row with the envelope; pure, never raises."""

    stored = _uazapi_comparable_token(stored_token)
    return _UazapiAuthCandidate(
        tenant_id=tenant_id,
        token_configured=stored is not None,
        token_matches=_uazapi_tokens_equal(
            _uazapi_comparable_token(envelope_token), stored
        ),
        provider_matches=isinstance(whatsapp_provider, str)
        and whatsapp_provider == "uazapi",
        base_url_matches=_uazapi_base_urls_match(envelope_base_url, stored_api_url),
    )


def _uazapi_auth_result(
    candidates: tuple[_UazapiAuthCandidate, ...], *, token_present: bool
) -> _UazapiAuth:
    """The closed result over every row of the instance; pure.

    No row: ``unknown_instance``. Exactly one matching row: ``match``. Two or
    more: ``ambiguous_instance``. No matching row: ``token_missing`` without
    a usable envelope token, else ``token_not_configured`` when no row has a
    stored token, else ``mismatch``.
    """

    if not candidates:
        return _UazapiAuth("unknown_instance", None, candidates)
    matching = [candidate for candidate in candidates if candidate.token_matches]
    if len(matching) == 1:
        return _UazapiAuth("match", matching[0], candidates)
    if matching:
        return _UazapiAuth("ambiguous_instance", None, candidates)
    if not token_present:
        return _UazapiAuth("token_missing", None, candidates)
    if not any(candidate.token_configured for candidate in candidates):
        return _UazapiAuth("token_not_configured", None, candidates)
    return _UazapiAuth("mismatch", None, candidates)


async def _load_uazapi_auth_candidates(
    instance_name: str,
    *,
    envelope_token: object,
    envelope_base_url: object,
    db: AsyncSession,
) -> tuple[_UazapiAuthCandidate, ...]:
    """Every row whose ``uazapi_instance_name`` is ``instance_name``, in one
    statement, each compared with the envelope (Uazapi only).

    Reads only ``tenant_id``, ``uazapi_token``, ``uazapi_api_url`` and
    ``whatsapp_provider``; the stored token is dropped once compared.
    """

    credentials = ClinicConfigTable.api_credentials
    rows = (
        await db.execute(
            select(
                ClinicConfigTable.tenant_id,
                credentials["uazapi_token"],
                credentials["uazapi_api_url"],
                credentials["whatsapp_provider"],
            )
            .where(credentials["uazapi_instance_name"].astext == instance_name)
            .order_by(ClinicConfigTable.tenant_id)
        )
    ).all()
    return tuple(
        _uazapi_auth_candidate(
            tenant_id=row[0],
            stored_token=row[1],
            stored_api_url=row[2],
            whatsapp_provider=row[3],
            envelope_token=envelope_token,
            envelope_base_url=envelope_base_url,
        )
        for row in rows
    )


# The longest instance name rendered in a log line (the rest is cut).
_UAZAPI_LOG_INSTANCE_LIMIT: Final = 128


def _loggable_instance(instance: str) -> str:
    """A body-controlled ``instanceName`` as one inert log token; never raises.

    Percent-encoded with nothing kept but the unreserved characters, so a
    newline, a space or an ``=`` cannot start a new line or forge a field;
    an ordinary name (``my-instance_1``) renders unchanged. Cut at
    ``_UAZAPI_LOG_INSTANCE_LIMIT`` characters (``...[truncated]`` marks the
    cut: ``[`` and ``]`` are always encoded in the name itself).
    """

    try:
        head = instance[:_UAZAPI_LOG_INSTANCE_LIMIT]
        rendered = quote(head, safe="", errors="backslashreplace")
        if len(instance) > _UAZAPI_LOG_INSTANCE_LIMIT:
            rendered += "...[truncated]"
        return rendered
    except Exception:
        return "[unloggable]"


def _record_uazapi_webhook_auth(
    auth: _UazapiAuth,
    *,
    mode: UazapiWebhookAuthMode,
    instance: str,
    routed_tenant_id: Optional[UUID],
    token_present: bool,
    owner_present: bool,
    from_me: bool,
) -> None:
    """Count and log one check (``uazapi_webhook_auth``); never raises.

    The row the per-row fields describe is the matching one, else the row
    the request was routed to, else the instance's only row. The line is
    rendered from ``fields`` by name, in its order; the only body-controlled
    value, ``instance``, is rendered through ``_loggable_instance``.
    """

    try:
        routed = next(
            (c for c in auth.candidates if c.tenant_id == routed_tenant_id), None
        )
        if auth.matched is not None:
            relevant: Optional[_UazapiAuthCandidate] = auth.matched
        elif routed is not None:
            relevant = routed
        else:
            relevant = auth.candidates[0] if len(auth.candidates) == 1 else None
        fields = {
            "mode": mode,
            "result": auth.result,
            "instance": instance,
            "token_present": token_present,
            "routed_matches": auth.matched is not None
            and routed_tenant_id == auth.matched.tenant_id,
            "provider_matches": relevant is not None and relevant.provider_matches,
            "owner_present": owner_present,
            "from_me": from_me,
            "base_url_matches": relevant is not None and relevant.base_url_matches,
        }
    except Exception:
        logger.error("uazapi_webhook_auth dropped: unreadable outcome")
        return
    with suppress(Exception):
        UAZAPI_WEBHOOK_AUTH_TOTAL.labels(mode=mode, result=auth.result).inc()
    refused = mode == "enforce" and auth.result != "match"
    with suppress(Exception):
        rendered = {**fields, "instance": _loggable_instance(instance)}
        (logger.warning if refused or auth.result == "check_error" else logger.info)(
            " ".join(
                ["uazapi_webhook_auth", *(f"{name}=%({name})s" for name in fields)]
            ),
            rendered,
            extra={"event": "uazapi_webhook_auth", **fields},
        )


# ── Uazapi webhook endpoint ─────────────────────────────────────────


@router.post(
    "/uazapi",
    summary="Uazapi Webhook",
    description=(
        "Receives incoming WhatsApp messages from a self-hosted Uazapi "
        "(wuzapi) gateway and responds automatically. The envelope ``token`` "
        "is checked against the instance token stored for the payload's "
        "``instanceName``: with ``uazapi_webhook_auth_enforced`` on, anything "
        "but exactly one matching tenant is 401 and a match routes to that "
        "tenant; otherwise the check only measures and the tenant is resolved "
        "by ``uazapi_instance_name`` as before."
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

    The callback envelope carries the instance ``token`` (uazapi 2.6.0). It
    is compared with the token stored for every tenant of ``instanceName``
    after the minimal parsing below and before the replay claim, so a
    refused callback claims, creates and stores nothing. Enforced, anything
    but exactly one matching tenant answers 401 ``invalid_webhook_auth``, and
    a match routes to that tenant as a ``production`` turn whose credit
    principal is authenticated (``inbound_verified``). Otherwise the check
    only measures (``uazapi_webhook_auth``): the callback is routed by
    instance name and is ``unknown``, as it always was.

    Credits (debito-cortesia-automatico Group 3): a builder under its
    courtesy rule is charged here through the Z-API/Meta path -- verified or
    not -- and a blocked one answered ``credit_suspended`` with nothing sent
    (``credit_metering.capture_uazapi`` / ``precheck_uazapi``); under the
    global, a mark or nothing, and with ``uazapi_egress_enabled`` closed,
    the turn stays observational.  Every patient message reads the mode
    once, before the AI-off and pause gates (so a failed read never undoes
    the pause gate's lazy unpause); the decision waits for the pre-check
    after them.  The Uazapi route sends no read receipt.

    Expected payload (text message, commercial schema):
    {
      "instanceName": "my-instance",
      "token": "<instance token>",
      "BaseUrl": "https://my-instance.uazapi.com",
      "owner": "5511900000000",
      "message": {
        "sender_pn": "5511999999999@s.whatsapp.net",
        "text": "Olá",
        "type": "text",
        "messageid": "3EB0..."
      },
      "chat": {"wa_chatid": "5511999999999@s.whatsapp.net"}
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
        _loggable_instance(instance_name),
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
    # Patient files (whatsapp-image-understanding, Decision 3): an image,
    # document or video from the patient takes the file path at the insertion
    # point below. Staff media, audio and stickers keep their own paths.
    patient_file_kind = None if is_from_me or is_audio_media else _uazapi_file_kind(msg)

    # ── inbound authentication: the envelope token (uazapi-webhook-auth) ──
    # The mode is read ONCE, here: before the instance query and outside any
    # transaction (nothing ran on ``db`` yet). That one reading decides the
    # logged ``mode``, the 401, the origin and the credit principal. Unread
    # or failed, it is the shadow mode: measure only, serve as before.
    auth_enforced = await uazapi_webhook_auth_enforced()
    token_present = _uazapi_comparable_token(body.get("token")) is not None
    try:
        candidates = await _load_uazapi_auth_candidates(
            instance_name,
            envelope_token=body.get("token"),
            envelope_base_url=body.get("BaseUrl"),
            db=db,
        )
    except Exception as exc:
        if auth_enforced:
            # Enforced: fail closed. A 500 (a server fault, as Z-API's), before
            # the claim: nothing is routed or created.
            raise
        # Shadow never changes serving: the query was the session's first
        # statement, so rolling back leaves it as the resolver always found
        # it (a dead database fails the resolver, as it always did).
        with suppress(Exception):
            await db.rollback()
        logger.warning(
            "uazapi_webhook_auth check failed in shadow: %s", type(exc).__name__
        )
        auth = _UazapiAuth("check_error", None, ())
    else:
        auth = _uazapi_auth_result(candidates, token_present=token_present)
    tenant_id: Optional[UUID]
    if auth_enforced:
        # Enforced: only the one matching row is routed to.
        tenant_id = auth.matched.tenant_id if auth.matched is not None else None
    else:
        # Shadow: routed exactly as before, by instance name.
        tenant_id = await _resolve_tenant_by_credential(
            "uazapi_instance_name",
            instance_name,
            db,
        )
    owner = body.get("owner")
    _record_uazapi_webhook_auth(
        auth,
        mode="enforce" if auth_enforced else "shadow",
        instance=instance_name,
        routed_tenant_id=tenant_id,
        token_present=token_present,
        owner_present=isinstance(owner, str) and bool(owner.strip()),
        from_me=is_from_me,
    )
    if auth_enforced and auth.result != "match":
        # Before the claim, the session and any event: nothing is created.
        return JSONResponse(status_code=401, content=dict(UAZAPI_WEBHOOK_AUTH_REFUSAL))

    # ── no tenant: shadow routing found no row for the instance ──
    if not tenant_id:
        logger.warning(
            "Uazapi webhook: no tenant for instance %s",
            _loggable_instance(instance_name),
        )
        return {"ok": True, "skipped": "unknown_instance"}

    # Origin, classified once: an instance name is a lookup key, not a proof
    # of the sender's authenticity. Only an enforced match proves the caller
    # (the envelope token is the stored instance token): then the turn is
    # production and its credit principal authenticated; otherwise unknown.
    inbound_verified = auth_enforced and auth.result == "match"
    origin = (
        InteractionOrigin.PRODUCTION if inbound_verified else InteractionOrigin.UNKNOWN
    )

    # Media-gate carve-outs:
    #   * ``is_from_me and is_audio_media`` — staff voice note. Group 3 routes
    #     it through ``_handle_clinic_message`` so it can pause the session;
    #     the audio bytes are never downloaded or transcribed (real STT cost
    #     on the clinic's own voice, same rationale as Z-API).
    #   * ``is_audio_media and not is_from_me`` — patient voice note. Group 4
    #     transcribes it below (STT → multimodal → text reroute chain shared
    #     with Z-API and Meta). Without this carve-out patient audio would be
    #     silently dropped as ``non-text``.
    #   * ``patient_file_kind`` — a patient image, document or video
    #     (whatsapp-image-understanding). It is described or acknowledged at
    #     the insertion point after the LGPD notice; a caption-less file has
    #     no text, so the second gate lets it through as well.
    #
    # Any other media (sticker, staff image/document/video, unknown kinds)
    # still exits as ``non-text``.
    audio_is_transcribable = is_audio_media and not is_from_me
    if (
        is_media_message
        and not (is_from_me and is_audio_media)
        and not audio_is_transcribable
        and patient_file_kind is None
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
        and patient_file_kind is None
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
    # 31-day retention: claim-and-hold, not consume. Patient audio and a
    # patient image (Decision 9) claim later, after their download; a
    # document or video is claimed here.
    if (
        msg_id
        and not audio_is_transcribable
        and patient_file_kind != "image"
        and not _claim_incoming_message("uazapi", tenant_id, msg_id)
    ):
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

    def _claim_deferred_message() -> dict[str, Any] | None:
        """Claim patient audio or a patient image (both skip the top-level
        claim) only after any required provider download succeeds."""

        if msg_id and not _claim_incoming_message("uazapi", tenant_id, msg_id):
            return {"ok": True, "skipped": "duplicate_message"}
        return None

    # ── credit capture (debito-cortesia-automatico Group 3, DESIGN item 8) ──
    # Every patient message's effective enforcement is read ONCE, here: after
    # the fromMe/echo routing, the 401 and the early non-actionable exits,
    # before the AI-off and pause gates (as on Z-API) and so before the first
    # thing a builder BLOCKED by its courtesy rule must not do -- a patient
    # audio's download, STT and fallback; the image's egress gate, the
    # session, the LGPD notice, the image description and every send.  Before
    # the pause gate on purpose: a failed read rolls the session back, and
    # after that gate it would undo the lazy unpause of an expired pause
    # (flushed, not yet committed) while the turn is still answered.  The
    # decision is the pre-check's, on this capture (no second read).  Uazapi
    # is charged only under the rule (``rule_only``, Decision 18): under the
    # global, a mark or nothing the capture is main's path.  The rule's D1
    # answers 503 at once -- before a patient audio's own claim (the provider
    # retries it), before a paused session's text capture; for a text, after
    # its Redis claim (the message is lost, as on Z-API).  A global or mark
    # D1, if one were ever raised, is held and answered at the pre-check.
    early_capture: Optional[credit_metering.EarlyWebhookCapture] = None
    try:
        early_capture = await credit_metering.capture_uazapi(
            request,
            db,
            tenant_id=tenant_id,
            message=msg,
            inbound_verified=inbound_verified,
        )
    except CreditEnforcementUnavailable:
        return JSONResponse(
            status_code=503,
            content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
        )
    rule_blocked = early_capture is not None and early_capture.blocked

    # ── patient audio: transcribe via STT → multimodal → text reroute ──
    # Mirrors the Z-API audio block (webhook.py: ``_process_zapi_authenticated_body``)
    # gate order: STT-provider-unsupported → duration → capability →
    # download → chain → reroute on failure. This block only runs for
    # patient audio (``is_audio_media and not is_from_me``); staff voice
    # notes were already routed above and returned before this point.
    # A patient audio above ``audio_max_seconds`` is never downloaded or
    # transcribed: it is handed off at the insertion point below.
    audio_too_long = False
    # Set only when this message is a transcribed audio.
    stt_usage_metadata: dict[str, Any] = {}
    if audio_is_transcribable and rule_blocked:
        # Blocked by its courtesy rule (owner decision 11): never downloaded,
        # transcribed nor answered with a fallback.  Claimed where a patient
        # audio always is -- before the AI-off and pause gates (a paused
        # session keeps it, untranscribed: no text to capture) --, then
        # answered ``credit_suspended`` by the pre-check below.
        duplicate = _claim_deferred_message()
        if duplicate is not None:
            return duplicate
    elif audio_is_transcribable:
        creds = await _tenant_creds()
        u_url = (creds.get("uazapi_api_url") or "").strip()
        u_token = (creds.get("uazapi_token") or "").strip()
        stt_provider = _stt_canonical_provider(creds.get("stt_provider"))
        agent_lang = _get_agent_language(clinic_config)

        # STT-provider-unsupported gate: same skip envelope as Z-API/Meta.
        # This gate is deliberate — some providers have no runtime adapter
        # and must not attempt download / transcription / reroute.
        if stt_provider in UNSUPPORTED_STT_PROVIDERS:
            duplicate = _claim_deferred_message()
            if duplicate is not None:
                return duplicate
            return _stt_skip_envelope(stt_provider)

        # Duration gate: field name confirmed against the captured evidence
        # (media-payload.json: ``message.content.seconds`` is an int). This
        # runs above the capability gate so a 3-minute audio hands off
        # instead of getting "please type" on Uazapi — matching Z-API/Meta.
        # Only the decision is taken here: the handoff and its notice wait
        # below the AI-off and pause gates and the session, as on Meta.
        content_obj = msg.get("content") or {}
        audio_seconds = 0
        if isinstance(content_obj, dict):
            audio_seconds = int(content_obj.get("seconds") or 0)
        audio_max_seconds = int(
            creds.get("audio_max_seconds") or DEFAULT_AUDIO_MAX_SECONDS
        )
        if audio_seconds > audio_max_seconds:
            duplicate = _claim_deferred_message()
            if duplicate is not None:
                return duplicate
            audio_too_long = True
            logger.info(
                "Uazapi audio too long (%ds > %ds limit), skipping transcription for %s",
                audio_seconds,
                audio_max_seconds,
                from_phone,
            )
        else:
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
                except EgressDisabledError:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "ok": False,
                            "error": "uazapi_egress_disabled",
                            "retryable": True,
                        },
                    )
                except UazapiMediaDownloadError as exc:
                    duplicate = _claim_deferred_message()
                    if duplicate is not None:
                        return duplicate
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
                duplicate = _claim_deferred_message()
                if duplicate is not None:
                    return duplicate
                if downloaded_mime:
                    effective_mime = downloaded_mime
                logger.info(
                    "Uazapi audio received for transcription: size=%d bytes, mimetype=%s",
                    len(audio_bytes),
                    effective_mime,
                )
            else:
                duplicate = _claim_deferred_message()
                if duplicate is not None:
                    return duplicate

            transcript, chain_failure = await _transcribe_audio_chain(
                audio_bytes,
                creds,
                mime_type=effective_mime,
                agent_language=agent_lang,
                channel="Uazapi",
            )
            if chain_failure:
                try:
                    return await _send_uazapi_stt_fallback(
                        tenant_id=tenant_id,
                        phone=from_phone,
                        u_url=u_url,
                        u_token=u_token,
                        agent_language=agent_lang,
                        failure_reason=chain_failure,
                    )
                except EgressDisabledError:
                    return {
                        "ok": True,
                        "replied": False,
                        "action": "stt_fallback_suppressed",
                        "reason": "uazapi_egress_disabled",
                    }
            # Success: feed the transcript into the normal pipeline. The
            # load-bearing ``assert message_text is not None`` below is now
            # satisfied on the patient-audio path.
            message_text = transcript
            stt_usage_metadata = _stt_usage_metadata(creds, audio_seconds=audio_seconds)

    # ── bot_active check — if AI is paused, drop silently ──
    bot_skip = await _check_bot_active(tenant_id, from_phone, db)
    if bot_skip:
        await db.commit()
        return JSONResponse(status_code=202, content=bot_skip)

    # ── pause check for patient messages ──
    # A long audio was never transcribed: it has no text to capture.
    pause_result = await _check_pause_state(
        tenant_id,
        from_phone,
        db,
        message_text=None if audio_too_long else message_text,
        origin=origin,
    )
    if pause_result:
        await db.commit()
        return JSONResponse(status_code=202, content=pause_result)

    # ── credit pre-check (debito-cortesia-automatico Group 3, DESIGN item 8) ──
    # Right after the AI-off and pause gates (a paused session keeps its
    # text-only capture, blocked or not) and before the image's egress gate,
    # the session, the LGPD notice, the long-audio handoff, the file forward,
    # the image description and every send: decided on the capture above
    # (never read again).  ``blocked`` is ``credit_suspended`` -- no
    # session, no reply, no LGPD notice -- before ``uazapi_egress_enabled`` is
    # read (wish Decision 7); ``charged`` with the egress closed is main's
    # observational path (Decision 19); ``charged`` is enforced: the
    # PostgreSQL claim right before phase 1, on top of the Redis claim taken
    # where it always is.  Under the global, a mark or nothing: main's path.
    # A 503 after the Redis claim loses the message, as on Z-API and Meta.
    enforcement: Optional[WebhookEnforcement] = None
    try:
        verdict = await credit_metering.precheck_uazapi(
            request,
            db,
            tenant_id=tenant_id,
            message=msg,
            inbound_verified=inbound_verified,
            early=early_capture,
        )
    except CreditEnforcementUnavailable:
        return JSONResponse(
            status_code=503,
            content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
        )
    if verdict == "credit_pending":
        return dict(credit_metering.CREDIT_PENDING_SKIP)
    if verdict == "credit_suspended":
        return dict(credit_metering.CREDIT_SUSPENDED_SKIP)
    if isinstance(verdict, WebhookEnforcement):
        enforcement = verdict

    # ── patient image: egress gate before the session (Decision 9) ──
    # The image has no claim yet, so a closed egress answers the retryable
    # 503 here, before ``is_new`` is consumed: the redelivery still finds a
    # new session and sends its LGPD notice.
    if patient_file_kind == "image":
        try:
            await require_uazapi_egress()
        except EgressDisabledError:
            return _uazapi_egress_retry_response()

    # ── get or create a session for this phone ──
    session_id, is_new = await _get_or_create_session(tenant_id, from_phone, db)
    context = build_interaction_context(
        effective_tenant_id=tenant_id,
        session_id=session_id,
        origin=origin,
        event_type=EventType.MESSAGE_IN,
    )
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
    egress_suppressed = False
    if is_new:
        lgpd_cfg = (clinic_config or {}).get("lgpd") or {}
        if u_url and u_token:

            async def _send_uazapi_privacy(msg: str) -> dict[str, Any]:
                return await send_uazapi_text(u_url, u_token, from_phone, msg)

            try:
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
            except EgressDisabledError:
                egress_suppressed = True

    # ── patient files (whatsapp-image-understanding, Decisions 3, 4, 8-14) ──
    # Here, after the AI-off and pause gates, the session and the LGPD
    # notice: nothing below runs for a paused session or with the AI off.
    agent_lang = _get_agent_language(clinic_config)

    # ── long patient audio: hand off to the team, never transcribed ──
    # Same decision as Meta's and Z-API's ``audio_too_long``, metered like
    # the file forward below: charged under the courtesy rule (the
    # pre-check's enforcement: claim ``uazapi``, handoff and debit in one
    # transaction), else observational. No pipeline and no ``message.in``.
    # A pending file review is left as is.
    if audio_too_long:
        skipped = await _meta_file_handoff(
            tenant_id=tenant_id,
            session_id=session_id,
            from_phone=from_phone,
            note="audio_too_long",
            context=context,
            db=db,
            metering=(
                None
                if enforcement is not None
                else uazapi_ingress(
                    request,
                    message=msg,
                    context=context,
                    inbound_verified=inbound_verified,
                )
            ),
            enforcement=enforcement,
            clear_pending_review=False,
        )
        if skipped is not None:
            return skipped
        egress_open = await _send_uazapi_handoff_notice(
            session_id=session_id,
            phone=from_phone,
            u_url=u_url,
            u_token=u_token,
            message=(
                _AUDIO_TOO_LONG_MSG_EN if agent_lang == "en" else _AUDIO_TOO_LONG_MSG_PT
            ),
            label="long-audio notice",
            db=db,
        )
        if not egress_open:
            # The handoff is committed; only the notice was held back.
            return {
                "ok": True,
                "skipped": "audio_too_long",
                "reason": "uazapi_egress_disabled",
            }
        return {"ok": True, "skipped": "audio_too_long"}

    if patient_file_kind is None:
        # A textual message (plain text, or audio transcribed above) resolves
        # an active pending review. A file's caption never does: files never
        # take this branch. An expired or legacy flag counts as absent.
        if await _pending_file_review_active(session_id, db):
            if _detect_forward_intent(message_text or ""):
                # Charged under the courtesy rule like Z-API's and Meta's
                # (one transaction in ``run_meta_file_handoff``: claim
                # ``uazapi``, handoff, debit), else the observational writer.
                skipped = await _meta_file_handoff(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    from_phone=from_phone,
                    note="file_pending_review",
                    context=context,
                    db=db,
                    metering=(
                        None
                        if enforcement is not None
                        else uazapi_ingress(
                            request,
                            message=msg,
                            context=context,
                            inbound_verified=inbound_verified,
                        )
                    ),
                    enforcement=enforcement,
                    clear_pending_review=True,
                )
                if skipped is not None:
                    return skipped
                await _send_uazapi_file_forwarded(
                    session_id=session_id,
                    phone=from_phone,
                    u_url=u_url,
                    u_token=u_token,
                    agent_language=agent_lang,
                    db=db,
                )
                return {"ok": True, "replied": True, "action": "file_handoff"}
            # Not a forward: the patient is telling what they need.
            await _meta_clear_pending_file_review(session_id, db)
            await db.commit()
    else:
        image_text = ""
        if patient_file_kind == "image":
            image_text, answer = await _describe_uazapi_image(
                msg,
                creds,
                msg_id=msg_id,
                claim=_claim_deferred_message,
                agent_language=agent_lang,
            )
            if answer is not None:
                return answer
        if not image_text:
            return await _send_uazapi_file_ack(
                session_id=session_id,
                phone=from_phone,
                u_url=u_url,
                u_token=u_token,
                agent_language=agent_lang,
                file_kind=patient_file_kind,
                db=db,
            )
        # Described: the composed caption + description is the patient's
        # message (Decision 10). An active pending review stays as it is
        # (Decision 14); the next textual message resolves it.
        message_text = image_text

    assert message_text is not None
    event_request = CanonicalEventRequest(
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
                **stt_usage_metadata,
            },
        ),
    )

    media = _inline_media(request, _UAZAPI_MEDIA_PROVIDER)
    try:
        outcome = await _run_pipeline_transactionally(
            service=service,
            event_request=event_request,
            db=db,
            context=context,
            metering=(
                None
                if enforcement is not None
                else uazapi_ingress(
                    request,
                    message=msg,
                    context=context,
                    inbound_verified=inbound_verified,
                )
            ),
            enforcement=enforcement,
            reply=_ReplyTarget("uazapi", from_phone, audio_is_transcribable),
            media=media,
        )
    except CreditEnforcementUnavailable:
        # The charged claim found the tenant relinked since the pre-check:
        # nothing written; the pre-check's 503 (the message is lost: the
        # Redis replay claim is spent).
        return JSONResponse(
            status_code=503,
            content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
        )
    if isinstance(outcome, dict):
        # Charged under the courtesy rule: claimed, charged and its reply
        # sent from the outbox (``CreditReplySender``, Uazapi branch).
        return outcome
    response = outcome

    suggested = (
        response.prompt.suggested_response
        if response.prompt and response.prompt.suggested_response
        else None
    )

    if not suggested:
        logger.info("Uazapi webhook: no suggested_response for %s", from_phone)
        if egress_suppressed:
            return {
                "ok": True,
                "replied": False,
                "reason": "uazapi_egress_disabled",
            }
        return {"ok": True, "replied": False}

    if not u_url or not u_token:
        logger.warning("Uazapi credentials missing for tenant %s", tenant_id)
        return {"ok": True, "replied": False, "reason": "no_credentials"}

    try:
        send_result, sent_as_audio = await _send_reply_with_optional_tts(
            credentials=creds,
            text=suggested,
            language_code=response.response_language,
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
    except EgressDisabledError:
        return {
            "ok": True,
            "replied": False,
            "reason": "uazapi_egress_disabled",
        }
    except Exception:
        logger.exception("Uazapi send failed for %s", from_phone)
        return {"ok": True, "replied": False, "reason": "send_failed"}

    # ── the turn's file, after the text's 2xx (envio-midia-agente) ──
    await _send_inline_reply_media(
        media,
        response,
        db=db,
        credentials=creds,
        phone=from_phone,
        tenant_id=tenant_id,
        session_id=session_id,
    )
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

# Message types that take the file path. Documents, videos and stickers are
# never downloaded or analysed (risk of opining on a sensitive document like
# a medical exam without clinical context); they always get the "file
# received" acknowledgement. An ``image`` is described when the tenant's
# OpenRouter bundle is operational and, in every other outcome, gets that same
# acknowledgement (whatsapp-image-understanding, Decision 5).
_META_FILE_TYPES = frozenset({"image", "document", "video", "sticker"})

# Pre-download size ceiling for a Meta audio, ~16 KB/s (128 kbps). Meta's
# webhook + media resolve endpoint do not expose duration, so a file above
# ``audio_max_seconds`` times this is too long before spending network / STT
# budget on it. It is far above a WhatsApp voice note's Opus bitrate, so for
# voice notes it only stops the very long ones; the real duration read from
# the downloaded Ogg Opus decides the rest. For the formats without a measured
# duration (MP3, AAC, AMR, M4A) it remains the only check.
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

# The owner's file acknowledgement, identical on every channel
# (whatsapp-image-understanding, Decision 6). Never personalised.
_FILE_ACK_MSG_PT = (
    "Recebi seu arquivo! Mas, no momento, não consigo acessar o conteúdo por "
    "aqui.\n\nVocê poderia me contar um pouco mais sobre o arquivo e o que "
    "precisa? Assim, consigo entender melhor a situação e tentar te orientar."
    "\n\nSe preferir, também posso encaminhar seu caso para nossa equipe "
    "avaliar com mais detalhes."
)

_FILE_ACK_MSG_EN = (
    "I received your file! But right now I can't access its contents here."
    "\n\nCould you tell me a bit more about the file and what you need? That "
    "way I can better understand the situation and try to help.\n\nIf you "
    "prefer, I can also forward your case to our team to review in more "
    "detail."
)

# How long a ``pending_file_review`` stays active after the acknowledgement
# (Decision 4). Covers a burst of files and a patient who answers later;
# past it the flag counts as absent everywhere.
FILE_REVIEW_WINDOW: Final = timedelta(hours=2)

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


def _build_meta_file_ack_message(agent_language: str) -> str:
    """The fixed acknowledgement for a file the agent does not analyse.

    The owner's copy of Decision 6 in the agent's language, never
    personalised. Never analyses or opines about the file's content — offers
    two options: tell more about the file and the need, or forward the case
    to the human team.
    """
    return _FILE_ACK_MSG_EN if agent_language == "en" else _FILE_ACK_MSG_PT


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


def _utc(moment: Optional[datetime]) -> datetime:
    """``moment`` (default: now) as an aware UTC datetime; naive means UTC."""
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


async def _meta_set_pending_file_review(
    session_id: UUID,
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
) -> None:
    """Mark the session as awaiting a decision on a received file/attachment.

    Writes ``pending_file_review = true`` and ``pending_file_review_at`` (UTC
    ISO 8601, ``now`` by default) into ``session_metadata``, overwriting both
    (Decision 4): the review is active only inside
    :data:`FILE_REVIEW_WINDOW` from that timestamp.

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
    meta["pending_file_review_at"] = _utc(now).isoformat()
    sess.session_metadata = meta
    await db.flush()


def _pending_file_review_fresh(meta: Any, now: datetime) -> bool:
    """Decision 4's predicate over one ``session_metadata`` value.

    True only for ``pending_file_review is True`` with a timezone-aware ISO
    ``pending_file_review_at`` that is not in the future and younger than
    :data:`FILE_REVIEW_WINDOW`. A flag without timestamp (rows written before
    the window existed), any malformed value and an expired review are all
    absent.
    """
    if not isinstance(meta, Mapping) or meta.get("pending_file_review") is not True:
        return False
    raw = meta.get("pending_file_review_at")
    if not isinstance(raw, str):
        return False
    try:
        set_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if set_at.tzinfo is None:
        return False
    age = now - set_at
    return timedelta(0) <= age < FILE_REVIEW_WINDOW


async def _pending_file_review_active(
    session_id: UUID,
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Whether the session has an *active* pending file review (Decision 4).

    Read-only: an expired, legacy (no timestamp) or malformed flag is
    reported as absent and left untouched — the next
    ``_meta_set_pending_file_review`` overwrites it, the next resolution
    clears it.
    """
    from src.models.session import Session as SessionModel

    result = await db.execute(
        select(SessionModel.session_metadata).where(
            SessionModel.id == session_id,
        )
    )
    return _pending_file_review_fresh(result.scalars().first(), _utc(now))


async def _file_ack_needed(
    session_id: UUID,
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Whether a new file must receive the acknowledgement: no active review.

    While a review is active, a new file does not resend the acknowledgement
    on any channel (Decision 4).
    """
    return not await _pending_file_review_active(session_id, db, now=now)


async def _meta_clear_pending_file_review(
    session_id: UUID,
    db: AsyncSession,
) -> None:
    """Clear the pending file review: both ``pending_file_review`` keys."""
    from src.models.session import Session as SessionModel

    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    sess = result.scalars().first()
    if not sess:
        return
    meta = dict(sess.session_metadata or {})
    keys = ("pending_file_review", "pending_file_review_at")
    if any(key in meta for key in keys):
        for key in keys:
            meta.pop(key, None)
        sess.session_metadata = meta
        await db.flush()


async def _meta_initiate_file_handoff(
    tenant_id: UUID,
    session_id: UUID,
    from_phone: str,
    note: str,
    *,
    context: ResolvedInteractionContext,
    session: Optional[AsyncSession] = None,
) -> Any:
    """Initiate a human handoff signalling that a file/audio is pending review.

    PostgreSQL insertion is mandatory. Failures propagate to the route so it
    cannot clear state or acknowledge a handoff that does not exist.

    ``context`` is the Meta message's own interaction context (``production``
    after a valid HMAC), handed unchanged to ``initiate_handoff``, which
    refuses it before any effect unless it matches this tenant and session.

    ``session`` (credit Group 3b-2, the enforced file handoff): the route's
    open transaction; the record and ``handoff.request`` are written in it
    and the creation webhook waits for its commit (``initiate_handoff``'s
    caller-session mode, #145).  Without it the handoff commits on its own.
    """
    from src.handoff.models import (
        HandoffPendingMedia,
        HandoffReason,
        HandoffRequest,
        HandoffUrgency,
    )
    from src.services.handoff_service import get_handoff_service

    # What the queue card and the summary say the team must review.
    pending_media = {
        "audio_too_long": HandoffPendingMedia.LONG_AUDIO,
        "file_pending_review": HandoffPendingMedia.FILE,
    }.get(note)
    handoff_service = get_handoff_service()
    request = HandoffRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        reason=HandoffReason.USER_REQUEST,
        urgency=HandoffUrgency.NORMAL,
        pending_media=pending_media,
        key_info={
            "numero_whatsapp": from_phone,
            "phone": from_phone,
            "file_review_note": note,
        },
    )
    # Main's call stays exactly ``initiate_handoff(request, context=...)``;
    # only the enforced path adds the caller's session.
    caller_session: dict[str, AsyncSession] = (
        {} if session is None else {"session": session}
    )
    try:
        response = await handoff_service.initiate_handoff(
            request, context=context, **caller_session
        )
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


async def _pause_after_file_handoff(
    *,
    tenant_id: UUID,
    session_id: UUID,
    note: str,
    db: AsyncSession,
    now: Optional[datetime] = None,
) -> str:
    """Pause the session after a file handoff, like an AI handoff does.

    The clinic's "Stand-by após handoff" policy (``resolve_handoff_pause``)
    decides it, as in the pipeline and the human takeover: "Somente
    manualmente" pauses with no deadline, which is what the Fila de Inativos
    lists; a custom period or a disabled stand-by pauses until a deadline;
    ``handoff_active != "yes"`` does not pause.  The write goes through the
    single tenant-scoped CAS with a ``prior_episode_context`` built from the
    AI/person history only: the long audio was never transcribed and the
    forward request never became a ``message.in``.

    Writes in ``db`` and does not commit: the caller commits it with the
    handoff's transaction (enforced) or right after the handoff (otherwise).
    Returns the outcome for the log: ``paused``, ``handoff_inactive``,
    ``already_paused`` or ``not_found`` (seen before the CAS, or the CAS
    lost), or ``superseded_session``.  A session already paused keeps its
    pause and its deadline.
    """
    from src.models.session import Session as SessionModel

    current_time = now or datetime.now(timezone.utc)
    decision = resolve_handoff_pause(
        await _load_handoff_config(tenant_id, db), current_time
    )
    for notice in decision.notices:
        logger.warning(
            "handoff_pause_policy_tolerated",
            extra={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "field": notice.field,
                "reason": notice.reason,
            },
        )
    if not decision.should_pause:
        logger.info(
            "handoff_pause_skipped",
            extra={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "note": note,
            },
        )
        return "handoff_inactive"

    result = await db.execute(
        select(SessionModel).where(
            SessionModel.id == session_id,
            SessionModel.tenant_id == tenant_id,
        )
    )
    session_row = result.scalars().first()
    if session_row is None or session_row.is_paused:
        # No CAS ran: a session already paused (by a human or with its own
        # deadline) keeps its pause as it is.
        skipped = "not_found" if session_row is None else "already_paused"
        logger.info(
            "handoff_pause_skipped",
            extra={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "note": note,
                "outcome": skipped,
            },
        )
        return skipped

    events = await _load_ai_person_history(tenant_id, session_id, db, limit=20)
    cognitive = session_row.get_cognitive_state()
    cognitive.prior_episode_context = build_prior_episode_context(
        tenant_id=tenant_id,
        session_id=session_id,
        events=events,
        current_message=None,
    )
    outcome = await SessionRepository(db).pause_effective(
        tenant_id,
        session_id,
        cognitive_state=cognitive.to_dict(),
        paused_until=decision.paused_until,
    )

    if outcome == "paused":
        await db.refresh(
            session_row,
            attribute_names=["cognitive_state", "is_paused", "paused_until"],
        )
        logger.info(
            "session_paused",
            extra={
                "event": "session_paused",
                "reason": "file_handoff",
                "note": note,
                "session_id": str(session_id),
                "tenant_id": str(tenant_id),
                "paused_until": (
                    decision.paused_until.isoformat() if decision.paused_until else None
                ),
            },
        )
    else:
        logger.info(
            "session_pause_cas_non_winner",
            extra={
                "session_id": str(session_id),
                "tenant_id": str(tenant_id),
                "field": "cognitive_state",
                "outcome": outcome,
                "note": note,
            },
        )
    return outcome


async def _meta_file_handoff(
    *,
    tenant_id: UUID,
    session_id: UUID,
    from_phone: str,
    note: str,
    context: ResolvedInteractionContext,
    db: AsyncSession,
    metering: Optional[ObservationalIngress],
    enforcement: Optional[WebhookEnforcement],
    clear_pending_review: bool,
) -> Optional[dict[str, Any] | Response]:
    """The file handoff decision of every call site: forward-on-request and
    ``audio_too_long``, on Meta, Z-API and Uazapi.  ``None``: the handoff is
    committed, the route sends the patient's text as today.  A dict: the 200 body to answer with,
    nothing handed off and nothing sent.

    Every committed file handoff also pauses the session by the clinic's
    stand-by policy (``_pause_after_file_handoff``), as an AI handoff does,
    so the AI stops answering and a pause with no deadline lands on the Fila
    de Inativos.  Nothing is paused when no handoff is committed.

    Without ``enforcement``, main's sequence (the handoff commits on its own;
    then the pause, and the forward clears the pending review, commit
    together -- before the Group 3a writer, which rolls back whatever the
    route's transaction still holds).  The handoff is already committed
    there, so a failed pause is logged and dropped: the patient still gets
    the notice and the AI keeps answering, as before the pause existed.
    With it (credit Group 3b-2, O2), ONE transaction in ``db``
    (``run_meta_file_handoff``): claim, handoff, debit, the
    ``credit_effect_id`` link and the pause commit together -- a hold answers
    ``credit_pending``, a redelivery ``duplicate_message``, neither pauses --
    and the creation webhook is announced only after that transaction ends.
    """
    if enforcement is None:
        file_handoff = await _meta_initiate_file_handoff(
            tenant_id, session_id, from_phone, note, context=context
        )
        try:
            await _pause_after_file_handoff(
                tenant_id=tenant_id, session_id=session_id, note=note, db=db
            )
        except Exception:
            logger.exception(
                "File handoff pause failed tenant=%s session=%s note=%s",
                tenant_id,
                session_id,
                note,
            )
            await db.rollback()
        if clear_pending_review:
            await _meta_clear_pending_file_review(session_id, db)
        await db.commit()
        await record_meta_file_handoff(metering, db, file_handoff, note=note)
        return None

    async def initiate() -> Any:
        return await _meta_initiate_file_handoff(
            tenant_id, session_id, from_phone, note, context=context, session=db
        )

    async def before_commit() -> None:
        await _pause_after_file_handoff(
            tenant_id=tenant_id, session_id=session_id, note=note, db=db
        )
        if clear_pending_review:
            await _meta_clear_pending_file_review(session_id, db)

    try:
        outcome = await credit_metering.run_meta_file_handoff(
            db,
            enforcement,
            session_id=session_id,
            note=note,
            initiate=initiate,
            before_commit=before_commit,
        )
    except CreditEnforcementUnavailable:
        # cobranca-credito-por-builder: the captured mark re-checked under the
        # account lock changed (or became unreadable) -- rolled back, nothing
        # written; answered like the pre-check's 503, which loses the message
        # (main's replay claim is spent).
        return JSONResponse(
            status_code=503,
            content={"error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
        )
    finally:
        # After the transaction's end only: a rolled-back record is dropped.
        try:
            await dispatch_committed_handoff_webhooks(db)
        except Exception as exc:
            record_handoff_post_commit_dispatch_failure(
                exc, owner="adapter", tenant_id=tenant_id, session_id=session_id
            )
    return None if outcome.answer is None else dict(outcome.answer)


async def _describe_meta_image(
    image: Mapping[str, Any],
    credentials: dict[str, Any],
    *,
    access_token: str,
    agent_language: str,
) -> str:
    """Resolve, download and describe one Meta patient image (Decisions 5, 9-12).

    Returns the Decision 10 message (``image.caption`` plus the bracketed
    description) when the image was described, and ``""`` when it goes down
    the file branch (Decision 5).

    Order: capability, ``resolve_meta_media``, the declared ``file_size`` /
    ``mime_type`` (the resolve's, else the webhook's ``image.mime_type``),
    ``safe_download_image`` with the tenant's Bearer token, then
    :func:`_describe_image_chain` on the effective MIME. Without capability
    nothing is resolved or downloaded and the chain itself reports
    ``no_capability``. The refusals decided here (no token or a failed
    resolve, a declared size or MIME off the limits, a failed download) emit
    their own ``image_description_attempt``, with the declared ``file_size``
    as ``size_bytes`` (``None`` when absent); after a download the chain
    emits the rest, so each image emits exactly one event.

    Nothing is claimed here (Decision 9): Meta's replay claim at the top of
    the route already covers the message. Logs carry the media id and a
    stable error code or exception type, never the token or the signed URL.
    """
    caption_value = image.get("caption")
    caption = caption_value if isinstance(caption_value, str) else None
    payload_mime = str(image.get("mime_type") or "")
    image_bytes: Optional[bytes] = None
    effective_mime = payload_mime

    if _multimodal_operational(credentials):
        media_id = str(image.get("id") or "").strip()
        declared_mime = payload_mime
        declared_size: Optional[int] = None
        media_url = ""
        if media_id and access_token:
            try:
                media_info = await resolve_meta_media(media_id, access_token)
            except Exception as exc:
                logger.warning(
                    "Meta image media resolve failed for %s — %s",
                    media_id,
                    type(exc).__name__,
                )
            else:
                media_url = str(media_info.get("url") or "")
                declared_mime = str(media_info.get("mime_type") or "") or payload_mime
                declared_size = _uazapi_declared_size(media_info.get("file_size"))
        refused: Optional[ImageDescriptionOutcome] = None
        normalized_mime = declared_mime.split(";", 1)[0].strip().lower()
        if not media_url:
            refused = "download_failed"
        elif declared_size is not None and declared_size > IMAGE_MAX_BYTES:
            refused = "too_large"
        elif normalized_mime and normalized_mime not in IMAGE_MIME_ALLOWLIST:
            refused = "unsupported_mime"
        else:
            try:
                download = await safe_download_image(
                    media_url,
                    max_bytes=IMAGE_MAX_BYTES,
                    headers={"Authorization": f"Bearer {access_token}"},
                    declared_mime=declared_mime or None,
                )
            except UnsafeEgressError as exc:
                logger.warning(
                    "Meta image download failed for %s code=%s", media_id, exc.code
                )
                refused = _image_download_outcome(exc.code)
            except Exception as exc:
                logger.warning(
                    "Meta image download failed for %s — %s",
                    media_id,
                    type(exc).__name__,
                )
                refused = "download_failed"
            else:
                image_bytes = download.content
                effective_mime = download.mime_type
        if refused is not None:
            _emit_image_description_attempt(
                channel="meta",
                outcome=refused,
                mime=declared_mime,
                size_bytes=declared_size,
                latency_ms=None,
            )
            return ""

    composed, _reason = await _describe_image_chain(
        image_bytes,
        credentials,
        mime_type=effective_mime,
        caption=caption,
        agent_language=agent_language,
        channel="meta",
    )
    return composed


async def _send_meta_file_ack(
    *,
    tenant_id: UUID,
    session_id: UUID,
    phone: str,
    meta_pnid: str,
    meta_token: str,
    agent_language: str,
    msg_type: str,
    send_privacy_notice: Callable[[], Awaitable[None]],
    db: AsyncSession,
) -> dict[str, Any]:
    """Meta's file branch: acknowledge a file the agent does not analyse.

    A document, video or sticker, or an image that was not described
    (Decision 5). Free: no claim and no debit; a handoff only ever comes from
    the forward-on-request path. No acknowledgement while a pending review is
    active (Decision 4). Otherwise the LGPD notice of a new session (once per
    request, ``send_privacy_notice``), then the owner's copy; its outbound id
    is tracked and stored like the AI reply's, and only then is the pending
    review written and committed (Decision 17): a failed send leaves nothing
    pending.
    """
    if not await _file_ack_needed(session_id, db):
        logger.info(
            "Meta file ack suppressed for %s (type=%s): review pending",
            phone,
            msg_type,
        )
        return {"ok": True, "replied": False, "action": "file_ack_suppressed"}
    if not meta_pnid or not meta_token:
        logger.warning("Meta credentials missing for file ack tenant %s", tenant_id)
        return {"ok": True, "replied": False, "reason": "no_credentials"}

    # The file path returns before the route's shared notice, so a file-first
    # session sends its LGPD notice here, ahead of the ack.
    await send_privacy_notice()

    try:
        send_result = await send_meta_cloud_text(
            meta_pnid,
            meta_token,
            phone,
            _build_meta_file_ack_message(agent_language),
        )
        _track_meta_sent_message(send_result)
        ack_mid = extract_meta_message_id(send_result)
        if ack_mid:
            await _store_ai_msg_id_to_db(session_id, ack_mid, db)
        await _meta_set_pending_file_review(session_id, db)
        await db.commit()
        logger.info("Meta file ack sent to %s (type=%s)", phone, msg_type)
    except Exception as exc:
        logger.exception("Meta file ack send failed for %s — %s", phone, exc)
        return {
            "ok": True,
            "replied": False,
            "reason": "send_failed",
            "error": str(exc),
        }
    return {"ok": True, "replied": True, "action": "file_ack"}


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
    # TenantIdentityMiddleware has already verified the exact raw bytes and
    # bound this request to the receiver's active tenant. The route consumes
    # only that canonical state; payload tenant fields are never authority.
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

    tenant_id = getattr(request.state, "canonical_tenant_id", None)
    tenant_creds = getattr(request.state, "meta_credentials", None)
    phone_number_id = getattr(request.state, "meta_phone_number_id", None)
    if (
        not isinstance(tenant_id, UUID)
        or not isinstance(tenant_creds, dict)
        or not isinstance(phone_number_id, str)
        or not phone_number_id
    ):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )
    # Origin, classified once, from the middleware's HMAC-bound tenant only.
    # A missing, inconsistent or foreign context is a 401, never ``unknown``.
    origin = _meta_authenticated_origin(
        getattr(request.state, "tenant", None), tenant_id
    )
    if origin is None:
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

    if (
        len(message_bearing_phone_ids) != 1
        or message_bearing_phone_ids[0] != phone_number_id
    ):
        # Zero or multiple message-bearing phone ids — fail closed with a
        # generic error so multi-tenant confusion cannot bleed downstream.
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_auth"},
        )

    # ── 3) messageId presence + replay claim (before any downstream effect) ──
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
            # D2: best-effort text extraction so a plain-text message received
            # during pause can still be captured with retention. Non-text types
            # (audio/image/document/video/sticker) are intentionally left
            # uncaptured here — resolving them requires a network round-trip
            # (media download/STT) that should not run against an already-
            # paused session; this is an accepted scope boundary, not a
            # regression (they were never captured before).
            _pause_check_text: Optional[str] = None
            if msg_type == "text":
                _pause_check_text = (msg.get("text") or {}).get(
                    "body", ""
                ).strip() or None
            pause_result = await _check_pause_state(
                tenant_id,
                from_phone,
                db,
                message_text=_pause_check_text,
                origin=origin,
            )
            if pause_result:
                await db.commit()
                return JSONResponse(status_code=202, content=pause_result)

            # ── credit pre-check (neurostack-credit-system Group 3b-2) ──
            # For every message type, right after the pause check and before
            # the session exists: under a hold (or for a standalone tenant)
            # once credits are enforced, a new patient gets no session row,
            # no LGPD notice, no file ack and no STT (it runs after the
            # session, below).
            try:
                verdict = await credit_metering.precheck_meta(
                    request, db, tenant_id=tenant_id, message=msg
                )
            except CreditEnforcementUnavailable:
                # D1 latch; in practice the message is dropped (main's replay
                # claim is spent: the provider's retry is a duplicate).
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE
                    },
                )
            if verdict == "credit_pending":
                return dict(credit_metering.CREDIT_PENDING_SKIP)
            if verdict == "credit_suspended":
                # Group 5: the builder is suspended -- no session, no reply.
                return dict(credit_metering.CREDIT_SUSPENDED_SKIP)
            enforcement = verdict if isinstance(verdict, WebhookEnforcement) else None

            # ── get or create a session for this phone ──
            session_id, is_new = await _get_or_create_session(tenant_id, from_phone, db)
            # One context per message: the file handoff and the pipeline below
            # both receive this very object.
            context = build_interaction_context(
                effective_tenant_id=tenant_id,
                session_id=session_id,
                origin=origin,
                event_type=EventType.MESSAGE_IN,
            )
            await db.commit()
            # Observational metering of this message's decision (credit
            # Group 3a): ``None`` unless the switch was captured on, and once
            # the message is enforced (3b-2) the enforced path decides.
            metering = (
                None
                if enforcement is not None
                else meta_ingress(request, message=msg, context=context)
            )

            # ── load clinic config + credentials ──
            service = get_event_service()
            clinic_config = await service._load_clinic_config(tenant_id)

            creds = await _load_tenant_credentials(tenant_id, db)
            meta_pnid = (creds.get("meta_phone_number_id") or "").strip()
            meta_token = (creds.get("meta_permanent_token") or "").strip()
            agent_lang = _get_agent_language(clinic_config)

            # LGPD privacy notice for a new session. Called by the text/audio
            # path right before the pipeline, by the file path right before
            # the fixed ack and by the image path right before the description,
            # so a patient whose first message is an attachment still receives
            # it on that first (and only ``is_new``) turn. ``privacy_sent``
            # makes it once per request whichever caller comes first: an image
            # sends it before its description, and then either the ack or the
            # pipeline would call it again (Decision 8).
            privacy_sent = False

            async def _send_meta_privacy_notice() -> None:
                nonlocal privacy_sent
                if privacy_sent or not (is_new and meta_pnid and meta_token):
                    return
                privacy_sent = True
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

            # ── dispatch by message type ──
            #
            # Group 4 adds two paths on top of the Group 2 text flow:
            #   • ``audio`` / ``voice`` — resolve media_id, cap by size before
            #     the download and by the real Ogg Opus duration after it to
            #     protect the STT budget, then transcribe via Whisper and
            #     fall through to the normal text pipeline.
            #   • ``image`` with the tenant's OpenRouter bundle operational
            #     (whatsapp-image-understanding, Decision 5) — resolved,
            #     checked against the declared size and MIME, downloaded and
            #     described; the caption plus the description falls through
            #     to the pipeline as the patient's message, billed like any
            #     turn. An active pending review stays untouched and the
            #     caption is never read as a forward request (Decision 14).
            #   • any image not described, ``document``, ``video``,
            #     ``sticker`` — never analysed (safety: we won't opine on a
            #     medical document without clinical context). Send the fixed
            #     acknowledgement, free (no claim, no debit), unless a review
            #     is already active (Decision 4), and flag the session so the
            #     next text reply can branch into a real handoff or a normal
            #     description.
            #   • ``unsupported`` — how Meta delivers a view-once photo, among
            #     other types — keeps the silent non-text skip (Decision 13).
            message_text: str = ""
            # Set only when this message is a transcribed audio.
            stt_usage_metadata: dict[str, Any] = {}

            if msg_type == "text":
                message_text = (msg.get("text") or {}).get("body", "").strip()
                if not message_text:
                    return {"ok": True, "skipped": "empty"}

                # If a previous file/attachment set an *active* pending review
                # (Decision 4), this text is the patient's choice: forward to
                # the team, or describe their need. Only text resolves it on
                # Meta (Decision 3); an expired or legacy flag counts as
                # absent, so the text continues the pipeline without forward
                # matching.
                if await _pending_file_review_active(session_id, db):
                    if _detect_forward_intent(message_text):
                        skipped = await _meta_file_handoff(
                            tenant_id=tenant_id,
                            session_id=session_id,
                            from_phone=from_phone,
                            note="file_pending_review",
                            context=context,
                            db=db,
                            metering=metering,
                            enforcement=enforcement,
                            clear_pending_review=True,
                        )
                        if skipped is not None:
                            return skipped
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

                # 2) Enforce audio_max_seconds. Meta's webhook carries no
                # duration, so a file_size above audio_max_seconds*16KB (the
                # resolve endpoint's) is too long before any download; below
                # it the audio is downloaded and an Ogg Opus (every WhatsApp
                # voice note) is measured by its real duration (step 3). Too
                # long is a handoff, never a 500.
                audio_max_seconds_value = (creds or {}).get("audio_max_seconds")
                if isinstance(audio_max_seconds_value, (str, int, float)):
                    audio_max_seconds = int(
                        audio_max_seconds_value or DEFAULT_AUDIO_MAX_SECONDS
                    )
                else:
                    audio_max_seconds = DEFAULT_AUDIO_MAX_SECONDS
                file_size_value = media_info.get("file_size")
                if isinstance(file_size_value, (str, int, float)):
                    file_size = int(file_size_value or 0)
                else:
                    file_size = 0
                max_bytes = audio_max_seconds * _META_AUDIO_APPROX_BYTES_PER_SECOND
                too_long_reason: Optional[str] = None
                if file_size and file_size > max_bytes:
                    too_long_reason = (
                        f"{file_size} bytes > ~{audio_max_seconds}s"
                        f"*{_META_AUDIO_APPROX_BYTES_PER_SECOND}B"
                    )
                else:
                    # 3) Download bytes — streaming with MIME allowlist + the
                    # same byte cap, then measure an Ogg Opus by its pages. A
                    # duration is compared in whole seconds, like the integer
                    # ``seconds`` Z-API and Uazapi report. Any other format
                    # (MP3, AAC, AMR, M4A) has no duration here and keeps the
                    # size check above alone.
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
                    audio_duration = ogg_opus_duration_seconds(audio_bytes)
                    if audio_duration is None:
                        # An Ogg that cannot be measured silently weakens the
                        # gate to the size check: make it visible.
                        if "ogg" in mime_type.lower():
                            logger.warning(
                                "Meta Ogg audio without a measurable duration "
                                "for %s tenant=%s — size check only",
                                media_id,
                                tenant_id,
                            )
                    elif int(audio_duration) > audio_max_seconds:
                        too_long_reason = (
                            f"{audio_duration:.1f}s > {audio_max_seconds}s"
                        )
                if too_long_reason is not None:
                    # A new session's LGPD notice goes out before the handoff,
                    # as on Z-API: this turn never reaches the pipeline that
                    # would otherwise send it.
                    await _send_meta_privacy_notice()
                    handoff_msg = (
                        _AUDIO_TOO_LONG_MSG_EN
                        if agent_lang == "en"
                        else _AUDIO_TOO_LONG_MSG_PT
                    )
                    skipped = await _meta_file_handoff(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        from_phone=from_phone,
                        note="audio_too_long",
                        context=context,
                        db=db,
                        metering=metering,
                        enforcement=enforcement,
                        clear_pending_review=False,
                    )
                    if skipped is not None:
                        return skipped
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
                        "Meta audio too long (%s) for %s — handoff",
                        too_long_reason,
                        from_phone,
                    )
                    return {"ok": True, "skipped": "audio_too_long"}

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
                # Formats without a measured duration record 0 seconds; the
                # usage cards count every audio as at least one second.
                stt_usage_metadata = _stt_usage_metadata(
                    creds,
                    audio_seconds=(
                        0 if audio_duration is None else int(audio_duration)
                    ),
                )

                # Falls through to the shared text pipeline below.

            elif msg_type in _META_FILE_TYPES:
                if msg_type == "image":
                    # Decision 8: a new session's LGPD notice goes out before
                    # the description, and so before the ack should the image
                    # fall back to the file branch.
                    await _send_meta_privacy_notice()
                    image_obj = msg.get("image")
                    message_text = await _describe_meta_image(
                        image_obj if isinstance(image_obj, Mapping) else {},
                        creds,
                        access_token=meta_token,
                        agent_language=agent_lang,
                    )
                if not message_text:
                    # Decision 5: every file that is not a described image
                    # gets the fixed acknowledgement, free, and the session
                    # awaits the patient's choice (describe vs forward).
                    return await _send_meta_file_ack(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        phone=from_phone,
                        meta_pnid=meta_pnid,
                        meta_token=meta_token,
                        agent_language=agent_lang,
                        msg_type=msg_type,
                        send_privacy_notice=_send_meta_privacy_notice,
                        db=db,
                    )
                # Described (Decision 10): the patient's message for the
                # pipeline below. An active pending review stays as it is
                # (Decision 14); the next text message resolves it.

            else:
                # Unknown message type (contacts, location, reactions…) —
                # keep behaviour compatible with Group 2's non-text skip.
                return {"ok": True, "skipped": "non-text"}

            # ── send LGPD privacy notice on new sessions ──
            await _send_meta_privacy_notice()

            # ── pipeline ──
            event_request = CanonicalEventRequest(
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
                        **stt_usage_metadata,
                    },
                ),
            )

            media = _inline_media(request, _META_MEDIA_PROVIDER)
            try:
                outcome = await _run_pipeline_transactionally(
                    service=service,
                    event_request=event_request,
                    db=db,
                    context=context,
                    metering=metering,
                    enforcement=enforcement,
                    reply=_ReplyTarget("meta", from_phone, input_was_audio),
                    media=media,
                )
            except CreditEnforcementUnavailable:
                # cobranca-credito-por-builder: the enforced claim found the
                # tenant relinked since the pre-check -- nothing written; the
                # pre-check's 503 (the message is lost, as there).
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": credit_metering.CREDIT_ENFORCEMENT_UNAVAILABLE_CODE
                    },
                )
            if isinstance(outcome, dict):
                # Enforced: claimed, charged and its reply sent from the outbox.
                return outcome
            response = outcome

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
                    language_code=response.response_language,
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

            # ── the turn's file, after the text's 2xx (envio-midia-agente) ──
            await _send_inline_reply_media(
                media,
                response,
                db=db,
                credentials=creds,
                phone=from_phone,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return {"ok": True, "replied": True}

    return {"ok": True, "skipped": "no_message"}
