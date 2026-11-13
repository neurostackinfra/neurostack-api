# Design: Débito automático da cortesia (todo builder com cortesia é cobrado dela, inclusive pelo Uazapi e pela Evolution)

| Field | Value |
|-------|-------|
| **Slug** | `debito-cortesia-automatico` |
| **Date** | 2026-11-12 |
| **WRS** | 100/100 |

## Problem

Um builder que recebe créditos de cortesia tem o consumo só "medido, não cobrado": o saldo de cortesia não cai com o uso real das clínicas dele, inclusive pelo WhatsApp via Uazapi (o canal usado hoje) e via Evolution, e nada para o agente quando a cortesia acaba. Hoje a cobrança real só existe para builders com marca manual de operador (`credit_charge_mark`) e só em Z-API, Meta e `POST /v1/events`; Uazapi e Evolution não têm adaptador cobrado e o tráfego não autenticado vale zero por regra.

## Scope

### IN

**1. A regra de cortesia, calculada na hora, sem tabela nova.** Definições (todas lidas de `credit_lots` e das somas de entradas existentes, atrás do portão de esquema da cortesia `COURTESY_SCHEMA_MIN_REVISION` = `046`):
- **saldo gastável de cortesia** = soma do gastável dos lotes `courtesy` não vencidos (a mesma leitura de `binding_coverage(include_courtesy=True)`);
- **plano pago em vigor** = lote `monthly` ou `topup` com `expires_at` no futuro (qualquer saldo), **ou** assinatura ASAAS ativa dentro de `binding_grace_end` do último ciclo (`credit_binding_grace_days`, hoje 5 dias), lida de `billing_subscriptions` e dos bindings, sem escrita;
- **builder sob a regra** = sem enforcement global, sem marca de operador e com ao menos um lote `courtesy` recebido, em qualquer data;
- estados de um builder sob a regra: **cobrado** (saldo gastável de cortesia > 0), **bloqueado** (saldo = 0 e nenhum plano pago em vigor), **protegido** (saldo = 0 e algum plano pago em vigor; comportamento observacional de hoje);
- builder que nunca recebeu cortesia, builder marcado e global setado: comportamento de `prod-fe800aec`, byte a byte.

**2. Novo valor de origem no leitor único do modo.** `CreditRepository.effective_enforcement(builder_id)` passa a devolver `origin = "courtesy"` com:
- `at` = `occurred_at` da **primeira** concessão de cortesia do builder (estável);
- `state` ∈ {`charged`, `blocked`}; o estado **protegido** devolve `origin = "none"` (observacional);
- igualdade de `EffectiveEnforcement` passa a comparar `(origin, at, state)`;
- precedência global > marca > cortesia.

Consequência de `at` estável (aceita): uma janela de contabilidade que começa depois de `at` abre como manutenção cobrada; a janela em que `at` cai abre como baseline grátis, e no mês da primeira concessão só os uploads acima da cobertura são cobrados.

**3. Débito só de cortesia, sem dívida, a partir do modo capturado.** Sob `origin = "courtesy"`, o filtro "só lotes `courtesy`" e o "sem dívida" vêm do modo capturado na pré-checagem e valem até o débito da fase 1 e da final, sem nova releitura:
- FEFO entre lotes `courtesy`; o último débito de trace antes de zerar pode ser parcial e o resto é perdoado (sem `_new_debt`);
- sob a regra o débito **não** liquida dívida aberta (`_settle`/`_settle_started_allowances` não rodam);
- lotes `monthly` e `topup` nunca são tocados pela regra;
- vale para traces (decisão entregável: resposta ou handoff), abertura de janela, uploads de RAG e mídia e egress acima do incluído;
- no armazenamento, o não pago não vira dívida mas fica fora da cobertura: a alocação (`credit_allocations`/`credit_inventory`) registra só o que a cortesia pagou, a cobertura avança só por esse valor e a próxima reserva da janela cobra a diferença.

