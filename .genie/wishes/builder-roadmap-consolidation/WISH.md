# Wish: Consolidação da Torre do Builder

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `builder-roadmap-consolidation` |
| **Date** | 2026-09-06 |
| **Author** | Codex |
| **Appetite** | small |
| **Branch** | `wish/builder-roadmap-consolidation` |
| **Repos touched** | `frontend/artifacts/neurostack` |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Retirar do menu Roadmap a entrada redundante “Portal de Parceiros”, tratada pelo produto como a antiga Torre do Builder, porque suas capacidades já estão disponíveis no Painel Builder. No painel existente, comunicar somente a entrega ainda pendente por meio do item “Certificação e co-branding NeuroStack”, identificado de forma inequívoca como “EM BREVE”.

## Problem

O menu ainda apresenta uma área de roadmap separada para uma jornada que já foi incorporada ao Painel Builder, enquanto a única capacidade não entregue, certificação e co-branding, não está sinalizada no local em que builders trabalham hoje.

## Scope

### IN

- Remover “Portal de Parceiros” (`/portal-parceiros`) da coleção de itens exibidos na seção Roadmap do sidebar para todos os papéis.
- Remover do layout imports e chaves de navegação que se tornarem sem uso pela retirada desse item.
- Adicionar à aba inicial “Visão Global” do Painel Builder um card informativo com o título exato “Certificação e co-branding NeuroStack” e o badge exato “EM BREVE” em português.
- Localizar o novo conteúdo no catálogo existente, com “NeuroStack certification and co-branding” e “COMING SOON” em inglês.
- Cobrir a ausência da entrada antiga no menu, a presença do novo item no Painel Builder e a paridade de idioma com testes frontend.

### OUT

- Excluir `portal-parceiros.tsx`, remover a rota direta `/portal-parceiros` ou definir redirecionamento para acessos antigos.
- Implementar certificação, selo, co-branding, white-label, configuração, upload de marca ou qualquer ação interativa associada ao novo item.
- Alterar funcionalidades já ativas do Painel Builder, permissões de builders, APIs ou persistência backend.
- Reorganizar ou remover outros itens da seção Roadmap.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Remover somente a entrada de navegação do roadmap e preservar a rota/página legada | O pedido nomeia o menu como redundante; apagar ou redirecionar uma URL existente ampliaria o risco e requer uma decisão separada sobre compatibilidade. |
| 2 | Exibir a pendência como card informativo e não clicável na aba “Visão Global” | O requisito atual é comunicar disponibilidade futura, sem destino ou fluxo funcional; a aba inicial oferece visibilidade no painel já adotado pelos builders. |
| 3 | Usar um badge textual de disponibilidade, sem botão desabilitado | “EM BREVE” descreve o estado sem sugerir uma ação possível nem criar comportamento falso. |
| 4 | Localizar título e estado nos catálogos `pt` e `en` existentes | O Painel Builder já é bilíngue e o tipo das traduções exige paridade entre os catálogos. |
| 5 | Manter navegação e comunicação no mesmo grupo de execução | As duas mudanças formam uma única transição de produto: retirar a representação antiga somente quando a pendência estiver visível no painel atual. |

## Simplicity Case

- **Simplest complete design:** remover uma entrada estática de `roadmapItems` e acrescentar um único card estático, localizado e não interativo na aba inicial do Painel Builder, com título e badge de estado.
- **Added machinery:** duas chaves de tradução e testes focados para layout e Painel Builder; são necessárias para manter a experiência bilíngue e impedir regressão da migração de comunicação.
- **Deferred until measured:** criar página, modal, link, feature flag, configuração ou integração para co-branding somente quando houver fluxo funcional aprovado e critérios próprios de ativação.
- **Complexity removed:** nenhuma API, modelo, migração, estado React, chamada de rede, permissão nova, dependência ou mecanismo de rollout.

## Dependencies

**depends-on:** central-clonagem-clinicas
**blocks:** none

## Success Criteria

