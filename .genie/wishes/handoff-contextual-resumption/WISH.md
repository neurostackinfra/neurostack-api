# Wish: Retomada contextual da IA após handoff

| Field | Value |
|-------|-------|
| **Status** | APPROVED |
| **Slug** | `handoff-contextual-resumption` |
| **Date** | 2026-09-30 |
| **Author** | Codex |
| **Appetite** | large |
| **Branch** | `wish/handoff-contextual-resumption` |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/handoff-contextual-resumption/DESIGN.md) |

## Summary

Reativar a IA passa a abrir um novo episódio contextual na mesma sessão: o estado que conduziu o handoff é reiniciado, enquanto nome, condição de paciente e um resumo bounded do episódio anterior mantêm a continuidade. O contexto é congelado atomicamente com a pausa, o histórico bruto anterior deixa de orientar a demanda atual e todos os caminhos de reativação aplicam a mesma transição idempotente.

## Scope

### IN

- Adicionar ao JSONB cognitivo um `context_episode_id` e um `prior_episode_context` tipado/versionado, sem tabela ou migration.
- Congelar `prior_episode_context` por uma única transição tenant-scoped `active → paused`, na mesma mutação/transação que grava `is_paused` e `paused_until`, usando até 20 eventos IA/pessoa e a mensagem corrente no handoff da IA.
- Tornar manual e expiração automática clientes de uma única transição de reativação condicional, com novo UUID de episódio e respostas idempotentes existentes.
- Resetar toda condução transitória da demanda e reconstruir `slots` por allowlist apenas com `lead_name` válido e `already_patient` booleano, preservando `Session.user_id`.
- Persistir o nome somente a partir de autoidentificação explícita e eliminar a divergência de uso entre `lead_name` e `user_name`.
- Etiquetar eventos conversacionais com o episódio atual e carregar como diálogo apenas `message.in`/`prompt.generated` desse episódio.
- Enviar o contexto anterior ao LLM como uma política `system` constante seguida por um payload `user` JSON canônico estruturalmente separado do histórico e da mensagem atual.
- Substituir a única entrada process-local do `FlowManager` por `session_id` quando o episódio durável mudar, inclusive em workers que não executaram a reativação.
- Responder diretamente à primeira demanda substantiva após a retomada; perguntar a nova necessidade apenas para saudação/mensagem neutra, usando o primeiro nome conhecido.
- Cobrir unidade, persistência, concorrência, request outbound e fluxo integrado com `EventService` e SRM reais.

### OUT

- Capturar, armazenar ou resumir mensagens trocadas com atendentes humanos durante a pausa, inclusive `fromMe`, mensagens descartadas ou `resolution_notes`.
- Alterar quando a IA é reativada, os prazos ou a política de stand-by já entregue por `handoff-standby-reactivation`.
- Criar nova sessão, tabela, migration, job, fila, cache, vetor de memória ou resumo por LLM.
- Modificar, concluir ou escolher contexto a partir do `HandoffRecord` mais recente.
- Preservar tratamento, serviço, agenda, modalidade, reclamação, preferências ou qualquer outro dado da demanda anterior como estado ativo.
- Suprimir um novo handoff quando a mensagem atual contém pedido explícito, risco, baixa confiança ou outro gatilho válido.
- Apagar ou reescrever eventos e handoffs anteriores.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Manter a mesma `Session` e gerar `context_episode_id` somente na reativação efetiva | Preserva identidade, auditoria e vínculos existentes sem criar um segundo lifecycle de sessão. |
| 2 | Persistir um único `prior_episode_context` fechado: `schema_version`, `lead_name` e `summary` | O objeto bounded fornece continuidade e captura atômica sem acumular memória. |
| 3 | Congelar o contexto quando a pausa é confirmada, não quando a IA é reativada | A `PipelineInput.message` fica disponível antes do `message.in` da fase 2 e não pode se perder numa reativação concorrente. |
| 4 | Nunca usar campo livre nem o “último” `HandoffRecord` como fonte de nome, pedido ou resumo | Records de link de agenda e takeover não possuem associação causal suficiente com a pausa. |
| 5 | Resetar por projeção explícita, preservando apenas `lead_name`, `already_patient` e `Session.user_id` | Uma allowlist impede flags, timestamps ou chaves futuras de reabrirem a demanda anterior por acidente. |
| 6 | Fazer manual e automático chamarem a mesma operação condicional | Mantém paridade e garante que somente a transição que sai de pausado crie episódio. |
| 7 | Etiquetar cada evento conversacional e filtrar o histórico por igualdade de episódio | Cria a fronteira exata sem apagar a trilha auditável. |
| 8 | Persistir nome somente por autoidentificação explícita | `pushName`, `senderName` e texto livre de handoff podem estar incorretos e não viram autoridade silenciosa. |
| 9 | Transportar contexto como dois elementos do array outbound: política `system` constante e payload `user` JSON | O conteúdo não confiável não é concatenado ao template, não altera a política e não cria role/mensagem. |
| 10 | Manter uma entrada do `FlowManager` por `session_id` e substituí-la quando o episódio mudar | Qualquer worker corrige seu cache a partir do estado durável sem acumular chaves por episódio. |
| 11 | Atender demanda substantiva imediatamente e reconduzir somente mensagem neutra | Reinicia a descoberta sem impor uma etapa artificial nem tratar a pessoa como desconhecida. |
| 12 | Manter conversa humana fora desta entrega | Esses dados não são capturados hoje e incluí-los ampliaria providers, retenção e PII sem requisito aprovado. |
| 13 | Fazer handoff da IA e takeover humano chamarem a mesma CAS tenant-scoped `active → paused` | Somente o vencedor grava pausa e contexto; concorrentes observam a classificação/estado vencedor sem last-writer-wins. |
| 14 | Conectar o extrator canônico ao fluxo normal antes de persistir o estado | Autoidentificação na mensagem real produz `state_patch.slots.lead_name` sem depender de slot pré-seeded. |

