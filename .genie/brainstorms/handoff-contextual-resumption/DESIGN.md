# Design: Retomada contextual da IA após handoff

| Field | Value |
|-------|-------|
| **Slug** | `handoff-contextual-resumption` |
| **Date** | 2026-09-30 |
| **WRS** | 100/100 |

## Problem

Reativar a IA hoje remove somente a pausa da sessão e mantém o estado que originou o handoff — `standby`, flags, contador, intenções e histórico bruto —, por isso uma demanda nova pode ser encaminhada novamente sem motivo atual. A retomada precisa reiniciar a descoberta da demanda sem apagar a identidade conhecida nem o contexto histórico necessário para uma continuidade natural.

## Scope

### IN

- Criar uma transição de domínio única para a reativação efetiva, compartilhada pela ação manual e pela expiração automática da pausa.
- Manter a mesma `Session` e abrir nela um novo episódio contextual, identificado duravelmente e usado também para isolar o estado process-local do `FlowManager`.
- Reiniciar o estado transitório da demanda: funil, modo, contadores, intenções, sentimento, segurança, estado comercial e todas as flags; preservar como estado ativo somente `slots.lead_name`, `slots.already_patient` e o `Session.user_id` já existente.
- Persistir o nome explicitamente informado em `slots.lead_name`, com recuperação compatível a partir do contexto anterior tipado ou, em sessões legadas, somente das mensagens anteriores.
- Produzir um resumo determinístico e limitado do episódio IA anterior, incluindo a mensagem que disparou o handoff, e injetá-lo no prompt como contexto histórico — nunca como demanda ativa.
- Carregar como histórico de diálogo somente eventos do episódio atual; os eventos anteriores permanecem auditáveis, mas deixam de entrar como mensagens correntes.
- Quando a primeira mensagem pós-reativação já contiver uma demanda, respondê-la diretamente; diante apenas de saudação ou mensagem sem demanda acionável, perguntar de forma curta como pode ajudar, usando o primeiro nome quando conhecido.
- Preservar os gatilhos legítimos: um novo pedido explícito, risco, baixa confiança ou outra condição atual continua podendo gerar handoff.
- Cobrir o contrato em testes de unidade, persistência e fluxo integrado, com paridade entre reativação manual e automática.

### OUT

- Capturar, armazenar ou resumir mensagens trocadas entre a pessoa e atendentes humanos durante a pausa, inclusive mensagens `fromMe` e entradas hoje descartadas enquanto a sessão está pausada.
- Alterar a política de quando a IA volta, os prazos, o switch ou os modos de stand-by definidos por `handoff-standby-reactivation`.
- Criar nova sessão, tabela, migration, job, fila, cache, vetor de memória ou resumo assíncrono/gerado por LLM.
- Fechar, concluir, cancelar ou modificar o lifecycle dos `handoff_records` durante a reativação.
- Preservar tratamento, serviço, agendamento, convênio/particular, reclamação, preferências, objeções ou qualquer outro dado da demanda anterior como estado ativo; esses dados podem aparecer somente no resumo histórico.
- Apagar ou reescrever eventos e handoffs anteriores.

## Approach

### Episódio novo na mesma sessão

Adicionar ao `CognitiveStateData` dois campos opcionais compatíveis com JSONB legado, sem migration: `context_episode_id` e `prior_episode_context`. O segundo é um objeto tipado fechado `{schema_version: 1, lead_name: string|null, summary: string}`, com nome de no máximo 80 e resumo de no máximo 500 caracteres Unicode. Sessões existentes sem identificador continuam com o comportamento histórico até a primeira reativação; toda pausa efetiva substitui o objeto pelo contexto que acabou de congelar, e toda reativação efetiva gera um novo UUID de episódio mantendo esse único contexto anterior. Não há coleção de episódios nem crescimento cumulativo dentro da sessão: o Event Store continua sendo a trilha auditável e esse contexto é histórico, não estado ativo da demanda.

