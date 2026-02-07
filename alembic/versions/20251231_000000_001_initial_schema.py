"""Initial schema - NeuroStack MVP

Revision ID: 001
Revises: 
Create Date: 2025-12-31 00:00:00.000000

Creates the initial database schema for NeuroStack:
- tenants: Builders and clinics using the platform
- sessions: Conversation sessions
- events: Append-only event store
- policies: Behavior rules (global and per-tenant)
- knowledge_documents: RAG knowledge base
- handoff_records: Human handoff tracking
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create initial database schema."""
    
    # Create extensions
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')
    
    # ============================================
    # Tenants Table
    # ============================================
    op.create_table(
        'tenants',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, 
                  server_default=sa.text('uuid_generate_v4()')),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('slug', sa.String(100), nullable=True, unique=True),
        sa.Column('api_key_hash', sa.String(255), nullable=False, unique=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False, 
                  server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('idx_tenants_api_key_hash', 'tenants', ['api_key_hash'])
    op.create_index('idx_tenants_is_active', 'tenants', ['is_active'])
    op.create_index('idx_tenants_slug', 'tenants', ['slug'])
    
    # ============================================
    # Sessions Table
    # ============================================
    op.create_table(
        'sessions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('uuid_generate_v4()')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), 
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.String(255), nullable=True),
        sa.Column('channel', sa.String(50), nullable=False, default='unknown'),
        sa.Column('cognitive_state', postgresql.JSONB(astext_type=sa.Text()), 
                  nullable=False, server_default='{}'),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), 
                  nullable=False, server_default='{}'),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.Column('message_count', sa.Integer(), nullable=False, default=0),
        sa.Column('handoff_status', sa.String(50), nullable=True),
    )
    op.create_index('idx_sessions_tenant_id', 'sessions', ['tenant_id'])
    op.create_index('idx_sessions_user_id', 'sessions', ['user_id'])
    op.create_index('idx_sessions_is_active', 'sessions', ['is_active'])
    op.create_index('idx_sessions_last_activity', 'sessions', ['last_activity_at'])
    
    # ============================================
    # Events Table (Append-only Event Store)
    # ============================================
    op.create_table(
        'events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('uuid_generate_v4()')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('session_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=True),
        sa.Column('event_type', sa.String(100), nullable=False),
        sa.Column('event_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('idx_events_tenant_id', 'events', ['tenant_id'])
    op.create_index('idx_events_session_id', 'events', ['session_id'])
    op.create_index('idx_events_event_type', 'events', ['event_type'])
    op.create_index('idx_events_created_at', 'events', ['created_at'])
    op.create_index('idx_events_tenant_session', 'events', 
                    ['tenant_id', 'session_id', sa.text('created_at DESC')])
    
    # ============================================
    # Policies Table
    # ============================================
    op.create_table(
        'policies',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('uuid_generate_v4()')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('policy_type', sa.String(50), nullable=False),
        sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default='[]'),
        sa.Column('priority', sa.Integer(), nullable=False, default=0),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.Column('version', sa.Integer(), nullable=False, default=1),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('idx_policies_tenant_id', 'policies', ['tenant_id'])
    op.create_index('idx_policies_policy_type', 'policies', ['policy_type'])
    op.create_index('idx_policies_is_active', 'policies', ['is_active'])
    
    # ============================================
    # Knowledge Documents Table
    # ============================================
    op.create_table(
        'knowledge_documents',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('uuid_generate_v4()')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('content_type', sa.String(50), nullable=False, default='text'),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default='{}'),
        sa.Column('vector_id', sa.String(255), nullable=True),
        sa.Column('is_indexed', sa.Boolean(), nullable=False, default=False),
        sa.Column('chunk_count', sa.Integer(), nullable=False, default=0),
        sa.Column('index_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('idx_knowledge_tenant_id', 'knowledge_documents', ['tenant_id'])
    op.create_index('idx_knowledge_is_indexed', 'knowledge_documents', ['is_indexed'])
    
    # ============================================
    # Handoff Records Table
    # ============================================
    op.create_table(
        'handoff_records',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('uuid_generate_v4()')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('session_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('reason', sa.String(100), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('context', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default='{}'),
        sa.Column('urgency', sa.String(20), nullable=False, default='normal'),
        sa.Column('status', sa.String(20), nullable=False, default='pending'),
        sa.Column('webhook_sent', sa.Boolean(), nullable=False, default=False),
        sa.Column('webhook_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('assigned_to', sa.String(255), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('idx_handoff_tenant_id', 'handoff_records', ['tenant_id'])
    op.create_index('idx_handoff_session_id', 'handoff_records', ['session_id'])
    op.create_index('idx_handoff_status', 'handoff_records', ['status'])
    op.create_index('idx_handoff_created_at', 'handoff_records', ['created_at'])
    
    # ============================================
    # Triggers for updated_at
    # ============================================
    op.execute('''
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    ''')
    
    op.execute('''
        CREATE TRIGGER update_tenants_updated_at
            BEFORE UPDATE ON tenants
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    ''')
    
    op.execute('''
        CREATE TRIGGER update_policies_updated_at
            BEFORE UPDATE ON policies
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    ''')
    
    op.execute('''
        CREATE TRIGGER update_knowledge_updated_at
            BEFORE UPDATE ON knowledge_documents
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    ''')


def downgrade() -> None:
    """Drop all tables and extensions."""
    
    # Drop triggers first
    op.execute('DROP TRIGGER IF EXISTS update_knowledge_updated_at ON knowledge_documents')
    op.execute('DROP TRIGGER IF EXISTS update_policies_updated_at ON policies')
    op.execute('DROP TRIGGER IF EXISTS update_tenants_updated_at ON tenants')
    op.execute('DROP FUNCTION IF EXISTS update_updated_at_column')
    
    # Drop tables in reverse order (respecting foreign keys)
    op.drop_table('handoff_records')
    op.drop_table('knowledge_documents')
    op.drop_table('policies')
    op.drop_table('events')
    op.drop_table('sessions')
    op.drop_table('tenants')
    
    # Drop extensions
    op.execute('DROP EXTENSION IF EXISTS "pg_trgm"')
    op.execute('DROP EXTENSION IF EXISTS "uuid-ossp"')

