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
from src.services.api_credentials import OPERATIONAL_MULTIMODAL_PROVIDERS
from src.services.multimodal_service import multimodal_kind_for_url
from src.services.safe_egress import (
    LLM_DROPOUT_HOSTS,
    LLM_OPENAI_HOSTS,
    UnsafeEgressError,
    parse_public_https_url,
    safe_async_client,
)

logger = get_logger(__name__)

_client: Optional["LLMClient"] = None


def _chat_completions_url(base_url: str) -> str:
    """Accept either an API base URL or the complete chat endpoint."""

    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


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
        *,
        egress_kind: Optional[str] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        # Egress kind is derived from the URL for the ordinary LLM path, but
        # the multimodal fallback factory needs to *pin* a specific
        # safe-egress policy label (``multimodal_openrouter``) that the LLM
        # URL heuristics would never emit on their own. Passing it in keeps
        # the transport selection under the factory's control instead of
        # tacking a second URL classifier onto the LLM shape.
        self._kind = egress_kind or _llm_kind_for_url(self._base_url)

    # Reasoning models use max_completion_tokens and don't accept temperature
    REASONING_PREFIXES = ("o1", "o3", "o4")
    GPT_OSS_PREFIXES = ("openai/gpt-oss-",)

    def _is_reasoning_model(self, model: str) -> bool:
        return any(
            model.startswith(prefix)
            for prefix in (*self.REASONING_PREFIXES, *self.GPT_OSS_PREFIXES)
        )

    def _is_gpt_oss_model(self, model: str) -> bool:
        return any(model.startswith(prefix) for prefix in self.GPT_OSS_PREFIXES)

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
            is_gpt_oss = self._is_gpt_oss_model(effective_model)
            # GPT-OSS includes hidden reasoning in the completion budget. The
            # pipeline's concise 300-token response limit can therefore be
            # exhausted before the model emits any visible content.
            payload["max_completion_tokens"] = (
                max(tokens, 1024) if is_gpt_oss else tokens
            )
            if is_gpt_oss:
                payload["reasoning_effort"] = "low"
                payload["reasoning_format"] = "hidden"
        else:
            payload["temperature"] = (
                temperature if temperature is not None else settings.llm_temperature
            )
            payload["max_tokens"] = tokens

        logger.debug("LLM request", model=effective_model, messages_count=len(messages))
        target_url = _chat_completions_url(self._base_url)
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


# Multimodal-text bundle field names — replicated locally instead of imported
# so the factory documents its own contract (four provider fields + gate) and
# stays decoupled from the credential schema's frozenset iteration order.
_MULTIMODAL_BUNDLE_FIELDS: tuple[str, str, str, str] = (
    "multimodal_provider",
    "multimodal_api_url",
    "multimodal_api_key",
    "multimodal_model_name",
)


def get_multimodal_text_llm_client(
    tenant_id: Any, api_credentials: dict[str, Any]
) -> Optional[LLMClient]:
    """Build the fail-closed multimodal-text fallback LLM client.

    Returns ``None`` (and never raises) whenever any precondition fails, so
    the hot path can fall through to the primary provider without leaking a
    secret through an exception. The order of the checks is deliberate — the
    cheapest, secret-free ones fail first:

    1. Opt-in gate (``multimodal_text_fallback_enabled``) MUST be exactly
       ``True``. A missing gate is legacy-default disabled.
    2. Provider MUST be in :data:`OPERATIONAL_MULTIMODAL_PROVIDERS`
       (currently only ``openrouter``), which is the same allowlist the
       audio adapter enforces. ``groq`` / ``openai`` / ``outros`` / custom
       are refused before touching the key.
    3. URL MUST resolve through ``multimodal_kind_for_url`` to
       ``multimodal_openrouter``. This closes the provider/host divergence
       case (openrouter provider but a groq/openai URL) at the egress
       boundary rather than at request time.
    4. The complete four-field bundle MUST be populated (provider, URL, key,
       model). We only look up the API key AFTER (1)-(3) pass so the
       preceding negatives never reach into secret material.
    """

    gate = api_credentials.get("multimodal_text_fallback_enabled")
    if gate is not True:
        return None

    provider = api_credentials.get("multimodal_provider")
    if provider not in OPERATIONAL_MULTIMODAL_PROVIDERS:
        return None

    base_url = api_credentials.get("multimodal_api_url")
    if not isinstance(base_url, str) or not base_url:
        return None
    try:
        kind = multimodal_kind_for_url(base_url)
    except UnsafeEgressError:
        return None
    if kind != "multimodal_openrouter":
        return None

    # Provider/host consistency: the wire provider says ``openrouter`` and
    # the URL resolves to the OpenRouter host allowlist. Explicit check so a
    # future OperationalMultimodalProviders widening cannot silently unlock a
    # divergent pairing.
    if provider != "openrouter":
        return None

    # Only now do we look at the key/model — after every non-secret check has
    # already passed, so a refusal never had to inspect secret material.
    for field in _MULTIMODAL_BUNDLE_FIELDS:
        value = api_credentials.get(field)
        if not isinstance(value, str) or not value:
            return None

    logger.debug(
        "Creating per-tenant multimodal-text LLM client",
        tenant_id=str(tenant_id),
        base_url=base_url,
    )
    return LLMClient(
        base_url=base_url,
        api_key=api_credentials["multimodal_api_key"],
        egress_kind="multimodal_openrouter",
    )
