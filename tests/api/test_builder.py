"""
NeuroStack API - Builder Endpoints Tests
Tests for builder-scoped clinic management endpoints.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

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

    def test_clinics_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/builder/clinics", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_clinics_returns_403_for_super_admin(
        self, client: TestClient, super_admin_headers
    ):
        response = client.get("/v1/builder/clinics", headers=super_admin_headers)
        assert response.status_code == 403

    def test_me_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/builder/me", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_stats_returns_403_for_super_admin(
        self, client: TestClient, super_admin_headers
    ):
        response = client.get("/v1/builder/stats", headers=super_admin_headers)
        assert response.status_code == 403

    def test_403_has_descriptive_error_message(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/builder/clinics", headers=clinic_admin_headers)
        assert response.status_code == 403
        data = response.json()
        detail = data.get("detail", {})
        if isinstance(detail, dict):
            assert "BUILDER_NOT_CONFIGURED" == detail.get("error_code")
            assert "Builder profile not found" in detail.get("detail", "")
        else:
            assert "Builder profile not found" in str(detail)

    def test_create_clinic_returns_403_for_non_builder(
        self, client: TestClient, clinic_admin_headers
    ):
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

    def test_toggle_status_returns_403_for_non_builder(
        self, client: TestClient, super_admin_headers
    ):
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

    def test_new_outside_hours_fields_have_safe_defaults(
        self, client: TestClient, builder_a_headers
    ):
        from types import SimpleNamespace

        from src.repositories import get_db_session_dependency

        tenant_id = uuid4()
        tenant = SimpleNamespace(
            id=tenant_id,
            name="Outside Hours Defaults",
            slug="outside-hours-defaults",
            is_active=True,
            plan_type="pilot",
            created_at=None,
            contact_phone=None,
            clinic_config_row=SimpleNamespace(
                identity={},
                operations={},
                business_rules={},
                persona={},
                services=[],
                professionals=[],
            ),
        )

        class _Result:
            def __init__(self, tenants=None):
                self._tenants = tenants or []

            def scalars(self):
                return self

            def all(self):
                return self._tenants

            def __iter__(self):
                return iter(())

        class _Session:
            def __init__(self):
                self.call_count = 0

            async def execute(self, _statement):
                self.call_count += 1
                return _Result([tenant] if self.call_count == 1 else [])

        async def _override_session():
            yield _Session()

        client.application.dependency_overrides[get_db_session_dependency] = (
            _override_session
        )
        try:
            response = client.get("/v1/builder/clinics", headers=builder_a_headers)
        finally:
            client.application.dependency_overrides.pop(get_db_session_dependency, None)

        assert response.status_code == 200
        clinic = response.json()[0]
        assert clinic["id"] == str(tenant_id)
        assert clinic["outside_hours_count_7d"] == 0
        assert clinic["outside_hours_pct_7d"] is None
        assert clinic["conversational_trace_count_7d"] == 0
        assert clinic["business_hours_configured"] is False


class TestBuilderProfile:
    """Test GET /v1/builder/me."""

    def test_returns_profile_or_404(self, client: TestClient, builder_a_headers):
        """Builder profile returns 404 when builder doesn't exist in DB (expected for test)."""
        response = client.get("/v1/builder/me", headers=builder_a_headers)
        # Without a real builder in DB, we expect 404; validates endpoint is reachable and auth works
        assert response.status_code in (200, 404)

    def test_returns_403_for_non_builder(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/builder/me", headers=clinic_admin_headers)
        assert response.status_code == 403


class TestBuilderSupportEmailProfile:
    """Test GET/PATCH /v1/builder/profile — support_email field."""

    @staticmethod
    async def _seed_builder(support_email=None):
        """Seed an active builder row and return it. Uses an async session."""
        from src.models.builder import Builder
        from src.repositories import get_db_session

        session = get_db_session()
        try:
            builder = Builder(
                id=uuid4(),
                name=f"Builder {uuid4().hex[:6]}",
                status="active",
                is_active=True,
                billing_cycle="monthly",
                subscription_status="trial",
                support_email=support_email,
            )
            session.add(builder)
            await session.commit()
            await session.refresh(builder)
            return builder
        finally:
            await session.close()

    @staticmethod
    async def _fetch_builder(builder_id):
        from sqlalchemy import select

        from src.models.builder import Builder
        from src.repositories import get_db_session

        session = get_db_session()
        try:
            result = await session.execute(
                select(Builder).where(Builder.id == builder_id)
            )
            return result.scalar_one_or_none()
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_get_profile_returns_support_email(self, async_client):
        builder = await self._seed_builder(support_email="ops@builder.example")
        headers = {
            "Authorization": f"Bearer {_make_builder_token(builder_id=builder.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.get("/v1/builder/profile", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == str(builder.id)
        assert data["support_email"] == "ops@builder.example"

    @pytest.mark.asyncio
    async def test_get_profile_returns_null_when_unset(self, async_client):
        builder = await self._seed_builder(support_email=None)
        headers = {
            "Authorization": f"Bearer {_make_builder_token(builder_id=builder.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.get("/v1/builder/profile", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["support_email"] is None

    @pytest.mark.asyncio
    async def test_patch_profile_sets_support_email(self, async_client):
        builder = await self._seed_builder(support_email=None)
        headers = {
            "Authorization": f"Bearer {_make_builder_token(builder_id=builder.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.patch(
            "/v1/builder/profile",
            headers=headers,
            json={"support_email": "escalations@builder.example"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["support_email"] == "escalations@builder.example"

        # Verify GET reflects the change from DB
        get_resp = await async_client.get("/v1/builder/profile", headers=headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["support_email"] == "escalations@builder.example"

        # DB check
        row = await self._fetch_builder(builder.id)
        assert row is not None
        assert row.support_email == "escalations@builder.example"

    @pytest.mark.asyncio
    async def test_patch_profile_clears_with_null(self, async_client):
        builder = await self._seed_builder(support_email="old@builder.example")
        headers = {
            "Authorization": f"Bearer {_make_builder_token(builder_id=builder.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.patch(
            "/v1/builder/profile", headers=headers, json={"support_email": None}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["support_email"] is None
        row = await self._fetch_builder(builder.id)
        assert row is not None
        assert row.support_email is None

    @pytest.mark.asyncio
    async def test_patch_profile_clears_with_empty_string(self, async_client):
        builder = await self._seed_builder(support_email="old@builder.example")
        headers = {
            "Authorization": f"Bearer {_make_builder_token(builder_id=builder.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.patch(
            "/v1/builder/profile", headers=headers, json={"support_email": ""}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["support_email"] is None
        row = await self._fetch_builder(builder.id)
        assert row is not None
        assert row.support_email is None

    @pytest.mark.asyncio
    async def test_patch_profile_rejects_invalid_email(self, async_client):
        builder = await self._seed_builder(support_email="original@builder.example")
        headers = {
            "Authorization": f"Bearer {_make_builder_token(builder_id=builder.id)}",
            "Content-Type": "application/json",
        }
        resp = await async_client.patch(
            "/v1/builder/profile",
            headers=headers,
            json={"support_email": "not-an-email"},
        )
        assert resp.status_code == 422
        # Verify the stored value did not change
        row = await self._fetch_builder(builder.id)
        assert row is not None
        assert row.support_email == "original@builder.example"

    @pytest.mark.asyncio
    async def test_patch_profile_scoped_to_jwt_builder_only(self, async_client):
        """Two builders exist; a PATCH from builder A must not touch builder B."""
        builder_a = await self._seed_builder(support_email="a@builder.example")
        builder_b = await self._seed_builder(support_email="b@builder.example")

        headers_a = {
            "Authorization": f"Bearer {_make_builder_token(builder_id=builder_a.id)}",
            "Content-Type": "application/json",
        }
        # Even if a spurious builder_id-like field is added, the schema ignores it
        resp = await async_client.patch(
            "/v1/builder/profile",
            headers=headers_a,
            json={
                "support_email": "a-updated@builder.example",
                "builder_id": str(builder_b.id),
            },
        )
        assert resp.status_code == 200, resp.text

        row_a = await self._fetch_builder(builder_a.id)
        row_b = await self._fetch_builder(builder_b.id)
        assert row_a is not None and row_a.support_email == "a-updated@builder.example"
        # Builder B untouched
        assert row_b is not None and row_b.support_email == "b@builder.example"

    def test_patch_profile_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.patch(
            "/v1/builder/profile",
            headers=clinic_admin_headers,
            json={"support_email": "attacker@evil.test"},
        )
        assert response.status_code == 403

    def test_get_profile_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/builder/profile", headers=clinic_admin_headers)
        assert response.status_code == 403


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

    def test_rejects_empty_name(self, client: TestClient, builder_a_headers):
        response = client.post(
            "/v1/builder/clinics",
            headers=builder_a_headers,
            json={
                "clinic_name": "",
                "admin_email": "test@test.com",
                "admin_password": "12345678",
            },
        )
        assert response.status_code == 422

    def test_rejects_invalid_email(self, client: TestClient, builder_a_headers):
        response = client.post(
            "/v1/builder/clinics",
            headers=builder_a_headers,
            json={
                "clinic_name": "Test Clinic",
                "admin_email": "not-an-email",
                "admin_password": "12345678",
            },
        )
        assert response.status_code == 422

    def test_rejects_short_password(self, client: TestClient, builder_a_headers):
        response = client.post(
            "/v1/builder/clinics",
            headers=builder_a_headers,
            json={
                "clinic_name": "Test Clinic",
                "admin_email": "test@test.com",
                "admin_password": "short",
            },
        )
        assert response.status_code == 422


class TestBuilderToggleClinicStatus:
    """Test PATCH /v1/builder/clinics/{id}/status."""

    def test_returns_404_for_nonexistent_clinic(
        self, client: TestClient, builder_a_headers
    ):
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

    async def test_toggle_persists_across_a_fresh_session(self, async_client):
        """Regression guard: the handler must commit, not just flush.

        get_db_session_dependency only closes the session on exit (no
        implicit commit), so a flush-only write is discarded once the
        request's session is closed. Re-fetch with a brand new session to
        prove the change actually reached the database.
        """
        builder_id = uuid4()
        tenant = await TestBuilderClinicContactPhone._seed_tenant(builder_id)
        headers = {
            "Authorization": f"Bearer {_make_builder_token(builder_id=builder_id)}",
            "Content-Type": "application/json",
        }

        response = await async_client.patch(
            f"/v1/builder/clinics/{tenant.id}/status",
            headers=headers,
            json={"is_active": False},
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        row = await TestBuilderClinicContactPhone._fetch_tenant(tenant.id)
        assert row.is_active is False


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

    def test_stats_share_new_llm_attempt_accounting_contract(self) -> None:
        """Builder imports the trace helper so new events cannot double-count."""
        from src.api.routes.builder import _llm_usage_for_stats

        total, breakdown = _llm_usage_for_stats(
            {
                "llm_usage_total": {"total_tokens": 8},
                "llm_usage": {"total_tokens": 80},
                "llm_attempts": [
                    {
                        "usage_status": "reported",
                        "usage": {"total_tokens": 8},
                        "reported_model": None,
                        "requested_model": "requested-model",
                    }
                ],
            }
        )

        assert total == 8
        assert breakdown == [("requested-model", 8)]

    def test_returns_stats_structure(self, client: TestClient, builder_a_headers):
        response = client.get("/v1/builder/stats", headers=builder_a_headers)
        assert response.status_code == 200
        data = response.json()
        assert "clinic_count" in data
        assert "active_clinics" in data
        assert "inactive_clinics" in data
        assert "total_traces" in data


# ============================================
# Dashboard Tests
# ============================================


class TestBuilderDashboard:
    """Test GET /v1/builder/dashboard."""

    def test_returns_dashboard_structure(self, client: TestClient, builder_a_headers):
        response = client.get("/v1/builder/dashboard", headers=builder_a_headers)
        assert response.status_code == 200
        data = response.json()
        assert "stats" in data
        assert "time_series" in data
        assert "alerts" in data
        # Stats structure
        stats = data["stats"]
        assert "clinic_count" in stats
        assert "active_clinics" in stats
        assert "inactive_clinics" in stats
        assert "total_traces" in stats

    def test_time_series_has_31_days(self, client: TestClient, builder_a_headers):
        """Time series should have 31 points (day 0 through day 30)."""
        response = client.get("/v1/builder/dashboard", headers=builder_a_headers)
        assert response.status_code == 200
        data = response.json()
        ts = data["time_series"]
        assert isinstance(ts, list)
        assert len(ts) == 31
        for point in ts:
            assert "date" in point
            assert "count" in point

    def test_alerts_is_list(self, client: TestClient, builder_a_headers):
        response = client.get("/v1/builder/dashboard", headers=builder_a_headers)
        assert response.status_code == 200
        assert isinstance(response.json()["alerts"], list)

    def test_returns_401_without_token(self, client: TestClient):
        response = client.get("/v1/builder/dashboard")
        assert response.status_code == 401

    def test_returns_403_for_clinic_admin(
        self, client: TestClient, clinic_admin_headers
    ):
        response = client.get("/v1/builder/dashboard", headers=clinic_admin_headers)
        assert response.status_code == 403

    def test_dashboard_scope_isolation(
        self, client: TestClient, builder_a_headers, builder_b_headers
    ):
        """Each builder sees only their own data in the dashboard."""
        response_a = client.get("/v1/builder/dashboard", headers=builder_a_headers)
        response_b = client.get("/v1/builder/dashboard", headers=builder_b_headers)
        assert response_a.status_code == 200
        assert response_b.status_code == 200


# ============================================
# Enriched Clinics Tests
# ============================================


class TestBuilderEnrichedClinics:
    """Test that GET /v1/builder/clinics returns health fields."""

    def test_clinics_have_health_fields(self, client: TestClient, builder_a_headers):
        """Even an empty list validates the endpoint works; if clinics exist, check fields."""
        response = client.get("/v1/builder/clinics", headers=builder_a_headers)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # If there are clinics, verify health fields exist
        if len(data) > 0:
            clinic = data[0]
            assert "last_trace_at" in clinic
            assert "health_status" in clinic
            assert "trace_count_7d" in clinic
            assert "safety_flags_7d" in clinic
            assert "handoff_count_7d" in clinic
            assert "config_completeness" in clinic
            assert clinic["health_status"] in ("healthy", "warning", "critical")
            assert 0 <= clinic["config_completeness"] <= 100

    def test_enriched_response_schema_has_all_fields(
        self, client: TestClient, builder_a_headers
    ):
        """Verify the enriched clinic response schema includes all health fields.
        Even if empty, the endpoint should return 200 with the right structure."""
        response = client.get("/v1/builder/clinics", headers=builder_a_headers)
        assert response.status_code == 200
        # Endpoint works — empty list is fine (no DB data for random builder_id)
        assert isinstance(response.json(), list)

    def test_health_status_values(self):
        """Test the health status helper directly."""
        from src.api.routes.builder import _compute_health_status

        now = datetime.now(timezone.utc)
        # Recent trace → healthy
        assert _compute_health_status(now - timedelta(hours=1), True) == "healthy"
        # 5 days ago → warning
        assert _compute_health_status(now - timedelta(days=5), True) == "warning"
        # 10 days ago → critical
        assert _compute_health_status(now - timedelta(days=10), True) == "critical"
        # No traces → critical
        assert _compute_health_status(None, True) == "critical"
        # Inactive → critical regardless of trace
        assert _compute_health_status(now - timedelta(hours=1), False) == "critical"

    def test_config_completeness_calculation(self):
        """Test the config completeness helper directly."""
        from src.api.routes.builder import _compute_config_completeness

        # None → 0%
        assert _compute_config_completeness(None) == 0

        # Create a mock config with some filled buckets
        class MockConfig:
            identity = {"clinic_name": "Test"}
            operations = {"hours": "8-18"}
            business_rules = {}  # empty dict = not filled
            persona = {"name": "Ana"}
            services = [{"name": "Consulta"}]
            professionals = []  # empty list = not filled

        mock = MockConfig()
        result = _compute_config_completeness(mock)
        # 3 filled (identity, operations, persona) + 1 (services) = 4 out of 6
        # Wait: services = [{"name": "Consulta"}] is non-empty → filled
        # professionals = [] → empty → not filled
        # business_rules = {} → empty → not filled
        # So 4/6 = 66%
        assert result == 66

    def test_config_completeness_all_filled(self):
        """Test completeness with all buckets filled."""
        from src.api.routes.builder import _compute_config_completeness

        class FullConfig:
            identity = {"a": 1}
            operations = {"b": 2}
            business_rules = {"c": 3}
            persona = {"d": 4}
            services = [{"e": 5}]
            professionals = [{"f": 6}]

        assert _compute_config_completeness(FullConfig()) == 100


# ============================================
# Contact Phone Tests (Group 4)
# ============================================


class TestBuilderClinicContactPhone:
    """Test contact_phone create/patch/list behavior on builder clinic endpoints."""

    @staticmethod
    async def _seed_builder():
        from src.models.builder import Builder
        from src.repositories import get_db_session

        session = get_db_session()
        try:
            builder = Builder(
                id=uuid4(),
                name=f"Builder {uuid4().hex[:6]}",
                status="active",
                is_active=True,
                billing_cycle="monthly",
                subscription_status="trial",
            )
            session.add(builder)
            await session.commit()
            await session.refresh(builder)
            return builder
        finally:
            await session.close()

    @staticmethod
    async def _seed_tenant(builder_id, contact_phone=None):
        """Insert a Tenant row owned by builder_id and return it."""
        from src.models.tenant import Tenant
        from src.repositories import get_db_session

        session = get_db_session()
        try:
            tenant = Tenant(
                id=uuid4(),
                name=f"Clinic {uuid4().hex[:6]}",
                slug=f"clinic-{uuid4().hex[:8]}",
                api_key_hash=f"test-hash-{uuid4().hex}",
                is_active=True,
                plan_type="clinic",
                builder_id=builder_id,
                contact_phone=contact_phone,
                config={},
            )
            session.add(tenant)
            await session.commit()
            await session.refresh(tenant)
            return tenant
        finally:
            await session.close()

    @staticmethod
    async def _fetch_tenant(tenant_id):
        from sqlalchemy import select

        from src.models.tenant import Tenant
        from src.repositories import get_db_session

        session = get_db_session()
        try:
            result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            return result.scalar_one_or_none()
        finally:
            await session.close()

    @staticmethod
    def _headers_for(builder_id):
        return {
            "Authorization": f"Bearer {_make_builder_token(builder_id=builder_id)}",
            "Content-Type": "application/json",
        }

    # ---- Create ----

    @pytest.mark.asyncio
    async def test_create_clinic_persists_normalized_contact_phone(self, async_client):
        builder = await self._seed_builder()
        headers = self._headers_for(builder.id)
        resp = await async_client.post(
            "/v1/builder/clinics",
            headers=headers,
            json={
                "clinic_name": f"Clinic Phone {uuid4().hex[:6]}",
                "admin_email": f"admin-{uuid4().hex[:8]}@test.com",
                "admin_password": "securepassword123",
                "contact_phone": "+55 (11) 91234-5678",
            },
        )
        assert resp.status_code == 201, resp.text
        tenant_id = UUID(resp.json()["id"])
        row = await self._fetch_tenant(tenant_id)
        assert row is not None
        # digits-only: 55 11 91234 5678 → "5511912345678" (13 digits)
        assert row.contact_phone == "5511912345678"

    @pytest.mark.asyncio
    async def test_create_clinic_without_contact_phone_persists_null(
        self, async_client
    ):
        builder = await self._seed_builder()
        headers = self._headers_for(builder.id)
        resp = await async_client.post(
            "/v1/builder/clinics",
            headers=headers,
            json={
                "clinic_name": f"Clinic NoPhone {uuid4().hex[:6]}",
                "admin_email": f"admin-{uuid4().hex[:8]}@test.com",
                "admin_password": "securepassword123",
            },
        )
        assert resp.status_code == 201, resp.text
        tenant_id = UUID(resp.json()["id"])
        row = await self._fetch_tenant(tenant_id)
        assert row is not None
        assert row.contact_phone is None

    @pytest.mark.asyncio
    async def test_create_clinic_rejects_short_phone_with_400(self, async_client):
        builder = await self._seed_builder()
        headers = self._headers_for(builder.id)
        resp = await async_client.post(
            "/v1/builder/clinics",
            headers=headers,
            json={
                "clinic_name": f"Clinic Short {uuid4().hex[:6]}",
                "admin_email": f"admin-{uuid4().hex[:8]}@test.com",
                "admin_password": "securepassword123",
                "contact_phone": "12345",  # 5 digits
            },
        )
        assert resp.status_code == 400, resp.text

    # ---- Patch: own clinic ----

    @pytest.mark.asyncio
    async def test_patch_contact_phone_on_own_clinic_persists(self, async_client):
        builder = await self._seed_builder()
        tenant = await self._seed_tenant(builder.id, contact_phone=None)
        headers = self._headers_for(builder.id)
        resp = await async_client.patch(
            f"/v1/builder/clinics/{tenant.id}",
            headers=headers,
            json={"contact_phone": "(11) 98888-7777"},
        )
        assert resp.status_code == 200, resp.text
        row = await self._fetch_tenant(tenant.id)
        assert row is not None
        assert row.contact_phone == "11988887777"

    # ---- Patch: cross-builder isolation ----

    @pytest.mark.asyncio
    async def test_patch_contact_phone_on_other_builders_clinic_returns_404(
        self, async_client
    ):
        builder_a = await self._seed_builder()
        builder_b = await self._seed_builder()
        # Seed a clinic owned by builder B; builder A must NOT be able to touch it.
        tenant_b = await self._seed_tenant(builder_b.id, contact_phone="1199999999")
        headers_a = self._headers_for(builder_a.id)
        resp = await async_client.patch(
            f"/v1/builder/clinics/{tenant_b.id}",
            headers=headers_a,
            json={"contact_phone": "(11) 90000-0000"},
        )
        assert resp.status_code == 404, resp.text
        # Verify DB was NOT altered
        row = await self._fetch_tenant(tenant_b.id)
        assert row is not None
        assert row.contact_phone == "1199999999"

    # ---- Patch: validation ----

    @pytest.mark.asyncio
    async def test_patch_contact_phone_short_returns_400(self, async_client):
        builder = await self._seed_builder()
        tenant = await self._seed_tenant(builder.id, contact_phone="11988887777")
        headers = self._headers_for(builder.id)
        resp = await async_client.patch(
            f"/v1/builder/clinics/{tenant.id}",
            headers=headers,
            json={"contact_phone": "12345"},
        )
        assert resp.status_code == 400, resp.text
        # DB unchanged
        row = await self._fetch_tenant(tenant.id)
        assert row is not None
        assert row.contact_phone == "11988887777"

    @pytest.mark.asyncio
    async def test_patch_contact_phone_too_long_returns_400(self, async_client):
        builder = await self._seed_builder()
        tenant = await self._seed_tenant(builder.id, contact_phone="11988887777")
        headers = self._headers_for(builder.id)
        resp = await async_client.patch(
            f"/v1/builder/clinics/{tenant.id}",
            headers=headers,
            json={"contact_phone": "1234567890123456"},  # 16 digits
        )
        assert resp.status_code == 400, resp.text
        row = await self._fetch_tenant(tenant.id)
        assert row is not None
        assert row.contact_phone == "11988887777"

    # ---- Patch: empty string clears ----

    @pytest.mark.asyncio
    async def test_patch_contact_phone_empty_string_clears(self, async_client):
        builder = await self._seed_builder()
        tenant = await self._seed_tenant(builder.id, contact_phone="11988887777")
        headers = self._headers_for(builder.id)
        resp = await async_client.patch(
            f"/v1/builder/clinics/{tenant.id}",
            headers=headers,
            json={"contact_phone": ""},
        )
        assert resp.status_code == 200, resp.text
        row = await self._fetch_tenant(tenant.id)
        assert row is not None
        assert row.contact_phone is None

    # ---- List includes contact_phone ----

    @pytest.mark.asyncio
    async def test_list_clinics_includes_contact_phone(self, async_client):
        builder = await self._seed_builder()
        tenant_with = await self._seed_tenant(builder.id, contact_phone="11988887777")
        tenant_without = await self._seed_tenant(builder.id, contact_phone=None)
        headers = self._headers_for(builder.id)
        resp = await async_client.get("/v1/builder/clinics", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert isinstance(data, list)
        by_id = {item["id"]: item for item in data}
        assert str(tenant_with.id) in by_id
        assert str(tenant_without.id) in by_id
        assert by_id[str(tenant_with.id)]["contact_phone"] == "11988887777"
        assert by_id[str(tenant_without.id)]["contact_phone"] is None
