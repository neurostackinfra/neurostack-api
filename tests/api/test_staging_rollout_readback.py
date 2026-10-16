from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from scripts.staging_rehearsal_common import (
    STAGING_API_ORIGIN,
    STAGING_ROLLOUT_READBACK_PATH,
    RehearsalError,
    staging_https_json,
    validate_backend_url,
)
from src.api.middleware.auth import TenantContext, require_super_admin_jwt
from src.api.routes import admin
from src.api.schemas.admin import BillingRolloutControlResponse
from src.config.settings import settings
from src.repositories.billing import BillingStateUnavailable


def _tenant() -> TenantContext:
    return TenantContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        role="super_admin",
        authentication_mode="jwt",
    )


@pytest.mark.asyncio
async def test_staging_readback_returns_persisted_closed_gate_without_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    control = SimpleNamespace(
        environment="staging",
        state="disabled",
        rollout_revision=3,
        topup_commercial_enabled=False,
        topup_revision=2,
    )

    class Repository:
        def __init__(self, session: object) -> None:
            assert session is sentinel

        async def lock_rollout_control(self, *, environment: str) -> object:
            assert environment == "staging"
            return control

    sentinel = object()
    monkeypatch.setattr(admin, "BillingRepository", Repository)
    response = Response()

    result = await admin.read_staging_billing_rollout(
        response=response,
        tenant=_tenant(),
        session=sentinel,  # type: ignore[arg-type]
    )

    assert result == BillingRolloutControlResponse(
        environment="staging",
        state="disabled",
        rollout_revision=3,
        topup_commercial_enabled=False,
        topup_revision=2,
    )
    assert result.model_dump() == {
        "environment": "staging",
        "state": "disabled",
        "rollout_revision": 3,
        "topup_commercial_enabled": False,
        "topup_revision": 2,
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_staging_readback_fails_closed_when_persisted_control_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")

    class Repository:
        def __init__(self, session: object) -> None:
            del session

        async def lock_rollout_control(self, *, environment: str) -> object:
            assert environment == "staging"
            raise BillingStateUnavailable("missing control")

    monkeypatch.setattr(admin, "BillingRepository", Repository)

    with pytest.raises(HTTPException) as captured:
        await admin.read_staging_billing_rollout(
            response=Response(),
            tenant=_tenant(),
            session=object(),  # type: ignore[arg-type]
        )

    assert captured.value.status_code == 503
    assert captured.value.detail == "billing_rollout_readback_unavailable"


@pytest.mark.asyncio
async def test_staging_readback_is_not_available_outside_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "production")

    class Repository:
        def __init__(self, session: object) -> None:
            del session

        async def lock_rollout_control(self, *, environment: str) -> object:
            pytest.fail(f"unexpected persisted read for {environment}")

    monkeypatch.setattr(admin, "BillingRepository", Repository)

    with pytest.raises(HTTPException) as captured:
        await admin.read_staging_billing_rollout(
            response=Response(),
            tenant=_tenant(),
            session=object(),  # type: ignore[arg-type]
        )

    assert captured.value.status_code == 404


def test_readback_route_is_mounted_as_get_under_admin() -> None:
    app = FastAPI()
    app.include_router(admin.router, prefix="/v1/admin")

    route = next(
        route
        for route in admin.router.routes
        if route.path == "/billing/rollout/readback"
    )
    paths = app.openapi()["paths"]
    assert "/v1/admin/billing/rollout/readback" in paths
    assert set(paths["/v1/admin/billing/rollout/readback"]) == {"get"}
    assert route.methods == {"GET"}
    assert require_super_admin_jwt in {
        dependency.call for dependency in route.dependant.dependencies
    }


def test_staging_cli_allows_only_the_mounted_readback_endpoint() -> None:
    validate_backend_url(f"{STAGING_API_ORIGIN}{STAGING_ROLLOUT_READBACK_PATH}")

    with pytest.raises(RehearsalError, match="outside the allowlist"):
        staging_https_json(
            path="/v1/admin/billing/authorization/consume",
            method="POST",
            jwt_token="placeholder",
            payload={},
        )