## Simplicity Case

- **Simplest complete design:** mesma sessão, um UUID de episódio, um contexto anterior bounded congelado com a pausa, reset seletivo e uma operação compartilhada de reativação.
- **Added machinery:** `context_episode_id` paga pela fronteira de histórico e invalidação cross-worker; `prior_episode_context` paga pela continuidade e pela mensagem gatilho antes da fase 2; o extrator único paga pelo requisito de chamar a pessoa pelo nome.
- **Deferred until measured:** transcript humano, mais de um resumo anterior, paginação de episódios e sumarização por LLM só entram se evidência operacional mostrar que o contexto bounded é insuficiente.
- **Complexity removed:** nenhuma nova sessão/tabela/migration, job, fila, cache, retry state machine, configuração por clínica ou coleção cumulativa; reativação não altera `handoff_records`.

## Dependencies

**depends-on:** `handoff-standby-reactivation`, `neurostack-core-dedomainification`
**blocks:** none

`handoff-standby-reactivation` já está concluído e fornece a política/pontos de pausa sobre os quais esta transição se conecta. `neurostack-core-dedomainification` precisa terminar antes da execução porque altera a mesma árvore de `flow`/prompt e seus testes; esta dependência evita implementar sobre contratos em movimento.

## Success Criteria

- [ ] Um handoff por demanda A que deixa `standby`, `handoff_requested` e `turn_count` alto, seguido de reativação e demanda B sem gatilho próprio, produz `handoff_required=false` e processa B em modo normal.
- [ ] A projeção de retomada volta a `lead_novo` e zera modo, contadores, intenções, flags, segurança, sentimento, `lead/sales/last` e todos os timestamps/marcadores cognitivos; somente `lead_name`, `already_patient` e `Session.user_id` sobrevivem como estado ativo.
- [ ] Reativação manual e expiração automática produzem a mesma projeção, resumo e fronteira; concorrência/repetição cria no máximo um UUID e preserva `reactivated`, `already_active` e os erros atuais.
- [ ] O contexto congelado tem schema fechado/versionado, `lead_name` com até 80 e `summary` com até 500 caracteres; não acumula episódios nem impede reativação quando precisa do resumo neutro.
- [ ] Com a gravação de `message.in` da fase 2 bloqueada, o commit da fase 1 já deixa pausa e contexto tipado visíveis; reativação imediata encontra no resumo a `PipelineInput.message` gatilho.
- [ ] Handoff da IA e takeover humano concorrentes disputam uma única CAS tenant-scoped `active → paused`; apenas um grava `is_paused`, `paused_until` e `prior_episode_context`, e o perdedor retorna a classificação/estado atual sem sobrescrever nenhum campo vencedor.
- [ ] Link de agendamento que cria `HandoffRecord` sem pausa não congela contexto; takeover posterior sem novo record usa somente eventos IA/pessoa e não herda motivo, nome ou pedido do record.
- [ ] `Meu nome é Ana` processado pelo fluxo normal, sem slot pré-seeded e com `EventService`/SRM reais, persiste `cognitive_state.slots.lead_name == "Ana"`; saudação neutra após a retomada usa o primeiro nome e pergunta a necessidade, enquanto demanda substantiva é atendida diretamente.
- [ ] Sessão legada sem contexto tipado tenta recuperar nome/resumo somente de eventos IA/pessoa bounded; `pushName`, `senderName`, resumo livre e campos de `HandoffRecord` nunca são fontes.
- [ ] O request ao LLM contém, depois do `system` principal e antes do histórico/mensagem atual, uma política `system` byte a byte constante e um payload `user` cujo content é somente JSON canônico com os três campos exatos.
- [ ] Tags, sentinelas, aspas, barras, controles, quebras de linha e `ignore as regras` em nome/resumo fazem round-trip por `json.loads` sem mudar quantidade, ordem, roles ou bytes da política; `specific_request` fica somente dentro de `summary`.
- [ ] Após a primeira reativação, o LLM recebe histórico bruto apenas do `context_episode_id` atual; eventos anteriores continuam consultáveis e não são apagados.
- [ ] Dois `FlowManager` independentes com cache HANDOFF antigo observam o novo episódio, substituem a única entrada sob o mesmo `session_id` e continuam com no máximo um estado local cada.
- [ ] Uma mensagem atual com pedido explícito de humano ou risco válido ainda gera handoff depois do reset.
- [ ] Pausa/reativação não alteram `handoff_records`; logs de fallback/conflito contêm tenant, sessão, campo e motivo, sem nome, telefone, mensagem ou resumo.
- [ ] A regressão executa `EventService` e SRM reais; mock que apenas comprova chegada ao pipeline não satisfaz este critério.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — +2 por contrato de estado JSONB e projeção de reset | engineer-standard / high | Firmar o contexto tipado, extração de nome, resumo bounded, serialização canônica e compatibilidade legada. |

