"""Add users.builder_id FK column

Revision ID: 008
Revises: 007
Create Date: 2026-05-02

Adds a nullable builder_id UUID FK column to the users table
referencing builders.id, for builder-role users.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 'builder' value to the userrole enum
    op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'builder'")

    op.add_column(
        "users",
        sa.Column(
            "builder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("builders.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_users_builder_id", "users", ["builder_id"])


def downgrade() -> None:
    op.drop_index("ix_users_builder_id", table_name="users")
    op.drop_column("users", "builder_id")
