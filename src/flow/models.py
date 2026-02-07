"""
NeuroStack - Flow Manager - Models
Data models for flow management and state machine.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class FlowMode(str, Enum):
    """Flow modes for conversation state machine."""
    NORMAL = "normal"
    RAG = "rag"
    SAFETY = "safety"
    HANDOFF = "handoff"


class TransitionTrigger(str, Enum):
    """Triggers that can cause state transitions."""
    # Safety triggers
    SAFETY_CONCERN_DETECTED = "safety_concern_detected"
    SAFETY_RESOLVED = "safety_resolved"
    
    # Handoff triggers
    HANDOFF_REQUESTED = "handoff_requested"
    HANDOFF_COMPLETED = "handoff_completed"
    HANDOFF_CANCELLED = "handoff_cancelled"
    
    # RAG triggers
    RAG_REQUIRED = "rag_required"
    RAG_COMPLETED = "rag_completed"
    
    # Normal flow
    CONTINUE_NORMAL = "continue_normal"
    RESET_TO_NORMAL = "reset_to_normal"
    
    # Session events
    SESSION_TIMEOUT = "session_timeout"
    SESSION_END = "session_end"


class TransitionResult(str, Enum):
    """Result of a state transition attempt."""
    SUCCESS = "success"
    BLOCKED = "blocked"
    INVALID = "invalid"
    NO_CHANGE = "no_change"


class FlowTransition(BaseModel):
    """Represents a state transition."""
    
    from_mode: FlowMode
    to_mode: FlowMode
    trigger: TransitionTrigger
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: Optional[UUID] = None
    tenant_id: Optional[UUID] = None
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class FlowState(BaseModel):
    """Current state of the flow manager for a session."""
    
    session_id: UUID
    tenant_id: UUID
    current_mode: FlowMode = FlowMode.NORMAL
    previous_mode: Optional[FlowMode] = None
    transition_count: int = 0
    mode_entry_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    transition_history: list[FlowTransition] = Field(default_factory=list, max_length=50)
    
    # Mode-specific data
    safety_level: int = 0  # 0=none, 1=low, 2=medium, 3=high
    handoff_agent_id: Optional[str] = None
    rag_query_count: int = 0
    
    # Flags
    is_locked: bool = False  # Prevents transitions when true
    pending_transition: Optional[TransitionTrigger] = None

    def get_time_in_mode(self) -> float:
        """Get seconds spent in current mode."""
        return (datetime.now(timezone.utc) - self.mode_entry_time).total_seconds()


class FlowContext(BaseModel):
    """Context for flow decision-making."""
    
    session_id: UUID
    tenant_id: UUID
    current_mode: FlowMode
    
    # From SRM
    detected_intent: Optional[str] = None
    intent_confidence: float = 0.0
    safety_concern: bool = False
    safety_category: Optional[str] = None
    handoff_required: bool = False
    handoff_reason: Optional[str] = None
    needs_rag: bool = False
    
    # Session info
    turn_count: int = 0
    message: str = ""
    
    # Additional context
    metadata: dict[str, Any] = Field(default_factory=dict)


class FlowDecision(BaseModel):
    """Decision made by the flow manager."""
    
    current_mode: FlowMode
    next_mode: FlowMode
    transition_required: bool
    trigger: Optional[TransitionTrigger] = None
    transition_result: TransitionResult = TransitionResult.NO_CHANGE
    
    # Instructions for handlers
    handler_action: str = "process"  # process, escalate, defer, block
    handler_params: dict[str, Any] = Field(default_factory=dict)
    
    # Response modifiers
    response_modifiers: list[str] = Field(default_factory=list)
    
    # Metadata
    decision_reason: str = ""
    processing_time_ms: float = 0.0


class ModeHandlerResult(BaseModel):
    """Result from a mode handler."""
    
    mode: FlowMode
    success: bool
    response: Optional[str] = None
    should_transition: bool = False
    next_trigger: Optional[TransitionTrigger] = None
    state_updates: dict[str, Any] = Field(default_factory=dict)
    events_to_log: list[dict[str, Any]] = Field(default_factory=list)
    processing_time_ms: float = 0.0

