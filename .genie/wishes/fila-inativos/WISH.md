# Wish: Fila de Inativos

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `fila-inativos` |
| **Date** | 2026-08-30 |
| **Author** | Codex |
| **Appetite** | medium |
| **Branch** | `wish/fila-inativos` |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/fila-inativos/DESIGN.md) |

## Summary

Criar uma Fila de Inativos para sessões ativas que permanecem em handoff permanente, permitindo que a clínica encontre cada conversa sem controle da IA e reative manualmente apenas a sessão escolhida. A entrega reutiliza o estado canônico de `sessions`, preserva isolamento por tenant e materializa no menu e no switch os dois estados das referências visuais fornecidas.

## Scope

### IN

- Contrato paginado `GET /v1/sessions/inactive` para a sessão ativa efetiva de cada identidade de roteamento que também satisfaça `is_paused=true` e `paused_until IS NULL`, ordenada por atividade recente dentro do tenant autorizado.
- Comando idempotente `POST /v1/sessions/{session_id}/reactivate` para limpar o estado de pausa permanente de uma sessão ativa autorizada.
- Allowlist backend de clinic admin, builder e super admin nos dois endpoints; investor e qualquer perfil não operacional recebem 403.
- DTO mínimo sem `cognitive_state` completo, contendo somente `session_id`, `contact_identifier`, `channel`, `last_intent`, `funnel_stage` e `last_activity_at`.
- Nova página responsiva `/inactive-queue`, item **Fila de Inativos** no local mostrado pela referência, polling REST de 30 segundos, paginação e estados de loading/vazio/erro.
- Switch acessível **Voltar atendimento para IA**; após 2xx, estado verde **IA no controle** e remoção do item; em falha, preservação da linha e feedback localizado.
- Traduções PT/EN e testes backend/frontend focados, seguidos pelos gates completos dos dois pacotes.

### OUT

- Reativação automática, mudança nas regras de criação de handoff ou reabertura de sessões encerradas.
- Escrita, conclusão ou sincronização de `handoff_records`: um registro `pending`/`accepted` permanece inalterado e pode continuar visível na Fila de Handoff após a reativação da IA; o operador deve fazer checkout dele separadamente.
- Migração, tabela própria, coluna `paused_at` ou histórico de ativações.
- Busca, filtros avançados, exportação, ações em massa, atribuição de atendentes ou notificações.
- WebSocket/SSE, aplicativo nativo ou dados mockados/visão operacional para investor.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Fonte canônica do controle atual da IA é `sessions` com o predicado triplo de sessão ativa em pausa permanente | É o estado consultado pelo runtime; `handoff_records` representa um ciclo operacional separado, incompleto para takeover humano, e não determina sozinho o controle atual da IA. |
| 2 | Dois endpoints dedicados, sob `/v1/sessions`, com paginação e comando idempotente | Mantém leitura e mutação explícitas, limita cardinalidade e tolera repetição/concorrência. |
| 3 | Allowlist de perfil é aplicada antes da resolução de tenant em ambas as rotas | Ocultar navegação não protege a API; leitura e mutação são operações clínicas. |
| 4 | `tenant_id` é opcional e só representa override para builder/super admin | Clinic admin deriva autoridade do token; overrides passam pela validação de vínculo existente. |
| 5 | Cross-tenant, ID inexistente ou sessão encerrada retornam 404; pausa temporária retorna 409 | Evita enumeração e impede que o comando altere estado fora de seu propósito. |
| 6 | O DTO projeta seis campos e nunca retorna o JSON `cognitive_state` | Reduz exposição e acoplamento; a tela só precisa da intenção e etapa projetadas. |
| 7 | Código curto é derivado de `session_id`; horário exibido é `last_activity_at` | Evita armazenamento derivado e não inventa um `paused_at` inexistente. |
| 8 | UI confirma sucesso somente após 2xx e mantém o estado verde por uma transição curta antes de remover | Corresponde à referência sem comunicar controle da IA antes da persistência. |
| 9 | Polling REST de 30 segundos, sem push | É suficiente para a fila operacional e segue a implementação da Fila de Handoff existente. |
| 10 | Para cada identidade de roteamento, a sessão ativa efetiva é a primeira por `last_activity_at DESC, id DESC`; com `user_id` não nulo a chave é `(tenant_id, user_id)`, e com `user_id` nulo cada `session_id` é sua própria chave | Alinha a fila e `SessionRepository.find_by_user` ao lookup do runtime, resolve duplicatas de forma determinística e mantém canais de sessão direta endereçáveis sem migração ou estado durável novo. |
| 11 | Reativar a IA não altera nem conclui `handoff_records`; dupla visibilidade entre Fila de Inativos e Fila de Handoff é aceita | O comando muda somente o controle da IA em `sessions`; um handoff `pending`/`accepted` continua exigindo checkout separado pelo operador. |

