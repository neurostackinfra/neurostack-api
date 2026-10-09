"""Pure contract tests for the API credential policy boundary."""

from __future__ import annotations

import pytest

from src.services.api_credentials import (
    API_CREDENTIAL_FIELDS,
    CAPABILITY_PROVIDER_FIELDS,
    CONFIG_CACHE_SCHEMA_VERSION,
    CONFIG_CACHE_VERSION_FIELD,
    GEMINI_API_BASE_URL,
    MULTIMODAL_OPENROUTER_API_BASE_URL,
    OPERATIONAL_LLM_PROVIDERS,
    OPERATIONAL_MULTIMODAL_PROVIDERS,
    OPERATIONAL_STT_PROVIDERS,
    PROVIDER_ALLOWLISTS,
    PROVIDER_AVAILABILITY,
    PUBLICLY_ACTIVE_LLM_PROVIDERS,
    READINESS_IDS,
    SECRET_FIELDS,
    STT_FIXED_API_BASES,
    TTS_FIELDS,
    WIRE_ERROR_CODES,
    CredentialPolicyError,
    build_cache_payload,
    compose_llm_credentials,
    config_cache_key,
    credential_boundary_fields,
    credential_status,
    is_cache_v2_payload,
    is_semantic_credentials_echo,
    merge_credentials,
    resolve_stt_credentials,
    runtime_projection,
    validate_provider_availability,
)


def test_provider_availability_matches_public_catalog() -> None:
    assert PROVIDER_AVAILABILITY == {
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
            "gemini": "active",
            "openai": "active",
            "outros": "active",
            "slm_cortex": "coming_soon",
        },
        "multimodal": {"openrouter": "active"},
        "stt": {
            "openrouter": "active",
            "openai_whisper": "active",
            "elevenlabs_scribe": "active",
            "google_stt": "coming_soon",
            "deepgram_stt": "coming_soon",
            "microsoft_stt": "coming_soon",
        },
        "tts": {
            "elevenlabs": "active",
            "google": "active",
            "openai": "active",
        },
    }
    assert sum(len(providers) for providers in PROVIDER_AVAILABILITY.values()) == 21
    states = [
        availability
        for providers in PROVIDER_AVAILABILITY.values()
        for availability in providers.values()
    ]
    assert states.count("active") == 15
    assert states.count("coming_soon") == 6
    assert PUBLICLY_ACTIVE_LLM_PROVIDERS == {"dropout", "gemini", "openai", "outros"}
    expected_allowlists = {
        capability: frozenset(providers)
        for capability, providers in PROVIDER_AVAILABILITY.items()
    }
    expected_allowlists["stt"] = expected_allowlists["stt"] | {"outros"}
    assert PROVIDER_ALLOWLISTS == expected_allowlists
    assert PROVIDER_AVAILABILITY["stt"]["openrouter"] == "active"
    assert "outros" not in PROVIDER_AVAILABILITY["stt"]
    assert {
        ("multimodal", "groq"),
        ("multimodal", "openai"),
        ("tts", "microsoft"),
    }.isdisjoint(
        (capability, provider)
        for capability, providers in PROVIDER_ALLOWLISTS.items()
        for provider in providers
    )


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
        "invalid_provider_endpoint",
        "invalid_provider_for_capability",
        "provider_switch_bundle_required",
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
        ("openai", "https://api.openai.com/v1", "alloy"),
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


