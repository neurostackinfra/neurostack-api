"""
NeuroStack — Uazapi (wuzapi) WhatsApp Service
Sends messages back to WhatsApp via the Uazapi / wuzapi REST endpoint.

Uazapi is a self-hosted WhatsApp Web multi-device gateway (Go, based on
wuzapi). Authentication is done via a per-instance ``Token`` header and the
send-text endpoint follows the ``POST /chat/send/text`` shape with a
``{"Phone": ..., "Body": ...}`` payload.
"""

import logging

import httpx

logger = logging.getLogger(__name__)


async def send_uazapi_text(
    api_url: str,
    token: str,
    phone: str,
    message: str,
) -> dict:
    """Send a text message via Uazapi (wuzapi).

    Args:
        api_url: Uazapi base URL (e.g. ``https://uazapi.example.com``). Trailing
            slashes are stripped so self-hosted deployments with a path prefix
            (``https://host/api/v1``) work as expected.
        token: Uazapi per-instance token (sent in the ``Token`` header).
        phone: Recipient phone in E.164 without leading '+'
            (e.g. ``"5511999999999"``).
        message: Text body.

    Returns:
        Uazapi response dict. The outbound message id is typically returned as
        ``MessageID`` (wuzapi) or ``id`` — callers should probe both keys.
    """
    url = f"{api_url.rstrip('/')}/chat/send/text"
    payload = {"Phone": phone, "Body": message}
    headers = {"Token": token, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        logger.info(
            "Uazapi send_text to %s — status %s", phone, resp.status_code,
        )
        return data
