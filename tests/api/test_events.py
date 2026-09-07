"""
NeuroStack API - Events Endpoint Tests
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient


class TestEventsEndpoint:
    """Test suite for events endpoint."""

    def test_process_event_returns_200(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that event processing returns 200 OK."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert "event_id" in data
        assert "decision" in data
        assert "state_patch" in data
        assert "prompt" in data
        assert "context" in data

    def test_process_event_returns_decision(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that event processing returns a valid decision."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        data = response.json()
        decision = data["decision"]

        assert "intent" in decision
        assert "funnel_stage" in decision
        assert "action" in decision
        assert "confidence" in decision
        assert isinstance(decision["confidence"], float)
        assert 0 <= decision["confidence"] <= 1
        assert "safety_concern" in decision
        assert "needs_rag" in decision
        assert "handoff_required" in decision

    def test_process_event_returns_prompt(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that event processing returns a valid prompt."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        data = response.json()
        prompt = data["prompt"]

        assert "system" in prompt
        assert "instructions" in prompt
        assert "constraints" in prompt
        assert isinstance(prompt["constraints"], list)

    def test_process_event_returns_context(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that event processing returns context."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        data = response.json()
        context = data["context"]

        assert "recent_messages" in context
        assert isinstance(context["recent_messages"], list)

    def test_process_event_with_invalid_tenant_id(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test that invalid tenant ID returns validation error."""
        payload = {
            "tenant_id": "invalid-uuid",
            "session_id": "c0000000-0000-0000-0000-000000000001",
            "event_type": "message.in",
            "payload": {
                "content": "Hello",
                "channel": "whatsapp",
            },
        }

        response = client.post("/v1/events", json=payload, headers=api_headers)
        assert response.status_code == 422

    def test_process_event_with_empty_content(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that empty content returns validation error."""
        sample_event_payload["payload"]["content"] = ""

        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )
        assert response.status_code == 422

    def test_process_event_mode_is_normal(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that default mode is normal."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        data = response.json()
        assert data["mode"] == "normal"


class TestEventsEndpointAsync:
    """Async test suite for events endpoint."""

    @pytest.mark.asyncio
    async def test_process_event_async(
        self,
        async_client,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test event processing with async client."""
        response = await async_client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert "decision" in data
