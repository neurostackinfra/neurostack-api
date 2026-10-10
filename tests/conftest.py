"""
NeuroStack API - Pytest Configuration and Fixtures
"""

import asyncio
import importlib
import os
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from concurrent.futures import Future
from contextlib import suppress
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.main import app
from src.repositories import database as database_module

repositories = importlib.import_module("src.repositories")

_DEFAULT_TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
_production_init_database = database_module.init_database
_production_create_async_engine = getattr(database_module, "create_async_engine")
_database_callable_stack: list[tuple[Any, Any, Any]] = []


def _validate_test_database_url(raw_url: str) -> str:
    """Accept only the canonical process-local pytest database."""
    if raw_url != _DEFAULT_TEST_DATABASE_URL:
        raise pytest.UsageError(
            "PYTEST_DATABASE_URL must be exactly "
            "sqlite+aiosqlite:///:memory:; PostgreSQL gates use their "
            "disposable smoke-test harnesses"
        )
    return raw_url


def _configured_test_database_url() -> str:
    return _validate_test_database_url(
        os.environ.get("PYTEST_DATABASE_URL", _DEFAULT_TEST_DATABASE_URL)
    )


def _guarded_create_async_engine(database_url: str, **options: Any) -> Any:
    validated_url = _validate_test_database_url(database_url)
    return _production_create_async_engine(validated_url, **options)


async def _guarded_init_database(
    *,
    database_url: str | None = None,
    engine_factory: Callable[..., Any] | None = None,
    session_factory_builder: Callable[..., Any] | None = None,
) -> None:
    """Initialize only the explicitly isolated pytest database."""
    resolved_url = (
        _configured_test_database_url() if database_url is None else database_url
    )
    await _production_init_database(
        database_url=_validate_test_database_url(resolved_url),
        engine_factory=engine_factory,
        session_factory_builder=session_factory_builder,
    )


def pytest_configure() -> None:
    """Fail before collection and guard aliases imported by test modules."""
    _configured_test_database_url()
    _database_callable_stack.append(
        (
            database_module.init_database,
            getattr(database_module, "create_async_engine"),
            getattr(repositories, "init_database"),
        )
    )
    setattr(database_module, "create_async_engine", _guarded_create_async_engine)
    setattr(database_module, "init_database", _guarded_init_database)
    setattr(repositories, "init_database", _guarded_init_database)


def pytest_unconfigure() -> None:
    """Restore exact callable identities after embedded pytest runs."""
    if not _database_callable_stack:
        return
    database_init, engine_factory, repositories_init = _database_callable_stack.pop()
    setattr(database_module, "init_database", database_init)
    setattr(database_module, "create_async_engine", engine_factory)
    setattr(repositories, "init_database", repositories_init)


def pytest_report_header() -> str:
    _configured_test_database_url()
    return "isolated database: sqlite/:memory:"


async def _keep_event_loop_responsive() -> None:
    """Prevent lost thread-safe wakeups from stalling aiosqlite in tests."""
    while True:
        await asyncio.sleep(0.01)


class LifespanTestClient:
    """Synchronous facade with a continuously running application event loop."""

    def __init__(self, application: Any) -> None:
        self.application = application
        self.lifespan_started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started: Future[None] = Future()
        self._finished: Future[None] = Future()
        self._thread = threading.Thread(
            target=self._run_application_loop,
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
        try:
            if self._client is not None:
                await self._client.__aexit__(None, None, None)
            if self._lifespan is not None:
                await self._lifespan.__aexit__(None, None, None)
        finally:
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

    def _run_application_loop(self) -> None:
        try:
            with asyncio.Runner() as runner:
                self._loop = runner.get_loop()
                try:
                    runner.run(self._start())
                except BaseException as error:
                    self._started.set_exception(error)
                    raise
                self._started.set_result(None)
                self._loop.run_forever()
                runner.run(self._stop())
        except BaseException as error:
            if not self._started.done():
                self._started.set_exception(error)
            self._finished.set_exception(error)
        else:
            self._finished.set_result(None)

    def _submit(self, coroutine: Any) -> Any:
        if self._loop is None:
            raise RuntimeError("Test client application loop has not started")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def drain(self) -> None:
        """Drain owned application tasks on the application's event loop."""
        from src.api.routes import rlhf

        self._submit(rlhf.wait_idle())

    def __enter__(self) -> "LifespanTestClient":
        self._thread.start()
        try:
            self._started.result()
        except BaseException:
            self._thread.join()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        cleanup_error: BaseException | None = None
        try:
            self.drain()
        except BaseException as error:
            cleanup_error = error
        finally:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
        try:
            self._finished.result()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error

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
# Application Fixtures
# ============================================


@pytest.fixture
def test_database_url() -> str:
    """Return the validated process-local pytest database URL."""
    return _configured_test_database_url()


@pytest.fixture
def database_url_validator() -> Callable[[str], str]:
    """Expose the fail-closed URL validator to regression tests."""
    return _validate_test_database_url


@pytest.fixture
def lifespan_test_client_factory() -> Callable[[Any], LifespanTestClient]:
    return LifespanTestClient


@pytest.fixture
def production_database_lifecycle() -> Callable[..., Awaitable[None]]:
    """Expose production lifecycle semantics only with an explicit fake engine."""

    async def initialize(
        *,
        engine_factory: Callable[..., Any],
        database_url: str | None = None,
        session_factory_builder: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(engine_factory, Mock):
            raise TypeError("production lifecycle tests require a mock engine factory")
        await _production_init_database(
            database_url=database_url,
            engine_factory=engine_factory,
            session_factory_builder=session_factory_builder,
        )

    return initialize


@pytest.fixture(autouse=True)
def _inject_isolated_database(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
) -> Generator[None, None, None]:
    """Keep every application lifespan on the canonical in-memory database."""
    monkeypatch.setattr(repositories, "init_database", _guarded_init_database)
    monkeypatch.setattr(database_module, "init_database", _guarded_init_database)
    monkeypatch.setattr(
        database_module,
        "create_async_engine",
        _guarded_create_async_engine,
    )
    assert test_database_url == _DEFAULT_TEST_DATABASE_URL
    yield


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
