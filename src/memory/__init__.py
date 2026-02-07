"""
NeuroStack - Memory Architecture 2.0
OLTP State Store and Event Store

Memory 2.0 provides:
- State Store (Redis): Fast OLTP operations for session state
- Event Store (PostgreSQL): Append-only event log for audit and analytics

Usage:
    from src.memory import StateStore, get_state_store, close_state_store
    from src.memory import EventStore, get_event_store, close_event_store
    from src.memory.models import SessionState, CognitiveState, StatePatch

    # State Store as context manager
    async with StateStore() as store:
        await store.create_session(session_state)
        state = await store.get_session(tenant_id, session_id)

    # Event Store as context manager
    async with EventStore() as store:
        await store.append(event)
        events = await store.query_by_session(tenant_id, session_id)

    # Or use global instances
    state_store = await get_state_store()
    event_store = await get_event_store()
"""

from src.core.exceptions import (
    EventStoreConnectionError,
    EventStoreError,
    StateNotFoundError,
    StateStoreConnectionError,
    StateStoreError,
)
from src.memory.state_store import (
    SessionState,
    StateStore,
    get_state_store,
    close_state_store,
)
from src.memory.event_store import (
    EventAggregation,
    EventData,
    EventQueryResult,
    EventStore,
    EventType,
    FunnelTransitionEventData,
    HandoffEventData,
    MessageEventData,
    SRMDecisionEventData,
    SafetyEventData,
    get_event_store,
    close_event_store,
)
from src.memory.models import (
    CognitiveState,
    FunnelStage,
    FlowMode,
    HandoffStatus,
    LeadInfo,
    SalesInfo,
    SalesProfile,
    LastDecision,
    SessionStateCreate,
    SessionStateUpdate,
    StatePatch,
    SessionStateResponse,
    RecentMessage,
    SessionContext,
    StateStoreStats,
)

__all__ = [
    # State Store
    "StateStore",
    "StateStoreError",
    "StateNotFoundError",
    "StateStoreConnectionError",
    "SessionState",
    "get_state_store",
    "close_state_store",
    # Event Store
    "EventStore",
    "EventStoreError",
    "EventStoreConnectionError",
    "EventData",
    "EventType",
    "EventQueryResult",
    "EventAggregation",
    "MessageEventData",
    "SRMDecisionEventData",
    "SafetyEventData",
    "HandoffEventData",
    "FunnelTransitionEventData",
    "get_event_store",
    "close_event_store",
    # Models
    "CognitiveState",
    "FunnelStage",
    "FlowMode",
    "HandoffStatus",
    "LeadInfo",
    "SalesInfo",
    "SalesProfile",
    "LastDecision",
    "SessionStateCreate",
    "SessionStateUpdate",
    "StatePatch",
    "SessionStateResponse",
    "RecentMessage",
    "SessionContext",
    "StateStoreStats",
]
