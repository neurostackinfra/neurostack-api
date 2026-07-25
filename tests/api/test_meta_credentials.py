"""
NeuroStack API - Meta Cloud API Credentials Tests

Covers save/status flow for Meta Cloud API (WhatsApp Business) credentials:
- 4 credential fields are persisted (phone_number_id, waba_id, permanent_token, app_secret)
- Verify token is auto-generated on first save and preserved on subsequent saves
- Secret fields (permanent_token, app_secret) are masked in status responses
- meta_phone_number_id is validated as unique per tenant (app-level check AND
  handling of a database-level IntegrityError race)
- Saving credentials with an unimplemented WhatsApp provider (360dialog, twilio,
  uazapi) returns a clear 501 error rather than silently accepting them
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

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


@pytest.fixture
def tenant_id() -> UUID:
    """Fresh tenant_id per test so each test's ClinicConfigTable row is isolated."""
    return uuid4()


@pytest.fixture
def headers(tenant_id: UUID) -> dict[str, str]:
    return _headers(tenant_id)


class TestSaveMetaCredentials:
    """PATCH /v1/config/api-credentials — Meta Cloud API fields."""

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

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["ok"] is True
        assert data["whatsapp_provider"] == "meta_cloud"
        assert data["meta_phone_number_id"] == "1234567890"
        assert data["meta_waba_id"] == "9876543210"
        # Set flags indicate the secrets were stored
        assert data["meta_permanent_token_set"] is True
        assert data["meta_app_secret_set"] is True

    def test_auto_generates_verify_token_on_first_save(
        self,
        client: TestClient,
        headers: dict[str, str],
    ) -> None:
        payload = {
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": "111222333",
            "meta_permanent_token": "EAAG_first_save_token_ABCDEFG",
        }
        response = client.patch(API_CRED_PATH, json=payload, headers=headers)

        assert response.status_code == 200
        data = response.json()
        # verify_token is generated automatically and returned in clear text
        # (it is not a secret — the user must copy it into the Meta dashboard).
        verify_token = data["meta_verify_token"]
        assert verify_token
        assert isinstance(verify_token, str)
        assert len(verify_token) >= 16

    def test_preserves_verify_token_across_saves(
        self,
        client: TestClient,
        headers: dict[str, str],
    ) -> None:
        # First save — generates a verify token
        first = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": "444555666",
                "meta_permanent_token": "EAAG_initial_token_ABCDEFG",
            },
            headers=headers,
        )
        assert first.status_code == 200
        initial_verify_token = first.json()["meta_verify_token"]
        assert initial_verify_token

        # Second save — update WABA id only, verify_token must be preserved
        second = client.patch(
            API_CRED_PATH,
            json={"meta_waba_id": "777888999"},
            headers=headers,
        )
        assert second.status_code == 200
        assert second.json()["meta_verify_token"] == initial_verify_token


class TestMaskingMetaCredentials:
    """GET /v1/config/api-credentials — sensitive Meta fields must be masked."""

    def test_get_returns_masked_secrets(
        self,
        client: TestClient,
        headers: dict[str, str],
    ) -> None:
        raw_token = "EAAG_super_secret_permanent_token_1234567890"
        raw_secret = "raw_app_secret_XYZ_ABCDEFG"
        client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": "555666777",
                "meta_permanent_token": raw_token,
                "meta_app_secret": raw_secret,
            },
            headers=headers,
        )

        response = client.get(API_CRED_PATH, headers=headers)
        assert response.status_code == 200
        data = response.json()

        # Set flags true, but the raw secrets must never appear in the response
        assert data["meta_permanent_token_set"] is True
        assert data["meta_app_secret_set"] is True
        assert data["meta_permanent_token_masked"]
        assert data["meta_app_secret_masked"]
        assert raw_token not in str(data)
        assert raw_secret not in str(data)
        # verify_token, being non-secret, is returned in clear text
        assert data["meta_verify_token"]
        assert isinstance(data["meta_verify_token"], str)


class TestPhoneNumberIdUniqueness:
    """meta_phone_number_id must be unique across tenants."""

    def test_second_tenant_rejects_duplicate_phone_number_id(
        self,
        client: TestClient,
    ) -> None:
        shared_phone_id = f"dup_phone_{uuid4().hex[:8]}"

        # Tenant A saves first
        headers_a = _headers(uuid4())
        resp_a = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": shared_phone_id,
                "meta_permanent_token": "EAAG_tenant_a_token_ABCDEF",
            },
            headers=headers_a,
        )
        assert resp_a.status_code == 200, resp_a.text

        # Tenant B tries to reuse the same phone_number_id
        headers_b = _headers(uuid4())
        resp_b = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": shared_phone_id,
                "meta_permanent_token": "EAAG_tenant_b_token_ABCDEF",
            },
            headers=headers_b,
        )
        assert resp_b.status_code == 409, resp_b.text
        body = resp_b.json()
        detail = body.get("detail", body)
        assert detail.get("error") == "meta_phone_number_id_conflict"
        # Friendly message, never a raw driver traceback
        assert "already registered" in detail.get("message", "").lower()

    def test_same_tenant_can_update_own_phone_number_id(
        self,
        client: TestClient,
    ) -> None:
        headers_a = _headers(uuid4())
        phone_id = f"self_update_{uuid4().hex[:8]}"
        first = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": phone_id,
                "meta_permanent_token": "EAAG_self_token_ABCDEF",
            },
            headers=headers_a,
        )
        assert first.status_code == 200

        # Re-saving the same phone_number_id for the same tenant is allowed.
        second = client.patch(
            API_CRED_PATH,
            json={"meta_phone_number_id": phone_id},
            headers=headers_a,
        )
        assert second.status_code == 200

    def test_race_integrity_error_is_translated_to_friendly_409(
        self,
        client: TestClient,
    ) -> None:
        """
        If two saves race past the app-level check, the Postgres partial
        unique index catches the second one. The endpoint must translate the
        IntegrityError into a friendly 409 instead of leaking the raw driver
        error (which the SQLite test backend cannot enforce, so we simulate).
        """
        headers = _headers(uuid4())

        with patch(
            "src.api.routes.config.AsyncSession.commit",
            side_effect=IntegrityError(
                statement="INSERT ...",
                params=None,
                orig=Exception(
                    'duplicate key value violates unique constraint '
                    '"uq_clinic_configs_meta_phone_number_id" DETAIL: '
                    'Key ((api_credentials->>\'meta_phone_number_id\'))=(9999) '
                    'already exists.'
                ),
            ),
        ):
            resp = client.patch(
                API_CRED_PATH,
                json={
                    "whatsapp_provider": "meta_cloud",
                    "meta_phone_number_id": f"race_{uuid4().hex[:8]}",
                    "meta_permanent_token": "EAAG_race_token_ABCDEFG",
                },
                headers=headers,
            )

        assert resp.status_code == 409, resp.text
        detail = resp.json().get("detail", resp.json())
        assert detail.get("error") == "meta_phone_number_id_conflict"
        assert "duplicate key value" not in detail.get("message", "").lower()


class TestUnimplementedProviders:
    """Providers accepted in the UI but without a backend must fail loudly."""

    @pytest.mark.parametrize("provider", ["dialog360", "twilio", "uazapi"])
    def test_returns_501_for_unimplemented_provider(
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
        # Message must name the provider clearly (UI shows this via toast)
        assert provider in detail.get("message", "")

    @pytest.mark.parametrize("provider", ["meta_cloud", "zapi", "evolution"])
    def test_accepts_implemented_provider(
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
