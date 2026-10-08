"""Tenant-scoped media assets stored transactionally in PostgreSQL."""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.tenant import Tenant


class MediaAsset(Base, TimestampMixin):
    """Binary catalog entry owned by exactly one tenant."""

    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint(
            "length(payload) = size_bytes",
            name="ck_media_assets_payload_size",
        ),
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 50000000",
            name="ck_media_assets_size_range",
        ),
        CheckConstraint(
            "length(sha256) = 32",
            name="ck_media_assets_sha256_length",
        ),
        CheckConstraint(
            "(category = 'video' AND mime_type = 'video/mp4') OR "
            "(category = 'audio' AND mime_type = 'audio/mpeg') OR "
            "(category = 'pdf' AND mime_type = 'application/pdf') OR "
            "(category = 'image' AND mime_type IN ('image/jpeg', 'image/png'))",
            name="ck_media_assets_category_mime",
        ),
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 200",
            name="ck_media_assets_display_name_length",
        ),
        CheckConstraint(
            "length(original_filename) BETWEEN 1 AND 255",
            name="ck_media_assets_original_filename_length",
        ),
        CheckConstraint(
            "usage_trigger IS NULL OR length(usage_trigger) <= 500",
            name="ck_media_assets_usage_trigger_length",
        ),
        CheckConstraint(
            "display_name NOT LIKE '%\r%' AND display_name NOT LIKE '%\n%' AND "
            "original_filename NOT LIKE '%\r%' AND "
            "original_filename NOT LIKE '%\n%' AND "
            "(usage_trigger IS NULL OR (usage_trigger NOT LIKE '%\r%' AND "
            "usage_trigger NOT LIKE '%\n%'))",
            name="ck_media_assets_text_controls",
        ),
        Index(
            "ix_media_assets_tenant_created_id",
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    usage_trigger: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_suggest: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="media_assets",
    )
