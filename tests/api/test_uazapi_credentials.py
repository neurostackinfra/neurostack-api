"""
NeuroStack API — Uazapi WhatsApp Credentials Tests

Covers save/status flow for Uazapi (wuzapi self-hosted) credentials:
- Three credential fields are persisted (uazapi_api_url, uazapi_token,
  uazapi_instance_name)
- The secret field (uazapi_token) is masked in status responses
- ``apis_configured`` becomes true when the three Uazapi fields are set
- Uazapi is an available provider, so public writes adopt it; providers
  without a runtime path stay fail closed.
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


def _seed_legacy_credentials(
    client: TestClient, tenant_id: UUID, credentials: dict[str, str]
) -> None:
    """Persist legacy runtime credentials without using the public PATCH API."""
    from src.models.clinic_config_table import ClinicConfigTable
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            db.add(ClinicConfigTable(tenant_id=tenant_id, api_credentials=credentials))
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


@pytest.fixture
def tenant_id() -> UUID:
    """Fresh tenant_id per test so each ClinicConfigTable row is isolated."""
    return uuid4()


@pytest.fixture
def headers(tenant_id: UUID) -> dict[str, str]:
    return _headers(tenant_id)


class TestUazapiProviderAvailability:
    """PATCH /v1/config/api-credentials adopts Uazapi."""

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
        assert data["whatsapp_provider"] == "uazapi"
        assert data["uazapi_api_url"] == "https://uazapi.example.com"
        assert data["uazapi_instance_name"] == "clinica-x"
        assert data["uazapi_token_set"] is True
        assert "uz_token_abcdefghij1234567890" not in response.text

    def test_patch_without_uazapi_does_not_break_other_providers(
        self,
        client: TestClient,
    ) -> None:
        """Adopting a sibling WhatsApp provider leaves the Uazapi fields empty."""
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
        assert data["zapi_instance_id"] == "instance-xyz"
        assert data["uazapi_api_url"] is None
        assert data["uazapi_token_set"] is False


class TestMaskingUazapiCredentials:
    """GET /v1/config/api-credentials — uazapi_token must be masked."""

    def test_get_returns_masked_token_and_plain_url(
        self,
        client: TestClient,
        headers: dict[str, str],
        tenant_id: UUID,
    ) -> None:
        raw_token = "uz_super_secret_token_1234567890ABCDEF"
        _seed_legacy_credentials(
            client,
            tenant_id,
            {
                "whatsapp_provider": "uazapi",
                "uazapi_api_url": "https://uazapi.example.com",
                "uazapi_token": raw_token,
                "uazapi_instance_name": "clinica-y",
            },
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
        tenant_id: UUID,
    ) -> None:
        _seed_legacy_credentials(
            client,
            tenant_id,
            {
                "whatsapp_provider": "uazapi",
                "uazapi_api_url": "https://uazapi.example.com",
                "uazapi_token": "uz_token_1234567890",
                "uazapi_instance_name": "clinica-z",
            },
        )
        resp = client.get(API_CRED_PATH, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["apis_configured"] is True

    def test_apis_configured_false_when_instance_name_missing(
        self,
        client: TestClient,
        headers: dict[str, str],
        tenant_id: UUID,
    ) -> None:
        """Missing any of the three fields must keep ``apis_configured`` false
        (unless the LLM credentials are set, which they aren't in this test)."""
        _seed_legacy_credentials(
            client,
            tenant_id,
            {
                "whatsapp_provider": "uazapi",
                "uazapi_api_url": "https://uazapi.example.com",
                "uazapi_token": "uz_token_missing_instance",
            },
        )
        resp = client.get(API_CRED_PATH, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["apis_configured"] is False


class TestUnavailableProviders:
    """Public writes report the same availability state for every provider."""

    @pytest.mark.parametrize("provider", ["dialog360", "twilio"])
    def test_returns_provider_not_available_for_dialog360_and_twilio(
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
        assert resp.status_code == 422, resp.text
        detail = resp.json().get("detail", resp.json())
        assert detail.get("error") == "provider_not_available"
        assert detail.get("provider") == provider

    @pytest.mark.parametrize(
        "provider",
        ["meta_cloud", "zapi", "evolution", "uazapi"],
    )
    def test_adopts_the_providers_the_runtime_can_serve(
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
