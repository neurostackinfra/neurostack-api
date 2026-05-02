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
    """Clinic summary for builder panel."""

    id: UUID
    name: str
    slug: Optional[str] = None
    status: str = "active"
    plan_type: str = "pilot"
    is_active: bool = True
    created_at: Optional[datetime] = None


class CreateBuilderClinicRequest(BaseModel):
    """Request body for creating a clinic under the authenticated builder."""

    clinic_name: str = Field(..., min_length=1, max_length=255)
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=8)


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
