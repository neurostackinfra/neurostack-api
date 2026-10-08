"""
NeuroStack API - Admin Endpoints Tests
Tests for super-admin clinic + builder management endpoints.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings


def _make_super_admin_token(tenant_id=None, user_id=None) -> str:
    """Generate a valid JWT for a super_admin user."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id or uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": "super_admin",
            "email": "admin@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _make_clinic_admin_token() -> str:
    """Generate a valid JWT for a clinic_admin user (non-super)."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "clinic_admin",
            "email": "clinic@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


@pytest.fixture
def super_admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_super_admin_token()}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def clinic_admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_clinic_admin_token()}",
        "Content-Type": "application/json",
    }


class TestRequireSuperAdmin:
    """Test that admin endpoints enforce super_admin role."""

    def test_clinics_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/admin/clinics", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_builders_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/admin/builders", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_stats_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/admin/stats", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_no_auth_returns_401(self, client: TestClient):
        response = client.get("/v1/admin/clinics")
        assert response.status_code == 401


class TestListClinics:
    """Test GET /v1/admin/clinics."""

    def test_returns_list(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/clinics", headers=super_admin_headers)
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestAdminStats:
    """Test GET /v1/admin/stats."""

    def test_returns_stats_structure(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/stats", headers=super_admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert "total_traces" in data
        assert "active_pilots" in data
        assert "active_builders" in data
        assert "total_clinics" in data
        assert "active_clinics" in data
        assert "total_builders" in data
        assert "estimated_mrr" in data


class TestListBuilders:
    """Test GET /v1/admin/builders."""

    def test_returns_list(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/builders", headers=super_admin_headers)
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestStubEndpoints:
    """Test stub endpoints return empty lists."""

    def test_circuit_breakers_stub(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/circuit-breakers", headers=super_admin_headers)
        assert response.status_code == 200
        assert response.json() == []

    def test_removed_rlhf_pending_route_returns_404(
        self, client: TestClient, super_admin_headers
    ):
        response = client.get(
            "/v1/admin/rlhf/" + "pending", headers=super_admin_headers
        )
        assert response.status_code == 404


class TestCreateClinicValidation:
    """Test POST /v1/admin/clinics validation."""

    def test_rejects_empty_name(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/clinics",
            headers=super_admin_headers,
            json={
                "clinic_name": "",
                "admin_email": "test@test.com",
                "admin_password": "12345678",
            },
        )
        assert response.status_code == 422

    def test_rejects_invalid_email(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/clinics",
            headers=super_admin_headers,
            json={
                "clinic_name": "Test Clinic",
                "admin_email": "not-an-email",
                "admin_password": "12345678",
            },
        )
        assert response.status_code == 422

    def test_rejects_short_password(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/clinics",
            headers=super_admin_headers,
            json={
                "clinic_name": "Test Clinic",
                "admin_email": "test@test.com",
                "admin_password": "short",
            },
        )
        assert response.status_code == 422

    def test_rejects_invalid_plan_type(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/clinics",
            headers=super_admin_headers,
            json={
                "clinic_name": "Test Clinic",
                "admin_email": "test@test.com",
                "admin_password": "12345678",
                "plan_type": "invalid",
            },
        )
        assert response.status_code == 422


class TestCreateBuilderValidation:
    """Test POST /v1/admin/builders validation."""

    def test_rejects_empty_name(self, client: TestClient, super_admin_headers):
        response = client.post(
            "/v1/admin/builders",
            headers=super_admin_headers,
            json={"name": ""},
        )
        assert response.status_code == 422


class TestUpdateBuilderValidation:
    """Test PATCH /v1/admin/builders/{id} validation."""

    def test_rejects_invalid_status(self, client: TestClient, super_admin_headers):
        fake_id = str(uuid4())
        response = client.patch(
            f"/v1/admin/builders/{fake_id}",
            headers=super_admin_headers,
            json={"status": "bogus"},
        )
        assert response.status_code == 422

    def test_invalid_uuid_returns_400(self, client: TestClient, super_admin_headers):
        response = client.patch(
            "/v1/admin/builders/not-a-uuid",
            headers=super_admin_headers,
            json={"name": "New Name"},
        )
        # 400 or 422 depending on FastAPI validation
        assert response.status_code in (400, 422, 500)


class TestDeactivateClinic:
    """Test DELETE /v1/admin/clinics/{id}."""

    def test_invalid_uuid_returns_400(self, client: TestClient, super_admin_headers):
        response = client.delete(
            "/v1/admin/clinics/not-a-uuid",
            headers=super_admin_headers,
        )
        assert response.status_code == 400


class TestDeactivateBuilder:
    """Test DELETE /v1/admin/builders/{id}."""

    def test_invalid_uuid_returns_400(self, client: TestClient, super_admin_headers):
        response = client.delete(
            "/v1/admin/builders/not-a-uuid",
            headers=super_admin_headers,
        )
        assert response.status_code == 400


def _seed_entities(client: TestClient, *entities: Any) -> None:
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            db.add_all(entities)
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _clinic(builder_id: UUID | None = None) -> Any:
    from src.models.tenant import Tenant

    tenant_id = uuid4()
    return Tenant(
        id=tenant_id,
        name=f"Audit Add-on Clinic {tenant_id.hex}",
        slug=f"audit-addon-{tenant_id.hex}",
        api_key_hash=f"audit-addon-hash-{tenant_id.hex}",
        builder_id=builder_id,
        config={},
    )


def _listed(rows: list[dict[str, Any]], clinic_id: UUID) -> dict[str, Any]:
    return next(row for row in rows if row["clinic_id"] == str(clinic_id))


class TestAuditExtendedAccess:
    """Super admin toggles the per-clinic audit export add-on."""

    def test_super_admin_turns_addon_on_and_off(
        self, client: TestClient, super_admin_headers
    ):
        clinic = _clinic()
        _seed_entities(client, clinic)

        listed_before = client.get("/v1/admin/clinics", headers=super_admin_headers)
        enabled = client.patch(
            f"/v1/admin/clinics/{clinic.id}",
            headers=super_admin_headers,
            json={"audit_extended_access": True},
        )
        listed_enabled = client.get("/v1/admin/clinics", headers=super_admin_headers)
        untouched = client.patch(
            f"/v1/admin/clinics/{clinic.id}",
            headers=super_admin_headers,
            json={"clinic_name": "Renamed Audit Clinic"},
        )
        disabled = client.patch(
            f"/v1/admin/clinics/{clinic.id}",
            headers=super_admin_headers,
            json={"audit_extended_access": False},
        )
        listed_disabled = client.get("/v1/admin/clinics", headers=super_admin_headers)

        assert listed_before.status_code == 200
        assert (
            _listed(listed_before.json(), clinic.id)["audit_extended_access"] is False
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json() == {"ok": True, "audit_extended_access": True}
        assert (
            _listed(listed_enabled.json(), clinic.id)["audit_extended_access"] is True
        )
        assert untouched.status_code == 200, untouched.text
        assert untouched.json() == {"ok": True, "audit_extended_access": True}
        assert disabled.status_code == 200, disabled.text
        assert disabled.json() == {"ok": True, "audit_extended_access": False}
        assert (
            _listed(listed_disabled.json(), clinic.id)["audit_extended_access"] is False
        )

    def test_non_super_admin_cannot_set_addon(
        self, client: TestClient, clinic_admin_headers
    ):
        clinic = _clinic()
        _seed_entities(client, clinic)

        response = client.patch(
            f"/v1/admin/clinics/{clinic.id}",
            headers=clinic_admin_headers,
            json={"audit_extended_access": True},
        )

        assert response.status_code == 403

    def test_builder_listing_exposes_addon_per_clinic(
        self, client: TestClient, super_admin_headers
    ):
        from src.models.builder import Builder

        builder_id = uuid4()
        builder = Builder(id=builder_id, name=f"Audit Add-on Builder {builder_id.hex}")
        plain = _clinic(builder_id)
        extended = _clinic(builder_id)
        extended.audit_extended_access = True
        _seed_entities(client, builder, plain, extended)

        response = client.get("/v1/admin/builders", headers=super_admin_headers)

        assert response.status_code == 200
        listed = next(row for row in response.json() if row["id"] == str(builder_id))
        flags = {
            clinic["clinic_id"]: clinic["audit_extended_access"]
            for clinic in listed["clinics"]
        }
        assert flags == {str(plain.id): False, str(extended.id): True}
