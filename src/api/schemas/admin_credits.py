"""Request/response bodies of the credit operational admin API (Group 5).

Every id of authority (the actor, the issuer) comes from the authenticated
JWT, never from these bodies; ``expected_environment`` is only compared.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.schemas.credits import LotStatus
from src.models.credit import COURTESY_REASON_MAX_LENGTH
from src.services.credit_courtesy import COURTESY_MAX_CREDITS

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
    usable_courtesy: bool = Field(
        ...,
        description=(
            "An unexpired courtesy lot with a positive balance: counts for the "
            "enforcement cutoff's coverage only, never for `usable` nor "
            "`reconciliation_failure`. Under the courtesy rule (a builder with "
            "courtesy, neither the global enforcement nor an operator mark) "
            "requests are not gated by this coverage nor by a quarantine at "
            "all: they debit only courtesy lots, which need no ASAAS fact."
        ),
    )
    usable: bool


class CreditRolloutStatus(_Strict):
    enforcement_at: Optional[datetime]
    capability_ready: bool
    reconciliation_ready: bool
    orphan_effects: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Courtesy credits (creditos-cortesia).  The actor is the JWT super admin; the
# ``Idempotency-Key`` header (1 to 200 characters) is a parameter of the
# route, never a body field.
# ---------------------------------------------------------------------------

_REASON_DESCRIPTION = (
    "Why (1 to 500 characters, not only whitespace, no NUL). Shown to "
    "super admins only: never to the builder, never logged."
)


def _valid_reason(value: str) -> str:
    # PostgreSQL text cannot hold NUL: refused as a 422, never a 500.
    if "\x00" in value:
        raise ValueError("reason must not contain NUL characters")
    # A reason of whitespace only says nothing: the audit needs a why.
    if not value.strip():
        raise ValueError("reason must not be blank")
    return value


class CourtesyGrantCreate(_Strict):
    """``POST /courtesy-grants``: a courtesy lot valid 30 civil days."""

    builder_id: UUID
    credits: int = Field(
        ...,
        strict=True,
        ge=1,
        le=COURTESY_MAX_CREDITS,
        description="Whole credits, 1 to 100,000 (1 credit = 1,000 mc).",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=COURTESY_REASON_MAX_LENGTH,
        description=_REASON_DESCRIPTION,
    )

    _reason_is_valid = field_validator("reason")(_valid_reason)


class CourtesyRemovalCreate(_Strict):
    """``POST /courtesy-grants/{lot_id}/removals``: exactly one of
    ``credits`` (N whole credits, at most the lot's whole spendable credits)
    and ``remove_all: true`` (the whole spendable balance, fraction of a
    credit included)."""

    credits: Optional[int] = Field(
        default=None,
        strict=True,
        ge=1,
        le=COURTESY_MAX_CREDITS,
        description=(
            "Whole credits to remove, 1 to floor(spendable mc / 1000) and at "
            "most 100,000; more than the balance is "
            "409 courtesy_removal_exceeds_balance. Exclusive with remove_all."
        ),
    )
    remove_all: Optional[bool] = Field(
        default=None,
        strict=True,
        description=(
            "true: remove the lot's whole spendable balance (fraction "
            "included). Exclusive with credits; false is refused."
        ),
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=COURTESY_REASON_MAX_LENGTH,
        description=_REASON_DESCRIPTION,
    )

    _reason_is_valid = field_validator("reason")(_valid_reason)

    @model_validator(mode="after")
    def _exactly_one_form(self) -> "CourtesyRemovalCreate":
        if self.remove_all is False:
            raise ValueError("remove_all is either true or absent")
        if (self.remove_all is True) == (self.credits is not None):
            raise ValueError("give exactly one of credits and remove_all")
        return self


class CourtesyGrantResponse(_Strict):
    """The grant as recorded; the same body for a replay of its key."""

    event_id: UUID
    builder_id: UUID
    lot_id: UUID
    granted_mc: int = Field(ge=0)
    expires_at: datetime
    occurred_at: datetime
    actor_user_id: UUID


class CourtesyRemovalResponse(_Strict):
    """The removal as recorded; the same body for a replay of its key."""

    event_id: UUID
    builder_id: UUID
    lot_id: UUID
    removed_mc: int = Field(ge=0)
    occurred_at: datetime
    actor_user_id: UUID


class CourtesyLotItem(_Strict):
    """One courtesy lot, with the names of the builder's ``CreditLot``.

    ``available_mc`` is the spendable balance after the expiry sweep at
    ``as_of`` (what ``remove_all`` removes); ``removed_mc`` what super
    admins took back.  A lot zeroed by removals is ``exhausted``."""

    lot_id: UUID
    kind: Literal["courtesy"]
    status: LotStatus
    granted_mc: int = Field(ge=0)
    available_mc: int = Field(ge=0)
    consumed_mc: int = Field(ge=0)
    removed_mc: int = Field(ge=0)
    expired_mc: int = Field(ge=0)
    paid_at: date = Field(..., description="The civil date of the grant.")
    expires_at: datetime


class CourtesyEventItem(_Strict):
    """One grant or removal with its actor and reason (super admins only)."""

    event_id: UUID
    lot_id: UUID
    kind: Literal["grant", "removal"]
    amount_mc: int = Field(gt=0)
    reason: str
    actor_user_id: UUID
    occurred_at: datetime


class CourtesyGrantsResponse(_Strict):
    """``GET /courtesy-grants?builder_id=``: every courtesy lot of the builder
    (expiry order, as the ledger spends them) and every event, oldest
    first."""

    as_of: datetime
    lots: list[CourtesyLotItem]
    events: list[CourtesyEventItem]


__all__ = [
    "AuthorizationCreate",
    "AuthorizationResponse",
    "CourtesyEventItem",
    "CourtesyGrantCreate",
    "CourtesyGrantResponse",
    "CourtesyGrantsResponse",
    "CourtesyLotItem",
    "CourtesyRemovalCreate",
    "CourtesyRemovalResponse",
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