def test_openai_tts_configuration_without_voice_is_incomplete() -> None:
    status = credential_status(
        {
            "tts_provider": "openai",
            "tts_api_url": "https://api.openai.com/v1",
            "tts_api_key": "tts-secret",
            "tts_model_name": "gpt-4o-mini-tts",
        }
    )
    assert status["tts_readiness"] == "incomplete"


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
def test_tts_control_merge_preserves_runtime_state_for_active_provider(
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
        "multimodal_api_url": (
            MULTIMODAL_OPENROUTER_API_BASE_URL
            if provider == "openrouter"
            else "https://multimodal.example/v1"
        ),
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


def test_openrouter_multimodal_full_adoption_is_ready_and_secret_free() -> None:
    patch = {
        "multimodal_provider": "openrouter",
        "multimodal_api_url": MULTIMODAL_OPENROUTER_API_BASE_URL,
        "multimodal_api_key": "fresh-multimodal-secret",
        "multimodal_model_name": "google/gemini-2.5-flash",
    }

    candidate = merge_credentials({}, patch)
    validate_provider_availability({}, candidate, patch=patch)
    status = credential_status(candidate)

    assert PROVIDER_AVAILABILITY["multimodal"]["openrouter"] == "active"
    assert "openrouter" in OPERATIONAL_MULTIMODAL_PROVIDERS
    assert status["multimodal_readiness"] == "active"
    assert status["multimodal_api_key_set"] is True
    assert status["multimodal_api_key_masked"] == "••••••••"
    assert "fresh-multimodal-secret" not in repr(status)


@pytest.mark.parametrize(
    "patch",
    [
        {"multimodal_provider": "openrouter"},
        {
            "multimodal_provider": "openrouter",
            "multimodal_api_url": MULTIMODAL_OPENROUTER_API_BASE_URL,
            "multimodal_api_key": "fresh-key",
        },
        {
            "multimodal_provider": "openrouter",
            "multimodal_api_url": MULTIMODAL_OPENROUTER_API_BASE_URL,
            "multimodal_api_key": "••••••••",
            "multimodal_model_name": "model",
        },
    ],
)
def test_openrouter_multimodal_adoption_requires_a_fresh_complete_bundle(
    patch: dict[str, object],
) -> None:
    existing = {
        "multimodal_provider": "groq",
        "multimodal_api_url": "https://api.groq.com/openai/v1",
        "multimodal_api_key": "stored-secret",
        "multimodal_model_name": "stored-model",
    }
    snapshot = dict(existing)

    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials(existing, patch)

    assert caught.value.detail == {
        "error": "provider_switch_bundle_required",
        "capability": "multimodal",
    }
    assert existing == snapshot


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/api/v1",
        "https://openrouter.ai:443/api/v1",
        "https://openrouter.ai/api/v1/chat/completions",
        "https://openrouter.ai/api/v1?query=sentinel",
        "https://openrouter.ai/api/v1#fragment",
    ],
)
def test_openrouter_multimodal_endpoint_is_exact_and_error_is_secret_free(
    endpoint: str,
) -> None:
    patch = {
        "multimodal_provider": "openrouter",
        "multimodal_api_url": endpoint,
        "multimodal_api_key": "secret-sentinel",
        "multimodal_model_name": "model-sentinel",
    }

    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials({}, patch)

    assert caught.value.detail == {
        "error": "invalid_provider_endpoint",
        "capability": "multimodal",
        "provider": "openrouter",
        "field": "multimodal_api_url",
    }
    rendered = repr(caught.value.detail)
    assert endpoint not in rendered
    assert "secret-sentinel" not in rendered
    assert "model-sentinel" not in rendered


def test_existing_openrouter_multimodal_bundle_allows_model_and_key_maintenance() -> (
    None
):
    existing = {
        "multimodal_provider": "openrouter",
        "multimodal_api_url": MULTIMODAL_OPENROUTER_API_BASE_URL,
        "multimodal_api_key": "stored-secret",
        "multimodal_model_name": "stored-model",
    }

    model_changed = merge_credentials(existing, {"multimodal_model_name": "new-model"})
    validate_provider_availability(existing, model_changed)
    assert model_changed["multimodal_model_name"] == "new-model"
    assert model_changed["multimodal_api_key"] == "stored-secret"

    key_changed = merge_credentials(existing, {"multimodal_api_key": "new-secret"})
    validate_provider_availability(existing, key_changed)
    assert key_changed["multimodal_api_key"] == "new-secret"
    assert key_changed["multimodal_model_name"] == "stored-model"


