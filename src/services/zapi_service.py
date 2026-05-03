"""
NeuroStack — WhatsApp Integration Service
Sends messages back to WhatsApp via Z-API and Evolution API REST endpoints.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

ZAPI_BASE = "https://api.z-api.io"


async def send_text_message(
    instance_id: str,
    token: str,
    phone: str,
    message: str,
    client_token: str = "",
) -> dict:
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
    headers = {"Content-Type": "application/json"}
    if client_token:
        headers["Client-Token"] = client_token

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        logger.info("Z-API send_text to %s — status %s", phone, resp.status_code)
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
    headers = {"Content-Type": "application/json"}
    if client_token:
        headers["Client-Token"] = client_token

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info(
                "zapi_mark_unread_success",
                extra={"phone": phone, "instance_id": instance_id, "status": resp.status_code},
            )
            return True
    except Exception as exc:
        logger.warning(
            "zapi_mark_unread_failed",
            extra={"phone": phone, "instance_id": instance_id, "error": str(exc)},
        )
        return False


async def send_evolution_text(
    api_url: str,
    instance_name: str,
    api_key: str,
    phone: str,
    message: str,
) -> dict:
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
    headers = {"apikey": api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        logger.info("Evolution send_text to %s — status %s", phone, resp.status_code)
        return data
