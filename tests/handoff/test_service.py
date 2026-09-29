"""
Tests for the Human Handoff Service.

Tests cover:
- ConversationSummarizer
- WebhookDispatcher
- HandoffService
- API endpoints
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.exceptions import (
    HandoffNotFoundError,
    HandoffServiceError,
    WebhookConfigError,
    WebhookError,
)
from src.handoff.models import (
    ConversationSummary,
    HandoffListItem,
    HandoffReason,
    HandoffRequest,
    HandoffResponse,
    HandoffStatus,
    HandoffUpdate,
    HandoffUrgency,
    KeyInformation,
    WebhookConfig,
    WebhookDelivery,
    WebhookPayload,
    WebhookStatus,
)
from src.handoff.summarizer import ConversationSummarizer
from src.handoff.webhook import WebhookDispatcher
from src.models.base import Base
from src.models.clinic_config_table import ClinicConfigTable
from src.models.handoff import HandoffRecord
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.database import _patch_jsonb_for_sqlite
from src.repositories.handoff import HandoffRecordRepository
from src.services import HandoffService
from src.services import handoff_service as handoff_service_module
from src.services.handoff_service import (
    HANDOFF_OPERATIONS,
    HandoffLifecycleError,
    HandoffPostCommitError,
    handoff_record_to_detail,
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


@pytest.fixture(scope="module")
async def handoff_session_factory():
    _patch_jsonb_for_sqlite()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
async def persisted_handoff_session(
    handoff_session_factory,
    tenant_id,
    session_id,
):
    async with handoff_session_factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"Clinic {tenant_id}",
                    slug=f"clinic-{tenant_id}",
                    api_key_hash=f"hash-{tenant_id}",
                    config={},
                )
            )
            session.add(
                Session(
                    id=session_id,
                    tenant_id=tenant_id,
                    channel="test",
                    cognitive_state={},
                    session_metadata={},
                )
            )


@pytest.fixture
def handoff_service(webhook_dispatcher, handoff_session_factory):
    return HandoffService(
        event_store=None,  # No event store for unit tests
        summarizer=ConversationSummarizer(),
        webhook_dispatcher=webhook_dispatcher,
        session_factory=handoff_session_factory,
    )


@pytest.fixture(autouse=True)
def default_event_store(monkeypatch):
    """Keep unit tests isolated while exercising the runtime lazy resolver."""

    event_store = MagicMock()
    event_store.append = AsyncMock()
    event_store.get_recent_messages = AsyncMock(return_value=[])
    resolver = AsyncMock(return_value=event_store)
    monkeypatch.setattr(handoff_service_module, "get_event_store", resolver)
    return resolver, event_store


@pytest.mark.asyncio
async def test_runtime_service_resolves_event_store_lazily(
    default_event_store,
    monkeypatch,
) -> None:
    resolver, event_store = default_event_store
    monkeypatch.setattr(handoff_service_module, "_service", None)

    service = handoff_service_module.get_handoff_service()

    assert service.event_store is None
    resolver.assert_not_awaited()

    messages = await service._recent_messages(uuid4(), uuid4())

    assert messages == []
    resolver.assert_awaited_once_with()
    assert service.event_store is event_store


def test_legacy_service_module_is_a_canonical_compatibility_shim() -> None:
    from src.handoff.service import HandoffService as CompatibilityService
    from src.handoff.service import get_handoff_service as compatibility_factory

    assert CompatibilityService is HandoffService
    assert compatibility_factory is handoff_service_module.get_handoff_service
    assert not hasattr(CompatibilityService(event_store=MagicMock()), "_handoffs")


def test_handoff_record_exposes_no_public_mutators_that_bypass_cas() -> None:
    bypasses = {
        "checkout",
        "accept",
        "start_handling",
        "complete",
        "cancel",
        "timeout",
        "mark_webhook_sent",
    }

    assert bypasses.isdisjoint(vars(HandoffRecord))


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

        assert (
            "dor de cabeça" in summary.key_info.symptoms_mentioned
            or "dor" in summary.key_info.symptoms_mentioned
        )
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

        with patch.object(webhook_dispatcher, "_get_client") as mock_get_client:
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

        with patch.object(webhook_dispatcher, "_get_client") as mock_get_client:
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
    @pytest.mark.parametrize(
        ("safety_concern", "spoofed_safety"),
        [(None, True), (True, False), (False, True)],
    )
    async def test_persisted_handoff_uses_only_typed_canonical_signals(
        self,
        handoff_service,
        handoff_session_factory,
        tenant_id,
        session_id,
        safety_concern,
        spoofed_safety,
    ):
        """Typed intent and safety win over unrelated reason and key_info."""
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
            detected_intent="pos_atendimento_sintomas_preocupantes",
            safety_concern=safety_concern,
            key_info={
                "topic": "follow_up",
                "safety_concern": spoofed_safety,
            },
        )

        response = await handoff_service.initiate_handoff(request)

        async with handoff_session_factory() as db:
            persisted = await db.get(HandoffRecord, response.id)
        assert persisted is not None
        assert persisted.reason == "user_request"
        assert persisted.intencao_detectada == "pos_atendimento_sintomas_preocupantes"
        assert persisted.context["key_info"]["topic"] == "follow_up"
        if safety_concern is None:
            assert "safety_concern" not in persisted.context
        else:
            assert persisted.context["safety_concern"] is safety_concern

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
        detail = await handoff_service.get_handoff(
            response.id,
            tenant_id=tenant_id,
        )

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
            tenant_id=tenant_id,
        )
        detail = await handoff_service.get_handoff(
            response.id,
            tenant_id=tenant_id,
        )
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
        handoff = await handoff_service.get_handoff(
            response.id,
            tenant_id=tenant_id,
        )

        assert handoff.id == response.id
        assert handoff.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_get_nonexistent_handoff(self, handoff_service, tenant_id):
        """Test getting a handoff that doesn't exist."""
        with pytest.raises(HandoffNotFoundError):
            await handoff_service.get_handoff(uuid4(), tenant_id=tenant_id)

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
            tenant_id=tenant_id,
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
        await handoff_service.accept_handoff(response.id, tenant_id=tenant_id)
        await handoff_service.confirm_handoff(response.id, tenant_id=tenant_id)

        # Complete it
        updated = await handoff_service.complete_handoff(
            response.id,
            notes="Resolved successfully",
            tenant_id=tenant_id,
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
            tenant_id=tenant_id,
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

        updated = await handoff_service.timeout_handoff(
            response.id,
            tenant_id=tenant_id,
        )

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
        await handoff_service.accept_handoff(response1.id, tenant_id=tenant_id)

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

        active = await handoff_service.get_active_handoff_for_session(
            session_id,
            tenant_id=tenant_id,
        )

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
        await handoff_service.confirm_handoff(response.id, tenant_id=tenant_id)
        await handoff_service.complete_handoff(response.id, tenant_id=tenant_id)

        active = await handoff_service.get_active_handoff_for_session(
            session_id,
            tenant_id=tenant_id,
        )

        assert active is None

    @pytest.mark.asyncio
    async def test_confirmation_is_write_once_and_completion_requires_it(
        self,
        handoff_service,
        handoff_session_factory,
        tenant_id,
        session_id,
    ):
        response = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )

        with pytest.raises(HandoffLifecycleError) as not_confirmed:
            await handoff_service.complete_handoff(
                response.id,
                tenant_id=tenant_id,
            )
        assert not_confirmed.value.code == "attendance_not_confirmed"

        first = await handoff_service.confirm_handoff(
            response.id,
            tenant_id=tenant_id,
        )
        with pytest.raises(HandoffLifecycleError) as repeated:
            await handoff_service.confirm_handoff(
                response.id,
                tenant_id=tenant_id,
            )
        assert repeated.value.code == "attendance_already_confirmed"

        async with handoff_session_factory() as database:
            record = await database.get(HandoffRecord, response.id)
        assert record is not None
        assert record.lifecycle_version == 1
        assert record.data_hora_saida is not None
        persisted_confirmation = record.data_hora_saida.replace(tzinfo=timezone.utc)
        assert persisted_confirmation == first.data_hora_saida
        assert record.updated_at is not None

    @pytest.mark.asyncio
    async def test_confirmation_uses_one_database_timestamp_for_all_fields(
        self,
        handoff_service,
        handoff_session_factory,
        monkeypatch,
        tenant_id,
        session_id,
    ):
        response = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        database_now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        clock = AsyncMock(return_value=database_now)
        monkeypatch.setattr(HandoffRecordRepository, "current_timestamp", clock)

        confirmed = await handoff_service.confirm_handoff(
            response.id,
            tenant_id=tenant_id,
        )

        async with handoff_session_factory() as database:
            record = await database.get(HandoffRecord, response.id)
        assert record is not None
        persisted_confirmation = record.data_hora_saida
        persisted_updated = record.updated_at
        assert persisted_confirmation is not None
        assert persisted_updated is not None
        assert persisted_confirmation.replace(tzinfo=timezone.utc) == database_now
        assert persisted_updated.replace(tzinfo=timezone.utc) == database_now
        assert confirmed.data_hora_saida == database_now
        clock.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_generic_update_cannot_bypass_attendance_confirmation(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        response = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )

        with pytest.raises(HandoffLifecycleError) as rejected:
            await handoff_service.update_handoff(
                HandoffUpdate(
                    handoff_id=response.id,
                    status=HandoffStatus.IN_PROGRESS,
                ),
                tenant_id=tenant_id,
            )

        assert rejected.value.code == "attendance_confirmation_required"
        detail = await handoff_service.get_handoff(
            response.id,
            tenant_id=tenant_id,
        )
        assert detail.status == HandoffStatus.PENDING
        assert detail.updated_at is None

    @pytest.mark.asyncio
    async def test_cross_tenant_mutation_is_not_found_and_does_not_write(
        self,
        handoff_service,
        handoff_session_factory,
        tenant_id,
        session_id,
    ):
        response = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )

        with pytest.raises(HandoffNotFoundError):
            await handoff_service.cancel_handoff(
                response.id,
                tenant_id=uuid4(),
            )

        async with handoff_session_factory() as database:
            record = await database.get(HandoffRecord, response.id)
        assert record is not None
        assert record.status == HandoffStatus.PENDING.value
        assert record.lifecycle_version == 0
        assert record.updated_at is None

    @pytest.mark.asyncio
    async def test_session_must_belong_to_tenant_before_insert(
        self,
        handoff_service,
        handoff_session_factory,
        session_id,
    ):
        wrong_tenant = uuid4()
        with pytest.raises(HandoffServiceError, match="handoff_session_not_found"):
            await handoff_service.initiate_handoff(
                HandoffRequest(
                    tenant_id=wrong_tenant,
                    session_id=session_id,
                    reason=HandoffReason.USER_REQUEST,
                )
            )

        async with handoff_session_factory() as database:
            records = list(
                (
                    await database.scalars(
                        select(HandoffRecord).where(
                            HandoffRecord.tenant_id == wrong_tenant
                        )
                    )
                ).all()
            )
        assert records == []

    @pytest.mark.asyncio
    async def test_event_store_failure_surfaces_after_sql_commit(
        self,
        webhook_dispatcher,
        handoff_session_factory,
        tenant_id,
        session_id,
    ):
        event_store = MagicMock()
        event_store.append = AsyncMock(side_effect=RuntimeError("event unavailable"))
        service = HandoffService(
            event_store=event_store,
            summarizer=ConversationSummarizer(),
            webhook_dispatcher=webhook_dispatcher,
            session_factory=handoff_session_factory,
        )

        with pytest.raises(HandoffPostCommitError):
            await service.initiate_handoff(
                HandoffRequest(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    reason=HandoffReason.USER_REQUEST,
                )
            )

        async with handoff_session_factory() as database:
            records = list(
                (
                    await database.scalars(
                        select(HandoffRecord).where(
                            HandoffRecord.tenant_id == tenant_id
                        )
                    )
                ).all()
            )
        assert len(records) == 1
        assert records[0].status == HandoffStatus.PENDING.value

    @pytest.mark.asyncio
    async def test_transition_event_failure_stops_later_effects_after_commit(
        self,
        webhook_dispatcher,
        handoff_session_factory,
        tenant_id,
        session_id,
    ):
        event_store = MagicMock()
        event_store.append = AsyncMock()
        event_store.get_recent_messages = AsyncMock(return_value=[])
        service = HandoffService(
            event_store=event_store,
            summarizer=ConversationSummarizer(),
            webhook_dispatcher=webhook_dispatcher,
            session_factory=handoff_session_factory,
        )
        response = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        event_store.append.reset_mock()
        event_store.append.side_effect = RuntimeError("event unavailable")
        webhook_dispatcher.set_config(
            tenant_id,
            WebhookConfig(url="https://example.test/handoff"),
        )
        webhook_dispatcher.send = AsyncMock()
        metric = HANDOFF_OPERATIONS.labels(operation="update", status="accepted")
        metric_before = metric._value.get()
        error_metric = HANDOFF_OPERATIONS.labels(operation="update", status="error")
        error_before = error_metric._value.get()

        with pytest.raises(HandoffPostCommitError):
            await service.accept_handoff(response.id, tenant_id=tenant_id)

        async with handoff_session_factory() as database:
            record = await database.get(HandoffRecord, response.id)
        assert record is not None
        assert record.status == HandoffStatus.ACCEPTED.value
        assert record.lifecycle_version == 1
        assert event_store.append.await_count == 1
        event_store.get_recent_messages.assert_not_awaited()
        webhook_dispatcher.send.assert_not_awaited()
        assert metric._value.get() == metric_before
        assert error_metric._value.get() == error_before + 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure_mode",
        ["failed-delivery", "raised-delivery"],
    )
    async def test_transition_webhook_failures_follow_nonfatal_policy(
        self,
        failure_mode,
        webhook_dispatcher,
        handoff_session_factory,
        tenant_id,
        session_id,
    ):
        event_store = MagicMock()
        event_store.append = AsyncMock()
        event_store.get_recent_messages = AsyncMock(return_value=[])
        service = HandoffService(
            event_store=event_store,
            summarizer=ConversationSummarizer(),
            webhook_dispatcher=webhook_dispatcher,
            session_factory=handoff_session_factory,
        )
        response = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        event_store.append.reset_mock()
        webhook_dispatcher.set_config(
            tenant_id,
            WebhookConfig(url="https://example.test/handoff"),
        )
        if failure_mode == "failed-delivery":
            webhook_dispatcher.send = AsyncMock(
                return_value=WebhookDelivery(
                    handoff_id=response.id,
                    tenant_id=tenant_id,
                    url="https://example.test/handoff",
                    status=WebhookStatus.FAILED,
                    response_status=503,
                    response_body="remote private response",
                    error_message="remote rejected",
                )
            )
        elif failure_mode == "raised-delivery":
            webhook_dispatcher.send = AsyncMock(
                side_effect=WebhookError("delivery failed")
            )
        metric = HANDOFF_OPERATIONS.labels(operation="update", status="accepted")
        metric_before = metric._value.get()

        updated = await service.accept_handoff(response.id, tenant_id=tenant_id)

        async with handoff_session_factory() as database:
            record = await database.get(HandoffRecord, response.id)
        assert updated.status == HandoffStatus.ACCEPTED
        assert record is not None
        assert record.status == HandoffStatus.ACCEPTED.value
        assert record.lifecycle_version == 1
        assert record.webhook_sent is False
        assert event_store.append.await_count == 1
        event_store.get_recent_messages.assert_awaited_once_with(
            tenant_id,
            session_id,
            limit=5,
        )
        assert webhook_dispatcher.send.await_count == 1
        assert metric._value.get() - metric_before == 1
        assert record.context["webhook_status"] == WebhookStatus.FAILED.value
        expected_response = {
            "status": "failed",
            "attempt": 1,
            "error_type": "delivery_failed",
        }
        if failure_mode == "failed-delivery":
            expected_response["response_status"] = 503
        assert record.webhook_response == expected_response
        assert "delivery failed" not in str(record.webhook_response)
        assert "remote rejected" not in str(record.webhook_response)
        assert "remote private response" not in str(record.webhook_response)

    @pytest.mark.asyncio
    async def test_webhook_failure_is_nonfatal_and_bookkeeping_is_not_lifecycle(
        self,
        webhook_dispatcher,
        handoff_session_factory,
        tenant_id,
        session_id,
    ):
        webhook_dispatcher.set_config(
            tenant_id,
            WebhookConfig(url="https://example.test/handoff"),
        )
        webhook_dispatcher.send = AsyncMock(side_effect=WebhookError("delivery failed"))
        service = HandoffService(
            summarizer=ConversationSummarizer(),
            webhook_dispatcher=webhook_dispatcher,
            session_factory=handoff_session_factory,
        )

        response = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )

        assert response.webhook_sent is False
        assert response.webhook_status == WebhookStatus.FAILED
        async with handoff_session_factory() as database:
            record = await database.get(HandoffRecord, response.id)
        assert record is not None
        assert record.context["webhook_status"] == WebhookStatus.FAILED.value
        assert record.webhook_response is not None
        assert record.webhook_response == {
            "status": "failed",
            "attempt": 1,
            "error_type": "delivery_failed",
        }
        assert "delivery failed" not in str(record.webhook_response)
        assert record.lifecycle_version == 0
        assert record.updated_at is None

    @pytest.mark.asyncio
    async def test_transition_preserves_event_then_webhook_order_and_payload(
        self,
        monkeypatch,
        handoff_session_factory,
        tenant_id,
        session_id,
    ):
        order: list[str] = []

        class TrackingTransaction:
            def __init__(self, transaction):
                self.transaction = transaction

            async def __aenter__(self):
                return await self.transaction.__aenter__()

            async def __aexit__(self, exc_type, exc_value, traceback):
                result = await self.transaction.__aexit__(
                    exc_type,
                    exc_value,
                    traceback,
                )
                if exc_type is None:
                    order.append("commit")
                return result

        class TrackingSession:
            def __init__(self, session):
                self.session = session

            async def __aenter__(self):
                await self.session.__aenter__()
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return await self.session.__aexit__(
                    exc_type,
                    exc_value,
                    traceback,
                )

            def begin(self):
                return TrackingTransaction(self.session.begin())

            def __getattr__(self, name):
                return getattr(self.session, name)

        event_store = MagicMock()

        async def append_event(event):
            order.append("event")

        event_store.append = AsyncMock(side_effect=append_event)
        event_store.get_recent_messages = AsyncMock(
            return_value=[
                handoff_service_module.EventData(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_type=handoff_service_module.EventType.MESSAGE_IN.value,
                    event_data={"role": "user", "content": "project once"},
                )
            ]
        )
        dispatcher = WebhookDispatcher()
        dispatcher.set_config(
            tenant_id,
            WebhookConfig(url="https://example.test/handoff"),
        )

        async def send_webhook(delivery_tenant, payload):
            order.append("webhook")
            return WebhookDelivery(
                handoff_id=payload.handoff_id,
                tenant_id=delivery_tenant,
                url="https://example.test/handoff",
                status=WebhookStatus.SENT,
            )

        dispatcher.send = AsyncMock(side_effect=send_webhook)
        original_bookkeeping = HandoffRecordRepository.update_webhook_delivery

        async def tracked_bookkeeping(repository, **kwargs):
            order.append("bookkeeping")
            return await original_bookkeeping(repository, **kwargs)

        monkeypatch.setattr(
            HandoffRecordRepository,
            "update_webhook_delivery",
            tracked_bookkeeping,
        )
        service = HandoffService(
            event_store=event_store,
            summarizer=ConversationSummarizer(),
            webhook_dispatcher=dispatcher,
            session_factory=lambda: TrackingSession(handoff_session_factory()),
        )
        response = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        order.clear()
        event_store.get_recent_messages.reset_mock()

        detail = await service.accept_handoff(
            response.id,
            agent_id="agent-order",
            tenant_id=tenant_id,
        )

        assert order == ["commit", "event", "webhook", "bookkeeping", "commit"]
        event_store.get_recent_messages.assert_awaited_once_with(
            tenant_id,
            session_id,
            limit=5,
        )
        event = event_store.append.await_args.args[0]
        payload = dispatcher.send.await_args.args[1]
        assert event.event_type == "handoff.accept"
        assert event.event_data == {
            "handoff_id": str(response.id),
            "old_status": "pending",
            "new_status": "accepted",
            "assigned_to": "agent-order",
        }
        assert payload.event_type == "handoff.accepted"
        assert payload.status == HandoffStatus.ACCEPTED
        assert payload.recent_messages == detail.recent_messages
        assert [message["content"] for message in detail.recent_messages] == [
            "project once"
        ]

    @pytest.mark.asyncio
    async def test_bookkeeping_failure_raises_after_exactly_one_send(
        self,
        monkeypatch,
        webhook_dispatcher,
        handoff_session_factory,
        tenant_id,
        session_id,
    ):
        event_store = MagicMock()
        event_store.append = AsyncMock()
        event_store.get_recent_messages = AsyncMock(return_value=[])
        service = HandoffService(
            event_store=event_store,
            summarizer=ConversationSummarizer(),
            webhook_dispatcher=webhook_dispatcher,
            session_factory=handoff_session_factory,
        )
        response = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        event_store.append.reset_mock()
        webhook_dispatcher.set_config(
            tenant_id,
            WebhookConfig(url="https://example.test/handoff"),
        )
        webhook_dispatcher.send = AsyncMock(
            return_value=WebhookDelivery(
                handoff_id=response.id,
                tenant_id=tenant_id,
                url="https://example.test/handoff",
                status=WebhookStatus.SENT,
            )
        )
        monkeypatch.setattr(
            HandoffRecordRepository,
            "update_webhook_delivery",
            AsyncMock(side_effect=RuntimeError("bookkeeping unavailable")),
        )
        success_metric = HANDOFF_OPERATIONS.labels(
            operation="update",
            status="accepted",
        )
        error_metric = HANDOFF_OPERATIONS.labels(operation="update", status="error")
        success_before = success_metric._value.get()
        error_before = error_metric._value.get()

        with pytest.raises(HandoffPostCommitError):
            await service.accept_handoff(response.id, tenant_id=tenant_id)

        async with handoff_session_factory() as database:
            record = await database.get(HandoffRecord, response.id)
        assert record is not None
        assert record.status == HandoffStatus.ACCEPTED.value
        assert record.lifecycle_version == 1
        assert record.webhook_response is None
        webhook_dispatcher.send.assert_awaited_once()
        assert success_metric._value.get() == success_before
        assert error_metric._value.get() == error_before + 1

    @pytest.mark.asyncio
    async def test_sla_snapshot_is_immutable_across_config_changes(
        self,
        handoff_service,
        handoff_session_factory,
        tenant_id,
        session_id,
    ):
        async with handoff_session_factory() as database:
            async with database.begin():
                database.add(
                    ClinicConfigTable(
                        tenant_id=tenant_id,
                        handoff={"meta_minutos_atendimento": 10},
                    )
                )
        first = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        async with handoff_session_factory() as database:
            async with database.begin():
                config = await database.scalar(
                    select(ClinicConfigTable).where(
                        ClinicConfigTable.tenant_id == tenant_id
                    )
                )
                assert config is not None
                config.handoff = {"meta_minutos_atendimento": 5}
        second = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )

        async with handoff_session_factory() as database:
            first_record = await database.get(HandoffRecord, first.id)
            second_record = await database.get(HandoffRecord, second.id)
        assert first_record is not None
        assert second_record is not None
        assert first_record.sla_target_seconds == 600
        assert second_record.sla_target_seconds == 300

    def test_mapper_tolerates_legacy_context_and_reason(self, tenant_id, session_id):
        created_at = datetime.now(timezone.utc)
        record = HandoffRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            reason="operator_takeover",
            summary="legacy",
            context={"legacy_key": "value", "verification_status": "legacy"},
            urgency="unknown",
            status="pending",
            webhook_sent=False,
            created_at=created_at,
        )

        detail = handoff_record_to_detail(record)

        assert detail.reason == HandoffReason.USER_REQUEST
        assert detail.urgency == HandoffUrgency.NORMAL
        assert detail.key_info == {"legacy_key": "value"}
        assert detail.verification_status == "legacy"
        assert detail.updated_at is None

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
        assert request.detected_intent is None
        assert request.safety_concern is None

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
            tenant_id=tenant_id,
        )
        assert updated.status == HandoffStatus.ACCEPTED

        # 3. Agent completes
        await handoff_service.confirm_handoff(response.id, tenant_id=tenant_id)
        final = await handoff_service.complete_handoff(
            response.id,
            notes="Issue resolved",
            tenant_id=tenant_id,
        )
        assert final.status == HandoffStatus.COMPLETED
        assert final.resolved_at is not None

        # 4. Verify no active handoff
        active = await handoff_service.get_active_handoff_for_session(
            session_id,
            tenant_id=tenant_id,
        )
        assert active is None

    @pytest.mark.asyncio
    async def test_handoff_with_webhook(
        self,
        tenant_id,
        session_id,
        handoff_session_factory,
    ):
        """Test handoff with webhook notification."""
        dispatcher = WebhookDispatcher()
        service = HandoffService(
            webhook_dispatcher=dispatcher,
            session_factory=handoff_session_factory,
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

        with patch.object(dispatcher, "_get_client") as mock_get_client:
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
