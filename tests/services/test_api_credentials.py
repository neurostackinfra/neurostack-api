"""Pure contract tests for the API credential policy boundary."""

from __future__ import annotations

import pytest

from src.services.api_credentials import (
    API_CREDENTIAL_FIELDS,
    CONFIG_CACHE_SCHEMA_VERSION,
    CONFIG_CACHE_VERSION_FIELD,
    PROVIDER_ALLOWLISTS,
    PROVIDER_AVAILABILITY,
    READINESS_IDS,
    SECRET_FIELDS,
    TTS_FIELDS,
    WIRE_ERROR_CODES,
    CredentialPolicyError,
    build_cache_payload,
    compose_llm_credentials,
    config_cache_key,
    credential_boundary_fields,
    credential_status,
    is_cache_v2_payload,
    merge_credentials,
    resolve_stt_credentials,
    runtime_projection,
    validate_provider_availability,
)


def test_provider_availability_is_the_exact_all_coming_soon_contract() -> None:
    assert PROVIDER_AVAILABILITY == {
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
        "tts": {"elevenlabs": "coming_soon", "google": "coming_soon"},
    }
    assert sum(len(providers) for providers in PROVIDER_AVAILABILITY.values()) == 19
    assert all(
        availability == "coming_soon"
        for providers in PROVIDER_AVAILABILITY.values()
        for availability in providers.values()
    )
    assert PROVIDER_ALLOWLISTS == {
        capability: frozenset(providers)
        for capability, providers in PROVIDER_AVAILABILITY.items()
    }
    assert "groq" not in PROVIDER_ALLOWLISTS["multimodal"]
    assert "openai" not in PROVIDER_ALLOWLISTS["multimodal"]
    assert "microsoft" not in PROVIDER_ALLOWLISTS["tts"]


def test_wire_ids_readiness_and_errors_match_design() -> None:
    assert READINESS_IDS == {
        "active",
        "incomplete",
        "signature_pending",
        "configuration_only",
    }
    assert {
        "credential_endpoint_required",
        "credentials_jwt_required",
        "credentials_role_forbidden",
        "invalid_credentials_payload",
        "invalid_provider_for_capability",
        "provider_not_available",
    }.issubset(WIRE_ERROR_CODES)


@pytest.mark.parametrize(
    ("patch", "error", "context"),
    [
        (
            {"llm_provider": "unknown"},
            "invalid_provider_for_capability",
            {"capability": "llm"},
        ),
        (
            {"multimodal_provider": "groq"},
            "invalid_provider_for_capability",
            {"capability": "multimodal"},
        ),
    ],
)
def test_merge_errors_are_exact_and_atomic(
    patch: dict[str, object], error: str, context: dict[str, str]
) -> None:
    existing = {"llm_provider": "dropout", "llm_api_key": "kept-secret"}
    snapshot = dict(existing)
    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials(existing, patch)
    assert caught.value.detail == {"error": error, **context}
    assert existing == snapshot


def test_post_merge_availability_rejects_effective_changes_and_allows_masked_echo() -> (
    None
):
    existing = {
        "llm_provider": "openai",
        "llm_api_url": "https://api.openai.com/v1",
        "llm_api_key": "secret-one",
        "llm_model_name": "old-model",
    }
    changed = merge_credentials(
        existing,
        {"llm_model_name": "new-model", "llm_api_key": "••••••••"},
    )
    with pytest.raises(CredentialPolicyError) as caught:
        validate_provider_availability(existing, changed)
    assert caught.value.detail == {
        "error": "provider_not_available",
        "capability": "llm",
        "provider": "openai",
    }

    echoed = merge_credentials(
        existing,
        {
            "llm_provider": "openai",
            "llm_api_url": "https://api.openai.com/v1",
            "llm_api_key": "••••••••",
            "llm_model_name": "old-model",
        },
    )
    assert echoed == existing
    validate_provider_availability(existing, echoed)


