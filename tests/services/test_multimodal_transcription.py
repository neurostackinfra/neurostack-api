"""Tests for :mod:`src.services.multimodal_service`.

The transport is mocked, so these tests can only prove the *shape* of what
leaves the process: the endpoint, the pinned Host, the ``input_audio``
content-block structure and the ``format`` derived from the MIME type. The
matching question — whether the provider accepts that shape — was answered
against the live endpoints and recorded in
``.genie/wishes/fallback-texto/evidence/multimodal-format-probe.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from src.services import multimodal_service, safe_egress
from src.services.multimodal_service import (
    DEFAULT_TRANSCRIPTION_PROMPT,
    MultimodalTranscriptionError,
    format_for_mimetype,
    multimodal_kind_for_url,
    transcribe_audio_multimodal,
)
from src.services.safe_egress import UnsafeEgressError

PUBLIC_IP = "104.18.2.1"
AUDIO = b"OggS\x00\x02fake-opus-payload"


@pytest.fixture(autouse=True)
def _preserve_current_loop_around_asyncio_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep synchronous ``asyncio.run`` calls from clearing an existing loop.

    pytest-asyncio 1.x owns async-test loops and no longer exposes the legacy
    ``event_loop`` fixture.  These synchronous tests still use ``asyncio.run``;
    preserving the loop that was current before each call avoids changing the
    surrounding session state.  ``asyncio.run`` remains responsible for closing
    every loop it creates, so this fixture neither shares nor leaks a test loop.
    """

    original_run = asyncio.run

    def preserving_run(
        main: Any,
        *,
        debug: bool | None = None,
    ) -> Any:
        try:
            previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            previous_loop = None

        try:
            return original_run(main, debug=debug)
        finally:
            if previous_loop is not None and not previous_loop.is_closed():
                asyncio.set_event_loop(previous_loop)

    monkeypatch.setattr(asyncio, "run", preserving_run)


def _addrinfo(ip: str, port: int = 443) -> tuple[Any, ...]:
    if ":" in ip:
        return (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (ip, port, 0, 0),
        )
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


def make_resolver(*ips: str):
    def _resolver(_host: str, port: int) -> list[tuple[Any, ...]]:
        return [_addrinfo(ip, port) for ip in ips]

    return _resolver


@contextmanager
def egress_env(mock_transport: httpx.MockTransport | None):
    """Patch the DNS resolver and force the mock transport on every client."""

    orig_dns = safe_egress._default_getaddrinfo
    orig_build = safe_egress.build_safe_async_client
    resolver = make_resolver(PUBLIC_IP)

    def _fake_build(validated, *, timeout=None, transport=None):
        return orig_build(validated, timeout=timeout, transport=mock_transport)

    safe_egress._default_getaddrinfo = resolver
    safe_egress.build_safe_async_client = _fake_build
    try:
        yield
    finally:
        safe_egress._default_getaddrinfo = orig_dns
        safe_egress.build_safe_async_client = orig_build


def recording_transport(
    response: httpx.Response | None = None,
    *,
    factory: Any = None,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Return a transport that records every request it is handed."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if factory is not None:
            return factory(request)
        assert response is not None
        return response

    return httpx.MockTransport(handler), seen


def chat_response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={"choices": [{"message": {"role": "assistant", "content": text}}]},
    )


# ---------------------------------------------------------------------------
# Kind resolution — no `outros` fallback for multimodal
# ---------------------------------------------------------------------------


