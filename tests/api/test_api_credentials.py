"""HTTP contract tests for the JWT-only provider credential boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.schemas.config import ApiCredentialsPatch
from src.config.settings import settings
from src.repositories import get_db_session_dependency
from src.services.api_credentials import PROVIDER_AVAILABILITY

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


@pytest.mark.parametrize(
    ("capability", "provider"),
    [
        ("whatsapp", "meta_cloud"),
        ("whatsapp", "zapi"),
        ("whatsapp", "uazapi"),
        ("whatsapp", "evolution"),
        ("whatsapp", "dialog360"),
        ("whatsapp", "twilio"),
        ("llm", "dropout"),
        ("llm", "openai"),
        ("llm", "outros"),
        ("llm", "slm_cortex"),
        ("multimodal", "openrouter"),
        ("stt", "outros"),
        ("stt", "openai_whisper"),
        ("stt", "elevenlabs_scribe"),
        ("stt", "google_stt"),
        ("stt", "deepgram_stt"),
        ("stt", "microsoft_stt"),
        ("tts", "elevenlabs"),
        ("tts", "google"),
    ],
)
def test_all_displayable_provider_changes_are_exact_and_atomic(
    client: TestClient, capability: str, provider: str
) -> None:
    headers = _jwt_headers("clinic_admin")
    provider_field = (
        "whatsapp_provider" if capability == "whatsapp" else f"{capability}_provider"
    )
    response = client.patch(PATH, headers=headers, json={provider_field: provider})
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "error": "provider_not_available",
        "capability": capability,
        "provider": provider,
    }
    assert client.get(PATH, headers=headers).json()[provider_field] is None


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
    ("payload", "expected_controls"),
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
            {"tts_enabled": True, "tts_reply_policy": "all_inputs"},
            {"tts_enabled": True, "tts_reply_policy": "all_inputs"},
        ),
    ],
)
def test_control_only_patch_preserves_complete_saved_tts_bundle(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    expected_controls: dict[str, object],
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

    assert response.status_code == 200, response.text
    assert response.json()["tts_enabled"] is expected_controls["tts_enabled"]
    assert response.json()["tts_reply_policy"] == expected_controls["tts_reply_policy"]
    assert response.json()["tts_readiness"] == "active"
    assert "saved-secret-that-must-not-leak" not in response.text
    assert calls == ["flush", "commit"]
    assert row.api_credentials["tts_api_key"] == existing["tts_api_key"]
    for field in (
        "tts_provider",
        "tts_api_url",
        "tts_model_name",
        "tts_voice_id",
    ):
        assert row.api_credentials[field] == existing[field]


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
            json={"llm_provider": "openai"},
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "provider_not_available",
        "capability": "llm",
        "provider": "openai",
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


def test_multimodal_text_fallback_gate_round_trips_true_and_false(
    client: TestClient,
) -> None:
    headers = _jwt_headers("clinic_admin")
    enabled = client.patch(
        PATH,
        headers=headers,
        json={"multimodal_text_fallback_enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["multimodal_text_fallback_enabled"] is True
    assert (
        client.get(PATH, headers=headers).json()["multimodal_text_fallback_enabled"]
        is True
    )

    disabled = client.patch(
        PATH,
        headers=headers,
        json={"multimodal_text_fallback_enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["multimodal_text_fallback_enabled"] is False
    assert (
        client.get(PATH, headers=headers).json()["multimodal_text_fallback_enabled"]
        is False
    )


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
    """422 with no partial write: a rejected patch keeps the previous value.

    First enable the gate, then submit a bad type, then confirm GET still
    reports `true` — proving the rejection is atomic and did not overwrite
    the stored value halfway.
    """
    headers = _jwt_headers("clinic_admin")
    setup = client.patch(
        PATH,
        headers=headers,
        json={"multimodal_text_fallback_enabled": True},
    )
    assert setup.status_code == 200, setup.text

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
    # No mutation: the stored value stayed `true`.
    assert (
        client.get(PATH, headers=headers).json()["multimodal_text_fallback_enabled"]
        is True
    )


def test_multimodal_text_fallback_gate_preserved_by_unrelated_patch(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A patch touching an unrelated capability must not flip or clear the gate."""
    monkeypatch.setitem(PROVIDER_AVAILABILITY["llm"], "openai", "active")
    headers = _jwt_headers("clinic_admin")
    on = client.patch(
        PATH,
        headers=headers,
        json={"multimodal_text_fallback_enabled": True},
    )
    assert on.status_code == 200, on.text

    unrelated = client.patch(
        PATH,
        headers=headers,
        json={
            "llm_provider": "openai",
            "llm_api_url": "https://api.openai.com/v1",
            "llm_api_key": "unrelated-llm-secret",
            "llm_model_name": "gpt-model",
        },
    )
    assert unrelated.status_code == 200, unrelated.text
    data = unrelated.json()
    # Gate unchanged
    assert data["multimodal_text_fallback_enabled"] is True
    # And the LLM patch actually landed
    assert data["llm_provider"] == "openai"
    assert data["llm_api_key_set"] is True

    reread = client.get(PATH, headers=headers).json()
    assert reread["multimodal_text_fallback_enabled"] is True


