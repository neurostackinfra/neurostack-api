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


# ---------------------------------------------------------------------------
# Group 10 E3: readiness request/response lose ``roster_revision`` (closed)
# ---------------------------------------------------------------------------


def _readiness_body() -> dict[str, object]:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    return {
        "scope": "rollout",
        "operation_id": str(uuid4()),
        "environment": "test",
        "from_state": "canary_allowlist",
        "to_state": "general",
        "budget_digest": "a" * 64,
        "deployment_digest": "b" * 64,
        "expires_at": expires_at.isoformat(),
        "receipt_kind": "readiness",
        "rollout_revision": 3,
        "status": "passed",
        "content_sha256": "c" * 64,
        "operation_receipt_id": str(uuid4()),
        "provider_receipt_id": str(uuid4()),
        "rule_digest": "d" * 64,
        "bundle_digest": "e" * 64,
        "app_digest": "b" * 64,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def test_readiness_request_and_response_are_closed_without_roster_revision() -> None:
    from pydantic import ValidationError

    from src.api.schemas.admin import (
        BillingRolloutReadinessReceiptRequest,
        BillingRolloutReadinessReceiptResponse,
    )

    for model in (
        BillingRolloutReadinessReceiptRequest,
        BillingRolloutReadinessReceiptResponse,
    ):
        assert "roster_revision" not in model.model_fields
        assert model.model_config.get("extra") == "forbid"
    body = BillingRolloutReadinessReceiptRequest.model_validate(_readiness_body())
    response = BillingRolloutReadinessReceiptResponse(
        **body.model_dump(), receipt_id=uuid4()
    )
    assert "roster_revision" not in response.model_dump()

    for extra in ({"roster_revision": 0}, {"roster_revision": 7}, {"unknown": 1}):
        with pytest.raises(ValidationError) as captured:
            BillingRolloutReadinessReceiptRequest.model_validate(
                {**_readiness_body(), **extra}
            )
        assert [(e["loc"], e["type"]) for e in captured.value.errors()] == [
            ((next(iter(extra)),), "extra_forbidden")
        ]


async def test_readiness_route_refuses_roster_revision_and_never_forwards_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from typing import AsyncIterator

    import httpx
    from fastapi import FastAPI
    from pydantic import SecretStr

    from src.api.routes import admin as admin_route
    from src.config.settings import settings
    from src.repositories.database import get_db_session_dependency

    calls: list[dict[str, object]] = []
    receipt_id = uuid4()
    actor = uuid4()

    class _RecordingService:
        def __init__(self, session: object, *, signing_key: bytes) -> None:
            del session, signing_key

        async def record_readiness_receipt(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(id=receipt_id)

    class _Session:
        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    async def _session() -> AsyncIterator[object]:
        yield _Session()

    monkeypatch.setattr(
        admin_route, "BillingRolloutOperationService", _RecordingService
    )
    monkeypatch.setattr(
        settings, "billing_operation_receipt_signing_key", SecretStr("k" * 32)
    )
    app = FastAPI()
    app.include_router(admin_route.router, prefix="/v1/admin")
    app.dependency_overrides[require_super_admin_jwt] = lambda: TenantContext(
        tenant_id=uuid4(),
        user_id=actor,
        role="super_admin",
        authentication_mode="jwt",
    )
    app.dependency_overrides[get_db_session_dependency] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://admin.test"
    ) as client:
        for value in (0, 1):
            refused = await client.post(
                "/v1/admin/billing/rollout/receipts",
                json={**_readiness_body(), "roster_revision": value},
            )
            assert refused.status_code == 422, refused.text
            assert [
                (error["loc"], error["type"]) for error in refused.json()["detail"]
            ] == [(["body", "roster_revision"], "extra_forbidden")]
        assert calls == []

        accepted = await client.post(
            "/v1/admin/billing/rollout/receipts", json=_readiness_body()
        )
    assert accepted.status_code == 201, accepted.text
    assert "roster_revision" not in accepted.json()
    assert accepted.json()["receipt_id"] == str(receipt_id)
    assert len(calls) == 1
    assert set(calls[0]) == {
        "environment",
        "receipt_kind",
        "rollout_revision",
        "status",
        "content_sha256",
        "material",
        "operation_receipt_id",
        "provider_receipt_id",
        "rule_digest",
        "bundle_digest",
        "app_digest",
        "observed_at",
        "expires_at",
        "recorder_user_id",
    }
    assert calls[0]["recorder_user_id"] == actor
