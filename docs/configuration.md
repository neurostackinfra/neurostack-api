# Configuração do Tenant e da Clínica

**Versão:** 1.0  
**Alinhamento:** Documentação Técnica MVP v0.5

Este documento descreve onde e como as informações de configuração do tenant e da clínica (ClinicConfig) são armazenadas, a estrutura dos dados e um exemplo completo em JSON.

---

## Índice

1. [Onde as configurações são armazenadas](#onde-as-configurações-são-armazenadas)
2. [Estrutura do config do tenant](#estrutura-do-config-do-tenant)
3. [ClinicConfig (configuração da clínica)](#clinicconfig-configuração-da-clínica)
4. [Valores permitidos (enums)](#valores-permitidos-enums)
5. [Exemplo completo](#exemplo-completo)
6. [Uso no pipeline](#uso-no-pipeline)

---

## Onde as configurações são armazenadas

### ClinicConfig (modelo fixo) — tabela SQL

A configuração da clínica (**ClinicConfig**) é um modelo fixo armazenado no **banco SQL**, na tabela **`clinic_configs`** (uma linha por tenant, relação 1:1).

| O quê | Onde |
|-------|------|
| **Persistência** | Tabela **`clinic_configs`** (colunas: `id`, `tenant_id`, `clinic_id`, `version`, `persona`, `scheduling`, `pricing`, `post_care`, `handoff` em JSONB; `created_at`, `updated_at`) |
| **Modelo ORM** | `src/models/clinic_config_table.py` → `ClinicConfigTable` |
| **Modelo Pydantic** | `src/models/clinic_config.py` → `ClinicConfig` |
| **Acesso no código** | `tenant.get_clinic_config()` retorna `ClinicConfig` ou `None` (lê primeiro da tabela, depois fallback em `config` JSON) |
| **Repositório** | `TenantRepository.upsert_clinic_config(tenant_id, clinic_config)`, `get_by_id_with_clinic_config(tenant_id)`, `find_by_api_key_hash_with_clinic_config(api_key_hash)` |

**Retrocompatibilidade:** Se não existir linha em `clinic_configs` para o tenant, `get_clinic_config()` usa o valor em `tenants.config["clinic_config"]` (JSON), quando existir.

### Config geral do tenant (rate limit, webhook, etc.)

A configuração **geral** do tenant (rate limit, webhook, features, branding) continua na tabela **`tenants`**, coluna **`config`** (JSONB).

| O quê | Onde |
|-------|------|
| **Persistência** | Tabela `tenants`, coluna `config` (JSONB) |
| **API / contratos** | `src/api/schemas/tenants.py` (TenantConfig com campo `clinic_config`) |

Cada **tenant** representa um builder ou clínica. A configuração geral fica em `tenants.config`; a configuração da clínica (ClinicConfig) fica na tabela `clinic_configs`.

---

## Estrutura do config do tenant

O campo `config` do tenant é um objeto JSON que pode conter:

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `description` | string | Descrição do tenant/clínica |
| `rate_limit` | int | Requisições por minuto (ex.: 100) |
| `session_ttl_seconds` | int | TTL da sessão em segundos (ex.: 86400) |
| `max_sessions_per_user` | int | Máximo de sessões ativas por usuário |
| `webhook_url` | string | URL para webhooks (handoff, eventos) |
| `features` | object | `rag_enabled`, `handoff_enabled`, `safety_strict_mode` |
| `branding` | object | `primary_color`, `logo_url`, etc. |
| `custom_settings` | object | Configurações customizadas |
| **`clinic_config`** | **object** | (Legado/fallback.) Configuração da clínica; o valor canônico fica na tabela `clinic_configs`. Ver abaixo. |

---

## ClinicConfig (configuração da clínica)

A chave `clinic_config` contém a configuração usada pelo SRM, Policy, CTX e Handoff (documentação MVP v0.5).

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `clinic_id` | UUID (opcional) | Identificador da clínica |
| `version` | int | Versão do schema (ex.: 1) |
| **persona** | object | Nome da clínica, do agente, uso de nome pessoal |
| **scheduling** | object | Agendamento (agente, chave RAG de horários) |
| **pricing** | object | Preços (modo de divulgação, tabelas, condições) |
| **post_care** | object | Pós-atendimento (instruções, avisos de emergência) |
| **handoff** | object | Handoff (modo padrão, SLAs por setor) |

### persona

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `clinic_display_name` | string | Nome exibido da clínica |
| `agent_display_name` | string | Nome exibido do assistente virtual |
| `use_personal_name` | boolean | Usar nome pessoal (ex.: “Dra. Ana”) |

### scheduling

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `has_scheduling_agent` | boolean | Se há agente de agendamento |
| `hours_rag_key` | string | Chave RAG para conteúdo de horários de atendimento |

### pricing

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `disclosure_mode` | enum | `consult_only`, `range_allowed`, `exact_allowed`, `no_prices` |
| `range_table_available` | boolean | Tabela de faixas disponível |
| `exact_table_available` | boolean | Tabela de valores exatos disponível |
| `range_format` | string | Formato de exibição da faixa (ex.: "A partir de R$ {min}") |
| `special_conditions_note_enabled` | boolean | Exibir nota de condições especiais |

### post_care

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `standard_instructions_rag_key` | string | Chave RAG para instruções pós-atendimento |
| `emergency_warning_text_enabled` | boolean | Exibir aviso de emergência |

### handoff

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `default_mode` | enum | `whatsapp_human_takeover`, `crm_task_create` |
| `sector_defaults` | object | SLA por setor (ver [Valores permitidos](#valores-permitidos-enums)) |

**Setores em sector_defaults:** `agendamento`, `financeiro`, `administrativo`, `gerencia`, `clinico`, `urgencia`, `fidelizacao`.

---

## Valores permitidos (enums)

### pricing.disclosure_mode

| Valor | Descrição |
|-------|-----------|
| `consult_only` | Só informar que valores são sob consulta |
| `range_allowed` | Pode informar faixa de preços |
| `exact_allowed` | Pode informar valores exatos |
| `no_prices` | Não divulgar preços |

### handoff.default_mode

| Valor | Descrição |
|-------|-----------|
| `whatsapp_human_takeover` | Transferência para humano (ex.: WhatsApp) |
| `crm_task_create` | Criar tarefa no CRM |

### handoff.sector_defaults (SLA por setor)

| Valor | Descrição |
|-------|-----------|
| `imediato` | Resposta imediata |
| `prioritario` | Prioridade alta |
| `rotina` | Rotina |

---

## Exemplo completo

Um exemplo completo de `config` do tenant (incluindo `clinic_config`) está em:

**[examples/tenant-config-with-clinic.json](./examples/tenant-config-with-clinic.json)**

Trecho principal:

```json
{
  "description": "Clínica de Dermatologia e Estética - atendimento via WhatsApp",
  "rate_limit": 100,
  "session_ttl_seconds": 86400,
  "max_sessions_per_user": 10,
  "webhook_url": "https://api.minha-clinica.com/webhooks/neurostack",
  "features": {
    "rag_enabled": true,
    "handoff_enabled": true,
    "safety_strict_mode": true
  },
  "branding": {
    "primary_color": "#1a365d",
    "logo_url": "https://cdn.minha-clinica.com/logo.png"
  },
  "custom_settings": {},
  "clinic_config": {
    "clinic_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "version": 1,
    "persona": {
      "clinic_display_name": "Clínica Saúde & Beleze",
      "agent_display_name": "Assistente Virtual",
      "use_personal_name": false
    },
    "scheduling": {
      "has_scheduling_agent": true,
      "hours_rag_key": "horarios-atendimento"
    },
    "pricing": {
      "disclosure_mode": "range_allowed",
      "range_table_available": true,
      "exact_table_available": false,
      "range_format": "A partir de R$ {min} (consulta tipo {tipo})",
      "special_conditions_note_enabled": true
    },
    "post_care": {
      "standard_instructions_rag_key": "pos-atendimento-padrao",
      "emergency_warning_text_enabled": true
    },
    "handoff": {
      "default_mode": "whatsapp_human_takeover",
      "sector_defaults": {
        "agendamento": "prioritario",
        "financeiro": "rotina",
        "administrativo": "rotina",
        "gerencia": "prioritario",
        "clinico": "prioritario",
        "urgencia": "imediato",
        "fidelizacao": "rotina"
      }
    }
  }
}
```

Use esse JSON como corpo do objeto `config` ao criar ou atualizar um tenant. O bloco `clinic_config` é o que o NeuroStack utiliza para persona, agendamento, preços, pós-atendimento e handoff por setor.

---

## Uso no pipeline

- O **PipelineInput** aceita `clinic_config` opcional (dict). Em produção, carregue o tenant com `get_by_id_with_clinic_config(tenant_id)` ou `find_by_api_key_hash_with_clinic_config(api_key_hash)` e use `tenant.get_clinic_config().model_dump()` para obter o dict (ou `None`).
- O **SRM**, **Policy** e **CTX** recebem `clinic_config` quando disponível e usam para decisões de intent, políticas de preço e handoff (setor/SLA).

## Migração e persistência

- **Criar/atualizar ClinicConfig:** use `TenantRepository.upsert_clinic_config(tenant_id, clinic_config: ClinicConfig)` para gravar na tabela `clinic_configs`.
- **Migração Alembic:** revisão `002` cria a tabela `clinic_configs`. Execute `alembic upgrade head` para aplicar.

---

*Documentação mantida pela equipe NeuroStack*