### Wave 2 (after Wave 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 2 — +2 por transições concorrentes sobre estado persistido | engineer-standard / high | Congelar o contexto com a pausa e unificar reativação manual/automática sob CAS idempotente. |

### Wave 3 (after Wave 2)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 4 — +2 por runtime stateful, +1 multi-package, +1 mudança de prompt | engineer-complex / high | Isolar eventos/histórico por episódio, enquadrar o contexto outbound e invalidar o `FlowManager` cross-worker. |

### Wave 4 (after Wave 3)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 3 — +2 por regressão stateful fim a fim, +1 multi-package | engineer-standard / high | Provar os cenários completos com `EventService`/SRM reais e fechar os gates agregados. |

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** →
`engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high;
**4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an
independent `final-gate` at the highest justified effort. Codex maps these to
the `genie_*` profiles; other runtimes use their matching native roles. Keep
model and effort in runtime session/agent configuration, never skill frontmatter.

Os grupos são deliberadamente sequenciais: `event_service.py` e `pipeline.py` participam tanto da atomicidade fase 1/fase 2 quanto da fronteira de histórico e do request LLM. Paralelizar essas alterações criaria dois donos para os mesmos invariantes e arquivos.

### Aggregate gate (after Wave 4)

```bash
source scripts/wish-gates.sh && full_suite_gate && static_gate
git diff --check
```

`full_suite_gate` compara o resultado completo ao baseline conhecido e rejeita execução incompleta; `static_gate` cobre Black, isort, Flake8, mypy, limite de pacote e API pública. O gate roda sobre o HEAD posterior às dependências e esta entrega não pode mascarar falha alterando os baselines.

## Execution Groups

### Group 1: Contrato tipado de episódio e identidade

**Goal:** Criar as funções puras e o contrato JSONB que distinguem memória histórica de estado ativo antes de tocar nos caminhos concorrentes.

**Deliverables:**
1. Estender `CognitiveStateData` com `context_episode_id` opcional e `prior_episode_context` fechado/versionado, incluindo `to_dict`/`from_dict` compatíveis com linhas legadas e limites de 80/500 caracteres.
2. Implementar uma projeção pura de retomada que inicializa todos os campos cognitivos e reconstrói slots exclusivamente por `lead_name` válido e `already_patient` booleano.
3. Extrair/normalizar somente autoidentificação explícita para `lead_name` em função pura reutilizável pelo `ConversationSummarizer` e pelo wiring de fluxo normal atribuído ao Grupo 2; remover a divergência `user_name`/`lead_name` sem promover perfil do provider ou record.
4. Compor contexto determinístico de até 20 eventos IA/pessoa mais mensagem corrente opcional, resumo neutro em ausência de histórico e objeto substituível, nunca cumulativo.
5. Fornecer serializer JSON canônico (`sort_keys`, separadores compactos) e constante imutável da política de contexto; nenhum dado livre é interpolado na política.

**Acceptance Criteria:**
- [ ] Estado legado sem os dois campos desserializa com defaults e reserializa sem perder campos existentes.
- [ ] Uma tabela com valores preenchidos prova que todo campo fora da allowlist, inclusive `first_contact_at`, `pos_atendimento_started_at` e `last_message_at`, volta ao estado inicial.
- [ ] Nome vem apenas de slot confirmado, contexto tipado ou autoidentificação em eventos/mensagem corrente; entradas de provider, `HandoffRecord` e frases que não sejam autoidentificação não o alteram.
- [ ] Contexto respeita schema/limites, inclui a mensagem corrente uma vez e produz fallback neutro sem falhar.
- [ ] Serialização adversarial faz round-trip para exatamente `lead_name`, `schema_version` e `summary`; os bytes da política permanecem constantes.

**Validation:**
```bash
poetry run pytest -q -p no:cacheprovider \
  tests/handoff/test_episode_context.py \
  tests/handoff/test_service.py \
  tests/memory/test_state_store.py
