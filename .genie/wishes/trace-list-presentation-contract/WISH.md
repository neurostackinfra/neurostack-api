# Wish: Restaurar o contrato visual da lista de traces

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `trace-list-presentation-contract` |
| **Date** | 2026-08-30 |
| **Author** | Lorenzo |
| **Appetite** | small |
| **Branch** | `wish/trace-list-presentation-contract` |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/trace-list-presentation-contract/DESIGN.md) |

## Summary

Restaurar a lista de traces conforme a referência `/mnt/c/Users/dell0/Downloads/image (5).png`: rótulo de intent e mensagem completos, cards alinhados e paleta idêntica à do painel. A correção fica protegida por uma única fonte frontend de apresentação de intents e por testes de contrato, sem alterar ou remover endpoints, schemas ou lógica backend.

## Scope

### IN

- Ampliar a área útil desktop da página de traces até o padrão `max-w-7xl` da referência.
- Exibir rótulos de intent e mensagens integrais, com quebra controlada e sem ellipsis, `truncate` ou `line-clamp`.
- Usar coluna de intent estável, badge ocupando a largura disponível e altura mínima uniforme que possa crescer com o conteúdo.
- Centralizar rótulo e paleta de intents em um catálogo frontend puro compartilhado por `IntentBadge` e dashboard.
- Preservar fallback neutro para intents novas/desconhecidas da API.
- Adicionar testes de contrato/componente e validar o pacote frontend completo.

### OUT

- Alterar classificador SRM, nomes canônicos, schemas, endpoints, payloads ou persistência de traces.
- Criar plataforma genérica de feature flags, serviço remoto de flags ou rollout por ambiente/tenant.
- Remover código backend isolado apenas porque uma capacidade ainda não está exposta na interface.
- Auditar ou remover uma capacidade backend concreta sem um fluxo ativo identificado; isso exige um wish próprio com seu limite de execução.
- Refazer filtros, exportação, paginação, detalhe do trace ou outros cards do dashboard.
- Fixar a altura máxima dos cards ou buscar equivalência pixel a pixel em toda viewport.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Remover truncagem e permitir quebra de linha | O conteúdo completo deve estar visível sem depender de hover. |
| 2 | Usar `max-w-7xl`, coluna desktop `md:w-56`, badge `w-full justify-center` e altura mínima `md:min-h-24` | Essas dimensões reproduzem a área de conteúdo da referência de 1408 px sem impor a geometria desktop a viewports estreitas nem impedir crescimento por conteúdo. |
| 3 | Fazer mensagem e metadados quebrarem/embrulharem em vez de cortar | Mantém desktop e mobile legíveis para valores longos. |
| 4 | Extrair metadados para `src/lib/intent-presentation.ts` | Os mapas duplicados em dashboard e trace já oferecem evidência concreta de drift. |
| 5 | Manter fallback neutro determinístico | Mudança backend aditiva não pode quebrar ou atribuir cor semântica inventada na UI. |
| 6 | Não alterar o backend nem criar flags gerais | A regressão pertence à apresentação; mecanismo de rollout sem segunda necessidade concreta seria especulativo. |
| 7 | Tratar ativação/remoção backend em wish próprio quando uma capacidade concreta alcançar o caminho ativo | A orientação fail-closed responde à dúvida de produto, mas não cria comentário, flag ou abstração sem comportamento atual que possa ser testado neste wish visual. |

## Simplicity Case

- **Simplest complete design:** ajustar classes da lista, centralizar os dois mapas já duplicados e testar os consumidores.
- **Added machinery:** um módulo frontend puro, exigido pela duplicação real entre `trace-viewer.tsx` e `dashboard.tsx`; não cria estado nem I/O.
- **Deferred until measured:** feature flags gerais somente quando duas ou mais capacidades independentes precisarem de rollout por ambiente/tenant e gates locais demonstrarem duplicação ou falha mensurável.
- **Complexity removed:** nenhum endpoint, payload visual, persistência, cache, sincronização, dependência ou estado de rollout novo.

## Dependencies

**depends-on:** trace-context-cards
**blocks:** none

## Success Criteria

