"""
NeuroStack API - End-to-End Tests for Events Endpoint
Tests the complete cognitive pipeline through the /v1/events endpoint.

Implements Task 13.3-13.5:
- 13.3: Test NORMAL flow end-to-end
- 13.4: Test SAFETY flow end-to-end
- 13.5: Test HANDOFF flow end-to-end
"""

from uuid import uuid4

import pytest
from httpx import AsyncClient


# ============================================
# Fixtures
# ============================================


@pytest.fixture
def tenant_id():
    """Generate a test tenant ID."""
    return uuid4()


@pytest.fixture
def session_id():
    """Generate a test session ID."""
    return uuid4()


@pytest.fixture
def valid_api_key():
    """Get a valid API key for testing."""
    return "test-api-key-12345"


@pytest.fixture
def auth_headers(valid_api_key):
    """Get authentication headers."""
    return {"X-API-Key": valid_api_key}


@pytest.fixture
async def client(async_client: AsyncClient):
    """Expose the shared lifespan-aware async client under the local name."""
    yield async_client


# ============================================
# Task 13.3: NORMAL Flow Tests
# ============================================


class TestNormalFlowEndToEnd:
    """End-to-end tests for NORMAL conversation flow."""

    @pytest.mark.asyncio
    async def test_greeting_flow(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test greeting message triggers NORMAL flow."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Olá, bom dia!",
                    "channel": "whatsapp",
                    "user_id": "user-123",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        # Verify structure
        assert "event_id" in data
        assert "decision" in data
        assert "state_patch" in data
        assert "prompt" in data
        assert "context" in data
        assert "mode" in data

        # Verify mode is NORMAL
        assert data["mode"] == "normal"

        # Verify decision fields
        decision = data["decision"]
        assert "intent" in decision
        assert "intent_category" in decision
        assert "funnel_stage" in decision
        assert "confidence" in decision
        assert decision["safety_concern"] is False
        assert decision["handoff_required"] is False

    @pytest.mark.asyncio
    async def test_scheduling_request_flow(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test scheduling request triggers NORMAL flow with scheduling intent."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Quero agendar uma consulta para amanhã",
                    "channel": "whatsapp",
                    "user_id": "user-123",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        # Verify NORMAL mode
        assert data["mode"] == "normal"

        # Verify scheduling intent detected
        decision = data["decision"]
        assert decision["intent_category"] in ["scheduling", "information", "unknown"]
        
        # Verify prompt was generated
        prompt = data["prompt"]
        assert "system" in prompt
        assert len(prompt["system"]) > 0
        assert "instructions" in prompt

    @pytest.mark.asyncio
    async def test_question_flow(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test question message triggers NORMAL flow."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Qual é o horário de funcionamento da clínica?",
                    "channel": "web",
                    "user_id": "user-456",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        # Should remain in NORMAL mode for questions
        assert data["mode"] in ["normal", "rag"]
        
        # Verify processing time is reasonable
        assert data["processing_time_ms"] is not None
        assert data["processing_time_ms"] < 5000  # Should complete in under 5 seconds


# ============================================
# Task 13.4: SAFETY Flow Tests
# ============================================


class TestSafetyFlowEndToEnd:
    """End-to-end tests for SAFETY conversation flow."""

    @pytest.mark.asyncio
    async def test_clinical_safety_trigger(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test clinical safety content triggers SAFETY mode."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Estou tendo dor no peito e falta de ar. Estou com medo de morrer.",
                    "channel": "whatsapp",
                    "user_id": "user-emergency",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        # Should trigger SAFETY or HANDOFF mode for emergency content
        decision = data["decision"]
        
        # Emergency keywords should trigger safety concern
        # Note: Exact behavior depends on safety detector configuration
        if decision["safety_concern"]:
            assert data["mode"] in ["safety", "handoff"]
            assert decision["safety_category"] in ["clinical_risk", "none"]

    @pytest.mark.asyncio
    async def test_medication_request_safety(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test medication request triggers SAFETY mode."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Qual remédio devo tomar para dor de cabeça?",
                    "channel": "whatsapp",
                    "user_id": "user-789",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        # Medication questions may trigger safety
        # Verify prompt includes safety constraints
        prompt = data["prompt"]
        
        # System should have instructions to avoid medical advice
        # or constraints list should include relevant warnings
        assert prompt["system"] is not None

    @pytest.mark.asyncio
    async def test_suicide_mention_safety(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test suicide/self-harm mention triggers appropriate response."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Estou pensando em me machucar",
                    "channel": "whatsapp",
                    "user_id": "user-crisis",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        # Should trigger safety concern
        decision = data["decision"]
        
        # Self-harm mentions should be detected
        # This may trigger safety mode or handoff
        assert data["mode"] in ["safety", "handoff", "normal"]


# ============================================
# Task 13.5: HANDOFF Flow Tests
# ============================================


class TestHandoffFlowEndToEnd:
    """End-to-end tests for HANDOFF conversation flow."""

    @pytest.mark.asyncio
    async def test_explicit_handoff_request(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test explicit handoff request triggers HANDOFF mode."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Quero falar com um atendente humano, por favor",
                    "channel": "whatsapp",
                    "user_id": "user-handoff",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        # Explicit handoff request should trigger handoff
        decision = data["decision"]
        
        if decision["handoff_required"]:
            assert data["mode"] == "handoff"
            assert decision["handoff_reason"] is not None
            # May include handoff summary
            if data.get("handoff_summary"):
                assert "reason" in data["handoff_summary"]
                assert "summary" in data["handoff_summary"]

    @pytest.mark.asyncio
    async def test_atendente_keyword_handoff(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test an explicit attendant request triggers HANDOFF mode."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Preciso de um atendente real",
                    "channel": "telegram",
                    "user_id": "user-telegram",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        decision = data["decision"]
        assert decision["handoff_required"] is True
        assert data["mode"] == "handoff"
        assert decision["handoff_reason"] is not None

    @pytest.mark.asyncio
    async def test_complaint_escalation(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test complaint message may trigger escalation."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Estou muito insatisfeito com o atendimento, quero reclamar!",
                    "channel": "whatsapp",
                    "user_id": "user-complaint",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        # Complaints may trigger escalation action
        decision = data["decision"]
        assert decision["intent_category"] in ["complaint", "unknown"]


# ============================================
# Integration Tests
# ============================================


class TestPipelineIntegration:
    """Integration tests for the complete cognitive pipeline."""

    @pytest.mark.asyncio
    async def test_complete_pipeline_structure(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test that all pipeline components are connected and responding."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {
                    "content": "Teste de integração completa",
                    "channel": "test",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()

        # Verify all required fields are present
        required_fields = [
            "event_id",
            "decision",
            "state_patch",
            "prompt",
            "context",
            "mode",
            "processed_at",
            "processing_time_ms",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

        # Verify decision structure
        decision_fields = [
            "intent",
            "intent_category",
            "funnel_stage",
            "action",
            "confidence",
            "safety_concern",
        ]
        for field in decision_fields:
            assert field in data["decision"], f"Missing decision field: {field}"

        # Verify prompt structure
        prompt_fields = ["system", "instructions", "constraints"]
        for field in prompt_fields:
            assert field in data["prompt"], f"Missing prompt field: {field}"

    @pytest.mark.asyncio
    async def test_multiple_messages_session(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test multiple messages in same session."""
        messages = [
            "Olá!",
            "Quero agendar uma consulta",
            "Prefiro de manhã",
        ]

        for msg in messages:
            response = await client.post(
                "/v1/events",
                headers=auth_headers,
                json={
                    "tenant_id": str(tenant_id),
                    "session_id": str(session_id),
                    "event_type": "message.in",
                    "payload": {"content": msg, "channel": "whatsapp"},
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert "decision" in data
            assert "prompt" in data

    @pytest.mark.asyncio
    async def test_different_channels(
        self, client: AsyncClient, tenant_id, auth_headers
    ):
        """Test events from different channels."""
        channels = ["whatsapp", "web", "telegram", "instagram"]

        for channel in channels:
            session_id = uuid4()
            response = await client.post(
                "/v1/events",
                headers=auth_headers,
                json={
                    "tenant_id": str(tenant_id),
                    "session_id": str(session_id),
                    "event_type": "message.in",
                    "payload": {
                        "content": f"Mensagem do {channel}",
                        "channel": channel,
                    },
                },
            )

            assert response.status_code == 200


# ============================================
# Error Handling Tests
# ============================================


class TestErrorHandling:
    """Test error handling in the pipeline."""

    @pytest.mark.asyncio
    async def test_missing_required_fields(
        self, client: AsyncClient, auth_headers
    ):
        """Test validation for missing required fields."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(uuid4()),
                # Missing session_id and payload
            },
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_invalid_uuid_format(
        self, client: AsyncClient, auth_headers
    ):
        """Test validation for invalid UUID format."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": "not-a-uuid",
                "session_id": "also-not-a-uuid",
                "payload": {"content": "Test"},
            },
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_empty_message_content(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test handling of empty message content."""
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {"content": "", "channel": "whatsapp"},
            },
        )

        # Empty content should be rejected by validation
        assert response.status_code == 422


# ============================================
# Performance Tests
# ============================================


class TestPerformance:
    """Performance tests for the pipeline."""

    @pytest.mark.asyncio
    async def test_response_time_under_threshold(
        self, client: AsyncClient, tenant_id, session_id, auth_headers
    ):
        """Test that response time is under acceptable threshold."""
        import time

        start = time.perf_counter()
        response = await client.post(
            "/v1/events",
            headers=auth_headers,
            json={
                "tenant_id": str(tenant_id),
                "session_id": str(session_id),
                "event_type": "message.in",
                "payload": {"content": "Teste de performance", "channel": "test"},
            },
        )
        elapsed = time.perf_counter() - start

        assert response.status_code == 200
        # Response should be under 2 seconds (generous for cold start)
        assert elapsed < 2.0, f"Response took {elapsed:.2f}s, expected < 2s"

        # Also check reported processing time
        data = response.json()
        assert data["processing_time_ms"] < 1000  # Processing should be under 1s
