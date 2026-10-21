# NeuroStack - Data Models

Este documento descreve os modelos de dados utilizados pelo NeuroStack.

## Visão Geral

O NeuroStack utiliza uma arquitetura de dados com dois componentes principais:

1. **OLTP (Redis)**: Estado operacional em tempo real (sessões ativas, cache)
2. **Event Store (PostgreSQL)**: Armazenamento persistente e append-only de eventos

## Diagrama de Relacionamentos

```
┌──────────────┐
│    Tenant    │
│   (builder/  │
│    clinic)   │
└──────┬───────┘
       │
       │ 1:N
       ▼
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  ┌─────────────┐   1:N    ┌─────────────┐   1:N    ┌──────┐│
│  │   Session   │◄─────────│   Events    │          │Policy││
│  │(conversation│          │(event store)│          │(rules)│
│  │   state)    │          └─────────────┘          └──────┘│
│  └──────┬──────┘                                           │
│         │                                                   │
│         │ 1:N                                               │
│         ▼                                                   │
│  ┌─────────────┐                     ┌──────────────────┐  │
│  │  Handoff    │                     │ KnowledgeDocument│  │
│  │  Records    │                     │    (RAG base)    │  │
│  └─────────────┘                     └──────────────────┘  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## Modelos

### 1. Tenant

Representa um builder ou clínica que utiliza o NeuroStack.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID | Identificador único |
| `name` | String(255) | Nome do tenant |
| `slug` | String(100) | Identificador URL-friendly (único) |
| `api_key_hash` | String(255) | Hash da API Key para autenticação |
| `is_active` | Boolean | Se o tenant está ativo |
| `config` | JSONB | Configurações (rate limit, features, branding) |
| `created_at` | Timestamp | Data de criação |
| `updated_at` | Timestamp | Data de última atualização |

**Configurações (`config`):**

```json
{
  "description": "Clínica de Dermatologia",
  "rate_limit": 100,
  "session_ttl_seconds": 86400,
  "max_sessions_per_user": 10,
  "webhook_url": "https://api.clinic.com/webhooks",
  "features": {
    "rag_enabled": true,
    "handoff_enabled": true,
    "safety_strict_mode": false
  },
  "branding": {
    "assistant_name": "Ana",
    "tone": "professional"
  }
}
```

### 2. Session

Representa uma sessão de conversação entre usuário e agente.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID | Identificador único |
| `tenant_id` | UUID (FK) | Tenant proprietário |
| `user_id` | String(255) | Identificador externo do usuário |
| `channel` | String(50) | Canal de comunicação (whatsapp, web) |
| `cognitive_state` | JSONB | Estado cognitivo da conversa |
| `metadata` | JSONB | Metadados adicionais |
| `started_at` | Timestamp | Início da sessão |
| `last_activity_at` | Timestamp | Última atividade |
| `ended_at` | Timestamp | Fim da sessão (se encerrada) |
| `is_active` | Boolean | Se a sessão está ativa |
| `message_count` | Integer | Total de mensagens |
| `handoff_status` | String(50) | Status de handoff se aplicável |

**Estado Cognitivo (`cognitive_state`):**

```json
{
  "funnel_stage": "consideration",
  "intent_history": ["greeting", "scheduling_request"],
  "slots": {
    "patient_name": "João Silva",
    "preferred_time": "afternoon"
  },
  "flags": {
    "first_visit": true,
    "price_sensitive": true
  },
  "turn_count": 5,
  "safety_triggers": 0,
  "last_intent": "scheduling_request",
  "confidence_avg": 0.87
}
```

### 3. Event

Evento no Event Store (append-only).

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID | Identificador único |
| `tenant_id` | UUID (FK) | Tenant proprietário |
| `session_id` | UUID (FK) | Sessão relacionada (opcional) |
| `event_type` | String(100) | Tipo do evento |
| `event_data` | JSONB | Dados do evento |
| `metadata` | JSONB | Metadados (timing, source) |
| `created_at` | Timestamp | Data do evento (imutável) |

**Tipos de Eventos:**

| Tipo | Descrição |
|------|-----------|
| `message.in` | Mensagem recebida do usuário |
| `message.out` | Mensagem enviada ao usuário |
| `session.start` | Nova sessão criada |
| `session.end` | Sessão encerrada |
| `srm.decision` | Decisão do SRM |
| `state.patch` | Atualização de estado |
| `funnel.transition` | Mudança de estágio do funil |
| `safety.trigger` | Safety concern detectado |
| `safety.block` | Resposta bloqueada por segurança |
| `handoff.request` | Handoff solicitado |
| `handoff.complete` | Handoff concluído |
| `rag.query` | Consulta RAG executada |
| `policy.applied` | Políticas aplicadas |

### 4. Policy

Regras de comportamento para o agente.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID | Identificador único |
| `tenant_id` | UUID (FK) | Tenant (NULL para global) |
| `name` | String(255) | Nome da política |
| `description` | Text | Descrição detalhada |
| `policy_type` | String(50) | Tipo: global, tenant, safety |
| `rules` | JSONB | Array de regras |
| `priority` | Integer | Prioridade (maior = primeiro) |
| `is_active` | Boolean | Se está ativa |
| `version` | Integer | Versão da política |
| `created_at` | Timestamp | Data de criação |
| `updated_at` | Timestamp | Data de atualização |

**Estrutura de Regra:**

```json
{
  "rule_id": "no_medical_diagnosis",
  "rule": "no_medical_diagnosis",
  "message": "Não fornecer diagnósticos médicos",
  "description": "O assistente não deve diagnosticar condições",
  "severity": "critical",
  "action": "block",
  "patterns": ["diagnóstico", "você tem", "isso parece ser"],
  "metadata": {"category": "clinical", "compliance": "CFM"}
}
```

**Severidades:** `info`, `warning`, `error`, `critical`

**Ações:** `log`, `warn`, `flag`, `block`, `handoff`

### 5. KnowledgeDocument

Documentos da base de conhecimento RAG.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID | Identificador único |
| `tenant_id` | UUID (FK) | Tenant proprietário |
| `title` | String(500) | Título do documento |
| `content` | Text | Conteúdo completo |
| `content_type` | String(50) | Tipo: text, html, markdown |
| `metadata` | JSONB | Metadados (tags, categoria) |
| `vector_id` | String(255) | ID no Qdrant |
| `is_indexed` | Boolean | Se foi indexado |
| `chunk_count` | Integer | Número de chunks |
| `index_error` | Text | Erro de indexação se houver |
| `created_at` | Timestamp | Data de criação |
| `updated_at` | Timestamp | Data de atualização |

### 6. HandoffRecord

Registro de handoffs humanos.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID | Identificador único |
| `tenant_id` | UUID (FK) | Tenant proprietário |
| `session_id` | UUID (FK) | Sessão do handoff |
| `reason` | String(100) | Motivo do handoff |
| `summary` | Text | Resumo da conversa |
| `context` | JSONB | Contexto para atendente |
| `urgency` | String(20) | low, normal, high, critical |
| `status` | String(20) | pending, accepted, in_progress, completed |
| `webhook_sent` | Boolean | Se webhook foi enviado |
| `webhook_response` | JSONB | Resposta do webhook |
| `assigned_to` | String(255) | Atendente designado |
| `resolved_at` | Timestamp | Data de resolução |
| `resolution_notes` | Text | Notas de resolução |
| `created_at` | Timestamp | Data de criação |

## Índices

### Tenants
- `idx_tenants_api_key_hash` - Autenticação rápida
- `idx_tenants_is_active` - Filtro de tenants ativos
- `idx_tenants_slug` - Busca por slug

### Sessions
- `idx_sessions_tenant_id` - Sessões por tenant
- `idx_sessions_user_id` - Sessões por usuário
- `idx_sessions_is_active` - Sessões ativas
- `idx_sessions_last_activity` - Ordenação por atividade

### Events
- `idx_events_tenant_id` - Eventos por tenant
- `idx_events_session_id` - Eventos por sessão
- `idx_events_event_type` - Eventos por tipo
- `idx_events_created_at` - Ordenação temporal
- `idx_events_tenant_session` - Composto para queries comuns

### Policies
- `idx_policies_tenant_id` - Políticas por tenant
- `idx_policies_policy_type` - Políticas por tipo
- `idx_policies_is_active` - Políticas ativas

### Knowledge Documents
- `idx_knowledge_tenant_id` - Documentos por tenant
- `idx_knowledge_is_indexed` - Documentos indexados

### Handoff Records
- `idx_handoff_tenant_id` - Handoffs por tenant
- `idx_handoff_session_id` - Handoffs por sessão
- `idx_handoff_status` - Handoffs por status
- `idx_handoff_created_at` - Ordenação temporal

## Migrations

As migrations são gerenciadas pelo Alembic. Ver `alembic/README.md` para comandos.

```bash
# Aplicar migrations: a revisão aprovada para o ambiente
# (`head` só num banco local de desenvolvimento, via `make db-migrate`)
alembic upgrade <revision>
make db-migrate  # banco local de desenvolvimento

# Reverter última migration
alembic downgrade -1

# Gerar nova migration
alembic revision --autogenerate -m "Descrição"
```

## Uso nos Módulos

### Importando Modelos

```python
from src.models import (
    Tenant,
    Session,
    Event,
    Policy,
    KnowledgeDocument,
    HandoffRecord,
)
```

### Criando Registros

```python
from src.models import Tenant, TenantConfig

# Criar tenant com config
config = TenantConfig(
    description="Minha Clínica",
    rate_limit=100,
)

tenant = Tenant(
    name="Clínica Exemplo",
    slug="clinica-exemplo",
    api_key_hash=hashed_key,
)
tenant.set_config(config)
```

### Queries

```python
from sqlalchemy import select
from src.models import Session

# Buscar sessões ativas de um tenant
stmt = select(Session).where(
    Session.tenant_id == tenant_id,
    Session.is_active == True
)
sessions = await db.execute(stmt)
```

---

*Documentação gerada para NeuroStack MVP - Dezembro 2025*

