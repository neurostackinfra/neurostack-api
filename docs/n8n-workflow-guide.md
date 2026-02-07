# Guia de Workflow n8n para NeuroStack

**Versão:** 1.0  
**Data:** Janeiro 2026  
**Audiência:** Desenvolvedores integrando NeuroStack via n8n

---

## Índice

1. [Visão Geral](#visão-geral)
2. [Pré-requisitos](#pré-requisitos)
3. [Instalação](#instalação)
4. [Configuração](#configuração)
5. [Estrutura do Workflow](#estrutura-do-workflow)
6. [Variáveis de Ambiente](#variáveis-de-ambiente)
7. [Testando o Workflow](#testando-o-workflow)
8. [Personalização](#personalização)
9. [Troubleshooting](#troubleshooting)

---

## Visão Geral

Este workflow n8n implementa uma integração completa com a API NeuroStack, incluindo:

- ✅ Recebimento de mensagens via webhook
- ✅ Gerenciamento automático de sessões
- ✅ Processamento cognitivo via NeuroStack
- ✅ Detecção e tratamento de handoffs
- ✅ Geração de respostas com OpenAI GPT-4
- ✅ Logging de mensagens no NeuroStack

### Fluxo de Execução

```
Webhook → Extrair Dados → Health Check → Verificar/Criar Sessão 
  → Processar no NeuroStack → Verificar Handoff
    ├─ Handoff → Notificar Sistema → Responder
    └─ Normal → Preparar LLM → OpenAI → Logar → Responder
```

---

## Pré-requisitos

### Conta n8n

- n8n instalado (self-hosted ou cloud)
- Versão 1.0 ou superior

### Credenciais Necessárias

1. **NeuroStack API**
   - `NEUROSTACK_API_KEY`: Chave de API
   - `NEUROSTACK_TENANT_ID`: ID do tenant
   - `NEUROSTACK_BASE_URL`: URL base (opcional, padrão: `https://api.neurostack.ai/v1`)

2. **OpenAI** (para geração de respostas)
   - Credenciais da API OpenAI configuradas no n8n

3. **Sistema de Handoff** (opcional)
   - `HANDOFF_WEBHOOK_URL`: URL para notificar handoffs

---

## Instalação

### 1. Importar Workflow

1. Abra seu n8n
2. Vá em **Workflows** → **Import from File**
3. Selecione o arquivo `n8n-workflow-neurostack.json`
4. O workflow será importado com todos os nós

### 2. Verificar Nós Importados

O workflow contém os seguintes nós:

- **Webhook - Receber Mensagem**: Recebe mensagens via POST
- **Extrair Dados da Mensagem**: Processa diferentes formatos de entrada
- **Health Check NeuroStack**: Verifica disponibilidade da API
- **Verificar/Criar Sessão**: Gerencia sessões automaticamente
- **Processar Mensagem no NeuroStack**: Envia para processamento cognitivo
- **Handoff Necessário?**: Verifica se precisa transferir para humano
- **Preparar Prompt para LLM**: Monta prompt estruturado
- **Gerar Resposta com OpenAI**: Chama GPT-4
- **Logar Resposta no NeuroStack**: Registra resposta enviada
- **Responder ao Webhook**: Retorna resposta ao cliente

---

## Configuração

### Variáveis de Ambiente

Configure as seguintes variáveis de ambiente no n8n:

```bash
# NeuroStack
NEUROSTACK_API_KEY=ns_live_abc123xyz789...
NEUROSTACK_TENANT_ID=a1b2c3d4-e5f6-7890-abcd-ef1234567890
NEUROSTACK_BASE_URL=https://api.neurostack.ai/v1

# Handoff (opcional)
HANDOFF_WEBHOOK_URL=https://api.seu-sistema.com/webhooks/handoff
```

**Como configurar no n8n:**

1. Vá em **Settings** → **Environment Variables**
2. Adicione cada variável
3. Salve as alterações

### Credenciais OpenAI

1. Vá em **Credentials** → **Add Credential**
2. Selecione **OpenAI API**
3. Insira sua API Key
4. Salve como "OpenAI API"

### Configurar Autenticação HTTP

Para os nós HTTP que chamam NeuroStack:

1. Selecione o nó (ex: "Processar Mensagem no NeuroStack")
2. Em **Authentication**, selecione **Generic Credential Type**
3. Em **Generic Auth Type**, selecione **HTTP Header Auth**
4. Configure:
   - **Name**: `X-API-Key`
   - **Value**: `{{ $env.NEUROSTACK_API_KEY }}`

---

## Estrutura do Workflow

### 1. Recebimento de Mensagem

O webhook aceita múltiplos formatos de entrada (o workflow tenta detectar automaticamente):

**Formato 1 - Direto (Recomendado):**
```json
{
  "message": "Olá, gostaria de agendar",
  "user_id": "user-123",
  "channel": "whatsapp",
  "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

**Nota:** O campo `session_id` é opcional. Se não fornecido, um novo GUID será gerado automaticamente. Para reutilizar uma sessão existente, inclua o `session_id` da sessão anterior.

**Formato 2 - Com campo 'text':**
```json
{
  "text": "Olá, preciso de ajuda",
  "from": "user-456",
  "channel": "telegram"
}
```

**Formato 3 - Com campo 'content':**
```json
{
  "content": "Gostaria de saber sobre consultas",
  "user_id": "user-789",
  "channel": "web"
}
```

**Formato 4 - Com 'payload':**
```json
{
  "payload": {
    "content": "Mensagem do usuário",
    "user_id": "user-123",
    "channel": "whatsapp"
  }
}
```

**Formato 5 - WhatsApp Business API:**
```json
{
  "entry": [{
    "changes": [{
      "value": {
        "messages": [{
          "from": "5511999999999",
          "text": {"body": "Olá"}
        }],
        "contacts": [{
          "wa_id": "5511999999999"
        }]
      }
    }]
  }]
}
```

**Formato 6 - String simples:**
```json
"Olá, esta é uma mensagem direta"
```

**Nota:** O workflow é tolerante a diferentes formatos e tentará extrair a mensagem e o user_id automaticamente. Se não encontrar um formato conhecido, usará valores padrão e incluirá o body original no campo `raw_body` para debug.

### 2. Gerenciamento de Sessão

O workflow:
- Gera automaticamente um **GUID (UUID v4)** como `session_id` para cada nova mensagem
- Tenta recuperar sessão existente (se você passar `session_id` no payload)
- Cria nova sessão se não existir
- Cada sessão recebe um identificador único no formato GUID: `xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`

**Nota:** Se você quiser reutilizar uma sessão existente, inclua o campo `session_id` no payload de entrada. Caso contrário, um novo GUID será gerado automaticamente.

### 3. Processamento NeuroStack

Envia mensagem para `/v1/events` com:
- `event_type`: `message.in`
- `payload.content`: Mensagem do usuário
- `payload.channel`: Canal de origem
- `payload.user_id`: ID do usuário

### 4. Decisão de Handoff

Se `decision.handoff_required === true`:
- Prepara mensagem de handoff
- Notifica sistema externo (se configurado)
- Retorna resposta informando transferência

### 5. Geração de Resposta

Se não houver handoff:
- Extrai prompt do NeuroStack
- Adiciona constraints e instruções
- Inclui histórico de mensagens
- Chama OpenAI GPT-4
- Formata resposta

### 6. Logging

Registra resposta enviada em `/v1/events` com:
- `event_type`: `message.out`
- `payload.content`: Resposta gerada

---

## Variáveis de Ambiente

### Obrigatórias

| Variável | Descrição | Exemplo |
|----------|-----------|---------|
| `NEUROSTACK_API_KEY` | Chave de API NeuroStack | `ns_live_abc123...` |
| `NEUROSTACK_TENANT_ID` | ID do tenant | `a1b2c3d4-...` |

### Opcionais

| Variável | Descrição | Padrão |
|----------|-----------|--------|
| `NEUROSTACK_BASE_URL` | URL base da API | `https://api.neurostack.ai/v1` |
| `HANDOFF_WEBHOOK_URL` | URL para notificar handoffs | - |

---

## Testando o Workflow

### 1. Ativar Workflow

1. Abra o workflow no n8n
2. Clique em **Active** (toggle no canto superior direito)
3. Copie a URL do webhook exibida

### 2. Teste Básico

```bash
curl -X POST https://seu-n8n.com/webhook/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Olá, gostaria de agendar uma consulta",
    "user_id": "test-user-123",
    "channel": "whatsapp"
  }'
```

### 3. Resposta Esperada

```json
{
  "reply": "Olá! Fico feliz em ajudar...",
  "intent": "schedule_appointment",
  "handoff_required": false
}
```

### 4. Teste de Handoff

```bash
curl -X POST https://seu-n8n.com/webhook/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Preciso falar com um humano agora",
    "user_id": "test-user-456",
    "channel": "whatsapp"
  }'
```

---

## Personalização

### Alterar Modelo OpenAI

No nó **"Gerar Resposta com OpenAI"**:
- Altere `model` para `gpt-3.5-turbo` ou outro modelo
- Ajuste `temperature` e `maxTokens` conforme necessário

### Adicionar Retry Logic

Adicione um nó **"Retry on Failure"** após:
- "Processar Mensagem no NeuroStack"
- "Gerar Resposta com OpenAI"

### Integrar com WhatsApp Business API

1. Configure webhook do WhatsApp para apontar para seu n8n
2. O workflow já suporta formato WhatsApp Business API
3. Adicione nó para enviar resposta via WhatsApp após "Responder ao Webhook"

### Adicionar Logging Customizado

Adicione nó **"Postgres"** ou **"MongoDB"** após "Formatar Resposta" para:
- Armazenar conversas
- Analytics
- Métricas de performance

---

## Troubleshooting

### Erro: "Formato de mensagem não reconhecido"

**Causa:** O workflow não conseguiu extrair a mensagem do formato recebido.

**Solução:**
1. Verifique o formato do payload enviado ao webhook
2. Confira o campo `raw_body` na saída do nó "Extrair Dados da Mensagem" para ver o formato recebido
3. Use um dos formatos suportados listados na seção [Recebimento de Mensagem](#1-recebimento-de-mensagem)
4. Se necessário, adicione um nó "Code" antes de "Extrair Dados da Mensagem" para transformar seu formato específico

**Formato mínimo aceito:**
```json
{
  "message": "sua mensagem aqui",
  "user_id": "id-do-usuario"
}
```

**Debug:**
- Adicione um nó "Set" após o webhook para ver o body recebido
- Verifique os logs de execução no n8n
- O campo `raw_body` na saída contém o payload original para análise

### Erro: "API Key inválida"

**Solução:**
- Verifique se `NEUROSTACK_API_KEY` está configurada corretamente
- Confirme que a chave não tem espaços extras
- Teste a chave diretamente com curl:
  ```bash
  curl -H "X-API-Key: $NEUROSTACK_API_KEY" \
       https://api.neurostack.ai/health
  ```

### Erro: "Sessão não encontrada"

**Solução:**
- Verifique se o nó "Verificar Sessão Existente" está funcionando
- Confirme que "Criar Nova Sessão" está sendo executado quando necessário
- Verifique logs do n8n para detalhes
- O workflow cria automaticamente uma nova sessão se não encontrar uma existente

### Erro: "OpenAI API Error"

**Solução:**
- Verifique credenciais OpenAI no n8n
- Confirme que tem créditos disponíveis
- Verifique rate limits da OpenAI
- Tente usar um modelo diferente (ex: `gpt-3.5-turbo`)

### Workflow não responde

**Solução:**
- Verifique se workflow está ativo (toggle no canto superior direito)
- Confirme que webhook está acessível publicamente
- Verifique logs de execução no n8n
- Teste health check do NeuroStack manualmente
- Verifique se há erros nos nós anteriores que impedem o fluxo

### Handoff não funciona

**Solução:**
- Verifique se `HANDOFF_WEBHOOK_URL` está configurada (opcional)
- Confirme que o endpoint de handoff está acessível
- Verifique logs do nó "Notificar Handoff"
- O workflow retorna resposta de handoff mesmo se a notificação falhar

### Mensagem vazia ou sem conteúdo

**Solução:**
- Verifique se o campo de mensagem está sendo enviado corretamente
- Confira o campo `user_message` na saída do nó "Extrair Dados da Mensagem"
- Se necessário, ajuste o código do nó para seu formato específico

---

## Exemplos de Uso

### Integração com WhatsApp

```javascript
// Após "Responder ao Webhook", adicione nó HTTP para WhatsApp
POST https://graph.facebook.com/v17.0/{PHONE_ID}/messages
Headers:
  Authorization: Bearer {WHATSAPP_TOKEN}
Body:
{
  "messaging_product": "whatsapp",
  "to": "{{ $json.user_id }}",
  "type": "text",
  "text": {"body": "{{ $json.reply }}"}
}
```

### Integração com Telegram

```javascript
// Após "Responder ao Webhook", adicione nó HTTP para Telegram
POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage
Body:
{
  "chat_id": "{{ $json.user_id }}",
  "text": "{{ $json.reply }}"
}
```

### Adicionar Analytics

```javascript
// Após "Formatar Resposta", adicione nó para analytics
POST https://api.analytics.com/events
Body:
{
  "event": "message_sent",
  "user_id": "{{ $json.user_id }}",
  "intent": "{{ $json.intent }}",
  "timestamp": "{{ $json.timestamp }}"
}
```

---

## Referências

- [Builder Integration Guide](./builder-integration-guide.md)
- [Code Examples](./code-examples.md)
- [API Contracts](./api-contracts.md)
- [n8n Documentation](https://docs.n8n.io/)

---

## Suporte

Para dúvidas sobre:
- **Workflow n8n**: Consulte [n8n Community](https://community.n8n.io/)
- **API NeuroStack**: Consulte documentação ou entre em contato com suporte

---

*Última atualização: Janeiro 2026*
