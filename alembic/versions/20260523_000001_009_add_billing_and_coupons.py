"""Add billing fields to builders + create coupons table

Revision ID: 009
Revises: 008
Create Date: 2026-05-23

Adds subscription_plan, billing_cycle, subscription_status to builders.
Creates coupons table for discount coupon management.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Builder billing columns
    op.add_column(
        "builders",
        sa.Column("subscription_plan", sa.String(20), nullable=True),
    )
    op.add_column(
        "builders",
        sa.Column(
            "billing_cycle",
            sa.String(10),
            nullable=False,
            server_default="monthly",
        ),
    )
    op.add_column(
        "builders",
        sa.Column(
            "subscription_status",
            sa.String(20),
            nullable=False,
            server_default="trial",
        ),
    )

    # Coupons table
    op.create_table(
        "coupons",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(50), unique=True, nullable=False, index=True),
        sa.Column("discount_percent", sa.Integer, nullable=False),
        sa.Column("max_uses", sa.Integer, nullable=True),
        sa.Column("uses_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default="true",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("coupons")
    op.drop_column("builders", "subscription_status")
    op.drop_column("builders", "billing_cycle")
    op.drop_column("builders", "subscription_plan")
