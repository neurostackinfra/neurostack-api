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


class CreateClinicRequest(BaseModel):
    """Request body for creating a new clinic (tenant + user + config)."""

    clinic_name: str = Field(..., min_length=1, max_length=255)
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=8)
    plan_type: str = Field(default="pilot", pattern=r"^(pilot|demo|admin)$")
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


class UpdateBuilderRequest(BaseModel):
    """Request body for updating a builder."""

    name: Optional[str] = Field(default=None, max_length=255)
    status: Optional[str] = Field(default=None, pattern=r"^(active|inactive|suspended)$")


class BuilderClinicResponse(BaseModel):
    """Clinic summary within a builder view."""

    clinic_id: UUID
    clinic_name: str
    slug: Optional[str] = None
    status: str = "active"
    plan_type: str = "pilot"
    trace_count: int = 0
    apis_configured: bool = False


class BuilderWithClinicsResponse(BaseModel):
    """Builder with linked clinics for admin panel."""

    id: UUID
    name: str
    status: str
    created_at: datetime
    clinics: list[BuilderClinicResponse] = Field(default_factory=list)


class CreateBuilderClinicRequest(BaseModel):
    """Request body for creating a clinic under a builder."""

    clinic_name: str = Field(..., min_length=1, max_length=255)
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=8)
