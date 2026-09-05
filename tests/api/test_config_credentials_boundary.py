"""Generic config must not become a second credential reader or writer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings
from src.services.api_credentials import PROVIDER_AVAILABILITY


def _headers() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "clinic_admin",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_generic_get_omits_credential_bucket_fields_and_secrets(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(PROVIDER_AVAILABILITY["llm"], "openai", "active")
    headers = _headers()
    secret = "generic-boundary-secret"
    saved = client.patch(
        "/v1/config/api-credentials",
        headers=headers,
        json={
            "llm_provider": "openai",
            "llm_api_url": "https://api.openai.com/v1",
            "llm_api_key": secret,
            "llm_model_name": "model",
        },
    )
    assert saved.status_code == 200, saved.text

    response = client.get("/v1/config", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "api_credentials" not in data
    assert "llm_provider" not in data
    assert "llm_api_key" not in data
    assert secret not in response.text


@pytest.mark.parametrize(
    "field",
    [
        "api_credentials",
        "whatsapp_provider",
        "zapi_token",
        "evolution_api_key",
        "meta_app_secret",
        "uazapi_token",
        "llm_api_key",
        "llm_base_url",
        "multimodal_api_key",
        # The fallback opt-in gate is a credential-bucket field even though
        # it is non-secret: the generic /v1/config endpoint must refuse it
        # too, so operators are forced through the capability endpoint
        # (`/v1/config/api-credentials`) where the strict Pydantic validator
        # and the merge policy apply.
        "multimodal_text_fallback_enabled",
        "stt_api_key",
        "whisper_api_key",
        "whisper_model",
        "tts_api_key",
        "tts_reply_policy",
        "tts_future_alias",
    ],
)
def test_generic_patch_rejects_every_credential_prefix_and_alias(
    client: TestClient, field: str
) -> None:
    headers = _headers()
    response = client.patch("/v1/config", headers=headers, json={field: "sentinel"})
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "credential_endpoint_required",
        "fields": [field],
    }


def test_generic_rejection_is_sorted_atomic_and_precedes_bucket_load(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_bucket_load(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("generic credential bypass loaded the config row")

    monkeypatch.setattr(
        "src.api.routes.config._get_or_create_config", unexpected_bucket_load
    )
    response = client.patch(
        "/v1/config",
        headers=_headers(),
        json={"tts_api_key": "one", "llm_api_key": "two"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "credential_endpoint_required",
        "fields": ["llm_api_key", "tts_api_key"],
    }


def test_generic_patch_allows_handoff_service_time_target(
    client: TestClient,
) -> None:
    response = client.patch(
        "/v1/config",
        headers=_headers(),
        json={"meta_minutos_atendimento": 15},
    )
    assert response.status_code == 200, response.text
    assert response.json()["meta_minutos_atendimento"] == 15


def test_sequential_http_saves_preserve_previous_capabilities(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(PROVIDER_AVAILABILITY["llm"], "dropout", "active")
    monkeypatch.setitem(PROVIDER_AVAILABILITY["stt"], "outros", "active")
    headers = _headers()
    first = client.patch(
        "/v1/config/api-credentials",
        headers=headers,
        json={
            "llm_provider": "dropout",
            "llm_api_url": "https://api.neuralake.cloud/v1",
            "llm_api_key": "first-secret",
            "llm_model_name": "text",
        },
    )
    assert first.status_code == 200, first.text
    second = client.patch(
        "/v1/config/api-credentials",
        headers=headers,
        json={
            "stt_provider": "outros",
            "stt_api_url": "https://speech.example/v1",
            "stt_api_key": "second-secret",
            "stt_model_name": "whisper-large",
        },
    )
    assert second.status_code == 200, second.text
    data = second.json()
    assert data["llm_provider"] == "dropout"
    assert data["llm_api_key_set"] is True
    assert data["stt_provider"] == "outros"
    assert data["stt_api_key_set"] is True
    assert "first-secret" not in second.text
    assert "second-secret" not in second.text
