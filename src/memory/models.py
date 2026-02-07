"""
NeuroStack - Memory Architecture 2.0 - Data Models
Models for State Store and Event Store operations.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

# Funil canônico compartilhado (11 etapas) - ver srm.models
from src.srm.models import FunnelStage


class SalesProfile(str, Enum):
    """Perfil comportamental do paciente (doc MVP v0.5)."""
    NOVIDADE = "novidade"
    DECEPCAO = "decepcao"
    CONTINUIDADE = "continuidade"


class FlowMode(str, Enum):
    """Flow modes for conversation state."""
    
    NORMAL = "normal"
    RAG = "rag"
    SAFETY = "safety"
    HANDOFF = "handoff"


class HandoffStatus(str, Enum):
    """Handoff status values."""
    
    NONE = "none"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class LeadInfo(BaseModel):
    """Lead info (UserState/FunnelState doc)."""
    lead_name: Optional[str] = None
    city_residence: Optional[str] = None
    already_patient: Optional[bool] = None


class SalesInfo(BaseModel):
    """Sales/vendas state (UserState doc): perfil e flags de fluxo."""
    profile: Optional[SalesProfile] = None
    authority_done: bool = False
    objection_active: bool = False


class LastDecision(BaseModel):
    """Última decisão (intent, action, policy_refs, constraints)."""
    intent: Optional[str] = None
    action: Optional[str] = None
    policy_refs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class CognitiveState(BaseModel):
    """
    UserState / FunnelState (doc MVP v0.5).
    Inclui lead, sales (profile, authority_done, objection_active), last, timestamps.
    """

    funnel_stage: FunnelStage = FunnelStage.LEAD_NOVO
    intent_history: list[str] = Field(default_factory=list)
    slots: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    turn_count: int = 0
    safety_triggers: int = 0
    last_intent: Optional[str] = None
    confidence_avg: Optional[float] = None
    current_mode: FlowMode = FlowMode.NORMAL
    sentiment_label: Optional[str] = None
    sentiment_score: Optional[float] = None
    sentiment_confidence: Optional[float] = None
    # UserState doc: lead, sales, last, timestamps
    lead: LeadInfo = Field(default_factory=LeadInfo)
    sales: SalesInfo = Field(default_factory=SalesInfo)
    last: LastDecision = Field(default_factory=LastDecision)
    first_contact_at: Optional[datetime] = None
    pos_atendimento_started_at: Optional[datetime] = None
    last_message_at: Optional[datetime] = None

    def add_intent(self, intent: str, confidence: Optional[float] = None) -> None:
        """Add intent to history and update last_intent."""
        self.intent_history.append(intent)
        self.last_intent = intent
        self.turn_count += 1
        if confidence is not None:
            self.update_confidence(confidence)

    def update_slot(self, key: str, value: Any) -> None:
        """Update a slot value."""
        self.slots[key] = value

    def set_flag(self, key: str, value: bool) -> None:
        """Set a flag value."""
        self.flags[key] = value

    def trigger_safety(self) -> None:
        """Increment safety trigger count and set mode."""
        self.safety_triggers += 1
        self.current_mode = FlowMode.SAFETY

    def update_confidence(self, confidence: float) -> None:
        """Update rolling average confidence."""
        if self.confidence_avg is None:
            self.confidence_avg = confidence
        else:
            # Rolling average
            weight = self.turn_count - 1 if self.turn_count > 1 else 1
            self.confidence_avg = (self.confidence_avg * weight + confidence) / self.turn_count

    def update_sentiment(
        self,
        label: Optional[str] = None,
        score: Optional[float] = None,
        confidence: Optional[float] = None,
    ) -> None:
        """Update sentiment fields."""
        if label is not None:
            self.sentiment_label = label
        if score is not None:
            self.sentiment_score = score
        if confidence is not None:
            self.sentiment_confidence = confidence

    def to_patch_dict(self) -> dict[str, Any]:
        """Convert to patch dictionary for SRM updates."""
        return {
            "funnel_stage": self.funnel_stage.value,
            "last_intent": self.last_intent,
            "slots": self.slots,
            "flags": self.flags,
            "confidence": self.confidence_avg,
            "sentiment_label": self.sentiment_label,
            "sentiment_score": self.sentiment_score,
            "sentiment_confidence": self.sentiment_confidence,
            "lead": self.lead.model_dump() if self.lead else {},
            "sales": self.sales.model_dump() if self.sales else {},
            "last": self.last.model_dump() if self.last else {},
        }


class SessionStateCreate(BaseModel):
    """Request model for creating a new session state."""

    tenant_id: UUID
    session_id: Optional[UUID] = Field(default_factory=uuid4)
    user_id: Optional[str] = None
    channel: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)
    initial_cognitive_state: Optional[CognitiveState] = None


class SessionStateUpdate(BaseModel):
    """Request model for updating session state."""

    cognitive_state: Optional[CognitiveState] = None
    metadata: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None
    handoff_status: Optional[HandoffStatus] = None


class StatePatch(BaseModel):
    """
    Patch model for partial cognitive state updates.
    Used by SRM to apply incremental updates to session state.
    """

    funnel_stage: Optional[FunnelStage] = None
    last_intent: Optional[str] = None
    slots: Optional[dict[str, Any]] = None
    flags: Optional[dict[str, bool]] = None
    safety_trigger: bool = False
    confidence: Optional[float] = None
    mode_transition: Optional[FlowMode] = None
    sentiment_label: Optional[str] = None
    sentiment_score: Optional[float] = None
    sentiment_confidence: Optional[float] = None


class SessionStateResponse(BaseModel):
    """Response model for session state."""

    session_id: UUID
    tenant_id: UUID
    user_id: Optional[str] = None
    channel: str
    cognitive_state: CognitiveState
    metadata: dict[str, Any]
    message_count: int
    is_active: bool
    started_at: datetime
    last_activity_at: datetime
    handoff_status: Optional[HandoffStatus] = None
    ttl_remaining: Optional[int] = None


class RecentMessage(BaseModel):
    """Model for recent message in context window."""

    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    intent: Optional[str] = None
    confidence: Optional[float] = None


class SessionContext(BaseModel):
    """
    Complete session context for CTX orchestrator.
    
    Combines cognitive state with recent messages and metadata
    for prompt construction.
    """

    session_id: UUID
    tenant_id: UUID
    cognitive_state: CognitiveState
    recent_messages: list[RecentMessage] = Field(default_factory=list)
    active_policies: list[str] = Field(default_factory=list)
    knowledge_context: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StateStoreStats(BaseModel):
    """Statistics for State Store operations."""

    total_sessions: int = 0
    active_sessions: int = 0
    operations_count: dict[str, int] = Field(default_factory=dict)
    avg_latency_ms: dict[str, float] = Field(default_factory=dict)
    redis_info: dict[str, Any] = Field(default_factory=dict)

