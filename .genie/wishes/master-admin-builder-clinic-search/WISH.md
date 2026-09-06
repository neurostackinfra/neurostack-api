# Wish: Busca de Builder e Clínica no Master Admin

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `master-admin-builder-clinic-search` |
| **Date** | 2026-09-06 |
| **Author** | Codex |
| **Appetite** | small |
| **Branch** | `wish/master-admin-builder-clinic-search` |
| **Repos touched** | `neurostack-api` |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Ampliar a busca existente na seção “Gestão de Builders” do Master Admin para localizar
um builder tanto pelo próprio nome quanto pelo nome de qualquer clínica vinculada. A
mudança reutiliza os dados já carregados por `/v1/admin/builders`, mantém a busca atual
por e-mail e evita alterações de API, banco ou paginação.

## Scope

### IN

- Fazer o filtro da seção “Gestão de Builders” considerar `builder.name`,
  `builder.admin_email` e cada `builder.clinics[].clinic_name`.
- Aplicar correspondência parcial sem distinção entre maiúsculas e minúsculas e ignorar
  espaços antes/depois do termo pesquisado.
- Exibir o card do builder pai quando o termo corresponder a uma clínica vinculada,
  preservando sua lista completa de clínicas para inspeção ao expandir o card.
- Atualizar o texto do campo de busca para comunicar que nomes de clínicas também são
  aceitos.
- Cobrir o comportamento e as regressões do filtro com testes automatizados de frontend.

### OUT

- Alterar `GET /v1/admin/builders`, schemas, modelos, banco de dados ou índices.
- Adicionar busca server-side, paginação, debounce, cache ou novas requisições de rede.
- Implementar busca tolerante a acentos, erros de digitação, palavras aproximadas ou
  ranking de resultados.
- Filtrar as clínicas internas do builder retornado ou expandir cards automaticamente.
- Alterar buscas de tickets, leads, alertas, transações ou outras áreas do Master Admin.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Estender o filtro client-side já existente. | O payload atual já contém os nomes de builders e clínicas; uma mudança de backend não agrega capacidade para o volume e fluxo atuais. |
| 2 | Manter a busca por e-mail de administrador. | É comportamento existente e útil; adicionar clínicas não deve causar regressão. |
| 3 | Retornar o builder inteiro quando uma clínica corresponder. | O card é a unidade da interface e fornece o contexto de propriedade; podar a lista filha criaria estado e regras adicionais. |
| 4 | Usar substring com `trim` e normalização de caixa, sem busca fuzzy. | É consistente com o comportamento atual e satisfaz nomes completos ou parciais sem dependências novas. |

## Simplicity Case

- **Simplest complete design:** acrescentar a verificação `some` sobre os nomes das
  clínicas ao predicado existente e ajustar o placeholder, com um teste focado no
  comportamento observável.
- **Added machinery:** nenhum serviço, endpoint, dependência ou estado novo; somente a
  cobertura automatizada exigida para fixar o contrato de busca.
- **Deferred until measured:** normalização sem acentos, fuzzy search e busca server-side
  só entram se feedback de uso ou crescimento do payload demonstrar necessidade.
- **Complexity removed:** sem sincronização de query string, debounce, paginação,
  autoexpansão, ranking ou segundo campo de busca.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [x] Pesquisar parte do nome de um builder mantém somente builders correspondentes na
  seção “Gestão de Builders”.
- [x] Pesquisar parte do nome de uma clínica vinculada mostra o card de seu builder pai.
- [x] A correspondência por builder ou clínica ignora caixa e espaços externos do termo.
- [x] A busca existente por e-mail do administrador do builder continua funcionando.
- [x] Builders sem correspondência no próprio nome, e-mail ou clínicas são ocultados.
- [x] Limpar o campo restaura todos os builders e nenhuma clínica é removida dos cards
  retornados.
- [x] O placeholder informa explicitamente “builder, clínica ou e-mail do builder”.
- [x] Quando não há correspondência, o estado vazio existente continua exibindo o termo
  pesquisado.
- [x] Teste automatizado cobre correspondência por builder, clínica e e-mail, ausência
  de correspondência, caixa/espaços, restauração após limpar, o placeholder visível com
  “builder, clínica ou e-mail do builder” e o estado vazio visível com o termo pesquisado.
