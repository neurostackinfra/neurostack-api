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
6. [Modalidades dos serviços](#modalidades-dos-serviços)
7. [Uso no pipeline](#uso-no-pipeline)

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

## Modalidades dos serviços

O endpoint administrativo `PATCH /v1/config` recebe um corpo **plano**.
`service_format` é o formato global da clínica e `services` substitui o array
inteiro. Cada item de serviço pode ser um objeto extensível ou uma string
legada. Em objetos, `service_format` aceita somente `online`, `presential` ou
`both`; a ausência e `null` permanecem compatíveis com dados existentes.
Omitir `services` preserva o catálogo atual; `services: []` o substitui por uma
lista vazia; e `services: null` persiste `null`. GET e PATCH retornam o último
caso como `services: []`, pois a resposta plana apresenta o catálogo como
array. Strings legadas, por exemplo `"Retorno"`, continuam aceitas.

```json
{
  "service_format": "both",
  "services": [
    {
      "name": "Consulta por vídeo",
      "service_format": "online",
      "frontend_metadata": {"color": "blue"}
    },
    "Retorno"
  ]
}
```

O armazenamento usa a representação aninhada `operations.service_format` e a
coluna JSON `services`; ela não é o formato do request da API. A resposta 200
também é plana e preserva os campos extras do item:

```json
{
  "service_format": "both",
  "services": [
    {
      "name": "Consulta por vídeo",
      "service_format": "online",
      "frontend_metadata": {"color": "blue"}
    },
    "Retorno"
  ]
}
```

Um serviço incompatível retorna 422 com um detalhe estável, sem ecoar nomes ou
descrições de serviços:

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

### Compatibilidade e fallback no contexto da IA

| Formato global canônico | Modalidades permitidas nos objetos de serviço | Fallback para serviço sem modalidade |
|-------|-------|-------|
| `online` | `online` | `online` |
| `presential` | `presential` | `presential` |
| `both` | `online`, `presential`, `both` | nenhum |
| vazio ou histórico | não impõe validação cruzada | nenhum |

A modalidade explícita do serviço sempre prevalece sobre a global. O contexto
da IA não infere canal para `both`, formato vazio ou legado e não declara
disponibilidade, agendamento ou autorização. A validação cruzada só roda em
PATCH que contenha `services` ou `service_format`; um PATCH sem esses campos
não reinterpreta dados históricos. `services: []` grava uma lista vazia e
`services: null` é mantido como nulo na persistência, embora GET/PATCH o
retornem como `services: []`.

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
    },
    "operations": {
      "service_format": "both"
    },
    "services": [
      {
        "name": "Consulta por vídeo",
        "service_format": "online"
      },
      {
        "name": "Avaliação presencial",
        "service_format": "presential"
      }
    ]
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

## Credenciais e catálogo público de provedores

As credenciais de provedores pertencem à fronteira dedicada
`/v1/config/api-credentials`. A rota genérica de configuração não aceita
campos `llm_*`. Leitura e escrita exigem JWT de `clinic_admin`, `builder` ou
`super_admin`; chaves de API de tenant não podem administrar credenciais. Um
builder só pode selecionar uma clínica que pertence ao seu próprio
`builder_id`. Segredos nunca são retornados: a resposta expõe apenas o estado
`*_set` e a máscara de tamanho constante.

O catálogo público atual é:

| Capacidade | Configuráveis | Ainda `coming_soon` |
|---|---|---|
| WhatsApp | Meta Cloud API (`meta_cloud`), Z-API (`zapi`), Uazapi (`uazapi`) e Evolution API (`evolution`) | 360dialog (`dialog360`) e Twilio (`twilio`) |
| LLM | Dropout (`dropout`), Gemini (`gemini`), GPT/OpenAI (`openai`) e Outros (`outros`) | SLM Cortex (`slm_cortex`) |
| STT | Whisper/OpenAI (`openai_whisper`), Scribe/ElevenLabs (`elevenlabs_scribe`) e Outros by OpenRouter (`openrouter`) | Google Cloud STT (`google_stt`), Deepgram (`deepgram_stt`) e Azure STT (`microsoft_stt`) |
| TTS | ElevenLabs (`elevenlabs`), Google Cloud TTS (`google`) e OpenAI TTS (`openai`) | — |
| Fallback multimodal | — | OpenRouter (`openrouter`) |

Em STT, o card público “Outros by OpenRouter” usa o ID explícito
`openrouter`. O ID genérico `outros` continua reconhecido somente para leitura e
migração de configurações legadas; ele não aparece como opção para uma nova
configuração.

Evolution e Uazapi aceitam hosts configuráveis e, por isso, não podem herdar o
segredo associado a uma URL anterior. Entrar ou trocar para um desses providers
exige o bundle completo no mesmo PATCH: `whatsapp_provider`, URL, nome da
instância e uma chave/token nova. Alterar uma `evolution_api_url` ou
`uazapi_api_url` para outro valor não vazio exige o mesmo bundle e a seleção
explícita do provider, inclusive quando os campos estão dormentes sob outro
provider. Um patch apenas de nome da instância continua válido quando provider
e endpoint não mudam. Ao sair ou descomissionar a integração, a URL pode ser
limpa explicitamente com `null` sem reapresentar o segredo antigo.

O contrato compartilhado de LLM usa exatamente quatro campos:

| Campo | Regra |
|-------|-------|
| `llm_provider` | Identificador wire: `dropout`, `gemini`, `openai` ou `outros` no catálogo público atual |
| `llm_api_url` | Base HTTPS do provedor |
| `llm_api_key` | Segredo write-only |
| `llm_model_name` | Modelo habilitado pelo provedor |

Para Gemini, a única base aceita é
`https://generativelanguage.googleapis.com/v1beta/openai`. Host, caminho e
ausência de porta explícita são fixos: até `:443` é recusado. Query, fragmento,
userinfo, outro host ou outro caminho também são recusados. Não existe override
por variável de ambiente ou tenant para a base, o prompt do smoke ou a política
de saída. O transporte resolve DNS na fronteira, valida todos os endereços como
públicos, fixa o IP da conexão e preserva
`generativelanguage.googleapis.com` em Host/SNI. Redirects são proibidos.

`llm_provider=outros` é o campo aberto para provedores com API compatível com
OpenAI, inclusive OpenRouter. A `llm_api_url` deve ser uma base HTTPS pública e
passa pelas mesmas proteções de saída segura; portanto, um provedor compatível
não precisa ganhar um card próprio para poder ser usado por esse caminho.

Qualquer entrada ou troca para um provider LLM ativo, assim como uma mudança de
`llm_api_url`, exige que os quatro campos sejam enviados juntos com chave nova.
Um patch parcial falha atomicamente com `provider_switch_bundle_required`;
nenhum campo é gravado. Quando provider e endpoint não mudam, um patch apenas
de modelo preserva a URL e a chave existentes. Valores vazios, `null` e máscaras
(`••••…` ou `****…`) nunca apagam nem substituem um segredo salvo; numa troca de
provider ou endpoint eles não contam como chave nova.

Um `llm_provider` presente na clínica é autoritativo mesmo se estiver incompleto
ou inválido. A herança do builder só ocorre quando a clínica não
tem o campo `llm_provider` e existe exatamente um tenant proprietário autorizado
do builder com bundle operacional completo. A resolução não carrega segredos de
outro tenant quando a clínica já declarou provider. No RLHF, `clinic_tenant_id`
e o tenant do `message_id` precisam existir, concordar e ser autorizados para o
clinic admin, builder ou tenant API key antes de qualquer leitura de segredo ou
chamada externa.

Erros públicos são estáveis e não incluem entrada ou segredo:

- `invalid_provider_endpoint`: a base não corresponde ao endpoint canônico de
  um provider fixo (Gemini em LLM; Scribe ou OpenRouter em STT);
- `provider_switch_bundle_required`: entrada ou troca para Evolution/Uazapi,
  mutação não vazia de seus endpoints configuráveis, ou entrada/troca/mudança
  de endpoint em LLM/STT/TTS sem todos os campos obrigatórios e uma nova
  chave/token;
- `provider_not_available`: provider reconhecido, mas indisponível;
- `invalid_provider_for_capability`: wire id desconhecido para a capacidade
  informada;
- `invalid_credentials_payload` / `unsafe_api_url`: payload ou URL recusado na
  fronteira genérica.

### Limitação beta e smoke sanitizado

O endpoint compatível com OpenAI do Gemini está em `v1beta`. Gemini permanece
publicamente configurável, agora junto de Dropout, OpenAI e `outros`. Para o
fluxo de release específico do Gemini, essa disponibilidade de produto não
substitui o gate operacional: apenas o sucessor receipt-only assinado do
candidate homologado passa o gate de produção.

O smoke independente usa somente duas entradas de ambiente privadas:

```bash
docker run --rm --read-only --tmpfs /tmp \
  --env GEMINI_API_KEY --env GEMINI_MODEL "$image_digest" \
  python -m src.llm.gemini_smoke --json \
  --tested-commit "$tested_commit" --image-digest "$image_digest" --run-id "$run_id"
```

A base e o prompt são fixos. A saída é uma única linha JSON canônica contendo
somente `schema`, `result`, `result_code`, `validation_kind`,
`validation_authority`, `timestamp`, `run_id`, `tested_commit`, `image_digest`,
`smoke_module_sha256` e `exit_code`. Ela nunca
imprime chave, prompt, resposta do provedor ou corpo de exceção. Classes de falha
incluem `missing_api_key`, `missing_model`, `provider_http_error`,
`provider_transport_error`, `invalid_provider_response`, `unexpected_response`
e `internal_error`.
Uma resposta sem a estrutura OpenAI esperada (`choices[].message.content`) é
`invalid_provider_response`; uma resposta estruturalmente válida cujo conteúdo
está vazio ou não pode ser usado é `unexpected_response`.

Para troubleshooting do gate de release:

1. `missing_api_key`/`missing_model`: corrija apenas o secret store privado;
2. `provider_http_error`: confirme acesso da chave ao modelo, sem copiar a
   resposta do provedor para logs ou tickets;
3. `provider_transport_error`: confirme DNS e HTTPS de saída para o host fixo;
4. `invalid_provider_response`/`unexpected_response`: confirme a compatibilidade
   beta do modelo;
5. smoke verde não libera sozinho: Operations precisa embutir os bytes e SHA no
   receipt, criar o sucessor assinado receipt-only e manter esse commit como HEAD.

## STT: Scribe e OpenRouter no catálogo público

Os IDs wire explícitos são `elevenlabs_scribe` (ElevenLabs Scribe) e
`openrouter` (OpenRouter). Ambos estão no catálogo público e podem ser
selecionados por tenants novos ou existentes. `outros` permanece um adapter
legado distinto e oculto: não é alias, rótulo alternativo nem fallback de
OpenRouter. Configurações legadas continuam legíveis e podem migrar para um dos
IDs explícitos sem que o card antigo volte ao catálogo.

A promoção pública habilita seleção e persistência das credenciais; ela não é
homologação live de chave, quota, modelo ou formato em produção. Os mocks
determinísticos provam o contrato local de configuração e transporte. Nenhum
probe live foi executado ou é autorizado por esta documentação.

### Bundle operacional e compatibilidade legado

Uma entrada ou troca para qualquer provider STT ativo, assim como uma mudança de
`stt_api_url`, exige no mesmo PATCH um bundle fresco com `stt_provider`,
`stt_api_url`, `stt_api_key` e `stt_model_name`. A chave precisa ser nova;
máscara, vazio e `null` não contam. Quando provider e endpoint não mudam, uma
edição apenas de modelo ou controle pode ser parcial. Para Scribe ou OpenRouter,
a URL precisa ser a base canônica da tabela abaixo. `audio_max_seconds` não
integra o bundle e mantém o default 60 quando ausente.

Um incumbent legado resolvido como `outros` pode repetir exatamente sua visão
canônica sem reescrever o JSON persistido ou migrar com um bundle fresco para
`elevenlabs_scribe` ou `openrouter`. `outros` não pode ser escolhido para uma
configuração nova nem reintroduzido pelo catálogo público.

### Bases, paths e wire contract

| ID | Base persistida exata | Sufixo POST | Autenticação | Campos multipart |
|---|---|---|---|---|
| `elevenlabs_scribe` | `https://api.elevenlabs.io/v1` | `/speech-to-text` | `xi-api-key: <secret>` | `file`, `model_id=<stt_model_name>` |
| `openrouter` | `https://openrouter.ai/api/v1` | `/audio/transcriptions` | `Authorization: Bearer <secret>` | `file`, `model=<stt_model_name>`, `language` quando informado |

As bases são literais: porta explícita (inclusive `:443`), query, fragmento,
userinfo, host/path alternativo ou URL já contendo o sufixo são recusados. O
modelo não é hardcoded: `stt_model_name` é obrigatório e é repassado no campo
próprio de cada provedor.

Ambos enviam os bytes originais em multipart `file`; este contrato escolhe
multipart e não JSON/base64. O MIME recebido é normalizado para lowercase e sem
parâmetros válidos antes do envio (por exemplo,
`audio/ogg; codecs=opus` vira `audio/ogg`), preservando os bytes. Os tipos
fechados são `audio/wav`, `audio/mpeg`, `audio/flac`, `audio/mp4`, `audio/ogg`,
`application/ogg`, `audio/webm` e `audio/aac`; tipo ou parâmetro malformado é
recusado antes da rede. A resposta deve ser JSON objeto e o transcript vem
exclusivamente de `.text`, após trim; texto ausente/inválido ou vazio falha.

A taxonomia pública/configuração permanece fechada:
`invalid_provider_endpoint`, `provider_switch_bundle_required`,
`provider_not_available`, `invalid_provider_for_capability`,
`invalid_credentials_payload` e `unsafe_api_url`. A fronteira de transporte
usa somente `stt_provider_unsupported`, `unsupported_audio_mime`,
`stt_configuration_invalid`,
`stt_provider_http_error`, `stt_provider_transport_error`,
`stt_malformed_response` e `stt_empty_transcript`. Corpo, header, URL, modelo e
segredo externos não são encadeados nesses erros.

Referências oficiais: [OpenRouter — transcription](https://openrouter.ai/blog/tutorials/transcription-on-openrouter/)
e [ElevenLabs — speech-to-text convert](https://elevenlabs.io/docs/api-reference/speech-to-text/convert).

---

*Documentação mantida pela equipe NeuroStack*