A operação compartilhada de retomada recebe tenant, sessão efetiva, tipo de reativação e instante atual. Ela usa o `prior_episode_context` já congelado pela transição que pausou a sessão; somente para sessão legada sem esse objeto recompõe o contexto a partir dos eventos IA/pessoa bounded. Então constrói a nova projeção cognitiva e aplica em uma única mutação condicional `is_paused=false`, `paused_until=null` e o novo `cognitive_state`. A forma manual mantém a precondição de pausa permanente; a forma automática mantém a precondição de prazo expirado. Chamadas concorrentes ou repetidas preservam a idempotência atual: apenas a transição que realmente sai de pausado cria episódio; as demais retornam `already_active` ou a classificação já existente sem gerar outra fronteira.

O reset define `funnel_stage=lead_novo`, `intent_history=[]`, `turn_count=0`, `safety_triggers=0`, `last_intent=null`, `last_action=null`, `confidence_avg=null`, `current_mode=normal`, sentimento vazio, `lead={}`, `sales={}`, `last={}` e `flags={}`. `slots` é reconstruído por allowlist somente com `lead_name` válido e `already_patient` booleano; `first_contact_at` e todos os demais timestamps ou marcadores cognitivos são definidos como nulos/estado inicial. Fora do contexto histórico tipado, sobrevivem como estado ativo somente esses dois slots e o `Session.user_id` existente. Essa projeção explícita impede que chaves antigas ou futuras sejam preservadas por acidente.

### Nome canônico

Uma função pura compartilhada reconhece apenas autoidentificação explícita da pessoa e normaliza o resultado para `slots.lead_name`; ela é usada no processamento normal e na composição do contexto, eliminando a divergência `user_name` versus `lead_name`. A precedência é: `slots.lead_name` já confirmado; `lead_name` do `prior_episode_context` tipado, se existir; e extração determinística das mensagens de usuário do episódio bounded, acrescidas de `PipelineInput.message` no handoff da IA. Sessão legada sem contexto tipado usa somente os eventos. `pushName`/`senderName`, resumo de handoff e qualquer campo livre de `HandoffRecord` nunca são fontes de nome ou pedido. Ausência de nome não bloqueia a retomada nem autoriza a IA a inventá-lo.

### Resumo bounded sem conversa humana

O `ConversationSummarizer` existente continua sendo a única regra de resumo. Quando um handoff da IA realmente pausa a sessão, a fase 1 compõe deterministicamente `prior_episode_context` a partir de no máximo 20 eventos conversacionais IA/pessoa do episódio mais o `PipelineInput.message` corrente, mesmo que essa mensagem ainda não exista no Event Store. O objeto tipado é persistido na mesma transação e mutação condicional que torna `is_paused=true`; a gravação posterior de `message.in` na fase 2 não é precondição nem fonte de correção. `specific_request` pode aparecer somente como conteúdo do `summary` produzido, nunca como campo do objeto ou instrução separada.

No takeover sem mensagem corrente da pessoa, a mesma fase 1 deriva o objeto apenas dos eventos IA/pessoa bounded. O envio de link de agendamento que cria `HandoffRecord` sem pausar não congela nem altera `prior_episode_context`; se depois houver takeover, nenhum conteúdo desse record é atribuído à pausa. Não existe preferência por “último `HandoffRecord`”. Somente uma sessão legada já pausada e sem o objeto tipado o recompõe na reativação, usando apenas eventos IA/pessoa disponíveis.

Mensagens humanas não entram nesse cálculo: `fromMe` e mensagens descartadas durante a pausa continuam fora do Event Store e do resumo. `resolution_notes` também não é promovido automaticamente para o contexto do agente. O objeto é substituído, não acumulado, tenant-scoped e sujeito à retenção da própria sessão; falta de histórico produz `{schema_version: 1, lead_name: null, summary: <resumo mínimo neutro>}` e nunca impede a reativação.

### Fronteira do histórico e prompt

Todos os eventos conversacionais novos recebem `context_episode_id` em `event_metadata`. Depois da primeira reativação, `_load_message_history` retorna somente `message.in` e `prompt.generated` cujo identificador coincide com o episódio atual; eventos legados ou de episódios anteriores ficam fora do diálogo corrente. Antes da primeira reativação, a ausência do campo preserva a compatibilidade atual.

