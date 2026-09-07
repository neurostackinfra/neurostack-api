"""
NeuroStack - CTX (Context Orchestrator) - Models
Data models for context orchestration and assembly.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    """Role of message sender."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class RelevanceLevel(str, Enum):
    """Relevance level for context items."""

    CRITICAL = "critical"  # Must always include
    HIGH = "high"  # Include if space allows
    MEDIUM = "medium"  # Include for context
    LOW = "low"  # Include only if needed
    NONE = "none"  # Do not include


class ContextMessage(BaseModel):
    """A message in the conversation context."""

    role: MessageRole
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    intent: Optional[str] = None
    confidence: Optional[float] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_prompt_format(self) -> dict[str, str]:
        """Convert to LLM prompt format."""
        return {
            "role": self.role.value,
            "content": self.content,
        }


class KnowledgeChunk(BaseModel):
    """A chunk of knowledge from the Knowledge Service."""

    id: str
    content: str
    source: Optional[str] = None
    relevance_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyConstraint(BaseModel):
    """A policy constraint for context."""

    rule_id: str
    message: str
    severity: str
    is_global: bool = True


class SessionInfo(BaseModel):
    """Session information for context."""

    session_id: UUID
    tenant_id: UUID
    user_id: Optional[str] = None
    channel: str = "unknown"
    turn_count: int = 0
    funnel_stage: str = "lead_novo"
    is_active: bool = True
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class CognitiveContext(BaseModel):
    """Cognitive state context for decision-making (doc: UserState)."""

    funnel_stage: str = "lead_novo"
    last_intent: Optional[str] = None
    intent_history: list[str] = Field(default_factory=list)
    slots: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    confidence_avg: Optional[float] = None
    safety_triggers: int = 0
    current_mode: str = "normal"
    clinic_config: Optional[dict[str, Any]] = None
    user_state: Optional[dict[str, Any]] = None


class ContextWindow(BaseModel):
    """
    Complete context window for LLM prompt generation.

    This is the main output of the CTX orchestrator.
    """

    # Session info
    session_id: UUID
    tenant_id: UUID

    # Cognitive state
    cognitive: CognitiveContext

    # Messages (ordered, recent first)
    messages: list[ContextMessage] = Field(default_factory=list)

    # Knowledge (RAG results)
    knowledge_chunks: list[KnowledgeChunk] = Field(default_factory=list)

    # Policies
    constraints: list[str] = Field(default_factory=list)
    safety_reminders: list[str] = Field(default_factory=list)

    # Summaries
    session_summary: Optional[str] = None
    compressed_history: Optional[str] = None

    # Metadata
    context_tokens: Optional[int] = None
    build_time_ms: Optional[int] = None
    relevance_score: float = 1.0

    def to_messages(self) -> list[dict[str, str]]:
        """Convert to list of message dicts for LLM."""
        return [m.to_prompt_format() for m in self.messages]

    def get_recent_n(self, n: int) -> list[ContextMessage]:
        """Get n most recent messages."""
        return self.messages[-n:] if len(self.messages) >= n else self.messages


class ContextRequest(BaseModel):
    """Request for building context (doc: Políticas > Clinic Config > UserState > RAG > Mensagem)."""

    tenant_id: UUID
    session_id: UUID
    current_message: str
    channel: str = "unknown"

    # Options
    max_messages: int = 10
    max_tokens: int = 4000
    include_knowledge: bool = True
    include_policies: bool = True
    include_summary: bool = False

    # Cognitive state (if available)
    cognitive_state: Optional[CognitiveContext] = None

    # SRM decision (if available)
    srm_intent: Optional[str] = None
    srm_confidence: Optional[float] = None
    needs_rag: bool = False

    # Doc MVP v0.5: ClinicConfig e UserState para prioridade de contexto
    clinic_config: Optional[dict[str, Any]] = None
    user_state: Optional[dict[str, Any]] = None


class ContextResponse(BaseModel):
    """Response from context building."""

    context: ContextWindow
    success: bool = True
    warnings: list[str] = Field(default_factory=list)
    build_time_ms: int = 0


class CompressionResult(BaseModel):
    """Result of context compression."""

    original_token_count: int
    compressed_token_count: int
    compression_ratio: float
    summary: Optional[str] = None
    removed_messages: int = 0
