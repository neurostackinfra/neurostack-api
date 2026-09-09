"""Tests for OpenAI-compatible LLM endpoint handling."""

import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest

import src.llm.client as client_module
from src.llm.client import (
    LLMClient,
    LLMClientError,
    _chat_completions_url,
    get_multimodal_text_llm_client,
    get_tenant_llm_client,
)
from src.services.api_credentials import GEMINI_API_BASE_URL


def test_chat_completions_url_appends_path_to_base_url() -> None:
    assert (
        _chat_completions_url("https://api.neuralake.cloud/v1")
        == "https://api.neuralake.cloud/v1/chat/completions"
    )


def test_chat_completions_url_preserves_complete_endpoint() -> None:
    assert (
        _chat_completions_url("https://api.neuralake.cloud/v1/chat/completions/")
        == "https://api.neuralake.cloud/v1/chat/completions"
    )


def _tenant_llm_bundle(**overrides: Any) -> dict[str, Any]:
    bundle = {
        "llm_provider": "openai",
        "llm_api_url": "https://api.openai.com/v1",
        "llm_api_key": "tenant-secret",
        "llm_model_name": "gpt-model",
    }
    bundle.update(overrides)
    return bundle


def test_unscoped_none_credentials_may_use_global_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr(client_module, "get_llm_client", lambda: sentinel)
    assert get_tenant_llm_client(uuid4(), None) is sentinel


def test_explicit_empty_tenant_bundle_fails_closed_without_global_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client_module,
        "get_llm_client",
        lambda: pytest.fail("explicit empty tenant bundle used global fallback"),
    )
    monkeypatch.setattr(
        client_module,
        "LLMClient",
        lambda **_kwargs: pytest.fail("explicit empty tenant bundle built a client"),
    )
    assert get_tenant_llm_client(uuid4(), {}) is None


@pytest.mark.parametrize(
    "bundle",
    [
        {"llm_api_key": "partial"},
        _tenant_llm_bundle(llm_model_name=""),
        _tenant_llm_bundle(llm_provider="slm_cortex"),
        _tenant_llm_bundle(llm_api_url="https://api.neuralake.cloud/v1"),
        _tenant_llm_bundle(
            llm_provider="dropout", llm_api_url="https://api.openai.com/v1"
        ),
        _tenant_llm_bundle(
            llm_provider="outros", llm_api_url="https://api.openai.com/v1"
        ),
    ],
)
def test_explicit_bad_tenant_bundle_fails_closed_without_global_fallback(
    monkeypatch: pytest.MonkeyPatch, bundle: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        client_module,
        "get_llm_client",
        lambda: pytest.fail("explicit tenant bundle used global fallback"),
    )
    assert get_tenant_llm_client(uuid4(), bundle) is None


def test_valid_tenant_bundle_pins_provider_egress() -> None:
    client = get_tenant_llm_client(uuid4(), _tenant_llm_bundle())
    assert client is not None
    assert client._kind == "llm_openai"


def test_valid_gemini_tenant_bundle_pins_gemini_egress() -> None:
    client = get_tenant_llm_client(
        uuid4(),
        _tenant_llm_bundle(
            llm_provider="gemini",
            llm_api_url=GEMINI_API_BASE_URL,
            llm_model_name="gemini-2.5-flash",
        ),
    )
    assert client is not None
    assert client._kind == "llm_gemini"