## Simplicity Case

- **Simplest complete design:** métodos focados no repositório/serviço de sessões, dois endpoints, uma página, uma rota/menu e um switch por registro.
- **Added machinery:** paginação limita consultas; polling de 30 segundos mantém a fila atual; atualização atômica/idempotente atende cliques concorrentes; o estado transitório pós-2xx reproduz o feedback visual solicitado.
- **Deferred until measured:** busca/filtros entram quando operar páginas de 20 itens se mostrar insuficiente; push só será reconsiderado se o atraso máximo de 30 segundos violar um SLA acordado.
- **Complexity removed:** nenhuma migração, tabela, cache, fila assíncrona, sincronização com handoffs, configuração ou dependência nova.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [ ] O menu mostra **Fila de Inativos** entre **Fila de Handoff** e **Histórico de Traces**, com ícone/seleção equivalentes à referência e sem oferecer a rota ao investor.
- [ ] Clinic admin, builder e super admin acessam a página com o tenant efetivo correto; investor e qualquer perfil não operacional recebem 403 em ambos os endpoints.
- [ ] `GET /v1/sessions/inactive` resolve primeiro a sessão ativa efetiva por identidade com `last_activity_at DESC, id DESC` e retorna apenas as efetivas que também satisfazem `is_paused=true AND paused_until IS NULL` no tenant autorizado.
- [ ] A identidade efetiva usa `(tenant_id, user_id)` quando `user_id` não é nulo e trata cada `session_id` como chave própria quando `user_id` é nulo; a lista ordena por `last_activity_at DESC, id DESC` e pagina `page`/`page_size` com total e flags coerentes.
- [ ] Cada item contém exatamente `session_id`, `contact_identifier`, `channel`, `last_intent`, `funnel_stage` e `last_activity_at`; o JSON `cognitive_state` não é exposto.
- [ ] Clinic admin não lista outro tenant, builder sem vínculo não acessa a clínica e super admin só usa o override explícito selecionado.
- [ ] `POST /v1/sessions/{session_id}/reactivate` exige que o ID seja a sessão ativa efetiva no tenant, define `is_paused=false` e `paused_until=null` e não altera `handoff_records`.
- [ ] Repetir a reativação de uma sessão ativa já sem pausa retorna 200 `already_active` sem mutação adicional.
- [ ] Reativação direta de uma linha ativa antiga superseded retorna 409 `superseded_session`; pausa temporária retorna 409; sessão encerrada, cross-tenant ou inexistente retorna 404; nenhum desses casos altera dados.
- [ ] Reativar a IA preserva exatamente qualquer `handoff_record` `pending`/`accepted`; sua permanência simultânea na Fila de Handoff é esperada até checkout separado.
- [ ] A página mostra os seis campos, deriva o código curto no cliente e nunca rotula `last_activity_at` como horário de pausa.
- [ ] A fila atualiza a cada 30 segundos, troca de clínica sem exibir resposta obsoleta e oferece paginação de 20 itens.
- [ ] O switch inicia desligado com **Voltar atendimento para IA**, fica indisponível durante a requisição e só mostra **IA no controle** em verde após 2xx.
- [ ] Em sucesso, a linha sai após a transição; em falha, permanece desligada/visível e um erro localizado é mostrado.
- [ ] Loading, vazio, erro e layout móvel/desktop funcionam sem overflow; switch tem nome acessível, foco visível e operação por teclado.
- [ ] Novas strings existem em PT/EN e testes frontend cobrem ordem/visibilidade do menu, renderização, troca de clínica, polling e sucesso/falha da reativação.
- [ ] Testes backend cobrem filtro triplo após resolução efetiva, DTO, ordenação/paginação, duplicatas ativas (inclusive `user_id` nulo), `superseded_session`, idempotência, pausa temporária, sessão encerrada, perfis, isolamento por tenant, handoff inalterado e o pause check da próxima mensagem após reativação.
- [ ] Gates agregados Python e frontend passam sem regressão.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| backend-contract | engineer | 2 — mutação de estado com invariantes de auth/tenant e concorrência | engineer-standard / high | Implementar DTO, consulta paginada, reativação idempotente e testes de segurança/contrato. |

