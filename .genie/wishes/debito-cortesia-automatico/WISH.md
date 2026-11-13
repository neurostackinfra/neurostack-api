# Wish: Débito automático da cortesia (todo builder com cortesia é cobrado dela, inclusive Uazapi e Evolution)

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `debito-cortesia-automatico` |
| **Date** | 2026-11-12 |
| **Author** | Claude (orquestrador), a pedido do owner |
| **Appetite** | large |
| **Branch** | `wish/debito-cortesia-automatico` |
| **Repos touched** | `neurostack-api` (código); ao vivo, `neurostackinfra/neurostack-infra-terraform` (só o `image_tag`) |
| **Design** | [DESIGN.md](../../brainstorms/debito-cortesia-automatico/DESIGN.md) |

## Summary

Todo builder que recebe créditos de cortesia passa a ter o uso descontado **só** da cortesia, automaticamente, em todos os canais (Uazapi e Evolution inclusive, por adaptadores cobrados completos no mesmo caminho da Z-API/Meta), e o agente dele é bloqueado na hora quando a cortesia acaba ou vence sem plano pago em vigor. A regra é um novo valor (`courtesy`) do leitor único de modo `effective_enforcement`, calculado dos lotes e das assinaturas existentes, sem migration; marca de operador, global e guarda do L10 ficam como estão.

## Scope

### IN

Tudo o que o [DESIGN](../../brainstorms/debito-cortesia-automatico/DESIGN.md) lista em IN (itens 1 a 15), mais as oito pendências não bloqueantes da rodada 3 da revisão do design (DRAFT, "Revisão do design, rodada 3") e a decisão 15 do owner (DRAFT, "sexta rodada"), que viram critérios explícitos:

- **Regra de cortesia** (DESIGN itens 1–2): `origin = "courtesy"` com `at` = primeira concessão e `state` ∈ {`charged`, `blocked`}; protegido = `none`; precedência global > marca > cortesia; portão de esquema `046`.
- **Plano pago em vigor** com a tolerância só para assinatura **ativa** (MEDIUM-1), a fonte do "último ciclo" fixada e o atraso interno protegido (MEDIUM-3 + decisão 15 do owner).
- **Débito só de cortesia, sem dívida e sem liquidação, pelo modo capturado** (item 3), com a regra de armazenamento (LOW-1).
- **Ciclo de vida inerte sob a regra**, salvo os limiares na base da cortesia (item 4).
- **Bloqueio na hora em cada portão** (item 5) e desbloqueio automático (item 6).
- **Portões ASAAS fora da regra** (item 7, owner rodada 5), com o reporte da rota de estado e da tela (MEDIUM-2) **sem abrir a recarga durante quarentena**.
- **Adaptador cobrado do Uazapi** (item 8), com o bloqueio antes de `uazapi_egress_enabled` (LOW-2).
- **Adaptador cobrado da Evolution** (item 9), sem cláusula de egress (LOW-2).
- **`unknown` cobrável só sob a regra**, origem real no `decision_digest` (item 10).
- **Corrida, D1 e troca de builder** (item 11); **gatilhos dos workers** (item 12).
- **Telas e comandos** (item 13), com os comentários velhos corrigidos (LOW-5).
- **Lista pré-deploy somente leitura** (item 14), com a leitura de `/v1/events` sem `Idempotency-Key` (Decisão 9) e a dívida aberta.
- **Testes** (item 15) com o critério de dominância (LOW-3) e a leitura de assinatura citada (LOW-4).
- **Runbook e docs** da regra, e as notas no runbook de armar e no wish `uazapi-webhook-auth`.

### OUT

- Tudo o que o DESIGN lista em OUT: simulador; guarda do L10 (fica fechada); marca de operador e global sem mudança (Uazapi e Evolution não cobram sob eles); mensagem ao paciente bloqueado; avisos por e-mail ou WhatsApp; autenticação do webhook Uazapi (decisão separada do owner); fromMe, ecos, grupos e mídia pulada; preço, ASAAS, assinatura, checkout, pedidos, reconciliação (nenhuma tabela `billing_*` escrita); tenants sem builder.
- **A regra de recarga não muda:** durante uma quarentena ASAAS o botão Recarregar continua bloqueado (Decisão 4).
- **Nenhuma migration nova** e nenhuma mudança nos seis arquivos de invariante do ASAAS (`scripts/asaas_migration_window.py`, `tests/scripts/test_asaas_migration_window.py`, `scripts/asaas_production_wiring.py`, `scripts/asaas_deploy_hook.py`, `scripts/verify_asaas_producer_image_rollout.py`, `infra/production/asaas-production-wiring.yaml`). Se a execução descobrir que algum precisa mudar, o grupo para e volta ao owner.
- **Nenhum passo ao vivo** sem o "vai" do owner no momento do passo (L2a, L1, L2b, L3 abaixo).
- Editar o DESIGN (digest `622f4b9d…`): qualquer correção de texto dele vira critério aqui.

## Decisions

As decisões 1–22 do DESIGN são o contrato e não se repetem aqui. Decisões deste plano:

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Um wish, uma imagem, **um PR** no fim (consolidação de todos os grupos numa árvore), sem ponte. | Sem migration (DESIGN D22), não há ordem imagem/janela; a regra só faz sentido com os adaptadores juntos (o owner quer tudo vivo junto; um adaptador sozinho seria inalcançável). |
| 2 | **Árvores de partida e posse de arquivos.** G1 parte de `origin/main` (`fe800aec`). G2 e G6 partem de uma cópia da árvore revisada do G1. G3 e G5a partem da árvore revisada do G2. G4 parte da árvore revisada do G3. G5b parte da árvore revisada do G5a. G7 e a consolidação usam a árvore única: G4 + diff do G5a + diff do G5b + diff do G6. `tests/integration/credit_rollout_world.py` e `tests/integration/test_credit_schema_dominance_postgres.py` são do G1 e do G2; em ondas paralelas, só um grupo por onda acrescenta a esses arquivos (G2 na Wave 2, G3 na Wave 3), e os outros põem as fixtures nos próprios arquivos de teste. Nada vai para a `main` antes da consolidação. | Os grupos compartilham módulos (`credit_metering.py`, `webhook.py`, `reply_media.py`, `credit_lifecycle.py`); a consolidação por patches só funciona com arquivos disjuntos entre ramos paralelos. |
| 3 | **MEDIUM-1:** assinatura **inativa ou cancelada não tem tolerância**; protege só até o `expires_at` do lote mensal. O Risco 6 do DESIGN, que fala em "fim do ciclo mais a tolerância" para plano cancelado, está errado e fica corrigido aqui. | `binding_grace_end` (`src/services/credit_lifecycle.py:175`) não dá tolerância a assinatura inativa; o DESIGN não pode ser editado sem novo carimbo. |
| 4 | **MEDIUM-2 + revisão do plano H1:** sob a regra, `pending_reasons` mantém `quarantine` e nunca recebe `reconciliation`; `state` é o da regra (`active` para cobrado, `suspended` para bloqueado), nunca `pending` por quarentena; o `/billing` separa exibição de recarga: o cobrado vê o saldo de cortesia sem "Pendente", e a **recarga continua bloqueada com `quarantine`** (inalterada). **Só `active` + `quarantine` (combinação que só a regra produz) passa a mostrar o saldo sem "Pendente"**; `suspended` + `quarantine` fica como hoje (caixa "Pendente" com "Serviço suspenso por falta de créditos" dentro, recarga bloqueada), igual para o builder marcado. Os saldos por tipo de lote continuam como hoje em `/v1/credits/summary`; sob a regra, o "Disponível líquido" do `/billing` é o saldo gastável de cortesia. | Coerência com DESIGN item 7 sem mexer na regra de recarga (dinheiro), que fica OUT. `creditLedgerStanding` (`billing-view.ts:120-131`) hoje decide as duas coisas juntas. |
| 5 | **Plano pago em vigor (MEDIUM-3 + decisão 15 do owner):** lote `monthly`/`topup` com `expires_at` futuro (qualquer saldo, qualquer quarentena), **ou** assinatura **ativa** dentro de `binding_grace_end` contado do `cycle_end` do último binding gravado, **ou** assinatura **ativa** cujo ciclo corrente já existe em `billing_subscription_cycles` **em estado pago** (liquidado ou restaurado; um ciclo estornado não conta) sem binding gravado (atraso interno do consumidor de fatos: protegido enquanto durar; o engenheiro confirma a coluna de estado do ciclo no código). A leitura **não** reaproveita `binding_coverage` (que retorna antes de calcular os lotes quando há quarentena e exige saldo positivo na recarga). | Owner rodadas 2, 5 e 6 ("assinatura ativa protege"; "atraso interno continua protegido"). |
| 6 | **LOW-1:** quando a cortesia paga 0 numa cobrança de armazenamento, não se cria efeito e a alocação é gravada com `charged_mc = 0` e `effect_id` nulo. | O CHECK em `src/models/credit_inventory.py:116-119`. |
| 7 | **LOW-2:** no Uazapi o bloqueio é checado **antes** de `uazapi_egress_enabled`; a Evolution não tem flag de egress e não ganha cláusula de egress (o DESIGN item 9 erra ao citar uma). | Um bloqueado não roda o LLM com egress fechado; `egress_policy` só tem as flags financeira e do Uazapi. |
| 8 | **LOW-3/LOW-4:** dominância = "escritas e respostas idênticas a `prod-fe800aec`; a única leitura nova é a da regra", e a leitura de assinatura/binding/ciclo só acontece com saldo de cortesia zero. | A regra acrescenta leituras; o DESIGN D22 e o Risco 11 não citam a de assinatura. |
| 9 | **Leitura de `/v1/events` sem `Idempotency-Key`** na lista pré-deploy: efeitos observacionais de `/v1/events` com principal autenticado (chave de API) e origem `production` sem claim de ingresso correspondente, por tenant, nos últimos 7 dias. | Esse tráfego não deixa claim; conta chaves ausentes e inválidas, que sob a regra recebem 400. |
| 10 | A lista pré-deploy é um **comando de operador somente leitura** (`python -m src.ops.credit_courtesy_rule status`), em `REPEATABLE READ READ ONLY`, saída JSON, sem escrita, e sem nomear `credit_enforcement_marks` (a cerca AST do `credit_charge_mark` pega). | Mesmo padrão de `credit_charge_mark status`; roda como tarefa pontual em `neurostack-prod`. |
| 11 | **G5 dividido** em G5a (backend: rotas, estado, comandos, comentários) e G5b (frontend: `/billing`, master-admin, diálogo de cortesia). | A revisão do plano pontuou o G5 único em 7; separado, cada parte fica num papel e num conjunto de arquivos. |
| 12 | Sem `make quality` (ordem do owner): cada grupo roda testes focados, o gate PostgreSQL de crédito (`scripts/check-credit-integration.sh`, 0 pulados, com o guarda `POSTGRES_SMOKE_DATABASE_URL`) e black/isort/flake8/mypy; o frontend roda vitest, typecheck e build; o handoff diz que o gate completo não rodou. O gate agregado é o gate PostgreSQL consolidado (lista em Success Criteria) + o `final-gate`. | Ordem do owner, a mesma dos wishes anteriores. |
| 13 | **Ordem ao vivo:** L2a (build, ECR, PR de infra, merge humano, apply; o serviço não é tocado) → **L1** (a lista pré-deploy roda numa tarefa pontual da task definition nova, antes de ela servir) → go/no-go do owner → L2b (`update-service`) → L3. No-go = sem `update-service` e um PR de infra que volta o `image_tag`. | A task definition nova só existe depois do apply; a regra só vale quando o serviço troca. |

## Simplicity Case

- **Simplest complete design:** o do DESIGN: um valor novo no leitor de modo, filtro de lote e sem-dívida no débito capturado, ciclo de vida inerte salvo limiares, bloqueio calculado nos portões, e dois adaptadores com as peças da Z-API/Meta. Este plano não acrescenta mecanismo.
- **Added machinery:** só o comando somente leitura da lista pré-deploy (Decisão 10), pago pelo DESIGN D21 e pelo risco de bloqueio em massa no deploy; e a separação exibição/recarga na tela (Decisão 4), paga pela decisão de manter a recarga bloqueada em quarentena.
- **Deferred until measured:** os mesmos do DESIGN (L10, mensagem ao paciente, aviso push, cache da leitura da regra).
- **Complexity removed:** nenhuma migration, janela, ponte ou segundo PR; nenhum estado de bloqueio gravado; nenhum worker novo.

## Dependencies

**depends-on:** cobranca-credito-por-builder, creditos-cortesia
**blocks:** none

Relacionado (não bloqueia): `uazapi-webhook-auth` (a autenticação do webhook Uazapi é a mitigação do Risco 1; este wish acrescenta uma nota a ele).

## Success Criteria

Todos os critérios do DESIGN (seção "Success Criteria"), com a redação corrigida pelos itens abaixo, e mais:

- [ ] **Regra:** para um builder sem global e sem marca, `effective_enforcement` devolve `courtesy`/`charged` com saldo gastável de cortesia > 0, `courtesy`/`blocked` com saldo 0 e sem plano pago em vigor, e `none` com saldo 0 e plano pago em vigor; `at` é o `occurred_at` da primeira concessão; abaixo da revisão `046` a regra não lê nada.
- [ ] **Plano pago em vigor (Decisão 5):** cortesia 0 + assinatura ativa na tolerância + quarentena → `none`; recarga com saldo 0 e `expires_at` futuro → protegido; assinatura inativa ou cancelada sem tolerância (Decisão 3); assinatura ativa com ciclo corrente em `billing_subscription_cycles` sem binding → protegido (atraso interno).
- [ ] **Desbloqueio automático:** concessão nova com saldo ou plano pago em vigor muda `blocked` para `charged`/`none` sem operador; com dívida antiga, a concessão liquida primeiro (Risco 7 do DESIGN).
- [ ] **Débito só de cortesia** pelo modo capturado, sem `credit_debts` novo, sem `_settle`; o `SpendHold` debita sob quarentena; armazenamento com o não pago fora da cobertura e a próxima reserva cobrando a diferença; cortesia que paga 0 → alocação `charged_mc = 0`, `effect_id` nulo, nenhum efeito (Decisão 6).
- [ ] **Janelas:** a janela em que `at` cai abre como baseline grátis; as seguintes abrem como manutenção cobrada só da cortesia; egress acima do incluído (`charge_egress`) cobrado só da cortesia.
- [ ] **Sem episódio** sob a regra; limiares 50/90/100 na base da cortesia, chave `c:{grant_effect_id}` da concessão mais recente, visíveis no `/billing` e em `/v1/credits/thresholds`.
- [ ] **Bloqueio** em cada portão do DESIGN item 5; o painel (consulta) e a exportação abertos, uploads pelo painel com 402.
- [ ] **ASAAS (Decisão 4):** builder cobrado em quarentena ou com `coverage.usable` falso segue atendido e debitado só da cortesia; `/v1/credits/state` com `quarantine` em `pending_reasons`, sem `reconciliation`, `state` da regra; o `/billing` mostra o saldo sem "Pendente" e o Recarregar continua bloqueado.
- [ ] **Uazapi:** `precheck_uazapi` antes de download, STT, descrição de imagem e envio, depois do roteamento fromMe/eco e do 401; bloqueio antes de `uazapi_egress_enabled` (Decisão 7); claim Redis em todo turno e PostgreSQL só nos cobrados; envio com `record` por `_extract_normalised_send_id` e `_store_ai_msg_id_to_db`; publicador; rotas internas (STT/áudio, áudio longo, arquivo, imagem, LGPD); sessão pausada de builder bloqueado mantém a captura só de texto; egress fechado → turno não cobrado.
- [ ] **Evolution:** `precheck_evolution` com `data.key.id`, depois das checagens de agente desligado e de pausa, antes da sessão; guardas de `_is_evolution_call_auto_reply` e fromMe antes; claim Redis mantido, PG nos cobrados; envio e publicador; sem cláusula de egress.
- [ ] **Mídia de saída** Uazapi/Evolution lê o modo do tenant e só cobra com origem `courtesy`; sob marca, comportamento de hoje.
- [ ] **Origem real:** as linhas cobradas de turnos `unknown` (Uazapi não verificado, Evolution) carregam `unknown` no `decision_digest`; as de produção, `production`.
- [ ] **Dominância (Decisão 8):** builder sem cortesia e builder marcado com escritas e respostas idênticas a `prod-fe800aec`; a única leitura nova é a da regra (e a de assinatura/binding/ciclo só com saldo zero).
- [ ] **Corrida:** `(origin, at, state)` comparado na pré-checagem e em `_enforced_book`; mudança → 503 sem escrita; a final nunca dá 503 pela regra; `reparented_since_capture` vale para `courtesy`; D1 por tenant para captura `courtesy`.
- [ ] **Workers:** publicador e passe de órfãos ligam com algum lote `courtesy` e recuperam uma resposta cobrada pendente (Uazapi e Evolution inclusive).
- [ ] **Superfícies:** master-admin com o estado da regra; `credit_charge_mark status` com origem `courtesy`; `mark` recusa `window_already_opened` por abertura sob a regra; texto do "Tirar tudo" avisa do bloqueio; o texto do 402 de `/v1/events` (`src/api/routes/events.py:~192`, hoje "72 hours of debt") correto para a regra; docstring de `uazapi_enforced_credit_adapter_available()` diz que o adaptador cobra só sob `courtesy` e mantém a referência a `uazapi-credit-enforcement` (o teste de pino continua verde, o corpo continua `return False`); comentários de `BindingCoverage.usable_courtesy`, `binding_coverage` e `src/api/schemas/admin_credits.py:127` atualizados (LOW-5).
- [ ] **Lista pré-deploy:** `python -m src.ops.credit_courtesy_rule status` imprime, por builder com cortesia, estado no deploy, saldo, dívida aberta, reserva estimada da primeira janela depois do deploy, clínicas por canal, tráfego de `/v1/events` sem `Idempotency-Key` (Decisão 9), assinatura ativa, atraso do binding e `coverage.usable`; somente leitura, provado por teste.
- [ ] **Docs:** runbook novo da regra (com a lista, o atraso do binding, deploy em L2a/L1/L2b e rollback por PR de infra); `credits.md`, `credit-courtesy.md`, `credit-charge-mark.md`, `credit-enforcement-arming.md`, `uazapi-webhook-auth.md` e o wish `uazapi-webhook-auth` atualizados.
- [ ] **Nada fora do escopo:** `git diff --name-only origin/main -- alembic/versions scripts/asaas_migration_window.py tests/scripts/test_asaas_migration_window.py scripts/asaas_production_wiring.py scripts/asaas_deploy_hook.py scripts/verify_asaas_producer_image_rollout.py infra/production/asaas-production-wiring.yaml` vazio na árvore consolidada, e `git status --porcelain` nos mesmos caminhos também vazio (nenhuma migration nova sem tracking).
- [ ] **Gate agregado:** na árvore consolidada, o gate PostgreSQL de crédito, um caminho por chamada, 0 pulados, nos 25 arquivos de crédito que rodam sem serviços externos (`test_credit_charge_mark_postgres`, `test_credit_charge_workers_postgres`, `test_credit_charge_badge_postgres`, `test_credit_effective_enforcement_postgres`, `test_credit_enforcement_marks_migration_postgres`, `test_credit_enforcement_ops_postgres`, `test_credit_enforcement_rollout_fence`, `test_credit_schema_dominance_postgres`, `test_credit_metering_postgres`, `test_credit_ledger_postgres`, `test_credit_binding_grace_postgres`, `test_credit_retention_postgres`, `test_credit_orphan_reconciliation_postgres`, `test_credit_maintenance_postgres`, `test_credit_courtesy_postgres`, `test_credit_courtesy_http_postgres`, `test_credit_courtesy_migration_postgres`, `test_credit_fact_replay_postgres`, `test_credit_migration_guards_postgres`, `test_credit_orm_ddl_drift_postgres`, `test_credit_legal_hold_barriers`, `test_credit_capability_ready`, `test_credit_object_copy_gc_postgres`, `test_reply_media_postgres`, `test_webhook_media_delivery_postgres`) mais os 4 novos (`test_credit_courtesy_rule_postgres`, `test_credit_uazapi_enforced_postgres`, `test_credit_evolution_enforced_postgres`, `test_credit_courtesy_rule_ops_postgres`); excluídos por precisarem de S3/Qdrant de teste: `test_credit_purge_jobs`, `test_credit_storage_s3`, `test_credit_qdrant_rebuild`, `test_credit_object_copy_gc`. Testes unitários afetados, black/isort/flake8/mypy e vitest/typecheck/build do frontend verdes; `make quality` não rodado (declarado no handoff); `final-gate` independente SHIP.

## Execution Strategy

### Wave 1 (sequencial)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 5 (stateful +2, cost +2, sem teste determinístico do filtro de lote existente +1) | `engineer-complex` / high | Núcleo: regra, leitor, livro, armazenamento, ciclo de vida |

### Wave 2 (paralela, worktrees próprios, arquivos disjuntos)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 6 (routing +2, stateful +2, cost +2) | `engineer-complex` / high | Entrada, corrida e workers no caminho cobrado compartilhado |
| 6 | engineer | 2 (cost +2) | `engineer-standard` / medium | Comando somente leitura da lista pré-deploy |

### Wave 3 (paralela, worktrees próprios, arquivos disjuntos)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 7 (routing +2, stateful +2, cost +2, retrabalho anterior na rota +1) | `engineer-complex` / high, + `final-gate` na consolidação | Adaptador cobrado do Uazapi |
| 5a | engineer | 5 (cost +2, stateful +2, multi-pacote +1) | `engineer-complex` / high | Superfícies do backend |

### Wave 4 (paralela, worktrees próprios, arquivos disjuntos)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 6 (routing +2, stateful +2, cost +2) | `engineer-complex` / high | Adaptador cobrado da Evolution |
| 5b | engineer | 3 (subjective acceptance +2, multi-pacote +1) | `engineer-standard` / high | Superfícies do frontend |

