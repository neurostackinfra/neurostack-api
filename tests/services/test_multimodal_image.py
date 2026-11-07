"""Tests for :func:`src.services.multimodal_service.describe_image_multimodal`.

The transport is mocked, so these tests can only prove the *shape* of what
leaves the process: the endpoint, the pinned Host, exactly one ``image_url``
content block carrying a ``data:`` URL of the normalized MIME, and the
description prompt in the agent's language (whatsapp-image-understanding,
Decision 11). They also prove that every refusal happens before any I/O (no
DNS lookup, no client, no request) and that every failure surfaces as one of
the stable, secret-free adapter codes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from contextlib import contextmanager
from typing import Any, Iterator

import httpx
import pytest

from src.services import multimodal_service, safe_egress
from src.services.multimodal_service import (
    IMAGE_DESCRIPTION_PROMPT_EN,
    IMAGE_DESCRIPTION_PROMPT_PT,
    IMAGE_ERROR_CODES,
    IMAGE_NO_CONTENT_MARKER,
    MultimodalImageError,
    describe_image_multimodal,
    image_description_prompt,
)
from src.services.safe_egress import IMAGE_MIME_ALLOWLIST, UnsafeEgressError

PUBLIC_IP = "104.18.2.1"
IMAGE = b"\xff\xd8\xff\xe0\x00\x10JFIF-fake-jpeg-payload"
OPENROUTER = "https://openrouter.ai/api/v1"
API_KEY = "sk-or-v1-super-secret"


@pytest.fixture(autouse=True)
def _preserve_current_loop_around_asyncio_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep synchronous ``asyncio.run`` calls from clearing an existing loop.

    Same shim as ``tests/services/test_multimodal_transcription.py``: these
    synchronous tests use ``asyncio.run`` and must not change the loop that
    was current before each call.
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
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


class _Io:
    """What the egress boundary was asked to do during one test."""

    def __init__(self) -> None:
        self.lookups: list[str] = []
        self.clients = 0
        self.requests: list[httpx.Request] = []

    @property
    def untouched(self) -> bool:
        return not self.lookups and self.clients == 0 and not self.requests


@contextmanager
def egress_env(
    response: httpx.Response | None = None,
    *,
    factory: Any = None,
) -> Iterator[_Io]:
    """Patch DNS and force a recording mock transport on every safe client."""

    io = _Io()
    orig_dns = safe_egress._default_getaddrinfo
    orig_build = safe_egress.build_safe_async_client

    def resolver(host: str, port: int) -> list[tuple[Any, ...]]:
        io.lookups.append(host)
        return [_addrinfo(PUBLIC_IP, port)]

    def handler(request: httpx.Request) -> httpx.Response:
        io.requests.append(request)
        if factory is not None:
            return factory(request)
        assert response is not None
        return response

    mock_transport = httpx.MockTransport(handler)

    def build(validated: Any, *, timeout: Any = None, transport: Any = None):
        io.clients += 1
        return orig_build(validated, timeout=timeout, transport=mock_transport)

    safe_egress._default_getaddrinfo = resolver
    safe_egress.build_safe_async_client = build
    try:
        yield io
    finally:
        safe_egress._default_getaddrinfo = orig_dns
        safe_egress.build_safe_async_client = orig_build


def chat_response(content: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


def describe(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "api_key": API_KEY,
        "image_bytes": IMAGE,
        "mimetype": "image/jpeg",
        "model": "google/gemini-2.5-flash",
        "base_url": OPENROUTER,
        "agent_language": "pt-BR",
    }
    kwargs.update(overrides)
    return asyncio.run(describe_image_multimodal(**kwargs))


def _image_blocks(request: httpx.Request) -> list[dict[str, Any]]:
    body = json.loads(request.content)
    return [
        block
        for message in body["messages"]
        for block in message["content"]
        if block.get("type") == "image_url"
    ]


# ---------------------------------------------------------------------------
# Prompt — Decision 11
# ---------------------------------------------------------------------------


class TestPrompt:
    @pytest.mark.parametrize(
        ("agent_language", "expected"),
        [
            ("en", IMAGE_DESCRIPTION_PROMPT_EN),
            ("pt-BR", IMAGE_DESCRIPTION_PROMPT_PT),
            ("", IMAGE_DESCRIPTION_PROMPT_PT),
            ("es", IMAGE_DESCRIPTION_PROMPT_PT),
        ],
    )
    def test_prompt_follows_the_agent_language(
        self, agent_language: str, expected: str
    ) -> None:
        assert image_description_prompt(agent_language) == expected

    def test_both_prompts_end_with_the_no_content_marker(self) -> None:
        assert IMAGE_NO_CONTENT_MARKER == "[[NO_IMAGE_CONTENT]]"
        for prompt in (IMAGE_DESCRIPTION_PROMPT_PT, IMAGE_DESCRIPTION_PROMPT_EN):
            assert prompt.rstrip().endswith(IMAGE_NO_CONTENT_MARKER)

    def test_portuguese_prompt_is_objective_and_never_clinical(self) -> None:
        prompt = IMAGE_DESCRIPTION_PROMPT_PT.lower()
        assert "português do brasil" in prompt
        assert "objetiva" in prompt
        assert "transcreva" in prompt and "texto visível" in prompt
        assert "não faça diagnóstico" in prompt
        assert "opinião clínica" in prompt

    def test_english_prompt_is_objective_and_never_clinical(self) -> None:
        prompt = IMAGE_DESCRIPTION_PROMPT_EN.lower()
        assert "in english" in prompt
        assert "objectively" in prompt
        assert "transcribe" in prompt and "text visible" in prompt
        assert "do not make a diagnosis" in prompt
        assert "clinical opinion" in prompt


# ---------------------------------------------------------------------------
# Happy path — the exact wire shape
# ---------------------------------------------------------------------------


class TestDescribeSuccess:
    def test_request_has_exactly_one_image_url_block_with_a_data_url(self) -> None:
        with egress_env(chat_response("Uma receita médica manuscrita.")) as io:
            text = describe()

        assert text == "Uma receita médica manuscrita."
        assert len(io.requests) == 1
        request = io.requests[0]
        assert str(request.url) == f"{OPENROUTER}/chat/completions"
        assert request.headers["Host"] == "openrouter.ai"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"

        body = json.loads(request.content)
        assert body["model"] == "google/gemini-2.5-flash"
        assert len(body["messages"]) == 1
        message = body["messages"][0]
        assert message["role"] == "user"
        encoded = base64.b64encode(IMAGE).decode("ascii")
        assert message["content"] == [
            {"type": "text", "text": IMAGE_DESCRIPTION_PROMPT_PT},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            },
        ]
        assert len(_image_blocks(request)) == 1
        # The bytes really round-trip: base64 of exactly what was handed in.
        url = _image_blocks(request)[0]["image_url"]["url"]
        assert base64.b64decode(url.split(",", 1)[1]) == IMAGE

    @pytest.mark.parametrize(
        ("mimetype", "normalized"),
        [
            ("image/jpeg", "image/jpeg"),
            ("IMAGE/JPEG", "image/jpeg"),
            ("image/png; charset=binary", "image/png"),
            ("  image/webp  ", "image/webp"),
        ],
    )
    def test_data_url_carries_the_normalized_mime(
        self, mimetype: str, normalized: str
    ) -> None:
        with egress_env(chat_response("ok")) as io:
            describe(mimetype=mimetype)
        url = _image_blocks(io.requests[0])[0]["image_url"]["url"]
        assert url.startswith(f"data:{normalized};base64,")

    def test_english_agent_gets_the_english_prompt(self) -> None:
        with egress_env(chat_response("A handwritten prescription.")) as io:
            text = describe(agent_language="en")
        assert text == "A handwritten prescription."
        body = json.loads(io.requests[0].content)
        texts = [b for b in body["messages"][0]["content"] if b["type"] == "text"]
        assert texts == [{"type": "text", "text": IMAGE_DESCRIPTION_PROMPT_EN}]

    def test_description_is_stripped(self) -> None:
        with egress_env(chat_response("  \n Uma foto de um gato. \n ")):
            assert describe() == "Uma foto de um gato."

    def test_content_block_list_response_is_joined(self) -> None:
        payload = [
            {"type": "text", "text": "Uma foto "},
            {"type": "text", "text": "de"},
        ]
        with egress_env(chat_response(payload)):
            assert describe() == "Uma foto de"

    def test_trailing_slash_base_url_is_normalized(self) -> None:
        with egress_env(chat_response("ok")) as io:
            describe(base_url=f"{OPENROUTER}/")
        assert str(io.requests[0].url) == f"{OPENROUTER}/chat/completions"


# ---------------------------------------------------------------------------
# Refusals — always before any I/O
# ---------------------------------------------------------------------------


class TestRefusedBeforeAnyIo:
    @pytest.mark.parametrize(
        "base_url",
        [
            "https://proxy.attacker.tld/v1",
            "https://openrouter.ai.attacker.tld/api/v1",
            "http://openrouter.ai/api/v1",
            "https://openrouter.ai:8443/api/v1",
        ],
    )
    def test_host_outside_the_pinned_providers_is_refused(self, base_url: str) -> None:
        with egress_env(chat_response("ok")) as io:
            with pytest.raises(UnsafeEgressError) as exc:
                describe(base_url=base_url)
        assert exc.value.code == "unsafe_api_url"
        assert io.untouched

    @pytest.mark.parametrize(
        "base_url",
        ["https://api.openai.com/v1", "https://api.groq.com/openai/v1"],
    )
    def test_pinned_but_non_openrouter_provider_is_refused(self, base_url: str) -> None:
        # Only the tenant's OpenRouter bundle may receive patient images.
        with egress_env(chat_response("ok")) as io:
            with pytest.raises(MultimodalImageError) as exc:
                describe(base_url=base_url)
        assert exc.value.code == "provider_not_multimodal_capable"
        assert io.untouched

    @pytest.mark.parametrize(
        "mimetype",
        [
            "image/gif",
            "image/heic",
            "image/jpg",
            "image/svg+xml",
            "application/pdf",
            "application/octet-stream",
            "",
            None,
        ],
    )
    def test_mime_outside_the_allowlist_is_refused(self, mimetype: str | None) -> None:
        with egress_env(chat_response("ok")) as io:
            with pytest.raises(MultimodalImageError) as exc:
                describe(mimetype=mimetype)
        assert exc.value.code == "unsupported_image_mime"
        assert io.untouched

    def test_the_adapter_allowlist_is_the_downloader_allowlist(self) -> None:
        assert IMAGE_MIME_ALLOWLIST == frozenset(
            {"image/jpeg", "image/png", "image/webp"}
        )

    def test_empty_image_is_refused(self) -> None:
        with egress_env(chat_response("ok")) as io:
            with pytest.raises(MultimodalImageError) as exc:
                describe(image_bytes=b"")
        assert exc.value.code == "no_image_content"
        assert io.untouched


# ---------------------------------------------------------------------------
# Failure modes — stable codes, never a silent empty string
# ---------------------------------------------------------------------------


class TestDescribeFailures:
    @pytest.mark.parametrize("status", [400, 401, 402, 429, 500, 503])
    def test_http_error_is_provider_http_error(self, status: int) -> None:
        with egress_env(httpx.Response(status, json={"error": "x"})):
            with pytest.raises(MultimodalImageError) as exc:
                describe()
        assert exc.value.code == "provider_http_error"
        assert exc.value.context == {"status_code": status}

    @pytest.mark.parametrize(
        "error",
        [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError],
    )
    def test_transport_error_is_provider_transport_error(
        self, error: type[httpx.TransportError]
    ) -> None:
        def factory(request: httpx.Request) -> httpx.Response:
            raise error("boom", request=request)

        with egress_env(factory=factory):
            with pytest.raises(MultimodalImageError) as exc:
                describe()
        assert exc.value.code == "provider_transport_error"

    def test_redirect_stays_an_egress_refusal(self) -> None:
        response = httpx.Response(302, headers={"Location": "https://elsewhere.tld/"})
        with egress_env(response):
            with pytest.raises(UnsafeEgressError) as exc:
                describe()
        assert exc.value.code == "redirect_forbidden"

    def test_non_json_body_is_invalid_provider_response(self) -> None:
        with egress_env(httpx.Response(200, text="<html>not json</html>")):
            with pytest.raises(MultimodalImageError) as exc:
                describe()
        assert exc.value.code == "invalid_provider_response"

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {},
            {"choices": []},
            {"choices": "nope"},
            {"choices": [{}]},
            {"choices": ["nope"]},
            {"choices": [{"message": "nope"}]},
            {"choices": [{"message": {"content": 5}}]},
        ],
    )
    def test_malformed_body_is_invalid_provider_response(self, payload: Any) -> None:
        with egress_env(httpx.Response(200, json=payload)):
            with pytest.raises(MultimodalImageError) as exc:
                describe()
        assert exc.value.code == "invalid_provider_response"

    @pytest.mark.parametrize(
        "message",
        [
            {"content": ""},
            {"content": "   \n"},
            {"content": None, "refusal": "I can't help with that."},
            {},
            {"content": []},
        ],
    )
    def test_empty_description_is_no_image_content(
        self, message: dict[str, Any]
    ) -> None:
        payload = {"choices": [{"message": message}]}
        with egress_env(httpx.Response(200, json=payload)):
            with pytest.raises(MultimodalImageError) as exc:
                describe()
        assert exc.value.code == "no_image_content"

    @pytest.mark.parametrize(
        "content",
        [
            IMAGE_NO_CONTENT_MARKER,
            f"  {IMAGE_NO_CONTENT_MARKER}\n",
            f"Imagem escura. {IMAGE_NO_CONTENT_MARKER}",
        ],
    )
    def test_marker_is_no_image_content_never_a_description(self, content: str) -> None:
        with egress_env(chat_response(content)):
            with pytest.raises(MultimodalImageError) as exc:
                describe()
        assert exc.value.code == "no_image_content"

    def test_errors_never_carry_the_api_key_or_the_image(self) -> None:
        with egress_env(httpx.Response(401, json={"error": "bad key"})):
            with pytest.raises(MultimodalImageError) as exc:
                describe()
        encoded = base64.b64encode(IMAGE).decode("ascii")
        rendered = repr(exc.value) + str(exc.value) + repr(exc.value.context)
        assert API_KEY not in rendered
        assert encoded not in rendered
        assert str(exc.value) == "provider_http_error"


class TestErrorCodes:
    def test_published_codes_are_exactly_decision_11(self) -> None:
        assert IMAGE_ERROR_CODES == frozenset(
            {
                "provider_not_multimodal_capable",
                "unsupported_image_mime",
                "provider_http_error",
                "provider_transport_error",
                "invalid_provider_response",
                "no_image_content",
            }
        )

    def test_image_error_is_a_sibling_of_the_transcription_error(self) -> None:
        error = MultimodalImageError("no_image_content", kind="multimodal_openrouter")
        assert error.code == "no_image_content"
        assert error.context == {"kind": "multimodal_openrouter"}
        assert not isinstance(error, multimodal_service.MultimodalTranscriptionError)
