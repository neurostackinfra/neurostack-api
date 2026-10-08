"""add audit_extended_access to tenants

Revision ID: 022
Revises: 021
Create Date: 2026-10-07 00:00:01

Adds the per-clinic audit add-on flag. When true, trace and handoff CSV
exports skip the 6-calendar-month lookback; the 60-day-per-file cap still
applies. Existing clinics default to false.
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "audit_extended_access",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "audit_extended_access")
