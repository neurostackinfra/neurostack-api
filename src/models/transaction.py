"""
NeuroStack - Transaction Model
Records confirmed payments received via AbacatePay webhooks.
"""

from typing import TYPE_CHECKING, Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.billing import BillingPaymentEffect


class Transaction(Base, TimestampMixin):
    """Payment transaction logged from AbacatePay webhook events."""

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "(provider IS NULL AND provider_payment_id IS NULL) OR "
            "(provider IN ('abacatepay', 'asaas') AND provider_payment_id IS NOT NULL)",
            name="ck_transactions_provider_payment_pair",
        ),
        CheckConstraint(
            "(billing_payment_effect_id IS NULL AND provider IS NULL AND "
            "provider_payment_id IS NULL) OR "
            "(billing_payment_effect_id IS NOT NULL AND provider IS NOT NULL "
            "AND provider_payment_id IS NOT NULL)",
            name="ck_transactions_billing_effect_identity",
        ),
        ForeignKeyConstraint(
            [
                "builder_id",
                "billing_payment_effect_id",
                "provider",
                "provider_payment_id",
            ],
            [
                "billing_payment_effects.builder_id",
                "billing_payment_effects.id",
                "billing_payment_effects.provider",
                "billing_payment_effects.payment_id",
            ],
            ondelete="RESTRICT",
            name="fk_transactions_billing_effect_identity",
        ),
        UniqueConstraint(
            "provider",
            "provider_payment_id",
            name="uq_transactions_provider_payment",
        ),
        UniqueConstraint(
            "billing_payment_effect_id",
            name="uq_transactions_billing_payment_effect",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    builder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    builder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    method: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    cycle: Mapped[str] = mapped_column(String(10), nullable=False, default="monthly")
    plan: Mapped[str] = mapped_column(String(20), nullable=False, default="start")
    coupon_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    provider_payment_id: Mapped[Optional[str]] = mapped_column(
        String(160), nullable=True
    )
    billing_payment_effect_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)

    billing_payment_effect: Mapped[Optional["BillingPaymentEffect"]] = relationship(
        "BillingPaymentEffect", back_populates="transaction"
    )

    def __repr__(self) -> str:
        return f"<Transaction(id={self.id}, builder={self.builder_name}, amount={self.amount_cents})>"
