"""ORM mirrors for Group 4b's additive copy registry and work queues."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class CreditObjectCopy(Base):
    __tablename__ = "credit_object_copies"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('media', 'knowledge')",
            name="ck_credit_object_copies_source",
        ),
        CheckConstraint(
            "state IN ('ready', 'deleted', 'held', 'failed')",
            name="ck_credit_object_copies_state",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_credit_object_copies_sha"
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "source_kind",
            "source_id",
            "content_sha256",
            name="uq_credit_object_copies_source",
        ),
        UniqueConstraint(
            "object_key", "version_id", name="uq_credit_object_copies_version"
        ),
        Index(
            "ix_credit_object_copies_tenant_source",
            "tenant_id",
            "source_kind",
            "source_id",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    builder_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    version_id: Mapped[str] = mapped_column(String(512), nullable=False)
    etag: Mapped[str] = mapped_column(String(512), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ready", server_default="ready"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditObjectCopyJob(Base):
    __tablename__ = "credit_object_copy_jobs"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('media', 'knowledge')",
            name="ck_credit_object_copy_jobs_source",
        ),
        CheckConstraint(
            "operation IN ('copy', 'gc')", name="ck_credit_object_copy_jobs_operation"
        ),
        CheckConstraint(
            "state IN ('pending', 'running', 'completed', 'held', 'failed')",
            name="ck_credit_object_copy_jobs_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_credit_object_copy_jobs_attempts"),
        UniqueConstraint(
            "source_kind",
            "source_id",
            "operation",
            "state",
            name="uq_credit_object_copy_jobs_active",
        ),
        Index("ix_credit_object_copy_jobs_claim", "state", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    copy_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credit_object_copies.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditQdrantJob(Base):
    __tablename__ = "credit_qdrant_jobs"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('media', 'knowledge')", name="ck_credit_qdrant_jobs_source"
        ),
        CheckConstraint(
            "operation IN ('rebuild', 'delete')", name="ck_credit_qdrant_jobs_operation"
        ),
        CheckConstraint(
            "state IN ('pending', 'running', 'completed', 'failed')",
            name="ck_credit_qdrant_jobs_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_credit_qdrant_jobs_attempts"),
        UniqueConstraint(
            "source_kind",
            "source_id",
            "operation",
            "state",
            name="uq_credit_qdrant_jobs_active",
        ),
        Index("ix_credit_qdrant_jobs_claim", "state", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["CreditObjectCopy", "CreditObjectCopyJob", "CreditQdrantJob"]
