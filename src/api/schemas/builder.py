"""
NeuroStack API - Builder Schemas
Pydantic models for builder-scoped endpoints.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


# ============================================
# Builder Profile
# ============================================


class BuilderProfileResponse(BaseModel):
    """Builder profile returned by GET /me."""

    id: UUID
    name: str
    status: str
    clinic_count: int = 0


# ============================================
# Clinic Schemas
# ============================================


class BuilderClinicResponse(BaseModel):
    """Clinic summary for builder panel with embedded health fields."""

    id: UUID
    name: str
    slug: Optional[str] = None
    status: str = "active"
    plan_type: str = "pilot"
    is_active: bool = True
    created_at: Optional[datetime] = None
    # Health fields (enriched)
    last_trace_at: Optional[datetime] = None
    health_status: str = "critical"
    trace_count_7d: int = 0
    safety_flags_7d: int = 0
    handoff_count_7d: int = 0
    outside_hours_count_7d: int = 0
    outside_hours_pct_7d: Optional[float] = None
    conversational_trace_count_7d: int = 0
    business_hours_configured: bool = False
    config_completeness: int = 0


class CreateBuilderClinicRequest(BaseModel):
    """Request body for creating a clinic under the authenticated builder."""

    clinic_name: str = Field(..., min_length=1, max_length=255)
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=6)


class ToggleClinicStatusRequest(BaseModel):
    """Request body for toggling clinic active/inactive status."""

    is_active: bool


# ============================================
# Stats
# ============================================


class BuilderStatsResponse(BaseModel):
    """Builder-scoped statistics."""

    clinic_count: int = 0
    active_clinics: int = 0
    inactive_clinics: int = 0
    total_traces: int = 0
    total_tokens: int = 0
    model_breakdown: list[dict] = []


# ============================================
# Dashboard & Alerts
# ============================================


class BuilderAlertResponse(BaseModel):
    """A single computed alert for a builder's clinic."""

    type: str  # inactivity, deactivated, api_error
    severity: str  # critical, warning, info
    clinic_id: UUID
    clinic_name: str
    message: str
    detected_at: Optional[datetime] = None


class TraceTimeSeriesPoint(BaseModel):
    """A single day in the traces time-series."""

    date: str  # YYYY-MM-DD
    count: int = 0


class BuilderDashboardResponse(BaseModel):
    """Combined dashboard response: stats + time-series + alerts."""

    stats: BuilderStatsResponse
    time_series: list[TraceTimeSeriesPoint] = []
    alerts: list[BuilderAlertResponse] = []
