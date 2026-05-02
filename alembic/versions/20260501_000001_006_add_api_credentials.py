"""Add api_credentials JSONB column to clinic_configs

Revision ID: 006
Revises: 005
Create Date: 2026-05-01

Adds api_credentials JSONB column for storing WhatsApp provider
(Z-API, Evolution API) and LLM provider credentials.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clinic_configs",
        sa.Column("api_credentials", JSONB, nullable=True, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("clinic_configs", "api_credentials")
