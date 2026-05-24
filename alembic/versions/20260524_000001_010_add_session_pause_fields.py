"""add_session_pause_fields

Revision ID: 010
Revises: 8794bce6b4ed
Create Date: 2026-05-24 12:00:00

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '010'
down_revision: Union[str, None] = '8794bce6b4ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column('is_paused', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('sessions', sa.Column('paused_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('sessions', 'paused_until')
    op.drop_column('sessions', 'is_paused')
