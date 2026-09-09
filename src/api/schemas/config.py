"""
NeuroStack API - Config Schemas
Pydantic schemas for clinic configuration CRUD endpoints.
"""

import ipaddress
from collections.abc import Mapping
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from src.services.api_credentials import (
    GEMINI_API_BASE_URL,
    CredentialPolicyError,
    validate_gemini_endpoint,
)


def _normalize_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.port is not None and parsed.port != 443)
        or parsed.hostname.lower() == "localhost"
    ):
        raise ValueError("unsafe_api_url")
    hostname = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("unsafe_api_url")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", netloc, path, parsed.query, ""))


Identifier = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=255),
]
Secret = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8192)]
ApiUrl = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=2048
    ),
    AfterValidator(_normalize_https_url),
]
CapabilityDigest = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64
    ),
]


class WhatsAppCredentialsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    whatsapp_provider: (
        Literal["meta_cloud", "dialog360", "twilio", "zapi", "evolution", "uazapi"]
        | None
    ) = None
    zapi_instance_id: Identifier | None = None
    zapi_token: Secret | None = None
    zapi_client_token: Secret | None = None
    zapi_inbound_token_digest: CapabilityDigest | None = None
    evolution_api_url: ApiUrl | None = None
    evolution_instance_name: Identifier | None = None
    evolution_api_key: Secret | None = None
    meta_phone_number_id: Identifier | None = None
    meta_waba_id: Identifier | None = None
    meta_permanent_token: Secret | None = None
    meta_app_secret: Secret | None = None
    meta_verify_token: Secret | None = None
    uazapi_api_url: ApiUrl | None = None
    uazapi_instance_name: Identifier | None = None
    uazapi_token: Secret | None = None


class LlmCredentialsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    llm_provider: (
        Literal["openai", "dropout", "gemini", "outros", "slm_cortex"] | None
    ) = None
    llm_api_url: ApiUrl | None = None
    llm_api_key: Secret | None = None
    llm_model_name: Identifier | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_raw_gemini_endpoint(cls, value: Any) -> Any:
        """Apply Gemini's exact endpoint contract to the unnormalized payload."""

        if not isinstance(value, Mapping) or "llm_api_url" not in value:
            return value
        raw_url = value.get("llm_api_url")
        canonical_host = urlsplit(GEMINI_API_BASE_URL).hostname
        try:
            parsed = urlsplit(raw_url) if isinstance(raw_url, str) else None
            explicit_gemini_port = bool(
                parsed is not None
                and parsed.hostname == canonical_host
                and parsed.port is not None
            )
        except ValueError:
            explicit_gemini_port = value.get("llm_provider") == "gemini"
        if explicit_gemini_port:
            raise ValueError("invalid_provider_endpoint")
        if value.get("llm_provider") == "gemini":
            try:
                validate_gemini_endpoint(raw_url)
            except CredentialPolicyError:
                raise ValueError("invalid_provider_endpoint") from None
        return value


class MultimodalCredentialsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    multimodal_provider: Literal["openrouter"] | None = None
    multimodal_api_url: ApiUrl | None = None
    multimodal_api_key: Secret | None = None
    multimodal_model_name: Identifier | None = None
    # Opt-in gate for the text-only multimodal fallback: legacy default is
    # `false`, so an existing tenant sees no behaviour change until they
    # explicitly flip it on. Strict boolean so `null` / string / number
    # payloads are rejected atomically at the boundary rather than silently
    # coerced. Omission (PATCH) is fine because `exclude_unset=True` drops
    # the field before it reaches the merge; the validator below only fires
    # when the caller sent an explicit `null`.
    multimodal_text_fallback_enabled: bool | None = None

    @field_validator("multimodal_text_fallback_enabled", mode="before")
    @classmethod
    def _reject_null_fallback_gate(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("bool_type")
        return value


class SttCredentialsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    stt_provider: (
        Literal[
            "outros",
            "openai_whisper",
            "elevenlabs_scribe",
            "google_stt",
            "deepgram_stt",
            "microsoft_stt",
        ]
        | None
    ) = None
    stt_api_url: ApiUrl | None = None
    stt_api_key: Secret | None = None
    stt_model_name: Identifier | None = None
    whisper_api_key: Secret | None = None
    whisper_model: Identifier | None = None
    audio_max_seconds: Annotated[int, Field(strict=True, ge=10, le=300)] | None = None


class TtsCredentialsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tts_provider: Literal["elevenlabs", "google"] | None = None
    tts_api_url: ApiUrl | None = None
    tts_api_key: Secret | None = None
    tts_model_name: Identifier | None = None
    tts_voice_id: Identifier | None = None
    tts_enabled: bool | None = None
    tts_reply_policy: Literal["audio_input", "all_inputs"] = "audio_input"


class ApiCredentialsPatch(
    WhatsAppCredentialsPatch,
    LlmCredentialsPatch,
    MultimodalCredentialsPatch,
    SttCredentialsPatch,
    TtsCredentialsPatch,
):
    """Flat, backwards-compatible patch composed from capability schemas."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ConfigResponse(BaseModel):
    """
    Flat dict response containing all config fields from every JSONB bucket.

    Uses extra="allow" so any field from any bucket can be included
    without enumerating every key here.
    """

    model_config = ConfigDict(extra="allow")

    clinic_id: Any = None
    version: int = 1


class ConfigPatchRequest(BaseModel):
    """
    Partial update request.

    Accepts any subset of config fields.  The service layer routes
    each field to its correct JSONB bucket.
    """

    model_config = ConfigDict(extra="allow")
