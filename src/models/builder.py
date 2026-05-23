"""
NeuroStack - Builder Model
Represents a builder (company/partner) that manages one or more clinic tenants.
"""

from typing import TYPE_CHECKING, Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.tenant import Tenant


class Builder(Base, TimestampMixin):
    """Builder entity — owns and manages multiple clinic tenants."""

    __tablename__ = "builders"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False,
    )

    # Billing / subscription
    subscription_plan: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True,
    )
    billing_cycle: Mapped[str] = mapped_column(
        String(10), default="monthly", server_default="monthly", nullable=False,
    )
    subscription_status: Mapped[str] = mapped_column(
        String(20), default="trial", server_default="trial", nullable=False,
    )

    # Relationships
    tenants: Mapped[list["Tenant"]] = relationship(
        "Tenant", back_populates="builder", lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<Builder(id={self.id}, name='{self.name}', status='{self.status}')>"
