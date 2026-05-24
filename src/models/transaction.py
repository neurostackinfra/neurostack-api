"""
NeuroStack - Transaction Model
Records confirmed payments received via AbacatePay webhooks.
"""

from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class Transaction(Base, TimestampMixin):
    """Payment transaction logged from AbacatePay webhook events."""

    __tablename__ = "transactions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
    )
    builder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True,
    )
    builder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    method: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    cycle: Mapped[str] = mapped_column(String(10), nullable=False, default="monthly")
    plan: Mapped[str] = mapped_column(String(20), nullable=False, default="start")
    coupon_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)

    def __repr__(self) -> str:
        return f"<Transaction(id={self.id}, builder={self.builder_name}, amount={self.amount_cents})>"
