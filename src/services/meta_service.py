"""
NeuroStack — Meta Cloud API (WhatsApp Business) Service
Sends messages back to WhatsApp via the Meta Graph API and resolves inbound
media (audio / documents / images) by ``media_id``.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Public Graph API base — pinned to v18.0 to match the payload shapes we handle
# in the webhook. Bump both here and in the webhook parser when upgrading.
META_GRAPH_BASE = "https://graph.facebook.com/v18.0"


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
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        logger.info(
            "Meta send_text to %s — status %s", to_phone, resp.status_code,
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
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def download_meta_media(url: str, access_token: str) -> bytes:
    """Download the raw bytes of a signed Meta media URL.

    The URL returned by ``resolve_meta_media`` is short-lived and still
    requires the same Bearer token — a bare GET without the header returns
    401. Callers are responsible for enforcing any size/duration cap
    *before* invoking this (audio) or *never* calling it (files/attachments
    are ack-only, we don't inspect their content).
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content
