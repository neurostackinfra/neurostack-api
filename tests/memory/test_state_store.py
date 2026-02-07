"""
NeuroStack - State Store Unit Tests
Tests for Memory Architecture 2.0 - OLTP State Store (Redis)
"""

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import redis.asyncio as redis

# Mock the problematic import before it's loaded
# This avoids SQLAlchemy model initialization issues in tests
sys.path.insert(0, "/home/jairomendes/neurostack/neurostack-api")


# Create a mock CognitiveStateData class for testing
class CognitiveStateData:
    """
    Mock CognitiveStateData for testing purposes.
    Mirrors the real implementation without SQLAlchemy dependencies.
    """

    def __init__(
        self,
        funnel_stage: str = "lead_novo",
        intent_history: Optional[list[str]] = None,
        slots: Optional[dict[str, Any]] = None,
        flags: Optional[dict[str, bool]] = None,
        turn_count: int = 0,
        safety_triggers: int = 0,
        last_intent: Optional[str] = None,
        confidence_avg: Optional[float] = None,
    ):
        self.funnel_stage = funnel_stage
        self.intent_history = intent_history or []
        self.slots = slots or {}
        self.flags = flags or {}
        self.turn_count = turn_count
        self.safety_triggers = safety_triggers
        self.last_intent = last_intent
        self.confidence_avg = confidence_avg

    def to_dict(self) -> dict[str, Any]:
        return {
            "funnel_stage": self.funnel_stage,
            "intent_history": self.intent_history,
            "slots": self.slots,
            "flags": self.flags,
            "turn_count": self.turn_count,
            "safety_triggers": self.safety_triggers,
            "last_intent": self.last_intent,
            "confidence_avg": self.confidence_avg,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CognitiveStateData":
        return cls(
            funnel_stage=data.get("funnel_stage", "lead_novo"),
            intent_history=data.get("intent_history", []),
            slots=data.get("slots", {}),
            flags=data.get("flags", {}),
            turn_count=data.get("turn_count", 0),
            safety_triggers=data.get("safety_triggers", 0),
            last_intent=data.get("last_intent"),
            confidence_avg=data.get("confidence_avg"),
        )

    def add_intent(self, intent: str) -> None:
        self.intent_history.append(intent)
        self.last_intent = intent
        self.turn_count += 1

    def update_slot(self, key: str, value: Any) -> None:
        self.slots[key] = value

    def set_flag(self, key: str, value: bool) -> None:
        self.flags[key] = value

    def trigger_safety(self) -> None:
        self.safety_triggers += 1

    def update_confidence(self, confidence: float) -> None:
        if self.confidence_avg is None:
            self.confidence_avg = confidence
        else:
            self.confidence_avg = (
                self.confidence_avg * (self.turn_count - 1) + confidence
            ) / self.turn_count


# Patch the module before importing state_store
sys.modules['src.models.session'] = MagicMock()
sys.modules['src.models.session'].CognitiveStateData = CognitiveStateData

from src.core.exceptions import (
    StateNotFoundError,
    StateStoreConnectionError,
    StateStoreError,
)
from src.memory.state_store import (
    StateStore,
    SessionState,
)
from src.memory.models import (
    CognitiveState,
    FunnelStage,
    FlowMode,
    StatePatch,
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
def sample_cognitive_state() -> dict:
    """Sample cognitive state dictionary."""
    return {
        "funnel_stage": "lead_novo",
        "intent_history": ["greeting"],
        "slots": {"name": "João"},
        "flags": {"first_visit": True},
        "turn_count": 1,
        "safety_triggers": 0,
        "last_intent": "greeting",
        "confidence_avg": 0.95,
    }


@pytest.fixture
def sample_session_state(
    sample_tenant_id: UUID,
    sample_session_id: UUID,
    sample_cognitive_state: dict,
) -> SessionState:
    """Sample session state for testing."""
    return SessionState(
        session_id=sample_session_id,
        tenant_id=sample_tenant_id,
        user_id="user-123",
        channel="whatsapp",
        cognitive_state=sample_cognitive_state,
        session_metadata={"source": "test"},
        message_count=1,
        is_active=True,
    )


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    mock = AsyncMock(spec=redis.Redis)
    mock.ping = AsyncMock(return_value=True)
    mock.get = AsyncMock(return_value=None)
    mock.set = AsyncMock(return_value=True)
    mock.delete = AsyncMock(return_value=1)
    mock.exists = AsyncMock(return_value=1)
    mock.expire = AsyncMock(return_value=True)
    mock.ttl = AsyncMock(return_value=86400)
    mock.sadd = AsyncMock(return_value=1)
    mock.srem = AsyncMock(return_value=1)
    mock.smembers = AsyncMock(return_value=set())
    mock.scard = AsyncMock(return_value=0)
    mock.info = AsyncMock(return_value={"redis_version": "7.0.0"})
    mock.aclose = AsyncMock()
    
    # Mock pipeline
    pipeline_mock = AsyncMock()
    pipeline_mock.set = MagicMock()
    pipeline_mock.sadd = MagicMock()
    pipeline_mock.expire = MagicMock()
    pipeline_mock.delete = MagicMock()
    pipeline_mock.srem = MagicMock()
    pipeline_mock.execute = AsyncMock(return_value=[True, 1, True])
    pipeline_mock.__aenter__ = AsyncMock(return_value=pipeline_mock)
    pipeline_mock.__aexit__ = AsyncMock(return_value=None)
    mock.pipeline = MagicMock(return_value=pipeline_mock)
    
    return mock


@pytest.fixture
def mock_connection_pool():
    """Mock Redis connection pool."""
    pool = MagicMock()
    pool.disconnect = AsyncMock()
    return pool


# ============================================
# Unit Tests - StateStore Initialization
# ============================================


class TestStateStoreInit:
    """Tests for StateStore initialization."""

    def test_init_with_defaults(self):
        """Test StateStore initialization with default settings."""
        store = StateStore()
        assert store._redis is None
        assert store._pool is None

    def test_init_with_custom_settings(self):
        """Test StateStore initialization with custom settings."""
        store = StateStore(
            redis_url="redis://custom:6379/1",
            max_connections=20,
            socket_timeout=10.0,
            default_ttl=3600,
        )
        assert store._redis_url == "redis://custom:6379/1"
        assert store._max_connections == 20
        assert store._socket_timeout == 10.0
        assert store._default_ttl == 3600


# ============================================
# Unit Tests - Key Generation
# ============================================


class TestKeyGeneration:
    """Tests for Redis key generation."""

    def test_get_session_key(self, sample_tenant_id: UUID, sample_session_id: UUID):
        """Test session key generation."""
        store = StateStore()
        key = store._get_session_key(sample_tenant_id, sample_session_id)
        assert key == f"neurostack:session:{sample_tenant_id}:{sample_session_id}"

    def test_get_ttl_key(self, sample_tenant_id: UUID):
        """Test TTL key generation."""
        store = StateStore()
        key = store._get_ttl_key(sample_tenant_id)
        assert key == f"neurostack:tenant:ttl:{sample_tenant_id}"

    def test_get_index_key(self, sample_tenant_id: UUID):
        """Test index key generation."""
        store = StateStore()
        key = store._get_index_key(sample_tenant_id)
        assert key == f"neurostack:tenant:sessions:{sample_tenant_id}"


# ============================================
# Unit Tests - Serialization
# ============================================


class TestSerialization:
    """Tests for session state serialization/deserialization."""

    def test_serialize_session(self, sample_session_state: SessionState):
        """Test session state serialization to JSON."""
        store = StateStore()
        json_str = store._serialize_session(sample_session_state)
        
        assert isinstance(json_str, str)
        assert str(sample_session_state.session_id) in json_str
        assert str(sample_session_state.tenant_id) in json_str

    def test_deserialize_session(self, sample_session_state: SessionState):
        """Test session state deserialization from JSON."""
        store = StateStore()
        json_str = store._serialize_session(sample_session_state)
        restored = store._deserialize_session(json_str)
        
        assert restored.session_id == sample_session_state.session_id
        assert restored.tenant_id == sample_session_state.tenant_id
        assert restored.user_id == sample_session_state.user_id
        assert restored.channel == sample_session_state.channel
        assert restored.cognitive_state == sample_session_state.cognitive_state

    def test_serialize_deserialize_roundtrip(self, sample_session_state: SessionState):
        """Test serialization/deserialization roundtrip."""
        store = StateStore()
        json_str = store._serialize_session(sample_session_state)
        restored = store._deserialize_session(json_str)
        
        # Re-serialize and compare
        json_str_2 = store._serialize_session(restored)
        assert json_str == json_str_2


# ============================================
# Unit Tests - CRUD Operations (Mocked)
# ============================================


class TestCRUDOperations:
    """Tests for CRUD operations with mocked Redis."""

    @pytest.mark.asyncio
    async def test_create_session(
        self,
        sample_session_state: SessionState,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test session creation."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        result = await store.create_session(sample_session_state)
        
        assert result.session_id == sample_session_state.session_id
        assert mock_redis.pipeline.called

    @pytest.mark.asyncio
    async def test_get_session_found(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        sample_session_state: SessionState,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test getting an existing session."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        # Mock the Redis get to return serialized session
        mock_redis.get.return_value = store._serialize_session(sample_session_state)
        
        result = await store.get_session(sample_tenant_id, sample_session_id)
        
        assert result.session_id == sample_session_id
        assert result.tenant_id == sample_tenant_id
        mock_redis.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_session_not_found(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test getting a non-existent session raises error."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.get.return_value = None
        
        with pytest.raises(StateNotFoundError):
            await store.get_session(sample_tenant_id, sample_session_id)

    @pytest.mark.asyncio
    async def test_get_session_or_none_found(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        sample_session_state: SessionState,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test get_session_or_none returns session when found."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.get.return_value = store._serialize_session(sample_session_state)
        
        result = await store.get_session_or_none(sample_tenant_id, sample_session_id)
        
        assert result is not None
        assert result.session_id == sample_session_id

    @pytest.mark.asyncio
    async def test_get_session_or_none_not_found(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test get_session_or_none returns None when not found."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.get.return_value = None
        
        result = await store.get_session_or_none(sample_tenant_id, sample_session_id)
        
        assert result is None

    @pytest.mark.asyncio
    async def test_update_session(
        self,
        sample_session_state: SessionState,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test session update."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        sample_session_state.message_count = 5
        result = await store.update_session(sample_session_state)
        
        assert result.message_count == 5
        mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_session_found(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test deleting an existing session."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        # Mock pipeline execute to return [1, 1] (deleted, removed from index)
        pipeline_mock = mock_redis.pipeline.return_value
        pipeline_mock.execute.return_value = [1, 1]
        
        result = await store.delete_session(sample_tenant_id, sample_session_id)
        
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_session_not_found(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test deleting a non-existent session."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        # Mock pipeline execute to return [0, 0] (not deleted)
        pipeline_mock = mock_redis.pipeline.return_value
        pipeline_mock.execute.return_value = [0, 0]
        
        result = await store.delete_session(sample_tenant_id, sample_session_id)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_exists(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test session existence check."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.exists.return_value = 1
        
        result = await store.exists(sample_tenant_id, sample_session_id)
        
        assert result is True


# ============================================
# Unit Tests - Cognitive State Operations
# ============================================


class TestCognitiveStateOperations:
    """Tests for cognitive state specific operations."""

    @pytest.mark.asyncio
    async def test_get_cognitive_state(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        sample_session_state: SessionState,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test getting cognitive state from session."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.get.return_value = store._serialize_session(sample_session_state)
        
        result = await store.get_cognitive_state(sample_tenant_id, sample_session_id)
        
        # Check that result has expected attributes instead of isinstance
        assert hasattr(result, "funnel_stage")
        assert hasattr(result, "last_intent")
        assert result.funnel_stage == "lead_novo"
        assert result.last_intent == "greeting"

    @pytest.mark.asyncio
    async def test_update_cognitive_state(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        sample_session_state: SessionState,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test updating cognitive state."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        serialized = store._serialize_session(sample_session_state)
        # First call returns session, second call returns TTL (or None)
        mock_redis.get.side_effect = [serialized, None]
        
        new_state = CognitiveStateData(
            funnel_stage="consideration",
            last_intent="scheduling",
            turn_count=3,
        )
        
        result = await store.update_cognitive_state(
            sample_tenant_id,
            sample_session_id,
            new_state,
        )
        
        assert result.cognitive_state["funnel_stage"] == "em_qualificacao"
        assert result.cognitive_state["last_intent"] == "scheduling"

    @pytest.mark.asyncio
    async def test_apply_state_patch(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        sample_session_state: SessionState,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test applying state patch."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        serialized = store._serialize_session(sample_session_state)
        # First call returns session, second call returns TTL (or None)
        mock_redis.get.side_effect = [serialized, None]
        
        patch = {
            "funnel_stage": "qualificado",
            "last_intent": "confirm_appointment",
            "slots": {"date": "2025-01-15", "time": "14:00"},
            "flags": {"appointment_confirmed": True},
            "confidence": 0.92,
        }
        
        result = await store.apply_state_patch(
            sample_tenant_id,
            sample_session_id,
            patch,
        )
        
        cognitive = result.get_cognitive_state_obj()
        assert cognitive.funnel_stage.value == "qualificado"
        assert cognitive.slots["date"] == "2025-01-15"
        assert cognitive.flags["appointment_confirmed"] is True


# ============================================
# Unit Tests - Tenant Operations
# ============================================


class TestTenantOperations:
    """Tests for tenant-level operations."""

    @pytest.mark.asyncio
    async def test_set_tenant_ttl(
        self,
        sample_tenant_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test setting tenant TTL."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        await store.set_tenant_ttl(sample_tenant_id, 3600)
        
        mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_sessions(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test listing sessions for tenant."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.smembers.return_value = {str(sample_session_id)}
        
        result = await store.list_sessions(sample_tenant_id)
        
        assert len(result) == 1
        assert result[0] == sample_session_id

    @pytest.mark.asyncio
    async def test_count_sessions(
        self,
        sample_tenant_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test counting sessions for tenant."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.scard.return_value = 10
        
        result = await store.count_sessions(sample_tenant_id)
        
        assert result == 10

    @pytest.mark.asyncio
    async def test_refresh_session_ttl(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test refreshing session TTL."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.expire.return_value = True
        
        result = await store.refresh_session_ttl(sample_tenant_id, sample_session_id)
        
        assert result is True


# ============================================
# Unit Tests - Health Check
# ============================================


class TestHealthCheck:
    """Tests for health check functionality."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(
        self,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test health check when Redis is healthy."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        result = await store.health_check()
        
        assert result["status"] == "healthy"
        assert "latency_ms" in result
        assert result["redis_version"] == "7.0.0"

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self):
        """Test health check when not connected."""
        store = StateStore()
        
        result = await store.health_check()
        
        assert result["status"] == "unhealthy"
        assert result["error"] == "Not connected"

    @pytest.mark.asyncio
    async def test_health_check_redis_error(
        self,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test health check when Redis returns error."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.ping.side_effect = redis.RedisError("Connection refused")
        
        result = await store.health_check()
        
        assert result["status"] == "unhealthy"
        assert "Connection refused" in result["error"]


# ============================================
# Unit Tests - Error Handling
# ============================================


class TestErrorHandling:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_operations_without_connection(self):
        """Test operations raise error when not connected."""
        store = StateStore()
        
        with pytest.raises(StateStoreConnectionError):
            await store.get_session(uuid4(), uuid4())

    @pytest.mark.asyncio
    async def test_redis_error_wrapped(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
        mock_redis: AsyncMock,
        mock_connection_pool: MagicMock,
    ):
        """Test Redis errors are wrapped in StateStoreError."""
        store = StateStore()
        store._redis = mock_redis
        store._pool = mock_connection_pool
        
        mock_redis.get.side_effect = redis.RedisError("Connection timeout")
        
        with pytest.raises(StateStoreError) as exc_info:
            await store.get_session(sample_tenant_id, sample_session_id)
        
        assert "Connection timeout" in str(exc_info.value)


# ============================================
# Unit Tests - SessionState Model
# ============================================


class TestSessionStateModel:
    """Tests for SessionState Pydantic model."""

    def test_session_state_creation(
        self,
        sample_tenant_id: UUID,
        sample_session_id: UUID,
    ):
        """Test SessionState creation with defaults."""
        state = SessionState(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
        )
        
        assert state.session_id == sample_session_id
        assert state.tenant_id == sample_tenant_id
        assert state.is_active is True
        assert state.message_count == 0
        assert state.cognitive_state == {}

    def test_session_state_update_activity(self, sample_session_state: SessionState):
        """Test activity timestamp update."""
        old_activity = sample_session_state.last_activity_at
        sample_session_state.update_activity()
        
        assert sample_session_state.last_activity_at >= old_activity

    def test_session_state_increment_message_count(
        self,
        sample_session_state: SessionState,
    ):
        """Test message count increment."""
        old_count = sample_session_state.message_count
        sample_session_state.increment_message_count()
        
        assert sample_session_state.message_count == old_count + 1

    def test_session_state_cognitive_state_roundtrip(
        self,
        sample_session_state: SessionState,
    ):
        """Test cognitive state getter/setter."""
        cognitive = sample_session_state.get_cognitive_state_obj()
        cognitive.add_intent("new_intent")
        sample_session_state.set_cognitive_state_obj(cognitive)
        
        restored = sample_session_state.get_cognitive_state_obj()
        assert "new_intent" in restored.intent_history


# ============================================
# Unit Tests - CognitiveState Model
# ============================================


class TestCognitiveStateModel:
    """Tests for CognitiveState Pydantic model from models.py."""

    def test_cognitive_state_creation(self):
        """Test CognitiveState creation with defaults."""
        state = CognitiveState()
        
        assert state.funnel_stage == FunnelStage.LEAD_NOVO
        assert state.intent_history == []
        assert state.turn_count == 0

    def test_cognitive_state_add_intent(self):
        """Test adding intent to cognitive state."""
        state = CognitiveState()
        state.add_intent("scheduling", confidence=0.9)
        
        assert "scheduling" in state.intent_history
        assert state.last_intent == "scheduling"
        assert state.turn_count == 1
        assert state.confidence_avg == 0.9

    def test_cognitive_state_update_slot(self):
        """Test updating slots."""
        state = CognitiveState()
        state.update_slot("patient_name", "Maria")
        state.update_slot("phone", "11999999999")
        
        assert state.slots["patient_name"] == "Maria"
        assert state.slots["phone"] == "11999999999"

    def test_cognitive_state_set_flag(self):
        """Test setting flags."""
        state = CognitiveState()
        state.set_flag("consent_given", True)
        
        assert state.flags["consent_given"] is True

    def test_cognitive_state_trigger_safety(self):
        """Test safety trigger."""
        state = CognitiveState()
        state.trigger_safety()
        
        assert state.safety_triggers == 1
        assert state.current_mode == FlowMode.SAFETY

    def test_cognitive_state_to_patch_dict(self):
        """Test conversion to patch dictionary."""
        state = CognitiveState(
            funnel_stage=FunnelStage.QUALIFICADO,
            last_intent="confirm",
            slots={"date": "2025-01-15"},
            confidence_avg=0.95,
        )
        
        patch = state.to_patch_dict()
        
        assert patch["funnel_stage"] == "qualificado"
        assert patch["last_intent"] == "confirm"
        assert patch["slots"]["date"] == "2025-01-15"

