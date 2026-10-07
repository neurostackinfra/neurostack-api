# Wish: Remover painel de contadores da fila de transbordos

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `remove-handoff-summary-panel` |
| **Date** | 2026-10-07 |
| **Author** | Lorenzo |
| **Appetite** | small |
| **Branch** | `wish/remove-handoff-summary-panel` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Remover da fila de transbordos a faixa de seis contadores exibida na imagem de referência, pois os
rótulos e agrupamentos não foram solicitados e tornam a tela confusa. A mudança é exclusivamente de
apresentação no frontend: os KPIs principais, a fila e o contrato de dados permanecem inalterados.

## Scope

### IN

- Remover da página de fila de transbordos o grid com os contadores `Dentro da meta`,
  `Confirmados fora`, `Pendentes vencidos`, `Em maturação`, `Cancelados sem confirmação` e
  `Legado sem medição`.
- Remover as chaves de tradução PT/EN que ficarem sem consumidor após a retirada do grid.
- Atualizar o teste da página para comprovar a ausência dos seis itens e a preservação dos KPIs
  principais ao receber o mesmo payload de estatísticas.

### OUT

- Alterar o cálculo, schema, payload ou endpoint de estatísticas da fila de transbordos.
- Remover ou redesenhar os cards de tempo médio, percentual dentro da meta, legenda de cores,
  filtros, exportação ou cards de atendimento.
- Substituir o painel por outro resumo, novos indicadores ou nova copy.
- Alterar qualquer outra tela ou fluxo de handoff.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Excluir o grid inteiro, sem substituto | O pedido é retirar um painel não solicitado; criar outra visualização repetiria a decisão de produto que gerou a confusão. |
| 2 | Preservar o contrato de estatísticas do backend | Alguns campos continuam úteis para os KPIs principais e remover campos da API ampliaria o risco sem necessidade para ocultar a faixa. |
| 3 | Excluir somente as traduções exclusivas do grid | Evita copy órfã sem afetar textos compartilhados ou a tipagem do restante da tela. |
| 4 | Cobrir PT e EN distinguindo o contador removido do KPI preservado | A estrutura React é compartilhada, mas `Within SLA` é texto tanto do contador removido quanto do título do KPI mantido; a regressão deve provar ausência no grid, não ausência global do texto. |

## Simplicity Case

- **Simplest complete design:** apagar o único bloco JSX que renderiza os seis contadores, remover
  suas doze entradas de tradução e ajustar o teste focado da página.
- **Added machinery:** nenhuma; não haverá prop, estado, flag, endpoint, componente ou abstração nova.
- **Deferred until measured:** qualquer novo resumo analítico só será planejado se houver uma nova
  descrição de produto com indicadores, significado e critérios de aceitação explícitos.
- **Complexity removed:** desaparecem um grid responsivo, seis rótulos localizados e uma asserção de
  renderização; o payload e os cálculos existentes continuam estáveis para evitar migração de API.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [x] A fila de transbordos não renderiza a faixa de seis contadores mostrada na imagem de referência.
- [x] Em português, os cinco rótulos exclusivos `Confirmados fora`, `Pendentes vencidos`,
  `Em maturação`, `Cancelados sem confirmação` e `Legado sem medição` não aparecem, e
  `Dentro da meta` não aparece como contador separado do KPI preservado.
- [x] Em inglês, os cinco rótulos exclusivos `Confirmed late`, `Unanswered overdue`, `Maturing`,
  `Cancelled unconfirmed` e `Unmeasured legacy` não aparecem; `Within SLA` aparece exatamente uma
  vez, como título do KPI principal preservado, junto de seu gauge e contexto, e não como contador.
- [x] O card de tempo médio continua exibindo valor ou estado sem dados conforme o payload.
- [x] O card de percentual dentro da meta continua exibindo gauge, numerador e denominador.
- [x] A legenda de cores, filtros, exportação e lista de atendimentos permanecem renderizados e funcionais.
- [x] O frontend continua aceitando o payload atual, inclusive os seis campos de contagem removidos apenas da apresentação.
- [x] As chaves de tradução exclusivas do painel não permanecem órfãs nos catálogos PT/EN.
- [x] O teste focado da fila e o typecheck do frontend passam.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 0 — remoção localizada de JSX, traduções e assertions, com teste determinístico existente | `engineer-trivial` / low | Remover o grid e atualizar a regressão focada sem alterar o contrato da API. |

A única wave mantém junto o pequeno diff funcional e sua prova de regressão. Separar código,
traduções e teste criaria ownership sobre os mesmos arquivos sem permitir entrega independente.

## Execution Groups

### Group 1: Remoção do painel e regressão

**Goal:** Retirar completamente a faixa confusa de seis contadores sem afetar os demais elementos da fila de transbordos.

