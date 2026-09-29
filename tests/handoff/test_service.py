"""
Tests for the Human Handoff Service.

Tests cover:
- ConversationSummarizer
- WebhookDispatcher
- HandoffService
- API endpoints
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

import src.services.handoff_service as handoff_service_module
from src.core.exceptions import HandoffNotFoundError, WebhookConfigError, WebhookError
from src.handoff.models import (
    ConversationSummary,
    HandoffListItem,
    HandoffReason,
    HandoffRequest,
    HandoffResponse,
    HandoffSector,
    HandoffSLA,
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
from src.memory.event_store import EventData, EventType
from src.models.handoff import HandoffRecord
from src.repositories.handoff import (
    HandoffCasOutcome,
    HandoffCasResult,
    HandoffLookupOutcome,
    HandoffLookupResult,
    HandoffRecordRepository,
    HandoffSlaConfiguration,
)
from src.services import HandoffService
from src.services.handoff_service import (
    HANDOFF_OPERATIONS,
    HandoffConflictError,
    HandoffPersistenceError,
    HandoffPostCommitError,
    HandoffSlaDataError,
    HandoffTransitionError,
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


@dataclass
class _FakeHandoffStore:
    records: dict[UUID, HandoffRecord] = field(default_factory=dict)
    configured_minutes: object = 10
    conflict_next: bool = False
    fail_insert: bool = False
    fail_bookkeeping: bool = False
    order: list[str] = field(default_factory=list)
    db_now: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class _FakeSession:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        self.store.order.append("commit")
        return None

    async def rollback(self):
        return None


class _FakeHandoffRepository:
    def __init__(self, store: _FakeHandoffStore):
        self.store = store

    async def load_sla_configuration(self, _tenant_id):
        return HandoffSlaConfiguration(self.store.configured_minutes, "fake")

    async def create_record(self, record):
        if self.store.fail_insert:
            raise RuntimeError("insert failed")
        record.created_at = datetime.now(timezone.utc)
        self.store.records[record.id] = record
        return record

    async def current_timestamp(self):
        return self.store.db_now

    async def lookup(self, tenant_id, handoff_id):
        record = self.store.records.get(handoff_id)
        if record is None or record.tenant_id != tenant_id:
            return HandoffLookupResult(HandoffLookupOutcome.NOT_FOUND)
        return HandoffLookupResult(HandoffLookupOutcome.FOUND, record)

    async def find_by_tenant(
        self,
        tenant_id,
        status=None,
        skip=0,
        limit=None,
    ):
        records = [
            record
            for record in self.store.records.values()
            if record.tenant_id == tenant_id
            and (status is None or record.status == status)
        ]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records[skip : None if limit is None else skip + limit]

    async def find_active_for_session(self, tenant_id, session_id):
        records = [
            record
            for record in self.store.records.values()
            if record.session_id == session_id
            and record.tenant_id == tenant_id
            and record.status in {"pending", "accepted", "in_progress"}
        ]
        if not records:
            return HandoffLookupResult(HandoffLookupOutcome.NOT_FOUND)
        records.sort(key=lambda record: record.created_at, reverse=True)
        return HandoffLookupResult(HandoffLookupOutcome.FOUND, records[0])

    async def accept_handoff(self, **kwargs):
        return self._cas(
            kwargs,
            status="accepted",
            assigned_to=kwargs["agent_id"],
        )

    async def confirm_handoff(self, **kwargs):
        record = self.store.records[kwargs["handoff_id"]]
        if record.data_hora_saida is not None:
            return HandoffCasResult(HandoffCasOutcome.CONFLICT)
        values = {
            "status": "in_progress",
            "data_hora_saida": kwargs["confirmed_at"],
        }
        if kwargs["agent_id"] is not None:
            values["assigned_to"] = kwargs["agent_id"]
        return self._cas(kwargs, **values)

    async def complete_handoff(self, **kwargs):
        record = self.store.records[kwargs["handoff_id"]]
        if record.data_hora_saida is None:
            return HandoffCasResult(HandoffCasOutcome.CONFLICT)
        return self._cas(
            kwargs,
            status="completed",
            resolved_at=kwargs["changed_at"],
            resolution_notes=kwargs["notes"],
        )

    async def cancel_handoff(self, **kwargs):
        return self._cas(
            kwargs,
            status="cancelled",
            resolved_at=kwargs["changed_at"],
            resolution_notes=kwargs["notes"],
        )

    async def timeout_handoff(self, **kwargs):
        return self._cas(
            kwargs,
            status="timeout",
            resolved_at=kwargs["changed_at"],
        )

    async def update_verification(self, **kwargs):
        return self._cas(kwargs, context=kwargs["context"])

    async def record_webhook_delivery(
        self,
        *,
        tenant_id,
        handoff_id,
        sent,
        response,
    ):
        self.store.order.append("bookkeeping")
        if self.store.fail_bookkeeping:
            raise RuntimeError("bookkeeping failed")
        record = self.store.records.get(handoff_id)
        if record is None or record.tenant_id != tenant_id:
            return False
        record.webhook_sent = sent
        record.webhook_response = response
        return True

    def _cas(self, kwargs: dict[str, Any], **values: Any):
        if self.store.conflict_next:
            self.store.conflict_next = False
            return HandoffCasResult(HandoffCasOutcome.CONFLICT)
        record = self.store.records.get(kwargs["handoff_id"])
        if (
            record is None
            or record.tenant_id != kwargs["tenant_id"]
            or record.status != kwargs["observed_status"]
            or record.lifecycle_version != kwargs["observed_lifecycle_version"]
        ):
            return HandoffCasResult(HandoffCasOutcome.CONFLICT)
        for key, value in values.items():
            setattr(record, key, value)
        changed_at = kwargs.get("changed_at") or kwargs.get("confirmed_at")
        record.updated_at = changed_at
        record.lifecycle_version += 1
        return HandoffCasResult(HandoffCasOutcome.APPLIED, record)


class _FakeEventStore:
    def __init__(self, recent_messages=None, append_error=None, order=None):
        self.recent_messages = recent_messages or []
        self.append_error = append_error
        self.appended = []
        self.append_calls = 0
        self.recent_calls = []
        self.order = order

    async def append(self, event):
        self.append_calls += 1
        if self.append_error is not None:
            raise self.append_error
        if self.order is not None:
            self.order.append("event")
        self.appended.append(event)
        return event

    async def get_recent_messages(self, tenant_id, session_id, limit):
        self.recent_calls.append((tenant_id, session_id, limit))
        return self.recent_messages


def _build_test_service(
    store: _FakeHandoffStore,
    webhook_dispatcher: WebhookDispatcher,
    *,
    event_store=None,
) -> HandoffService:
    return HandoffService(
        event_store=event_store,
        summarizer=ConversationSummarizer(),
        webhook_dispatcher=webhook_dispatcher,
        session_factory=lambda: _FakeSession(store),
        repository_factory=lambda _session: _FakeHandoffRepository(store),
    )


@pytest.fixture
def handoff_store():
    return _FakeHandoffStore()


@pytest.fixture
def handoff_service(webhook_dispatcher, handoff_store):
    return _build_test_service(handoff_store, webhook_dispatcher)


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


def test_legacy_service_path_is_canonical_identity_shim_without_lifecycle_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.handoff.service as legacy_service_module

    assert legacy_service_module.HandoffService is handoff_service_module.HandoffService
    assert (
        legacy_service_module.get_handoff_service
        is handoff_service_module.get_handoff_service
    )
    assert (
        legacy_service_module.close_handoff_service
        is handoff_service_module.close_handoff_service
    )
    assert "_service" not in vars(legacy_service_module)
    assert "_handoffs" not in vars(legacy_service_module)

    monkeypatch.setattr(handoff_service_module, "_service", None)
    canonical = handoff_service_module.get_handoff_service()
    assert legacy_service_module.get_handoff_service() is canonical
    assert not hasattr(canonical, "_handoffs")

    for mutator in (
        "checkout",
        "accept",
        "start_handling",
        "complete",
        "cancel",
        "timeout",
    ):
        assert not hasattr(HandoffRecord, mutator)


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
    async def test_queue_sla_wraps_corrupt_persisted_timestamps(
        self,
        tenant_id,
        session_id,
    ):
        created_at = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        corrupt = HandoffRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            reason="user_request",
            summary="corrupt persisted occurrence",
            context={},
            urgency="normal",
            status="completed",
            created_at=created_at,
            data_hora_saida=created_at - timedelta(seconds=1),
            sla_target_seconds=600,
        )
        repository = MagicMock()
        repository.load_clinic_timezone = AsyncMock(return_value="UTC")

        async def stream_corrupt(**_kwargs: object):
            yield corrupt

        repository.stream_sla_cohort = stream_corrupt
        service = HandoffService(
            session_factory=lambda: _FakeSession(_FakeHandoffStore()),
            repository_factory=lambda _session: repository,
        )

        with pytest.raises(HandoffSlaDataError) as error:
            await service.get_queue_sla(
                tenant_id,
                from_date=date(2026, 9, 28),
                to_date=date(2026, 9, 28),
                as_of=created_at,
            )

        assert str(error.value) == "invalid_handoff_timestamps"
        assert isinstance(error.value.__cause__, ValueError)

    def test_sla_cohort_statement_is_tenant_scoped_narrow_projection(
        self,
        tenant_id,
    ):
        statement = HandoffRecordRepository._sla_cohort_statement(
            tenant_id=tenant_id,
            start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_exclusive_utc=datetime(2026, 4, 2, tzinfo=timezone.utc),
        )

        assert [item["name"] for item in statement.column_descriptions] == [
            "id",
            "numero_whatsapp",
            "session_id",
            "context",
            "reason",
            "intencao_detectada",
            "estagio_atual",
            "created_at",
            "data_hora_saida",
            "sla_target_seconds",
            "status",
        ]
        sql = str(statement)
        assert "handoff_records.tenant_id" in sql
        assert sql.count("handoff_records.created_at") >= 3
        for excluded_column in (
            "summary",
            "primeira_linha_mensagem",
            "webhook_response",
            "resolution_notes",
        ):
            assert excluded_column not in sql

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
        handoff_store,
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

        persisted = handoff_store.records[response.id]
        assert persisted.reason == "user_request"
        assert persisted.intencao_detectada == "pos_atendimento_sintomas_preocupantes"
        assert persisted.context["key_info"]["extracted_slots"]["topic"] == "follow_up"
        assert "recent_messages" not in persisted.context
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
        detail = await handoff_service.get_handoff(response.id, tenant_id)

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
        detail = await handoff_service.get_handoff(response.id, tenant_id)
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
        handoff = await handoff_service.get_handoff(response.id, tenant_id)

        assert handoff.id == response.id
        assert handoff.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_get_nonexistent_handoff(self, handoff_service, tenant_id):
        """Test getting a handoff that doesn't exist."""
        with pytest.raises(HandoffNotFoundError):
            await handoff_service.get_handoff(uuid4(), tenant_id)

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
        # Create and confirm handoff
        request = HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
        )
        response = await handoff_service.initiate_handoff(request)
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

    @pytest.mark.asyncio
    async def test_restart_instances_share_only_postgres_source_of_truth(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        service_a = _build_test_service(handoff_store, webhook_dispatcher)
        service_b = _build_test_service(handoff_store, webhook_dispatcher)
        service_c = _build_test_service(handoff_store, webhook_dispatcher)

        created = await service_a.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        confirmed = await service_b.confirm_handoff(
            created.id,
            tenant_id=tenant_id,
        )
        reread = await service_c.get_handoff(created.id, tenant_id)

        assert confirmed.status is HandoffStatus.IN_PROGRESS
        assert reread.status is HandoffStatus.IN_PROGRESS
        assert reread.data_hora_saida == confirmed.data_hora_saida
        assert not hasattr(service_a, "_handoffs")

    @pytest.mark.asyncio
    async def test_default_session_provider_is_resolved_per_operation(
        self,
        monkeypatch,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        captured_during_construction = _FakeHandoffStore()
        current_provider_store = _FakeHandoffStore()
        monkeypatch.setattr(
            handoff_service_module,
            "get_db_session",
            lambda: _FakeSession(captured_during_construction),
        )
        service = HandoffService(
            event_store=None,
            webhook_dispatcher=webhook_dispatcher,
            repository_factory=lambda session: _FakeHandoffRepository(session.store),
        )
        monkeypatch.setattr(
            handoff_service_module,
            "get_db_session",
            lambda: _FakeSession(current_provider_store),
        )

        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )

        assert created.id in current_provider_store.records
        assert created.id not in captured_during_construction.records

    @pytest.mark.asyncio
    async def test_default_event_store_provider_appends_and_projects_lazily(
        self,
        monkeypatch,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        event_store = _FakeEventStore(
            recent_messages=[
                EventData(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_type=EventType.MESSAGE_IN.value,
                    event_data={"role": "user", "content": "projected"},
                )
            ]
        )
        provider = AsyncMock(return_value=event_store)
        monkeypatch.setattr(
            handoff_service_module.event_store_module,
            "get_event_store",
            provider,
        )

        def _no_application_engine():
            raise RuntimeError("application database not initialized")

        monkeypatch.setattr(
            handoff_service_module,
            "get_db_engine",
            _no_application_engine,
        )
        service = HandoffService(
            webhook_dispatcher=webhook_dispatcher,
            session_factory=lambda: _FakeSession(handoff_store),
            repository_factory=lambda _session: _FakeHandoffRepository(handoff_store),
        )

        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        detail = await service.get_handoff(created.id, tenant_id)

        assert [event.event_type for event in event_store.appended] == [
            EventType.HANDOFF_REQUEST.value
        ]
        assert [message["content"] for message in detail.recent_messages] == [
            "projected"
        ]
        assert event_store.recent_calls == [(tenant_id, session_id, 5)]
        assert provider.await_count == 2

    def test_late_event_store_injection_does_not_replace_existing_singleton(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(handoff_service_module, "_service", None)
        existing = handoff_service_module.get_handoff_service()

        late = handoff_service_module.get_handoff_service(_FakeEventStore())

        assert late is existing

    @pytest.mark.asyncio
    async def test_completion_requires_write_once_confirmation(
        self,
        handoff_service,
        handoff_store,
        tenant_id,
        session_id,
    ):
        created = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )

        with pytest.raises(HandoffTransitionError):
            await handoff_service.complete_handoff(
                created.id,
                tenant_id=tenant_id,
            )

        first = await handoff_service.confirm_handoff(
            created.id,
            tenant_id=tenant_id,
        )
        with pytest.raises(HandoffConflictError):
            await handoff_service.confirm_handoff(
                created.id,
                tenant_id=tenant_id,
            )
        reread = await handoff_service.get_handoff(created.id, tenant_id)
        assert reread.data_hora_saida == first.data_hora_saida
        assert handoff_store.records[created.id].lifecycle_version == 1

    @pytest.mark.asyncio
    async def test_legacy_in_progress_without_confirmation_can_confirm_once(
        self,
        handoff_service,
        handoff_store,
        tenant_id,
        session_id,
    ):
        created = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        legacy = handoff_store.records[created.id]
        legacy.status = HandoffStatus.IN_PROGRESS.value
        legacy.data_hora_saida = None

        confirmed = await handoff_service.confirm_handoff(
            created.id,
            tenant_id=tenant_id,
        )

        assert confirmed.status is HandoffStatus.IN_PROGRESS
        assert confirmed.data_hora_saida is not None
        assert legacy.lifecycle_version == 1

    @pytest.mark.asyncio
    async def test_confirmation_uses_one_database_timestamp_for_mutation_fields(
        self,
        handoff_service,
        handoff_store,
        tenant_id,
        session_id,
    ):
        created = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        database_now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        handoff_store.db_now = database_now

        confirmed = await handoff_service.confirm_handoff(
            created.id,
            tenant_id=tenant_id,
        )

        persisted = handoff_store.records[created.id]
        assert confirmed.data_hora_saida == database_now
        assert persisted.data_hora_saida == database_now
        assert persisted.updated_at == database_now

    @pytest.mark.asyncio
    async def test_generic_update_cannot_manufacture_in_progress(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        created = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )

        with pytest.raises(HandoffTransitionError):
            await handoff_service.update_handoff(
                HandoffUpdate(
                    handoff_id=created.id,
                    status=HandoffStatus.IN_PROGRESS,
                ),
                tenant_id=tenant_id,
            )
        assert (await handoff_service.get_handoff(created.id, tenant_id)).status is (
            HandoffStatus.PENDING
        )

    @pytest.mark.asyncio
    async def test_cas_conflict_emits_no_public_effects_and_counts_conflict(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        event_store = _FakeEventStore()
        service = _build_test_service(
            handoff_store,
            webhook_dispatcher,
            event_store=event_store,
        )
        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        service.configure_webhook(
            tenant_id,
            WebhookConfig(url="https://example.com/webhook"),
        )
        service.webhook_dispatcher.send = AsyncMock()
        before = {
            status_name: HANDOFF_OPERATIONS.labels(
                operation="update",
                status=status_name,
            )._value.get()
            for status_name in ("accepted", "error", "conflict")
        }
        handoff_store.conflict_next = True

        with pytest.raises(HandoffConflictError):
            await service.accept_handoff(created.id, tenant_id=tenant_id)

        after = {
            status_name: HANDOFF_OPERATIONS.labels(
                operation="update",
                status=status_name,
            )._value.get()
            for status_name in before
        }
        assert len(event_store.appended) == 1
        service.webhook_dispatcher.send.assert_not_awaited()
        assert after["conflict"] == before["conflict"] + 1
        assert after["accepted"] == before["accepted"]
        assert after["error"] == before["error"]

    @pytest.mark.asyncio
    async def test_success_effect_order_is_commit_event_webhook_bookkeeping(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        recent = EventData(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=EventType.MESSAGE_IN.value,
            event_data={"role": "user", "content": "project once"},
        )
        event_store = _FakeEventStore(
            recent_messages=[recent],
            order=handoff_store.order,
        )
        service = _build_test_service(
            handoff_store,
            webhook_dispatcher,
            event_store=event_store,
        )
        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        service.configure_webhook(
            tenant_id,
            WebhookConfig(url="https://example.com/webhook"),
        )

        async def send(_tenant_id, _payload):
            handoff_store.order.append("webhook")
            return WebhookDelivery(
                handoff_id=created.id,
                tenant_id=tenant_id,
                url="https://example.com/webhook",
                status=WebhookStatus.SENT,
            )

        service.webhook_dispatcher.send = AsyncMock(side_effect=send)
        handoff_store.order.clear()
        before = {
            status_name: HANDOFF_OPERATIONS.labels(
                operation="update",
                status=status_name,
            )._value.get()
            for status_name in ("accepted", "error", "conflict")
        }

        detail = await service.accept_handoff(created.id, tenant_id=tenant_id)

        assert handoff_store.order == [
            "commit",
            "event",
            "webhook",
            "bookkeeping",
            "commit",
        ]
        assert event_store.recent_calls == [(tenant_id, session_id, 5)]
        assert [message["content"] for message in detail.recent_messages] == [
            "project once"
        ]
        payload = service.webhook_dispatcher.send.await_args.args[1]
        assert [message["content"] for message in payload.recent_messages] == [
            "project once"
        ]
        after = {
            status_name: HANDOFF_OPERATIONS.labels(
                operation="update",
                status=status_name,
            )._value.get()
            for status_name in before
        }
        assert after["accepted"] == before["accepted"] + 1
        assert after["error"] == before["error"]
        assert after["conflict"] == before["conflict"]

    @pytest.mark.asyncio
    async def test_transition_event_failure_preserves_cas_and_suppresses_webhook(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        event_store = _FakeEventStore(order=handoff_store.order)
        service = _build_test_service(
            handoff_store,
            webhook_dispatcher,
            event_store=event_store,
        )
        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        service.configure_webhook(
            tenant_id,
            WebhookConfig(url="https://example.com/webhook"),
        )
        service.webhook_dispatcher.send = AsyncMock()
        handoff_store.order.clear()
        event_store.append_error = RuntimeError("event store unavailable")
        before = {
            status_name: HANDOFF_OPERATIONS.labels(
                operation="update",
                status=status_name,
            )._value.get()
            for status_name in ("accepted", "error", "conflict")
        }

        with pytest.raises(HandoffPostCommitError):
            await service.accept_handoff(created.id, tenant_id=tenant_id)

        persisted = handoff_store.records[created.id]
        assert persisted.status == HandoffStatus.ACCEPTED.value
        assert persisted.lifecycle_version == 1
        assert handoff_store.order == ["commit"]
        assert event_store.append_calls == 2
        assert event_store.recent_calls == []
        service.webhook_dispatcher.send.assert_not_awaited()
        after = {
            status_name: HANDOFF_OPERATIONS.labels(
                operation="update",
                status=status_name,
            )._value.get()
            for status_name in before
        }
        assert after["error"] == before["error"] + 1
        assert after["accepted"] == before["accepted"]
        assert after["conflict"] == before["conflict"]

    @pytest.mark.asyncio
    async def test_failed_delivery_is_sanitized_and_transition_stays_successful(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        event_store = _FakeEventStore(order=handoff_store.order)
        service = _build_test_service(
            handoff_store,
            webhook_dispatcher,
            event_store=event_store,
        )
        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        service.configure_webhook(
            tenant_id,
            WebhookConfig(url="https://example.com/webhook"),
        )

        async def failed_send(_tenant_id, _payload):
            handoff_store.order.append("webhook")
            return WebhookDelivery(
                handoff_id=created.id,
                tenant_id=tenant_id,
                url="https://example.com/webhook",
                status=WebhookStatus.FAILED,
                error_message="patient secret from remote response",
            )

        service.webhook_dispatcher.send = AsyncMock(side_effect=failed_send)
        handoff_store.order.clear()

        detail = await service.accept_handoff(created.id, tenant_id=tenant_id)

        persisted = handoff_store.records[created.id]
        assert detail.status is HandoffStatus.ACCEPTED
        assert persisted.lifecycle_version == 1
        assert persisted.webhook_sent is False
        assert persisted.webhook_response == {
            "status": "failed",
            "attempt": 1,
            "error_type": "delivery_failed",
        }
        assert "patient secret" not in str(persisted.webhook_response)
        assert handoff_store.order == [
            "commit",
            "event",
            "webhook",
            "bookkeeping",
            "commit",
        ]
        service.webhook_dispatcher.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bookkeeping_failure_occurs_after_exactly_one_webhook_send(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        event_store = _FakeEventStore(order=handoff_store.order)
        service = _build_test_service(
            handoff_store,
            webhook_dispatcher,
            event_store=event_store,
        )
        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        service.configure_webhook(
            tenant_id,
            WebhookConfig(url="https://example.com/webhook"),
        )

        async def successful_send(_tenant_id, _payload):
            handoff_store.order.append("webhook")
            return WebhookDelivery(
                handoff_id=created.id,
                tenant_id=tenant_id,
                url="https://example.com/webhook",
                status=WebhookStatus.SENT,
            )

        service.webhook_dispatcher.send = AsyncMock(side_effect=successful_send)
        handoff_store.fail_bookkeeping = True
        handoff_store.order.clear()
        before_error = HANDOFF_OPERATIONS.labels(
            operation="update",
            status="error",
        )._value.get()

        with pytest.raises(HandoffPostCommitError):
            await service.accept_handoff(created.id, tenant_id=tenant_id)

        persisted = handoff_store.records[created.id]
        assert persisted.status == HandoffStatus.ACCEPTED.value
        assert persisted.lifecycle_version == 1
        assert persisted.webhook_response is None
        assert handoff_store.order == [
            "commit",
            "event",
            "webhook",
            "bookkeeping",
        ]
        assert event_store.recent_calls == [(tenant_id, session_id, 5)]
        service.webhook_dispatcher.send.assert_awaited_once()
        after_error = HANDOFF_OPERATIONS.labels(
            operation="update",
            status="error",
        )._value.get()
        assert after_error == before_error + 1

    @pytest.mark.asyncio
    async def test_wrong_tenant_cannot_mutate_record(
        self,
        handoff_service,
        tenant_id,
        session_id,
    ):
        created = await handoff_service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )

        with pytest.raises(HandoffNotFoundError):
            await handoff_service.cancel_handoff(
                created.id,
                tenant_id=uuid4(),
            )

        detail = await handoff_service.get_handoff(created.id, tenant_id)
        assert detail.status is HandoffStatus.PENDING

    @pytest.mark.asyncio
    async def test_event_store_failure_is_post_commit_and_stops_webhook(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        event_store = _FakeEventStore(append_error=RuntimeError("unavailable"))
        service = _build_test_service(
            handoff_store,
            webhook_dispatcher,
            event_store=event_store,
        )
        service.configure_webhook(
            tenant_id,
            WebhookConfig(url="https://example.com/webhook"),
        )
        service.webhook_dispatcher.send = AsyncMock()

        with pytest.raises(HandoffPostCommitError):
            await service.initiate_handoff(
                HandoffRequest(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    reason=HandoffReason.USER_REQUEST,
                )
            )

        assert len(handoff_store.records) == 1
        service.webhook_dispatcher.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_insert_failure_is_typed_and_emits_nothing(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        handoff_store.fail_insert = True
        event_store = _FakeEventStore()
        service = _build_test_service(
            handoff_store,
            webhook_dispatcher,
            event_store=event_store,
        )

        with pytest.raises(HandoffPersistenceError):
            await service.initiate_handoff(
                HandoffRequest(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    reason=HandoffReason.USER_REQUEST,
                )
            )

        assert not handoff_store.records
        assert not event_store.appended

    @pytest.mark.asyncio
    async def test_failed_webhook_is_nonfatal_and_persists_sanitized_status(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        service = _build_test_service(handoff_store, webhook_dispatcher)
        service.configure_webhook(
            tenant_id,
            WebhookConfig(url="https://example.com/webhook"),
        )
        service.webhook_dispatcher.send = AsyncMock(
            side_effect=WebhookError("patient secret must not persist")
        )

        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
            )
        )
        detail = await service.get_handoff(created.id, tenant_id)

        assert created.webhook_status is WebhookStatus.FAILED
        assert detail.webhook_status is WebhookStatus.FAILED
        assert detail.webhook_response == {
            "status": "failed",
            "attempt": 1,
            "error_type": "delivery_failed",
        }
        assert "patient secret" not in str(detail.webhook_response)

    @pytest.mark.asyncio
    async def test_mapper_round_trip_preserves_enriched_fields_without_version(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        service = _build_test_service(handoff_store, webhook_dispatcher)
        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.SCHEDULING,
                sector=HandoffSector.AGENDAMENTO,
                sla=HandoffSLA.PRIORITARIO,
                key_info={"appointment": "morning"},
                verification_status="link_sent",
            )
        )
        await service.confirm_handoff(
            created.id,
            agent_id="agent-7",
            tenant_id=tenant_id,
        )
        completed = await service.complete_handoff(
            created.id,
            notes="resolved",
            tenant_id=tenant_id,
        )
        dumped = completed.model_dump()

        assert completed.sector is HandoffSector.AGENDAMENTO
        assert completed.sla is HandoffSLA.PRIORITARIO
        assert completed.summary
        assert completed.key_info["extracted_slots"]["appointment"] == "morning"
        assert completed.verification_status == "link_sent"
        assert completed.assigned_to == "agent-7"
        assert completed.resolution_notes == "resolved"
        assert completed.sla_target_seconds == 600
        assert completed.updated_at is not None
        assert "lifecycle_version" not in dumped

    @pytest.mark.asyncio
    async def test_recent_messages_are_tenant_scoped_bounded_projection_only(
        self,
        handoff_store,
        webhook_dispatcher,
        tenant_id,
        session_id,
    ):
        recent = [
            EventData(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=(
                    EventType.MESSAGE_IN.value
                    if index % 2 == 0
                    else EventType.MESSAGE_OUT.value
                ),
                event_data={
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"message-{index}",
                },
            )
            for index in range(7)
        ]
        event_store = _FakeEventStore(recent_messages=recent)
        service = _build_test_service(
            handoff_store,
            webhook_dispatcher,
            event_store=event_store,
        )
        created = await service.initiate_handoff(
            HandoffRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                reason=HandoffReason.USER_REQUEST,
                recent_messages=[{"role": "user", "content": "request-only"}],
            )
        )
        detail = await service.get_handoff(created.id, tenant_id)

        assert [message["content"] for message in detail.recent_messages] == [
            f"message-{index}" for index in range(5)
        ]
        assert event_store.recent_calls == [(tenant_id, session_id, 5)]
        assert "recent_messages" not in handoff_store.records[created.id].context


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

        # 3. Agent confirms first response
        confirmed = await handoff_service.confirm_handoff(
            response.id,
            tenant_id=tenant_id,
        )
        assert confirmed.status == HandoffStatus.IN_PROGRESS

        # 4. Agent completes
        final = await handoff_service.complete_handoff(
            response.id,
            notes="Issue resolved",
            tenant_id=tenant_id,
        )
        assert final.status == HandoffStatus.COMPLETED
        assert final.resolved_at is not None

        # 5. Verify no active handoff
        active = await handoff_service.get_active_handoff_for_session(
            session_id,
            tenant_id=tenant_id,
        )
        assert active is None

    @pytest.mark.asyncio
    async def test_handoff_with_webhook(
        self,
        handoff_store,
        tenant_id,
        session_id,
    ):
        """Test handoff with webhook notification."""
        dispatcher = WebhookDispatcher()
        service = _build_test_service(handoff_store, dispatcher)

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