poetry run black --check \
  src/handoff/episode_context.py src/handoff/summarizer.py src/models/session.py \
  tests/handoff/test_episode_context.py tests/handoff/test_service.py \
  tests/memory/test_state_store.py
poetry run isort --check-only \
  src/handoff/episode_context.py src/handoff/summarizer.py src/models/session.py \
  tests/handoff/test_episode_context.py tests/handoff/test_service.py \
  tests/memory/test_state_store.py
poetry run flake8 \
  src/handoff/episode_context.py src/handoff/summarizer.py src/models/session.py \
  tests/handoff/test_episode_context.py tests/handoff/test_service.py \
  tests/memory/test_state_store.py
poetry run mypy \
  src/handoff/episode_context.py src/handoff/summarizer.py src/models/session.py
```

**depends-on:** none

---

### Group 2: Pausa e reativação atômicas

**Goal:** Garantir que qualquer pausa efetiva passe por uma única CAS, que o fluxo normal persista o nome canônico e que toda retomada abra exatamente um episódio novo com a mesma semântica.

**Deliverables:**
1. Implementar no repositório/serviço uma única operação tenant-scoped `active → paused`: um `UPDATE`/CAS condicional por tenant e sessão, com precondição `is_paused=false`, grava `is_paused=true`, `paused_until` e `prior_episode_context` na mesma mutação/transação. Somente o vencedor retorna `paused`; zero rows affected relê/classifica o estado atual (`already_paused` ou erro existente) e nunca atualiza nem recompõe o contexto vencedor.
2. Fazer o handoff da IA compor seu candidato com eventos bounded mais `PipelineInput.message` e chamar essa operação na fase 1, antes da persistência eventual de `message.in`; fazer takeover humano compor outro candidato somente dos eventos IA/pessoa e chamar exatamente a mesma operação. Dois pausers podem calcular candidatos, mas nunca escrever depois do vencedor.
3. Manter o scheduling-link que cria record sem pausa fora dessa operação: ele não congela contexto, não altera pausa e nenhum dos três caminhos consulta o record mais recente.
4. Conectar o extrator puro do Grupo 1 ao processamento normal em `src/core/pipeline.py`/`src/services/event_service.py`: consumir a mensagem real antes da persistência cognitiva, mesclar `lead_name` em `state_patch.slots` quando houver autoidentificação válida e deixar slots inalterados quando não houver, sem pre-seeding nem fonte de provider/record.
5. Evoluir o repositório/serviço para uma transição condicional tenant-scoped que limpa a pausa, aplica a projeção e cria UUID somente para o vencedor; manter todas as classificações HTTP existentes.
6. Fazer o endpoint manual e `_check_pause_state` na expiração automática chamarem a mesma operação de retomada, variando apenas a precondição de pausa permanente ou deadline vencido.
7. Para sessão legada pausada sem objeto tipado, compor fallback apenas dos eventos IA/pessoa antes da mutação; falha/ausência de histórico usa resumo neutro e não bloqueia.
8. Registrar conflitos/fallbacks somente com tenant, sessão, campo e motivo, sem PII ou conteúdo conversacional.

**Acceptance Criteria:**
- [ ] Um teste controla a fronteira entre fases: bloqueia `_save_events_to_store`, observa pausa/contexto confirmados, reativa e encontra a mensagem gatilho mesmo sem `message.in` persistido.
- [ ] Um teste com barreira lança obrigatoriamente handoff da IA versus takeover humano sobre a mesma sessão ativa, fornece candidatos distinguíveis e prova: um único resultado `paused`, um perdedor idempotente, banco igual por inteiro ao candidato vencedor e nenhuma sobrescrita last-writer-wins; uma variante IA versus IA pode complementar, mas não substituir esse caso.
- [ ] Duas reativações concorrentes/repetidas geram um UUID, uma resposta `reactivated` e as demais `already_active`/classificação atual; nenhuma grava estado parcial.
- [ ] Manual e automático produzem objetos cognitivos iguais para o mesmo estado inicial e preservam sessão temporária, superseded, encerrada, inexistente e conflito.
- [ ] Scheduling-link sem pausa seguido de takeover sem record congela somente eventos IA/pessoa; `handoff_records` permanece byte a byte inalterado.
- [ ] Um teste focado envia `Meu nome é Ana` sem preencher slots previamente pelo fluxo normal com `EventService` e SRM reais e verifica, após a persistência do patch, `cognitive_state.slots.lead_name == "Ana"`; frase sem autoidentificação não cria nem altera o slot.
- [ ] Falha do resumo não reabre a sessão com estado antigo nem impede a retomada; usa o objeto neutro tipado.

**Validation:**
```bash
poetry run pytest -q -p no:cacheprovider \
  tests/api/test_sessions.py \
  tests/api/test_webhook_uazapi.py \
  tests/api/test_events.py \
  tests/core/test_pipeline.py \
  tests/handoff/test_episode_context.py \
  tests/services/test_session_pause_transition.py \
  tests/services/test_event_service_context_episode.py