def test_incomplete_openrouter_incumbent_cannot_inherit_stored_key() -> None:
    existing = {
        "multimodal_provider": "openrouter",
        "multimodal_api_url": MULTIMODAL_OPENROUTER_API_BASE_URL,
        "multimodal_api_key": "stored-secret-sentinel",
    }
    snapshot = dict(existing)

    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials(existing, {"multimodal_model_name": "fresh-model"})

    assert caught.value.detail == {
        "error": "provider_switch_bundle_required",
        "capability": "multimodal",
    }
    assert "stored-secret-sentinel" not in repr(caught.value.detail)
    assert existing == snapshot


def test_noncanonical_openrouter_incumbent_requires_atomic_remediation() -> None:
    existing = {
        "multimodal_provider": "openrouter",
        "multimodal_api_url": "https://historical-openrouter-proxy.example/v1",
        "multimodal_api_key": "stored-secret-sentinel",
        "multimodal_model_name": "stored-model",
        "llm_provider": "openai",
        "llm_api_url": "https://api.openai.com/v1",
        "llm_api_key": "stored-llm-secret",
        "llm_model_name": "stored-llm-model",
    }

    assert credential_status(existing)["multimodal_readiness"] == "configuration_only"

    unrelated = merge_credentials(existing, {"llm_model_name": "new-llm-model"})
    validate_provider_availability(existing, unrelated)
    assert unrelated["multimodal_api_url"] == existing["multimodal_api_url"]
    assert unrelated["multimodal_api_key"] == existing["multimodal_api_key"]

    patch = {
        "multimodal_provider": "openrouter",
        "multimodal_api_url": MULTIMODAL_OPENROUTER_API_BASE_URL,
        "multimodal_api_key": "fresh-multimodal-secret",
        "multimodal_model_name": "fresh-model",
    }
    remediated = merge_credentials(existing, patch)
    validate_provider_availability(existing, remediated, patch=patch)
    status = credential_status(remediated)
    assert status["multimodal_readiness"] == "active"
    assert status["multimodal_api_key_masked"] == "••••••••"
    assert "fresh-multimodal-secret" not in repr(status)


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
    assert status["stt_readiness"] == "active"
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


def test_explicit_incomplete_clinic_llm_is_authoritative_over_builder() -> None:
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
    assert composed["llm_provider"] == "openai"
    assert "llm_api_url" not in composed
    assert "llm_api_key" not in composed
    assert "llm_model_name" not in composed
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
    clinic_x: dict[str, str] = {}
    clinic_y: dict[str, str] = {}
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


@pytest.mark.parametrize("explicit_provider", [None, "invalid", "gemini"])
def test_any_explicit_clinic_provider_blocks_builder_inheritance(
    explicit_provider: object,
) -> None:
    clinic = {"llm_provider": explicit_provider}
    builder = {
        "llm_provider": "openai",
        "llm_api_url": "https://api.openai.com/v1",
        "llm_api_key": "BUILDER-SECRET",
        "llm_model_name": "gpt",
    }
    composed = compose_llm_credentials(clinic, builder)
    assert composed == clinic
    assert "BUILDER-SECRET" not in repr(composed)


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
    # Operational STT keeps the non-secret runtime fields, never the key.
    assert projection["stt_provider"] == "elevenlabs_scribe"
    assert projection["stt_api_url"] == "https://api.elevenlabs.io/v1"
    assert projection["stt_model_name"] == "scribe"
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


