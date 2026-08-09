"""
NeuroStack - LLM Client
Async client for OpenAI-compatible chat completion APIs.

All outbound HTTP goes through :mod:`src.services.safe_egress`, so every
request is pinned to a validated public HTTPS destination (SSRF-safe) and
never follows a redirect. Provider pinning is derived from the caller's
``base_url``: exact host allowlists for OpenAI/Dropout, custom-public
validation for the ``outros`` provider.
"""

from typing import Any, Optional

import httpx

from src.config.logging import get_logger
from src.config.settings import settings
from src.services.safe_egress import (
    LLM_DROPOUT_HOSTS,
    LLM_OPENAI_HOSTS,
    UnsafeEgressError,
    parse_public_https_url,
    safe_async_client,
)

logger = get_logger(__name__)

_client: Optional["LLMClient"] = None


def _llm_kind_for_url(base_url: str) -> str:
    """Return the safe-egress kind label for a given LLM base URL.

    OpenAI/Dropout have pinned host allowlists; everything else (the
    ``outros`` provider) falls back to the custom-public policy.
    """

    try:
        parsed = parse_public_https_url(base_url)
    except UnsafeEgressError:
        # Delegate the failure to the transport call so the caller sees
        # the same UnsafeEgressError as any downstream validation.
        return "llm_outros"
    if parsed.hostname in LLM_OPENAI_HOSTS:
        return "llm_openai"
    if parsed.hostname in LLM_DROPOUT_HOSTS:
        return "llm_dropout"
    return "llm_outros"


class LLMClient:
    """Async LLM client for OpenAI-compatible APIs.

    Constructs one validated safe transport per outbound call. httpx
    ``AsyncClient`` is deliberately created per call rather than pooled,
    which mirrors the existing per-tenant pattern and avoids keeping a
    live socket to a possibly rotated provider URL.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._kind = _llm_kind_for_url(self._base_url)

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
    ) -> dict:
        """
        Call the chat completions endpoint and return a dict with content, usage, and model.

        Automatically adapts parameters for reasoning models (o1/o3/o4)
        which use max_completion_tokens instead of max_tokens and
        don't accept temperature.

        Returns:
            dict with keys: content (str), usage (dict), model (str)
        """
        effective_model = model or settings.llm_model
        tokens = max_tokens or settings.llm_max_tokens

        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": messages,
        }

        if self._is_reasoning_model(effective_model):
            payload["max_completion_tokens"] = tokens
        else:
            payload["temperature"] = (
                temperature if temperature is not None else settings.llm_temperature
            )
            payload["max_tokens"] = tokens

        logger.debug("LLM request", model=effective_model, messages_count=len(messages))
        target_url = f"{self._base_url}/chat/completions"
        async with safe_async_client(
            target_url,
            kind=self._kind,
            timeout=httpx.Timeout(self._timeout, connect=10.0),
        ) as (client, validated):
            resp = await client.post(
                validated.url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Host": validated.sni_host,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content")
        finish = choice.get("finish_reason")
        usage = data.get("usage", {})
        reasoning_tokens = usage.get("completion_tokens_details", {}).get(
            "reasoning_tokens", 0
        )

        if not content:
            logger.warning(
                "LLM returned empty content",
                model=effective_model,
                finish_reason=finish,
                reasoning_tokens=reasoning_tokens,
                max_completion_tokens=tokens,
            )
            return {"content": "", "usage": usage, "model": effective_model}
        return {"content": content, "usage": usage, "model": effective_model}

    async def close(self) -> None:
        """Compatibility no-op; every call uses a per-request client."""

        return None


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


def get_tenant_llm_client(tenant_id: Any, api_credentials: dict) -> Optional[LLMClient]:
    """
    Create an LLM client for a specific tenant using their saved API credentials.

    If the tenant has no llm_api_key configured, falls back to the global client.
    Creates a new client on each call (safe-egress transports are per-call).
    """
    api_key = api_credentials.get("llm_api_key")
    if not api_key:
        return get_llm_client()

    base_url = api_credentials.get("llm_api_url") or settings.llm_base_url
    logger.debug(
        "Creating per-tenant LLM client",
        tenant_id=str(tenant_id),
        base_url=base_url,
    )
    return LLMClient(base_url=base_url, api_key=api_key)
