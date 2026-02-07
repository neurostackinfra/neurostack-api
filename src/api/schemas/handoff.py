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


__all__ = [
    "HandoffListResponse",
    "WebhookTestRequest",
    "WebhookTestResponse",
    "HandoffUpdateRequest",
    "MessageResponse",
    # Re-export for convenience
    "HandoffDetail",
    "HandoffListItem",
    "HandoffStatus",
    "HandoffUrgency",
    "WebhookConfigResponse",
    "WebhookConfigCreate",
    "WebhookConfigUpdate",
]
