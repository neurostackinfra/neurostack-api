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
   - [Billing do builder (leitura)](#billing-do-builder-leitura)
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

**Autorização por tenant.** Todas as rotas de políticas resolvem um único
tenant autorizado com a regra canônica `resolve_effective_tenant` antes de
ler ou alterar qualquer política. O `tenant_id` enviado pelo cliente (query,
path ou body) é um pedido de override, nunca uma autorização:

| Chamador | Sem `tenant_id` | `tenant_id` = próprio tenant | `tenant_id` de outro tenant |
|----------|-----------------|------------------------------|-----------------------------|
| API key | próprio tenant | permitido | `403` `tenant_override_forbidden` |
| JWT `clinic_admin` | próprio tenant | permitido | `403` `tenant_override_forbidden` |
| JWT `builder` | tenant do token | permitido | permitido só para clínica cujo `builder_id` é o do token; senão `403` `tenant_override_forbidden` |
| JWT `super_admin` | tenant do token | permitido | permitido (qualquer tenant; a autorização não checa existência, e o upload de knowledge a checa depois, ver [Knowledge](#knowledge)) |
| Outros papéis JWT (`investor`, desconhecidos) | `403` `caller_role_forbidden` | `403` `caller_role_forbidden` | `403` `caller_role_forbidden` |

O corpo do `403` é `{"detail": {"code": "<código>"}}`. Sem credencial, ou com
token inválido/expirado, a resposta é `401`. Uma política de outro tenant
pedida por ID dentro do próprio escopo (`GET`/`PATCH`/`DELETE`
`/v1/policies/{policy_id}`) responde `404`, igual a uma política inexistente.
A política global (`tenant_id: null`) continua visível para todo chamador
autorizado e continua imutável (`PATCH`/`DELETE` → `403`). Respostas de
`/v1/policies*` nunca são servidas do cache de respostas (a autorização roda
em toda requisição), e as respostas de sucesso trazem `Cache-Control:
no-store`. Em `GET /v1/policies`, `global_count` e `tenant_count` contam as
políticas devolvidas depois dos filtros (por escopo), e `total` é a soma.

#### POST /v1/policies

Cria uma nova política. `tenant_id` no body é opcional (padrão: tenant do
chamador) e segue a tabela acima.

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

Lista as políticas do tenant autorizado mais a política global.

**Query Parameters:**

| Parâmetro | Tipo | Default | Descrição |
|-----------|------|---------|-----------|
| `tenant_id` | UUID | null | Tenant a listar; omitido = tenant do chamador (override conforme a tabela acima) |
| `include_global` | bool | true | Incluir políticas globais |
| `active_only` | bool | true | Apenas políticas ativas |
| `policy_type` | string | null | Filtrar por tipo |

#### GET /v1/policies/{policy_id}

Retorna uma política. `tenant_id` (query, opcional, padrão: tenant do
chamador) define o escopo; a política global é retornada para qualquer
chamador autorizado.

#### PATCH /v1/policies/{policy_id}

Atualiza uma política. `tenant_id` (query) é obrigatório e precisa ser
autorizado conforme a tabela acima.

#### DELETE /v1/policies/{policy_id}

Remove uma política. `tenant_id` (query) é obrigatório e precisa ser
autorizado conforme a tabela acima.

#### GET /v1/policies/tenant/{tenant_id}/effective

Retorna as políticas efetivas (merged) para um tenant. O `tenant_id` do path
precisa ser autorizado conforme a tabela acima.

#### POST /v1/policies/evaluate

Avalia uma mensagem (`tenant_id`, `session_id`, `message` na query) contra as
políticas do tenant. `tenant_id` é obrigatório e precisa ser autorizado
conforme a tabela acima.

---

### Knowledge

**Autorização por tenant.** Todas as rotas de knowledge resolvem um único
tenant autorizado com a regra canônica `resolve_effective_tenant` antes de
ler, criar ou remover qualquer documento, e o repositório só é consultado com
esse tenant. O `tenant_id` enviado pelo cliente (query ou body) é um pedido de
override, nunca uma autorização:

| Chamador | Sem `tenant_id` | `tenant_id` = próprio tenant | `tenant_id` de outro tenant |
|----------|-----------------|------------------------------|-----------------------------|
| API key | próprio tenant | permitido | `403` `tenant_override_forbidden` |
| JWT `clinic_admin` | próprio tenant | permitido | `403` `tenant_override_forbidden` |
| JWT `builder` | tenant do token | permitido | permitido só para clínica cujo `builder_id` é o do token; senão `403` `tenant_override_forbidden` |
| JWT `super_admin` | tenant do token | permitido | permitido (qualquer tenant; a autorização não checa existência, mas o upload checa em seguida e responde `404` `Tenant not found` se o tenant não tem registro) |
| Outros papéis JWT (`investor`, desconhecidos) | `403` `caller_role_forbidden` | `403` `caller_role_forbidden` | `403` `caller_role_forbidden` |

De onde vem o tenant em cada rota (os outros lugares são ignorados; o header
`X-Tenant-ID` nunca seleciona tenant):

| Rota | Fonte do `tenant_id` |
|------|----------------------|
| `POST /v1/knowledge/documents` | body (opcional) |
| `POST /v1/knowledge/search` | body (opcional) |
| `GET /v1/knowledge/documents` | query (opcional) |
| `GET /v1/knowledge/documents/{document_id}` | query (opcional) |
| `DELETE /v1/knowledge/documents/{document_id}` | query (opcional) |

O corpo do `403` é `{"detail": {"code": "<código>"}}` e não traz dados do
tenant pedido. Sem credencial, ou com token inválido/expirado ou API key
desconhecida, a resposta é `401`. Em `GET`/`DELETE`
`/v1/knowledge/documents/{document_id}` o documento é buscado por ID **e**
tenant autorizado juntos: um documento de outro tenant responde `404`
`{"detail": "Document <document_id> not found"}`, idêntico a um ID
inexistente, então a resposta não revela se o ID existe. Para o `super_admin`
(e o `builder`) sem `tenant_id`, o escopo é o tenant do token: para ler ou
remover o documento de uma clínica é preciso passar `tenant_id` da clínica.
Respostas de `/v1/knowledge*` não entram no cache de respostas, e as
respostas de sucesso trazem `Cache-Control: no-store`.

#### POST /v1/knowledge/documents

Upload e indexação de documento. `tenant_id` no body é opcional (padrão:
tenant do chamador) e segue a tabela acima. O `201` só é respondido depois do
commit: o documento fica legível pelo mesmo tenant (e só por ele) nas rotas
abaixo. Se o tenant autorizado não tem registro, a resposta é `404`
`{"detail": "Tenant not found"}`, igual para todo chamador e sem ecoar o
`tenant_id` (o `builder` continua recebendo antes o `403` da checagem de
posse, então não descobre se uma clínica alheia existe). Uma falha antes do
commit (por exemplo depois do insert, ao montar a resposta) não deixa
documento gravado. Uma falha depois do commit (serialização da resposta,
middleware externo, confirmação do `COMMIT` perdida na conexão) pode responder
`500` com o documento já gravado: o cliente que recebe `500` deve listar ou
buscar antes de reenviar, porque o upload não é idempotente.

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

Busca semântica na base de conhecimento. `tenant_id` no body é opcional
(padrão: tenant do chamador) e segue a tabela acima. A busca ainda é um stub
que devolve `results: []`; a autorização já é aplicada.

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

#### GET /v1/knowledge/documents

Lista os documentos do tenant autorizado.

**Query Parameters:**

| Parâmetro | Tipo | Default | Descrição |
|-----------|------|---------|-----------|
| `tenant_id` | UUID | null | Tenant a listar; omitido = tenant do chamador (override conforme a tabela acima) |
| `page` | int | 1 | Número da página |
| `page_size` | int | 20 | Itens por página (max 100) |
| `category` | string | null | Filtrar por categoria |

`total` conta o mesmo conjunto filtrado da página (tenant autorizado e, quando
informada, a `category`), não todos os documentos do tenant. `category` vazia
(`?category=`) não filtra: página e `total` cobrem todos os documentos do
tenant. Um tenant sem documentos, ou sem registro, responde `200` com
`documents: []` e `total: 0`.

#### GET /v1/knowledge/documents/{document_id}

Retorna um documento (metadados, sem o conteúdo). `tenant_id` (query,
opcional, padrão: tenant do chamador) define o escopo; documento de outro
tenant responde `404`, igual a um inexistente.

#### DELETE /v1/knowledge/documents/{document_id}

Remove um documento (`204`, após o commit). Se outra transação segura a linha,
a remoção espera: se ela desfaz (rollback), a remoção responde `204`; se ela
remove e confirma, `404`. `tenant_id` (query, opcional, padrão: tenant do
chamador) define o escopo; documento de outro tenant responde `404`, igual a
um inexistente. Remover de novo responde `404`, e de
duas remoções simultâneas do mesmo documento exatamente uma responde `204`.

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

### Billing do builder (leitura)

Rotas para a tela de pagamentos. Exigem login de **builder** (JWT com claim
`builder_id`; outros papéis recebem `403`, sem token `401`). O builder vem
sempre do JWT: `builder_id`/`tenant_id` enviados pelo cliente são ignorados.
As leituras não alteram nenhuma linha, não reservam nada e não tomam lock de
linha nem advisory lock (somente `SELECT` simples). Valores monetários são
inteiros em centavos de BRL; o cliente nunca calcula preço.

#### GET /v1/billing/catalog

Catálogo imutável da versão `catalog_version_id` (uma mudança comercial
publica uma nova versão). Resposta igual para todo builder.

**Response (200):**

```json
{
  "catalog_version_id": "payment-2026-10-03",
  "offers": [
    {
      "offer_id": "plan_plus_monthly_v1",
      "kind": "subscription",
      "currency": "BRL",
      "amount_cents": 99900,
      "credits": 5000,
      "included_clinics": 1,
      "additional_clinic_amount_cents": 20000
    },
    {
      "offer_id": "package_credits_500_v1",
      "kind": "topup",
      "currency": "BRL",
      "amount_cents": 10000,
      "credits": 500,
      "included_clinics": 0,
      "additional_clinic_amount_cents": 0
    }
  ]
}
```

`offers` traz as sete ofertas na ordem canônica (Plus, Pro, Max e os pacotes
de 500/1000/2000/4000 créditos). `kind` é `subscription` (mensal) ou `topup`
(avulso); `additional_clinic_amount_cents` é o preço por clínica adicional/mês.

#### GET /v1/billing/summary

Mantém byte a byte todos os campos legados (`plan`, `active_clinics`,
`free_tier`, `extra_clinics`, `base_price`, `extra_price`, `total_monthly`,
`total_annual`, `discount_pct`, `subscription_status`, `billing_cycle`,
`usage`) e acrescenta, depois de `usage`:

```json
{
  "subscription": {
    "offer_id": "plan_pro_monthly_v1",
    "catalog_version_id": "payment-2026-10-03",
    "status": "active",
    "amount_cents": 265000,
    "current_period_end": "2026-11-11"
  },
  "checkout": {
    "available_offer_ids": ["package_credits_500_v1"],
    "live_session": {
      "checkout_session_id": "5b0c8f7e-0000-4000-8000-000000000000",
      "kind": "topup",
      "state": "active"
    },
    "available": true
  },
  "topup_commercial_enabled": true
}
```

- `subscription`: a assinatura ASAAS atual (não terminal) ou `null`.
  `status` é o estado financeiro do registro de assinatura (`active_linked`
  até a primeira liquidação, `active` depois dela, `readback_required` ou
  `non_billable_pending_readback` enquanto há evidência do provedor
  pendente), **não** o flag legado `subscription_status`. `amount_cents` é o
  valor mensal contratado (snapshot do checkout). `status` continua `active`
  durante uma renovação vencida e depois de um estorno: ele não é prova de
  período pago; para isso use `current_period_end`.
- `subscription.current_period_end`: o maior `cycle_end` entre os ciclos da
  assinatura **integralmente pagos, líquidos de estornos**, isto é,
  `state IN ('settled', 'restored') AND reversed_cents = restored_cents`.
  Ciclos estornados (`reversed`), parcialmente restaurados, `pending` ou
  `quarantined` são ignorados: um ciclo atual estornado faz o valor voltar ao
  fim do último ciclo integralmente pago, e `null` significa que nenhum ciclo
  está integralmente pago líquido de estornos: nenhum jamais foi pago, ou
  todos estão estornados, parcialmente restaurados (`reversed_cents` maior
  que `restored_cents`), `pending` ou `quarantined`. Um ciclo com estorno parcial
  **não** conta (padrão conservador; é uma decisão de política do dono do
  produto e pode mudar). É uma **data de calendário** (`YYYY-MM-DD`, o
  vencimento do ASAAS, sem fuso), não um instante UTC: o cliente deve exibi-la
  como data local sem conversão. Em JavaScript, `new Date("2026-11-11")` é
  meia-noite UTC e aparece como 10/11 em `America/Sao_Paulo`; separe
  ano/mês/dia (ou use `new Date(2026, 10, 11)`) em vez disso.
- Assinaturas legadas (de um provedor que não é o ASAAS) e
  encerradas (`inactive`/`cancelled`) resultam em `subscription: null`,
  mesmo que os campos legados (`plan`, `subscription_status`) digam `active`.
  Ao encerrar, a data de período pago também deixa de ser informada (o objeto
  inteiro vira `null`).
- `checkout.available_offer_ids`: em ordem de catálogo, as ofertas que um
  `POST /v1/billing/checkout` sem cupom aceitaria agora (rollout persistido,
  builder do canário, restrição Plus/500 créditos e sem cupom do canário,
  elegibilidade de assinatura, gate de recarga). Uma oferta com tentativa
  viva do próprio builder aparece porque o `POST` devolve essa tentativa. É
  **consultivo**: o `POST` continua sendo a autoridade, e a resposta nunca
  informa por que uma oferta está ausente. A lista **não** reflete o kill
  switch de egress financeiro, a prontidão do webhook nem o método de
  pagamento: o `POST` pode responder `503` (ou `422` para um método não
  permitido) para uma oferta listada.
- `checkout.live_session`: a tentativa viva mais recente do builder
  (`creating`, `active`, `paid_pending_subscription`,
  `reconciliation_required`, `terminal_pending_readback`) ou `null`; a URL
  do provedor é obtida em `GET /v1/billing/checkout/{checkout_session_id}`.
  Não traz `offer_id`: a oferta da tentativa é lida nesse `GET`.
- `checkout.available`: `true` quando a leitura consultiva funcionou (mesmo
  com rollout fechado e lista vazia: "nada à venda"). A mesma resposta
  `available: true` com lista vazia também ocorre quando a configuração
  ASAAS é incompatível, quando não existe a linha de rollout do ambiente ou
  quando o builder não é o canário em `canary_allowlist` (casos em que o
  `POST` também recusa com `503`). `false` somente quando
  a própria leitura falhou: então `available_offer_ids` é `[]`,
  `live_session` é `null` e `topup_commercial_enabled` é `false` (fechado
  para compra), e a tela deve mostrar "indisponível", não "nada à venda". A
  falha incrementa `neurostack_billing_summary_checkout_unavailable_total` e
  registra o aviso estruturado `billing summary checkout availability
  unavailable` (apenas `error_type`). É por requisição: nada é guardado nem
  compartilhado, então a falha de um builder não afeta outro. Os campos
  legados, `subscription` e o status `200` não mudam.
- `topup_commercial_enabled`: `true` somente se o flag armazenado é
  verdadeiro, o rollout está em `general`, a capability de recarga está
  válida agora (inclusive o vínculo dos recibos de operação) e a
  configuração ASAAS é compatível; `false` por padrão e em qualquer falha de
  leitura. O critério de aceite falava em "três condições", mas o flag também
  exige a configuração ASAAS compatível (quatro). Em `canary_allowlist` a
  recarga de 500 créditos do canário pode aparecer em `available_offer_ids`
  com `topup_commercial_enabled=false`: a tela habilita "Recarregar" pelo
  flag, não pela lista.

Se o estado financeiro (assinatura ou tentativa viva) não puder ser lido sem
adivinhar, a rota responde `503 {"detail": "billing_summary_unavailable"}` em
vez de informar "sem assinatura".

#### POST /v1/billing/checkout — `202`

O contrato `200` e os demais status não mudam. Quando o resultado no
provedor é incerto (timeout, resposta inválida), a reserva já estava
persistida e a resposta `202` passa a incluir o seu identificador (todo
caminho de criação atual persiste a reserva antes do egress; sem reserva, o
corpo continua sendo só `{"detail": ...}`):

```json
{
  "detail": "billing_checkout_reconciliation_required",
  "checkout_session_id": "5b0c8f7e-0000-4000-8000-000000000000"
}
```

Use o identificador em `GET /v1/billing/checkout/{checkout_session_id}` ou
`POST /v1/billing/checkout/{checkout_session_id}/reconcile`; nunca repita o
`POST` às cegas.

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

## Billing ASAAS (assinatura mensal no cartão)

As rotas abaixo exigem o JWT de builder (`require_builder`). Catálogo, preço,
clínicas adicionais, cupom, ciclo e vencimento são resolvidos no servidor; o
navegador escolhe somente a oferta e o método permitido. Fonte:
`src/api/routes/billing.py` e `src/api/schemas/billing.py`.

### GET `/v1/billing/summary` e GET `/v1/billing/usage`

`summary` retorna `plan`, `subscription_status`, `billing_cycle`,
`active_clinics` e `usage` (o mesmo objeto de `/usage`: período, traces,
armazenamento RAG/mídia e `credits.purchase_enabled`/`coming_soon`). Os campos
`free_tier`, `base_price`, `extra_price`, `total_monthly`, `total_annual` e
`discount_pct` são legados e não descrevem o preço das ofertas ASAAS.
`subscription_status` é uma marca de mão única: a liquidação do webhook ASAAS
(e a rota legada) só a escreve como `active`, e nada a limpa em estorno,
chargeback ou cancelamento. A página usa esse valor para duas coisas: bloquear
um novo checkout e exibir o selo "Assinatura Ativa" (com os botões
"Gerenciar Assinatura" desabilitados no lugar de "Assinar"). O selo apenas
mostra essa marca de mão única: não é prova de direito vigente e continua
aparecendo depois de um estorno, chargeback ou cancelamento. O estado financeiro
confirmado vem do estado da sessão de checkout relida do servidor
(`GET /v1/billing/checkout/{id}`); o navegador nunca escreve nenhum dos dois.

### POST `/v1/billing/checkout`

Request fechado (`extra="forbid"`):

```json
{
  "offer_id": "plan_pro_monthly_v1",
  "method": "credit_card",
  "coupon_code": "OPCIONAL"
}
```

- Ofertas de assinatura (`cycle=MONTHLY`): `plan_plus_monthly_v1`,
  `plan_pro_monthly_v1`, `plan_max_monthly_v1`. Para elas `method` é sempre
  `credit_card`; `pix` é rejeitado no schema (422).
- `coupon_code` é opcional e aceito somente para assinatura; é apenas um
  seletor normalizado (`strip().upper()`) e resolvido pelo servidor.
- Ofertas `DETACHED` (pacotes de créditos) pertencem ao contrato de recarga,
  que continua fechado: a recarga comercial está adiada e a UI não a expõe.

Resposta 200 (`CheckoutResponse`):

```json
{
  "ok": true,
  "checkout_session_id": "<uuid>",
  "kind": "subscription",
  "state": "active",
  "checkout_url": "https://asaas.com/checkoutSession/show?id=<id>"
}
```

`checkout_url` só é preenchido quando `state=active`; a URL é construída pelo
backend a partir de uma origem fixa. A página navega para ela no mesmo
separador. Um novo POST para a mesma oferta e método devolve a sessão viva
existente em vez de criar outra.

| Status | `detail` | Significado |
|--------|----------|-------------|
| 200 | — | Sessão criada ou sessão viva existente. |
| 202 | `billing_checkout_reconciliation_required` | Resultado do provedor incerto; o corpo traz também o `checkout_session_id` da reserva persistida (ver "POST /v1/billing/checkout — `202`" acima). O usuário não deve pagar de novo: para retomar a sessão existente, envia o mesmo plano outra vez (o servidor devolve a sessão viva em vez de criar outra) ou aguarda e atualiza a página. |
| 400 | `billing_checkout_coupon_invalid` / `billing_checkout_coupon_not_eligible` | Cupom inexistente, inativo, expirado, esgotado, ou não utilizável nesta oferta/estado. |
| 409 | `billing_checkout_not_eligible` | Código único para todo bloqueio de elegibilidade (assinatura não terminal, outro checkout vivo etc.). |
| 422 | `billing_checkout_input_rejected` ou erro de schema | Oferta/método/cupom inválidos. |
| 502 | `billing_checkout_creation_rejected` | O provedor recusou a criação; nada foi cobrado. |
| 503 | `billing_checkout_unavailable` | Rollout fechado, egress financeiro desligado, webhook não pronto ou configuração ausente; nenhum POST ao provedor. |

Atenção: o 202 é um status "ok" do HTTP, mas não é uma sessão criada: traz
`detail` e `checkout_session_id`, sem `ok`, `state` nem `checkout_url`. A
página atual lê apenas `detail` do 202 (e continua funcionando); o ID permite
reler ou reconciliar exatamente essa reserva.
Um 403 nestas rotas significa que o usuário autenticado não é builder (não é
sessão expirada, que é 401).

### GET `/v1/billing/checkout/{checkout_session_id}`

Relê a sessão do próprio builder, sem lock nem egress. IDs alheios ou
inexistentes retornam o mesmo 404. Estados possíveis: `creating`, `active`,
`paid_pending_subscription`, `reconciliation_required`,
`terminal_pending_readback` (vivos) e `completed`, `abandoned`, `cancelled`,
`expired`, `failed` (terminais).

### POST `/v1/billing/checkout/{checkout_session_id}/reconcile`

Reconciliação somente leitura por referência exata; nunca cria um novo
Checkout. Só age sobre `reconciliation_required` ou `creating` órfão; falha de
leitura do provedor retorna 202 `billing_checkout_reconciliation_required`: a
sessão continua em verificação pelo servidor (não é falha de leitura da
sessão), e a página informa isso sem liberar um novo checkout.

### Retorno do checkout hospedado

O backend deriva de `ASAAS_CHECKOUT_RETURN_URL` três URLs de navegação:
`?checkout=success`, `?checkout=cancel` e `?checkout=expired`. Esses valores
não são resultado financeiro. A página remove o marcador, relê
`/v1/billing/summary` e, se guardou o `checkout_session_id` em
`sessionStorage` antes do redirect, relê `GET /v1/billing/checkout/{id}` uma
vez. Não há polling: novas leituras são acionadas pelo usuário. Enquanto um ID
guardado não puder ser lido (erro diferente de 404), a página mostra o erro,
oferece nova leitura e mantém os botões de assinatura bloqueados.
