"""
NeuroStack - Support Ticket Model
Persists support tickets submitted by clinics/builders.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    clinic_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    tipo: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    mensagem: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    filename: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    file_type: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="open",
        server_default="open",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    routed_to: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
    )
    routed_to_builder_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<SupportTicket(id={self.id}, email='{self.email}', tipo='{self.tipo}')>"