- [x] Nenhum sidebar renderiza “Portal de Parceiros” nem um link de roadmap para `/portal-parceiros`, independentemente do papel autenticado.
- [x] Os demais itens do Roadmap mantêm ordem, destino, visibilidade por papel e badges atuais.
- [x] Um builder encontra na aba padrão “Visão Global” exatamente um item com o título “Certificação e co-branding NeuroStack”.
- [x] Em português, o item mostra o badge textual exato “EM BREVE”; em inglês, mostra “NeuroStack certification and co-branding” e “COMING SOON”.
- [x] O item é puramente informativo: não contém link, botão, controle desabilitado nem dispara requisição adicional.
- [x] A rota direta `/portal-parceiros` e sua página permanecem inalteradas por este wish.
- [x] Como precondição de execução, a Central de Clonagem já está disponível como aba em `/builder`, comprovada pelo teste `src/pages/builder-cloning-center.test.tsx` entregue por `central-clonagem-clinicas`; este wish não implementa nem altera essa aba.
- [x] Testes focados, typecheck, suíte frontend e build de produção passam sem regressão.

## Execution Strategy

**Entry gate:** a Wave 1 só pode iniciar depois de `central-clonagem-clinicas` estar concluído (`Status: SHIPPED`) e de seu teste focado confirmar que a aba Central de Clonagem está disponível em `/builder`:

```bash
test -f frontend/artifacts/neurostack/src/pages/builder-cloning-center.test.tsx
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/builder-cloning-center.test.tsx
```

Essa aba é uma precondição consumida por este wish, não uma entrega a ser implementada ou alterada por ele.

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 0 — mudança estática em um único pacote, sem estado novo e com validação determinística | `engineer-trivial` / low | Consolidar a comunicação da antiga Torre do Builder no painel ativo e proteger a navegação e o conteúdo com testes. |

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

### Group 1: Consolidar roadmap no Painel Builder

**Goal:** Remover do roadmap a navegação redundante da Torre do Builder e comunicar sua única capacidade pendente no painel funcional usado pelos builders.

**Deliverables:**
1. `src/components/layout.tsx` sem a entrada `/portal-parceiros` em `roadmapItems` e sem imports que ficarem órfãos.
2. `src/pages/builder.tsx` com um card informativo na aba padrão “Visão Global”, sem interação ou nova chamada de API.
3. `src/locales/pt.ts` e `src/locales/en.ts` com título e estado localizados do novo item e sem a chave de navegação que deixar de ter consumidor.
4. Cobertura em `src/components/layout.test.tsx` e em teste focado do Painel Builder para o contrato de migração, conteúdo e ausência de interação.

**Acceptance Criteria:**
- [x] `roadmapItems` não contém item com `href="/portal-parceiros"`, id `portal-parceiros` ou rótulo `nav_partners`.
- [x] Testes de layout comprovam que a entrada antiga não aparece para ao menos `builder`, `investor` e `super_admin`, papéis que hoje podem recebê-la.
- [x] O restante do array `roadmapItems` e suas regras `hideFor` não é alterado.
- [x] A aba `overview` renderiza um único card com título e badge localizados depois que o carregamento inicial termina com sucesso.
- [x] O locale `pt` resolve exatamente para “Certificação e co-branding NeuroStack” e “EM BREVE”.
- [x] O locale `en` resolve exatamente para “NeuroStack certification and co-branding” e “COMING SOON”.
- [x] O card não possui elemento com papel `link`, `button`, `checkbox` ou `switch` associado à funcionalidade futura.
- [x] A renderização do card não adiciona `fetch`, estado local, handler, dependência ou prop de API.
- [x] O import, o componente e a declaração de rota de `PortalParceiros` em `App.tsx` permanecem intactos.

