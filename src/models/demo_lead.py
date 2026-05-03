"""
NeuroStack - Demo Lead Model
Captures leads from the investor demo gate form.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class DemoLead(Base):
    """
    Demo lead captured from the investor portal gate form.

    Attributes:
        id: Unique lead identifier (UUID)
        name: Full name of the lead
        email: Email address
        company: Company or organization name
        phone: Phone number
        created_at: When the lead was captured
    """

    __tablename__ = "demo_leads"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    company: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    phone: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<DemoLead(id={self.id}, email='{self.email}')>"
