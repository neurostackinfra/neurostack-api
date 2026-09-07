# Wish: Consolidar fontes do contexto selecionado em dois cards

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `trace-context-cards` |
| **Date** | 2026-09-06 |
| **Author** | Codex + usuário |
| **Appetite** | small |
| **Branch** | `wish/trace-context-cards` |
| **Repos touched** | neurostack-api (frontend workspace) |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Reorganizar o painel “Contexto Selecionado” do visualizador de traces para exibir somente dois cards de origem: um card fixo de RAG e um card de Configuração da Clínica que reúne, em lista, todos os tópicos de configuração consultados. O card de RAG permanece visível mesmo sem consulta e apresenta “Não utilizado”, eliminando a repetição atual de um card completo por tópico e aproximando a interface da referência visual fornecida. O wish é direto, sem brainstorm, porque a referência e o componente existente delimitam uma única mudança de apresentação com critérios determinísticos.

## Scope

### IN

- Agrupar os itens de `trace.selectedContext` por origem normalizada (`rag` e `config`) antes da renderização.
- Renderizar sempre dois cards de origem, RAG e Configuração da Clínica, em duas colunas no desktop e empilhados em telas menores.
- Reproduzir a anatomia da referência: cabeçalho próprio por card com ícone e rótulo de origem, divisor e corpo com a lista de tópicos em pares título/valor.
- Renderizar dentro de cada card os tópicos da respectiva origem, preservando a ordem recebida e o par título/valor de cada item.
- Exibir a mensagem localizada “Não utilizado” / “Not used” no card de RAG quando não houver item RAG e no card de configuração quando não houver tópico de configuração, mantendo o layout estável mesmo com `selectedContext` vazio.
- Preservar itens de origem desconhecida em um fallback neutro e compacto fora da grade dos dois cards, sem ocultá-los nem atribuir uma origem incorreta.
- Atualizar os testes de `TraceViewer` para cobrir agrupamento, múltiplos tópicos, estados parcialmente e totalmente vazios, responsividade declarativa, fallbacks de título e traduções PT/EN.

### OUT

- Alterar o contrato ou a geração de `context_sources` no backend, inclusive o filtro de relevância e a ordem dos itens.
- Alterar o mapeamento dos dados de trace em `trace-detail.tsx` ou `chat.tsx`.
- Criar um terceiro card para origens desconhecidas ou reclassificá-las como RAG/configuração.
- Redesenhar os demais cards do visualizador de traces, o Developer Payload ou outras páginas.
- Adicionar expansão, paginação, truncamento, limite de tópicos ou novas dependências de UI.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Os cards representam origens, e os tópicos viram itens internos | Atende diretamente ao pedido de eliminar um card de Configuração da Clínica por tópico sem perder título ou valor. |
| 2 | RAG e Configuração da Clínica ficam sempre visíveis | Garante a composição estável de dois cards da referência e torna explícita a ausência de uso de uma origem. |
| 3 | A chave localizada existente `ctx_empty` passa a representar o estado vazio dentro dos cards | “Não utilizado” comunica ausência de consulta de forma curta e consistente sem criar uma chave de tradução redundante. |
| 4 | A partição usa `normalizeContextSource` e preserva a ordem original dentro de cada grupo | Mantém compatibilidade com variantes de caixa/espaço e evita reordenação arbitrária dos tópicos. |
| 5 | Origens desconhecidas ficam em um fallback neutro fora da grade, sem criar outro card | O backend documenta `config`/`rag`, mas o frontend já é defensivo; preservar o conteúdo evita regressão e rotulagem incorreta para usuários sem Developer Payload. |
| 6 | Cada card usa cabeçalho separado com ícone de origem, divisor e corpo, conforme a referência | Torna a fidelidade visual verificável e mantém a hierarquia “origem → tópicos” legível. |
| 7 | A hierarquia visual usa componentes, ícones Lucide e tokens atuais | A mudança exige somente reorganização do JSX e classes existentes, sem um novo componente genérico ou dependência. |

## Simplicity Case

