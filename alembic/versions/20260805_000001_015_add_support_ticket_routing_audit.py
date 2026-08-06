"""add support_tickets routing audit columns

Revision ID: 015
Revises: 014
Create Date: 2026-08-05 00:00:01

Adds two nullable columns to `support_tickets` so the system can persist,
immutably, which email destination was chosen at send time:

- `routed_to` (String(20)): high-level routing bucket (e.g. builder/central).
- `routed_to_builder_id` (UUID): when routed to a builder, the builder's id.

No FK is declared — same convention as `tenant_id`/`user_id` in this table.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "support_tickets",
        sa.Column("routed_to", sa.String(20), nullable=True),
    )
    op.add_column(
        "support_tickets",
        sa.Column("routed_to_builder_id", UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("support_tickets", "routed_to_builder_id")
    op.drop_column("support_tickets", "routed_to")