poetry run black --check \
  src/repositories/session.py src/services/session_service.py \
  src/services/event_service.py src/api/routes/webhook.py src/core/pipeline.py \
  tests/api/test_sessions.py tests/api/test_webhook_uazapi.py \
  tests/api/test_events.py tests/core/test_pipeline.py \
  tests/services/test_session_pause_transition.py \
  tests/services/test_event_service_context_episode.py
poetry run isort --check-only \
  src/repositories/session.py src/services/session_service.py \
  src/services/event_service.py src/api/routes/webhook.py src/core/pipeline.py \
  tests/api/test_sessions.py tests/api/test_webhook_uazapi.py \
  tests/api/test_events.py tests/core/test_pipeline.py \
  tests/services/test_session_pause_transition.py \
  tests/services/test_event_service_context_episode.py
poetry run flake8 \
  src/repositories/session.py src/services/session_service.py \
  src/services/event_service.py src/api/routes/webhook.py src/core/pipeline.py \
  tests/api/test_sessions.py tests/api/test_webhook_uazapi.py \
  tests/api/test_events.py tests/core/test_pipeline.py \
  tests/services/test_session_pause_transition.py \
  tests/services/test_event_service_context_episode.py
poetry run mypy \
  src/repositories/session.py src/services/session_service.py \
  src/services/event_service.py src/api/routes/webhook.py src/core/pipeline.py
