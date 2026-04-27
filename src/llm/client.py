"""
NeuroStack - LLM Client
Async client for OpenAI-compatible chat completion APIs.
"""

from typing import Optional

import httpx

from src.config.logging import get_logger
from src.config.settings import settings

logger = get_logger(__name__)

_client: Optional["LLMClient"] = None


class LLMClient:
    """Async LLM client for OpenAI-compatible APIs."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    # Reasoning models use max_completion_tokens and don't accept temperature
    REASONING_PREFIXES = ("o1", "o3", "o4")

    def _is_reasoning_model(self, model: str) -> bool:
        return any(model.startswith(p) for p in self.REASONING_PREFIXES)

    async def generate(
        self,
        messages: list[dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Call the chat completions endpoint and return the assistant message.

        Automatically adapts parameters for reasoning models (o1/o3/o4)
        which use max_completion_tokens instead of max_tokens and
        don't accept temperature.
        """
        effective_model = model or settings.llm_model
        tokens = max_tokens or settings.llm_max_tokens

        payload: dict = {
            "model": effective_model,
            "messages": messages,
        }

        if self._is_reasoning_model(effective_model):
            payload["max_completion_tokens"] = tokens
        else:
            payload["temperature"] = temperature if temperature is not None else settings.llm_temperature
            payload["max_tokens"] = tokens

        resp = await self._http.post("/chat/completions", json=payload)
        resp.raise_for_status()

        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def close(self):
        await self._http.aclose()


def get_llm_client() -> Optional[LLMClient]:
    """
    Get or create the singleton LLM client.

    Returns None if LLM is not configured (no API key).
    """
    global _client

    if not settings.llm_api_key:
        return None

    if _client is None:
        _client = LLMClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )
        logger.info(
            "LLM client initialized",
            base_url=settings.llm_base_url,
            model=settings.llm_model,
        )

    return _client
