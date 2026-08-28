"""Tests for OpenAI-compatible LLM endpoint handling."""

from contextlib import asynccontextmanager
from typing import Any

import pytest

import src.llm.client as client_module
from src.llm.client import LLMClient, _chat_completions_url


def test_chat_completions_url_appends_path_to_base_url() -> None:
    assert (
        _chat_completions_url("https://api.neuralake.cloud/v1")
        == "https://api.neuralake.cloud/v1/chat/completions"
    )


def test_chat_completions_url_preserves_complete_endpoint() -> None:
    assert (
        _chat_completions_url(
            "https://api.neuralake.cloud/v1/chat/completions/"
        )
        == "https://api.neuralake.cloud/v1/chat/completions"
    )


@pytest.mark.asyncio
async def test_gpt_oss_reserves_visible_output_budget(monkeypatch: pytest.MonkeyPatch) -> None:
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
