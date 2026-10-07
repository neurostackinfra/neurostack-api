"""
NeuroStack API - Builder Schemas
Pydantic models for builder-scoped endpoints.
"""

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    TypeAdapter,
    field_validator,
)

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


# ============================================
# Builder Profile
# ============================================


class BuilderProfileResponse(BaseModel):
    """Builder profile returned by GET /me."""

    id: UUID
    name: str
    status: str
    clinic_count: int = 0
    support_email: Optional[EmailStr] = None


class UpdateBuilderProfileRequest(BaseModel):
    """Request body for PATCH /v1/builder/profile.

    `support_email` may be `null` or an empty string to clear the field.
    Non-empty values must be a valid email address (422 otherwise).
    """

    support_email: Optional[str] = None

    @field_validator("support_email", mode="before")
    @classmethod
    def _validate_support_email(cls, value: object) -> Optional[str]:
        # Explicit null clears
        if value is None:
            return None
        # Normalize to string, empty means "clear"
        if not isinstance(value, str):
            raise ValueError("support_email must be a string or null")
        stripped = value.strip()
        if stripped == "":
            return None
        # Re-use EmailStr validation for the non-empty case
        return _EMAIL_ADAPTER.validate_python(stripped)


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
    contact_phone: Optional[str] = None
    admin_email: Optional[EmailStr] = None
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
    contact_phone: Optional[str] = None


class UpdateBuilderClinicRequest(BaseModel):
    """Request body for PATCH /v1/builder/clinics/{clinic_id}.

    `contact_phone` semantics (enforced by the route, not this schema):
    - Absent or `null`: don't touch the field.
    - Empty/whitespace-only string: clear the field (persist `NULL`).
    - Non-empty string: strip non-digits, must yield 10-15 digits (else 400),
      persist digits-only.
    """

    contact_phone: Optional[str] = None


class UpdateClinicAdminEmailRequest(BaseModel):
    """Strict payload for changing a linked clinic administrator's email."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class ClinicClonePreviewResponse(BaseModel):
    """Advisory impact summary for cloning a Builder-owned clinic."""

    model_config = ConfigDict(extra="forbid", strict=True)

    suggested_name: str
    document_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class ClinicCloneResponse(BaseModel):
    """Authoritative identity and inactive state of a persisted clinic clone."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    name: str
    is_active: Literal[False]


class ClinicCloneErrorDetail(BaseModel):
    """Stable, redacted machine-readable clone failure."""

    model_config = ConfigDict(extra="forbid", strict=True)

    error: Literal[
        "clinic_clone_forbidden",
        "clinic_clone_source_not_found",
        "clinic_clone_invalid",
        "clinic_clone_persistence_failed",
    ]


class ClinicCloneErrorResponse(BaseModel):
    """HTTP error envelope emitted by clinic clone operations."""

    model_config = ConfigDict(extra="forbid", strict=True)

    detail: ClinicCloneErrorDetail


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
    model_breakdown: list[dict[str, object]] = []


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


# ============================================
# Support Tickets (builder-scoped)
# ============================================


class BuilderSupportTicketResponse(BaseModel):
    """A single support ticket routed to the authenticated builder.

    `clinic_name` and `contact_phone` are joined from the ticket's originating
    tenant (via `SupportTicket.tenant_id`), not from the snapshot columns on
    the ticket row, so the builder always sees the current display data.
    """

    id: int
    clinic_id: Optional[UUID] = None
    clinic_name: Optional[str] = None
    contact_phone: Optional[str] = None
    email: str
    tipo: str
    mensagem: str
    filename: Optional[str] = None
    file_type: Optional[str] = None
    status: str
    created_at: datetime


class UpdateTicketStatusRequest(BaseModel):
    """Request body for PATCH /v1/builder/tickets/{ticket_id}/status.

    `status` must be exactly `open` or `closed`; any other value yields 400.
    """

    status: str
