"""Pure policy for API-provider credentials.

This module deliberately has no FastAPI, database, Redis, or logging imports.
It is the single credential contract used by public routes and runtime callers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

MASKED_SECRET = "••••••••"

ProviderAvailability = Literal["active", "coming_soon"]

# This is the public credential-write contract.  A provider is `active` only
# where a runtime send/transcribe/synthesize path actually dispatches on it;
# anything still `coming_soon` would present a green badge over a capability
# that fails at message time, so adoption of those stays fail-closed.
PROVIDER_AVAILABILITY: dict[str, dict[str, ProviderAvailability]] = {
    "whatsapp": {
        "meta_cloud": "active",
        "zapi": "active",
        "uazapi": "active",
        "evolution": "active",
        "dialog360": "coming_soon",
        "twilio": "coming_soon",
    },
    "llm": {
        "dropout": "active",
        "openai": "active",
        "outros": "active",
        "slm_cortex": "coming_soon",
    },
    "multimodal": {"openrouter": "active"},
    "stt": {
        "outros": "active",
        "openai_whisper": "active",
        "elevenlabs_scribe": "coming_soon",
        "google_stt": "coming_soon",
        "deepgram_stt": "coming_soon",
        "microsoft_stt": "coming_soon",
    },
    "tts": {"elevenlabs": "active", "google": "active"},
}

# The allowlist remains the request-universe boundary.  Deriving it from the
# availability matrix makes the 19 displayable pairs impossible to drift.
PROVIDER_ALLOWLISTS: dict[str, frozenset[str]] = {
    capability: frozenset(providers)
    for capability, providers in PROVIDER_AVAILABILITY.items()
}

OPERATIONAL_LLM_PROVIDERS = frozenset({"openai", "dropout", "outros"})
OPERATIONAL_STT_PROVIDERS = frozenset({"outros", "openai_whisper"})
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
        "invalid_provider_for_capability",
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
TTS_CONTROL_FIELDS = frozenset({"tts_enabled", "tts_reply_policy"})
# Behavior knobs that carry no credential material, so writing them adopts no
# provider and stays available to a tenant that has none configured.
PROVIDER_INDEPENDENT_CONTROLS: dict[str, frozenset[str]] = {
    "multimodal": frozenset({"multimodal_text_fallback_enabled"}),
    "stt": frozenset({"audio_max_seconds"}),
}
CAPABILITY_PROVIDER_FIELDS: dict[str, str] = {
    "whatsapp": "whatsapp_provider",
    "llm": "llm_provider",
    "multimodal": "multimodal_provider",
    "stt": "stt_provider",
    "tts": "tts_provider",
}
# WhatsApp is the one capability whose runtime ignores the provider field: the
# webhook resolves a tenant from `meta_phone_number_id` and picks its outbound
# path from whichever token is set, so `whatsapp_provider` is only a label.
# Writing another provider's bundle therefore adopts that provider even when the
# label is left untouched, and the gate has to key on the bundle instead.
WHATSAPP_PROVIDER_FIELD_GROUPS: dict[str, frozenset[str]] = {
    "meta_cloud": frozenset(
        {
            "meta_phone_number_id",
            "meta_waba_id",
            "meta_permanent_token",
            "meta_app_secret",
            "meta_verify_token",
        }
    ),
    "zapi": frozenset(
        {
            "zapi_instance_id",
            "zapi_token",
            "zapi_client_token",
            "zapi_inbound_token_digest",
        }
    ),
    "evolution": frozenset(
        {"evolution_api_url", "evolution_instance_name", "evolution_api_key"}
    ),
    "uazapi": frozenset({"uazapi_api_url", "uazapi_instance_name", "uazapi_token"}),
}
# Exactly what each outbound send path reads. A relabel is gated on this rather
# than on readiness, because readiness also counts inbound hardening — Meta's
# app secret, Z-API's inbound token digest — which a tenant legitimately adds
# after it is already sending, and which no credential form collects up front.
WHATSAPP_OUTBOUND_FIELDS: dict[str, frozenset[str]] = {
    "meta_cloud": frozenset({"meta_phone_number_id", "meta_permanent_token"}),
    "zapi": frozenset({"zapi_instance_id", "zapi_token"}),
    "evolution": frozenset(
        {"evolution_api_url", "evolution_instance_name", "evolution_api_key"}
    ),
    "uazapi": frozenset({"uazapi_api_url", "uazapi_token"}),
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
    return merged


def _tts_required_fields(provider: Any) -> tuple[str, ...]:
    base = ("tts_provider", "tts_api_url", "tts_api_key", "tts_model_name")
    # ElevenLabs picks a voice at synthesis time, so its bundle is one wider.
    return base + ("tts_voice_id",) if provider == "elevenlabs" else base


def _tts_bundle_complete(credentials: Mapping[str, Any]) -> bool:
    provider = credentials.get("tts_provider")
    if provider not in PROVIDER_ALLOWLISTS["tts"]:
        return False
    return all(bool(credentials.get(field)) for field in _tts_required_fields(provider))


def _populates(fields: Iterable[str], credentials: Mapping[str, Any]) -> bool:
    """True when any of `fields` still carries a value after the write."""

    return any(credentials.get(field) for field in fields)


def persisted_provider(capability: str, credentials: Mapping[str, Any]) -> str | None:
    """Return the allowlisted provider a tenant already stores for a capability."""

    source = (
        resolve_stt_credentials(credentials) if capability == "stt" else credentials
    )
    provider = source.get(CAPABILITY_PROVIDER_FIELDS[capability])
    if not isinstance(provider, str):
        return None
    return provider if provider in PROVIDER_ALLOWLISTS[capability] else None


def validate_provider_availability(
    existing: Mapping[str, Any], candidate: Mapping[str, Any]
) -> None:
    """Reject an effective write that adopts a provider the tenant does not hold.

    This runs strictly after :func:`merge_credentials`.  Comparing the merged
    candidate with the stored values makes a masked, normalized legacy echo a
    no-op, while any resulting provider capability mutation stays atomic at
    the HTTP boundary.

    A tenant may keep maintaining the provider it already has persisted, even
    while that provider is `coming_soon` and even while its stored bundle is
    incomplete — otherwise a half-configured capability would have no way to
    be completed.  Everything else fails closed: adopting a second provider,
    bootstrapping a capability from nothing, and clearing a stored provider
    all stay rejected, so no tenant reaches an unvalidated provider it was not
    already using.
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
        # Both sides go through the same resolution so a legacy STT tenant,
        # whose provider id is only implied by its `whisper_*` aliases, is not
        # read as adopting a provider it has stored all along.
        stored = persisted_provider(capability, existing)
        if changed_fields <= PROVIDER_INDEPENDENT_CONTROLS.get(capability, frozenset()):
            continue
        if capability == "tts":
            # Enabling voice replies requires an already-operational bundle
            # whatever else the same request carries; completeness is
            # deliberately evaluated against `existing`, never a partial
            # candidate assembled by this very request.
            turning_on = bool(candidate.get("tts_enabled")) and not bool(
                existing.get("tts_enabled")
            )
            if (
                turning_on or changed_fields <= TTS_CONTROL_FIELDS
            ) and not _tts_bundle_complete(existing):
                raise CredentialPolicyError(
                    "provider_not_available",
                    capability=capability,
                    provider=provider,
                )
            if changed_fields <= TTS_CONTROL_FIELDS:
                continue
            # The credential fields are flat and shared between providers, so a
            # switch carried by the label alone inherits the previous provider's
            # endpoint, key and model. That bundle reads complete, so the card
            # would seal as configured while every synthesis call is dispatched
            # to one provider using another's credentials.
            if isinstance(provider, str) and stored is not None and provider != stored:
                supplied = set(_tts_required_fields(provider)) - {"tts_provider"}
                if not supplied <= changed_fields or not _tts_bundle_complete(
                    candidate
                ):
                    raise CredentialPolicyError(
                        "provider_not_available",
                        capability=capability,
                        provider=provider,
                    )
        if capability == "whatsapp":
            # Emptying a group is decommissioning, not adoption, and has to stay
            # reachable: the outbound path is chosen from whichever bundle is
            # populated, so a tenant who switched providers needs some way to
            # stop the old one, which still resolves its own inbound webhooks.
            adopted = {
                group_provider
                for group_provider, group in WHATSAPP_PROVIDER_FIELD_GROUPS.items()
                if _populates(changed_fields & group, candidate)
            } - {stored}
            # This runs before the `active` escape below, so it has to consult
            # availability itself; otherwise writing an available provider's
            # bundle would be rejected as adoption no matter what.
            blocked = sorted(p for p in adopted if providers.get(p) != "active")
            # Adopting an available provider still has to be declared: the
            # runtime dispatches on the bundle, so an undeclared write would
            # leave the label naming a provider that no longer serves traffic.
            unnamed = sorted(p for p in adopted if p != provider)
            for rejected in (blocked, unnamed):
                if rejected:
                    raise CredentialPolicyError(
                        "provider_not_available",
                        capability=capability,
                        provider=rejected[0],
                    )
            # The label carries no runtime effect — the outbound path is picked
            # from whichever bundle is populated — so a relabel to a provider
            # that cannot send would leave the old one serving traffic under the
            # new one's name. Writing the outgoing bundle in the same request is
            # refused for the mirror reason: it would leave two populated
            # bundles and no answer to which of them sends.
            # A legacy row can carry a live bundle under no label at all, so
            # incumbency is read from the credentials there: otherwise the first
            # relabel of such a row is unchecked, because `stored` is None and
            # the switch below never fires while the old bundle keeps sending.
            if stored is None:
                departing = {
                    p
                    for p, outbound in WHATSAPP_OUTBOUND_FIELDS.items()
                    if p != provider and all(existing.get(field) for field in outbound)
                }
            else:
                departing = {stored} - {provider}
            if isinstance(provider, str) and departing:
                outgoing = frozenset().union(
                    *(
                        WHATSAPP_PROVIDER_FIELD_GROUPS.get(p, frozenset())
                        for p in departing
                    )
                )
                required = WHATSAPP_OUTBOUND_FIELDS.get(provider)
                if (
                    required is None
                    or not all(candidate.get(field) for field in required)
                    or _populates(changed_fields & outgoing, candidate)
                ):
                    raise CredentialPolicyError(
                        "provider_not_available",
                        capability=capability,
                        provider=provider,
                    )
        if isinstance(provider, str) and providers.get(provider) == "active":
            continue
        if stored is not None and stored == persisted_provider(capability, candidate):
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
    """Overlay exactly the four LLM fields when clinic LLM is incomplete."""

    result = dict(clinic)
    if _llm_bundle_complete(clinic):
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
