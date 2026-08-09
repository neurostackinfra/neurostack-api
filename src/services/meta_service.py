"""
NeuroStack — Meta Cloud API (WhatsApp Business) Service
Sends messages back to WhatsApp via the Meta Graph API and resolves inbound
media (audio / documents / images) by ``media_id``.

All outbound HTTP goes through :mod:`src.services.safe_egress`. Meta pins
``graph.facebook.com`` exactly, so any drift raises
:class:`~src.services.safe_egress.UnsafeEgressError` before the token
touches the wire.
"""

import logging
from typing import Optional

import httpx

from src.services.safe_egress import safe_async_client, safe_download_audio

logger = logging.getLogger(__name__)

# Public Graph API base — pinned to v18.0 to match the payload shapes we handle
# in the webhook. Bump both here and in the webhook parser when upgrading.
META_GRAPH_BASE = "https://graph.facebook.com/v18.0"


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
) -> dict:
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
        data = resp.json()
        logger.info(
            "Meta send_text to %s — status %s",
            to_phone,
            resp.status_code,
        )
        return data


def extract_meta_message_id(send_result: dict) -> Optional[str]:
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


async def resolve_meta_media(media_id: str, access_token: str) -> dict:
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
        return resp.json()


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
