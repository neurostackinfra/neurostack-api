# Schema do banco de dados e índices

**Versão:** 1.0  
**Migração de índices:** 003

Este documento descreve as tabelas, relacionamentos e índices do NeuroStack (PostgreSQL).

---

## Diagrama de relacionamentos

```
tenants (1) ──┬── (N) sessions
              ├── (N) events
              ├── (N) policies          [tenant_id NULL = política global]
              ├── (N) knowledge_documents
              ├── (N) handoff_records
              └── (1) clinic_configs    [1:1, opcional]

sessions (1) ──┬── (N) events          [session_id opcional em events]
               └── (N) handoff_records
```

- **tenants:** builder/clínica; raiz da hierarquia.
- **sessions:** sessão de conversa; pertence a um tenant; tem eventos e handoffs.
- **events:** evento append-only; tenant_id obrigatório, session_id opcional.
- **policies:** política global (tenant_id NULL) ou por tenant.
- **knowledge_documents:** documento RAG por tenant.
- **handoff_records:** handoff por tenant e sessão.
- **clinic_configs:** configuração da clínica; uma linha por tenant (FK única).

---

## Tabelas e FKs

| Tabela | PK | FKs | Observação |
|--------|----|-----|------------|
| **tenants** | id (UUID) | — | config (JSONB) para rate_limit, webhook, features, branding |
| **clinic_configs** | id (UUID) | tenant_id → tenants.id (CASCADE, UNIQUE) | 1:1 com tenant |
| **sessions** | id (UUID) | tenant_id → tenants.id (CASCADE) | user_id, channel, cognitive_state |
| **events** | id (UUID) | tenant_id → tenants.id (CASCADE), session_id → sessions.id (CASCADE, NULL) | Append-only |
| **policies** | id (UUID) | tenant_id → tenants.id (CASCADE, NULL) | NULL = global |
| **knowledge_documents** | id (UUID) | tenant_id → tenants.id (CASCADE) | vector_id, is_indexed |
| **handoff_records** | id (UUID) | tenant_id → tenants.id (CASCADE), session_id → sessions.id (CASCADE) | status, urgency |

---

## Índices (revisão 003)

### Tenants
| Índice | Colunas | Uso |
|--------|---------|-----|
| idx_tenants_api_key_hash | (api_key_hash) | Autenticação |
| idx_tenants_slug | (slug) | Lookup por slug |
| idx_tenants_is_active | (is_active) | Listar ativos |

### Clinic_configs
| Índice | Colunas | Uso |
|--------|---------|-----|
| idx_clinic_configs_tenant_id | (tenant_id) UNIQUE | 1:1 por tenant |

### Sessions
| Índice | Colunas | Uso |
|--------|---------|-----|
| idx_sessions_tenant_id | (tenant_id) | Listar por tenant |
| idx_sessions_user_id | (user_id) | Buscar por usuário |
| idx_sessions_is_active | (is_active) | Filtrar ativas |
| idx_sessions_last_activity | (last_activity_at) | Ordenação |
| **idx_sessions_tenant_channel** | **(tenant_id, channel)** | Listar por tenant + canal |
| **idx_sessions_tenant_active_activity** | **(tenant_id, is_active, last_activity_at DESC)** | Listar ativas por tenant ordenadas por atividade |

### Events
| Índice | Colunas | Uso |
|--------|---------|-----|
| idx_events_tenant_id | (tenant_id) | Por tenant |
| idx_events_session_id | (session_id) | Por sessão |
| idx_events_event_type | (event_type) | Por tipo |
| idx_events_created_at | (created_at) | Ordenação |
| idx_events_tenant_session | (tenant_id, session_id, created_at DESC) | Eventos de sessão por tenant |
| **idx_events_tenant_type_created** | **(tenant_id, event_type, created_at DESC)** | Filtrar por tipo e tempo |
| **idx_events_session_created** | **(session_id, created_at DESC)** WHERE session_id IS NOT NULL | Eventos da sessão ordenados |

### Policies
| Índice | Colunas | Uso |
|--------|---------|-----|
| idx_policies_tenant_id | (tenant_id) | Por tenant |
| idx_policies_policy_type | (policy_type) | Por tipo |
| idx_policies_is_active | (is_active) | Ativas |
| **idx_policies_tenant_active_priority** | **(tenant_id, is_active, priority DESC)** | Merge de políticas (global + tenant) |

### Knowledge_documents
| Índice | Colunas | Uso |
|--------|---------|-----|
| idx_knowledge_tenant_id | (tenant_id) | Por tenant |
| idx_knowledge_is_indexed | (is_indexed) | Pendentes de indexação |
| **idx_knowledge_tenant_indexed** | **(tenant_id, is_indexed)** | Listar pendentes por tenant |
| **idx_knowledge_tenant_created** | **(tenant_id, created_at DESC)** | Listagem ordenada por tenant |

### Handoff_records
| Índice | Colunas | Uso |
|--------|---------|-----|
| idx_handoff_tenant_id | (tenant_id) | Por tenant |
| idx_handoff_session_id | (session_id) | Por sessão |
| idx_handoff_status | (status) | Por status |
| idx_handoff_created_at | (created_at) | Ordenação |
| **idx_handoff_tenant_status** | **(tenant_id, status)** | Fila por tenant e status |
| **idx_handoff_tenant_status_created** | **(tenant_id, status, created_at DESC)** | Listagem ordenada (ex.: pendentes) |

---

## Triggers

- **update_updated_at_column:** função que define `NEW.updated_at = CURRENT_TIMESTAMP`.
- **update_tenants_updated_at:** em `tenants`, BEFORE UPDATE.
- **update_policies_updated_at:** em `policies`, BEFORE UPDATE.
- **update_knowledge_updated_at:** em `knowledge_documents`, BEFORE UPDATE.
- **update_clinic_configs_updated_at:** em `clinic_configs`, BEFORE UPDATE (migração 003).

---

## Aplicar migrações

```bash
alembic upgrade head
```

Reverter apenas a revisão 003 (índices e trigger clinic_configs):

```bash
alembic downgrade 002
```
