"""Request/response bodies of the credit operational admin API (Group 5).

Every id of authority (the actor, the issuer) comes from the authenticated
JWT, never from these bodies; ``expected_environment`` is only compared.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

OperationalRole = Literal[
    "production_data_lifecycle_owner",
    "legal_hold_officer",
    "legal_hold_release_officer",
]
AuthorizationPurpose = Literal["hold_request", "hold_activate", "hold_release", "purge"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleAssignmentCreate(_Strict):
    role: OperationalRole
    user_id: UUID
    reason: str = Field(min_length=1, max_length=500)
    expires_at: Optional[datetime] = None


class RoleAssignmentRevoke(_Strict):
    reason: str = Field(min_length=1, max_length=500)


class RoleAssignmentResponse(BaseModel):
    assignment_id: UUID


class RoleAssignmentItem(BaseModel):
    id: UUID
    role: str
    user_id: UUID
    environment: str
    action: str
    revokes_assignment_id: Optional[UUID]
    actor_user_id: UUID
    expires_at: Optional[datetime]
    created_at: Optional[datetime]


class AuthorizationCreate(_Strict):
    purpose: AuthorizationPurpose
    builder_id: UUID
    scope: str = Field(min_length=1, max_length=200)
    subject_user_id: UUID
    resource: Optional[str] = Field(default=None, max_length=512)
    fence_generation: Optional[int] = Field(default=None, ge=0)
    ttl_seconds: int = Field(default=3600, ge=1, le=86400)


class AuthorizationResponse(BaseModel):
    authorization_id: UUID
    signature: str
    payload: dict[str, Any]


class HoldEventCreate(_Strict):
    builder_id: UUID
    scope: str = Field(min_length=1, max_length=200)
    reason_code: str = Field(min_length=1, max_length=80)
    authorization_id: UUID
    authorization_signature: str = Field(min_length=64, max_length=64)
    intent_id: Optional[UUID] = None
    justification: Optional[str] = Field(default=None, max_length=2000)


class HoldEventResponse(BaseModel):
    event_id: UUID
    intent_id: UUID
    event_type: str
    generation: int


class RefusalResponse(BaseModel):
    code: str


class CreditReconciliationStatus(_Strict):
    facts_without_ack: int = Field(ge=0)
    facts_without_inbox: int = Field(ge=0)
    rejected_open: int = Field(ge=0)
    digest_mismatches: int = Field(ge=0)
    open_revision_gaps: int = Field(ge=0)
    effects_without_outbox: int = Field(ge=0)
    quarantined_lines: int = Field(ge=0)
    binding_lag: int = Field(ge=0)
    open_alerts: int = Field(ge=0)
    ready: bool


class CreditCapabilityStatus(_Strict):
    environment: str
    billing_fact_schema_digest: str = Field(min_length=64, max_length=64)
    inbox_high_watermark: int = Field(ge=0)
    reconciliation_complete: bool
    readback_complete: bool
    has_open_gap: bool
    has_backlog: bool
    has_alert: bool
    ready: bool


class CreditBindingStatus(_Strict):
    builder_id: UUID
    monthly_binding: bool
    usable_topup: bool
    quarantined: bool
    identity_conflict: bool
    reconciliation_failure: bool
    usable: bool


class CreditRolloutStatus(_Strict):
    enforcement_at: Optional[datetime]
    capability_ready: bool
    reconciliation_ready: bool
    orphan_effects: int = Field(ge=0)


__all__ = [
    "AuthorizationCreate",
    "AuthorizationResponse",
    "CreditBindingStatus",
    "CreditCapabilityStatus",
    "CreditReconciliationStatus",
    "CreditRolloutStatus",
    "HoldEventCreate",
    "HoldEventResponse",
    "RefusalResponse",
    "RoleAssignmentCreate",
    "RoleAssignmentItem",
    "RoleAssignmentResponse",
    "RoleAssignmentRevoke",
]
