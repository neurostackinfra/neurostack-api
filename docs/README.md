# NeuroStack API - Documentação Técnica

**Versão:** 1.0  
**Última Atualização:** Janeiro 2026

---

## 📚 Índice de Documentação

### Para Desenvolvedores (Builders)

| Documento | Descrição | Audiência |
|-----------|-----------|-----------|
| [Guia de Integração](./builder-integration-guide.md) | Guia completo para integrar NeuroStack | Desenvolvedores |
| [API Contracts](./api-contracts.md) | Especificação completa da API | Desenvolvedores |
| [Exemplos de Código](./code-examples.md) | Snippets em Python, Node.js, cURL | Desenvolvedores |
| [Configuração de Políticas](./policy-configuration.md) | Como configurar guardrails | Administradores |
| [Troubleshooting](./troubleshooting.md) | Resolução de problemas comuns | Desenvolvedores/Ops |

### Para Operações

| Documento | Descrição | Audiência |
|-----------|-----------|-----------|
| [Testes de Carga](./load-testing.md) | Guia de load testing e performance | DevOps/SRE |
| [Pipeline Cognitivo](./cognitive-pipeline.md) | Arquitetura interna do pipeline | Engenheiros |
| [Templates de Prompt e Funil](./prompt-templates-and-funnel.md) | Relação entre templates (Normal, Safety, Handoff) e as 11 etapas do funil | Engenheiros / Prompt |
| [Observabilidade](./observability.md) | Métricas, logs, alertas e dashboards | DevOps/SRE |

---

## 🚀 Quick Start

### 1. Obter Credenciais

```bash
# Você receberá da equipe NeuroStack:
NEUROSTACK_API_KEY=ns_live_xxx...
NEUROSTACK_TENANT_ID=a1b2c3d4-...
```

### 2. Verificar Conectividade

```bash
curl -H "X-API-Key: $NEUROSTACK_API_KEY" \
     https://api.neurostack.ai/health
```

### 3. Processar Primeira Mensagem

```bash
curl -X POST https://api.neurostack.ai/v1/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEUROSTACK_API_KEY" \
  -d '{
    "tenant_id": "'$NEUROSTACK_TENANT_ID'",
    "session_id": "my-first-session",
    "event_type": "message.in",
    "payload": {
      "content": "Olá, gostaria de agendar uma consulta",
      "channel": "whatsapp",
      "user_id": "user-123"
    }
  }'
```

### 4. Usar o Prompt no seu LLM

```python
# Resposta do NeuroStack contém prompt estruturado
prompt = response["prompt"]

# Usar com OpenAI
openai_response = openai.ChatCompletion.create(
    model="gpt-4",
    messages=[
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": user_message},
    ],
)
```

---

## 📖 Documentação Interativa

### Swagger UI

Acesse a documentação interativa da API:

- **Produção:** https://api.neurostack.ai/docs
- **Staging:** https://staging-api.neurostack.ai/docs
- **Local:** http://localhost:8000/docs

### ReDoc

Documentação alternativa com melhor visualização:

- **Produção:** https://api.neurostack.ai/redoc
- **Local:** http://localhost:8000/redoc

### OpenAPI Spec

Download da especificação OpenAPI:

```bash
curl https://api.neurostack.ai/v1/openapi.json -o openapi.json
```

---

## 🏗️ Arquitetura

```
┌─────────────────────────────────────────────────────────────────┐
│                        NeuroStack API                            │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐             │
│  │  API    │  │ Policy  │  │  SRM    │  │  CTX    │             │
│  │  Layer  │──│ Engine  │──│ Engine  │──│ Builder │             │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘             │
│       │            │            │            │                   │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐             │
│  │  Flow   │  │ Prompt  │  │ Memory  │  │ Handoff │             │
│  │ Manager │──│ Engine  │──│  Store  │──│ Service │             │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘             │
├─────────────────────────────────────────────────────────────────┤
│                    Infrastructure Layer                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐         │
│  │PostgreSQL│  │  Redis   │  │  Qdrant  │  │Prometheus│         │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘         │
├─────────────────────────────────────────────────────────────────┤
│                    Observability Stack                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐         │
│  │ Grafana  │  │   Loki   │  │Alertmgr  │  │ Promtail │         │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘         │
└─────────────────────────────────────────────────────────────────┘
```

### Componentes

