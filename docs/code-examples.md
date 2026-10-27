# Exemplos de Código - NeuroStack API

**Versão:** 1.0  
**Data:** Janeiro 2026  
**Audiência:** Desenvolvedores integrando NeuroStack

---

## Índice

1. [Setup Inicial](#setup-inicial)
2. [Python](#python)
3. [Node.js / TypeScript](#nodejs--typescript)
4. [cURL](#curl)
5. [Casos de Uso Completos](#casos-de-uso-completos)
6. [Integração com LLMs](#integração-com-llms)

---

## Setup Inicial

### Variáveis de Ambiente

```bash
# .env
NEUROSTACK_API_KEY=ns_live_abc123xyz789...
NEUROSTACK_TENANT_ID=a1b2c3d4-e5f6-7890-abcd-ef1234567890
NEUROSTACK_BASE_URL=https://api.neurostack.ai/v1

# Ambiente de desenvolvimento
# NEUROSTACK_BASE_URL=http://localhost:8000/v1
```

---

## Python

### Instalação

```bash
pip install httpx python-dotenv
```

### Cliente Básico

```python
# neurostack_client.py
import os
from uuid import uuid4
from datetime import datetime
from typing import Optional, Any

import httpx
from dotenv import load_dotenv

load_dotenv()


class NeuroStackClient:
    """Cliente Python para NeuroStack API."""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ["NEUROSTACK_API_KEY"]
        self.tenant_id = tenant_id or os.environ["NEUROSTACK_TENANT_ID"]
        self.base_url = base_url or os.environ.get(
            "NEUROSTACK_BASE_URL", "https://api.neurostack.ai/v1"
        )
        
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
            },
            timeout=10.0,
        )
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        await self._client.aclose()
    
    async def health_check(self) -> dict:
        """Verifica saúde da API."""
        response = await self._client.get("/health")
        response.raise_for_status()
        return response.json()
    
    async def create_session(
        self,
        user_id: str,
        channel: str = "whatsapp",
        initial_context: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Cria uma nova sessão."""
        payload = {
            "tenant_id": self.tenant_id,
            "user_id": user_id,
            "channel": channel,
            "initial_context": initial_context or {},
            "metadata": metadata or {},
        }
        
        response = await self._client.post("/sessions", json=payload)
        response.raise_for_status()
        return response.json()
    
    async def get_session(self, session_id: str) -> dict:
        """Recupera detalhes de uma sessão."""
        response = await self._client.get(f"/sessions/{session_id}")
        response.raise_for_status()
        return response.json()
    
    async def process_message(
        self,
        session_id: str,
        message: str,
        user_id: str,
        channel: str = "whatsapp",
        metadata: Optional[dict] = None,
    ) -> dict:
        """Processa uma mensagem do usuário."""
        payload = {
            "tenant_id": self.tenant_id,
            "session_id": session_id,
            "event_type": "message.in",
            "payload": {
                "content": message,
                "channel": channel,
                "user_id": user_id,
                "metadata": metadata or {},
            },
        }
        
        response = await self._client.post(
            "/events",
            json=payload,
            headers={"X-Request-ID": str(uuid4())},
        )
        response.raise_for_status()
        return response.json()
    
    async def complete_handoff(
        self,
        session_id: str,
        resolution: str,
        agent_id: Optional[str] = None,
    ) -> dict:
        """Notifica conclusão de handoff."""
        payload = {
            "tenant_id": self.tenant_id,
            "session_id": session_id,
            "event_type": "handoff.complete",
            "payload": {
                "content": "",
                "channel": "internal",
                "metadata": {
                    "resolution": resolution,
                    "agent": agent_id,
                    "completed_at": datetime.utcnow().isoformat(),
                },
            },
        }
        
        response = await self._client.post("/events", json=payload)
        response.raise_for_status()
        return response.json()


# Uso
async def main():
    async with NeuroStackClient() as client:
        # Verificar saúde
        health = await client.health_check()
        print(f"Status: {health['status']}")
        
        # Criar sessão
        session = await client.create_session(
            user_id="user-123",
            channel="whatsapp",
        )
        session_id = session["id"]
        print(f"Session: {session_id}")
        
        # Processar mensagem
        result = await client.process_message(
            session_id=session_id,
            message="Olá, gostaria de agendar uma consulta",
            user_id="user-123",
        )
        
        print(f"Intent: {result['decision']['intent']}")
        print(f"Mode: {result['mode']}")
        print(f"Handoff: {result['decision']['handoff_required']}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

### Integração Completa com FastAPI

```python
# app/services/neurostack.py
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import httpx

from neurostack_client import NeuroStackClient


app = FastAPI()
neurostack = NeuroStackClient()


class MessageRequest(BaseModel):
    user_id: str
    message: str
    channel: str = "whatsapp"


class MessageResponse(BaseModel):
    reply: str
    intent: str
    handoff_required: bool


@app.post("/api/chat", response_model=MessageResponse)
async def chat(request: MessageRequest):
    """Endpoint de chat integrado com NeuroStack."""
    
    # 1. Obter ou criar sessão
    session_id = f"session-{request.user_id}"
    
    # 2. Processar no NeuroStack
    try:
        result = await neurostack.process_message(
            session_id=session_id,
            message=request.message,
            user_id=request.user_id,
            channel=request.channel,
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    
    # 3. Verificar handoff
    if result["decision"]["handoff_required"]:
        return MessageResponse(
            reply="Vou transferir você para um atendente humano.",
            intent=result["decision"]["intent"],
            handoff_required=True,
        )
    
    # 4. Gerar resposta com LLM
    llm_reply = await generate_llm_response(
        prompt=result["prompt"],
        context=result["context"],
        message=request.message,
    )
    
    return MessageResponse(
        reply=llm_reply,
        intent=result["decision"]["intent"],
        handoff_required=False,
    )


async def generate_llm_response(prompt: dict, context: dict, message: str) -> str:
    """Gera resposta usando LLM com prompt do NeuroStack."""
    import openai
    
    messages = [
        {"role": "system", "content": prompt["system"]},
    ]
    
    # Adicionar histórico
    for msg in context["recent_messages"]:
        messages.append({
            "role": msg["role"],
            "content": msg["content"],
        })
    
    # Adicionar mensagem atual
    messages.append({"role": "user", "content": message})
    
    # Adicionar instruções específicas
    if prompt.get("instructions"):
        messages.append({
            "role": "system",
            "content": f"Instruções: {prompt['instructions']}"
        })
    
    response = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=messages,
        temperature=0.7,
        max_tokens=500,
    )
    
    return response.choices[0].message.content
```

### Retry com Backoff

```python
# app/utils/retry.py
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
import httpx


class RetryableError(Exception):
    """Erro que deve ser retentado."""
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(RetryableError),
)
async def call_neurostack_with_retry(client: httpx.AsyncClient, payload: dict) -> dict:
    """Chama NeuroStack com retry automático."""
    try:
        response = await client.post("/events", json=payload)
        response.raise_for_status()
        return response.json()
    
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            # Rate limited - retry
            raise RetryableError("Rate limited")
        elif e.response.status_code >= 500:
            # Server error - retry
            raise RetryableError(f"Server error: {e.response.status_code}")
        else:
            # Client error - não retry
            raise
    
    except httpx.TimeoutException:
        raise RetryableError("Timeout")
```

---

## Node.js / TypeScript

### Instalação

```bash
npm install axios uuid dotenv
# ou
yarn add axios uuid dotenv
```

### Cliente TypeScript

```typescript
// src/neurostack-client.ts
import axios, { AxiosInstance } from 'axios';
import { v4 as uuidv4 } from 'uuid';

interface ProcessMessageResponse {
  event_id: string;
  // ID do message.in persistido; null para outros event_type ou falha do Event Store
  interaction_event_id: string | null;
  decision: {
    intent: string;
    intent_category: string;
    funnel_stage: string;
    action: string;
    confidence: number;
    safety_concern: boolean;
    safety_category: string;
    needs_rag: boolean;
    handoff_required: boolean;
    handoff_reason?: string;
    reasoning: string;
  };
  state_patch: {
    slots: Record<string, any>;
    funnel_stage?: string;
    flags: Record<string, boolean>;
  };
  prompt: {
    system: string;
    instructions: string;
    constraints: string[];
    context_summary: string;
  };
  context: {
    recent_messages: Array<{
      role: string;
      content: string;
      timestamp: string;
    }>;
    session_summary: string;
    rag_sources: any[];
    turn_count: number;
  };
  mode: 'normal' | 'rag' | 'safety' | 'handoff';
  handoff_summary?: {
    reason: string;
    summary: string;
    key_info: Record<string, any>;
    urgency: string;
  };
  processed_at: string;
  processing_time_ms: number;
}

interface SessionResponse {
  id: string;
  tenant_id: string;
  user_id: string;
  channel: string;
  cognitive_state: {
    funnel_stage: string;
    intent_history: string[];
    slots: Record<string, any>;
    flags: Record<string, boolean>;
    turn_count: number;
    safety_triggers: number;
  };
  is_active: boolean;
  started_at: string;
  last_activity_at: string;
  message_count: number;
}

export class NeuroStackClient {
  private client: AxiosInstance;
  private tenantId: string;

  constructor(
    apiKey: string = process.env.NEUROSTACK_API_KEY!,
    tenantId: string = process.env.NEUROSTACK_TENANT_ID!,
    baseUrl: string = process.env.NEUROSTACK_BASE_URL || 'https://api.neurostack.ai/v1'
  ) {
    this.tenantId = tenantId;
    
    this.client = axios.create({
      baseURL: baseUrl,
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': apiKey,
      },
      timeout: 10000,
    });
  }

  async healthCheck(): Promise<{ status: string; version: string }> {
    const response = await this.client.get('/health');
    return response.data;
  }

  async createSession(
    userId: string,
    channel: string = 'whatsapp',
    initialContext?: Record<string, any>,
    metadata?: Record<string, any>
  ): Promise<SessionResponse> {
    const response = await this.client.post('/sessions', {
      tenant_id: this.tenantId,
      user_id: userId,
      channel,
      initial_context: initialContext || {},
      metadata: metadata || {},
    });
    return response.data;
  }

  async getSession(sessionId: string): Promise<SessionResponse> {
    const response = await this.client.get(`/sessions/${sessionId}`);
    return response.data;
  }

  async processMessage(
    sessionId: string,
    message: string,
    userId: string,
    channel: string = 'whatsapp',
    metadata?: Record<string, any>
  ): Promise<ProcessMessageResponse> {
    const response = await this.client.post(
      '/events',
      {
        tenant_id: this.tenantId,
        session_id: sessionId,
        event_type: 'message.in',
        payload: {
          content: message,
          channel,
          user_id: userId,
          metadata: metadata || {},
        },
      },
      {
        headers: {
          'X-Request-ID': uuidv4(),
        },
      }
    );
    return response.data;
  }

  async completeHandoff(
    sessionId: string,
    resolution: string,
    agentId?: string
  ): Promise<ProcessMessageResponse> {
    const response = await this.client.post('/events', {
      tenant_id: this.tenantId,
      session_id: sessionId,
      event_type: 'handoff.complete',
      payload: {
        content: '',
        channel: 'internal',
        metadata: {
          resolution,
          agent: agentId,
          completed_at: new Date().toISOString(),
        },
      },
    });
    return response.data;
  }
}

// Uso
async function main() {
  const client = new NeuroStackClient();
  
  // Verificar saúde
  const health = await client.healthCheck();
  console.log(`Status: ${health.status}`);
  
  // Criar sessão
  const session = await client.createSession('user-123', 'whatsapp');
  console.log(`Session: ${session.id}`);
  
  // Processar mensagem
  const result = await client.processMessage(
    session.id,
    'Olá, gostaria de agendar uma consulta',
    'user-123'
  );
  
  console.log(`Intent: ${result.decision.intent}`);
  console.log(`Mode: ${result.mode}`);
  console.log(`Handoff: ${result.decision.handoff_required}`);
}

main().catch(console.error);
```

### Express.js Integration

```typescript
// src/routes/chat.ts
import express from 'express';
import { NeuroStackClient } from './neurostack-client';
import OpenAI from 'openai';

const router = express.Router();
const neurostack = new NeuroStackClient();
const openai = new OpenAI();

interface ChatRequest {
  user_id: string;
  message: string;
  channel?: string;
}

router.post('/chat', async (req, res) => {
  try {
    const { user_id, message, channel = 'whatsapp' }: ChatRequest = req.body;
    
    // 1. Session ID baseado no user
    const sessionId = `session-${user_id}`;
    
    // 2. Processar no NeuroStack
    const result = await neurostack.processMessage(
      sessionId,
      message,
      user_id,
      channel
    );
    
    // 3. Verificar handoff
    if (result.decision.handoff_required) {
      return res.json({
        reply: 'Vou transferir você para um atendente.',
        intent: result.decision.intent,
        handoff: true,
        handoff_summary: result.handoff_summary,
      });
    }
    
    // 4. Gerar resposta com OpenAI
    const messages: any[] = [
      { role: 'system', content: result.prompt.system },
    ];
    
    // Adicionar histórico
    result.context.recent_messages.forEach((msg) => {
      messages.push({ role: msg.role, content: msg.content });
    });
    
    // Adicionar mensagem atual
    messages.push({ role: 'user', content: message });
    
    const completion = await openai.chat.completions.create({
      model: 'gpt-4',
      messages,
      temperature: 0.7,
      max_tokens: 500,
    });
    
    res.json({
      reply: completion.choices[0].message.content,
      intent: result.decision.intent,
      handoff: false,
    });
    
  } catch (error: any) {
    console.error('Chat error:', error);
    res.status(500).json({ error: error.message });
  }
});

export default router;
```

---

## cURL

### Health Check

```bash
curl -X GET https://api.neurostack.ai/health \
  -H "X-API-Key: $NEUROSTACK_API_KEY"
```

### Criar Sessão

```bash
curl -X POST https://api.neurostack.ai/v1/sessions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEUROSTACK_API_KEY" \
  -d '{
    "tenant_id": "'"$NEUROSTACK_TENANT_ID"'",
    "user_id": "user-123",
    "channel": "whatsapp",
    "initial_context": {
      "referrer": "instagram"
    }
  }'
```

### Processar Mensagem

```bash
curl -X POST https://api.neurostack.ai/v1/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEUROSTACK_API_KEY" \
  -H "X-Request-ID: $(uuidgen)" \
  -d '{
    "tenant_id": "'"$NEUROSTACK_TENANT_ID"'",
    "session_id": "SESSION_ID",
    "event_type": "message.in",
    "payload": {
      "content": "Olá, gostaria de agendar uma consulta",
      "channel": "whatsapp",
      "user_id": "user-123"
    }
  }'
```

A resposta traz `event_id` (legado) e `interaction_event_id` (o `message.in`
persistido, ou `null`). Não envie `interaction_origin`: o campo é
server-managed e retorna `422` `{"detail": {"code": "interaction_origin_server_managed"}}`.

### Listar Sessões

```bash
curl -X GET "https://api.neurostack.ai/v1/sessions?tenant_id=$NEUROSTACK_TENANT_ID&page=1&page_size=20" \
  -H "X-API-Key: $NEUROSTACK_API_KEY"
```

### Criar Política

```bash
curl -X POST https://api.neurostack.ai/v1/policies \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEUROSTACK_API_KEY" \
  -d '{
    "tenant_id": "'"$NEUROSTACK_TENANT_ID"'",
    "name": "Custom Policy",
    "description": "Política customizada",
    "policy_type": "tenant",
    "priority": 100,
    "rules": [
      {
        "rule": "collect_phone",
        "message": "Coletar telefone do lead",
        "severity": "info",
        "action": "log",
        "patterns": ["meu telefone", "celular", "whatsapp"]
      }
    ]
  }'
```

---

## Casos de Uso Completos

### WhatsApp Business Integration

```python
# examples/whatsapp_bot.py
"""
Integração completa com WhatsApp Business API.
"""
import os
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from neurostack_client import NeuroStackClient
import openai

app = FastAPI()
neurostack = NeuroStackClient()

# WhatsApp API Config
WA_TOKEN = os.environ["WHATSAPP_TOKEN"]
WA_PHONE_ID = os.environ["WHATSAPP_PHONE_ID"]


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Recebe mensagens do WhatsApp."""
    data = await request.json()
    
    # Verificar se é mensagem
    if "messages" not in data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}):
        return {"status": "ignored"}
    
    message_data = data["entry"][0]["changes"][0]["value"]["messages"][0]
    
    # Extrair dados
    from_number = message_data["from"]
    message_text = message_data.get("text", {}).get("body", "")
    
    # Processar em background para responder rápido ao webhook
    background_tasks.add_task(
        process_and_reply,
        from_number,
        message_text,
    )
    
    return {"status": "processing"}


async def process_and_reply(phone: str, message: str):
    """Processa mensagem e envia resposta."""
    session_id = f"wa-{phone}"
    
    # 1. Processar no NeuroStack
    result = await neurostack.process_message(
        session_id=session_id,
        message=message,
        user_id=phone,
        channel="whatsapp",
    )
    
    # 2. Verificar handoff
    if result["decision"]["handoff_required"]:
        # Notificar equipe
        await notify_human_agent(phone, result["handoff_summary"])
        
        # Enviar mensagem de espera
        await send_whatsapp_message(
            phone,
            "Vou transferir você para um de nossos atendentes. "
            "Aguarde um momento, por favor."
        )
        return
    
    # 3. Gerar resposta com LLM
    reply = await generate_response(result, message)
    
    # 4. Enviar resposta
    await send_whatsapp_message(phone, reply)


async def generate_response(neurostack_result: dict, user_message: str) -> str:
    """Gera resposta usando GPT com prompt do NeuroStack."""
    prompt = neurostack_result["prompt"]
    context = neurostack_result["context"]
    
    messages = [
        {"role": "system", "content": prompt["system"]},
    ]
    
    # Histórico
    for msg in context["recent_messages"][-5:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    
    messages.append({"role": "user", "content": user_message})
    
    # Instruções adicionais
    if prompt.get("instructions"):
        messages.append({
            "role": "system",
            "content": f"Instruções para esta resposta: {prompt['instructions']}"
        })
    
    response = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=messages,
        temperature=0.7,
        max_tokens=300,
    )
    
    return response.choices[0].message.content


async def send_whatsapp_message(to: str, text: str):
    """Envia mensagem via WhatsApp Business API."""
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://graph.facebook.com/v17.0/{WA_PHONE_ID}/messages",
            headers={
                "Authorization": f"Bearer {WA_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
        )


async def notify_human_agent(phone: str, handoff_summary: dict):
    """Notifica equipe sobre handoff necessário."""
    # Implementar notificação (Slack, email, etc.)
    print(f"HANDOFF NEEDED for {phone}: {handoff_summary}")
```

---

## Integração com LLMs

### OpenAI GPT-4

```python
# examples/openai_integration.py
import openai
from neurostack_client import NeuroStackClient

async def chat_with_gpt4(neurostack_result: dict, user_message: str) -> str:
    """Usa GPT-4 com prompt estruturado do NeuroStack."""
    
    prompt = neurostack_result["prompt"]
    context = neurostack_result["context"]
    
    # System prompt do NeuroStack
    system_content = prompt["system"]
    
    # Adicionar constraints como parte do system prompt
    if prompt.get("constraints"):
        constraints_text = "\n\nRESTRIÇÕES IMPORTANTES:\n" + "\n".join(
            f"- {c}" for c in prompt["constraints"]
        )
        system_content += constraints_text
    
    messages = [
        {"role": "system", "content": system_content},
    ]
    
    # Adicionar histórico recente
    for msg in context["recent_messages"]:
        messages.append({
            "role": msg["role"],
            "content": msg["content"],
        })
    
    # Adicionar mensagem atual
    messages.append({"role": "user", "content": user_message})
    
    # Adicionar instruções específicas para este turno
    if prompt.get("instructions"):
        messages.append({
            "role": "system",
            "content": f"Para esta resposta específica: {prompt['instructions']}"
        })
    
    response = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=messages,
        temperature=0.7,
        max_tokens=500,
        presence_penalty=0.1,
        frequency_penalty=0.1,
    )
    
    return response.choices[0].message.content
```

### Anthropic Claude

```python
# examples/claude_integration.py
import anthropic
from neurostack_client import NeuroStackClient

async def chat_with_claude(neurostack_result: dict, user_message: str) -> str:
    """Usa Claude com prompt estruturado do NeuroStack."""
    
    client = anthropic.Anthropic()
    prompt = neurostack_result["prompt"]
    context = neurostack_result["context"]
    
    # Construir system prompt
    system = prompt["system"]
    if prompt.get("constraints"):
        system += "\n\nIMPORTANTE - Nunca faça:\n" + "\n".join(
            f"- {c}" for c in prompt["constraints"]
        )
    
    # Construir mensagens
    messages = []
    for msg in context["recent_messages"]:
        messages.append({
            "role": msg["role"],
            "content": msg["content"],
        })
    
    messages.append({"role": "user", "content": user_message})
    
    response = client.messages.create(
        model="claude-3-sonnet-20240229",
        max_tokens=500,
        system=system,
        messages=messages,
    )
    
    return response.content[0].text
```

---

## Referências

- [API Contracts](./api-contracts.md)
- [Builder Integration Guide](./builder-integration-guide.md)
- [Policy Configuration](./policy-configuration.md)

---

*Última atualização: Janeiro 2026*

