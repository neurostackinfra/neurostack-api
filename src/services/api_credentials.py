"""Pure policy for API-provider credentials.

This module deliberately has no FastAPI, database, Redis, or logging imports.
It is the single credential contract used by public routes and runtime callers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

MASKED_SECRET = "••••••••"
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
STT_FIXED_API_BASES: dict[str, str] = {
    "elevenlabs_scribe": "https://api.elevenlabs.io/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

ProviderAvailability = Literal["active", "coming_soon"]

# This is the public credential-write contract.  Runtime support is deliberately
# not evidence of public availability: every displayable provider remains
# fail-closed until a direct homologation receipt promotes it in a later wish.
PROVIDER_AVAILABILITY: dict[str, dict[str, ProviderAvailability]] = {
    "whatsapp": {
        "meta_cloud": "coming_soon",
        "zapi": "coming_soon",
        "uazapi": "coming_soon",
        "evolution": "coming_soon",
        "dialog360": "coming_soon",
        "twilio": "coming_soon",
    },
    "llm": {
        "dropout": "coming_soon",
        "gemini": "active",
        "openai": "coming_soon",
        "outros": "coming_soon",
        "slm_cortex": "coming_soon",
    },
    "multimodal": {"openrouter": "coming_soon"},
    "stt": {
        "outros": "coming_soon",
        "openai_whisper": "coming_soon",
        "elevenlabs_scribe": "coming_soon",
        "google_stt": "coming_soon",
        "deepgram_stt": "coming_soon",
        "microsoft_stt": "coming_soon",
    },
    "tts": {
        "elevenlabs": "coming_soon",
        "google": "coming_soon",
        "openai": "coming_soon",
    },
}

# The allowlist remains the request-universe boundary.  Deriving it from the
# availability matrix makes the displayable provider pairs impossible to drift.
PROVIDER_ALLOWLISTS: dict[str, frozenset[str]] = {
    capability: frozenset(providers)
    for capability, providers in PROVIDER_AVAILABILITY.items()
}
# Recognition is an internal wire/runtime concern, not public catalog state.
# OpenRouter deliberately remains absent from ``PROVIDER_AVAILABILITY`` until
# the catalog wish owns that public promotion.
PROVIDER_ALLOWLISTS["stt"] = PROVIDER_ALLOWLISTS["stt"] | {"openrouter"}

# Public product activation is deliberately narrower than legacy runtime
# compatibility used to read already-stored tenant configurations.
PUBLICLY_ACTIVE_LLM_PROVIDERS = frozenset({"gemini"})
OPERATIONAL_LLM_PROVIDERS = frozenset({"openai", "dropout", "gemini", "outros"})
OPERATIONAL_STT_PROVIDERS = frozenset(
    {"outros", "openai_whisper", "elevenlabs_scribe", "openrouter"}
)
# Multimodal providers whose chat-completions endpoint was measured against
# the live API and proven to transcribe the OGG/Opus that WhatsApp actually
# delivers. Evidence:
# `.genie/wishes/fallback-texto/evidence/multimodal-format-probe.md`.
# `groq` rejects the `input_audio` content block outright; `openai` only
# accepts `wav`/`mp3` and refuses OGG both by enum and by sniffing, so
# neither can serve a voice note without transcoding.
OPERATIONAL_MULTIMODAL_PROVIDERS = frozenset({"openrouter"})
READINESS_IDS = frozenset(
    {"active", "incomplete", "signature_pending", "configuration_only"}
)
WIRE_ERROR_CODES = frozenset(
    {
        "credential_endpoint_required",
        "credentials_jwt_required",
        "credentials_role_forbidden",
        "invalid_credentials_payload",
        "invalid_provider_endpoint",
        "invalid_provider_for_capability",
        "provider_switch_bundle_required",
        "provider_not_available",
        "unsafe_api_url",
    }
)

WHATSAPP_FIELDS = frozenset(
    {
        "whatsapp_provider",
        "zapi_instance_id",
        "zapi_token",
        "zapi_client_token",
        "zapi_inbound_token_digest",
        "evolution_api_url",
        "evolution_instance_name",
        "evolution_api_key",
        "meta_phone_number_id",
        "meta_waba_id",
        "meta_permanent_token",
        "meta_app_secret",
        "meta_verify_token",
        "uazapi_api_url",
        "uazapi_instance_name",
        "uazapi_token",
    }
)
LLM_FIELDS = frozenset({"llm_provider", "llm_api_url", "llm_api_key", "llm_model_name"})
MULTIMODAL_FIELDS = frozenset(
    {
        "multimodal_provider",
        "multimodal_api_url",
        "multimodal_api_key",
        "multimodal_model_name",
        # HTTP opt-in gate for the text-only multimodal fallback. Non-secret
        # (default `false` legacy, must round-trip verbatim through the GET
        # projection alongside the rest of the non-secret credential fields).
        "multimodal_text_fallback_enabled",
    }
)
STT_FIELDS = frozenset(
    {
        "stt_provider",
        "stt_api_url",
        "stt_api_key",
        "stt_model_name",
        "whisper_api_key",
        "whisper_model",
        "audio_max_seconds",
    }
)
TTS_FIELDS = frozenset(
    {
        "tts_provider",
        "tts_api_url",
        "tts_api_key",
        "tts_model_name",
        "tts_voice_id",
        "tts_enabled",
        "tts_reply_policy",
    }
)
API_CREDENTIAL_FIELDS = frozenset(
    WHATSAPP_FIELDS | LLM_FIELDS | MULTIMODAL_FIELDS | STT_FIELDS | TTS_FIELDS
)
CAPABILITY_CREDENTIAL_FIELDS: dict[str, frozenset[str]] = {
    "whatsapp": WHATSAPP_FIELDS,
    "llm": LLM_FIELDS,
    "multimodal": MULTIMODAL_FIELDS,
    "stt": STT_FIELDS,
    "tts": TTS_FIELDS,
}
CAPABILITY_PROVIDER_FIELDS: dict[str, str] = {
    "whatsapp": "whatsapp_provider",
    "llm": "llm_provider",
    "multimodal": "multimodal_provider",
    "stt": "stt_provider",
    "tts": "tts_provider",
}
LEGACY_CREDENTIAL_ALIASES = frozenset(
    {"llm_base_url", "whisper_api_key", "whisper_model"}
)
CREDENTIAL_PREFIXES = (
    "whatsapp_",
    "zapi_",
    "evolution_",
    "meta_",
    "uazapi_",
    "dialog360_",
    "twilio_",
    "llm_",
    "multimodal_",
    "stt_",
    "tts_",
    "whisper_",
)

# Generic clinic configuration fields that happen to share a provider prefix.
# Keep this allowlist explicit: the broad prefix checks below still close the
# boundary against future credential aliases without blocking legitimate data.
GENERIC_CONFIG_PREFIX_EXCEPTIONS = frozenset({"meta_minutos_atendimento"})

SECRET_FIELDS = frozenset(
    {
        "zapi_token",
        "zapi_client_token",
        "zapi_inbound_token_digest",
        "evolution_api_key",
        "meta_permanent_token",
        "meta_app_secret",
        "meta_verify_token",
        "uazapi_token",
        "llm_api_key",
        "multimodal_api_key",
        "stt_api_key",
        "whisper_api_key",
        "tts_api_key",
    }
)


class CredentialPolicyError(ValueError):
    """Stable, secret-free domain error translated by the HTTP boundary."""

    def __init__(self, error: str, **context: Any) -> None:
        self.detail: dict[str, Any] = {"error": error, **context}
        super().__init__(error)


def is_masked_secret(value: Any) -> bool:
    """Whether a submitted value is a UI mask rather than fresh key material."""

    return isinstance(value, str) and ("••••" in value or "****" in value)


def is_fresh_secret(value: Any) -> bool:
    """Whether a submitted secret replaces stored secret material."""

    return isinstance(value, str) and bool(value) and not is_masked_secret(value)


def credential_boundary_fields(fields: Mapping[str, Any]) -> list[str]:
    """Return sorted generic-config fields that belong to the credential boundary."""

    return sorted(
        field
        for field in fields
        if field not in GENERIC_CONFIG_PREFIX_EXCEPTIONS
        and (
            field in API_CREDENTIAL_FIELDS
            or field in LEGACY_CREDENTIAL_ALIASES
            or field == "api_credentials"
            or field.startswith(CREDENTIAL_PREFIXES)
        )
    )


def validate_provider(capability: str, provider: Any) -> None:
    """Validate a wire provider id without leaking any secret field."""

    if provider is None:
        return
    if not isinstance(provider, str) or provider not in PROVIDER_ALLOWLISTS[capability]:
        raise CredentialPolicyError(
            "invalid_provider_for_capability",
            capability=capability,
        )


def validate_gemini_endpoint(value: Any) -> None:
    """Require the immutable Gemini OpenAI-compatible API base.

    The comparison is deliberately byte-for-byte after a defensive parse.
    That excludes explicit ports (including ``:443``), userinfo, alternate
    hosts or paths, queries, and fragments. Errors carry only stable policy
    identifiers; the submitted value is never attached to the exception.
    """

    if not isinstance(value, str):
        raise CredentialPolicyError(
            "invalid_provider_endpoint",
            capability="llm",
            provider="gemini",
            field="llm_api_url",
        )
    try:
        parsed = urlsplit(value)
        explicit_port = parsed.port is not None
    except ValueError:
        raise CredentialPolicyError(
            "invalid_provider_endpoint",
            capability="llm",
            provider="gemini",
            field="llm_api_url",
        ) from None
    if explicit_port or value != GEMINI_API_BASE_URL:
        raise CredentialPolicyError(
            "invalid_provider_endpoint",
            capability="llm",
            provider="gemini",
            field="llm_api_url",
        )


def validate_llm_provider_endpoint(credentials: Mapping[str, Any]) -> None:
    """Validate provider-specific endpoint invariants on a complete candidate."""

    if credentials.get("llm_provider") == "gemini":
        validate_gemini_endpoint(credentials.get("llm_api_url"))


def validate_stt_provider_endpoint(provider: Any, value: Any) -> None:
    """Require the immutable base for an explicit Scribe/OpenRouter provider."""

    expected = STT_FIXED_API_BASES.get(provider)
    if expected is None:
        return
    if not isinstance(value, str):
        raise CredentialPolicyError(
            "invalid_provider_endpoint",
            capability="stt",
            provider=provider,
            field="stt_api_url",
        )
    try:
        parsed = urlsplit(value)
        explicit_port = parsed.port is not None
    except ValueError:
        explicit_port = True
    if explicit_port or value != expected:
        raise CredentialPolicyError(
            "invalid_provider_endpoint",
            capability="stt",
            provider=provider,
            field="stt_api_url",
        )


def _validate_llm_provider_switch(
    existing: Mapping[str, Any], patch: Mapping[str, Any]
) -> None:
    """Require a fresh atomic four-field bundle for every provider switch."""

    existing_provider = existing.get("llm_provider")
    patched_provider = patch.get("llm_provider")
    endpoint_switches_into_gemini = (
        existing_provider != "gemini"
        and patch.get("llm_api_url") == GEMINI_API_BASE_URL
    )
    endpoint_switches_out_of_gemini = (
        existing_provider == "gemini"
        and "llm_api_url" in patch
        and patch.get("llm_api_url") != GEMINI_API_BASE_URL
    )
    provider_switches_gemini = (
        "llm_provider" in patch
        and patched_provider != existing_provider
        and "gemini" in {existing_provider, patched_provider}
    )
    if not (
        provider_switches_gemini
        or endpoint_switches_into_gemini
        or endpoint_switches_out_of_gemini
    ):
        return
    complete = all(
        field in patch and isinstance(patch.get(field), str) and bool(patch.get(field))
        for field in LLM_OVERLAY_FIELDS
    )
    if not complete or not is_fresh_secret(patch.get("llm_api_key")):
        raise CredentialPolicyError("provider_switch_bundle_required", capability="llm")


def merge_credentials(
    existing: Mapping[str, Any], patch: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate and atomically merge one flat credential patch."""

    unknown = sorted(set(patch) - API_CREDENTIAL_FIELDS)
    if unknown:
        raise CredentialPolicyError(
            "invalid_credentials_payload",
            fields=[{"field": field, "code": "extra_forbidden"} for field in unknown],
        )
    for capability, provider_field in CAPABILITY_PROVIDER_FIELDS.items():
        if provider_field in patch:
            validate_provider(capability, patch[provider_field])
    _validate_llm_provider_switch(existing, patch)

    merged = dict(existing)
    for field, value in patch.items():
        if field in SECRET_FIELDS and (
            value is None or value == "" or is_masked_secret(value)
        ):
            continue
        merged[field] = value
    if "stt_api_key" in patch and is_fresh_secret(patch["stt_api_key"]):
        merged.pop("whisper_api_key", None)
    if "stt_model_name" in patch:
        merged.pop("whisper_model", None)
    validate_llm_provider_endpoint(merged)
    return merged