@pytest.mark.parametrize(
    ("capability", "provider"),
    [
        (capability, provider)
        for capability, providers in PROVIDER_AVAILABILITY.items()
        for provider, availability in providers.items()
        if availability == "coming_soon"
    ],
)
def test_every_coming_soon_provider_change_is_rejected(
    capability: str, provider: str
) -> None:
    provider_field = CAPABILITY_PROVIDER_FIELDS[capability]
    existing: dict[str, object] = {}
    patch: dict[str, object] = {provider_field: provider}
    candidate = merge_credentials(existing, patch)

    with pytest.raises(CredentialPolicyError) as caught:
        validate_provider_availability(existing, candidate)

    assert caught.value.detail == {
        "error": "provider_not_available",
        "capability": capability,
        "provider": provider,
    }
    assert existing == {}


@pytest.mark.parametrize(
    ("provider", "endpoint_field", "instance_field", "secret_field"),
    [
        (
            "evolution",
            "evolution_api_url",
            "evolution_instance_name",
            "evolution_api_key",
        ),
        ("uazapi", "uazapi_api_url", "uazapi_instance_name", "uazapi_token"),
    ],
)
def test_configurable_whatsapp_egress_changes_require_a_fresh_bundle(
    provider: str,
    endpoint_field: str,
    instance_field: str,
    secret_field: str,
) -> None:
    old_url = f"https://old-{provider}.example/v1"
    new_url = f"https://new-{provider}.example/v1"
    active = {
        "whatsapp_provider": provider,
        endpoint_field: old_url,
        instance_field: "stored-instance",
        secret_field: "stored-secret",
    }
    dormant = {
        **active,
        "whatsapp_provider": "meta_cloud",
        "meta_phone_number_id": "stored-meta-phone",
    }

    for existing, patch in (
        ({}, {"whatsapp_provider": provider}),
        (active, {endpoint_field: new_url}),
        (
            active,
            {
                "whatsapp_provider": provider,
                endpoint_field: new_url,
                instance_field: "fresh-instance",
                secret_field: "••••••••",
            },
        ),
        (dormant, {endpoint_field: new_url}),
    ):
        snapshot = dict(existing)
        with pytest.raises(CredentialPolicyError) as caught:
            merge_credentials(existing, patch)
        assert caught.value.detail == {
            "error": "provider_switch_bundle_required",
            "capability": "whatsapp",
        }
        assert existing == snapshot


@pytest.mark.parametrize(
    ("provider", "endpoint_field", "instance_field", "secret_field"),
    [
        (
            "evolution",
            "evolution_api_url",
            "evolution_instance_name",
            "evolution_api_key",
        ),
        ("uazapi", "uazapi_api_url", "uazapi_instance_name", "uazapi_token"),
    ],
)
def test_configurable_whatsapp_full_rotation_and_instance_only_edit_are_valid(
    provider: str,
    endpoint_field: str,
    instance_field: str,
    secret_field: str,
) -> None:
    old_url = f"https://old-{provider}.example/v1"
    new_url = f"https://new-{provider}.example/v1"
    existing = {
        "whatsapp_provider": provider,
        endpoint_field: old_url,
        instance_field: "stored-instance",
        secret_field: "stored-secret",
    }

    instance_edit = merge_credentials(existing, {instance_field: "renamed-instance"})
    assert instance_edit == {**existing, instance_field: "renamed-instance"}

    rotated = merge_credentials(
        existing,
        {
            "whatsapp_provider": provider,
            endpoint_field: new_url,
            instance_field: "fresh-instance",
            secret_field: "fresh-secret",
        },
    )
    assert rotated[endpoint_field] == new_url
    assert rotated[instance_field] == "fresh-instance"
    assert rotated[secret_field] == "fresh-secret"

    decommissioned = merge_credentials(
        existing,
        {"whatsapp_provider": "meta_cloud", endpoint_field: None},
    )
    assert decommissioned["whatsapp_provider"] == "meta_cloud"
    assert decommissioned[endpoint_field] is None
    assert decommissioned[secret_field] == "stored-secret"


