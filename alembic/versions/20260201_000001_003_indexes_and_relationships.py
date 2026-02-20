"""Melhoria de modelagem: índices compostos e trigger clinic_configs

Revision ID: 003
Revises: 002
Create Date: 2026-02-01

Adiciona:
- Índices compostos para consultas comuns (tenant+channel, tenant+status+created_at, etc.)
- Trigger updated_at na tabela clinic_configs
- Índice (session_id, created_at DESC) para eventos por sessão ordenados
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ============================================
    # Sessions: listagem por tenant + canal e por tenant + ativas + atividade
    # ============================================
    op.create_index(
        "idx_sessions_tenant_channel",
        "sessions",
        ["tenant_id", "channel"],
        unique=False,
    )
    op.create_index(
        "idx_sessions_tenant_active_activity",
        "sessions",
        ["tenant_id", "is_active", "last_activity_at"],
        unique=False,
        postgresql_ops={"last_activity_at": "DESC"},
    )

    # ============================================
    # Events: por tenant + tipo + tempo; por sessão ordenado por created_at
    # ============================================
    op.create_index(
        "idx_events_tenant_type_created",
        "events",
        ["tenant_id", "event_type", "created_at"],
        unique=False,
        postgresql_ops={"created_at": "DESC"},
    )
    op.create_index(
        "idx_events_session_created",
        "events",
        ["session_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("session_id IS NOT NULL"),
        postgresql_ops={"created_at": "DESC"},
    )

    # ============================================
    # Policies: merge por tenant + ativa + prioridade
    # ============================================
    op.create_index(
        "idx_policies_tenant_active_priority",
        "policies",
        ["tenant_id", "is_active", "priority"],
        unique=False,
        postgresql_ops={"priority": "DESC"},
    )

    # ============================================
    # Knowledge: por tenant + is_indexed (pendentes de indexação)
    # ============================================
    op.create_index(
        "idx_knowledge_tenant_indexed",
        "knowledge_documents",
        ["tenant_id", "is_indexed"],
        unique=False,
    )
    op.create_index(
        "idx_knowledge_tenant_created",
        "knowledge_documents",
        ["tenant_id", "created_at"],
        unique=False,
        postgresql_ops={"created_at": "DESC"},
    )

    # ============================================
    # Handoff: fila por tenant + status; listagem por tenant + status + tempo
    # ============================================
    op.create_index(
        "idx_handoff_tenant_status",
        "handoff_records",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "idx_handoff_tenant_status_created",
        "handoff_records",
        ["tenant_id", "status", "created_at"],
        unique=False,
        postgresql_ops={"created_at": "DESC"},
    )

    # ============================================
    # Clinic_configs: trigger updated_at (alinhado às outras tabelas)
    # ============================================
    op.execute("""
        CREATE TRIGGER update_clinic_configs_updated_at
            BEFORE UPDATE ON clinic_configs
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS update_clinic_configs_updated_at ON clinic_configs")

    op.drop_index("idx_handoff_tenant_status_created", table_name="handoff_records")
    op.drop_index("idx_handoff_tenant_status", table_name="handoff_records")
    op.drop_index("idx_knowledge_tenant_created", table_name="knowledge_documents")
    op.drop_index("idx_knowledge_tenant_indexed", table_name="knowledge_documents")
    op.drop_index("idx_policies_tenant_active_priority", table_name="policies")
    op.drop_index("idx_events_session_created", table_name="events")
    op.drop_index("idx_events_tenant_type_created", table_name="events")
    op.drop_index("idx_sessions_tenant_active_activity", table_name="sessions")
    op.drop_index("idx_sessions_tenant_channel", table_name="sessions")
