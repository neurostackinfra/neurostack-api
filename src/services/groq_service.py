"""
NeuroStack - STT Transcription Service
OpenAI-compatible audio transcription client.
Supports Groq, OpenAI Whisper, and any other OpenAI-compatible STT endpoint.

All outbound HTTP flows through :mod:`src.services.safe_egress`. Provider
kinds are picked from the destination host: Groq/OpenAI use pinned host
allowlists, everything else uses the ``outros`` custom-public policy.
"""

import httpx

from src.services.safe_egress import (
    STT_GROQ_HOSTS,
    STT_OPENAI_HOSTS,
    UnsafeEgressError,
    parse_public_https_url,
    safe_async_client,
)

GROQ_API_BASE = "https://api.groq.com/openai/v1"
OPENAI_API_BASE = "https://api.openai.com/v1"


def _stt_kind_for_url(base_url: str) -> str:
    """Return the safe-egress kind label for a given STT base URL."""

    try:
        parsed = parse_public_https_url(base_url)
    except UnsafeEgressError:
        return "stt_outros"
    if parsed.hostname in STT_GROQ_HOSTS:
        return "stt_groq"
    if parsed.hostname in STT_OPENAI_HOSTS:
        return "stt_openai"
    return "stt_outros"


async def transcribe_audio(
    api_key: str,
    audio_bytes: bytes,
    mimetype: str,
    model: str = "whisper-large-v3-turbo",
    language: str | None = None,
    base_url: str | None = None,
) -> str:
    """Transcribe audio using an OpenAI-compatible STT API. Returns transcribed text.

    Args:
        api_key: Bearer token for the STT provider.
        audio_bytes: Raw audio file content.
        mimetype: MIME type of the audio (e.g. "audio/ogg").
        model: Model identifier (provider-specific).
        language: Optional ISO-639-1 language hint. Defaults to ``None`` so the
            provider autodetects — text and audio turns must feed the same
            PT/EN language-decision machine without any STT-side translation.
        base_url: Base URL for the STT endpoint. Defaults to Groq's API.
                  Examples: GROQ_API_BASE, OPENAI_API_BASE, or a custom URL.
    """
    api_base = (base_url or "").rstrip("/") or GROQ_API_BASE
    kind = _stt_kind_for_url(api_base)
    target_url = f"{api_base}/audio/transcriptions"
    async with safe_async_client(
        target_url,
        kind=kind,
        timeout=httpx.Timeout(60.0, connect=10.0),
    ) as (client, validated):
        files = {"file": ("audio.ogg", audio_bytes, mimetype)}
        data: dict[str, str] = {"model": model, "response_format": "text"}
        if language is not None:
            data["language"] = language
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Host": validated.sni_host,
        }
        resp = await client.post(
            validated.url,
            headers=headers,
            files=files,
            data=data,
        )
        resp.raise_for_status()
        return resp.text.strip()
