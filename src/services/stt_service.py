"""Private STT transport primitives for Scribe and OpenRouter.

The public dispatcher in this module intentionally has no production caller yet.
Provider activation, credential policy, and webhook routing are separate concerns;
this module only owns the fixed vendor transports and their closed error boundary.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from src.services.safe_egress import UnsafeEgressError, safe_async_client

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

MIME_EXTENSION_BY_TYPE: dict[str, str] = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/flac": "flac",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/webm": "webm",
    "audio/aac": "aac",
}

_MIME_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class SttTranscriptionError(RuntimeError):
    """Secret-free STT failure with a stable machine-readable ``code``."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalize_audio_mime(mimetype: str) -> tuple[str, str]:
    """Return the canonical media type and deterministic filename extension.

    Parameters are accepted only when every semicolon-delimited segment is a
    non-empty ``token=token`` pair. Valid parameters are deliberately discarded:
    vendors receive the canonical media type rather than caller-controlled
    multipart metadata.
    """

    if not isinstance(mimetype, str):
        raise SttTranscriptionError("unsupported_audio_mime")

    segments = mimetype.strip().split(";")
    media_type = segments[0].strip().lower()
    for raw_parameter in segments[1:]:
        parameter = raw_parameter.strip()
        if not parameter or parameter.count("=") != 1:
            raise SttTranscriptionError("unsupported_audio_mime")
        name, value = (part.strip() for part in parameter.split("=", 1))
        if not _MIME_TOKEN.fullmatch(name) or not _MIME_TOKEN.fullmatch(value):
            raise SttTranscriptionError("unsupported_audio_mime")

    extension = MIME_EXTENSION_BY_TYPE.get(media_type)
    if extension is None:
        raise SttTranscriptionError("unsupported_audio_mime")
    return media_type, extension


def _parse_transcript(response: httpx.Response) -> str:
    try:
        payload: Any = response.json()
    except ValueError:
        raise SttTranscriptionError("stt_malformed_response") from None

    if not isinstance(payload, dict):
        raise SttTranscriptionError("stt_malformed_response")
    raw_text = payload.get("text")
    if not isinstance(raw_text, str):
        raise SttTranscriptionError("stt_malformed_response")
    transcript = raw_text.strip()
    if not transcript:
        raise SttTranscriptionError("stt_empty_transcript")
    return transcript


async def _post_transcription(
    *,
    target_url: str,
    kind: str,
    headers: dict[str, str],
    data: dict[str, str],
    audio_bytes: bytes,
    canonical_mime: str,
    extension: str,
    resolver: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    try:
        async with safe_async_client(
            target_url,
            kind=kind,
            resolver=resolver,
            timeout=_TIMEOUT,
            transport=transport,
        ) as (client, validated):
            request_headers = dict(headers)
            request_headers["Host"] = validated.sni_host
            response = await client.post(
                validated.url,
                headers=request_headers,
                files={
                    "file": (
                        f"audio.{extension}",
                        audio_bytes,
                        canonical_mime,
                    )
                },
                data=data,
            )
            if not response.is_success:
                raise SttTranscriptionError("stt_provider_http_error")
            return _parse_transcript(response)
    except SttTranscriptionError:
        raise
    except UnsafeEgressError as exc:
        code = (
            "stt_provider_http_error"
            if exc.code == "redirect_forbidden"
            else "stt_provider_transport_error"
        )
        raise SttTranscriptionError(code) from None
    except httpx.HTTPError:
        raise SttTranscriptionError("stt_provider_transport_error") from None


async def _transcribe_openrouter(
    *,
    api_key: str,
    audio_bytes: bytes,
    canonical_mime: str,
    extension: str,
    model_name: str,
    language: str | None,
    resolver: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    data = {"model": model_name}
    if language is not None:
        data["language"] = language
    return await _post_transcription(
        target_url=f"{OPENROUTER_API_BASE}/audio/transcriptions",
        kind="stt_openrouter",
        headers={"Authorization": f"Bearer {api_key}"},
        data=data,
        audio_bytes=audio_bytes,
        canonical_mime=canonical_mime,
        extension=extension,
        resolver=resolver,
        transport=transport,
    )


async def _transcribe_elevenlabs(
    *,
    api_key: str,
    audio_bytes: bytes,
    canonical_mime: str,
    extension: str,
    model_name: str,
    resolver: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    return await _post_transcription(
        target_url=f"{ELEVENLABS_API_BASE}/speech-to-text",
        kind="stt_elevenlabs",
        headers={"xi-api-key": api_key},
        data={"model_id": model_name},
        audio_bytes=audio_bytes,
        canonical_mime=canonical_mime,
        extension=extension,
        resolver=resolver,
        transport=transport,
    )


async def transcribe_audio(
    *,
    provider: str,
    api_url: str,
    api_key: str,
    audio_bytes: bytes,
    mimetype: str,
    model_name: str,
    language: str | None = "pt",
    resolver: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Dispatch to a fixed Scribe/OpenRouter adapter.

    Both explicit provider bases are exact contracts. Any custom host, alternate
    path, explicit port, query, userinfo, fragment, or operation URL is rejected
    before MIME processing, DNS resolution, or HTTP client construction.
    """

    expected_base = {
        "elevenlabs_scribe": ELEVENLABS_API_BASE,
        "openrouter": OPENROUTER_API_BASE,
    }.get(provider)
    if expected_base is None:
        raise SttTranscriptionError("stt_provider_unsupported")
    if api_url != expected_base:
        raise SttTranscriptionError("invalid_provider_endpoint")

    canonical_mime, extension = normalize_audio_mime(mimetype)
    if provider == "openrouter":
        return await _transcribe_openrouter(
            api_key=api_key,
            audio_bytes=audio_bytes,
            canonical_mime=canonical_mime,
            extension=extension,
            model_name=model_name,
            language=language,
            resolver=resolver,
            transport=transport,
        )
    return await _transcribe_elevenlabs(
        api_key=api_key,
        audio_bytes=audio_bytes,
        canonical_mime=canonical_mime,
        extension=extension,
        model_name=model_name,
        resolver=resolver,
        transport=transport,
    )


__all__ = [
    "ELEVENLABS_API_BASE",
    "MIME_EXTENSION_BY_TYPE",
    "OPENROUTER_API_BASE",
    "SttTranscriptionError",
    "normalize_audio_mime",
    "transcribe_audio",
]
