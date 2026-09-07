"""
NeuroStack - Flow Manager
Manages conversation flow modes and state transitions.

The Flow Manager orchestrates how conversations flow between
different modes (NORMAL, RAG, SAFETY, HANDOFF) based on:
- SRM decisions (intent, safety, handoff)
- Policy constraints
- Session state

Usage:
    from src.flow import FlowManager, get_flow_manager, process_flow

    # Using the manager directly
    manager = get_flow_manager()
    decision = await manager.process(context)

    # Using the convenience function
    decision = await process_flow(
        session_id=session_id,
        tenant_id=tenant_id,
        message="Preciso de ajuda",
        safety_concern=True,
        safety_category="clinical_risk",
    )
"""

from src.flow.handlers import (
    BaseModeHandler,
    HandoffModeHandler,
    NormalModeHandler,
    RagModeHandler,
    SafetyModeHandler,
)
from src.flow.manager import FlowManager, get_flow_manager, process_flow
from src.flow.models import (  # Enums; Models
    FlowContext,
    FlowDecision,
    FlowMode,
    FlowState,
    FlowTransition,
    ModeHandlerResult,
    TransitionResult,
    TransitionTrigger,
)
from src.flow.state_machine import (
    MODE_PRIORITY,
    VALID_TRANSITIONS,
    FlowStateMachine,
    get_state_machine,
)
from src.flow.transitions import (
    TransitionAnalytics,
    TransitionLogger,
    get_transition_analytics,
    get_transition_logger,
)

__all__ = [
    # Enums
    "FlowMode",
    "TransitionTrigger",
    "TransitionResult",
    # Models
    "FlowTransition",
    "FlowState",
    "FlowContext",
    "FlowDecision",
    "ModeHandlerResult",
    # State Machine
    "FlowStateMachine",
    "get_state_machine",
    "VALID_TRANSITIONS",
    "MODE_PRIORITY",
    # Transition Logging
    "TransitionLogger",
    "TransitionAnalytics",
    "get_transition_logger",
    "get_transition_analytics",
    # Handlers
    "BaseModeHandler",
    "NormalModeHandler",
    "RagModeHandler",
    "SafetyModeHandler",
    "HandoffModeHandler",
    # Manager
    "FlowManager",
    "get_flow_manager",
    "process_flow",
]
