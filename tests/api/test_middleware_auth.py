"""
NeuroStack API - Authentication Middleware Tests
"""

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from httpx import AsyncClient
from starlette.responses import RedirectResponse

from src.api.middleware.auth import (
    APIKeyStore,
    TenantContext,
    require_credentials_jwt,
    resolve_effective_tenant,
)
from tests.conftest import LifespanTestClient


@pytest.fixture(autouse=True)
def scoped_session_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticated event probes operate on an existing tenant session."""

    from src.models.session import Session
    from src.repositories.session import SessionRepository

    async def get_by_tenant_for_update(
        _repo: SessionRepository, session_id: UUID, tenant_id: UUID
    ) -> Session:
        return Session(
            id=session_id,
            tenant_id=tenant_id,
            channel="test",
            cognitive_state={},
            session_metadata={},
            message_count=0,
        )

    monkeypatch.setattr(
        SessionRepository, "get_by_tenant_for_update", get_by_tenant_for_update
    )


def _request_with_tenant(tenant: TenantContext) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )
    request.state.tenant = tenant
    return request


@asynccontextmanager
async def _auth_session() -> AsyncIterator[AsyncMock]:
    yield AsyncMock()


_INVALID_API_KEY_BODY = {
    "error": "unauthorized",
    "message": "Invalid API key",
    "documentation_url": "https://docs.neurostack.ai/auth",
}


def _guard_tenant_scan(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    """Record any tenant lookup or bcrypt verify an API key would trigger.

    ``verify`` is patched on ``CryptContext`` itself so the guard catches a
    hash check through any context instance, not only a module-level one.
    A working session is provided so a scan, if one existed, would reach
    ``find_active`` instead of failing early on an uninitialised database.
    """
    from passlib.context import CryptContext

    from src.api.middleware import auth
    from src.repositories.tenant import TenantRepository

    tenants = [MagicMock(id=uuid4(), api_key_hash="$2b$12$x") for _ in range(3)]
    find_active = AsyncMock(return_value=tenants)
    verify = MagicMock(return_value=False)
    monkeypatch.setattr(auth, "get_db_session", _auth_session, raising=False)
    monkeypatch.setattr(TenantRepository, "find_active", find_active)
    monkeypatch.setattr(CryptContext, "verify", verify)
    return find_active, verify


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "api_key",
    ["bogus-key", "ns_dev_anything", "ns_dev_test_key_12345", "test-api-key-12345"],
)
def test_api_key_rejected_without_tenant_scan_outside_development_and_test(
    monkeypatch: pytest.MonkeyPatch, environment: str, api_key: str
) -> None:
    """Deployed environments reject API keys before any DB read or bcrypt."""
    from src.api.middleware import auth

    find_active, verify = _guard_tenant_scan(monkeypatch)
    monkeypatch.setattr(auth.settings, "environment", environment)

    assert asyncio.run(APIKeyStore.validate_api_key(api_key)) is None
    assert find_active.await_count == 0
    assert verify.call_count == 0


@pytest.mark.parametrize("environment", ["development", "test"])
def test_dev_keys_accepted_in_development_and_test(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    from src.api.middleware import auth

    find_active, verify = _guard_tenant_scan(monkeypatch)
    monkeypatch.setattr(auth.settings, "environment", environment)

    result = asyncio.run(APIKeyStore.validate_api_key("ns_dev_anything"))

    assert result is not None
    assert result.tenant_id == UUID("a0000000-0000-0000-0000-000000000001")
    assert result.api_key == "ns_dev_anything"
    assert find_active.await_count == 0
    assert verify.call_count == 0


@pytest.mark.parametrize("environment", ["development", "test"])
def test_unknown_api_key_rejected_without_tenant_scan_in_development_and_test(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    from src.api.middleware import auth

    find_active, verify = _guard_tenant_scan(monkeypatch)
    monkeypatch.setattr(auth.settings, "environment", environment)

    assert asyncio.run(APIKeyStore.validate_api_key("bogus-key")) is None
    assert find_active.await_count == 0
    assert verify.call_count == 0


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_bogus_api_key_request_is_401_without_tenant_scan(
    client: TestClient,
    sample_event_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    """An unauthenticated X-API-Key request costs no DB read and no bcrypt."""
    from src.api.middleware import auth

    find_active, verify = _guard_tenant_scan(monkeypatch)
    monkeypatch.setattr(auth.settings, "environment", environment)

    response = client.post(
        "/v1/events",
        json=sample_event_payload,
        headers={"Content-Type": "application/json", "X-API-Key": "bogus-key"},
    )

    assert response.status_code == 401
    assert response.json() == _INVALID_API_KEY_BODY
    assert find_active.await_count == 0
    assert verify.call_count == 0


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


@pytest.mark.parametrize("role", ["investor", "unknown", None])
def test_tenant_resolver_denies_unlisted_jwt_roles_before_db(
    role: str | None,
) -> None:
    tenant = TenantContext(tenant_id=uuid4(), user_id=uuid4(), role=role)
    db = AsyncMock()

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            resolve_effective_tenant(
                _request_with_tenant(tenant),
                tenant,
                tenant.tenant_id,
                db,
            )
        )

    assert error.value.status_code == 403
    assert cast(Any, error.value.detail) == {"code": "caller_role_forbidden"}
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("role", [None, "clinic_admin"])
def test_tenant_resolver_prevents_tenant_scoped_override(
    role: str | None,
) -> None:
    own_tenant = uuid4()
    tenant = TenantContext(
        tenant_id=own_tenant,
        api_key="key" if role is None else "",
        user_id=uuid4() if role else None,
        role=role,
    )
    db = AsyncMock()

    assert (
        asyncio.run(
            resolve_effective_tenant(
                _request_with_tenant(tenant), tenant, own_tenant, db
            )
        )
        == own_tenant
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            resolve_effective_tenant(_request_with_tenant(tenant), tenant, uuid4(), db)
        )
    assert error.value.status_code == 403
    assert cast(Any, error.value.detail) == {"code": "tenant_override_forbidden"}
    db.execute.assert_not_awaited()


def test_tenant_resolver_allows_super_admin_selection() -> None:
    tenant = TenantContext(tenant_id=uuid4(), user_id=uuid4(), role="super_admin")
    selected = uuid4()
    db = AsyncMock()
    assert (
        asyncio.run(
            resolve_effective_tenant(_request_with_tenant(tenant), tenant, selected, db)
        )
        == selected
    )
    db.execute.assert_not_awaited()


def test_tenant_resolver_requires_builder_ownership() -> None:
    own_tenant = uuid4()
    selected = uuid4()
    builder_id = uuid4()
    tenant = TenantContext(
        tenant_id=own_tenant,
        user_id=uuid4(),
        role="builder",
        builder_id=builder_id,
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = selected
    db.execute.return_value = result

    assert (
        asyncio.run(
            resolve_effective_tenant(_request_with_tenant(tenant), tenant, selected, db)
        )
        == selected
    )
    db.execute.assert_awaited_once()


def test_tenant_resolver_denies_builder_without_builder_claim_before_db() -> None:
    """A builder token without ``builder_id`` never reaches the ownership query.

    That query compares ``Tenant.builder_id`` with the claim; with no claim it
    compiles to ``IS NULL`` and matches every clinic that has no builder. The
    mocked query answers like that, so only the claim guard can deny.
    """
    own_tenant = uuid4()
    builderless_clinic = uuid4()
    tenant = TenantContext(
        tenant_id=own_tenant,
        user_id=uuid4(),
        role="builder",
        builder_id=None,
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = builderless_clinic
    db.execute.return_value = result

    assert (
        asyncio.run(
            resolve_effective_tenant(
                _request_with_tenant(tenant), tenant, own_tenant, db
            )
        )
        == own_tenant
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            resolve_effective_tenant(
                _request_with_tenant(tenant), tenant, builderless_clinic, db
            )
        )

    assert error.value.status_code == 403
    assert cast(Any, error.value.detail) == {"code": "tenant_override_forbidden"}
    db.execute.assert_not_awaited()


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
        assert facade._loop is not None
        assert facade._loop.is_closed()
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


def test_sync_client_restores_registered_current_loop_after_lifespan_error() -> None:
    @asynccontextmanager
    async def failing_lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        raise RuntimeError("shutdown failed")

    previous = asyncio.new_event_loop()
    asyncio.set_event_loop(previous)
    facade = LifespanTestClient(FastAPI(lifespan=failing_lifespan))
    try:
        with pytest.raises(RuntimeError, match="shutdown failed"):
            with facade:
                pass
        assert asyncio.get_event_loop() is previous
        assert facade._loop is not None
        assert facade._loop.is_closed()
    finally:
        asyncio.set_event_loop(None)
        previous.close()


def test_sync_client_preserves_absent_current_loop_after_lifespan_error() -> None:
    @asynccontextmanager
    async def failing_lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        raise RuntimeError("shutdown failed")

    try:
        original: asyncio.AbstractEventLoop | None = asyncio.get_event_loop()
    except RuntimeError:
        original = None
    asyncio.set_event_loop(None)
    facade = LifespanTestClient(FastAPI(lifespan=failing_lifespan))
    try:
        with pytest.raises(RuntimeError, match="shutdown failed"):
            with facade:
                pass
        with pytest.raises(RuntimeError, match="no current event loop"):
            asyncio.get_event_loop()
        assert facade._loop is not None
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
        assert response.json() == _INVALID_API_KEY_BODY

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

    def test_event_without_tenant_assertion_uses_authenticated_tenant(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """The deprecated body tenant is optional and never routing authority."""

        payload = dict(sample_event_payload)
        payload.pop("tenant_id")
        response = client.post("/v1/events", json=payload, headers=api_headers)

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
        known_keys = {
            "ns_dev_test_key_12345": "a0000000-0000-0000-0000-000000000001",
            "ns_dev_builder_key_67890": "a0000000-0000-0000-0000-000000000002",
            "test-api-key": "a0000000-0000-0000-0000-000000000001",
        }

        for key, tenant_id in known_keys.items():
            response = client.post(
                "/v1/events",
                json={**sample_event_payload, "tenant_id": tenant_id},
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