| Componente | Responsabilidade |
|------------|-----------------|
| **API Layer** | Endpoints REST, autenticação, rate limiting |
| **Policy Engine** | Avaliação de regras e guardrails |
| **SRM Engine** | Classificação de intent, funnel, safety |
| **CTX Builder** | Construção de contexto para LLM |
| **Flow Manager** | State machine de modos de operação |
| **Prompt Engine** | Renderização de prompts estruturados |
| **Memory Store** | Estado de sessão (Redis) + eventos (PostgreSQL) |
| **Handoff Service** | Transferência para atendimento humano |

---

## 📊 Endpoints Principais

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `POST` | `/v1/events` | Processar mensagem (endpoint principal) |
| `POST` | `/v1/sessions` | Criar sessão |
| `GET` | `/v1/sessions/{id}` | Obter sessão |
| `GET` | `/v1/policies` | Listar políticas |
| `POST` | `/v1/policies` | Criar política |
| `GET` | `/health` | Health check |
| `GET` | `/ready` | Readiness probe |
| `GET` | `/live` | Liveness probe |

Veja [API Contracts](./api-contracts.md) para documentação completa.

---

## 🔒 Segurança

### Autenticação

Todas as requisições requerem header `X-API-Key`:

```bash
curl -H "X-API-Key: ns_live_xxx..." https://api.neurostack.ai/v1/...
```

### Rate Limiting

| Limite | Valor |
|--------|-------|
| Requisições/minuto | 60 (configurável) |
| Burst | 10 |

### Endpoints Públicos

- `GET /health`
- `GET /ready`
- `GET /live`

---

## 🎯 Performance Targets

| Métrica | Target | Medição |
|---------|--------|---------|
| Latência p95 | < 200ms | Prometheus |
| Latência p99 | < 500ms | Prometheus |
| Throughput | ≥ 1000 req/s | Load tests |
| Error rate | < 0.1% | Prometheus |
| Uptime | 99.9% | Monitoring |

Veja [Testes de Carga](./load-testing.md) para detalhes.

---

## 📞 Suporte

### Canais

| Canal | Uso | SLA |
|-------|-----|-----|
| Email | suporte@neurostack.ai | 24h |
| Slack | #neurostack-builders | 4h |
| Emergência | +55 11 99999-9999 | 1h |

### Documentação Adicional

- [Changelog](./CHANGELOG.md)
- [Status Page](https://status.neurostack.ai)
- [Postman Collection](https://api.neurostack.ai/docs/postman-collection.json)

---

## 📝 Changelog

### v1.1.0 (Fevereiro 2026) – Alinhamento MVP v0.5

- ✅ **Funil canônico:** 11 etapas (lead_novo, em_qualificacao, qualificado, followup, agendamento_solicitado, agendado, paciente_ativo, pos_atendimento, fidelizacao, inativo, standby)
- ✅ **Intenções oficiais:** 23 intenções e catálogo de ações da documentação
- ✅ **Handoff por setor e SLA:** HandoffSector (agendamento, financeiro, gerencia, administrativo, clinico, urgencia, fidelizacao), HandoffSLA (imediato, prioritario, rotina)
- ✅ **Eventos CRM:** `ns.funnel_stage_changed`, `ns.handoff_created` para integradores
- ✅ **Decision Trace:** schema CTXDecisionOutput (version, event_id, funnel_stage_before/after, risk_flags, policy_refs, constraints, justificativa_curta)
- ✅ **ClinicConfig:** persona, scheduling, pricing (disclosure_mode), post_care, handoff (sector_defaults)
- ✅ **UserState/FunnelState:** lead, sales, last, timestamps em sessão e StateStore
- ✅ **Políticas:** IDs oficiais C_* e POL_* (constraints e políticas da documentação)
- ✅ Pipeline e CTX aceitam `clinic_config` e `user_state` opcionais

### v1.0.0 (Janeiro 2026)

- ✅ Pipeline cognitivo completo
- ✅ SRM com classificação de intent
- ✅ Policy engine com guardrails
- ✅ Handoff service
- ✅ Documentação técnica completa

### Próximas Features

- 🔄 Knowledge Service (RAG) - Em desenvolvimento
- 🔄 Analytics Dashboard - Q1 2026
- 🔄 SDK Python oficial - Q1 2026

---

*Documentação mantida pela equipe NeuroStack*

