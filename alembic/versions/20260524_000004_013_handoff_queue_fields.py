"""add handoff queue display fields

Revision ID: 013
Revises: 012
Create Date: 2026-05-24

Adds display columns to handoff_records for the queue UI:
- numero_whatsapp, intencao_detectada, estagio_atual, primeira_linha_mensagem, data_hora_saida
- Composite index (tenant_id, status) for queue queries
"""

from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "handoff_records",
        sa.Column("numero_whatsapp", sa.String(50), nullable=True),
    )
    op.add_column(
        "handoff_records",
        sa.Column("intencao_detectada", sa.String(100), nullable=True),
    )
    op.add_column(
        "handoff_records",
        sa.Column("estagio_atual", sa.String(50), nullable=True),
    )
    op.add_column(
        "handoff_records",
        sa.Column("primeira_linha_mensagem", sa.Text(), nullable=True),
    )
    op.add_column(
        "handoff_records",
        sa.Column(
            "data_hora_saida",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_handoff_records_tenant_status",
        "handoff_records",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_handoff_records_tenant_status", table_name="handoff_records")
    op.drop_column("handoff_records", "data_hora_saida")
    op.drop_column("handoff_records", "primeira_linha_mensagem")
    op.drop_column("handoff_records", "estagio_atual")
    op.drop_column("handoff_records", "intencao_detectada")
    op.drop_column("handoff_records", "numero_whatsapp")
