"""
Tests for the Human Handoff Service.

Tests cover:
- ConversationSummarizer
- WebhookDispatcher
- HandoffService
- API endpoints
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from src.handoff.models import (
    ConversationMessage,
    ConversationSummary,
    HandoffDetail,
    HandoffListItem,
    HandoffReason,
    HandoffRequest,
    HandoffResponse,
    HandoffServiceConfig,
    HandoffStatus,
    HandoffUpdate,
    HandoffUrgency,
    KeyInformation,
    WebhookConfig,
    WebhookDelivery,
    WebhookPayload,
    WebhookStatus,
)
from src.core.exceptions import (
    HandoffNotFoundError,
    HandoffServiceError,
)
from src.services import (
    HandoffService,
    get_handoff_service,
)
from src.handoff.summarizer import ConversationSummarizer
from src.core.exceptions import (
    WebhookConfigError,
    WebhookDeliveryError,
)
from src.handoff.webhook import (
    WebhookDispatcher,
)


# ============================================
# Fixtures
# ============================================


@pytest.fixture
def tenant_id():
    return uuid4()


@pytest.fixture
def session_id():
    return uuid4()


@pytest.fixture
def sample_messages():
    return [
        {
            "role": "user",
            "content": "Olá, meu nome é João Silva",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "intent": "greeting",
        },
        {
            "role": "assistant",
            "content": "Olá João! Como posso ajudá-lo hoje?",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        {
            "role": "user",
            "content": "Quero agendar uma consulta para semana que vem, de manhã",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "intent": "scheduling",
        },
        {
            "role": "assistant",
            "content": "Claro! Temos horários disponíveis na segunda e quarta de manhã.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        {
            "role": "user",
            "content": "Estou com muita dor de cabeça e enjoo",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "intent": "symptom_report",
        },
        {
            "role": "user",
            "content": "Quero falar com um atendente humano",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "intent": "handoff_request",
        },
    ]


@pytest.fixture
def summarizer():
    return ConversationSummarizer(
        max_recent_messages=5,
        max_summary_length=500,
    )


@pytest.fixture
def webhook_dispatcher():
    return WebhookDispatcher(
        default_timeout=5,
        max_retries=2,
        retry_delay=1,
    )


@pytest.fixture
def handoff_service(webhook_dispatcher):
    return HandoffService(
        event_store=None,  # No event store for unit tests
        summarizer=ConversationSummarizer(),
        webhook_dispatcher=webhook_dispatcher,
    )


# ============================================
# ConversationSummarizer Tests
# ============================================


class TestConversationSummarizer:
    """Tests for ConversationSummarizer."""

    def test_summarize_creates_valid_summary(
        self,
        summarizer,
        tenant_id,
        session_id,
        sample_messages,
    ):
        """Test that summarize creates a valid ConversationSummary."""
        summary = summarizer.summarize(
            session_id=session_id,
            tenant_id=tenant_id,
            messages=sample_messages,
            reason=HandoffReason.USER_REQUEST,
            urgency=HandoffUrgency.NORMAL,
            funnel_stage="consideration",
        )

        assert isinstance(summary, ConversationSummary)
        assert summary.session_id == session_id
        assert summary.tenant_id == tenant_id
        assert summary.handoff_reason == HandoffReason.USER_REQUEST
        assert summary.urgency == HandoffUrgency.NORMAL
        assert summary.funnel_stage == "consideration"
        assert len(summary.summary_text) > 0

    def test_summarize_extracts_user_name(
        self,
        summarizer,
        tenant_id,
        session_id,
        sample_messages,
    ):
        """Test that summarizer extracts user name."""
        summary = summarizer.summarize(
            session_id=session_id,
            tenant_id=tenant_id,
            messages=sample_messages,
            reason=HandoffReason.USER_REQUEST,
        )

        assert summary.key_info.user_name == "João Silva"

    def test_summarize_extracts_symptoms(
        self,
        summarizer,
        tenant_id,
        session_id,
        sample_messages,
    ):
        """Test that summarizer extracts symptoms."""
        summary = summarizer.summarize(
            session_id=session_id,
            tenant_id=tenant_id,
            messages=sample_messages,
            reason=HandoffReason.USER_REQUEST,
        )

        assert "dor de cabeça" in summary.key_info.symptoms_mentioned or \
               "dor" in summary.key_info.symptoms_mentioned
        assert "enjoo" in summary.key_info.symptoms_mentioned

    def test_summarize_extracts_appointment_preferences(
        self,
        summarizer,
        tenant_id,
        session_id,
        sample_messages,
    ):
        """Test that summarizer extracts appointment preferences."""
        summary = summarizer.summarize(
            session_id=session_id,
            tenant_id=tenant_id,
            messages=sample_messages,
            reason=HandoffReason.USER_REQUEST,
        )

        assert summary.key_info.appointment_preferences is not None
        assert "manhã" in summary.key_info.appointment_preferences

    def test_summarize_includes_recent_messages(
        self,
        summarizer,
        tenant_id,
        session_id,
        sample_messages,
    ):
        """Test that summarizer includes recent messages."""
        summary = summarizer.summarize(
            session_id=session_id,
            tenant_id=tenant_id,
            messages=sample_messages,
            reason=HandoffReason.USER_REQUEST,
        )

        assert len(summary.recent_messages) <= 5
        assert len(summary.recent_messages) > 0

    def test_summarize_counts_messages_correctly(
        self,
        summarizer,
        tenant_id,
        session_id,
        sample_messages,
    ):
        """Test message counting."""
        summary = summarizer.summarize(
            session_id=session_id,
            tenant_id=tenant_id,
            messages=sample_messages,
            reason=HandoffReason.USER_REQUEST,
        )

        assert summary.total_messages == len(sample_messages)
        user_count = len([m for m in sample_messages if m["role"] == "user"])
        assert summary.turn_count == user_count

    def test_quick_summary_returns_string(
        self,
        summarizer,
        sample_messages,
    ):
        """Test quick_summary returns a brief string."""
        summary = summarizer.quick_summary(
            messages=sample_messages,
            reason=HandoffReason.USER_REQUEST,
            max_length=200,
        )

        assert isinstance(summary, str)
        assert len(summary) <= 200

    def test_summarize_empty_messages(
        self,
        summarizer,
        tenant_id,
        session_id,
    ):
        """Test summarizing with no messages."""
        summary = summarizer.summarize(
            session_id=session_id,
            tenant_id=tenant_id,
            messages=[],
            reason=HandoffReason.USER_REQUEST,
        )

        assert summary.total_messages == 0
        assert summary.turn_count == 0

    def test_summarize_with_contact_info(
        self,
        summarizer,
        tenant_id,
        session_id,
    ):
        """Test extracting contact info."""
        messages = [
            {
                "role": "user",
                "content": "Meu telefone é 11 98765-4321",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]

        summary = summarizer.summarize(
            session_id=session_id,
            tenant_id=tenant_id,
            messages=messages,
            reason=HandoffReason.USER_REQUEST,
        )

        assert summary.key_info.contact_info is not None

    def test_summarize_with_email(
        self,
        summarizer,
        tenant_id,
        session_id,
    ):
        """Test extracting email."""
        messages = [
            {
                "role": "user",
                "content": "Meu email é joao@example.com",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]

        summary = summarizer.summarize(
            session_id=session_id,
            tenant_id=tenant_id,
            messages=messages,
            reason=HandoffReason.USER_REQUEST,
        )

        assert summary.key_info.contact_info == "joao@example.com"


# ============================================
# WebhookDispatcher Tests
# ============================================


class TestWebhookDispatcher:
    """Tests for WebhookDispatcher."""

    def test_set_and_get_config(self, webhook_dispatcher, tenant_id):
        """Test setting and getting webhook config."""
        config = WebhookConfig(
            url="https://example.com/webhook",
            secret="test-secret",
            timeout_seconds=30,
            retry_count=3,
        )

        webhook_dispatcher.set_config(tenant_id, config)
        retrieved = webhook_dispatcher.get_config(tenant_id)

        assert retrieved is not None
        assert retrieved.url == config.url
        assert retrieved.secret == config.secret

    def test_remove_config(self, webhook_dispatcher, tenant_id):
        """Test removing webhook config."""
        config = WebhookConfig(url="https://example.com/webhook")
        webhook_dispatcher.set_config(tenant_id, config)

        result = webhook_dispatcher.remove_config(tenant_id)
        assert result is True

        retrieved = webhook_dispatcher.get_config(tenant_id)
        assert retrieved is None

    def test_remove_nonexistent_config(self, webhook_dispatcher, tenant_id):
        """Test removing config that doesn't exist."""
        result = webhook_dispatcher.remove_config(tenant_id)
        assert result is False

    def test_is_configured(self, webhook_dispatcher, tenant_id):
        """Test is_configured check."""
        assert webhook_dispatcher.is_configured(tenant_id) is False

        config = WebhookConfig(url="https://example.com/webhook", enabled=True)
        webhook_dispatcher.set_config(tenant_id, config)

        assert webhook_dispatcher.is_configured(tenant_id) is True

    def test_is_configured_when_disabled(self, webhook_dispatcher, tenant_id):
        """Test is_configured when webhook is disabled."""
        config = WebhookConfig(url="https://example.com/webhook", enabled=False)
        webhook_dispatcher.set_config(tenant_id, config)

        assert webhook_dispatcher.is_configured(tenant_id) is False

    def test_create_payload(self, webhook_dispatcher, tenant_id, session_id):
        """Test creating webhook payload."""
        handoff_id = uuid4()
        payload = webhook_dispatcher.create_payload(
            handoff_id=handoff_id,
            tenant_id=tenant_id,
            session_id=session_id,
            status=HandoffStatus.PENDING,
            urgency=HandoffUrgency.HIGH,
            reason=HandoffReason.SAFETY_CONCERN,
            summary="Test summary",
            key_info={"test": "value"},
            turn_count=5,
            funnel_stage="consideration",
        )

        assert isinstance(payload, WebhookPayload)
        assert payload.handoff_id == handoff_id
        assert payload.event_type == "handoff.pending"
        assert payload.urgency == HandoffUrgency.HIGH
        assert payload.reason == HandoffReason.SAFETY_CONCERN

    def test_generate_signature(self, webhook_dispatcher):
        """Test HMAC signature generation."""
        payload = {"test": "data", "value": 123}
        secret = "test-secret"

        signature = webhook_dispatcher.generate_signature(payload, secret)

        assert signature.startswith("sha256=")
        assert len(signature) > 10

    def test_signature_is_deterministic(self, webhook_dispatcher):
        """Test that same payload + secret produces same signature."""
        payload = {"test": "data"}
        secret = "test-secret"

        sig1 = webhook_dispatcher.generate_signature(payload, secret)
        sig2 = webhook_dispatcher.generate_signature(payload, secret)

        assert sig1 == sig2

    def test_signature_differs_with_different_secret(self, webhook_dispatcher):
        """Test that different secrets produce different signatures."""
        payload = {"test": "data"}

        sig1 = webhook_dispatcher.generate_signature(payload, "secret1")
        sig2 = webhook_dispatcher.generate_signature(payload, "secret2")

        assert sig1 != sig2

    @pytest.mark.asyncio
    async def test_send_without_config_raises_error(
        self,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        """Test that sending without config raises error."""
        payload = webhook_dispatcher.create_payload(
            handoff_id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            status=HandoffStatus.PENDING,
            urgency=HandoffUrgency.NORMAL,
            reason=HandoffReason.USER_REQUEST,
            summary="Test",
        )

        with pytest.raises(WebhookConfigError):
            await webhook_dispatcher.send(tenant_id, payload)

    @pytest.mark.asyncio
    async def test_send_disabled_webhook(
        self,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        """Test sending when webhook is disabled."""
        config = WebhookConfig(url="https://example.com/webhook", enabled=False)
        webhook_dispatcher.set_config(tenant_id, config)

        payload = webhook_dispatcher.create_payload(
            handoff_id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            status=HandoffStatus.PENDING,
            urgency=HandoffUrgency.NORMAL,
            reason=HandoffReason.USER_REQUEST,
            summary="Test",
        )

        delivery = await webhook_dispatcher.send(tenant_id, payload)

        assert delivery.status == WebhookStatus.FAILED
        assert "disabled" in delivery.error_message.lower()

    @pytest.mark.asyncio
    async def test_send_successful(self, webhook_dispatcher, tenant_id, session_id):
        """Test successful webhook send."""
        config = WebhookConfig(
            url="https://example.com/webhook",
            timeout_seconds=5,
            retry_count=1,
        )
        webhook_dispatcher.set_config(tenant_id, config)

        payload = webhook_dispatcher.create_payload(
            handoff_id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            status=HandoffStatus.PENDING,
            urgency=HandoffUrgency.NORMAL,
            reason=HandoffReason.USER_REQUEST,
            summary="Test",
        )

        # Mock the HTTP client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"success": true}'

        with patch.object(webhook_dispatcher, '_get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            delivery = await webhook_dispatcher.send(tenant_id, payload)

            assert delivery.status == WebhookStatus.SENT
            assert delivery.response_status == 200

    @pytest.mark.asyncio
    async def test_send_with_retries(self, webhook_dispatcher, tenant_id, session_id):
        """Test webhook retries on failure."""
        config = WebhookConfig(
            url="https://example.com/webhook",
            timeout_seconds=5,
            retry_count=2,
            retry_delay_seconds=1,  # Minimum allowed delay
        )
        webhook_dispatcher.set_config(tenant_id, config)

        payload = webhook_dispatcher.create_payload(
            handoff_id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            status=HandoffStatus.PENDING,
            urgency=HandoffUrgency.NORMAL,
            reason=HandoffReason.USER_REQUEST,
            summary="Test",
        )

        # Mock to fail first, succeed second
        mock_response_fail = MagicMock()
        mock_response_fail.status_code = 500
        mock_response_fail.text = "Error"

        mock_response_success = MagicMock()
        mock_response_success.status_code = 200
        mock_response_success.text = "OK"

        with patch.object(webhook_dispatcher, '_get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.side_effect = [mock_response_fail, mock_response_success]
            mock_get_client.return_value = mock_client

            delivery = await webhook_dispatcher.send(tenant_id, payload)

            assert delivery.status == WebhookStatus.SENT
            assert delivery.attempt == 2

    def test_get_deliveries_empty(self, webhook_dispatcher, tenant_id):
        """Test getting deliveries when none exist."""
        deliveries = webhook_dispatcher.get_deliveries(tenant_id=tenant_id)
        assert len(deliveries) == 0

    def test_get_delivery_stats_empty(self, webhook_dispatcher, tenant_id):
        """Test getting stats when no deliveries."""
        stats = webhook_dispatcher.get_delivery_stats(tenant_id)

        assert stats["total"] == 0
        assert stats["sent"] == 0
        assert stats["failed"] == 0


# ============================================
# HandoffService Tests
# ============================================


class TestHandoffService:
    """Tests for HandoffService."""

    @pytest.mark.asyncio
    async def test_initiate_handoff(
        self,
        handoff_service,
        tenant_id,
        session_id,
        sample_messages,
    ):
        """Test initiating a handoff."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
            urgency=HandoffUrgency.NORMAL,
            funnel_stage="consideration",
            recent_messages=sample_messages,
        )

        response = await handoff_service.initiate_handoff(request)

        assert isinstance(response, HandoffResponse)
        assert response.tenant_id == tenant_id
        assert response.session_id == session_id
        assert response.status == HandoffStatus.PENDING
        assert len(response.user_message) > 0

    @pytest.mark.asyncio
    async def test_initiate_handoff_with_safety_concern(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test initiating handoff for safety concern."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.SAFETY_CONCERN,
            urgency=HandoffUrgency.CRITICAL,
            recent_messages=[],
        )

        response = await handoff_service.initiate_handoff(request)

        assert response.status == HandoffStatus.PENDING
        assert "segurança" in response.user_message.lower()

    @pytest.mark.asyncio
    async def test_scheduling_link_handoff_carries_verification_status(
        self,
        handoff_service,
        tenant_id,
        session_id,
        sample_messages,
    ):
        """Scheduling-link handoff starts pending with link_sent status."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.SCHEDULING,
            urgency=HandoffUrgency.NORMAL,
            sector="agendamento",
            sla="prioritario",
            funnel_stage="agendamento_solicitado",
            recent_messages=sample_messages,
            verification_status="link_sent",
        )

        response = await handoff_service.initiate_handoff(request)
        detail = await handoff_service.get_handoff(response.id)

        assert response.verification_status == "link_sent"
        assert response.summary.verification_status == "link_sent"
        assert "LINK DE AGENDAMENTO ENVIADO" in response.summary.summary_text
        assert detail.verification_status == "link_sent"
        assert detail.sector == "agendamento"

    @pytest.mark.asyncio
    async def test_enrich_scheduling_verification_updates_same_handoff(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Outcome enrichment updates the existing handoff and emits update payload."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.SCHEDULING,
            urgency=HandoffUrgency.NORMAL,
            sector="agendamento",
            sla="prioritario",
            verification_status="link_sent",
        )
        response = await handoff_service.initiate_handoff(request)

        config = WebhookConfig(url="https://example.com/webhook", enabled=True)
        handoff_service.configure_webhook(tenant_id, config)
        delivery = WebhookDelivery(
            handoff_id=response.id,
            tenant_id=tenant_id,
            url=config.url,
            status=WebhookStatus.SENT,
        )
        handoff_service.webhook_dispatcher.send = AsyncMock(return_value=delivery)

        updated_id = await handoff_service.enrich_scheduling_verification(
            session_id=session_id,
            verification_status="concluiu",
            handoff_id=response.id,
        )
        detail = await handoff_service.get_handoff(response.id)
        handoffs = await handoff_service.list_handoffs(tenant_id)

        assert updated_id == response.id
        assert detail.status == HandoffStatus.PENDING
        assert detail.verification_status == "concluiu"
        assert len(handoffs) == 1

        payload = handoff_service.webhook_dispatcher.send.await_args.args[1]
        assert payload.event_type == "handoff.updated"
        assert payload.handoff_id == response.id
        assert payload.verification_status == "concluiu"

    @pytest.mark.asyncio
    async def test_get_handoff(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test getting a handoff by ID."""
        # Create a handoff first
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        response = await handoff_service.initiate_handoff(request)

        # Get the handoff
        handoff = await handoff_service.get_handoff(response.id)

        assert handoff.id == response.id
        assert handoff.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_get_nonexistent_handoff(self, handoff_service):
        """Test getting a handoff that doesn't exist."""
        with pytest.raises(HandoffNotFoundError):
            await handoff_service.get_handoff(uuid4())

    @pytest.mark.asyncio
    async def test_accept_handoff(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test accepting a handoff."""
        # Create handoff
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        response = await handoff_service.initiate_handoff(request)

        # Accept it
        updated = await handoff_service.accept_handoff(
            response.id,
            agent_id="agent-123",
        )

        assert updated.status == HandoffStatus.ACCEPTED
        assert updated.assigned_to == "agent-123"

    @pytest.mark.asyncio
    async def test_complete_handoff(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test completing a handoff."""
        # Create and accept handoff
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        response = await handoff_service.initiate_handoff(request)
        await handoff_service.accept_handoff(response.id)

        # Complete it
        updated = await handoff_service.complete_handoff(
            response.id,
            notes="Resolved successfully",
        )

        assert updated.status == HandoffStatus.COMPLETED
        assert updated.resolved_at is not None
        assert updated.resolution_notes == "Resolved successfully"

    @pytest.mark.asyncio
    async def test_cancel_handoff(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test cancelling a handoff."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        response = await handoff_service.initiate_handoff(request)

        updated = await handoff_service.cancel_handoff(
            response.id,
            notes="User cancelled",
        )

        assert updated.status == HandoffStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_timeout_handoff(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test timing out a handoff."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        response = await handoff_service.initiate_handoff(request)

        updated = await handoff_service.timeout_handoff(response.id)

        assert updated.status == HandoffStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_list_handoffs(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test listing handoffs for a tenant."""
        # Create multiple handoffs
        for _ in range(3):
            request = HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
            await handoff_service.initiate_handoff(request)

        # List them
        handoffs = await handoff_service.list_handoffs(tenant_id)

        assert len(handoffs) == 3
        assert all(isinstance(h, HandoffListItem) for h in handoffs)

    @pytest.mark.asyncio
    async def test_list_handoffs_with_status_filter(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test filtering handoffs by status."""
        # Create handoffs
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        response1 = await handoff_service.initiate_handoff(request)
        response2 = await handoff_service.initiate_handoff(request)

        # Accept one
        await handoff_service.accept_handoff(response1.id)

        # Filter by pending
        pending = await handoff_service.list_handoffs(
            tenant_id,
            status=HandoffStatus.PENDING,
        )

        assert len(pending) == 1
        assert pending[0].id == response2.id

    @pytest.mark.asyncio
    async def test_get_pending_handoffs(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test getting only pending handoffs."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        await handoff_service.initiate_handoff(request)

        pending = await handoff_service.get_pending_handoffs(tenant_id)

        assert len(pending) >= 1
        assert all(h.status == HandoffStatus.PENDING for h in pending)

    @pytest.mark.asyncio
    async def test_get_active_handoff_for_session(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test getting active handoff for a session."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        response = await handoff_service.initiate_handoff(request)

        active = await handoff_service.get_active_handoff_for_session(session_id)

        assert active is not None
        assert active.id == response.id

    @pytest.mark.asyncio
    async def test_no_active_handoff_after_completion(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        """Test that completed handoff is not returned as active."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        response = await handoff_service.initiate_handoff(request)
        await handoff_service.complete_handoff(response.id)

        active = await handoff_service.get_active_handoff_for_session(session_id)

        assert active is None

    def test_configure_webhook(
        self,
        handoff_service,
        tenant_id,
    ):
        """Test configuring webhook through service."""
        config = WebhookConfig(
            url="https://example.com/webhook",
            secret="test-secret",
        )

        handoff_service.configure_webhook(tenant_id, config)

        retrieved = handoff_service.get_webhook_config(tenant_id)
        assert retrieved is not None
        assert retrieved.url == config.url

    def test_remove_webhook_config(
        self,
        handoff_service,
        tenant_id,
    ):
        """Test removing webhook config through service."""
        config = WebhookConfig(url="https://example.com/webhook")
        handoff_service.configure_webhook(tenant_id, config)

        result = handoff_service.remove_webhook_config(tenant_id)

        assert result is True
        assert handoff_service.get_webhook_config(tenant_id) is None


# ============================================
# Model Tests
# ============================================


class TestModels:
    """Tests for data models."""

    def test_webhook_payload_to_dict(self, tenant_id, session_id):
        """Test WebhookPayload serialization."""
        payload = WebhookPayload(
            event_type="handoff.pending",
            handoff_id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            status=HandoffStatus.PENDING,
            urgency=HandoffUrgency.HIGH,
            reason=HandoffReason.SAFETY_CONCERN,
            summary="Test summary",
            turn_count=5,
            funnel_stage="consideration",
        )

        data = payload.to_dict()

        assert isinstance(data, dict)
        assert data["event_type"] == "handoff.pending"
        assert "timestamp" in data

    def test_conversation_message_to_prompt_format(self):
        """Test ConversationMessage prompt format conversion."""
        from src.handoff.models import ConversationMessage
        
        msg = ConversationMessage(
            role="user",
            content="Hello",
            timestamp=datetime.now(timezone.utc),
        )

        # Verify it has expected attributes
        assert msg.role == "user"
        assert msg.content == "Hello"

    def test_key_information_model(self):
        """Test KeyInformation model."""
        info = KeyInformation(
            user_name="João",
            contact_info="11 98765-4321",
            main_topic="agendamento",
            symptoms_mentioned=["dor", "febre"],
            appointment_preferences="manhã",
        )

        assert info.user_name == "João"
        assert len(info.symptoms_mentioned) == 2

    def test_handoff_request_defaults(self, tenant_id, session_id):
        """Test HandoffRequest default values."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )

        assert request.urgency == HandoffUrgency.NORMAL
        assert request.funnel_stage == "unknown"
        assert request.requested_by == "system"

    def test_webhook_config_defaults(self):
        """Test WebhookConfig default values."""
        config = WebhookConfig(url="https://example.com/webhook")

        assert config.timeout_seconds == 30
        assert config.retry_count == 3
        assert config.enabled is True


# ============================================
# Integration Tests
# ============================================


class TestHandoffIntegration:
    """Integration tests for handoff flow."""

    @pytest.mark.asyncio
    async def test_full_handoff_flow(
        self,
        handoff_service,
        tenant_id,
        session_id,
        sample_messages,
    ):
        """Test complete handoff flow from creation to completion."""
        # 1. Initiate handoff
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
            urgency=HandoffUrgency.NORMAL,
            recent_messages=sample_messages,
        )
        response = await handoff_service.initiate_handoff(request)
        assert response.status == HandoffStatus.PENDING

        # 2. Agent accepts
        updated = await handoff_service.accept_handoff(
            response.id,
            agent_id="agent-456",
        )
        assert updated.status == HandoffStatus.ACCEPTED

        # 3. Agent completes
        final = await handoff_service.complete_handoff(
            response.id,
            notes="Issue resolved",
        )
        assert final.status == HandoffStatus.COMPLETED
        assert final.resolved_at is not None

        # 4. Verify no active handoff
        active = await handoff_service.get_active_handoff_for_session(session_id)
        assert active is None

    @pytest.mark.asyncio
    async def test_handoff_with_webhook(
        self,
        tenant_id,
        session_id,
    ):
        """Test handoff with webhook notification."""
        dispatcher = WebhookDispatcher()
        service = HandoffService(
            webhook_dispatcher=dispatcher,
        )

        # Configure webhook with at least 1 retry to enter the loop
        config = WebhookConfig(
            url="https://example.com/webhook",
            enabled=True,
            retry_count=1,  # At least 1 to enter the retry loop
        )
        service.configure_webhook(tenant_id, config)

        # Mock HTTP client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"received": true}'

        with patch.object(dispatcher, '_get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            request = HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
            response = await service.initiate_handoff(request)

            assert response.webhook_sent is True
            assert response.webhook_status == WebhookStatus.SENT
