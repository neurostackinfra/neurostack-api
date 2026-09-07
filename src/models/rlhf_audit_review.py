"""
NeuroStack - RLHF Audit Review Model

Records a single human review signed against an :class:`RlhfRating`.

The verdict (``revisado``) is required; the free-text observation
(``observacao``) is optional because marking a rating as reviewed with
no additional comment is itself a legitimate outcome. The email of the
reviewer is captured at write time (``revisado_por_email``) so that the
audit trail survives even after the ``users`` row is removed, while the
foreign key (``revisado_por``) degrades to ``NULL`` in the same event.

If the parent rating is removed (which only happens when its owner
clinic is deleted — see :mod:`src.models.rlhf_rating`), every review of
that rating is cascaded away with it.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class RlhfAuditReview(Base):
    """A single signed human review of an :class:`RlhfRating`."""

    __tablename__ = "rlhf_audit_reviews"
    __table_args__ = (
        Index(
            "idx_rlhf_audit_reviews_rating_revisado_em_id",
            "rating_id",
            text("revisado_em DESC"),
            text("id DESC"),
        ),
    )

    # Primary Key
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Parent rating
    rating_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rlhf_ratings.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Reviewer verdict + optional note
    observacao: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    revisado: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )

    # Reviewer identity — id nullable (SET NULL), email frozen at write time
    revisado_por: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    revisado_por_email: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    revisado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<RlhfAuditReview(id={self.id}, rating={self.rating_id}, "
            f"revisado={self.revisado})>"
        )
