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


__all__ = [
    "AuthorizationCreate",
    "AuthorizationResponse",
    "HoldEventCreate",
    "HoldEventResponse",
    "RefusalResponse",
    "RoleAssignmentCreate",
    "RoleAssignmentItem",
    "RoleAssignmentResponse",
    "RoleAssignmentRevoke",
]
