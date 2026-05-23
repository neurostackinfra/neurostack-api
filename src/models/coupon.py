"""
NeuroStack - Coupon Model
Discount coupons managed by Master Admin for billing.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class Coupon(Base, TimestampMixin):
    """Discount coupon for billing checkout."""

    __tablename__ = "coupons"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
    )
    code: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True,
    )
    discount_percent: Mapped[int] = mapped_column(
        Integer, nullable=False,
    )
    max_uses: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    uses_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False,
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Coupon(code='{self.code}', discount={self.discount_percent}%)>"
