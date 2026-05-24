"""add bot_active to tenants

Revision ID: 012
Revises: 011
Create Date: 2026-05-24

Adds the `bot_active` column to the tenants table.
When False, the webhook skips the AI pipeline and sends an offline message
once per session — without affecting billing (is_active stays True).
"""

from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "bot_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "bot_active")
