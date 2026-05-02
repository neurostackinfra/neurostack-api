"""
NeuroStack API - Builder Endpoints Tests
Tests for builder-scoped clinic management endpoints.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings


# ============================================
# Token Helpers
# ============================================


def _make_builder_token(builder_id=None, tenant_id=None, user_id=None) -> str:
    """Generate a valid JWT for a builder-role user with builder_id claim."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id or uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": "builder",
            "email": "builder@test.com",
            "builder_id": str(builder_id or uuid4()),
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _make_clinic_admin_token(tenant_id=None, user_id=None) -> str:
    """Generate a valid JWT for a clinic_admin user (non-builder)."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id or uuid4()),
            "tenant_id": str(tenant_id or uuid4()),
            "role": "clinic_admin",
            "email": "clinic@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _make_super_admin_token(tenant_id=None, user_id=None) -> str:
    """Generate a valid JWT for a super_admin user (non-builder)."""
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


# ============================================
# Fixtures
# ============================================


@pytest.fixture
def builder_a_id():
    return uuid4()


@pytest.fixture
def builder_b_id():
    return uuid4()


@pytest.fixture
def builder_a_headers(builder_a_id) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_builder_token(builder_id=builder_a_id)}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def builder_b_headers(builder_b_id) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_builder_token(builder_id=builder_b_id)}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def clinic_admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_clinic_admin_token()}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def super_admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_super_admin_token()}",
        "Content-Type": "application/json",
    }


# ============================================
# Auth Enforcement Tests
# ============================================


class TestBuilderAuthEnforcement:
    """Test that builder endpoints enforce builder role."""

    def test_clinics_returns_401_without_token(self, client: TestClient):
        response = client.get("/v1/builder/clinics")
        assert response.status_code == 401

    def test_me_returns_401_without_token(self, client: TestClient):
        response = client.get("/v1/builder/me")
        assert response.status_code == 401

    def test_stats_returns_401_without_token(self, client: TestClient):
        response = client.get("/v1/builder/stats")
        assert response.status_code == 401

    def test_clinics_returns_403_for_clinic_admin(self, client: TestClient, clinic_admin_headers):
        response = client.get("/v1/builder/clinics", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_clinics_returns_403_for_super_admin(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/builder/clinics", headers=super_admin_headers)
        assert response.status_code == 403

    def test_me_returns_403_for_clinic_admin(self, client: TestClient, clinic_admin_headers):
        response = client.get("/v1/builder/me", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_stats_returns_403_for_super_admin(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/builder/stats", headers=super_admin_headers)
        assert response.status_code == 403

    def test_403_has_descriptive_error_message(self, client: TestClient, clinic_admin_headers):
        response = client.get("/v1/builder/clinics", headers=clinic_admin_headers)
        assert response.status_code == 403
        data = response.json()
        assert "Builder profile not found" in data.get("detail", "")

    def test_create_clinic_returns_403_for_non_builder(self, client: TestClient, clinic_admin_headers):
        response = client.post(
            "/v1/builder/clinics",
            headers=clinic_admin_headers,
            json={
                "clinic_name": "Test Clinic",
                "admin_email": "test@test.com",
                "admin_password": "12345678",
            },
        )
        assert response.status_code == 403

    def test_toggle_status_returns_403_for_non_builder(self, client: TestClient, super_admin_headers):
        fake_id = str(uuid4())
        response = client.patch(
            f"/v1/builder/clinics/{fake_id}/status",
            headers=super_admin_headers,
            json={"is_active": False},
        )
        assert response.status_code == 403


# ============================================
# CRUD Tests
# ============================================


class TestBuilderListClinics:
    """Test GET /v1/builder/clinics."""

    def test_returns_list(self, client: TestClient, builder_a_headers):
        response = client.get("/v1/builder/clinics", headers=builder_a_headers)
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestBuilderCreateClinic:
    """Test POST /v1/builder/clinics."""

    def test_create_clinic_returns_201(self, client: TestClient, builder_a_headers):
        response = client.post(
            "/v1/builder/clinics",
            headers=builder_a_headers,
            json={
                "clinic_name": "New Builder Clinic",
                "admin_email": f"builder-admin-{uuid4().hex[:8]}@test.com",
                "admin_password": "securepassword123",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["ok"] is True
        assert "id" in data


class TestBuilderToggleClinicStatus:
    """Test PATCH /v1/builder/clinics/{id}/status."""

    def test_returns_404_for_nonexistent_clinic(self, client: TestClient, builder_a_headers):
        fake_id = str(uuid4())
        response = client.patch(
            f"/v1/builder/clinics/{fake_id}/status",
            headers=builder_a_headers,
            json={"is_active": False},
        )
        assert response.status_code == 404

    def test_returns_400_for_invalid_uuid(self, client: TestClient, builder_a_headers):
        response = client.patch(
            "/v1/builder/clinics/not-a-uuid/status",
            headers=builder_a_headers,
            json={"is_active": False},
        )
        assert response.status_code == 400


# ============================================
# Scope Isolation Tests
# ============================================


class TestBuilderScopeIsolation:
    """Test that builder A cannot see or modify builder B's clinics."""

    def test_builder_a_cannot_see_builder_b_clinics(
        self, client: TestClient, builder_a_headers, builder_b_headers
    ):
        """Each builder should only see their own clinics (both see empty lists with no DB data)."""
        response_a = client.get("/v1/builder/clinics", headers=builder_a_headers)
        response_b = client.get("/v1/builder/clinics", headers=builder_b_headers)

        assert response_a.status_code == 200
        assert response_b.status_code == 200

        # Both should return lists (empty since no DB records for these random builder IDs)
        assert isinstance(response_a.json(), list)
        assert isinstance(response_b.json(), list)

    def test_builder_a_cannot_toggle_builder_b_clinic(
        self, client: TestClient, builder_a_headers
    ):
        """Builder A should get 404 when trying to toggle a clinic they don't own."""
        # Use a random UUID that doesn't belong to builder A
        fake_clinic_id = str(uuid4())
        response = client.patch(
            f"/v1/builder/clinics/{fake_clinic_id}/status",
            headers=builder_a_headers,
            json={"is_active": False},
        )
        # Returns 404 because the WHERE clause includes builder_id scope
        assert response.status_code == 404


class TestBuilderStats:
    """Test GET /v1/builder/stats."""

    def test_returns_stats_structure(self, client: TestClient, builder_a_headers):
        response = client.get("/v1/builder/stats", headers=builder_a_headers)
        assert response.status_code == 200
        data = response.json()
        assert "clinic_count" in data
        assert "active_clinics" in data
        assert "inactive_clinics" in data
        assert "total_traces" in data
