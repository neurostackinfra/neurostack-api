"""
NeuroStack API - Sessions Schemas
Contracts for session management endpoints (/v1/sessions).
"""

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.core.interaction_context import InteractionOrigin


class FunnelStage(str, Enum):
    """
    Funil canônico de 11 etapas (MVP v0.5).
    """

    LEAD_NOVO = "lead_novo"
    EM_QUALIFICACAO = "em_qualificacao"
    QUALIFICADO = "qualificado"
    FOLLOWUP = "followup"
    AGENDAMENTO_SOLICITADO = "agendamento_solicitado"
    AGENDADO = "agendado"
    PACIENTE_ATIVO = "paciente_ativo"
    POS_ATENDIMENTO = "pos_atendimento"
    FIDELIZACAO = "fidelizacao"
    INATIVO = "inativo"
    STANDBY = "standby"


class CognitiveState(BaseModel):
    """
    Cognitive state of a session.

    This represents the AI's understanding of the conversation state,
    including funnel position, extracted information, and behavioral flags.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "funnel_stage": "consideration",
                "intent_history": ["greeting", "scheduling_request"],
                "slots": {
                    "patient_name": "João Silva",
                    "preferred_time": "afternoon",
                    "specialty": "dermatology",
                },
                "flags": {
                    "first_visit": True,
                    "price_sensitive": True,
                    "urgent": False,
                },
                "turn_count": 5,
                "safety_triggers": 0,
                "last_intent": "scheduling_request",
                "confidence_avg": 0.87,
            }
        }
    )

    funnel_stage: FunnelStage = Field(
        default=FunnelStage.LEAD_NOVO,
        description="Current stage in the customer journey funnel (11 etapas canônicas)",
        json_schema_extra={"example": "em_qualificacao"},
    )
    intent_history: list[str] = Field(
        default_factory=list,
        description="History of detected intents in order",
        json_schema_extra={"example": ["greeting", "scheduling_request"]},
    )
    slots: dict[str, Any] = Field(
        default_factory=dict,
        description="Extracted slot values (entities, preferences, etc.)",
        json_schema_extra={"example": {"patient_name": "João Silva"}},
    )
    flags: dict[str, bool] = Field(
        default_factory=dict,
        description="Boolean flags indicating conversation state",
        json_schema_extra={"example": {"first_visit": True}},
    )
    turn_count: int = Field(
        default=0,
        ge=0,
        description="Number of conversation turns",
    )
    safety_triggers: int = Field(
        default=0,
        ge=0,
        description="Number of safety concerns triggered in this session",
    )
    last_intent: Optional[str] = Field(
        default=None,
        description="Most recently detected intent",
        json_schema_extra={"example": "scheduling_request"},
    )
    last_action: Optional[str] = Field(
        default=None,
        description="Last action taken (doc MVP v0.5)",
    )
    confidence_avg: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Average confidence score across decisions",
    )
    current_mode: str = Field(
        default="normal",
        description="Flow mode (normal, rag, safety, handoff)",
    )
    lead: dict[str, Any] = Field(
        default_factory=dict,
        description="Lead info: lead_name, city_residence, already_patient (doc MVP v0.5)",
    )
    sales: dict[str, Any] = Field(
        default_factory=dict,
        description="Sales: profile, authority_done, objection_active (doc MVP v0.5)",
    )
    last: dict[str, Any] = Field(
        default_factory=dict,
        description="Last decision: intent, action, policy_refs, constraints (doc MVP v0.5)",
    )
    first_contact_at: Optional[str] = Field(
        default=None, description="First contact timestamp"
    )
    pos_atendimento_started_at: Optional[str] = Field(
        default=None, description="Pós-atendimento start"
    )
    last_message_at: Optional[str] = Field(
        default=None, description="Last message timestamp"
    )


class SessionResponse(BaseModel):
    """
    Session details response.

    Contains complete information about a conversation session
    including its cognitive state and activity metadata.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "user_id": "user-xyz-123",
                "channel": "whatsapp",
                "cognitive_state": {
                    "funnel_stage": "consideration",
                    "intent_history": ["greeting", "scheduling_request"],
                    "slots": {"patient_name": "João Silva"},
                    "flags": {"first_visit": True},
                    "turn_count": 5,
                    "safety_triggers": 0,
                },
                "is_active": True,
                "started_at": "2025-12-31T10:00:00Z",
                "last_activity_at": "2025-12-31T10:30:00Z",
                "message_count": 10,
                "handoff_status": None,
                "metadata": {},
            }
        }
    )

    id: UUID = Field(
        ...,
        description="Unique session identifier",
        json_schema_extra={"example": "b2c3d4e5-f6a7-8901-bcde-f12345678901"},
    )
    tenant_id: UUID = Field(
        ...,
        description="Tenant that owns this session",
        json_schema_extra={"example": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"},
    )
    user_id: Optional[str] = Field(
        default=None,
        description="External user identifier from the builder's system",
        json_schema_extra={"example": "user-xyz-123"},
    )
    channel: str = Field(
        ...,
        description="Communication channel",
        json_schema_extra={"example": "whatsapp"},
    )
    cognitive_state: CognitiveState = Field(
        ...,
        description="Current cognitive state of the session",
    )
    is_active: bool = Field(
        default=True,
        description="Whether the session is currently active",
    )
    started_at: str = Field(
        ...,
        description="ISO 8601 timestamp of session start",
        json_schema_extra={"example": "2025-12-31T10:00:00Z"},
    )
    last_activity_at: str = Field(
        ...,
        description="ISO 8601 timestamp of last activity",
        json_schema_extra={"example": "2025-12-31T10:30:00Z"},
    )
    message_count: int = Field(
        default=0,
        ge=0,
        description="Total number of messages in the session",
    )
    handoff_status: Optional[str] = Field(
        default=None,
        description="Handoff status if applicable (pending, active, completed)",
        json_schema_extra={"example": None},
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional session metadata",
    )


class SessionCreateRequest(BaseModel):
    """
    Request to create a new session.

    Creates a new conversation session for a tenant/user.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "user_id": "user-xyz-123",
                "channel": "whatsapp",
                "initial_context": {
                    "referrer": "instagram_ad",
                    "campaign": "summer_promo",
                },
                "metadata": {"phone": "+5511999999999"},
            }
        }
    )

    tenant_id: UUID = Field(
        ...,
        description="Tenant identifier",
        json_schema_extra={"example": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"},
    )
    user_id: Optional[str] = Field(
        default=None,
        description="External user identifier",
        json_schema_extra={"example": "user-xyz-123"},
    )
    channel: str = Field(
        default="unknown",
        description="Communication channel",
        json_schema_extra={"example": "whatsapp"},
    )
    initial_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Initial context to seed the session",
        json_schema_extra={"example": {"referrer": "instagram_ad"}},
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata to store with the session",
    )


class SessionUpdateRequest(BaseModel):
    """
    Request to update an existing session.

    Allows updating session metadata and state.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "metadata": {"priority": "high"},
                "is_active": True,
            }
        }
    )

    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        description="Metadata to update/merge",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description="Update active status",
    )