### Wave 2 (after backend-contract)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| frontend-queue | engineer | 2 — aceitação visual/interativa e integração de dois estados assíncronos | engineer-standard / high | Implementar página, rota/menu, locales e testes frontend conforme referências. |

### Aggregate Gate (after Wave 2)

```bash
make quality
pnpm --dir frontend/artifacts/neurostack test
pnpm --dir frontend/artifacts/neurostack typecheck
pnpm --dir frontend/artifacts/neurostack build
```

O gate completo é necessário porque a entrega atravessa autenticação/tenant, contrato HTTP e navegação compartilhada; os checks focados de cada grupo continuam sendo a primeira evidência de falha.

## Execution Groups

### Group 1: Backend — contrato seguro da fila e reativação

**Goal:** Expor uma fila paginada de sessões ativas em pausa permanente e uma reativação idempotente, ambas limitadas ao perfil e tenant autorizados.

**Deliverables:**
1. `src/api/schemas/sessions.py`: `InactiveSessionItem`, resposta paginada e resposta tipada de reativação; item limitado aos seis campos aprovados.
2. `src/repositories/session.py`: resolução compartilhada da sessão ativa efetiva por `last_activity_at DESC, id DESC`, incluindo o desempate de `find_by_user`; consulta/contagem das efetivas pelo predicado de pausa permanente e atualização atômica somente da efetiva.
3. `src/services/session_service.py`: projeção segura de `last_intent`/`funnel_stage`, paginação e resultados `reactivated`/`already_active`/`superseded_session`, sem devolver `cognitive_state` nem alterar handoffs.
4. `src/api/routes/sessions.py`: `GET /inactive` e `POST /{session_id}/reactivate`, posicionados sem conflito com `/{session_id}`, allowlist operacional e resolução segura de tenant/override.
5. `tests/api/test_sessions.py`: casos de contrato, filtro, paginação/ordem, duplicatas ativas por identidade, `user_id` nulo, `superseded_session`, idempotência, concorrência lógica, pausa temporária, encerrada, perfis, cross-tenant e handoff inalterado com dupla visibilidade aceita.
6. `tests/api/test_webhook_uazapi.py`: regressão integrada em que, após reativar a efetiva entre duplicatas ativas, a próxima mensagem passa pelo pause check comum e continua no pipeline da IA.

**Acceptance Criteria:**
- [ ] Listagem e contagem particionam sessões ativas por `(tenant_id, user_id)` quando `user_id` não é nulo e por `session_id` quando é nulo, escolhem uma efetiva por `last_activity_at DESC, id DESC` e só então aplicam `is_paused=true AND paused_until IS NULL`.
- [ ] `SessionRepository.find_by_user(..., active_only=True, limit=1)` usa `last_activity_at DESC, id DESC`, garantindo que o runtime e a fila escolham a mesma linha entre duplicatas ativas.
- [ ] Resposta paginada tem `items`, `total`, `page`, `page_size`, `has_next`, `has_previous`; cada item tem somente os seis campos do DTO.
- [ ] Clinic admin usa seu tenant; builder precisa de vínculo; super admin pode usar override explícito; ambos os endpoints devolvem 403 para investor/outros perfis.
- [ ] Reativação autorizada da efetiva é atômica, limpa ambos os campos de pausa e retorna `reactivated`; repetição da mesma efetiva ativa retorna 200 `already_active`.
- [ ] Uma linha ativa mais antiga e superseded devolve 409 `superseded_session`; pausa temporária devolve 409; encerrada, cross-tenant e inexistente devolvem 404 sem mutação ou enumeração.
- [ ] Fixtures com duplicatas provam que só a efetiva pausada é listada, que uma efetiva não pausada oculta a duplicata antiga pausada e que a próxima mensagem após reativação usa a mesma efetiva e continua na IA.
- [ ] Nenhuma migration, tabela, configuração ou alteração em `handoff_records` é introduzida; registros `pending`/`accepted` permanecem byte-for-byte inalterados e podem continuar na Fila de Handoff.

