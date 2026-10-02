# Brainstorm: Retomada contextual da IA após handoff

| Field | Value |
|-------|-------|
| **Slug** | `handoff-contextual-resumption` |
| **Date** | 2026-09-30 |
| **WRS** | 100/100 |

## Readiness

WRS: ██████████ 100/100

- Problem ✅ — ao reativar a IA depois de um atendimento humano, o estado anterior ainda conduz a decisão e pode disparar o mesmo handoff outra vez.
- Scope ✅ — a demanda ativa reinicia na mesma sessão; nome e condição de paciente sobrevivem; o resumo cobre somente o episódio IA pré-handoff, sem capturar a conversa humana.
- Decisions ✅ — mesma sessão com fronteira durável de episódio, reset seletivo, histórico pós-fronteira e resumo bounded separado; a primeira mensagem substantiva é atendida diretamente.
- Risks ✅ — foram identificados riscos de apagar identidade, reintroduzir a intenção antiga via histórico, divergir entre reativação manual/automática/canais e misturar esta entrega com a configuração de stand-by.
- Criteria ✅ — já existem cenários observáveis de não recorrência do handoff, preservação do nome e continuidade com contexto.

## Problem

Depois que um handoff termina e a IA é reativada, a conversa precisa voltar a descobrir a demanda atual da pessoa. Hoje, informações da demanda que originou o handoff continuam influenciando o fluxo e podem fazer a IA encaminhar a pessoa novamente sem um novo motivo. Ao mesmo tempo, a retomada não pode parecer uma conversa sem memória: a IA deve preservar a identidade conhecida, especialmente o nome, e receber um resumo das interações anteriores como contexto histórico.

## Evidence from the repository

- A reativação manual atual somente limpa `sessions.is_paused` e `sessions.paused_until`; ela não altera o estado cognitivo nem estabelece uma fronteira de contexto (`src/repositories/session.py`, `src/services/session_service.py`).
- O estado cognitivo persistido contém etapa do funil, histórico/última intenção, slots, flags e contadores. O prompt sabe consumir `cognitive_state.slots.lead_name`, mas o fluxo normal não possui hoje uma fonte canônica confiável que sempre grave ali o nome informado (`src/models/session.py`, `src/services/event_service.py`).
- O pipeline seguinte recarrega integralmente esse estado e também até 20 eventos anteriores da mesma sessão (`src/services/event_service.py`).
- A chamada ao LLM recebe novamente esse histórico de mensagens; portanto, apenas limpar campos cognitivos não cria por si só uma fronteira segura contra a demanda antiga (`src/services/event_service.py`).
- Intenções de escalada gravam a etapa `standby`; essa etapa não possui transição por nova intenção e seu prompt manda encaminhar para atendimento humano (`packages/neurostack-core/neurostack_core/srm/engine.py`, `funnel_detector.py`, `prompt/templates/normal.py`).
- O decisor de handoff também dispara por `turn_count >= max_turns` e por `flags.handoff_requested`; ambos sobrevivem à reativação atual (`packages/neurostack-core/neurostack_core/srm/handoff_decider.py`).
- O `FlowManager` mantém ainda estado process-local por sessão e oferece `reset_session`, mas a reativação não o chama; a correção não pode depender só desse reset porque múltiplos workers precisam derivar a mesma retomada do estado durável (`packages/neurostack-core/neurostack_core/flow/manager.py`).
- Mensagens do atendente (`fromMe`) e mensagens da pessoa descartadas durante a pausa não são hoje registradas no histórico conversacional usado pelo pipeline; um resumo que inclua o atendimento humano exigirá ampliar explicitamente essa captura (`src/api/routes/webhook.py`).
- O resumo atual de handoff não serve como contrato de memória: ele usa apenas mensagens já persistidas antes do turno gatilho, limita-se às cinco recentes e não é reinjetado na reativação (`src/core/pipeline.py`, `src/services/handoff_service.py`).
- A reativação automática por expiração e a reativação manual seguem caminhos distintos, o que exige um contrato compartilhado para não produzirem retomadas diferentes (`src/api/routes/webhook.py`, `src/repositories/session.py`).
- O wish `handoff-standby-reactivation` trata de quando a IA volta, não de como o contexto conversacional deve ser retomado; este brainstorm permanece separado.

## Scope

### IN

