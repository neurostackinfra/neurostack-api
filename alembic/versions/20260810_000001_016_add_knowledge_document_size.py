"""add knowledge document byte size for usage metering

Revision ID: 016
Revises: 015
Create Date: 2026-08-10 00:00:01
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "size_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute("UPDATE knowledge_documents SET size_bytes = octet_length(content)")
    op.create_check_constraint(
        "ck_knowledge_documents_size_bytes_nonnegative",
        "knowledge_documents",
        "size_bytes >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_documents_size_bytes_nonnegative",
        "knowledge_documents",
        type_="check",
    )
    op.drop_column("knowledge_documents", "size_bytes")
