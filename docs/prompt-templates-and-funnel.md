# Templates de Prompt e Funil Canônico

**Versão:** 1.0  
**Data:** Fevereiro 2026  
**Alinhamento:** Documentação Técnica MVP v0.5 (funil 11 etapas)

---

## Índice

1. [Visão geral](#visão-geral)
2. [Seleção de template por modo de fluxo](#seleção-de-template-por-modo-de-fluxo)
3. [Template Normal e as 11 etapas do funil](#template-normal-e-as-11-etapas-do-funil)
4. [Template Safety](#template-safety)
5. [Template Handoff](#template-handoff)
6. [Fluxo: funil → modo → template](#fluxo-funil--modo--template)
7. [Variáveis de contexto por etapa](#variáveis-de-contexto-por-etapa)

---

## Visão geral

O **Prompt Engine** escolhe o template com base no **modo de fluxo** (NORMAL, RAG, SAFETY, HANDOFF), definido após a decisão do SRM e do Flow Manager. O **funil canônico** (11 etapas) entra principalmente no **Template Normal**, que injeta instruções específicas por estágio. Os templates **Safety** e **Handoff** são usados quando o modo de fluxo é segurança ou handoff, independentemente da etapa do funil (embora o `funnel_stage` continue disponível nas variáveis).

| Modo de fluxo | Template usado | Uso do funil |
|---------------|----------------|--------------|
| `normal`     | **NormalTemplate** | Instruções específicas por etapa (FUNNEL_INSTRUCTIONS) |
| `rag`        | **NormalTemplate** | Mesmo que normal + bloco de conhecimento (knowledge_context) |
| `safety`     | **SafetyTemplate** | Não varia por etapa; usa safety_category (clinical_risk, legal_risk, conduct_risk, emergency) |
| `handoff`    | **HandoffTemplate** | Não varia por etapa; usa handoff_reason (user_request, safety_concern, escalation, etc.) |

---

## Seleção de template por modo de fluxo

O pipeline cognitivo determina o **modo efetivo** na seguinte ordem de prioridade:

1. **Handoff** – se `handoff_required == True`
2. **Safety** – se `safety_concern == True`
3. **RAG** – se `needs_rag == True`
4. **Normal** – caso contrário

O **PromptRenderer** mapeia modo → template:

```text
PromptMode.NORMAL  → NormalTemplate()
PromptMode.RAG     → NormalTemplate()   # RAG = Normal + knowledge_context
PromptMode.SAFETY  → SafetyTemplate()
PromptMode.HANDOFF → HandoffTemplate()
```

Ou seja: apenas dois “tipos” de prompt (Normal e Safety/Handoff). O funil impacta diretamente só o **NormalTemplate**.

---

## Template Normal e as 11 etapas do funil

O **NormalTemplate** (`src/prompt/templates/normal.py`) usa o dicionário **FUNNEL_INSTRUCTIONS**, que associa cada etapa do funil a um bloco de instruções incluído no prompt do sistema.

### Mapeamento: etapa do funil → instruções no prompt

| # | Etapa do funil           | Chave em FUNNEL_INSTRUCTIONS | Instruções resumidas |
|---|--------------------------|------------------------------|----------------------|
| 1 | `lead_novo`              | `lead_novo`                  | Primeiro contato; verificar se já é paciente; acolher; apresentar nome e clínica; perguntar objetivo e depois nome. |
| 2 | `em_qualificacao`        | `em_qualificacao`            | Fluxo de venda em andamento; sequência de perguntas para qualificação; fazer a Pergunta-Chave sobre experiência prévia (perfil Novidade/Decepção/Continuidade). |
| 3 | `qualificado`            | `qualificado`                | Lead pronto para decisão de agendamento; direcionar para agendamento ou esclarecer dúvidas. |
| 4 | `followup`               | `followup`                   | Reengajamento após qualificado; retomar intenção, resolver objeções, conduzir para agendamento. |
| 5 | `agendamento_solicitado` | `agendamento_solicitado`      | Usuário informou preferências (dia/turno); confirmar se finalizou agendamento. |
| 6 | `agendado`               | `agendado`                   | Pessoa confirmou agendamento; dúvidas operacionais (confirmar, cancelar) ou perguntar se já foi atendido. |
| 7 | `paciente_ativo`         | `paciente_ativo`             | Usuário já é paciente; nova venda/qualificação ou dúvidas operacionais. |
| 8 | `pos_atendimento`        | `pos_atendimento`            | Dúvidas ou suporte após atendimento; identificar riscos (safety_concern) e encaminhar se necessário. |
| 9 | `fidelizacao`            | `fidelizacao`                | Reengajamento; handoff ou agente de fidelização. |
|10 | `inativo`                | `inativo`                    | Sem interação por x dias; handoff ou agente de inativo. |
|11 | `standby`                | `standby`                    | Automação desligada; atendimento 100% humano. |

A variável `variables.funnel_stage` (valor canônico em minúsculas, ex.: `lead_novo`, `em_qualificacao`) é usada para escolher o bloco. Se não houver chave correspondente, nenhum bloco de funil é adicionado (o template ainda usa system prompt base + restrições + histórico).

O NormalTemplate também pode adicionar **instruções por intent** (`last_intent`), por exemplo: `scheduling_request`, `question_price`, `cancellation_request`, `complaint_general`. Essas se somam às instruções da etapa do funil.

---

## Template Safety

O **SafetyTemplate** é usado quando o modo de fluxo é **SAFETY** (preocupação de segurança detectada pelo SRM/Safety Detector). Não há variação por etapa do funil; a variação é por **categoria de segurança**:

| Categoria      | Uso no prompt |
|----------------|----------------|
| `clinical_risk` | Risco à saúde ou à vida; manter calma, fornecer CVV 188, SAMU 192, emergência; não ser terapeuta; confirmar acesso a ajuda. |
| `legal_risk`    | Implicações legais; não dar orientação médica/jurídica; direcionar a profissionais; tom profissional e claro. |
| `conduct_risk`  | Comportamento inadequado; manter profissionalismo; não responder a provocações; redirecionar ou oferecer encerrar/transferir. |
| `emergency`     | Emergência detectada; números de emergência; escalação para humano obrigatória. |

O Safety pode ser acionado em **qualquer** etapa do funil (ex.: `pos_atendimento` + sintomas preocupantes → safety; `lead_novo` + menção a medicamento → safety). O `funnel_stage` continua nas variáveis, mas o SafetyTemplate não define instruções por etapa.

---

## Template Handoff

O **HandoffTemplate** é usado quando o modo de fluxo é **HANDOFF** (transferência para humano). A variação é por **motivo do handoff** (`handoff_reason`), não por etapa do funil:

| Motivo                | Uso no prompt |
|-----------------------|----------------|
| `user_request` / `explicit_request` | Usuário pediu atendente; confirmar, informar que está transferindo, perguntar o que o atendente deve saber. |
| `safety_concern`      | Transferência por segurança; manter calma; informar que um especialista irá ajudar; não minimizar. |
| `max_turns_exceeded`  | Complexidade; reconhecer necessidade de atenção especial; preparar resumo para o atendente. |
| `low_confidence`      | Incerteza; pedir desculpas; explicar que um atendente ajudará melhor; coletar contexto. |
| `complex_query`       | Consulta complexa; reconhecer complexidade; informar que especialista irá auxiliar; preparar contexto. |
| `escalation`          | Reclamação/supervisor; levar a sério; não ser defensivo; confirmar pontos de insatisfação. |

O handoff pode ocorrer em **qualquer** etapa (ex.: `lead_novo` com pedido explícito de humano → HandoffTemplate; `pos_atendimento` com sintomas → primeiro Safety, depois Handoff). O template não muda por `funnel_stage`.

---

## Fluxo: funil → modo → template

Resumo do fluxo até a escolha do template:

```text
[Estado da sessão]
  funnel_stage (11 etapas) + intent + safety_concern + handoff_required + needs_rag
           ↓
[SRM + Flow]
  define modo efetivo: handoff | safety | rag | normal
           ↓
[PromptRenderer]
  modo → template: HANDOFF → HandoffTemplate, SAFETY → SafetyTemplate,
         NORMAL/RAG → NormalTemplate
           ↓
[NormalTemplate apenas]
  variables.funnel_stage → FUNNEL_INSTRUCTIONS[funnel_stage]
  variables.last_intent  → instruções por intent
  (RAG: variables.knowledge_context também injetado)
```

Exemplos:

- **lead_novo** + mensagem normal → modo `normal` → **NormalTemplate** com instruções de **Lead Novo**.
- **em_qualificacao** + pedido de preço → modo `normal` → **NormalTemplate** com instruções de **Em Qualificação** + intent de preço.
- **pos_atendimento** + sintomas preocupantes → modo `safety` (e depois handoff) → **SafetyTemplate** (e depois **HandoffTemplate**); funil não altera o texto desses templates.
- **qualificado** + “quero falar com alguém” → modo `handoff` → **HandoffTemplate** com motivo usuário pediu atendente.

---

## Variáveis de contexto por etapa

As variáveis do **PromptVariables** que o NormalTemplate usa em conjunto com o funil:

| Variável          | Uso no NormalTemplate |
|-------------------|------------------------|
| `funnel_stage`    | Seleção do bloco em FUNNEL_INSTRUCTIONS (obrigatório para instruções por etapa). |
| `last_intent`     | Instruções adicionais por intent (agendamento, preço, cancelamento, reclamação, etc.). |
| `channel`         | Incluído no system prompt (ex.: whatsapp, web). |
| `turn_count`      | Incluído no system prompt (turno atual). |
| `slots`           | Seção “Informações Coletadas” (nome, preferências, etc.). |
| `knowledge_context` | Usado em modo RAG; bloco “Contexto de Conhecimento” + fontes. |
| `constraints`     | Restrições de política (todos os modos). |
| `safety_reminders`| Lembretes de segurança quando aplicável. |

Para Safety e Handoff, as variáveis relevantes são `safety_category` / `safety_level` e `handoff_reason` / `handoff_status`, respectivamente; `funnel_stage` pode estar presente mas não altera o texto do template.

---

## Referências

- **Funil canônico:** Documentação Técnica NeuroStack (MVP v0.5) – 11 etapas (lead_novo a standby).
- **Pipeline cognitivo:** [cognitive-pipeline.md](./cognitive-pipeline.md) – ordem Policy → SRM → Flow → CTX → Prompt → Handoff.
- **Código dos templates:** `src/prompt/templates/` – `base.py`, `normal.py`, `safety.py`, `handoff.py`.
- **Renderer:** `src/prompt/renderer.py` – mapeamento `PromptMode` → template.

---

*Documentação Prompt Templates e Funil - NeuroStack API v1.0*
