"""
NeuroStack — Z-API WhatsApp Integration Service
Sends messages back to WhatsApp via Z-API REST endpoints.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ZAPI_BASE = "https://api.z-api.io"
SEND_TEXT_PATH = "/send-text"


async def send_text_message(
    instance_id: str,
    token: str,
    phone: str,
    message: str,
) -> dict:
    """Send a text message via Z-API.

    Args:
        instance_id: Z-API instance ID.
        token: Z-API instance token.
        phone: Recipient phone in E.164 (e.g. "5511999999999").
        message: Text body.

    Returns:
        Z-API response dict.
    """
    url = f"{ZAPI_BASE}/instances/{instance_id}/token/{token}/send-text"
    payload = {"phone": phone, "message": message}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        logger.info("Z-API send_text to %s — status %s", phone, resp.status_code)
        return data