**4. Ciclo de vida sob a regra: sem episódio.** Sob `origin = "courtesy"`, `CreditLifecycle.evaluate` não abre, encurta nem transiciona episódio e `request_gate` não age; o worker do ciclo de vida não seleciona builders da regra. Só os **limiares 50/90/100** rodam, com capacidade = `_funded_mc` dos lotes `courtesy` em vigor (concessões menos retiradas), disponível = saldo gastável de cortesia e chave de armação da concessão de cortesia mais recente (`c:{grant_effect_id}`; `arm_key` é VARCHAR(120) sem CHECK de formato).

**5. Bloqueio na hora, em cada portão.** Com o builder **bloqueado**:
- `POST /v1/events` → 402 `credit_suspended`;
- webhooks Uazapi, Evolution, Z-API e Meta → 200 `{"ok": true, "skipped": "credit_suspended"}`, sem sessão, sem resposta e sem aviso de LGPD;
- uploads de RAG e mídia → 402 `credit_suspended` antes de qualquer escrita;
- busca de RAG e download de mídia por chave de API → 402 `credit_suspended`;
- mídia de saída do agente não sai;
- `/v1/credits/state` → `state = "suspended"` com `suspension_due_at`, `deletion_due_at` e `export_deadline_at` nulos;
- o painel (consulta) e a exportação continuam abertos; uploads feitos pelo painel também recebem o 402.

**6. Desbloqueio automático,** sem ação de operador: aparece plano pago em vigor ou uma cortesia nova com saldo (uma concessão nova primeiro paga dívida antiga aberta, se houver; ver Risco 7).

**7. Portões ASAAS fora da regra.** Sob `origin = "courtesy"` (cobrado ou bloqueado), a pré-checagem, a reserva e o débito do ledger (`SpendHold`: `LedgerBook.debit`, a abertura de janela em `CreditLedger.debit` e `_enforced_book`) **não** aplicam `binding_coverage().usable` nem `quarantine_hold`: o débito só toca lotes `courtesy`, que não dependem de fato ASAAS. Isso **substitui, só para a regra**, a Decision 15 do `creditos-cortesia` (débitos recusados sob quarentena) e a cláusula "a cortesia não pode mascarar quarentena" da Decision 11 dele (owner, rodada 5).

**8. Adaptador cobrado do Uazapi** (o caminho da Z-API/Meta), cobrando **só** sob `origin = "courtesy"` (sob marca e global o Uazapi continua observacional):
- `"uazapi"` em `WebhookSource`; `precheck_uazapi` sobre `precheck_webhook`, com `provider_message_id` = `message.messageid` (fallback `message.id`), na mesma derivação de `uazapi_ingress`;
- **claim Redis mantido para todo turno, na posição de hoje** (inclusive os claims adiados de áudio e imagem); o **claim PostgreSQL** (`acquire_webhook_claim`) entra **além** dele, só para turnos cobrados, logo antes da fase 1, como na Z-API/Meta; um 503 depois do claim Redis perde a mensagem (aceito, como na Z-API/Meta);
- a pré-checagem fica **antes** de qualquer download, STT, descrição de imagem ou envio ao paciente; o roteamento fromMe/eco e o 401 da autenticação continuam antes dela; para um builder **bloqueado** com a sessão pausada, a captura só de texto de hoje (`paused_capture`) continua, sem STT e sem envio, para a equipe da clínica não perder a mensagem;
- ramo `uazapi` no `CreditReplySender` (`send_uazapi_text`/`send_uazapi_audio`, credenciais `uazapi_api_url`/`uazapi_token`), com `record` por `_extract_normalised_send_id` e `_store_ai_msg_id_to_db` (o eco da resposta cobrada não pode parecer mensagem da clínica e pausar a sessão), e no publicador da fila;
- a mídia de saída do Uazapi lê o modo do tenant e só cobra com origem `courtesy` (o conjunto estático `PER_BUILDER_PROVIDERS` não basta);
- rotas internas com o mesmo tratamento cobrado da Z-API/Meta: STT/áudio, handoff de áudio longo, encaminhamento de arquivo, imagem, aviso de LGPD;
- com `uazapi_egress_enabled` fechado, o turno não é cobrado (fica no caminho observacional de hoje): nada seria entregue.