- **Simplest complete design:** particionar o array já recebido em duas listas válidas e uma lista defensiva, renderizar as duas primeiras em containers fixos com cabeçalho/corpo e mostrar a terceira como fallback neutro somente se existir.
- **Added machinery:** seletores de teste estáveis por card/tópico e um fallback compacto para origens desconhecidas; o primeiro verifica a anatomia solicitada e o segundo preserva conteúdo que a interface atual já exibe. A cópia vazia reutiliza `ctx_empty`.
- **Deferred until measured:** colapso, paginação ou limite de tópicos só serão considerados se traces reais produzirem listas que prejudiquem a leitura ou o tempo de renderização, comprovado em QA/telemetria.
- **Complexity removed:** desaparecem o card por item, o estado vazio global alternativo, uma tradução redundante e a possibilidade de quantidade variável de cards para origens válidas; não há estado React, cache, API ou dependência adicional.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [ ] O painel “Contexto Selecionado” renderiza exatamente dois cards para dados válidos: RAG e Configuração da Clínica.
- [ ] Cada card possui cabeçalho separado por divisor, ícone e rótulo de origem; o corpo contém os tópicos ou o estado “Não utilizado”, seguindo a hierarquia da referência visual.
- [ ] O card RAG é renderizado mesmo quando nenhum item RAG foi usado e mostra “Não utilizado” em português ou “Not used” em inglês.
- [ ] Todos os itens `config`/`clinic_config`, incluindo variantes com caixa e espaços, aparecem dentro de um único card de Configuração da Clínica.
- [ ] Vários itens RAG aparecem dentro do único card RAG, sem criar cards adicionais.
- [ ] Cada tópico mantém seu título resolvido, valor integral, quebra de linha/palavra e ordem relativa da resposta da API.
- [ ] Quando uma origem não tem itens, somente essa origem mostra o estado “Não utilizado”; a outra continua listando seus tópicos.
- [ ] Quando `selectedContext` está vazio ou ausente, ambos os cards continuam visíveis e mostram “Não utilizado”.
- [ ] A grade usa uma coluna em telas pequenas e duas colunas a partir do breakpoint atual de desktop.
- [ ] Uma origem desconhecida permanece legível em um fallback neutro fora da grade, não é apresentada como RAG/Configuração da Clínica e não cria um terceiro card.
- [ ] Os fallbacks existentes de título (`title`, campo/chave traduzido e fallback terminal) continuam funcionando dentro das listas agrupadas.
- [ ] Os testes focados do `TraceViewer`, o typecheck e o build do app passam.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — aceitação visual, limitada a um componente e seus testes | engineer-standard / medium | Agrupar as fontes, implementar os dois cards fixos, localizar o estado vazio e atualizar a cobertura. |

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

### Group 1: Agrupar e renderizar as fontes do contexto

**Goal:** Substituir os cards por item por dois cards fixos de origem sem perder o conteúdo consultado nem a localização PT/EN.

**Deliverables:**
1. `frontend/artifacts/neurostack/src/components/trace-viewer.tsx` — partição dos contextos e renderização dos cards fixos RAG/Configuração da Clínica com listas internas e estado vazio localizado.
2. `frontend/artifacts/neurostack/src/components/trace-viewer.test.tsx` — testes atualizados para o novo agrupamento, estados vazios, compatibilidade de origens, conteúdo, ordem e layout responsivo.
3. `frontend/artifacts/neurostack/src/locales/pt.ts` e `frontend/artifacts/neurostack/src/locales/en.ts` — atualização da chave existente `ctx_empty` para “Não utilizado” / “Not used”.

**Acceptance Criteria:**
- [ ] Um trace com múltiplos contextos config e RAG produz somente `context-card-rag` e `context-card-config`, cada qual contendo todos e somente os itens de sua origem em ordem.
- [ ] Cada card expõe cabeçalho separado, divisor, ícone Lucide coerente com a origem e corpo; cada tópico interno mantém título e valor como elementos distintos.
- [ ] Os cards permanecem presentes quando uma ou ambas as coleções estão vazias; cada coleção vazia exibe a tradução atualizada de `ctx_empty`.
- [ ] Itens config aceitam `config` e `clinic_config` com normalização de caixa/espaço; itens RAG aceitam a mesma normalização.
- [ ] Itens com origem desconhecida aparecem em um fallback neutro fora da grade, sem gerar card adicional nem receber rótulo de RAG/configuração.
- [ ] Títulos usam a cadeia de fallback existente e valores continuam completos, com whitespace e palavras longas quebrando sem truncamento.
- [ ] O container mantém `grid-cols-1` e `md:grid-cols-2`; a quantidade de cards válidos não depende da quantidade de tópicos.
- [ ] Rótulos, estado vazio e fallbacks relevantes funcionam em português e inglês.
- [ ] Nenhuma mudança é feita no contrato backend ou nos adaptadores `trace-detail.tsx`/`chat.tsx`.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/components/trace-viewer.test.tsx \
  && pnpm --dir frontend/artifacts/neurostack typecheck \
  && pnpm --dir frontend/artifacts/neurostack build