class TestMultimodalKindForUrl:
    def test_resolves_each_pinned_provider(self) -> None:
        assert (
            multimodal_kind_for_url("https://openrouter.ai/api/v1")
            == "multimodal_openrouter"
        )
        assert (
            multimodal_kind_for_url("https://api.groq.com/openai/v1")
            == "multimodal_groq"
        )
        assert (
            multimodal_kind_for_url("https://api.openai.com/v1") == "multimodal_openai"
        )

    def test_unknown_host_is_refused_not_downgraded(self) -> None:
        # STT falls back to `stt_outros`; multimodal has no such escape hatch.
        with pytest.raises(UnsafeEgressError) as exc:
            multimodal_kind_for_url("https://proxy.attacker.tld/v1")
        assert exc.value.code == "unsafe_api_url"

    def test_lookalike_host_is_refused(self) -> None:
        with pytest.raises(UnsafeEgressError):
            multimodal_kind_for_url("https://openrouter.ai.attacker.tld/api/v1")

    def test_non_https_is_refused(self) -> None:
        with pytest.raises(UnsafeEgressError):
            multimodal_kind_for_url("http://openrouter.ai/api/v1")

    def test_every_kind_resolves_to_a_policy(self) -> None:
        for kind in (
            "multimodal_openrouter",
            "multimodal_groq",
            "multimodal_openai",
        ):
            policy = safe_egress.resolve_policy(kind)
            assert policy.kind == kind
            assert policy.allowed_hosts, "multimodal policies must pin a host"


# ---------------------------------------------------------------------------
# MIME type -> input_audio.format
# ---------------------------------------------------------------------------


class TestFormatForMimetype:
    @pytest.mark.parametrize(
        ("mimetype", "expected"),
        [
            ("audio/ogg", "ogg"),
            ("audio/ogg; codecs=opus", "ogg"),
            ("AUDIO/OGG", "ogg"),
            ("audio/opus", "ogg"),
            ("application/ogg", "ogg"),
            ("audio/wav", "wav"),
            ("audio/x-wav", "wav"),
            ("audio/mpeg", "mp3"),
            ("audio/mp4", "m4a"),
            ("audio/aac", "aac"),
        ],
    )
    def test_known_mimetypes(self, mimetype: str, expected: str) -> None:
        assert format_for_mimetype(mimetype) == expected

    @pytest.mark.parametrize("mimetype", ["", None, "video/mp4", "application/json"])
    def test_unknown_mimetype_raises(self, mimetype: str | None) -> None:
        with pytest.raises(MultimodalTranscriptionError) as exc:
            format_for_mimetype(mimetype)
        assert exc.value.code == "unsupported_audio_format"


# ---------------------------------------------------------------------------
# Happy path — the exact wire shape
# ---------------------------------------------------------------------------


