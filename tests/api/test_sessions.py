"""
NeuroStack API - Sessions Endpoint Tests
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient


class TestSessionsEndpoint:
    """Test suite for sessions endpoint."""

    def test_create_session_returns_201(
        self,
        client: TestClient,
        sample_session_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that session creation returns 201 Created."""
        response = client.post("/v1/sessions", json=sample_session_payload, headers=api_headers)

        assert response.status_code == 201
        data = response.json()
        assert "id" in data
        assert "tenant_id" in data
        assert "cognitive_state" in data
        assert data["is_active"] is True

    def test_create_session_returns_cognitive_state(
        self,
        client: TestClient,
        sample_session_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that created session has cognitive state."""
        response = client.post("/v1/sessions", json=sample_session_payload, headers=api_headers)

        data = response.json()
        state = data["cognitive_state"]

        assert "funnel_stage" in state
        assert "slots" in state
        assert "flags" in state
        assert "turn_count" in state

    def test_create_session_with_minimal_payload(
        self,
        client: TestClient,
        sample_tenant_id,
        api_headers: dict[str, str],
    ) -> None:
        """Test session creation with minimal payload."""
        payload = {"tenant_id": str(sample_tenant_id)}

        response = client.post("/v1/sessions", json=payload, headers=api_headers)

        assert response.status_code == 201
        data = response.json()
        assert data["channel"] == "unknown"
        assert data["user_id"] is None

    def test_list_sessions_returns_200(
        self,
        client: TestClient,
        sample_tenant_id,
        api_headers: dict[str, str],
    ) -> None:
        """Test that session listing returns 200 OK."""
        response = client.get(
            "/v1/sessions",
            params={"tenant_id": str(sample_tenant_id)},
            headers=api_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "sessions" in data
        assert "total" in data
        assert "page" in data
        assert "page_size" in data

    def test_list_sessions_with_pagination(
        self,
        client: TestClient,
        sample_tenant_id,
        api_headers: dict[str, str],
    ) -> None:
        """Test session listing with pagination parameters."""
        response = client.get(
            "/v1/sessions",
            params={
                "tenant_id": str(sample_tenant_id),
                "page": 2,
                "page_size": 10,
            },
            headers=api_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 2
        assert data["page_size"] == 10


class TestSessionsEndpointAsync:
    """Async test suite for sessions endpoint."""

    @pytest.mark.asyncio
    async def test_create_session_async(
        self,
        async_client,
        sample_session_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test session creation with async client."""
        response = await async_client.post("/v1/sessions", json=sample_session_payload, headers=api_headers)

        assert response.status_code == 201
        data = response.json()
        assert "id" in data