- Criar uma fronteira explícita de retomada após handoff para que intenções, flags e condução da demanda anterior não acionem novamente o mesmo fluxo.
- Preservar dados estáveis já conhecidos da pessoa, no mínimo `lead_name`, para a IA poder chamá-la pelo nome sem perguntar novamente.
- Fornecer ao agente um resumo histórico das interações anteriores como contexto de continuidade, distinguindo-o da demanda ativa atual.
- Definir e persistir uma fonte canônica para o nome conhecido, em vez de depender da janela temporária do histórico ou de chaves divergentes `lead_name`/`user_name`.
- Aplicar o mesmo comportamento à reativação manual e automática e aos canais que usam o mesmo runtime de sessão.
- Responder diretamente quando a primeira mensagem pós-reativação já contém uma nova demanda; perguntar pelo novo objetivo somente diante de saudação ou mensagem neutra.
- Cobrir com testes a retomada sem novo handoff e a preservação contextual.

### OUT

- Alterar os gatilhos normais de handoff quando surge uma nova solicitação real, risco, baixa confiança ou outra regra válida.
- Redesenhar tempo, switch ou política de stand-by; isso pertence ao wish `handoff-standby-reactivation`.
- Apagar o registro auditável de mensagens, eventos ou handoffs anteriores.
- Capturar ou resumir mensagens trocadas entre a pessoa e atendentes humanos durante a pausa.
- Criar memória ilimitada, nova fila, job ou mecanismo de sincronização sem uma necessidade atual que os justifique.

## Simplest complete design candidate

Na transição única de reativação, separar o que é **memória estável** do que é **estado da demanda ativa**: preservar identidade e um resumo histórico bounded; reinicializar apenas os campos que conduzem a demanda/handoff; e marcar uma fronteira para que o histórico bruto anterior não seja tratado como diálogo corrente pelo próximo turno. O mesmo resolvedor de retomada seria chamado por todas as formas de reativação.

Essa proposta adiciona uma única transição de domínio e nenhum job, cache, tabela ou state machine paralela. A forma exata do resumo e da fronteira permanece em aberto até as decisões abaixo.

## Approaches under consideration

### A. Nova sessão a cada retomada

Encerrar a sessão em handoff e criar outra na reativação, copiando perfil e resumo. A fronteira é forte, mas duplica identidade de runtime, complica vínculos com handoff/auditoria, conflita com a resolução de sessão efetiva e adiciona recuperação para criação parcial. Não é a recomendação atual.

### B. Mesma sessão com fronteira de episódio (chosen)

Registrar na própria sessão uma fronteira de retomada e um resumo bounded do episódio anterior. A operação compartilhada de reativação preserva slots estáveis, reinicia somente estado da demanda e faz o carregador enviar ao LLM apenas o resumo histórico mais mensagens posteriores à fronteira. Mantém IDs e auditoria existentes, adicionando somente os estados duráveis pagos pelos requisitos: marcador de fronteira e resumo anterior.

### C. Instrução de prompt sem reset de estado

Manter todo o estado/histórico e acrescentar “não repita o handoff” ao prompt. É insuficiente porque SRM/handoff decidem antes do LLM e ainda enxergam `standby`, contadores e flags persistidos. Rejeitada.

## Simplicity gate

- **Simplest complete design:** abordagem B, com uma única transição de retomada reutilizada por todos os caminhos.
- **Durable state added:** um marcador de fronteira e um resumo histórico bounded; ambos correspondem diretamente ao requisito de separar a demanda atual sem perder contexto.
- **Recovery:** ausência/falha do resumo não pode impedir reativação; usar fallback estruturado a partir de identidade, último handoff e fatos persistidos, sem reabrir a demanda antiga como ativa.
- **Deferred:** nova tabela, múltiplos episódios pagináveis, resumo assíncrono, vetor de memória e configuração por clínica ficam fora até existir uso medido que os exija.
- **Invariants:** identidade estável sobrevive; estado transitório reinicia; histórico auditável nunca é apagado; contexto corrente nunca atravessa a fronteira como conversa ativa.

## Confirmed product decisions

1. Se a primeira mensagem após a reativação já trouxer uma nova demanda, a IA responde diretamente; apenas saudação ou mensagem neutra recebe uma pergunta curta pela nova necessidade, usando o nome quando conhecido.
2. O resumo histórico não inclui nem exige captura das mensagens trocadas com o atendente humano durante a pausa.
3. O estado ativo preserva somente identidade e fatos estáveis mínimos: `lead_name`, `already_patient` e o `Session.user_id` já existente. Tratamento, agendamento, modalidade e reclamações anteriores ficam apenas no resumo histórico, sem conduzir o novo fluxo.
4. A mesma sessão recebe uma fronteira durável no instante efetivo da reativação; reativação manual e expiração automática chamam a mesma transição.

## Risks and constraints

