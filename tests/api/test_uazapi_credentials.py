"""
NeuroStack API — Uazapi WhatsApp Credentials Tests

Covers save/status flow for Uazapi (wuzapi self-hosted) credentials:
- Three credential fields are persisted (uazapi_api_url, uazapi_token,
  uazapi_instance_name)
- The secret field (uazapi_token) is masked in status responses
- ``apis_configured`` becomes true when the three Uazapi fields are set
- Active Uazapi credentials can be saved through the public boundary
- Directly seeded legacy credentials retain masking and readiness behavior;
  exact echoes remain accepted without changing stored material
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
    """Uazapi writes are public while token material remains masked."""

    def test_accepts_effective_uazapi_bundle_atomically(
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
        assert "uz_token_abcdefghij1234567890" not in response.text

        stored = client.get(API_CRED_PATH, headers=headers)
        assert stored.status_code == 200
        data = stored.json()
        assert data["whatsapp_provider"] == "uazapi"
        assert data["uazapi_api_url"] == "https://uazapi.example.com"
        assert data["uazapi_instance_name"] == "clinica-x"
        assert data["uazapi_token_set"] is True
        assert data["whatsapp_readiness"] == "active"

    def test_accepts_active_sibling_provider_bundle_atomically(
        self,
        client: TestClient,
    ) -> None:
        """Adopting an active sibling does not populate Uazapi fields."""
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
        assert "zapi_token_1234567890" not in resp.text
        data = client.get(API_CRED_PATH, headers=headers).json()
        assert data["whatsapp_provider"] == "zapi"
        assert data["zapi_instance_id"] == "instance-xyz"
        assert data["uazapi_api_url"] is None
        assert data["uazapi_token_set"] is False

    def test_exact_legacy_echo_preserves_uazapi_credentials(
        self,
        client: TestClient,
        headers: dict[str, str],
        tenant_id: UUID,
    ) -> None:
        raw_token = "uz_legacy_token_1234567890"
        _seed_legacy_credentials(
            client,
            tenant_id,
            {
                "whatsapp_provider": "uazapi",
                "uazapi_api_url": "https://uazapi.example.com",
                "uazapi_token": raw_token,
                "uazapi_instance_name": "clinica-legacy",
            },
        )

        echo = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "uazapi",
                "uazapi_api_url": "https://uazapi.example.com/",
                "uazapi_token": "••••••••",
                "uazapi_instance_name": "clinica-legacy",
            },
            headers=headers,
        )
        assert echo.status_code == 200, echo.text
        assert echo.json()["whatsapp_provider"] == "uazapi"
        assert echo.json()["uazapi_api_url"] == "https://uazapi.example.com"
        assert echo.json()["uazapi_instance_name"] == "clinica-legacy"
        assert echo.json()["uazapi_token_set"] is True
        assert raw_token not in echo.text


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


class TestProviderCatalog:
    """Active and unavailable WhatsApp IDs follow the public matrix."""

    @pytest.mark.parametrize(
        "provider",
        ["meta_cloud", "zapi"],
    )
    def test_active_whatsapp_provider_selection_is_accepted(
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
        assert (
            client.get(API_CRED_PATH, headers=headers).json()["whatsapp_provider"]
            == provider
        )

    @pytest.mark.parametrize("provider", ["uazapi", "evolution"])
    def test_configurable_endpoint_provider_selection_requires_a_fresh_bundle(
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
        assert resp.json()["detail"] == {
            "error": "provider_switch_bundle_required",
            "capability": "whatsapp",
        }
        assert (
            client.get(API_CRED_PATH, headers=headers).json()["whatsapp_provider"]
            is None
        )

    @pytest.mark.parametrize("provider", ["dialog360", "twilio"])
    def test_unavailable_whatsapp_provider_selection_fails_closed(
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
        assert resp.json()["detail"] == {
            "error": "provider_not_available",
            "capability": "whatsapp",
            "provider": provider,
        }
        assert (
            client.get(API_CRED_PATH, headers=headers).json()["whatsapp_provider"]
            is None
        )
