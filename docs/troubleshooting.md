# Troubleshooting - NeuroStack API

**Versão:** 1.0  
**Data:** Janeiro 2026  
**Audiência:** Desenvolvedores e Operadores

---

## Índice

1. [Problemas de Conectividade](#problemas-de-conectividade)
2. [Erros de Autenticação](#erros-de-autenticação)
3. [Rate Limiting](#rate-limiting)
4. [Problemas de Sessão](#problemas-de-sessão)
5. [Erros no Pipeline Cognitivo](#erros-no-pipeline-cognitivo)
6. [Problemas de Performance](#problemas-de-performance)
7. [Handoff Issues](#handoff-issues)
8. [Logs e Debugging](#logs-e-debugging)
9. [FAQ](#faq)

---

## Problemas de Conectividade

### API não responde

**Sintomas:**
- Timeout nas requisições
- Connection refused
- DNS resolution failed

**Diagnóstico:**

```bash
# 1. Verificar se API está acessível
curl -v https://api.neurostack.ai/live

# 2. Verificar DNS
nslookup api.neurostack.ai

# 3. Verificar conectividade
ping api.neurostack.ai

# 4. Testar porta
nc -zv api.neurostack.ai 443
```

**Soluções:**

| Problema | Solução |
|----------|---------|
| DNS falha | Verificar configuração DNS do servidor |
| Timeout | Aumentar timeout para 10s |
| Connection refused | Verificar firewall/proxy |
| SSL error | Verificar certificados e clock do servidor |

### Timeout em requisições

**Sintomas:**
- Requisições demoram mais de 5s
- Erros de timeout intermitentes

**Diagnóstico:**

```python
import httpx
import time

async def test_latency():
    async with httpx.AsyncClient() as client:
        start = time.time()
        try:
            response = await client.get(
                "https://api.neurostack.ai/health",
                timeout=10.0,
            )
            latency = (time.time() - start) * 1000
            print(f"Latency: {latency:.0f}ms")
            print(f"Status: {response.status_code}")
        except httpx.TimeoutException:
            print(f"Timeout after {(time.time() - start) * 1000:.0f}ms")
```

**Soluções:**

1. **Aumentar timeout:**
   ```python
   client = httpx.AsyncClient(timeout=10.0)
   ```

2. **Implementar retry:**
   ```python
   from tenacity import retry, stop_after_attempt, wait_exponential
   
   @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
   async def call_api(payload):
       return await client.post("/events", json=payload)
   ```

3. **Verificar região:**
   - API está hospedada em São Paulo (sa-east-1)
   - Considerar latência de rede se estiver em outra região

---

## Erros de Autenticação

### 401 Unauthorized

**Resposta:**
```json
{
  "error": "unauthorized",
  "message": "Invalid or missing API key"
}
```

**Causas e Soluções:**

| Causa | Verificação | Solução |
|-------|-------------|---------|
| API Key ausente | Verificar header `X-API-Key` | Adicionar header |
| API Key inválida | Verificar se key é válida | Contatar suporte para nova key |
| API Key expirada | Key pode ter sido revogada | Solicitar rotação de key |
| Header errado | Verificar case do header | Usar exatamente `X-API-Key` |

**Teste:**

```bash
# Verificar se API Key está sendo enviada
curl -v -H "X-API-Key: $API_KEY" https://api.neurostack.ai/health

# Procurar por "X-API-Key" nos headers de request
```

### 403 Forbidden

**Resposta:**
```json
{
  "error": "forbidden",
  "message": "Access denied for tenant"
}
```

**Causas:**
- Tentando acessar recursos de outro tenant
- API Key não tem permissão para o endpoint
- Tenant desativado

**Solução:**
```python
# Verificar tenant_id está correto
print(f"Using tenant: {os.environ['NEUROSTACK_TENANT_ID']}")

# Verificar se está usando o tenant correto no payload
payload = {
    "tenant_id": TENANT_ID,  # Deve corresponder à API Key
    ...
}
```

---

## Rate Limiting

### 429 Too Many Requests

**Resposta:**
```json
{
  "error": "rate_limited",
  "message": "Rate limit exceeded. Try again in 60 seconds.",
  "retry_after": 60
}
```

**Headers de resposta:**
```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1704067200
Retry-After: 60
```

### Diagnóstico

```python
async def check_rate_limit_status(response):
    """Verifica status do rate limit."""
    headers = response.headers
    
    print(f"Limit: {headers.get('X-RateLimit-Limit')}")
    print(f"Remaining: {headers.get('X-RateLimit-Remaining')}")
    print(f"Reset at: {headers.get('X-RateLimit-Reset')}")
    
    if response.status_code == 429:
        retry_after = int(headers.get('Retry-After', 60))
        print(f"Rate limited! Retry after {retry_after}s")
```

### Soluções

1. **Implementar backoff:**
   ```python
   import asyncio
   
   async def call_with_rate_limit_handling(client, payload):
       while True:
           response = await client.post("/events", json=payload)
           
           if response.status_code == 429:
               retry_after = int(response.headers.get("Retry-After", 60))
               print(f"Rate limited, waiting {retry_after}s")
               await asyncio.sleep(retry_after)
               continue
           
           return response
   ```

2. **Distribuir carga:**
   - Espalhar requisições ao longo do tempo
   - Evitar bursts de requisições

3. **Solicitar aumento de limite:**
   - Contatar suporte para aumentar rate limit
   - Limite padrão: 60 req/min

---

## Problemas de Sessão

### Session Not Found (404)

**Resposta:**
```json
{
  "error": "session_not_found",
  "message": "Session not found or has expired"
}
```

**Causas:**
- Sessão expirou (TTL padrão: 24h)
- Session ID inválido
- Sessão de outro tenant

**Solução:**

```python
async def get_or_create_session(user_id: str) -> str:
    """Obtém sessão existente ou cria nova."""
    session_id = f"session-{user_id}"
    
    try:
        # Tentar obter sessão
        session = await neurostack.get_session(session_id)
        if session.get("is_active"):
            return session_id
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
    
    # Criar nova sessão
    new_session = await neurostack.create_session(
        user_id=user_id,
        channel="whatsapp",
    )
    return new_session["id"]
```

### Session Expired

**Sintomas:**
- Sessão existia mas agora retorna 404
- Estado cognitivo perdido

**Soluções:**

1. **Recriar sessão:**
   ```python
   # Criar nova sessão quando expirar
   session = await neurostack.create_session(user_id, channel)
   ```

2. **Aumentar TTL:**
   - Contatar suporte para aumentar session_ttl_seconds
   - Default: 86400 (24h)

3. **Persistir estado localmente:**
   ```python
   # Salvar estado importante localmente
   local_cache[user_id] = {
       "session_id": session_id,
       "context": result["context"],
   }
   ```

---

## Erros no Pipeline Cognitivo

### Intent Classification Errors

**Sintoma:** Intent incorreto retornado

**Diagnóstico:**

```python
result = await neurostack.process_message(...)

print(f"Intent: {result['decision']['intent']}")
print(f"Confidence: {result['decision']['confidence']}")
print(f"Reasoning: {result['decision']['reasoning']}")

# Se confidence < 0.7, classificação pode estar incerta
```

**Soluções:**

1. **Mensagens mais claras:**
   - Orientar usuários a serem específicos
   - Usar mensagens de clarificação

2. **Feedback para melhoria:**
   - Reportar classificações incorretas
   - NeuroStack usa feedback para melhorar

### Safety False Positives

**Sintoma:** Mensagem normal ativa modo safety

**Diagnóstico:**

```python
if result["decision"]["safety_concern"]:
    print(f"Safety category: {result['decision']['safety_category']}")
    print(f"Mode: {result['mode']}")
```

**Soluções:**

1. **Verificar patterns:**
   - Algumas palavras podem ter duplo sentido
   - Contexto médico tem regras rígidas

2. **Reportar falso positivo:**
   - Enviar mensagem original e contexto
   - NeuroStack ajustará thresholds

### Handoff Inesperado

**Sintoma:** Handoff sendo solicitado sem necessidade

**Causas:**
- Muitos turnos sem resolução (default: 10)
- Frustração detectada
- Safety concern

**Diagnóstico:**

```python
if result["decision"]["handoff_required"]:
    print(f"Reason: {result['decision']['handoff_reason']}")
    print(f"Turn count: {result['context']['turn_count']}")
```

**Soluções:**

1. **Ajustar limite de turnos:**
   - Solicitar aumento de `handoff_max_turns`

2. **Melhorar resoluções:**
   - Resolver dúvidas mais rapidamente
   - Usar prompts mais direcionados

---

## Problemas de Performance

### Latência Alta

**Sintoma:** Requisições > 200ms

**Diagnóstico:**

```python
import time

start = time.time()
result = await neurostack.process_message(...)
latency_ms = (time.time() - start) * 1000

print(f"API latency: {latency_ms:.0f}ms")
print(f"Processing time: {result['processing_time_ms']}ms")

# Se API latency >> processing_time, problema é rede
```

**Soluções:**

| Problema | Solução |
|----------|---------|
| Rede lenta | Verificar conexão, usar região mais próxima |
| Processing alto | Reportar para análise |
| Cold start | Primeiras requisições podem ser mais lentas |

### Métricas de Performance

```python
async def monitor_performance(client, num_requests=100):
    """Monitora performance das requisições."""
    latencies = []
    errors = 0
    
    for i in range(num_requests):
        start = time.time()
        try:
            response = await client.get("/health")
            latencies.append((time.time() - start) * 1000)
        except Exception:
            errors += 1
    
    print(f"Requests: {num_requests}")
    print(f"Errors: {errors} ({errors/num_requests*100:.1f}%)")
    print(f"Avg latency: {sum(latencies)/len(latencies):.0f}ms")
    print(f"P95 latency: {sorted(latencies)[int(len(latencies)*0.95)]:.0f}ms")
```

---

## Handoff Issues

### Webhook não recebido

**Sintomas:**
- Handoff solicitado mas webhook não chega
- Sistema de atendimento não notificado

**Diagnóstico:**

1. **Verificar configuração:**
   ```bash
   # Verificar se webhook está configurado
   curl -H "X-API-Key: $API_KEY" \
        https://api.neurostack.ai/v1/tenants/$TENANT_ID
   ```

2. **Verificar endpoint:**
   ```bash
   # Testar se seu endpoint está acessível
   curl -X POST https://seu-sistema.com/webhooks/neurostack \
        -H "Content-Type: application/json" \
        -d '{"test": true}'
   ```

3. **Verificar logs:**
   - NeuroStack registra tentativas de webhook
   - Verificar logs de erro do seu servidor

**Soluções:**

1. **Validar URL do webhook:**
   - Deve ser HTTPS em produção
   - Deve retornar 2xx rapidamente

2. **Implementar retry:**
   - Webhooks são retentados 3x
   - Implementar idempotência

### Resumo de Handoff incompleto

**Sintoma:** Handoff summary faltando informações

**Solução:**

```python
# Extrair informações adicionais do state_patch
handoff_info = {
    "summary": result["handoff_summary"]["summary"],
    "key_info": result["handoff_summary"]["key_info"],
    "slots": result["state_patch"]["slots"],  # Dados extraídos
    "turn_count": result["context"]["turn_count"],
    "funnel_stage": result["decision"]["funnel_stage"],
}
```

---

## Logs e Debugging

### Habilitando logs detalhados

```python
import logging
import httpx

# Habilitar logs do httpx
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.DEBUG)

# Ou usar hooks
async def log_request(request):
    print(f"Request: {request.method} {request.url}")
    print(f"Headers: {dict(request.headers)}")

async def log_response(response):
    print(f"Response: {response.status_code}")
    print(f"Headers: {dict(response.headers)}")

client = httpx.AsyncClient(
    event_hooks={
        "request": [log_request],
        "response": [log_response],
    }
)
```

### Request ID para suporte

Sempre inclua o Request ID ao contatar suporte:

```python
import uuid

request_id = str(uuid.uuid4())

response = await client.post(
    "/events",
    json=payload,
    headers={"X-Request-ID": request_id},
)

print(f"Request ID: {request_id}")
# Guardar para troubleshooting
```

### Estrutura de log recomendada

```python
import structlog

log = structlog.get_logger()

async def process_message_with_logging(user_id, message):
    request_id = str(uuid.uuid4())
    
    log.info(
        "processing_message",
        request_id=request_id,
        user_id=user_id,
        message_length=len(message),
    )
    
    try:
        result = await neurostack.process_message(...)
        
        log.info(
            "message_processed",
            request_id=request_id,
            intent=result["decision"]["intent"],
            mode=result["mode"],
            processing_time_ms=result["processing_time_ms"],
        )
        
        return result
        
    except Exception as e:
        log.error(
            "processing_failed",
            request_id=request_id,
            error=str(e),
        )
        raise
```

---

## FAQ

### Posso usar o mesmo session_id para usuários diferentes?

**Não.** Cada sessão deve ser única por usuário/conversa. Usar o mesmo session_id para usuários diferentes causará conflitos de estado.

```python
# Correto
session_id = f"session-{user_id}"

# Incorreto
session_id = "shared-session"
```

### Como saber se devo criar nova sessão?

Crie nova sessão quando:
- É a primeira mensagem do usuário
- Passaram mais de 24h desde a última mensagem
- Usuário solicita explicitamente "começar de novo"
- Sessão anterior retorna 404

### Qual o tamanho máximo de mensagem?

- **Content:** 4000 caracteres
- **Metadata:** 10KB JSON

### Posso enviar imagens/áudio?

Atualmente a API processa apenas texto. Para mídia:
1. Transcreva áudio para texto
2. Descreva imagens como texto
3. Envie o texto resultante

### Como testar em ambiente de desenvolvimento?

```bash
# Usar ambiente de staging
export NEUROSTACK_BASE_URL=https://staging-api.neurostack.ai/v1

# Ou rodar localmente
export NEUROSTACK_BASE_URL=http://localhost:8000/v1
```

### Rate limit é por API Key ou por Tenant?

Por **tenant**. Todas as API Keys do mesmo tenant compartilham o mesmo limite.

### Logs são retidos por quanto tempo?

- **Eventos:** 90 dias
- **Métricas:** 30 dias
- **Logs de debug:** 7 dias

---

## Recuperação do release Gemini

- **Python ausente/antigo no VPS:** instale Python 3.11+ e execute novamente;
  nada de Docker ou `.env` ocorre antes desse gate.
- **Buildx ausente:** corrija o runner protegido. Nunca instale buildx no VPS
  como bypass e nunca use `docker build` no checkout.
- **Metadata ausente ou digest inválido:** descarte o run, refaça o build por
  `git archive` e leia apenas `containerimage.digest`; não resolva a tag.
- **Smoke/digest divergente:** descarte imagem e artefato. O smoke deve rodar
  read-only exatamente em `repository@sha256` que será implantado.
- **Smoke `internal_error`:** houve falha local inesperada, não uma resposta
  homologada do provider. Consulte somente o diagnóstico privado do runner,
  corrija runner ou imagem e crie um run novo; não copie segredo ou corpo externo.
- **Trust/fingerprint/assinatura falha:** importe a chave Operations aprovada e
  configure `GEMINI_RECEIPT_SIGNER_FINGERPRINT` protegido. Não desabilite
  `git verify-commit`.
- **HEAD, pai ou diff divergente; checkout dirty; receipt stale:** não limpe ou
  emende em produção para contornar o gate. Crie novo candidate limpo, build,
  smoke live, receipt-only assinado e implante com o receipt como HEAD exato.
- **Compose recusa segredo/origem:** corrija `.env` manualmente com valores
  distintos e fortes conforme o runbook. A mensagem identifica regra/campo, mas
  nunca o valor. O deploy nunca cria, altera ou gera segredos.

O checker usa somente a biblioteca padrão do Python. Falha fechada é o estado
esperado enquanto o receipt protegido ainda não existe.

---

## Contato para Suporte

### Níveis de Suporte

| Nível | Tempo de Resposta | Canal |
|-------|-------------------|-------|
| Crítico (produção down) | 1h | Telefone |
| Alto (funcionalidade bloqueada) | 4h | Slack |
| Normal (dúvidas técnicas) | 24h | Email |

### Informações para Suporte

Ao contatar suporte, inclua:

1. **Request ID** da requisição com problema
2. **Tenant ID**
3. **Timestamp** (UTC)
4. **Payload** enviado (sanitizado)
5. **Resposta** recebida
6. **Logs** relevantes

### Canais

- **Email:** suporte@neurostack.ai
- **Slack:** #neurostack-builders
- **Emergência:** +55 11 99999-9999

---

*Última atualização: Janeiro 2026*