class SessionListResponse(BaseModel):
    """
    Response for session list with pagination.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sessions": [],
                "total": 100,
                "page": 1,
                "page_size": 20,
                "has_next": True,
                "has_previous": False,
            }
        }
    )

    sessions: list[SessionResponse] = Field(
        ...,
        description="List of sessions for the current page",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of sessions matching the filter",
    )
    page: int = Field(
        ...,
        ge=1,
        description="Current page number",
    )
    page_size: int = Field(
        ...,
        ge=1,
        description="Number of items per page",
    )
    has_next: bool = Field(
        ...,
        description="Whether there is a next page",
    )
    has_previous: bool = Field(
        ...,
        description="Whether there is a previous page",
    )


class InactiveSessionItem(BaseModel):
    """Minimal projection for the permanent-pause operations queue."""

    session_id: UUID
    contact_identifier: Optional[str] = None
    channel: str
    last_intent: Optional[str] = None
    funnel_stage: str
    last_activity_at: datetime


class InactiveSessionsResponse(BaseModel):
    """Paginated effective sessions in permanent pause."""

    items: list[InactiveSessionItem]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    has_next: bool
    has_previous: bool


class SessionReactivationResponse(BaseModel):
    """Result of an idempotent session reactivation command."""

    session_id: UUID
    status: Literal["reactivated", "already_active"]


class SessionEventResponse(BaseModel):
    """
    Event associated with a session.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "event_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
                "event_type": "message.in",
                "payload": {"content": "Olá", "channel": "whatsapp"},
                "decision": {"intent": "greeting", "confidence": 0.95},
                "created_at": "2025-12-31T10:30:00Z",
            }
        }
    )

    event_id: UUID = Field(..., description="Event identifier")
    tenant_id: UUID = Field(..., description="Tenant that owns the event")
    session_id: Optional[UUID] = Field(
        default=None,
        description="Session associated with the event",
    )
    event_type: str = Field(..., description="Type of event")
    interaction_origin: InteractionOrigin = Field(
        default=InteractionOrigin.UNKNOWN,
        description="Server-managed interaction provenance",
    )
    interaction_event_id: Optional[UUID] = Field(
        default=None,
        description="Root message.in event identifier when this is that event",
    )
    payload: dict[str, Any] = Field(..., description="Event payload")
    decision: Optional[dict[str, Any]] = Field(default=None, description="SRM decision")
    created_at: str = Field(..., description="Event timestamp")


class SessionEventsResponse(BaseModel):
    """
    Response for session events list.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                "events": [],
                "total": 10,
                "limit": 50,
                "offset": 0,
            }
        }
    )

    session_id: UUID = Field(..., description="Session identifier")
    events: list[SessionEventResponse] = Field(..., description="List of events")
    total: int = Field(..., ge=0, description="Total number of events")
    limit: int = Field(..., ge=1, description="Limit applied")
    offset: int = Field(..., ge=0, description="Offset applied")
