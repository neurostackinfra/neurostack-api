"""
NeuroStack API - Admin Schemas
Pydantic models for super-admin endpoints (clinics + builders management).
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


# ============================================
# Clinic Schemas (Group 3)
# ============================================


class ClinicUserResponse(BaseModel):
    """User summary within a clinic slot."""

    id: UUID
    email: str
    role: str


class ClinicSlotResponse(BaseModel):
    """Full clinic representation for the admin panel."""

    clinic_id: UUID
    clinic_name: str
    agent_name: Optional[str] = None
    slug: Optional[str] = None
    status: str = "active"
    plan_type: str = "pilot"
    trace_count: int = 0
    apis_configured: bool = False
    zapi_instance_id: Optional[str] = None
    users: list[ClinicUserResponse] = Field(default_factory=list)
    # Health fields (enriched)
    health_status: Optional[str] = None
    last_trace_at: Optional[datetime] = None
    config_completeness: int = 0


class CreateClinicRequest(BaseModel):
    """Request body for creating a new clinic (tenant + user + config)."""

    clinic_name: str = Field(..., min_length=1, max_length=255)
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=6)
    plan_type: str = Field(default="clinic", pattern=r"^(clinic|pilot|demo|admin)$")
    builder_id: Optional[UUID] = None


class AdminStatsResponse(BaseModel):
    """Aggregated stats for the admin dashboard."""

    total_traces: int = 0
    total_tokens: int = 0
    open_tickets: int = 0
    active_pilots: int = 0
    active_builders: int = 0
    active_managed_clinics: int = 0
    estimated_mrr: float = 0.0


# ============================================
# Builder Schemas (Group 4)
# ============================================


class CreateBuilderRequest(BaseModel):
    """Request body for creating a builder."""

    name: str = Field(..., min_length=1, max_length=255)
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=6)


class UpdateBuilderRequest(BaseModel):
    """Request body for updating a builder."""

    name: Optional[str] = Field(default=None, max_length=255)
    status: Optional[str] = Field(default=None, pattern=r"^(active|inactive|suspended)$")
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(default=None, min_length=6)


class UpdateClinicRequest(BaseModel):
    """Request body for updating a clinic (tenant + user credentials)."""

    clinic_name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(default=None, min_length=6)


class BuilderClinicResponse(BaseModel):
    """Clinic summary within a builder view."""

    clinic_id: UUID
    clinic_name: str
    admin_email: Optional[str] = None
    slug: Optional[str] = None
    status: str = "active"
    plan_type: str = "pilot"
    trace_count: int = 0
    apis_configured: bool = False
    # Health fields (enriched)
    health_status: Optional[str] = None
    last_trace_at: Optional[datetime] = None
    config_completeness: int = 0


class BuilderWithClinicsResponse(BaseModel):
    """Builder with linked clinics for admin panel."""

    id: UUID
    name: str
    admin_email: Optional[str] = None
    status: str
    created_at: datetime
    clinics: list[BuilderClinicResponse] = Field(default_factory=list)


class CreateBuilderClinicRequest(BaseModel):
    """Request body for creating a clinic under a builder."""

    clinic_name: str = Field(..., min_length=1, max_length=255)
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=6)


# ============================================
# Dashboard Schemas (Admin Control Tower)
# ============================================


class AdminDashboardStatsResponse(BaseModel):
    """Extended stats for the admin dashboard (superset of AdminStatsResponse)."""

    total_traces: int = 0
    total_tokens: int = 0
    open_tickets: int = 0
    active_pilots: int = 0
    active_builders: int = 0
    active_managed_clinics: int = 0
    estimated_mrr: float = 0.0
    safety_flags_7d: int = 0
    handoff_count_7d: int = 0


class AdminAlertResponse(BaseModel):
    """A single computed alert for the admin dashboard."""

    type: str  # inactivity, deactivated, api_error
    severity: str  # critical, warning, info
    clinic_id: UUID
    clinic_name: str
    message: str
    detected_at: Optional[datetime] = None


class AdminTimeSeriesPoint(BaseModel):
    """A single day in the admin traces time-series."""

    date: str  # YYYY-MM-DD
    count: int = 0


class AdminDashboardResponse(BaseModel):
    """Combined admin dashboard response: stats + time-series + alerts."""

    stats: AdminDashboardStatsResponse
    time_series: list[AdminTimeSeriesPoint] = []
    alerts: list[AdminAlertResponse] = []