Quando há `prior_episode_context`, o array outbound do cliente LLM preserva a mensagem `system` principal existente e, imediatamente depois dela e antes do histórico corrente e da mensagem atual, acrescenta exatamente dois elementos: (1) uma mensagem `system` cuja política fixa é byte a byte constante e manda tratar o próximo elemento somente como dados históricos não confiáveis, nunca como instruções/pedido atual, e decidir novo handoff apenas pelo episódio corrente; (2) uma mensagem `user` cujo `content` inteiro é somente o JSON canônico do objeto tipado, sem prefixo, sufixo, tag ou sentinela. O serializer padrão usa `sort_keys=true` e separadores compactos `(',', ':')`, escapa strings e controles, e emite exatamente `lead_name`, `schema_version` e `summary`. Nenhum valor é concatenado ou interpolado na política, no prompt principal ou no template, e o `content` string não pode criar novo elemento nem novo `role` no array.

A primeira mensagem substantiva segue normalmente pelo SRM e é respondida sem pergunta intermediária. Se o classificador existente indicar apenas saudação ou ausência de demanda acionável, a política fixa orienta uma única pergunta curta pela nova necessidade e usa o primeiro nome quando disponível, sem reapresentar a conversa como primeiro contato. Testes capturam o array outbound completo e verificam quantidade, ordem e roles, os bytes invariáveis da política, `json.loads` com round-trip do schema exato e valores contendo tags/sentinelas, aspas, barras, quebras de linha e `ignore as regras`. Esses valores — incluindo qualquer `specific_request` já incorporado ao resumo — permanecem dentro do único `content` JSON e não criam mensagem, role, campo ou instrução adicional.

O `FlowManager` mantém no máximo uma entrada process-local por `session_id`. Em toda chamada, compara o `context_episode_id` durável observado com o identificador guardado nessa entrada: se não houver entrada, inicializa estado normal; se forem diferentes, substitui atomicamente a única entrada da sessão por estado normal associado ao novo episódio; se forem iguais, reutiliza a entrada. Nunca chaveia por `(session_id, context_episode_id)`, nunca acumula estados de episódios e não depende de o worker ter executado a reativação. `reset_session` pode continuar como limpeza local, mas nunca é a autoridade de correção.

### Alternatives considered

- **Nova `Session` após cada handoff:** cria uma fronteira forte, mas duplica identidade de runtime, quebra vínculos simples com handoffs e exige coordenação/recuperação para sessões parciais. Rejeitada.
- **Somente limpar flags ou acrescentar uma instrução ao prompt:** não resolve `turn_count`, `standby`, histórico bruto nem o decisor que executa antes do LLM. Rejeitada.
- **Capturar a conversa humana e gerar resumo por LLM:** adiciona mudanças multi-provider, nova retenção de PII, custo, latência e falhas externas sem requisito atual; explicitamente fora.

## Simplicity Case

