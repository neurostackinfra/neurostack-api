"""Unit tests for the operational TTS adapters and their egress boundary."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from src.services import safe_egress, tts_service
from src.services.safe_egress import UnsafeEgressError
from src.services.tts_service import TtsSynthesisError

PUBLIC_IP = "142.250.219.10"
AUDIO = b"ID3-fake-mp3"


def _addrinfo(ip: str, port: int = 443) -> tuple[Any, ...]:
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


@contextmanager
def egress_env(mock_transport: httpx.MockTransport):
    original_dns = safe_egress._default_getaddrinfo
    original_build = safe_egress.build_safe_async_client

    def resolver(_host: str, port: int) -> list[tuple[Any, ...]]:
        return [_addrinfo(PUBLIC_IP, port)]

    def build(validated, *, timeout=None, transport=None):
        return original_build(validated, timeout=timeout, transport=mock_transport)

    safe_egress._default_getaddrinfo = resolver
    safe_egress.build_safe_async_client = build
    try:
        yield
    finally:
        safe_egress._default_getaddrinfo = original_dns
        safe_egress.build_safe_async_client = original_build


def test_elevenlabs_sends_model_voice_and_key_and_returns_mp3() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=AUDIO, headers={"Content-Type": "audio/mpeg"}
        )

    credentials = {
        "tts_provider": "elevenlabs",
        "tts_api_url": "https://api.elevenlabs.io/v1",
        "tts_api_key": "secret-eleven-key",
        "tts_model_name": "eleven_multilingual_v2",
        "tts_voice_id": "voice-123",
    }
    with egress_env(httpx.MockTransport(handler)):
        audio = asyncio.run(tts_service.synthesize_speech("Olá", credentials))

    assert audio.content == AUDIO
    assert audio.mime_type == "audio/mpeg"
    request = seen[0]
    assert request.url.path == "/v1/text-to-speech/voice-123"
    assert request.url.params["output_format"] == "mp3_44100_128"
    assert request.headers["xi-api-key"] == "secret-eleven-key"
    assert request.headers["Host"] == "api.elevenlabs.io"
    assert json.loads(request.content) == {
        "text": "Olá",
        "model_id": "eleven_multilingual_v2",
    }


def test_elevenlabs_accepts_saved_synthesis_endpoint_without_duplicating_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=AUDIO)

    credentials = {
        "tts_provider": "elevenlabs",
        "tts_api_url": "https://api.elevenlabs.io/v1/text-to-speech",
        "tts_api_key": "secret",
        "tts_model_name": "eleven_multilingual_v2",
        "tts_voice_id": "voice-123",
    }
    with egress_env(httpx.MockTransport(handler)):
        asyncio.run(tts_service.synthesize_speech("Olá", credentials))

    assert seen[0].url.path == "/v1/text-to-speech/voice-123"
    assert seen[0].url.path.count("text-to-speech") == 1


def test_google_sends_voice_language_and_decodes_audio() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"audioContent": base64.b64encode(AUDIO).decode("ascii")},
        )

    credentials = {
        "tts_provider": "google",
        "tts_api_url": "https://texttospeech.googleapis.com/v1",
        "tts_api_key": "secret-google-key",
        "tts_model_name": "pt-BR-Neural2-A",
    }
    with egress_env(httpx.MockTransport(handler)):
        audio = asyncio.run(
            tts_service.synthesize_speech("Bom dia", credentials, language_code="pt-BR")
        )

    assert audio.content == AUDIO
    request = seen[0]
    assert request.url.path == "/v1/text:synthesize"
    assert request.headers["x-goog-api-key"] == "secret-google-key"
    assert request.headers["Host"] == "texttospeech.googleapis.com"
    assert json.loads(request.content) == {
        "input": {"text": "Bom dia"},
        "voice": {"languageCode": "pt-BR", "name": "pt-BR-Neural2-A"},
        "audioConfig": {"audioEncoding": "MP3"},
    }


def test_elevenlabs_requires_voice_id_before_network() -> None:
    credentials = {
        "tts_provider": "elevenlabs",
        "tts_api_url": "https://api.elevenlabs.io/v1",
        "tts_api_key": "secret",
        "tts_model_name": "model",
    }
    with pytest.raises(TtsSynthesisError) as exc:
        asyncio.run(tts_service.synthesize_speech("Olá", credentials))
    assert exc.value.code == "tts_configuration_incomplete"


def test_provider_host_is_pinned() -> None:
    credentials = {
        "tts_provider": "google",
        "tts_api_url": "https://attacker.example/v1",
        "tts_api_key": "secret",
        "tts_model_name": "pt-BR-Neural2-A",
    }
    with egress_env(
        httpx.MockTransport(lambda request: pytest.fail("network must not be reached"))
    ):
        with pytest.raises(UnsafeEgressError):
            asyncio.run(tts_service.synthesize_speech("Olá", credentials))


def test_upstream_error_is_secret_free() -> None:
    secret = "very-secret-eleven-key"
    credentials = {
        "tts_provider": "elevenlabs",
        "tts_api_url": "https://api.elevenlabs.io/v1",
        "tts_api_key": secret,
        "tts_model_name": "model",
        "tts_voice_id": "voice",
    }
    with egress_env(httpx.MockTransport(lambda request: httpx.Response(401))):
        with pytest.raises(TtsSynthesisError) as exc:
            asyncio.run(tts_service.synthesize_speech("Olá", credentials))
    assert exc.value.code == "tts_upstream_http_error_401"
    assert secret not in repr(exc.value)