class TestTranscribeSuccess:
    def test_returns_transcribed_text(self) -> None:
        transport, seen = recording_transport(chat_response("  bom dia doutor  "))
        with egress_env(transport):
            text = asyncio.run(
                transcribe_audio_multimodal(
                    api_key="k",
                    audio_bytes=AUDIO,
                    mimetype="audio/ogg; codecs=opus",
                )
            )
        assert text == "bom dia doutor"
        assert len(seen) == 1

    def test_request_body_has_the_exact_input_audio_shape(self) -> None:
        transport, seen = recording_transport(chat_response("oi"))
        with egress_env(transport):
            asyncio.run(
                transcribe_audio_multimodal(
                    api_key="super-secret",
                    audio_bytes=AUDIO,
                    mimetype="audio/ogg; codecs=opus",
                    model="google/gemini-2.5-flash",
                )
            )
        request = seen[0]
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Host"] == "openrouter.ai"
        assert request.headers["Authorization"] == "Bearer super-secret"

        body = json.loads(request.content)
        assert body["model"] == "google/gemini-2.5-flash"
        assert len(body["messages"]) == 1
        message = body["messages"][0]
        assert message["role"] == "user"
        assert message["content"] == [
            {"type": "text", "text": DEFAULT_TRANSCRIPTION_PROMPT},
            {
                "type": "input_audio",
                "input_audio": {
                    "data": base64.b64encode(AUDIO).decode("ascii"),
                    "format": "ogg",
                },
            },
        ]
        # The audio really round-trips: base64 of the exact bytes handed in.
        assert base64.b64decode(message["content"][1]["input_audio"]["data"]) == AUDIO

    @pytest.mark.parametrize(
        ("mimetype", "expected_format"),
        [("audio/ogg", "ogg"), ("audio/wav", "wav"), ("audio/mpeg", "mp3")],
    )
    def test_format_on_the_wire_follows_the_mimetype(
        self, mimetype: str, expected_format: str
    ) -> None:
        transport, seen = recording_transport(chat_response("oi"))
        with egress_env(transport):
            asyncio.run(
                transcribe_audio_multimodal(
                    api_key="k", audio_bytes=AUDIO, mimetype=mimetype
                )
            )
        body = json.loads(seen[0].content)
        block = body["messages"][0]["content"][1]
        assert block["input_audio"]["format"] == expected_format

    def test_custom_prompt_replaces_the_default(self) -> None:
        transport, seen = recording_transport(chat_response("oi"))
        with egress_env(transport):
            asyncio.run(
                transcribe_audio_multimodal(
                    api_key="k",
                    audio_bytes=AUDIO,
                    mimetype="audio/ogg",
                    prompt="transcribe",
                )
            )
        body = json.loads(seen[0].content)
        assert body["messages"][0]["content"][0] == {
            "type": "text",
            "text": "transcribe",
        }

    def test_openai_base_url_pins_to_openai_host(self) -> None:
        transport, seen = recording_transport(chat_response("hello"))
        with egress_env(transport):
            text = asyncio.run(
                transcribe_audio_multimodal(
                    api_key="k",
                    audio_bytes=AUDIO,
                    mimetype="audio/wav",
                    model="gpt-audio-mini",
                    base_url="https://api.openai.com/v1/",
                )
            )
        assert text == "hello"
        assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
        assert seen[0].headers["Host"] == "api.openai.com"

    def test_content_block_list_response_is_joined(self) -> None:
        transport, _ = recording_transport(
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {"type": "text", "text": "bom "},
                                    {"type": "text", "text": "dia"},
                                ]
                            }
                        }
                    ]
                },
            )
        )
        with egress_env(transport):
            text = asyncio.run(
                transcribe_audio_multimodal(
                    api_key="k", audio_bytes=AUDIO, mimetype="audio/ogg"
                )
            )
        assert text == "bom dia"


# ---------------------------------------------------------------------------
# Failure modes — never a silent empty string
# ---------------------------------------------------------------------------


