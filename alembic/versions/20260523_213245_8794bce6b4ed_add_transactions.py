"""add_transactions

Revision ID: 8794bce6b4ed
Revises: 009
Create Date: 2026-05-23 21:32:45.078703

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8794bce6b4ed'
down_revision: Union[str, None] = '009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create transactions table to log confirmed payments from AbacatePay."""
    op.create_table(
        'transactions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('builder_id', sa.UUID(), nullable=False),
        sa.Column('builder_name', sa.String(length=255), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('method', sa.String(length=20), nullable=False),
        sa.Column('cycle', sa.String(length=10), nullable=False),
        sa.Column('plan', sa.String(length=20), nullable=False),
        sa.Column('coupon_code', sa.String(length=50), nullable=True),
        sa.Column('external_id', sa.String(length=100), nullable=True),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_transactions_builder_id', 'transactions', ['builder_id'], unique=False)


def downgrade() -> None:
    """Drop transactions table."""
    op.drop_index('ix_transactions_builder_id', table_name='transactions')
    op.drop_table('transactions')
