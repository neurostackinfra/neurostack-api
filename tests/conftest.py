"""
NeuroStack API - Pytest Configuration and Fixtures
"""

import asyncio
import threading
from collections.abc import AsyncGenerator, Generator
from contextlib import suppress
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.main import app


async def _keep_event_loop_responsive() -> None:
    """Prevent lost thread-safe wakeups from stalling aiosqlite in tests."""
    while True:
        await asyncio.sleep(0.01)


class LifespanTestClient:
    """Synchronous facade with a continuously running application event loop."""

    def __init__(self, application: Any) -> None:
        self.application = application
        self.lifespan_started = False
        policy_local = getattr(asyncio.get_event_loop_policy(), "_local", None)
        self._previous_loop: asyncio.AbstractEventLoop | None = getattr(
            policy_local, "_loop", None
        )
        self._runner = asyncio.Runner()
        self._loop = self._runner.get_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="lifespan-test-client",
            daemon=True,
        )
        self._client: AsyncClient | None = None
        self._lifespan: Any = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._baseline_tasks: set[asyncio.Task[Any]] = set()

    async def _start(self) -> None:
        self._baseline_tasks = set(asyncio.all_tasks())
        self._heartbeat = asyncio.create_task(_keep_event_loop_responsive())
        self._lifespan = self.application.router.lifespan_context(self.application)
        await self._lifespan.__aenter__()
        transport = ASGITransport(
            app=self.application,
            client=("testclient", 50000),
        )
        self._client = AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=True,
        )
        await self._client.__aenter__()
        self.lifespan_started = True

    async def _stop(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
        if self._lifespan is not None:
            await self._lifespan.__aexit__(None, None, None)
        current = asyncio.current_task()
        leaked_tasks = [
            task
            for task in asyncio.all_tasks()
            if task not in self._baseline_tasks
            and task is not current
            and task is not self._heartbeat
            and not task.done()
        ]
        for task in leaked_tasks:
            task.cancel()
        if leaked_tasks:
            await asyncio.gather(*leaked_tasks, return_exceptions=True)
        loop = asyncio.get_running_loop()
        try:
            await loop.shutdown_asyncgens()
            await loop.shutdown_default_executor()
        finally:
            if self._heartbeat is not None:
                self._heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await self._heartbeat
        self.lifespan_started = False
        if leaked_tasks:
            leaked = ", ".join(sorted(task.get_name() for task in leaked_tasks))
            raise AssertionError(f"application task leak: {leaked}")

    def _submit(self, coroutine: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def __enter__(self) -> "LifespanTestClient":
        try:
            self._runner.run(self._start())
        except BaseException:
            try:
                self._runner.close()
            finally:
                self._restore_current_loop()
            raise
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        cleanup_error: BaseException | None = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        try:
            self._runner.run(self._stop())
        except BaseException as error:
            cleanup_error = error
        finally:
            try:
                self._runner.close()
            finally:
                self._restore_current_loop()
        if cleanup_error is not None:
            raise cleanup_error

    def _restore_current_loop(self) -> None:
        if self._previous_loop is not None and not self._previous_loop.is_closed():
            asyncio.set_event_loop(self._previous_loop)
        else:
            asyncio.set_event_loop(None)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        if self._client is None:
            raise RuntimeError("Test client has not been started")
        return self._submit(self._client.request(method, url, **kwargs))

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> Any:
        return self.request("OPTIONS", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Any:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.request("DELETE", url, **kwargs)


# ============================================
# Event Loop Configuration
# ============================================


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================
# Application Fixtures
# ============================================


@pytest.fixture(scope="session")
def test_app() -> Any:
    """Get the test application instance."""
    return app


@pytest.fixture
def client(test_app: Any) -> Generator[LifespanTestClient, None, None]:
    """Create a synchronous client with explicit application lifespan."""
    with LifespanTestClient(test_app) as c:
        yield c


@pytest.fixture
async def async_client(test_app: Any) -> AsyncGenerator[AsyncClient, None]:
    """Create an async client while running the real application lifespan."""
    heartbeat = asyncio.create_task(_keep_event_loop_responsive())
    try:
        async with test_app.router.lifespan_context(test_app):
            transport = ASGITransport(
                app=test_app,
                client=("testclient", 50000),
            )
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=True,
            ) as ac:
                yield ac
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


# ============================================
# Test Data Fixtures
# ============================================


@pytest.fixture
def sample_tenant_id() -> UUID:
    """Sample tenant ID for testing."""
    return UUID("a0000000-0000-0000-0000-000000000001")


@pytest.fixture
def sample_session_id() -> UUID:
    """Sample session ID for testing."""
    return UUID("c0000000-0000-0000-0000-000000000001")


@pytest.fixture
def sample_event_payload() -> dict[str, Any]:
    """Sample event payload for testing."""
    return {
        "tenant_id": "a0000000-0000-0000-0000-000000000001",
        "session_id": "c0000000-0000-0000-0000-000000000001",
        "event_type": "message.in",
        "payload": {
            "content": "Olá, gostaria de agendar uma consulta",
            "channel": "whatsapp",
            "user_id": "user-123",
            "metadata": {},
        },
    }


@pytest.fixture
def sample_policy_payload() -> dict[str, Any]:
    """Sample policy payload for testing."""
    return {
        "name": "Test Policy",
        "description": "A test policy",
        "policy_type": "tenant",
        "rules": [
            {
                "rule": "test_rule",
                "message": "This is a test rule",
                "severity": "warning",
                "action": "warn",
            }
        ],
        "priority": 10,
    }


@pytest.fixture
def sample_session_payload(sample_tenant_id: UUID) -> dict[str, Any]:
    """Sample session creation payload."""
    return {
        "tenant_id": str(sample_tenant_id),
        "user_id": "user-123",
        "channel": "whatsapp",
        "metadata": {"source": "test"},
    }


# ============================================
# API Headers Fixtures
# ============================================


@pytest.fixture
def api_headers() -> dict[str, str]:
    """Default API headers for testing."""
    return {
        "Content-Type": "application/json",
        "X-API-Key": "test-api-key",
    }


# ============================================
# Database Fixtures (Placeholder)
# ============================================


@pytest.fixture
async def db_session() -> AsyncGenerator[None, None]:
    """
    Database session fixture.
    TODO: Implement when database is set up.
    """
    yield None


@pytest.fixture
async def redis_client() -> AsyncGenerator[None, None]:
    """
    Redis client fixture.
    TODO: Implement when Redis is set up.
    """
    yield None