@pytest.mark.asyncio
async def test_gemini_openai_compatible_transport_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [{"message": {"content": "GEMINI_SMOKE_OK"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                "model": "ignored-provider-model",
            }

    class FakeClient:
        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    @asynccontextmanager
    async def fake_safe_async_client(url: str, **kwargs: Any):
        captured["requested_url"] = url
        captured["kind"] = kwargs["kind"]
        validated = type(
            "Validated",
            (),
            {
                "url": f"{GEMINI_API_BASE_URL}/chat/completions",
                "sni_host": "generativelanguage.googleapis.com",
            },
        )()
        yield FakeClient(), validated

    monkeypatch.setattr(client_module, "safe_async_client", fake_safe_async_client)
    result = await LLMClient(GEMINI_API_BASE_URL, "gemini-secret").generate(
        [{"role": "user", "content": "fixed"}],
        model="gemini-2.5-flash",
        temperature=0.0,
        max_tokens=16,
    )

    assert captured["requested_url"] == f"{GEMINI_API_BASE_URL}/chat/completions"
    assert captured["url"] == f"{GEMINI_API_BASE_URL}/chat/completions"
    assert captured["kind"] == "llm_gemini"
    assert captured["headers"] == {
        "Authorization": "Bearer gemini-secret",
        "Content-Type": "application/json",
        "Host": "generativelanguage.googleapis.com",
    }
    assert captured["json"]["model"] == "gemini-2.5-flash"
    assert result == {
        "content": "GEMINI_SMOKE_OK",
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        "model": "gemini-2.5-flash",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
    ],
)
async def test_missing_openai_response_shape_is_invalid_provider_response(
    monkeypatch: pytest.MonkeyPatch,
    provider_payload: dict[str, Any],
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return provider_payload

    class FakeClient:
        async def post(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            return FakeResponse()

    @asynccontextmanager
    async def fake_safe_async_client(url: str, **_kwargs: Any):
        validated = type(
            "Validated",
            (),
            {"url": url, "sni_host": "generativelanguage.googleapis.com"},
        )()
        yield FakeClient(), validated

    monkeypatch.setattr(client_module, "safe_async_client", fake_safe_async_client)
    with pytest.raises(LLMClientError) as caught:
        await LLMClient(GEMINI_API_BASE_URL, "secret").generate(
            [{"role": "user", "content": "fixed"}], model="gemini-model"
        )
    assert caught.value.code == "invalid_provider_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", None, []])
async def test_structurally_valid_unusable_content_remains_empty(
    monkeypatch: pytest.MonkeyPatch,
    content: Any,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": content}}]}

    class FakeClient:
        async def post(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            return FakeResponse()

    @asynccontextmanager
    async def fake_safe_async_client(url: str, **_kwargs: Any):
        validated = type(
            "Validated",
            (),
            {"url": url, "sni_host": "generativelanguage.googleapis.com"},
        )()
        yield FakeClient(), validated

    monkeypatch.setattr(client_module, "safe_async_client", fake_safe_async_client)
    result = await LLMClient(GEMINI_API_BASE_URL, "secret").generate(
        [{"role": "user", "content": "fixed"}], model="gemini-model"
    )
    assert result["content"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["http", "transport"])
async def test_client_failures_are_static_and_redact_every_surface(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
) -> None:
    secret = "SUPER-SECRET-GEMINI-KEY"
    submitted_url = f"{GEMINI_API_BASE_URL}/chat/completions?secret-url"
    payload_secret = "PRIVATE-PROMPT-PAYLOAD"
    response_secret = "PRIVATE-RESPONSE-BODY"

    class FakeResponse:
        def raise_for_status(self) -> None:
            request = httpx.Request("POST", submitted_url)
            response = httpx.Response(500, request=request, text=response_secret)
            raise httpx.HTTPStatusError(
                f"{secret} {payload_secret} {response_secret}",
                request=request,
                response=response,
            )

    class FakeClient:
        async def post(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            if failure_kind == "transport":
                raise httpx.ConnectError(
                    f"{secret} {submitted_url} {payload_secret} {response_secret}"
                )
            return FakeResponse()

    @asynccontextmanager
    async def fake_safe_async_client(*_args: Any, **_kwargs: Any):
        validated = type(
            "Validated",
            (),
            {"url": submitted_url, "sni_host": "generativelanguage.googleapis.com"},
        )()
        yield FakeClient(), validated

    monkeypatch.setattr(client_module, "safe_async_client", fake_safe_async_client)
    with caplog.at_level(logging.DEBUG), pytest.raises(LLMClientError) as caught:
        await LLMClient(GEMINI_API_BASE_URL, secret).generate(
            [{"role": "user", "content": payload_secret}],
            model="gemini-model",
        )

    rendered = " ".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
            caplog.text,
        )
    )
    assert caught.value.code == (
        "provider_http_error" if failure_kind == "http" else "provider_transport_error"
    )
    for forbidden in (secret, submitted_url, payload_secret, response_secret):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_gpt_oss_reserves_visible_output_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {"content": "Olá!"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }

    class FakeClient:
        async def post(
            self,
            _url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> FakeResponse:
            captured.update(json)
            return FakeResponse()

    @asynccontextmanager
    async def fake_safe_async_client(*_args: Any, **_kwargs: Any):
        validated = type(
            "Validated",
            (),
            {
                "url": "https://api.groq.com/openai/v1/chat/completions",
                "sni_host": "api.groq.com",
            },
        )()
        yield FakeClient(), validated

    monkeypatch.setattr(client_module, "safe_async_client", fake_safe_async_client)

    result = await LLMClient(
        "https://api.groq.com/openai/v1",
        "secret",
    ).generate(
        [{"role": "user", "content": "olá"}],
        model="openai/gpt-oss-20b",
        max_tokens=300,
    )

    assert result["content"] == "Olá!"
    assert captured["max_completion_tokens"] == 1024
    assert captured["reasoning_effort"] == "low"
    assert captured["reasoning_format"] == "hidden"
    assert "max_tokens" not in captured
    assert "temperature" not in captured


# ============================================
# Multimodal-text fallback factory (Wave 1, deliverable 5)
# ============================================
#
# The factory is deliberately fail-closed. Every non-happy path here proves
# a specific negative case (opt-out, wrong provider, unsafe URL,
# provider↔host divergence, missing key) returns `None` WITHOUT ever
# instantiating an `LLMClient`. The happy path proves the returned client
# is pinned to the ``multimodal_openrouter`` egress kind rather than
# whatever `_llm_kind_for_url` would have derived on its own.


def _base_multimodal_creds() -> dict[str, Any]:
    return {
        "multimodal_provider": "openrouter",
        "multimodal_api_url": "https://openrouter.ai/api/v1",
        "multimodal_api_key": "SECRET-MULTIMODAL-KEY",
        "multimodal_model_name": "google/gemini-2.5-flash",
        "multimodal_text_fallback_enabled": True,
    }


def test_multimodal_factory_happy_path_pins_openrouter_egress_kind() -> None:
    client = get_multimodal_text_llm_client(uuid4(), _base_multimodal_creds())
    assert client is not None
    assert client._kind == "multimodal_openrouter"
    assert client._base_url == "https://openrouter.ai/api/v1"
    assert client._api_key == "SECRET-MULTIMODAL-KEY"


@pytest.mark.parametrize(
    "gate_value",
    [
        None,  # legacy default: gate absent
        False,  # explicit opt-out
        "true",  # truthy string never coerced to True
        1,  # truthy int never coerced to True
    ],
)
def test_multimodal_factory_refuses_without_explicit_true_gate(gate_value: Any) -> None:
    creds = _base_multimodal_creds()
    if gate_value is None:
        creds.pop("multimodal_text_fallback_enabled")
    else:
        creds["multimodal_text_fallback_enabled"] = gate_value
    assert get_multimodal_text_llm_client(uuid4(), creds) is None


@pytest.mark.parametrize(
    "bad_provider",
    ["groq", "openai", "outros", "llm_outros", "unknown", "", None],
)
def test_multimodal_factory_refuses_non_operational_or_custom_provider(
    bad_provider: Any,
) -> None:
    """`groq` / `openai` / `outros` / unknown / empty MUST NOT unlock the fallback.

    The operational allowlist (`OPERATIONAL_MULTIMODAL_PROVIDERS`) is
    intentionally narrower than the write allowlist — only `openrouter` was
    measured against WhatsApp's OGG/Opus, and the same policy applies to the
    text-only fallback so it never routes through an unproven provider.
    """
    creds = _base_multimodal_creds()
    creds["multimodal_provider"] = bad_provider
    assert get_multimodal_text_llm_client(uuid4(), creds) is None


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://api.groq.com/openai/v1",  # divergent host, right scheme
        "https://api.openai.com/v1",  # divergent host, right scheme
        "https://evil.example.com/v1",  # arbitrary public host
        "http://openrouter.ai/api/v1",  # non-HTTPS
        "https://openrouter.ai:8443/api/v1",  # non-443 port
        "https://user:pass@openrouter.ai/v1",  # userinfo
        "https://localhost/v1",  # localhost
        "",  # empty
        None,  # missing
    ],
)
def test_multimodal_factory_refuses_provider_host_divergence_and_unsafe_urls(
    bad_url: Any,
) -> None:
    creds = _base_multimodal_creds()
    creds["multimodal_api_url"] = bad_url
    assert get_multimodal_text_llm_client(uuid4(), creds) is None


@pytest.mark.parametrize(
    "missing_field",
    [
        "multimodal_provider",
        "multimodal_api_url",
        "multimodal_api_key",
        "multimodal_model_name",
    ],
)
def test_multimodal_factory_refuses_incomplete_bundle(missing_field: str) -> None:
    creds = _base_multimodal_creds()
    creds.pop(missing_field)
    assert get_multimodal_text_llm_client(uuid4(), creds) is None


def test_multimodal_factory_refuses_when_key_is_empty_string() -> None:
    creds = _base_multimodal_creds()
    creds["multimodal_api_key"] = ""
    assert get_multimodal_text_llm_client(uuid4(), creds) is None


def test_multimodal_factory_never_falls_back_to_primary_llm_key() -> None:
    """Even when a primary llm bundle is present alongside, the factory must
    not accidentally reach for it — the multimodal fallback is a SEPARATE
    credential set by design."""
    creds = _base_multimodal_creds()
    creds.pop("multimodal_api_key")
    creds["llm_api_key"] = "PRIMARY-LLM-KEY-MUST-NOT-BE-USED"
    creds["llm_api_url"] = "https://api.openai.com/v1"
    assert get_multimodal_text_llm_client(uuid4(), creds) is None


def test_llm_client_egress_kind_override_takes_precedence_over_url_heuristic() -> None:
    """The optional `egress_kind` parameter is the mechanism the multimodal
    factory uses to pin an egress label the URL heuristic would never emit.
    Without the override the OpenRouter URL would resolve to `llm_outros`
    (the fallback for anything not in the OpenAI/Dropout host allowlist)."""

    default = LLMClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="k",
    )
    assert default._kind == "llm_outros"

    pinned = LLMClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="k",
        egress_kind="multimodal_openrouter",
    )
    assert pinned._kind == "multimodal_openrouter"