**Deliverables:**
1. Remover o grid de seis contadores de `handoff-queue.tsx`.
2. Remover de `pt.ts` e `en.ts` as seis chaves `handoff_count_*`, após confirmar que não possuem outro consumidor.
3. Atualizar `handoff-queue.test.tsx` para afirmar, em PT/EN, a ausência dos cinco rótulos
   exclusivos e do contador `handoff_count_within`, ao mesmo tempo preservando o KPI principal:
   em EN, `Within SLA` deve ocorrer exatamente uma vez como título do card com gauge/contexto.

**Acceptance Criteria:**
- [x] O bloco JSX entre os cards analíticos e a legenda de cores deixa de existir.
- [x] As doze traduções exclusivas do painel deixam de existir e nenhuma referência `handoff_count_*` permanece no frontend.
- [x] O teste usa um payload que ainda contém as contagens, provando que a remoção é apenas visual e não depende de mudar a API.
- [x] A regressão bilíngue verifica que os cinco rótulos exclusivos de cada idioma estão
  ausentes e que o sexto contador não existe como elemento separado; ela não exige ausência
  global de `Within SLA`/`Dentro da meta`.
- [x] Os KPIs de tempo médio e percentual continuam cobertos; no cenário EN, `Within SLA`
  ocorre exatamente uma vez no título do card percentual, que mantém gauge, numerador,
  denominador e contexto.
- [x] Não há mudança em arquivos de backend, tipos de resposta, filtros, exportação, legenda ou cards de atendimento.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/handoff-queue.test.tsx
pnpm --dir frontend/artifacts/neurostack exec tsc -p tsconfig.json --noEmit
handoff_count_rg_status=0
handoff_count_matches="$(rg -l 'handoff_count_(within|late|overdue|maturing|cancelled|legacy)' frontend/artifacts/neurostack/src)" || handoff_count_rg_status=$?
if [ "$handoff_count_rg_status" -eq 0 ]; then
  printf '%s\n' "$handoff_count_matches"
  exit 1