**Validation:**

```bash
poetry run pytest tests/api/test_sessions.py tests/api/test_webhook_uazapi.py -q
poetry run mypy src/api/routes/sessions.py src/api/schemas/sessions.py src/repositories/session.py src/services/session_service.py
poetry run black --check src/api/routes/sessions.py src/api/schemas/sessions.py src/repositories/session.py src/services/session_service.py tests/api/test_sessions.py tests/api/test_webhook_uazapi.py
poetry run flake8 src/api/routes/sessions.py src/api/schemas/sessions.py src/repositories/session.py src/services/session_service.py tests/api/test_sessions.py tests/api/test_webhook_uazapi.py
```

Esses checks cobrem diretamente o novo contrato público, a mutação e a fronteira auth/tenant; o gate completo permanece obrigatório após a integração frontend.

**depends-on:** none

---

### Group 2: Frontend — menu, fila responsiva e switch de retomada

**Goal:** Entregar a tela operacional conforme as referências, consumindo o contrato aprovado e comunicando corretamente sucesso e falha da retomada.

**Deliverables:**
1. `frontend/artifacts/neurostack/src/pages/inactive-queue.tsx`: fetch paginado, polling de 30 segundos, proteção contra resposta obsoleta na troca de clínica, estados loading/vazio/erro e linhas responsivas.
2. Switch shadcn acessível por linha: **Voltar atendimento para IA** desligado; disabled durante POST; **IA no controle** verde somente após 2xx; remoção após transição; falha preserva linha e dispara toast localizado.
3. `frontend/artifacts/neurostack/src/App.tsx` e `src/components/layout.tsx`: rota `/inactive-queue` e menu entre handoff/traces com ícone de usuário inativo; oculto para investor.
4. `frontend/artifacts/neurostack/src/locales/pt.ts` e `en.ts`: títulos, labels, estados, paginação e mensagens de erro/vazio.
5. `inactive-queue.test.tsx` e atualização de `layout.test.tsx`: DTO/renderização, menu/perfis, tenant selecionado, paginação/polling e mutação sucesso/falha.

**Acceptance Criteria:**
- [ ] Ordem, texto, ícone e seleção do menu correspondem à referência; investor não vê o item nem acessa a página operacional.
- [ ] Cada linha mostra identificador, código derivado, canal, intenção, etapa e última atividade sem inventar nome/horário de pausa.
- [ ] A consulta usa tenant efetivo, página de 20 itens e polling de 30 segundos; troca de clínica ignora/cancela resposta anterior.
- [ ] Switch e labels reproduzem os dois estados fornecidos, têm semântica acessível e nunca antecipam sucesso.
- [ ] POST 2xx mostra o estado verde e remove a linha; erro mantém a linha/estado desligado e mostra toast traduzido.
- [ ] Loading, vazio, erro, paginação e layouts móvel/desktop são cobertos; PT e EN têm todas as chaves.

**Validation:**

```bash
pnpm --dir frontend/artifacts/neurostack test -- src/pages/inactive-queue.test.tsx src/components/layout.test.tsx
pnpm --dir frontend/artifacts/neurostack typecheck
pnpm --dir frontend/artifacts/neurostack build
```

A suíte focada prova navegação e interação; typecheck/build cobrem o contrato TypeScript e a rota compartilhada tocados pelo grupo.