def test_sequential_saves_preserve_capabilities_and_clean_stt_aliases() -> None:
    first = merge_credentials(
        {},
        {
            "llm_provider": "openai",
            "llm_api_url": "https://api.openai.com/v1",
            "llm_api_key": "llm-secret",
            "llm_model_name": "gpt-model",
        },
    )
    second = merge_credentials(
        first,
        {
            "stt_provider": "outros",
            "stt_api_url": "https://speech.example/v1",
            "stt_api_key": "stt-secret",
            "stt_model_name": "whisper-large",
        },
    )
    assert second["llm_api_key"] == "llm-secret"
    assert second["stt_api_key"] == "stt-secret"

    legacy = {
        **second,
        "whisper_api_key": "legacy-secret",
        "whisper_model": "legacy-model",
    }
    resolved = resolve_stt_credentials(legacy)
    assert resolved["stt_api_key"] == "stt-secret"
    assert resolved["stt_model_name"] == "whisper-large"
    cleaned = merge_credentials(
        legacy,
        {"stt_api_key": "canonical-new", "stt_model_name": "canonical-model"},
    )
    assert "whisper_api_key" not in cleaned
    assert "whisper_model" not in cleaned


@pytest.mark.parametrize(
    ("provider", "api_url", "voice_id"),
    [
        ("elevenlabs", "https://api.elevenlabs.io/v1", "voice-123"),
        ("google", "https://texttospeech.googleapis.com/v1", None),
    ],
)
def test_tts_configuration_is_operational_but_never_projected_to_cache(
    provider: str,
    api_url: str,
    voice_id: str | None,
) -> None:
    credentials = merge_credentials(
        {},
        {
            "tts_provider": provider,
            "tts_api_url": api_url,
            "tts_api_key": "tts-secret",
            "tts_model_name": "pt-br-voice",
            "tts_voice_id": voice_id,
            "tts_enabled": True,
            "tts_reply_policy": "all_inputs",
        },
    )

    status = credential_status(credentials)
    assert status["tts_provider"] == provider
    assert status["tts_api_key_set"] is True
    assert status["tts_api_key_masked"] == "••••••••"
    assert status["tts_enabled"] is True
    assert status["tts_voice_id"] == voice_id
    assert status["tts_reply_policy"] == "all_inputs"
    assert status["tts_readiness"] == "active"
    assert not any(
        field.startswith("tts_") for field in runtime_projection(credentials)
    )
    assert "tts-secret" not in repr(status)


@pytest.mark.parametrize(
    ("patch", "expected_controls"),
    [
        (
            {"tts_reply_policy": "all_inputs"},
            {"tts_enabled": False, "tts_reply_policy": "all_inputs"},
        ),
        (
            {"tts_enabled": True},
            {"tts_enabled": True, "tts_reply_policy": "audio_input"},
        ),
        (
            {
                "tts_enabled": True,
                "tts_reply_policy": "all_inputs",
                "tts_api_key": "••••••••",
            },
            {"tts_enabled": True, "tts_reply_policy": "all_inputs"},
        ),
    ],
)
def test_tts_control_merge_preserves_complete_saved_bundle_and_readiness(
    patch: dict[str, object], expected_controls: dict[str, object]
) -> None:
    existing = {
        "tts_provider": "google",
        "tts_api_url": "https://texttospeech.googleapis.com/v1",
        "tts_api_key": "saved-tts-secret",
        "tts_model_name": "pt-br-voice",
        "tts_voice_id": "saved-voice",
        "tts_enabled": False,
        "llm_model_name": "unrelated-model",
    }

    merged = merge_credentials(existing, patch)

    assert "tts_reply_policy" in TTS_FIELDS
    validate_provider_availability(existing, merged)
    status = credential_status(merged)
    assert status["tts_enabled"] is expected_controls["tts_enabled"]
    assert status["tts_reply_policy"] == expected_controls["tts_reply_policy"]
    assert status["tts_readiness"] == "active"
    for field in (
        "tts_provider",
        "tts_api_url",
        "tts_api_key",
        "tts_model_name",
        "tts_voice_id",
        "llm_model_name",
    ):
        assert merged[field] == existing[field]
    assert "saved-tts-secret" not in repr(status)
    assert not any(field.startswith("tts_") for field in runtime_projection(merged))