def test_legacy_echo_and_maintenance_persist_while_a_switch_stays_atomic(
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
        assert calls == ["flush", "commit"]
        assert row.api_credentials == existing

        calls.clear()
        maintained = client.patch(
            PATH,
            headers=headers,
            json={"llm_model_name": "new-model"},
        )
        assert maintained.status_code == 200, maintained.text
        assert calls == ["flush", "commit"]
        assert row.api_credentials["llm_model_name"] == "new-model"
        assert row.api_credentials["llm_api_key"] == "legacy-secret"

        row.api_credentials = dict(existing)
        calls.clear()
        switched = client.patch(
            PATH,
            headers=headers,
            json={"llm_provider": "dropout"},
        )
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert switched.status_code == 422, switched.text
    assert switched.json()["detail"] == {
        "error": "provider_not_available",
        "capability": "llm",
        "provider": "dropout",
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


def test_tts_provider_switch_and_clear_are_atomic(
    client: TestClient,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr("src.api.routes.config._load_config", load_existing)
    test_app.dependency_overrides[get_db_session_dependency] = override_session
    rejected_mutations = (
        {"tts_provider": None},
        {"tts_provider": "elevenlabs"},
    )
    maintenance_mutations = (
        {"tts_api_url": "https://texttospeech.example/v2"},
        {"tts_api_key": "replacement-secret-that-must-not-leak"},
        {"tts_model_name": "replacement-model"},
        {"tts_voice_id": "replacement-voice"},
    )
    try:
        for config_patch in rejected_mutations:
            for mixed_with_controls in (False, True):
                row.api_credentials = dict(existing)
                calls.clear()
                payload = dict(config_patch)
                if mixed_with_controls:
                    payload.update(
                        {"tts_enabled": True, "tts_reply_policy": "all_inputs"}
                    )
                response = client.patch(
                    PATH,
                    headers=_jwt_headers("clinic_admin"),
                    json=payload,
                )
                assert response.status_code == 422, response.text
                assert response.json()["detail"]["error"] == ("provider_not_available")
                assert calls == []
                assert row.api_credentials == existing

        for config_patch in maintenance_mutations:
            row.api_credentials = dict(existing)
            calls.clear()
            response = client.patch(
                PATH,
                headers=_jwt_headers("clinic_admin"),
                json=dict(config_patch),
            )
            assert response.status_code == 200, response.text
            assert "replacement-secret-that-must-not-leak" not in response.text
            assert calls == ["flush", "commit"]
            assert row.api_credentials["tts_provider"] == "google"
            for field, value in config_patch.items():
                assert row.api_credentials[field] == value
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)
