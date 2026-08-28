"""
NeuroStack — Uazapi (wuzapi) WhatsApp Service
Sends messages back to WhatsApp via the Uazapi / wuzapi REST endpoint.

Uazapi is a self-hosted WhatsApp Web multi-device gateway (Go, based on
wuzapi). Authentication is done via a per-instance ``Token`` header and the
send-text endpoint follows the ``POST /chat/send/text`` shape with a
``{"Phone": ..., "Body": ...}`` payload.

All outbound HTTP flows through :mod:`src.services.safe_egress`. Uazapi is
self-hosted, so we use the custom-public HTTPS policy (arbitrary hostname
but public unicast IPs only, no redirects, timeout enforced).
"""

import base64
import binascii
import logging

import httpx

from src.services.safe_egress import safe_async_client

logger = logging.getLogger(__name__)


class UazapiMediaDownloadError(RuntimeError):
    """Raised when :func:`download_uazapi_media` cannot return usable bytes.

    Wraps the underlying cause so the webhook can degrade to the STT text
    reroute exactly the same way it does on network / HTTP / decode failures
    elsewhere in the chain.
    """


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
    url = f"{api_url.rstrip('/')}/send/text"
    payload = {"number": phone, "text": message}

    async with safe_async_client(
        url, kind="uazapi", timeout=httpx.Timeout(15.0, connect=5.0)
    ) as (client, validated):
        headers = {
            "Token": token,
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        resp = await client.post(validated.url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        logger.info(
            "Uazapi send_text to %s — status %s",
            phone,
            resp.status_code,
        )
        return data


async def send_uazapi_audio(
    api_url: str,
    token: str,
    phone: str,
    audio_bytes: bytes,
    *,
    mime_type: str = "audio/mpeg",
) -> dict:
    """Send synthesized audio as a push-to-talk message through Uazapi."""

    url = f"{api_url.rstrip('/')}/send/media"
    encoded = base64.b64encode(audio_bytes).decode("ascii")
    payload = {
        "number": phone,
        "type": "ptt",
        "file": f"data:{mime_type};base64,{encoded}",
        "mimetype": mime_type,
    }
    async with safe_async_client(
        url, kind="uazapi", timeout=httpx.Timeout(30.0, connect=5.0)
    ) as (client, validated):
        headers = {
            "Token": token,
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        resp = await client.post(validated.url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        logger.info("Uazapi send_audio to %s - status %s", phone, resp.status_code)
        return data


async def download_uazapi_media(
    api_url: str,
    token: str,
    message_id: str,
    *,
    generate_mp3: bool = False,
) -> tuple[bytes, str]:
    """Fetch decrypted media bytes for a Uazapi message id.

    Uazapi's inbound payload references media by a WhatsApp signed URL that
    resolves to *raw WhatsApp-encrypted bytes* (HKDF-SHA256 + AES-256-CBC +
    HMAC). We do not implement that client-side decryption; Uazapi exposes a
    server-side endpoint that returns the plaintext instead.

    Endpoint: ``POST {api_url}/message/download`` with header ``Token: <t>``
    and body ``{"id": <messageid>, "return_base64": true,
    "return_link": false, "generate_mp3": <flag>}``. The successful response
    carries ``base64Data`` (plaintext bytes) and ``mimetype`` (verbatim from
    WhatsApp).

    ``generate_mp3`` defaults to ``False`` on purpose: the multimodal STT
    rung in :func:`_multimodal_transcribe_audio` passes WhatsApp's OGG/Opus
    through untouched (there is no transcoding step anywhere in the chain),
    and asking Uazapi to hand back MP3 would silently break that assumption.

    Args:
        api_url: Uazapi base URL (trailing slashes tolerated).
        token: Uazapi per-instance token.
        message_id: The pure Uazapi ``messageid`` (not the composite
            ``owner:MSGID`` form).
        generate_mp3: When ``True``, ask Uazapi to transcode audio to MP3.
            Default ``False`` keeps the native OGG/Opus.

    Returns:
        A ``(bytes, mimetype)`` tuple. ``mimetype`` is Uazapi's verbatim
        value (typically ``"audio/ogg; codecs=opus"`` for a voice note).

    Raises:
        UazapiMediaDownloadError: for missing arguments, HTTP failures,
            missing ``base64Data``, or base64 decode errors. The webhook
            catches this and reroutes to the text fallback.
    """
    if not api_url or not token or not message_id:
        raise UazapiMediaDownloadError("missing_arguments")

    url = f"{api_url.rstrip('/')}/message/download"
    payload = {
        "id": message_id,
        "return_base64": True,
        "return_link": False,
        "generate_mp3": generate_mp3,
    }

    try:
        async with safe_async_client(
            url, kind="uazapi", timeout=httpx.Timeout(30.0, connect=5.0)
        ) as (client, validated):
            headers = {
                "Token": token,
                "Content-Type": "application/json",
                "Host": validated.sni_host,
            }
            resp = await client.post(validated.url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise UazapiMediaDownloadError(f"http_error: {exc}") from exc
    except ValueError as exc:  # non-JSON response body
        raise UazapiMediaDownloadError(f"invalid_json: {exc}") from exc

    if not isinstance(data, dict):
        raise UazapiMediaDownloadError("invalid_response_shape")

    b64_payload = data.get("base64Data") or data.get("base64data") or ""
    mimetype = str(data.get("mimetype") or "").strip()
    if not b64_payload:
        raise UazapiMediaDownloadError("missing_base64")

    try:
        # Strip the optional ``data:...;base64,`` prefix Uazapi sometimes
        # returns so downstream STT gets pure bytes.
        if isinstance(b64_payload, str) and b64_payload.startswith("data:"):
            comma = b64_payload.find(",")
            if comma == -1:
                raise UazapiMediaDownloadError("invalid_data_url")
            b64_payload = b64_payload[comma + 1 :]
        raw = base64.b64decode(b64_payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise UazapiMediaDownloadError(f"base64_decode_failed: {exc}") from exc

    logger.info(
        "Uazapi download_media id=%s bytes=%d mimetype=%s",
        message_id,
        len(raw),
        mimetype or "unknown",
    )
    return raw, mimetype