```

O teste focado comprova a transformação e os estados visuais; typecheck e build cobrem os limites TypeScript/Vite alcançados pelo componente e pelas traduções sem escalar para o backend, que permanece fora do diff.

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Abrir um trace que usou vários tópicos da configuração e confirmar um único card “Configuração da Clínica” com todos os tópicos na ordem recebida.
- [ ] Abrir um trace que usou RAG e configuração e confirmar somente os dois cards, cada item na origem correta.
- [ ] Abrir um trace sem RAG e confirmar o card RAG fixo com “Não utilizado”, ao lado do card de configuração no desktop.
- [ ] Abrir um trace sem contexto e confirmar os dois cards, ambos com “Não utilizado”, sem o antigo estado vazio global.
- [ ] Em fixture com origem desconhecida, confirmar que o conteúdo permanece legível num fallback neutro fora da grade e que ainda existem somente dois cards.
- [ ] Alternar PT/EN e confirmar cabeçalhos, fallbacks e “Não utilizado” / “Not used”.
- [ ] Conferir viewport móvel (cards empilhados) e desktop (dois cards lado a lado), incluindo valores multilinha e palavras longas sem corte.
- [ ] Confirmar que SRM Decision, Developer Payload e RatingBlock permanecem visual e funcionalmente inalterados.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| A referência diz que haverá dois cards, mas explicita o vazio somente para RAG | Low | O wish adota dois cards sempre fixos e usa o mesmo estado vazio no card de configuração; isso preserva o layout e evita um card sem conteúdo. |
| Um trace legado pode conter origem fora de `config`/`rag` | Low | Preservar título/valor em fallback neutro fora da grade, sem rotular incorretamente nem criar terceiro card. |
| Muitos tópicos podem aumentar a altura do card de configuração | Low | Preservar todo o conteúdo agora; só adicionar colapso/limite após evidência concreta de problema de leitura ou performance. |
| Mudanças nos `data-testid` quebrarão expectativas antigas | Low | Atualizar a suíte focada para testar cards por origem e itens internos, priorizando comportamento em vez de índices globais. |

---

## Review Results

### Plan review — 2026-09-06T12:49:58Z — SHIP

- **Context:** PLAN REVIEW, re-review after the questioner-lens findings were incorporated.
- **Target:** `.genie/wishes/trace-context-cards/WISH.md`
- **Reviewed content SHA-256:** `3696aa291ed8b5d6733add85fbb5aca5df0e56e5d91e7946a9cbdb31b47fe31f`
- **Repository HEAD:** `d5ee36ac3c76f555a2b538f37194b97919b815cc`
- **Evidence:** complete wish inspection and structural checks passed; all required sections, one execution group, non-zero validation, dependency tags, five OUT bullets and no placeholders were present. Repository inspection confirmed `ctx_empty` is limited to `TraceViewer` plus the PT/EN locale definitions, and the planned Vitest/typecheck/build commands exist.
- **Validation availability:** the repository has no root `package.json` or `wishes:lint` script, so the official wish linter was unavailable; independent structural validation passed. Runtime validation was not run during this read-only plan review because the build may write artifacts.
- **Questioner lens:** SHIP after the plan made the visual anatomy explicit, reused `ctx_empty`, preserved unknown-origin content in a neutral non-card fallback and justified the direct-wish route.
- **Gaps:** CRITICAL none; HIGH none; MEDIUM none; LOW none.
- **Verdict:** **SHIP**.

### Group 1 execution review — 2026-09-06 — FIX-FIRST → SHIP (fix loop 1/2)

- **Initial acceptance review (2026-09-06T13:06:07Z):** **FIX-FIRST**. HIGH: `ctx_empty` still contained the old PT/EN copy, so the new empty-state assertions could not pass. MEDIUM: validation evidence was not reproducible in the reviewed tree and the locale diff also contained `investor_sales_cta`, traced to concurrent `menu-demo` work.
- **Initial quality review (2026-09-06T13:06:15Z):** **FIX-FIRST**. Confirmed the HIGH i18n gap and requested explicit coverage for an omitted `selectedContext` plus semantic origin sections named for assistive technology.
- **Fix loop 1:** updated `ctx_empty` to `Não utilizado` / `Not used`; made each fixed card a named `section` linked to an internal `h3`; added accessible-role assertions and the omitted-`selectedContext` fixture. The concurrent `investor_sales_cta` hunks were preserved unchanged and are not attributed to this group.
- **Acceptance re-review (2026-09-06T13:30:24Z):** **SHIP**, with all Group 1 criteria confirmed and no CRITICAL/HIGH/MEDIUM/LOW gaps. Reviewed hashes: `trace-viewer.tsx` `f3cd9baa6076b10caacd9f4d12e4b4a14b2464aed39405cc1e3f721ed60b63d7`; `trace-viewer.test.tsx` `bd5550174bd3231d5111a4a1c95ce6808921138b4a1e113c417fb066a1716813`; `pt.ts` `4af10b17809914d49e4eda8b40b2d7bdfa79b021787a34cca1d6150234404e8f`; `en.ts` `a81ab26e544329eb3d5a65b67af97a4cf896343ff75086bd501c87559a3c9b52`.
- **Quality re-review (2026-09-06T13:28:39Z):** **SHIP**, zero blocking gaps. The only non-blocking checkout observation is that `investor_sales_cta` must be staged with its owning `menu-demo` work, not this group.

### Group 1 orchestrator validation — 2026-09-06T13:37:30Z — PASS

- `pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/components/trace-viewer.test.tsx` — PASS, 17/17 tests.
- `pnpm --dir frontend/artifacts/neurostack typecheck` — PASS, exit 0.
- `PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack build` — PASS, exit 0, 2,804 modules transformed. Existing sourcemap and chunk-size warnings remained non-blocking.
- `git diff --check -- <four Group 1 files>` — PASS, exit 0.
- **Validation scope:** focused component behavior plus the affected app's TypeScript and Vite production boundaries is proportional to this frontend-only change; backend and API adapters remained out of scope and unchanged by the group.
- **Command correction:** the literal build command in the plan is not self-contained because `vite.config.ts` requires `PORT` and `BASE_PATH`; the configured invocation above exercises the same production build successfully.
- **Task state:** `t_mtpsw1wc5673c542` marked `done` only after both SHIP verdicts and the passing orchestrator gate.

### Main integration validation — 2026-09-06T13:55:29Z — PASS

- **Base:** `origin/main` at `17be12b9dbc9ea5cc802a545037f600474885fd7`.
- The context-card implementation was integrated onto the latest main without reverting the already-merged five-intent badge behavior and risk-marker tests.
- Direct workspace binaries were used because `pnpm` attempted an unnecessary install in the isolated worktree and its global store database was unavailable in the sandbox.
- `vitest run --config vitest.config.ts src/components/trace-viewer.test.tsx` — PASS, 19/19 tests, including the two main-only intent/risk-marker cases.
- `tsc -p tsconfig.json --noEmit` — PASS, exit 0.
- `PORT=3001 BASE_PATH=/ vite build --config vite.config.ts` — PASS, exit 0, 2,804 modules transformed; known sourcemap and chunk-size warnings remained non-blocking.
- `investor_sales_cta` and every unrelated working-tree change were excluded from this isolated main patch.

---

## Files to Create/Modify

```
frontend/artifacts/neurostack/src/components/trace-viewer.tsx       (modify: group context topics into two fixed cards)
frontend/artifacts/neurostack/src/components/trace-viewer.test.tsx  (modify: cover grouping, empty states, i18n, order, wrapping)
frontend/artifacts/neurostack/src/locales/pt.ts                      (modify: update existing empty copy for per-card use)
frontend/artifacts/neurostack/src/locales/en.ts                      (modify: update existing empty copy for per-card use)
```
