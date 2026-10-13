"""bind capability schema evidence and rollout readiness to the roster

Revision ID: 031
Revises: 030
Create Date: 2026-10-12 00:00:08
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE billing_operation_receipts
                ADD COLUMN IF NOT EXISTS billing_fact_schema_digest VARCHAR(64) NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE billing_operation_receipts
                DROP CONSTRAINT IF EXISTS ck_billing_operation_receipts_fact_schema_digest,
                ADD CONSTRAINT ck_billing_operation_receipts_fact_schema_digest CHECK (
                    billing_fact_schema_digest IS NULL
                    OR billing_fact_schema_digest ~ '^[0-9a-f]{64}$'
                )
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE billing_rollout_readiness_receipts
                ADD COLUMN IF NOT EXISTS roster_revision BIGINT NOT NULL DEFAULT 0
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE billing_rollout_readiness_receipts
                ALTER COLUMN roster_revision DROP DEFAULT,
                DROP CONSTRAINT IF EXISTS ck_billing_rollout_readiness_roster_revision,
                ADD CONSTRAINT ck_billing_rollout_readiness_roster_revision
                    CHECK (roster_revision >= 0)
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE billing_rollout_readiness_receipts
                DROP CONSTRAINT IF EXISTS ck_billing_rollout_readiness_roster_revision,
                DROP COLUMN IF EXISTS roster_revision
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE billing_operation_receipts
                DROP CONSTRAINT IF EXISTS ck_billing_operation_receipts_fact_schema_digest,
                DROP COLUMN IF EXISTS billing_fact_schema_digest
            """
        )
    )
