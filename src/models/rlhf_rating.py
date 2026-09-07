"""
NeuroStack - RLHF Rating Model

Persists user-submitted quality ratings for assistant responses, decoupled
from the append-only Event Store so that human review can mutate audit
metadata (analysis, approval, acceptance) without violating the
immutability contract of ``events``.

Row lifecycle rules are enforced by the schema and match the executive
delete semantics of ``DELETE /v1/admin/clinics/{id}``:

* ``tenant_id`` / ``subject_tenant_id`` cascade on tenant deletion so that
  removing a clinic wipes both the ratings it produced and the ratings
  written *about* it.
* ``trace_event_id`` / ``trace_tenant_id`` are cleared together via
  ``SET NULL`` — a rating outlives the trace it once pointed at.
* ``submitted_by`` / ``accepted_by`` degrade to ``NULL`` when the user
  record is removed, preserving the audit trail with only the identity
  of the actor stripped.
* ``session_id`` intentionally carries **no** foreign key. Sessions on
  the ``events`` table cascade on delete, and inheriting that behaviour
  here would give a rating a second cause of death, contradicting the
  rule the remaining FKs establish (a rating only disappears with its
  owner clinic).
"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class RlhfRating(Base):
    """Human-in-the-loop rating for a single assistant response."""

    __tablename__ = "rlhf_ratings"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('whatsapp', 'web', 'unknown')",
            name="ck_rlhf_ratings_channel",
        ),
        CheckConstraint(
            "rating BETWEEN 1 AND 5",
            name="ck_rlhf_ratings_rating_range",
        ),
        Index(
            "idx_rlhf_ratings_created_at_desc",
            text("created_at DESC"),
        ),
        Index(
            "idx_rlhf_ratings_tenant_created_at_desc",
            "tenant_id",
            text("created_at DESC"),
        ),
    )

    # Primary Key
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Ownership / scope
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Session / message correlation
    # NOTE: no FK on session_id — see module docstring.
    session_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    message_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Trace linkage (nullable pair — filled or cleared together)
    trace_event_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )
    trace_tenant_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Rating payload
    channel: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    rating: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    agent_response: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    user_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Rater identity
    submitted_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # AI analysis metadata
    ai_analysis: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
    )
    ai_analysis_status: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Human review outcome
    approved_for_training: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    ignored: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    accepted_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<RlhfRating(id={self.id}, tenant={self.tenant_id}, "
            f"subject={self.subject_tenant_id}, rating={self.rating})>"
        )