@pytest.mark.parametrize(
    ("existing_provider", "target_provider", "target_url"),
    [
        ("openai", "gemini", GEMINI_API_BASE_URL),
        ("gemini", "openai", "https://api.openai.com/v1"),
        ("openai", "dropout", "https://api.neuralake.cloud/v1"),
        ("dropout", "outros", "https://custom-llm.example/v1"),
    ],
)
@pytest.mark.parametrize(
    "omitted_field",
    ["llm_provider", "llm_api_url", "llm_api_key", "llm_model_name"],
)
def test_active_llm_provider_switch_requires_fresh_atomic_bundle(
    existing_provider: str,
    target_provider: str,
    target_url: str,
    omitted_field: str,
) -> None:
    existing_url = (
        GEMINI_API_BASE_URL
        if existing_provider == "gemini"
        else "https://api.openai.com/v1"
    )
    existing = {
        "llm_provider": existing_provider,
        "llm_api_url": existing_url,
        "llm_api_key": "stored-secret",
        "llm_model_name": "stored-model",
    }
    patch = {
        "llm_provider": target_provider,
        "llm_api_url": target_url,
        "llm_api_key": "fresh-secret",
        "llm_model_name": "fresh-model",
    }
    patch.pop(omitted_field)

    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials(existing, patch)

    assert caught.value.detail == {
        "error": "provider_switch_bundle_required",
        "capability": "llm",
    }
    assert existing["llm_api_key"] == "stored-secret"


def test_same_provider_model_only_edit_preserves_url_and_key() -> None:
    existing = {
        "llm_provider": "gemini",
        "llm_api_url": GEMINI_API_BASE_URL,
        "llm_api_key": "stored-secret",
        "llm_model_name": "old-model",
    }
    candidate = merge_credentials(existing, {"llm_model_name": "new-model"})
    assert candidate == {**existing, "llm_model_name": "new-model"}


def test_active_llm_endpoint_change_requires_a_fresh_atomic_bundle() -> None:
    existing = {
        "llm_provider": "outros",
        "llm_api_url": "https://old-llm.example/v1",
        "llm_api_key": "stored-secret",
        "llm_model_name": "stored-model",
    }
    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials(existing, {"llm_api_url": "https://new-llm.example/v1"})
    assert caught.value.detail == {
        "error": "provider_switch_bundle_required",
        "capability": "llm",
    }

    candidate = merge_credentials(
        existing,
        {
            "llm_provider": "outros",
            "llm_api_url": "https://new-llm.example/v1",
            "llm_api_key": "fresh-secret",
            "llm_model_name": "fresh-model",
        },
    )
    assert candidate["llm_api_key"] == "fresh-secret"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://generativelanguage.googleapis.com:443/v1beta/openai",
        "https://generativelanguage.googleapis.com:8443/v1beta/openai",
        "https://generativelanguage.googleapis.com/v1/openai",
        "https://generativelanguage.googleapis.com/v1beta/openai?x=1",
        "https://generativelanguage.googleapis.com/v1beta/openai#fragment",
        "https://user@generativelanguage.googleapis.com/v1beta/openai",
        "https://example.com/v1beta/openai",
    ],
)
def test_gemini_endpoint_is_exact_and_error_context_is_input_free(
    endpoint: str,
) -> None:
    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials(
            {},
            {
                "llm_provider": "gemini",
                "llm_api_url": endpoint,
                "llm_api_key": "SECRET-KEY",
                "llm_model_name": "gemini-model",
            },
        )
    assert caught.value.detail == {
        "error": "invalid_provider_endpoint",
        "capability": "llm",
        "provider": "gemini",
        "field": "llm_api_url",
    }
    rendered = f"{caught.value!s} {caught.value!r} {caught.value.detail!r}"
    assert endpoint not in rendered
    assert "SECRET-KEY" not in rendered