```

**depends-on:** Group 1

---

### Group 3: Fronteira de runtime, prompt e `FlowManager`

**Goal:** Fazer cada worker e cada request ao LLM enxergarem o contexto anterior apenas como memória e o episódio novo como único diálogo ativo.

**Deliverables:**
1. Centralizar metadata conversacional e gravar `context_episode_id` em `message.in` e `prompt.generated` sem remover os metadados existentes.
2. Filtrar `_load_message_history` por igualdade com o episódio atual depois da primeira reativação; manter o comportamento legado somente antes dessa fronteira.
3. Montar o array outbound na ordem contratada: `system` principal, política `system` constante, payload `user` JSON, histórico do episódio e mensagem atual; primary e fallback recebem o mesmo array.
4. Propagar `context_episode_id` durável ao `FlowManager` pelo contexto existente e armazená-lo junto à única entrada `_states[session_id]`; mismatch substitui atomicamente por estado NORMAL.
5. Preservar resposta direta para demanda substantiva e a pergunta curta nominal apenas para saudação/neutralidade, sem enfraquecer gatilhos atuais de handoff.
6. Garantir logs de evento ausente, payload inválido e troca de episódio sem valores brutos ou PII.

**Acceptance Criteria:**
- [ ] Eventos antigos e eventos sem ID ficam auditáveis, mas não entram no histórico depois da fronteira; os dois tipos conversacionais novos carregam o ID atual.
- [ ] Captura do cliente LLM prova quantidade, ordem, roles, política constante e `json.loads` exato para todos os valores adversariais, sem concatenar payload ao prompt principal.
- [ ] Fallback multimodal recebe exatamente as mesmas mensagens e limites do primary, inclusive o contexto enquadrado.
- [ ] Dois managers independentes, ambos em HANDOFF no episódio antigo, substituem a entrada ao observar o novo ID e mantêm tamanho um por sessão.
- [ ] Mesmo ID reutiliza estado; ID diferente nunca reutiliza HANDOFF; `reset_session` continua opcional e não participa da correção cross-worker.

**Validation:**
```bash
poetry run pytest -q -p no:cacheprovider \
  tests/services/test_event_service_context_episode.py \
  tests/services/test_event_service_emission.py \
  tests/services/test_event_service_llm_fallback.py \
  tests/flow/test_flow_manager.py \
  tests/srm/test_prompt_injection.py \
  tests/core/test_pipeline.py
source scripts/wish-gates.sh && core_suite_gate
poetry run black --check \
  src/services/event_service.py src/core/pipeline.py \
  packages/neurostack-core/neurostack_core/flow/manager.py \
  tests/services/test_event_service_context_episode.py \
  tests/services/test_event_service_emission.py \
  tests/services/test_event_service_llm_fallback.py \
  tests/flow/test_flow_manager.py tests/srm/test_prompt_injection.py \
  tests/core/test_pipeline.py
poetry run isort --check-only \
  src/services/event_service.py src/core/pipeline.py \
  packages/neurostack-core/neurostack_core/flow/manager.py \
  tests/services/test_event_service_context_episode.py \
  tests/services/test_event_service_emission.py \
  tests/services/test_event_service_llm_fallback.py \
  tests/flow/test_flow_manager.py tests/srm/test_prompt_injection.py \
  tests/core/test_pipeline.py
poetry run flake8 \
  src/services/event_service.py src/core/pipeline.py \
  packages/neurostack-core/neurostack_core/flow/manager.py \
  tests/services/test_event_service_context_episode.py \
  tests/services/test_event_service_emission.py \
  tests/services/test_event_service_llm_fallback.py \
  tests/flow/test_flow_manager.py tests/srm/test_prompt_injection.py \
  tests/core/test_pipeline.py
poetry run mypy \
  src/services/event_service.py src/core/pipeline.py \
  packages/neurostack-core/neurostack_core/flow/manager.py