**Validation:**
```bash
# Os dois testes focados provam os contratos de navegação, conteúdo, localização e ausência de ação.
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/components/layout.test.tsx
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/builder-roadmap-item.test.tsx

# O typecheck valida imports removidos e paridade entre as chaves dos catálogos.
pnpm --dir frontend/artifacts/neurostack typecheck

# Layout e Painel Builder são superfícies compartilhadas; suíte e build preservam o gate agregado do frontend.
pnpm --dir frontend/artifacts/neurostack test
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack build
```

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Entrar como builder e confirmar que “Portal de Parceiros” não aparece no Roadmap do sidebar desktop nem no menu móvel.
- [ ] Na aba padrão “Visão Global”, confirmar que o card “Certificação e co-branding NeuroStack” e o badge “EM BREVE” ficam visíveis sem clique adicional e se adaptam a desktop e mobile sem corte ou overflow.
- [ ] Confirmar que o item futuro não recebe foco nem oferece qualquer ação ao mouse ou teclado.
- [ ] Alternar o locale para inglês e confirmar “NeuroStack certification and co-branding” e “COMING SOON”, sem reaparecimento do item antigo.
- [ ] Entrar como investor e super admin e confirmar que “Portal de Parceiros” também não aparece, enquanto os demais itens do Roadmap continuam disponíveis conforme as regras atuais.
- [ ] Abrir diretamente `/portal-parceiros` com um usuário autenticado e confirmar que a rota legada continua respondendo como antes.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| “Torre do Builder” ser um nome interno diferente de “Portal de Parceiros” | Medium | O repositório possui uma única entrada de roadmap dedicada a builders/parceiros, `/portal-parceiros`; registrar explicitamente essa equivalência e limitar a mudança a ela. |
| Usuários ainda possuírem links diretos para a página legada | Low | Preservar import, rota e página; retirar somente sua promoção no menu. |
| O card estático ser confundido com funcionalidade ativa | Low | Usar badge explícito “EM BREVE”/“COMING SOON” e não renderizar controles ou links. |
| Alterações concorrentes já presentes em layout, catálogos ou Painel Builder causarem conflito durante a execução | Medium | Preservar os diffs existentes, aplicar mudanças cirúrgicas e validar o diff do grupo sem reverter trabalho de outros wishes. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — 2026-09-06T12:21:48-03:00

- **Context:** before-work plan review.
- **Reviewed target:** `.genie/wishes/builder-roadmap-consolidation/WISH.md` at SHA-256 `562fdf86bc11ebc28b56882245c48836e1fe20c9ffa1db62dfde432ea9d9aeaa`.
- **Evidence:** `/portal-parceiros` is the roadmap entry whose page identifies itself as the Builder control tower; `/builder` is the active Builder panel; `central-clonagem-clinicas` is still delivering a promised Builder capability.
- **Validation inspection:** the focused Vitest commands, typecheck, frontend suite and environment-complete build were valid and proportional but were not run before implementation. The repository has no root `wishes:lint`; the bundled legacy validator's `Group A` expectation conflicts with the official current template's `Group 1` format and was not treated as a structural gap.
- **Gap:** [HIGH] missing `central-clonagem-clinicas` dependency made the consolidation premise false until the cloning center reaches the Builder panel.
- **Verdict:** **FIX-FIRST**.

### Plan Re-review — 2026-09-06T12:26:35-03:00 (fix loop 1/2)

- **Reviewed target:** `.genie/wishes/builder-roadmap-consolidation/WISH.md` at SHA-256 `7630e6c84b18ad7f27a1145e07eb19a6960191f65902993d1f322db500383e7d`.
- **Prior gap:** resolved by declaring the dependency, adding a focused-test entry gate and keeping the cloning center outside this wish's deliverables.
- **Gap:** [HIGH] the entry gate required `Status: DONE`, which is incompatible with the governing lifecycle's terminal `SHIPPED` state and could block execution indefinitely.
- **Verdict:** **FIX-FIRST**.

### Plan Re-review — 2026-09-06T12:28:25-03:00 (fix loop 2/2)

- **Reviewed target:** `.genie/wishes/builder-roadmap-consolidation/WISH.md` at SHA-256 `010cc2bf90e12ca57ab86005d546c506b4d099ecd9cd2e787794348f54380c1f`.
- **Evidence:** the load-bearing assumption is now protected by `depends-on: central-clonagem-clinicas`, a `SHIPPED` lifecycle gate and its focused Builder-tab test; IN/OUT, criteria, files and the single group are concrete, testable, bite-sized and free of speculative machinery; direct access to the legacy route remains compatible.
- **Validation inspection:** focused tests, typecheck, aggregate frontend suite and build remain valid and proportional; they were not executed because the planned implementation and dependent cloning test do not exist in the reviewed snapshot.
- **Severity-tagged gaps:** none.
- **Verdict:** **SHIP**.