- [x] Teste focado, typecheck e build do frontend terminam com exit 0.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 0 — alteração local, determinística e sem novo estado ou boundary | `engineer-trivial` / low | Ampliar o filtro do Master Admin, atualizar o texto de ajuda e adicionar teste focado. |

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** →
`engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high;
**4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an
independent `final-gate` at the highest justified effort. Codex maps these to
the `genie_*` profiles; other runtimes use their matching native roles. Keep
model and effort in runtime session/agent configuration, never skill
frontmatter.

## Execution Groups

### Group 1: Filtro unificado de builders e clínicas

**Goal:** Permitir que o Master Admin encontre o builder correto pelo nome do próprio
builder ou de uma clínica vinculada, sem mudar o contrato de dados.

**Deliverables:**

1. Atualizar o predicado de `filteredBuilders` em
   `frontend/artifacts/neurostack/src/pages/master-admin.tsx` para incluir nomes de
   clínicas, mantendo nome e e-mail do builder.
2. Atualizar o placeholder do campo para dizer “builder, clínica ou e-mail do builder”.
3. Adicionar
   `frontend/artifacts/neurostack/src/pages/master-admin-builder-clinic-search.test.tsx`
   com fixtures de múltiplos builders e clínicas e as asserções do contrato.

**Acceptance Criteria:**

- [x] O predicado normaliza o termo uma vez e o aplica a nome/e-mail do builder e a
  `clinic_name` de cada clínica vinculada.
- [x] Uma correspondência em qualquer clínica inclui exatamente seu builder pai e
  preserva todas as clínicas desse builder.
- [x] Qualquer builder cujo `name` ou `admin_email` corresponda ao termo permanece
  incluído depois da adição do ramo de clínica ao predicado; string vazia ou composta
  apenas por espaços restaura a lista completa.
- [x] O estado vazio aparece somente quando há termo efetivo e nenhuma correspondência.
- [x] O texto visível do campo menciona “builder, clínica ou e-mail do builder”.
- [x] Os testes usam o payload real de `BuilderWithClinics` e falham se o predicado
  voltar a ignorar `clinic_name`.
- [x] O teste automatizado de correspondência por clínica afirma que o builder pai é
  retornado inicialmente no estado colapsado existente e que, após expansão manual,
  ficam visíveis tanto a clínica correspondente quanto outra clínica vinculada que não
  corresponde ao termo.

**Validation:**

```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/master-admin-builder-clinic-search.test.tsx
pnpm --dir frontend/artifacts/neurostack typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack build
```

O teste focado cobre cada ramo do predicado e o estado visível da página; typecheck e
build cobrem o boundary React/TypeScript alcançado pela mudança de runtime. Se qualquer
comando falhar, o grupo permanece incompleto: a causa deve ser diagnosticada e o comando
deve ser executado novamente até terminar com exit 0. Se a falha indicar uma possível
regressão mais ampla no frontend, escalar a validação para a suíte Vitest completa com
`pnpm --dir frontend/artifacts/neurostack test` antes de concluir o grupo.

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Como Master Admin, buscar um trecho do nome de um builder mostra esse builder e
  oculta os demais.
- [ ] Buscar um trecho do nome de uma clínica mostra o builder pai; ao expandi-lo, a
  clínica correspondente e as demais clínicas vinculadas continuam disponíveis.
- [ ] Repetir as buscas com variação de caixa e espaços externos produz o mesmo resultado.
- [ ] Buscar pelo e-mail do builder continua funcionando e limpar o campo restaura todos
  os cards.
- [ ] Um termo inexistente mostra o estado “Nenhum builder encontrado” com o termo
  informado.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| O usuário esperar que apenas a clínica correspondente apareça dentro do card | Low | Manter todas as clínicas preserva contexto e ações; o wish explicita esse comportamento e o QA o valida. |
| Payload futuro ficar grande demais para filtro local | Low | Manter o desenho atual; adotar busca server-side somente após medição de latência ou paginação da API. |
| Nomes com e sem acento não corresponderem | Low | Preservar a semântica atual de substring; abrir normalização de diacríticos quando houver requisito explícito ou evidência de uso. |

---

## Review Results

### 2026-09-06 — Plan Review

- **Target SHA-256:** `69e5578f6c1bae8774c270576d35e280521513101aa8f59a472fcc5987da9f1b`
- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/plan_review_master_admin_search`
- **Evidence:** estrutura e typecheck passaram; o comando de build não fornecia as
  variáveis obrigatórias `PORT` e `BASE_PATH`.
