# NeuroStack API - Contratos de API

**Versão:** 0.3.0  
**Data:** Fevereiro 2026  
**Alinhamento:** Documentação Técnica MVP v0.5  
**Última Atualização:** Eventos CRM, Decision Trace, Handoff setor/SLA, ClinicConfig, UserState

---

## Índice

1. [Visão Geral](#visão-geral)
2. [Autenticação](#autenticação)
3. [Rate Limiting](#rate-limiting)
4. [Códigos de Erro](#códigos-de-erro)
5. [Endpoints](#endpoints)
   - [Events](#events)
   - [Sessions](#sessions)
   - [Policies](#policies)
   - [Knowledge](#knowledge)
   - [Health](#health)
6. [Schemas](#schemas)
7. [Exemplos de Integração](#exemplos-de-integração)

---

## Visão Geral

A API NeuroStack fornece infraestrutura cognitiva para agentes de IA em healthcare. A API é RESTful e retorna respostas em JSON.

### Base URL

```
Production: https://api.neurostack.ai/v1
Staging:    https://staging-api.neurostack.ai/v1
Local:      http://localhost:8000/v1
```

### Headers Obrigatórios

| Header | Descrição |
|--------|-----------|
| `Content-Type` | `application/json` |
| `X-API-Key` | Sua chave de API |

---

## Autenticação

A API usa autenticação via API Key. Inclua sua chave no header `X-API-Key`:

```bash
curl -H "X-API-Key: your-api-key" https://api.neurostack.ai/v1/health
```

### Obtendo uma API Key

1. Entre em contato com a equipe NeuroStack
2. Forneça informações do seu tenant (clínica/builder)
3. Receba sua API Key e tenant_id

### Segurança

- Nunca exponha sua API Key em código client-side
- Rotacione chaves regularmente
- Use variáveis de ambiente para armazenar chaves

---

## Rate Limiting

| Limite | Valor |
|--------|-------|
| Requisições por minuto | 60 (configurável por tenant) |
| Burst | 10 requisições |

### Headers de Rate Limit

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 45
X-RateLimit-Reset: 1704067200
```

### Resposta quando excedido (429)

```json
{
  "error": "rate_limited",
  "message": "Rate limit exceeded. Try again in 60 seconds.",
  "retry_after": 60
}
```

---

## Códigos de Erro

### Formato de Erro

Todos os erros seguem este formato:

```json
{
  "error": "error_code",
  "message": "Human-readable message",
  "details": [
    {
      "field": "payload.content",
      "message": "Content is required",
      "code": "value_error.missing"
    }
  ],
  "request_id": "req_abc123xyz",
  "documentation_url": "https://docs.neurostack.ai/errors/error_code"
}
```

### Códigos HTTP

| Código | Erro | Descrição |
|--------|------|-----------|
| 400 | `bad_request` | Request inválida |
| 401 | `unauthorized` | API Key inválida ou ausente |
| 403 | `forbidden` | Sem permissão para o recurso |
| 404 | `not_found` | Recurso não encontrado |
| 422 | `unprocessable_entity` | Payload válida mas não processável |
| 429 | `rate_limited` | Limite de requisições excedido |
| 500 | `internal_server_error` | Erro interno |
| 501 | `not_implemented` | Funcionalidade não implementada |
| 503 | `service_unavailable` | Serviço temporariamente indisponível |

### Códigos de Domínio

| Código | Descrição |
|--------|-----------|
| `tenant_not_found` | Tenant não existe |
| `session_not_found` | Sessão não encontrada |
| `session_expired` | Sessão expirada |
| `policy_not_found` | Política não encontrada |
| `safety_violation` | Violação de regra de segurança |
| `context_overflow` | Contexto excede limite de tokens |

---

## Endpoints

### Events

#### POST /v1/events

Processa um evento através do pipeline cognitivo NeuroStack.

**Request:**

```json
{
  "session_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "event_type": "message.in",
  "payload": {
    "content": "Olá, gostaria de agendar uma consulta",
    "channel": "whatsapp",
    "user_id": "user-xyz-123",
    "metadata": {}
  }
}
```

O tenant operacional é sempre derivado da credencial autenticada (`Bearer`
JWT ou `X-API-Key`). O campo `tenant_id` no corpo é opcional e obsoleto: ele é
aceito apenas como uma asserção de compatibilidade. Quando informado, deve ser
igual ao tenant autenticado; divergências retornam `403` com
`tenant_identity_mismatch` e nunca alteram o roteamento da requisição.

**Response (200):**

```json
{
  "event_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
  "decision": {
    "intent": "agendar",
    "intent_category": "scheduling",
    "funnel_stage": "lead_novo",
    "funnel_stage_before": "lead_novo",
    "action": "iniciar_fluxo_qualificacao_srm",
    "confidence": 0.92,
    "safety_concern": false,
    "safety_category": "none",
    "needs_rag": false,
    "handoff_required": false,
    "reasoning": "User expressed intent to schedule",
    "version": 1,
    "event_id": "...",
    "risk_flags": {"safety_concern": false, "human_handoff_needed": false, ...},
    "policy_refs": [],
    "constraints": [],
    "justificativa_curta": "..."
  },
  "state_patch": {
    "slots": {},
    "funnel_stage": "em_qualificacao",
    "flags": {},
    "policy_refs": [],
    "constraints": []
  },
  "prompt": {
    "system": "Você é um assistente de agendamento para uma clínica de saúde...",
    "instructions": "Pergunte sobre a disponibilidade de horário do paciente...",
    "constraints": [
      "Não forneça diagnósticos médicos",
      "Não recomende medicamentos"
    ],
    "context_summary": "Nova sessão de agendamento iniciada"
  },
  "context": {
    "recent_messages": [
      {
        "role": "user",
        "content": "Olá, gostaria de agendar uma consulta",
        "timestamp": "2025-12-31T10:30:00Z"
      }
    ],
    "session_summary": "Nova sessão de agendamento",
    "rag_sources": [],
    "turn_count": 1
  },
  "mode": "normal",
  "processed_at": "2025-12-31T10:30:00Z",
  "processing_time_ms": 45
}
```

#### Event Types

| Tipo | Descrição |
|------|-----------|
| `message.in` | Mensagem recebida do usuário |
| `message.out` | Mensagem enviada para o usuário (log) |
| `session.start` | Início de sessão |
| `session.end` | Fim de sessão |
| `handoff.request` | Solicitação de handoff |
| `handoff.complete` | Handoff concluído |
| `safety.trigger` | Safety concern detectado |
| **`ns.funnel_stage_changed`** | **Integração CRM:** etapa do funil alterada (payload: `ns_stage`, `crm_stage_id`) |
| **`ns.handoff_created`** | **Integração CRM:** handoff criado (payload: `setor`, `prioridade`, `resumo`, etc.) |

#### Flow Modes

| Modo | Descrição |
|------|-----------|
| `normal` | Fluxo conversacional padrão |
| `rag` | Consulta de conhecimento ativa |
| `safety` | Resposta com restrições de segurança |
| `handoff` | Preparação para handoff humano |

---

### Sessions

#### POST /v1/sessions

Cria uma nova sessão de conversação.

**Request:**

```json
{
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "user_id": "user-xyz-123",
  "channel": "whatsapp",
  "initial_context": {
    "referrer": "instagram_ad"
  },
  "metadata": {
    "phone": "+5511999999999"
  }
}
```

**Response (201):**

```json
{
  "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "user_id": "user-xyz-123",
  "channel": "whatsapp",
  "cognitive_state": {
    "funnel_stage": "lead_novo",
    "intent_history": [],
    "slots": {},
    "flags": {},
    "turn_count": 0,
    "safety_triggers": 0,
    "lead": {},
    "sales": {},
    "last": {},
    "first_contact_at": null,
    "last_message_at": null
  },
  "is_active": true,
  "started_at": "2025-12-31T10:00:00Z",
  "last_activity_at": "2025-12-31T10:00:00Z",
  "message_count": 0
}
```

#### GET /v1/sessions/{session_id}

Recupera detalhes de uma sessão.

#### GET /v1/sessions

Lista sessões com paginação.

**Query Parameters:**

| Parâmetro | Tipo | Default | Descrição |
|-----------|------|---------|-----------|
| `tenant_id` | UUID | required | Filtrar por tenant |
| `page` | int | 1 | Número da página |
| `page_size` | int | 20 | Itens por página (max 100) |
| `active_only` | bool | true | Apenas sessões ativas |

#### DELETE /v1/sessions/{session_id}

Encerra uma sessão (marca como inativa).

#### GET /v1/sessions/{session_id}/events

Recupera eventos de uma sessão.

---

### Policies

#### POST /v1/policies

Cria uma nova política.

**Request:**

```json
{
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "name": "Clinic XYZ Policy",
  "description": "Regras específicas para a Clínica XYZ",
  "policy_type": "tenant",
  "rules": [
    {
      "rule": "no_price_mention",
      "message": "Não mencionar valores de consulta",
      "severity": "warning",
      "action": "warn"
    }
  ],
  "priority": 100
}
```

#### GET /v1/policies

Lista políticas com filtros.

**Query Parameters:**

| Parâmetro | Tipo | Default | Descrição |
|-----------|------|---------|-----------|
| `tenant_id` | UUID | null | Filtrar por tenant |
| `include_global` | bool | true | Incluir políticas globais |
| `active_only` | bool | true | Apenas políticas ativas |
| `policy_type` | string | null | Filtrar por tipo |

#### GET /v1/policies/tenant/{tenant_id}/effective

Retorna as políticas efetivas (merged) para um tenant.

---

### Knowledge

#### POST /v1/knowledge/documents

Upload e indexação de documento.

**Request:**

```json
{
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "content": "A clínica funciona de segunda a sexta, das 8h às 18h...",
  "metadata": {
    "title": "Horários de Atendimento",
    "category": "info",
    "tags": ["horarios", "atendimento"]
  }
}
```

#### POST /v1/knowledge/search

Busca semântica na base de conhecimento.

**Request:**

```json
{
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "query": "Qual o horário de funcionamento?",
  "top_k": 5,
  "filters": {
    "category": "info"
  },
  "min_score": 0.5
}
```

---

### Health

#### GET /health

Health check completo com status dos componentes.

**Response:**

```json
{
  "status": "healthy",
  "version": "0.1.0",
  "environment": "production",
  "timestamp": "2025-12-31T10:30:00Z",
  "uptime_seconds": 3600,
  "components": [
    {"name": "api", "status": "healthy"},
    {"name": "database", "status": "healthy", "latency_ms": 5},
    {"name": "redis", "status": "healthy", "latency_ms": 1},
    {"name": "qdrant", "status": "healthy", "latency_ms": 3}
  ]
}
```

#### GET /ready

Readiness probe para orquestradores.

#### GET /live

Liveness probe para orquestradores.

---

---

### Tenants

#### POST /v1/tenants

Cria um novo tenant.

**Request:**

```json
{
  "name": "Clínica Saúde Premium",
  "slug": "clinica-saude-premium",
  "config": {
    "description": "Clínica de Dermatologia Premium",
    "rate_limit": 100,
    "webhook_url": "https://api.clinic.com/webhooks/neurostack",
    "features": {
      "rag_enabled": true,
      "handoff_enabled": true
    }
  }
}
```

**Response (201):**

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "name": "Clínica Saúde Premium",
  "slug": "clinica-saude-premium",
  "is_active": true,
  "config": {
    "description": "Clínica de Dermatologia Premium",
    "rate_limit": 100,
    "session_ttl_seconds": 86400
  },
  "created_at": "2025-12-31T10:00:00Z",
  "updated_at": "2025-12-31T10:00:00Z"
}
```

#### GET /v1/tenants/{tenant_id}

Recupera detalhes de um tenant.

#### PUT /v1/tenants/{tenant_id}

Atualiza um tenant existente.

#### POST /v1/tenants/{tenant_id}/api-keys/rotate

Rotaciona (regenera) a API key do tenant.

**Request:**

```json
{
  "revoke_previous": true,
  "reason": "Scheduled rotation"
}
```

**Response (200):**

```json
{
  "api_key": "ns_live_abc123xyz789...",
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "created_at": "2025-12-31T10:00:00Z",
  "warning": "Store this key securely. It will not be shown again."
}
```

#### GET /v1/tenants/{tenant_id}/stats

Retorna estatísticas do tenant.

---

## Schemas

### Configuração da clínica — `PATCH /v1/config`

O endpoint recebe e devolve configuração em formato **plano**. Para atualizar
modalidades, envie somente os campos necessários; o campo `services` sempre
substitui o array inteiro, não faz merge por item.

Omitir `services` preserva o catálogo já gravado em um PATCH; `services: []`
substitui o catálogo por uma lista vazia; e `services: null` grava `null` (o
campo é nullable), embora a resposta plana de GET/PATCH o normalize para
`services: []`. Itens string legados, como `"Retorno"`, continuam aceitos.

Request:

```json
{
  "service_format": "online",
  "services": [
    {"name": "Teleconsulta", "service_format": "online"},
    "Retorno"
  ]
}
```

Resposta 200 (também plana):

```json
{
  "clinic_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "version": 1,
  "service_format": "online",
  "services": [
    {"name": "Teleconsulta", "service_format": "online"},
    "Retorno"
  ]
}
```

Cada objeto de `services` aceita propriedades extras. Quando presente, o seu
`service_format` deve ser exatamente `online`, `presential` ou `both`; Strings
legadas, campo ausente e `null` continuam aceitos. Um valor inválido usa o
422 padrão de validação Pydantic (`detail` é uma lista). `services: null`
permanece nullable no armazenamento, mas as respostas GET/PATCH o achatam para
`services: []`.

Quando o formato global canônico é incompatível com um serviço, a resposta é
422 com este contrato (índices seguem a ordem do array):

```json
{
  "detail": {
    "error": "service_format_conflict",
    "message": "One or more service modalities are incompatible with the clinic service format.",
    "clinic_service_format": "online",
    "allowed_service_formats": ["online"],
    "conflicts": [{"index": 1, "service_format": "presential"}],
    "action": "Change the listed service modalities or broaden the clinic service format."
  }
}
```

| Formato global | Serviços permitidos | Fallback no contexto da IA para serviço sem modalidade |
|-------|-------|-------|
| `online` | `online` | `online` |
| `presential` | `presential` | `presential` |
| `both` | `online`, `presential`, `both` | nenhum |
| vazio ou histórico | sem rejeição/reescrita | nenhum |

A validação cruzada roda somente quando o PATCH inclui `services` ou
`service_format`; PATCHes sem esses campos preservam dados legados. A
modalidade explícita do serviço tem precedência no contexto da IA.

### Tenant

Representa um builder ou clínica.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | UUID | Identificador único |
| `name` | string | Nome do tenant |
| `slug` | string | Identificador URL-friendly |
| `is_active` | bool | Se está ativo |
| `config` | TenantConfig | Configurações |
| `created_at` | string | Timestamp de criação |
| `updated_at` | string | Timestamp de atualização |

### TenantConfig

Configurações do tenant.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `description` | string | Descrição do tenant |
| `rate_limit` | int | Requisições por minuto |
| `session_ttl_seconds` | int | TTL da sessão |
| `max_sessions_per_user` | int | Máximo de sessões por usuário |
| `webhook_url` | string | URL do webhook |
| `features` | TenantFeatures | Feature flags |
| `branding` | TenantBranding | Configurações de branding |
| **`clinic_config`** | **ClinicConfig** | **Doc MVP v0.5:** persona, scheduling, pricing (disclosure_mode), post_care, handoff (sector_defaults). Ver [Configuração (Tenant e ClinicConfig)](./configuration.md) e [exemplo JSON](./examples/tenant-config-with-clinic.json). |

### SRMDecision / Decision Trace (CTXDecisionOutput)

Decisão cognitiva do SRM (Small Reasoning Module). O evento `srm.decision` persiste o Decision Trace conforme documentação.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `version` | int | Versão do schema (1) |
| `event_id` | string | ID do evento |
| `timestamp` | string | Timestamp ISO 8601 |
| `clinic_id` | string | ID da clínica (tenant_id) |
| `user_id` | string | ID do usuário |
| `channel` | string | Canal |
| `intent` | string | Intenção oficial (23 intenções) |
| `intent_category` | string | Categoria de alto nível |
| `action` | string | Ação do catálogo oficial |
| `funnel_stage_before` | string | Etapa antes da transição |
| `funnel_stage_after` | string | Etapa após (11 etapas canônicas) |
| `funnel_stage` | string | Etapa atual |
| `risk_flags` | object | safety_concern, human_handoff_needed, abuse_risk, compliance_risk, etc. |
| `policy_refs` | array | IDs das políticas aplicadas (POL_*) |
| `constraints` | array | IDs das constraints (C_*) |
| `confidence` | float | Score de confiança (0-1) |
| `safety_concern` | bool | Se há preocupação de segurança |
| `safety_category` | string | Categoria de safety |
| `needs_rag` | bool | Se precisa consultar conhecimento |
| `handoff_required` | bool | Se requer handoff humano |
| `handoff_reason` | string | Motivo do handoff |
| **`handoff_sector`** | **string** | **Setor:** agendamento, financeiro, gerencia, administrativo, clinico, urgencia, fidelizacao |
| **`handoff_sla`** | **string** | **SLA:** imediato, prioritario, rotina |
| `reasoning` | string | Explicação da decisão |
| `justificativa_curta` | string | Justificativa curta (Decision Trace) |

### StatePatch

Atualizações de estado a aplicar na sessão.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `slots` | object | Valores de slots extraídos |
| `funnel_stage` | string | Novo estágio do funil |
| `flags` | object | Flags booleanos de estado |

### PromptOutput

Prompt estruturado para o LLM.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `system` | string | System prompt |
| `instructions` | string | Instruções específicas para o turno |
| `constraints` | array | Lista de restrições |
| `context_summary` | string | Resumo do contexto |
| `suggested_response` | string | Resposta sugerida (opcional) |

### CognitiveState / UserState (FunnelState)

Estado cognitivo de uma sessão (doc MVP v0.5: UserState/FunnelState completo).

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `funnel_stage` | string | Estágio do funil (11 etapas: lead_novo, em_qualificacao, ...) |
| `intent_history` | array | Histórico de intents |
| `slots` | object | Slots extraídos |
| `flags` | object | Flags de estado |
| `turn_count` | int | Número de turnos |
| `safety_triggers` | int | Contagem de safety triggers |
| `last_action` | string | Última ação executada |
| **`lead`** | **object** | **lead_name, city_residence, already_patient** |
| **`sales`** | **object** | **profile** (novidade/decepcao/continuidade), **authority_done**, **objection_active** |
| **`last`** | **object** | **intent, action, policy_refs, constraints** |
| **`first_contact_at`** | string | Timestamp primeiro contato |
| **`pos_atendimento_started_at`** | string | Início pós-atendimento |
| **`last_message_at`** | string | Última mensagem |

### PolicyRule

Regra de política.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `rule` | string | Identificador da regra |
| `message` | string | Descrição legível |
| `severity` | enum | info, warning, error, critical |
| `action` | enum | log, warn, flag, block, handoff |
| `patterns` | array | Patterns de trigger |

---

## Exemplos de Integração

### Python

```python
import requests

API_KEY = "your-api-key"
BASE_URL = "https://api.neurostack.ai/v1"

def process_message(tenant_id: str, session_id: str, message: str):
    response = requests.post(
        f"{BASE_URL}/events",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": API_KEY,
        },
        json={
            "tenant_id": tenant_id,
            "session_id": session_id,
            "event_type": "message.in",
            "payload": {
                "content": message,
                "channel": "whatsapp",
            },
        },
    )
    response.raise_for_status()
    return response.json()

# Uso
result = process_message(
    tenant_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    session_id="b2c3d4e5-f6a7-8901-bcde-f12345678901",
    message="Olá, gostaria de agendar uma consulta",
)

# Acessar decisão
print(f"Intent: {result['decision']['intent']}")
print(f"Funnel: {result['decision']['funnel_stage']}")
print(f"Safety: {result['decision']['safety_concern']}")

# Usar prompt no LLM
system_prompt = result['prompt']['system']
instructions = result['prompt']['instructions']
```

### Node.js

```javascript
const axios = require('axios');

const API_KEY = 'your-api-key';
const BASE_URL = 'https://api.neurostack.ai/v1';

async function processMessage(tenantId, sessionId, message) {
  const response = await axios.post(
    `${BASE_URL}/events`,
    {
      tenant_id: tenantId,
      session_id: sessionId,
      event_type: 'message.in',
      payload: {
        content: message,
        channel: 'whatsapp',
      },
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': API_KEY,
      },
    }
  );
  
  return response.data;
}

// Uso
const result = await processMessage(
  'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
  'b2c3d4e5-f6a7-8901-bcde-f12345678901',
  'Olá, gostaria de agendar uma consulta'
);

console.log(`Intent: ${result.decision.intent}`);
console.log(`Handoff: ${result.decision.handoff_required}`);
```

### cURL

```bash
curl -X POST https://api.neurostack.ai/v1/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "session_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    "event_type": "message.in",
    "payload": {
      "content": "Olá, gostaria de agendar uma consulta",
      "channel": "whatsapp"
    }
  }'
```

---

## Middleware

A API implementa os seguintes middlewares que processam todas as requisições:

### Request ID Middleware

Cada requisição recebe um ID único para correlação de logs e tracing:

```
X-Request-ID: 550e8400-e29b-41d4-a716-446655440000
```

Você pode enviar seu próprio Request ID no header `X-Request-ID` e ele será preservado.

### Authentication Middleware

Todas as requisições (exceto endpoints de health) requerem autenticação via `X-API-Key`.

**Endpoints Públicos (sem autenticação):**
- `GET /health`
- `GET /ready`
- `GET /live`
- `GET /docs`
- `GET /redoc`
- `GET /metrics`

### Rate Limiting Middleware

Rate limiting é aplicado por tenant usando algoritmo Token Bucket:

| Configuração | Valor Padrão |
|--------------|--------------|
| Limite por minuto | 60 (configurável) |
| Burst permitido | 10 requisições |
| Algoritmo | Token Bucket |

Os limites são configuráveis por tenant no momento da criação da API Key.

---

## Changelog

### v0.1.0 (Dezembro 2025)

- Versão inicial dos contratos de API
- Endpoints: Events, Sessions, Policies, Knowledge, Health
- Schemas para SRM Decision, Prompt Output, Cognitive State

### v0.1.1 (31 Dezembro 2025) - Task 4.0 API Layer

- **Implementado:** Middleware de autenticação (API Key)
- **Implementado:** Middleware de rate limiting por tenant (Token Bucket)
- **Implementado:** Middleware de Request ID para correlação
- **Implementado:** OpenTelemetry tracing (configurável)
- **Implementado:** CORS completo com origens configuráveis
- **Implementado:** Logging estruturado (JSON/Console)
- **Adicionado:** 69 testes de integração para endpoints

### v0.2.0 (01 Janeiro 2026) - Task 13.0 Integração End-to-End

- **Implementado:** CognitivePipeline - orquestrador central conectando todos os módulos
- **Implementado:** Fluxo completo no endpoint /v1/events
- **Implementado:** Suporte a todos os modos de fluxo (NORMAL, RAG, SAFETY, HANDOFF)
- **Adicionado:** Métricas Prometheus por estágio do pipeline

### v0.3.0 (Fevereiro 2026) - Alinhamento MVP v0.5 (Documentação Técnica)

- **Funil canônico:** 11 etapas (lead_novo, em_qualificacao, qualificado, followup, agendamento_solicitado, agendado, paciente_ativo, pos_atendimento, fidelizacao, inativo, standby)
- **Intenções oficiais:** 23 intenções e catálogo de ações
- **Handoff por setor e SLA:** HandoffSector (agendamento, financeiro, gerencia, administrativo, clinico, urgencia, fidelizacao), HandoffSLA (imediato, prioritario, rotina)
- **Eventos CRM:** `ns.funnel_stage_changed`, `ns.handoff_created` para integradores
- **Decision Trace:** schema CTXDecisionOutput (version, event_id, funnel_stage_before/after, risk_flags, policy_refs, constraints, justificativa_curta)
- **ClinicConfig:** persona, scheduling, pricing (disclosure_mode), post_care, handoff (sector_defaults); opcional em TenantConfig
- **UserState/FunnelState:** lead, sales, last, timestamps em CognitiveState e StateStore
- **Políticas:** IDs oficiais C_* (constraints) e POL_* (políticas)

---

*Documentação gerada automaticamente. Última atualização: Fevereiro 2026*
