"""Operational text-to-speech adapters for WhatsApp agent replies.

ElevenLabs and Google Cloud TTS are supported. Provider URLs are always sent
through the safe-egress boundary with exact host pinning; errors exposed to
callers are stable codes and never contain credentials or response bodies.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from src.services.safe_egress import enforce_content_length, safe_async_client

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
GOOGLE_TTS_API_BASE = "https://texttospeech.googleapis.com/v1"
DEFAULT_ELEVENLABS_MODEL = "eleven_multilingual_v2"
DEFAULT_GOOGLE_VOICE = "pt-BR-Neural2-A"

MAX_TTS_TEXT_CHARS = 4_000
MAX_TTS_AUDIO_BYTES = 8 * 1024 * 1024


class TtsSynthesisError(RuntimeError):
    """Secret-free synthesis failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SynthesizedAudio:
    """Audio returned by a TTS provider and ready for WhatsApp delivery."""

    content: bytes
    mime_type: str = "audio/mpeg"


def tts_configuration_complete(credentials: Mapping[str, Any]) -> bool:
    """Return whether the selected provider has every field needed at runtime."""

    provider = str(credentials.get("tts_provider") or "").strip()
    required = (
        credentials.get("tts_api_url"),
        credentials.get("tts_api_key"),
        credentials.get("tts_model_name"),
    )
    if provider not in {"elevenlabs", "google"} or not all(required):
        return False
    return provider != "elevenlabs" or bool(credentials.get("tts_voice_id"))


def _clean_text(text: str) -> str:
    value = text.strip() if isinstance(text, str) else ""
    if not value:
        raise TtsSynthesisError("empty_tts_text")
    if len(value) > MAX_TTS_TEXT_CHARS:
        raise TtsSynthesisError("tts_text_too_long")
    return value


def _validate_audio(content: bytes) -> SynthesizedAudio:
    if not content:
        raise TtsSynthesisError("empty_tts_audio")
    if len(content) > MAX_TTS_AUDIO_BYTES:
        raise TtsSynthesisError("tts_audio_too_large")
    return SynthesizedAudio(content=content)


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise TtsSynthesisError(f"tts_upstream_http_error_{status}") from None


async def _synthesize_elevenlabs(
    *,
    text: str,
    api_url: str,
    api_key: str,
    model_name: str,
    voice_id: str,
) -> SynthesizedAudio:
    api_base = api_url.rstrip("/")
    synthesis_base = (
        api_base
        if api_base.endswith("/text-to-speech")
        else f"{api_base}/text-to-speech"
    )
    target_url = (
        f"{synthesis_base}/{quote(voice_id, safe='')}"
        "?output_format=mp3_44100_128"
    )
    body = {"text": text, "model_id": model_name}
    async with safe_async_client(
        target_url,
        kind="tts_elevenlabs",
        timeout=httpx.Timeout(45.0, connect=10.0),
    ) as (client, validated):
        headers = {
            "xi-api-key": api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        response = await client.post(validated.url, headers=headers, json=body)
        _raise_for_status(response)
        try:
            enforce_content_length(
                response.headers.get("content-length"), max_bytes=MAX_TTS_AUDIO_BYTES
            )
        except ValueError as exc:
            raise TtsSynthesisError("tts_audio_too_large") from exc
        return _validate_audio(response.content)


async def _synthesize_google(
    *,
    text: str,
    api_url: str,
    api_key: str,
    voice_name: str,
    language_code: str,
) -> SynthesizedAudio:
    api_base = api_url.rstrip("/")
    target_url = (
        api_base if api_base.endswith("/text:synthesize") else f"{api_base}/text:synthesize"
    )
    body = {
        "input": {"text": text},
        "voice": {"languageCode": language_code, "name": voice_name},
        "audioConfig": {"audioEncoding": "MP3"},
    }
    async with safe_async_client(
        target_url,
        kind="tts_google",
        timeout=httpx.Timeout(45.0, connect=10.0),
    ) as (client, validated):
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        response = await client.post(validated.url, headers=headers, json=body)
        _raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TtsSynthesisError("invalid_tts_response") from exc

    encoded = payload.get("audioContent") if isinstance(payload, dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise TtsSynthesisError("empty_tts_audio")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TtsSynthesisError("invalid_tts_response") from exc
    return _validate_audio(content)


async def synthesize_speech(
    text: str,
    credentials: Mapping[str, Any],
    *,
    language_code: str = "pt-BR",
) -> SynthesizedAudio:
    """Synthesize one agent reply using the clinic's selected TTS provider."""

    if not tts_configuration_complete(credentials):
        raise TtsSynthesisError("tts_configuration_incomplete")

    clean_text = _clean_text(text)
    provider = str(credentials["tts_provider"]).strip()
    api_url = str(credentials["tts_api_url"]).strip()
    api_key = str(credentials["tts_api_key"])
    model_name = str(credentials["tts_model_name"]).strip()

    if provider == "elevenlabs":
        return await _synthesize_elevenlabs(
            text=clean_text,
            api_url=api_url,
            api_key=api_key,
            model_name=model_name,
            voice_id=str(credentials["tts_voice_id"]).strip(),
        )
    if provider == "google":
        return await _synthesize_google(
            text=clean_text,
            api_url=api_url,
            api_key=api_key,
            voice_name=model_name,
            language_code=language_code,
        )
    raise TtsSynthesisError("unsupported_tts_provider")


__all__ = [
    "DEFAULT_ELEVENLABS_MODEL",
    "DEFAULT_GOOGLE_VOICE",
    "ELEVENLABS_API_BASE",
    "GOOGLE_TTS_API_BASE",
    "MAX_TTS_AUDIO_BYTES",
    "MAX_TTS_TEXT_CHARS",
    "SynthesizedAudio",
    "TtsSynthesisError",
    "synthesize_speech",
    "tts_configuration_complete",
]
