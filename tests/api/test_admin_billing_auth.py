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
        scope="rollout",
        operation_id=uuid4(),
        environment="test",
        from_state="disabled",
        to_state="canary_allowlist",
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


def test_every_billing_admin_route_requires_jwt() -> None:
    from src.api.routes.admin import router

    billing_routes = {
        route.path: route
        for route in router.routes
        if getattr(route, "path", "").startswith(("/admin/billing/", "/billing/"))
    }
    # Non-vacuous: the signed control plane is enumerated.
    assert {
        "/billing/operations/assignments",
        "/billing/operations/receipts",
        "/billing/rollout/transitions",
        "/billing/rollout/receipts",
        "/billing/topup-capability/enable",
    } <= set(billing_routes)
    for path, route in billing_routes.items():
        dependencies = {
            dependency.call for dependency in route.dependant.dependencies  # type: ignore[attr-defined]
        }
        assert require_super_admin_jwt in dependencies, path


@pytest.mark.parametrize(
    "constraint",
    [
        "uq_billing_operation_assignments_actor",
        "uq_billing_rollout_readiness_revision",
    ],
)
def test_billing_integrity_errors_map_to_the_domain_conflict_code(
    constraint: str,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from src.api.routes.admin import _billing_http_error

    exc = IntegrityError(
        "INSERT INTO billing_table ...",
        {},
        Exception(f'duplicate key value violates unique constraint "{constraint}"'),
    )
    # SQLAlchemy's own documentation key; it must never become the detail.
    assert isinstance(exc.code, str) and not exc.code.startswith("billing_")

    error = _billing_http_error(exc)

    assert (error.status_code, error.detail) == (409, "billing_operation_conflict")


def test_billing_domain_rejection_codes_pass_through_unchanged() -> None:
    from src.api.routes.admin import _billing_http_error
    from src.services.billing_operations import BillingOperationRejected

    error = _billing_http_error(
        BillingOperationRejected("billing_rollout_from_state_mismatch")
    )

    assert (error.status_code, error.detail) == (
        409,
        "billing_rollout_from_state_mismatch",
    )