```

**depends-on:** Group 1, Group 2

---

### Group 4: Regressão integrada da retomada

**Goal:** Provar o comportamento observável completo sem substituir `EventService`, SRM, decisor de handoff ou persistência por mocks de passagem.

**Deliverables:**
1. Criar uma regressão API integrada que percorra demanda A → handoff/pausa → reativação → saudação ou demanda B, mockando no máximo o transporte externo do LLM/provider.
2. Cobrir manual e deadline automático, preservação de nome/condição de paciente, resumo anterior, exclusão de conversa humana e ausência de re-handoff residual.
3. Cobrir um novo pedido explícito de atendente e um risco atual para provar que gatilhos legítimos continuam ativos.
4. Cobrir scheduling-link → takeover, fase 1 → reativação antes da fase 2, CAS concorrente IA versus takeover, duas instâncias de manager e ausência de PII nos logs.
5. Executar os gates focados e agregados, documentando contagens/resultado sem atualizar baseline para esconder regressão.

**Acceptance Criteria:**
- [ ] A nova suite falha contra o comportamento anterior por re-handoff/contaminação e passa somente com fronteira, reset e memória contextual completos.
- [ ] A demanda B aparece como a única demanda ativa no request/decisão; o resumo de A permanece disponível apenas no payload histórico.
- [ ] A saudação usa o primeiro nome e pergunta a necessidade sem apresentação de primeiro contato; mensagem substantiva não recebe pergunta intermediária.
- [ ] Nenhuma mensagem humana, descartada durante a pausa ou `resolution_notes` entra no resumo.
- [ ] Focused suites, `full_suite_gate`, `static_gate` e `git diff --check` terminam com sucesso.

**Validation:**
```bash
poetry run pytest -q -p no:cacheprovider \
  tests/api/test_handoff_contextual_resumption.py \
  tests/api/test_sessions.py \
  tests/api/test_webhook_uazapi.py \
  tests/api/test_events.py \
  tests/core/test_pipeline.py \
  tests/flow/test_flow_manager.py \
  tests/handoff/test_episode_context.py \
  tests/services/test_event_service_context_episode.py \
  tests/services/test_session_pause_transition.py
