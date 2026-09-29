"""
NeuroStack - Handoff Record Model
Tracks human handoff events and their resolution.
"""

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.session import Session
    from src.models.tenant import Tenant


class HandoffStatus(str, Enum):
    """Status of a handoff request."""

    PENDING = "pending"  # Waiting for human agent
    ACCEPTED = "accepted"  # Human agent accepted
    IN_PROGRESS = "in_progress"  # Being handled by human
    COMPLETED = "completed"  # Successfully completed
    CANCELLED = "cancelled"  # Cancelled by user or system
    TIMEOUT = "timeout"  # Timed out waiting for agent


class HandoffUrgency(str, Enum):
    """Urgency levels for handoff."""

    LOW = "low"  # Can wait
    NORMAL = "normal"  # Standard priority
    HIGH = "high"  # Should be handled soon
    CRITICAL = "critical"  # Immediate attention required


class HandoffRecord(Base):
    """
    Handoff record model for tracking human agent handoffs.

    When the AI agent determines that human intervention is needed,
    a handoff record is created. This tracks:
    - Why the handoff was requested
    - Summary of the conversation
    - Status of the handoff
    - Webhook notifications sent

    Attributes:
        id: Unique handoff identifier (UUID)
        tenant_id: Tenant where handoff occurred
        session_id: Session that triggered the handoff
        reason: Why the handoff was requested
        summary: AI-generated summary of conversation
        context: Additional context for human agent
        urgency: Urgency level
        status: Current handoff status
        webhook_sent: Whether webhook was dispatched
        webhook_response: Response from webhook
        assigned_to: Human agent assigned (if applicable)
        resolved_at: When handoff was resolved
        resolution_notes: Notes from resolution
        created_at: Handoff creation timestamp

    Relationships:
        tenant: The tenant that owns this handoff
        session: The session that triggered this handoff
    """

    __tablename__ = "handoff_records"
    __table_args__ = (
        CheckConstraint(
            "sla_target_seconds IS NULL OR " "sla_target_seconds BETWEEN 60 AND 7200",
            name="ck_handoff_records_sla_target_seconds_range",
        ),
        CheckConstraint(
            "lifecycle_version >= 0",
            name="ck_handoff_records_lifecycle_version_nonnegative",
        ),
    )

    # Primary Key
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Foreign Keys
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Core Fields
    reason: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Context (JSONB)
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
    )

    # Status
    urgency: Mapped[str] = mapped_column(
        String(20),
        default=HandoffUrgency.NORMAL.value,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default=HandoffStatus.PENDING.value,
        nullable=False,
        index=True,
    )

    # Webhook
    webhook_sent: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    webhook_response: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
    )

    # Assignment
    assigned_to: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    # Resolution
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    resolution_notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Queue display fields
    numero_whatsapp: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )
    intencao_detectada: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )
    estagio_atual: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )
    primeira_linha_mensagem: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    data_hora_saida: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # SLA snapshot and optimistic lifecycle coordination
    sla_target_seconds: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lifecycle_version: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="handoff_records",
    )
    session: Mapped["Session"] = relationship(
        "Session",
        back_populates="handoff_records",
    )

    @property
    def is_pending(self) -> bool:
        """Check if handoff is still pending."""
        return self.status == HandoffStatus.PENDING.value

    @property
    def is_active(self) -> bool:
        """Check if handoff is actively being handled."""
        return self.status in (
            HandoffStatus.PENDING.value,
            HandoffStatus.ACCEPTED.value,
            HandoffStatus.IN_PROGRESS.value,
        )

    @property
    def is_resolved(self) -> bool:
        """Check if handoff has been resolved."""
        return self.status in (
            HandoffStatus.COMPLETED.value,
            HandoffStatus.CANCELLED.value,
            HandoffStatus.TIMEOUT.value,
        )

    @property
    def key_info(self) -> dict[str, Any]:
        """Get key information from context."""
        return self.context.get("key_info", {}) if self.context else {}

    def __repr__(self) -> str:
        return f"<HandoffRecord(id={self.id}, session={self.session_id}, status='{self.status}')>"
