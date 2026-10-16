"""
NeuroStack API - Admin Schemas
Pydantic models for super-admin endpoints (clinics + builders management).
"""

from datetime import date, datetime
from typing import Any, Literal, Optional
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
# Billing migration and control-plane schemas
# ============================================


class BillingOperationMaterialRequest(BaseModel):
    """Closed material signed by each billing-operation participant."""

    scope: Literal["migration", "rollout", "topup_capability", "incident"]
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
    assignment_id: UUID
    nonce: UUID
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
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


class BillingMigrationTargetRequest(BaseModel):
    catalog_version_id: str = Field(min_length=1, max_length=80)
    offer_id: str = Field(min_length=1, max_length=100)
    plan: str = Field(min_length=1, max_length=100)
    coupon_code: Optional[str] = Field(default=None, max_length=50)
    amount_cents: int = Field(ge=0)
    snapshot: dict[str, Any]


class BillingMigrationRosterRequest(BaseModel):
    builder_id: UUID
    subscription_id: UUID
    renewal_date: date
    target: BillingMigrationTargetRequest


class BillingMigrationTransitionRequest(BillingOperationMaterialRequest):
    migration_id: UUID
    readback_receipt_id: Optional[UUID] = None


class BillingMigrationCancelBeginRequest(BillingMigrationTransitionRequest):
    pass


class BillingMigrationCancelReconcileRequest(BillingMigrationTransitionRequest):
    """A fresh panel observation; the backend persists it as the receipt."""

    cancelled: bool
    observed_at: Optional[datetime] = None


class BillingMigrationRecoverCheckoutRequest(BillingMigrationTransitionRequest):
    checkout_session_id: UUID
    environment: Literal["development", "staging", "production", "test"]


class BillingMigrationReserveAttemptRequest(BillingMigrationTransitionRequest):
    environment: Literal["development", "staging", "production", "test"]
    absence_receipt_id: UUID


class BillingProviderEvidenceRequest(BaseModel):
    """One provider-signed ASAAS observation, verbatim from the authority."""

    model_config = ConfigDict(extra="forbid")

    external_reference: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,160}$")
    provider_event_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,160}$")
    provider_key_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,80}$")
    provider_operation: Literal[
        "subscription.migration.provider_flip", "subscription.migration.absence"
    ]
    provider_signature: str = Field(pattern=r"^[0-9a-f]{128}$")
    provider_observed_at: datetime
    provider_expires_at: datetime
    provider_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class BillingMigrationProviderFlipRequest(BillingMigrationTransitionRequest):
    """The ASAAS ID is ``evidence.external_reference``, covered by the signature."""

    evidence: BillingProviderEvidenceRequest
    observed_at: Optional[datetime] = None


class BillingMigrationAbsenceReadRequest(BaseModel):
    """Record one provider-signed empty ASAAS read for a recreate decision."""

    model_config = ConfigDict(extra="forbid")

    migration_id: UUID
    evidence: BillingProviderEvidenceRequest
    observed_at: Optional[datetime] = None


class BillingMigrationAbsenceReadResponse(BaseModel):
    read_id: UUID
    read_no: int
    prior_checkout_session_id: Optional[UUID] = None
    convergence_limit_at: datetime
    authorization_receipt_id: Optional[UUID] = None


class BillingMigrationResponse(BaseModel):
    migration_id: UUID
    subscription_id: UUID
    state: str
    attempt_count: int
    source_roster_revision: int
    renewal_date: date
    open_by_date: date
    cutover_by_date: date


class BillingCheckoutReservationResponse(BaseModel):
    checkout_session_id: UUID
    attempt_no: int
    state: str
    next_due_date: Optional[date] = None


class BillingRolloutTransitionRequest(BillingOperationMaterialRequest):
    expected_revision: int = Field(ge=0)
    canary_builder_id: Optional[UUID] = None


class BillingRolloutReadinessReceiptRequest(BillingOperationMaterialRequest):
    environment: Literal["development", "staging", "production", "test"]
    receipt_kind: Literal["readiness", "backlog", "provisioning", "smoke"]
    rollout_revision: int = Field(ge=0)
    roster_revision: int = Field(ge=0)
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
