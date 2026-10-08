"""
NeuroStack API - Health Endpoint Tests
"""

import pytest
from fastapi.testclient import TestClient


class TestHealthEndpoints:
    """Test suite for health check endpoints."""

    def test_health_check_returns_200(self, client: TestClient) -> None:
        """Test that health check returns 200 OK."""
        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "environment" in data
        assert "timestamp" in data

    def test_health_check_contains_version(self, client: TestClient) -> None:
        """Test that health check contains version information."""
        response = client.get("/health")

        data = response.json()
        assert data["version"] == "0.1.0"

    def test_readiness_check_returns_200(self, client: TestClient) -> None:
        """Test that readiness check returns 200 OK."""
        response = client.get("/ready")

        assert response.status_code == 200
        data = response.json()
        assert "ready" in data
        assert "checks" in data
        assert isinstance(data["checks"], dict)

    def test_readiness_check_contains_all_services(self, client: TestClient) -> None:
        """Test that readiness check contains all service checks."""
        response = client.get("/ready")

        data = response.json()
        expected_services = ["api", "database", "redis", "qdrant"]

        for service in expected_services:
            assert service in data["checks"], f"Missing check for {service}"

    def test_liveness_check_returns_200(self, client: TestClient) -> None:
        """Test that liveness check returns 200 OK."""
        response = client.get("/live")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"
        assert data["alive"] is True

    def test_health_components_included(self, client: TestClient) -> None:
        """Test that health response includes component details."""
        response = client.get("/health")

        data = response.json()
        assert "components" in data
        assert isinstance(data["components"], list)
        assert len(data["components"]) > 0

        # Verify component structure
        for component in data["components"]:
            assert "name" in component
            assert "status" in component

    def test_openapi_endpoints_start_with_media_contract(
        self, client: TestClient
    ) -> None:
        """Both published schema URLs remain healthy after router registration."""
        for path in ("/openapi.json", "/v1/openapi.json"):
            response = client.get(path)
            assert response.status_code == 200
            schema = response.json()
            assert schema["openapi"] == "3.1.0"
            assert "/v1/media-assets" in schema["paths"]


class TestHealthEndpointsAsync:
    """Async test suite for health check endpoints."""

    @pytest.mark.asyncio
    async def test_health_check_async(self, async_client) -> None:
        """Test health check with async client."""
        response = await async_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_ready_check_async(self, async_client) -> None:
        """Test ready check with async client."""
        response = await async_client.get("/ready")

        assert response.status_code == 200
        data = response.json()
        assert "ready" in data

    @pytest.mark.asyncio
    async def test_live_check_async(self, async_client) -> None:
        """Test live check with async client."""
        response = await async_client.get("/live")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"
