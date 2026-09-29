"""HTTP contract tests for the JWT-only provider credential boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.schemas.config import ApiCredentialsPatch, TtsCredentialsPatch
from src.config.settings import settings
from src.repositories import get_db_session_dependency
from src.services.api_credentials import (
    GEMINI_API_BASE_URL,
    PROVIDER_AVAILABILITY,
    STT_FIXED_API_BASES,
    resolve_stt_credentials,
)

PATH = "/v1/config/api-credentials"


def _jwt_headers(
    role: str | None,
    tenant_id: UUID | None = None,
    *,
    builder_id: UUID | None = None,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id or uuid4()),
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    if role is not None:
        payload["role"] = role
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.mark.parametrize("role", ["clinic_admin", "builder", "super_admin"])
def test_allowed_jwt_roles_can_read_own_credentials(
    client: TestClient, role: str
) -> None:
    headers = _jwt_headers(role, builder_id=uuid4() if role == "builder" else None)
    assert client.get(PATH, headers=headers).status_code == 200


def test_tenant_api_key_and_forbidden_jwt_roles_fail_before_bucket_access(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_bucket_load(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("credential bucket was loaded before authorization")

    monkeypatch.setattr(
        "src.api.routes.config._get_or_create_config", unexpected_bucket_load
    )
    api_key = client.get(PATH, headers={"X-API-Key": "test-api-key"})
    assert api_key.status_code == 403
    assert api_key.json()["detail"] == {"error": "credentials_jwt_required"}

    for role in ("investor", None):
        response = client.get(PATH, headers=_jwt_headers(role))
        assert response.status_code == 403
        assert response.json()["detail"] == {"error": "credentials_role_forbidden"}


def test_clinic_admin_cannot_override_another_tenant(client: TestClient) -> None:
    response = client.get(
        f"{PATH}?tenant_id={uuid4()}",
        headers=_jwt_headers("clinic_admin", uuid4()),
    )
    assert response.status_code == 403


def _displayable(availability: str) -> list[tuple[str, str]]:
    return [
        (capability, provider)
        for capability, providers in PROVIDER_AVAILABILITY.items()
        for provider, state in providers.items()
        if state == availability
    ]


def _provider_field(capability: str) -> str:
    return "whatsapp_provider" if capability == "whatsapp" else f"{capability}_provider"


@pytest.mark.parametrize(("capability", "provider"), _displayable("coming_soon"))
def test_every_displayable_provider_change_is_exact_and_atomic(
    client: TestClient, capability: str, provider: str
) -> None:
    headers = _jwt_headers("clinic_admin")
    provider_field = _provider_field(capability)
    payload: dict[str, object] = {provider_field: provider}
    if capability == "llm" and provider == "gemini":
        payload.update(
            {
                "llm_api_url": GEMINI_API_BASE_URL,
                "llm_api_key": "fresh-gemini-key",
                "llm_model_name": "gemini-model",
            }
        )
    response = client.patch(PATH, headers=headers, json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "error": "provider_not_available",
        "capability": capability,
        "provider": provider,
    }
    assert client.get(PATH, headers=headers).json()[provider_field] is None


def test_new_tenant_can_save_and_read_configured_gemini(client: TestClient) -> None:
    tenant_id = uuid4()
    headers = _jwt_headers("clinic_admin", tenant_id)
    secret = "fresh-gemini-secret"
    response = client.patch(
        PATH,
        headers=headers,
        json={
            "llm_provider": "gemini",
            "llm_api_url": GEMINI_API_BASE_URL,
            "llm_api_key": secret,
            "llm_model_name": "gemini-2.5-flash",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["llm_provider"] == "gemini"
    assert body["llm_readiness"] == "active"
    assert body["llm_api_key_set"] is True
    assert body["llm_api_key_masked"] == "••••••••"
    assert secret not in response.text
    loaded = client.get(PATH, headers=headers)
    assert loaded.status_code == 200
    assert loaded.json()["llm_provider"] == "gemini"
    assert secret not in loaded.text


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://generativelanguage.googleapis.com:443/v1beta/openai",
        "https://generativelanguage.googleapis.com:8443/v1beta/openai",
        "https://generativelanguage.googleapis.com/v1/openai",
        "https://generativelanguage.googleapis.com/v1beta/openai?query=1",
        "https://generativelanguage.googleapis.com/v1beta/openai#fragment",
        "https://user@generativelanguage.googleapis.com/v1beta/openai",
        "https://example.com/v1beta/openai",
    ],
)
def test_gemini_endpoint_rejection_is_stable_secret_and_input_free(
    client: TestClient, endpoint: str
) -> None:
    secret = "GEMINI-SECRET-THAT-MUST-NOT-LEAK"
    response = client.patch(
        PATH,
        headers=_jwt_headers("clinic_admin"),
        json={
            "llm_provider": "gemini",
            "llm_api_url": endpoint,
            "llm_api_key": secret,
            "llm_model_name": "gemini-model",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "invalid_provider_endpoint",
        "capability": "llm",
        "provider": "gemini",
        "field": "llm_api_url",
    }
    assert endpoint not in response.text
    assert secret not in response.text


@pytest.mark.parametrize(
    ("existing_provider", "target_provider", "target_url"),
    [
        ("openai", "gemini", GEMINI_API_BASE_URL),
        ("gemini", "openai", "https://api.openai.com/v1"),
    ],
)
@pytest.mark.parametrize(
    "omitted_field",
    ["llm_provider", "llm_api_url", "llm_api_key", "llm_model_name"],
)
def test_gemini_switch_is_atomic_when_any_fresh_field_is_missing(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    existing_provider: str,
    target_provider: str,
    target_url: str,
    omitted_field: str,
) -> None:
    existing = {
        "llm_provider": existing_provider,
        "llm_api_url": (
            GEMINI_API_BASE_URL
            if existing_provider == "gemini"
            else "https://api.openai.com/v1"
        ),
        "llm_api_key": "stored-secret",
        "llm_model_name": "stored-model",
    }
    row = SimpleNamespace(api_credentials=dict(existing))
    calls: list[str] = []

    class SessionSpy:
        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return row

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    payload = {
        "llm_provider": target_provider,
        "llm_api_url": target_url,
        "llm_api_key": "fresh-secret",
        "llm_model_name": "fresh-model",
    }
    payload.pop(omitted_field)
    try:
        response = client.patch(
            PATH, headers=_jwt_headers("clinic_admin"), json=payload
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "provider_switch_bundle_required",
        "capability": "llm",
    }
    assert row.api_credentials == existing
    assert calls == []


def test_invalid_payload_and_unsafe_url_return_stable_secret_free_422(
    client: TestClient,
) -> None:
    headers = _jwt_headers("clinic_admin")
    sentinel = "credential-value-that-must-not-leak"
    invalid = client.patch(
        PATH,
        headers=headers,
        json={"llm_api_key": sentinel, "unexpected_credential": "value"},
    )
    assert invalid.status_code == 422
    invalid_detail = invalid.json()["detail"]
    assert invalid_detail["error"] == "invalid_credentials_payload"
    assert invalid_detail["fields"] == [
        {"field": "unexpected_credential", "code": "extra_forbidden"}
    ]
    assert sentinel not in invalid.text

    unsafe = client.patch(
        PATH,
        headers=headers,
        json={
            "llm_provider": "outros",
            "llm_api_url": "http://127.0.0.1/private",
            "llm_api_key": sentinel,
        },
    )
    assert unsafe.status_code == 422
    assert unsafe.json()["detail"] == {
        "error": "unsafe_api_url",
        "field": "llm_api_url",
    }
    assert sentinel not in unsafe.text


def test_openapi_uses_typed_credential_patch_and_validation_never_echoes_input(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    schema = client.get("/openapi.json").json()
    request_schema = schema["paths"][PATH]["patch"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert request_schema["$ref"].endswith("/ApiCredentialsPatch")
    stt_provider_schema = schema["components"]["schemas"]["ApiCredentialsPatch"][
        "properties"
    ]["stt_provider"]
    assert "openrouter" in stt_provider_schema["anyOf"][0]["enum"]
    description = schema["paths"][PATH]["patch"]["description"]
    assert "Public provider availability is unchanged" in description
    assert "All displayable providers are currently coming soon" not in description

    headers = _jwt_headers("clinic_admin")
    sentinel = "submitted-secret-sentinel"
    for payload in (
        {"llm_provider": {"nested": sentinel}},
        {"llm_api_key": sentinel, "audio_max_seconds": "60"},
        sentinel,
    ):
        response = client.patch(PATH, headers=headers, json=payload)
        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "invalid_credentials_payload"
        assert sentinel not in response.text
    assert sentinel not in caplog.text


def test_pydantic_credential_types_and_bounds_are_strict() -> None:
    prefix = "https://speech.example/"
    valid_url = prefix + "x" * (2048 - len(prefix))
    valid = ApiCredentialsPatch.model_validate(
        {
            "stt_provider": "outros",
            "stt_api_url": valid_url,
            "stt_api_key": "k" * 8192,
            "stt_model_name": "m" * 255,
            "audio_max_seconds": 60,
            "zapi_inbound_token_digest": "a" * 64,
        }
    )
    assert len(valid.stt_api_url or "") == 2048
    assert valid.audio_max_seconds == 60

    invalid_payloads: tuple[dict[str, Any], ...] = (
        {"audio_max_seconds": "60"},
        {"audio_max_seconds": 9},
        {"audio_max_seconds": 301},
        {"stt_model_name": "m" * 256},
        {"stt_api_key": "k" * 8193},
        {"stt_api_url": valid_url + "x"},
        {"stt_provider": 7},
        {"multimodal_provider": "groq"},
        {"multimodal_provider": "openai"},
        {"tts_provider": "microsoft"},
        {"tts_reply_policy": None},
        {"tts_reply_policy": "audio_output"},
        {"zapi_inbound_token_digest": "A" * 64},
        {"zapi_inbound_token_digest": "a" * 63},
    )
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            ApiCredentialsPatch.model_validate(payload)


def test_tts_reply_policy_default_is_not_an_implicit_patch() -> None:
    omitted = ApiCredentialsPatch.model_validate({})
    assert omitted.tts_reply_policy == "audio_input"
    assert "tts_reply_policy" not in omitted.model_fields_set
    assert omitted.model_dump(exclude_unset=True) == {}

    explicit = ApiCredentialsPatch.model_validate({"tts_reply_policy": "audio_input"})
    assert explicit.model_fields_set == {"tts_reply_policy"}
    assert explicit.model_dump(exclude_unset=True) == {
        "tts_reply_policy": "audio_input"
    }


def test_openai_tts_provider_validates_at_the_schema_boundary() -> None:
    patch = TtsCredentialsPatch.model_validate({"tts_provider": "openai"})
    assert patch.tts_provider == "openai"
    assert patch.model_dump(exclude_unset=True) == {"tts_provider": "openai"}


def test_tts_control_patch_requires_bundle_and_invalid_policy_is_atomic(
    client: TestClient,
) -> None:
    headers = _jwt_headers("clinic_admin")
    initial = client.get(PATH, headers=headers)
    assert initial.status_code == 200
    assert initial.json()["tts_reply_policy"] == "audio_input"

    for control_patch in (
        {"tts_reply_policy": "all_inputs"},
        {"tts_enabled": True},
        {"tts_enabled": True, "tts_reply_policy": "all_inputs"},
    ):
        refused = client.patch(PATH, headers=headers, json=control_patch)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == {
            "error": "provider_not_available",
            "capability": "tts",
            "provider": None,
        }

    for invalid_policy in (None, "audio_output"):
        invalid = client.patch(
            PATH,
            headers=headers,
            json={"tts_reply_policy": invalid_policy},
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["error"] == "invalid_credentials_payload"

    unchanged = client.get(PATH, headers=headers)
    assert unchanged.status_code == 200
    assert unchanged.json()["tts_reply_policy"] == "audio_input"
    assert unchanged.json()["tts_enabled"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"tts_reply_policy": "all_inputs"},
        {"tts_enabled": True},
        {"tts_enabled": True, "tts_reply_policy": "all_inputs"},
    ],
)
def test_control_only_patch_cannot_change_a_coming_soon_tts_bundle(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    existing = {
        "tts_provider": "google",
        "tts_api_url": "https://texttospeech.googleapis.com/v1",
        "tts_api_key": "saved-secret-that-must-not-leak",
        "tts_model_name": "pt-br-voice",
        "tts_voice_id": "saved-voice",
        "tts_enabled": False,
    }
    row = SimpleNamespace(api_credentials=dict(existing))
    calls: list[str] = []

    class SessionSpy:
        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return row

    async def no_cache_invalidation(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache",
        no_cache_invalidation,
    )
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    try:
        response = client.patch(
            PATH,
            headers=_jwt_headers("clinic_admin"),
            json=payload,
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "error": "provider_not_available",
        "capability": "tts",
        "provider": "google",
    }
    assert "saved-secret-that-must-not-leak" not in response.text
    assert calls == []
    assert row.api_credentials == existing


def test_invalid_merge_does_not_create_or_flush_missing_config(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class SessionSpy:
        def add(self, row: Any) -> None:
            calls.append("add")

        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

    session = SessionSpy()

    async def override_session() -> Any:
        yield session

    async def missing_config(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("src.api.routes.config._load_config", missing_config)
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    try:
        response = client.patch(
            PATH,
            headers=_jwt_headers("clinic_admin"),
            json={"llm_provider": "slm_cortex"},
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "provider_not_available",
        "capability": "llm",
        "provider": "slm_cortex",
    }
    assert calls == []


@pytest.mark.parametrize(
    ("role", "target_exists", "expected_status"),
    [
        ("builder", True, 200),
        ("builder", False, 403),
        ("clinic_admin", True, 403),
        ("super_admin", True, 200),
        ("super_admin", False, 404),
    ],
)
def test_target_tenant_is_authorized_before_credential_bucket_load(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    target_exists: bool,
    expected_status: int,
) -> None:
    target = uuid4()
    bucket_loads: list[UUID] = []

    class Result:
        def scalar_one_or_none(self) -> UUID | None:
            return target if target_exists else None

    class Session:
        async def execute(self, statement: Any) -> Result:
            if role == "clinic_admin":
                raise AssertionError("clinic-admin denial queried the target")
            return Result()

    async def override_session() -> Any:
        yield Session()

    async def bucket(*args: Any, **kwargs: Any) -> Any:
        bucket_loads.append(target)
        return SimpleNamespace(api_credentials={})

    monkeypatch.setattr("src.api.routes.config._get_or_create_config", bucket)
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    try:
        response = client.get(
            f"{PATH}?tenant_id={target}",
            headers=_jwt_headers(
                role,
                builder_id=uuid4() if role == "builder" else None,
            ),
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == expected_status
    assert bucket_loads == ([target] if expected_status == 200 else [])
    if expected_status == 403:
        assert response.json()["detail"] == {"error": "target_tenant_forbidden"}
    if expected_status == 404:
        assert response.json()["detail"] == {"error": "target_tenant_not_found"}


# ============================================
# multimodal_text_fallback_enabled (Wave 1, deliverable 2)
# ============================================
#
# The HTTP surface for the opt-in fallback gate. Six cases below cover
# (1) legacy default, (2)+(3) round-trip both booleans, (4)-(6) atomic 422
# rejection of `null` / string / number, and (7) preservation under an
# unrelated PATCH. Every response body is scanned so the gate never leaks
# secret material, and the merge is asserted to remain atomic (a rejected
# PATCH must NOT flip the stored value halfway).


def test_multimodal_text_fallback_gate_defaults_to_false_and_is_secret_free(
    client: TestClient,
) -> None:
    """Legacy tenant has no gate value; GET must project `false` explicitly."""
    headers = _jwt_headers("clinic_admin")
    read = client.get(PATH, headers=headers)
    assert read.status_code == 200, read.text
    data = read.json()
    assert data["multimodal_text_fallback_enabled"] is False
    # The gate is a non-secret boolean; the GET must not carry a `_set` /
    # `_masked` twin (those are reserved for the SECRET_FIELDS projection).
    assert "multimodal_text_fallback_enabled_set" not in data
    assert "multimodal_text_fallback_enabled_masked" not in data


@pytest.mark.parametrize(
    ("payload", "capability", "provider"),
    [
        pytest.param(
            {"multimodal_text_fallback_enabled": True},
            "multimodal",
            "openrouter",
            id="multimodal_control",
        ),
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
            "multimodal",
            "openrouter",
            id="mixed_controls",
        ),
    ],
)
def test_runtime_controls_cannot_bypass_coming_soon_provider_gate_atomically(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    capability: str,
    provider: str,
) -> None:
    existing = {
        "multimodal_provider": "openrouter",
        "multimodal_api_key": "saved-secret-that-must-not-leak",
        "multimodal_text_fallback_enabled": False,
        "stt_provider": "outros",
        "stt_api_key": "saved-stt-secret-that-must-not-leak",
        "audio_max_seconds": 60,
    }
    row = SimpleNamespace(api_credentials=dict(existing))
    calls: list[str] = []

    class SessionSpy:
        def add(self, row: Any) -> None:
            calls.append("add")

        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return row

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    try:
        response = client.patch(
            PATH,
            headers=_jwt_headers("clinic_admin"),
            json=payload,
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "error": "provider_not_available",
        "capability": capability,
        "provider": provider,
    }
    assert "saved-secret-that-must-not-leak" not in response.text
    assert "saved-stt-secret-that-must-not-leak" not in response.text
    assert calls == []
    assert row.api_credentials == existing


@pytest.mark.parametrize(
    "bad_value",
    [
        None,  # explicit `null` in the JSON body
        "true",  # string never silently coerced to bool
        1,  # number never silently coerced to bool
    ],
)
def test_multimodal_text_fallback_gate_rejects_null_and_wrong_type_atomically(
    client: TestClient, bad_value: Any
) -> None:
    """422 with no partial write: an invalid patch keeps the legacy default."""
    headers = _jwt_headers("clinic_admin")
    refused = client.patch(
        PATH,
        headers=headers,
        json={"multimodal_text_fallback_enabled": bad_value},
    )
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    # Stable, secret-free error surface — matches the rest of the credential
    # boundary rather than leaking the raw Pydantic field path or the sent
    # value.
    assert detail["error"] == "invalid_credentials_payload"
    assert any(
        entry.get("field") == "multimodal_text_fallback_enabled"
        for entry in detail["fields"]
    )
    # No mutation: the legacy default stayed `false`.
    assert (
        client.get(PATH, headers=headers).json()["multimodal_text_fallback_enabled"]
        is False
    )


def test_runtime_control_legacy_echo_is_accepted_without_mutation(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = {
        "multimodal_provider": "openrouter",
        "multimodal_text_fallback_enabled": True,
        "stt_provider": "outros",
        "audio_max_seconds": 45,
    }
    row = SimpleNamespace(api_credentials=dict(existing))
    calls: list[str] = []

    class SessionSpy:
        def add(self, row: Any) -> None:
            calls.append("add")

        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return row

    async def no_cache_invalidation(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache",
        no_cache_invalidation,
    )
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    try:
        response = client.patch(
            PATH,
            headers=_jwt_headers("clinic_admin"),
            json={
                "multimodal_text_fallback_enabled": True,
                "audio_max_seconds": 45,
            },
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 200, response.text
    assert response.json()["multimodal_text_fallback_enabled"] is True
    assert response.json()["audio_max_seconds"] == 45
    assert calls == []
    assert row.api_credentials == existing


def test_legacy_echo_passes_while_maintenance_and_switch_stay_atomic(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = {
        "llm_provider": "openai",
        "llm_api_url": "https://api.openai.com/v1",
        "llm_api_key": "legacy-secret",
        "llm_model_name": "legacy-model",
    }
    row = SimpleNamespace(api_credentials=dict(existing))
    calls: list[str] = []

    class SessionSpy:
        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return row

    async def no_cache_invalidation(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache",
        no_cache_invalidation,
    )
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    headers = _jwt_headers("clinic_admin")
    try:
        echo = client.patch(
            PATH,
            headers=headers,
            json={
                "llm_provider": "openai",
                "llm_api_url": "HTTPS://API.OPENAI.COM:443/v1/",
                "llm_api_key": "••••••••",
                "llm_model_name": "legacy-model",
            },
        )
        assert echo.status_code == 200, echo.text
        assert echo.json()["llm_api_url"] == "https://api.openai.com/v1"
        assert calls == []
        assert row.api_credentials == existing

        calls.clear()
        maintained = client.patch(
            PATH,
            headers=headers,
            json={"llm_model_name": "new-model"},
        )
        assert maintained.status_code == 422, maintained.text
        assert maintained.json()["detail"] == {
            "error": "provider_not_available",
            "capability": "llm",
            "provider": "openai",
        }
        assert calls == []
        assert row.api_credentials == existing

        switched = client.patch(
            PATH,
            headers=headers,
            json={"llm_provider": "slm_cortex"},
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert switched.status_code == 422, switched.text
    assert switched.json()["detail"] == {
        "error": "provider_not_available",
        "capability": "llm",
        "provider": "slm_cortex",
    }
    assert calls == []
    assert row.api_credentials == existing


@pytest.mark.parametrize(
    "existing",
    [
        {},
        {
            "tts_provider": "unknown",
            "tts_api_url": "https://tts.example/v1",
            "tts_api_key": "saved-secret-that-must-not-leak",
            "tts_model_name": "legacy-model",
            "tts_voice_id": "legacy-voice",
        },
    ],
    ids=["provider_missing", "provider_unknown"],
)
@pytest.mark.parametrize(
    "config_patch",
    [
        {"tts_api_url": "https://tts.example/v2"},
        {"tts_api_key": "replacement-secret-that-must-not-leak"},
        {"tts_model_name": "replacement-model"},
        {"tts_voice_id": "replacement-voice"},
    ],
)
@pytest.mark.parametrize("mixed_with_controls", [False, True])
def test_tts_config_mutations_without_known_provider_are_atomic(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    existing: dict[str, object],
    config_patch: dict[str, object],
    mixed_with_controls: bool,
) -> None:
    row = SimpleNamespace(api_credentials=dict(existing))
    calls: list[str] = []

    class SessionSpy:
        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return row

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    payload = dict(config_patch)
    if mixed_with_controls:
        payload.update({"tts_enabled": True, "tts_reply_policy": "all_inputs"})
    try:
        response = client.patch(
            PATH,
            headers=_jwt_headers("clinic_admin"),
            json=payload,
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "error": "provider_not_available",
        "capability": "tts",
        "provider": existing.get("tts_provider"),
    }
    assert "replacement-secret-that-must-not-leak" not in response.text
    assert "saved-secret-that-must-not-leak" not in response.text
    assert calls == []
    assert row.api_credentials == existing


STT_INCUMBENT_FIXTURES = [
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
    pytest.param({"stt_api_url": "https://speech.example/v1"}, id="url_only"),
    pytest.param({"stt_api_key": "key-only"}, id="key_only"),
    pytest.param({"stt_model_name": "model-only"}, id="model_only"),
    pytest.param({"whisper_api_key": "alias-key-only"}, id="alias_key_only"),
    pytest.param({"whisper_model": "alias-model-only"}, id="alias_model_only"),
]


def _canonical_stt_echo(existing: dict[str, object]) -> dict[str, object]:
    resolved = resolve_stt_credentials(existing)
    return {
        "stt_provider": "outros",
        "stt_api_url": resolved.get("stt_api_url"),
        "stt_api_key": "••••••••" if resolved.get("stt_api_key") else None,
        "stt_model_name": resolved.get("stt_model_name"),
        "audio_max_seconds": resolved.get("audio_max_seconds"),
    }


@pytest.mark.parametrize("existing", STT_INCUMBENT_FIXTURES)
def test_stt_incumbent_canonical_echo_is_zero_effect_and_preserves_raw_json(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    existing: dict[str, object],
) -> None:
    row = SimpleNamespace(api_credentials=dict(existing))
    snapshot = dict(existing)
    calls: list[str] = []

    class SessionSpy:
        def add(self, value: Any) -> None:
            calls.append("add")

        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

        async def rollback(self) -> None:
            calls.append("rollback")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> Any:
        return row

    async def invalidate(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    def emit(**kwargs: Any) -> None:
        calls.append("emit")

    network = AsyncMock()
    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", invalidate
    )
    monkeypatch.setattr("src.api.routes.config._emit_api_credentials_changed", emit)
    monkeypatch.setattr("src.services.stt_service.transcribe_audio", network)
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    try:
        response = client.patch(
            PATH,
            headers=_jwt_headers("clinic_admin"),
            json=_canonical_stt_echo(existing),
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 200, response.text
    assert calls == []
    assert network.await_count == 0
    assert row.api_credentials == snapshot


@pytest.mark.parametrize("provider", ["elevenlabs_scribe", "openrouter"])
def test_stt_incumbent_switch_orders_commit_cache_emit_and_redacts_payload(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    existing = {
        "whisper_api_key": "legacy-key-sentinel",
        "whisper_model": "legacy-model-sentinel",
    }
    row = SimpleNamespace(api_credentials=dict(existing))
    calls: list[str] = []
    emitted: dict[str, Any] = {}

    class SessionSpy:
        def add(self, value: Any) -> None:
            calls.append("add")

        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

        async def rollback(self) -> None:
            calls.append("rollback")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> Any:
        return row

    async def invalidate(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    def emit(event: str, **kwargs: Any) -> None:
        calls.append("emit")
        emitted["event"] = event
        emitted.update(kwargs)

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", invalidate
    )
    monkeypatch.setattr("src.api.routes.config._audit_logger.info", emit)
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    payload = {
        "stt_provider": provider,
        "stt_api_url": STT_FIXED_API_BASES[provider],
        "stt_api_key": "fresh-key-sentinel",
        "stt_model_name": "fresh-model-sentinel",
    }
    try:
        response = client.patch(
            PATH, headers=_jwt_headers("clinic_admin"), json=payload
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 200, response.text
    assert calls == ["flush", "commit", "cache", "emit"]
    assert row.api_credentials == payload
    assert emitted["event"] == "api_credentials_changed"
    assert emitted["capability"] == "stt"
    assert emitted["previous_provider"] == "outros"
    assert emitted["new_provider"] == provider
    assert emitted["endpoint_class"] == "fixed"
    assert emitted["fields"] == (
        "stt_provider",
        "stt_api_url",
        "stt_api_key",
        "stt_model_name",
    )
    rendered = repr(emitted)
    for secret_value in (
        "legacy-key-sentinel",
        "legacy-model-sentinel",
        "fresh-key-sentinel",
        "fresh-model-sentinel",
        STT_FIXED_API_BASES[provider],
    ):
        assert secret_value not in rendered


@pytest.mark.parametrize(
    ("existing", "payload", "error"),
    [
        (
            {},
            {
                "stt_provider": "openrouter",
                "stt_api_url": STT_FIXED_API_BASES["openrouter"],
                "stt_api_key": "fresh-key",
                "stt_model_name": "fresh-model",
            },
            "provider_not_available",
        ),
        (
            {"stt_provider": "openai_whisper", "stt_api_key": "saved"},
            {
                "stt_provider": "openrouter",
                "stt_api_url": STT_FIXED_API_BASES["openrouter"],
                "stt_api_key": "fresh-key",
                "stt_model_name": "fresh-model",
            },
            "provider_not_available",
        ),
        (
            {"whisper_api_key": "legacy"},
            {
                "stt_provider": "openrouter",
                "stt_api_url": "",
                "stt_api_key": "fresh-key",
                "stt_model_name": "fresh-model",
            },
            "provider_switch_bundle_required",
        ),
        (
            {"whisper_api_key": "legacy"},
            {
                "stt_provider": "openrouter",
                "stt_api_url": "   ",
                "stt_api_key": "fresh-key",
                "stt_model_name": "fresh-model",
            },
            "provider_switch_bundle_required",
        ),
        (
            {"whisper_api_key": "legacy"},
            {
                "stt_provider": "openrouter",
                "stt_api_url": None,
                "stt_api_key": "fresh-key",
                "stt_model_name": "fresh-model",
            },
            "provider_switch_bundle_required",
        ),
        (
            {"whisper_api_key": "legacy"},
            {
                "stt_provider": "openrouter",
                "stt_api_url": STT_FIXED_API_BASES["openrouter"],
                "stt_api_key": "••••••••",
                "stt_model_name": "fresh-model",
            },
            "provider_switch_bundle_required",
        ),
        (
            {"whisper_api_key": "legacy"},
            {
                "stt_provider": "openrouter",
                "stt_api_url": STT_FIXED_API_BASES["openrouter"],
                "stt_api_key": "",
                "stt_model_name": "fresh-model",
            },
            "provider_switch_bundle_required",
        ),
        (
            {"whisper_api_key": "legacy"},
            {
                "stt_provider": "openrouter",
                "stt_api_url": STT_FIXED_API_BASES["openrouter"],
                "stt_api_key": "   ",
                "stt_model_name": "fresh-model",
            },
            "provider_switch_bundle_required",
        ),
        (
            {"stt_provider": "outros", "stt_api_key": "legacy"},
            {"audio_max_seconds": 45},
            "provider_not_available",
        ),
        (
            {
                "stt_provider": "openrouter",
                "stt_api_url": STT_FIXED_API_BASES["openrouter"],
                "stt_api_key": "saved",
                "stt_model_name": "saved-model",
            },
            {"stt_model_name": "replacement"},
            "provider_not_available",
        ),
        ({}, {"stt_provider": "outros"}, "provider_not_available"),
    ],
)
def test_stt_rejected_transitions_have_zero_effects(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    existing: dict[str, object],
    payload: dict[str, object],
    error: str,
) -> None:
    row = SimpleNamespace(api_credentials=dict(existing))
    snapshot = dict(existing)
    calls: list[str] = []

    class SessionSpy:
        def add(self, value: Any) -> None:
            calls.append("add")

        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

        async def rollback(self) -> None:
            calls.append("rollback")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> Any:
        return row

    async def invalidate(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    def emit(**kwargs: Any) -> None:
        calls.append("emit")

    network = AsyncMock()
    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", invalidate
    )
    monkeypatch.setattr("src.api.routes.config._emit_api_credentials_changed", emit)
    monkeypatch.setattr("src.services.stt_service.transcribe_audio", network)
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    try:
        response = client.patch(
            PATH, headers=_jwt_headers("clinic_admin"), json=payload
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == error
    assert calls == []
    assert network.await_count == 0
    assert row.api_credentials == snapshot


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://openrouter.ai:443/api/v1",
        "https://openrouter.ai:8443/api/v1",
        "https://openrouter.ai/api/v1/audio/transcriptions",
        "https://openrouter.ai/api/v1?query=sentinel",
        "https://openrouter.ai/api/v1#fragment",
        "https://user@openrouter.ai/api/v1",
        "https://evil.example/api/v1",
    ],
)
def test_stt_fixed_endpoint_rejection_uses_general_secret_free_translation(
    client: TestClient, endpoint: str
) -> None:
    response = client.patch(
        PATH,
        headers=_jwt_headers("clinic_admin"),
        json={
            "stt_provider": "openrouter",
            "stt_api_url": endpoint,
            "stt_api_key": "secret-sentinel",
            "stt_model_name": "model-sentinel",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "invalid_provider_endpoint",
        "capability": "stt",
        "provider": "openrouter",
        "field": "stt_api_url",
    }
    assert endpoint not in response.text
    assert "secret-sentinel" not in response.text
    assert "model-sentinel" not in response.text


def test_stt_switch_emitter_failure_is_counted_once_without_rollback(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = SimpleNamespace(api_credentials={"whisper_api_key": "legacy"})
    calls: list[str] = []

    class SessionSpy:
        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")

        async def rollback(self) -> None:
            calls.append("rollback")

    class CounterSpy:
        def inc(self) -> None:
            calls.append("counter")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> Any:
        return row

    async def invalidate(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    def fail_emit(**kwargs: Any) -> None:
        calls.append("emit")
        raise RuntimeError("emitter-secret-sentinel")

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", invalidate
    )
    monkeypatch.setattr(
        "src.api.routes.config._emit_api_credentials_changed", fail_emit
    )
    monkeypatch.setattr(
        "src.api.routes.config.API_CREDENTIALS_CHANGE_EMIT_FAILURES_TOTAL",
        CounterSpy(),
    )
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    try:
        response = client.patch(
            PATH,
            headers=_jwt_headers("clinic_admin"),
            json={
                "stt_provider": "openrouter",
                "stt_api_url": STT_FIXED_API_BASES["openrouter"],
                "stt_api_key": "fresh-key",
                "stt_model_name": "fresh-model",
            },
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 200, response.text
    assert calls == ["flush", "commit", "cache", "emit", "counter"]
    assert "emitter-secret-sentinel" not in caplog.text


def test_stt_switch_commit_failure_rolls_back_without_cache_emit_or_counter(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(api_credentials={"whisper_api_key": "legacy"})
    calls: list[str] = []

    class SessionSpy:
        async def flush(self) -> None:
            calls.append("flush")

        async def commit(self) -> None:
            calls.append("commit")
            raise RuntimeError("commit failed")

        async def rollback(self) -> None:
            calls.append("rollback")

    class CounterSpy:
        def inc(self) -> None:
            calls.append("counter")

    async def override_session() -> Any:
        yield SessionSpy()

    async def load_existing(*args: Any, **kwargs: Any) -> Any:
        return row

    async def invalidate(*args: Any, **kwargs: Any) -> None:
        calls.append("cache")

    def emit(**kwargs: Any) -> None:
        calls.append("emit")

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    monkeypatch.setattr(
        "src.services.event_service.invalidate_clinic_config_cache", invalidate
    )
    monkeypatch.setattr("src.api.routes.config._emit_api_credentials_changed", emit)
    monkeypatch.setattr(
        "src.api.routes.config.API_CREDENTIALS_CHANGE_EMIT_FAILURES_TOTAL",
        CounterSpy(),
    )
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            client.patch(
                PATH,
                headers=_jwt_headers("clinic_admin"),
                json={
                    "stt_provider": "openrouter",
                    "stt_api_url": STT_FIXED_API_BASES["openrouter"],
                    "stt_api_key": "fresh-key",
                    "stt_model_name": "fresh-model",
                },
            )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert calls == ["flush", "commit", "rollback"]
