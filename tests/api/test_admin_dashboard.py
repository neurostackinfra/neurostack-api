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
        assert "total_clinics" in stats
        assert "active_clinics" in stats
        assert "total_builders" in stats
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

    @pytest.mark.asyncio
    async def test_stats_and_dashboard_share_real_clinic_and_active_builder_totals(
        self, async_client, super_admin_headers, monkeypatch
    ):
        """KPI endpoints use the clinic-list population and billing-active builders."""
        from src.api.routes.admin import _compute_platform_totals
        from src.models.builder import Builder
        from src.models.tenant import Tenant
        from src.models.user import User, UserRole
        from src.repositories import get_db_session
        from sqlalchemy import func, select

        async def _empty_time_series(*args, **kwargs):
            return []

        async def _empty_alerts(*args, **kwargs):
            return []

        # Keep this KPI contract test independent of PostgreSQL-specific
        # time-series aggregation and alert computation.
        monkeypatch.setattr("src.api.routes.admin._get_time_series", _empty_time_series)
        monkeypatch.setattr("src.api.routes.admin._compute_alerts", _empty_alerts)

        session = get_db_session()
        try:
            baseline = await _compute_platform_totals(session)
            baseline_active_pilots_result = await session.execute(
                select(func.count(Tenant.id)).where(
                    Tenant.is_active.is_(True),
                    Tenant.plan_type.in_(("pilot", "clinic")),
                )
            )
            baseline_active_pilots = baseline_active_pilots_result.scalar() or 0

            active_clinic_id = uuid4()
            inactive_clinic_id = uuid4()
            internal_tenant_id = uuid4()
            builder_tenant_id = uuid4()
            investor_tenant_id = uuid4()
            session.add_all(
                [
                    Tenant(
                        id=active_clinic_id,
                        name="KPI Active Clinic",
                        slug=f"kpi-active-{active_clinic_id.hex}",
                        api_key_hash=f"kpi-active-hash-{active_clinic_id.hex}",
                        is_active=True,
                        config={},
                    ),
                    Tenant(
                        id=inactive_clinic_id,
                        name="KPI Inactive Clinic",
                        slug=f"kpi-inactive-{inactive_clinic_id.hex}",
                        api_key_hash=f"kpi-inactive-hash-{inactive_clinic_id.hex}",
                        is_active=False,
                        config={},
                    ),
                    Tenant(
                        id=internal_tenant_id,
                        name="KPI Internal Tenant",
                        slug=f"kpi-internal-{internal_tenant_id.hex}",
                        api_key_hash=f"kpi-internal-hash-{internal_tenant_id.hex}",
                        is_active=True,
                        config={},
                    ),
                    Tenant(
                        id=builder_tenant_id,
                        name="KPI Builder Tenant",
                        slug=f"kpi-builder-{builder_tenant_id.hex}",
                        api_key_hash=f"kpi-builder-hash-{builder_tenant_id.hex}",
                        is_active=True,
                        config={},
                    ),
                    Tenant(
                        id=investor_tenant_id,
                        name="KPI Investor Tenant",
                        slug=f"kpi-investor-{investor_tenant_id.hex}",
                        api_key_hash=f"kpi-investor-hash-{investor_tenant_id.hex}",
                        is_active=True,
                        config={},
                    ),
                    User(
                        tenant_id=active_clinic_id,
                        email=f"clinic-{active_clinic_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.clinic_admin,
                        is_active=True,
                    ),
                    User(
                        tenant_id=internal_tenant_id,
                        email=f"admin-{internal_tenant_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.super_admin,
                        is_active=True,
                    ),
                    User(
                        tenant_id=builder_tenant_id,
                        email=f"builder-{builder_tenant_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.builder,
                        is_active=True,
                    ),
                    User(
                        tenant_id=investor_tenant_id,
                        email=f"investor-{investor_tenant_id.hex}@test.local",
                        password_hash="test",
                        role=UserRole.investor,
                        is_active=True,
                    ),
                ]
            )

            toggle_builder_id = uuid4()
            for builder_id, subscription_status, is_active, builder_status in [
                (uuid4(), "trial", True, "active"),
                (uuid4(), "pilot", True, "active"),
                (toggle_builder_id, "active", True, "active"),
                (uuid4(), "past_due", True, "active"),
                (uuid4(), "canceled", True, "active"),
                (uuid4(), "active", False, "active"),
                (uuid4(), "active", True, "inactive"),
            ]:
                session.add(
                    Builder(
                        id=builder_id,
                        name=f"KPI Builder {subscription_status} {uuid4().hex}",
                        status=builder_status,
                        is_active=is_active,
                        subscription_status=subscription_status,
                    )
                )
            await session.commit()
        finally:
            await session.close()

        stats_response = await async_client.get("/v1/admin/stats", headers=super_admin_headers)
        dashboard_response = await async_client.get("/v1/admin/dashboard", headers=super_admin_headers)
        assert stats_response.status_code == 200
        assert dashboard_response.status_code == 200

        stats = stats_response.json()
        dashboard_stats = dashboard_response.json()["stats"]
        expected = {
            "total_clinics": baseline["total_clinics"] + 2,
            "active_clinics": baseline["active_clinics"] + 1,
            "total_builders": baseline["total_builders"] + 7,
            "active_builders": baseline["active_builders"] + 3,
        }
        assert {field: stats[field] for field in expected} == expected
        assert {field: dashboard_stats[field] for field in expected} == expected
        assert stats["active_pilots"] == baseline_active_pilots + 4
        assert dashboard_stats["active_pilots"] == baseline_active_pilots + 4

        patch_response = await async_client.patch(
            f"/v1/admin/builders/{toggle_builder_id}",
            headers=super_admin_headers,
            json={"status": "inactive"},
        )
        assert patch_response.status_code == 200

        stats_after = (await async_client.get("/v1/admin/stats", headers=super_admin_headers)).json()
        dashboard_after = (
            await async_client.get("/v1/admin/dashboard", headers=super_admin_headers)
        ).json()["stats"]
        expected_after = {**expected, "active_builders": expected["active_builders"] - 1}
        assert {field: stats_after[field] for field in expected_after} == expected_after
        assert {field: dashboard_after[field] for field in expected_after} == expected_after
        assert stats_after["active_pilots"] == stats["active_pilots"]
        assert dashboard_after["active_pilots"] == dashboard_stats["active_pilots"]


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

    @pytest.mark.asyncio
    async def test_builders_keeps_inactive_linked_clinic_manageable(
        self, async_client, super_admin_headers
    ):
        from src.models.builder import Builder
        from src.models.clinic_config_table import ClinicConfigTable
        from src.models.tenant import Tenant
        from src.repositories import get_db_session

        builder_id = uuid4()
        clinic_id = uuid4()
        session = get_db_session()
        try:
            session.add_all(
                [
                    Builder(
                        id=builder_id,
                        name="Inactive Clinic Builder",
                        status="active",
                        is_active=True,
                        subscription_status="active",
                    ),
                    Tenant(
                        id=clinic_id,
                        name="Inactive Linked Clinic",
                        slug=f"inactive-linked-{clinic_id.hex}",
                        api_key_hash=f"inactive-linked-hash-{clinic_id.hex}",
                        is_active=False,
                        plan_type="clinic",
                        builder_id=builder_id,
                        config={},
                    ),
                    ClinicConfigTable(
                        tenant_id=clinic_id,
                        clinic_id=clinic_id,
                        api_credentials={"zapi_instance_id": "configured-instance"},
                    ),
                ]
            )
            await session.commit()
        finally:
            await session.close()

        response = await async_client.get("/v1/admin/builders", headers=super_admin_headers)
        assert response.status_code == 200
        builder = next(item for item in response.json() if item["id"] == str(builder_id))
        clinic = next(item for item in builder["clinics"] if item["clinic_id"] == str(clinic_id))
        assert clinic["status"] == "inactive"
        assert clinic["apis_configured"] is True

        clinics_response = await async_client.get("/v1/admin/clinics", headers=super_admin_headers)
        assert clinics_response.status_code == 200
        admin_clinic = next(
            item for item in clinics_response.json() if item["clinic_id"] == str(clinic_id)
        )
        assert admin_clinic["apis_configured"] is True
        assert "zapi_instance_id" not in admin_clinic


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


