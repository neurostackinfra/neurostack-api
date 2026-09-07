"""
NeuroStack - Base ORM Models
Foundation classes for SQLAlchemy models.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as PythonUUID
from uuid import uuid4

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy models.

    Provides common functionality like automatic table naming
    and JSON serialization.
    """

    def to_dict(self) -> dict[str, Any]:
        """Convert model instance to dictionary."""
        result = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            result[column.name] = value
        return result

    def __repr__(self) -> str:
        """String representation of model."""
        class_name = self.__class__.__name__
        if hasattr(self, "id"):
            return f"<{class_name}(id={self.id})>"
        return f"<{class_name}>"


class TimestampMixin:
    """
    Mixin that adds created_at and updated_at columns.

    - created_at: Set automatically on insert
    - updated_at: Updated automatically on every update
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UUIDMixin:
    """
    Mixin that adds a UUID primary key column.
    """

    id: Mapped[PythonUUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        nullable=False,
    )
