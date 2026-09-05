"""
NeuroStack API - Builder Clinic Access Tests

Tests for the builder tenant_id override mechanism on traces, sessions,
and session creation endpoints.
"""

import time
from typing import Optional
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings
from src.services import get_session_service

# ============================================
# Helpers
# ============================================


def _make_builder_jwt(
    tenant_id: UUID,
    builder_id: UUID,
    user_id: Optional[UUID] = None,
) -> str:
    """Create a valid builder JWT for testing."""
    payload = {
        "sub": str(user_id or uuid4()),
        "tenant_id": str(tenant_id),
        "role": "builder",
        "builder_id": str(builder_id),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _make_clinic_admin_jwt(
    tenant_id: UUID,
    user_id: Optional[UUID] = None,
) -> str:
    """Create a valid clinic_admin JWT for testing."""
    payload = {
        "sub": str(user_id or uuid4()),
        "tenant_id": str(tenant_id),
        "role": "clinic_admin",
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ============================================
# Test Data
# ============================================

BUILDER_TENANT_ID = UUID("b0000000-0000-0000-0000-000000000001")
BUILDER_ID = UUID("b0000000-0000-0000-0000-000000000099")
CLINIC_TENANT_ID = UUID("c0000000-0000-0000-0000-000000000001")
CLINIC_ADMIN_TENANT_ID = UUID("c0000000-0000-0000-0000-000000000002")


# ============================================
# Tests: Non-builder roles get 400 on tenant_id param
# ============================================


class TestNonBuilderTenantIdRejection:
    """Non-builder roles passing tenant_id query param must get 400."""

    def test_clinic_admin_traces_list_with_tenant_id_returns_400(
        self, client: TestClient
    ) -> None:
        token = _make_clinic_admin_jwt(CLINIC_ADMIN_TENANT_ID)
        response = client.get(
            "/v1/traces",
            params={"tenant_id": str(CLINIC_TENANT_ID)},
            headers=_auth_headers(token),
        )
        assert response.status_code == 400
        assert "not allowed" in response.json()["detail"].lower()

    def test_clinic_admin_trace_detail_with_tenant_id_returns_400(
        self, client: TestClient
    ) -> None:
        token = _make_clinic_admin_jwt(CLINIC_ADMIN_TENANT_ID)
        trace_id = uuid4()
        response = client.get(
            f"/v1/traces/{trace_id}",
            params={"tenant_id": str(CLINIC_TENANT_ID)},
            headers=_auth_headers(token),
        )
        assert response.status_code == 400

    def test_clinic_admin_sessions_list_with_different_tenant_id_returns_400(
        self, client: TestClient
    ) -> None:
        """clinic_admin passing a different tenant_id gets 400 because
        the route delegates to _resolve_tenant for builder-only override."""
        token = _make_clinic_admin_jwt(CLINIC_ADMIN_TENANT_ID)
        response = client.get(
            "/v1/sessions",
            params={"tenant_id": str(CLINIC_TENANT_ID)},
            headers=_auth_headers(token),
        )
        # clinic_admin can only query their own tenant_id; different ID
        # is handled by normal role logic (not builder override),
        # so this should return 200 (their own data) when IDs match,
        # or proceed normally when they pass their own tenant_id.
        # When they pass a DIFFERENT tenant_id, the endpoint just queries
        # with that tenant_id (sessions list doesn't have the same
        # _resolve_tenant guard for non-builders because tenant_id is the
        # required query param). This is the "general security gap" noted
        # as out of scope for this wish.
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "tenant_override_forbidden"


# ============================================
# Tests: Builder tenant_id override validation
# ============================================


class TestBuilderTenantOverride:
    """Builder passing tenant_id of a clinic they DON'T manage gets 403."""

    def test_builder_traces_list_unowned_clinic_returns_403(
        self, client: TestClient
    ) -> None:
        token = _make_builder_jwt(BUILDER_TENANT_ID, BUILDER_ID)
        response = client.get(
            "/v1/traces",
            params={"tenant_id": str(CLINIC_TENANT_ID)},
            headers=_auth_headers(token),
        )
        # 403 because no clinic in DB has builder_id matching BUILDER_ID
        assert response.status_code == 403
        assert "does not manage" in response.json()["detail"].lower()

    def test_builder_trace_detail_unowned_clinic_returns_403(
        self, client: TestClient
    ) -> None:
        token = _make_builder_jwt(BUILDER_TENANT_ID, BUILDER_ID)
        trace_id = uuid4()
        response = client.get(
            f"/v1/traces/{trace_id}",
            params={"tenant_id": str(CLINIC_TENANT_ID)},
            headers=_auth_headers(token),
        )
        assert response.status_code == 403

    def test_builder_session_events_unowned_clinic_returns_403(
        self, client: TestClient
    ) -> None:
        token = _make_builder_jwt(BUILDER_TENANT_ID, BUILDER_ID)
        session_id = uuid4()
        response = client.get(
            f"/v1/sessions/{session_id}/events",
            params={"tenant_id": str(CLINIC_TENANT_ID)},
            headers=_auth_headers(token),
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "tenant_override_forbidden"

    def test_builder_sessions_list_unowned_clinic_returns_403(
        self, client: TestClient
    ) -> None:
        token = _make_builder_jwt(BUILDER_TENANT_ID, BUILDER_ID)
        response = client.get(
            "/v1/sessions",
            params={"tenant_id": str(CLINIC_TENANT_ID)},
            headers=_auth_headers(token),
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "tenant_override_forbidden"


# ============================================
# Tests: Builder using own tenant_id (no override)
# ============================================


class TestBuilderOwnTenant:
    """Builder using their own tenant_id should work normally (no override triggered)."""

    def test_builder_traces_own_tenant_no_override(self, client: TestClient) -> None:
        token = _make_builder_jwt(BUILDER_TENANT_ID, BUILDER_ID)
        response = client.get(
            "/v1/traces",
            headers=_auth_headers(token),
        )
        # No tenant_id override → uses JWT tenant_id → 200 (empty list is fine)
        assert response.status_code == 200

    def test_builder_sessions_own_tenant_no_override(self, client: TestClient) -> None:
        token = _make_builder_jwt(BUILDER_TENANT_ID, BUILDER_ID)
        response = client.get(
            "/v1/sessions",
            params={"tenant_id": str(BUILDER_TENANT_ID)},
            headers=_auth_headers(token),
        )
        assert response.status_code == 200


# ============================================
# Tests: Session creation with builder override
# ============================================


class TestBuilderSessionCreation:
    """Builder creating sessions for clinics they don't manage gets 403."""

    def test_builder_create_session_unowned_clinic_returns_403(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = get_session_service()
        create_spy = AsyncMock(wraps=service.create_session)
        monkeypatch.setattr(service, "create_session", create_spy)
        token = _make_builder_jwt(BUILDER_TENANT_ID, BUILDER_ID)
        payload = {
            "tenant_id": str(CLINIC_TENANT_ID),
            "channel": "whatsapp",
        }
        response = client.post(
            "/v1/sessions",
            json=payload,
            headers=_auth_headers(token),
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "tenant_override_forbidden"
        create_spy.assert_not_awaited()

    def test_builder_create_session_own_tenant_succeeds(
        self, client: TestClient
    ) -> None:
        """Builder creating session for their own tenant_id should not
        trigger the override path (tenant_id matches JWT)."""
        token = _make_builder_jwt(BUILDER_TENANT_ID, BUILDER_ID)
        payload = {
            "tenant_id": str(BUILDER_TENANT_ID),
            "channel": "whatsapp",
        }
        response = client.post(
            "/v1/sessions",
            json=payload,
            headers=_auth_headers(token),
        )
        # 404 because BUILDER_TENANT_ID doesn't exist in the test DB,
        # but that's the service layer — the auth check passed (no 403).
        assert response.status_code in (201, 404)
        assert response.status_code != 403


# ============================================
# Tests: Audit logging
# ============================================


class TestAuditLogging:
    """Verify that builder_clinic_override is logged."""

    def test_builder_override_logs_audit(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The _resolve_tenant helper should emit an audit log before
        the ownership check. Since the clinic doesn't exist in the test
        DB, we'll get a 403 — but the log should still fire because
        logging happens before the SQL check fails."""
        token = _make_builder_jwt(BUILDER_TENANT_ID, BUILDER_ID)
        with caplog.at_level("INFO"):
            client.get(
                "/v1/traces",
                params={"tenant_id": str(CLINIC_TENANT_ID)},
                headers=_auth_headers(token),
            )
        # Check that audit log was emitted
        audit_messages = [
            r
            for r in caplog.records
            if "builder_clinic_override" in str(r.getMessage())
        ]
        assert len(audit_messages) >= 1
