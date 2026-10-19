"""
NeuroStack API - Admin Schemas
Pydantic models for super-admin endpoints (clinics + builders management).
"""

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

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
    users: list[ClinicUserResponse] = Field(default_factory=list)
    # Health fields (enriched)
    health_status: Optional[str] = None
    last_trace_at: Optional[datetime] = None
    config_completeness: int = 0
    # Audit add-on: waives the 6-month export lookback (super admin only)
    audit_extended_access: bool = False


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
    total_clinics: int = 0
    active_clinics: int = 0
    total_builders: int = 0


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
    status: Optional[str] = Field(
        default=None, pattern=r"^(active|inactive|suspended)$"
    )
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(default=None, min_length=6)


class UpdateClinicRequest(BaseModel):
    """Request body for updating a clinic (tenant + user credentials)."""

    clinic_name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(default=None, min_length=6)
    audit_extended_access: Optional[bool] = Field(
        default=None,
        description=(
            "Audit add-on: when true, trace and handoff CSV exports skip the "
            "6-month lookback (the 60-day-per-file cap still applies)."
        ),
    )


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
    # Audit add-on: waives the 6-month export lookback (super admin only)
    audit_extended_access: bool = False


class BuilderWithClinicsResponse(BaseModel):
    """Builder with linked clinics for admin panel."""

    id: UUID
    name: str
    admin_email: Optional[str] = None
    status: str
    subscription_status: str
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
    total_clinics: int = 0
    active_clinics: int = 0
    total_builders: int = 0
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


# ============================================
# Billing control-plane schemas
# ============================================


class BillingOperationMaterialRequest(BaseModel):
    """Closed material signed by each billing-operation participant."""

    scope: Literal["rollout", "topup_capability", "incident"]
    operation_id: UUID
    environment: Literal["development", "staging", "production", "test"]
    from_state: str = Field(min_length=1, max_length=40)
    to_state: str = Field(min_length=1, max_length=40)
    budget_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deployment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime


class BillingOperationAssignmentRequest(BillingOperationMaterialRequest):
    role: Literal["deployer", "approver", "reviewer"]
    actor_user_id: UUID


class BillingOperationAssignmentResponse(BaseModel):
    assignment_id: UUID
    operation_id: UUID
    role: str
    actor_user_id: UUID
    active: bool
    expires_at: datetime
    payload_digest: str


class BillingOperationReceiptRequest(BillingOperationMaterialRequest):
    """Ask the backend to sign one receipt; no client signature is accepted.

    Closed: an extra field (such as the retired ``signature``) is a 422.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: UUID
    nonce: UUID
    evidence_type: Optional[
        Literal["producer", "credit", "reconciliation", "verification"]
    ] = None
    request_id: Optional[UUID] = None
    inbox_high_watermark: Optional[int] = Field(default=None, ge=0)


class BillingOperationReceiptResponse(BaseModel):
    receipt_id: UUID
    operation_id: UUID
    role: str
    actor_user_id: UUID
    nonce: UUID
    applied: bool


class BillingRolloutTransitionRequest(BillingOperationMaterialRequest):
    expected_revision: int = Field(ge=0)
    canary_builder_id: Optional[UUID] = None


class BillingRolloutReadinessReceiptRequest(BillingOperationMaterialRequest):
    """One readiness receipt for the ``canary_allowlist -> general`` quorum.

    The response repeats the accepted request with its ``receipt_id``.  The
    request is closed: a field not declared here is refused with 422.
    """

    model_config = ConfigDict(extra="forbid")

    environment: Literal["development", "staging", "production", "test"]
    receipt_kind: Literal["readiness", "backlog", "provisioning", "smoke"]
    rollout_revision: int = Field(ge=0)
    status: Literal["passed"]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_receipt_id: UUID
    provider_receipt_id: UUID
    rule_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    app_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    expires_at: datetime


class BillingRolloutReadinessReceiptResponse(BillingRolloutReadinessReceiptRequest):
    receipt_id: UUID


class BillingTopupCapabilityEvidenceRequest(BaseModel):
    capability_receipt_id: UUID
    producer_receipt_id: UUID
    credit_receipt_id: UUID
    reconciliation_receipt_id: UUID
    verification_receipt_id: UUID
    producer_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    credit_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    billing_fact_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    producer_actor_id: UUID
    credit_actor_id: UUID
    reconciliation_actor_id: UUID
    verification_actor_id: UUID
    verified_at: datetime
    operation_id: UUID
    deployment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: UUID
    inbox_high_watermark: int = Field(ge=0)


class BillingTopupCapabilityEnableRequest(BillingOperationMaterialRequest):
    expected_revision: int = Field(ge=0)
    evidence: BillingTopupCapabilityEvidenceRequest


class BillingTopupCapabilityDisableRequest(BillingOperationMaterialRequest):
    expected_revision: int = Field(ge=0)
    reason: Literal["health", "revocation"]


class BillingRolloutControlResponse(BaseModel):
    environment: str
    state: str
    rollout_revision: int
    topup_commercial_enabled: bool
    topup_revision: int
