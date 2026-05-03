-- ============================================
-- NeuroStack API - Database Initialization
-- This script runs on first container start
-- ============================================

-- Create extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ============================================
-- Tenants Table
-- ============================================
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    api_key_hash VARCHAR(255) NOT NULL UNIQUE,
    is_active BOOLEAN DEFAULT TRUE,
    config JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tenants_api_key_hash ON tenants(api_key_hash);
CREATE INDEX IF NOT EXISTS idx_tenants_is_active ON tenants(is_active);

-- ============================================
-- Sessions Table
-- ============================================
CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    user_id VARCHAR(255),
    channel VARCHAR(50) DEFAULT 'unknown',
    cognitive_state JSONB DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT TRUE,
    message_count INTEGER DEFAULT 0,
    handoff_status VARCHAR(50),
    is_paused BOOLEAN NOT NULL DEFAULT FALSE,
    paused_until TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_sessions_tenant_id ON sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_is_active ON sessions(is_active);
CREATE INDEX IF NOT EXISTS idx_sessions_last_activity ON sessions(last_activity_at);

-- ============================================
-- Events Table (Append-only Event Store)
-- ============================================
CREATE TABLE IF NOT EXISTS events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    session_id UUID REFERENCES sessions(id),
    event_type VARCHAR(100) NOT NULL,
    event_data JSONB NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_events_tenant_id ON events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);

-- Composite index for common queries
CREATE INDEX IF NOT EXISTS idx_events_tenant_session ON events(tenant_id, session_id, created_at DESC);

-- ============================================
-- Policies Table
-- ============================================
CREATE TABLE IF NOT EXISTS policies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id),  -- NULL for global policies
    name VARCHAR(255) NOT NULL,
    description TEXT,
    policy_type VARCHAR(50) NOT NULL,  -- 'global', 'tenant', 'safety'
    rules JSONB NOT NULL DEFAULT '[]',
    priority INT DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_policies_tenant_id ON policies(tenant_id);
CREATE INDEX IF NOT EXISTS idx_policies_policy_type ON policies(policy_type);
CREATE INDEX IF NOT EXISTS idx_policies_is_active ON policies(is_active);

-- ============================================
-- Knowledge Documents Table
-- ============================================
CREATE TABLE IF NOT EXISTS knowledge_documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    title VARCHAR(500) NOT NULL,
    content TEXT NOT NULL,
    content_type VARCHAR(50) DEFAULT 'text',
    metadata JSONB DEFAULT '{}',
    vector_id VARCHAR(255),  -- Reference to Qdrant
    is_indexed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_knowledge_tenant_id ON knowledge_documents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_is_indexed ON knowledge_documents(is_indexed);

-- ============================================
-- Handoff Records Table
-- ============================================
CREATE TABLE IF NOT EXISTS handoff_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    session_id UUID NOT NULL REFERENCES sessions(id),
    reason VARCHAR(100) NOT NULL,
    summary TEXT,
    context JSONB DEFAULT '{}',
    webhook_sent BOOLEAN DEFAULT FALSE,
    webhook_response JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_handoff_tenant_id ON handoff_records(tenant_id);
CREATE INDEX IF NOT EXISTS idx_handoff_session_id ON handoff_records(session_id);
CREATE INDEX IF NOT EXISTS idx_handoff_created_at ON handoff_records(created_at);

-- ============================================
-- Functions
-- ============================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply trigger to tables with updated_at
CREATE TRIGGER update_tenants_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_policies_updated_at
    BEFORE UPDATE ON policies
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_knowledge_updated_at
    BEFORE UPDATE ON knowledge_documents
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- Seed Data (Development Only)
-- ============================================
-- Insert a default tenant for development
INSERT INTO tenants (id, name, api_key_hash, config)
VALUES (
    'a0000000-0000-0000-0000-000000000001',
    'Development Tenant',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/X.NfA1yQGfE0uL5Wy',  -- 'dev-api-key-12345'
    '{"description": "Default development tenant", "rate_limit": 100}'::jsonb
)
ON CONFLICT (api_key_hash) DO NOTHING;

-- Insert global safety policies
INSERT INTO policies (id, tenant_id, name, description, policy_type, rules, priority)
VALUES (
    'b0000000-0000-0000-0000-000000000001',
    NULL,
    'Global Safety Policy',
    'Global safety rules that apply to all tenants',
    'global',
    '[
        {"rule": "no_medical_diagnosis", "message": "Não fornecer diagnósticos médicos"},
        {"rule": "no_medication_advice", "message": "Não recomendar medicamentos"},
        {"rule": "no_result_promises", "message": "Não prometer resultados de tratamento"}
    ]'::jsonb,
    1000
)
ON CONFLICT DO NOTHING;

-- ============================================
-- Comments
-- ============================================
COMMENT ON TABLE tenants IS 'Tenants (builders or clinics) using NeuroStack';
COMMENT ON TABLE sessions IS 'User conversation sessions';
COMMENT ON TABLE events IS 'Append-only event store for all cognitive events';
COMMENT ON TABLE policies IS 'Policy rules (global and per-tenant)';
COMMENT ON TABLE knowledge_documents IS 'RAG knowledge base documents';
COMMENT ON TABLE handoff_records IS 'Human handoff records';

-- Done!
SELECT 'Database initialized successfully!' as status;

