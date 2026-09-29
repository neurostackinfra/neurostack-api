"""
NeuroStack API - Handoff Schemas
Request and response models for handoff endpoints.
"""

from datetime import datetime as _dt
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

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
    sla_target_seconds: Optional[int]
    status: str


class HandoffQueueResponse(BaseModel):
    """Response for GET /queue — items + aggregate stats."""

    items: list[HandoffQueueItem]
    avg_wait_seconds: Optional[float]
    pct_within_sla: Optional[float]
    within_sla_count: int
    confirmed_late_count: int
    unanswered_overdue_count: int
    maturing_count: int
    cancelled_unconfirmed_count: int
    unmeasured_legacy_count: int
    confirmed_count: int
    sla_eligible_count: int
    period_timezone: str
    period_start: _dt
    period_end_exclusive: _dt


class HandoffCheckoutResponse(HandoffQueueItem):
    """Response after checkout — item + computed wait."""

    wait_seconds: Optional[float] = None


__all__ = [
    "HandoffListResponse",
    "WebhookTestRequest",
    "WebhookTestResponse",
    "HandoffUpdateRequest",
    "MessageResponse",
    "HandoffQueueItem",
    "HandoffQueueResponse",
    "HandoffCheckoutResponse",
    # Re-export for convenience
    "HandoffDetail",
    "HandoffListItem",
    "HandoffStatus",
    "HandoffUrgency",
    "WebhookConfigResponse",
    "WebhookConfigCreate",
    "WebhookConfigUpdate",
]