**depends-on:** backend-contract

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Clinic admin vê apenas inativos permanentes ativos da própria clínica e consegue devolver uma conversa à IA.
- [ ] Builder troca entre duas clínicas vinculadas sem flash/vazamento de itens da seleção anterior; clínica sem vínculo é negada.
- [ ] Super admin usa a clínica selecionada; investor não vê menu e chamadas diretas à API recebem 403.
- [ ] Pausa temporária e sessão encerrada não aparecem; chamadas diretas de reativação retornam 409/404 respectivamente.
- [ ] Com duas linhas ativas para a mesma identidade, somente a efetiva por `last_activity_at DESC, id DESC` pode aparecer; reativar diretamente a antiga retorna 409 `superseded_session`, enquanto cross-tenant continua 404.
- [ ] Em mobile e desktop, a linha não estoura horizontalmente e o switch funciona por mouse e teclado.
- [ ] Uma falha de rede preserva o registro e exibe erro; sucesso exibe **IA no controle** e remove somente a sessão correta.
- [ ] Após reativação, a próxima mensagem elegível da sessão ativa volta a passar pelo pipeline da IA.
- [ ] Um `handoff_record` `pending`/`accepted` ligado à conversa permanece inalterado após reativação e pode seguir visível na Fila de Handoff; o checkout separado continua disponível.
- [ ] Fila de Handoff, Histórico de Traces, listagem genérica de sessões e webhooks existentes continuam passando nos gates agregados.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Reutilizar a autorização permissiva da listagem genérica | High | Allowlist explícita e resolução de tenant dedicada antes da consulta/comando; testes por perfil e cross-tenant. |
| Fila e webhook escolherem linhas diferentes entre sessões ativas duplicadas | High | Uma regra compartilhada `last_activity_at DESC, id DESC`, desempate em `find_by_user` e testes integrados de listagem, reativação e próxima mensagem. |
| Corrida entre dois operadores reativando a mesma sessão | Medium | Update transacional/atômico e resultado idempotente `already_active`, sem lock distribuído. |
| Handoff `pending`/`accepted` continuar visível após a IA ser reativada | Medium | Risco residual aceito: não sincronizar estados; testar ausência de mutação e orientar checkout separado na Fila de Handoff. |
| Poll sobrescrever a remoção/sucesso ou mostrar tenant anterior | Medium | Identificar requisições por tenant/página, cancelar/ignorar respostas obsoletas e remover por `session_id`. |
| `user_id` não ser telefone | Low | DTO chama o valor de `contact_identifier`, mostra canal e não oferece link WhatsApp. |
| Ausência de `paused_at` induzir label incorreto | Low | Exibir **Última atividade**; não mostrar duração nem “pausado desde”. |

---

## Review Results

### Plan Review — 2026-08-30T15:19:46Z

- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/review_fila_inativos_wish`
- **Reviewed WISH SHA-256:** `d37e6f249b8918b03adfa5579a91e7921b307295c3501741de5c70be9391788e`
- **Validation:** structure/placeholders, `git diff --check`, `make -n quality` and frontend script existence passed; runtime suites not run because this is a pre-implementation plan review.
- **HIGH:** list/reactivation did not share the runtime's effective-session selection under duplicate active rows.
- **MEDIUM:** unchanged active `handoff_records` behavior was not explicitly accepted/tested.
- **MEDIUM:** aggregate frontend commands were named only in prose.
- **Required correction:** align the plan to deterministic effective-session selection, cover duplicate rows and next-message behavior, document/test cross-queue coexistence, and persist exact aggregate commands before re-review.

### Plan Re-review — 2026-08-30T15:29:01Z

- **Verdict:** SHIP
- **Reviewer:** `/root/rereview_fila_inativos_wish`
- **Reviewed WISH SHA-256:** `0124462c12f55e400c44aee834b9e34d0f409f417170b8b5ad6572a8e719e119`
- **Governing design reviewed-content SHA-256:** `5af261f845acb68c93d1b45e23d5fe607d8c63a565f927fe4a51c1a65b3f0035`
- **Validation:** placeholder/diff/structure checks, files manifest, `make -n quality` and frontend script inspection passed; root `wishes:lint` unavailable; runtime suites remain execution-time gates.
- **Gaps:** none. Effective-session selection, duplicate/next-message coverage, accepted handoff coexistence and exact aggregate commands were verified closed.

### Execution Review — backend-contract — 2026-08-30T16:10:31Z

- **Verdict:** SHIP
- **Reviewer:** `/root/review_fila_inativos_backend`
- **Diff SHA-256:** `5b011d792ff0fc70bceb868835653f232f0cd45a371bb6a872ddf031cd1fde2d`
- **Validation:** 53 focused tests passed; mypy, Black, Flake8, AST/OpenAPI/SQL compilation and diff checks passed.
- **Residual MEDIUM:** unlocked miss classification race; polled query overfetch/unmeasured scale; lost dedicated legacy assertions.
- **Residual LOW:** unbounded page/offset.

### Quality Review — backend-contract — 2026-08-30T16:20:27Z

- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/quality_review_fila_backend`
- **Diff SHA-256:** `5b011d792ff0fc70bceb868835653f232f0cd45a371bb6a872ddf031cd1fde2d`
- **HIGH:** a conditional-update miss could be classified as `already_active` after effective-session churn while the target remained permanently paused.
- **MEDIUM:** targeted isort drift and loss of unique legacy assertions for minimal defaults and generic pagination metadata.
- **Residual MEDIUM/LOW:** list/count query cost and deep OFFSET remain measurement-led follow-ups within the approved no-migration scope.
- **Required correction:** consistent CAS/retry classification with deterministic regression, sort touched imports, restore unique legacy assertions, then re-review and rerun all group gates.