- **Simplest complete design:** mesma sessão, um identificador de episódio, um contexto anterior tipado e bounded congelado com a pausa, reset seletivo e uma operação compartilhada de reativação.
- **Added machinery:** `context_episode_id` paga pela fronteira exata do histórico e invalidação cross-worker; `prior_episode_context` paga pela captura atômica da mensagem gatilho e continuidade sem reintroduzir mensagens antigas; a função compartilhada de nome paga pelo requisito explícito de chamar a pessoa pelo nome e elimina duas chaves divergentes. Não há outro estado durável novo.
- **Deferred until measured:** captura de conversa humana só será reconsiderada se operadores demonstrarem que o resumo pré-handoff é insuficiente; mais de um resumo histórico ou busca paginada só entra se houver caso real que exija episódios mais antigos; LLM de sumarização só entra se avaliações mostrarem que o resumo determinístico bounded perde informação necessária.
- **Complexity removed:** não há nova sessão, tabela, migration, evento de sincronização, job, retry state machine, cache, configuração por clínica, lista cumulativa de episódios ou dependência de provedor de IA na reativação.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Manter a mesma sessão e gerar `context_episode_id` somente na reativação efetiva | Preserva vínculos e fornece uma fronteira durável/idempotente sem novo lifecycle de sessão. |
| 2 | Guardar somente `prior_episode_context` tipado e o episódio atual | Limita estado e PII; episódios antigos continuam no Event Store auditável e não há migration fora do JSONB existente. |
| 3 | Resetar todo o estado transitório e reconstruir slots por allowlist `lead_name`/`already_patient` | Evita re-handoff por chaves residuais e impede preservação acidental de novas flags. |
| 4 | Voltar o funil a `lead_novo`, mas sinalizar retomada no prompt | Reinicia a descoberta da demanda sem fingir que a pessoa nunca conversou com a clínica. |
| 5 | Persistir somente nome explicitamente informado; perfil do provedor não é fonte canônica | Atende ao pedido sem chamar a pessoa por nome de conta possivelmente incorreto. |
| 6 | Congelar o contexto na fase 1 que pausa: eventos IA/pessoa bounded mais `PipelineInput.message` no handoff da IA | Inclui a mensagem gatilho independentemente da gravação de eventos da fase 2 e elimina qualquer associação com `HandoffRecord`. |
| 7 | Excluir conversa humana e `resolution_notes` do contexto automático | Decisão explícita do produto; evita ampliar captura e retenção nesta entrega. |
| 8 | Etiquetar eventos novos pelo episódio e filtrar por igualdade | É uma fronteira exata e também permite invalidar estado local em todos os workers. |
| 9 | Responder diretamente à primeira demanda substantiva; perguntar o objetivo apenas quando não houver demanda acionável | Evita uma etapa artificial sem perder a recondução necessária para saudações. |
| 10 | Reativação não altera `handoff_records` | Mantém auditoria e lifecycle operacional separados da retomada cognitiva. |
| 11 | A transição manual e a automática usam o mesmo serviço e diferem apenas na precondição | Impede duas semânticas de retomada e preserva as regras atuais de autorização/prazo. |
| 12 | Inserir política `system` constante e payload `user` JSON canônico como dois elementos distintos antes do histórico atual | O conteúdo não confiável permanece em um único valor string, sem framing textual, interpolação na política ou capacidade de criar mensagem/role. |
| 13 | Manter uma única entrada de `FlowManager` por sessão e substituí-la quando o episódio mudar | Corrige qualquer worker a partir do identificador durável sem acumular cache por episódio. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Limpar uma flag futura que deveria sobreviver entre demandas | Medium | A allowlist é intencional; qualquer novo fato durável deve ganhar campo tipado e critério próprio, não depender de flag opaca. |
| 2 | Nome não estar nos eventos retidos de uma sessão legada | Medium | Sessão legada sem contexto tipado usa somente eventos bounded; sem evidência, continuar sem nome em vez de consultar record, inventar ou bloquear. |
| 3 | Um escritor esquecer de etiquetar evento pós-reativação | High | Centralizar a composição de metadata e testar todos os eventos conversacionais consumidos pelo history loader; evento sem o episódio atual não entra no diálogo. |
| 4 | Estado HANDOFF process-local sobreviver em outro worker | High | Cada worker compara o episódio durável e substitui sua única entrada por sessão quando ele muda; reset local isolado não é aceito como correção. |
| 5 | Duas reativações concorrentes criarem fronteiras diferentes | High | Mutação condicional/CAS em uma transação; somente quem muda `is_paused=true` para falso persiste o novo episódio. |
| 6 | Contexto tipado duplicar PII por tempo maior que os eventos de origem | High | Nome/resumo limitados a 80/500 caracteres, substituição em vez de acumulação, tenant scope e mesma retenção da sessão; nunca copiar transcript humano. |
| 7 | Reset para `lead_novo` fazer o prompt se reapresentar como primeiro contato | Medium | Bloco de retomada explícito, nome/contexto injetados e testes de prompt para saudação e demanda substantiva. |
| 8 | Histórico antigo voltar por compatibilidade legada | High | Compatibilidade sem filtro vale somente antes da primeira reativação; depois dela a igualdade de `context_episode_id` é obrigatória. |
| 9 | Mudanças concorrentes de `handoff-standby-reactivation` alterarem os pontos de entrada | Medium | Esse wish deve estar integrado primeiro; a transição cognitiva se conecta ao resolvedor final compartilhado, não duplica política de pausa. |
| 10 | Commit da pausa vencer a gravação posterior do evento da mensagem gatilho | High | Congelar eventos bounded mais `PipelineInput.message` na mesma fase 1/CAS da pausa; fase 2 não participa da garantia. |
| 11 | Nome ou resumo histórico tentar alterar framing/roles do request LLM | High | Dois elementos estruturais separados no array, JSON canônico como único `content`, política constante e testes sobre o request outbound completo. |