source scripts/wish-gates.sh && full_suite_gate && static_gate
git diff --check
```

O teste integrado usa o banco/roteamento da aplicação e as implementações reais de `EventService` e SRM; é permitido isolar chamadas externas não determinísticas do LLM e do provider, mas não mockar a decisão que está sob teste.

**depends-on:** Group 2, Group 3

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Funcional: após um atendimento humano, a primeira saudação da pessoa recebe uma retomada nominal e uma pergunta curta pela necessidade, sem novo handoff automático.
- [ ] Funcional: se a primeira mensagem já descreve uma demanda nova, a IA trata essa demanda diretamente e não exige confirmação genérica.
- [ ] Funcional: o agente demonstra continuidade coerente com o resumo pré-handoff, mas não afirma que a demanda antiga segue ativa.
- [ ] Integração: painel/manual e expiração automática produzem a mesma retomada na mesma sessão e preservam registros de handoff/eventos para auditoria.
- [ ] Integração: dois workers com cache antigo processam o novo episódio em NORMAL, sem depender de qual worker reativou.
- [ ] Privacidade: nome, telefone, mensagem e resumo não aparecem em logs de conflito/fallback; conversa humana permanece ausente do contexto automático.
- [ ] Regressão: novo pedido de atendente, risco ou outra condição atual continua gerando handoff normalmente.
- [ ] Regressão: sessões legadas, links de agendamento sem pausa e respostas/classificações do endpoint de reativação mantêm compatibilidade.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Pausa ficar visível antes de o evento gatilho existir | High | Congelar o contexto com `PipelineInput.message` na mesma fase 1/CAS da pausa e testar reativação antes da fase 2. |
| Handoff da IA e takeover concorrentes sobrescreverem contexto | High | Uma única CAS tenant-scoped `active → paused`; somente o vencedor grava os três campos, e teste com barreira compara o estado final ao candidato vencedor. |
| Reativações concorrentes criarem episódios divergentes | High | Update condicional tenant-scoped; somente a transição vencedora grava UUID/projeção. |
| Histórico antigo voltar ao LLM por writer sem metadata | High | Centralizar metadata, exigir igualdade após a fronteira e testar ambos os tipos conversacionais. |
| Conteúdo histórico tentar alterar instruções/roles | High | Elementos estruturais separados, política constante, JSON canônico e captura adversarial do request completo. |
| Worker reutilizar HANDOFF process-local | High | Comparar o episódio durável a cada chamada e substituir a única entrada por sessão. |
| Reset limpar um fato que futuramente deveria sobreviver | Medium | Allowlist intencional; qualquer novo fato durável exige campo tipado, critério e decisão de produto. |
| Nome não aparecer na janela de sessão legada | Medium | Continuar sem nome em vez de inventar ou consultar provider/record; novas conversas persistem a fonte canônica. |
| Resumo/contexto duplicar PII por mais tempo | High | Limites 80/500, substituição em vez de acumulação, tenant scope e mesma retenção da sessão. |
| Mudanças concorrentes de `neurostack-core-dedomainification` colidirem em flow/prompt | High | Manter dependência de execução; rebasear sobre seu contrato concluído antes do Grupo 1. |
| Falha ao compor resumo bloquear reativação | Medium | Objeto neutro tipado e log sem PII; nunca restaurar estado transitório antigo como fallback. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan review — 2026-10-01T01:39:31Z

- **Context:** plan review before execution
- **Reviewed SHA-256:** `7f56d21cda843917d06183b54d03fea10309918e72d211484378f07f25373732`
- **Repository HEAD:** `96edb2579ded08c3a1b34549bb73d1ee86a77325`
- **Reviewer:** `/root/design_review_2`
- **Verdict:** FIX-FIRST
- **HIGH:** definir uma única transição tenant-scoped `active → paused` com update condicional que grave pausa e `prior_episode_context` juntos; IA e takeover devem usá-la, e dois pausers concorrentes não podem sobrescrever o contexto do vencedor.
- **MEDIUM:** atribuir o wiring do extrator canônico de nome a um grupo/arquivo e provar por fluxo normal real que uma autoidentificação persiste `slots.lead_name` sem pré-seeding.
- **Accepted:** design/evidência, framing adversarial, compatibilidade legada, scheduling-link → takeover, `FlowManager` em dois workers, DAG sequencial, gates, dependências e integração real.
- **Checks:** blocos Bash em `bash -n`, `git diff --check`, placeholders e existência de arquivos/gates; nenhuma suite de implementação executada.

### Plan re-review — 2026-10-01T01:46:19Z (fix loop 1/2)

- **Context:** plan re-review after first fix
- **Reviewed SHA-256:** `a4bdcfc698e08aea675494459b81b3c961eb2ec0c0d153eada3b548921457940`
- **Repository HEAD:** `96edb2579ded08c3a1b34549bb73d1ee86a77325`
- **Reviewer:** `/root/design_review_2`
- **Verdict:** SHIP
- **Severity findings:** none; 0 CRITICAL, 0 HIGH, 0 MEDIUM and 0 blocking gaps.
- **Evidence:** a CAS tenant-scoped `active → paused` única atende handoff IA e takeover sem overwrite do perdedor, com teste de barreira IA-versus-takeover; o wiring canônico de nome pertence a `pipeline.py`/`event_service.py` e o teste real persiste `Meu nome é Ana` sem pre-seeding.
- **Accepted:** evidência SHIP do design, DAG sequencial, fase 1/fase 2, framing, dependências, ownership, critérios e gates. `neurostack-core-dedomainification` permanece pré-requisito informativo de execução.
- **Checks:** sintaxe de todos os blocos Bash, `git diff --check`, placeholders e digest exato; nenhuma suite de implementação executada.

---

## Files to Create/Modify

```
CREATE  src/handoff/episode_context.py
CREATE  tests/handoff/test_episode_context.py
CREATE  tests/services/test_event_service_context_episode.py
CREATE  tests/services/test_session_pause_transition.py
CREATE  tests/api/test_handoff_contextual_resumption.py

MODIFY  src/models/session.py
MODIFY  src/handoff/summarizer.py
MODIFY  src/repositories/session.py
MODIFY  src/services/session_service.py
MODIFY  src/services/event_service.py
MODIFY  src/api/routes/webhook.py
MODIFY  src/core/pipeline.py
MODIFY  packages/neurostack-core/neurostack_core/flow/manager.py

MODIFY  tests/memory/test_state_store.py
MODIFY  tests/handoff/test_service.py
MODIFY  tests/api/test_sessions.py
MODIFY  tests/api/test_webhook_uazapi.py
MODIFY  tests/api/test_events.py
MODIFY  tests/core/test_pipeline.py
MODIFY  tests/flow/test_flow_manager.py
MODIFY  tests/srm/test_prompt_injection.py
MODIFY  tests/services/test_event_service_emission.py
MODIFY  tests/services/test_event_service_llm_fallback.py
```
