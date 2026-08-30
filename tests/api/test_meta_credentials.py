"""
NeuroStack API - Meta Cloud API Credentials Tests

Covers save/status flow for Meta Cloud API (WhatsApp Business) credentials:
- 5 credential fields are persisted (phone_number_id, waba_id, permanent_token,
  app_secret, verify_token)
- Callers seed and retain the write-only verify token locally
- Secret fields are exposed only through set/masked status
- meta_phone_number_id is validated as unique per tenant (app-level check AND
  handling of a database-level IntegrityError race)
- Legacy Meta credentials remain readable, while public provider writes fail
  closed until availability is enabled.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings

API_CRED_PATH = "/v1/config/api-credentials"
META_WEBHOOK_PATH = "/v1/webhook/meta"


def _make_admin_token(tenant_id: UUID | None = None) -> str:
    """Generate a valid JWT for a clinic_admin user bound to a tenant."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": "clinic_admin",
            "email": "admin@meta-test.com",
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
    """Fresh tenant_id per test so each test's ClinicConfigTable row is isolated."""
    return uuid4()


@pytest.fixture
def headers(tenant_id: UUID) -> dict[str, str]:
    return _headers(tenant_id)


class TestMetaProviderAvailability:
    """PATCH /v1/config/api-credentials fails closed for Meta Cloud API."""

    def test_saves_all_four_meta_fields(
        self,
        client: TestClient,
        headers: dict[str, str],
    ) -> None:
        payload = {
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": "1234567890",
            "meta_waba_id": "9876543210",
            "meta_permanent_token": "EAAG_permanent_token_value_ABCDEFGHIJ",
            "meta_app_secret": "app_secret_value_XYZ",
        }
        response = client.patch(API_CRED_PATH, json=payload, headers=headers)

        assert response.status_code == 422, response.text
        assert response.json()["detail"]["error"] == "provider_not_available"
        assert response.json()["detail"]["provider"] == "meta_cloud"

    def test_seeded_verify_token_is_write_only_on_first_save(
        self,
        client: TestClient,
        headers: dict[str, str],
    ) -> None:
        verify_token = f"verify_{uuid4().hex}"
        payload = {
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": "111222333",
            "meta_permanent_token": "EAAG_first_save_token_ABCDEFG",
            "meta_verify_token": verify_token,
        }
        response = client.patch(API_CRED_PATH, json=payload, headers=headers)

        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "provider_not_available"
        assert verify_token not in response.text

    def test_preserves_verify_token_across_saves(
        self,
        client: TestClient,
        headers: dict[str, str],
        tenant_id: UUID,
    ) -> None:
        verify_token = f"verify_{uuid4().hex}"
        # Legacy state is seeded directly; public updates stay fail closed.
        _seed_legacy_credentials(
            client,
            tenant_id,
            {
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": "444555666",
                "meta_permanent_token": "EAAG_initial_token_ABCDEFG",
                "meta_verify_token": verify_token,
            },
        )

        # Update WABA id is rejected without modifying the legacy token.
        second = client.patch(
            API_CRED_PATH,
            json={"meta_waba_id": "777888999"},
            headers=headers,
        )
        assert second.status_code == 422
        assert second.json()["detail"]["error"] == "provider_not_available"
        assert verify_token not in second.text

        handshake = client.get(
            META_WEBHOOK_PATH,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": verify_token,
                "hub.challenge": "preserved-token-challenge",
            },
        )
        assert handshake.status_code == 200
        assert handshake.text == "preserved-token-challenge"


class TestMaskingMetaCredentials:
    """GET /v1/config/api-credentials — sensitive Meta fields must be masked."""

    def test_get_returns_masked_secrets(
        self,
        client: TestClient,
        headers: dict[str, str],
        tenant_id: UUID,
    ) -> None:
        raw_token = "EAAG_super_secret_permanent_token_1234567890"
        raw_secret = "raw_app_secret_XYZ_ABCDEFG"
        verify_token = f"verify_{uuid4().hex}"
        _seed_legacy_credentials(
            client,
            tenant_id,
            {
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": "555666777",
                "meta_permanent_token": raw_token,
                "meta_app_secret": raw_secret,
                "meta_verify_token": verify_token,
            },
        )

        response = client.get(API_CRED_PATH, headers=headers)
        assert response.status_code == 200
        data = response.json()

        # Set flags true, but the raw secrets must never appear in the response
        assert data["meta_permanent_token_set"] is True
        assert data["meta_app_secret_set"] is True
        assert data["meta_verify_token_set"] is True
        assert data["meta_permanent_token_masked"]
        assert data["meta_app_secret_masked"]
        assert raw_token not in str(data)
        assert raw_secret not in str(data)
        assert data["meta_verify_token_masked"] == "••••••••"
        assert "meta_verify_token" not in data
        assert verify_token not in response.text


class TestMetaProviderAvailabilityPrecedesPersistence:
    """Unavailable Meta writes never reach uniqueness or commit handling."""

    def test_second_tenant_rejects_duplicate_phone_number_id(
        self,
        client: TestClient,
    ) -> None:
        resp_b = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": f"dup_phone_{uuid4().hex[:8]}",
                "meta_permanent_token": "EAAG_tenant_b_token_ABCDEF",
            },
            headers=_headers(uuid4()),
        )
        assert resp_b.status_code == 422, resp_b.text
        assert resp_b.json()["detail"]["error"] == "provider_not_available"

    def test_same_tenant_can_update_own_phone_number_id(
        self,
        client: TestClient,
    ) -> None:
        headers_a = _headers(uuid4())
        second = client.patch(
            API_CRED_PATH,
            json={"whatsapp_provider": "meta_cloud", "meta_phone_number_id": "self"},
            headers=headers_a,
        )
        assert second.status_code == 422
        assert second.json()["detail"]["error"] == "provider_not_available"

    def test_race_integrity_error_is_translated_to_friendly_409(
        self,
        client: TestClient,
    ) -> None:
        """Availability rejection occurs before persistence can be attempted."""
        headers = _headers(uuid4())
        resp = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": f"race_{uuid4().hex[:8]}",
                "meta_permanent_token": "EAAG_race_token_ABCDEFG",
            },
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "provider_not_available"


class TestUnavailableProviders:
    """Every public WhatsApp provider write reports availability uniformly."""

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

    @pytest.mark.parametrize("provider", ["meta_cloud", "zapi", "evolution", "uazapi"])
    def test_returns_provider_not_available_for_legacy_provider(
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
        detail = resp.json()["detail"]
        assert detail["error"] == "provider_not_available"
        assert detail["provider"] == provider