# ============================================
# api_error alert unit tests (readiness-driven)
# ============================================

_COMPLETE_LLM = {
    "llm_provider": "openai",
    "llm_api_url": "https://api.openai.com/v1",
    "llm_api_key": "sk-live",
    "llm_model_name": "gpt-4o-mini",
}

_COMPLETE_META_WHATSAPP = {
    "whatsapp_provider": "meta_cloud",
    "meta_phone_number_id": "1234567890",
    "meta_permanent_token": "EAA-token",
    "meta_app_secret": "super-secret",
}

_COMPLETE_UAZAPI_WHATSAPP = {
    "whatsapp_provider": "uazapi",
    "uazapi_api_url": "https://uazapi.example.com",
    "uazapi_instance_name": "clinic-inst",
    "uazapi_token": "uaz-token",
}

_COMPLETE_ZAPI_WHATSAPP = {
    "whatsapp_provider": "zapi",
    "zapi_instance_id": "zapi-inst",
    "zapi_token": "zapi-tok",
    "zapi_inbound_token_digest": "digest-sha256",
}


def _seed_tenant_and_config(session, *, builder_id, tenant_id, name, api_credentials):
    """Seed a builder, tenant and (optionally) a clinic_config row.

    We deliberately do not seed a recent Event: SQLite drops tz info from
    DateTime columns and `_compute_alerts` compares tz-aware ``created_at``
    against a tz-aware "now". Skipping the event lets every clinic pick up an
    additional ``inactivity`` alert, which we then filter out of assertions by
    matching on ``type == 'api_error'`` and ``clinic_id``.
    """
    from src.models.builder import Builder
    from src.models.clinic_config_table import ClinicConfigTable
    from src.models.tenant import Tenant

    session.add_all(
        [
            Builder(id=builder_id, name=f"Builder {name}", status="active",
                    is_active=True),
            Tenant(
                id=tenant_id,
                name=name,
                slug=f"clinic-{tenant_id.hex}",
                api_key_hash=f"hash-{tenant_id.hex}",
                is_active=True,
                bot_active=True,
                builder_id=builder_id,
                plan_type="pilot",
                config={},
            ),
        ]
    )
    if api_credentials is not None:
        session.add(
            ClinicConfigTable(
                tenant_id=tenant_id,
                clinic_id=tenant_id,
                api_credentials=api_credentials,
            )
        )


