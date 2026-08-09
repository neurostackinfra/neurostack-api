"""
NeuroStack API - Authentication Middleware Tests
"""

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from httpx import AsyncClient
from starlette.responses import RedirectResponse

from src.api.middleware.auth import TenantContext, require_credentials_jwt
from tests.conftest import LifespanTestClient


def _request_with_tenant(tenant: TenantContext) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/"})
    request.state.tenant = tenant
    return request


@pytest.mark.parametrize("role", ["clinic_admin", "builder", "super_admin"])
def test_credentials_dependency_accepts_only_allowed_jwt_roles(role: str) -> None:
    tenant = TenantContext(tenant_id=uuid4(), user_id=uuid4(), role=role, api_key="")
    assert require_credentials_jwt(_request_with_tenant(tenant)) is tenant


def test_credentials_dependency_rejects_api_key_and_forbidden_role() -> None:
    api_key_tenant = TenantContext(tenant_id=uuid4(), api_key="test-key")
    with pytest.raises(HTTPException) as api_key_error:
        require_credentials_jwt(_request_with_tenant(api_key_tenant))
    assert cast(Any, api_key_error.value.detail) == {
        "error": "credentials_jwt_required"
    }

    investor = TenantContext(
        tenant_id=uuid4(), user_id=uuid4(), role="investor", api_key=""
    )
    with pytest.raises(HTTPException) as role_error:
        require_credentials_jwt(_request_with_tenant(investor))
    assert cast(Any, role_error.value.detail) == {"error": "credentials_role_forbidden"}


def test_sync_client_runs_application_lifespan(client: Any) -> None:
    assert client.lifespan_started is True
    assert client.get("/health").status_code == 200


def test_sync_client_matches_redirect_scope_lifespan_and_loop_cleanup() -> None:
    events: list[str] = []
    ticks: list[int] = []

    async def ticker() -> None:
        while True:
            ticks.append(len(ticks))
            await asyncio.sleep(0.005)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        events.append("startup")
        task = asyncio.create_task(ticker(), name="managed-application-task")
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            events.append("shutdown")

    application = FastAPI(lifespan=lifespan)

    @application.get("/scope")
    async def scope(request: Request) -> dict[str, Any]:
        return {
            "client": (
                [request.client.host, request.client.port] if request.client else None
            )
        }

    @application.get("/redirect")
    async def redirect() -> RedirectResponse:
        return RedirectResponse("/scope")

    facade = LifespanTestClient(application)
    with facade:
        assert facade.lifespan_started is True
        before = len(ticks)
        time.sleep(0.03)
        assert len(ticks) > before
        response = facade.get("/redirect")
        assert response.status_code == 200
        assert response.history[0].status_code == 307
        assert response.json() == {"client": ["testclient", 50000]}

    assert events == ["startup", "shutdown"]
    assert facade.lifespan_started is False
    assert not facade._thread.is_alive()
    assert facade._loop.is_closed()


def test_sync_client_fails_and_cleans_up_application_task_leaks() -> None:
    application = FastAPI()

    async def never_finishes() -> None:
        await asyncio.Event().wait()

    @application.get("/leak")
    async def leak() -> dict[str, bool]:
        asyncio.create_task(never_finishes(), name="deliberate-application-leak")
        return {"ok": True}

    facade = LifespanTestClient(application)
    with pytest.raises(AssertionError, match="deliberate-application-leak"):
        with facade:
            assert facade.get("/leak").status_code == 200
    assert not facade._thread.is_alive()
    assert facade._loop.is_closed()


def test_sync_client_restores_registered_current_loop() -> None:
    previous = asyncio.new_event_loop()
    asyncio.set_event_loop(previous)
    try:
        facade = LifespanTestClient(FastAPI())
        with facade:
            assert facade.get("/openapi.json").status_code == 200
        assert asyncio.get_event_loop() is previous
        with pytest.raises(RuntimeError, match="closed"):
            facade._runner.get_loop()
    finally:
        asyncio.set_event_loop(None)
        previous.close()


def test_sync_client_clears_current_loop_and_joins_default_executor_thread() -> None:
    try:
        original: asyncio.AbstractEventLoop | None = asyncio.get_event_loop()
    except RuntimeError:
        original = None
    asyncio.set_event_loop(None)
    executor_threads: list[Any] = []
    application = FastAPI()

    def record_executor_thread() -> None:
        executor_threads.append(threading.current_thread())

    @application.get("/executor")
    async def executor() -> dict[str, bool]:
        await asyncio.get_running_loop().run_in_executor(None, record_executor_thread)
        return {"ok": True}

    try:
        facade = LifespanTestClient(application)
        with facade:
            assert facade.get("/executor").json() == {"ok": True}
            assert executor_threads[0].is_alive()
        with pytest.raises(RuntimeError, match="no current event loop"):
            asyncio.get_event_loop()
        assert not executor_threads[0].is_alive()
        assert not facade._thread.is_alive()
        assert facade._loop.is_closed()
    finally:
        asyncio.set_event_loop(original)


class TestAuthMiddleware:
    """Test suite for authentication middleware."""

    def test_request_without_api_key_returns_401(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
    ) -> None:
        """Test that requests without API key return 401 Unauthorized."""
        response = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 401
        data = response.json()
        assert data["error"] == "unauthorized"
        assert "X-API-Key" in data["message"]

    def test_request_with_invalid_api_key_returns_401(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
    ) -> None:
        """Test that requests with invalid API key return 401 Unauthorized."""
        response = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": "invalid-key-12345",
            },
        )

        assert response.status_code == 401
        data = response.json()
        assert data["error"] == "unauthorized"
        assert "Invalid API key" in data["message"]

    def test_request_with_valid_api_key_returns_200(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that requests with valid API key are processed."""
        response = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=api_headers,
        )

        assert response.status_code == 200

    def test_health_endpoint_no_auth_required(
        self,
        client: TestClient,
    ) -> None:
        """Test that health endpoints don't require authentication."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_ready_endpoint_no_auth_required(
        self,
        client: TestClient,
    ) -> None:
        """Test that ready endpoint doesn't require authentication."""
        response = client.get("/ready")
        assert response.status_code == 200

    def test_live_endpoint_no_auth_required(
        self,
        client: TestClient,
    ) -> None:
        """Test that live endpoint doesn't require authentication."""
        response = client.get("/live")
        assert response.status_code == 200

    def test_dev_api_key_accepted_in_development(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
    ) -> None:
        """Test that ns_dev_ prefixed keys work in development."""
        response = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": "ns_dev_custom_key_for_testing",
            },
        )

        # Should work in development mode
        assert response.status_code == 200

    def test_known_dev_keys_accepted(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
    ) -> None:
        """Test that known development keys are accepted."""
        known_keys = [
            "ns_dev_test_key_12345",
            "ns_dev_builder_key_67890",
            "test-api-key",
        ]

        for key in known_keys:
            response = client.post(
                "/v1/events",
                json=sample_event_payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": key,
                },
            )
            assert response.status_code == 200, f"Key {key} should be accepted"


class TestAuthMiddlewareAsync:
    """Async test suite for authentication middleware."""

    @pytest.mark.asyncio
    async def test_async_request_without_api_key(
        self,
        async_client: AsyncClient,
        sample_event_payload: dict[str, Any],
    ) -> None:
        """Test async request without API key returns 401."""
        response = await async_client.post(
            "/v1/events",
            json=sample_event_payload,
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_async_request_with_valid_api_key(
        self,
        async_client: AsyncClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test async request with valid API key is processed."""
        response = await async_client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=api_headers,
        )

        assert response.status_code == 200