**9. Adaptador cobrado da Evolution,** igual ao do Uazapi e também só sob `origin = "courtesy"`:
- `"evolution"` em `WebhookSource`; a mídia de saída da Evolution lê o modo do tenant e só cobra com origem `courtesy`; `precheck_evolution` com `provider_message_id` = `data.key.id`, depois das checagens de agente desligado e de pausa e antes da sessão (como na Z-API), com o bloqueio checado antes da flag de egress (um builder bloqueado não roda o LLM com egress fechado);
- `_is_evolution_call_auto_reply` e as guardas de fromMe continuam antes da pré-checagem; claim Redis de hoje mantido, claim PostgreSQL além dele para turnos cobrados;
- ramo `evolution` no `CreditReplySender` (`send_evolution_text`/`send_evolution_audio`) e no publicador; rotas internas que a Evolution tem, com tratamento cobrado;
- toda mensagem Evolution é de origem `unknown` (não existe verificação de origem para a Evolution), então cobra só pela regra (decisão 3).

**10. Origem `unknown` cobrável só pela regra.** Turnos Uazapi não verificados e todos os turnos Evolution entram no caminho cobrado **somente** sob `origin = "courtesy"`; para global e marca a regra de hoje fica (não autenticado é atendido de graça, Decision 25 do sistema de crédito). As linhas cobradas carregam a **origem real** do turno no `decision_digest` (não mais `PRODUCTION` fixo nas fases); nenhuma coluna nova.

**11. Corrida.** A re-checagem do modo roda só na pré-checagem e em `_enforced_book`, como hoje para a marca, comparando `(origin, at, state)`; uma mudança (concessão, retirada, vencimento que troca o estado) entre a captura e o lock → 503 `credit_enforcement_unavailable` antes de qualquer escrita. A fase final **não** relê o modo nem dá 503: debita o que houver de cortesia e perdoa o resto. Uma falha de leitura da regra depois de uma captura `courtesy` responde 503 só para aquele tenant (o D1 da marca, estendido). A detecção de troca de builder entre a captura e a fase final (`reparented_since_capture`, hoje só para `origin == "mark"`) passa a valer também para `origin == "courtesy"`, para um tenant movido não ser debitado na cortesia do builder novo.

**12. Workers.** Os gatilhos de processo do publicador da fila de saída (`CreditResponsePublisher._tick`) e do passe de órfãos ligam também quando existe algum lote `courtesy`, para recuperar respostas cobradas pendentes.

**13. Telas e comandos.**
- `/billing`: `mode = enforced` sem o aviso "Modo observacional" para o builder **cobrado**; "Serviço suspenso por falta de créditos" para o **bloqueado** (a compra de plano existente fica igual); barra de consumo e "Alertas atingidos" na base da capacidade de cortesia, com `/v1/credits/thresholds` na mesma base e chave.
- Os avisos de 50/90/100% são **registro + métrica + `/billing`**; nada é enviado por e-mail ou WhatsApp.
- master-admin: um campo só leitura com o estado da regra (cobrado/bloqueado) na linha do builder; o campo `charging` da marca não muda.
- `credit_charge_mark status` mostra a origem `courtesy`; `mark` passa a recusar `window_already_opened` também por uma abertura de janela feita sob a regra no mês corrente (aceito).
- O texto de confirmação do "Tirar tudo" da cortesia avisa que, sem plano pago, o builder fica bloqueado na hora.
- O docstring de `uazapi_enforced_credit_adapter_available()` e o wish `uazapi-webhook-auth` passam a dizer que o adaptador vem deste design e que a guarda do L10 continua fechada (ninguém abre um `uazapi-credit-enforcement` duplicado).

**14. Lista pré-deploy (somente leitura), mostrada ao owner antes do deploy:** por builder com cortesia, o estado que ele terá no instante do deploy (cobrado, bloqueado ou protegido), o saldo de cortesia, a estimativa de reserva de armazenamento da primeira janela que abrir depois do deploy (a corrente, se a primeira concessão veio antes dela), as clínicas por canal (Uazapi, Evolution, Z-API, Meta), o tráfego de `POST /v1/events` por chave de API sem `Idempotency-Key` nos últimos 7 dias (a leitura exata é definida no wish: esse tráfego não deixa claim de ingresso), a assinatura ativa e o `coverage.usable`.

