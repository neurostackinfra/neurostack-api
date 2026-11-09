"""
NeuroStack — WhatsApp Integration Service
Sends messages back to WhatsApp via Z-API and Evolution API REST endpoints.

All outbound HTTP goes through :mod:`src.services.safe_egress`. Z-API pins
``api.z-api.io`` exactly; Evolution uses the custom-public policy (any
public HTTPS host with the DNS/IP filter).
"""

import base64
import logging
from collections.abc import Mapping
from typing import Final

import httpx

from src.services.media_send_body import (
    MEDIA_SEND_TIMEOUT,
    MediaCategory,
    base64_json_body,
    require_sendable_media,
    sanitize_transport_error,
)
from src.services.safe_egress import safe_async_client

logger = logging.getLogger(__name__)

ZAPI_BASE = "https://api.z-api.io"

# media_assets category -> (Z-API route suffix, body key holding the media).
_ZAPI_MEDIA_ROUTES: Final[Mapping[str, tuple[str, str]]] = {
    "image": ("send-image", "image"),
    "pdf": ("send-document/pdf", "document"),
    "audio": ("send-audio", "audio"),
    "video": ("send-video", "video"),
}

# media_assets category -> Evolution ``sendMedia`` ``mediatype``. Audio is
# absent on purpose: it goes through ``sendWhatsAppAudio``.
_EVOLUTION_MEDIATYPES: Final[Mapping[str, str]] = {
    "image": "image",
    "pdf": "document",
    "video": "video",
}


def _response_json_object(response: httpx.Response) -> dict[str, object]:
    """Decode and validate an upstream JSON object response."""
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise RuntimeError("invalid_upstream_response") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("invalid_upstream_response")

    result: dict[str, object] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise RuntimeError("invalid_upstream_response")
        result[key] = value
    return result


def _sanitize_http_status_error(exc: httpx.HTTPStatusError) -> RuntimeError:
    """Return a URL-free RuntimeError describing an upstream HTTP failure.

    ``httpx.HTTPStatusError.__str__`` includes the full request URL, which
    for Z-API embeds the capability token in the path segment
    (``/instances/{id}/token/{TOKEN}/...``). Callers that log or serialise
    the exception (structlog ``error=str(exc)``, JSON ``{"error": str(exc)}``)
    would otherwise leak that token. This helper builds a minimal message
    that carries only the upstream HTTP status.
    """

    status = exc.response.status_code if exc.response is not None else "unknown"
    return RuntimeError(f"upstream_http_error status={status}")