### Execution Re-review — backend-contract — 2026-08-30T16:36:50Z

- **Verdict:** SHIP
- **Reviewer:** `/root/rereview_fila_inativos_backend`
- **Diff SHA-256:** `9efc8db7676634a23ce633a18aad29511a2d0d9eff28219e37b8fc2e1a88aa5d`
- **Validation:** 55 tests passed; mypy, Black, Flake8, isort and diff checks passed independently; post-validation digest unchanged.
- **Gaps:** none. CAS retries are bounded, exhaustion returns `reactivation_conflict`, false `already_active` is covered, and legacy assertions/import order were restored.
- **Accepted residuals:** query scale and deep OFFSET remain measurement-led follow-ups under the approved no-migration scope.

### Orchestrator Validation — backend-contract — 2026-08-30

- **Result:** PASS
- **Command scope rationale:** exact group gate plus isort covers the public API, runtime lookup, state mutation, auth/tenant boundary and every touched backend/test file; aggregate repository gates remain scheduled after frontend integration.
- **Pytest:** 55 passed, 4 deprecation warnings, 156.15s.
- **Static gates:** mypy clean (4 source files); Black clean (6 files); Flake8, isort and `git diff --check` exited 0 with no output.

### Execution Review — frontend-queue

- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/review_fila_inativos_frontend`
- **Owned-file manifest SHA-256:** `cb6d23bc313af7c96c50aade0a2bad14d4c00648deb5785191030a0abe9ab46c`
- **HIGH:** delayed success removed/decremented locally without reconciling page/hasNext/hasPrevious; POST/timer completion was not scoped to tenant/request generation and could mutate the newly selected clinic.
- **MEDIUM:** success reference must retain **Voltar atendimento para IA** while adding green **IA no controle**; tests lacked mutation/pagination races, super-admin tenant and responsive/browser evidence.
- **Validation context:** owned tests 18/18 and typecheck passed; environment-complete build passed; broad wrapper failure is the documented unrelated clinic-config locale mismatch.
- **Required correction:** context-keyed mutation lifecycle, success tombstone, authoritative refetch/page clamp, exact reference label composition and deterministic race/boundary tests.

### QA Advisory — frontend-queue

- **Verdict:** FIX-FIRST advisory
- **Reviewer:** `/root/qa_lens_fila_frontend`
- **Focused validation:** 18/18 tests passed; unrelated clinic-config locale test remains the known 4-pass/1-fail baseline mismatch.
- **HIGH confirmed:** stale clinic-A POST could decrement clinic-B pagination.
- **Coverage gaps:** exact mutation URL/tenant/auth, already-active success, super-admin/no-clinic/page reset, keyboard/focus, English copy, pagination boundaries and responsive semantics.

### Execution Re-review — frontend-queue — loop 1

- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/rereview_fila_inativos_frontend_loop1`
- **Owned-file manifest SHA-256:** `d55c5594580288e8456f8a5808a97098f8fdb154e8dab7fa000652a1780858a6`
- **Validation:** focused Vitest 25/25, typecheck, environment-complete production build and exact-manifest diff check passed.
- **HIGH:** a poll completing while POST is pending can remove the row before the 2xx handler snapshots it, preventing the required green success transition and its final authoritative reconciliation.
- **MEDIUM:** a POST resolving after unmount can still emit a toast, create a timer and start a GET because unmount does not invalidate/cancel the mutation context.
- **Verified closed:** authoritative pagination/clamp, tenant/page generation guard, stale pre-POST GET invalidation, success-transition tombstone, reference label composition and the previously missing role/locale/focus/responsive assertions.
- **Required correction:** preserve a context-scoped item snapshot from mutation start through transition, invalidate/abort POST on unmount, and add deterministic poll-before-POST plus unmount-before-response regressions.

