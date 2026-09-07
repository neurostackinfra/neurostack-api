"""
NeuroStack API - Handoff Schemas
Request and response models for handoff endpoints.
"""

from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from src.handoff.models import (
    HandoffDetail,
    HandoffListItem,
    HandoffStatus,
    HandoffUrgency,
    WebhookConfigResponse,
    WebhookConfigCreate,
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

from datetime import datetime as _dt


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
    status: str


class HandoffQueueResponse(BaseModel):
    """Response for GET /queue — items + aggregate stats."""

    items: list[HandoffQueueItem]
    avg_wait_seconds: float
    pct_within_sla: float


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
