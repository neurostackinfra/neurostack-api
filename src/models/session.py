"""
NeuroStack - Session Model
Represents a conversation session between a user and the AI agent.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.tenant import Tenant
    from src.models.event import Event
    from src.models.handoff import HandoffRecord


class CognitiveStateData:
    """
    UserState / FunnelState (doc MVP v0.5).
    Full cognitive state: funnel_stage (11 values), lead, sales, last, timestamps.
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
        last_action: Optional[str] = None,
        confidence_avg: Optional[float] = None,
        current_mode: str = "normal",
        sentiment_label: Optional[str] = None,
        sentiment_score: Optional[float] = None,
        sentiment_confidence: Optional[float] = None,
        lead: Optional[dict[str, Any]] = None,
        sales: Optional[dict[str, Any]] = None,
        last: Optional[dict[str, Any]] = None,
        first_contact_at: Optional[str] = None,
        pos_atendimento_started_at: Optional[str] = None,
        last_message_at: Optional[str] = None,
    ):
        self.funnel_stage = funnel_stage
        self.intent_history = intent_history or []
        self.slots = slots or {}
        self.flags = flags or {}
        self.turn_count = turn_count
        self.safety_triggers = safety_triggers
        self.last_intent = last_intent
        self.last_action = last_action
        self.confidence_avg = confidence_avg
        self.current_mode = current_mode
        self.sentiment_label = sentiment_label
        self.sentiment_score = sentiment_score
        self.sentiment_confidence = sentiment_confidence
        self.lead = lead or {}
        self.sales = sales or {}
        self.last = last or {}
        self.first_contact_at = first_contact_at
        self.pos_atendimento_started_at = pos_atendimento_started_at
        self.last_message_at = last_message_at

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON storage (UserState completo)."""
        return {
            "funnel_stage": self.funnel_stage,
            "intent_history": self.intent_history,
            "slots": self.slots,
            "flags": self.flags,
            "turn_count": self.turn_count,
            "safety_triggers": self.safety_triggers,
            "last_intent": self.last_intent,
            "last_action": getattr(self, "last_action", None),
            "confidence_avg": self.confidence_avg,
            "current_mode": getattr(self, "current_mode", "normal"),
            "sentiment_label": self.sentiment_label,
            "sentiment_score": self.sentiment_score,
            "sentiment_confidence": self.sentiment_confidence,
            "lead": getattr(self, "lead", {}),
            "sales": getattr(self, "sales", {}),
            "last": getattr(self, "last", {}),
            "first_contact_at": getattr(self, "first_contact_at", None),
            "pos_atendimento_started_at": getattr(self, "pos_atendimento_started_at", None),
            "last_message_at": getattr(self, "last_message_at", None),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CognitiveStateData":
        """Create from dictionary (UserState completo)."""
        return cls(
            funnel_stage=data.get("funnel_stage", "lead_novo"),
            intent_history=data.get("intent_history", []),
            slots=data.get("slots", {}),
            flags=data.get("flags", {}),
            turn_count=data.get("turn_count", 0),
            safety_triggers=data.get("safety_triggers", 0),
            last_intent=data.get("last_intent"),
            last_action=data.get("last_action"),
            confidence_avg=data.get("confidence_avg"),
            current_mode=data.get("current_mode", "normal"),
            sentiment_label=data.get("sentiment_label"),
            sentiment_score=data.get("sentiment_score"),
            sentiment_confidence=data.get("sentiment_confidence"),
            lead=data.get("lead"),
            sales=data.get("sales"),
            last=data.get("last"),
            first_contact_at=data.get("first_contact_at"),
            pos_atendimento_started_at=data.get("pos_atendimento_started_at"),
            last_message_at=data.get("last_message_at"),
        )

    def add_intent(self, intent: str) -> None:
        """Add intent to history and update last_intent."""
        self.intent_history.append(intent)
        self.last_intent = intent
        self.turn_count += 1

    def update_slot(self, key: str, value: Any) -> None:
        """Update a slot value."""
        self.slots[key] = value

    def set_flag(self, key: str, value: bool) -> None:
        """Set a flag value."""
        self.flags[key] = value

    def trigger_safety(self) -> None:
        """Increment safety trigger count."""
        self.safety_triggers += 1

    def update_confidence(self, confidence: float) -> None:
        """Update rolling average confidence."""
        if self.confidence_avg is None:
            self.confidence_avg = confidence
        else:
            # Rolling average
            self.confidence_avg = (
                self.confidence_avg * (self.turn_count - 1) + confidence
            ) / self.turn_count

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


class Session(Base):
    """
    Session model for conversation tracking.
    
    A session represents a single conversation between a user and the AI agent.
    Sessions track:
    - User identity (optional)
    - Communication channel
    - Cognitive state (funnel, intents, slots)
    - Activity timestamps
    - Message count
    
    Attributes:
        id: Unique session identifier (UUID)
        tenant_id: Owning tenant
        user_id: External user identifier (from builder's system)
        channel: Communication channel (whatsapp, web, etc.)
        cognitive_state: JSON object with conversation state
        metadata: Additional session metadata
        started_at: Session start timestamp
        last_activity_at: Last activity timestamp
        ended_at: Session end timestamp (if ended)
        is_active: Whether session is currently active
        message_count: Total messages in session
        handoff_status: Current handoff status if any
    
    Relationships:
        tenant: The tenant that owns this session
        events: All events for this session
        handoff_records: Handoff records for this session
    """

    __tablename__ = "sessions"

    # Primary Key
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Foreign Keys
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Core Fields
    user_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )
    channel: Mapped[str] = mapped_column(
        String(50),
        default="unknown",
        nullable=False,
    )

    # Cognitive State (JSONB)
    cognitive_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
    )

    # Session Metadata (JSONB)
    # Note: Named session_metadata to avoid conflict with SQLAlchemy's reserved 'metadata'
    session_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",  # Column name in DB
        JSONB,
        default=dict,
        nullable=False,
    )

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
    )

    # Counters
    message_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Handoff tracking
    handoff_status: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="sessions",
    )
    events: Mapped[list["Event"]] = relationship(
        "Event",
        back_populates="session",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    handoff_records: Mapped[list["HandoffRecord"]] = relationship(
        "HandoffRecord",
        back_populates="session",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def get_cognitive_state(self) -> CognitiveStateData:
        """Get typed cognitive state object."""
        return CognitiveStateData.from_dict(self.cognitive_state or {})

    def set_cognitive_state(self, state: CognitiveStateData) -> None:
        """Set cognitive state from typed object."""
        self.cognitive_state = state.to_dict()

    def update_activity(self) -> None:
        """Update last activity timestamp."""
        self.last_activity_at = datetime.utcnow()

    def increment_message_count(self) -> None:
        """Increment message count and update activity."""
        self.message_count += 1
        self.update_activity()

    def end_session(self) -> None:
        """Mark session as ended."""
        self.is_active = False
        self.ended_at = datetime.utcnow()
        self.update_activity()

    @property
    def funnel_stage(self) -> str:
        """Get current funnel stage."""
        return self.cognitive_state.get("funnel_stage", "lead_novo") if self.cognitive_state else "lead_novo"

    @property
    def turn_count(self) -> int:
        """Get current turn count."""
        return self.cognitive_state.get("turn_count", 0) if self.cognitive_state else 0

    def __repr__(self) -> str:
        return f"<Session(id={self.id}, tenant_id={self.tenant_id}, active={self.is_active})>"

