"""Add builders table + tenants.builder_id + tenants.plan_type

Revision ID: 007
Revises: 006
Create Date: 2026-05-02

Creates builders table for builder management and adds builder_id FK
and plan_type column to tenants table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create builders table
    op.create_table(
        "builders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Add builder_id FK to tenants
    op.add_column(
        "tenants",
        sa.Column("builder_id", UUID(as_uuid=True), sa.ForeignKey("builders.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_tenants_builder_id", "tenants", ["builder_id"])

    # Add plan_type to tenants
    op.add_column(
        "tenants",
        sa.Column("plan_type", sa.String(20), server_default="pilot", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tenants", "plan_type")
    op.drop_index("ix_tenants_builder_id", table_name="tenants")
    op.drop_column("tenants", "builder_id")
    op.drop_table("builders")