class TestTranscribeFailures:
    @pytest.mark.parametrize("status", [400, 401, 429, 500])
    def test_http_error_raises(self, status: int) -> None:
        transport, _ = recording_transport(httpx.Response(status, json={"error": "x"}))
        with egress_env(transport):
            with pytest.raises(httpx.HTTPStatusError):
                asyncio.run(
                    transcribe_audio_multimodal(
                        api_key="k", audio_bytes=AUDIO, mimetype="audio/ogg"
                    )
                )

    def test_transport_error_raises(self) -> None:
        def factory(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        transport, _ = recording_transport(factory=factory)
        with egress_env(transport):
            with pytest.raises(httpx.HTTPError):
                asyncio.run(
                    transcribe_audio_multimodal(
                        api_key="k", audio_bytes=AUDIO, mimetype="audio/ogg"
                    )
                )

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"choices": []},
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": "   "}}]},
            {"choices": [{"message": {"content": None, "refusal": "no"}}]},
            {"choices": [{"message": {}}]},
            {"choices": [{}]},
        ],
    )
    def test_empty_response_raises_instead_of_returning_empty_string(
        self, payload: dict[str, Any]
    ) -> None:
        transport, _ = recording_transport(httpx.Response(200, json=payload))
        with egress_env(transport):
            with pytest.raises(MultimodalTranscriptionError) as exc:
                asyncio.run(
                    transcribe_audio_multimodal(
                        api_key="k", audio_bytes=AUDIO, mimetype="audio/ogg"
                    )
                )
        assert exc.value.code == "empty_multimodal_response"

    def test_non_json_body_raises(self) -> None:
        transport, _ = recording_transport(httpx.Response(200, text="not json"))
        with egress_env(transport):
            with pytest.raises(MultimodalTranscriptionError) as exc:
                asyncio.run(
                    transcribe_audio_multimodal(
                        api_key="k", audio_bytes=AUDIO, mimetype="audio/ogg"
                    )
                )
        assert exc.value.code == "empty_multimodal_response"

    def test_unknown_mimetype_refuses_before_any_request(self) -> None:
        transport, seen = recording_transport(chat_response("oi"))
        with egress_env(transport):
            with pytest.raises(MultimodalTranscriptionError) as exc:
                asyncio.run(
                    transcribe_audio_multimodal(
                        api_key="k", audio_bytes=AUDIO, mimetype="video/mp4"
                    )
                )
        assert exc.value.code == "unsupported_audio_format"
        assert seen == []

    def test_groq_is_refused_before_any_request(self) -> None:
        # Groq's chat schema rejects the `input_audio` block itself, so the
        # adapter fails fast rather than spending a request on a 400.
        transport, seen = recording_transport(chat_response("oi"))
        with egress_env(transport):
            with pytest.raises(MultimodalTranscriptionError) as exc:
                asyncio.run(
                    transcribe_audio_multimodal(
                        api_key="k",
                        audio_bytes=AUDIO,
                        mimetype="audio/ogg",
                        base_url="https://api.groq.com/openai/v1",
                    )
                )
        assert exc.value.code == "provider_not_multimodal_capable"
        assert seen == []

    def test_openai_refuses_ogg_before_any_request(self) -> None:
        # api.openai.com only accepts 'wav'/'mp3' in input_audio.format.
        transport, seen = recording_transport(chat_response("oi"))
        with egress_env(transport):
            with pytest.raises(MultimodalTranscriptionError) as exc:
                asyncio.run(
                    transcribe_audio_multimodal(
                        api_key="k",
                        audio_bytes=AUDIO,
                        mimetype="audio/ogg",
                        base_url="https://api.openai.com/v1",
                    )
                )
        assert exc.value.code == "unsupported_audio_format"
        assert seen == []

    def test_host_outside_the_allowlist_is_rejected(self) -> None:
        transport, seen = recording_transport(chat_response("oi"))
        with egress_env(transport):
            with pytest.raises(UnsafeEgressError):
                asyncio.run(
                    transcribe_audio_multimodal(
                        api_key="k",
                        audio_bytes=AUDIO,
                        mimetype="audio/ogg",
                        base_url="https://evil.example.com/v1",
                    )
                )
        assert seen == []


# ---------------------------------------------------------------------------
# Provider capability table vs. the published operational set
# ---------------------------------------------------------------------------


class TestProviderCapabilityTable:
    def test_openrouter_is_the_only_provider_accepting_whatsapp_audio(self) -> None:
        table = multimodal_service.ACCEPTED_FORMATS_BY_KIND
        assert "ogg" in table["multimodal_openrouter"]
        assert "ogg" not in table["multimodal_openai"]
        assert table["multimodal_groq"] == frozenset()

    def test_openrouter_row_separates_measured_from_unverified(self) -> None:
        measured = multimodal_service.OPENROUTER_MEASURED_FORMATS
        unverified = multimodal_service.OPENROUTER_UNVERIFIED_FORMATS
        # Only what the probe evidence records may claim to be measured.
        assert measured == frozenset({"ogg", "wav"})
        assert not (measured & unverified)
        assert (
            multimodal_service.ACCEPTED_FORMATS_BY_KIND["multimodal_openrouter"]
            == measured | unverified
        )

    def test_operational_set_matches_the_capability_table(self) -> None:
        from src.services.api_credentials import OPERATIONAL_MULTIMODAL_PROVIDERS

        whatsapp_format = format_for_mimetype("audio/ogg")
        capable = {
            provider
            for provider, kind in (
                ("openrouter", "multimodal_openrouter"),
                ("groq", "multimodal_groq"),
                ("openai", "multimodal_openai"),
            )
            if whatsapp_format in multimodal_service.ACCEPTED_FORMATS_BY_KIND[kind]
        }
        assert set(OPERATIONAL_MULTIMODAL_PROVIDERS) == capable == {"openrouter"}
