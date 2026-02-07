"""
NeuroStack - Memory Architecture 2.0 - State Store
OLTP State Store implementation using Redis for real-time session state management.

The State Store provides:
- Fast read/write access to session state (low latency)
- TTL-based expiration per tenant
- Serialization/deserialization of CognitiveState
- Metrics collection for observability
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import redis.asyncio as redis
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram

from src.models.session import CognitiveStateData


# ============================================
# Prometheus Metrics
# ============================================

REDIS_OPERATIONS = Counter(
    "neurostack_redis_operations_total",
    "Total number of Redis operations",
    ["operation", "status"],
)

REDIS_LATENCY = Histogram(
    "neurostack_redis_latency_seconds",
    "Redis operation latency in seconds",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)


# ============================================
# Models
# ============================================


class SessionState(BaseModel):
    """
    Session state stored in Redis.
    
    This represents the real-time state of a conversation session,
    optimized for fast read/write access during message processing.
    """

    session_id: UUID
    tenant_id: UUID
    user_id: Optional[str] = None
    channel: str = "unknown"
    cognitive_state: dict[str, Any] = Field(default_factory=dict)
    session_metadata: dict[str, Any] = Field(default_factory=dict)
    message_count: int = 0
    is_active: bool = True
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    handoff_status: Optional[str] = None

    def get_cognitive_state_obj(self) -> CognitiveStateData:
        """Get typed CognitiveState object."""
        return CognitiveStateData.from_dict(self.cognitive_state)

    def set_cognitive_state_obj(self, state: CognitiveStateData) -> None:
        """Set cognitive state from typed object."""
        self.cognitive_state = state.to_dict()

    def update_activity(self) -> None:
        """Update last activity timestamp."""
        self.last_activity_at = datetime.now(timezone.utc)

    def increment_message_count(self) -> None:
        """Increment message count and update activity."""
        self.message_count += 1
        self.update_activity()


from src.core.exceptions import (
    StateNotFoundError,
    StateStoreConnectionError,
    StateStoreError,
)


# ============================================
# State Store Implementation
# ============================================


class StateStore:
    """
    Redis-based State Store for session state management.
    
    Provides fast OLTP operations for:
    - Session state CRUD
    - CognitiveState serialization/deserialization
    - TTL-based expiration per tenant
    - Metrics collection
    
    Key naming convention:
        neurostack:session:{tenant_id}:{session_id}
    
    Usage:
        async with StateStore() as store:
            await store.create_session(session_state)
            state = await store.get_session(tenant_id, session_id)
            await store.update_session(session_state)
            await store.delete_session(tenant_id, session_id)
    """

    # Key prefix for session states
    KEY_PREFIX = "neurostack:session"
    
    # Key prefix for tenant TTL configs
    TTL_PREFIX = "neurostack:tenant:ttl"
    
    # Index key for active sessions per tenant
    INDEX_PREFIX = "neurostack:tenant:sessions"

    def __init__(
        self,
        redis_url: Optional[str] = None,
        max_connections: Optional[int] = None,
        socket_timeout: Optional[float] = None,
        default_ttl: Optional[int] = None,
    ):
        """
        Initialize State Store.
        
        Args:
            redis_url: Redis connection URL (defaults to settings)
            max_connections: Maximum connection pool size
            socket_timeout: Socket timeout in seconds
            default_ttl: Default session TTL in seconds
        """
        # Import settings here to avoid circular import
        from src.config.settings import settings
        
        self._redis_url = redis_url or settings.redis_url
        self._max_connections = max_connections or settings.redis_max_connections
        self._socket_timeout = socket_timeout or settings.redis_socket_timeout
        self._default_ttl = default_ttl or settings.session_ttl_seconds
        self._pool: Optional[redis.ConnectionPool] = None
        self._redis: Optional[redis.Redis] = None

    async def connect(self) -> None:
        """Establish connection to Redis."""
        try:
            self._pool = redis.ConnectionPool.from_url(
                self._redis_url,
                max_connections=self._max_connections,
                socket_timeout=self._socket_timeout,
                decode_responses=True,
            )
            self._redis = redis.Redis(connection_pool=self._pool)
            # Test connection
            await self._redis.ping()
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="connect", status="error").inc()
            raise StateStoreConnectionError(f"Failed to connect to Redis: {e}") from e

    async def disconnect(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        if self._pool:
            await self._pool.disconnect()
            self._pool = None

    async def __aenter__(self) -> "StateStore":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()

    def _get_session_key(self, tenant_id: UUID, session_id: UUID) -> str:
        """Generate Redis key for session state."""
        return f"{self.KEY_PREFIX}:{tenant_id}:{session_id}"

    def _get_ttl_key(self, tenant_id: UUID) -> str:
        """Generate Redis key for tenant TTL config."""
        return f"{self.TTL_PREFIX}:{tenant_id}"

    def _get_index_key(self, tenant_id: UUID) -> str:
        """Generate Redis key for tenant session index."""
        return f"{self.INDEX_PREFIX}:{tenant_id}"

    def _serialize_session(self, state: SessionState) -> str:
        """Serialize SessionState to JSON string."""
        return state.model_dump_json()

    def _deserialize_session(self, data: str) -> SessionState:
        """Deserialize JSON string to SessionState."""
        return SessionState.model_validate_json(data)

    async def _get_tenant_ttl(self, tenant_id: UUID) -> int:
        """Get TTL for a specific tenant (with caching in Redis)."""
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        ttl_key = self._get_ttl_key(tenant_id)
        cached_ttl = await self._redis.get(ttl_key)
        
        if cached_ttl:
            return int(cached_ttl)
        
        # Return default TTL if not configured
        return self._default_ttl

    async def set_tenant_ttl(self, tenant_id: UUID, ttl_seconds: int) -> None:
        """
        Set custom TTL for a tenant.
        
        Args:
            tenant_id: Tenant identifier
            ttl_seconds: TTL in seconds
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        start_time = time.perf_counter()
        try:
            ttl_key = self._get_ttl_key(tenant_id)
            # TTL config never expires (or very long TTL)
            await self._redis.set(ttl_key, ttl_seconds, ex=86400 * 365)
            
            latency = time.perf_counter() - start_time
            REDIS_LATENCY.labels(operation="set_tenant_ttl").observe(latency)
            REDIS_OPERATIONS.labels(operation="set_tenant_ttl", status="success").inc()
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="set_tenant_ttl", status="error").inc()
            raise StateStoreError(f"Failed to set tenant TTL: {e}") from e

    # ============================================
    # CRUD Operations
    # ============================================

    async def create_session(
        self,
        state: SessionState,
        ttl_seconds: Optional[int] = None,
    ) -> SessionState:
        """
        Create a new session state in Redis.
        
        Args:
            state: Session state to create
            ttl_seconds: Optional TTL override
            
        Returns:
            Created session state
            
        Raises:
            StateStoreError: If creation fails
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        start_time = time.perf_counter()
        try:
            key = self._get_session_key(state.tenant_id, state.session_id)
            index_key = self._get_index_key(state.tenant_id)
            
            # Get TTL for this tenant
            ttl = ttl_seconds or await self._get_tenant_ttl(state.tenant_id)
            
            # Serialize and store
            data = self._serialize_session(state)
            
            # Use pipeline for atomic operation
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(key, data, ex=ttl)
                pipe.sadd(index_key, str(state.session_id))
                pipe.expire(index_key, ttl)
                await pipe.execute()
            
            latency = time.perf_counter() - start_time
            REDIS_LATENCY.labels(operation="create_session").observe(latency)
            REDIS_OPERATIONS.labels(operation="create_session", status="success").inc()
            
            return state
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="create_session", status="error").inc()
            raise StateStoreError(f"Failed to create session: {e}") from e

    async def get_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> SessionState:
        """
        Get session state from Redis.
        
        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            
        Returns:
            Session state
            
        Raises:
            StateNotFoundError: If session not found
            StateStoreError: If read fails
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        start_time = time.perf_counter()
        try:
            key = self._get_session_key(tenant_id, session_id)
            data = await self._redis.get(key)
            
            latency = time.perf_counter() - start_time
            REDIS_LATENCY.labels(operation="get_session").observe(latency)
            
            if data is None:
                REDIS_OPERATIONS.labels(operation="get_session", status="not_found").inc()
                raise StateNotFoundError(
                    f"Session {session_id} not found for tenant {tenant_id}"
                )
            
            REDIS_OPERATIONS.labels(operation="get_session", status="success").inc()
            return self._deserialize_session(data)
        except StateNotFoundError:
            raise
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="get_session", status="error").inc()
            raise StateStoreError(f"Failed to get session: {e}") from e

    async def get_session_or_none(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> Optional[SessionState]:
        """
        Get session state from Redis, returning None if not found.
        
        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            
        Returns:
            Session state or None
        """
        try:
            return await self.get_session(tenant_id, session_id)
        except StateNotFoundError:
            return None

    async def update_session(
        self,
        state: SessionState,
        ttl_seconds: Optional[int] = None,
        refresh_ttl: bool = True,
    ) -> SessionState:
        """
        Update session state in Redis.
        
        Args:
            state: Session state to update
            ttl_seconds: Optional TTL override
            refresh_ttl: Whether to refresh the TTL
            
        Returns:
            Updated session state
            
        Raises:
            StateStoreError: If update fails
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        start_time = time.perf_counter()
        try:
            key = self._get_session_key(state.tenant_id, state.session_id)
            
            # Update activity timestamp
            state.update_activity()
            
            # Serialize
            data = self._serialize_session(state)
            
            if refresh_ttl:
                ttl = ttl_seconds or await self._get_tenant_ttl(state.tenant_id)
                await self._redis.set(key, data, ex=ttl)
            else:
                # Keep existing TTL
                await self._redis.set(key, data, keepttl=True)
            
            latency = time.perf_counter() - start_time
            REDIS_LATENCY.labels(operation="update_session").observe(latency)
            REDIS_OPERATIONS.labels(operation="update_session", status="success").inc()
            
            return state
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="update_session", status="error").inc()
            raise StateStoreError(f"Failed to update session: {e}") from e

    async def delete_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> bool:
        """
        Delete session state from Redis.
        
        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            
        Returns:
            True if deleted, False if not found
            
        Raises:
            StateStoreError: If deletion fails
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        start_time = time.perf_counter()
        try:
            key = self._get_session_key(tenant_id, session_id)
            index_key = self._get_index_key(tenant_id)
            
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                pipe.srem(index_key, str(session_id))
                results = await pipe.execute()
            
            deleted = results[0] > 0
            
            latency = time.perf_counter() - start_time
            REDIS_LATENCY.labels(operation="delete_session").observe(latency)
            
            if deleted:
                REDIS_OPERATIONS.labels(operation="delete_session", status="success").inc()
            else:
                REDIS_OPERATIONS.labels(operation="delete_session", status="not_found").inc()
            
            return deleted
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="delete_session", status="error").inc()
            raise StateStoreError(f"Failed to delete session: {e}") from e

    async def exists(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> bool:
        """
        Check if session exists in Redis.
        
        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            
        Returns:
            True if exists, False otherwise
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        start_time = time.perf_counter()
        try:
            key = self._get_session_key(tenant_id, session_id)
            result = await self._redis.exists(key)
            
            latency = time.perf_counter() - start_time
            REDIS_LATENCY.labels(operation="exists").observe(latency)
            REDIS_OPERATIONS.labels(operation="exists", status="success").inc()
            
            return result > 0
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="exists", status="error").inc()
            raise StateStoreError(f"Failed to check session existence: {e}") from e

    # ============================================
    # Cognitive State Operations
    # ============================================

    async def get_cognitive_state(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> CognitiveStateData:
        """
        Get cognitive state for a session.
        
        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            
        Returns:
            CognitiveStateData object
            
        Raises:
            StateNotFoundError: If session not found
        """
        state = await self.get_session(tenant_id, session_id)
        return state.get_cognitive_state_obj()

    async def update_cognitive_state(
        self,
        tenant_id: UUID,
        session_id: UUID,
        cognitive_state: CognitiveStateData,
    ) -> SessionState:
        """
        Update cognitive state for a session.
        
        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            cognitive_state: New cognitive state
            
        Returns:
            Updated session state
            
        Raises:
            StateNotFoundError: If session not found
        """
        state = await self.get_session(tenant_id, session_id)
        state.set_cognitive_state_obj(cognitive_state)
        return await self.update_session(state)

    async def apply_state_patch(
        self,
        tenant_id: UUID,
        session_id: UUID,
        patch: dict[str, Any],
    ) -> SessionState:
        """
        Apply a partial update (patch) to cognitive state.
        
        This is useful for SRM state_patch operations.
        
        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            patch: Dictionary with updates to apply
            
        Returns:
            Updated session state
            
        Raises:
            StateNotFoundError: If session not found
        """
        state = await self.get_session(tenant_id, session_id)
        cognitive = state.get_cognitive_state_obj()
        
        # Apply patch fields
        if "funnel_stage" in patch:
            cognitive.funnel_stage = patch["funnel_stage"]
        if "last_intent" in patch:
            cognitive.add_intent(patch["last_intent"])
        if "slots" in patch:
            for key, value in patch["slots"].items():
                cognitive.update_slot(key, value)
        if "flags" in patch:
            for key, value in patch["flags"].items():
                cognitive.set_flag(key, value)
        if "safety_trigger" in patch and patch["safety_trigger"]:
            cognitive.trigger_safety()
        if "confidence" in patch:
            cognitive.update_confidence(patch["confidence"])
        if (
            "sentiment_label" in patch
            or "sentiment_score" in patch
            or "sentiment_confidence" in patch
        ):
            cognitive.update_sentiment(
                label=patch.get("sentiment_label"),
                score=patch.get("sentiment_score"),
                confidence=patch.get("sentiment_confidence"),
            )
        
        state.set_cognitive_state_obj(cognitive)
        return await self.update_session(state)

    # ============================================
    # Tenant Operations
    # ============================================

    async def list_sessions(
        self,
        tenant_id: UUID,
        limit: int = 100,
    ) -> list[UUID]:
        """
        List active session IDs for a tenant.
        
        Args:
            tenant_id: Tenant identifier
            limit: Maximum number of sessions to return
            
        Returns:
            List of session UUIDs
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        start_time = time.perf_counter()
        try:
            index_key = self._get_index_key(tenant_id)
            members = await self._redis.smembers(index_key)
            
            session_ids = [UUID(m) for m in list(members)[:limit]]
            
            latency = time.perf_counter() - start_time
            REDIS_LATENCY.labels(operation="list_sessions").observe(latency)
            REDIS_OPERATIONS.labels(operation="list_sessions", status="success").inc()
            
            return session_ids
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="list_sessions", status="error").inc()
            raise StateStoreError(f"Failed to list sessions: {e}") from e

    async def count_sessions(self, tenant_id: UUID) -> int:
        """
        Count active sessions for a tenant.
        
        Args:
            tenant_id: Tenant identifier
            
        Returns:
            Number of active sessions
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        start_time = time.perf_counter()
        try:
            index_key = self._get_index_key(tenant_id)
            count = await self._redis.scard(index_key)
            
            latency = time.perf_counter() - start_time
            REDIS_LATENCY.labels(operation="count_sessions").observe(latency)
            REDIS_OPERATIONS.labels(operation="count_sessions", status="success").inc()
            
            return count
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="count_sessions", status="error").inc()
            raise StateStoreError(f"Failed to count sessions: {e}") from e

    async def get_session_ttl(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> int:
        """
        Get remaining TTL for a session.
        
        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            
        Returns:
            Remaining TTL in seconds (-1 if no expiry, -2 if not found)
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        key = self._get_session_key(tenant_id, session_id)
        return await self._redis.ttl(key)

    async def refresh_session_ttl(
        self,
        tenant_id: UUID,
        session_id: UUID,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        """
        Refresh TTL for a session without updating data.
        
        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            ttl_seconds: Optional TTL override
            
        Returns:
            True if refreshed, False if session not found
        """
        if not self._redis:
            raise StateStoreConnectionError("Not connected to Redis")

        start_time = time.perf_counter()
        try:
            key = self._get_session_key(tenant_id, session_id)
            ttl = ttl_seconds or await self._get_tenant_ttl(tenant_id)
            
            result = await self._redis.expire(key, ttl)
            
            latency = time.perf_counter() - start_time
            REDIS_LATENCY.labels(operation="refresh_ttl").observe(latency)
            REDIS_OPERATIONS.labels(operation="refresh_ttl", status="success").inc()
            
            return result
        except redis.RedisError as e:
            REDIS_OPERATIONS.labels(operation="refresh_ttl", status="error").inc()
            raise StateStoreError(f"Failed to refresh TTL: {e}") from e

    # ============================================
    # Health Check
    # ============================================

    async def health_check(self) -> dict[str, Any]:
        """
        Perform health check on Redis connection.
        
        Returns:
            Health check result with status and latency
        """
        if not self._redis:
            return {
                "status": "unhealthy",
                "error": "Not connected",
            }

        start_time = time.perf_counter()
        try:
            await self._redis.ping()
            latency = time.perf_counter() - start_time
            
            info = await self._redis.info("server")
            
            return {
                "status": "healthy",
                "latency_ms": round(latency * 1000, 2),
                "redis_version": info.get("redis_version", "unknown"),
            }
        except redis.RedisError as e:
            return {
                "status": "unhealthy",
                "error": str(e),
            }


# ============================================
# Singleton Instance
# ============================================

# Global state store instance (to be initialized at app startup)
_state_store: Optional[StateStore] = None


async def get_state_store() -> StateStore:
    """
    Get or create the global StateStore instance.
    
    Returns:
        StateStore instance
    """
    global _state_store
    if _state_store is None:
        _state_store = StateStore()
        await _state_store.connect()
    return _state_store


async def close_state_store() -> None:
    """Close the global StateStore instance."""
    global _state_store
    if _state_store is not None:
        await _state_store.disconnect()
        _state_store = None