### Work Entry Gate — 2026-09-07

- **Dependency evidence:** `central-clonagem-clinicas` has all five tasks marked `done`, a persisted final aggregate review verdict of `SHIP`, and repository-terminal status `DONE`; this is treated as the completed equivalent of the planned `SHIPPED` gate.
- **Focused validation:** the repository-documented `pnpm --dir` wrapper reproduced its known preinstall/user-agent failure, so the same local Vitest binary was run from `frontend/artifacts/neurostack`; `src/pages/builder-cloning-center.test.tsx` passed 16/16 with exit code 0.
- **Gate result:** **PASS**; Wave 1 may start.

### Group 1 Execution Review — 2026-09-07T20:24:22-03:00

- **Context:** first review of the uncommitted six-file Group 1 target at HEAD `f66c24076cb3fa419e6f02d684a68b4972896702` and WISH SHA-256 `b31bb0e91cb09925f1e08980bbc09f7692ca035cbc34a8ae24b03fc18ae31f76`.
- **Evidence:** the card, dedicated test, direct legacy route and cloning center were intact, but the target changed during review: another session restored `/portal-parceiros` and removed the new locale keys.
- **Gaps:** [HIGH] roadmap entry/import removal was lost; [HIGH] the two PT/EN locale keys were lost while staged hunks from other wishes had to be preserved.
- **Verdict:** **FIX-FIRST**.

### Group 1 Execution Re-review — 2026-09-07T20:32:41-03:00 (fix loop 1/2)

- **Target identity:** `layout.tsx` `c6f7c7af…`; `layout.test.tsx` `89475e51…`; `builder.tsx` `684d6138…`; `builder-roadmap-item.test.tsx` `e11f6e96…`; `pt.ts` `8f4b45fe…`; `en.ts` `35513931…`. Hashes remained stable through the review.
- **Evidence:** `/portal-parceiros`, `portal-parceiros`, `nav_partners` and the orphan `Briefcase` import are absent; all remaining roadmap ordering/role rules are intact; the localized, non-interactive card renders once in `overview`; App/legacy route and the cloning center have no group diff; concurrent staged locale hunks were preserved.
- **Validation:** the two focused files passed 26/26 and `git diff --check` passed.
- **Severity-tagged gaps:** none in the reviewed target; aggregate typecheck and suite remained for orchestrator validation.
- **Verdict:** **SHIP**.

### Group 1 Quality Review — 2026-09-07T20:37:53-03:00

- **Target:** the same stable six-file hashes from the execution re-review.
- **Evidence:** static content introduces no untrusted input, navigation, request, state, effect, handler, dependency or meaningful runtime cost; text remains readable and non-focusable; i18n parity and descriptive keys are intact; tests cover three roles, two locales, uniqueness, absence of interactive roles and the unchanged six-request load contract.
- **Validation:** `git diff --check` passed; focused 26/26 and the prior 2,804-module build were applicable to the reviewed hashes.
- **Severity-tagged gaps:** none; aggregate typecheck and suite remained an informational pending gate.
- **Verdict:** **SHIP**.

### Group 1 Orchestrator Validation — 2026-09-07

- **Stable target:** all six full SHA-256 hashes matched both clean reviews before validation; `git diff --check` passed.
- **Wrapper diagnosis:** every planned `pnpm --dir frontend/artifacts/neurostack ...` command attempts an implicit reinstall and fails the repository preinstall user-agent guard. No dependency, lockfile or configuration was changed; already-installed local binaries were used as the command-equivalent route documented by the completed cloning-center wish.
- **Focused tests:** `layout.test.tsx` plus `builder-roadmap-item.test.tsx` passed 2/2 files and 26/26 tests in both the shared checkout and an isolated snapshot.
- **Typecheck:** `frontend/node_modules/.bin/tsc -p frontend/artifacts/neurostack/tsconfig.json --noEmit` passed with exit code 0 in the shared checkout and isolated snapshot.
- **Build:** the isolated production build passed with 2,804 modules transformed in 3m15s; only existing sourcemap and chunk-size warnings were emitted.
- **Aggregate suite:** the shared checkout and isolated HEAD `dd26bbb68a2357e074b4fecd364e31d670ad58e9` plus only this six-file diff produced the same result: 25/26 files and 228/229 tests passed. The sole failure is outside this wish at `src/pages/handoff-queue.test.tsx:185`, whose pre-existing assertion expects the em dash `—` for null intent while the current Handoff card does not render it.
- **Scope diagnosis:** the failure reproduces without concurrent staged hunks and is not reachable from any Group 1 file. Repairing Handoff behavior/test belongs to separate work and would expand this wish's explicit OUT scope.
- **Result:** **BLOCKED on aggregate baseline**. Functional and quality reviews are SHIP, but task `t_mtpyfl4d257b8fcc` remains `in_progress` and must not be marked `done` until the required full frontend suite is green on the integrated snapshot.