**15. Testes** focados e o gate PostgreSQL de crédito (`scripts/check-credit-integration.sh`, 0 pulados); sem `make quality` (ordem do owner).

### OUT

- **Simulador do painel** (origem `simulator`): não desconta.
- **Guarda do L10** (`uazapi_enforced_credit_adapter_available()` sempre `False`, `arm` sempre recusado): fica fechada. Gatilho: planejamento do L10 (relatório de corte, tratamento de `unknown` no corte, decisão do owner).
- **Marca de operador e global:** sem mudança de comportamento; os adaptadores Uazapi e Evolution não cobram sob eles; a allowlist de canais do `mark` não muda.
- **Mensagem ao paciente quando bloqueado:** nenhuma (silêncio, decisão do owner).
- **Avisos por e-mail ou WhatsApp ao builder:** não existem hoje e não entram.
- **Autenticação do webhook Uazapi:** decisão separada do owner (runbook `uazapi-webhook-auth.md`).
- **Mensagens nunca cobradas:** fromMe da equipe, ecos, grupos e mídia pulada pelas guardas de hoje.
- **Preço, ASAAS, assinatura, checkout, pedidos, reconciliação:** nada é tocado; nenhuma tabela `billing_*` é escrita; nenhuma migration nova.
- **Tenants sem builder** (`builder_id` nulo, inclusive o tenant da casa dos builders): fora da regra.

## Approach

**Escolhida (decisão do owner): A, adaptadores cobrados completos de Uazapi e Evolution + origem `courtesy` no leitor único do modo.** Uazapi e Evolution passam a usar o mesmo caminho cobrado da Z-API e da Meta (pré-checagem, claim PostgreSQL além do Redis, duas fases, fila de saída, envio cobrado, publicador), com cobrança exatamente uma vez e reenvio. A regra de cortesia entra como mais um valor de `effective_enforcement`, de modo que os pontos de decisão existentes (entrada, débito, janela, ciclo de vida, telas) a obedecem sem caminho paralelo; o que muda dentro deles é a semântica sob `origin = "courtesy"`: gastar só cortesia a partir do modo capturado, sem dívida, sem episódio, bloquear na hora, avisos sobre a capacidade de cortesia.

**Alternativa considerada: B, portão na entrada + débito depois da decisão** no ponto onde todo canal já grava o efeito observacional, sem claim PostgreSQL nem fila. Menor, mas só "pelo menos uma vez" nas bordas e sem base para cobrar créditos pagos pelo Uazapi depois. O orquestrador recomendou B pelo critério de simplicidade; o **owner escolheu A**.

**Alternativa descartada:** marcar cada builder com cortesia pela marca de operador. Operação manual, cobra todos os lotes e tem carência de 72 h, contra as decisões 1, 4 e 8.

## Simplicity Case

- **Simplest complete design:** um valor novo (`courtesy`) no leitor de modo existente, filtro de tipo de lote e "sem dívida" no débito capturado, ciclo de vida inerte sob a regra salvo os limiares, um bloqueio calculado nos portões de entrada, e dois adaptadores feitos com as peças da Z-API/Meta.
- **Added machinery (cada uma paga por um requisito presente):**
  - adaptadores Uazapi e Evolution: decisões 5 e 12;
  - origem `courtesy` com filtro só-cortesia, sem dívida e sem liquidação: decisão 4;
  - bloqueio calculado nos portões, sem episódio: decisões 2, 7 e 8;
  - proteção por plano pago em vigor: decisão 6;
  - limiares na base da cortesia: decisão 10;
  - débito de armazenamento sob a regra: decisão 9;
  - `unknown` cobrável só sob a regra: decisão 3;
  - gatilhos dos workers por lote `courtesy`: reenvio, que é o benefício que a decisão 5 compra.
