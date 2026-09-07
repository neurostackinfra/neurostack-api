"""create rlhf_ratings and rlhf_audit_reviews

Revision ID: 020
Revises: 019
Create Date: 2026-09-07 00:00:01
"""

from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rlhf_ratings",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column(
            "trace_event_id",
            UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "trace_tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("agent_response", sa.Text(), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False),
        sa.Column(
            "submitted_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column(
            "ai_analysis",
            JSONB(),
            nullable=True,
        ),
        sa.Column("ai_analysis_status", sa.Text(), nullable=True),
        sa.Column(
            "approved_for_training",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "ignored",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "accepted_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('whatsapp', 'web', 'unknown')",
            name="ck_rlhf_ratings_channel",
        ),
        sa.CheckConstraint(
            "rating BETWEEN 1 AND 5",
            name="ck_rlhf_ratings_rating_range",
        ),
    )
    op.create_index(
        "idx_rlhf_ratings_created_at_desc",
        "rlhf_ratings",
        [sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_rlhf_ratings_tenant_created_at_desc",
        "rlhf_ratings",
        ["tenant_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "rlhf_audit_reviews",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "rating_id",
            UUID(as_uuid=True),
            sa.ForeignKey("rlhf_ratings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("observacao", sa.Text(), nullable=True),
        sa.Column("revisado", sa.Boolean(), nullable=False),
        sa.Column(
            "revisado_por",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revisado_por_email", sa.Text(), nullable=False),
        sa.Column(
            "revisado_em",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_rlhf_audit_reviews_rating_revisado_em_id",
        "rlhf_audit_reviews",
        ["rating_id", sa.text("revisado_em DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_rlhf_audit_reviews_rating_revisado_em_id",
        table_name="rlhf_audit_reviews",
    )
    op.drop_table("rlhf_audit_reviews")

    op.drop_index(
        "idx_rlhf_ratings_tenant_created_at_desc",
        table_name="rlhf_ratings",
    )
    op.drop_index(
        "idx_rlhf_ratings_created_at_desc",
        table_name="rlhf_ratings",
    )
    op.drop_table("rlhf_ratings")
