"""
NeuroStack - Event Store Unit Tests
Tests for Memory Architecture 2.0 - Event Store (PostgreSQL)

Note: These tests use mocked database connections and do not require
a running PostgreSQL instance.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import src.memory.event_store as event_store_module
from src.core.exceptions import EventStoreConnectionError, EventStoreError
from src.core.interaction_context import InteractionOrigin
from src.memory.event_store import (
    EventAggregation,
    EventData,
    EventQueryResult,
    EventStore,
    EventType,
    FunnelTransitionEventData,
    HandoffEventData,
    MessageEventData,
    SafetyEventData,
    SRMDecisionEventData,
)

# ============================================
# Fixtures
# ============================================


@pytest.fixture
def sample_tenant_id() -> UUID:
    """Sample tenant ID for testing."""
    return UUID("a0000000-0000-0000-0000-000000000001")


@pytest.fixture
def sample_session_id() -> UUID:
    """Sample session ID for testing."""
    return UUID("c0000000-0000-0000-0000-000000000001")


@pytest.fixture
def sample_event_id() -> UUID:
    """Sample event ID for testing."""
    return UUID("e0000000-0000-0000-0000-000000000001")


@pytest.fixture
def sample_message_event(
    sample_tenant_id: UUID,
    sample_session_id: UUID,
    sample_event_id: UUID,
) -> EventData:
    """Sample message event for testing."""
    return EventData(
        id=sample_event_id,
        tenant_id=sample_tenant_id,
        session_id=sample_session_id,
        event_type=EventType.MESSAGE_IN.value,
        event_data={
            "content": "Olá, gostaria de agendar uma consulta",
            "channel": "whatsapp",
            "user_id": "user-123",
            "role": "user",
        },
        event_metadata={"source": "test"},
    )


@pytest.fixture
def sample_srm_decision_event(
    sample_tenant_id: UUID,
    sample_session_id: UUID,
) -> EventData:
    """Sample SRM decision event for testing."""
    return EventData(
        tenant_id=sample_tenant_id,
        session_id=sample_session_id,
        event_type=EventType.SRM_DECISION.value,
        event_data={
            "intent": "scheduling",
            "confidence": 0.95,
            "action": "normal",
            "needs_rag": False,
            "needs_handoff": False,
            "safety_concern": False,
            "state_patch": {"funnel_stage": "consideration"},
        },
        event_metadata={"processing_time_ms": 50},
    )


@pytest.fixture
def mock_session():
    """Mock database session."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


@pytest.fixture
def mock_session_factory(mock_session):
    """Mock session factory."""
    factory = MagicMock(return_value=mock_session)
    return factory


@pytest.fixture
def mock_engine():
    """Mock database engine."""
    engine = AsyncMock()
    engine.dispose = AsyncMock()

    # Mock begin context manager
    conn_mock = AsyncMock()
    conn_mock.execute = AsyncMock()
    conn_mock.__aenter__ = AsyncMock(return_value=conn_mock)
    conn_mock.__aexit__ = AsyncMock(return_value=None)
    engine.begin = MagicMock(return_value=conn_mock)

    return engine


# ============================================
# Unit Tests - EventStore Initialization
# ============================================


class TestEventStoreInit:
    """Tests for EventStore initialization."""

    def test_init_with_defaults(self):
        """An unbound store owns no database resources."""
        store = EventStore()
        assert store._session_factory is None

    def test_init_with_injected_factory(self, mock_session_factory: MagicMock) -> None:
        store = EventStore(session_factory=mock_session_factory)
        assert store._session_factory is mock_session_factory
        assert not hasattr(store, "connect")
        assert not hasattr(store, "disconnect")