fi
test "$handoff_count_rg_status" -eq 1
git diff --check -- frontend/artifacts/neurostack/src/pages/handoff-queue.tsx frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx frontend/artifacts/neurostack/src/locales/pt.ts frontend/artifacts/neurostack/src/locales/en.ts
```

O teste focado cobre diretamente a única tela alterada; o typecheck valida a fronteira de chaves i18n
e JSX tipado; a busca negativa impede referências órfãs; e `git diff --check` valida os quatro arquivos
do diff. Como não há alteração de contrato, dependência, configuração, runtime compartilhado ou
backend, a suíte completa e o build não são necessários para este grupo localizado.

Todos os quatro comandos formam o gate do grupo. Qualquer falha mantém o Group 1 incompleto:
diagnosticar e corrigir a causa e então executar novamente o gate inteiro. Se o teste focado revelar
indício de regressão mais ampla no frontend, escalar a validação para o Vitest completo do pacote
antes da re-review. Falhas de ambiente ou ferramenta devem ser reportadas, corrigidas ou repetidas;
nenhum comando pode ser dispensado, ignorado ou marcado como aprovado sem execução válida.

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Em desktop e mobile, não existe espaço vazio nem grid residual entre os dois KPIs principais e a legenda de cores.
- [ ] Em PT e EN, os cinco rótulos exclusivos removidos não aparecem e não existe um sexto
  contador separado; o título equivalente a `Within SLA` permanece uma única vez no KPI principal
  com gauge e contexto.
- [ ] Tempo médio, percentual dentro da meta, legenda, filtros, exportação e lista continuam visíveis e utilizáveis.
- [ ] Respostas do endpoint com todos os campos atuais continuam sendo consumidas sem erro de console.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| O rótulo `Dentro da meta` também aparece dentro do texto contextual do KPI principal | Medium | Remover somente a chave `handoff_count_within`; manter e testar `handoff_sla_context`, distinguindo o card principal do rótulo isolado do grid. |
| O texto inglês `Within SLA` é compartilhado pelo contador removido e pelo título do KPI principal | High | Não usar ausência global: remover somente `handoff_count_within` e testar que `Within SLA` ocorre exatamente uma vez como título do KPI com gauge/contexto. |
| Remoção acidental dos KPIs ou da legenda adjacente | Medium | Limitar o diff ao bloco exato e manter as asserções focadas dos elementos vizinhos. |
| Campos de contagem parecerem sem uso no frontend após a remoção | Low | Preservar o schema e o payload; limpeza de contrato backend está explicitamente fora do escopo. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — Loop 1 — 2026-10-07T23:09:49Z

- **Reviewer:** `/root/remove_panel_plan_review`.
- **Reviewed SHA-256:** `93fcd9a42cf9537dae14ea2df43016143b0534fa2f8a2487b5a9cdeabc1841c5`.
- **Verdict:** FIX-FIRST.
- **Evidence:** o painel é um bloco JSX isolado; as chaves `handoff_count_*` são exclusivas do grid;
  o contrato atual pode ser preservado; os quatro comandos propostos são proporcionais ao diff.
- **HIGH corrigido no loop 1:** a ausência global de `Within SLA` contradizia a preservação do KPI
  inglês; o plano agora diferencia o título mantido do contador removido.
- **MEDIUM corrigido no loop 1:** o grupo agora define repetição integral do gate, escalonamento para
  Vitest completo diante de regressão mais ampla e tratamento não dispensável de falhas de ambiente.

### Plan Review — Loop 2 — 2026-10-07T23:13:15Z

- **Reviewer:** `/root/remove_panel_plan_review`.
- **Reviewed SHA-256:** `4a4869f5be2fe2008ce9b9370b043e0123fcaa4dfcee19e806047551ce96e49d`.
- **Verdict:** FIX-FIRST.
- **Evidence:** a ambiguidade PT/EN foi resolvida e sete critérios do plan review passaram; teste
  focado, typecheck, busca de órfãos e diff check continuam proporcionais ao alcance.
- **HIGH pendente para o loop 2:** o `|| true` da busca negativa também mascarava erro do `rg`;
  o gate deve aceitar somente o status 1 de ausência e falhar tanto em matches quanto em erro.

### Plan Review — Final — 2026-10-07T23:15:08Z

- **Reviewer:** `/root/remove_panel_plan_review`.
- **Reviewed SHA-256:** `0f028beffdd7f50b4de32da37b0bfd2e6eadd2dc8396327d1c6a2f2a9f06b613`.
- **Verdict:** SHIP, sem gaps.
- **Evidence:** os oito critérios do plan review passaram; o bloco é isolado, o payload atual é
  preservado, as doze traduções são exclusivas do painel e a regressão bilíngue distingue o KPI
  mantido do contador removido.
- **Gate proof:** matches/status 0 falha, ausência/status 1 passa e erros de ferramenta/status maior
  que 1 falham; o grupo exige repetição integral dos gates e escalonamento quando necessário.

### Execution Review — Group 1 — 2026-10-07T23:32:04Z

- **Reviewer:** `/root/remove_panel_execution_review`.
- **Target:** diff vivo não commitado dos quatro arquivos do grupo; hashes dos arquivos permaneceram
  estáveis antes e depois da validação.
- **Verdict:** SHIP, sem gaps.
- **Evidence:** Vitest focado passou 38/38, typecheck passou, a busca `handoff_count_*` retornou o
  status esperado 1 e `git diff --check` passou. O grid isolado e as doze traduções foram removidos;
  tipos, fixture com contagens e contrato da API foram preservados.
- **Scope:** mudanças concorrentes pré-existentes nos locales foram preservadas e excluídas do
  escopo; nenhum backend, filtro, export, legenda ou fluxo de card foi alterado pelo grupo.

### QA Advisory — Group 1 — 2026-10-07

- **Reviewer:** `/root/remove_panel_qa_lens`.
- **Verdict:** SHIP, sem gaps de QA.
- **Evidence:** a regressão bilíngue falha se a faixa antiga voltar, mantém todos os campos de
  contagem na fixture e preserva título, gauge e contexto do KPI inglês `Within SLA`.

### Quality Review — Group 1 — 2026-10-07T23:34:37Z

- **Reviewer:** `/root/remove_panel_quality_review`.
- **Reviewed WISH SHA-256:** `409835e43c2ce7f3e6be0da6105321ab4b9d0e0c0ef81ddbf04ce5c8f4c02fb2`.
- **Verdict:** SHIP, sem gaps CRITICAL, HIGH, MEDIUM ou LOW.
- **Evidence:** nenhuma nova abstração, estado, flag, dependência, superfície de segurança ou custo
  de renderização; contrato e acessibilidade do KPI foram preservados, e o DOM ficou menor.
- **Residual não bloqueante:** confirmação visual responsiva permanece no QA pós-merge previsto.

### Aggregate Validation — Group 1 — 2026-10-07

- **Orchestrator focused test:** `vitest ... src/pages/handoff-queue.test.tsx` — PASS, 1 arquivo,
  38/38 testes, exit 0, duração 181,58s.
- **TypeScript:** `tsc -p tsconfig.json --noEmit` — PASS, exit 0.
- **Orphan-key gate:** busca `handoff_count_*` retornou exatamente status 1, sem matches.
- **Formatting:** `git diff --check` nos quatro arquivos do grupo — PASS, exit 0.
- **Reviewed file hashes:** source `3e543046…`, test `73e5f18d…`, PT `2a13c14…`, EN `29e42f50…`;
  permanecem iguais aos alvos das revisões independentes.
- **Scope rationale:** teste focado e typecheck cobrem o diff React/i18n localizado; a busca negativa
  prova a limpeza das chaves e o diff check cobre os quatro arquivos. Nenhum runtime compartilhado,
  schema, dependência, configuração ou backend mudou, portanto build e suíte integral não foram
  necessários.

---

## Files to Create/Modify

```text
frontend/artifacts/neurostack/src/pages/handoff-queue.tsx
frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
```
