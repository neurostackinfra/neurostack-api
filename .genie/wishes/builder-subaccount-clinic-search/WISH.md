# Wish: Busca de Clínica nas Sub-Contas do Builder

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `builder-subaccount-clinic-search` |
| **Date** | 2026-10-07 |
| **Author** | Codex |
| **Appetite** | small |
| **Branch** | `wish/builder-subaccount-clinic-search` |
| **Repos touched** | `neurostack-api` |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Builders com muitas subcontas precisam percorrer manualmente todos os cards da aba
“Sub-Contas” para localizar uma clínica. Este wish adiciona uma busca por nome nessa aba,
filtrando localmente os cards já carregados sem alterar a API, a seleção global de clínica
ou as ações existentes em cada subconta.

Um direct wish é suficiente porque a mudança é local e determinística, reutiliza dados
e textos já existentes e não deixa decisões abertas de produto ou arquitetura.

## Scope

### IN

- Campo “Buscar clínicas” na aba “Sub-Contas” do Painel Builder, usando o rótulo e o
  placeholder localizados que o frontend já possui.
- Filtro imediato dos cards por trecho do nome da clínica, ignorando maiúsculas,
  minúsculas, diacríticos e espaços externos da consulta.
- Estado vazio específico para uma busca sem correspondência, sem confundi-lo com o
  estado de Builder que ainda não possui clínicas.
- Preservação da ordenação por saúde e de todas as ações do card quando ele corresponde
  à busca.
- Teste automatizado de componente para busca, limpeza e estados vazios.

### OUT

- Alterar `GET /v1/builder/clinics`, schemas, banco de dados, paginação ou autorização.
- Modificar a busca do seletor global de clínicas no sidebar ou a busca do Master Admin,
  que já possuem fluxos próprios.
- Buscar por slug, telefone, status, plano, e-mail ou qualquer campo diferente do nome.
- Adicionar debounce, cache, persistência da consulta, query string, ranking ou fuzzy
  search.
- Alterar a ordem dos cards, a contagem total exibida ou o comportamento de criar,
  expandir, configurar, ativar, desativar e editar uma clínica.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Filtrar no frontend a lista já carregada por `/v1/builder/clinics`. | A aba já possui todos os nomes necessários; uma busca server-side adicionaria contrato, latência e estado sem ampliar a capacidade atual. |
| 2 | Derivar a lista visível depois da ordenação existente por saúde. | Filtrar o resultado de `sortedClinics` preserva exatamente a prioridade atual entre os cards correspondentes. |
| 3 | Reutilizar `label_search_clinics`, `placeholder_search_clinics` e `clinic_search_no_results`. | Os textos em português e inglês já expressam o comportamento desejado no seletor global, evitando novas chaves equivalentes. |
| 4 | Normalizar consulta e nome com `trim`, lowercase e remoção de diacríticos. | A semântica fica consistente com a busca de clínicas já entregue no sidebar e permite que “clinica sao” encontre “Clínica São”. |
| 5 | Manter o badge com o total de clínicas, não a quantidade filtrada. | O indicador atual representa o inventário da conta; a busca muda somente a lista visível e não o dado de negócio. |

## Simplicity Case

- **Simplest complete design:** um estado local para a consulta, uma lista derivada por
  `useMemo`, um input acima dos cards e um estado vazio condicional na aba existente.
- **Added machinery:** apenas um teste de componente; os dados, componentes de input e
  textos localizados necessários já existem.
- **Deferred until measured:** busca server-side e paginação somente serão consideradas
  se `/v1/builder/clinics` deixar de retornar a lista completa ou se a filtragem exceder
  100 ms em dispositivo-alvo.
- **Complexity removed:** nenhum endpoint, dependência, debounce, estado global,
  persistência, URL sincronizada ou refatoração do seletor do sidebar.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [x] A aba “Sub-Contas” mostra um campo acessível “Buscar clínicas” quando o Builder
  possui ao menos uma clínica.
- [x] Digitar parte do nome mantém visíveis somente os cards correspondentes.
- [x] A correspondência ignora caixa, diacríticos e espaços antes/depois da consulta;
  por exemplo, ` clinica sao ` encontra `Clínica São Lucas`.
- [x] Limpar a consulta restaura todos os cards na mesma ordem de saúde usada antes da
  busca.
- [x] Uma consulta sem correspondência mostra “Nenhuma clínica encontrada” e não mostra
  o estado “Nenhuma clínica cadastrada”.
- [x] Um Builder sem clínicas continua vendo apenas o estado atual “Nenhuma clínica
  cadastrada” e a ação para criar uma clínica, sem campo de busca.
- [x] O badge “Total Clínicas” continua exibindo o total carregado, independentemente da
  quantidade de cards filtrados.
- [x] Digitar ou limpar a busca não realiza nova requisição e não altera, expande ou
  executa ações em nenhuma clínica.
