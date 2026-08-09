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
def test_allowed_jwt_roles_can_read_and_patch_own_credentials(
    client: TestClient, role: str
) -> None:
    headers = _jwt_headers(role, builder_id=uuid4() if role == "builder" else None)
    response = client.patch(
        PATH,
        headers=headers,
        json={
            "llm_provider": "openai",
            "llm_api_url": "HTTPS://API.OPENAI.COM:443/v1/",
            "llm_api_key": "provider-key-value",
            "llm_model_name": "gpt-model",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["llm_api_url"] == "https://api.openai.com/v1"
    assert data["llm_api_key_set"] is True
    assert data["llm_api_key_masked"] == "••••••••"
    assert "provider-key-value" not in response.text
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
    ("payload", "detail"),
    [
        (
            {"llm_provider": "made_up"},
            {
                "error": "invalid_credentials_payload",
                "fields": [{"field": "llm_provider", "code": "literal_error"}],
            },
        ),
        (
            {"llm_provider": "slm_cortex"},
            {
                "error": "provider_not_operational",
                "capability": "llm",
                "provider": "slm_cortex",
            },
        ),
    ],
)
def test_provider_errors_are_exact_and_atomic(
    client: TestClient, payload: dict[str, str], detail: dict[str, str]
) -> None:
    headers = _jwt_headers("clinic_admin")
    response = client.patch(PATH, headers=headers, json=payload)
    assert response.status_code == 422
    assert response.json()["detail"] == detail
    assert client.get(PATH, headers=headers).json()["llm_provider"] is None


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


def test_zapi_digest_is_write_only_and_readiness_only(client: TestClient) -> None:
    headers = _jwt_headers("clinic_admin")
    digest = "b" * 64
    response = client.patch(
        PATH,
        headers=headers,
        json={
            "whatsapp_provider": "zapi",
            "zapi_instance_id": "instance-42",
            "zapi_token": "outbound-zapi-token",
            "zapi_inbound_token_digest": digest,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["zapi_inbound_token_set"] is True
    assert data["whatsapp_readiness"] == "active"
    assert digest not in response.text
    assert "zapi_inbound_token_digest" not in data

    read = client.get(PATH, headers=headers)
    assert read.status_code == 200
    assert digest not in read.text
    assert "zapi_inbound_token_digest" not in read.json()


@pytest.mark.parametrize(
    ("provider", "api_url", "model_name"),
    [
        ("elevenlabs", "https://api.elevenlabs.io/v1", "eleven_multilingual_v2"),
        ("google", "https://texttospeech.googleapis.com/v1", "pt-BR-Neural2-A"),
    ],
)
def test_tts_credentials_are_saved_masked_and_configuration_only(
    client: TestClient,
    provider: str,
    api_url: str,
    model_name: str,
) -> None:
    headers = _jwt_headers("clinic_admin")
    secret = f"{provider}-tts-secret"
    response = client.patch(
        PATH,
        headers=headers,
        json={
            "tts_provider": provider,
            "tts_api_url": api_url,
            "tts_api_key": secret,
            "tts_model_name": model_name,
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["tts_provider"] == provider
    assert data["tts_api_url"] == api_url
    assert data["tts_model_name"] == model_name
    assert data["tts_api_key_set"] is True
    assert data["tts_api_key_masked"] == "••••••••"
    assert data["tts_readiness"] == "configuration_only"
    assert secret not in response.text

    read = client.get(PATH, headers=headers)
    assert read.status_code == 200
    assert read.json()["tts_provider"] == provider
    assert secret not in read.text


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
        {"tts_provider": "microsoft"},
        {"zapi_inbound_token_digest": "A" * 64},
        {"zapi_inbound_token_digest": "a" * 63},
    )
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            ApiCredentialsPatch.model_validate(payload)


def test_meta_verify_token_is_write_only_across_patch_and_get(
    client: TestClient,
) -> None:
    headers = _jwt_headers("clinic_admin")
    sentinel = "meta-verify-token-sentinel"
    response = client.patch(
        PATH,
        headers=headers,
        json={
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": "phone-42",
            "meta_permanent_token": "permanent-secret",
            "meta_app_secret": "signature-secret",
            "meta_verify_token": sentinel,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["meta_verify_token_set"] is True
    assert response.json()["meta_verify_token_masked"] == "••••••••"
    assert sentinel not in response.text

    read = client.get(PATH, headers=headers)
    assert read.status_code == 200
    assert read.json()["meta_verify_token_set"] is True
    assert sentinel not in read.text


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
        "error": "fresh_api_key_required",
        "capability": "llm",
        "reason": "provider_changed",
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
