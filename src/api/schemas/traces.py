"""
NeuroStack API - Traces Schemas
Contracts for read-only trace view endpoints (/v1/traces).
"""

from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TraceItem(BaseModel):
    """
    A single trace item representing a processed message event.

    Traces provide a read-only view over message.in events that
    were fully processed through the cognitive pipeline.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
                "timestamp": "2025-12-31T10:30:00Z",
                "user_message": "Ola, gostaria de agendar uma consulta",
                "srm_decision": {
                    "intent": "scheduling_request",
                    "intent_category": "scheduling",
                    "funnel_stage": "consideration",
                    "action": "collect_availability",
                    "confidence": 0.92,
                    "safety_concern": False,
                    "handoff_required": False,
                },
                "context_summary": "Paciente novo buscando agendamento",
                "prompt_built": None,
                "processing_time_ms": 45,
                "mode": "normal",
            }
        }
    )

    id: UUID = Field(
        ...,
        description="Unique event identifier",
    )
    timestamp: str = Field(
        ...,
        description="ISO 8601 timestamp of the event",
    )
    user_message: str = Field(
        ...,
        description="Original user message content",
    )
    srm_decision: dict[str, Any] = Field(
        ...,
        description="SRM decision output with intent, intent_category, funnel_stage, action, confidence, safety_concern, handoff_required",
    )
    context_summary: Optional[str] = Field(
        default=None,
        description="Brief summary of the conversation context",
    )
    prompt_built: Optional[str] = Field(
        default=None,
        description="Generated prompt instructions",
    )
    processing_time_ms: Optional[int] = Field(
        default=None,
        ge=0,
        description="Processing time in milliseconds",
    )
    mode: str = Field(
        default="normal",
        description="Flow mode used for this response (normal, rag, safety, handoff)",
    )
    channel: str = Field(
        default="unknown",
        description="Communication channel (whatsapp, web)",
    )


class TraceListResponse(BaseModel):
    """Paginated list of traces."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [],
                "total": 0,
                "limit": 20,
                "offset": 0,
            }
        }
    )

    items: list[TraceItem] = Field(
        default_factory=list,
        description="List of trace items",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of matching traces",
    )
    limit: int = Field(
        ...,
        ge=1,
        description="Maximum items per page",
    )
    offset: int = Field(
        ...,
        ge=0,
        description="Current offset",
    )


class TraceDetailResponse(TraceItem):
    """
    Detailed trace response extending TraceItem with full context and prompt objects.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
                "timestamp": "2025-12-31T10:30:00Z",
                "user_message": "Ola, gostaria de agendar uma consulta",
                "srm_decision": {
                    "intent": "scheduling_request",
                    "intent_category": "scheduling",
                    "funnel_stage": "consideration",
                    "action": "collect_availability",
                    "confidence": 0.92,
                    "safety_concern": False,
                    "handoff_required": False,
                },
                "context_summary": "Paciente novo buscando agendamento",
                "prompt_built": "Pergunte sobre a disponibilidade...",
                "processing_time_ms": 45,
                "mode": "normal",
                "full_context": {
                    "recent_messages": [],
                    "session_summary": "Nova sessao de agendamento",
                },
                "full_prompt": {
                    "system": "Voce e um assistente...",
                    "instructions": "Pergunte sobre a disponibilidade...",
                    "constraints": [],
                },
                "session_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                "event_data": {},
                "event_metadata": {},
            }
        }
    )

    full_context: Optional[dict[str, Any]] = Field(
        default=None,
        description="Full context object used in the decision",
    )
    full_prompt: Optional[dict[str, Any]] = Field(
        default=None,
        description="Full prompt object generated for the LLM",
    )
    session_id: Optional[UUID] = Field(
        default=None,
        description="Associated session identifier",
    )
    event_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw event data payload",
    )
    event_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw event metadata",
    )
    llm_response: Optional[str] = Field(
        default=None,
        description="LLM-generated suggested response",
    )
    selected_context: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Context sources used in prompt generation (config fields, RAG docs)",
    )


class TraceStatsResponse(BaseModel):
    """Aggregated trace statistics over a time period."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_traces": 150,
                "avg_processing_time_ms": 42.5,
                "safety_flags_count": 3,
                "handoff_count": 7,
                "intent_distribution": {
                    "scheduling_request": 45,
                    "price_question": 30,
                    "greeting": 25,
                },
                "period_start": "2025-12-24T00:00:00Z",
                "period_end": "2025-12-31T00:00:00Z",
            }
        }
    )

    total_traces: int = Field(
        ...,
        ge=0,
        description="Total number of traces in the period",
    )
    avg_processing_time_ms: float = Field(
        ...,
        ge=0.0,
        description="Average processing time in milliseconds",
    )
    safety_flags_count: int = Field(
        ...,
        ge=0,
        description="Number of traces with safety concerns flagged",
    )
    handoff_count: int = Field(
        ...,
        ge=0,
        description="Number of traces requiring human handoff",
    )
    total_tokens_sum: int = Field(
        default=0,
        ge=0,
        description="Total LLM tokens consumed in the period",
    )
    model_breakdown: list[dict] = Field(
        default_factory=list,
        description="Token consumption per model, sorted descending by tokens",
    )
    intent_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="Distribution of intents across traces",
    )
    outside_hours_count: int = Field(
        default=0,
        ge=0,
        description="Conversational traces received outside configured business hours",
    )
    outside_hours_pct: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Percentage of conversational traces received outside business hours",
    )
    conversational_trace_count: int = Field(
        default=0,
        ge=0,
        description="Message traces carrying conversational decision markers",
    )
    new_users_count: int = Field(
        default=0,
        ge=0,
        description="Distinct external users whose first-ever interaction occurred in the period",
    )
    returning_users_count: int = Field(
        default=0,
        ge=0,
        description="Distinct external users first seen before the period who interacted in it",
    )
    new_user_interactions: int = Field(
        default=0,
        ge=0,
        description="Interactions in the period made by users first seen during the period",
    )
    returning_user_interactions: int = Field(
        default=0,
        ge=0,
        description="Interactions in the period made by users first seen before the period",
    )
    new_user_interaction_pct: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Percentage of identified interactions made by new users",
    )
    returning_user_interaction_pct: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Percentage of identified interactions made by returning users",
    )
    identified_user_interactions: int = Field(
        default=0,
        ge=0,
        description="Interactions carrying a phone/username that can be classified",
    )
    unidentified_user_interactions: int = Field(
        default=0,
        ge=0,
        description="Interactions without a phone/username, excluded from audience percentages",
    )
    business_hours_configured: bool = Field(
        default=False,
        description="Whether valid structured business hours are configured",
    )
    period_start: str = Field(
        ...,
        description="ISO 8601 start of the statistics period",
    )
    period_end: str = Field(
        ...,
        description="ISO 8601 end of the statistics period",
    )