- [x] Um card correspondente preserva status, métricas, expansão e ações existentes.
- [x] A busca usa os textos localizados já existentes em português e inglês.
- [x] Teste automatizado focado e typecheck do frontend terminam com exit 0.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — estado local de UI (+2 stateful work), com filtro determinístico em um único arquivo de produção | `engineer-standard` / medium | Adicionar a busca local aos cards de Sub-Contas e cobrir o contrato com teste de componente. |

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

### Group 1: Filtro por nome nas Sub-Contas

**Goal:** Permitir que o Builder encontre rapidamente uma clínica pelo nome na lista de
Sub-Contas sem mudar dados, ordenação ou ações dos cards.

**Deliverables:**
1. Atualizar `frontend/artifacts/neurostack/src/pages/builder.tsx` com estado de consulta,
   normalização de nome, lista filtrada derivada de `sortedClinics`, input acessível e
   estado de zero resultados na aba “Sub-Contas”.
2. Adicionar
   `frontend/artifacts/neurostack/src/pages/builder-subaccount-clinic-search.test.tsx`
   com fixtures de clínicas em diferentes estados de saúde e nomes com diacríticos.

**Acceptance Criteria:**
- [x] O input é renderizado somente quando há clínicas carregadas e reutiliza
  `label_search_clinics` e `placeholder_search_clinics`.
- [x] A normalização aplica `trim`, lowercase e remoção de marcas diacríticas à consulta
  e ao nome antes de comparar por substring.
- [x] A lista visível é derivada de `sortedClinics`, de modo que filtrar e limpar não
  alteram a ordenação por `critical`, `warning` e `healthy`.
- [x] A filtragem considera exclusivamente `clinic.name`; outros campos não produzem
  correspondência.
- [x] O estado `clinic_search_no_results` aparece somente quando existem clínicas,
  existe consulta efetiva e nenhum card corresponde.
- [x] Consulta vazia ou composta apenas por espaços restaura a lista completa e não
  mostra o estado de zero resultados.
- [x] O estado `builder_empty` e seu botão de criação continuam exclusivos do caso
  `clinics.length === 0`.
- [x] O badge continua vinculado a `clinics.length`, não à lista filtrada.
- [x] O teste afirma filtragem parcial sem caixa/acentos, exclusão de não correspondentes,
  preservação da ordem, restauração ao limpar, estado sem resultados, ausência do input
  sem clínicas e ausência de novas chamadas de rede durante a interação.
- [x] O teste expande um card filtrado e comprova que seu conteúdo existente continua
  disponível, sem alterar automaticamente `expandedClinicId` ao digitar.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/builder-subaccount-clinic-search.test.tsx src/pages/builder-cloning-center.test.tsx src/pages/builder-password-management.test.tsx src/pages/builder-roadmap-item.test.tsx src/pages/builder-support-tickets.test.tsx
pnpm --dir frontend/artifacts/neurostack run typecheck
```

O comando de testes é um gate futuro incondicional: cobre o novo estado e executa
explicitamente todos os testes Builder existentes. O typecheck cobre o boundary
React/TypeScript alcançado. A mudança não toca backend, dependências, schemas, build
config ou runtime compartilhado, portanto os gates completos de backend e E2E não são
proporcionais a este grupo.

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Como Builder com várias subcontas, buscar um trecho do nome com caixa e acentos
  diferentes mostra somente as clínicas correspondentes.
- [ ] Apagar a busca restaura todos os cards na prioridade de saúde original e o badge
  continua exibindo o total cadastrado.
- [ ] Uma busca inexistente mostra o estado localizado de zero resultados; um Builder
  realmente sem clínicas continua vendo o estado de cadastro vazio e a ação “Nova
  Clínica”.
- [ ] Expandir e usar as ações de um card encontrado mantém o comportamento atual.
- [ ] Digitar no filtro não dispara chamadas adicionais a `/v1/builder/clinics`.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| A busca da aba divergir semanticamente da busca do sidebar | Low | Reusar a mesma normalização observável e as mesmas chaves de tradução; fixar caixa, acentos e espaços no teste. |
| O estado vazio de filtro substituir incorretamente o estado de conta sem clínicas | Low | Condicionar os dois estados por `clinics.length` e cobrir ambos no teste. |
| Uma consulta ativa ocultar uma clínica recém-criada ou atualizada após `fetchAll` | Low | Tratar a consulta como filtro vigente: o novo card aparece somente se o nome corresponder; limpar restaura a lista atualizada. |

---

## Review Results

### 2026-10-07 — Plan Review

- **Target SHA-256:** `344398943525d08910bb1c8a5dfc6056e7624d2ac6f9d9bd6b67989e1f34b843`
- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/review_builder_subaccount_search`
- **Evidence:** o fluxo real de `builder.tsx`, os textos PT/EN, a semântica de busca do
  sidebar, a estrutura do wish e o `git diff --check` foram verificados; o novo teste
  ainda não existe, como esperado nesta etapa. O typecheck não chegou ao compilador
  porque o pnpm tentou remover/reinstalar `node_modules` sem TTY
  (`ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY`), portanto a checagem ficou inconclusiva
  por ambiente e não demonstrou erro TypeScript.
