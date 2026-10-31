"""Credit enforcement rollout records (Group 7).

This module deliberately contains no rollout authority.  A cutoff acceptance
only records the owner decision that a *particular* tenant/report snapshot may
proceed through the server-disabled enforcement transaction.  It never makes
the tenant eligible for credit and it cannot enable ASAAS/top-up rollout.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class CreditCutoffAcceptance(Base):
    """Immutable owner acknowledgement for one tenant and cutoff digest."""

    __tablename__ = "credit_cutoff_acceptances"
    __table_args__ = (
        CheckConstraint(
            "length(reason) > 0", name="ck_credit_cutoff_acceptances_reason"
        ),
        CheckConstraint(
            "report_digest ~ '^[0-9a-f]{64}$'",
            name="ck_credit_cutoff_acceptances_report_digest",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "tenant_id",
            "builder_id",
            "reason",
            "report_digest",
            "author_user_id",
            "accepted_on",
            name="uq_credit_cutoff_acceptances_cutoff",
        ),
        Index("ix_credit_cutoff_acceptances_report", "report_digest", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    builder_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    report_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    author_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["CreditCutoffAcceptance"]