def test_tts_reply_policy_status_defaults_only_for_legacy_storage() -> None:
    assert credential_status({})["tts_reply_policy"] == "audio_input"
    assert credential_status({"tts_provider": "google"})["tts_reply_policy"] == (
        "audio_input"
    )
    assert (
        credential_status({"tts_reply_policy": "all_inputs"})["tts_reply_policy"]
        == "all_inputs"
    )


@pytest.mark.parametrize(
    ("provider", "expected_readiness"),
    [
        # Measured against the live API: only OpenRouter transcribes the
        # OGG/Opus WhatsApp delivers, so only it may claim `active`.
        ("openrouter", "active"),
        ("groq", "configuration_only"),
        ("openai", "configuration_only"),
    ],
)
def test_multimodal_readiness_is_active_only_for_operational_providers(
    provider: str, expected_readiness: str
) -> None:
    # Readiness is deliberately outside the provider availability contract:
    # preserve its legacy behavior for existing stored credentials, including
    # removed providers that can no longer be submitted through the schema.
    credentials = {
        "multimodal_provider": provider,
        "multimodal_api_url": "https://multimodal.example/v1",
        "multimodal_api_key": "fake-multimodal-key",
        "multimodal_model_name": "some-audio-model",
    }

    status = credential_status(credentials)
    assert status["multimodal_readiness"] == expected_readiness
    assert status["multimodal_provider"] == provider
    assert status["multimodal_api_key_set"] is True
    assert status["multimodal_api_key_masked"] == "••••••••"
    assert "fake-multimodal-key" not in repr(status)
    # Readiness never widens the runtime projection: even an `active` bundle
    # stays out of the cache payload.
    assert not any(
        field.startswith("multimodal_") for field in runtime_projection(credentials)
    )


@pytest.mark.parametrize(
    "missing_field",
    [
        "multimodal_provider",
        "multimodal_api_url",
        "multimodal_api_key",
        "multimodal_model_name",
    ],
)
def test_multimodal_readiness_is_incomplete_until_every_field_is_present(
    missing_field: str,
) -> None:
    # Built directly instead of through `merge_credentials`: dropping the key
    # while changing provider/url is a different policy (fresh_api_key_required)
    # and would mask the readiness branch under test.
    credentials = {
        "multimodal_provider": "openrouter",
        "multimodal_api_url": "https://openrouter.ai/api/v1",
        "multimodal_api_key": "fake-multimodal-key",
        "multimodal_model_name": "some-audio-model",
    }
    credentials.pop(missing_field)

    status = credential_status(credentials)
    assert status["multimodal_readiness"] == "incomplete"


def test_status_and_runtime_projection_never_expose_secrets_or_digest() -> None:
    credentials = {
        "whatsapp_provider": "zapi",
        "zapi_instance_id": "instance-1",
        "zapi_token": "zapi-secret",
        "zapi_inbound_token_digest": "a" * 64,
        "meta_verify_token": "meta-verification-sentinel",
        "llm_provider": "openai",
        "llm_api_url": "https://api.openai.com/v1",
        "llm_api_key": "llm-secret",
        "llm_model_name": "model",
        "multimodal_provider": "groq",
        "multimodal_api_key": "multi-secret",
        "tts_provider": "google",
        "tts_api_key": "tts-secret",
        "stt_provider": "elevenlabs_scribe",
        "stt_api_url": "https://speech.example",
        "stt_api_key": "stt-secret",
        "stt_model_name": "scribe",
    }
    status = credential_status(credentials)
    projection = runtime_projection(credentials)
    serialized = repr({"status": status, "projection": projection})
    for forbidden in (
        "zapi-secret",
        "llm-secret",
        "multi-secret",
        "tts-secret",
        "stt-secret",
        "a" * 64,
        "meta-verification-sentinel",
    ):
        assert forbidden not in serialized
    assert status["zapi_inbound_token_set"] is True
    assert status["meta_verify_token_set"] is True
    assert status["meta_verify_token_masked"] == "••••••••"
    assert "zapi_inbound_token_digest" not in status
    assert status["stt_readiness"] == "configuration_only"
    assert not any(field in projection for field in SECRET_FIELDS)
    assert not any(field.startswith(("multimodal_", "tts_")) for field in projection)
    assert "meta_verify_token" not in projection