async def _run_alerts(builder_id):
    from src.api.helpers.health import _compute_alerts
    from src.repositories import get_db_session

    session = get_db_session()
    try:
        return await _compute_alerts(builder_id, session)
    finally:
        await session.close()


def _api_error_alerts_for(alerts, clinic_id):
    return [a for a in alerts if a.type == "api_error" and a.clinic_id == clinic_id]


class TestApiErrorAlertReadiness:
    """`_compute_alerts` derives api_error from credential readiness, one per clinic."""

    @pytest.mark.asyncio
    async def test_meta_cloud_complete_no_alert(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="Meta Complete",
                api_credentials={**_COMPLETE_META_WHATSAPP, **_COMPLETE_LLM},
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        assert _api_error_alerts_for(alerts, tenant_id) == []

    @pytest.mark.asyncio
    async def test_uazapi_complete_no_alert(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="Uazapi Complete",
                api_credentials={**_COMPLETE_UAZAPI_WHATSAPP, **_COMPLETE_LLM},
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        assert _api_error_alerts_for(alerts, tenant_id) == []

    @pytest.mark.asyncio
    async def test_zapi_complete_no_alert(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="Zapi Complete",
                api_credentials={**_COMPLETE_ZAPI_WHATSAPP, **_COMPLETE_LLM},
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        assert _api_error_alerts_for(alerts, tenant_id) == []

    @pytest.mark.asyncio
    async def test_missing_llm_only_one_alert_naming_llm(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="No LLM",
                api_credentials=dict(_COMPLETE_META_WHATSAPP),
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        my = _api_error_alerts_for(alerts, tenant_id)
        assert len(my) == 1
        assert my[0].severity == "info"
        assert "API LLM" in my[0].message
        assert "API WhatsApp" not in my[0].message

    @pytest.mark.asyncio
    async def test_missing_whatsapp_and_llm_single_alert_lists_both(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="Empty Creds",
                api_credentials={},
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        my = _api_error_alerts_for(alerts, tenant_id)
        assert len(my) == 1
        msg = my[0].message
        assert "API WhatsApp" in msg
        assert "API LLM" in msg
        # Single "sem" clause listing both, joined with " e ".
        assert msg.startswith("sem ")
        assert " e " in msg

    @pytest.mark.asyncio
    async def test_signature_pending_message_cites_app_secret(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="Meta Sig Pending",
                api_credentials={
                    "whatsapp_provider": "meta_cloud",
                    "meta_phone_number_id": "1234567890",
                    "meta_permanent_token": "EAA-token",
                    # meta_app_secret intentionally missing
                    **_COMPLETE_LLM,
                },
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        my = _api_error_alerts_for(alerts, tenant_id)
        assert len(my) == 1
        msg = my[0].message
        assert "App Secret" in msg
        assert "sem API WhatsApp" not in msg

    @pytest.mark.asyncio
    async def test_stt_key_without_model_flags_incomplete(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="STT Missing Model",
                api_credentials={
                    **_COMPLETE_META_WHATSAPP,
                    **_COMPLETE_LLM,
                    "stt_provider": "openai_whisper",
                    "stt_api_url": "https://api.openai.com/v1",
                    "stt_api_key": "sk-stt",
                    # stt_model_name missing
                },
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        my = _api_error_alerts_for(alerts, tenant_id)
        assert len(my) == 1
        assert "API STT incompleta: falta URL ou modelo" in my[0].message

    @pytest.mark.asyncio
    async def test_stt_configuration_only_provider_flagged(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="STT google_stt",
                api_credentials={
                    **_COMPLETE_META_WHATSAPP,
                    **_COMPLETE_LLM,
                    "stt_provider": "google_stt",
                    "stt_api_url": "https://speech.googleapis.com",
                    "stt_api_key": "goog-key",
                    "stt_model_name": "latest_long",
                },
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        my = _api_error_alerts_for(alerts, tenant_id)
        assert len(my) == 1
        assert "API STT incompleta: provedor nao homologado" in my[0].message

    @pytest.mark.asyncio
    async def test_stt_no_key_absent_from_message(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="Missing LLM Only",
                api_credentials=dict(_COMPLETE_META_WHATSAPP),
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        my = _api_error_alerts_for(alerts, tenant_id)
        assert len(my) == 1
        assert "API STT" not in my[0].message

    @pytest.mark.asyncio
    async def test_legacy_whisper_alias_without_url_alerts(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="Legacy whisper alias",
                api_credentials={
                    **_COMPLETE_META_WHATSAPP,
                    **_COMPLETE_LLM,
                    # legacy alias for stt_api_key, no stt_api_url.
                    "whisper_api_key": "sk-legacy",
                },
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        my = _api_error_alerts_for(alerts, tenant_id)
        assert len(my) == 1
        assert "API STT" in my[0].message

    @pytest.mark.asyncio
    async def test_legacy_whisper_alias_with_url_resolves_active(self, async_client):
        # Same alias flows through resolve_stt_credentials which defaults
        # stt_provider to "outros" (operational) when a URL is also present,
        # so readiness resolves `active` and no alert should be produced.
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="Legacy whisper alias complete",
                api_credentials={
                    **_COMPLETE_META_WHATSAPP,
                    **_COMPLETE_LLM,
                    "whisper_api_key": "sk-legacy",
                    "stt_api_url": "https://api.openai.com/v1",
                    "stt_model_name": "whisper-1",
                },
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        assert _api_error_alerts_for(alerts, tenant_id) == []

    @pytest.mark.asyncio
    async def test_no_config_row_yields_single_alert_with_both_names(self, async_client):
        from src.repositories import get_db_session
        builder_id = uuid4()
        tenant_id = uuid4()
        session = get_db_session()
        try:
            _seed_tenant_and_config(
                session,
                builder_id=builder_id,
                tenant_id=tenant_id,
                name="No Config",
                api_credentials=None,  # do not seed ClinicConfigTable
            )
            await session.commit()
        finally:
            await session.close()

        alerts = await _run_alerts(builder_id)
        my = _api_error_alerts_for(alerts, tenant_id)
        assert len(my) == 1
        msg = my[0].message
        assert "API WhatsApp" in msg
        assert "API LLM" in msg