### Wave 5 (sequencial)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 7 | engineer | 1 (docs) | `engineer-trivial` / low | Runbook e docs |
| Consolidação | orquestrador + `final-gate` | — | `final-gate` / highest justified | Árvore única, gate agregado, checagem de escopo, `final-gate` independente |

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** → `engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high; **4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an independent `final-gate` at the highest justified effort.

Cada grupo: engenheiro → revisor independente (≠ engenheiro) → até 2 laços de correção → validação do orquestrador → `genie task done`. Nada de commit, push ou PR sem o owner pedir; o PR único sai depois da consolidação; o merge é humano.

## Execution Groups

Todo bloco de validação que usa PostgreSQL roda com o PostgreSQL descartável do grupo exportado em `POSTGRES_SMOKE_DATABASE_URL`, e o primeiro comando dele é o guarda `: "${POSTGRES_SMOKE_DATABASE_URL:?defina o PostgreSQL de teste}"`.

### Group 1: Núcleo da regra

**Goal:** `effective_enforcement` devolve a origem `courtesy` com `at` e `state`, e o livro, o armazenamento e o ciclo de vida obedecem à regra.

**Deliverables:**
1. Leitura da regra em `src/repositories/credit.py`: saldo gastável de cortesia, primeira concessão, plano pago em vigor pela Decisão 5 (lotes por `expires_at`; tolerância por `binding_grace_end`; ciclo corrente em `billing_subscription_cycles` sem binding), **sem** reaproveitar `binding_coverage`, atrás do portão `046` (`src/services/credit_switch.py`); `effective_enforcement` com `origin = "courtesy"`, `at`, `state`, igualdade `(origin, at, state)`, precedência global > marca > cortesia.
2. `src/services/credit_ledger.py`: sob a regra, pelo modo capturado, débito só de lotes `courtesy` (FEFO entre eles), último parcial, resto perdoado, sem `_new_debt`, sem `_settle`/`_settle_started_allowances`; `SpendHold` sem cobertura de binding nem quarentena (DESIGN item 7).
3. `src/services/credit_inventory.py` e `src/services/credit_allocations.py`: reserva, abertura de janela e egress sob a regra pagos só pela cortesia, cobertura só pelo pago, próxima reserva cobra a diferença, `charged_mc = 0`/`effect_id` nulo quando nada é pago (Decisão 6); `_enforced_book` sem os portões ASAAS sob a regra.
4. `src/services/credit_lifecycle.py`: sob a regra `evaluate` não abre, encurta nem transiciona episódio; `request_gate` inerte; worker não seleciona builders da regra; limiares na base da cortesia com a chave da concessão mais recente.
5. Testes unitários e o arquivo PostgreSQL novo `tests/integration/test_credit_courtesy_rule_postgres.py`; seções novas em `tests/integration/credit_rollout_world.py` e `tests/integration/test_credit_schema_dominance_postgres.py` (posse do G1).

**Acceptance Criteria:**
- [ ] Os critérios "Regra", "Plano pago em vigor", "Desbloqueio automático", "Débito só de cortesia", "Janelas", "Sem episódio" e "Dominância" de Success Criteria, cada um com teste PostgreSQL.
- [ ] Nenhum lote `monthly`/`topup` é tocado e nenhum `credit_debts` nasce sob a regra.
- [ ] `courtesy_removal` continua fora do consumo; os limiares não contam retirada como uso.

**Validation:**
```bash
: "${POSTGRES_SMOKE_DATABASE_URL:?defina o PostgreSQL de teste}" \
 && python -m pytest tests/services/test_credit_ledger.py tests/services/test_credit_lifecycle.py tests/services/test_credit_inventory.py tests/services/test_credit_allocations.py tests/services/test_credit_switch.py tests/services/test_credit_courtesy.py -q -p no:cacheprovider \
 && for f in test_credit_courtesy_rule_postgres test_credit_ledger_postgres test_credit_courtesy_postgres test_credit_effective_enforcement_postgres test_credit_schema_dominance_postgres test_credit_maintenance_postgres test_credit_binding_grace_postgres test_credit_retention_postgres; do bash scripts/check-credit-integration.sh tests/integration/$f.py || exit 1; done \
 && python -m black --check src tests && python -m isort --check-only src tests && python -m flake8 src tests && python -m mypy src --ignore-missing-imports
```
Escopo: núcleo de crédito com estado (ledger, ciclo de vida, inventário, janelas, carência); o gate PostgreSQL de crédito é o gate documentado do repositório para isso.

**depends-on:** none

---

### Group 2: Entrada, corrida e workers no caminho cobrado

**Goal:** as pré-checagens de `/v1/events` e dos webhooks Z-API/Meta capturam a regra, bloqueiam na hora e protegem a corrida, e os workers ligam com cortesia.

**Deliverables:**
1. `src/services/credit_metering.py`: `capture_events_enforcement`/`precheck_events`/`precheck_webhook` com a regra: `blocked` → `credit_suspended` (402 em `/v1/events`, 200 `skipped` nos webhooks); sem cobertura de binding nem quarentena sob a regra; principal não autenticado aceito no caminho cobrado **só** sob `courtesy`; `require_captured_enforcement` comparando `(origin, at, state)`; D1 por tenant para captura `courtesy`; `reparented_since_capture` para `courtesy`; a final nunca dá 503 pela regra; `decision_digest` com a origem real.
2. `src/services/credit_principal.py` ajustado só no necessário para o item 1; `src/api/routes/events.py`: texto do 402 correto para a regra.
3. Gatilhos: `CreditResponsePublisher._tick` e o passe de órfãos (`src/services/credit_rollout.py`) ligam também com algum lote `courtesy`.
4. Portão de bloqueio compartilhado em `src/services/credit_lifecycle.py` (`tenant_suspended`, `reservation_verdict`) reportando o bloqueio calculado da regra; os grupos 3, 4 e 5a só o consomem.
5. Testes unitários e PostgreSQL (seções novas em `test_credit_effective_enforcement_postgres.py`, `test_credit_metering_postgres.py`, `test_credit_schema_dominance_postgres.py`).

**Acceptance Criteria:**
- [ ] Os critérios "Corrida", "Workers" e "Origem real" de Success Criteria, para `/v1/events`, Z-API e Meta.
- [ ] Builder bloqueado: `/v1/events` 402 `credit_suspended`; Z-API/Meta 200 `skipped: credit_suspended` sem sessão nem resposta.
- [ ] Builder cobrado em quarentena ou com `coverage.usable` falso segue atendido (sem `credit_pending`).
- [ ] Sob marca e global, o caminho de hoje (dominância); a cerca do L10 continua verde.

**Validation:**
```bash
: "${POSTGRES_SMOKE_DATABASE_URL:?defina o PostgreSQL de teste}" \
 && python -m pytest tests/services/test_credit_metering.py tests/services/test_credit_principal.py tests/services/test_credit_response_publisher.py tests/services/test_credit_rollout.py tests/api/test_events_credit_enforced.py tests/api/test_credit_suspension_webhooks.py tests/api/test_credit_suspension_events.py -q -p no:cacheprovider \
 && for f in test_credit_effective_enforcement_postgres test_credit_metering_postgres test_credit_orphan_reconciliation_postgres test_credit_enforcement_rollout_fence test_credit_charge_workers_postgres test_credit_schema_dominance_postgres; do bash scripts/check-credit-integration.sh tests/integration/$f.py || exit 1; done \
 && python -m black --check src tests && python -m isort --check-only src tests && python -m flake8 src tests && python -m mypy src --ignore-missing-imports
```
Escopo: o caminho cobrado compartilhado, os workers e a cerca do L10 (que não pode mudar).

**depends-on:** Group 1

---

### Group 3: Adaptador cobrado do Uazapi

**Goal:** sob a regra, o webhook do Uazapi cobra cada decisão entregável exatamente uma vez pelo caminho da Z-API/Meta, e bloqueia na hora.

**Deliverables:**
1. `src/services/credit_metering.py`: `"uazapi"` em `WebhookSource`; `precheck_uazapi` (`provider_message_id` = `messageid`, fallback `id`).
2. `src/api/routes/webhook.py`, rota `/uazapi`: pré-checagem na posição da Decisão 7 e do critério "Uazapi"; claim Redis mantido em todo turno (inclusive os adiados de áudio e imagem), claim PostgreSQL só nos cobrados, logo antes da fase 1; `_run_pipeline_transactionally` com `enforcement=` e `reply=_ReplyTarget("uazapi", …)`; captura só de texto em sessão pausada de builder bloqueado; rotas internas (STT/áudio, handoff de áudio longo, encaminhamento de arquivo, imagem, aviso de LGPD) com tratamento cobrado; egress fechado → turno não cobrado.
3. `CreditReplySender` e publicador com ramo `uazapi` (`send_uazapi_text`/`send_uazapi_audio`, `record` por `_extract_normalised_send_id` e `_store_ai_msg_id_to_db`).
4. `src/services/reply_media.py`: mídia de saída Uazapi lê o modo do tenant e só cobra com `courtesy`; mídia de saída de builder bloqueado não sai.
5. Testes: `tests/integration/test_credit_uazapi_enforced_postgres.py` (novo) e testes de rota do Uazapi (reentrega, mudança de modo entre entregas, corrida, pausa, egress fechado, cada rota interna).

**Acceptance Criteria:**
- [ ] Os critérios "Uazapi" e "Mídia de saída" de Success Criteria.
- [ ] Reentrega do mesmo `messageid` não debita de novo nem dá segunda resposta, inclusive cruzando uma mudança de modo (teste PostgreSQL; não depende de reentrega ao vivo).
- [ ] Turno Uazapi não verificado cobra só sob `courtesy`; sob marca, comportamento de hoje.

**Validation:**
```bash
: "${POSTGRES_SMOKE_DATABASE_URL:?defina o PostgreSQL de teste}" \
 && python -m pytest tests/api/test_webhook_uazapi.py tests/api/test_webhook_egress.py tests/api/test_webhook_ai_msg_dedup.py tests/api/test_webhook_auth_order.py tests/api/test_webhook_media_delivery.py tests/services/test_reply_media.py tests/services/test_credit_reply_media.py tests/services/test_credit_response_publisher.py -q -p no:cacheprovider \
 && for f in test_credit_uazapi_enforced_postgres test_reply_media_postgres test_webhook_media_delivery_postgres test_credit_effective_enforcement_postgres; do bash scripts/check-credit-integration.sh tests/integration/$f.py || exit 1; done \
 && python -m black --check src tests && python -m isort --check-only src tests && python -m flake8 src tests && python -m mypy src --ignore-missing-imports
```
Escopo: a rota mais usada em produção, a ordem dos claims, o eco e a mídia.

**depends-on:** Group 2

---

### Group 4: Adaptador cobrado da Evolution

**Goal:** sob a regra, o webhook da Evolution cobra e bloqueia como o do Uazapi.

**Deliverables:**
1. `src/services/credit_metering.py`: `"evolution"` em `WebhookSource`; `precheck_evolution` (`provider_message_id` = `data.key.id`).
2. `src/api/routes/webhook.py`, rota `/evolution`: guardas de `_is_evolution_call_auto_reply` e fromMe antes; pré-checagem depois das checagens de agente desligado e de pausa, antes da sessão; claim Redis mantido, PG nos cobrados; rotas internas da Evolution com tratamento cobrado; sem cláusula de egress (Decisão 7).
3. `CreditReplySender` e publicador com ramo `evolution` (`send_evolution_text`/`send_evolution_audio`); `reply_media.py` com a mídia de saída da Evolution lendo o modo do tenant.
4. Testes: `tests/integration/test_credit_evolution_enforced_postgres.py` (novo) e testes de rota da Evolution.

**Acceptance Criteria:**
- [ ] O critério "Evolution" de Success Criteria.
- [ ] Reentrega do mesmo `data.key.id` não debita de novo nem dá segunda resposta (teste PostgreSQL).
- [ ] Sob marca, comportamento de hoje (observacional); o Uazapi do G3 não regride.

**Validation:**
```bash
: "${POSTGRES_SMOKE_DATABASE_URL:?defina o PostgreSQL de teste}" \
 && python -m pytest tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_egress.py tests/api/test_webhook_ai_msg_dedup.py tests/services/test_reply_media.py tests/services/test_credit_reply_media.py tests/services/test_credit_response_publisher.py -q -p no:cacheprovider \
 && for f in test_credit_evolution_enforced_postgres test_credit_uazapi_enforced_postgres test_reply_media_postgres; do bash scripts/check-credit-integration.sh tests/integration/$f.py || exit 1; done \
 && python -m black --check src tests && python -m isort --check-only src tests && python -m flake8 src tests && python -m mypy src --ignore-missing-imports
```
Escopo: segundo adaptador nos mesmos arquivos do Grupo 3 (por isso depois dele); os testes do Uazapi rodam de novo para provar que nada regrediu.

**depends-on:** Group 3

---

### Group 5a: Superfícies do backend

**Goal:** as rotas, a rota de estado e os comandos aplicam e reportam a regra.

**Deliverables:**
1. Rotas: uploads de RAG e mídia 402 `credit_suspended` antes de escrever; busca de RAG e download de mídia por chave de API 402 (`src/api/routes/knowledge.py`, `src/api/routes/media_assets.py`), pelo portão do G2.
2. `src/api/routes/credits.py` (e `src/api/schemas/credits.py` se um campo mudar): `/v1/credits/state` `suspended` com prazos nulos para o bloqueado; `pending_reasons`/`state` pela Decisão 4; `/v1/credits/thresholds` na base da cortesia; `mode` correto.
3. master-admin: campo só leitura com o estado da regra (`src/api/routes/admin.py`, `src/api/schemas/admin.py`).
4. `src/ops/credit_charge_mark.py`: `status` com origem `courtesy`; `mark` recusa `window_already_opened` por abertura sob a regra.
5. Comentários: docstring de `uazapi_enforced_credit_adapter_available()` (adaptador cobra só sob `courtesy`; referência a `uazapi-credit-enforcement` mantida; corpo `return False`), `BindingCoverage.usable_courtesy` e `binding_coverage` em `src/services/credit_rollout.py`, `src/api/schemas/admin_credits.py:127` (LOW-5).

**Acceptance Criteria:**
- [ ] Os critérios "Bloqueio" (rotas), "ASAAS" (rota de estado) e "Superfícies" (backend) de Success Criteria.
- [ ] O contrato OpenAPI (`tests/api/test_credit_openapi.py`) atualizado só no que a Decisão 4 exige; o teste de pino da guarda do L10 continua verde.

**Validation:**
```bash
: "${POSTGRES_SMOKE_DATABASE_URL:?defina o PostgreSQL de teste}" \
 && python -m pytest tests/api/test_credits.py tests/api/test_credit_openapi.py tests/api/test_credit_media_assets.py tests/api/test_credit_suspension_rag_media.py tests/api/test_credit_rag.py tests/api/test_credit_category_gate.py tests/api/test_admin.py tests/ops/test_credit_charge_mark.py tests/services/test_credit_rollout.py -q -p no:cacheprovider \
 && for f in test_credit_charge_badge_postgres test_credit_courtesy_http_postgres test_credit_charge_mark_postgres; do bash scripts/check-credit-integration.sh tests/integration/$f.py || exit 1; done \
 && python -m black --check src tests && python -m isort --check-only src tests && python -m flake8 src tests && python -m mypy src --ignore-missing-imports
```
Escopo: rotas HTTP, contrato, um comando e comentários; arquivos disjuntos do G3.

**depends-on:** Group 2

---

### Group 5b: Superfícies do frontend

**Goal:** o `/billing`, o master-admin e o diálogo de cortesia mostram a regra sem abrir a recarga em quarentena.

**Deliverables:**
1. `frontend/artifacts/neurostack/src/pages/billing-view.ts` (e `billing-view.test.ts`): separar "mostrar o saldo" de "oferecer recarga" (Decisão 4); `billing-contract.ts`/`billing-contract.test.ts` se o contrato mudar.
2. `frontend/artifacts/neurostack/src/components/billing/credits-and-topups.tsx`: modo cobrado sem o aviso observacional, "Serviço suspenso" para bloqueado, barra e "Alertas atingidos" na base da cortesia, sem "Pendente" para cobrado em quarentena.
3. `frontend/artifacts/neurostack/src/pages/master-admin.tsx` (e teste): estado da regra na linha do builder.
4. `frontend/artifacts/neurostack/src/components/admin/courtesy-credits-dialog.tsx` (e teste): o "Tirar tudo" avisa que, sem plano pago, o builder fica bloqueado na hora.

**Acceptance Criteria:**
- [ ] Os critérios "ASAAS" (tela) e "Superfícies" (frontend) de Success Criteria.
- [ ] Testes cobrem cobrado, bloqueado, protegido e cobrado em quarentena (sem "Pendente", saldo visível, Recarregar bloqueado).
- [ ] Builder marcado em `pending` ou `suspended` com `quarantine` renderiza exatamente como hoje (teste).

**Validation:**
```bash
cd frontend/artifacts/neurostack \
 && pnpm exec vitest run src/pages/billing-view.test.ts src/pages/billing-contract.test.ts src/pages/billing.test.tsx src/pages/master-admin.test.tsx src/components/admin/courtesy-credits-dialog.test.tsx \
 && CI=true pnpm run typecheck \
 && PORT=5173 BASE_PATH=/ pnpm run build
```
Escopo: frontend tocado; vitest, typecheck e build, como no wish anterior (no worktree com `node_modules` próprio; nunca rodar pnpm numa cópia que compartilha `node_modules`).

**depends-on:** Group 5a

---

### Group 6: Lista pré-deploy somente leitura

**Goal:** um comando de operador mostra, antes do deploy, quem passa a ser cobrado, bloqueado ou protegido.

**Deliverables:**
1. `src/ops/credit_courtesy_rule.py` (`status`): `REPEATABLE READ READ ONLY`, rollback, saída JSON por builder com cortesia: estado no deploy, saldo de cortesia, dívida aberta, reserva estimada da primeira janela depois do deploy, clínicas por canal (Uazapi, Evolution, Z-API, Meta), tráfego de `/v1/events` sem `Idempotency-Key` (Decisão 9), assinatura ativa, atraso do binding e `coverage.usable`; usa a leitura da regra do G1; não nomeia `credit_enforcement_marks`.
2. Testes: `tests/ops/test_credit_courtesy_rule.py` (incluindo a prova de que não escreve) e `tests/integration/test_credit_courtesy_rule_ops_postgres.py`.

**Acceptance Criteria:**
- [ ] O critério "Lista pré-deploy" de Success Criteria.
- [ ] O comando não escreve nada (teste com captura de SQL) e nunca imprime credenciais; a cerca AST do `credit_charge_mark` continua verde.

**Validation:**
```bash
: "${POSTGRES_SMOKE_DATABASE_URL:?defina o PostgreSQL de teste}" \
 && python -m pytest tests/ops/test_credit_courtesy_rule.py tests/ops/test_credit_charge_mark.py -q -p no:cacheprovider \
 && bash scripts/check-credit-integration.sh tests/integration/test_credit_courtesy_rule_ops_postgres.py \
 && python -m black --check src tests && python -m isort --check-only src tests && python -m flake8 src tests && python -m mypy src --ignore-missing-imports
```
Escopo: um comando novo somente leitura; arquivos disjuntos dos grupos da Wave 2 e 3.

**depends-on:** Group 1

---

### Group 7: Runbook e docs

**Goal:** o operador sabe o que a regra faz, como checar antes do deploy, como fazer rollback e o que muda para builder e paciente.

**Deliverables:**
1. `docs/runbooks/credit-courtesy-rule.md` (novo): a regra, os estados, a lista pré-deploy (tarefa pontual em `neurostack-prod` na task definition nova, entre o apply e o `update-service`), o atraso do binding (`binding_lag`) e o que fazer, deploy em L2a/L1/L2b, rollback (PR de infra de volta para `prod-fe800aec`, apply e `update-service`, com a latência esperada e o efeito sobre a fila de Uazapi/Evolution), os riscos aceitos pelo owner (mensagens forjadas no Uazapi e na Evolution; armazenamento grande).
2. Atualizar `docs/billing/credits.md`, `docs/runbooks/credit-courtesy.md`, `docs/runbooks/credit-charge-mark.md`, `docs/runbooks/credit-enforcement-arming.md` (nota de `message_without_observation` para a regra; guarda do L10 fechada), `docs/runbooks/uazapi-webhook-auth.md` (a autenticação como mitigação do Risco 1) e o wish `.genie/wishes/uazapi-webhook-auth/WISH.md` (o adaptador vem deste wish; a guarda do L10 continua fechada).

**Acceptance Criteria:**
- [ ] O critério "Docs" de Success Criteria; cada afirmação de comportamento aponta para o código ou teste que a sustenta.

**Validation:**
```bash
python -m pytest tests/docs tests/scripts/test_production_migration_window.py tests/scripts/test_asaas_production_wiring.py tests/scripts/test_asaas_migration_window.py -q -p no:cacheprovider \
 && grep -q "credit_courtesy_rule" docs/runbooks/credit-courtesy-rule.md \
 && grep -q "prod-fe800aec" docs/runbooks/credit-courtesy-rule.md \
 && grep -q "binding_lag" docs/runbooks/credit-courtesy-rule.md \
 && grep -q "message_without_observation" docs/runbooks/credit-enforcement-arming.md
