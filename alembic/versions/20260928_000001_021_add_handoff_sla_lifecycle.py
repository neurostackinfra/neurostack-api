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

_SLA_CHECK = "ck_handoff_records_sla_target_seconds_range"
_VERSION_CHECK = "ck_handoff_records_lifecycle_version_nonnegative"


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
        _SLA_CHECK,
        "handoff_records",
        "sla_target_seconds IS NULL OR sla_target_seconds BETWEEN 60 AND 7200",
        postgresql_not_valid=True,
    )
    op.create_check_constraint(
        _VERSION_CHECK,
        "handoff_records",
        "lifecycle_version >= 0",
        postgresql_not_valid=True,
    )
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(f"ALTER TABLE handoff_records VALIDATE CONSTRAINT {_SLA_CHECK}")
        )
        op.execute(
            sa.text(f"ALTER TABLE handoff_records VALIDATE CONSTRAINT {_VERSION_CHECK}")
        )


def downgrade() -> None:
    op.drop_constraint(
        _VERSION_CHECK,
        "handoff_records",
        type_="check",
    )
    op.drop_constraint(
        _SLA_CHECK,
        "handoff_records",
        type_="check",
    )
    op.drop_column("handoff_records", "lifecycle_version")
    op.drop_column("handoff_records", "updated_at")
    op.drop_column("handoff_records", "sla_target_seconds")