- [ ] O nome/rótulo completo de toda intent fica visível no badge, inclusive `Dúvida Pós-Procedimento`, sem invadir a coluna de conteúdo.
- [ ] A mensagem completa de cada trace fica visível sem `truncate`, ellipsis ou `line-clamp` no elemento ou em ancestral que a oculte.
- [ ] Mensagens longas quebram em múltiplas linhas sem overflow horizontal, e o card inteiro continua navegável.
- [ ] Em área de conteúdo desktop larga, página, coluna de intent, badge e altura mínima seguem respectivamente `max-w-7xl`, `md:w-56`, `w-full justify-center` e `md:min-h-24`; o card pode crescer além da altura mínima.
- [ ] Em mobile, intent, data, mensagem, etapa, canal, duração e alertas permanecem legíveis e os metadados embrulham quando necessário.
- [ ] `pedido_preco`, `interesse_tratamento`, `agendar`, `pos_atendimento_duvida` e `reclamacao` preservam azul, violeta, verde, índigo e rosa em traces e painel.
- [ ] `IntentBadge` e dashboard consomem o mesmo catálogo de apresentação e não mantêm mapas de cores paralelos.
- [ ] Intent desconhecida exibe seu identificador com estilo neutro e não usa cor/layout vindo do backend.
- [ ] Nenhum arquivo de backend, schema, endpoint ou payload é alterado neste wish.
- [ ] Testes focados, suíte frontend completa, typecheck, build e `git diff --check` passam.

## Execution Strategy

### Wave 1 (sequential after `trace-context-cards`)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 1 — correção reincidente (+1); mesmo pacote, contrato determinístico e sem estado | `engineer-trivial` / low | Criar catálogo único de intent e migrar os dois consumidores com testes. |
| 2 | engineer | 3 — aceitação visual (+2) e correção reincidente (+1); sem estado ou contrato externo | `engineer-standard` / high | Restaurar largura, geometria, quebra e conteúdo integral da lista com testes. |

Group 2 começa após Group 1 para consumir a API final de `IntentBadge` e evitar edições concorrentes em seus testes. O wish inteiro só começa depois de `trace-context-cards`, que já possui mudanças locais em `trace-viewer.tsx` e `trace-viewer.test.tsx` que devem ser preservadas.

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** →
`engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high;
**4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an
independent `final-gate` at the highest justified effort. Codex maps these to
the `genie_*` profiles; other runtimes use their matching native roles. Keep
model and effort in runtime session/agent configuration, never skill frontmatter.

## Execution Groups

### Group 1: Fonte única de apresentação de intents

**Group ID:** intent-catalog

**Goal:** Eliminar o risco de divergência de rótulo/cor entre painel e traces sem mover decisões visuais para a API.

**Deliverables:**
1. `src/lib/intent-presentation.ts` com chave de tradução, classe base de fundo, complemento de badge (hover/texto) e cor hexadecimal, além de fallback neutro; barra e ponto reutilizam a classe base em vez de duplicá-la.
2. `IntentBadge` e `IntentBreakdown` migrados para o catálogo compartilhado, removendo `INTENT_CLASSNAMES`, `INTENT_META` e helpers de rótulo paralelos dos consumidores.
3. Suporte opcional de `className` em `IntentBadge`, mesclado pelo helper `cn`, para a lista aplicar largura/alinhamento sem duplicar a paleta.
4. Testes do catálogo, do badge e do dashboard fixando a paleta representativa, rótulos localizados, fallback e a fonte única.

**Acceptance Criteria:**
- [ ] Os cinco intents da referência resolvem para azul, violeta, verde, índigo e rosa nos tokens Tailwind e hexadecimais correspondentes já usados pelo painel.
- [ ] A união completa das intents hoje presentes em `INTENT_CLASSNAMES` e `INTENT_META` preserva a paleta atual; o teste de catálogo falha se uma chave for perdida ou remapeada sem decisão explícita.
- [ ] Trace e dashboard importam `intent-presentation.ts`; não existe mapa local paralelo de cor ou label nesses consumidores.
- [ ] `IntentBadge` aceita `className` opcional sem mudar o visual dos consumidores existentes.
- [ ] Intent desconhecida mantém identificador legível e tokens neutros.
- [ ] Nenhum dado de cor, CSS ou tradução é solicitado ao backend.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts \
  src/lib/intent-presentation.test.ts \
  src/components/trace-viewer.test.tsx \
  src/pages/dashboard-intents.test.tsx && \
pnpm --dir frontend/artifacts/neurostack run typecheck && \
PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build && \
git diff --check
```

