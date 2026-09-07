"""
NeuroStack - User Model
Represents authenticated users (clinic admins, super admins, investors).
"""

from enum import Enum
from typing import TYPE_CHECKING, Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean
from sqlalchemy import Enum as PGEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.tenant import Tenant


class UserRole(str, Enum):
    """Roles available to authenticated users."""

    clinic_admin = "clinic_admin"
    super_admin = "super_admin"
    investor = "investor"
    builder = "builder"


class User(Base, TimestampMixin):
    """
    User model for JWT-authenticated access.

    Each user belongs to a tenant and has a role that determines
    their permissions in the web dashboard.

    Attributes:
        id: Unique user identifier (UUID)
        tenant_id: FK to tenants.id
        email: Unique email address
        password_hash: Bcrypt password hash
        role: User role (clinic_admin, super_admin, investor)
        is_active: Whether the user can log in
    """

    __tablename__ = "users"

    # Primary Key
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Tenant FK
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Core Fields
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    role: Mapped[UserRole] = mapped_column(
        PGEnum(UserRole, name="userrole", create_type=True),
        nullable=False,
    )

    # Builder FK (nullable — only builder-role users have this)
    builder_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship("Tenant")

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email='{self.email}', role={self.role.value})>"
