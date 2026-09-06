"""
NeuroStack API - Policies Endpoint Tests
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient


class TestPoliciesEndpoint:
    """Test suite for policies endpoint."""

    def test_create_policy_returns_201(
        self,
        client: TestClient,
        sample_policy_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that policy creation returns 201 Created."""
        response = client.post(
            "/v1/policies", json=sample_policy_payload, headers=api_headers
        )

        assert response.status_code == 201
        data = response.json()
        assert "id" in data
        assert data["name"] == sample_policy_payload["name"]
        assert data["is_active"] is True

    def test_create_policy_with_rules(
        self,
        client: TestClient,
        sample_policy_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test policy creation with rules."""
        response = client.post(
            "/v1/policies", json=sample_policy_payload, headers=api_headers
        )

        data = response.json()
        assert len(data["rules"]) == 1
        assert data["rules"][0]["rule"] == "test_rule"

    def test_create_global_policy(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test global policy creation (no tenant_id)."""
        payload = {
            "name": "Global Test Policy",
            "policy_type": "global",
            "rules": [],
        }

        response = client.post("/v1/policies", json=payload, headers=api_headers)

        assert response.status_code == 403
        assert response.json() == {
            "detail": "Global policies can only be managed by system administrators"
        }

    def test_list_policies_returns_200(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test that policy listing returns 200 OK."""
        response = client.get("/v1/policies", headers=api_headers)

        assert response.status_code == 200
        data = response.json()
        assert "policies" in data
        assert "total" in data
        assert isinstance(data["policies"], list)

    def test_list_policies_includes_global(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test that policy listing includes global policies."""
        response = client.get("/v1/policies", headers=api_headers)

        data = response.json()
        assert data["total"] > 0

        # Check for global safety policy
        global_policies = [p for p in data["policies"] if p["tenant_id"] is None]
        assert len(global_policies) > 0

    def test_list_policies_with_tenant_filter(
        self,
        client: TestClient,
        sample_tenant_id,
        api_headers: dict[str, str],
    ) -> None:
        """Test policy listing with tenant filter."""
        response = client.get(
            "/v1/policies",
            params={"tenant_id": str(sample_tenant_id)},
            headers=api_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "policies" in data

    def test_get_effective_policies(
        self,
        client: TestClient,
        sample_tenant_id,
        api_headers: dict[str, str],
    ) -> None:
        """Test getting effective policies for a tenant."""
        response = client.get(
            f"/v1/policies/tenant/{sample_tenant_id}/effective", headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        # Endpoint returns effective_rules and source_policies, not policies
        assert "effective_rules" in data
        assert "source_policies" in data
        assert "tenant_id" in data


class TestPoliciesEndpointAsync:
    """Async test suite for policies endpoint."""

    @pytest.mark.asyncio
    async def test_create_policy_async(
        self,
        async_client,
        sample_policy_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test policy creation with async client."""
        response = await async_client.post(
            "/v1/policies", json=sample_policy_payload, headers=api_headers
        )

        assert response.status_code == 201
        data = response.json()
        assert "id" in data
