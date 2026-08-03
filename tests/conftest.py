"""
NeuroStack API - Pytest Configuration and Fixtures
"""

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import sys
sys.path.insert(0, "/home/jairomendes/neurostack/neurostack-api")

from src.api.main import app


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
def test_app():
    """Get the test application instance."""
    return app


@pytest.fixture
def client(test_app) -> Generator[TestClient, None, None]:
    """Create a synchronous test client."""
    with TestClient(test_app) as c:
        yield c


@pytest.fixture
async def async_client(test_app) -> AsyncGenerator[AsyncClient, None]:
    """Create an asynchronous test client using ASGITransport for httpx >= 0.25.

    ASGITransport does not run the app lifespan, so ensure the database is
    initialized here (mirroring the lifespan startup). init_database() is
    idempotent, so leaving the engine up for later tests is safe and matches
    how the running app keeps a long-lived engine.
    """
    from src.repositories import init_database

    await init_database()
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


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
async def db_session():
    """
    Database session fixture.
    TODO: Implement when database is set up.
    """
    yield None


@pytest.fixture
async def redis_client():
    """
    Redis client fixture.
    TODO: Implement when Redis is set up.
    """
    yield None

