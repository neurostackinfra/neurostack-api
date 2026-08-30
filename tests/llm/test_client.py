"""Tests for OpenAI-compatible LLM endpoint handling."""

from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest

import src.llm.client as client_module
from src.llm.client import (
    LLMClient,
    _chat_completions_url,
    get_multimodal_text_llm_client,
)


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
