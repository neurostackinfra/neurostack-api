"""
NeuroStack API - Authentication Middleware Tests
"""

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient


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
        async_client,
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
        async_client,
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

