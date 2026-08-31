# Wish: Cards de contexto do trace

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `trace-context-cards` |
| **Date** | 2026-08-29 |
| **Author** | Lorenzo |
| **Appetite** | small |
| **Branch** | `wish/trace-context-cards` |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/trace-context-cards/DESIGN.md) |

## Summary

Redesenhar o painel “Contexto Selecionado” do detalhe do trace como cards responsivos que deixam explícitos a origem, o título e o conteúdo usado pela IA. Além de alinhar a interface à referência visual fornecida, a mudança corrige a classificação case-sensitive que hoje pode mostrar uma origem `RAG` em maiúsculas como se fosse configuração da clínica.

## Scope

### IN

- Substituir os blocos genéricos do painel de contexto em `TraceViewer` por cards com cabeçalho de fonte, ícone, título e conteúdo.
- Normalizar origens com trim e lowercase, reconhecendo `rag` e a origem canônica `config`, aceitando `clinic_config` como alias de compatibilidade e usando apresentação neutra para valor vazio/desconhecido.
- Usar o `title` não vazio como título do card, o rótulo traduzido de `field/key` como fallback e, se `title`, `field` e `key` estiverem ausentes ou em branco, o título neutro localizado “Contexto” / “Context”, preservando o conteúdo integral com quebra de texto.
- Renderizar uma coluna por padrão e duas a partir do breakpoint `md`, mantendo a ordem e esticando os cards da mesma linha.
- Atualizar título e rótulos do painel em português e inglês, incluindo a origem desconhecida.
- Adicionar testes de componente para classificação, conteúdo, fallbacks, ordem, contrato responsivo e estado vazio.

### OUT

- Alterar seleção, ranking, quantidade, conteúdo ou ordenação do contexto produzido pelo backend.
- Alterar endpoint, schema da API, banco, pipeline RAG ou configuração da clínica.
- Redesenhar outros painéis do `TraceViewer`, a página de detalhe inteira ou o simulador de chat.
- Adicionar dependência visual, imagem ao bundle, equivalência pixel a pixel ou interação de expandir/copiar/filtrar.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Normalizar a origem dentro de `TraceViewer` | API, demo e simulador montam o mesmo tipo por caminhos diferentes; o limite compartilhado corrige todos sem duplicação nos mapeadores. |
| 2 | Resolver três estados: `rag`, `config` e `unknown`, mapeando `clinic_config` para `config` como compatibilidade | `config` é o valor canônico da API; o alias preserva traces legados e o fallback neutro evita atribuição falsa. |
| 3 | Comunicar fonte com ícone e texto | Preserva acessibilidade e a hierarquia da referência sem depender somente de cor. |
| 4 | Preferir `title`, cair para o rótulo de `field/key` e por fim para “Contexto” / “Context” | Exibe nomes amigáveis quando disponíveis sem regredir traces que só têm o campo técnico nem deixar sem título os itens sem identificador. |
| 5 | Grid CSS `grid-cols-1 md:grid-cols-2` | Entrega a composição em dois cards da referência e uma leitura móvel correta sem estado ou medição JavaScript. |
| 6 | Manter a mudança no módulo existente | Existe um único consumidor visual; extrair abstração agora adicionaria navegação sem reuso comprovado. |

## Simplicity Case

- **Simplest complete design:** trocar somente o mapa visual atual por um grid responsivo e uma função pura de normalização de origem no mesmo módulo.
- **Added machinery:** apenas o estado visual `unknown` e suas traduções, exigidos para não rotular origens futuras ou inválidas como configuração da clínica.
- **Deferred until measured:** extrair componente reutilizável somente quando houver segundo consumidor real; incluir expandir/copiar/filtrar somente após uma história de usuário aprovada.
- **Complexity removed:** nenhum estado React novo, fetch, persistência, alteração de contrato, dependência, configuração, tooltip ou lógica duplicada nos mapeadores.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [x] O painel mostra “Contexto Selecionado” em PT e “Selected Context” em EN.
- [x] Origens `RAG`/`rag` e a canônica `CONFIG`/`config`, inclusive com espaços externos, recebem rótulos e ícones corretos; `CLINIC_CONFIG`/`clinic_config` e suas variantes de espaços recebem a mesma classificação de configuração como alias de compatibilidade.
- [x] Origem vazia ou desconhecida usa rótulo/ícone neutros e nunca aparece como configuração da clínica.
- [x] Dois itens aparecem na ordem recebida com seus títulos e conteúdos completos; título vazio cai para o rótulo de `field/key`, e `title`, `field` e `key` todos ausentes ou em branco caem para “Contexto” em PT e “Context” em EN.
- [x] O grid declara uma coluna por padrão e duas a partir de `md`, e textos longos quebram sem truncamento intencional.
- [x] Lista vazia continua mostrando a mensagem localizada existente.
- [x] Fontes são distinguíveis por texto e ícone e os cards reutilizam tokens compatíveis com temas claro e escuro.
- [x] Teste focado, typecheck e build do app frontend passam.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — aceitação visual subjetiva; sem estado, multi-package ou contrato externo | `engineer-standard` / medium | Implementar cards, traduções e testes do painel compartilhado. |

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

