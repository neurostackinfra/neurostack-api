"""add tenant-scoped media asset catalog

Revision ID: 022
Revises: 021
Create Date: 2026-10-07 00:00:01
"""

from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(length=32), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("usage_trigger", sa.Text(), nullable=True),
        sa.Column(
            "auto_suggest",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(payload) = size_bytes",
            name="ck_media_assets_payload_size",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 50000000",
            name="ck_media_assets_size_range",
        ),
        sa.CheckConstraint(
            "octet_length(sha256) = 32",
            name="ck_media_assets_sha256_length",
        ),
        sa.CheckConstraint(
            "(category = 'video' AND mime_type = 'video/mp4') OR "
            "(category = 'audio' AND mime_type = 'audio/mpeg') OR "
            "(category = 'pdf' AND mime_type = 'application/pdf') OR "
            "(category = 'image' AND mime_type IN ('image/jpeg', 'image/png'))",
            name="ck_media_assets_category_mime",
        ),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 200",
            name="ck_media_assets_display_name_length",
        ),
        sa.CheckConstraint(
            "char_length(original_filename) BETWEEN 1 AND 255",
            name="ck_media_assets_original_filename_length",
        ),
        sa.CheckConstraint(
            "usage_trigger IS NULL OR char_length(usage_trigger) <= 500",
            name="ck_media_assets_usage_trigger_length",
        ),
        sa.CheckConstraint(
            "position(chr(13) in display_name) = 0 AND "
            "position(chr(10) in display_name) = 0 AND "
            "position(chr(13) in original_filename) = 0 AND "
            "position(chr(10) in original_filename) = 0 AND "
            "(usage_trigger IS NULL OR ("
            "position(chr(13) in usage_trigger) = 0 AND "
            "position(chr(10) in usage_trigger) = 0))",
            name="ck_media_assets_text_controls",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_media_assets_tenant_created_id",
        "media_assets",
        ["tenant_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_assets_tenant_created_id",
        table_name="media_assets",
    )
    op.drop_table("media_assets")
