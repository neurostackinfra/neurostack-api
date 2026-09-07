"""
NeuroStack — WhatsApp Integration Service
Sends messages back to WhatsApp via Z-API and Evolution API REST endpoints.

All outbound HTTP goes through :mod:`src.services.safe_egress`. Z-API pins
``api.z-api.io`` exactly; Evolution uses the custom-public policy (any
public HTTPS host with the DNS/IP filter).
"""

import base64
import logging

import httpx

from src.services.safe_egress import safe_async_client

logger = logging.getLogger(__name__)

ZAPI_BASE = "https://api.z-api.io"


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
