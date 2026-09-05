"""
NeuroStack API - Integration Tests
End-to-end tests for the complete API flow.
"""

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient


class TestAPIIntegration:
    """Integration test suite for complete API flows."""

    def test_complete_event_processing_flow(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test complete event processing flow."""
        # Process an event
        response = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=api_headers,
        )

        assert response.status_code == 200
        data = response.json()

        # Verify all expected fields are present
        assert "event_id" in data
        assert "decision" in data
        assert "state_patch" in data
        assert "prompt" in data
        assert "context" in data
        assert "mode" in data
        assert "processed_at" in data
        assert "processing_time_ms" in data

        # Verify decision structure
        decision = data["decision"]
        assert "intent" in decision
        assert "intent_category" in decision
        assert "funnel_stage" in decision
        assert "action" in decision
        assert "confidence" in decision
        assert "safety_concern" in decision
        assert "needs_rag" in decision
        assert "handoff_required" in decision

        # Verify prompt structure
        prompt = data["prompt"]
        assert "system" in prompt
        assert "instructions" in prompt
        assert "constraints" in prompt
        assert isinstance(prompt["constraints"], list)

    def test_session_creation_flow(
        self,
        client: TestClient,
        sample_session_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test session creation flow."""
        from src.models.tenant import Tenant
        from src.repositories.database import get_db_session

        tenant_id = UUID(sample_session_payload["tenant_id"])

        async def seed_tenant() -> None:
            db = get_db_session()
            try:
                db.add(
                    Tenant(
                        id=tenant_id,
                        name="Integration tenant",
                        slug=f"integration-{tenant_id}",
                        api_key_hash=f"integration-{tenant_id}",
                        config={},
                    )
                )
                await db.commit()
            finally:
                await db.close()

        client._submit(seed_tenant())  # type: ignore[attr-defined]
        response = client.post(
            "/v1/sessions",
            json=sample_session_payload,
            headers=api_headers,
        )

        assert response.status_code == 201
        data = response.json()

        # Verify session structure
        assert "id" in data
        assert "tenant_id" in data
        assert "cognitive_state" in data
        assert "is_active" in data
        assert data["is_active"] is True

        # Verify cognitive state
        state = data["cognitive_state"]
        assert state["funnel_stage"] == "lead_novo"
        assert state["turn_count"] == 0
        assert state["safety_triggers"] == 0

    def test_session_listing_flow(
        self,
        client: TestClient,
        sample_tenant_id: UUID,
        api_headers: dict[str, str],
    ) -> None:
        """Test session listing flow."""
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
        assert "has_next" in data
        assert "has_previous" in data

    def test_health_check_flow(
        self,
        client: TestClient,
    ) -> None:
        """Test health check endpoints."""
        # Health check
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "environment" in data

        # Readiness check
        response = client.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert "ready" in data
        assert "checks" in data

        # Liveness check
        response = client.get("/live")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"
        assert data["alive"] is True

    def test_request_id_propagation(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that request ID is propagated in responses."""
        response = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=api_headers,
        )

        assert response.status_code == 200
        assert "X-Request-ID" in response.headers
        assert len(response.headers["X-Request-ID"]) > 0

    def test_custom_request_id_preserved(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that custom request ID is preserved."""
        custom_request_id = "custom-request-id-12345"
        headers = {**api_headers, "X-Request-ID": custom_request_id}

        response = client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=headers,
        )

        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == custom_request_id

    def test_cors_headers_present(
        self,
        client: TestClient,
    ) -> None:
        """Test that CORS headers are present."""
        response = client.options(
            "/v1/events",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )

        # CORS preflight should work
        assert response.status_code == 200
        assert "access-control-allow-origin" in response.headers


class TestEventTypes:
    """Test different event types."""

    def test_message_in_event(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test message.in event type."""
        payload = {
            "tenant_id": "a0000000-0000-0000-0000-000000000001",
            "session_id": "c0000000-0000-0000-0000-000000000001",
            "event_type": "message.in",
            "payload": {
                "content": "Olá, preciso de ajuda",
                "channel": "whatsapp",
                "user_id": "user-123",
            },
        }

        response = client.post("/v1/events", json=payload, headers=api_headers)
        assert response.status_code == 200
        assert response.json()["mode"] == "normal"

    def test_session_start_event(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test session.start event type."""
        payload = {
            "tenant_id": "a0000000-0000-0000-0000-000000000001",
            "session_id": "c0000000-0000-0000-0000-000000000001",
            "event_type": "session.start",
            "payload": {
                "content": "session started",
                "channel": "web",
                "user_id": "user-456",
            },
        }

        response = client.post("/v1/events", json=payload, headers=api_headers)
        assert response.status_code == 200


class TestValidation:
    """Test request validation."""

    def test_invalid_uuid_rejected(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test that invalid UUIDs are rejected."""
        payload = {
            "tenant_id": "not-a-uuid",
            "session_id": "c0000000-0000-0000-0000-000000000001",
            "event_type": "message.in",
            "payload": {
                "content": "Hello",
                "channel": "whatsapp",
            },
        }

        response = client.post("/v1/events", json=payload, headers=api_headers)
        assert response.status_code == 422

    def test_missing_required_fields_rejected(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test that missing required fields are rejected."""
        payload = {
            "tenant_id": "a0000000-0000-0000-0000-000000000001",
            # Missing session_id
            "event_type": "message.in",
            "payload": {
                "content": "Hello",
                "channel": "whatsapp",
            },
        }

        response = client.post("/v1/events", json=payload, headers=api_headers)
        assert response.status_code == 422

    def test_empty_content_rejected(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test that empty content is rejected."""
        payload = {
            "tenant_id": "a0000000-0000-0000-0000-000000000001",
            "session_id": "c0000000-0000-0000-0000-000000000001",
            "event_type": "message.in",
            "payload": {
                "content": "",
                "channel": "whatsapp",
            },
        }

        response = client.post("/v1/events", json=payload, headers=api_headers)
        assert response.status_code == 422

    def test_invalid_event_type_rejected(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test that invalid event types are rejected."""
        payload = {
            "tenant_id": "a0000000-0000-0000-0000-000000000001",
            "session_id": "c0000000-0000-0000-0000-000000000001",
            "event_type": "invalid.type",
            "payload": {
                "content": "Hello",
                "channel": "whatsapp",
            },
        }

        response = client.post("/v1/events", json=payload, headers=api_headers)
        assert response.status_code == 422


class TestAPIIntegrationAsync:
    """Async integration test suite."""

    @pytest.mark.asyncio
    async def test_async_complete_flow(
        self,
        async_client,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test complete flow with async client."""
        response = await async_client.post(
            "/v1/events",
            json=sample_event_payload,
            headers=api_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "decision" in data
        assert "prompt" in data

    @pytest.mark.asyncio
    async def test_async_health_checks(
        self,
        async_client,
    ) -> None:
        """Test health checks with async client."""
        health = await async_client.get("/health")
        ready = await async_client.get("/ready")
        live = await async_client.get("/live")

        assert health.status_code == 200
        assert ready.status_code == 200
        assert live.status_code == 200