- **Deferred until measured:** destravar o L10 (planejamento do L10); mensagem ao paciente bloqueado (pedido do owner); aviso push ao builder (pedido do owner); cache da leitura da regra (latência medida acima do orçamento atual).
- **Complexity removed:** nenhuma tabela, migration ou janela nova; nenhum estado de bloqueio gravado (recalculado dos lotes; desbloqueia sozinho); nenhum episódio, carência ou prazo sob a regra; marca, global e guarda do L10 sem mudança; claim Redis do Uazapi e da Evolution sem mudança de posição.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Todo builder com cortesia entra na regra automaticamente, sem marca. | Owner (rodada 1). |
| 2 | Cortesia esgotada → o agente do builder é bloqueado. | Owner (rodada 1). |
| 3 | Todas as mensagens descontam, verificadas ou não; `unknown` entra no caminho cobrado só sob a regra. | Owner (rodada 1), risco de mensagens forjadas aceito (Riscos 1 e 2). Restringir à regra mantém a Decision 25 do sistema de crédito para global e marca. |
| 4 | Só lotes `courtesy` são debitados pela regra; o resto é perdoado, sem dívida e sem liquidar dívida antiga. | Owner (rodada 1). Dívida só existiria para ser paga por lotes pagos, que a regra não toca. |
| 5 | Abordagem A: adaptador cobrado completo do Uazapi. | Owner (rodada 2), contra a recomendação B do orquestrador; registrado. |
| 6 | Cortesia + plano pago em vigor: cortesia descontada, nunca bloqueado. Plano pago em vigor = `monthly` ou `topup` com `expires_at` futuro, ou assinatura ASAAS ativa dentro da tolerância do ciclo (`credit_binding_grace_days`). | Owner (rodadas 2 e 5). A tolerância é a mesma dos outros pagantes: uma renovação paga com atraso não bloqueia. Lotes por vencimento, não por saldo (Risco 6). |
| 7 | Cortesia vencida com saldo, sem plano pago em vigor → bloqueia, como esgotada. | Owner (rodada 2). |
| 8 | Bloqueio na hora, sem carência e sem episódio. | Owner (rodada 2). |
| 9 | Respostas e armazenamento descontam da cortesia. | Owner (rodada 3). |
| 10 | Avisos de 50/90/100% na base da cortesia, como registro + métrica + `/billing`. | Owner (rodada 3); não existe canal push hoje. |
| 11 | Paciente de builder bloqueado não recebe nada. | Owner (rodada 3). |
| 12 | Evolution com débito automático completo, como o Uazapi. | Owner (rodada 4). |
| 13 | `origin = "courtesy"` com `at` = primeira concessão e `state` ∈ {`charged`, `blocked`}; protegido = `none`; igualdade por `(origin, at, state)`; precedência global > marca > cortesia. | `at` estável evita que cada concessão nova mude a janela e o modo; o estado separa cobrado de bloqueado sem valor de origem extra. |
| 14 | Filtro só-cortesia e sem-dívida vêm do modo capturado; a fase final não relê o modo. | A cortesia acaba por tráfego comum: uma releitura na fase final trocaria o turno para outro modo e debitaria lotes pagos. |
| 15 | Re-checagem só na pré-checagem e em `_enforced_book`; mudança → 503 antes de escrever; falha de leitura após captura `courtesy` → 503 só no tenant. | O mecanismo da marca, sem novo protocolo. |
| 16 | Claim Redis de hoje mantido em todo turno Uazapi/Evolution; claim PostgreSQL além dele nos turnos cobrados. | É o que a Z-API/Meta já fazem; reentrega que cruza uma mudança de modo continua deduplicada. |
| 17 | Sob a regra (cobrado ou bloqueado), pré-checagem, reserva e `SpendHold` do ledger sem cobertura de binding nem retenção de quarentena; substitui, só para a regra, as Decisions 15 e 11 do `creditos-cortesia`. | Owner (rodada 5). O débito só toca cortesia; os portões ASAAS dariam silêncio ao paciente de um builder **cobrado** com plano pago (contra a decisão 6). |
| 18 | Adaptadores Uazapi e Evolution cobram só sob a regra. | Marca e global ficam como foram entregues. |
| 19 | `uazapi_egress_enabled` fechado → o turno Uazapi não é cobrado. | Nada seria entregue; cobrar seria cobrar sem serviço. |
| 20 | Gatilhos do publicador e do passe de órfãos também por lote `courtesy`. | Sem isso, respostas cobradas pendentes nunca seriam reenviadas (produção não tem marcas). |
| 21 | Lista pré-deploy dos builders cobrados, bloqueados e protegidos, com canais, armazenamento, `/v1/events` sem `Idempotency-Key` e `coverage.usable`. | O deploy liga a regra para todos de uma vez. |
| 22 | Sem migration. | A regra lê `credit_lots` e as somas existentes (índice `ix_credit_lots_builder_expires`); `INGRESS_SOURCES` já inclui `uazapi` e `evolution`; nenhum dos seis arquivos de invariante do ASAAS muda. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | **Mensagens forjadas no Uazapi** sem autenticação gastam a cortesia e, pelo bloqueio na hora, deixam o agente mudo para pacientes reais. | High (aceito pelo owner) | Risco aceito (decisão 3). Mitigação pronta e separada: `uazapi_webhook_auth_enforced` (runbook; duplicidade resolvida e critério dispensado pelo owner em 2026-11-12). |
| 2 | **Mensagens forjadas na Evolution:** o mesmo risco, **sem mitigação disponível** (a Evolution não tem verificação de origem). | High (aceito pelo owner) | Risco aceito (decisões 3 e 12). Gatilho para uma autenticação da Evolution: o primeiro abuso observado ou pedido do owner. |
| 3 | **Bloqueio em massa no deploy:** builders com cortesia antiga esgotada ou vencida e sem plano ficam mudos no instante do deploy. | High | Decisão 21: lista somente leitura antes do deploy, com o owner ciente; cortesia nova antes do deploy, se ele quiser. |
| 4 | **Contrato do `POST /v1/events` muda no deploy:** sob a regra, uma integração por chave de API sem `Idempotency-Key` passa a receber 400 `idempotency_key_required`. | High | A lista pré-deploy conta esse tráfego por builder; o owner aceita ou avisa os builders antes. |
| 5 | **Cortesia que some num armazenamento grande:** a janela seguinte ou um upload grande consome a cortesia e bloqueia na hora. | Medium | Decisão 9 (aceita). Limiares 50/90/100 no `/billing`; a lista pré-deploy mostra a reserva estimada. |
| 6 | **"Plano pago em vigor" por vencimento:** uma recarga zerada protege até vencer; um plano cancelado protege até o fim do ciclo mais a tolerância; um lote mensal estornado conta até vencer. Renovação paga com atraso não bloqueia dentro da tolerância (decisão 6). | Low | Definição explícita (decisão 6). |
| 7 | **Concessão nova primeiro paga dívida antiga** (`grant_courtesy` → `_settle`): um builder com dívida antiga pode continuar bloqueado depois da concessão. Retirada ("Tirar tudo") sem plano bloqueia na hora. | Medium | Texto do diálogo de retirada (item 13); o `status` e a lista mostram a dívida aberta. |
| 8 | **Ordem dos webhooks Uazapi/Evolution** com a pré-checagem e o claim PostgreSQL novos pode duplicar ou perder mensagem. | High | Decisão 16 (Redis mantido); pré-checagem antes de downloads e envios; testes de reentrega, de mudança de modo e de corrida no gate PostgreSQL; o owner acompanha o primeiro tráfego. |
| 9 | **Rotas internas** (STT, áudio longo, arquivo, imagem, LGPD) sem o tratamento cobrado. | Medium | Cada rota interna como item com teste próprio, espelhando a Z-API/Meta. |
| 10 | **Egress fechado:** com o Uazapi fechado, turnos não são cobrados nem entregues. | Low | Decisão 19. |
| 11 | **Custo no caminho quente:** a regra lê lotes e somas a cada entrada de builder sem global nem marca. | Low | Uma leitura indexada por builder, medida no gate; cache só se medido acima do orçamento. |
| 12 | **Rollback** para `prod-fe800aec` desliga a regra; débitos ficam; linhas pendentes de Uazapi/Evolution na fila nunca saem pela imagem antiga (o envio dela aceita só `zapi`/`meta` e o publicador fica parado sem marcas) e a limpeza de retenção as apaga. | Medium | Rollback documentado; o claim Redis mantido (decisão 16) segue deduplicando reentregas depois do rollback. |
| 13 | **Relatório de corte do L10:** tráfego cobrado pela regra não grava efeito observacional e gera `message_without_observation`; a origem real fica só no `decision_digest`. | Low | Nota no runbook de armar; a guarda do L10 continua fechada. |