def _tts_required_fields(provider: Any) -> tuple[str, ...]:
    base = ("tts_provider", "tts_api_url", "tts_api_key", "tts_model_name")
    # ElevenLabs and OpenAI pick a voice at synthesis time, so their bundles
    # are one field wider.
    return base + ("tts_voice_id",) if provider in {"elevenlabs", "openai"} else base


def _tts_bundle_complete(credentials: Mapping[str, Any]) -> bool:
    provider = credentials.get("tts_provider")
    if provider not in PROVIDER_ALLOWLISTS["tts"]:
        return False
    return all(bool(credentials.get(field)) for field in _tts_required_fields(provider))


def validate_provider_availability(
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    patch: Mapping[str, Any] | None = None,
    resolved_existing: Mapping[str, Any] | None = None,
) -> None:
    """Reject an effective write whose resulting provider is not available.

    This runs strictly after :func:`merge_credentials`.  Comparing the merged
    candidate with the stored values makes a masked, normalized legacy echo a
    no-op, while any resulting provider capability mutation stays atomic at
    the HTTP boundary.

    Exact legacy echoes produce no changed fields and therefore remain valid.
    Runtime support and stored incumbency do not bypass the public availability
    matrix. Runtime controls are capability mutations and pass through the same
    fail-closed gate as credential fields.
    """

    for capability, providers in PROVIDER_AVAILABILITY.items():
        provider_field = CAPABILITY_PROVIDER_FIELDS[capability]
        changed_fields = {
            field
            for field in CAPABILITY_CREDENTIAL_FIELDS[capability]
            if candidate.get(field) != existing.get(field)
        }
        if not changed_fields:
            continue
        provider = candidate.get(provider_field)
        if capability == "stt" and patch is not None:
            resolved = (
                resolved_existing
                if resolved_existing is not None
                else resolve_stt_credentials(existing)
            )
            if _validate_stt_incumbent_transition(existing, resolved, patch):
                continue
            if provider is None:
                provider = resolved.get("stt_provider")
        if isinstance(provider, str) and providers.get(provider) == "active":
            continue
        raise CredentialPolicyError(
            "provider_not_available",
            capability=capability,
            provider=provider,
        )