| Risk | Impact | Direction |
|------|--------|-----------|
| Limpar o `cognitive_state` inteiro apaga nome e fatos úteis | High | Reset seletivo por contrato, nunca zeragem total. |
| Manter todo o histórico bruto faz a frase que originou o handoff reaparecer como contexto corrente | High | Fronteira explícita entre histórico anterior e turno corrente. |
| Resumo gerado somente no fim pode não existir em falhas/restarts | High | Definir fallback determinístico a partir de estado/eventos persistidos. |
| Reativação manual e expiração automática executarem regras diferentes | High | Uma operação compartilhada e testes de paridade. |
| Mensagens humanas não entram hoje no mesmo histórico usado pelo pipeline | Medium | Confirmar a expectativa do produto antes de ampliar captura/retensão. |
| Nome vindo do perfil do WhatsApp, texto livre e resumo usar fontes/chaves diferentes | High | Escolher uma fonte canônica e uma precedência explícita antes de prometer personalização. |
| Dados clínicos e PII serem copiados para uma nova memória sem limite | High | Reusar retenção existente e manter resumo bounded, tenant-scoped e sem duplicação desnecessária. |

## Initial acceptance criteria

- [ ] Após um handoff por uma demanda A e posterior reativação, uma mensagem neutra ou uma nova demanda B não reutiliza A como motivo para disparar handoff novamente.
- [ ] Se o nome foi conhecido antes do handoff, a primeira resposta adequada após a retomada pode usar esse nome e não volta a perguntá-lo.
- [ ] O agente recebe contexto histórico explicitamente separado da demanda ativa e consegue dar continuidade sem afirmar que a demanda anterior ainda está em andamento.
- [ ] Reativação manual e automática produzem o mesmo estado conversacional efetivo.
- [ ] Um novo motivo real de handoff após a retomada continua gerando handoff normalmente.
- [ ] Histórico auditável anterior permanece disponível; a reinicialização afeta a condução futura, não os registros passados.

## Revisão pós-council (2026-10-01)

O plano original (WRS 100/100, design e wish plan revisados em SHIP) ficou `BLOCKED` em plan review por um council de cinco lentes depois que `neurostack-core-dedomainification` mergeou (PR #48, verificado por diff zero contra `origin/main`, não apenas por status `done`). O council identificou uma precondição bloqueante já resolvida (B1), uma colisão de gate ainda aberta (B2: `parity_tests_unchanged()` rejeitaria os arquivos de teste que este wish precisa tocar), oito emendas obrigatórias (A1–A8, principalmente sobre consistência do identity map ORM, escritor único na coluna `cognitive_state`, geração de UUID de episódio, resumo sem PII, e supressão do prompt de primeiro contato) e três decisões de design em aberto (D1–D3).

Decisões humanas explícitas, registradas em `DESIGN.md` (linhas 14–16 da tabela de Decisions):

- **D1 — timing do contexto:** manter o congelamento na pausa (não compor na reativação), mesmo pagando a disciplina adicional de escritor único e `refresh()`/`expire()` do identity map para eliminar A1/A2 por disciplina de implementação em vez de eliminá-los arquiteturalmente.
- **D2 — mensagens do paciente durante a pausa:** passam a ser retidas com a retenção já existente da sessão (deixam de ser descartadas), mas continuam fora do contexto automático enviado à IA.
- **D3 — tamanho da entrega:** permanece uma única entrega; todas as emendas A1–A8 e a exceção de gate B2 precisam estar resolvidas em `DESIGN.md` antes de uma nova revisão de plano, em vez de dividir em bug fix + feature de continuidade.

A emenda B2 foi resolvida com uma exceção nomeada por arquivo (não por diretório) em `parity_tests_unchanged()` para `tests/flow/test_flow_manager.py`, e com a suite adversarial nova vivendo em `tests/srm/test_handoff_context_injection.py` — fora da árvore de `tests/srm/test_prompt_injection.py`, que é hoje `TestPromptConfigInjection` (paridade de configuração de clínica, não defesa de prompt injection). As emendas A1–A8 foram incorporadas ao `DESIGN.md` (seções "Escritor único e consistência do identity map", parágrafo de UUID/classificação em "Episódio novo na mesma sessão", parametrização do `ConversationSummarizer` e modo de saída sem PII em "Resumo bounded sem conversa humana", e caminho de funil de retomada em "Fronteira do histórico e prompt"), com critérios de aceite e riscos/mitigações correspondentes adicionados.

Próximo passo: nova revisão de design independente sobre o `DESIGN.md` revisado, seguida de atualização do `WISH.md` e nova revisão de plano antes de reabrir execução.