```
Escopo: docs; os testes de docs e de `tests/scripts` que contam texto dos runbooks.

**depends-on:** Group 4, Group 5b, Group 6

---

## QA Criteria

_Passos ao vivo depois do merge humano do PR único; cada um exige o "vai" do owner no momento do passo (Decisão 13)._

- [ ] **L2a (sem trocar o serviço):** build "Staging image build" na `main` com `expected_head=047` (repetir uma vez em falha intermitente), cópia por digest para o ECR `neurostack-prod`, PR de infra com o `image_tag`, merge humano, `terraform-apply-prod` na camada `app`; a task definition nova existe e o serviço continua na anterior.
- [ ] **L1 (somente leitura):** `python -m src.ops.credit_courtesy_rule status` numa tarefa pontual da task definition nova (rede copiada do serviço, `startedBy` rotulado); o owner vê quem fica cobrado, bloqueado e protegido e dá o go ou o no-go. No-go = nenhum `update-service` e um PR de infra que volta o `image_tag`.
- [ ] **L2b:** `update-service`; alvos saudáveis, `/health`, switch de crédito `effective`, rotas de crédito 401 sem token, sem erro novo no log.
- [ ] **L3 (QA ao vivo):** pré-condições registradas antes: `uazapi_egress_enabled` aberto (versão da AppConfig anotada), estado da autenticação do webhook anotado, credenciais de LLM e `agent_presentation` da clínica preenchidos. Com o builder de teste do owner e a clínica de teste dele no Uazapi (cortesia de 200 créditos; os identificadores ficam fora do repositório, que é público): uma mensagem real pelo WhatsApp debita 1 crédito da cortesia e o `/billing` mostra o saldo caindo; com a cortesia retirada ("Tirar tudo") o agente fica mudo e o `/billing` mostra "Serviço suspenso"; com cortesia nova volta a responder. Nenhum outro builder muda além do previsto no L1. (A reentrega sem débito duplo é provada no gate PostgreSQL; uma reentrega ao vivo exige um POST real ao webhook de produção e um "vai" próprio.)

---

## Assumptions / Risks

Os riscos 1–13 do DESIGN valem, com duas correções: o **Risco 6** (assinatura cancelada não tem tolerância, Decisão 3) e o **Risco 11**/D22 (a regra também lê assinatura, binding e ciclo, só com saldo de cortesia zero, Decisão 8). Riscos do plano:

| Risk | Severity | Mitigation |
|------|----------|------------|
| Grupos 1–5a tocam os mesmos módulos e a consolidação pode esconder uma falha entre grupos (lição do `creditos-cortesia`). | High | Árvores de partida e posse da Decisão 2; gate PostgreSQL agregado na árvore consolidada; `final-gate` independente. |
| A separação exibição/recarga na tela abre a recarga em quarentena por engano. | High | Decisão 4 e o teste do G5b "cobrado em quarentena: Recarregar bloqueado". |
| O frontend precisa de `node_modules` no worktree do G5b. | Low | Receita offline do pnpm já usada; nunca rodar pnpm numa cópia que compartilha `node_modules`. |
| O deploy liga a regra para todos os builders com cortesia de uma vez. | High | L1 entre o apply e o `update-service`, com o go/no-go do owner (Decisão 13). |
| Atraso interno do consumidor de fatos deixa um builder protegido por muito tempo sem binding. | Low | Decisão 5 (owner); o runbook e a lista mostram o atraso (`binding_lag`). |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan review, rodada 1 (2026-11-12T03:31:35Z): FIX-FIRST

`genie-reviewer/plan/claude-opus-5-5`, WISH sha256 `ca2ef1548f982f412ab32ae391649b136f170340fe4ff340e04ca25e62b7bae4`, DESIGN verify exit 0. 0 CRITICAL, 1 HIGH (H1: a tela mudaria a regra de recarga em quarentena), 7 MEDIUM (Decisão 5 ao owner; armadilha de reaproveitar `binding_coverage`; ordem L1/L2 impossível; árvores de partida; G5 subpontuado e testes faltando; validações sem encadeamento; itens órfãos), 10 LOW. Decisão 5 levada ao owner (rodada 6: atraso interno protegido). Todos os itens aplicados nesta versão; enviada para a rodada 2.

### Plan review, rodada 2 (2026-11-12T11:53:20Z): SHIP

`genie-reviewer/plan/claude-opus-5-5`, WISH sha256 `47237d71a916e9e9ef4eb97162071a3c1c88fc7a1e049d9aa768927474aa0823`, DESIGN verify exit 0. 0 CRITICAL, 0 HIGH, 1 MEDIUM, 6 LOW; nada volta ao owner. DAG sem ciclo; ondas disjuntas (Wave 2 G2×G6, Wave 3 G3×G5a, Wave 4 G4×G5b); o portão compartilhado é do G2. Depois do SHIP, por pedido do revisor (antes de despachar o G5b), aplicadas só as correções de redação: MEDIUM-1 (só `active` + `quarantine` muda na tela; `suspended` + `quarantine` igual ao marcado; teste novo no G5b), LOW-1 (um grupo por onda acrescenta aos arquivos de teste compartilhados), LOW-2 (`test_credit_object_copy_gc` nas exclusões), LOW-3 (`git status --porcelain` na checagem de escopo), LOW-4 (só ciclo corrente pago protege), LOW-5 (guarda só nos blocos com PostgreSQL), LOW-6 (qual saldo o cobrado vê). Status DRAFT → APPROVED pelo orquestrador.

---

### Execução: início, 2026-11-12

- `/genie:work debito-cortesia-automatico` pelo owner. Status `APPROVED` → `IN_PROGRESS`. Wave 1 (Group 1) despachada a partir de `origin/main` no commit lido abaixo.

### Group 1: entrega do engenheiro, 2026-11-12

- Worktree `/home/lorenzo/work/cortesia-g1` (branch `feat/courtesy-rule-core`, base `fe800aec`), sem commit. `git diff | sha256sum` = `7270f5fcd5a72af772942278bc04d06a1cac8e3d7a8fc7cc4850d5f0ed33e2a7` (14 arquivos, +1786/−91); novo `tests/integration/test_credit_courtesy_rule_postgres.py` sha256 `69020e6089568aee1ddf9f0cf1be14f84c728229b5600a39a2a2942abde9f4de`. Conferidos pelo orquestrador; nenhuma migration, nenhum arquivo de invariante ASAAS, nada em `src/api`, `src/ops`, `src/models`, `credit_metering.py`, frontend, docs.
- Relato do engenheiro (não verificado ainda): bloco de validação do G1 exit 0 (unitários 398; PG courtesy_rule 22, ledger 56, courtesy 13, effective_enforcement 23, schema_dominance 37, maintenance 11, binding_grace 26, retention 18, todos 0 pulados); `test_credit_charge_mark.py` 221; cerca do L10 109/109; 12 de 13 mutantes pegos (sobrevivente: só a guarda de modo do livro em `evaluate`, coberta pela releitura).
- Fica para o G2 (dito pelo engenheiro): `credit_metering` ainda não passa a captura a `CreditLedger.debit`, `request_gate` e às reservas; livro e ciclo de vida releem o modo sob o lock. Pré-checagens ainda aplicam quarentena/cobertura a builders da regra; `tenant_suspended`/`reservation_verdict` sem mudança de código.
- Decisões onde o design era silente, a avaliar pelo revisor: tolerância só com binding `active` (último `cycle_end`); só assinatura `asaas` ativa; "ciclo corrente" pela data civil de São Paulo; regra decide também com o portão 047 fechado (`rule_unread` novo); débito de trace sob a regra sempre grava o efeito mesmo pagando 0; armazenamento pagando 0 sem efeito; episódio antigo da era da marca fica aberto intocado sob a regra; base da cortesia com capacidade zero não avisa.
- **Validação do orquestrador** (2026-11-12 13:04:57Z → 13:11:35Z, PostgreSQL 16 próprio em 127.0.0.1:55452, `src` resolvido no worktree, diff `7270f5fc…`/arquivo novo `69020e60…` iguais no início e no fim): o bloco de validação do G1 inteiro, exit 0. Unitários 398 passed. Gate PostgreSQL, cada um "evidence valid", 0 pulados: courtesy_rule 22/22, ledger 56/56, courtesy 13/13, effective_enforcement 23/23, schema_dominance 37/37, maintenance 11/11, binding_grace 26/26, retention 18/18. black, isort, flake8 limpos; mypy sem problemas em 217 arquivos. `make quality` não rodado (ordem do owner). Escopo: o gate documentado do repositório para o núcleo de crédito com estado.

### Group 1: revisão de execução, rodada 1 (2026-11-12T13:25:12Z): FIX-FIRST

- Revisor independente (`genie:reviewer`, ≠ engenheiro), PostgreSQL próprio em 55451, fingerprints iguais no início e no fim. Bloco de validação do G1 verde (os mesmos números da validação do orquestrador) e, além dele, verdes com 0 pulados: courtesy_http 9, orphan_reconciliation 60, charge_mark 33, charge_workers 14, charge_badge 2, metering 55, rollout_fence 109. Log do servidor só com erros intencionais. Critérios Regra, Plano pago em vigor, Desbloqueio automático (com Risco 7), Débito só de cortesia, Janelas, Sem episódio, Dominância, "nenhum lote pago tocado / nenhuma dívida", `courtesy_removal`: PASS, cada um com teste PostgreSQL. A leitura do plano pago segue a Decisão 5 sem reaproveitar `binding_coverage`; a regra é uma leitura indexada e a do plano pago só roda com saldo de cortesia zero.
- **HIGH-1:** um trace perdoado sob a regra (débito < 1000 mc, sem dívida) é contado como `trace_partial_debit` por `CreditRolloutService._orphan_report` (`src/services/credit_rollout.py:1271-1302`), para sempre. Liga `capability_ready().has_alert` (recusa a capability de recarga em `billing_capability.py:312` / `_capability_is_valid`), o gauge `neurostack_credit_orphan_effects` e a recusa do `arm()`. Provado por sonda (0 → 1). Aconteceria no próprio L3. **Decisão do orquestrador:** a correção entra no G1 (o regressor é do G1; o G2 parte da árvore revisada do G1, sem conflito); o resto de `credit_rollout.py` continua com o G2 (gatilhos) e o G5a (comentários).
- **MEDIUM-1:** "sem `_settle`" sem prova PostgreSQL que possa falhar (todas as cobranças da regra no arquivo novo rodam sem dívida aberta).
- **LOW-1:** a explicação do mutante sobrevivente está errada: a guarda não é redundante, o mutante abre um episódio no PostgreSQL (corrida captura → marca → débito); só um teste unitário pega.
- **LOW-2:** janela/reserva/egress sem lote pago concorrente nas provas PostgreSQL.
- **LOW-3:** captura de marca + `unmark` antes do débito, builder com cortesia: `spend_hold` liga `enforced` pela releitura da cortesia (quarentena e janela cobrada de todos os lotes).
- **LOW-4:** sem captura, `spend_hold` julga a regra no relógio do Python e não no `at` do livro.
- **LOW-5 (notas para grupos seguintes, sem mudança no G1):** livro de gasto sob a regra expira lotes pagos sem o catch-up de início (dívida antiga da marca pode não ser paga; segue o design, não ocorre no prod hoje); o G5a não pode mostrar em `/v1/credits/state` os prazos de um episódio velho da marca para builder da regra; o G2 aplica o D1 em `rule_unread` para captura `courtesy`, não em `mark_unread`.
- Decisões do engenheiro onde o design era silente: todas aceitas pelo revisor (a do mutante sobrevivente rejeitada, LOW-1). Laço de correção 1 de 2 despachado a um `genie:fixer` (≠ engenheiro, ≠ revisor) com HIGH-1, MEDIUM-1 e LOW-1..4.

### Group 1: laço de correção 1 (fixer), 2026-11-12

- Fingerprints conferidos pelo orquestrador: `git diff | sha256sum` = `5a963397295906c5016eb246f1e123485a886d835b48c9a6ffe11ab92c2b9011` (15 arquivos, +1821/−91; `src/services/credit_rollout.py` entra no diff); arquivo novo `fa5ee299636e3e1b3d26e2a50af10753758c0535b9019e5eb1a94870fc326289`. Nenhum cluster ou processo deixado.
- Relato do fixer (a confirmar na re-revisão): HIGH-1 em `_orphan_report` (uma instrução só; trace não é parcial com dívida 0, débito < 1000, nenhum débito em lote não-cortesia **e o builder já teve lote de cortesia**, condição a mais para manter verdes dois testes existentes de efeito sem lançamentos); MEDIUM-1, LOW-1, LOW-2, LOW-3 (`spend_hold` não dá semântica de cortesia a captura de marca/global), LOW-4 (`spend_hold(at=)` vindo do `open_book`), cada um com teste e mutante que falha. Um controle existente em `test_the_captured_rule_debits_only_courtesy_fefo_under_quarantine` mudou (agora com marca real numa sessão revertida). Validação do fixer: unitários 423; PG courtesy_rule 26, ledger 56, courtesy 13, effective_enforcement 23, schema_dominance 37, maintenance 11, binding_grace 26, retention 18, orphan_reconciliation 60, capability_ready 26, enforcement_ops 52, rollout_fence 109; lint/mypy limpos; cerca AST 221.
- **Para o G2** (fora dos arquivos do G1, achado do fixer): `credit_metering.py:1880-1887` `DecisionCharge` não carrega `forgiven_mc`; `effect_without_decision` (`credit_rollout.py` ~1094) casa traces só com claims `mode == 'enforced'`, então o G2 tem de gravar os pedidos da regra com `mode = 'enforced'` ou cada trace da regra vira órfão.

### Group 1: re-revisão (laço 1) (2026-11-12T13:51:53Z → 14:04:48Z): SHIP

- Mesmo revisor, PostgreSQL próprio novo em 55451, fingerprints `5a963397…`/`fa5ee299…` iguais no início e no fim. Unitários 423; 14 arquivos PostgreSQL "evidence valid", 0 pulados, 545 testes (courtesy_rule 26, ledger 56, orphan_reconciliation 60, capability_ready 26, enforcement_ops 52, effective_enforcement 23, schema_dominance 37, courtesy 13, maintenance 11, binding_grace 26, retention 18, rollout_fence 109, charge_mark 33, metering 55); lint/mypy limpos; log do servidor só com erros intencionais.
- HIGH-1, MEDIUM-1, LOW-1..4 corrigidos, cada um com teste que falha. A condição a mais do fixer ("o builder já teve cortesia") aceita: mantém os testes existentes de efeito sem lançamentos e foi pinada por mutante; o ponto cego que sobra (trace sem dívida, só em cortesia, de builder que já teve cortesia) só nasce de bug do livro, e marcá-lo exigiria migration (fora do escopo). O controle alterado no teste FEFO ficou mais forte (marca real numa sessão revertida). Os dois itens do fixer para o G2 confirmados, mais um: o G2 não pode pular `_charge_decision` para traces perdoados (`decision_without_effect` só fica quieto porque o trace pago 0 grava o efeito).
- **LOW-A (não bloqueia, vai para o G2):** `debt = 0` e `debit < TRACE_COST_MC` da exclusão sem teste (mutantes M1/M2 sobrevivem); acrescentar a `test_forgiven_rule_traces_are_never_partial_debits` dois controles de builder com cortesia, cada um contando 1 e ligando `has_alert`: 400 mc no lote de cortesia + dívida de 300 mc; 1500 mc de débito em lotes de cortesia sem dívida.
- **LOW-B:** as notas para o G2/G5a já registradas acima.

### Group 1: validação do orquestrador após o laço 1 e conclusão

- 2026-11-12 13:51:31Z → 14:05:38Z, PostgreSQL próprio em 55452, diff `5a963397…`/arquivo novo `fa5ee299…` iguais no início e no fim, `src` no worktree: unitários (os 6 do G1 + `test_credit_rollout.py`) 423 passed; PostgreSQL "evidence valid", 0 pulados: courtesy_rule 26, ledger 56, courtesy 13, effective_enforcement 23, schema_dominance 37, maintenance 11, binding_grace 26, retention 18, orphan_reconciliation 60, capability_ready 26, enforcement_ops 52, rollout_fence 109; black/isort/flake8 limpos, mypy sem problemas em 217 arquivos; cerca AST `test_credit_charge_mark.py` 221 passed. `make quality` não rodado (ordem do owner). Escopo ampliado em relação ao bloco do G1 porque o laço tocou `credit_rollout.py` (relatório de órfãos e capability).
- `genie task done t_mwczaxxh6bb71151`. Árvore revisada salva como `cortesia-g1-reviewed.patch` (sha256 `3ecfbaa242688c5ca40b35b8e8c830f7d3b2d973a2c985a660405e35ac0452bf`, no scratchpad da sessão).

### Wave 2: despacho, 2026-11-12

- Worktrees a partir de `fe800aec` + a árvore revisada do G1 (Decisão 2), conferidos (diff `5a963397…`, arquivo novo `fa5ee299…`): `/home/lorenzo/work/cortesia-g2` (branch `feat/courtesy-rule-g2`) e `/home/lorenzo/work/cortesia-g6` (branch `feat/courtesy-rule-g6`). Na Wave 2 só o G2 acrescenta a `credit_rollout_world.py` e `test_credit_schema_dominance_postgres.py`; o G6 põe as fixtures nos próprios arquivos.

### Group 6: entrega do engenheiro, 2026-11-12

- Worktree `/home/lorenzo/work/cortesia-g6`, sem commit. Diff rastreado igual ao do G1 (`5a963397…`, nenhum arquivo existente mudou); três arquivos novos, conferidos pelo orquestrador: `src/ops/credit_courtesy_rule.py` `40786246475b448b10554a9c3527fc6c6eb724a45fc6c19ccfd42a93cc1970a1`, `tests/ops/test_credit_courtesy_rule.py` `9bd2a54d56d6b1901be39bf613f69140ba7371495701a1c400f775b099dbb6a0`, `tests/integration/test_credit_courtesy_rule_ops_postgres.py` `84cb28503acd8aee185727523c9f7fce1210763ab875f98e4b0a385897d04304`. O módulo não nomeia `credit_enforcement_marks`.
- Relato do engenheiro (a confirmar): `status` em uma transação `REPEATABLE READ READ ONLY` sempre revertida, recusa abaixo de 046, estado vindo de `effective_enforcement` no relógio do banco; saída JSON com estado, cortesia, plano pago, dívida aberta, estimativa de reserva (com campo `approximation`), clínicas por canal (só booleanos vêm do banco), `/v1/events` sem `Idempotency-Key` (efeitos observacionais `trace`, `production`, autenticados, 7 dias, sem claim de ingresso do mesmo tenant e `interaction_id = trace_id`), assinatura, atraso do binding e `coverage`. Validação do engenheiro: 261 unitários; PG 6/6 "evidence valid", 0 pulados; lint/mypy limpos; 5 mutantes pegos. Aproximação admitida: o efeito não grava o ingresso, então uma decisão de webhook sem id de mensagem do provedor também contaria (o revisor confere se o tráfego observacional de webhook grava claim).
- **Validação do orquestrador** (2026-11-12 14:42:27Z → 14:47:03Z, PostgreSQL próprio em 55456, `src` no worktree, fingerprints iguais no início e no fim): o bloco de validação do G6, exit 0. Unitários (`test_credit_courtesy_rule.py` + `test_credit_charge_mark.py`) 261 passed; `test_credit_courtesy_rule_ops_postgres` "evidence valid" 6/6, 0 pulados; black/isort/flake8 limpos; mypy sem problemas em 218 arquivos; 0 linhas ERROR no log do servidor. `make quality` não rodado (ordem do owner).

### Group 6: revisão de execução (2026-11-12, fim 14:47:45Z+): SHIP

- Revisor independente, PostgreSQL próprio em 55457, fingerprints iguais no início e no fim, worktree intocado. Bloco de validação do G6 exit 0 (261 unitários; PG 6/6 "evidence valid", 0 pulados; lint/mypy limpos). Critérios "Lista pré-deploy", "não escreve" (tabelas e sequências idênticas no PostgreSQL; transação vista por dentro com `repeatable read` e `read_only = on`), "não imprime credenciais" e cerca AST: PASS.
- **A preocupação HIGH foi refutada:** em `prod-fe800aec` os turnos observacionais de webhook gravam claim de ingresso (Z-API/Meta/Uazapi verificado com id do provedor; `/v1/events` com chave), e o `trace_id` do efeito é o `interaction_id` do claim (`credit_metering.py:634-653`, `:799-831`; `repositories/credit.py:840-890`). Sonda com o gravador real: só as chamadas sem chave (e uma decisão Z-API sem `messageId`, a aproximação admitida) contam. Estimativa de reserva confere com `WindowWork._open`/`charge` do G1; canais conferem com os resolvedores do webhook.
- **MEDIUM-1:** três filtros do atraso do binding sem teste (mutantes L1/L2/L3 sobrevivem). **MEDIUM-2:** o contrato da contagem de `/v1/events` só é semeado com SQL cru; o G2 mexe em `credit_metering.py` em paralelo, então falta um teste com o gravador real (`record_observational_trace`). LOW-1 total "sob a regra" no resumo; LOW-2 última validade da cortesia e validade do lote pago; LOW-3 `payable_by_courtesy_mc` de protegido sem asserção; LOW-4 efeito de `sweep_expired` sem teste; LOW-5 `PRODUCTION_ORIGIN` sem pino; LOW-6 a contagem é um piso (pedidos com erro ou gravação descartada não contam); LOW-7 tamanho da saída (≈ 5–8 KB no prod de hoje; nota de runbook).
- **Decisão do orquestrador:** o G6 não bloqueia nenhum grupo e o G2 ainda roda, então um laço curto do `genie:fixer` corrige MEDIUM-1, MEDIUM-2 e LOW-1..6 agora; LOW-6 (parte de runbook) e LOW-7 vão para o G7.

### Group 6: laço de reforço (fixer), 2026-11-12

- Fingerprints conferidos pelo orquestrador: diff rastreado ainda `5a963397…`, arquivo do G1 `fa5ee299…` intacto; novos: `src/ops/credit_courtesy_rule.py` `e0122b83e873e512beeb760b9b8da232dfd9308daaf51be0b1a6cbb4522d0bfe`, `tests/ops/test_credit_courtesy_rule.py` `15293b96b4892583dad86423d218c75ea3b52cfcae8a32f510c88c0d928e277b`, `tests/integration/test_credit_courtesy_rule_ops_postgres.py` `95ed14a33c3167710646e22dccf26dac58c5a29a196e17492bc1396dce801552`. Sem cluster nem cópia deixados.
- Relato do fixer (a confirmar na re-checagem): MEDIUM-1 (`test_no_binding_lag_without_an_unbound_paid_cycle_of_an_active_plan`), MEDIUM-2 (`test_the_count_matches_the_real_observational_writer`, só com o gravador real), LOW-1 (`summary` agora com `under_rule` e `all_states`; as chaves antigas do topo foram para `all_states`), LOW-2 (`courtesy.last_expiry_at`, `paid_plan.paid_lot_expires_at`), LOW-3, LOW-4 (`test_the_open_debt_is_the_swept_one`), LOW-5 (pino em `InteractionOrigin.PRODUCTION.value`), LOW-6 (`events_window.approximation`). Validação do fixer: 265 unitários; PG 9/9, 0 pulados; lint/mypy limpos; 7 de 7 mutantes pegos.
- **Validação do orquestrador após o reforço** (2026-11-12 16:34:48Z → 16:41:51Z, PostgreSQL próprio em 55456, fingerprints `5a963397…`/`e0122b83…`/`15293b96…`/`95ed14a3…` iguais no início e no fim, `src` no worktree): bloco do G6 exit 0. Unitários 265 passed; `test_credit_courtesy_rule_ops_postgres` "evidence valid" 9/9, 0 pulados; black/isort/flake8 limpos; mypy sem problemas em 218 arquivos; 0 linhas ERROR no log do servidor.

### Group 6: re-checagem (16:34:49Z → 16:41:52Z): SHIP, e conclusão

- Mesmo revisor, PostgreSQL novo em 55457, cinco fingerprints iguais no início e no fim, worktree intocado. Bloco do G6 exit 0 (265 unitários; PG 9/9, 0 pulados; lint/mypy limpos). MEDIUM-1, MEDIUM-2 e LOW-1..6 confirmados; mutantes R1, L1, L2, L3, D1 (os sobreviventes da primeira revisão), N1, N3, N4, N5 e W1/W2 (o gravador de `credit_metering.py` deixando de gravar claim ou ignorando o claim existente) mortos; nada fora dos três arquivos do G6 cita as chaves antigas do `summary`. Sonda: uma transação `repeatable read` `read_only = on` revertida, só SELECTs; e-mail, telefone, nome do tenant, URL e chave da Evolution, instância, id do número da Meta e id externo da assinatura ASAAS semeados e ausentes da saída.
- **LOW-A (novo, não bloqueia; fica para a consolidação):** mutante N2 sobrevive (`_paid_lot_expiry` sem o filtro `expires_at > now` mostraria uma data passada num campo só de exibição). Correção: no `test_the_list_shows_who_is_charged_blocked_or_protected`, dar ao builder `blocked` uma recarga já vencida e afirmar `paid_plan.paid_lot_expires_at is None` com o estado ainda `blocked`.
- `genie task done t_mwczayhm68beebe6`. Os três arquivos revisados do G6 arquivados no scratchpad (`cortesia-g6-reviewed-newfiles.tar`); a fonte durável é o worktree `/home/lorenzo/work/cortesia-g6`.

### Group 2: entrega do engenheiro, 2026-11-12

- Worktree `/home/lorenzo/work/cortesia-g2`, sem commit. Conferido pelo orquestrador: `git diff | sha256sum` = `3931e58b74700efafef383100a1e4f960e5979039f1b06ba9f1938e3861af8a6` (25 arquivos, +4341/−193); `tests/integration/test_credit_courtesy_rule_postgres.py` `5ef893f562e9f7d78202e870246df8c9e0f35f70306527b9249a320c3e5a4d6b`. Nada em `webhook.py`, migrations, `src/models`, frontend, docs ou nos arquivos de invariante do ASAAS.
- Relato do engenheiro (a confirmar): pré-checagens com a regra (bloqueado → `credit_suspended`; cobrado sem quarentena, cobertura e portão de episódio); `(origin, at, state)` sob o lock; final sem releitura; reparent e D1 (`rule_unread`) para `courtesy`; workers ligam com lote `courtesy`; origem real no `decision_digest`; portão compartilhado (`courtesy_block`, `tenant_suspended`, `reservation_verdict` com `captured`, `CategoryVerdict` ganha `"suspended"`); LOW-A feito. Fora da lista de arquivos, por necessidade: `test_credit_charge_workers_postgres.py` (variante `courtesy=False`), dois testes de armazenamento do G1 em `test_credit_inventory.py` e dois no arquivo da regra passam a stubar a re-checagem. Validação do engenheiro: 804 unitários; PG effective_enforcement 40, metering 57, orphan_reconciliation 60, rollout_fence 109, charge_workers 14, schema_dominance 38, courtesy_rule 26, ledger 56, charge_mark 33, capability_ready 26, 0 pulados; lint/mypy limpos; 14 de 14 mutantes pegos.
- **Lacuna apontada pelo engenheiro, não corrigida:** na Z-API o STT e a mensagem de fallback dele rodam antes da pré-checagem, então o paciente de um builder bloqueado ainda pode receber o texto de fallback (e o STT custa), contra o DESIGN item 5 ("sem resposta") e a decisão 11 do owner. A correção é mover a pré-checagem em `webhook.py`, fora do escopo do G2 como despachado.
- **Validação do orquestrador** (2026-11-12 16:34:44Z → 16:59:22Z, PostgreSQL próprio em 55459, diff `3931e58b…`/arquivo da regra `5ef893f5…` iguais no início e no fim, `src` no worktree): bloco do G2 (com `test_credit_ledger`, `test_credit_lifecycle`, `test_credit_inventory`, `test_credit_charge_mark` a mais nos unitários e courtesy_rule, ledger, charge_mark, capability_ready a mais no PG), exit 0. Unitários 804 passed. PG "evidence valid", 0 pulados: effective_enforcement 40, metering 57, orphan_reconciliation 60, rollout_fence 109, charge_workers 14, schema_dominance 38, courtesy_rule 26, ledger 56, charge_mark 33, capability_ready 26. black/isort/flake8 limpos; mypy sem problemas em 217 arquivos. Log do servidor: 40 tipos de ERROR, todos provas negativas intencionais (append-only, CHECK, unique, FK, cancelamento), nenhum de sintaxe/tipo/objeto inexistente. `make quality` não rodado.
- **Revisão de execução, rodada 1: FIX-FIRST.** Revisor independente (≠ engenheiro), PostgreSQL próprio em 55460 (mutantes em 55461), fingerprints iguais no início e no fim, delta isolado contra a árvore do G1 (fontes do G1 byte a byte iguais). Unitários 804; PG "evidence valid", 0 pulados: effective_enforcement 40, metering 57, orphan_reconciliation 60, rollout_fence 109, charge_workers 14, schema_dominance 38, courtesy_rule 26, ledger 56, charge_mark 33, capability_ready 26, reply_media 25, webhook_media_delivery 6, courtesy_http 9, charge_badge 2; `tests/api` de crédito/webhook/events 1409 passed e 1 falha intermitente já existente (`test_admin_credit_courtesy.py::test_the_builder_reads_its_courtesy_never_the_reason`, compara `as_of` de duas requisições; passa 3/3 isolado; rotas e teste não tocados pelo G2); lint/mypy limpos. Mutantes do revisor: 6 mortos, 4 sobreviventes (MA, MB, MD1, ME).
  - Critérios: Workers, Origem real, quarentena/cobertura, dominância e carry-overs PASS; Corrida PARCIAL (a re-checagem em `_enforced_book` sem teste); bloqueado: `/v1/events` 402, Meta e texto Z-API PASS, **áudio Z-API FAIL**.
  - **HIGH-1:** na Z-API, antes de `precheck_zapi` (`webhook.py:3574`), o ramo de áudio baixa (`:3451`) e transcreve (`:3478`) com a chave da clínica e, na falha, `_send_zapi_stt_fallback` (`:3486`) manda ao paciente "Recebi seu áudio! …", chama `mark_conversation_unread` e abre janela Redis de 6 h; `mark_message_read` (`:3537-3546`) vai para toda mensagem. Reproduzido: builder bloqueado pela regra, áudio Z-API → `stt_fallback` enviado. Meta e `/v1/events` sem vazamento antes da pré-checagem. Correção proposta: separar `precheck_webhook` em captura (`capture_webhook`) e decisão (`decide_webhook`), capturar na Z-API logo depois da guarda `if not phone` (`:3319`), e sob `courtesy` decidir na hora (bloqueado: sem download, STT, fallback; mantém `_check_bot_active` e a captura só de texto da sessão pausada; devolve `CREDIT_SUSPENDED_SKIP`); marca e global com respostas e escritas iguais às de `prod-fe800aec` (a leitura de modo só muda de posição na requisição, o conjunto de instruções é o mesmo).
  - **MEDIUM-1:** a re-checagem por `(origin, at, state)` em `_enforced_book` sem teste (mutante MA sobrevive a 396 testes). **LOW-1:** re-checagem do handoff de arquivo sem teste (MB). **LOW-2:** log `courtesy_forgiven` sem teste (MD1). **LOW-3:** leitura `courtesy` limpando o conjunto D1 da marca sem teste (ME). **LOW-4 (decisão do owner):** recibo de leitura da Z-API enviado para builder bloqueado (marca o chat como lido no telefone da clínica com o agente mudo); o revisor recomenda suprimir. **LOW-5 (para G3/G5a):** `media_assets.py:653-661` e `knowledge.py:409-418` comparam `== "category_exhausted"`; ao passar `captured`, têm de mapear `"suspended"` para 402 `credit_suspended`.
  - **Decisão 16 do owner (AskUserQuestion, 2026-11-12):** builder **bloqueado pela regra** na Z-API **não** recebe o recibo de leitura (`mark_message_read`): o paciente não vê "lida" e a mensagem continua não lida no celular da clínica. Marca e global: sem mudança.
  - **Decisão do orquestrador:** a correção do HIGH-1 entra no laço de correção do G2 (Z-API em `webhook.py`, sem tocar no código Uazapi/Evolution; o G3 parte da árvore revisada do G2); a mudança de posição da única leitura de modo sob marca/global é aceita (mesmas instruções, escritas e respostas; o critério de dominância não fixa a ordem dentro da requisição). LOW-5 vai para os briefs do G3 e do G5a. Laço de correção 1 de 2 despachado a um `genie:fixer` com HIGH-1, MEDIUM-1, LOW-1..4.
  - **Laço de correção 1 (fixer):** fingerprints conferidos pelo orquestrador: `git diff | sha256sum` = `ff9bc327feb6588d182e560741492409ca09614f1f6fe6fabb24b24a383a797d` (26 arquivos, +4860/−215); arquivo da regra `5c5d7f382830e928c15f6fc87e03a0ffb2b54c819de0b60793014f9da80ac2bf`; fontes do G1 byte a byte iguais às do worktree do G1. Relato do fixer (a confirmar na re-revisão): `precheck_webhook` = `capture_webhook` + `decide_webhook`; na Z-API a captura é preguiçosa (antes do bloco de áudio para áudio de paciente, senão logo antes do recibo de leitura); bloqueado pela regra → sem download, STT, fallback, `mark_conversation_unread` nem recibo; pausa/agente desligado continuam com captura só de texto (respondem 202 `session_paused`/`bot_paused`, como na Meta); D1 da regra → 503 na hora, D1 de marca/global segurado até a posição antiga. Diferenças residuais declaradas sob marca/global (a julgar na re-revisão): uma leitura de modo a mais nos caminhos de fallback de STT, STT sem suporte, falha de download, `bot_paused` e `session_paused`; e, quando a leitura falha sem latch, o rollback agora vem antes da escrita preguiçosa de despausa, que passa a ficar. Num mutante, um teste chegou a chamar `api.groq.com` com chave fictícia (401); o espião foi corrigido. Validação do fixer: 812 unitários; PG 12 arquivos, 0 pulados (courtesy_rule 27); suítes vizinhas da Z-API 1137 em 27 arquivos; lint/mypy limpos; 7 de 7 mutantes pegos.
  - **Validação do orquestrador após o laço 1** (2026-11-12 18:13:15Z → 19:01:20Z, PostgreSQL próprio em 55459, diff `ff9bc327…`/arquivo da regra `5c5d7f38…` iguais no início e no fim, `src` no worktree): unitários do G2 812 passed; PG "evidence valid", 0 pulados: effective_enforcement 40, metering 57, orphan_reconciliation 60, rollout_fence 109, charge_workers 14, schema_dominance 38, courtesy_rule 27, ledger 56, charge_mark 33, capability_ready 26, reply_media 25, webhook_media_delivery 6; black/isort/flake8 limpos; mypy sem problemas em 217 arquivos; log do servidor só com provas negativas intencionais. Suítes de webhook (`test_webhook_zapi`, `_meta`, `_uazapi`, `_evolution`, `_ai_msg_dedup`, `_auth_order`, `_egress`, `_tts`, `_media_delivery`, `_image_helpers`, `test_phase1_handoff_post_commit`, `test_handoff_contextual_resumption`): 904 passed, **1 failed**: `test_webhook_zapi.py::TestZapiLongAudioOpensAHandoff::test_the_pause_follows_the_clinic_stand_by_policy[handoff3-True-deadline3]`, com `paused_until` 0,51 s **depois** do `datetime.now() + 2 dias` lido pelo teste em seguida (só possível com o relógio de parede voltando; a máquina rodava quatro varreduras PostgreSQL ao mesmo tempo). Reexecução isolada da classe: 21/21 em 3 rodadas no G2 e 21/21 em 3 rodadas no G1. Tratada como intermitência de ambiente; a re-revisão confirma.
  - **Re-revisão (laço 1): SHIP.** Mesmo revisor, PostgreSQL próprio novo em 55460 (mutantes em 55461), fingerprints `ff9bc327…`/`5c5d7f38…` iguais no início e no fim; o fixer mudou exatamente os cinco arquivos declarados; fontes do G1 iguais. Unitários 812; suítes `tests/api` pedidas 934 passed (a falha do orquestrador em `TestZapiLongAudioOpensAHandoff` não se repetiu); lint/mypy limpos; PG "evidence valid", 0 pulados: effective_enforcement 40, metering 57, orphan 60, rollout_fence 109, charge_workers 14, courtesy_rule 27, ledger 56, charge_mark 33, capability_ready 26, reply_media 25, webhook_media_delivery 6, courtesy_http 9, charge_badge 2. **schema_dominance vermelho uma vez** (3 de 38, o caso `events-api-key-enforced-retry`, corpo da resposta diferente entre config ligada e desligada; o fixer não tocou no caminho de `/v1/events`) e verde 38/38 em três reexecuções (incluindo pelo gate). Mutantes: MA, MB, MD1, ME, MZ1, MZ2, MZ3, MZ4, MZ6 mortos; MZ5 (fromMe também capturado) e MZ7 (D1 da regra segurado até a pré-checagem) sobrevivem (LOW).
  - HIGH-1 fechado (bloqueado na Z-API: sem download, STT, fallback, marca de não lida nem recibo; sessão pausada mantém a captura só de texto; marcado suspenso mantém fallback e recibo). Os três desvios do fixer aceitos. **Dominância (a):** a leitura de modo a mais nos caminhos de saída cedo da Z-API sob marca/global/none quebra a letra de "a única leitura nova é a da regra", sem mudar escritas nem respostas, e é inevitável para as decisões 11 e 16 do owner (saber o modo antes do STT e do recibo exige ler global e marca primeiro, pela precedência). **Emenda registrada pelo orquestrador:** na Z-API, e só nela, a leitura do modo acontece antes do bloco de áudio e do recibo de leitura também sob marca/global/none (mesmas instruções por mensagem atendida; uma leitura a mais nos caminhos que antes saíam cedo), por causa das decisões 11 e 16. **(b):** a despausa preguiçosa que agora fica quando a leitura falha sem latch é inofensiva e mais coerente (igual ao caminho em que a leitura funciona); registrada, sem correção.
  - Teste já existente que sai para a rede: `test_webhook_zapi.py::TestZapiSttFallbackChain::test_long_audio_without_capability_still_hands_off` resolve `api.z-api.io` (`mark_message_read` sem mock); acontece igual na árvore do G1 (`webhook.py` igual a `fe800aec`), então é anterior a este wish; higiene de teste para depois.
  - **Investigação do orquestrador da intermitência de dominância (2026-11-12):** os três testes do caso `/v1/events` (`test_config_on_answers_and_stores_like_config_off`, `test_with_the_config_off_an_enforced_database_answers_like_main`, `test_the_transport_on_keeps_dominance_and_presents_only_when_effective`) com `-vv`, 4 rodadas por árvore, dois clusters em paralelo (55463, 55464): **árvore do G2 falhou em 3 de 4 rodadas** (2–3 casos por rodada: `events-api-key-enforced-retry`, `events-api-key-session-start`), **árvore do G1 passou 4 de 4**. A diferença é a **ordem de `context.recent_messages`** na resposta de `/v1/events`: dois turnos inteiros da mesma sessão saem trocados. Conclusão: regressão de dominância introduzida pelo G2 (sensível a carga), não do oráculo; o SHIP da re-revisão não vale para este ponto. **Laço de correção 2 de 2 (o último)** despachado a um `genie:fixer` com causa raiz primeiro; LOW-A/LOW-B só depois, se couber.
  - **Laço 2 (fixer), causa raiz: não é regressão do G2; a conclusão anterior do orquestrador estava errada.** O relógio de parede do WSL2 volta ≈ 1 s a cada ≈ 29 s (dois passos de ≈ −0,4 s e ≈ −0,6 s, também com a máquina parada; medido com `time.time()` contra `time.monotonic()`), e o código de base, igual em `fe800aec`, no G1 e no G2, grava o `created_at` dos eventos com o relógio do Python (`src/memory/event_store.py:143`) e lê o histórico com `.order_by(EventModel.created_at.desc())` sem desempate (`src/services/event_service.py:1285`). Um passo para trás entre dois turnos da mesma sessão troca a ordem de `recent_messages`. Reprodução instrumentada com as duas árvores em paralelo (55465, 55466): G2 2 de 8 rodadas vermelhas, **G1 2 de 8** (no G1 as quatro rodadas verdes anteriores foram acaso); cada replay trocado tem um passo para trás dentro da janela da sessão. Conferido pelo orquestrador: log do relógio parado, resumos das rodadas, os dois arquivos iguais a `fe800aec`. O G2 não toca na gravação nem na leitura do histórico de `/v1/events`. Nenhuma correção aplicada (mudaria a ordem do histórico em produção para todos e está fora do escopo); worktree inalterado (`ff9bc327…`/`5c5d7f38…`). LOW-A/LOW-B não feitos. A mesma causa explica a falha de `TestZapiLongAudioOpensAHandoff` na validação do orquestrador.
  - **Decisão 17 do owner (AskUserQuestion, 2026-11-12), relógio do WSL:** o gate continua igual; se os três testes de dominância de `/v1/events` falharem, repete-se, e a falha só é aceita quando a única diferença é a ordem trocada de `recent_messages` (evidência registrada no log do grupo). Nenhuma mudança de código por isso neste wish. O mesmo vale para `TestZapiLongAudioOpensAHandoff::test_the_pause_follows_the_clinic_stand_by_policy` (limite de `now()` violado por um passo do relógio).
  - **Conclusão do G2:** revisão SHIP (re-revisão do laço 1) e validação do orquestrador verde (dominância 38/38 na rodada dele); a intermitência de dominância é anterior ao wish e de ambiente. `genie task done t_mwczay1d0aa29941`. Árvore revisada salva como `cortesia-g2-reviewed.patch` no scratchpad; a fonte durável é o worktree `/home/lorenzo/work/cortesia-g2`.
  - LOW restantes (não bloqueiam; LOW-A e LOW-B foram para o brief do G3, LOW-C fica para a consolidação): LOW-A pinar a guarda fromMe na captura (MZ5); LOW-B pinar o 503 imediato do D1 da regra na Z-API (MZ7); LOW-C opcional: com D1 da regra e sessão pausada, manter a captura só de texto e responder o 503 na pré-checagem.
  - Decisões do engenheiro aceitas pelo revisor (variante `courtesy=False` do teste dos workers, stub da re-checagem nos testes de armazenamento do G1, `rule_only`, `/v1/events` só `production`, `_note_mark_state`, handoff de arquivo, `CategoryVerdict` `"suspended"` inalcançável pelos chamadores de hoje, `origin` obrigatório).
- Para os grupos 3, 4 e 5a: `reply_media.py`, `media_assets.py` e `knowledge.py` precisam passar `captured=mode.captured` ao portão e mapear `"suspended"` para 402 `credit_suspended`; G3/G4 põem `"uazapi"`/`"evolution"` em `WebhookSource`, e a dominância deles precisa admitir a leitura de modo a mais por turno Uazapi/Evolution sob marca/global.

### Wave 3: despacho, 2026-11-12 (owner: "pode")

- Worktrees a partir de `fe800aec` + a árvore revisada do G2 (Decisão 2), conferidos (diff `ff9bc327…`, arquivo da regra `5c5d7f38…`): `/home/lorenzo/work/cortesia-g3` (branch `feat/courtesy-rule-g3`, PostgreSQL do engenheiro em 55467) e `/home/lorenzo/work/cortesia-g5a` (branch `feat/courtesy-rule-g5a`, 55468). Na Wave 3 só o G3 acrescenta a `credit_rollout_world.py` e `test_credit_schema_dominance_postgres.py`.
- Levado ao brief do G3: LOW-A/LOW-B da re-revisão do G2 (pinos da guarda fromMe e do 503 imediato do D1 da regra na Z-API); decisão 16 aplicada ao Uazapi se a rota mandar recibo de leitura; medir e reportar a diferença de instruções por turno Uazapi sob none/marca/global (uma leitura de modo, nunca duas; emenda análoga à da Z-API a registrar); decisão 17 (relógio do WSL).
- Levado ao brief do G5a: LOW-5 do G2 (mapear `"suspended"` para 402 `credit_suspended` em `media_assets.py`/`knowledge.py` ao passar `captured`); não mostrar prazos de episódio velho da marca em `/v1/credits/state` para builder da regra (G1); Decisão 4 (recarga continua bloqueada em quarentena).

### Group 5a: entrega do engenheiro, 2026-11-12

- Worktree `/home/lorenzo/work/cortesia-g5a`, sem commit. Conferido pelo orquestrador: `git diff | sha256sum` = `1cd60e7f1f0510b9c63c4120c59b70b9dede0257dd9b83fdcf2418369a403864`; arquivo da regra do G1 `5c5d7f38…` inalterado; novo `tests/integration/test_credit_courtesy_surfaces_postgres.py` `6b7534dfb0a9384754eba2c1bdfa3be44298511401cd4cea4330fd19b10de8c4`. `credit_metering.py`, `credit_lifecycle.py`, `credit_ledger.py`, `credit_inventory.py`, `repositories/credit.py`, `webhook.py`, `events.py` iguais aos do G2; nada em migrations, `src/models`, frontend, docs ou arquivos de invariante do ASAAS.
- Relato do engenheiro (a confirmar): uploads de RAG/mídia de builder bloqueado → 402 `credit_suspended` antes de ler o corpo (chave e painel); busca e download por chave → 402; leitura, listas e exportação do painel abertas; `/v1/credits/*` sob a regra com `state` `active`/`suspended`, `mode` `enforced`, prazos nulos (inclusive com episódio velho da marca), `quarantine` mantido, nunca `reconciliation`, `net_available_mc` = cortesia gastável, limiares na base da cortesia; master-admin com `courtesy_rule` (`"charged"`/`"blocked"`/`null`) em leitura em lote; `credit_charge_mark status` com `origin: "courtesy"` e `state`, `mark` recusa `window_already_opened` por abertura sob a regra; comentários (guarda do L10 com `return False` e a referência mantida, `BindingCoverage.usable_courtesy`, `binding_coverage`, `admin_credits.py:127`). OpenAPI: só 14 descrições mudaram, estrutura idêntica. Validação do engenheiro: 447 unitários; PG charge_badge 2, courtesy_http 9, charge_mark 35, rollout_fence 109, courtesy_rule 27, courtesy_surfaces 8, 0 pulados; varredura extra verde; 27 de 27 mutantes pegos.
- Decisão a julgar pelo revisor (Decisão 8): um builder marcado que tem lotes de cortesia ganha duas leituras (global e marca) em `/balance` e `/thresholds` avulsos, com respostas iguais. Fica para a consolidação: o docstring de `MarkWindowState` (`src/repositories/credit.py:474-481`, arquivo do G1) ainda diz que a abertura veio de uma marca.
- **Validação do orquestrador** (2026-11-12 21:37:38Z → 21:55:05Z, PostgreSQL próprio em 55473, diff `1cd60e7f…` e arquivos novos `5c5d7f38…`/`6b7534df…` iguais no início e no fim, `src` no worktree): bloco do G5a, exit 0. Unitários 447 passed; PG "evidence valid", 0 pulados: charge_badge 2, courtesy_http 9, charge_mark 35, rollout_fence 109, courtesy_rule 27, courtesy_surfaces 8; black/isort/flake8 limpos; mypy sem problemas em 217 arquivos; log do servidor com 3 tipos de ERROR, todos sondas intencionais (`no_such_table`, `no_such_marks_table` e afins). `make quality` não rodado.

### Group 5a: revisão de execução (fim 22:04:48Z): SHIP

- Revisor independente, PostgreSQL próprio em 55471 (sondas e mutantes em 55481), fingerprints iguais no início e no fim, delta de 19 arquivos dentro da lista do G5a, nada editado. Unitários do bloco 447; PG "evidence valid", 0 pulados: charge_badge 2, courtesy_http 9, charge_mark 35, rollout_fence 109, courtesy_rule 27, courtesy_surfaces 8, marks_migration 44, schema_dominance 38 (verde na primeira); suítes extras de `tests/api` (35 arquivos) 1361 passed; lint/mypy limpos; log só com provas negativas intencionais. OpenAPI gerado nas duas árvores: só 14 descrições mudaram. Sonda lado a lado contra a árvore do G2 com 10 builders e as rotas `/v1/credits/*`, busca, download, uploads, master-admin e contagem de escritas: fora da regra, respostas e escritas idênticas; o SQL só difere em `/balance` e `/thresholds` para builders com lotes de cortesia (global + marca para o marcado; mais regra e plano pago para o protegido) e no master-admin (um lote de lotes e um de plano pago).
- Critérios Bloqueio (rotas), ASAAS (rota de estado), Superfícies (backend), Sem episódio (limiares), OpenAPI e o carry-over do episódio velho: PASS. Decisões do engenheiro aceitas (PATCH/DELETE de mídia sem portão: não custam nem chegam ao paciente; bloqueio logo depois da autenticação, ordem de erro igual fora da regra; `as_of`; `exhausted_at` nulo; `net_available_mc`).
- **Emenda registrada pelo orquestrador (Decisão 8):** em `/v1/credits/balance` e `/v1/credits/thresholds` avulsos, um builder fora da regra que tem lotes de cortesia (marcado ou protegido) ganha a leitura de precedência (global + marca, e regra + plano pago para o protegido), com respostas e escritas iguais; é a mesma leitura de precedência das emendas da Z-API e do Uazapi.
- Gaps (não bloqueiam): **MEDIUM-1** nada pina `/v1/credits/state` = `suspended` para cortesia vencida com saldo e sem plano pago (DESIGN Decisão 7; mutante X18 sobrevive). **MEDIUM-2** `admin._courtesy_rule_by_builder`/`admin._paid_plans_in_force` duplicam o SQL de `CreditRepository.courtesy_rule`/`paid_plan_in_force`; a matriz de 12 casos pega dois desvios (X1, X17) mas não o X3 (binding não `active`). **LOW-1** limiares com capacidade zero respondem `used_percent: 100`, `used_mc: 0`, `reached: []` sem pino. **LOW-2** `builder_subscriptions` lê `credit_switch_reading()` duas vezes. **LOW-3** o teste de dominância das superfícies não remenda o predicado do admin no passe "regra fechada". Notas: docstring de `MarkWindowState` (`src/repositories/credit.py:474-481`); mensagem `CreditEnforcementRejected("uazapi has no enforced credit adapter")` levemente velha (cosmética).
- **Decisão do orquestrador:** o G5b parte da árvore revisada do G5a e o G3 ainda está em revisão, então um laço curto do `genie:fixer` corrige agora MEDIUM-1, MEDIUM-2 (caso X3 na matriz e, se mantiver o SQL do caminho de requisição byte a byte, uma definição só no repositório), LOW-1, LOW-2, LOW-3 e o docstring de `MarkWindowState`.
- **Laço de reforço do G5a (fixer):** fingerprints conferidos pelo orquestrador: `git diff | sha256sum` = `6fd6567871c4e17957e20e7c25e75b1116380bfef1dd44cbf75df385f426bafb`; regra `5c5d7f38…` inalterado; superfícies `c788a6a35f2d3206823732edb9a5b7478bc1b7b85c8bb2cdb66fb73cde7b31c8`. `credit_metering.py`, `credit_lifecycle.py`, `credit_ledger.py`, `credit_inventory.py`, `webhook.py`, `events.py`, `credit_rollout_world.py` iguais aos do G2; **`src/repositories/credit.py` mudou** (refatoração do MEDIUM-2); o do G3 é igual ao do G2, então a consolidação não conflita (o G4 não pode tocar nesse arquivo). Relato do fixer (a confirmar na re-checagem): MEDIUM-1 (cortesia vencida com saldo → `suspended`, prazos nulos, `net_available_mc` 0), MEDIUM-2(a) caso `reversed_binding_blocked` na matriz, MEDIUM-2(b) **refatoração feita**: construtores compartilhados no repositório, leitores em lote `courtesy_rules_for_builders`/`paid_plans_in_force_for`, `admin._paid_plans_in_force` removido; SQL do caminho de requisição compilado antes e depois com os parâmetros, idêntico (sha256 `57e3947d…` nos dois); LOW-1 (resposta de capacidade zero pinada e descrita), LOW-2 (uma leitura do switch), LOW-3, docstring de `MarkWindowState`. Validação do fixer: 708 unitários, 0 pulados; PG charge_badge 2, courtesy_http 9, charge_mark 35, rollout_fence 109, courtesy_rule 27, courtesy_surfaces 8, schema_dominance 38, effective_enforcement 40, ledger 56, metering 57, binding_grace 26, 0 pulados; lint/mypy limpos; 6 de 6 mutantes pegos.

### Group 3: entrega do engenheiro, 2026-11-12

- Worktree `/home/lorenzo/work/cortesia-g3`, sem commit. Conferido pelo orquestrador: `git diff | sha256sum` = `de1e5876a67784655a6bcb775052adc2b04ba3b76b01a3f1cce7c64d3e909dfa`; arquivo da regra `5c5d7f38…` inalterado; novo `tests/integration/test_credit_uazapi_enforced_postgres.py` `58eb383eada720b7243857bf8d05940382b478a375b83fd65b0b2006367346da`. `credit_lifecycle.py`, `credit_ledger.py`, `credit_inventory.py`, `repositories/credit.py`, `credit_rollout.py` (guarda do L10 `return False`), `events.py` e `credit_rollout_world.py` iguais aos do G2; nada do G5a, migrations, `src/models`, frontend, docs, Evolution ou arquivos de invariante do ASAAS.
- Relato do engenheiro (a confirmar): `"uazapi"` em `WebhookSource`; `capture_uazapi`/`precheck_uazapi` pelo `precheck_webhook`; leitura do modo antes do download/STT/fallback para áudio de paciente e, no resto, na pré-checagem (depois das portas de agente desligado e pausa, antes do portão de egress de imagem, da sessão, LGPD, handoffs, descrição de imagem e envios); bloqueado decidido sem ler `uazapi_egress_enabled`; claim Redis igual em todo turno, PG só nos cobrados; `CreditReplySender` com ramo `uazapi` (texto e áudio TTS, eco registrado); rotas internas cobradas; sessão pausada de bloqueado mantém o texto; egress fechado → não cobrado; mídia de saída Uazapi só sob `courtesy`, bloqueado sem mídia; origem real no `decision_digest`; reentrega cruzando mudança de modo sem débito nem resposta duplicados (cinco casos PG) e corrida concorrente; o Uazapi não manda recibo de leitura hoje (nada a suprimir); LOW-A e LOW-B da Z-API feitos. Validação do engenheiro (comando do brief, exit 0): 1431 unitários/rota; PG uazapi_enforced 15, reply_media 31, webhook_media_delivery 6, effective_enforcement 40, metering 57, schema_dominance 48, rollout_fence 109, orphan 60, charge_workers 14, courtesy_rule 27, 0 pulados; lint/mypy limpos; 20 de 20 mutantes pegos. Duas falhas avulsas de dominância de `/v1/events` antes da rodada final (uma em `test_the_schema_revision_alone_changes_nothing`, fora da lista da decisão 17), ambas com diff só na ordem de `recent_messages`, em rodadas `below-minimum` onde o código do G3 não roda.
- **Diferença de instruções por turno Uazapi fora da regra** (switch ligado na entrada; zero com ele desligado), provada por `test_a_uazapi_turn_outside_the_rule_is_main_s_plus_one_mode_read` (5 modos × verificado/não): mesmas respostas, envios e escritas que a rota de `prod-fe800aec`, mais um bloco contíguo de leituras de modo: global +1, tenant avulso +2, marca +3, builder sem cortesia +4 (inclui a leitura agregada de `credit_lots` da regra), protegido +5 (inclui a do plano pago); nunca uma segunda leitura. Mídia de saída Uazapi com global desligado: +1 leitura `rule_only` (+4/+3 instruções). **Emenda registrada pelo orquestrador (análoga à da Z-API):** no Uazapi (e na Evolution, no G4) a leitura de precedência global → marca → regra entra uma vez por turno, também fora da regra, porque saber se o builder está sob a regra exige ela (DESIGN item 1, decisões 3, 11 e 18); as escritas e respostas fora da regra continuam as de `prod-fe800aec`.
- Decisões do engenheiro onde o design era silente: Uazapi verificado também só cobra sob a regra (coerente com a Decisão 18 do DESIGN); leituras `rule_only` não mexem no conjunto D1 da marca; mensagem sem id tem claim `payload-sha256:<hex>`; flag de egress lida só para captura cobrada; claim Redis de áudio bloqueado no início do bloco pulado; egress fechado entre a pré-checagem e o envio → outbox `failed` e decisão cobrada (como resposta perdida); turno cobrado sem resposta usa o corpo compartilhado (`no_llm_response`); mídia Uazapi com duas leituras sem global (o auxiliar de leitura única está em `credit_inventory.py`, fora do escopo).
- **Decisão 18 do owner (AskUserQuestion, 2026-11-12):** se o builder fica bloqueado entre a resposta e o arquivo (a própria resposta gastou o último crédito), o texto fixo de arquivo não enviado **vai** ao paciente (sem custo); a partir da mensagem seguinte, silêncio. É o que o G3 implementou.
- Para o G4: `"evolution"` em `WebhookSource`, captura/pré-checagem com `capture_webhook(rule_only=True)` sem cláusula de egress, `"evolution"` em `RULE_ONLY_PROVIDERS`, `_CREDIT_REPLY_LABELS` e `_CREDIT_MEDIA_PROVIDERS`, ramo no `CreditReplySender` e no teste de tempo de envio. Nota: `tests/credit_switch_forced_plugin.py` não lista a suíte PG nova do Uazapi em `ENFORCED_SUITES`.
- A decisão 17 (relógio do WSL) passa a cobrir qualquer caso de dominância de `/v1/events` cuja única diferença seja a ordem de `recent_messages`, não só os três testes citados.
- **Validação do orquestrador** (2026-11-12 21:37:35Z → 22:13:36Z, PostgreSQL próprio em 55472, diff `de1e5876…` e arquivos novos `5c5d7f38…`/`58eb383e…` iguais no início e no fim, `src` no worktree): bloco do G3, exit 0. Unitários e de rota (Uazapi, egress, dedupe, ordem de autenticação, entrega de mídia, suspensão, Z-API, Meta, Evolution, reply_media, publicador, medição, comando de marca) 1431 passed; PG "evidence valid", 0 pulados: uazapi_enforced 15, reply_media 31, webhook_media_delivery 6, effective_enforcement 40, metering 57, schema_dominance 48 (verde na primeira), rollout_fence 109, orphan_reconciliation 60, charge_workers 14, courtesy_rule 27; black/isort/flake8 limpos; mypy sem problemas em 217 arquivos; log do servidor com 9 tipos de ERROR, nenhum de sintaxe/objeto inexistente (provas negativas intencionais). `make quality` não rodado.

### Group 3: revisão de execução: SHIP

- Revisor independente, PostgreSQL próprio em 55469, fingerprints iguais no início e no fim (G3 `de1e5876…`, regra `5c5d7f38…`, Uazapi `58eb383e…`; G2 `ff9bc327…`), nada editado; o delta Uazapi do G3 é o mesmo contra `fe800aec`. Unitários/rota da lista 1431; suítes extras de handoff/STT/LGPD/imagem/TTS/Uazapi 588; PG "evidence valid", 0 pulados: uazapi_enforced 15, reply_media 31, webhook_media_delivery 6, effective_enforcement 40, metering 57, schema_dominance 48 (verde na primeira), rollout_fence 109, orphan 60, charge_workers 14, courtesy_rule 27; lint/mypy limpos; plugin de switch forçado nas suítes de webhook 498 passed; testes novos sob guarda de rede só loopback 536 passed, 0 tentativas de rede; log só com provas negativas. Mutantes do revisor: 21 mortos, 3 sobreviventes equivalentes.
- Critérios Uazapi, Mídia de saída, Reentrega cruzando mudança de modo, não verificado só sob `courtesy`, Origem real, carry-overs LOW-A/LOW-B: PASS. **Oráculo de dominância fiel:** os mesmos turnos sob global/avulso/marca/none/protegido (verificado e não) na rota do G3 e na rota do G2 (= `fe800aec` para o Uazapi): respostas, envios e escritas idênticos nos 10 casos; instruções iguais salvo um bloco contíguo de 1–5 leituras de modo. Varredura por tipo de mensagem: nada chega ao paciente de um bloqueado antes do bloqueio; o Uazapi não tem recibo de leitura. Decisões do engenheiro aceitas (inclusive o claim `payload-sha256:` de mensagem sem id, melhor que hoje; a decisão 18 do owner confirmada sem custo).
- **MEDIUM-1:** fora da regra, numa leitura de modo que falha sem latch, `read_tenant_enforcement` faz rollback depois de `_check_pause_state` e desfaz a despausa preguiçosa: o agente responde mas a sessão fica `is_paused=True` (reproduzido; no `fe800aec` a sessão é despausada). Correção proposta: capturar todo turno de paciente antes de `_check_bot_active`/`_check_pause_state` (como o G2 fez na Z-API) e decidir na pré-checagem com `early`, com teste de regressão; alternativa: `db.commit()` logo antes de `precheck_uazapi`.
- **LOW-1:** a entrega de mídia faz a própria leitura de precedência (separada da leitura do turno); a emenda do Uazapi conta essa leitura à parte. **LOW-2:** egress fechando durante o LLM cobra uma resposta não entregue (igual à regra de resposta perdida da Z-API/Meta; reler na final contraria a Decisão 14 do DESIGN) → resíduo aceito, a registrar no runbook (G7). **LOW-3 (opcional):** mudança de modo de cobrado para grátis depende só do Redis para deduplicar (já era assim pela Decisão 16); teste PG opcional. **NIT-1:** `precheck_uazapi` passa a `precheck_webhook` três argumentos que nunca são usados quando `early` vem.

### Wave 3: continuação sem esperar (owner: "pode seguir sem esperar")

- O revisor do G5a que o owner retomou pela transcrição ficou parado e foi encerrado pelo owner; para a re-checagem aprovada do G5a (o reforço tocou `src/repositories/credit.py`) foi aberto um `genie:reviewer` novo e independente, com o contexto completo, incluindo a comparação própria do SQL do caminho de requisição contra a árvore do G2.
- Reforço do G3 despachado a um `genie:fixer` (≠ engenheiro, ≠ revisor): MEDIUM-1 (capturar todo turno de paciente antes das portas de agente desligado e pausa, como na Z-API, com teste de regressão da despausa), LOW-3 (teste PG documentando a dedupe só por Redis na mudança cobrado → grátis) e NIT-1. LOW-1 (leitura própria da entrega de mídia) entra na emenda do Uazapi como leitura separada da do turno; LOW-2 vai para o runbook do G7.
- **Validação do orquestrador do G5a após o reforço** (2026-11-12 22:53:26Z → 23:15:59Z, PostgreSQL próprio em 55473, diff `6fd65678…` e arquivos novos `5c5d7f38…`/`c788a6a3…` iguais no início e no fim, `src` no worktree): exit 0. Unitários (os 9 do G5a + ledger, lifecycle, switch) 708 passed; PG "evidence valid", 0 pulados: charge_badge 2, courtesy_http 9, charge_mark 35, rollout_fence 109, courtesy_rule 27, courtesy_surfaces 8, schema_dominance 38 (verde na primeira), effective_enforcement 40, ledger 56, metering 57, binding_grace 26, courtesy 13, maintenance 11, retention 18; black/isort/flake8 limpos; mypy sem problemas em 217 arquivos; log do servidor sem erro de sintaxe, operador ou coluna inexistente (os 81 tipos de ERROR são provas negativas das suítes do núcleo). `make quality` não rodado.

### Group 5a: re-checagem após o reforço: SHIP, e conclusão

- Revisor novo e independente, PostgreSQL próprio em 55471 (mutantes em 55481), fingerprints `6fd65678…`/`5c5d7f38…`/`c788a6a3…` iguais no início e no fim, os sete arquivos que têm de ser iguais aos do G2 iguais; o fixer tocou exatamente os 8 arquivos declarados. Bloco com núcleo: unitários 708, PG 14 arquivos "evidence valid", 0 pulados (dominância verde na primeira); lint/mypy limpos.
- **SQL do caminho de requisição byte a byte igual, prova própria:** 14 estados de builder semeados num PostgreSQL real, os dois trees (G2 e G5a) rodando o mesmo script no mesmo banco com 4 instantes fixos (incluindo 02:30Z, onde a data de São Paulo difere da UTC): 224 chamadas, 516 instruções, texto e parâmetros posicionais do asyncpg e todos os resultados gravados; arquivos de saída idênticos (sha256 `a76896f4…`). Leitores em lote iguais aos individuais nos 14 estados × 4 instantes (0 divergências), 2 instruções por chamada independentemente do número de builders, predicados indexáveis. Diff completo de `credit.py` contra o G2 só com os auxiliares novos, os dois métodos reconstruídos, os dois em lote e o docstring. Respostas e escritas da sonda da primeira revisão iguais (172 chaves). Mutantes: 22 de 24 mortos; sobreviventes P5s/P5b (data UTC em vez de São Paulo), lacuna anterior ao wish.
- Gaps abertos (não bloqueiam; ficam para a consolidação): **LOW-1** `_aware_utc` morto em `src/api/routes/admin.py:1035-1040` (apagar); **LOW-2** nenhum teste pina a data de São Paulo do ciclo corrente (prova PG sugerida com ciclo `settled` sem binding, `cycle_start = D+1`, julgado às 01:00Z de D+1 e 3 h depois, também pelo leitor em lote); **NIT** `paid_plans_in_force_for` ancora em `builders`, sem FK de `credit_accounts.builder_id`; ancorar em `CreditAccount.builder_id`.
- `genie task done t_mwczaydk2ff24822`. Delta do G5a contra a árvore do G2 salvo como `cortesia-g5a-delta.patch` (20 arquivos, sha256 `c9e2c08aea4e462666e92cc87158bc94cdf2c10a76b300607db65f2c012a0833`, gerado por árvores temporárias com `GIT_INDEX_FILE`, sem tocar no índice real); fonte durável: o worktree `/home/lorenzo/work/cortesia-g5a`.

### Group 3: reforço interrompido pelo reinício da sessão (2026-11-12, ~23:45Z)

- O fixer do reforço do G3 aplicou mudanças (última edição 22:57Z; arquivos `src/api/routes/webhook.py`, `src/services/credit_metering.py`, `tests/integration/test_credit_uazapi_enforced_postgres.py` e possivelmente outros) e foi interrompido pelo reinício da sessão durante a própria validação (log de unitários vazio); sem relatório. O PostgreSQL dele (55475) ficou órfão e foi parado e apagado pelo orquestrador. **Não havia cópia salva da árvore revisada (`de1e5876…`)**: `mutc3-base` no scratchpad era o estado novo (conferido montando G2 + esses arquivos: diff `3bed799d…`).
- Estado atual salvo como `cortesia-g3-postfix-unverified.patch` (sha256 `1566ee482a05cfbc1830f6dbf31b4f7988e95db60e7e57bad717630d1a7fcba2`): `git diff | sha256sum` = `3bed799d1c7d2204892ed3a70d3ee75508f4894aae2f108402fd345e47764c07` (31 arquivos); `test_credit_uazapi_enforced_postgres.py` `c5c94aade1eba9adbc1a1fa1ce806eb4f3b079813ce04da710b5037a0e9d9731`; arquivos do núcleo iguais aos do G2.
- **Escolha do owner (AskUserQuestion):** manter as mudanças do fixer, sem retomá-lo; o orquestrador roda a validação completa do G3 (PostgreSQL 55472) e um revisor novo e independente (o anterior não pôde ser retomado: transcrição da sessão antiga) checa só o que mudou depois da revisão (MEDIUM-1, LOW-3, NIT-1) e procura restos de edição interrompida.
- **Validação do orquestrador da árvore atual** (2026-11-12 23:52:48Z → 2026-11-13 00:24:08Z, PostgreSQL próprio em 55472, diff `3bed799d…` e arquivos novos `5c5d7f38…`/`c5c94aad…` iguais no início e no fim, `src` no worktree): exit 0. Unitários e de rota (a lista do G3 mais handoff contextual, handoff pós-commit da fase 1, TTS e auxiliares de imagem) 1566 passed; PG "evidence valid", 0 pulados: uazapi_enforced 17 (eram 15), reply_media 31, webhook_media_delivery 6, effective_enforcement 40, metering 57, schema_dominance 48 (verde na primeira), rollout_fence 109, orphan_reconciliation 60, charge_workers 14, courtesy_rule 27; black/isort/flake8 limpos; mypy sem problemas em 217 arquivos; log do servidor só com provas negativas intencionais. `make quality` não rodado.

### Group 3: checagem pós-reforço: SHIP, e conclusão

- Revisor novo e independente, PostgreSQL próprio em 55469 (mutantes em 55470), fingerprints `3bed799d…`/`c5c94aad…`/`5c5d7f38…` iguais no início e no fim, nada editado. Arquivos mudados depois da primeira revisão, por mtime e conferidos contra os artefatos `rev3/` do primeiro revisor (strings dos mutantes, oráculo de dominância e sonda da despausa reexecutados): exatamente `webhook.py`, `credit_metering.py`, `test_credit_suspension_webhooks.py`, `test_credit_metering.py`, `test_credit_uazapi_enforced_postgres.py`.
- **MEDIUM-1 PASS:** captura incondicional (`webhook.py:5250-5264`) antes de `_check_bot_active` e `_check_pause_state`, decisão na pré-checagem com `early`; teste de regressão `test_a_failed_uazapi_mode_read_outside_the_rule_keeps_the_lazy_unpause` (4 modos); sonda: a sessão termina despausada como no `fe800aec`; rota atual contra a rota do `fe800aec` transplantada (5 modos × verificado/não × 5 tipos de turno): escritas, envios e respostas idênticos, um bloco contíguo de leituras de modo, nunca uma segunda leitura. **LOW-3 PASS** (`test_charged_then_free_with_the_replay_store_down_is_the_accepted_residual`, comportamento de hoje documentado). **NIT-1 PASS** (`precheck_uazapi` chama `decide_webhook`; `precheck_webhook`, `decide_webhook`, `capture_zapi`, `precheck_zapi`, `precheck_meta` iguais aos do G2 por AST). Nenhum resto de edição interrompida.
- Validação do revisor: 858 unitários; PG uazapi_enforced 17, schema_dominance 48 (verde na primeira), metering 57, effective_enforcement 40, 0 pulados; lint/mypy limpos; testes do fixer sob guarda de rede só loopback; 14 de 14 mutantes mortos.
- Gaps (não bloqueiam; vão para o G4, que parte desta árvore e precisa do mesmo para a Evolution): **LOW-1** o plugin de switch forçado (`tests/credit_switch_forced_plugin.py:203-216, 288-289`) conta só `precheck_webhook`, então as decisões do Uazapi não chegam a ele (provado); envolver `credit_metering.precheck_uazapi` (e, no G4, `precheck_evolution`) como `_counted_webhook_precheck`, assertar em `_assert_forced` e pôr as suítes PG novas em `ENFORCED_SUITES`. **NIT-1** `prechecks == []` em `test_credit_suspension_webhooks.py:1223-1230, 1258` não falha mais (espiar `decide_webhook`). **NIT-2** pinar `seen.observed == [("unknown", False, 0)]` e uma linha `delivered` no teste do LOW-3. **NIT-3** docstring de `test_credit_metering.py:5367`.
- `genie task done t_mwczay5j7520b88f`. Árvore revisada salva como `cortesia-g3-reviewed.patch` (igual ao `cortesia-g3-postfix-unverified.patch`, agora revisado) e a do G5a como `cortesia-g5a-reviewed.patch`, no scratchpad da sessão `f946da1d`.

### Wave 4: despacho, 2026-11-13 (owner: "pode")

- Worktrees a partir de `fe800aec`, conferidos: `/home/lorenzo/work/cortesia-g4` (branch `feat/courtesy-rule-g4`) = árvore revisada do G3 (diff `3bed799d…`, arquivos novos `5c5d7f38…`/`c5c94aad…`; PostgreSQL do engenheiro em 55476); `/home/lorenzo/work/cortesia-g5b` (branch `feat/courtesy-rule-g5b`) = árvore revisada do G5a (diff `6fd65678…`, arquivos novos `5c5d7f38…`/`c788a6a3…`).
- G4 (`engineer-complex`): adaptador da Evolution no molde final do Uazapi (captura uma vez antes das portas de agente desligado e pausa, decisão na pré-checagem depois delas e antes da sessão, sem cláusula de egress, `capture_webhook(rule_only=True)`); proibido tocar em `src/repositories/credit.py` (refatorado pelo G5a) e nos arquivos do G5a; carry-overs da checagem do G3: LOW-1 (plugin de switch forçado contando `precheck_uazapi`/`precheck_evolution`, suítes novas em `ENFORCED_SUITES`), NIT-1, NIT-2, NIT-3; decisões 11, 16, 17 e 18 do owner no brief.
- G5b (`engineer-standard`, esforço alto): só frontend, com as formas exatas das respostas revisadas no G5a (incluindo a resposta de capacidade zero dos limiares); pnpm só dentro do próprio worktree (`--frozen-lockfile --offline --ignore-scripts`), nunca numa cópia que compartilha `node_modules`.

### Group 5b: entrega do engenheiro, 2026-11-13

- Worktree `/home/lorenzo/work/cortesia-g5b`, sem commit. Conferido pelo orquestrador: `git diff -- frontend | sha256sum` = `e8693e33655589c22ffd071dd5eedca4b36cbefbea239dd779c7f2111c17e267` (8 arquivos do frontend), diff completo `2285814dd6ea11bf2f1be8d0cd368cb40d832d81da2fe8d61556ebcaff53e64b`, parte de backend `6fd65678…` (igual ao G5a); nenhum link quebrado em `node_modules`; nada rodando. Patch do frontend salvo como `cortesia-g5b-frontend.patch`.
- Relato do engenheiro (a confirmar): `chargedInQuarantine` e `creditBalanceStanding` novos (exibição); `creditLedgerStanding` inalterado decidindo só a recarga (Decisão 4); capacidade zero → "Capacidade consumida: Sem créditos em vigor" sem barra; selos do master-admin "Cortesia: cobrando"/"Cortesia: bloqueado"; texto do "Tirar tudo": "Se este for o último saldo de cortesia do builder e ele não tiver plano pago em vigor, ele fica bloqueado na hora: o agente para de responder."; três snapshots HTML fixando o render de hoje (marcado `pending`+quarentena, marcado `suspended`+quarentena, regra `suspended`+quarentena). Validação do engenheiro: vitest focado 413, typecheck e build ok, pacote inteiro 45 arquivos/1124 testes; 11 de 11 mutantes. Capturas de tela reais (Chromium, desktop e celular) em `g5b-shots/`; o orquestrador olhou cobrado em quarentena (saldo sem "Pendente", recarga bloqueada com aviso de pendência) e bloqueado com capacidade zero ("Serviço suspenso por falta de créditos", "Sem créditos em vigor").
- Revisão independente despachada; a validação do orquestrador roda depois dela, para não haver dois builds no mesmo `dist/`.

### Group 5b: revisão de execução: SHIP

- Revisor independente, fingerprints (backend `6fd65678…`, frontend `e8693e33…`, completo `2285814d…`) iguais no início e no fim; 33 arquivos mutados e restaurados conferidos por sha256; sem links quebrados; nenhum servidor de preview deixado. Vitest focado 413/413; typecheck e build exit 0; pacote inteiro 45 arquivos/1124. Procedência dos pinos provada (fontes revertidas para `HEAD`: os 3 snapshots passam e os testes novos distinguem o antigo do novo). Mutantes do revisor: 11 de 13 mortos (M5, M6 sobrevivem).
- Critérios ASAAS (tela), Superfícies (frontend), cobertura dos quatro estados e render igual do builder marcado: PASS. A decisão da recarga continua sendo `creditLedgerStanding` sem mudança de lógica; `chargedInQuarantine` fecha em caso de dúvida; "Recarregue para reativar." correto (recarga paga → protegido); textos precisos.
- **MEDIUM-1:** a guarda de dominância em `noCapacityInForce` (`thresholds.reached.length === 0`) não tem teste (M5); o fixture "shows zero balances as data and an empty history…" (`billing.test.tsx` ~1024-1042) omite `reached`, que o servidor real devolve `[50, 90, 100]`; acrescentar e assertar a barra de 100 % e os alertas. **MEDIUM-2:** a captura 02 e os testes de bloqueado com capacidade zero usam fixtures que o servidor não produz (`_funded_mc` não desconta consumo; capacidade zero só com toda cortesia vencida ou retirada inteira antes de qualquer uso): usar lote de cortesia vencido, renomear o teste, corrigir o comentário de `noCapacityInForce` e refazer a captura 02.
- LOW: **LOW-1** captura 03 (protegido) mostra 0 %, o servidor real mostraria 80 % com o lote de cortesia esgotado e ainda não vencido (comportamento anterior ao wish: um protegido vê a cortesia gasta em "Capacidade consumida" até o lote vencer); **LOW-2** cobrado em quarentena com recarga comercial desligada não vê nenhuma menção à verificação de pagamento; **LOW-3** o aviso do diálogo também aparece para marcado/regra desligada (texto condicional, aceitável); **LOW-4** M6 equivalente na prática.
- **Decisão 19 do owner (AskUserQuestion, 2026-11-13):** mostrar uma linha neutra para o builder cobrado em quarentena quando a área de recarga não aparece (ex.: "Um pagamento está em verificação; o consumo da cortesia não é afetado."); nada muda para os demais.
- Plano: laço curto do `genie:fixer` no G5b (MEDIUM-1, MEDIUM-2, decisão 19, captura 03 com dados reais) depois que a validação do orquestrador terminar no mesmo worktree.
- **Validação do orquestrador** (2026-11-13 00:56:00Z → 00:58:20Z, no worktree, fingerprints completo `2285814d…`/frontend `e8693e33…` iguais no início e no fim): vitest focado 5 arquivos, 413 passed; `CI=true pnpm run typecheck` exit 0; build exit 0 (só os avisos de sourcemap de `src/components/ui/collapsible.tsx` e `progress.tsx`, arquivos não tocados); pacote inteiro 45 arquivos, 1124 passed; 0 links quebrados em `node_modules`.

### Group 5b: laço de reforço (fixer), 2026-11-13

- Fingerprints conferidos pelo orquestrador: frontend `4e87a94ef762c31e3eb75f7ea3d9a04fc2ac0195211704cf2fb3a01cd5721f70`, completo `dd58da026927b7d3df5e074267be72fa43427ac56521827e3308c1c767081751`, backend `6fd65678…` (inalterado); só `credits-and-topups.tsx` e `billing.test.tsx` mudaram; 0 links quebrados; nada rodando. Patch salvo como `cortesia-g5b-frontend-r2.patch`.
- Relato do fixer (a confirmar na re-checagem): MEDIUM-1 (`reached: [50, 90, 100]` no fixture do builder fora da regra a zero, com barra de 100 % e alertas assertados); MEDIUM-2 (fixture `expiredCourtesyHistory` no formato de `lot_view`, testes de bloqueado com capacidade zero renomeados para "…courtesy expired or removed in full before use", comentário de `noCapacityInForce` corrigido; o pino do bloqueado em quarentena pela regra ficou como estava); **decisão 19**: linha "Um pagamento está em verificação; o consumo da cortesia não é afetado." (EN "A payment is being verified; courtesy usage is not affected.") só para cobrado em quarentena com a área de recarga escondida, nunca junto da linha de recarga bloqueada, ausente nos outros cinco estados; os três pinos HTML intactos. Validação do fixer: vitest focado 421, typecheck e build ok, pacote 1132; 4 de 4 mutantes. Capturas refeitas (02 com lote vencido, 03 protegido com 80 % e "Alertas atingidos: 50%", 06 nova com a linha); o orquestrador olhou a 06. Resto anotado: o teste "blocked after spending all its courtesy" ainda usa um lote ativo com 4.000 ao lado de 0 gastável (inconsistência de fixture, fora do brief).

### Group 4: entrega do engenheiro, 2026-11-13

- Worktree `/home/lorenzo/work/cortesia-g4`, sem commit. Conferido pelo orquestrador: `git diff | sha256sum` = `fed6453fada3388d0a7e1481921c49411a57dad7efc27673161c63ca827dac8b` (32 arquivos contra `fe800aec`); não rastreados: regra `5c5d7f38…` (inalterado), Uazapi `ddb29d8265b55663e044dd90b9d850a0f093ce965cfe80f7fca673a97e8d1c91` (só NIT-2), Evolution `9a73909b1b1b00afbc4990bdabcdac980a8524868989ee89ac95f6e32b5d7941` (novo); `src/repositories/credit.py`, arquivos do G5a e serviços do núcleo iguais aos do G3; cluster 55476 parado. Patch salvo antes de qualquer revisão: `cortesia-g4-engineer.patch`.
- Relato do engenheiro (a confirmar): `"evolution"` em `WebhookSource`, `capture_evolution`/`precheck_evolution` (`data.key.id`, `rule_only`, sem egress); rota `/evolution` com guardas de fromMe/chamada/eco antes da captura, captura antes das portas de agente desligado e pausa, decisão na pré-checagem antes da sessão; ramo `evolution` no `CreditReplySender` e no publicador (eco registrado); mídia de saída só sob `courtesy`; LGPD grátis; handoff de IA cobrado; a Evolution não tem STT, áudio longo, encaminhamento de arquivo nem imagem (mensagens não-texto saem cedo como no `fe800aec`); sem recibo de leitura. Fora da regra: +1/+2/+3/+4/+5 leituras (global/avulso/marca/none/protegido), nunca uma segunda. Carry-overs do G3 feitos (plugin de switch forçado conta Uazapi e Evolution, suítes em `ENFORCED_SUITES`; NIT-1, NIT-2, NIT-3). Validação do engenheiro: 1404 unitários/rota; PG evolution_enforced 15, uazapi_enforced 17, reply_media 35, webhook_media_delivery 6, effective_enforcement 40, metering 57, schema_dominance 53, rollout_fence 109, orphan 60, charge_workers 14, courtesy_rule 27, 0 pulados; 17 de 17 mutantes.
- Despachados juntos: revisão independente do G4 (PostgreSQL 55477/55478), reforço do G5b (MEDIUM-1, MEDIUM-2, decisão 19 do owner e capturas refeitas) e a validação do orquestrador do G4 (55479).
- **Validação do orquestrador** (2026-11-13 01:36:12Z → 02:09:15Z, PostgreSQL próprio em 55479, diff `fed6453f…` e arquivos novos `5c5d7f38…`/`ddb29d82…`/`9a73909b…` iguais no início e no fim, `src` no worktree): PG "evidence valid", 0 pulados: evolution_enforced 15, uazapi_enforced 17, reply_media 35, webhook_media_delivery 6, effective_enforcement 40, metering 57, schema_dominance 53 (verde na primeira), rollout_fence 109, orphan_reconciliation 60, charge_workers 14, courtesy_rule 27; black/isort/flake8 limpos; mypy sem problemas em 217 arquivos. **Unitários/rota: 1625 passed, 1 failed** (determinístico, causado pelo G4): `tests/api/test_webhook_media_delivery.py::test_the_credit_reply_channels_map_to_the_same_providers` fixa o mapa de canais da resposta cobrada como `{zapi, meta, uazapi}` e o G4 acrescentou `evolution`. Esse arquivo não estava na lista de validação do brief do G4 (estava na do G3). Vai para o laço de correção do G4.

### Reinício do WSL com perda do /tmp (2026-11-13, boot 08:40Z)

- O WSL reiniciou e o `/tmp` foi apagado: perdidos o overlay de venv, o shim do `poetry`, todos os patches salvos, os logs de validação/revisão e as capturas de tela do G5b; o revisor do G4 morreu antes do veredito (só relatos parciais, todos verdes). Nenhum agente nem cluster ficou rodando.
- **Worktrees intactos** (ficam em `/home/lorenzo/work`), fingerprints conferidos contra este log: g1 `5a963397…`, g2 `ff9bc327…`, g3 `3bed799d…`, g4 `fed6453f…` (não rastreados `5c5d7f38…`/`ddb29d82…`/`9a73909b…`), g5a `6fd65678…`, g5b completo `dd58da02…`/frontend `4e87a94e…`/backend `6fd65678…`, g6 `5a963397…` + `e0122b83…`/`15293b96…`/`95ed14a3…`. Os patches regenerados dos worktrees têm os mesmos sha256 de antes (g1 `3ecfbaa2…`, g2 `2a81c82c…`, g3 `1566ee48…`, g4 `17cc7a19…`, g5a `83d5b059…`).
- **Ambiente durável reconstruído** em `/home/lorenzo/work/cortesia-env/`: `venv` (overlay sobre o venv do poetry `dYUOnVPp`, com `bin/alembic` e `bin/pytest`), `shim/poetry` (`poetry run X` → `X`), `patches/` (g1, g2, g3, g5a, g6 revisados; g4 e g5b ainda não fechados), `logs/`. Uso: `export VIRTUAL_ENV=$ENV/venv PATH=$ENV/shim:$ENV/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1`. Daqui em diante nada durável vai para `/tmp`.
- Retomada: revisor novo e independente para o G4 (PostgreSQL 55477/55478), já avisado da falha conhecida (`test_the_credit_reply_channels_map_to_the_same_providers`) e com pedido de varredura de outros pinos velhos; revisor novo para a re-checagem do reforço do G5b.
- **Validação do orquestrador do G5b pós-reforço** (2026-11-13 08:45:11Z → 08:46:16Z, no worktree, fingerprints completo `dd58da02…`/frontend `4e87a94e…`/backend `6fd65678…` iguais no início e no fim): vitest focado 5 arquivos, 421 passed; `CI=true pnpm run typecheck` exit 0; build exit 0; pacote inteiro 45 arquivos, 1132 passed; 0 links quebrados. As capturas de tela se perderam com o `/tmp`; refazer na consolidação, na árvore única.

### Group 5b: re-checagem do reforço (2026-11-13 08:49Z → 08:53Z): SHIP, e conclusão

- Revisor novo e independente; fingerprints (backend `6fd65678…`, frontend `4e87a94e…`, completo `dd58da02…`) iguais no início e no fim; 0 links quebrados; nada rodando. O fixer mudou só `credits-and-topups.tsx` e `billing.test.tsx` (os outros seis arquivos contêm só o escopo original; contagem de testes 413 → 421 e 1124 → 1132 = exatamente os 8 testes novos).
- **MEDIUM-1 PASS** (`billing.test.tsx:1024` com `reached: [50, 90, 100]`, barra de 100 % e alertas; mutante da guarda morto). **MEDIUM-2 PASS** (`expiredCourtesyHistory` confere com `lot_view`/`_lot_status` e com `sweep_expired`; "Vencido" é comportamento que já existia em `lotStatus`). **Decisão 19 PASS** (linha em `credits-and-topups.tsx:952-963`, textos exatos; enumeração de `topup.status` por `deriveTopupState`: em todo estado alcançável o cobrado em quarentena vê exatamente uma menção à verificação, a linha nova ou a de recarga bloqueada, nunca nenhuma e nunca as duas; a linha não pisca durante o carregamento).
- Vitest focado 421/421, typecheck exit 0 (build e pacote inteiro pela validação do orquestrador na mesma árvore). 6 de 6 mutantes mortos. Procedência dos três pinos HTML reconfirmada com as fontes de `HEAD`.
- Gaps (LOW, não bloqueiam; ficam para a consolidação): **LOW-1** o teste "blocked after spending all its courtesy" (`billing.test.tsx:2229`) usa um lote ativo com 4.000 disponíveis ao lado de 0 gastável; trocar por `exhaustedCourtesyHistory` (`status: "exhausted"`, `available_mc: 0`, `consumed_mc: 10_000_000`), `expiries: []`, e assertar "Esgotado" e a ausência de `credit-expiries`. **LOW-2/NIT** o mesmo pareamento no pino do bloqueado em quarentena pela regra (se mudar o fixture, regravar com as fontes de `HEAD`).
- `genie task done t_mwdhihlfab66fb6f`. Patches duráveis: `/home/lorenzo/work/cortesia-env/patches/g5b-reviewed.patch` (árvore inteira) e `g5b-frontend-reviewed.patch` (só o frontend, para a consolidação).

### Group 4: revisão de execução (2026-11-13): FIX-FIRST (só um pino de teste)

- Revisor novo e independente (o anterior morreu no reboot), PostgreSQL próprio em 55477, fingerprints `fed6453f…`/`5c5d7f38…`/`ddb29d82…`/`9a73909b…` iguais no início e no fim, nada editado. Unitários/rota (14 arquivos) 1494 passed, 1 failed (o pino conhecido); PG 11 arquivos "evidence valid", 0 pulados (evolution_enforced 15, uazapi_enforced 17, reply_media 35, webhook_media_delivery 6, effective_enforcement 40, metering 57, schema_dominance 53, rollout_fence 109, orphan 60, charge_workers 14, courtesy_rule 27), mais `test_conversation_language_postgres` 7/7; plugin de switch forçado nas 9 suítes de webhook: `evolution main=36, enforced=10, raised=1`, `uazapi main=170, enforced=19`, nenhum veredito cobrado em suíte não listada; testes novos sob guarda de rede só loopback (0 tentativas externas); lint/mypy limpos; log do servidor só com provas negativas. Mutantes do revisor: 11 de 12 mortos (o sobrevivente é equivalente).
- Critérios Evolution, Reentrega (cruzando mudança de modo), observacional sob marca/global com uma leitura, Mídia de saída, Origem real, Bloqueado e os carry-overs do G3 (LOW-1 plugin, NIT-1, NIT-2, NIT-3): PASS. **Oráculo fiel, com transplante próprio:** a rota `/evolution` literal do `fe800aec` ao lado da do G4 na mesma app e banco, 5 modos × 8 formas de turno = 40 casos: respostas, envios, sessão, eventos, claims, observações e linhas cobradas idênticos; instruções = as do `fe800aec` mais um bloco contíguo de +1/+2/+3/+4/+5 leituras, nunca uma segunda. No `fe800aec` a Evolution só trata texto (`conversation` e `extendedTextMessage.text`); nada fica sem cobrar nem sem bloquear; sem recibo de leitura. Varredura de pinos velhos: só um.
- **HIGH-1 (único bloqueio):** `tests/api/test_webhook_media_delivery.py::test_the_credit_reply_channels_map_to_the_same_providers` fixa o mapa sem `evolution`. **LOW-1 (entre adaptadores, não é do G4):** para um builder bloqueado pela regra, a despausa preguiçosa de uma pausa recém-vencida é desfeita pelo rollback do caminho bloqueado (a sessão fica `is_paused=True` com `paused_until` no passado; o paciente não é afetado). **Decisão do orquestrador:** não aplicar o `commit` sugerido: um turno bloqueado não grava nada (DESIGN item 5, "sem sessão"; testes do G2 "with nothing written"); a despausa acontece no primeiro turno depois do desbloqueio. Fica como resíduo aceito, a citar no handoff. **LOW-2:** tenant cobrado com instância configurada e URL ou chave vazia é debitado a cada turno sem entrega até bloquear (coerente com a regra de resposta perdida da Z-API/Meta/Uazapi, mas persistente): nota no runbook (G7); a ideia de a lista pré-deploy apontar clínicas com credenciais incompletas vai ao owner como oferta. **NIT-1** docstring de `test_credit_metering.py:5376` com 97 colunas; **NIT-2** corpo `no_llm_response` da Evolution sem pino próprio.
- **Ambiente depois do reboot (achado pelo revisor, corrigido pelo orquestrador em `/home/lorenzo/work/cortesia-env/`):** (1) o snapshot de `neurostack_core` do venv do poetry (2026-11-08) não tem `MEDIA_BINDABLE_INTENT_IDS`; o overlay agora põe na frente um export de `packages/neurostack-core` do `fe800aec` (`core/`; nenhum worktree muda `packages/`). (2) Nesta inicialização do WSL (rede espelhada, Docker Desktop parado) um connect a uma porta fechada de `127.0.0.1` é descartado em vez de recusado (`::1` recusa), e os replays de dominância apontam o Redis para uma porta fechada de propósito esperando a recusa; sem `sudo`, o overlay carrega `cortesia_fastrefuse` (via `aa-core.pth`), que devolve `ConnectionRefusedError` na hora para portas de loopback sem ouvinte no Linux e não toca em portas abertas nem em endereços externos. Uso: `. /home/lorenzo/work/cortesia-env/env.sh`. Script de validação do orquestrador: `orch_py.sh`.
- Laço de correção 1 de 2 (só testes) despachado a um `genie:fixer`: HIGH-1, NIT-1, NIT-2. Estado antes do laço salvo em `patches/g4-review1-fixfirst.patch`.

### Decisão 20 do owner (2026-11-13: "inclui na consolidação")

- A lista pré-deploy (`python -m src.ops.credit_courtesy_rule status`) passa a **apontar, por clínica e por canal, credenciais incompletas** (canal com chave de roteamento configurada mas sem o resto necessário para enviar: URL, token ou chave), para o owner ver antes do go/no-go quais clínicas gastariam cortesia sem entregar resposta (LOW-2 da revisão do G4). Só booleanos: nenhuma credencial, URL ou telefone impresso; continua uma transação `REPEATABLE READ READ ONLY` revertida. Entra na consolidação (arquivos do G6: `src/ops/credit_courtesy_rule.py` e os dois testes), com revisão e teste próprios; o runbook do G7 cita o campo.

### Lista do que entra na consolidação (além de juntar as árvores)

1. Decisão 20: credenciais incompletas na lista pré-deploy.
2. G6 LOW-A: teste de `paid_plan.paid_lot_expires_at` com recarga já vencida para o builder bloqueado.
3. G5a (re-checagem): apagar `_aware_utc` morto em `src/api/routes/admin.py`; prova PG da data de São Paulo do ciclo corrente (caminho de requisição e leitor em lote); ancorar `paid_plans_in_force_for` em `CreditAccount.builder_id`.
4. G5b LOW-1: fixture `exhaustedCourtesyHistory` no teste "blocked after spending all its courtesy".
5. Capturas de tela do `/billing` e do master-admin refeitas na árvore única (as anteriores se perderam com o `/tmp`).
6. Resíduos aceitos a citar no PR e no runbook, sem mudança de código: a despausa preguiçosa de builder bloqueado (G4 LOW-1); o D1 da regra antes da captura de texto de sessão pausada (G2 LOW-C/G3/G4); egress fechando durante o LLM cobra a resposta não entregue (G3 LOW-2); dedupe só por Redis na mudança cobrado → grátis (G3 LOW-3); a leitura própria da entrega de mídia (G3 LOW-1); a contagem de `/v1/events` é um piso e o tamanho da saída da lista (G6 LOW-6/LOW-7).

### Group 4: re-checagem do laço 1: SHIP, validação e conclusão (2026-11-13)

- Laço 1 (fixer, só testes): `git diff | sha256sum` = `26a0a01144a3b480df899524ed6c6549e57d4272714925091e06104e93d5793b`; `git diff -- src | sha256sum` = `a6094004…` (igual ao revisado); não rastreados inalterados. Comparado arquivo por arquivo com o estado revisado (`patches/g4-review1-fixfirst.patch`): só `tests/api/test_webhook_media_delivery.py` (HIGH-1), `tests/services/test_credit_metering.py` (NIT-1) e `tests/api/test_credit_suspension_webhooks.py` (NIT-2: `test_a_charged_evolution_turn_without_a_reply_is_answered_and_decided_once`).
- Re-checagem por revisor novo e independente (aberto com o "pode" do owner; o anterior tinha sido encerrado): **SHIP**, sem gaps. Delta de três arquivos reproduzido; 404 testes nos quatro arquivos pedidos; lint/mypy limpos; 6 de 6 mutantes mortos (em cópia isolada, porque a validação do orquestrador rodava no worktree). Confirmado no código que "turno sem resposta → `trace_no_response` com custo zero" é o comportamento do caminho cobrado compartilhado já no `fe800aec` (`_no_response_effect`, `deliverable_response`), coerente com o item 3 do DESIGN.
- **Validação do orquestrador** (2026-11-13 09:39:42Z → 10:08:09Z, PostgreSQL próprio em 55479, ambiente durável, diff `26a0a011…` e não rastreados iguais no início e no fim): unitários/rota (18 arquivos) 1627 passed, 0 failed; PG "evidence valid", 0 pulados: evolution_enforced 15, uazapi_enforced 17, reply_media 35, webhook_media_delivery 6, effective_enforcement 40, metering 57, schema_dominance 53 (verde na primeira), rollout_fence 109, orphan_reconciliation 60, charge_workers 14, courtesy_rule 27; black/isort/flake8 limpos; mypy sem problemas em 217 arquivos; log do servidor sem erro de sintaxe ou objeto inexistente.
- `genie task done t_mwczay9h67b95d06`. Patch durável: `patches/g4-reviewed.patch`.

### Consolidação: árvore única montada (2026-11-13)

- `origin/main` continua em `fe800aec`. Worktree `/home/lorenzo/work/cortesia-prc` (branch `wish/debito-cortesia-automatico`, sem commit) = `fe800aec` + árvore revisada do G4 + delta do G5a contra o G2 (`patches/g5a-delta-vs-g2.patch`, sha256 `c9e2c08a…`, o mesmo de antes) + frontend revisado do G5b + os três arquivos do G6. **Procedência conferida arquivo por arquivo: 63 arquivos, 0 divergências** contra os worktrees revisados (os três arquivos em que o delta do G5a se sobrepõe ao G4 — `repositories/credit.py`, `credit_rollout.py`, `test_credit_rollout.py` — ficam na versão do G5a; o G3/G4 não os tinham tocado). `git diff | sha256sum` começa `602bc699992a181a`; 7 não rastreados. Checagem de escopo: `git diff --name-only fe800aec` e `git status --porcelain` vazios para `alembic/versions` e os seis arquivos de invariante do ASAAS. Cópia: `patches/prc-consolidated-v0.patch` (sha256 `628af52a…`).
- Despachados em paralelo no worktree consolidado, com arquivos disjuntos e a regra de congelamento do git: (a) itens de backend da consolidação (`engineer-standard`, PostgreSQL 55481): decisão 20, G6 LOW-A, G5a LOW-1/LOW-2/NIT; (b) frontend (`engineer-standard`): fixture do G5b LOW-1 e capturas de tela refeitas em `/home/lorenzo/work/cortesia-env/shots/`; (c) **Group 7** runbook e docs (`engineer-trivial`, tarefa `t_mwczaynz160260a9`). Depois: revisão de cada um, gate PostgreSQL agregado (29 arquivos), varredura de unitários, vitest/typecheck/build e `final-gate`.

### Consolidação: frontend (fixture + capturas), 2026-11-13

- Engenheiro do item de frontend terminou; conferido pelo orquestrador: `git diff -- frontend | sha256sum` = `354f14d3de04ef17e3bf655b5273d23113a2aaa04fa9d4d9f48ca2da44279f3f`; só `billing.test.tsx` difere do frontend revisado do G5b (fixture `exhaustedCourtesyHistory`, `expiries: []`, asserções "Esgotado" e ausência de `credit-expiries`; os três pinos HTML intocados); `node_modules` instalado offline no worktree, 0 links quebrados; nenhum servidor rodando. Validação do engenheiro: vitest focado 421, typecheck e build ok, pacote 1132.
- 14 capturas reais (Playwright 1.49 + Chromium, build servido por `vite preview`, `/v1` simulado com formatos que o servidor produz), desktop 1440 e celular 390, em `/home/lorenzo/work/cortesia-env/shots/` (durável), com `shots.cjs` e `README.md`: `billing-01-charged`, `-02-blocked-zero-capacity`, `-03-protected`, `-04-charged-quarantine-topups-on`, `-05-blocked-courtesy-spent`, `-06-charged-quarantine-topups-off`, `master-admin-rows`. O orquestrador olhou `master-admin-rows-desktop` ("Cortesia: cobrando", "Cortesia: bloqueado", linha sem selo, "Cobrança ativa") e `billing-05` (suspenso, barra 100 %, "Esgotado"). Sem transbordo horizontal nos painéis deste wish; a página do master-admin em 390 px transborda 224 px pela barra de busca/"Novo Builder" (anterior ao wish, fora do diff).

### Group 7: entrega do engenheiro, 2026-11-13

- Conferido pelo orquestrador (sha256): `docs/runbooks/credit-courtesy-rule.md` novo `8175e53e…` (291 linhas: The rule; The states — What is debited, Block, Channels, What the builder sees; The pre-deploy list (L1) — The binding lag; Deploy (L2a, L1, L2b); Rollback; Risks the owner accepted; Accepted residuals), `docs/billing/credits.md` `ec87ce23…`, `docs/runbooks/credit-courtesy.md` `a60fcd78…`, `credit-charge-mark.md` `6af01f92…`, `credit-enforcement-arming.md` `d05f30f7…`, `uazapi-webhook-auth.md` `e764e5f7…`, `.genie/wishes/uazapi-webhook-auth/WISH.md` `7e7db0d9…`; nada fora do escopo. Validação do engenheiro: bloco do G7 exit 0 (1571 passed e os quatro greps).
- O engenheiro listou o que NÃO verificou no código (efeitos do bloqueio nos uploads/busca/download, painel e exportação abertos, sem recibo/transcrição/download, resíduos (b), (c), (e), o publicador e o envio da imagem antiga, a limpeza de retenção, tamanhos de saída e limites do CloudWatch, passos de infra): a revisão independente verifica cada um. Revisão do G7 despachada.

### Group 7: revisão de execução (2026-11-13, fim 10:40Z): FIX-FIRST

- Revisor independente; os sete hashes das docs iguais no início e no fim; nada editado. Bloco do G7 exit 0 (1571 passed + greps); `tests/ops/test_credit_enforcement.py` (o único teste que fixa texto de uma doc editada) 76 passed; os 48 nomes de teste citados existem e cada afirmação foi conferida contra o corpo do teste; sem links quebrados; sem segredo, token, ARN, e-mail, telefone ou UUID no texto novo. Ordem do deploy (L2a → L1 → go/no-go → L2b → L3), no-go, pipeline e o "vai" do owner em cada passo: corretos; nenhuma doc manda um agente rodar passo ao vivo.
- **HIGH-1:** "Rollback", terceiro item: "a limpeza de retenção as apaga" é **falso**. No `fe800aec` `sweep_expired_replies` → `sweep_outbox_payloads` só esvazia o `payload` (`'{}'`) das linhas mais velhas que `credit_replay_retention_hours` (24 h); as linhas ficam `pending` (uma linha `sending` deixada por uma task que parou guarda o payload). Nada apaga linhas do outbox além da cascata do tenant. **Isto também corrige o Risco 12 do DESIGN** (que diz que a limpeza "as apaga"; o DESIGN não pode ser editado, a correção vale aqui).
- **HIGH-2:** "Block": "the patient receives nothing" contradiz a decisão 18 do owner (o texto fixo "Não consegui enviar o arquivo agora. A equipe da clínica pode te enviar por aqui." sai sem custo quando o builder bloqueia entre a resposta e o arquivo; o teste afirma `ReplyMediaResult("credit_refused", True)`), e o item dos webhooks omite a decisão 16 (sem recibo de leitura na Z-API), as respostas 202 `session_paused`/`bot_paused` e a captura só de texto da sessão pausada.
- **MEDIUM:** (1) o runbook omite os efeitos em RAG/mídia e na rota de estado (estão só em `credits.md`, parciais); (2) a receita do L1 aponta para "Running a step" do `credit-charge-mark.md`, que usa a task definition **corrente** do serviço e a guarda de outro módulo (tem de ser a revisão recém-registrada e `src.ops.credit_courtesy_rule`), e o L2b não lista "rotas de crédito 401 sem token"; (3) resíduo (b) sem o custo (texto já reivindicado no Redis → a reentrega responde `duplicate_message` e a mensagem se perde, também para a equipe; áudio é reentregue); (4) "binding lag — what to do" com o efeito errado (o atraso só decide o estado de quem tem a cortesia esgotada: `protected` em vez de `blocked`) e o runbook errado (`asaas-billing-reconciliation.md`); (5) referências faltando ou erradas (resíduos (c) e (e), recarga bloqueada em quarentena → `billing-view.test.ts`, rollback); (6) frases antigas das docs editadas que a mudança torna falsas (`credits.md` "Courtesy never changes the enforcement instant", FEFO; `uazapi-webhook-auth.md` linhas 84, 658-661, 1113, 1130-1132, 1148-1150; `credit-enforcement-arming.md` linhas 37-38).
- **LOW (10):** limite do CloudWatch (hoje 1 MB por evento, não 256 KB; o shim do Fargate corta linhas acima de 16 KiB; acima de uns seis builders a linha é dividida) e a fonte da estimativa de tamanho (medido pelo revisor: 2.456 B para um builder com uma clínica, +262 B por clínica); valores de `state` (`not_under_rule`, `rule_unread`) e `reservation` nulo/erro; caminho exato das chaves da decisão 20 (`clinics.tenants[].credentials_incomplete`, `clinics.credentials_incomplete = {count, tenant_ids, by_channel}`, totais em `summary.under_rule.*`/`summary.all_states.*`); redação do resíduo (a); nota de rollback (o apply pode deixar as task definitions antigas INACTIVE: o PR de infra seria o único caminho de volta; conferir no plano); protegido mostra `origin: "none"` no `status`; `enforce` recusa todo resultado diferente de `match`; afirmações sem fonte ou datadas; origem `unknown` nos canais; pré-condições do L3.
- Chaves da decisão 20 conferidas no fim da revisão contra `src/ops/credit_courtesy_rule.py` (ainda em edição pelo engenheiro de backend; só reformatação entre as duas leituras): nomes e semântica batem.
- Laço de correção 1 de 2 (só docs) a despachar a um `genie:fixer` com os textos corrigidos do revisor, depois que o engenheiro de backend da consolidação terminar (pedido do owner: esperar os agentes).

### Consolidação: itens de backend, entrega do engenheiro (2026-11-13)

- Conferido pelo orquestrador (sha256): `src/ops/credit_courtesy_rule.py` `7eafa3ca…`, `tests/ops/test_credit_courtesy_rule.py` `10c3b920…`, `tests/integration/test_credit_courtesy_rule_ops_postgres.py` `df823542…`, `src/api/routes/admin.py` `f85cab19…`, `src/repositories/credit.py` `8cb2ca43…`, `tests/integration/test_credit_courtesy_surfaces_postgres.py` `3047d827…`, `tests/integration/test_credit_courtesy_rule_postgres.py` `3d1b709e…`, `tests/api/test_admin.py` `d948e3ed…`. Comparado com a árvore v0: desde a montagem mudaram exatamente 16 arquivos (estes 8, as 7 docs do G7 e `billing.test.tsx`). Checagem de escopo (migrations e invariantes ASAAS) vazia; cluster 55481 parado. Cópia: `patches/prc-consolidated-v1-unreviewed.patch`.
- Relato do engenheiro (a confirmar na revisão): **decisão 20** — a regra espelha `CreditReplySender.prepare` (`webhook.py` ~:619-663): zapi `zapi_token` + `zapi_instance_id`; uazapi `uazapi_api_url` + `uazapi_token`; evolution `evolution_api_url` + `evolution_instance_name` + `evolution_api_key` (sem strip); meta `meta_phone_number_id` + `meta_permanent_token`; calculado dentro do PostgreSQL como booleanos na consulta de tenants já existente (sem instrução nova; `TRIM(valor, :whitespace)` com os 29 caracteres de `str.isspace()`); saída: `clinics.tenants[].credentials_incomplete`, `clinics.credentials_incomplete = {count, tenant_ids, by_channel}`, `summary.under_rule`/`all_states` com `clinics_with_incomplete_credentials` e `incomplete_credentials_per_channel`; teste de pino contra o enviador real em 114 casos (unitário e PostgreSQL). G6 LOW-A feito; `_aware_utc` apagado; prova da data de São Paulo nos dois caminhos; lote ancorado em `CreditAccount.builder_id` com o SQL do caminho de requisição byte a byte igual (hashes `cbfda511…`/`5618ee9c…` antes e depois). Validação do engenheiro: 433 não-PG; PG ops 10, surfaces 8, rule 29, dominance 53, effective enforcement 40, charge badge 2, binding grace 26, 0 pulados; lint/mypy limpos (218 arquivos); 20 de 20 mutantes.
- Despachados juntos: (a) `genie:fixer` das docs do G7 com os textos corrigidos em `/home/lorenzo/work/cortesia-env/reviews/g7-review1-gaps.md`; (b) revisão independente dos itens de backend e do fixture do frontend (PostgreSQL 55483/55484); (c) gate PostgreSQL agregado do orquestrador na árvore consolidada (55482): os 25 arquivos da lista, os 4 novos do wish, mais `test_credit_courtesy_surfaces_postgres` e `test_conversation_language_postgres` (31 arquivos), black/isort/flake8/mypy. A varredura de unitários e o vitest/typecheck/build rodam depois que as docs fecharem.

### Group 7: laço de correção 1 e re-checagem (2026-11-13)

- Fixer (≠ engenheiro, ≠ revisor) aplicou os 18 gaps; hashes conferidos pelo orquestrador: `credit-courtesy-rule.md` `6a8e220d…` (418 linhas), `credits.md` `50fbd935…`, `credit-charge-mark.md` `0145b7f9…`, `credit-enforcement-arming.md` `e1223e01…`, `uazapi-webhook-auth.md` `5c7049b7…`; `credit-courtesy.md` e o wish `uazapi-webhook-auth` inalterados. Validação do fixer: 1647 passed + greps. Desvios: não escreveu o histórico do limite do CloudWatch (não verificável no repositório; manda conferir a cota atual de PutLogEvents); tamanhos remedidos (≈ 2,3 KB por builder com uma clínica, ≈ 0,3 KB por clínica a mais, ≈ 3,7 KB com um builder, ≈ 12,9 KB com cinco, 16 KiB no sétimo); o wish citado como caminho, não link (`.genie/*` é ignorado pelo git; só existe no checkout principal).
- **Re-checagem (mesmo revisor): os 18 gaps fechados**; chaves de completude por canal iguais a `SEND_CREDENTIALS`; caminhos das chaves da decisão 20 iguais ao código final; sem segredo, link quebrado ou "256 KB"; 1647 passed. Dois MEDIUM novos, cada um de uma frase: **MEDIUM-A** resíduo (b) dizia "A patient audio is not yet claimed and is retried", verdade só no Uazapi (Z-API e Evolution reivindicam no Redis antes da captura para toda mensagem; a frase vinha do texto sugerido pelo próprio revisor); **MEDIUM-B** o item da decisão 20 mandava "fix the clinic's credentials or tell the owner", uma escrita em produção sem o "vai" do owner (a frase vinha do brief do orquestrador) → "report every non-empty list to the owner; a clinic's credentials are changed only by the clinic or on the owner's own go-ahead". LOW-1..5: "non-empty" em vez de "presence"; antecedente de "It must land before L10"; "Outside the courtesy rule" no consumo observado; "(production has no mark event recorded)" no rollback; linhas longas.
- Laço de correção 2 de 2 despachado ao mesmo fixer com os sete textos.

### Consolidação: gate PostgreSQL agregado, rodada 1 (2026-11-13 10:56:03Z → 11:21:38Z): 30 de 31 verdes, 1 falha real

- PostgreSQL próprio em 55482, ambiente durável, árvore consolidada (arquivos de código iguais no início e no fim; só as docs mudaram durante a rodada, pelo fixer do G7). "evidence valid", 0 pulados, 889 testes: charge_mark 35, charge_workers 14, charge_badge 2, effective_enforcement 40, enforcement_marks_migration 44, enforcement_ops 52, rollout_fence 109, schema_dominance 53 (verde na primeira), metering 57, ledger 56, binding_grace 26, retention 18, orphan_reconciliation 60, maintenance 11, courtesy 13, courtesy_http 9, fact_replay 37, migration_guards 68, orm_ddl_drift 4, legal_hold_barriers 9, capability_ready 26, object_copy_gc 19, reply_media 35, webhook_media_delivery 6, courtesy_rule 29, uazapi_enforced 17, evolution_enforced 15, courtesy_rule_ops 10, courtesy_surfaces 8, conversation_language 7. black/isort/flake8 limpos; mypy sem problemas em 218 arquivos. Log do servidor: os erros de "does not exist" são sondas intencionais (`no_such_table`, `no_such_marks_table`, tenant inexistente).
- **Falha (determinística, causada por este wish, não vista por nenhum grupo):** `test_credit_courtesy_migration_postgres.py::test_the_bridge_maps_the_046_schema_and_nothing_writes_or_reads_it` (1 failed, 53 passed). A guarda do `creditos-cortesia` (Decisões 4 e 17) varre `src/services` e `src/api` atrás de `'courtesy'`/`"courtesy"`/`courtesy_removal`/`credit_courtesy_events` e só admite os arquivos de `_COURTESY_FILES`; este wish fez `src/services/credit_inventory.py`, `credit_metering.py` e `reply_media.py` nomearem `"courtesy"` como ORIGEM do modo (não como tipo de lote). O arquivo estava na lista do gate agregado, mas em nenhuma lista de grupo: é exatamente o tipo de falha entre grupos que o gate agregado existe para pegar.
- **Correção despachada ao engenheiro da consolidação (só o arquivo de teste):** um segundo conjunto nomeado (`_COURTESY_RULE_FILES`) com os três arquivos e o motivo de cada um, mantendo neles a proibição de nomear o esquema de cortesia (`credit_courtesy_events|CreditCourtesyEvent|courtesy_removal`); mutantes; e uma varredura de outras guardas do mesmo tipo em `tests/integration` fora da lista agregada.

### Group 7: laço de correção 2 (2026-11-13)

- Fixer aplicou MEDIUM-A, MEDIUM-B e LOW-1..5; hashes conferidos: `credit-courtesy-rule.md` `c6d0b2eb…`, `credit-enforcement-arming.md` `c9a3dac3…`, `uazapi-webhook-auth.md` `40e67266…`; os demais inalterados. Validação do fixer: 1647 passed + greps. Re-checagem final pedida ao mesmo revisor.

### Consolidação: revisão dos itens de backend e do fixture do frontend (fim 11:24:33Z): SHIP

- Revisor independente, PostgreSQL próprio em 55483 (mutantes em 55484), os nove hashes iguais no início e no fim, nada editado; delta derivado aplicando `prc-consolidated-v0.patch` num rascunho e comparando a árvore inteira (só os nove arquivos e as docs diferem do v0; o revisor também notou a edição concorrente da guarda `test_credit_courtesy_migration_postgres.py`, fora do escopo dele: precisa de revisão própria). Bloco de validação exit 0: 433 unitários/rota; PG "evidence valid", 0 pulados: ops 10, surfaces 8, rule 29, dominance 53 (verde na primeira), effective enforcement 40, charge badge 2, binding grace 26; lint/mypy limpos (218 arquivos). Frontend: `billing.test.tsx` 187/187, typecheck ok.
- **Decisão 20 PASS:** o veredito da lista é o do enviador real nos quatro canais (`prepare` em `webhook.py:619-663`, envios em `:676-760`; `zapi_client_token` é opcional; o nome da instância do Uazapi só serve para o roteamento de entrada); `TRIM(valor, PYTHON_WHITESPACE)` comparado com `str.strip()` em todo code point no PostgreSQL 16 e no SQLite: 0 divergências; o caminho de erro só imprime o nome da classe da exceção; a transação é a mesma (91 instruções nas duas árvores, só a consulta de tenants muda, um `SET TRANSACTION … READ ONLY`, 0 commits); cerca AST verde. G6 LOW-A, `_aware_utc`, data de São Paulo, lote ancorado em contas e fixture do frontend: PASS. **SQL do caminho de requisição byte a byte igual, reproduzido de forma independente** (13 estados × 5 instantes, 195 chamadas, 427 instruções com parâmetros, mesmo sha `70d18c3b…` nas duas árvores); o bug do v0 era real (o lote dava `KeyError` para builder com lotes e sem linha em `builders`). Mutantes: 20 de 21 mortos.
- Gaps (não bloqueiam): **LOW-1** um valor cru de credencial pode ser selecionado no resultado da consulta de tenants sem nenhum teste falhar (o que imprime é pego): acrescentar `test_the_tenant_read_returns_only_the_declared_columns` (texto dado pelo revisor; mata o mutante). **LOW-2** "completo" quer dizer que o `prepare` vai tentar enviar, não que a resposta chega (Z-API com token de segurança ligado e sem `zapi_client_token`, token errado ou vencido, egress do Uazapi fechado; tenant apontado só porque `whatsapp_provider` nomeia o canal não tem rota de entrada e nunca é cobrado): uma frase no runbook. **LOW-3 (opcional, oferta ao owner):** listar por canal os NOMES das chaves que faltam (nomes são esquema, não segredo). **NIT** `paid_at` do fixture depois de `AS_OF` (copiado do fixture existente).

### Group 7: re-checagem do laço 2 (2026-11-13 11:23Z): SHIP

- Mesmo revisor: os sete itens (MEDIUM-A, MEDIUM-B, LOW-1..5) fechados e conferidos contra o código (claims no Redis antes da captura na Z-API `webhook.py:3344` e na Evolution `:4212`; no Uazapi áudio e imagem só são reivindicados em `_claim_deferred_message` `:5349`; `_tick` do `fe800aec` só roda com global ou evento de marca); o delta só toca as três docs editadas, um trecho por item; 1647 passed + greps; nenhum segredo. NIT: duas linhas longas em `credit-courtesy-rule.md` (250 e 353).
- Falta para fechar o G7: a frase do LOW-2 da revisão da consolidação ("completo" = o `prepare` vai tentar enviar, não garante entrega) e, se o owner aceitar o LOW-3, a menção ao campo novo; depois a validação do orquestrador nas docs finais e `genie task done t_mwczaynz160260a9`.

### Consolidação: correção da guarda do `creditos-cortesia` (2026-11-13)

- Engenheiro da consolidação mudou só `tests/integration/test_credit_courtesy_migration_postgres.py` (sha256 `3903510bd74ad31c89a864d948feea54ccb2f3498127dbda40a2c51513934bff`, +44/−4, conferido): `_COURTESY_FILES` inalterado; novo `_COURTESY_RULE_FILES` (`credit_inventory.py`, `credit_metering.py`, `reply_media.py`, com o motivo de cada um); para esses três continuam proibidos o esquema de cortesia (`credit_courtesy_events|CreditCourtesyEvent|courtesy_removal`) e `lot_kind` comparado com "courtesy"; os dois conjuntos não se sobrepõem e todo arquivo listado tem de existir. Relato do engenheiro: gate do arquivo 54/54 "evidence valid", 0 pulados; lint limpo; 7 de 7 mutantes mortos; varredura de outras guardas em `tests/integration`: só esta e a cerca do L10 (109/109 verde). A revisão desta mudança entra no `final-gate` (alarga uma guarda: precisa de olhar independente).
- Em andamento: LOW-1 da revisão da consolidação (teste `test_the_tenant_read_returns_only_the_declared_columns`) com o engenheiro; frase do LOW-2 no runbook com o fixer das docs; varredura de unitários do orquestrador refeita em 4 partes paralelas (a execução única estava em 11 % depois de 25 min e foi interrompida): `tests/api` em duas metades e `tests/services` já rodando; `tests/ops`, `docs`, `repositories`, `models`, `core`, `security` e `scripts` depois que o teste do LOW-1 entrar.

### Group 7: conclusão; árvore congelada (2026-11-13 11:37Z)

- Última adição às docs (LOW-2 da revisão da consolidação): parágrafo sobre o que "completo" quer e não quer dizer, com as referências abertas pelo fixer (`CreditReplySender.prepare`, `zapi_service.py` `Client-Token`, `inbound_routes`, `TenantChannels.channels`); `credit-courtesy-rule.md` final `2e636dc021e1…`. LOW-1 da mesma revisão feito pelo engenheiro: `test_the_tenant_read_returns_only_the_declared_columns` (`tests/ops/test_credit_courtesy_rule.py` `a9964a78…`, 50 testes; 2 mutantes mortos). Estas duas adições e a guarda estendida ficam para o `final-gate` revisar.
- **Validação do orquestrador do G7** (11:33:04Z → 11:36:44Z, docs finais iguais no início e no fim): `tests/docs`, `tests/ops/test_credit_enforcement.py` e os três `tests/scripts` do bloco: 1647 passed; os quatro greps ok. `genie task done t_mwczaynz160260a9`. **Todos os oito grupos concluídos.**
- **Árvore consolidada congelada** (`/home/lorenzo/work/cortesia-prc`, nenhum agente escrevendo): `git diff | sha256sum` = `6e70bc53b3e4b2bf8248c5df5b6926b1c49de4fd344e127f65097b7b18671bf6`; 63 arquivos rastreados alterados + 8 novos (`credit-courtesy-rule.md` `2e636dc0…`, `src/ops/credit_courtesy_rule.py` `7eafa3ca…`, `test_credit_courtesy_rule_ops_postgres.py` `df823542…`, `test_credit_courtesy_rule_postgres.py` `3d1b709e…`, `test_credit_courtesy_surfaces_postgres.py` `3047d827…`, `test_credit_evolution_enforced_postgres.py` `9a73909b…`, `test_credit_uazapi_enforced_postgres.py` `ddb29d82…`, `tests/ops/test_credit_courtesy_rule.py` `a9964a78…`); partes: src `b5cef918…`, tests `bb2f09db…`, frontend `354f14d3…`, docs+.genie `093a867d…`. Checagem de escopo: 0 arquivos alterados e 0 novos em `alembic/versions` e nos seis arquivos de invariante do ASAAS. Cópia durável: `patches/prc-consolidated-v2-frozen.patch` (sha256 `1f501cc4…`).
- **Frontend na árvore congelada** (11:37:28Z → 11:39:43Z): vitest focado 421 passed (5 arquivos); typecheck exit 0; build exit 0; pacote inteiro 1132 passed (45 arquivos); 0 links quebrados.
- Em andamento na árvore congelada: gate PostgreSQL agregado (31 arquivos, 55482) e a varredura de unitários em 4 partes. Depois: `final-gate` independente.

### Consolidação: varredura de unitários do orquestrador na árvore congelada (2026-11-13 11:28Z → 11:53Z)

- Quatro execuções paralelas no ambiente durável, todas com `src` `b5cef918…` e `tests` `bb2f09db…` iguais no início e no fim: `tests/api` primeira metade (50 arquivos) 1677 passed, 1 skipped; `tests/api` segunda metade (50 arquivos) 2491 passed, 1 skipped; `tests/services` 3188 passed, 1 skipped; `tests/ops` + `docs` + `repositories` + `models` + `core` + `security` + `scripts` (sem `test_backup_encryption.py` e `test_guard_alembic_upgrade_head.py`, que falham igual na `main` por gpg e volume do dev stack) 5784 passed, 17 skipped, **1 failed**. Total: **13.140 passed, 1 failed**.
- A falha é anterior ao wish: `tests/repositories/test_database.py::test_only_database_application_module_creates_async_engines` acusa `ops/billing_fact_requeue.py`; reproduzida pelo orquestrador num worktree limpo do `fe800aec` (mesma asserção), e o wish não toca nesse arquivo. Pulados: dependem de ambiente (`POSTGRES_SMOKE_DATABASE_URL` não definido na varredura de unitários, artefatos do SPA, ferramentas dos scripts).
- A varredura cobre muito mais que os "unitários afetados" do critério; foi feita inteira porque o gate agregado tinha achado uma guarda entre grupos que nenhuma lista de grupo cobria. `make quality` não rodado (ordem do owner).

### Consolidação: gate PostgreSQL agregado na árvore congelada (2026-11-13 11:37:24Z → 12:01:53Z): 31 de 31 verdes

- PostgreSQL próprio em 55482, ambiente durável, `src` resolvido no worktree consolidado; `git diff | sha256sum` `6e70bc53…` e os 8 arquivos novos iguais no início e no fim. Um caminho por chamada de `scripts/check-credit-integration.sh`, cada um "evidence valid", 0 pulados, **943 testes**: os 25 da lista do critério (charge_mark 35, charge_workers 14, charge_badge 2, effective_enforcement 40, enforcement_marks_migration 44, enforcement_ops 52, rollout_fence 109, schema_dominance 53, metering 57, ledger 56, binding_grace 26, retention 18, orphan_reconciliation 60, maintenance 11, courtesy 13, courtesy_http 9, **courtesy_migration 54** (a guarda corrigida), fact_replay 37, migration_guards 68, orm_ddl_drift 4, legal_hold_barriers 9, capability_ready 26, object_copy_gc 19, reply_media 35, webhook_media_delivery 6), os 4 novos do wish (courtesy_rule 29, uazapi_enforced 17, evolution_enforced 15, courtesy_rule_ops 10) e mais 2 (courtesy_surfaces 8, conversation_language 7). black/isort/flake8 limpos; mypy sem problemas em 218 arquivos. Log do servidor: nenhum erro de sintaxe; os "does not exist" são sondas intencionais das suítes.
- **Critério "Gate agregado" atendido na parte do orquestrador:** gate PostgreSQL consolidado (acima), unitários (13.140 passed; 1 falha anterior ao wish), black/isort/flake8/mypy, vitest/typecheck/build do frontend (421 e 1132 passed). `make quality` não rodado (ordem do owner). Excluídos por precisarem de S3/Qdrant de teste, como previsto: `test_credit_purge_jobs`, `test_credit_storage_s3`, `test_credit_qdrant_rebuild`, `test_credit_object_copy_gc`. Falta o `final-gate` independente (despachado).

### `final-gate` independente (2026-11-13T12:03:29Z → 13:07:04Z): SHIP

- Agente `genie:final-gate` (não escreveu nem revisou nada do wish), somente leitura; PostgreSQL próprio em 55485/55486 (parados e apagados). Fingerprints iguais no início e no fim: `git diff | sha256sum` `6e70bc53…`, os 8 arquivos novos, escopo vazio em `alembic/versions` e nos seis arquivos de invariante do ASAAS, `design-review-evidence.mjs verify` exit 0. **0 CRITICAL, 0 HIGH, 2 MEDIUM (só de teste), 6 LOW.**
- **As três mudanças sem revisão própria: aceitas.** Guarda estendida: legítima e apertada (os 13 literais nos três arquivos são comparação/construção da origem ou comentário; nenhum importa `CreditLot`; 4 mutantes de fonte falham a guarda). `test_the_tenant_read_returns_only_the_declared_columns`: sólido. Parágrafo do runbook sobre "completo": exato.
- **Livro de critérios:** todos os critérios de sucesso MET; Uazapi, Evolution e Dominância MET-WITH-AMENDMENT (as leituras de modo a mais registradas). Emendas e decisões 16–20 registradas e coerentes com o código.
- **Costuras entre grupos:** SQL do caminho de requisição byte a byte igual em G2, G4, G5a e na árvore consolidada (terceiro método independente: 6 casos, 21 instruções, mesmo sha256 `3700ebce…`); consumidores do portão passam `captured` e mapeiam `"suspended"`; a lista está pinada ao enviador final nos dois sentidos; lote = leitor individual; frontend e backend de acordo. 32 arquivos PostgreSQL fora da lista agregada rodados na árvore congelada: 26 passam (901 testes: knowledge, mídia, handoff, origem, contrato de leitura do billing, ASAAS, worker de fatos, clonagem, migrations); 2 recusados pela própria lista de portas (só rodam na porta do dev stack; intocados pelo wish); 4 pulados por S3/Qdrant.
- **Sonda própria de dominância:** o corpus do `fe800aec`, com o executor do `fe800aec`, contra um export do `fe800aec` e contra o `src` consolidado, em clones idênticos do banco, comparando respostas, eventos, todas as linhas das 84 tabelas e os conjuntos de instruções: sem cortesia, marcado, marcado com cortesia (50 e 2 créditos, com dívida, episódio e alertas), global, global com cortesia e config desligada: **0 diferenças de resposta e de linha, 0 escritas a mais**; só as leituras a mais registradas (+26/+28 sem cortesia, +6 a +10 marcado, +2 a +4 global, 0 com a config desligada). Fumaça em todas as entradas: bloqueado → os quatro webhooks `credit_suspended`, `/v1/events` 402, uploads e download 402, exportação 200, nada escrito.
- **Validação própria:** os 31 arquivos do gate agregado, 943 testes, "evidence valid", 0 pulados; lint/mypy limpos (218 arquivos); frontend focado 387 (4 arquivos). 25 mutantes: 22 mortos, 3 sobreviventes (os dois MEDIUM e um equivalente). O shim `cortesia_fastrefuse` só restaura a recusa normal de portas sem ouvinte: não pode tornar verde um teste que falha.
- **MEDIUM-1 (só teste):** nada pina que um builder marcado ou global COM lotes de cortesia mantém os números da marca em `/v1/credits/*` (o deslize `effective.origin == "courtesy"` → `effective.at is not None` em `src/api/routes/credits.py` sobrevive a 9 suítes; o código está certo hoje). Teste validado pronto: `/home/lorenzo/work/cortesia-env/logs/final-gate/proposed_fix_M12_test.py` (`test_a_marked_builder_with_courtesy_keeps_the_marks_figures`, para `test_credit_courtesy_surfaces_postgres.py`). **MEDIUM-2 (só teste):** nada pina que uma reserva capturada por marca/global mantém o portão de cobertura de binding (o mesmo deslize em `_enforced_book`, `src/services/credit_inventory.py`, sobrevive a 13 suítes). Teste validado pronto: `proposed_fix_M21_test.py` (`test_a_mark_or_global_captured_reservation_keeps_the_coverage_gate[mark|global]`, para `test_credit_courtesy_rule_postgres.py`). Ambos só afetam builders marcados/global, que a produção não tem; o gate recomenda incluí-los neste PR.
- **LOW:** (1) `credit_lifecycle.request_gate(captured=…)` sem chamador de produção; (2) o runbook não diz que o simulador do painel continua respondendo para um builder bloqueado (grátis e observacional; pinado em `tests/api/test_events_credit_enforced.py:1587`); (3) o runbook cita o caminho do wish, que não está na árvore (escolha do owner: `git add -f` do wish e do design no PR, ou aceitar); (4) a regex de `lot_kind` da guarda é de uma linha só; (5) as suítes PG do Uazapi/Evolution não semeiam lote pago (as de regra e livro pegam); (6) Evolution: "completo" por texto SQL contra a veracidade do Python só difere para JSON não-string.
- Simplicidade e segurança: nenhum cache, tabela, coluna, migration, worker, flag ou estado de bloqueio gravado; único estado novo de módulo é `_rule_seen_tenants` (o D1 por tenant do design); nenhum segredo, token, telefone real, e-mail ou UUID nas 21.080 linhas acrescentadas; corpo da guarda do L10 `return False`.
- Lista do que a descrição do PR tem de declarar: em `/home/lorenzo/work/cortesia-env/logs/final-gate/` e no resumo do gate (não rodado: `make quality`, 4 arquivos S3/Qdrant, 2 de lista de portas, passos ao vivo, Uazapi cobrado ponta a ponta com egress real; falhas anteriores ao wish; shims de ambiente; emendas; decisões 16–20; resíduos aceitos; riscos aceitos pelo owner; rollout/rollback; os achados do gate).
- **Todos os grupos concluídos, revisão de execução e gate agregado SHIP.** O wish continua `IN_PROGRESS` até o PR, a CI, o merge humano e o QA ao vivo. Nada foi commitado; o PR único só sai quando o owner pedir.

### Depois do `final-gate`: decisão do owner (2026-11-13: "inclui os dois testes e a frase do runbook")

- A árvore congelada (`6e70bc53…`, cópia `patches/prc-consolidated-v2-frozen.patch`) é reaberta só para: MEDIUM-1 (`test_a_marked_builder_with_courtesy_keeps_the_marks_figures` em `test_credit_courtesy_surfaces_postgres.py`), MEDIUM-2 (`test_a_mark_or_global_captured_reservation_keeps_the_coverage_gate[mark|global]` em `test_credit_courtesy_rule_postgres.py`), ambos a partir dos arquivos validados do gate (`logs/final-gate/proposed_fix_M12_test.py` e `proposed_fix_M21_test.py`), aplicados pelo engenheiro da consolidação; e LOW-2 (frase no "Block" do runbook: o simulador do painel continua respondendo para um builder bloqueado, grátis e observacional), pelo fixer das docs. Nenhuma mudança em `src`.
- Depois: re-checagem independente dos dois testes (mutantes M12/M21) e da frase, validação do orquestrador nos dois arquivos PostgreSQL e no bloco de docs, novo congelamento e confirmação do `final-gate` sobre o delta. A oferta de listar os nomes das chaves que faltam por canal não foi pedida: fica fora.

### Depois do `final-gate`: os dois testes e a frase do runbook aplicados (2026-11-13)

- Engenheiro da consolidação: `test_credit_courtesy_surfaces_postgres.py` `5e4dd39a…` (10 testes: `test_a_marked_builder_with_courtesy_keeps_the_marks_figures` e o gêmeo `test_a_global_builder_with_courtesy_keeps_the_globals_figures`) e `test_credit_courtesy_rule_postgres.py` `4845f938…` (31 testes: `test_a_mark_or_global_captured_reservation_keeps_the_coverage_gate[mark|global]`); asserções do gate mantidas; mutantes M12 e M21 mortos nas duas variantes. Fixer das docs: último item do "Block" em `credit-courtesy-rule.md` (`05b03bc0…`): o simulador do painel não é bloqueado (`capture_events_enforcement` sai antes de ler o modo para origem diferente de `production`; `would_debit_mc` zero); **achado do fixer:** os testes citados fixam esse ramo sob o global, nenhum o exercita para um builder bloqueado pela regra (o runbook diz isso). Oferta ao owner em aberto: um teste "bloqueado pela regra + simulador → 200 sem cobrança".
- Conferido pelo orquestrador contra a árvore congelada v2: mudaram exatamente esses três arquivos; `src` `b5cef918…` e frontend `354f14d3…` inalterados.
- **Validação do orquestrador** (13:22:38Z → 13:28:38Z, PostgreSQL próprio em 55482, arquivos iguais no início e no fim): courtesy_surfaces 10/10, courtesy_rule 31/31, schema_dominance 53/53 (verde na primeira), charge_badge 2/2, "evidence valid", 0 pulados; black/isort/flake8 limpos; mypy sem problemas em 218 arquivos; único ERROR no log: a sonda intencional `no_such_marks_table`.
- Re-checagens independentes em andamento: os dois testes (revisor da consolidação, com os mutantes) e o item do simulador (revisor das docs).

### Depois do `final-gate`: re-checagens independentes (2026-11-13): SHIP nas duas

- **Docs (revisor do G7):** SHIP. O item do simulador confere cláusula a cláusula: o JWT do painel é origem `simulator` (`events.py:513`); `capture_events_enforcement` sai antes de `read_tenant_enforcement`; sob a regra o turno grava uma linha observacional (`simulator`, `would_debit_mc` 0), sem claim; nenhum outro portão pode recusá-lo (os 402 de `/v1/events` ficam em `_process_enforced_event`; o pipeline não importa portão de crédito; `/v1/events` não envia mídia). O parágrafo de "completo" exato; nada mais mudou no arquivo; 1647 passed.
- **Testes (revisor da consolidação):** SHIP. Delta só aditivo nos dois arquivos (0 linhas removidas); asserções do gate mantidas; gate dos dois arquivos verde (surfaces 10/10, rule 31/31, 0 pulados) e lint limpo; M12 e M21 mortos pelos testes novos (e sobrevivem às suítes antigas, como o gate tinha dito); o `force_credit_switch` do helper é legítimo (só substitui o switch do app durante a preparação em processo). NIT: a redação de um comentário do helper.
- **Intermitência de carga anotada pelo revisor:** na primeira rodada dele (três cargas PostgreSQL ao mesmo tempo) o teste já existente `test_dominance_without_courtesy_and_for_a_marked_builder` falhou em `credit_observational_effects >= 1` (log: `credit observational write dropped … reason: timeout`; essa gravação tem orçamento de 0,25 s, `credit_metering.py:282`); 4/4 reexecuções da versão atual e 4/4 da v2 verdes; na validação do orquestrador passou. Não é regressão; é um teste do G5a sensível a carga.
- **Opinião dos dois revisores sobre o teste do simulador:** vale acrescentar, LOW: hoje o código está certo por construção (o retorno cedo já existia no `fe800aec`), mas só o teste sob o global existe, e uma mudança futura do tipo "bloqueado pela regra recusa toda origem" contradiria o runbook sem nenhum teste falhar. Lugar: `tests/api/test_credit_suspension_events.py`, depois de `test_a_blocked_rule_builder_is_402_credit_suspended_and_not_served`.

### Decisão do owner (AskUserQuestion, 2026-11-13): rodada curta final

- "Fazer a rodada curta": (1) teste novo `test_the_panel_simulator_is_served_free_for_a_rule_blocked_builder` em `tests/api/test_credit_suspension_events.py` (bloqueado pela regra: chave de API 402, JWT do painel 200, sem claim nem efeito/lançamento novo); (2) o runbook cita esse teste e perde a ressalva "no test drives it…", dizendo o que o turno grava sob a regra (uma linha observacional `simulator`, `would_debit_mc` 0, sem claim); (3) reforço do teste sensível a carga `test_dominance_without_courtesy_and_for_a_marked_builder` (o orçamento de 250 ms da gravação observacional, `OBSERVATIONAL_SERVER_TIMEOUT`), sem tocar em `src` e sem enfraquecer o que ele prova; (4) redação do comentário do helper. Estado antes da rodada: `patches/prc-consolidated-v3-postgate.patch`. Engenheiro da consolidação (testes) e fixer das docs (runbook) despachados em paralelo; depois re-checagens, validação do orquestrador, novo congelamento e confirmação do `final-gate`.

### Rodada curta aplicada e árvore congelada de novo (v4, 2026-11-13)

- Engenheiro da consolidação (só testes, `src` inalterado): `tests/api/test_credit_suspension_events.py` `0c8ed9f3…` (8 testes; novo `test_the_panel_simulator_is_served_free_for_a_rule_blocked_builder`: sem linha global; chave de API 402 `credit_suspended`; JWT do painel sem `Idempotency-Key` 200 com o LLM chamado uma vez; sem claim, sem efeito/lançamento novo, sem transição; exatamente uma linha observacional `simulator` com `would_debit_mc` 0; mutante "bloqueado recusa toda origem" → falha). `test_credit_courtesy_surfaces_postgres.py` `8f323d5c…` (10 testes): fixture não-autouse `patient_observational_writer` pedida só por `test_dominance_without_courtesy_and_for_a_marked_builder` (padrão da casa de `_patient_observational_writer` em `test_credit_effective_enforcement_postgres.py`: orçamento de 10 s nos três gravadores observacionais; a asserção não mudou; o orçamento real de 250 ms continua pinado em `test_credit_metering_postgres.py`); prova em cópia: com o orçamento em 1 ms o teste original falha em `0 >= 1` e o reforçado passa. Comentário do helper corrigido.
- Fixer das docs: `credit-courtesy-rule.md` `462ef877…`: o item do simulador cita o teste novo (regra) e o do global, diz o que o turno grava (uma linha observacional `simulator`/0, sem claim) e perdeu a ressalva. 1647 passed.
- **Árvore congelada v4** (`/home/lorenzo/work/cortesia-prc`, HEAD `fe800aec`, nada commitado): `git diff | sha256sum` = `7869c0237edb744ef475526e9224a2c9998a9f7e8c73bdf0c55e018aa6bbba4c`; 63 arquivos versionados + 8 novos; `src` `b5cef918…` e frontend `354f14d3…` idênticos à árvore do gate; testes `29502b29…`; docs+.genie `093a867d…`; escopo vazio. Em relação à v2 mudaram exatamente quatro arquivos: o runbook e os três de teste (`test_credit_suspension_events.py`, `test_credit_courtesy_rule_postgres.py`, `test_credit_courtesy_surfaces_postgres.py`). Cópia: `patches/prc-consolidated-v4-frozen.patch` (`cbad207b…`).

### `final-gate`, rodada 2 (2026-11-13T13:46:02Z → 13:51:40Z): SHIP mantido para a árvore `7869c023…`

- **0 CRITICAL, 0 HIGH, 0 MEDIUM.** MEDIUM-1, MEDIUM-2 e LOW-2 da rodada 1 fechados; os outros cinco LOW continuam, sem bloquear. Fingerprints iguais no início e no fim; delta contra a v2 reconstruída: exatamente os quatro arquivos, tudo aditivo (fora o argumento da fixture e um import reformatado).
- Itens que eram dele para revisar: fixture `patient_observational_writer` aceita (cópia fiel de uma que já existe no `fe800aec`, não-autouse, um teste só, asserções inalteradas, o teste de dominância continua mordendo com ela; mais 4 execuções seguidas verdes sob carga); comentário do helper exato; teste do simulador não-vácuo e sobre um builder bloqueado pela REGRA (concessão e remoção reais, sem linha global, 402 pela chave de API no mesmo teste, o tenant do JWT é do mesmo builder); item do runbook confere cláusula a cláusula.
- Validação dele: surfaces 10/10, rule 31/31, schema_dominance 53/53 ("evidence valid", 0 pulados); `test_credit_suspension_events.py` + `test_events_credit_enforced.py` 73 passed; black/isort/flake8 limpos (607 arquivos); 0 ERROR nos logs. Mutantes 5/5 mortos: M12, M21, S1 (guarda de origem removida), S2 (simulador debitaria), D1 (master-admin ignora a precedência da marca). Não repetiu mypy, frontend nem o resto do gate agregado: `src` e frontend são byte a byte os da rodada 1.
- Mudanças na lista do PR: os dois MEDIUM passam a "lacunas de teste achadas pelo gate final e fechadas neste PR"; "o simulador continua aberto quando bloqueado" passa de resíduo a comportamento pinado e documentado (decisão de design); fingerprints novos (`7869c023…`, v4; contagens surfaces 10, rule 31, suspension events 8). O resto da lista da rodada 1 vale como estava.

### Validação do orquestrador na árvore congelada v4 (2026-11-13T13:46:00Z → 13:57:15Z): verde

- PostgreSQL próprio em 55482 (parado e apagado); fingerprints iguais no início e no fim (`7869c023…`, os 8 arquivos novos). Unitários (suspensão events/webhooks/rag-media, `test_events_credit_enforced`, `tests/docs`, `tests/ops/test_credit_enforcement.py`, `tests/ops/test_credit_courtesy_rule.py`, os três `tests/scripts` de janela/wiring): **1864 passed**. Gate PostgreSQL por arquivo, "evidence valid", 0 pulados: courtesy_surfaces 10/10, courtesy_rule 31/31, schema_dominance 53/53 (verde na primeira), courtesy_migration 54/54 (a guarda). black/isort/flake8 limpos; mypy sem problemas em 218 arquivos. Log do servidor: 0 erros de sintaxe ou de objeto inexistente; os 24 tipos de ERROR são as provas negativas intencionais da suíte de migration (check constraints, recusas de downgrade da 046, append-only, lock timeout).
- **Estado final:** árvore v4 congelada, `final-gate` SHIP (rodada 2), validação do orquestrador verde. `make quality` não foi rodado (diretriz do owner). Nada commitado; o PR único só sai quando o owner pedir. Em aberto com o owner: incluir wish/DESIGN/DRAFT no PR com `git add -f` (proposto, sem resposta).

### Pedido do owner (2026-11-13): "pode abrir o PR, inclui o wish e o design"

- Commit na branch `wish/debito-cortesia-automatico` a partir da árvore congelada v4 (`7869c023…`, base `fe800aec` = `origin/main`, que não andou), push e **um** PR contra a `main` (push e criação em chamadas separadas). O merge é humano.
- Entram no PR, com `git add -f`: este `WISH.md` e o `DESIGN.md` (inalterado; `design-review-evidence.mjs verify` exit 0). O `DRAFT.md` não foi pedido e fica fora. O `.genie/INDEX.md` não entra (atualizado quando o wish virar SHIPPED).
- O repositório é público: wish e design conferidos antes de publicar (nenhum e-mail, telefone, URL, token, ARN, número de conta nem UUID completo). A única linha do wish que citava o nome do builder e da clínica de teste (critério L3 do QA) foi reescrita sem nomes nem prefixos de id; o sentido do critério não mudou.
- Depois do PR: CI, merge humano, e só então os passos ao vivo L2a → L1 → go/no-go → L2b → L3, cada um com o "vai" do owner.

---

## Files to Create/Modify

```
# Grupo 1
src/repositories/credit.py
src/services/credit_switch.py
src/services/credit_ledger.py
src/services/credit_inventory.py
src/services/credit_allocations.py
src/services/credit_lifecycle.py
tests/integration/credit_rollout_world.py
tests/integration/test_credit_schema_dominance_postgres.py
tests/integration/test_credit_courtesy_rule_postgres.py (novo)
# Grupo 2
src/services/credit_metering.py
src/services/credit_principal.py
src/services/credit_rollout.py
src/services/credit_lifecycle.py (tenant_suspended, reservation_verdict)
src/api/routes/events.py
# Grupo 3
src/api/routes/webhook.py
src/services/reply_media.py
tests/integration/test_credit_uazapi_enforced_postgres.py (novo)
# Grupo 4
tests/integration/test_credit_evolution_enforced_postgres.py (novo)
# Grupo 5a
src/api/routes/credits.py
src/api/schemas/credits.py (se um campo mudar)
src/api/routes/knowledge.py
src/api/routes/media_assets.py
src/api/routes/admin.py
src/api/schemas/admin.py
src/api/schemas/admin_credits.py
src/services/credit_rollout.py (comentários)
src/ops/credit_charge_mark.py
# Grupo 5b
frontend/artifacts/neurostack/src/pages/billing-view.ts
frontend/artifacts/neurostack/src/pages/billing-view.test.ts
frontend/artifacts/neurostack/src/pages/billing-contract.ts (se o contrato mudar)
frontend/artifacts/neurostack/src/pages/billing-contract.test.ts
frontend/artifacts/neurostack/src/components/billing/credits-and-topups.tsx
frontend/artifacts/neurostack/src/pages/billing.test.tsx
frontend/artifacts/neurostack/src/pages/master-admin.tsx
frontend/artifacts/neurostack/src/pages/master-admin.test.tsx
frontend/artifacts/neurostack/src/components/admin/courtesy-credits-dialog.tsx
frontend/artifacts/neurostack/src/components/admin/courtesy-credits-dialog.test.tsx
# Grupo 6
src/ops/credit_courtesy_rule.py (novo)
tests/ops/test_credit_courtesy_rule.py (novo)
tests/integration/test_credit_courtesy_rule_ops_postgres.py (novo)
# Grupo 7
docs/runbooks/credit-courtesy-rule.md (novo)
docs/billing/credits.md
docs/runbooks/credit-courtesy.md
docs/runbooks/credit-charge-mark.md
docs/runbooks/credit-enforcement-arming.md
docs/runbooks/uazapi-webhook-auth.md
.genie/wishes/uazapi-webhook-auth/WISH.md
# mais os testes unitários e de rota de cada grupo
```