### Group 1: Redesenhar e testar o contexto selecionado

**Goal:** Tornar a origem e o conteúdo de cada contexto auditáveis no detalhe do trace, reproduzindo a hierarquia da referência visual sem alterar dados ou contratos.

**Deliverables:**
1. `trace-viewer.tsx` com normalizador puro de origem e cards responsivos RAG, configuração canônica (`config`, com alias `clinic_config`) e desconhecido.
2. `en.ts` e `pt.ts` com título/rótulos do novo painel, origem desconhecida e fallback final neutro “Context” / “Contexto”.
3. `trace-viewer.test.tsx` com cobertura das variantes de origem, ordem, título/conteúdo, cadeia de fallback, grid e estado vazio.

**Acceptance Criteria:**
- [x] `RAG`/`rag` e `CONFIG`/`config` são classificados corretamente após trim/lowercase; `CLINIC_CONFIG`/`clinic_config` e variantes com espaços são classificados como `config` por compatibilidade.
- [x] Origem ausente ou nova usa a variante neutra e não o rótulo da clínica.
- [x] Cada card contém ícone e texto de origem, título amigável, fallback de campo ou o fallback final localizado “Contexto” / “Context”, e valor integral.
- [x] O teste focado cobre `title`, `field` e `key` simultaneamente ausentes ou em branco em PT e EN.
- [x] A ordem da lista não muda; o container usa `grid-cols-1 md:grid-cols-2` e o card permite quebra de texto longo.
- [x] PT e EN exibem título do painel, fontes e estado vazio localizados.
- [x] Nenhum mapper, tipo gerado, endpoint ou arquivo backend é alterado.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/components/trace-viewer.test.tsx && \
pnpm --dir frontend/artifacts/neurostack run typecheck && \
PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build && \
git diff --check
```

O Vitest focado comprova o comportamento e a semântica do painel; typecheck cobre o contrato React/i18n e o build alcança a integração Vite/Tailwind em produção. A mudança é isolada a um componente e traduções, sem dependência, schema, core compartilhado ou release config, portanto a suíte backend e E2E não são proporcionais a este grupo; `git diff --check` preserva o gate de higiene do repositório.

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Em PT e EN, abrir um trace com RAG e configuração mostra dois cards alinhados à referência, com origem, título e conteúdo legíveis.
- [ ] Em viewport móvel os cards ficam em uma coluna; em desktop ficam em duas, preservando ordem e sem overflow de nomes/descrições longos.
- [ ] Tema escuro mantém contraste e as fontes continuam distinguíveis sem depender apenas de cor.
- [ ] Trace sem contexto mantém o estado vazio e os demais painéis do detalhe permanecem inalterados.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| A referência não define móvel nem modo escuro | Medium | Usar grid responsivo e tokens de tema existentes; verificar ambos manualmente em QA. |
| `source`, `title`, `field`, `key` ou `value` podem chegar vazios | Medium | Resolver origem neutra e cadeia de fallback segura; cobrir variantes de `config`/`clinic_config` e `title`/`field`/`key` simultaneamente ausentes ou em branco no teste. |
| Textos longos podem produzir alturas diferentes | Low | Esticar cards da mesma linha, quebrar palavras e preservar altura guiada pelo conteúdo. |
| `en.ts` e `pt.ts` já têm mudanças locais de outra wish | Medium | Acrescentar somente as chaves deste painel e preservar/acomodar edições concorrentes sem sobrescrever o arquivo. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan review — 2026-08-30T03:01:11Z

- **Reviewer:** `/root/review_trace_context_wish`
- **Target SHA-256:** `0c7d01543fc63454969d053eb72241abbcca0b4eea167fce02caa97903cc49e8`
- **Governing design reviewed-content SHA-256:** `2b3035e03f34e9a60c7c86df00b4eb0c8cdc1966c02bc964372bc981edbf09da`
- **Verdict:** FIX-FIRST
- **HIGH:** o plano não reconhecia a origem canônica `config` emitida pelo pipeline/API e poderia reclassificá-la como desconhecida; corrigir design e wish para `config`, mantendo `clinic_config` como alias, e testar caixa/espaços.
- **MEDIUM:** faltava o fallback terminal quando `title`, `field` e `key` estivessem todos ausentes ou em branco; definir e testar “Contexto” / “Context”.
- **Checks:** blobs staged e SHA-256 capturados; evidência do design verificada; `git diff --cached --check` passou; nenhum placeholder foi encontrado. Testes/build futuros não foram executados em revisão read-only.

### Plan re-review — 2026-08-30T03:21:33Z

- **Reviewer:** `codex:/root/review_trace_context_wish_v2`
- **Target SHA-256:** `ecb59265ee415a017a9c00a5a5844b26133d48720db45c5f555bccf81e11ebe0`
- **Staged Git blob:** `c48e7b86c5ad92bf3352217644c46049ca8a2a33`
- **Governing design reviewed-content SHA-256:** `4a75d7c723e857bc35432e0704e6108a59c8e0bfa0ca811641b40f85386ca686`
- **Verdict:** SHIP
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW.
- **Checks:** design verificado e digest recomputado; artefatos staged iguais ao worktree; `git diff --cached --check` passou; contratos reais de `config`, `CLINIC_CONFIG`, mapeadores e `TraceViewer` inspecionados. Testes/typecheck/build permanecem gates de execução, pois o teste ainda não existe e o build escreve em `dist/public`.

### Group `panel` execution review — 2026-08-30T14:26:22Z

- **Reviewer:** `/root/review_trace_context_execution`
- **Task:** `t_mtf7wq3h418534d1`
- **Verdict:** SHIP
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW.
- **Scoped SHA-256:** `trace-viewer.tsx` `20d98d3bec6486b55c30ec89a015cdb53918432fe2c2c5c8d0e027498f0476ec`; `trace-viewer.test.tsx` `e69048bf987894bbb5df85a82b9fa268b9d60c3715c9d5d44e8037b366107a50`; `en.ts` `e477326f2a4b4e9e16efda1f8fa706539c0ce8a7f8e5abc350b6fa016aeb26e2`; `pt.ts` `59203692592ada7baf6ba80d26cfa2012bb3d4c10c5b070f2e9368004a97e50d`.
- **Checks:** reviewer reproduziu Vitest focado 6/6, typecheck e `git diff --check`; inspecionou normalização, fallbacks, ordem, grid/wrapping, i18n e ausência de scope creep. Build ficou para o gate independente do orquestrador por escrever artefato.

### Group `panel` QA advisory — 2026-08-30

- **Lens:** `/root/qa_trace_context_execution`
- **Finding:** sem regressão funcional; duas melhorias MEDIUM não bloqueantes (PT field/key e assert negativo de truncamento nos nós de texto) e duas LOW (render de source falsy e tokens dark/stretch em teste). O typecheck e a higiene passaram; QA visual permanece no checklist pós-merge.

### Group `panel` quality review — 2026-08-30T14:35:37Z

- **Reviewer:** `codex:/root/quality_trace_context_execution`
- **Verdict:** SHIP
- **Gaps bloqueantes:** nenhum CRITICAL ou HIGH.
- **Adjudicação:** implementação atual é correta, acessível, responsiva, theme-aware e O(n), sem estado, dependência, abstração ou alteração de mapper/backend. As melhorias QA são não bloqueantes porque `TraceContextItem` exige strings, os mappers normalizam ausências para `""`, o componente já cobre unknown e os nós atuais não têm truncamento.
- **Checks:** `tsc --noEmit` e `git diff --check` passaram; hashes scoped permaneceram iguais aos do execution review.

### Group `panel` orchestrator validation — 2026-08-30T14:42:36Z

- **Command:** `pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/components/trace-viewer.test.tsx && pnpm --dir frontend/artifacts/neurostack run typecheck && PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build && git diff --check`
- **Outcome:** PASS, exit 0. Vitest: 1 arquivo, 6/6 testes, 110.65s. Typecheck: PASS. Build: 2.797 módulos, PASS em 1m47s. `git diff --check`: PASS silencioso.
- **Warnings não bloqueantes:** sourcemap sem localização original em componentes UI existentes e chunk principal acima de 500 kB; nenhum warning aponta para os quatro arquivos do grupo.
- **Scope rationale:** Vitest focado prova a semântica do componente; typecheck cobre React/i18n; build cobre integração Vite/Tailwind; o diff é frontend isolado sem backend, schema, dependência, core compartilhado ou release config, portanto backend/E2E não são proporcionais.

---

## Files to Create/Modify

```
frontend/artifacts/neurostack/src/components/trace-viewer.tsx
frontend/artifacts/neurostack/src/components/trace-viewer.test.tsx
frontend/artifacts/neurostack/src/locales/en.ts
frontend/artifacts/neurostack/src/locales/pt.ts
```
