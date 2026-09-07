"""
NeuroStack - Handoff Service Models
Data models for human handoff orchestration.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

# ============================================
# Enums
# ============================================


class HandoffStatus(str, Enum):
    """Status of a handoff request."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class HandoffUrgency(str, Enum):
    """Urgency levels for handoff."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class HandoffSector(str, Enum):
    """Setor do handoff (documentação MVP v0.5)."""

    AGENDAMENTO = "agendamento"
    FINANCEIRO = "financeiro"
    GERENCIA = "gerencia"
    ADMINISTRATIVO = "administrativo"
    CLINICO = "clinico"
    URGENCIA = "urgencia"
    FIDELIZACAO = "fidelizacao"


class HandoffSLA(str, Enum):
    """SLA do handoff (documentação MVP v0.5)."""

    IMEDIATO = "imediato"
    PRIORITARIO = "prioritario"
    ROTINA = "rotina"


class HandoffReason(str, Enum):
    """Reasons for human handoff."""

    USER_REQUEST = "user_request"
    SAFETY_CONCERN = "safety_concern"
    MAX_TURNS_EXCEEDED = "max_turns_exceeded"
    LOW_CONFIDENCE = "low_confidence"
    COMPLEX_QUERY = "complex_query"
    ESCALATION = "escalation"
    SCHEDULING = "scheduling"


class WebhookStatus(str, Enum):
    """Status of webhook delivery."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    RETRYING = "retrying"


# ============================================
# Webhook Configuration
# ============================================


class WebhookConfig(BaseModel):
    """Configuration for tenant webhook."""

    url: str
    secret: Optional[str] = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    retry_count: int = Field(default=3, ge=0, le=5)
    retry_delay_seconds: int = Field(default=5, ge=1, le=60)
    enabled: bool = True

    # Filter which events to send
    send_on_pending: bool = True
    send_on_accepted: bool = True
    send_on_completed: bool = True
    send_on_timeout: bool = True


class WebhookDelivery(BaseModel):
    """Record of a webhook delivery attempt."""

    id: UUID = Field(default_factory=uuid4)
    handoff_id: UUID
    tenant_id: UUID
    url: str
    status: WebhookStatus = WebhookStatus.PENDING
    attempt: int = 1
    request_body: dict[str, Any] = Field(default_factory=dict)
    response_status: Optional[int] = None
    response_body: Optional[str] = None
    error_message: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================
# Conversation Summary
# ============================================