def test_legacy_only_stt_resolves_to_canonical_status_and_projection() -> None:
    credentials = {
        "whisper_api_key": "legacy-stt-secret",
        "whisper_model": "legacy-whisper-model",
        "stt_api_url": "https://speech.example/v1",
    }
    resolved = resolve_stt_credentials(credentials)
    status = credential_status(credentials)
    projection = runtime_projection(credentials)

    assert resolved["stt_provider"] == "outros"
    assert resolved["audio_max_seconds"] == 60
    assert status["stt_provider"] == "outros"
    assert status["audio_max_seconds"] == 60
    assert status["stt_api_url"] == "https://speech.example/v1"
    assert status["stt_model_name"] == "legacy-whisper-model"
    assert status["stt_api_key_set"] is True
    assert "legacy-stt-secret" not in repr(status)
    assert projection == {
        "stt_provider": "outros",
        "stt_api_url": "https://speech.example/v1",
        "stt_model_name": "legacy-whisper-model",
        "audio_max_seconds": 60,
    }


def test_meta_signature_readiness_and_llm_overlay_are_fail_closed() -> None:
    meta = credential_status(
        {
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": "phone-id",
            "meta_permanent_token": "token",
        }
    )
    assert meta["whatsapp_readiness"] == "signature_pending"
    assert meta["apis_configured"] is False

    clinic = {"whatsapp_provider": "zapi", "stt_provider": "outros"}
    builder = {
        "llm_provider": "dropout",
        "llm_api_url": "https://api.neuralake.cloud/v1",
        "llm_api_key": "builder-key",
        "llm_model_name": "text",
        "stt_provider": "microsoft_stt",
    }
    composed = compose_llm_credentials(clinic, builder)
    assert composed["llm_api_key"] == "builder-key"
    assert composed["whatsapp_provider"] == "zapi"
    assert composed["stt_provider"] == "outros"


def test_clinic_complete_llm_wins_over_builder_and_never_pulls_other_fields() -> None:
    clinic = {
        "whatsapp_provider": "zapi",
        "zapi_instance_id": "instance-clinic",
        "stt_provider": "outros",
        "stt_api_url": "https://speech.clinic",
        "stt_api_key": "clinic-stt-secret",
        "llm_provider": "openai",
        "llm_api_url": "https://api.openai.com/v1",
        "llm_api_key": "clinic-llm-secret",
        "llm_model_name": "clinic-model",
    }
    builder = {
        "whatsapp_provider": "meta_cloud",
        "zapi_instance_id": "instance-builder",
        "stt_provider": "microsoft_stt",
        "stt_api_key": "builder-stt-secret",
        "llm_provider": "dropout",
        "llm_api_url": "https://api.neuralake.cloud/v1",
        "llm_api_key": "builder-llm-secret",
        "llm_model_name": "builder-model",
    }
    composed = compose_llm_credentials(clinic, builder)
    # LLM stays with the clinic
    assert composed["llm_provider"] == "openai"
    assert composed["llm_api_key"] == "clinic-llm-secret"
    # NON-LLM fields byte-for-byte identical to the clinic base
    for field in (
        "whatsapp_provider",
        "zapi_instance_id",
        "stt_provider",
        "stt_api_url",
        "stt_api_key",
    ):
        assert composed[field] == clinic[field]