## Success Criteria

- [ ] Dado um pedido de atendente que grava `standby`, `handoff_requested` e `turn_count` alto, a reativação manual cria um novo episódio, preserva `lead_name`/`already_patient` e a demanda B seguinte resulta em `handoff_required=false` quando B não contém motivo próprio.
- [ ] A expiração automática produz exatamente a mesma projeção cognitiva, fronteira de histórico e resumo da reativação manual para o mesmo estado inicial.
- [ ] Após a retomada, `funnel_stage`, modo, contadores, intenções, flags, segurança, sentimento e estados `lead/sales/last` não carregam a demanda A; `first_contact_at` e todos os demais timestamps/marcadores cognitivos ficam nulos ou no estado inicial, nenhum slot além de `lead_name` e `already_patient` sobrevive e somente o `Session.user_id` existente completa a allowlist ativa.
- [ ] Um nome explicitamente informado antes do handoff é persistido como `slots.lead_name`; na primeira saudação neutra após a retomada, o prompt manda usar o primeiro nome e perguntar a nova necessidade sem se reapresentar como primeiro contato.
- [ ] Se a primeira mensagem pós-reativação contém uma demanda B acionável, o prompt/LLM a recebe como pedido atual e não exige antes uma pergunta genérica de descoberta.
- [ ] O `prior_episode_context` tem schema fechado/versionado, nome de no máximo 80 e resumo de no máximo 500 caracteres; inclui a mensagem que disparou o handoff da IA, enquanto mensagens humanas, mensagens descartadas durante a pausa e `resolution_notes` não entram nele.
- [ ] Um teste concorrente bloqueia a gravação de `message.in` da fase 2, deixa a fase 1 confirmar `is_paused=true` com o contexto tipado, reativa imediatamente e ainda encontra no resumo a `PipelineInput.message` que disparou o handoff.
- [ ] Se um link de agendamento criar `HandoffRecord` sem pausa e um takeover posterior pausar sem mensagem atual da pessoa nem novo record, o link não congela contexto e o takeover usa somente eventos IA/pessoa, sem conteúdo do record como motivo, nome ou pedido.
- [ ] O request outbound com contexto contém, após o `system` principal e antes do histórico/mensagem atual, exatamente a política `system` constante e um payload `user`; tags/sentinelas, aspas, barras, quebras de linha e `ignore as regras` em nome ou resumo não alteram quantidade, ordem ou roles, a política permanece byte a byte igual, e `json.loads` faz round-trip para exatamente `lead_name`, `schema_version` e `summary`.
- [ ] `specific_request` aparece, quando aplicável, somente dentro do valor `summary` do JSON e nunca como campo, mensagem ou instrução independente.
- [ ] O LLM recebe somente histórico bruto com o `context_episode_id` atual mais a mensagem corrente; os eventos anteriores continuam consultáveis no Event Store e não são apagados.
- [ ] Reativação concorrente ou repetida cria no máximo um novo episódio e preserva as respostas `reactivated`/`already_active` e erros atuais para sessão temporária, superseded, encerrada ou inexistente.
- [ ] `handoff_records` permanece byte a byte inalterado pela pausa e reativação, e nenhum campo livre ou leitura do “mais recente” participa da composição do contexto.
- [ ] Uma nova mensagem que por si só contém pedido explícito de humano ou risco válido continua gerando handoff após o reset.
- [ ] O comportamento é coberto com o `EventService` e SRM reais; mocks que apenas provam chegada ao pipeline não contam como regressão suficiente.
- [ ] Dois workers simulados, cada um com cache HANDOFF do episódio antigo, observam o novo `context_episode_id`, substituem a única entrada mantida sob o mesmo `session_id` e processam em modo normal; cada worker continua com no máximo um estado process-local para a sessão.
- [ ] Logs de fallback/conflito identificam tenant, sessão, campo e motivo, sem conteúdo de mensagem, nome, telefone ou resumo.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `bd7a98f88b13ecd989d4b9526a6c668c6a782a68111c0119c56ac4269b69a0da`
- **Reviewer:** /root/design_review_2
- **Reviewed at:** 2026-10-01T01:31:23.000Z
<!-- genie-design-review:end -->