class ConversationMessage(BaseModel):
    """A message in the conversation."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime
    intent: Optional[str] = None


class KeyInformation(BaseModel):
    """Key information extracted from conversation."""

    # Patient/User info
    user_name: Optional[str] = None
    contact_info: Optional[str] = None

    # Context
    main_topic: Optional[str] = None
    specific_request: Optional[str] = None

    # Healthcare specific
    symptoms_mentioned: list[str] = Field(default_factory=list)
    appointment_preferences: Optional[str] = None

    # Additional slots
    extracted_slots: dict[str, Any] = Field(default_factory=dict)


class ConversationSummary(BaseModel):
    """Structured summary of a conversation for handoff."""

    # Overview
    session_id: UUID
    tenant_id: UUID
    summary_text: str

    # Context
    total_messages: int
    turn_count: int
    duration_seconds: Optional[int] = None

    # Funnel info
    funnel_stage: str
    intents_detected: list[str] = Field(default_factory=list)

    # Key info extracted
    key_info: KeyInformation = Field(default_factory=KeyInformation)

    # Handoff specific (doc: setor + SLA)
    handoff_reason: HandoffReason
    urgency: HandoffUrgency = HandoffUrgency.NORMAL
    sector: Optional[HandoffSector] = None
    sla: Optional[HandoffSLA] = None
    verification_status: Optional[str] = None

    # Safety
    safety_flags: list[str] = Field(default_factory=list)

    # Recent messages for context
    recent_messages: list[ConversationMessage] = Field(default_factory=list)

    # Metadata
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================
# Handoff Request
# ============================================


class HandoffRequest(BaseModel):
    """Request to initiate a handoff."""

    tenant_id: UUID
    session_id: UUID
    reason: HandoffReason
    detected_intent: Optional[str] = None
    safety_concern: Optional[bool] = None
    urgency: HandoffUrgency = HandoffUrgency.NORMAL
    sector: Optional[HandoffSector] = None
    sla: Optional[HandoffSLA] = None

    # Optional pre-built summary
    summary: Optional[str] = None
    key_info: dict[str, Any] = Field(default_factory=dict)
    verification_status: Optional[str] = None

    # Session context
    funnel_stage: str = "unknown"
    turn_count: int = 0
    current_mode: str = "handoff"

    # Last messages for quick summary
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)

    # Requestor
    requested_by: str = "system"  # "system" or "user"


class HandoffResponse(BaseModel):
    """Response from handoff initiation."""

    id: UUID
    tenant_id: UUID
    session_id: UUID
    status: HandoffStatus = HandoffStatus.PENDING

    # Summary
    summary: ConversationSummary

    # Webhook
    webhook_sent: bool = False
    webhook_status: Optional[WebhookStatus] = None

    # Message to show user
    user_message: str
    verification_status: Optional[str] = None

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HandoffUpdate(BaseModel):
    """Update to a handoff status."""

    handoff_id: UUID
    status: HandoffStatus
    assigned_to: Optional[str] = None
    notes: Optional[str] = None
    verification_status: Optional[str] = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================
# Webhook Payload
# ============================================


class WebhookPayload(BaseModel):
    """Payload sent to tenant webhook."""

    event_type: str  # "handoff.created", "handoff.updated", etc.
    handoff_id: UUID
    tenant_id: UUID
    session_id: UUID

    # Status (doc: setor + SLA)
    status: HandoffStatus
    urgency: HandoffUrgency
    reason: HandoffReason
    sector: Optional[HandoffSector] = None
    sla: Optional[HandoffSLA] = None

    # Summary
    summary: str
    key_info: dict[str, Any] = Field(default_factory=dict)
    verification_status: Optional[str] = None

    # Context
    turn_count: int
    funnel_stage: str
    recent_messages: list[dict[str, str]] = Field(default_factory=list)

    # Metadata
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    api_version: str = "v1"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = self.model_dump(mode="json")
        data["timestamp"] = self.timestamp.isoformat()
        return data


# ============================================
# Service Configuration
# ============================================


class HandoffServiceConfig(BaseModel):
    """Configuration for handoff service."""

    # Summarizer settings
    max_recent_messages: int = Field(default=5, ge=1, le=20)
    max_summary_length: int = Field(default=500, ge=100, le=2000)

    # Webhook settings
    default_webhook_timeout: int = Field(default=30, ge=5, le=120)
    max_webhook_retries: int = Field(default=3, ge=0, le=5)

    # Timeout settings
    handoff_timeout_minutes: int = Field(default=30, ge=5, le=180)

    # Messages — REGRA: nunca mencionar "humano", "atendente humano" ou "transferir".
    # Sempre usar "nossa equipe", "equipe especializada" ou "setor especializado".
    default_handoff_messages: dict[str, str] = Field(
        default_factory=lambda: {
            HandoffReason.USER_REQUEST.value: (
                "Claro! Vou encaminhar você para nossa equipe agora. "
                "Em breve alguém dará continuidade ao seu atendimento."
            ),
            HandoffReason.SAFETY_CONCERN.value: (
                "Para garantir sua segurança e o melhor cuidado possível, vou encaminhar para nossa equipe especializada. "
                "Um profissional entrará em contato em breve."
            ),
            HandoffReason.MAX_TURNS_EXCEEDED.value: (
                "Para te ajudar ainda melhor, vou encaminhar para o setor da nossa equipe "
                "que poderá dar continuidade ao seu atendimento."
            ),
            HandoffReason.LOW_CONFIDENCE.value: (
                "Para garantir que você receba a orientação mais completa, "
                "vou encaminhar para nossa equipe especializada."
            ),
            HandoffReason.ESCALATION.value: (
                "Entendo a sua situação. Vou encaminhar para o setor responsável "
                "da nossa equipe, que poderá ajudá-lo da melhor forma."
            ),
            HandoffReason.COMPLEX_QUERY.value: (
                "Essa questão merece uma atenção mais detalhada. "
                "Vou encaminhar para a nossa equipe especializada."
            ),
            HandoffReason.SCHEDULING.value: (
                "Vou te encaminhar para a nossa equipe, que dará seguimento "
                "e verificará os melhores horários disponíveis para você."
            ),
        }
    )


# ============================================
# API Schemas
# ============================================


class WebhookConfigCreate(BaseModel):
    """Schema for creating webhook configuration."""

    url: str
    secret: Optional[str] = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    retry_count: int = Field(default=3, ge=0, le=5)
    enabled: bool = True


class WebhookConfigUpdate(BaseModel):
    """Schema for updating webhook configuration."""

    url: Optional[str] = None
    secret: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    timeout_seconds: Optional[int] = Field(default=None, ge=5, le=120)
    retry_count: Optional[int] = Field(default=None, ge=0, le=5)
    enabled: Optional[bool] = None


class WebhookConfigResponse(BaseModel):
    """Response schema for webhook configuration."""

    tenant_id: UUID
    url: str
    timeout_seconds: int
    retry_count: int
    enabled: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    # Don't expose secret, just whether it's set
    has_secret: bool = False


class HandoffListItem(BaseModel):
    """Item in handoff list response."""

    id: UUID
    session_id: UUID
    status: HandoffStatus
    urgency: HandoffUrgency
    reason: HandoffReason
    sector: Optional[HandoffSector] = None
    sla: Optional[HandoffSLA] = None
    verification_status: Optional[str] = None
    summary_preview: str
    created_at: datetime
    assigned_to: Optional[str] = None


class HandoffDetail(BaseModel):
    """Detailed handoff information."""

    id: UUID
    tenant_id: UUID
    session_id: UUID

    # Status (doc: setor + SLA)
    status: HandoffStatus
    urgency: HandoffUrgency
    reason: HandoffReason
    sector: Optional[HandoffSector] = None
    sla: Optional[HandoffSLA] = None
    verification_status: Optional[str] = None

    # Content
    summary: str
    key_info: dict[str, Any] = Field(default_factory=dict)
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)

    # Assignment
    assigned_to: Optional[str] = None

    # Webhook
    webhook_sent: bool = False
    webhook_status: Optional[WebhookStatus] = None

    # Timestamps
    created_at: datetime
    updated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    # Resolution
    resolution_notes: Optional[str] = None