- **Gaps:** corrigir o ambiente do build, tornar preciso o critério de regressão,
  automatizar a sequência colapsado→expandido, declarar escalonamento de validação e
  explicitar que somente o e-mail do builder é pesquisável.

### 2026-09-06 — Plan Re-review (Fix Loop 1/2)

- **Target SHA-256:** `a2d510d9ab41e4ddf330a01b6348a68440e48a059d800c47a994564aa46df4d5`
- **Verdict:** SHIP
- **Reviewer:** `/root/plan_review_master_admin_search`
- **Checks:** `sha256sum` correspondeu ao alvo; `git diff --check` e
  `pnpm --dir frontend/artifacts/neurostack typecheck` saíram com código 0; o comando de
  build corrigido fornece as duas variáveis exigidas pelo Vite.
- **Gaps:** nenhum. O teste focado e o build são gates futuros da execução; não foram
  rodados no review read-only porque o arquivo de teste ainda não existe e o build
  reescreve `dist/public`.

### 2026-09-06 — Group 1 Execution Review

- **Task:** `t_mtpt2dqz164947c1`
- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/execution_review_master_admin_search`
- **Target:** produção `341f2310f9cbc50c96f3249d55df0ea0d13b7058b0ea4b0cd5c74994c5585128`;
  teste `2378651ecd9630b20904a7678e78e1dde459830f44a4a9dbcea7d651e5fe9a80`.
- **Evidence:** typecheck e diff-check passaram; a execução ampla registrou 153 testes
  verdes e uma única falha no novo teste, que configurava o mock mas não o instalava
  como `globalThis.fetch`.
- **Gaps:** conectar e limpar o mock global, obter os três gates verdes e corrigir o
  comando do Vitest para filtrar realmente o arquivo planejado.

### 2026-09-06 — Group 1 Execution Re-review (Fix Loop 1/2)

- **Verdict:** SHIP
- **Reviewer:** `/root/execution_review_master_admin_search`
- **Target:** WISH `1fa8f78e5573a43c0b2985dc1aa81e1e21abb22263d6662c457ec7299570e14a`;
  produção `341f2310f9cbc50c96f3249d55df0ea0d13b7058b0ea4b0cd5c74994c5585128`;
  teste `2561387bc52aaf1cbfeeb2192b6bbb0774e4cc5b14b69277e0faca534cf48404`.
- **Validation:** teste focado 1/1, typecheck, build de produção e `git diff --check`
  terminaram com exit 0; o build transformou 2.804 módulos e emitiu apenas warnings
  não bloqueantes de sourcemap e tamanho de chunk.
- **QA lens:** SHIP-compatible, sem HIGH/MEDIUM; o teste cobre busca por builder,
  clínica e e-mail do builder, normalização, estado vazio, restauração e expansão manual
  com clínica irmã preservada.
- **Residual risks:** somente as limitações Low já aceitas no plano — sem fuzzy/remoção
  de acentos e eventual pressão futura para busca server-side.

### 2026-09-06 — Group 1 Quality Review

- **Verdict:** SHIP
- **Reviewer:** `/root/quality_review_master_admin_search`
- **Evidence:** escopo limitado aos dois arquivos planejados; nenhuma API, dependência,
  requisição ou estado novo; filtro permanece local em `O(builders + clinics)` por render;
  e-mail administrativo das clínicas não participa do predicado.
- **Finding:** LOW não bloqueante — o teste não fixa explicitamente substring parcial em
  nome/e-mail nem a exclusão do e-mail administrativo da clínica; a implementação atual
  foi verificada como correta.

---

## Files to Create/Modify

```text
frontend/artifacts/neurostack/src/pages/master-admin.tsx
frontend/artifacts/neurostack/src/pages/master-admin-builder-clinic-search.test.tsx
```