Os testes focados comprovam o catálogo e os dois consumidores; typecheck valida a nova interface compartilhada e build valida a descoberta das classes Tailwind estáticas. Backend e E2E não são proporcionais porque o grupo permanece em um pacote frontend, sem schema, dependência, configuração ou fluxo de rede.

**depends-on:** none

---

### Group 2: Layout integral e responsivo da lista

**Group ID:** trace-list-layout

**Goal:** Fazer cada linha reproduzir o contrato da referência e mostrar integralmente intent e mensagem em desktop e mobile.

**Deliverables:**
1. `pages/traces.tsx` com área `max-w-7xl`, cards `md:min-h-24`, coluna `md:w-56`, badge `w-full justify-center` e contêineres sem corte.
2. Mensagem com quebra de palavra/linha e metadados com `flex-wrap`, preservando crescimento natural do card e navegação pelo link inteiro.
3. `pages/traces.test.tsx` cobrindo rótulo longo, mensagem integral, classes de geometria/quebra, fallback desconhecido e ausência de classes de truncagem/clamp nos nós relevantes.

**Acceptance Criteria:**
- [ ] `Dúvida Pós-Procedimento` e a mensagem associada aparecem integralmente sem depender de `title`/tooltip.
- [ ] Nenhum nó que contém badge ou mensagem, nem seus contêineres de clipping, usa `truncate`, `line-clamp` ou ellipsis.
- [ ] A página declara `max-w-7xl`; cada card pode crescer além de `md:min-h-24`; a coluna/badge usam `md:w-56` e `w-full justify-center` em desktop.
- [ ] Mensagem usa quebra segura inclusive para token sem espaços; metadados usam wrapping e continuam legíveis em largura estreita.
- [ ] Clique em qualquer ponto do card continua levando a `/traces/:id`, e filtros/paginação não mudam.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts \
  src/pages/traces.test.tsx && \
