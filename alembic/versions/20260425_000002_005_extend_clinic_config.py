"""Extend clinic_configs with JSONB columns and add events composite index

Revision ID: 005
Revises: 004
Create Date: 2026-04-25

Adds six new JSONB columns to clinic_configs:
  - identity, operations, business_rules, lgpd (default '{}')
  - professionals, services (default '[]')

Adds composite index on events (tenant_id, event_type, created_at DESC)
for traces query performance. Note: this index was already created in
migration 003; we ensure it exists via IF NOT EXISTS.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ============================================
    # clinic_configs: add six new JSONB columns
    # ============================================
    op.add_column("clinic_configs", sa.Column("identity", JSONB, nullable=True, server_default="{}"))
    op.add_column("clinic_configs", sa.Column("operations", JSONB, nullable=True, server_default="{}"))
    op.add_column("clinic_configs", sa.Column("business_rules", JSONB, nullable=True, server_default="{}"))
    op.add_column("clinic_configs", sa.Column("lgpd", JSONB, nullable=True, server_default="{}"))
    op.add_column("clinic_configs", sa.Column("professionals", JSONB, nullable=True, server_default="[]"))
    op.add_column("clinic_configs", sa.Column("services", JSONB, nullable=True, server_default="[]"))

    # ============================================
    # events: composite index for traces query performance
    # Already created in migration 003, so use IF NOT EXISTS
    # ============================================
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_tenant_type_created "
        "ON events (tenant_id, event_type, created_at DESC)"
    )


def downgrade() -> None:
    # Note: we do NOT drop idx_events_tenant_type_created here because
    # it was originally created in migration 003. Dropping it would leave
    # 003's upgrade orphaned. Migration 003's downgrade handles its removal.

    op.drop_column("clinic_configs", "services")
    op.drop_column("clinic_configs", "professionals")
    op.drop_column("clinic_configs", "lgpd")
    op.drop_column("clinic_configs", "business_rules")
    op.drop_column("clinic_configs", "operations")
    op.drop_column("clinic_configs", "identity")
