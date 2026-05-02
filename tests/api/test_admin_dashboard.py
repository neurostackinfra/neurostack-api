"""
NeuroStack API - Admin Dashboard Tests
Tests for admin control tower: dashboard endpoint, enriched clinic/builder
responses, and shared health helpers.
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


def _make_builder_token(builder_id=None) -> str:
    """Generate a valid JWT for a builder-role user."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
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


# ============================================
# Fixtures
# ============================================


@pytest.fixture
def super_admin_headers() -> dict:
    return {
        "Authorization": f"Bearer {_make_super_admin_token()}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def clinic_admin_headers() -> dict:
    return {
        "Authorization": f"Bearer {_make_clinic_admin_token()}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def builder_headers() -> dict:
    return {
        "Authorization": f"Bearer {_make_builder_token()}",
        "Content-Type": "application/json",
    }


# ============================================
# Dashboard Structure Tests
# ============================================


class TestAdminDashboard:
    """Test GET /v1/admin/dashboard."""

    def test_returns_dashboard_structure(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/dashboard", headers=super_admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert "stats" in data
        assert "time_series" in data
        assert "alerts" in data

    def test_stats_has_required_fields(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/dashboard", headers=super_admin_headers)
        assert response.status_code == 200
        stats = response.json()["stats"]
        assert "total_traces" in stats
        assert "active_pilots" in stats
        assert "active_builders" in stats
        assert "active_managed_clinics" in stats
        assert "estimated_mrr" in stats
        assert "safety_flags_7d" in stats
        assert "handoff_count_7d" in stats

    def test_time_series_has_31_points(self, client: TestClient, super_admin_headers):
        """Time series should have 31 points (day 0 through day 30)."""
        response = client.get("/v1/admin/dashboard", headers=super_admin_headers)
        assert response.status_code == 200
        ts = response.json()["time_series"]
        assert isinstance(ts, list)
        assert len(ts) == 31
        for point in ts:
            assert "date" in point
            assert "count" in point

    def test_alerts_is_list(self, client: TestClient, super_admin_headers):
        response = client.get("/v1/admin/dashboard", headers=super_admin_headers)
        assert response.status_code == 200
        assert isinstance(response.json()["alerts"], list)


# ============================================
# Auth Enforcement Tests
# ============================================


class TestAdminDashboardAuth:
    """Test auth enforcement for admin dashboard."""

    def test_returns_401_without_token(self, client: TestClient):
        response = client.get("/v1/admin/dashboard")
        assert response.status_code == 401

    def test_returns_403_for_clinic_admin(self, client: TestClient, clinic_admin_headers):
        response = client.get("/v1/admin/dashboard", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_returns_403_for_builder(self, client: TestClient, builder_headers):
        response = client.get("/v1/admin/dashboard", headers=builder_headers)
        assert response.status_code == 403


# ============================================
# Enriched Clinic Fields Tests
# ============================================


class TestAdminEnrichedClinics:
    """Test that admin clinic endpoints include health fields."""

    def test_clinics_response_has_health_fields(self, client: TestClient, super_admin_headers):
        """GET /v1/admin/clinics response objects should include health fields."""
        response = client.get("/v1/admin/clinics", headers=super_admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # If there are clinics, verify health fields
        if len(data) > 0:
            clinic = data[0]
            assert "health_status" in clinic
            assert "last_trace_at" in clinic
            assert "config_completeness" in clinic

    def test_builders_clinics_have_health_fields(self, client: TestClient, super_admin_headers):
        """GET /v1/admin/builders clinic objects should include health fields."""
        response = client.get("/v1/admin/builders", headers=super_admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # If there are builders with clinics, verify health fields
        for builder in data:
            for clinic in builder.get("clinics", []):
                assert "health_status" in clinic
                assert "last_trace_at" in clinic
                assert "config_completeness" in clinic


# ============================================
# Helper Unit Tests
# ============================================


class TestHealthHelpers:
    """Unit tests for shared health computation helpers."""

    def test_compute_health_status_healthy(self):
        from src.api.helpers.health import _compute_health_status

        now = datetime.now(timezone.utc)
        assert _compute_health_status(now - timedelta(hours=1), True) == "healthy"

    def test_compute_health_status_warning(self):
        from src.api.helpers.health import _compute_health_status

        now = datetime.now(timezone.utc)
        assert _compute_health_status(now - timedelta(days=5), True) == "warning"

    def test_compute_health_status_critical_old(self):
        from src.api.helpers.health import _compute_health_status

        now = datetime.now(timezone.utc)
        assert _compute_health_status(now - timedelta(days=10), True) == "critical"

    def test_compute_health_status_critical_no_trace(self):
        from src.api.helpers.health import _compute_health_status

        assert _compute_health_status(None, True) == "critical"

    def test_compute_health_status_critical_inactive(self):
        from src.api.helpers.health import _compute_health_status

        now = datetime.now(timezone.utc)
        assert _compute_health_status(now - timedelta(hours=1), False) == "critical"

    def test_compute_config_completeness_none(self):
        from src.api.helpers.health import _compute_config_completeness

        assert _compute_config_completeness(None) == 0

    def test_compute_config_completeness_partial(self):
        from src.api.helpers.health import _compute_config_completeness

        class MockConfig:
            identity = {"clinic_name": "Test"}
            operations = {"hours": "8-18"}
            business_rules = {}  # empty dict = not filled
            persona = {"name": "Ana"}
            services = [{"name": "Consulta"}]
            professionals = []  # empty list = not filled

        assert _compute_config_completeness(MockConfig()) == 66

    def test_compute_config_completeness_full(self):
        from src.api.helpers.health import _compute_config_completeness

        class FullConfig:
            identity = {"a": 1}
            operations = {"b": 2}
            business_rules = {"c": 3}
            persona = {"d": 4}
            services = [{"e": 5}]
            professionals = [{"f": 6}]

        assert _compute_config_completeness(FullConfig()) == 100


class TestHealthHelperImports:
    """Test that helpers remain importable from builder.py (backward compat)."""

    def test_import_compute_health_status_from_builder(self):
        from src.api.routes.builder import _compute_health_status
        assert callable(_compute_health_status)

    def test_import_compute_config_completeness_from_builder(self):
        from src.api.routes.builder import _compute_config_completeness
        assert callable(_compute_config_completeness)

    def test_import_config_buckets_from_builder(self):
        from src.api.routes.builder import CONFIG_COMPLETENESS_BUCKETS
        assert isinstance(CONFIG_COMPLETENESS_BUCKETS, list)
        assert len(CONFIG_COMPLETENESS_BUCKETS) == 6