def test_incomplete_clinic_llm_inherits_only_four_llm_fields_from_builder() -> None:
    clinic = {
        "whatsapp_provider": "zapi",
        "zapi_instance_id": "instance-clinic",
        "stt_provider": "outros",
        "stt_api_url": "https://speech.clinic",
        # LLM only partially set: no url, no key, no model
        "llm_provider": "openai",
    }
    builder = {
        "whatsapp_provider": "meta_cloud",  # MUST NOT be inherited
        "zapi_instance_id": "should-not-inherit",
        "stt_provider": "microsoft_stt",
        "stt_api_key": "builder-stt-secret",
        "llm_provider": "dropout",
        "llm_api_url": "https://api.neuralake.cloud/v1",
        "llm_api_key": "builder-llm-secret",
        "llm_model_name": "builder-model",
    }
    composed = compose_llm_credentials(clinic, builder)
    # Exactly the four llm_* fields from builder overlay the clinic
    assert composed["llm_provider"] == "dropout"
    assert composed["llm_api_url"] == "https://api.neuralake.cloud/v1"
    assert composed["llm_api_key"] == "builder-llm-secret"
    assert composed["llm_model_name"] == "builder-model"
    # WhatsApp / STT / any other clinic field remains unchanged
    assert composed["whatsapp_provider"] == "zapi"
    assert composed["zapi_instance_id"] == "instance-clinic"
    assert composed["stt_provider"] == "outros"
    assert composed["stt_api_url"] == "https://speech.clinic"
    assert "stt_api_key" not in composed
    # Builder secrets outside the four fields never appear
    assert "builder-stt-secret" not in repr(composed)


def test_incomplete_clinic_no_builder_or_incomplete_builder_leaves_clinic() -> None:
    clinic = {"whatsapp_provider": "zapi", "llm_provider": "openai"}
    # No builder at all
    assert compose_llm_credentials(clinic, None) == clinic
    # Empty builder mapping
    assert compose_llm_credentials(clinic, {}) == clinic
    # Builder incomplete (missing model)
    partial_builder = {
        "llm_provider": "dropout",
        "llm_api_url": "https://api.neuralake.cloud/v1",
        "llm_api_key": "builder-key",
    }
    assert compose_llm_credentials(clinic, partial_builder) == clinic
    # Builder with non-operational provider is not eligible
    non_op_builder = {
        "llm_provider": "slm_cortex",
        "llm_api_url": "https://x",
        "llm_api_key": "k",
        "llm_model_name": "m",
    }
    assert compose_llm_credentials(clinic, non_op_builder) == clinic


def test_two_builders_cross_clinics_prove_zero_credential_bleed() -> None:
    """Simulate builders A and B with clinics X and Y: X must never see B's key.

    The overlay function is pure; it composes clinic + a resolved owner only.
    The test asserts the invariant that different owners produce different keys
    and never mix — the *resolver* (event_service._resolve_builder_owner_tenant_id)
    is exercised in the DB-only integration tests.
    """
    clinic_x = {"llm_provider": "openai"}  # incomplete → will inherit
    clinic_y = {"llm_provider": "openai"}
    builder_a = {
        "llm_provider": "dropout",
        "llm_api_url": "https://api.neuralake.cloud/v1",
        "llm_api_key": "BUILDER-A-SECRET",
        "llm_model_name": "text",
    }
    builder_b = {
        "llm_provider": "openai",
        "llm_api_url": "https://api.openai.com/v1",
        "llm_api_key": "BUILDER-B-SECRET",
        "llm_model_name": "gpt",
    }
    xa = compose_llm_credentials(clinic_x, builder_a)
    yb = compose_llm_credentials(clinic_y, builder_b)
    assert xa["llm_api_key"] == "BUILDER-A-SECRET"
    assert yb["llm_api_key"] == "BUILDER-B-SECRET"
    # Zero credential bleed across the two computations
    assert "BUILDER-B-SECRET" not in repr(xa)
    assert "BUILDER-A-SECRET" not in repr(yb)


