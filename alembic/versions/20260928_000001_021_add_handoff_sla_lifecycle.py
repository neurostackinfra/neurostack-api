"""add handoff SLA snapshot and lifecycle metadata

Revision ID: 021
Revises: 020
Create Date: 2026-09-28 00:00:01
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "handoff_records",
        sa.Column("sla_target_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "handoff_records",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "handoff_records",
        sa.Column(
            "lifecycle_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_handoff_records_sla_target_seconds_range",
        "handoff_records",
        "sla_target_seconds IS NULL OR sla_target_seconds BETWEEN 60 AND 7200",
    )
    op.create_check_constraint(
        "ck_handoff_records_lifecycle_version_nonnegative",
        "handoff_records",
        "lifecycle_version >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_handoff_records_lifecycle_version_nonnegative",
        "handoff_records",
        type_="check",
    )
    op.drop_constraint(
        "ck_handoff_records_sla_target_seconds_range",
        "handoff_records",
        type_="check",
    )
    op.drop_column("handoff_records", "lifecycle_version")
    op.drop_column("handoff_records", "updated_at")
    op.drop_column("handoff_records", "sla_target_seconds")