def test_gemini_is_operational_and_ready_after_promotion() -> None:
    assert PROVIDER_AVAILABILITY["llm"]["gemini"] == "active"
    assert "gemini" in OPERATIONAL_LLM_PROVIDERS
    status = credential_status(
        {
            "llm_provider": "gemini",
            "llm_api_url": GEMINI_API_BASE_URL,
            "llm_api_key": "secret",
            "llm_model_name": "gemini-model",
        }
    )
    assert status["llm_readiness"] == "active"
    assert status["apis_configured"] is True


@pytest.mark.parametrize(
    ("patch", "capability", "provider"),
    [
        pytest.param(
            {"audio_max_seconds": 45},
            "stt",
            "outros",
            id="stt_control",
        ),
        pytest.param(
            {
                "multimodal_text_fallback_enabled": True,
                "audio_max_seconds": 45,
            },
            "stt",
            "outros",
            id="mixed_controls",
        ),
    ],
)
def test_runtime_controls_cannot_bypass_coming_soon_provider_gate(
    patch: dict[str, object], capability: str, provider: str
) -> None:
    existing = {
        "multimodal_provider": "openrouter",
        "multimodal_api_url": MULTIMODAL_OPENROUTER_API_BASE_URL,
        "multimodal_api_key": "saved-secret",
        "multimodal_model_name": "saved-model",
        "multimodal_text_fallback_enabled": False,
        "stt_provider": "outros",
        "audio_max_seconds": 60,
    }
    snapshot = dict(existing)
    candidate = merge_credentials(existing, patch)

    with pytest.raises(CredentialPolicyError) as caught:
        validate_provider_availability(existing, candidate)

    assert caught.value.detail == {
        "error": "provider_not_available",
        "capability": capability,
        "provider": provider,
    }
    assert existing == snapshot


def test_active_llm_maintenance_and_identical_echo_pass() -> None:
    existing = {
        "llm_provider": "openai",
        "llm_api_url": "https://api.openai.com/v1",
        "llm_api_key": "legacy-secret",
        "llm_model_name": "legacy-model",
    }
    snapshot = dict(existing)
    changed = merge_credentials(existing, {"llm_model_name": "new-model"})

    validate_provider_availability(existing, changed)
    assert changed["llm_model_name"] == "new-model"
    assert existing == snapshot

    echoed = merge_credentials(
        existing,
        {
            "llm_provider": "openai",
            "llm_api_url": "https://api.openai.com/v1",
            "llm_api_key": "••••••••",
            "llm_model_name": "legacy-model",
        },
    )
    assert echoed == existing
    validate_provider_availability(existing, echoed)


LEGACY_STT_INCUMBENTS = [
    pytest.param(
        {
            "whisper_api_key": "legacy-stt-secret",
            "whisper_model": "legacy-whisper-model",
        },
        id="alias_only",
    ),
    pytest.param(
        {
            "stt_api_key": "canonical-stt-secret",
            "stt_model_name": "canonical-model",
            "stt_api_url": "https://speech.example/v1",
        },
        id="canonical_only",
    ),
    pytest.param(
        {
            "stt_api_key": "canonical-stt-secret",
            "whisper_model": "legacy-model",
            "stt_api_url": "https://speech.example/v1",
        },
        id="mixed",
    ),
    pytest.param(
        {
            "stt_provider": "outros",
            "stt_api_key": "explicit-secret",
            "stt_model_name": "explicit-model",
            "stt_api_url": "https://speech.example/v1",
        },
        id="explicit_outros",
    ),
]


@pytest.mark.parametrize("existing", LEGACY_STT_INCUMBENTS)
def test_stt_canonical_echo_preserves_raw_incumbent(existing: dict[str, str]) -> None:
    snapshot = dict(existing)
    resolved = resolve_stt_credentials(existing)
    echo = {
        "stt_provider": "outros",
        "stt_api_url": resolved.get("stt_api_url"),
        "stt_api_key": "••••••••" if resolved.get("stt_api_key") else None,
        "stt_model_name": resolved.get("stt_model_name"),
        "audio_max_seconds": resolved.get("audio_max_seconds"),
    }

    assert is_semantic_credentials_echo(existing, resolved, echo)
    assert existing == snapshot


