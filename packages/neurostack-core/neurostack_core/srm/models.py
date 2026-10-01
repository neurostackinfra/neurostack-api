"""
NeuroStack - SRM (Small Reasoning Module) - Models
Data models for SRM decision engine.
"""

from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from neurostack_core.protocol import OFFICIAL_INTENT_SEQUENCE
from neurostack_core.protocol import ActionType as ActionType
from neurostack_core.protocol import FlowMode as FlowMode
from neurostack_core.protocol import FunnelStage as FunnelStage
from neurostack_core.protocol import HandoffReason as HandoffReason
from neurostack_core.protocol import IntentCategory as IntentCategory
from neurostack_core.protocol import SafetyCategory as SafetyCategory
from neurostack_core.protocol import SentimentLabel as SentimentLabel

# Mutable-list compatibility for callers of the historic public symbol.  The
# canonical contract itself is the immutable sequence in protocol.py.
OFFICIAL_INTENTS = list(OFFICIAL_INTENT_SEQUENCE)


# ============================================
# Input Models
# ============================================


class SRMInput(BaseModel):
    """Input for SRM processing."""

    tenant_id: UUID
    session_id: UUID
    message: str
    channel: str = "unknown"
    user_id: Optional[str] = None
    language: str = "pt-BR"

    # Current session state
    current_funnel_stage: FunnelStage = FunnelStage.LEAD_NOVO
    turn_count: int = 0
    intent_history: list[str] = Field(default_factory=list)
    slots: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    safety_triggers: int = 0

    # Configuration
    max_turns_for_handoff: int = 10
    safety_threshold: float = 0.5
    confidence_threshold: float = 0.7
    # Doc MVP v0.5: ClinicConfig e UserState (quando disponíveis)
    clinic_config: Optional[dict[str, Any]] = None
    user_state: Optional[dict[str, Any]] = None


# ============================================
# Output Models
# ============================================


class IntentResult(BaseModel):
    """Result from intent classification."""

    intent: str
    category: IntentCategory
    confidence: float = Field(ge=0.0, le=1.0)
    matched_patterns: list[str] = Field(default_factory=list)


class SafetyResult(BaseModel):
    """Result from safety detection."""

    is_safe: bool = True
    category: SafetyCategory = SafetyCategory.NONE
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    triggers: list[str] = Field(default_factory=list)
    action: str = "continue"


class FunnelResult(BaseModel):
    """Result from funnel detection."""

    current_stage: FunnelStage
    should_transition: bool = False
    next_stage: Optional[FunnelStage] = None
    transition_reason: Optional[str] = None


class HandoffResult(BaseModel):
    """Result from handoff decision (doc: setor + SLA)."""

    required: bool = False
    reason: Optional[HandoffReason] = None
    urgency: str = "normal"
    summary: Optional[str] = None
    sector: Optional[str] = (
        None  # agendamento, financeiro, gerencia, administrativo, clinico, urgencia, fidelizacao
    )
    sla: Optional[str] = None  # imediato, prioritario, rotina
    # Two-step scheduling confirmation flow
    confirmation_pending: bool = False  # ask user to confirm before triggering handoff
    scheduling_cancelled: bool = False  # user declined — clear scheduling_interest flag
    # Optional scheduling-link handoff: "link_sent" on the send turn, "concluiu"/"nao_concluiu" on the enrich turn
    verification_status: Optional[str] = None


class RagResult(BaseModel):
    """Result from RAG decision."""

    needs_rag: bool = False
    query_suggestion: Optional[str] = None
    reason: Optional[str] = None


class SentimentResult(BaseModel):
    """Result from sentiment classification."""

    label: SentimentLabel = SentimentLabel.NEUTRAL
    score: float = Field(default=0.0, ge=-1.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    probabilities: dict[str, float] = Field(default_factory=dict)


class StatePatch(BaseModel):
    """State changes to apply to session (UserState/FunnelState doc)."""

    funnel_stage: Optional[str] = None
    last_intent: Optional[str] = None
    last_action: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    safety_trigger: bool = False
    confidence: Optional[float] = None
    sentiment_label: Optional[str] = None
    sentiment_score: Optional[float] = None
    sentiment_confidence: Optional[float] = None
    # lead (doc)
    lead_name: Optional[str] = None
    city_residence: Optional[str] = None
    already_patient: Optional[bool] = None
    # sales (doc): profile, authority_done, objection_active
    sales_profile: Optional[str] = None  # novidade, decepcao, continuidade
    authority_done: Optional[bool] = None
    objection_active: Optional[bool] = None
    # last (policy_refs, constraints applied in policy layer)
    policy_refs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class SRMDecision(BaseModel):
    """
    Complete SRM decision output.

    Contains the cognitive analysis of the user's message including
    intent classification, funnel stage, safety assessment, and action recommendations.
    """

    # Intent
    intent: str
    intent_category: IntentCategory

    # Funnel
    funnel_stage: FunnelStage
    funnel_transition: Optional[str] = None

    # Action
    action: ActionType
    action_details: Optional[str] = None

    # Confidence
    confidence: float = Field(ge=0.0, le=1.0)

    # Safety
    safety_concern: bool = False
    safety_category: SafetyCategory = SafetyCategory.NONE

    # Sentiment
    sentiment_label: SentimentLabel = SentimentLabel.NEUTRAL
    sentiment_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    sentiment_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # Flow
    mode: FlowMode = FlowMode.NORMAL
    needs_rag: bool = False

    # Handoff (doc: setor + SLA)
    handoff_required: bool = False
    handoff_reason: Optional[HandoffReason] = None
    handoff_urgency: str = "normal"
    handoff_sector: Optional[str] = None
    handoff_sla: Optional[str] = None
    handoff_verification_status: Optional[str] = (
        None  # scheduling-link: link_sent / concluiu / nao_concluiu
    )

    # State changes
    state_patch: StatePatch = Field(default_factory=StatePatch)

    # Reasoning (for debugging/audit)
    reasoning: Optional[str] = None
    processing_time_ms: Optional[int] = None

    def to_api_response(self) -> dict[str, Any]:
        """Convert to API response format."""
        return {
            "intent": self.intent,
            "intent_category": self.intent_category.value,
            "funnel_stage": self.funnel_stage.value,
            "action": self.action.value,
            "confidence": self.confidence,
            "safety_concern": self.safety_concern,
            "safety_category": self.safety_category.value,
            "sentiment_label": self.sentiment_label.value,
            "sentiment_score": self.sentiment_score,
            "sentiment_confidence": self.sentiment_confidence,
            "needs_rag": self.needs_rag,
            "handoff_required": self.handoff_required,
            "handoff_reason": (
                self.handoff_reason.value if self.handoff_reason else None
            ),
            "reasoning": self.reasoning,
        }