### Code-quality Review — frontend-queue — loop 1

- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/quality_fila_inativos_frontend_loop1`
- **Owned-file manifest SHA-256:** `d55c5594580288e8456f8a5808a97098f8fdb154e8dab7fa000652a1780858a6`
- **HIGH:** confirmed pending-POST/poll race; queue rows also remain unkeyed until a passive effect, allowing a prior tenant/page row set to render once under the new context.
- **MEDIUM:** render-time ref mutation is unsafe under abandoned concurrent renders; unmount does not invalidate POST continuations; 2xx JSON lacks dependency-free runtime validation; keyboard test did not prove activation.
- **Validation:** fresh typecheck and scoped diff check passed; supplied focused 25/25 and production build evidence remained pinned to the unchanged manifest.
- **Required correction:** key rendered queue state by committed context, move identity synchronization to committed lifecycle with A→B→A-safe generations, preserve a mutation-start snapshot, invalidate POST on unmount, validate essential response shapes and exercise real keyboard activation.

### Execution Re-review — frontend-queue — loop 2

- **Verdict:** SHIP
- **Reviewer:** `/root/rereview_fila_inativos_frontend_loop2`
- **Owned-file manifest SHA-256:** `5aed453282e7fb6db9d9bbd8ffea19983ff4c79426dcc26bc3d4529acc4cb75d`
- **Validation evidence:** page 23/23; combined 33/33; typecheck, production build and scoped diff check passed on the exact manifest.
- **Closed:** pending POST/poll preservation, 600 ms success transition and authoritative clamp/refetch, zero prior-context render, committed monotonic identity, A→B→A safety, GET/POST/timer invalidation, runtime payload guards, real Space/Enter activation and duplicate suppression.
- **Residual QA:** browser confirmation of mobile overflow/native focus; a failed background poll during a protected transition can temporarily display the page error state without deleting or corrupting the protected row.

### Code-quality Re-review — frontend-queue — loop 2

- **Verdict:** SHIP
- **Reviewer:** `/root/quality_fila_inativos_frontend_loop1`
- **Owned-file manifest SHA-256:** `5aed453282e7fb6db9d9bbd8ffea19983ff4c79426dcc26bc3d4529acc4cb75d`
- **Result:** no CRITICAL/HIGH gaps; lifecycle, context isolation, cleanup, runtime guards, deterministic regressions and duplicate-request protection are sound with no new dependencies.
- **Non-blocking residuals:** parseability of `last_activity_at` is trusted from the typed backend contract; `ProtectedItem.phase` is unused; A→B→A is structurally guarded but not named as a dedicated three-context test.

### Orchestrator Validation — frontend-queue — 2026-08-30

- **Result:** PASS
- **Frozen owned-file manifest SHA-256:** `5aed453282e7fb6db9d9bbd8ffea19983ff4c79426dcc26bc3d4529acc4cb75d`
- **Focused Vitest:** 2 files, 33/33 tests passed in 291.48s (23 queue lifecycle/API/rendering tests and 10 layout/menu tests).
- **Static gate:** `pnpm --dir frontend/artifacts/neurostack typecheck` passed; scoped `git diff --check` passed and reproduced the frozen digest.
- **Production build:** `PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack build` passed; 2,798 modules transformed in 1m35s with only the existing sourcemap and large-chunk warnings.

### Aggregate Repository Gates — 2026-08-30

- **Feature-scoped result:** PASS — backend contract/repository/service/API tests 55/55; frontend queue/layout tests 33/33; touched-file mypy/Black/Flake8/isort/diff checks, frontend typecheck and production build all passed.
- **`make quality`:** baseline failure before lint/tests because repository-wide Black check would reformat 162 unrelated files; no bulk formatting was applied.
- **Global lint/typecheck:** baseline failure outside this WISH; Flake8 emitted 2,388 lines across the repository and global mypy reported 364 errors in 73 files. Touched backend and frontend scopes remain clean.
- **Backend broad suite:** collected 2,295 tests; execution was stopped at 7% after reproducing two unrelated failures in `tests/api/test_config_credentials_boundary.py`; all executed fila-inativos tests were green and the exact 55-test group gate had already completed.
- **Frontend broad suite:** 62/63 passed across 10 files; the sole failure is the documented pre-existing `clinic-config-api-settings.test.tsx:120` Portuguese expectation against the application's English default locale. `inactive-queue.test.tsx` passed 23/23 inside the broad run.
- **Scope decision:** aggregate baseline debt was recorded rather than modifying unrelated user-owned files or widening the approved WISH.

### Final Execution Gate — 2026-08-30 — pass 1

- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/final_gate_fila_inativos`
- **HIGH:** the backend contract and regression permit `contact_identifier: null`, while the frontend DTO/runtime guard require `string`; one valid null-identity row therefore rejects the entire response and displays the page error state.
- **Required correction:** use `string | null`, accept null in the guard, render `—`, add a frontend regression and rerun the frozen frontend gates plus independent final review.
- **MEDIUM checkout hygiene:** current branch/index contain other active wishes and shared-file edits; do not blanket-stage or commit from this checkout. Isolate fila hunks on the intended branch/worktree before PR creation.
- **Accepted baseline debt:** global Black/Flake8/mypy failures, the known frontend locale mismatch and unrelated backend config-credential failures are not fila regressions.
- **Lifecycle:** remain `IN_PROGRESS` until a clean final gate and an isolated PR targeting `dev`.

