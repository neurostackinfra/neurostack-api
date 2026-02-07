# Guia de Integração para Builders - NeuroStack API

**Versão:** 1.0  
**Data:** Janeiro 2026  
**Audiência:** Desenvolvedores e Builders integrando NeuroStack

---

## Índice

1. [Introdução](#introdução)
2. [Pré-requisitos](#pré-requisitos)
3. [Quick Start](#quick-start)
4. [Arquitetura de Integração](#arquitetura-de-integração)
5. [Fluxo de Implementação](#fluxo-de-implementação)
6. [Configuração do Tenant](#configuração-do-tenant)
7. [Processamento de Mensagens](#processamento-de-mensagens)
8. [Gerenciamento de Sessões](#gerenciamento-de-sessões)
9. [Políticas e Guardrails](#políticas-e-guardrails)
10. [Handoff para Humanos](#handoff-para-humanos)
11. [Webhooks](#webhooks)
12. [Melhores Práticas](#melhores-práticas)
13. [SDKs e Exemplos](#sdks-e-exemplos)

---

## Introdução

NeuroStack é uma **infraestrutura cognitiva** para agentes de IA em healthcare. Ao integrar NeuroStack, você ganha:

- **Decisões Determinísticas**: SRM (Small Reasoning Module) para classificação de intents
- **Gerenciamento de Estado**: Memória de sessão e funil de conversão
- **Guardrails de Safety**: Proteção contra respostas inadequadas
- **Prompts Contextualizados**: Prompts otimizados para seu LLM

### O que NeuroStack NÃO faz

- **Não gera respostas**: Você continua usando seu LLM preferido (GPT, Claude, etc.)
- **Não armazena conversas**: Apenas o estado cognitivo é mantido
- **Não processa pagamentos**: Integração com sistemas externos é sua responsabilidade

---

## Pré-requisitos

### Credenciais

Você precisará de:

| Credencial | Descrição | Onde Obter |
|------------|-----------|------------|
| `API_KEY` | Chave de autenticação | Equipe NeuroStack |
| `TENANT_ID` | Identificador do seu tenant | Provisioning |

### Ambiente

```bash
# Variáveis de ambiente recomendadas
NEUROSTACK_API_KEY=ns_live_abc123...
NEUROSTACK_TENANT_ID=a1b2c3d4-e5f6-7890-abcd-ef1234567890
NEUROSTACK_BASE_URL=https://api.neurostack.ai/v1
```

### Requisitos de Rede

- HTTPS obrigatório em produção
- Timeout recomendado: 5 segundos
- Retry com backoff exponencial

---

## Quick Start

### 1. Verificar Conectividade

```bash
curl -H "X-API-Key: $NEUROSTACK_API_KEY" \
     https://api.neurostack.ai/health
```

**Resposta esperada:**
```json
{"status": "healthy", "version": "0.1.0"}
```

### 2. Criar uma Sessão

```bash
curl -X POST https://api.neurostack.ai/v1/sessions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEUROSTACK_API_KEY" \
  -d '{
    "tenant_id": "'$NEUROSTACK_TENANT_ID'",
    "user_id": "user-123",
    "channel": "whatsapp"
  }'
```

### 3. Processar uma Mensagem

```bash
curl -X POST https://api.neurostack.ai/v1/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEUROSTACK_API_KEY" \
  -d '{
    "tenant_id": "'$NEUROSTACK_TENANT_ID'",
    "session_id": "SESSION_ID_AQUI",
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
# Exemplo com OpenAI
import openai

# Resposta do NeuroStack
neurostack_response = {...}

# Usar o prompt estruturado
response = openai.ChatCompletion.create(
    model="gpt-4",
    messages=[
        {"role": "system", "content": neurostack_response["prompt"]["system"]},
        {"role": "user", "content": user_message},
    ],
    temperature=0.7,
)
```

---

## Arquitetura de Integração

```
┌─────────────────┐     ┌───────────────────┐     ┌─────────────┐
│   Seu Sistema   │────▶│    NeuroStack     │────▶│   Seu LLM   │
│   (Builder)     │◀────│   (Cognitivo)     │     │  (OpenAI)   │
└─────────────────┘     └───────────────────┘     └─────────────┘
        │                        │
        ▼                        ▼
┌─────────────────┐     ┌───────────────────┐
│   Usuário       │     │   Seu Backend     │
│   (WhatsApp)    │     │   (Webhooks)      │
└─────────────────┘     └───────────────────┘
```

### Fluxo de Dados

1. **Usuário envia mensagem** → Seu sistema recebe
2. **Seu sistema chama NeuroStack** → `/v1/events`
3. **NeuroStack retorna decisão** → Intent, Prompt, State
4. **Seu sistema chama seu LLM** → Com o prompt do NeuroStack
5. **LLM retorna resposta** → Você envia ao usuário

---

## Fluxo de Implementação

### Fase 1: Setup Inicial (Dia 1)

```
1. ✅ Receber credenciais (API Key + Tenant ID)
2. ✅ Testar conectividade (/health)
3. ✅ Criar primeira sessão
4. ✅ Processar mensagem de teste
```

### Fase 2: Integração Básica (Semana 1)

```
1. ✅ Integrar no fluxo de mensagens
2. ✅ Implementar gerenciamento de sessões
3. ✅ Conectar com seu LLM
4. ✅ Testar fluxos principais
```

### Fase 3: Produção (Semana 2)

```
1. ✅ Configurar políticas específicas
2. ✅ Implementar webhooks de handoff
3. ✅ Adicionar monitoramento
4. ✅ Deploy em produção
```

---

## Configuração do Tenant

### Configurações Disponíveis

```json
{
  "tenant_config": {
    "rate_limit": 100,
    "session_ttl_seconds": 86400,
    "webhook_url": "https://api.seu-sistema.com/webhooks/neurostack",
    "features": {
      "rag_enabled": true,
      "handoff_enabled": true,
      "analytics_enabled": true
    },
    "branding": {
      "assistant_name": "Dra. Ana",
      "clinic_name": "Clínica Saúde Premium"
    }
  }
}
```

### Solicitar Alterações

Entre em contato com a equipe NeuroStack para ajustar:

- Rate limits customizados
- TTL de sessão
- Feature flags
- Configurações de branding

---

## Processamento de Mensagens

### Endpoint Principal

```
POST /v1/events
```

### Request Completo

```python
import httpx
from uuid import uuid4

async def process_message(
    tenant_id: str,
    session_id: str,
    user_message: str,
    user_id: str,
    channel: str = "whatsapp",
) -> dict:
    """
    Processa uma mensagem do usuário através do NeuroStack.
    
    Args:
        tenant_id: ID do seu tenant
        session_id: ID da sessão (criar se nova)
        user_message: Texto da mensagem do usuário
        user_id: Identificador único do usuário
        channel: Canal de origem (whatsapp, web, telegram)
    
    Returns:
        Decisão cognitiva com prompt para LLM
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{NEUROSTACK_BASE_URL}/events",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": NEUROSTACK_API_KEY,
                "X-Request-ID": str(uuid4()),  # Para correlação de logs
            },
            json={
                "tenant_id": tenant_id,
                "session_id": session_id,
                "event_type": "message.in",
                "payload": {
                    "content": user_message,
                    "channel": channel,
                    "user_id": user_id,
                    "metadata": {
                        "timestamp": datetime.utcnow().isoformat(),
                    },
                },
            },
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
```

### Interpretando a Resposta

```python
def handle_neurostack_response(response: dict):
    """Processa a resposta do NeuroStack."""
    
    # 1. Verificar modo de operação
    mode = response["mode"]
    
    if mode == "handoff":
        # Transferir para humano
        return handle_handoff(response["handoff_summary"])
    
    if mode == "safety":
        # Resposta com restrições - usar prompt de safety
        prompt = response["prompt"]
        # Incluir constraints extras
        
    # 2. Extrair decisão
    decision = response["decision"]
    print(f"Intent: {decision['intent']}")
    print(f"Funnel: {decision['funnel_stage']}")
    print(f"Safety: {decision['safety_concern']}")
    
    # 3. Atualizar estado local (se necessário)
    state_patch = response["state_patch"]
    if state_patch.get("slots"):
        # Slots extraídos (ex: nome, telefone, data)
        update_local_state(state_patch["slots"])
    
    # 4. Usar prompt no LLM
    prompt = response["prompt"]
    llm_response = call_your_llm(
        system=prompt["system"],
        instructions=prompt["instructions"],
        constraints=prompt["constraints"],
        context=response["context"]["recent_messages"],
    )
    
    return llm_response
```

### Event Types

| Tipo | Quando Usar |
|------|-------------|
| `message.in` | Mensagem recebida do usuário |
| `message.out` | Mensagem enviada (para logging) |
| `session.start` | Início explícito de sessão |
| `session.end` | Fim de sessão |
| `handoff.complete` | Handoff humano concluído |

---

## Gerenciamento de Sessões

### Estratégias de Session ID

#### Opção 1: User ID = Session ID (Simples)

```python
# Para casos onde há 1 sessão por usuário
session_id = f"session-{user_id}"
```

#### Opção 2: Sessão por Conversa (Recomendado)

```python
# Criar nova sessão quando:
# - Usuário inicia nova conversa
# - Passaram mais de 24h desde última mensagem
# - Usuário solicita explicitamente

if should_create_new_session(user_id):
    session = create_neurostack_session(tenant_id, user_id)
    session_id = session["id"]
else:
    session_id = get_active_session(user_id)
```

### Criar Sessão

```python
async def create_session(
    tenant_id: str,
    user_id: str,
    channel: str,
    initial_context: dict = None,
) -> dict:
    """Cria uma nova sessão de conversação."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{NEUROSTACK_BASE_URL}/sessions",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": NEUROSTACK_API_KEY,
            },
            json={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "channel": channel,
                "initial_context": initial_context or {},
                "metadata": {
                    "created_by": "builder_api",
                },
            },
        )
        return response.json()
```

### Verificar Estado da Sessão

```python
async def get_session_state(session_id: str) -> dict:
    """Recupera o estado atual de uma sessão."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{NEUROSTACK_BASE_URL}/sessions/{session_id}/state",
            headers={"X-API-Key": NEUROSTACK_API_KEY},
        )
        return response.json()
```

---

## Políticas e Guardrails

### Políticas Globais (Pré-configuradas)

Todas as clínicas herdam estas regras:

| Regra | Descrição | Ação |
|-------|-----------|------|
| `no_medical_diagnosis` | Não dar diagnósticos | block |
| `no_medication_prescription` | Não indicar medicamentos | block |
| `no_result_promises` | Não prometer resultados | warn |
| `emergency_detection` | Detectar emergências | handoff |

### Políticas Customizadas

Você pode adicionar políticas específicas para sua clínica:

```json
{
  "name": "Clinica XYZ Custom Policy",
  "policy_type": "tenant",
  "rules": [
    {
      "rule": "no_competitor_mention",
      "message": "Não mencionar concorrentes",
      "severity": "warning",
      "action": "warn"
    },
    {
      "rule": "collect_phone_first",
      "message": "Sempre coletar telefone antes de agendar",
      "severity": "info",
      "action": "log"
    }
  ]
}
```

### Ver Políticas Efetivas

```bash
curl -H "X-API-Key: $NEUROSTACK_API_KEY" \
     "https://api.neurostack.ai/v1/policies/tenant/$TENANT_ID/effective"
```

---

## Handoff para Humanos

### Quando Ocorre Handoff

O NeuroStack solicita handoff quando:

1. **Usuário pede explicitamente** ("Quero falar com um humano")
2. **Safety concern detectado** (emergência médica, risco)
3. **Limite de turnos atingido** (configurável, default: 10)
4. **Frustração detectada** (múltiplas tentativas sem sucesso)

### Detectando Handoff na Resposta

```python
def check_handoff(response: dict) -> bool:
    """Verifica se handoff é necessário."""
    return response["decision"]["handoff_required"]

def get_handoff_info(response: dict) -> dict:
    """Extrai informações do handoff."""
    return {
        "reason": response["decision"]["handoff_reason"],
        "summary": response["handoff_summary"]["summary"],
        "key_info": response["handoff_summary"]["key_info"],
        "urgency": response["handoff_summary"]["urgency"],
    }
```

### Configurar Webhook de Handoff

```python
# No seu servidor
from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/webhooks/neurostack/handoff")
async def handle_handoff_webhook(request: Request):
    """Recebe notificações de handoff do NeuroStack."""
    payload = await request.json()
    
    # Extrair informações
    session_id = payload["session_id"]
    user_id = payload["user_id"]
    reason = payload["reason"]
    summary = payload["summary"]
    urgency = payload["urgency"]
    
    # Notificar sua equipe
    if urgency == "urgent":
        send_urgent_notification(user_id, summary)
    else:
        add_to_queue(user_id, summary)
    
    return {"status": "received"}
```

### Notificar Conclusão do Handoff

```python
async def complete_handoff(session_id: str, resolution: str):
    """Notifica NeuroStack que o handoff foi concluído."""
    await httpx.post(
        f"{NEUROSTACK_BASE_URL}/events",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": NEUROSTACK_API_KEY,
        },
        json={
            "tenant_id": TENANT_ID,
            "session_id": session_id,
            "event_type": "handoff.complete",
            "payload": {
                "content": "",
                "channel": "internal",
                "metadata": {
                    "resolution": resolution,
                    "agent": "human_agent_id",
                },
            },
        },
    )
```

---

## Webhooks

### Eventos Disponíveis

| Evento | Descrição |
|--------|-----------|
| `handoff.requested` | Handoff iniciado |
| `safety.triggered` | Safety concern detectado |
| `session.expired` | Sessão expirou |
| `funnel.converted` | Usuário avançou no funil |

### Configurar Webhook URL

Entre em contato com a equipe NeuroStack para configurar:

```json
{
  "webhook_url": "https://api.seu-sistema.com/webhooks/neurostack",
  "events": ["handoff.requested", "safety.triggered"],
  "secret": "webhook_secret_for_validation"
}
```

### Validar Webhook

```python
import hmac
import hashlib

def verify_webhook_signature(
    payload: bytes,
    signature: str,
    secret: str,
) -> bool:
    """Valida assinatura do webhook."""
    expected = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    
    return hmac.compare_digest(f"sha256={expected}", signature)
```

---

## Melhores Práticas

### ✅ Faça

1. **Use Request IDs** para correlação de logs
2. **Implemente retry** com backoff exponencial
3. **Cache sessões** localmente para evitar lookups
4. **Monitore latência** das chamadas
5. **Trate handoffs** imediatamente

### ❌ Evite

1. **Não ignore** o campo `mode` da resposta
2. **Não descarte** os `constraints` do prompt
3. **Não crie** sessões desnecessárias
4. **Não exponha** a API Key no frontend
5. **Não ignore** safety concerns

### Tratamento de Erros

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
async def call_neurostack(payload: dict) -> dict:
    """Chamada com retry automático."""
    try:
        response = await httpx.post(
            f"{NEUROSTACK_BASE_URL}/events",
            json=payload,
            headers={"X-API-Key": NEUROSTACK_API_KEY},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            # Rate limited - aguardar
            retry_after = int(e.response.headers.get("Retry-After", 60))
            await asyncio.sleep(retry_after)
            raise  # Retry
        elif e.response.status_code >= 500:
            raise  # Retry para erros de servidor
        else:
            raise  # Não fazer retry para erros de cliente
```

---

## SDKs e Exemplos

### SDK Python (Oficial)

```bash
pip install neurostack
```

```python
from neurostack import NeuroStackClient

client = NeuroStackClient(
    api_key=os.environ["NEUROSTACK_API_KEY"],
    tenant_id=os.environ["NEUROSTACK_TENANT_ID"],
)

# Processar mensagem
result = await client.process_message(
    session_id="session-123",
    message="Olá, gostaria de agendar",
    channel="whatsapp",
    user_id="user-456",
)

print(result.intent)
print(result.prompt.system)
```

### Exemplos Completos

#### WhatsApp Business Integration

```python
# examples/whatsapp_integration.py
from fastapi import FastAPI, Request
from neurostack import NeuroStackClient
import openai

app = FastAPI()
neurostack = NeuroStackClient(...)

@app.post("/webhook/whatsapp")
async def handle_whatsapp_message(request: Request):
    data = await request.json()
    
    # Extrair mensagem do WhatsApp
    message = data["entry"][0]["changes"][0]["value"]["messages"][0]
    user_id = message["from"]
    text = message["text"]["body"]
    
    # Processar no NeuroStack
    result = await neurostack.process_message(
        session_id=f"wa-{user_id}",
        message=text,
        channel="whatsapp",
        user_id=user_id,
    )
    
    # Verificar handoff
    if result.handoff_required:
        await notify_human_agent(user_id, result.handoff_summary)
        return {"status": "handoff"}
    
    # Gerar resposta com LLM
    llm_response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": result.prompt.system},
            *result.context.recent_messages,
            {"role": "user", "content": text},
        ],
    )
    
    # Enviar resposta
    await send_whatsapp_message(user_id, llm_response.choices[0].message.content)
    
    return {"status": "ok"}
```

### Postman Collection

Importe nossa collection do Postman:

```
https://api.neurostack.ai/docs/postman-collection.json
```

---

## Suporte

### Canais de Suporte

| Canal | Uso | SLA |
|-------|-----|-----|
| Email | suporte@neurostack.ai | 24h |
| Slack | #neurostack-builders | 4h (business hours) |
| Emergência | +55 11 99999-9999 | Imediato |

### Documentação Adicional

- [API Reference](./api-contracts.md)
- [Cognitive Pipeline](./cognitive-pipeline.md)
- [Policy Configuration](./policy-configuration.md)
- [Troubleshooting](./troubleshooting.md)

---

*Última atualização: Janeiro 2026*