def test_cache_marker_and_projection_wrap_scrubs_secrets() -> None:
    config = {
        "identity": {"clinic_name": "Clinic A"},
        "api_credentials": {
            "whatsapp_provider": "zapi",
            "zapi_instance_id": "instance-1",
            "zapi_token": "SECRET-ZAPI",
            "zapi_inbound_token_digest": "d" * 64,
            "llm_provider": "openai",
            "llm_api_url": "https://api.openai.com/v1",
            "llm_api_key": "SECRET-LLM",
            "llm_model_name": "gpt",
            "multimodal_provider": "groq",
            "multimodal_api_url": "https://api.groq.com",
            "multimodal_api_key": "SECRET-MULTIMODAL",
            "tts_provider": "google",
            "tts_api_key": "SECRET-TTS",
            "stt_provider": "outros",
            "stt_api_url": "https://speech.example",
            "stt_api_key": "SECRET-STT",
            "stt_model_name": "whisper-large",
        },
    }
    payload = build_cache_payload(config)
    assert payload[CONFIG_CACHE_VERSION_FIELD] == CONFIG_CACHE_SCHEMA_VERSION
    assert is_cache_v2_payload(payload)
    projected = payload["api_credentials"]
    # No secret from any capability leaked
    serialized = repr(payload)
    for forbidden in (
        "SECRET-ZAPI",
        "SECRET-LLM",
        "SECRET-MULTIMODAL",
        "SECRET-TTS",
        "SECRET-STT",
        "d" * 64,
    ):
        assert forbidden not in serialized
    # Config-only capabilities entirely stripped
    assert not any(field.startswith(("multimodal_", "tts_")) for field in projected)
    # Operational STT keeps url/model but no key
    assert projected["stt_provider"] == "outros"
    assert projected["stt_api_url"] == "https://speech.example"
    assert projected["stt_model_name"] == "whisper-large"
    assert "stt_api_key" not in projected
    # Base config data preserved outside api_credentials
    assert payload["identity"] == {"clinic_name": "Clinic A"}


def test_cache_marker_detects_missing_and_legacy_payloads() -> None:
    assert is_cache_v2_payload(None) is False
    assert is_cache_v2_payload({}) is False
    assert is_cache_v2_payload({"api_credentials": {}}) is False
    assert is_cache_v2_payload({CONFIG_CACHE_VERSION_FIELD: 1}) is False
    assert (
        is_cache_v2_payload({CONFIG_CACHE_VERSION_FIELD: CONFIG_CACHE_SCHEMA_VERSION})
        is True
    )


def test_config_cache_key_deterministic_per_tenant() -> None:
    key = config_cache_key("a0000000-0000-0000-0000-000000000001")
    assert key == "neurostack:clinic_config:a0000000-0000-0000-0000-000000000001"


def test_runtime_projection_never_carries_config_only_or_secrets() -> None:
    creds = {
        "whatsapp_provider": "meta_cloud",
        "meta_phone_number_id": "phone",
        "meta_permanent_token": "SECRET-META",
        "llm_provider": "outros",
        "llm_api_url": "https://custom.example/v1",
        "llm_api_key": "SECRET-LLM",
        "llm_model_name": "custom-model",
        "multimodal_provider": "openrouter",
        "multimodal_api_key": "SECRET-MULTIMODAL",
        "tts_provider": "elevenlabs",
        "tts_api_key": "SECRET-TTS",
        "stt_provider": "openai_whisper",
        "stt_api_url": "https://api.openai.com/v1",
        "stt_api_key": "SECRET-STT",
        "stt_model_name": "whisper-1",
    }
    projection = runtime_projection(creds)
    # Operational STT keeps public url/model, but never any key
    assert projection["stt_provider"] == "openai_whisper"
    assert projection["stt_api_url"] == "https://api.openai.com/v1"
    assert projection["stt_model_name"] == "whisper-1"
    # LLM public fields kept, api_key stripped
    assert projection["llm_provider"] == "outros"
    assert projection["llm_api_url"] == "https://custom.example/v1"
    assert projection["llm_model_name"] == "custom-model"
    assert "llm_api_key" not in projection
    # No secrets whatsoever
    serialized = repr(projection)
    for forbidden in (
        "SECRET-META",
        "SECRET-LLM",
        "SECRET-MULTIMODAL",
        "SECRET-TTS",
        "SECRET-STT",
    ):
        assert forbidden not in serialized
    # Config-only capabilities absent
    assert not any(field.startswith(("multimodal_", "tts_")) for field in projection)
    # meta_permanent_token is a secret and never projected
    assert "meta_permanent_token" not in projection


