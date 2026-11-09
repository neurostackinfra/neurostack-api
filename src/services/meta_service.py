"""
NeuroStack — Meta Cloud API (WhatsApp Business) Service
Sends messages back to WhatsApp via the Meta Graph API and resolves inbound
media (audio / documents / images) by ``media_id``.

All outbound HTTP goes through :mod:`src.services.safe_egress`. Meta pins
``graph.facebook.com`` exactly, so any drift raises
:class:`~src.services.safe_egress.UnsafeEgressError` before the token
touches the wire.
"""

import io
import logging
from collections.abc import Mapping
from typing import Final, Optional

import httpx

from src.services.media_assets import MEDIA_FILE_EXTENSIONS
from src.services.media_send_body import (
    MEDIA_SEND_TIMEOUT,
    MediaCategory,
    require_sendable_media,
    sanitize_transport_error,
)
from src.services.safe_egress import safe_async_client, safe_download_audio

logger = logging.getLogger(__name__)

# Public Graph API base — pinned to v18.0 to match the payload shapes we handle
# in the webhook. Bump both here and in the webhook parser when upgrading.
META_GRAPH_BASE = "https://graph.facebook.com/v18.0"

# media_assets category -> Graph message type.
_META_MEDIA_TYPES: Final[Mapping[str, str]] = {
    "image": "image",
    "pdf": "document",
    "audio": "audio",
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

    ``httpx.HTTPStatusError.__str__`` embeds the full request URL, which
    for Meta signed media URLs carries short-lived capability tokens in
    the query string. Callers that log or serialise the exception would
    leak the token — this helper surfaces only the HTTP status.
    """

    status = exc.response.status_code if exc.response is not None else "unknown"
    return RuntimeError(f"upstream_http_error status={status}")


async def send_meta_cloud_text(
    phone_number_id: str,
    access_token: str,
    to_phone: str,
    message: str,
) -> dict[str, object]:
    """Send a text message via Meta Cloud API (WhatsApp Business).

    Args:
        phone_number_id: Meta Cloud API phone number ID.
        access_token: Meta permanent (or long-lived) access token.
        to_phone: Recipient phone in E.164 without leading '+'
            (e.g. "5511999999999").
        message: Text body.

    Returns:
        Graph API response dict. Meta returns the outbound message id at
        ``response["messages"][0]["id"]`` (`wamid.*`).
    """
    url = f"{META_GRAPH_BASE}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"body": message},
    }

    async with safe_async_client(
        url, kind="meta", timeout=httpx.Timeout(15.0, connect=5.0)
    ) as (client, validated):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        resp = await client.post(validated.url, json=payload, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        data = _response_json_object(resp)
        logger.info(
            "Meta send_text to %s — status %s",
            to_phone,
            resp.status_code,
        )
        return data


async def send_meta_cloud_audio(
    phone_number_id: str,
    access_token: str,
    to_phone: str,
    audio_bytes: bytes,
    *,
    mime_type: str = "audio/mpeg",
) -> dict[str, object]:
    """Upload synthesized audio and send it through Meta Cloud API."""

    upload_url = f"{META_GRAPH_BASE}/{phone_number_id}/media"
    async with safe_async_client(
        upload_url, kind="meta", timeout=httpx.Timeout(30.0, connect=5.0)
    ) as (client, validated):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Host": validated.sni_host,
        }
        response = await client.post(
            validated.url,
            headers=headers,
            data={"messaging_product": "whatsapp"},
            files={"file": ("reply.mp3", audio_bytes, mime_type)},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        upload_payload = _response_json_object(response)

    media_id = upload_payload.get("id")
    if not isinstance(media_id, str) or not media_id:
        raise RuntimeError("missing_uploaded_media_id")

    send_url = f"{META_GRAPH_BASE}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "audio",
        "audio": {"id": media_id},
    }
    async with safe_async_client(
        send_url, kind="meta", timeout=httpx.Timeout(15.0, connect=5.0)
    ) as (client, validated):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        response = await client.post(validated.url, headers=headers, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        data = _response_json_object(response)
        logger.info("Meta send_audio to %s - status %s", to_phone, response.status_code)
        return data


async def send_meta_cloud_media(
    phone_number_id: str,
    access_token: str,
    to_phone: str,
    payload: bytes,
    *,
    category: MediaCategory,
    mime_type: str,
    filename: str,
) -> dict[str, object]:
    """Upload a media asset and send it through Meta Cloud API.

    Contract (checked 2026-11-07):
    - Upload: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media
      ``POST /{phone_number_id}/media`` multipart with ``messaging_product``,
      ``type`` (the MIME type) and ``file``; returns ``{"id": <media_id>}``.
    - Send: https://developers.facebook.com/docs/whatsapp/cloud-api/messages/document-messages
      ``POST /{phone_number_id}/messages`` with ``type`` image|document|audio|video
      and ``{<type>: {"id": <media_id>}}``; documents also carry ``filename``
      (with extension, so WhatsApp shows the right icon).
    - Audio (https://developers.facebook.com/docs/whatsapp/cloud-api/messages/audio-messages,
      docs scout 2026-11-08): ``audio/ogg`` is OPUS with mono input only;
      ``audio/mp4`` (M4A) and ``audio/mpeg`` too. The upload file name's
      extension must match ``type`` or the upload fails with 131053, so the
      multipart name is ``media`` plus the MIME's extension; the author's
      ``filename`` only names the document in the message.

    The asset goes into the multipart body as a ``BytesIO`` view of the
    payload, so httpx reads and writes it in small chunks: a raw ``bytes``
    field would reach the transport as one 20 MB block, which TLS then copies
    whole. ``BytesIO`` over ``bytes`` shares the buffer, and the
    ``Content-Length`` is unchanged.

    Args:
        phone_number_id: Meta Cloud API phone number ID.
        access_token: Meta access token (Bearer).
        to_phone: Recipient phone in E.164 without leading '+'.
        payload: The asset bytes.
        category: ``media_assets.category``: image, pdf, audio or video.
        mime_type: Canonical MIME type of the asset.
        filename: ``media_assets.original_filename`` (the document's
            ``filename`` in the message).

    Returns:
        Graph API ``/messages`` response; the id is at ``messages[0].id``.

    Raises:
        ValueError: a category/MIME pair Meta does not send, an empty payload
            or a non-mono Ogg Opus (``require_sendable_media``), before any
            network call.
        RuntimeError: sanitized upstream failure (no URL, no token).
    """

    message_type = _meta_media_type(category, mime_type, payload)
    # Meta checks the upload name's extension against ``type`` (error 131053),
    # so it comes from the canonical MIME, never from the author's file name.
    upload_filename = f"media{MEDIA_FILE_EXTENSIONS[mime_type]}"

    upload_url = f"{META_GRAPH_BASE}/{phone_number_id}/media"
    async with safe_async_client(
        upload_url, kind="meta", timeout=MEDIA_SEND_TIMEOUT
    ) as (client, validated):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Host": validated.sni_host,
        }
        try:
            response = await client.post(
                validated.url,
                headers=headers,
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (upload_filename, io.BytesIO(payload), mime_type)},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        except httpx.RequestError as exc:
            raise sanitize_transport_error(exc) from None
        upload_payload = _response_json_object(response)

    media_id = upload_payload.get("id")
    if not isinstance(media_id, str) or not media_id:
        raise RuntimeError("missing_uploaded_media_id")

    media_object: dict[str, str] = {"id": media_id}
    if message_type == "document":
        media_object["filename"] = filename
    message = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": message_type,
        message_type: media_object,
    }
    send_url = f"{META_GRAPH_BASE}/{phone_number_id}/messages"
    async with safe_async_client(send_url, kind="meta", timeout=MEDIA_SEND_TIMEOUT) as (
        client,
        validated,
    ):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        try:
            response = await client.post(validated.url, headers=headers, json=message)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        except httpx.RequestError as exc:
            raise sanitize_transport_error(exc) from None
        data = _response_json_object(response)
        logger.info(
            "Meta send_media to %s - type %s status %s",
            to_phone,
            message_type,
            response.status_code,
        )
        return data


def _meta_media_type(category: str, mime_type: str, payload: bytes) -> str:
    """Map an asset category to its Graph message type, failing closed."""

    require_sendable_media("meta", category, mime_type, payload)
    return _META_MEDIA_TYPES[category]


def extract_meta_message_id(send_result: dict[str, object]) -> Optional[str]:
    """Return the outbound `wamid.*` from a Graph API send response.

    Meta wraps the id inside ``messages[0].id``. Returns ``None`` when the
    payload is malformed (never raises — callers use this in fire-and-forget
    tracking paths).
    """
    if not isinstance(send_result, dict):
        return None
    messages = send_result.get("messages") or []
    if not messages or not isinstance(messages, list):
        return None
    first = messages[0]
    if not isinstance(first, dict):
        return None
    mid = first.get("id")
    return mid if isinstance(mid, str) and mid else None


async def resolve_meta_media(media_id: str, access_token: str) -> dict[str, object]:
    """Resolve a Meta ``media_id`` to a signed download URL + metadata.

    Meta requires a two-hop dance for inbound media:
    1. ``GET /{media_id}`` returns a short-lived signed URL plus mime/size hints
    2. Download the bytes from that URL with the same Bearer token

    Returns the raw Graph API JSON (``url``, ``mime_type``, ``file_size``,
    ``sha256``…). Raises on network / auth errors so the caller can log the
    failure at the appropriate severity.
    """
    url = f"{META_GRAPH_BASE}/{media_id}"
    async with safe_async_client(
        url, kind="meta", timeout=httpx.Timeout(15.0, connect=5.0)
    ) as (client, validated):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Host": validated.sni_host,
        }
        resp = await client.get(validated.url, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        return _response_json_object(resp)


async def download_meta_media(url: str, access_token: str) -> bytes:
    """Download the raw bytes of a signed Meta media URL.

    The URL returned by ``resolve_meta_media`` is short-lived and still
    requires the same Bearer token — a bare GET without the header returns
    401. The safe egress transport also revalidates the URL and its DNS
    resolution before the bytes flow, so a redirect or private-network URL
    cannot exfiltrate the token.
    """
    # Meta's signed CDN URLs may resolve to `lookaside.fbsbx.com` or other
    # public Facebook hosts, which are outside the pinned ``graph.facebook.com``
    # allowlist but still require the same Bearer token. We validate them as
    # ``audio_url`` (custom-public HTTPS), which enforces the same DNS/IP
    # filter and disables redirects. ``safe_async_client`` runs
    # ``parse_public_https_url`` internally via ``validate_url``, so no
    # explicit pre-check is required here.
    async with safe_async_client(
        url, kind="audio_url", timeout=httpx.Timeout(30.0, connect=5.0)
    ) as (client, validated):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Host": validated.sni_host,
        }
        resp = await client.get(validated.url, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _sanitize_http_status_error(exc) from None
        return resp.content


async def download_meta_media_audio(
    url: str, access_token: str, *, max_bytes: int
) -> bytes:
    """Stream a Meta signed media URL through the safe audio downloader.

    Callers that treat the result as inbound WhatsApp audio should use this
    variant so the MIME allowlist and byte cap are enforced. The Bearer
    token is only attached after the URL passes the safe-egress checks and
    is never echoed to logs on failure (the downloader raises
    :class:`UnsafeEgressError` with a stable code instead).
    """
    result = await safe_download_audio(
        url,
        max_bytes=max_bytes,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return result.content
