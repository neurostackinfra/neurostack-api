"""Credit-only operational roles and signed authorizations (Group 5, 041).

Mirrors migration ``041``.  The three roles exist only here (never the ASAAS
``billing_operation_assignments``); ``super_admin`` is the existing JWT role
of ``users.role``, not an assignment.  All three tables are append-only by
trigger: a revocation is a new ``revoked`` row, a use is a receipt row whose
primary key is the authorization (single use).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base

OPERATIONAL_ROLES: Final = (
    "production_data_lifecycle_owner",
    "legal_hold_officer",
    "legal_hold_release_officer",
)
ASSIGNMENT_ACTIONS: Final = ("granted", "revoked")
AUTHORIZATION_PURPOSES: Final = (
    "hold_request",
    "hold_activate",
    "hold_release",
    "purge",
)
ENVIRONMENTS: Final = ("development", "staging", "production", "test")
_SCOPE_PATTERN: Final = (
    "^tenant:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class CreditOperationalRoleAssignment(Base):
    """A grant (never for its own actor) or the revocation of one grant."""

    __tablename__ = "credit_operational_role_assignments"
    __table_args__ = (
        CheckConstraint(
            _in("role", OPERATIONAL_ROLES),
            name="ck_credit_operational_role_assignments_role",
        ),
        CheckConstraint(
            _in("environment", ENVIRONMENTS),
            name="ck_credit_operational_role_assignments_environment",
        ),
        CheckConstraint(
            "(action = 'granted' AND revokes_assignment_id IS NULL "
            "AND actor_user_id <> user_id) "
            "OR (action = 'revoked' AND revokes_assignment_id IS NOT NULL "
            "AND expires_at IS NULL)",
            name="ck_credit_operational_role_assignments_action",
        ),
        CheckConstraint(
            "length(reason) > 0",
            name="ck_credit_operational_role_assignments_reason",
        ),
        UniqueConstraint(
            "revokes_assignment_id",
            name="uq_credit_operational_role_assignments_revocation",
        ),
        Index(
            "ix_credit_operational_role_assignments_user",
            "user_id",
            "role",
            "environment",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    revokes_assignment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credit_operational_role_assignments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    actor_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditOperationalAuthorization(Base):
    """One backend-signed authorization (HMAC-SHA256 of its canonical
    payload with ``credit_authorization_signing_key``)."""

    __tablename__ = "credit_operational_authorizations"
    __table_args__ = (
        CheckConstraint(
            _in("purpose", AUTHORIZATION_PURPOSES),
            name="ck_credit_operational_authorizations_purpose",
        ),
        CheckConstraint(
            _in("environment", ENVIRONMENTS),
            name="ck_credit_operational_authorizations_environment",
        ),
        CheckConstraint(
            f"(scope = 'builder' OR scope ~ '{_SCOPE_PATTERN}')",
            name="ck_credit_operational_authorizations_scope",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(purpose = 'purge' AND fence_generation IS NOT NULL "
            "AND fence_generation >= 0) "
            "OR (purpose <> 'purge' AND fence_generation IS NULL)",
            name="ck_credit_operational_authorizations_fence",
        ),
        CheckConstraint(
            "expires_at > issued_at",
            name="ck_credit_operational_authorizations_window",
        ),
        CheckConstraint(
            "issued_by <> subject_user_id",
            name="ck_credit_operational_authorizations_issuer",
        ),
        CheckConstraint(
            "signature ~ '^[0-9a-f]{64}$' AND key_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_credit_operational_authorizations_digests",
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fence_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subject_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    issued_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditOperationalAuthorizationReceipt(Base):
    """The single use of one authorization."""

    __tablename__ = "credit_operational_authorization_receipts"
    __table_args__ = (
        CheckConstraint(
            "length(consumed_for) > 0 AND length(result_ref) > 0",
            name="ck_credit_operational_authorization_receipts_ref",
        ),
    )

    authorization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credit_operational_authorizations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    consumed_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    consumed_for: Mapped[str] = mapped_column(String(40), nullable=False)
    result_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "AUTHORIZATION_PURPOSES",
    "OPERATIONAL_ROLES",
    "CreditOperationalAuthorization",
    "CreditOperationalAuthorizationReceipt",
    "CreditOperationalRoleAssignment",
]