pnpm --dir frontend/artifacts/neurostack run typecheck && \
PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build && \
git diff --check
```

O teste de página prova conteúdo e contrato CSS nos nós responsáveis pela regressão; typecheck cobre React/props e build cobre Vite/Tailwind. O grupo não toca backend, dependências ou configuração de release, portanto suíte backend e E2E não são proporcionais.

**depends-on:** intent-catalog

---

## Aggregate Integration Gate

Depois dos dois grupos, executar no estado combinado:

```bash
pnpm --dir frontend/artifacts/neurostack test && \
pnpm --dir frontend/artifacts/neurostack run typecheck && \
PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build && \
git diff --check
```

Esse gate preserva a suíte completa documentada do pacote e valida os dois consumidores compartilhados no bundle final. Backend e Playwright permanecem fora porque nenhum contrato de rede, schema, dependência, rota ou fluxo browser novo é introduzido; a equivalência visual responsiva é verificada em QA.

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Em uma área de conteúdo de 1408 × 843 (recorte sem sidebar, não viewport inteiro), comparar com `image (5).png`: lista ocupa a largura ampla, cards têm coluna/badge alinhados e nenhuma intent ou mensagem está cortada.
- [ ] Uma mensagem de uma linha mantém cards visualmente uniformes; uma mensagem longa aumenta a altura somente do card necessário e permanece completa.
- [ ] Em viewport móvel, badge, data, mensagem e metadados empilham/embrulham sem overflow horizontal.
- [ ] Paleta das cinco intents visíveis é idêntica entre lista de traces e painel em tema claro e escuro.
- [ ] Intent desconhecida usa fallback neutro e o card continua abrindo o detalhe correto.
- [ ] Filtros, exportação, paginação e navegação de detalhe continuam funcionando como antes.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Mensagens longas produzem alturas diferentes | Low | Usar altura mínima, nunca altura fixa; a paginação existente limita a 20 itens. |
| Classes compartilhadas podem não entrar no CSS de produção | Medium | Manter strings Tailwind estáticas no catálogo e exigir build no gate de cada grupo e no agregado. |
| Extração altera cor de um intent não presente na referência | Medium | Migrar toda a tabela atual e testar os cinco intents visíveis mais fallback; revisão do diff verifica paridade restante. |
| Layout móvel sofre overflow após remover truncagem | Medium | Usar `min-w-0`, `break-words`, `overflow-wrap:anywhere` e `flex-wrap`; validar teste semântico e QA móvel. |
| `trace-context-cards` possui mudanças não commitadas nos mesmos arquivos de viewer/teste | High | Dependência explícita; executar somente após integrar esse wish e preservar suas alterações ao migrar o catálogo. |
| “Mesmo tamanho” pode ser confundido com altura fixa | Medium | Critério define largura/colunas/altura mínima consistentes e crescimento obrigatório quando o conteúdo exigir. |
| A largura de 1408 px da referência pode ser confundida com o viewport completo | Medium | QA mede a área de conteúdo recortada; sidebar e padding externo não entram na comparação. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Initial plan review — 2026-08-30T18:25:02Z

- **Reviewer:** `/root/review_trace_list_wish`
- **Target SHA-256:** `a64ba51c3aac759664e929fa98271c32325f039f33aa0cce57a9d4b944305899`
- **Staged Git blob:** `26e1daaa3e75338c4287fa32dae669027beae593`
- **Verdict:** SHIP on the pre-advisory draft.
- **Checks:** governing design evidence verified; placeholders absent; staged/worktree parity and diff checks passed; task IDs, scripts, manifest and validation scope inspected.

### Questioner advisory — 2026-08-30

- **HIGH addressed:** clarified that 1408 × 843 is the content crop rather than the full app viewport and moved fixed desktop geometry from `sm` to `md`.
- **MEDIUM addressed:** backend activation policy remains guidance/OUT, with no flag, comment-only mechanism or abstraction delivered by this visual wish.
- **MEDIUM addressed:** catalog avoids redundant bar/dot fields and must preserve the union of every currently mapped intent, not only the five visible examples.
- **MEDIUM addressed:** explicit group IDs now match the task/DAG identifiers; the two sequential groups remain because palette parity and list layout are independently testable failure modes.
- **Dependency confirmed:** `trace-context-cards` is a transient operational dependency because it owns current changes in `trace-viewer.tsx` and its test.

### Plan re-review — 2026-08-30T18:29:29Z

- **Reviewer:** `/root/review_trace_list_wish`
- **Target SHA-256:** `c20aa29537ad4904e09dfab40c9b866af1e90a121c1a5f1cabc6b1bf2c79a6ca`
- **Staged Git blob:** `6f76110fdcc6c16bb639ef03fa60b1a8cebd53d8`
- **Governing design reviewed-content SHA-256:** `c1fb39096e1e3ef15b444d967d710b3c5b03332395717e60c6d10dafed332cec`
- **Verdict:** SHIP.
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW.
- **Checks:** referência confirmada como recorte 1408 × 843; geometria `md` consistente; ativação backend somente OUT/follow-up; catálogo sem campos redundantes e com paridade das 29 intents; IDs/DAG e tasks conferidos; placeholders ausentes; design verify, staged/worktree parity e diff checks passaram; scripts e manifesto de oito arquivos validados estruturalmente.

### Group `intent-catalog` execution review — 2026-08-30T18:52:29Z

- **Reviewer:** `/root/review_intent_catalog`
- **Task:** `t_mtg4wv63e9eca799`
- **WISH SHA-256:** `337fddb2a37704df2e4be1c1e64c3035b5094b064fb5a8233b3be978353ce412`
- **Verdict:** FIX-FIRST.
- **HIGH:** o teste garantia as 29 chaves, mas congelava classe/hex apenas das cinco intents de referência; exigir contrato completo de `baseClassName`, `badgeClassName` e `hex` para todas as 29.
- **HIGH:** os dois arquivos novos em `src/lib` estavam ignorados por `.gitignore:21` e ausentes do índice; o orquestrador deve adicioná-los explicitamente com `git add -f`.
- **Positive evidence:** implementação preserva 29/29 badges e 25/25 entradas prévias do dashboard, mantém as cinco cores de referência, usa `cn`, fallback neutro e catálogo compartilhado sem mapa/helper paralelo; delta restrito aos seis arquivos frontend e preserva `trace-context-cards`.
- **Checks:** Vitest 3 arquivos/14 testes PASS; typecheck não incremental PASS; `git diff --check` PASS; build ficou para o gate do orquestrador. Hashes revisados: catálogo `4a62bd76…f1df2`, teste `34007bb6…96c`, viewer `4a90e679…cd9d`, teste viewer `121c598e…22e8`, dashboard `79b379d8…de20`, teste dashboard `e59deb42…67dd`.

### Group `intent-catalog` fix loop 1 and re-review — 2026-08-30T19:01:02Z

- **Fixer:** `/root/fix_intent_catalog_contract`; apenas `intent-presentation.test.ts` foi alterado para comparar `baseClassName`, `badgeClassName` e `hex` explicitamente nas 29 intents.
- **Fix validation:** teste do catálogo 3/3 PASS; typecheck PASS; `git diff --check` PASS.
- **Reviewer:** `/root/review_intent_catalog`
- **Task:** `t_mtg4wv63e9eca799`
- **WISH SHA-256:** `7ca5deac3505fa26e6fc6dd4bf2ffbb9e5455e66462916c8891942fcbdf2aff1`
- **Verdict:** SHIP; nenhum gap CRITICAL, HIGH, MEDIUM ou LOW.
- **Resolved:** contrato completo 29/29; os dois arquivos ignorados estão staged como `A` e conteúdo staged/worktree coincide.
- **Checks:** Vitest focado 3 arquivos/14 testes PASS; typecheck não incremental PASS; `git diff --cached --check` e `git diff --check` PASS; 29/29 badges e 25/25 hex do dashboard preservados. Build permanece gate do orquestrador.
- **Reviewed hashes:** catálogo `4a62bd76…f1df2`, teste `422893d8…e4f`, viewer `4a90e679…cd9d`, teste viewer `121c598e…22e8`, dashboard `79b379d8…de20`, teste dashboard `e59deb42…67dd`.

### Group `intent-catalog` quality review — 2026-08-30T19:04:15Z

- **Reviewer:** `/root/quality_intent_catalog`
- **Task:** `t_mtg4wv63e9eca799`
- **WISH SHA-256:** `1514ee837a4c5fc23c9678bd1daae3ce5921632d705d6ad9b9a04c25adb1286c`
- **Verdict:** FIX-FIRST.
- **HIGH:** lookups em objeto comum aceitavam nomes herdados (`constructor`, `__proto__`, `toString`) e burlavam o fallback neutro; exigir own-property/null-prototype e regressões no catálogo/badge.
- **MEDIUM:** chave de tradução aceitava cast não verificado, não estava congelada no fixture 29/29 e o fallback anunciava `intent_faq`; tipar o domínio, testar as 29 chaves e modelar fallback sem chave enganosa.
- **Positive evidence:** classes/hex são literais estáticos, sem CSS backend, segredo, I/O, estado, cache ou dependência; lookup O(1), baseline preservado e fixtures de cor independentes; diff checks passaram.
- **Hashes:** catálogo `4a62bd76…f1df2`, teste `422893d8…e4f`, viewer `4a90e679…cd9d`, teste viewer `121c598e…22e8`, dashboard `79b379d8…de20`, teste dashboard `e59deb42…67dd`.

### Group `intent-catalog` quality fix and re-review — 2026-08-30T19:15:41Z

- **Fixer:** `/root/fix_intent_catalog_quality`; único loop de qualidade usado.
- **Fix:** own-property guard para `constructor`/`__proto__`/`toString`; 29 translation keys literais e tipadas; fixture independente congela translation key/classes/hex 29/29; fallback desconhecido sem `intent_faq`; regressões no catálogo e badge.
- **Fix checks:** Vitest focado 3 arquivos/20 testes PASS; typecheck PASS; `git diff --check` PASS.
- **Reviewer:** `/root/quality_intent_catalog`
- **Task:** `t_mtg4wv63e9eca799`
- **WISH SHA-256:** `4d1945f3b15f0a712b0e0aed0fccc5e32f83e4a792d94501e47275a8456e2dcb`
- **Verdict:** SHIP; nenhum gap CRITICAL, HIGH, MEDIUM ou LOW.
- **Quality evidence:** fallback neutro/raw para nomes herdados; sem CSS backend, segredo, I/O, estado, mapa redundante ou abstração especulativa; tokens estáticos, lookup O(1), baseline de contexto preservado e staged/worktree idênticos.
- **Hashes:** catálogo `77342d48…7978`, teste `12ee81cf…720b`, viewer `4a90e679…cd9d`, teste viewer `0b835af3…c427`, dashboard `79b379d8…de20`, teste dashboard `e59deb42…67dd`.

### Group `intent-catalog` orchestrator validation — 2026-08-30T19:20:23Z

- **Command:** `pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/lib/intent-presentation.test.ts src/components/trace-viewer.test.tsx src/pages/dashboard-intents.test.tsx && pnpm --dir frontend/artifacts/neurostack run typecheck && PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build && git diff --check`
- **Outcome:** PASS, exit 0. Vitest: 3 arquivos, 20/20 testes. Typecheck: PASS. Build: 2.799 módulos, PASS em 1m17s. `git diff --check`: PASS silencioso.
- **Warnings não bloqueantes:** sourcemap sem localização original em componentes UI existentes e chunk principal acima de 500 kB; nenhum warning aponta para os seis arquivos do grupo.
- **Scope rationale:** testes focados cobrem catálogo e dois consumidores; typecheck valida o contrato compartilhado; build confirma descoberta de tokens Tailwind estáticos; diff isolado a frontend sem schema, dependência, rede ou backend, portanto backend/E2E não são proporcionais.

### Group `trace-list-layout` execution review — 2026-08-30T19:35:51Z

- **Reviewer:** `/root/review_trace_list_layout`
- **Task:** `t_mtg4wvcj7afce191`
- **WISH SHA-256:** `ff1ae303b30c514db7b7d3f754d31cb80e719856c5567c602d72adf2f0aa051d`
- **Verdict:** SHIP; nenhum gap CRITICAL, HIGH, MEDIUM ou LOW.
- **Evidence:** `max-w-7xl`, `md:min-h-24`, `md:w-56`, badge `w-full justify-center`, crescimento natural, mensagem com wrap seguro e metadados `flex-wrap`; sem clipping/truncagem nos ancestrais relevantes; rótulo/mensagem PT, token sem espaços, fallback neutro e rota real `/traces/long-trace` exercitados; filtros, offset de paginação e query de exportação preservados.
- **Checks:** Vitest sem cache 1 arquivo/2 testes PASS; typecheck PASS; staged diff-check e auditoria de escopo PASS. Build permanece gate do orquestrador.
- **Hashes:** baseline `f61062a3…fa7`; traces `275d2392…829a`; teste `0095315c…ec3`.

### Group `trace-list-layout` quality review — 2026-08-30T19:39:54Z

- **Reviewer:** `/root/quality_trace_list_layout`
- **Task:** `t_mtg4wvcj7afce191`
- **WISH SHA-256:** `c1a0a068…3aa67`
- **Verdict:** SHIP; nenhum gap CRITICAL ou HIGH.
- **MEDIUM não bloqueante:** `Badge` herda `whitespace-nowrap`; um identificador backend desconhecido excepcionalmente longo pode ultrapassar a coluna. Labels conhecidas e `future_intent` satisfazem o contrato atual; hardening futuro pode aplicar wrap local e testar token desconhecido longo.
- **Evidence:** sem height/clipping conflitante; link nativo cobre todo o card e preserva teclado/rota; strings React-escaped, CSS somente do catálogo own-property; custo limitado a 20 linhas; testes independentes cobrem conteúdo, geometria, ancestrais, fallback, navegação, filtros, paginação e export; Group 1 intacto e staged/worktree idênticos.
- **Hashes:** baseline `f61062a3…fa7`, traces `275d2392…829a`, teste `0095315c…ec3`.

### Group `trace-list-layout` orchestrator validation — 2026-08-30T19:45:00Z

- **Command:** `pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/traces.test.tsx && pnpm --dir frontend/artifacts/neurostack run typecheck && PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build && git diff --check`
- **Outcome:** PASS, exit 0. Vitest: 1 arquivo, 2/2 testes. Typecheck: PASS. Build: 2.799 módulos, PASS em 1m21s. `git diff --check`: PASS silencioso.
- **Warnings não bloqueantes:** sourcemap sem localização original em componentes UI existentes e chunk principal acima de 500 kB; nenhum warning aponta para os dois arquivos do grupo.
- **Scope rationale:** teste de página cobre conteúdo, CSS semântico, navegação e controles; typecheck cobre props/React; build cobre Vite/Tailwind; mudança limitada a dois arquivos frontend, sem rede, schema, dependência ou backend, portanto backend/E2E não são proporcionais.

### Aggregate integration gate — 2026-08-30T19:53:42Z

- **Literal command:** `pnpm --dir frontend/artifacts/neurostack test && pnpm --dir frontend/artifacts/neurostack run typecheck && PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build && git diff --check`
- **Outcome:** FAIL no primeiro passo; Vitest: 11 arquivos PASS, 1 arquivo FAIL; 77/78 testes PASS.
- **Wish-owned coverage:** todos os 22 testes de catálogo/viewer/dashboard/traces passaram dentro da suíte agregada.
- **Sole failure:** `clinic-config-api-settings.test.tsx:120` esperava texto PT (`configurações de API existentes são preservadas`) e recebeu texto EN (`Existing API settings are preserved...`). O arquivo não pertence ao manifesto deste wish.
- **Isolation:** execução isolada do arquivo reproduziu exatamente 1/5 falha e 4/5 PASS, descartando contaminação entre arquivos desta suíte; a mesma dívida de locale já era documentada antes desta execução. `en.ts`/`pt.ts` já tinham mudanças preexistentes no início do work e não pertencem aos grupos deste wish.
- **Remaining chain:** o `&&` corretamente impediu typecheck/build/diff-check nesta tentativa literal. No estado combinado de código, Group 2 já havia executado typecheck PASS, build 2.799 módulos PASS e diff-check PASS às 19:45:00Z; nenhuma implementação mudou depois disso.
- **Disposition:** gate agregado literal permanece vermelho; nenhuma correção fora de escopo foi aplicada. O estado segue `IN_PROGRESS` para revisão final decidir o handoff.

### Final execution review — 2026-08-30T16:57:35-03:00

- **Reviewer:** `/root/final_execution_review_trace_list`
- **HEAD:** `24f14ff5ddd86dfdfc56bc54d632228921112062`
- **WISH SHA-256:** `c8c180efa0370b4b93ab357feb7e8443438a79b50dec397ffa133d2c0d32881d`
- **Manifest diff SHA-256:** `058fa9611a634da9b03863581ce51c34c021f7b179f40035ee5625afe285b9e2`
- **Verdict:** BLOCKED.
- **Implementation:** completa e correta dentro do contrato; catálogo único 29/29, fallback own-property, geometria/wrap/link integral, somente os oito arquivos frontend previstos, staged/worktree idênticos e diff checks PASS.
- **HIGH external blocker:** gate agregado obrigatório 77/78; única falha de locale em `clinic-config-api-settings.test.tsx:120` impede handoff apesar de não ser causada pelo wish.
- **MEDIUM nonblocking:** identificador desconhecido excepcionalmente longo pode ultrapassar a coluna por `Badge whitespace-nowrap`; hardening futuro.
- **Non-regression:** `clinic-config-api-settings.test.tsx`, `clinic-config.tsx` e `context/i18n.tsx` são byte-idênticos ao HEAD; reprodução isolada falha 1/5 do mesmo modo; todos os 22 testes do wish passam; typecheck/build/diff do código combinado passam.
- **Corrective route:** proprietário da dívida de API settings corrige o teste/locale em escopo próprio; depois repetir o comando agregado literal sobre os mesmos hashes e reexecutar a revisão final. Este wish permanece `IN_PROGRESS` e não segue para PR.

---

## Files to Create/Modify

```
frontend/artifacts/neurostack/src/lib/intent-presentation.ts
frontend/artifacts/neurostack/src/lib/intent-presentation.test.ts
frontend/artifacts/neurostack/src/components/trace-viewer.tsx
frontend/artifacts/neurostack/src/components/trace-viewer.test.tsx
frontend/artifacts/neurostack/src/pages/dashboard.tsx
frontend/artifacts/neurostack/src/pages/dashboard-intents.test.tsx
frontend/artifacts/neurostack/src/pages/traces.tsx
frontend/artifacts/neurostack/src/pages/traces.test.tsx
```