async def send_text_message(
    instance_id: str,
    token: str,
    phone: str,
    message: str,
    client_token: str = "",
) -> dict[str, object]:
    """Send a text message via Z-API.

    Args:
        instance_id: Z-API instance ID.
        token: Z-API instance token.
        phone: Recipient phone in E.164 (e.g. "5511999999999").
        message: Text body.
        client_token: Z-API client-token (security header).

    Returns:
        Z-API response dict.
    """
    url = f"{ZAPI_BASE}/instances/{instance_id}/token/{token}/send-text"
    payload = {"phone": phone, "message": message}

    async with safe_async_client(
        url, kind="zapi", timeout=httpx.Timeout(15.0, connect=5.0)
    ) as (client, validated):
        headers = {"Content-Type": "application/json", "Host": validated.sni_host}
        if client_token:
            headers["Client-Token"] = client_token
        resp = await client.post(validated.url, json=payload, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        data = _response_json_object(resp)
        logger.info("Z-API send_text to %s — status %s", phone, resp.status_code)
        return data


async def send_zapi_audio(
    instance_id: str,
    token: str,
    phone: str,
    audio_bytes: bytes,
    *,
    mime_type: str = "audio/mpeg",
    client_token: str = "",
) -> dict[str, object]:
    """Send synthesized audio as a WhatsApp voice message through Z-API."""

    url = f"{ZAPI_BASE}/instances/{instance_id}/token/{token}/send-audio"
    encoded = base64.b64encode(audio_bytes).decode("ascii")
    payload = {
        "phone": phone,
        "audio": f"data:{mime_type};base64,{encoded}",
        "waveform": True,
    }
    async with safe_async_client(
        url, kind="zapi", timeout=httpx.Timeout(30.0, connect=5.0)
    ) as (client, validated):
        headers = {"Content-Type": "application/json", "Host": validated.sni_host}
        if client_token:
            headers["Client-Token"] = client_token
        resp = await client.post(validated.url, json=payload, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        data = _response_json_object(resp)
        logger.info("Z-API send_audio to %s - status %s", phone, resp.status_code)
        return data


async def send_zapi_media(
    instance_id: str,
    token: str,
    phone: str,
    payload: bytes,
    *,
    category: MediaCategory,
    mime_type: str,
    filename: str,
    client_token: str = "",
) -> dict[str, object]:
    """Send a media asset through Z-API.

    Contract (checked 2026-11-07), index https://developer.z-api.io/llms.txt:
    - image: https://developer.z-api.io/en/message/send-message-image
      ``POST /instances/{id}/token/{token}/send-image`` ``{phone, image}``;
    - pdf: https://developer.z-api.io/en/message/send-message-document
      ``POST .../send-document/{extension}`` ``{phone, document, fileName}``;
    - audio: https://developer.z-api.io/en/message/send-message-audio
      ``POST .../send-audio`` ``{phone, audio}``; the page shows only
      ``audio/mpeg`` (docs scout, 2026-11-08), so OGG and M4A are not sent
      (``media_send_body.sendable_mime_types("zapi")``);
    - video: https://developer.z-api.io/en/message/send-message-video
      ``POST .../send-video`` ``{phone, video}``.
    Each media field takes a link or a base64 data URI
    (``data:<mime>;base64,...``). The response carries ``zaapId``,
    ``messageId`` and ``id``.

    Args:
        instance_id: Z-API instance ID.
        token: Z-API instance token (part of the URL path).
        phone: Recipient phone in E.164 without leading '+'.
        payload: The asset bytes.
        category: ``media_assets.category``: image, pdf, audio or video.
        mime_type: Canonical MIME type of the asset.
        filename: ``media_assets.original_filename`` (``fileName`` for PDFs).
        client_token: Z-API account security header, sent when set.

    Raises:
        ValueError: unsupported category/MIME pair or empty payload.
        RuntimeError: sanitized upstream failure (no URL, no token).
    """

    require_sendable_media("zapi", category, mime_type, payload)
    route, media_key = _ZAPI_MEDIA_ROUTES[category]
    fields: dict[str, object] = {"phone": phone}
    if category == "pdf":
        fields["fileName"] = filename
    body, content_length = base64_json_body(
        fields, media_key, payload, data_uri_mime=mime_type
    )

    url = f"{ZAPI_BASE}/instances/{instance_id}/token/{token}/{route}"
    async with safe_async_client(url, kind="zapi", timeout=MEDIA_SEND_TIMEOUT) as (
        client,
        validated,
    ):
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(content_length),
            "Host": validated.sni_host,
        }
        if client_token:
            headers["Client-Token"] = client_token
        try:
            resp = await client.post(validated.url, content=body, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        except httpx.RequestError as exc:
            raise sanitize_transport_error(exc) from None
        data = _response_json_object(resp)
        logger.info(
            "Z-API send_media to %s - route %s status %s",
            phone,
            route,
            resp.status_code,
        )
        return data


async def mark_conversation_unread(
    instance_id: str,
    token: str,
    phone: str,
    client_token: str = "",
) -> bool:
    """Mark a WhatsApp conversation as unread via Z-API.

    Fire-and-forget: logs errors but never raises.

    Args:
        instance_id: Z-API instance ID.
        token: Z-API instance token.
        phone: Phone number to mark unread.
        client_token: Z-API client-token (security header).

    Returns:
        True on success, False on failure.
    """
    url = f"{ZAPI_BASE}/instances/{instance_id}/token/{token}/modify-chat"
    payload = {"phone": phone, "action": "unread"}

    try:
        async with safe_async_client(
            url, kind="zapi", timeout=httpx.Timeout(5.0, connect=5.0)
        ) as (client, validated):
            headers = {"Content-Type": "application/json", "Host": validated.sni_host}
            if client_token:
                headers["Client-Token"] = client_token
            resp = await client.post(validated.url, json=payload, headers=headers)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise _sanitize_http_status_error(exc) from None
            logger.info(
                "zapi_mark_unread_success",
                extra={
                    "phone": phone,
                    "instance_id": instance_id,
                    "status": resp.status_code,
                },
            )
            return True
    except Exception as exc:
        logger.warning(
            "zapi_mark_unread_failed",
            extra={"phone": phone, "instance_id": instance_id, "error": str(exc)},
        )
        return False


async def mark_message_read(
    instance_id: str,
    token: str,
    phone: str,
    message_id: str = "",
    client_token: str = "",
) -> bool:
    """Mark a WhatsApp message as read (blue checkmarks) via Z-API.

    Fire-and-forget: logs errors but never raises.
    """
    url = f"{ZAPI_BASE}/instances/{instance_id}/token/{token}/read-message"
    payload = {"phone": phone}
    if message_id:
        payload["messageId"] = message_id

    try:
        async with safe_async_client(
            url, kind="zapi", timeout=httpx.Timeout(5.0, connect=5.0)
        ) as (client, validated):
            headers = {"Content-Type": "application/json", "Host": validated.sni_host}
            if client_token:
                headers["Client-Token"] = client_token
            resp = await client.post(validated.url, json=payload, headers=headers)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise _sanitize_http_status_error(exc) from None
            logger.info("zapi_mark_read_success", extra={"phone": phone})
            return True
    except Exception as exc:
        logger.warning(
            "zapi_mark_read_failed", extra={"phone": phone, "error": str(exc)}
        )
        return False


async def send_evolution_text(
    api_url: str,
    instance_name: str,
    api_key: str,
    phone: str,
    message: str,
) -> dict[str, object]:
    """Send a text message via Evolution API.

    Args:
        api_url: Evolution API base URL (e.g. "https://evo.example.com").
        instance_name: Evolution instance name.
        api_key: Evolution API key.
        phone: Recipient phone in E.164 (e.g. "5511999999999").
        message: Text body.

    Returns:
        Evolution API response dict.
    """
    url = f"{api_url.rstrip('/')}/message/sendText/{instance_name}"
    payload = {"number": phone, "text": message}

    async with safe_async_client(
        url, kind="evolution", timeout=httpx.Timeout(15.0, connect=5.0)
    ) as (client, validated):
        headers = {
            "apikey": api_key,
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        resp = await client.post(validated.url, json=payload, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        data = _response_json_object(resp)
        logger.info("Evolution send_text to %s — status %s", phone, resp.status_code)
        return data


async def send_evolution_audio(
    api_url: str,
    instance_name: str,
    api_key: str,
    phone: str,
    audio_bytes: bytes,
) -> dict[str, object]:
    """Send synthesized MP3 audio through an Evolution API instance."""

    url = f"{api_url.rstrip('/')}/message/sendWhatsAppAudio/{instance_name}"
    payload = {
        "number": phone,
        "audio": base64.b64encode(audio_bytes).decode("ascii"),
        "encoding": True,
    }
    async with safe_async_client(
        url, kind="evolution", timeout=httpx.Timeout(30.0, connect=5.0)
    ) as (client, validated):
        headers = {
            "apikey": api_key,
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        resp = await client.post(validated.url, json=payload, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        data = _response_json_object(resp)
        logger.info("Evolution send_audio to %s - status %s", phone, resp.status_code)
        return data


async def send_evolution_media(
    api_url: str,
    instance_name: str,
    api_key: str,
    phone: str,
    payload: bytes,
    *,
    category: MediaCategory,
    mime_type: str,
    filename: str,
) -> dict[str, object]:
    """Send a media asset through an Evolution API instance.

    Contract (checked 2026-11-07):
    - https://doc.evolution-api.com/v2/api-reference/message-controller/send-media
      and the v2 source (``src/api/dto/sendMessage.dto.ts``,
      ``src/validate/message.schema.ts``, ``sendMessage.controller.ts``):
      ``POST /message/sendMedia/{instance}`` with header ``apikey`` and JSON
      ``number``, ``mediatype`` (image|document|video|audio), ``mimetype``,
      ``media`` (URL or raw base64, no data URI) and ``fileName`` (required
      for a base64 document).
    - Audio goes through ``POST /message/sendWhatsAppAudio/{instance}`` with
      ``{number, audio: <raw base64>, encoding: true}``, the same shape
      :func:`send_evolution_audio` uses, so it arrives as a voice note.

    Args:
        api_url: Evolution API base URL.
        instance_name: Evolution instance name.
        api_key: Evolution API key.
        phone: Recipient phone in E.164 without leading '+'.
        payload: The asset bytes.
        category: ``media_assets.category``: image, pdf, audio or video.
        mime_type: Canonical MIME type of the asset.
        filename: ``media_assets.original_filename``.

    Returns:
        Evolution response dict (message id at ``key.id``).

    Raises:
        ValueError: unsupported category/MIME pair or empty payload.
        RuntimeError: sanitized upstream failure (no URL, no API key).
    """

    require_sendable_media("evolution", category, mime_type, payload)
    base = api_url.rstrip("/")
    fields: dict[str, object]
    if category == "audio":
        url = f"{base}/message/sendWhatsAppAudio/{instance_name}"
        fields = {"number": phone, "encoding": True}
        media_key = "audio"
    else:
        url = f"{base}/message/sendMedia/{instance_name}"
        fields = {
            "number": phone,
            "mediatype": _EVOLUTION_MEDIATYPES[category],
            "mimetype": mime_type,
            "fileName": filename,
        }
        media_key = "media"
    body, content_length = base64_json_body(fields, media_key, payload)

    async with safe_async_client(url, kind="evolution", timeout=MEDIA_SEND_TIMEOUT) as (
        client,
        validated,
    ):
        headers = {
            "apikey": api_key,
            "Content-Type": "application/json",
            "Content-Length": str(content_length),
            "Host": validated.sni_host,
        }
        try:
            resp = await client.post(validated.url, content=body, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        except httpx.RequestError as exc:
            raise sanitize_transport_error(exc) from None
        data = _response_json_object(resp)
        logger.info(
            "Evolution send_media to %s - category %s status %s",
            phone,
            category,
            resp.status_code,
        )
        return data