@pytest.mark.parametrize("existing", LEGACY_STT_INCUMBENTS)
@pytest.mark.parametrize("provider", ["elevenlabs_scribe", "openrouter"])
def test_stt_incumbent_can_leave_only_with_fresh_fixed_bundle(
    existing: dict[str, str], provider: str
) -> None:
    patch = {
        "stt_provider": provider,
        "stt_api_url": STT_FIXED_API_BASES[provider],
        "stt_api_key": "fresh-stt-secret",
        "stt_model_name": "fresh-model",
    }
    resolved = resolve_stt_credentials(existing)
    candidate = merge_credentials(existing, patch)

    validate_provider_availability(
        existing,
        candidate,
        patch=patch,
        resolved_existing=resolved,
    )
    assert candidate["stt_provider"] == provider
    assert candidate["stt_api_url"] == STT_FIXED_API_BASES[provider]
    assert candidate["stt_api_key"] == "fresh-stt-secret"
    assert candidate["stt_model_name"] == "fresh-model"
    assert "whisper_api_key" not in candidate
    assert "whisper_model" not in candidate


@pytest.mark.parametrize(
    "existing",
    [
        {},
        {"stt_provider": "openai_whisper", "stt_api_key": "saved"},
        {"stt_provider": "elevenlabs_scribe", "stt_api_key": "saved"},
    ],
    ids=["empty", "other_provider", "explicit_new_provider"],
)
def test_stt_non_incumbents_can_enter_active_fixed_provider(
    existing: dict[str, str],
) -> None:
    patch = {
        "stt_provider": "openrouter",
        "stt_api_url": STT_FIXED_API_BASES["openrouter"],
        "stt_api_key": "fresh-stt-secret",
        "stt_model_name": "fresh-model",
    }
    candidate = merge_credentials(existing, patch)
    validate_provider_availability(
        existing,
        candidate,
        patch=patch,
        resolved_existing=resolve_stt_credentials(existing),
    )
    assert candidate["stt_provider"] == "openrouter"
    assert candidate["stt_api_url"] == STT_FIXED_API_BASES["openrouter"]


@pytest.mark.parametrize(
    "existing",
    [
        {"stt_api_url": "https://speech.example/v1"},
        {"stt_api_key": "only-key"},
        {"stt_model_name": "only-model"},
        {"whisper_api_key": "only-alias-key"},
        {"whisper_model": "only-alias-model"},
    ],
)
def test_every_single_effective_stt_field_is_an_eligible_incumbent(
    existing: dict[str, str],
) -> None:
    patch = {
        "stt_provider": "elevenlabs_scribe",
        "stt_api_url": STT_FIXED_API_BASES["elevenlabs_scribe"],
        "stt_api_key": "fresh-stt-secret",
        "stt_model_name": "fresh-model",
    }
    candidate = merge_credentials(existing, patch)
    validate_provider_availability(
        existing,
        candidate,
        patch=patch,
        resolved_existing=resolve_stt_credentials(existing),
    )


@pytest.mark.parametrize(
    "existing",
    [
        {},
        {"whisper_api_key": "legacy", "whisper_model": "legacy-model"},
        {
            "stt_provider": "openai_whisper",
            "stt_api_url": "https://api.openai.com/v1",
            "stt_api_key": "stored-secret",
            "stt_model_name": "stored-model",
        },
    ],
    ids=["new_tenant", "legacy_incumbent", "active_incumbent"],
)
@pytest.mark.parametrize("missing", ["stt_api_url", "stt_api_key", "stt_model_name"])
def test_stt_switch_requires_every_field_fresh(
    existing: dict[str, str], missing: str
) -> None:
    patch = {
        "stt_provider": "openrouter",
        "stt_api_url": STT_FIXED_API_BASES["openrouter"],
        "stt_api_key": "fresh-stt-secret",
        "stt_model_name": "fresh-model",
    }
    patch.pop(missing)
    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials(existing, patch)
    assert caught.value.detail == {
        "error": "provider_switch_bundle_required",
        "capability": "stt",
    }


