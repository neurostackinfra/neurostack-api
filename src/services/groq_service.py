"""
NeuroStack - Groq Whisper Service
Async client for Groq's OpenAI-compatible audio transcription API.
"""

import httpx

GROQ_API_BASE = "https://api.groq.com/openai/v1"


async def transcribe_audio(
    api_key: str,
    audio_bytes: bytes,
    mimetype: str,
    model: str = "whisper-large-v3-turbo",
    language: str = "pt",
) -> str:
    """Transcribe audio using Groq Whisper API. Returns transcribed text."""
    async with httpx.AsyncClient(timeout=60) as client:
        files = {"file": ("audio.ogg", audio_bytes, mimetype)}
        data = {"model": model, "language": language, "response_format": "text"}
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = await client.post(
            f"{GROQ_API_BASE}/audio/transcriptions",
            headers=headers,
            files=files,
            data=data,
        )
        resp.raise_for_status()
        return resp.text.strip()
