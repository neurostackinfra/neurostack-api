"""
NeuroStack API — Uazapi WhatsApp Credentials Tests

Covers save/status flow for Uazapi (wuzapi self-hosted) credentials:
- Three credential fields are persisted (uazapi_api_url, uazapi_token,
  uazapi_instance_name)
- The secret field (uazapi_token) is masked in status responses
- ``apis_configured`` becomes true when the three Uazapi fields are set
- Uazapi is now a first-class provider (removed from
  ``UNIMPLEMENTED_WHATSAPP_PROVIDERS``); dialog360 and twilio still 501
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings


API_CRED_PATH = "/v1/config/api-credentials"


def _make_admin_token(tenant_id: UUID | None = None) -> str:
    """Generate a valid JWT for a clinic_admin user bound to a tenant."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": "clinic_admin",
            "email": "admin@uazapi-test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _headers(tenant_id: UUID | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_admin_token(tenant_id)}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def tenant_id() -> UUID:
    """Fresh tenant_id per test so each ClinicConfigTable row is isolated."""
    return uuid4()


@pytest.fixture
def headers(tenant_id: UUID) -> dict[str, str]:
    return _headers(tenant_id)


class TestSaveUazapiCredentials:
    """PATCH /v1/config/api-credentials — Uazapi fields."""

    def test_saves_all_three_uazapi_fields(
        self,
        client: TestClient,
        headers: dict[str, str],
    ) -> None:
        payload = {
            "whatsapp_provider": "uazapi",
            "uazapi_api_url": "https://uazapi.example.com",
            "uazapi_token": "uz_token_abcdefghij1234567890",
            "uazapi_instance_name": "clinica-x",
        }
        response = client.patch(API_CRED_PATH, json=payload, headers=headers)

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["ok"] is True
        assert data["whatsapp_provider"] == "uazapi"
        assert data["uazapi_api_url"] == "https://uazapi.example.com"
        assert data["uazapi_instance_name"] == "clinica-x"
        # Secret is persisted → set flag true; raw value not returned
        assert data["uazapi_token_set"] is True

    def test_patch_without_uazapi_does_not_break_other_providers(
        self,
        client: TestClient,
    ) -> None:
        """A PATCH targeting Z-API must still succeed even after Uazapi became
        a first-class provider — no regression on the Z-API save path."""
        headers = _headers(uuid4())
        resp = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "zapi",
                "zapi_instance_id": "instance-xyz",
                "zapi_token": "zapi_token_1234567890",
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["whatsapp_provider"] == "zapi"
        assert data["zapi_token_set"] is True


class TestMaskingUazapiCredentials:
    """GET /v1/config/api-credentials — uazapi_token must be masked."""

    def test_get_returns_masked_token_and_plain_url(
        self,
        client: TestClient,
        headers: dict[str, str],
    ) -> None:
        raw_token = "uz_super_secret_token_1234567890ABCDEF"
        client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "uazapi",
                "uazapi_api_url": "https://uazapi.example.com",
                "uazapi_token": raw_token,
                "uazapi_instance_name": "clinica-y",
            },
            headers=headers,
        )

        response = client.get(API_CRED_PATH, headers=headers)
        assert response.status_code == 200
        data = response.json()

        # Set flag true, masked value present, raw value never leaked
        assert data["uazapi_token_set"] is True
        assert data["uazapi_token_masked"]
        assert raw_token not in str(data)
        # URL + instance name are non-secret metadata → returned in the clear
        assert data["uazapi_api_url"] == "https://uazapi.example.com"
        assert data["uazapi_instance_name"] == "clinica-y"


class TestUazapiIsConfigured:
    """``apis_configured`` reflects the full Uazapi credential triple."""

    def test_apis_configured_true_when_all_three_fields_set(
        self,
        client: TestClient,
        headers: dict[str, str],
    ) -> None:
        client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "uazapi",
                "uazapi_api_url": "https://uazapi.example.com",
                "uazapi_token": "uz_token_1234567890",
                "uazapi_instance_name": "clinica-z",
            },
            headers=headers,
        )
        resp = client.get(API_CRED_PATH, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["apis_configured"] is True

    def test_apis_configured_false_when_instance_name_missing(
        self,
        client: TestClient,
        headers: dict[str, str],
    ) -> None:
        """Missing any of the three fields must keep ``apis_configured`` false
        (unless the LLM credentials are set, which they aren't in this test)."""
        client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "uazapi",
                "uazapi_api_url": "https://uazapi.example.com",
                "uazapi_token": "uz_token_missing_instance",
            },
            headers=headers,
        )
        resp = client.get(API_CRED_PATH, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["apis_configured"] is False


class TestUnimplementedRemains:
    """Uazapi is now supported; dialog360 and twilio must still 501."""

    @pytest.mark.parametrize("provider", ["dialog360", "twilio"])
    def test_still_returns_501_for_dialog360_and_twilio(
        self,
        client: TestClient,
        provider: str,
    ) -> None:
        headers = _headers(uuid4())
        resp = client.patch(
            API_CRED_PATH,
            json={"whatsapp_provider": provider},
            headers=headers,
        )
        assert resp.status_code == 501, resp.text
        detail = resp.json().get("detail", resp.json())
        assert detail.get("error") == "provider_not_implemented"
        assert detail.get("provider") == provider

    @pytest.mark.parametrize(
        "provider", ["meta_cloud", "zapi", "evolution", "uazapi"],
    )
    def test_accepts_implemented_providers_including_uazapi(
        self,
        client: TestClient,
        provider: str,
    ) -> None:
        headers = _headers(uuid4())
        resp = client.patch(
            API_CRED_PATH,
            json={"whatsapp_provider": provider},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["whatsapp_provider"] == provider
