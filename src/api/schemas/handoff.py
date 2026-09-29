"""
NeuroStack API - Handoff Schemas
Request and response models for handoff endpoints.
"""

from datetime import datetime as _dt
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from src.handoff.models import (
    HandoffDetail,
    HandoffListItem,
    HandoffStatus,
    HandoffUrgency,
    WebhookConfigCreate,
    WebhookConfigResponse,
    WebhookConfigUpdate,
)


class HandoffListResponse(BaseModel):
    """Response for handoff list."""

    handoffs: list[HandoffListItem]
    total: int
    limit: int
    offset: int


class WebhookTestRequest(BaseModel):
    """Request to test webhook."""

    url: Optional[str] = None


class WebhookTestResponse(BaseModel):
    """Response from webhook test."""

    success: bool
    message: str
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None


class HandoffUpdateRequest(BaseModel):
    """Request to update handoff status."""

    status: HandoffStatus
    assigned_to: Optional[str] = None
    notes: Optional[str] = None


class MessageResponse(BaseModel):
    """Simple message response."""

    message: str


# ── Queue schemas ──────────────────────────────────────────────────────────────


class HandoffQueueItem(BaseModel):
    """Single item in the handoff queue."""

    id: UUID
    numero_whatsapp: Optional[str] = None
    session_id: UUID
    safety_flag: bool
    intencao_detectada: Optional[str] = None
    estagio_atual: Optional[str] = None
    primeira_linha_mensagem: Optional[str] = None
    created_at: _dt
    data_hora_saida: Optional[_dt] = None
    sla_target_seconds: Optional[int] = Field(ge=60, le=7200)
    status: HandoffStatus


class HandoffQueueResponse(BaseModel):
    """Response for GET /queue — items + aggregate stats."""

    items: list[HandoffQueueItem]
    avg_wait_seconds: Optional[float] = Field(ge=0)
    pct_within_sla: Optional[float] = Field(ge=0, le=100)
    within_sla_count: int = Field(ge=0)
    confirmed_late_count: int = Field(ge=0)
    unanswered_overdue_count: int = Field(ge=0)
    maturing_count: int = Field(ge=0)
    cancelled_unconfirmed_count: int = Field(ge=0)
    unmeasured_legacy_count: int = Field(ge=0)
    confirmed_count: int = Field(ge=0)
    sla_eligible_count: int = Field(ge=0)
    period_timezone: str
    period_start: _dt
    period_end_exclusive: _dt
    as_of: _dt


class HandoffCheckoutResponse(HandoffQueueItem):
    """Response after checkout — item + computed wait."""

    wait_seconds: Optional[float] = None


class HandoffNotFoundResponse(BaseModel):
    """Stable lifecycle not-found error."""

    detail: Literal["handoff_not_found"]


class HandoffConflictResponse(BaseModel):
    """Stable lifecycle conflict errors."""

    detail: Literal["handoff_conflict", "invalid_handoff_transition"]


class HandoffUnavailableResponse(BaseModel):
    """Stable lifecycle dependency failure."""

    detail: Literal["handoff_service_unavailable"]


__all__ = [
    "HandoffListResponse",
    "WebhookTestRequest",
    "WebhookTestResponse",
    "HandoffUpdateRequest",
    "MessageResponse",
    "HandoffQueueItem",
    "HandoffQueueResponse",
    "HandoffCheckoutResponse",
    "HandoffNotFoundResponse",
    "HandoffConflictResponse",
    "HandoffUnavailableResponse",
    # Re-export for convenience
    "HandoffDetail",
    "HandoffListItem",
    "HandoffStatus",
    "HandoffUrgency",
    "WebhookConfigResponse",
    "WebhookConfigCreate",
    "WebhookConfigUpdate",
]