class TestGlobalEventStoreLifecycle:
    @pytest.mark.asyncio
    async def test_each_consumer_uses_shared_database_factory_without_pool_creation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        shared_factory = MagicMock()
        monkeypatch.setattr(
            "src.repositories.database.get_db_session_factory",
            MagicMock(return_value=shared_factory),
        )

        first_result, second_result = await asyncio.gather(
            event_store_module.get_event_store(), event_store_module.get_event_store()
        )

        assert first_result is not second_result
        assert first_result._session_factory is shared_factory
        assert second_result._session_factory is shared_factory
        assert not hasattr(event_store_module, "close_event_store")


# ============================================
# Unit Tests - EventData Model
# ============================================


class TestEventDataModel:
    """Tests for EventData Pydantic model."""

    def test_event_data_creation(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test EventData creation with defaults."""
        event = EventData(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            event_type=EventType.MESSAGE_IN.value,
            event_data={"content": "Hello"},
        )

        assert event.tenant_id == sample_tenant_id
        assert event.session_id == sample_session_id
        assert event.event_type == EventType.MESSAGE_IN.value
        assert event.event_data == {"content": "Hello"}
        assert event.id is not None
        assert event.created_at is not None
        assert event.interaction_origin is InteractionOrigin.UNKNOWN

    def test_event_data_without_session(self, sample_tenant_id: UUID):
        """Test EventData creation without session (system event)."""
        event = EventData(
            tenant_id=sample_tenant_id,
            session_id=None,
            event_type=EventType.ERROR.value,
            event_data={"error": "Something went wrong"},
        )

        assert event.session_id is None
        assert event.event_type == EventType.ERROR.value

    def test_to_insert_dict(self, sample_message_event: EventData):
        """Test conversion to insert dictionary."""
        insert_dict = sample_message_event.to_insert_dict()

        assert "id" in insert_dict
        assert "tenant_id" in insert_dict
        assert "session_id" in insert_dict
        assert "event_type" in insert_dict
        assert insert_dict["interaction_origin"] == "unknown"
        assert "event_data" in insert_dict
        assert "metadata" in insert_dict
        assert "created_at" in insert_dict


# ============================================
# Unit Tests - Event Type Enum
# ============================================


class TestEventTypeEnum:
    """Tests for EventType enum."""

    def test_message_events(self):
        """Test message event types."""
        assert EventType.MESSAGE_IN.value == "message.in"
        assert EventType.MESSAGE_OUT.value == "message.out"

    def test_session_events(self):
        """Test session lifecycle event types."""
        assert EventType.SESSION_START.value == "session.start"
        assert EventType.SESSION_END.value == "session.end"
        assert EventType.SESSION_TIMEOUT.value == "session.timeout"

    def test_cognitive_events(self):
        """Test cognitive event types."""
        assert EventType.SRM_DECISION.value == "srm.decision"
        assert EventType.STATE_PATCH.value == "state.patch"
        assert EventType.FUNNEL_TRANSITION.value == "funnel.transition"

    def test_safety_events(self):
        """Test safety event types."""
        assert EventType.SAFETY_TRIGGER.value == "safety.trigger"
        assert EventType.SAFETY_BLOCK.value == "safety.block"

    def test_handoff_events(self):
        """Test handoff event types."""
        assert EventType.HANDOFF_REQUEST.value == "handoff.request"
        assert EventType.HANDOFF_ACCEPT.value == "handoff.accept"
        assert EventType.HANDOFF_COMPLETE.value == "handoff.complete"


# ============================================
# Unit Tests - Factory Methods
# ============================================


class TestFactoryMethods:
    """Tests for event factory methods."""

    def test_create_message_event(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test message event factory."""
        store = EventStore()
        event = store.create_message_event(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            content="Olá!",
            channel="whatsapp",
            is_incoming=True,
            user_id="user-123",
        )

        assert event.event_type == EventType.MESSAGE_IN.value
        assert event.event_data["content"] == "Olá!"
        assert event.event_data["channel"] == "whatsapp"
        assert event.event_data["role"] == "user"

    def test_create_message_event_outgoing(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test outgoing message event factory."""
        store = EventStore()
        event = store.create_message_event(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            content="Olá! Como posso ajudar?",
            channel="whatsapp",
            is_incoming=False,
        )

        assert event.event_type == EventType.MESSAGE_OUT.value
        assert event.event_data["role"] == "assistant"

    def test_create_srm_decision_event(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test SRM decision event factory."""
        store = EventStore()
        decision = SRMDecisionEventData(
            intent="scheduling",
            confidence=0.95,
            action="normal",
            needs_rag=False,
        )
        event = store.create_srm_decision_event(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            decision=decision,
        )

        assert event.event_type == EventType.SRM_DECISION.value
        assert event.event_data["intent"] == "scheduling"
        assert event.event_data["confidence"] == 0.95

    def test_create_safety_event(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test safety event factory."""
        store = EventStore()
        safety_data = SafetyEventData(
            safety_category="clinical",
            confidence=0.88,
            trigger_content="sintomas de infarto",
            action_taken="escalate",
        )
        event = store.create_safety_event(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            safety_data=safety_data,
        )

        assert event.event_type == EventType.SAFETY_TRIGGER.value
        assert event.event_data["safety_category"] == "clinical"

    def test_create_handoff_event(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test handoff event factory."""
        store = EventStore()
        handoff_data = HandoffEventData(
            reason="user_request",
            summary="Paciente solicitou falar com atendente",
            urgency="high",
        )
        event = store.create_handoff_event(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            handoff_data=handoff_data,
        )

        assert event.event_type == EventType.HANDOFF_REQUEST.value
        assert event.event_data["reason"] == "user_request"
        assert event.event_data["urgency"] == "high"

    def test_create_funnel_transition_event(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test funnel transition event factory."""
        store = EventStore()
        transition = FunnelTransitionEventData(
            from_stage="awareness",
            to_stage="consideration",
            trigger_intent="scheduling",
        )
        event = store.create_funnel_transition_event(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            transition=transition,
        )

        assert event.event_type == EventType.FUNNEL_TRANSITION.value
        assert event.event_data["from_stage"] == "awareness"
        assert event.event_data["to_stage"] == "consideration"

    def test_create_flow_transition_event(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test flow transition event factory."""
        store = EventStore()
        event = store.create_flow_transition_event(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            from_mode="normal",
            to_mode="safety",
            trigger="safety_concern",
        )

        assert event.event_type == EventType.FLOW_TRANSITION.value
        assert event.event_data["from_mode"] == "normal"
        assert event.event_data["to_mode"] == "safety"

    def test_create_error_event(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test error event factory."""
        store = EventStore()
        event = store.create_error_event(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            error_type="ValidationError",
            error_message="Invalid input",
            stack_trace="File...",
        )

        assert event.event_type == EventType.ERROR.value
        assert event.event_data["error_type"] == "ValidationError"
        assert event.event_data["error_message"] == "Invalid input"


# ============================================
# Unit Tests - CRUD Operations (Mocked)
# ============================================


class TestCRUDOperations:
    """Tests for CRUD operations with mocked database."""

    def test_append_statement_keeps_postgres_types_and_sqlite_portability(
        self,
    ) -> None:
        statement = insert(event_store_module._EVENTS_TABLE).returning(
            event_store_module._EVENTS_TABLE.c.id,
            event_store_module._EVENTS_TABLE.c.created_at,
        )

        postgres_sql = str(statement.compile(dialect=postgresql.dialect()))
        sqlite_sql = str(statement.compile(dialect=sqlite.dialect()))

        assert "::UUID" in postgres_sql
        assert "::JSONB" in postgres_sql
        assert "jsonb" not in sqlite_sql.lower()
        assert "uuid" not in sqlite_sql.lower()

    @pytest.mark.asyncio
    async def test_append_event(
        self,
        sample_message_event: EventData,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test appending a single event."""
        store = EventStore(session_factory=mock_session_factory)

        # Mock the execute result
        mock_result = MagicMock()
        mock_result.fetchone.return_value = MagicMock(
            id=sample_message_event.id,
            created_at=datetime.now(timezone.utc),
        )
        mock_session.execute.return_value = mock_result

        result = await store.append(sample_message_event)

        assert result.id == sample_message_event.id
        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

        parameters = mock_session.execute.call_args.args[1]
        assert parameters["interaction_origin"] == "unknown"

    @pytest.mark.asyncio
    async def test_append_batch(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test appending multiple events in batch."""
        store = EventStore(session_factory=mock_session_factory)

        events = [
            EventData(
                tenant_id=sample_tenant_id,
                session_id=sample_session_id,
                event_type=EventType.MESSAGE_IN.value,
                event_data={"content": f"Message {i}"},
            )
            for i in range(3)
        ]

        result = await store.append_batch(events)

        assert len(result) == 3
        assert mock_session.execute.call_count == 3
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_append_batch_empty(
        self,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test appending empty batch."""
        store = EventStore(session_factory=mock_session_factory)

        result = await store.append_batch([])

        assert result == []
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_sqlite_append_paths_preserve_prior_committed_work(self) -> None:
        """Typed inserts adapt UUID/JSON without rolling back earlier work."""
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
        )
        factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE committed_state (value TEXT NOT NULL)")
                )
                await connection.execute(text("""
                        CREATE TABLE events (
                            id CHAR(32) PRIMARY KEY,
                            tenant_id CHAR(32) NOT NULL,
                            session_id CHAR(32),
                            event_type VARCHAR(100) NOT NULL,
                            interaction_origin VARCHAR(20) NOT NULL,
                            event_data JSON NOT NULL,
                            metadata JSON NOT NULL,
                            created_at DATETIME NOT NULL
                        )
                        """))

            async with factory() as caller_session:
                await caller_session.execute(
                    text("INSERT INTO committed_state (value) VALUES ('paused')")
                )
                await caller_session.commit()

            tenant_id = uuid4()
            session_id = uuid4()
            single = EventData(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=EventType.MESSAGE_IN.value,
                event_data={"content": "single"},
                event_metadata={"source": "sqlite"},
            )
            batch = [
                EventData(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_type=EventType.MESSAGE_OUT.value,
                    event_data={"content": f"batch-{index}"},
                    event_metadata={"index": index},
                )
                for index in range(2)
            ]

            store = EventStore(session_factory=factory)
            await store.append(single)
            await store.append_batch(batch)

            async with factory() as verifier:
                committed_value = await verifier.scalar(
                    text("SELECT value FROM committed_state")
                )
                event_count = await verifier.scalar(text("SELECT COUNT(*) FROM events"))
                json_values = (
                    (
                        await verifier.execute(
                            text(
                                "SELECT json_extract(event_data, '$.content') "
                                "FROM events ORDER BY created_at, id"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

            assert committed_value == "paused"
            assert event_count == 3
            assert set(json_values) == {"single", "batch-0", "batch-1"}
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_get_event_found(
        self,
        sample_event_id: UUID,
        sample_message_event: EventData,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test getting an existing event."""
        store = EventStore(session_factory=mock_session_factory)

        mock_result = MagicMock()
        mock_result.fetchone.return_value = MagicMock(
            id=sample_message_event.id,
            tenant_id=sample_message_event.tenant_id,
            session_id=sample_message_event.session_id,
            event_type=sample_message_event.event_type,
            interaction_origin=sample_message_event.interaction_origin.value,
            event_data=sample_message_event.event_data,
            metadata=sample_message_event.event_metadata,
            created_at=sample_message_event.created_at,
        )
        mock_session.execute.return_value = mock_result

        result = await store.get_event(sample_event_id)

        assert result is not None
        assert result.id == sample_message_event.id

    @pytest.mark.asyncio
    async def test_get_event_not_found(
        self,
        sample_event_id: UUID,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test getting a non-existent event."""
        store = EventStore(session_factory=mock_session_factory)

        mock_result = MagicMock()
        mock_result.fetchone.return_value = None
        mock_session.execute.return_value = mock_result

        result = await store.get_event(sample_event_id)

        assert result is None


# ============================================
# Unit Tests - Query Operations
# ============================================


class TestQueryOperations:
    """Tests for query operations."""

    @pytest.mark.asyncio
    async def test_query_by_session(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        sample_message_event: EventData,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test querying events by session."""
        store = EventStore(session_factory=mock_session_factory)

        # Mock fetch result
        mock_row = MagicMock(
            id=sample_message_event.id,
            tenant_id=sample_message_event.tenant_id,
            session_id=sample_message_event.session_id,
            event_type=sample_message_event.event_type,
            interaction_origin=sample_message_event.interaction_origin.value,
            event_data=sample_message_event.event_data,
            metadata=sample_message_event.event_metadata,
            created_at=sample_message_event.created_at,
        )
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [mock_row]
        mock_result.scalar.return_value = 1
        mock_session.execute.return_value = mock_result

        result = await store.query_by_session(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
        )

        assert isinstance(result, EventQueryResult)
        assert len(result.events) == 1
        assert result.total_count == 1
        assert result.has_more is False
        assert mock_session.execute.await_count == 2
        assert "COUNT(*)" in str(mock_session.execute.await_args_list[1].args[0])

    @pytest.mark.asyncio
    async def test_recent_messages_uses_one_limited_select_without_count(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        newest = SimpleNamespace(
            id=uuid4(),
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            event_type=EventType.MESSAGE_OUT.value,
            interaction_origin=InteractionOrigin.UNKNOWN.value,
            event_data={"role": "assistant", "content": "newest"},
            metadata={},
            created_at=now,
        )
        older = SimpleNamespace(
            id=uuid4(),
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            event_type=EventType.MESSAGE_IN.value,
            interaction_origin=InteractionOrigin.UNKNOWN.value,
            event_data={"role": "user", "content": "older"},
            metadata={},
            created_at=now - timedelta(seconds=1),
        )
        query_result = MagicMock()
        query_result.fetchall.return_value = [newest, older]
        mock_session.execute.return_value = query_result
        store = EventStore(session_factory=mock_session_factory)

        events = await store.get_recent_messages(
            sample_tenant_id,
            sample_session_id,
            limit=5,
        )

        assert [event.event_data["content"] for event in events] == [
            "older",
            "newest",
        ]
        mock_session.execute.assert_awaited_once()
        statement, params = mock_session.execute.await_args.args
        assert "LIMIT :limit" in str(statement)
        assert "COUNT(" not in str(statement).upper()
        assert params == {
            "tenant_id": sample_tenant_id,
            "session_id": sample_session_id,
            "event_types": [
                EventType.MESSAGE_IN.value,
                EventType.MESSAGE_OUT.value,
            ],
            "limit": 5,
        }

    @pytest.mark.asyncio
    async def test_query_by_session_with_type_filter(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test querying events with type filter."""
        store = EventStore(session_factory=mock_session_factory)

        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_result.scalar.return_value = 0
        mock_session.execute.return_value = mock_result

        result = await store.query_by_session(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            event_types=[EventType.MESSAGE_IN.value, EventType.MESSAGE_OUT.value],
        )

        assert isinstance(result, EventQueryResult)
        assert result.events == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", list(InteractionOrigin))
    async def test_all_read_paths_preserve_interaction_origin(
        self,
        origin: InteractionOrigin,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ) -> None:
        event_id = uuid4()
        row = SimpleNamespace(
            id=event_id,
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            event_type=EventType.MESSAGE_IN.value,
            interaction_origin=origin.value,
            event_data={"content": "origin round-trip"},
            metadata={},
            created_at=datetime.now(timezone.utc),
        )
        rows_result = MagicMock()
        rows_result.fetchall.return_value = [row]
        rows_result.fetchone.return_value = row
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        store = EventStore(session_factory=mock_session_factory)

        mock_session.execute.side_effect = [rows_result, count_result]
        by_session = await store.query_by_session(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
        )
        assert by_session.events[0].interaction_origin is origin

        mock_session.execute.side_effect = [rows_result, count_result]
        by_tenant = await store.query_by_tenant(tenant_id=sample_tenant_id)
        assert by_tenant.events[0].interaction_origin is origin

        mock_session.execute.side_effect = None
        mock_session.execute.return_value = rows_result
        fetched = await store.get_event(event_id)
        assert fetched is not None
        assert fetched.interaction_origin is origin

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", list(InteractionOrigin))
    async def test_all_append_paths_persist_interaction_origin(
        self,
        origin: InteractionOrigin,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ) -> None:
        event = EventData(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            event_type=EventType.MESSAGE_IN.value,
            interaction_origin=origin,
        )
        append_result = MagicMock()
        append_result.fetchone.return_value = SimpleNamespace(
            id=event.id,
            created_at=event.created_at,
        )
        mock_session.execute.return_value = append_result

        store = EventStore(session_factory=mock_session_factory)

        await store.append(event)
        assert (
            mock_session.execute.call_args.args[1]["interaction_origin"] == origin.value
        )

        mock_session.reset_mock()
        await store.append_batch([event])
        assert (
            mock_session.execute.call_args.args[1]["interaction_origin"] == origin.value
        )


# ============================================
# Unit Tests - Aggregation Operations
# ============================================


class TestAggregationOperations:
    """Tests for aggregation operations."""

    @pytest.mark.asyncio
    async def test_count_by_type(
        self,
        sample_tenant_id: UUID,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test counting events by type."""
        store = EventStore(session_factory=mock_session_factory)

        mock_rows = [
            MagicMock(
                event_type="message.in",
                count=10,
                first_event=datetime.now(timezone.utc),
                last_event=datetime.now(timezone.utc),
            ),
            MagicMock(
                event_type="message.out",
                count=8,
                first_event=datetime.now(timezone.utc),
                last_event=datetime.now(timezone.utc),
            ),
        ]
        mock_result = MagicMock()
        mock_result.fetchall.return_value = mock_rows
        mock_session.execute.return_value = mock_result

        result = await store.count_by_type(tenant_id=sample_tenant_id)

        assert len(result) == 2
        assert result[0].event_type == "message.in"
        assert result[0].count == 10

    @pytest.mark.asyncio
    async def test_count_sessions_with_events(
        self,
        sample_tenant_id: UUID,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test counting sessions with specific event type."""
        store = EventStore(session_factory=mock_session_factory)

        mock_result = MagicMock()
        mock_result.scalar.return_value = 5
        mock_session.execute.return_value = mock_result

        result = await store.count_sessions_with_events(
            tenant_id=sample_tenant_id,
            event_type=EventType.SAFETY_TRIGGER.value,
        )

        assert result == 5


# ============================================
# Unit Tests - Health Check
# ============================================


class TestHealthCheck:
    """Tests for health check functionality."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(
        self,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
    ):
        """Test health check when PostgreSQL is healthy."""
        store = EventStore(session_factory=mock_session_factory)

        # Mock version result
        mock_result = MagicMock()
        mock_result.scalar.return_value = "PostgreSQL 16.0"
        mock_session.execute.return_value = mock_result

        result = await store.health_check()

        assert result["status"] == "healthy"
        assert "latency_ms" in result
        assert result["postgres_version"] == "16.0"

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self):
        """Test health check when not connected."""
        store = EventStore()

        result = await store.health_check()

        assert result["status"] == "unhealthy"
        assert result["error"] == "Not connected"


# ============================================
# Unit Tests - Error Handling
# ============================================


class TestErrorHandling:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_operations_without_connection(self):
        """Test operations raise error when not connected."""
        store = EventStore()

        with pytest.raises(EventStoreConnectionError):
            store._get_session()

    @pytest.mark.asyncio
    async def test_append_error_wrapped(
        self,
        sample_message_event: EventData,
        mock_session: AsyncMock,
        mock_session_factory: MagicMock,
        mock_engine: AsyncMock,
    ):
        """Test database errors are wrapped in EventStoreError."""
        store = EventStore(session_factory=mock_session_factory)

        mock_session.execute.side_effect = Exception("Database connection lost")

        with pytest.raises(EventStoreError) as exc_info:
            await store.append(sample_message_event)

        assert "Database connection lost" in str(exc_info.value)


# ============================================
# Unit Tests - Event Data Models
# ============================================


class TestEventDataModels:
    """Tests for event data Pydantic models."""

    def test_message_event_data(self):
        """Test MessageEventData model."""
        data = MessageEventData(
            content="Hello",
            channel="whatsapp",
            user_id="user-123",
            role="user",
        )

        assert data.content == "Hello"
        assert data.channel == "whatsapp"
        assert data.role == "user"

    def test_srm_decision_event_data(self):
        """Test SRMDecisionEventData model."""
        data = SRMDecisionEventData(
            intent="scheduling",
            confidence=0.95,
            action="normal",
            needs_rag=True,
            state_patch={"funnel_stage": "consideration"},
        )

        assert data.intent == "scheduling"
        assert data.confidence == 0.95
        assert data.needs_rag is True
        assert data.state_patch == {"funnel_stage": "consideration"}

    def test_safety_event_data(self):
        """Test SafetyEventData model."""
        data = SafetyEventData(
            safety_category="clinical",
            confidence=0.88,
            trigger_content="sintomas graves",
            action_taken="escalate",
        )

        assert data.safety_category == "clinical"
        assert data.confidence == 0.88

    def test_handoff_event_data(self):
        """Test HandoffEventData model."""
        data = HandoffEventData(
            reason="user_request",
            summary="Paciente solicitou atendente",
            urgency="high",
            key_info={"patient_name": "João"},
        )

        assert data.reason == "user_request"
        assert data.urgency == "high"
        assert data.key_info["patient_name"] == "João"

    def test_funnel_transition_event_data(self):
        """Test FunnelTransitionEventData model."""
        data = FunnelTransitionEventData(
            from_stage="awareness",
            to_stage="consideration",
            trigger_intent="scheduling",
        )

        assert data.from_stage == "awareness"
        assert data.to_stage == "consideration"


# ============================================
# Unit Tests - EventQueryResult Model
# ============================================


class TestEventQueryResult:
    """Tests for EventQueryResult model."""

    def test_event_query_result_creation(
        self,
        sample_message_event: EventData,
    ):
        """Test EventQueryResult creation."""
        result = EventQueryResult(
            events=[sample_message_event],
            total_count=1,
            has_more=False,
        )

        assert len(result.events) == 1
        assert result.total_count == 1
        assert result.has_more is False

    def test_event_query_result_empty(self):
        """Test EventQueryResult with no events."""
        result = EventQueryResult(
            events=[],
            total_count=0,
            has_more=False,
        )

        assert len(result.events) == 0
        assert result.total_count == 0


# ============================================
# Unit Tests - EventAggregation Model
# ============================================


class TestEventAggregation:
    """Tests for EventAggregation model."""

    def test_event_aggregation_creation(self):
        """Test EventAggregation creation."""
        now = datetime.now(timezone.utc)
        agg = EventAggregation(
            event_type="message.in",
            count=100,
            first_event=now - timedelta(days=7),
            last_event=now,
        )

        assert agg.event_type == "message.in"
        assert agg.count == 100
        assert agg.first_event is not None
        assert agg.last_event is not None

    def test_event_aggregation_without_timestamps(self):
        """Test EventAggregation without timestamps."""
        agg = EventAggregation(
            event_type="error",
            count=0,
        )

        assert agg.event_type == "error"
        assert agg.count == 0
        assert agg.first_event is None
        assert agg.last_event is None
