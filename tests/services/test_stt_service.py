"""Deterministic contract tests for the private STT transport primitives."""

from __future__ import annotations

import asyncio
import base64
import socket
from email import policy
from email.parser import BytesParser
from typing import Any

import httpx
import pytest

from src.services import stt_service
from src.services.stt_service import (
    ELEVENLABS_API_BASE,
    MIME_EXTENSION_BY_TYPE,
    OPENROUTER_API_BASE,
    SttTranscriptionError,
    normalize_audio_mime,
    transcribe_audio,
)

PUBLIC_IP = "93.184.216.34"
RAW_AUDIO = b"OggS\x00\xffraw-audio-bytes\x10"


def _resolver(hostname: str, port: int) -> list[tuple[Any, ...]]:
    del hostname
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (PUBLIC_IP, port),
        )
    ]


def _multipart_parts(request: httpx.Request) -> dict[str, dict[str, Any]]:
    content_type = request.headers["content-type"]
    message = BytesParser(policy=policy.default).parsebytes(
        (f"Content-Type: {content_type}\r\n" "MIME-Version: 1.0\r\n\r\n").encode(
            "ascii"
        )
        + request.content
    )
    assert message.is_multipart()
    result: dict[str, dict[str, Any]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        assert isinstance(name, str)
        result[name] = {
            "content": part.get_payload(decode=True),
            "content_type": part.get_content_type(),
            "filename": part.get_filename(),
        }
    return result


def _call(
    provider: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    mimetype: str = "audio/ogg; codecs=opus",
    language: str | None = "pt-BR",
    api_url: str | None = None,
    resolver: Any = _resolver,
) -> str:
    base = {
        "elevenlabs_scribe": ELEVENLABS_API_BASE,
        "openrouter": OPENROUTER_API_BASE,
    }.get(provider, "https://unused.invalid")
    return asyncio.run(
        transcribe_audio(
            provider=provider,
            api_url=api_url if api_url is not None else base,
            api_key="stt-secret-sentinel",
            audio_bytes=RAW_AUDIO,
            mimetype=mimetype,
            model_name="vendor-model-sentinel",
            language=language,
            resolver=resolver,
            transport=transport,
        )
    )


@pytest.mark.parametrize(
    ("provider", "expected_host", "expected_path", "auth_header", "model_field"),
    [
        (
            "openrouter",
            "openrouter.ai",
            "/api/v1/audio/transcriptions",
            "authorization",
            "model",
        ),
        (
            "elevenlabs_scribe",
            "api.elevenlabs.io",
            "/v1/speech-to-text",
            "xi-api-key",
            "model_id",
        ),
    ],
)
def test_provider_contracts_use_exact_multipart_request(
    provider: str,
    expected_host: str,
    expected_path: str,
    auth_header: str,
    model_field: str,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "  transcrição válida  "})

    result = _call(provider, transport=httpx.MockTransport(handler))

    assert result == "transcrição válida"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.host == expected_host
    assert request.url.path == expected_path
    assert request.headers["host"] == expected_host
    assert request.headers[auth_header] == (
        "Bearer stt-secret-sentinel"
        if provider == "openrouter"
        else "stt-secret-sentinel"
    )
    assert ("xi-api-key" in request.headers) is (provider == "elevenlabs_scribe")
    assert ("authorization" in request.headers) is (provider == "openrouter")
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")

    parts = _multipart_parts(request)
    assert parts["file"] == {
        "content": RAW_AUDIO,
        "content_type": "audio/ogg",
        "filename": "audio.ogg",
    }
    assert parts[model_field]["content"] == b"vendor-model-sentinel"
    assert ("language" in parts) is (provider == "openrouter")
    if provider == "openrouter":
        assert parts["language"]["content"] == b"pt-BR"
    assert base64.b64encode(RAW_AUDIO) not in request.content


def test_openrouter_omits_optional_language() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "ok"})

    assert (
        _call(
            "openrouter",
            language=None,
            transport=httpx.MockTransport(handler),
        )
        == "ok"
    )
    assert "language" not in _multipart_parts(seen[0])


@pytest.mark.parametrize(
    ("raw", "canonical", "extension"),
    [
        ("audio/wav", "audio/wav", "wav"),
        ("audio/mpeg", "audio/mpeg", "mp3"),
        ("audio/flac", "audio/flac", "flac"),
        ("audio/mp4", "audio/mp4", "m4a"),
        ("audio/ogg", "audio/ogg", "ogg"),
        ("application/ogg", "application/ogg", "ogg"),
        ("audio/webm", "audio/webm", "webm"),
        ("audio/aac", "audio/aac", "aac"),
        (" Audio/OGG ; codecs=opus ", "audio/ogg", "ogg"),
        ("audio/ogg;codecs=opus; rate=48000", "audio/ogg", "ogg"),
    ],
)
def test_mime_normalization_is_closed_and_parameter_aware(
    raw: str, canonical: str, extension: str
) -> None:
    assert normalize_audio_mime(raw) == (canonical, extension)


def test_mime_map_has_only_the_governed_types() -> None:
    assert MIME_EXTENSION_BY_TYPE == {
        "audio/wav": "wav",
        "audio/mpeg": "mp3",
        "audio/flac": "flac",
        "audio/mp4": "m4a",
        "audio/ogg": "ogg",
        "application/ogg": "ogg",
        "audio/webm": "webm",
        "audio/aac": "aac",
    }


