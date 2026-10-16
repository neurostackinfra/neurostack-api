"""JWT-only boundary tests for Group 5 administrative billing routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.api.middleware.auth import TenantContext, require_super_admin_jwt
from src.api.routes.admin import create_billing_operation_assignment
from src.api.schemas.admin import BillingOperationAssignmentRequest


def _request_with(context: TenantContext) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/admin/billing/operations/receipts",
            "headers": [],
            "scheme": "https",
            "server": ("testserver", 443),
        }
    )
    request.state.tenant = context
    return request


def test_billing_admin_rejects_super_admin_api_key() -> None:
    request = _request_with(
        TenantContext(
            tenant_id=uuid4(),
            user_id=uuid4(),
            role="super_admin",
            api_key="not-logged-or-returned",
            authentication_mode="api_key",
        )
    )

    with pytest.raises(HTTPException) as captured:
        require_super_admin_jwt(request)

    assert captured.value.status_code == 403
    assert captured.value.detail == {"error": "billing_super_admin_jwt_required"}
    assert "not-logged-or-returned" not in str(captured.value.detail)


def test_billing_admin_rejects_non_jwt_context_even_with_an_actor() -> None:
    request = _request_with(
        TenantContext(
            tenant_id=uuid4(),
            user_id=uuid4(),
            role="super_admin",
            authentication_mode="meta_hmac",
        )
    )

    with pytest.raises(HTTPException, match="billing_super_admin_jwt_required"):
        require_super_admin_jwt(request)


def test_billing_admin_accepts_super_admin_jwt_context() -> None:
    actor = uuid4()
    request = _request_with(
        TenantContext(
            tenant_id=uuid4(),
            user_id=actor,
            role="super_admin",
            authentication_mode="jwt",
        )
    )

    assert require_super_admin_jwt(request).user_id == actor


def test_assignment_actor_must_match_the_authenticated_jwt_subject() -> None:
    actor = uuid4()
    body = BillingOperationAssignmentRequest(
        scope="migration",
        operation_id=uuid4(),
        environment="test",
        from_state="pending",
        to_state="in_progress",
        budget_digest="a" * 64,
        deployment_digest="b" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        role="deployer",
        actor_user_id=uuid4(),
    )
    tenant = TenantContext(
        tenant_id=uuid4(),
        user_id=actor,
        role="super_admin",
        authentication_mode="jwt",
    )

    with pytest.raises(HTTPException, match="billing_operation_actor_mismatch"):
        asyncio.run(
            create_billing_operation_assignment(
                body=body,
                tenant=tenant,
                session=None,  # type: ignore[arg-type]  # Boundary fails before DB use.
            )
        )


def test_every_billing_admin_route_including_absence_reads_requires_jwt() -> None:
    from src.api.routes.admin import router

    billing_routes = {
        route.path: route
        for route in router.routes
        if getattr(route, "path", "").startswith(("/admin/billing/", "/billing/"))
    }
    assert any(
        path.endswith("/billing/migrations/absence-reads") for path in billing_routes
    )
    for path, route in billing_routes.items():
        dependencies = {
            dependency.call for dependency in route.dependant.dependencies  # type: ignore[attr-defined]
        }
        assert require_super_admin_jwt in dependencies, path


def test_provider_flip_takes_the_asaas_id_only_from_the_signed_envelope() -> None:
    from pydantic import ValidationError

    from src.api.schemas.admin import (
        BillingMigrationProviderFlipRequest,
        BillingProviderEvidenceRequest,
    )

    assert (
        "asaas_subscription_id" not in BillingMigrationProviderFlipRequest.model_fields
    )
    assert BillingMigrationProviderFlipRequest.model_fields["evidence"].is_required()
    with pytest.raises(ValidationError):
        BillingProviderEvidenceRequest.model_validate(
            {
                "external_reference": "sub_1",
                "provider_event_id": "evt_1",
                "provider_key_id": "key-1",
                "provider_operation": "subscription.migration.provider_flip",
                "provider_signature": "a" * 128,
                "provider_observed_at": "2026-10-12T20:00:00+00:00",
                "provider_expires_at": "2026-10-12T20:15:00+00:00",
                "provider_snapshot_digest": "b" * 64,
                "provider_evidence_digest": "c" * 64,
                "asaas_subscription_id": "sub_caller_asserted",
            }
        )