@pytest.mark.parametrize(
    ("existing", "target_provider", "target_url"),
    [
        (
            {
                "stt_provider": "openrouter",
                "stt_api_url": STT_FIXED_API_BASES["openrouter"],
                "stt_api_key": "stored-secret",
                "stt_model_name": "stored-model",
            },
            "elevenlabs_scribe",
            STT_FIXED_API_BASES["elevenlabs_scribe"],
        ),
        (
            {
                "stt_provider": "elevenlabs_scribe",
                "stt_api_url": STT_FIXED_API_BASES["elevenlabs_scribe"],
                "stt_api_key": "stored-secret",
                "stt_model_name": "stored-model",
            },
            "openai_whisper",
            "https://api.openai.com/v1",
        ),
    ],
)
def test_active_stt_switch_requires_a_fresh_key(
    existing: dict[str, str], target_provider: str, target_url: str
) -> None:
    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials(
            existing,
            {
                "stt_provider": target_provider,
                "stt_api_url": target_url,
                "stt_api_key": "••••••••",
                "stt_model_name": "fresh-model",
            },
        )
    assert caught.value.detail == {
        "error": "provider_switch_bundle_required",
        "capability": "stt",
    }


@pytest.mark.parametrize(
    ("target_provider", "target_url", "target_voice"),
    [
        ("google", "https://texttospeech.googleapis.com/v1", None),
        ("openai", "https://api.openai.com/v1", "alloy"),
        ("elevenlabs", "https://api.elevenlabs.io/v1", "voice-123"),
    ],
)
def test_active_tts_switch_requires_a_fresh_complete_bundle(
    target_provider: str, target_url: str, target_voice: str | None
) -> None:
    existing_provider = "openai" if target_provider == "google" else "google"
    existing = {
        "tts_provider": existing_provider,
        "tts_api_url": (
            "https://api.openai.com/v1"
            if existing_provider == "openai"
            else "https://texttospeech.googleapis.com/v1"
        ),
        "tts_api_key": "stored-secret",
        "tts_model_name": "stored-model",
    }
    if existing_provider == "openai":
        existing["tts_voice_id"] = "stored-voice"
    patch: dict[str, object] = {
        "tts_provider": target_provider,
        "tts_api_url": target_url,
        "tts_api_key": "••••••••",
        "tts_model_name": "fresh-model",
    }
    if target_voice is not None:
        patch["tts_voice_id"] = target_voice
    with pytest.raises(CredentialPolicyError) as caught:
        merge_credentials(existing, patch)
    assert caught.value.detail == {
        "error": "provider_switch_bundle_required",
        "capability": "tts",
    }


def test_promoted_stt_ids_are_operational_and_publicly_active() -> None:
    assert {"elevenlabs_scribe", "openrouter"} <= OPERATIONAL_STT_PROVIDERS
    assert PROVIDER_AVAILABILITY["stt"]["openrouter"] == "active"
    assert PROVIDER_AVAILABILITY["stt"]["elevenlabs_scribe"] == "active"
    for provider, base_url in STT_FIXED_API_BASES.items():
        status = credential_status(
            {
                "stt_provider": provider,
                "stt_api_url": base_url,
                "stt_api_key": "secret",
                "stt_model_name": "model",
            }
        )
        assert status["stt_readiness"] == "active"
        projection = runtime_projection(
            {
                "stt_provider": provider,
                "stt_api_url": base_url,
                "stt_api_key": "secret",
                "stt_model_name": "model",
            }
        )
        assert projection["stt_api_url"] == base_url
        assert projection["stt_model_name"] == "model"
        assert "stt_api_key" not in projection
