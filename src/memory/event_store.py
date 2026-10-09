"""
NeuroStack - Memory Architecture 2.0 - Event Store
Append-only Event Store implementation using PostgreSQL for audit and analytics.

The Event Store provides:
- Append-only event persistence (immutable log)
- Query events by session, tenant, type, and time range
- Analytics aggregations
- Training data generation support
"""

import asyncio
import time
from datetime import datetime, timezone
from enum import Enum
from types import TracebackType
from typing import Any, Optional, cast
from uuid import UUID, uuid4

from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config.logging import get_logger
from src.core.exceptions import EventStoreConnectionError, EventStoreError
from src.core.interaction_context import InteractionOrigin

logger = get_logger(__name__)


# ============================================
# Prometheus Metrics
# ============================================

EVENT_STORE_OPERATIONS = Counter(
    "neurostack_event_store_operations_total",
    "Total number of Event Store operations",
    ["operation", "status"],
)

EVENT_STORE_LATENCY = Histogram(
    "neurostack_event_store_latency_seconds",
    "Event Store operation latency in seconds",
    ["operation"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

EVENTS_APPENDED = Counter(
    "neurostack_events_appended_total",
    "Total number of events appended",
    ["event_type"],
)


# ============================================
# Enums
# ============================================


class EventType(str, Enum):
    """Types of events stored in the Event Store."""

    # Message Events
    MESSAGE_IN = "message.in"
    MESSAGE_OUT = "message.out"

    # Session Lifecycle
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    SESSION_TIMEOUT = "session.timeout"

    # Cognitive Events
    SRM_DECISION = "srm.decision"
    STATE_PATCH = "state.patch"
    FUNNEL_TRANSITION = "funnel.transition"

    # Flow Events
    FLOW_TRANSITION = "flow.transition"
    MODE_CHANGE = "mode.change"

    # Safety Events
    SAFETY_TRIGGER = "safety.trigger"
    SAFETY_BLOCK = "safety.block"

    # Handoff Events
    HANDOFF_REQUEST = "handoff.request"
    HANDOFF_ACCEPT = "handoff.accept"
    HANDOFF_COMPLETE = "handoff.complete"
    HANDOFF_TIMEOUT = "handoff.timeout"

    # RAG Events
    RAG_QUERY = "rag.query"
    RAG_RESULT = "rag.result"

    # Policy Events
    POLICY_APPLIED = "policy.applied"
    POLICY_VIOLATION = "policy.violation"

    # Prompt Events
    PROMPT_GENERATED = "prompt.generated"

    # CRM integration (doc MVP v0.5)
    NS_FUNNEL_STAGE_CHANGED = "ns.funnel_stage_changed"
    NS_HANDOFF_CREATED = "ns.handoff_created"

    # System Events
    ERROR = "error"
    WARNING = "warning"


# ============================================
# Pydantic Models
# ============================================


class EventData(BaseModel):
    """Base model for event data stored in the Event Store."""

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    session_id: Optional[UUID] = None
    event_type: str
    interaction_origin: InteractionOrigin = InteractionOrigin.UNKNOWN
    event_data: dict[str, Any] = Field(default_factory=dict)
    event_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_insert_dict(self) -> dict[str, Any]:
        """Convert to dictionary for database insert."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "interaction_origin": self.interaction_origin.value,
            "event_data": self.event_data,
            "metadata": self.event_metadata,
            "created_at": self.created_at,
        }


class MessageEventData(BaseModel):
    """Event data for message events."""

    content: str
    channel: str
    user_id: Optional[str] = None
    role: str = "user"  # "user" or "assistant"


class SRMDecisionEventData(BaseModel):
    """Event data for SRM decision events."""

    intent: str
    confidence: float
    action: str
    needs_rag: bool = False
    needs_handoff: bool = False
    safety_concern: bool = False
    state_patch: Optional[dict[str, Any]] = None
    processing_time_ms: Optional[int] = None


class SafetyEventData(BaseModel):
    """Event data for safety events."""

    safety_category: str
    confidence: float
    trigger_content: str
    action_taken: str


class HandoffEventData(BaseModel):
    """Event data for handoff events."""

    reason: str
    summary: str
    urgency: str = "normal"
    key_info: dict[str, Any] = Field(default_factory=dict)


class FunnelTransitionEventData(BaseModel):
    """Event data for funnel transition events."""

    from_stage: str
    to_stage: str
    trigger_intent: str


class EventQueryResult(BaseModel):
    """Result model for event queries."""

    events: list[EventData]
    total_count: int
    has_more: bool


class EventAggregation(BaseModel):
    """Model for event aggregation results."""

    event_type: str
    count: int
    first_event: Optional[datetime] = None
    last_event: Optional[datetime] = None


# ============================================
# Event Store Implementation
# ============================================


class EventStore:
    """
    PostgreSQL-based Event Store for append-only event logging.

    Provides:
    - Append events (single and batch)
    - Query events by session, tenant, type, and time range
    - Event count and aggregation
    - Analytics support

    The Event Store is append-only by design:
    - Events are never updated or deleted
    - Provides complete audit trail
    - Supports event sourcing patterns

    Usage:
        async with EventStore() as store:
            await store.append(event)
            events = await store.query_by_session(tenant_id, session_id)
    """

    def __init__(
        self,
        database_url: Optional[str] = None,
        pool_size: Optional[int] = None,
        max_overflow: Optional[int] = None,
    ) -> None:
        """
        Initialize Event Store.

        Args:
            database_url: PostgreSQL connection URL
            pool_size: Connection pool size
            max_overflow: Maximum pool overflow
        """
        # Import settings here to avoid circular import
        from src.config.settings import settings

        self._database_url = database_url or settings.database_url
        self._pool_size = pool_size or settings.database_pool_size
        self._max_overflow = max_overflow or settings.database_max_overflow
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self._connection_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Establish a probed connection without publishing partial state."""
        # Import settings here to avoid circular import
        from src.config.settings import settings

        async with self._connection_lock:
            if self._engine is not None and self._session_factory is not None:
                return

            engine: AsyncEngine | None = None
            try:
                engine = create_async_engine(
                    self._database_url,
                    pool_size=self._pool_size,
                    max_overflow=self._max_overflow,
                    echo=settings.debug,
                )
                session_factory = async_sessionmaker(
                    engine,
                    class_=AsyncSession,
                    expire_on_commit=False,
                )
                async with engine.begin() as conn:
                    await conn.execute(text("SELECT 1"))
            except BaseException as exc:
                self._engine = None
                self._session_factory = None
                if engine is not None:
                    try:
                        await engine.dispose()
                    except Exception as dispose_exc:
                        logger.error(
                            "Failed to dispose rejected Event Store engine",
                            error_type=type(dispose_exc).__name__,
                            exc_info=True,
                        )
                if not isinstance(exc, Exception):
                    raise
                EVENT_STORE_OPERATIONS.labels(operation="connect", status="error").inc()
                if not isinstance(exc, SQLAlchemyError):
                    logger.error(
                        "Unexpected error connecting to Event Store",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        exc_info=True,
                    )
                raise EventStoreConnectionError(
                    f"Failed to connect to PostgreSQL: {exc}"
                ) from exc

            self._engine = engine
            self._session_factory = session_factory

    async def disconnect(self) -> None:
        """Close PostgreSQL connection and clear published state first."""
        async with self._connection_lock:
            engine = self._engine
            self._engine = None
            self._session_factory = None
            if engine is not None:
                await engine.dispose()

    async def __aenter__(self) -> "EventStore":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        """Async context manager exit."""
        await self.disconnect()

    def _get_session(self) -> AsyncSession:
        """Get a database session."""
        if not self._session_factory:
            raise EventStoreConnectionError("Not connected to PostgreSQL")
        return self._session_factory()

    @staticmethod
    def _row_to_event_data(row: Any) -> EventData:
        """Reconstruct an event without silently discarding provenance."""
        return EventData(
            id=row.id,
            tenant_id=row.tenant_id,
            session_id=row.session_id,
            event_type=row.event_type,
            interaction_origin=row.interaction_origin,
            event_data=row.event_data,
            event_metadata=row.metadata,
            created_at=row.created_at,
        )

    # ============================================
    # Append Operations
    # ============================================

    async def append(self, event: EventData) -> EventData:
        """
        Append a single event to the Event Store.

        Args:
            event: Event to append

        Returns:
            Appended event with generated ID

        Raises:
            EventStoreError: If append fails
        """
        start_time = time.perf_counter()
        try:
            async with self._get_session() as session:
                import json

                # Use bindparam to properly handle JSONB casting
                query = text("""
                    INSERT INTO events (id, tenant_id, session_id, event_type, interaction_origin, event_data, metadata, created_at)
                    VALUES (:id, :tenant_id, :session_id, :event_type, :interaction_origin, CAST(:event_data AS jsonb), CAST(:metadata AS jsonb), :created_at)
                    RETURNING id, created_at
                """)

                result = await session.execute(
                    query,
                    {
                        "id": event.id,
                        "tenant_id": event.tenant_id,
                        "session_id": event.session_id,
                        "event_type": event.event_type,
                        "interaction_origin": event.interaction_origin.value,
                        "event_data": json.dumps(event.event_data),
                        "metadata": json.dumps(event.event_metadata),
                        "created_at": event.created_at,
                    },
                )
                await session.commit()
                row = result.fetchone()
                if row is None:
                    raise EventStoreError("Append returned no event row")
                event.created_at = row.created_at

            latency = time.perf_counter() - start_time
            EVENT_STORE_LATENCY.labels(operation="append").observe(latency)
            EVENT_STORE_OPERATIONS.labels(operation="append", status="success").inc()
            EVENTS_APPENDED.labels(event_type=event.event_type).inc()

            logger.debug(
                "Event appended to Event Store",
                event_id=str(event.id),
                event_type=event.event_type,
                tenant_id=str(event.tenant_id),
                session_id=str(event.session_id),
                latency_ms=round(latency * 1000, 2),
            )

            return event
        except SQLAlchemyError as e:
            EVENT_STORE_OPERATIONS.labels(operation="append", status="error").inc()
            logger.error(
                "Database error appending event to Event Store",
                event_id=str(event.id),
                event_type=event.event_type,
                tenant_id=str(event.tenant_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to append event: {e}") from e
        except Exception as e:
            EVENT_STORE_OPERATIONS.labels(operation="append", status="error").inc()
            logger.error(
                "Unexpected error appending event to Event Store",
                event_id=str(event.id),
                event_type=event.event_type,
                tenant_id=str(event.tenant_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to append event: {e}") from e

    async def append_batch(self, events: list[EventData]) -> list[EventData]:
        """
        Append multiple events in a single transaction.

        Args:
            events: List of events to append

        Returns:
            List of appended events

        Raises:
            EventStoreError: If append fails
        """
        if not events:
            return []

        start_time = time.perf_counter()
        try:
            import json

            async with self._get_session() as session:
                for event in events:
                    query = text("""
                        INSERT INTO events (id, tenant_id, session_id, event_type, interaction_origin, event_data, metadata, created_at)
                        VALUES (:id, :tenant_id, :session_id, :event_type, :interaction_origin, CAST(:event_data AS jsonb), CAST(:metadata AS jsonb), :created_at)
                    """)
                    await session.execute(
                        query,
                        {
                            "id": event.id,
                            "tenant_id": event.tenant_id,
                            "session_id": event.session_id,
                            "event_type": event.event_type,
                            "interaction_origin": event.interaction_origin.value,
                            "event_data": json.dumps(event.event_data),
                            "metadata": json.dumps(event.event_metadata),
                            "created_at": event.created_at,
                        },
                    )
                await session.commit()

            latency = time.perf_counter() - start_time
            EVENT_STORE_LATENCY.labels(operation="append_batch").observe(latency)
            EVENT_STORE_OPERATIONS.labels(
                operation="append_batch", status="success"
            ).inc()

            for event in events:
                EVENTS_APPENDED.labels(event_type=event.event_type).inc()

            return events
        except SQLAlchemyError as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="append_batch", status="error"
            ).inc()
            logger.error(
                "Database error appending batch to Event Store",
                batch_size=len(events),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to append batch: {e}") from e
        except Exception as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="append_batch", status="error"
            ).inc()
            logger.error(
                "Unexpected error appending batch to Event Store",
                batch_size=len(events),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to append batch: {e}") from e

    # ============================================
    # Query Operations
    # ============================================

    async def query_by_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
        event_types: Optional[list[str]] = None,
        limit: int = 100,
        offset: int = 0,
        order_desc: bool = True,
    ) -> EventQueryResult:
        """
        Query events by session ID.

        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            event_types: Optional filter by event types
            limit: Maximum number of events to return
            offset: Number of events to skip
            order_desc: Order by created_at descending (newest first)

        Returns:
            EventQueryResult with events and pagination info
        """
        start_time = time.perf_counter()
        try:
            async with self._get_session() as session:
                # Build query
                order = "DESC" if order_desc else "ASC"
                type_filter = ""
                params: dict[str, Any] = {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "limit": limit + 1,  # Fetch one extra to check has_more
                    "offset": offset,
                }

                if event_types:
                    type_filter = "AND event_type = ANY(:event_types)"
                    params["event_types"] = event_types

                query = text(f"""
                    SELECT id, tenant_id, session_id, event_type, interaction_origin, event_data, metadata, created_at
                    FROM events
                    WHERE tenant_id = :tenant_id AND session_id = :session_id
                    {type_filter}
                    ORDER BY created_at {order}
                    LIMIT :limit OFFSET :offset
                """)

                result = await session.execute(query, params)
                rows = result.fetchall()

                # Check if there are more results
                has_more = len(rows) > limit
                if has_more:
                    rows = rows[:limit]

                events = [self._row_to_event_data(row) for row in rows]

                # Get total count
                count_query = text(f"""
                    SELECT COUNT(*) as total
                    FROM events
                    WHERE tenant_id = :tenant_id AND session_id = :session_id
                    {type_filter}
                """)
                count_result = await session.execute(
                    count_query,
                    {
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        **({} if not event_types else {"event_types": event_types}),
                    },
                )
                total_count = count_result.scalar()

            latency = time.perf_counter() - start_time
            EVENT_STORE_LATENCY.labels(operation="query_by_session").observe(latency)
            EVENT_STORE_OPERATIONS.labels(
                operation="query_by_session", status="success"
            ).inc()

            return EventQueryResult(
                events=events,
                total_count=total_count or 0,
                has_more=has_more,
            )
        except SQLAlchemyError as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="query_by_session", status="error"
            ).inc()
            logger.error(
                "Database error querying events by session",
                tenant_id=str(tenant_id),
                session_id=str(session_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to query events: {e}") from e
        except Exception as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="query_by_session", status="error"
            ).inc()
            logger.error(
                "Unexpected error querying events by session",
                tenant_id=str(tenant_id),
                session_id=str(session_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to query events: {e}") from e

    async def query_by_tenant(
        self,
        tenant_id: UUID,
        event_types: Optional[list[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> EventQueryResult:
        """
        Query events by tenant ID with optional filters.

        Args:
            tenant_id: Tenant identifier
            event_types: Optional filter by event types
            start_time: Optional start time filter
            end_time: Optional end time filter
            limit: Maximum number of events to return
            offset: Number of events to skip

        Returns:
            EventQueryResult with events and pagination info
        """
        query_start = time.perf_counter()
        try:
            async with self._get_session() as session:
                filters = ["tenant_id = :tenant_id"]
                params: dict[str, Any] = {
                    "tenant_id": tenant_id,
                    "limit": limit + 1,
                    "offset": offset,
                }

                if event_types:
                    filters.append("event_type = ANY(:event_types)")
                    params["event_types"] = event_types
                if start_time:
                    filters.append("created_at >= :start_time")
                    params["start_time"] = start_time
                if end_time:
                    filters.append("created_at <= :end_time")
                    params["end_time"] = end_time

                where_clause = " AND ".join(filters)

                query = text(f"""
                    SELECT id, tenant_id, session_id, event_type, interaction_origin, event_data, metadata, created_at
                    FROM events
                    WHERE {where_clause}
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                """)

                result = await session.execute(query, params)
                rows = result.fetchall()

                has_more = len(rows) > limit
                if has_more:
                    rows = rows[:limit]

                events = [self._row_to_event_data(row) for row in rows]

                # Get total count
                count_params = {
                    k: v for k, v in params.items() if k not in ("limit", "offset")
                }
                count_query = text(f"""
                    SELECT COUNT(*) as total
                    FROM events
                    WHERE {where_clause}
                """)
                count_result = await session.execute(count_query, count_params)
                total_count = count_result.scalar()

            latency = time.perf_counter() - query_start
            EVENT_STORE_LATENCY.labels(operation="query_by_tenant").observe(latency)
            EVENT_STORE_OPERATIONS.labels(
                operation="query_by_tenant", status="success"
            ).inc()

            return EventQueryResult(
                events=events,
                total_count=total_count or 0,
                has_more=has_more,
            )
        except SQLAlchemyError as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="query_by_tenant", status="error"
            ).inc()
            logger.error(
                "Database error querying events by tenant",
                tenant_id=str(tenant_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to query events: {e}") from e
        except Exception as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="query_by_tenant", status="error"
            ).inc()
            logger.error(
                "Unexpected error querying events by tenant",
                tenant_id=str(tenant_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to query events: {e}") from e

    async def get_event(self, event_id: UUID) -> Optional[EventData]:
        """
        Get a single event by ID.

        Args:
            event_id: Event identifier

        Returns:
            Event or None if not found
        """
        start_time = time.perf_counter()
        try:
            async with self._get_session() as session:
                query = text("""
                    SELECT id, tenant_id, session_id, event_type, interaction_origin, event_data, metadata, created_at
                    FROM events
                    WHERE id = :event_id
                """)
                result = await session.execute(query, {"event_id": event_id})
                row = result.fetchone()

            latency = time.perf_counter() - start_time
            EVENT_STORE_LATENCY.labels(operation="get_event").observe(latency)

            if row:
                EVENT_STORE_OPERATIONS.labels(
                    operation="get_event", status="success"
                ).inc()
                return self._row_to_event_data(row)
            else:
                EVENT_STORE_OPERATIONS.labels(
                    operation="get_event", status="not_found"
                ).inc()
                return None
        except SQLAlchemyError as e:
            EVENT_STORE_OPERATIONS.labels(operation="get_event", status="error").inc()
            logger.error(
                "Database error getting event",
                event_id=str(event_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to get event: {e}") from e
        except Exception as e:
            EVENT_STORE_OPERATIONS.labels(operation="get_event", status="error").inc()
            logger.error(
                "Unexpected error getting event",
                event_id=str(event_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to get event: {e}") from e

    async def get_recent_messages(
        self,
        tenant_id: UUID,
        session_id: UUID,
        limit: int = 10,
    ) -> list[EventData]:
        """
        Get recent message events for a session.

        Args:
            tenant_id: Tenant identifier
            session_id: Session identifier
            limit: Maximum number of messages

        Returns:
            List of message events (oldest first)
        """
        start_time = time.perf_counter()
        try:
            async with self._get_session() as session:
                query = text("""
                    SELECT id, tenant_id, session_id, event_type, interaction_origin,
                           event_data, metadata, created_at
                    FROM events
                    WHERE tenant_id = :tenant_id
                      AND session_id = :session_id
                      AND event_type = ANY(:event_types)
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                    """)
                result = await session.execute(
                    query,
                    {
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "event_types": [
                            EventType.MESSAGE_IN.value,
                            EventType.MESSAGE_OUT.value,
                        ],
                        "limit": limit,
                    },
                )
                rows = result.fetchall()
                events = [self._row_to_event_data(row) for row in rows]

            latency = time.perf_counter() - start_time
            EVENT_STORE_LATENCY.labels(operation="get_recent_messages").observe(latency)
            EVENT_STORE_OPERATIONS.labels(
                operation="get_recent_messages", status="success"
            ).inc()
            return list(reversed(events))
        except SQLAlchemyError as exc:
            EVENT_STORE_OPERATIONS.labels(
                operation="get_recent_messages", status="error"
            ).inc()
            raise EventStoreError(f"Failed to query recent messages: {exc}") from exc
        except Exception as exc:
            EVENT_STORE_OPERATIONS.labels(
                operation="get_recent_messages", status="error"
            ).inc()
            raise EventStoreError(f"Failed to query recent messages: {exc}") from exc

    # ============================================
    # Aggregation Operations
    # ============================================

    async def count_by_type(
        self,
        tenant_id: UUID,
        session_id: Optional[UUID] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[EventAggregation]:
        """
        Count events grouped by type.

        Args:
            tenant_id: Tenant identifier
            session_id: Optional session filter
            start_time: Optional start time filter
            end_time: Optional end time filter

        Returns:
            List of event type counts
        """
        query_start = time.perf_counter()
        try:
            async with self._get_session() as session:
                filters = ["tenant_id = :tenant_id"]
                params: dict[str, Any] = {"tenant_id": tenant_id}

                if session_id:
                    filters.append("session_id = :session_id")
                    params["session_id"] = session_id
                if start_time:
                    filters.append("created_at >= :start_time")
                    params["start_time"] = start_time
                if end_time:
                    filters.append("created_at <= :end_time")
                    params["end_time"] = end_time

                where_clause = " AND ".join(filters)

                query = text(f"""
                    SELECT
                        event_type,
                        COUNT(*) as count,
                        MIN(created_at) as first_event,
                        MAX(created_at) as last_event
                    FROM events
                    WHERE {where_clause}
                    GROUP BY event_type
                    ORDER BY count DESC
                """)

                result = await session.execute(query, params)
                rows = result.fetchall()

            latency = time.perf_counter() - query_start
            EVENT_STORE_LATENCY.labels(operation="count_by_type").observe(latency)
            EVENT_STORE_OPERATIONS.labels(
                operation="count_by_type", status="success"
            ).inc()

            return [
                EventAggregation(
                    event_type=cast(str, row.event_type),
                    count=cast(int, row.count),
                    first_event=cast(Optional[datetime], row.first_event),
                    last_event=cast(Optional[datetime], row.last_event),
                )
                for row in rows
            ]
        except SQLAlchemyError as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="count_by_type", status="error"
            ).inc()
            logger.error(
                "Database error counting events by type",
                tenant_id=str(tenant_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to count events: {e}") from e
        except Exception as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="count_by_type", status="error"
            ).inc()
            logger.error(
                "Unexpected error counting events by type",
                tenant_id=str(tenant_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to count events: {e}") from e

    async def count_sessions_with_events(
        self,
        tenant_id: UUID,
        event_type: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> int:
        """
        Count unique sessions with a specific event type.

        Args:
            tenant_id: Tenant identifier
            event_type: Event type to filter
            start_time: Optional start time filter
            end_time: Optional end time filter

        Returns:
            Number of unique sessions
        """
        query_start = time.perf_counter()
        try:
            async with self._get_session() as session:
                filters = ["tenant_id = :tenant_id", "event_type = :event_type"]
                params: dict[str, Any] = {
                    "tenant_id": tenant_id,
                    "event_type": event_type,
                }

                if start_time:
                    filters.append("created_at >= :start_time")
                    params["start_time"] = start_time
                if end_time:
                    filters.append("created_at <= :end_time")
                    params["end_time"] = end_time

                where_clause = " AND ".join(filters)

                query = text(f"""
                    SELECT COUNT(DISTINCT session_id) as count
                    FROM events
                    WHERE {where_clause}
                """)

                result = await session.execute(query, params)
                count = result.scalar()

            latency = time.perf_counter() - query_start
            EVENT_STORE_LATENCY.labels(operation="count_sessions").observe(latency)
            EVENT_STORE_OPERATIONS.labels(
                operation="count_sessions", status="success"
            ).inc()

            return count or 0
        except SQLAlchemyError as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="count_sessions", status="error"
            ).inc()
            logger.error(
                "Database error counting sessions with events",
                tenant_id=str(tenant_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to count sessions: {e}") from e
        except Exception as e:
            EVENT_STORE_OPERATIONS.labels(
                operation="count_sessions", status="error"
            ).inc()
            logger.error(
                "Unexpected error counting sessions with events",
                tenant_id=str(tenant_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise EventStoreError(f"Failed to count sessions: {e}") from e

    # ============================================
    # Factory Methods for Common Events
    # ============================================

    def create_message_event(
        self,
        tenant_id: UUID,
        session_id: UUID,
        content: str,
        channel: str,
        is_incoming: bool = True,
        user_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> EventData:
        """Create a message event."""
        event_type = EventType.MESSAGE_IN if is_incoming else EventType.MESSAGE_OUT
        return EventData(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=event_type.value,
            event_data={
                "content": content,
                "channel": channel,
                "user_id": user_id,
                "role": "user" if is_incoming else "assistant",
            },
            event_metadata=metadata or {},
        )

    def create_srm_decision_event(
        self,
        tenant_id: UUID,
        session_id: UUID,
        decision: SRMDecisionEventData,
    ) -> EventData:
        """Create an SRM decision event."""
        return EventData(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=EventType.SRM_DECISION.value,
            event_data=decision.model_dump(),
            event_metadata={},
        )

    def create_safety_event(
        self,
        tenant_id: UUID,
        session_id: UUID,
        safety_data: SafetyEventData,
    ) -> EventData:
        """Create a safety trigger event."""
        return EventData(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=EventType.SAFETY_TRIGGER.value,
            event_data=safety_data.model_dump(),
            event_metadata={},
        )

    def create_handoff_event(
        self,
        tenant_id: UUID,
        session_id: UUID,
        handoff_data: HandoffEventData,
    ) -> EventData:
        """Create a handoff request event."""
        return EventData(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=EventType.HANDOFF_REQUEST.value,
            event_data=handoff_data.model_dump(),
            event_metadata={},
        )

    def create_funnel_transition_event(
        self,
        tenant_id: UUID,
        session_id: UUID,
        transition: FunnelTransitionEventData,
    ) -> EventData:
        """Create a funnel transition event."""
        return EventData(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=EventType.FUNNEL_TRANSITION.value,
            event_data=transition.model_dump(),
            event_metadata={},
        )

    def create_flow_transition_event(
        self,
        tenant_id: UUID,
        session_id: UUID,
        from_mode: str,
        to_mode: str,
        trigger: str,
    ) -> EventData:
        """Create a flow/mode transition event."""
        return EventData(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=EventType.FLOW_TRANSITION.value,
            event_data={
                "from_mode": from_mode,
                "to_mode": to_mode,
                "trigger": trigger,
            },
            event_metadata={},
        )

    def create_error_event(
        self,
        tenant_id: UUID,
        session_id: Optional[UUID],
        error_type: str,
        error_message: str,
        stack_trace: Optional[str] = None,
    ) -> EventData:
        """Create an error event."""
        return EventData(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=EventType.ERROR.value,
            event_data={
                "error_type": error_type,
                "error_message": error_message,
                "stack_trace": stack_trace,
            },
            event_metadata={},
        )

    # ============================================
    # Health Check
    # ============================================

    async def health_check(self) -> dict[str, Any]:
        """
        Perform health check on PostgreSQL connection.

        Returns:
            Health check result with status and latency
        """
        if not self._engine:
            return {
                "status": "unhealthy",
                "error": "Not connected",
            }

        start_time = time.perf_counter()
        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(text("SELECT version()"))
                version = result.scalar()

            latency = time.perf_counter() - start_time

            return {
                "status": "healthy",
                "latency_ms": round(latency * 1000, 2),
                "postgres_version": version.split()[1] if version else "unknown",
            }
        except SQLAlchemyError as e:
            logger.warning(
                "Database error during health check",
                error=str(e),
                error_type=type(e).__name__,
            )
            return {
                "status": "unhealthy",
                "error": f"Database error: {str(e)}",
            }
        except Exception as e:
            logger.warning(
                "Unexpected error during health check",
                error=str(e),
                error_type=type(e).__name__,
            )
            return {
                "status": "unhealthy",
                "error": str(e),
            }


# ============================================
# Singleton Instance
# ============================================

_event_store: Optional[EventStore] = None
_event_store_lifecycle_lock = asyncio.Lock()


async def get_event_store() -> EventStore:
    """
    Get or create the global EventStore instance.

    Returns:
        EventStore instance
    """
    global _event_store
    async with _event_store_lifecycle_lock:
        if _event_store is not None:
            return _event_store

        candidate = EventStore()
        try:
            await candidate.connect()
        except BaseException:
            try:
                await candidate.disconnect()
            except Exception:
                logger.error(
                    "Failed to dispose rejected global Event Store candidate",
                    exc_info=True,
                )
            raise
        _event_store = candidate
        return candidate


async def close_event_store() -> None:
    """Close the global EventStore instance."""
    global _event_store
    async with _event_store_lifecycle_lock:
        store = _event_store
        _event_store = None
        if store is not None:
            await store.disconnect()
