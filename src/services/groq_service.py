"""
NeuroStack - STT Transcription Service
OpenAI-compatible audio transcription client.
Supports Groq, OpenAI Whisper, and any other OpenAI-compatible STT endpoint.
"""

import httpx

GROQ_API_BASE = "https://api.groq.com/openai/v1"
OPENAI_API_BASE = "https://api.openai.com/v1"


async def transcribe_audio(
    api_key: str,
    audio_bytes: bytes,
    mimetype: str,
    model: str = "whisper-large-v3-turbo",
    language: str = "pt",
    base_url: str | None = None,
) -> str:
    """Transcribe audio using an OpenAI-compatible STT API. Returns transcribed text.

    Args:
        api_key: Bearer token for the STT provider.
        audio_bytes: Raw audio file content.
        mimetype: MIME type of the audio (e.g. "audio/ogg").
        model: Model identifier (provider-specific).
        language: ISO-639-1 language hint for the transcription.
        base_url: Base URL for the STT endpoint. Defaults to Groq's API.
                  Examples: GROQ_API_BASE, OPENAI_API_BASE, or a custom URL.
    """
    api_base = (base_url or "").rstrip("/") or GROQ_API_BASE
    async with httpx.AsyncClient(timeout=60) as client:
        files = {"file": ("audio.ogg", audio_bytes, mimetype)}
        data = {"model": model, "language": language, "response_format": "text"}
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = await client.post(
            f"{api_base}/audio/transcriptions",
            headers=headers,
            files=files,
            data=data,
        )
        resp.raise_for_status()
        return resp.text.strip()
