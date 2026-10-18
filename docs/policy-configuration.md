# Configuração de Políticas - NeuroStack API

**Versão:** 1.1  
**Data:** Fevereiro 2026  
**Audiência:** Administradores de Tenant e Builders  
**Alinhamento:** Documentação Técnica MVP v0.5 – IDs oficiais C_* (constraints) e POL_* (políticas)

---

## Índice

1. [Visão Geral](#visão-geral)
2. [Hierarquia de Políticas](#hierarquia-de-políticas)
3. [Políticas Globais](#políticas-globais)
4. [Políticas de Tenant](#políticas-de-tenant)
5. [Estrutura de Regras](#estrutura-de-regras)
6. [Ações Disponíveis](#ações-disponíveis)
7. [Patterns e Triggers](#patterns-e-triggers)
8. [Exemplos Práticos](#exemplos-práticos)
9. [API de Políticas](#api-de-políticas)
10. [Troubleshooting](#troubleshooting)

---

## Visão Geral

O sistema de políticas do NeuroStack funciona como **guardrails** para garantir que as respostas da IA sigam diretrizes de safety, compliance e qualidade.

### Conceitos Chave

| Conceito | Descrição |
|----------|-----------|
| **Política** | Conjunto de regras aplicáveis a um contexto |
| **Regra** | Definição individual de um guardrail |
| **Trigger** | Condição que ativa a regra |
| **Ação** | Comportamento quando a regra é ativada |

### Quando as Políticas são Aplicadas

```
1. Mensagem recebida
        ↓
2. [PRE-CHECK] Políticas verificadas ANTES do SRM
        ↓
3. SRM processa a mensagem
        ↓
4. [POST-CHECK] Políticas verificadas APÓS decisão
        ↓
5. Prompt gerado com constraints das políticas
```

---

## Hierarquia de Políticas

As políticas seguem uma hierarquia de prioridade:

```
┌─────────────────────────────────────┐
│         POLÍTICAS GLOBAIS           │  ← Mandatórias (não podem ser removidas)
│  (Hardcoded - Healthcare Safety)    │
├─────────────────────────────────────┤
│       POLÍTICAS DE SISTEMA          │  ← Configuráveis pelo NeuroStack
│    (Default para todos tenants)     │
├─────────────────────────────────────┤
│       POLÍTICAS DE TENANT           │  ← Configuráveis pelo Builder
│   (Específicas para sua clínica)    │
└─────────────────────────────────────┘
```

### Merge de Políticas

Quando um tenant tem políticas customizadas:

1. Políticas **globais** sempre aplicadas (não removíveis)
2. Políticas de **sistema** aplicadas (podem ser sobrescritas)
3. Políticas de **tenant** adicionadas ou sobrescrevem sistema

---

## IDs oficiais (Documentação MVP v0.5)

A API utiliza identificadores padronizados para constraints e políticas, conforme a documentação técnica:

### Constraints (C_*)

| ID | Descrição |
|----|-----------|
| `C_MEDICAL_NO_DIAGNOSIS` | Não fornecer diagnósticos médicos |
| `C_MEDICAL_NO_MED_CHANGE_POSTCARE` | Não alterar medicação em pós-atendimento |
| `C_COMPLIANCE_NO_PROMISE_OF_RESULTS` | Não prometer resultados de tratamentos |
| `C_SEQUENCE_AUTHORITY_BEFORE_PRICE` | Sequência: autoridade antes de preço |
| `C_OBJECTION_NO_PASSIVE_CLOSE` | Objeção: não fechar passivamente |
| `C_STYLE_AVOID_DOUBT_PHRASES` | Evitar frases de dúvida |

### Políticas (POL_*)

| ID | Descrição |
|----|-----------|
| `POL_ONB_001_PRESENTATION_OBJECTIVE_NAME_SEQUENCE` | Apresentação / objetivo / nome (onboarding) |
| `POL_QUAL_001_KEY_QUESTION_AND_PROFILE_CLASSIFICATION` | Pergunta-chave e classificação de perfil (qualificação) |
| `POL_OBJ_001_CLARIFY_OBJECTION_BEFORE_CLOSING` | Esclarecer objeção antes de fechar |
| `POL_PRICE_002_RANGE_FIRST_EXACT_ON_INSISTENCE` | Preço: faixa primeiro, exato sob insistência |
| `POL_SAFETY_001_EMERGENCY_POSTCARE_OUT_OF_HOURS` | Emergência pós-atendimento fora do horário |
| `POL_HANDOFF_001_USER_REQUESTS_HUMAN_OR_AI_DETECTED` | Handoff quando usuário pede humano ou IA detecta necessidade |

O Decision Trace e o evento `srm.decision` expõem `policy_refs` e `constraints` com esses IDs.

---

## Políticas Globais

Regras mandatórias para todos os tenants healthcare:

### Healthcare Safety (Obrigatórias)

```python
GLOBAL_RULES = [
    {
        "rule": "no_medical_diagnosis",
        "message": "Não fornecer diagnósticos médicos",
        "severity": "critical",
        "action": "block",
        "patterns": [
            "você tem", "isso é", "diagnóstico",
            "você está com", "parece ser"
        ]
    },
    {
        "rule": "no_medication_prescription",
        "message": "Não indicar ou recomendar medicamentos",
        "severity": "critical",
        "action": "block",
        "patterns": [
            "tome", "medicamento", "remédio",
            "comprimido", "dosagem", "mg"
        ]
    },
    {
        "rule": "no_result_promises",
        "message": "Não prometer resultados de tratamentos",
        "severity": "error",
        "action": "warn",
        "patterns": [
            "garanto", "prometo", "certeza que",
            "com certeza vai", "100%"
        ]
    },
    {
        "rule": "emergency_detection",
        "message": "Detectar situações de emergência médica",
        "severity": "critical",
        "action": "handoff",
        "patterns": [
            "emergência", "urgente", "dor forte",
            "não consigo respirar", "desmaiei",
            "sangramento", "acidente"
        ]
    },
    {
        "rule": "suicide_prevention",
        "message": "Detectar ideação suicida",
        "severity": "critical",
        "action": "handoff",
        "patterns": [
            "suicídio", "me matar", "acabar com tudo",
            "não quero mais viver", "vou me machucar"
        ]
    }
]
```

### Compliance (Obrigatórias)

```python
COMPLIANCE_RULES = [
    {
        "rule": "no_financial_advice",
        "message": "Não dar conselhos financeiros",
        "severity": "error",
        "action": "warn",
        "patterns": ["investir", "aplicação", "rendimento"]
    },
    {
        "rule": "data_privacy",
        "message": "Não solicitar dados sensíveis desnecessários",
        "severity": "warning",
        "action": "log",
        "patterns": ["cpf", "senha", "cartão de crédito"]
    }
]
```

---

## Políticas de Tenant

Regras customizáveis por cada tenant/clínica.

### Criar Política de Tenant

```bash
curl -X POST https://api.neurostack.ai/v1/policies \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "name": "Clinica Premium Policy",
    "description": "Regras específicas para a Clínica Premium",
    "policy_type": "tenant",
    "priority": 100,
    "is_active": true,
    "rules": [
      {
        "rule": "premium_greeting",
        "message": "Usar saudação premium",
        "severity": "info",
        "action": "log"
      }
    ]
  }'
```

### Exemplos de Políticas de Tenant

#### Clínica de Estética

```json
{
  "name": "Política Clínica Estética",
  "rules": [
    {
      "rule": "no_surgery_details",
      "message": "Não detalhar procedimentos cirúrgicos",
      "severity": "error",
      "action": "warn",
      "patterns": ["cortar", "incisão", "anestesia geral"]
    },
    {
      "rule": "redirect_pricing",
      "message": "Redirecionar perguntas de preço para atendimento",
      "severity": "info",
      "action": "flag",
      "patterns": ["quanto custa", "preço", "valor", "orçamento"]
    },
    {
      "rule": "collect_instagram",
      "message": "Coletar Instagram do lead",
      "severity": "info",
      "action": "log",
      "patterns": ["@", "instagram", "insta"]
    }
  ]
}
```

#### Clínica Odontológica

```json
{
  "name": "Política Clínica Odontológica",
  "rules": [
    {
      "rule": "emergency_dental",
      "message": "Detectar emergência odontológica",
      "severity": "critical",
      "action": "handoff",
      "patterns": ["dente quebrou", "sangue na boca", "abscesso", "inchaço"]
    },
    {
      "rule": "insurance_redirect",
      "message": "Redirecionar dúvidas de convênio",
      "severity": "info",
      "action": "flag",
      "patterns": ["convênio", "plano", "cobertura", "reembolso"]
    }
  ]
}
```

---

## Estrutura de Regras

### Schema Completo

```typescript
interface PolicyRule {
  // Identificação
  rule: string;              // ID único da regra (snake_case)
  message: string;           // Descrição legível
  
  // Severidade e Ação
  severity: "info" | "warning" | "error" | "critical";
  action: "log" | "warn" | "flag" | "block" | "handoff";
  
  // Triggers (opcional)
  patterns?: string[];       // Keywords que ativam a regra
  regex?: string[];          // Expressões regulares
  intent_match?: string[];   // Intents que ativam
  funnel_stage?: string[];   // Estágios do funil
  
  // Condições (opcional)
  conditions?: {
    min_confidence?: number; // Confiança mínima para ativar
    turn_count_min?: number; // Número mínimo de turnos
    turn_count_max?: number; // Número máximo de turnos
    channel?: string[];      // Canais específicos
  };
  
  // Metadata
  enabled?: boolean;         // Se a regra está ativa
  priority?: number;         // Prioridade (maior = primeiro)
}
```

### Exemplos de Estrutura

#### Regra Simples

```json
{
  "rule": "no_competitor_mention",
  "message": "Não mencionar concorrentes",
  "severity": "warning",
  "action": "warn",
  "patterns": ["outra clínica", "concorrente", "mais barato"]
}
```

#### Regra com Condições

```json
{
  "rule": "frustration_detection",
  "message": "Detectar frustração do usuário",
  "severity": "warning",
  "action": "flag",
  "patterns": ["não entende", "já falei", "repetindo"],
  "conditions": {
    "turn_count_min": 3,
    "min_confidence": 0.7
  }
}
```

#### Regra com Regex

```json
{
  "rule": "phone_collection",
  "message": "Usuário forneceu telefone",
  "severity": "info",
  "action": "log",
  "regex": ["\\(?\\d{2}\\)?\\s?\\d{4,5}-?\\d{4}"]
}
```

---

## Ações Disponíveis

| Ação | Comportamento | Uso Típico |
|------|---------------|------------|
| `log` | Apenas registra no event log | Coleta de informações |
| `warn` | Adiciona warning no prompt | Orientar LLM |
| `flag` | Marca sessão com flag | Ativar fluxos especiais |
| `block` | Impede ação e retorna mensagem padrão | Safety crítico |
| `handoff` | Inicia transferência para humano | Emergências |

### Detalhamento das Ações

#### `log` - Logging

```python
# Apenas registra a ocorrência
# Útil para analytics e tracking
{
  "action": "log",
  "effect": "Event logged for analytics"
}
```

#### `warn` - Warning

```python
# Adiciona constraint ao prompt
# LLM é orientado a evitar o comportamento
{
  "action": "warn",
  "effect": "Constraint added to prompt.constraints[]"
}
```

#### `flag` - Flag

```python
# Adiciona flag ao estado da sessão
# Pode ativar fluxos específicos
{
  "action": "flag",
  "effect": "state_patch.flags['{rule_name}'] = true"
}
```

#### `block` - Block

```python
# Bloqueia processamento
# Retorna mensagem padrão de safety
{
  "action": "block",
  "effect": "Returns safety response, skips LLM"
}
```

#### `handoff` - Handoff

```python
# Inicia transferência para humano
# Gera resumo para atendente
{
  "action": "handoff",
  "effect": "Triggers handoff flow with summary"
}
```

---

## Patterns e Triggers

### Keywords Simples

```json
{
  "patterns": ["agendar", "consulta", "marcar", "horário"]
}
```

Matching: case-insensitive, partial match

### Regex Avançado

```json
{
  "regex": [
    "\\b(quero|gostaria de|preciso)\\s+agendar\\b",
    "\\d{2}/\\d{2}/\\d{4}"
  ]
}
```

### Por Intent

```json
{
  "intent_match": ["scheduling_request", "cancellation_request"]
}
```

### Por Estágio do Funil

```json
{
  "funnel_stage": ["decision", "retention"]
}
```

### Combinações

```json
{
  "patterns": ["preço", "valor"],
  "funnel_stage": ["awareness", "consideration"],
  "conditions": {
    "turn_count_min": 2,
    "channel": ["whatsapp", "instagram"]
  }
}
```

---

## Exemplos Práticos

### Exemplo 1: Coleta de Lead

```json
{
  "name": "Lead Collection Policy",
  "rules": [
    {
      "rule": "collect_name",
      "message": "Nome do lead coletado",
      "severity": "info",
      "action": "log",
      "patterns": ["me chamo", "meu nome é", "sou o", "sou a"]
    },
    {
      "rule": "collect_phone",
      "message": "Telefone do lead coletado",
      "severity": "info",
      "action": "flag",
      "regex": ["\\(?\\d{2}\\)?\\s?9?\\d{4}-?\\d{4}"]
    },
    {
      "rule": "collect_email",
      "message": "Email do lead coletado",
      "severity": "info",
      "action": "flag",
      "regex": ["[\\w.-]+@[\\w.-]+\\.\\w+"]
    }
  ]
}
```

### Exemplo 2: Qualificação de Lead

```json
{
  "name": "Lead Qualification Policy",
  "rules": [
    {
      "rule": "high_intent",
      "message": "Lead com alta intenção de compra",
      "severity": "info",
      "action": "flag",
      "patterns": ["quero fechar", "vamos agendar", "pode ser amanhã"],
      "intent_match": ["scheduling_confirmation"]
    },
    {
      "rule": "budget_concern",
      "message": "Lead com objeção de preço",
      "severity": "info",
      "action": "flag",
      "patterns": ["caro", "não tenho", "muito valor", "parcelar"]
    },
    {
      "rule": "competitor_mention",
      "message": "Lead mencionou concorrente",
      "severity": "warning",
      "action": "flag",
      "patterns": ["outra clínica", "vi mais barato", "concorrente"]
    }
  ]
}
```

### Exemplo 3: Safety Personalizado

```json
{
  "name": "Custom Safety Policy",
  "rules": [
    {
      "rule": "anxiety_detection",
      "message": "Detectado alto nível de ansiedade",
      "severity": "warning",
      "action": "flag",
      "patterns": ["muito nervoso", "ansioso", "não consigo dormir", "preocupado demais"]
    },
    {
      "rule": "dissatisfaction_escalation",
      "message": "Escalação por insatisfação",
      "severity": "error",
      "action": "handoff",
      "patterns": ["reclamar", "procon", "advogado", "processo", "nunca mais volto"],
      "conditions": {
        "turn_count_min": 3
      }
    }
  ]
}
```

---

## API de Políticas

### Criar Política

```bash
POST /v1/policies
```

```json
{
  "tenant_id": "uuid",
  "name": "Nome da Política",
  "description": "Descrição",
  "policy_type": "tenant",
  "rules": [...],
  "priority": 100,
  "is_active": true
}
```

### Autorização por Tenant

`tenant_id` nunca autoriza sozinho: API keys e `clinic_admin` só acessam o
próprio tenant, `builder` só clínicas que possui, `super_admin` pode escolher
qualquer tenant, e outros papéis JWT recebem `403`. Tenant alheio responde
`403` (`tenant_override_forbidden`). Detalhes em
[api-contracts.md](api-contracts.md#policies).

### Listar Políticas

```bash
GET /v1/policies?tenant_id={uuid}&include_global=true
```

Sem `tenant_id`, lista o tenant do chamador mais a política global.

### Ver Política Específica

```bash
GET /v1/policies/{policy_id}
```

### Atualizar Política

```bash
PATCH /v1/policies/{policy_id}?tenant_id={uuid}
```

### Deletar Política

```bash
DELETE /v1/policies/{policy_id}?tenant_id={uuid}
```

### Ver Políticas Efetivas (Merged)

```bash
GET /v1/policies/tenant/{tenant_id}/effective
```

Retorna todas as políticas aplicáveis ao tenant após merge.

---

## Troubleshooting

### Política não está sendo aplicada

1. **Verificar se está ativa:**
   ```bash
   GET /v1/policies/{id}
   # Check: is_active = true
   ```

2. **Verificar patterns:**
   - Patterns são case-insensitive
   - Partial match é usado
   - Verificar typos

3. **Verificar prioridade:**
   - Políticas com maior priority são avaliadas primeiro
   - Políticas globais não podem ser sobrescritas

### Muitos falsos positivos

1. **Refinar patterns:**
   - Usar patterns mais específicos
   - Considerar usar regex
   - Adicionar conditions

2. **Ajustar severidade:**
   - Usar `info` ou `warning` para casos menos críticos

3. **Usar intent_match:**
   - Mais preciso que patterns de texto

### Regra não detecta o esperado

1. **Testar patterns isoladamente:**
   ```python
   import re
   pattern = "seu_pattern"
   text = "texto de teste"
   if pattern.lower() in text.lower():
       print("Match!")
   ```

2. **Verificar logs:**
   - Eventos de política são logados
   - Verificar se a regra está sendo avaliada

---

## Referências

- [API Contracts](./api-contracts.md)
- [Builder Integration Guide](./builder-integration-guide.md)
- [Troubleshooting](./troubleshooting.md)

---

*Última atualização: Janeiro 2026*

