"""Add users table

Revision ID: 004
Revises: 003
Create Date: 2026-04-25

Creates the users table with UUID primary key, tenant FK, email unique
constraint, role enum, and timestamp columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the userrole enum type via raw SQL to avoid double-create
    op.execute("DO $$ BEGIN CREATE TYPE userrole AS ENUM ('clinic_admin', 'super_admin', 'investor'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;")

    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "role",
            sa.VARCHAR(50),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_index("idx_users_tenant_id", "users", ["tenant_id"], unique=False)
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_index("ix_users_email", "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.drop_index("idx_users_tenant_id", table_name="users")
    op.drop_table("users")

    sa.Enum(name="userrole").drop(op.get_bind(), checkfirst=True)