def test_operational_stt_projection_scoped_to_url_and_model_only() -> None:
    creds = {
        "stt_provider": "elevenlabs_scribe",
        "stt_api_url": "https://api.elevenlabs.io/v1",
        "stt_api_key": "SECRET-STT",
        "stt_model_name": "scribe",
    }
    projection = runtime_projection(creds)
    # Non-operational STT: url/model excluded, key never present
    assert projection["stt_provider"] == "elevenlabs_scribe"
    assert "stt_api_url" not in projection
    assert "stt_model_name" not in projection
    assert "stt_api_key" not in projection


def test_multimodal_text_fallback_gate_lives_in_credential_bucket_only() -> None:
    """The opt-in gate is a credential field even though it is non-secret.

    Wire contract:
      * It IS in `API_CREDENTIAL_FIELDS` (so the capability schema owns it
        and the generic /v1/config endpoint refuses it via the boundary).
      * It is NOT in `SECRET_FIELDS` (round-trips verbatim on GET; no
        `_set` / `_masked` twin).
      * It is NOT in the runtime cache projection (bundle stays DB-only via
        the private multimodal-fallback loader — see event_service).
    """

    assert "multimodal_text_fallback_enabled" in API_CREDENTIAL_FIELDS
    assert "multimodal_text_fallback_enabled" not in SECRET_FIELDS

    # Default legacy: absent from credentials => `false` in the projected
    # response so the UI can bind a boolean control directly.
    legacy_status = credential_status({})
    assert legacy_status["multimodal_text_fallback_enabled"] is False
    # Non-secret projection: no `_set` / `_masked` twin.
    assert "multimodal_text_fallback_enabled_set" not in legacy_status
    assert "multimodal_text_fallback_enabled_masked" not in legacy_status

    # Round-trip: both booleans preserved verbatim in the response.
    enabled_status = credential_status({"multimodal_text_fallback_enabled": True})
    assert enabled_status["multimodal_text_fallback_enabled"] is True
    disabled_status = credential_status({"multimodal_text_fallback_enabled": False})
    assert disabled_status["multimodal_text_fallback_enabled"] is False

    # merge_credentials round-trips both booleans without triggering
    # fresh-key policy (the gate is orthogonal to provider/url changes).
    merged_on = merge_credentials({}, {"multimodal_text_fallback_enabled": True})
    assert merged_on["multimodal_text_fallback_enabled"] is True
    merged_off = merge_credentials(
        {"multimodal_text_fallback_enabled": True},
        {"multimodal_text_fallback_enabled": False},
    )
    assert merged_off["multimodal_text_fallback_enabled"] is False

    # Runtime cache projection MUST stay silent about the gate — the bundle
    # is loaded DB-only through the private multimodal-fallback loader.
    projection = runtime_projection({"multimodal_text_fallback_enabled": True})
    assert "multimodal_text_fallback_enabled" not in projection


def test_generic_boundary_covers_exact_fields_aliases_and_future_prefixes() -> None:
    candidates: dict[str, object] = {field: None for field in API_CREDENTIAL_FIELDS}
    candidates.update(
        {
            "api_credentials": {},
            "llm_base_url": "masked",
            "tts_future_secret": "masked",
            "meta_minutos_atendimento": 10,
            "ordinary_field": "allowed",
        }
    )
    blocked = credential_boundary_fields(candidates)
    assert "ordinary_field" not in blocked
    assert "meta_minutos_atendimento" not in blocked
    assert "api_credentials" in blocked
    assert "llm_base_url" in blocked
    assert "tts_future_secret" in blocked
    assert set(API_CREDENTIAL_FIELDS).issubset(blocked)