### Aggregate Unblock Fix Review — 2026-09-07T21:34:14-03:00

- **Target:** commit `4c4a9d13b5094092eeefabe5f4e5cd866db900c0`, relative to parent `dd26bbb68a2357e074b4fecd364e31d670ad58e9`.
- **Scope:** the one-file, one-line functional repair restores `item.intencao_detectada ?? "—"` in `handoff-queue.tsx`; the regression test and all six Builder roadmap files remain untouched by the fix.
- **Isolated validation:** a clean detached worktree at the exact target commit passed `src/pages/handoff-queue.test.tsx`, 1/1 file and 10/10 tests, exit code 0; the temporary worktree was removed afterward.
- **Independent re-review:** the reviewer confirmed the fallback for null intent, non-null intent rendering, safety badge, filters and remaining behavior; `git diff --check` passed and no severity-tagged gaps remained.
- **Verdict:** **SHIP**.

### Group 1 Final Orchestrator Validation — 2026-09-07

- **Integrated snapshot:** HEAD `4c4a9d13b5094092eeefabe5f4e5cd866db900c0` plus the six-file Builder roadmap diff. All six SHA-256 hashes remained identical to the execution and quality reviews, and `git diff --check` passed.
- **Focused Builder tests:** `layout.test.tsx` plus `builder-roadmap-item.test.tsx` passed 2/2 files and 26/26 tests.
- **Typecheck:** `frontend/node_modules/.bin/tsc -p frontend/artifacts/neurostack/tsconfig.json --noEmit` passed with exit code 0.
- **Aggregate suite:** all 26/26 frontend test files and 229/229 tests passed in 418.94s, including the formerly failing handoff regression.
- **Production build:** Vite transformed 2,804 modules and completed in 2m58s; only the pre-existing sourcemap and chunk-size warnings were emitted.
- **Result:** **PASS**. The prior aggregate baseline block is resolved, both independent reviews are SHIP, and task `t_mtpyfl4d257b8fcc` is eligible for completion.

### PR Review — 2026-09-07T22:02:23-03:00

- **Snapshot:** commit `52643d4b0164536848c3bbcea27ed197ac368355`, based on `origin/main` at `c8ce6e1e1236211987889da15a06086ef3ff83c0`; target requested by the user: `main`.
- **Scope:** exactly the WISH artifact and the six planned product files, with 394 insertions and 4 removals; the direct legacy route and Central de Clonagem remain intact.
- **Safety:** no secrets or credentials were found; `builder-token` is an explicit test fixture. The card is static, passive and bilingual, and the commit follows Conventional Commits with the exact wish slug in its body.
- **Validation:** `git diff --check` passed. The reviewer confirmed that the persisted 26/26 focused tests, 229/229 aggregate tests, typecheck and 2,804-module production build apply to the immutable reviewed tree and current `main` base.
- **Severity-tagged gaps:** none; residual warnings are limited to the pre-existing sourcemap and chunk-size notices.
- **Verdict:** **SHIP**.

---

## Files to Create/Modify

```
frontend/artifacts/neurostack/src/components/layout.tsx
frontend/artifacts/neurostack/src/components/layout.test.tsx
frontend/artifacts/neurostack/src/pages/builder.tsx
frontend/artifacts/neurostack/src/pages/builder-roadmap-item.test.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
```