### Final-gate Remediation — nullable contact identity

- **Change:** frontend `contact_identifier` now follows the backend contract as `string | null`; the runtime guard accepts null and the row renders `—` without rejecting the page response.
- **Regression:** a valid null-contact item renders the queue, fallback and remaining fields without entering the page error state.
- **Frozen seven-file manifest SHA-256:** `be882b63c764dd252551257d2373ce9d42fbf88831e13da2736029d35bbe2d52`
- **Orchestrator validation:** focused Vitest 34/34 passed in 266.07s; frontend typecheck and scoped diff check passed; production build passed with 2,799 modules in 1m50s and only existing sourcemap/large-chunk warnings.

### Final Execution Gate — 2026-08-30 — pass 2

- **Verdict:** SHIP
- **Reviewer:** `/root/final_gate_fila_inativos`
- **Backend digest:** `9efc8db7676634a23ce633a18aad29511a2d0d9eff28219e37b8fc2e1a88aa5d`
- **Frontend seven-file digest:** `be882b63c764dd252551257d2373ce9d42fbf88831e13da2736029d35bbe2d52`
- **Result:** no CRITICAL/HIGH execution gaps remain; nullable contact, tenant isolation, bounded backend CAS, frontend async lifecycle, polling, pagination, menu/route access and reference-state behavior are acceptance-complete with digest-bound tests/static/build evidence.
- **Residual QA:** confirm mobile overflow and native focus rendering in a real browser after merge.
- **Delivery hygiene:** isolate fila hunks on `wish/fila-inativos`, use a conventional commit and open a PR to `dev`; the current mixed branch/index must not be blanket-staged or committed.
- **Lifecycle:** keep `IN_PROGRESS` until isolated PR, authorized merge and required QA; only then transition to `SHIPPED`.

---

## Files to Create/Modify

```text
src/api/routes/sessions.py
src/api/schemas/sessions.py
src/repositories/session.py
src/services/session_service.py
tests/api/test_sessions.py
tests/api/test_webhook_uazapi.py
frontend/artifacts/neurostack/src/pages/inactive-queue.tsx
frontend/artifacts/neurostack/src/pages/inactive-queue.test.tsx
frontend/artifacts/neurostack/src/App.tsx
frontend/artifacts/neurostack/src/components/layout.tsx
frontend/artifacts/neurostack/src/components/layout.test.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
```