- **HIGH:** o WISH estava coberto pela regra `.genie/wishes/*` e não era versionável;
  adicionar a exceção individual no `.gitignore` antes da execução.
- **MEDIUM:** explicitar por que um direct wish é suficiente; tornar observável o
  escalonamento para os testes Builder existentes; manter o typecheck literal e
  comprová-lo após restaurar o ambiente pnpm suportado.

### 2026-10-07 — Plan Re-review (Fix Loop 1/2)

- **Target SHA-256:** `19363e8e73ddf803da2a7904509e9baf959950b0550a795009cd901cc8cd0023`
- **Verdict:** SHIP
- **Reviewer:** `/root/rereview_builder_subaccount_search`
- **Evidence:** o SHA permaneceu estável durante a inspeção; `git diff --check` passou;
  `git check-ignore -q` confirmou que o WISH não está ignorado; o endpoint foi
  verificado como lista completa sem paginação; fluxo, textos, normalização, critérios,
  dependências e validações foram confrontados com o código real.
- **Checks futuros:** o novo teste ainda não existe, como esperado em plan review; a
  execução deve obter exit 0 nos cinco testes Builder listados e no typecheck literal.
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW. Plano aprovado para execução.

### 2026-10-07 — Group 1 Execution Review

- **Task:** `t_muymm6mg0848d273`
- **Verdict:** SHIP
- **Reviewer:** `/root/review_builder_subaccount_execution`
- **Target:** produção
  `52dbdd4d3f021aab75a5818317f2a4b9ab4b2a8146f850e3c8d109bda5b16aed`;
  teste `d4a103f7c58f62913e3494709de23b649a9c287b524beca39af95b580a05e366`.
- **Evidence:** teste focal 2/2, typecheck e diff-check terminaram com exit 0; inspeção
  confirmou normalização, filtro exclusivo por nome, ordenação, estados vazios, badge,
  ausência de requests e preservação dos hunks concorrentes de e-mail.
- **Gap residual MEDIUM:** a prova de expansão usava inicialmente o editor concorrente
  de e-mail; antes do commit, a asserção foi isolada no input de telefone preexistente.

### 2026-10-07 — Group 1 Quality Review

- **Verdict:** SHIP
- **Reviewer:** `/root/quality_review_builder_subaccount_search`
- **Evidence:** filtro linear sobre dados já carregados, sem HTML dinâmico, regex do
  usuário, dependência, cache, estado global ou requisição adicional; input possui label
  acessível e os estados vazios são mutuamente exclusivos.
- **Gap residual MEDIUM:** ausência de `role="status"`/`aria-live` na mensagem dinâmica
  sem resultados. O acoplamento da asserção de expansão foi removido antes do commit.

### 2026-10-07 — Pre-commit Test Isolation

- A prova de expansão passou a usar `input-clinic-phone-healthy-aurora`, conteúdo
  expansível presente no baseline, em vez do editor de e-mail concorrente.
- O teste focal e o typecheck foram repetidos após o delta antes da criação do commit.

### 2026-10-07 — Group 1 Orchestrator Validation

- **Testes:** o comando planejado com o novo arquivo e quatro suítes Builder terminou
  com exit 0 — 5 arquivos e 32 testes aprovados em 161,94 s.
- **Typecheck:** `pnpm --dir frontend/artifacts/neurostack run typecheck` terminou com
  exit 0 (`tsc -p tsconfig.json --noEmit`).
- **Integridade:** `git diff --check` passou; hashes de produção/teste permaneceram
  iguais aos revisados; `package.json`, `pnpm-workspace.yaml` e `pnpm-lock.yaml` não
  foram modificados.
- **Escopo:** testes de componente da página Builder mais typecheck são proporcionais à
  alteração React local; backend, schema, dependências, build config e E2E não foram
  alcançados.

### 2026-10-07 — Main Isolation Validation

- A implementação e o teste foram reaplicados sobre `origin/main` em worktree isolada,
  excluindo integralmente os hunks concorrentes do editor de e-mail.
- As cinco suítes Builder planejadas terminaram com exit 0 — 5 arquivos e 30 testes
  aprovados; a diferença de contagem corresponde aos testes concorrentes ausentes na
  `main`.
- O typecheck literal `tsc -p tsconfig.json --noEmit` e `git diff --check` terminaram
  com exit 0 antes do commit.

---

## Files to Create/Modify

```
frontend/artifacts/neurostack/src/pages/builder.tsx
frontend/artifacts/neurostack/src/pages/builder-subaccount-clinic-search.test.tsx
```
