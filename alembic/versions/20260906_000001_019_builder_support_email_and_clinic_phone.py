"""add builder support_email and tenant contact_phone

Revision ID: 019
Revises: 018
Create Date: 2026-09-06 00:00:01
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "builders",
        sa.Column(
            "support_email",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "contact_phone",
            sa.String(length=32),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "contact_phone")
    op.drop_column("builders", "support_email")