## Success Criteria

- [ ] Builder **cobrado** (sem global, sem marca): cada decisão entregável (resposta ou handoff) em Uazapi (verificada ou não), Evolution, Z-API, Meta e `POST /v1/events` grava 1.000 mc de débito em lotes `courtesy` (FEFO entre eles; o último pode ser parcial); reentrega do mesmo id do provedor não debita de novo.
- [ ] Abertura de janela (depois da janela da primeira concessão), uploads de RAG/mídia e egress acima do incluído debitam só lotes `courtesy`, e a alocação registra só o que a cortesia pagou.
- [ ] Nenhum débito da regra toca lote `monthly` ou `topup`, cria `credit_debts` ou liquida dívida aberta; uma releitura que trocaria o modo na fase final não muda o débito.
- [ ] Sob a regra nenhum episódio é aberto, encurtado ou transicionado, e o worker do ciclo de vida não seleciona builders da regra.
- [ ] Builder **bloqueado**: `/v1/events` 402 `credit_suspended`; webhooks Uazapi, Evolution, Z-API e Meta 200 `skipped: credit_suspended` sem sessão nem resposta; uploads 402 antes de escrever; busca RAG e download por chave de API 402; mídia de saída não sai; `/v1/credits/state` `suspended` com prazos nulos.
- [ ] Builder **protegido** (cortesia zerada + plano pago em vigor, inclusive assinatura ativa dentro da tolerância do ciclo): nunca bloqueado, comportamento observacional.
- [ ] Builder **cobrado** com quarentena ASAAS ou `coverage.usable` falso: continua atendido e debitado só da cortesia (sem `credit_pending`).
- [ ] Concessão nova com saldo ou plano pago em vigor desbloqueia sem operador.
- [ ] Limiares 50/90/100 na base da cortesia, um por limiar por concessão, visíveis no `/billing`.
- [ ] Builder sem cortesia e builder marcado: SQL e respostas idênticos a `prod-fe800aec` (testes de dominância), inclusive Uazapi e Evolution sem cobrança sob a marca; sob o global (inalcançável com a guarda do L10 fechada) o comportamento de hoje também não muda.
- [ ] Corrida: concessão, retirada ou vencimento entre a captura e o lock → 503 sem escrita; a fase final nunca dá 503 pela regra; tenant movido de builder entre a captura e a final não é debitado na cortesia do builder novo.
- [ ] Builder bloqueado com sessão pausada no Uazapi: a mensagem do paciente continua capturada só como texto para a equipe, sem STT e sem envio.
- [ ] Claim Redis presente em todo turno Uazapi/Evolution; claim PostgreSQL só nos cobrados; reentrega que cruza uma mudança de modo não dá segunda resposta.
- [ ] Publicador e passe de órfãos rodam com lote `courtesy` existente e recuperam uma resposta cobrada pendente.
- [ ] Com `uazapi_egress_enabled` fechado, nenhum turno Uazapi é cobrado.
- [ ] Lista pré-deploy (somente leitura) entregue ao owner antes do deploy.
- [ ] Testes focados e o gate PostgreSQL de crédito verdes, 0 pulados; `make quality` não rodado (ordem do owner), declarado no handoff.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `622f4b9df4007cce526c982b40256a61fb65c39b05556c6d8be2ae09394371eb`
- **Reviewer:** genie-reviewer/claude-opus-5-5
- **Reviewed at:** 2026-11-12T03:14:56.000Z
<!-- genie-design-review:end -->