def resolve_stt_credentials(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Return canonical-first STT fields while retaining unrelated credentials."""

    resolved = dict(credentials)
    if not resolved.get("stt_api_key") and resolved.get("whisper_api_key"):
        resolved["stt_api_key"] = resolved["whisper_api_key"]
    if not resolved.get("stt_model_name") and resolved.get("whisper_model"):
        resolved["stt_model_name"] = resolved["whisper_model"]
    has_stt_configuration = any(
        resolved.get(field) not in (None, "")
        for field in (
            "stt_provider",
            "stt_api_url",
            "stt_api_key",
            "stt_model_name",
            "whisper_api_key",
            "whisper_model",
        )
    )
    if has_stt_configuration:
        if not resolved.get("stt_provider"):
            resolved["stt_provider"] = "outros"
        if resolved.get("audio_max_seconds") is None:
            resolved["audio_max_seconds"] = 60
    return resolved


def _raw_has_effective_stt_configuration(existing: Mapping[str, Any]) -> bool:
    return any(
        existing.get(field) not in (None, "")
        for field in (
            "stt_api_url",
            "stt_api_key",
            "stt_model_name",
            "whisper_api_key",
            "whisper_model",
        )
    )


def _eligible_legacy_stt_incumbent(
    existing: Mapping[str, Any], resolved_existing: Mapping[str, Any]
) -> bool:
    """Classify the only legacy state allowed to leave ``outros``."""

    if resolved_existing.get("stt_provider") != "outros":
        return False
    raw_provider = existing.get("stt_provider")
    if raw_provider == "outros":
        return True
    return not raw_provider and _raw_has_effective_stt_configuration(existing)


def is_semantic_credentials_echo(
    existing: Mapping[str, Any],
    resolved_existing: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> bool:
    """Detect an exact incumbent echo without rewriting its raw JSON mapping."""

    if not patch:
        return True

    touched_capabilities = {
        capability
        for capability, fields in CAPABILITY_CREDENTIAL_FIELDS.items()
        if set(patch) & fields
    }
    for capability in touched_capabilities:
        provider_field = CAPABILITY_PROVIDER_FIELDS[capability]
        current = (
            resolved_existing.get(provider_field)
            if capability == "stt"
            else existing.get(provider_field)
        )
        if current not in PROVIDER_ALLOWLISTS[capability]:
            return False

    for field, value in patch.items():
        current = (
            resolved_existing.get(field) if field in STT_FIELDS else existing.get(field)
        )
        if field in SECRET_FIELDS and is_masked_secret(value):
            if not current:
                return False
            continue
        if value != current:
            return False
    return True


def _validate_stt_incumbent_transition(
    existing: Mapping[str, Any],
    resolved_existing: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> bool:
    """Validate and identify the one compatibility-only STT provider switch."""

    target_provider = patch.get("stt_provider")
    if target_provider not in STT_FIXED_API_BASES:
        return False
    if not _eligible_legacy_stt_incumbent(existing, resolved_existing):
        return False

    required = ("stt_provider", "stt_api_url", "stt_api_key", "stt_model_name")
    complete = all(
        field in patch
        and isinstance(patch.get(field), str)
        and bool(str(patch.get(field)).strip())
        for field in required
    )
    if not complete or not is_fresh_secret(patch.get("stt_api_key")):
        raise CredentialPolicyError("provider_switch_bundle_required", capability="stt")
    validate_stt_provider_endpoint(target_provider, patch.get("stt_api_url"))
    return True


LLM_OVERLAY_FIELDS: tuple[str, str, str, str] = (
    "llm_provider",
    "llm_api_url",
    "llm_api_key",
    "llm_model_name",
)


def _llm_bundle_complete(candidate: Mapping[str, Any] | None) -> bool:
    if not candidate:
        return False
    if candidate.get("llm_provider") not in OPERATIONAL_LLM_PROVIDERS:
        return False
    return all(candidate.get(field) for field in LLM_OVERLAY_FIELDS[1:])


def compose_llm_credentials(
    clinic: Mapping[str, Any], builder: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Use explicit clinic ownership, otherwise inherit one complete builder bundle."""

    result = dict(clinic)
    # Presence is authoritative even for null, invalid, incomplete, or
    # coming-soon values. A clinic that explicitly chose a provider must
    # never receive another tenant's secret-bearing bundle as a fallback.
    if "llm_provider" in clinic:
        return result
    if builder is not None and _llm_bundle_complete(builder):
        for field in LLM_OVERLAY_FIELDS:
            result[field] = builder[field]
    return result


# ============================================
# Cache v2 marker and payload wrapping
# ============================================

CONFIG_CACHE_SCHEMA_VERSION = 2
CONFIG_CACHE_VERSION_FIELD = "config_cache_schema_version"
CONFIG_CACHE_KEY_PREFIX = "neurostack:clinic_config"


def config_cache_key(tenant_id: Any) -> str:
    """Deterministic cache key for a tenant configuration entry."""

    return f"{CONFIG_CACHE_KEY_PREFIX}:{tenant_id}"


def is_cache_v2_payload(payload: Any) -> bool:
    """Return True only when the payload carries the current schema marker."""

    if not isinstance(payload, Mapping):
        return False
    return payload.get(CONFIG_CACHE_VERSION_FIELD) == CONFIG_CACHE_SCHEMA_VERSION


def build_cache_payload(config: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a clinic config snapshot with the v2 marker and projected creds."""

    raw_credentials = config.get("api_credentials") or {}
    projected = runtime_projection(raw_credentials)
    payload = {key: value for key, value in config.items() if key != "api_credentials"}
    payload["api_credentials"] = projected
    payload[CONFIG_CACHE_VERSION_FIELD] = CONFIG_CACHE_SCHEMA_VERSION
    return payload


def mask_secret(value: Any) -> str | None:
    """Return a constant mask so response length never reveals secret length."""

    return MASKED_SECRET if isinstance(value, str) and bool(value) else None


def _complete(credentials: Mapping[str, Any], fields: tuple[str, ...]) -> bool:
    return all(bool(credentials.get(field)) for field in fields)


def _whatsapp_readiness(credentials: Mapping[str, Any]) -> str:
    provider = credentials.get("whatsapp_provider")
    if provider == "meta_cloud":
        identity_ready = _complete(
            credentials, ("meta_phone_number_id", "meta_permanent_token")
        )
        if identity_ready and not credentials.get("meta_app_secret"):
            return "signature_pending"
        return (
            "active"
            if identity_ready and credentials.get("meta_app_secret")
            else "incomplete"
        )
    requirements = {
        "zapi": ("zapi_instance_id", "zapi_token", "zapi_inbound_token_digest"),
        "evolution": (
            "evolution_api_url",
            "evolution_instance_name",
            "evolution_api_key",
        ),
        "uazapi": ("uazapi_api_url", "uazapi_instance_name", "uazapi_token"),
    }
    fields = requirements.get(str(provider))
    return "active" if fields and _complete(credentials, fields) else "incomplete"


def credential_status(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Build the secret-free public GET/PATCH response contract."""

    resolved = resolve_stt_credentials(credentials)
    result: dict[str, Any] = {}
    for field in sorted(API_CREDENTIAL_FIELDS - SECRET_FIELDS):
        if field in {"whisper_model"}:
            continue
        value = resolved.get(field)
        # Legacy default: tenants that never opted in must see the fallback
        # gate as an explicit `false` rather than `null`, so the UI can bind
        # a boolean control directly and the wire contract is unambiguous.
        if field == "multimodal_text_fallback_enabled" and value is None:
            value = False
        result[field] = value
    if "tts_reply_policy" not in credentials:
        # Legacy rows predate the explicit trigger policy. Their behavior was
        # audio-in/audio-out, so expose that compatibility default without
        # mutating persisted JSON.
        result["tts_reply_policy"] = "audio_input"
    for field in sorted(SECRET_FIELDS):
        public_name = (
            "zapi_inbound_token" if field == "zapi_inbound_token_digest" else field
        )
        secret_value = (
            resolved.get("stt_api_key")
            if field == "stt_api_key"
            else credentials.get(field)
        )
        result[f"{public_name}_set"] = bool(secret_value)
        if field != "zapi_inbound_token_digest":
            result[f"{public_name}_masked"] = mask_secret(secret_value)

    llm_ready = credentials.get(
        "llm_provider"
    ) in OPERATIONAL_LLM_PROVIDERS and _complete(
        credentials, ("llm_api_url", "llm_api_key", "llm_model_name")
    )
    multimodal_provider = credentials.get("multimodal_provider")
    multimodal_complete = _complete(
        credentials,
        (
            "multimodal_provider",
            "multimodal_api_url",
            "multimodal_api_key",
            "multimodal_model_name",
        ),
    )
    stt_provider = resolved.get("stt_provider", "outros")
    stt_complete = _complete(resolved, ("stt_api_url", "stt_api_key", "stt_model_name"))
    tts_complete = _tts_bundle_complete(credentials)
    whatsapp_readiness = _whatsapp_readiness(credentials)
    result.update(
        {
            "audio_max_seconds": resolved.get("audio_max_seconds", 60),
            "llm_base_url": resolved.get("llm_api_url"),
            "whisper_api_key_set": bool(resolved.get("stt_api_key")),
            "whisper_api_key_masked": mask_secret(resolved.get("stt_api_key")),
            "whisper_model": resolved.get("stt_model_name"),
            "whatsapp_readiness": whatsapp_readiness,
            "llm_readiness": "active" if llm_ready else "incomplete",
            # A complete bundle is only `active` when the selected provider was
            # measured against the live API and proven to transcribe WhatsApp's
            # OGG/Opus. Everything else stays `configuration_only`: the write
            # allowlist is deliberately wider than the operational set, so
            # announcing `active` there would promise a runtime the multimodal
            # adapter cannot deliver.
            "multimodal_readiness": (
                "active"
                if multimodal_complete
                and multimodal_provider in OPERATIONAL_MULTIMODAL_PROVIDERS
                else "configuration_only" if multimodal_complete else "incomplete"
            ),
            "stt_readiness": (
                "active"
                if stt_complete and stt_provider in OPERATIONAL_STT_PROVIDERS
                else "configuration_only" if stt_complete else "incomplete"
            ),
            "tts_readiness": "active" if tts_complete else "incomplete",
            "apis_configured": whatsapp_readiness == "active" or llm_ready,
        }
    )
    return result


def runtime_projection(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Return the strictly non-secret API credential projection allowed in cache."""

    resolved = resolve_stt_credentials(credentials)
    allowed = {
        "whatsapp_provider",
        "zapi_instance_id",
        "evolution_api_url",
        "evolution_instance_name",
        "meta_phone_number_id",
        "meta_waba_id",
        "uazapi_api_url",
        "uazapi_instance_name",
        "llm_provider",
        "llm_api_url",
        "llm_model_name",
        "stt_provider",
        "audio_max_seconds",
    }
    projected = {field: resolved[field] for field in allowed if field in resolved}
    if resolved.get("stt_provider") in OPERATIONAL_STT_PROVIDERS:
        for field in ("stt_api_url", "stt_model_name"):
            if field in resolved:
                projected[field] = resolved[field]
    return projected