@pytest.mark.parametrize(
    "mimetype",
    [
        "text/plain",
        "audio/mp3",
        "audio/ogg;",
        "audio/ogg;codecs",
        "audio/ogg;=opus",
        "audio/ogg;codecs=",
        "audio/ogg;codecs=opus;;rate=48000",
        "audio/ogg;codecs=op=us",
        'audio/ogg;codecs="opus"',
        "audio/ogg;codécs=opus",
    ],
)
def test_bad_mime_fails_before_client_dns_or_http(
    monkeypatch: pytest.MonkeyPatch, mimetype: str
) -> None:
    def forbidden_client(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("safe client must not be constructed")

    monkeypatch.setattr(stt_service, "safe_async_client", forbidden_client)
    with pytest.raises(SttTranscriptionError) as excinfo:
        _call("openrouter", mimetype=mimetype, resolver=None)
    assert excinfo.value.code == "unsupported_audio_mime"


@pytest.mark.parametrize(
    "api_url",
    [
        "https://openrouter.ai/api/v1/",
        "https://openrouter.ai:443/api/v1",
        "https://openrouter.ai/api/v1?region=us",
        "https://user:secret@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1#fragment",
        "https://openrouter.ai/api/v1/audio/transcriptions",
        "https://api.openrouter.ai/api/v1",
        "https://attacker.example/api/v1",
    ],
)
def test_noncanonical_base_fails_before_client_dns_or_http(
    monkeypatch: pytest.MonkeyPatch, api_url: str
) -> None:
    def forbidden_client(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("safe client must not be constructed")

    monkeypatch.setattr(stt_service, "safe_async_client", forbidden_client)
    with pytest.raises(SttTranscriptionError) as excinfo:
        _call("openrouter", api_url=api_url, resolver=None)
    assert excinfo.value.code == "invalid_provider_endpoint"


def test_unknown_provider_fails_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_client(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("safe client must not be constructed")

    monkeypatch.setattr(stt_service, "safe_async_client", forbidden_client)
    with pytest.raises(SttTranscriptionError) as excinfo:
        _call("deepgram", resolver=None)
    assert excinfo.value.code == "stt_provider_unsupported"


@pytest.mark.parametrize(
    "response_kwargs",
    [
        {"content": b"not-json"},
        {"json": []},
        {"json": {}},
        {"json": {"text": None}},
        {"json": {"text": 42}},
    ],
)
def test_malformed_response_is_strict_and_redacted(
    response_kwargs: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "provider-body-secret-sentinel"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        kwargs = dict(response_kwargs)
        if kwargs.get("content") == b"not-json":
            kwargs["content"] += sentinel.encode()
        return httpx.Response(200, **kwargs)

    with pytest.raises(SttTranscriptionError) as excinfo:
        _call("elevenlabs_scribe", transport=httpx.MockTransport(handler))
    assert excinfo.value.code == "stt_malformed_response"
    assert str(excinfo.value) == "stt_malformed_response"
    assert excinfo.value.__cause__ is None
    assert sentinel not in str(excinfo.value)
    assert sentinel not in caplog.text


@pytest.mark.parametrize("text", ["", "  \r\n\t "])
def test_empty_transcript_has_its_own_token(text: str) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"text": text}, request=request)
    )
    with pytest.raises(SttTranscriptionError) as excinfo:
        _call("openrouter", transport=transport)
    assert excinfo.value.code == "stt_empty_transcript"


def test_http_error_redacts_status_body_headers_and_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinels = (
        "status-418-sentinel",
        "provider-body-secret-sentinel",
        "provider-header-secret-sentinel",
        "stt-secret-sentinel",
        "vendor-model-sentinel",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            418,
            content=f"{sentinels[0]} {sentinels[1]}".encode(),
            headers={"x-provider-detail": sentinels[2]},
            request=request,
        )

    with pytest.raises(SttTranscriptionError) as excinfo:
        _call("openrouter", transport=httpx.MockTransport(handler))
    assert excinfo.value.code == "stt_provider_http_error"
    evidence = repr(excinfo.value) + str(excinfo.value) + caplog.text
    assert all(sentinel not in evidence for sentinel in sentinels)
    assert excinfo.value.__cause__ is None


def test_transport_error_is_redacted_and_unchained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "transport-and-url-secret-sentinel"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(sentinel, request=request)

    with pytest.raises(SttTranscriptionError) as excinfo:
        _call("elevenlabs_scribe", transport=httpx.MockTransport(handler))
    assert excinfo.value.code == "stt_provider_transport_error"
    assert sentinel not in repr(excinfo.value) + str(excinfo.value) + caplog.text
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_dns_failure_is_transport_error_before_http() -> None:
    http_calls = 0

    def resolver(_hostname: str, _port: int) -> list[tuple[Any, ...]]:
        raise socket.gaierror("dns-secret-sentinel")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={"text": "unexpected"}, request=request)

    with pytest.raises(SttTranscriptionError) as excinfo:
        _call(
            "openrouter",
            resolver=resolver,
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.code == "stt_provider_transport_error"
    assert "dns-secret-sentinel" not in str(excinfo.value)
    assert http_calls == 0


def test_redirect_is_http_error_and_is_not_followed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://attacker.example/secret-path"},
            request=request,
        )

    with pytest.raises(SttTranscriptionError) as excinfo:
        _call("openrouter", transport=httpx.MockTransport(handler))
    assert excinfo.value.code == "stt_provider_http_error"
    assert len(seen) == 1
    assert "attacker.example" not in str(excinfo.value)
