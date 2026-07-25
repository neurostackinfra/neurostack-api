"""add partial unique index on api_credentials.meta_phone_number_id

Revision ID: 014
Revises: 013
Create Date: 2026-07-25

Adds a partial UNIQUE index on the JSONB expression
`(api_credentials->>'meta_phone_number_id')` in `clinic_configs`. The
`meta_phone_number_id` is the key used to resolve the correct tenant when
receiving Meta Cloud API (WhatsApp Business) webhooks — two clinics sharing
the same value would collapse into a cross-tenant data leak. Guarding the
constraint at the database level closes the race condition that a pure
application-level check would leave open.

The index is Postgres-specific (relies on the JSONB `->>` operator). When
the migration runs against a non-Postgres backend (e.g. the SQLite engine
used by the test suite), the index is skipped — the test tier still enforces
the invariant via application-level validation in `src/api/routes/config.py`.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "uq_clinic_configs_meta_phone_number_id"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (used in tests) has no JSONB operator; the app-level check
        # in patch_api_credentials preserves the invariant during tests.
        return
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
        ON clinic_configs ((api_credentials->>'meta_phone_number_id'))
        WHERE api_credentials->>'meta_phone_number_id' IS NOT NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
