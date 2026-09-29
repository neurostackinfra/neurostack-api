# Wish: Fonte única de modalidade por serviço

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `clinic-modality-source-of-truth` |
| **Date** | 2026-09-28 |
| **Author** | Codex |
| **Appetite** | small |
| **Branch** | `wish/clinic-modality-source-of-truth` |
| **Repos touched** | `neurostack-api` (frontend in-repo) |
| **Design** | _No brainstorm — direct wish_ |

## Summary

A aba **Operação** manterá somente a modalidade geral da clínica — online,
presencial ou ambas — e deixará de exibir ou reenviar listas livres de serviços
online e presenciais. A modalidade de cada serviço continuará sendo definida
exclusivamente em seu card na aba **Serviços**, eliminando a edição redundante e
as possíveis contradições entre duas superfícies administrativas.

## Scope

### IN

- Manter na aba **Operação** o seletor global `service_format` com as opções
  Online e Presencial, Apenas Online e Apenas Presencial.
- Remover da interface as caixas **Serviços realizados Online** e **Serviços
  realizados Presencialmente**, independentemente da modalidade global escolhida
  e do papel editável ou somente leitura.
- Remover do frontend o estado, a hidratação, os valores de demonstração e o
  envio de `online_services_desc` e `presential_services_desc` no PATCH de
  configuração.
- Continuar aceitando respostas GET que contenham os dois campos legados sem
  renderizá-los, reenviá-los, zerá-los ou impedir o restante da edição.
- Preservar a modalidade específica `services[].service_format`, seus três
  valores e a validação de compatibilidade com a modalidade global.
- Cobrir a ausência das caixas, a permanência do seletor global, a omissão dos
  campos legados no PATCH e o fluxo de modalidade por serviço com testes
  frontend automatizados.

### OUT

- Remover `online_services_desc` ou `presential_services_desc` dos modelos,
  mapeamentos, resposta da API, clonagem ou armazenamento existentes.
- Apagar, migrar, preencher com `null` ou executar backfill dos valores legados
  já persistidos; a compatibilidade permanece não destrutiva.
- Alterar os tokens `online|presential|both`, a matriz de compatibilidade entre
  clínica e serviço ou a precedência da modalidade explícita do serviço.
- Tornar a modalidade global obrigatória, normalizar valores globais históricos
  ou remover o fallback atual de canal único para serviços legados sem modalidade.
- Derivar automaticamente a modalidade global a partir do catálogo ou criar
  modalidade por profissional, unidade, agenda ou faixa de horário.
- Renomear campos, textos ou opções dos cards da aba **Serviços**, inclusive o
  rótulo mostrado na imagem de referência.
- Alterar prompt, pipeline, OpenAPI, schema de banco ou criar migration.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | A mudança será frontend-only | Os dois textos livres não são consumidos pelo pipeline ou pelo prompt; removê-los da superfície e do payload elimina a fonte editável de conflito sem ampliar o contrato. |
| 2 | `service_format` global continuará representando a capacidade geral da clínica, enquanto `services[].service_format` continuará representando o serviço específico | Uma clínica híbrida pode possuir serviços online, presenciais ou híbridos; os dois níveis têm responsabilidades diferentes e já possuem validação explícita. |
| 3 | O novo frontend omitirá os campos legados no PATCH em vez de enviar `null` | O PATCH é parcial. Omissão evita apagamento silencioso de dados históricos e permite rollback/compatibilidade com escritores antigos sem manter as caixas na UI atual. |
| 4 | Backend, persistência e clonagem continuarão tolerando os dois campos legados | Não há requisito de exclusão de dados nem inventário completo de escritores externos que justifique uma quebra ou migration. Como os valores são inertes no runtime, essa tolerância não recria conflito para a IA. |
| 5 | Este wish substitui apenas a decisão de UI do predecessor `clinic-service-modality` que mantinha os textos editáveis | A modalidade por serviço do predecessor já está mergeada em `origin/main`; todo o restante de seu contrato e de suas proteções permanece válido. |
| 6 | A regressão será provada no teste da aba Operação e no teste existente da modalidade por serviço | Os dois testes cobrem os limites alterados: ausência/serialização da antiga UI e preservação do catálogo específico. |

## Simplicity Case

- **Simplest complete design:** excluir os dois `Textarea` condicionais e toda a
  sua fiação local — mock, estado, hidratação e propriedades do payload — sem
  tocar no seletor global nem nos cards de serviços.
- **Added machinery:** nenhuma em produção; somente asserções de regressão nos
  testes já usados pela página de configuração.
- **Deferred until measured:** remover os campos legados do contrato e do banco
  somente após inventário de escritores externos, período de depreciação e uma
  decisão explícita sobre retenção/migração dos dados. Tornar a modalidade global
  obrigatória ou retirar o fallback legado exige métricas de adoção que provem
  não existirem registros/clientes antigos dependentes desses comportamentos.
- **Complexity removed:** duas fontes de texto livre, dois estados React, duas
  etapas de hidratação/serialização e os conflitos informacionais entre Operação
  e Serviços deixam de existir na superfície administrativa.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [ ] A aba **Operação** exibe exatamente um controle de modalidade geral da
  clínica e mantém as opções Online e Presencial, Apenas Online e Apenas
  Presencial nos modos editável e somente leitura.
- [ ] As caixas e rótulos **Serviços realizados Online** e **Serviços realizados
  Presencialmente** não são renderizados para `online`, `presential`, `both` nem
  para modalidade global vazia.
- [ ] Uma resposta GET contendo `online_services_desc` e
  `presential_services_desc` continua carregando a página normalmente, mas os
  valores não são hidratados em controles invisíveis.
- [ ] Ao salvar, o PATCH mantém `service_format` com o valor selecionado e não
  contém as chaves `online_services_desc` ou `presential_services_desc`.
- [ ] O save omite, em vez de zerar, as descrições legadas; o contrato parcial do
  backend portanto preserva os valores já armazenados sem que a UI volte a
  tratá-los como fonte de verdade.
- [ ] Cada card nomeado da aba **Serviços** continua exibindo sua modalidade
  própria com Online, Presencial e Online e Presencial, conforme a referência.
- [ ] A hidratação, edição, validação e serialização de
  `services[].service_format` continuam passando para os três valores canônicos.
- [ ] As incompatibilidades atuais entre modalidade global e modalidade por
  serviço continuam bloqueando o PATCH, sem alterar dados silenciosamente.
- [ ] O frontend não mantém estado, mock ou lógica de hidratação/serialização
  dedicados às duas descrições livres removidas.
- [ ] Nenhum arquivo de backend, prompt, persistência, OpenAPI ou migration é
  alterado por esta entrega.
- [ ] Testes frontend focados, suíte frontend completa, typecheck e build de
  produção passam.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — formulário com estado e serialização (+2) | `engineer-standard` / medium | Remover as listas redundantes e sua fiação, preservando e testando os dois níveis canônicos de modalidade. |

### Aggregate gate

Após o grupo, executar primeiro os dois testes focados e depois a suíte frontend
completa, typecheck e build. Uma falha deve ser corrigida dentro do escopo; se o
mesmo erro também ocorrer na árvore-base ou vier de mudanças concorrentes, o
executor registra a evidência e separa o débito em vez de reverter trabalho alheio.

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

### Group 1: Remover listas redundantes da aba Operação

**Goal:** Fazer da aba Serviços a única superfície que descreve a modalidade de
cada serviço, mantendo em Operação somente a capacidade global da clínica.

**Deliverables:**
1. Excluir de `clinic-config.tsx` os dois controles condicionais e seus estados,
   hidratação, dados de demonstração e propriedades do payload, preservando
   `service_format` e todo o array `services`.
2. Estender o teste da aba Operação com uma configuração que contenha os campos
   legados para provar ausência visual, hidratação do seletor global e omissão
   das chaves antigas no PATCH.
3. Reexecutar o teste de modalidade por serviço para provar que os três valores,
   o round-trip e as validações existentes permanecem intactos.

**Acceptance Criteria:**
- [ ] Nenhum rótulo, `Textarea` ou `data-testid` das duas listas livres aparece
  na aba Operação para qualquer modalidade global.
- [ ] Salvar uma configuração recebida com valores legados envia
  `service_format`, mas omite `online_services_desc` e
  `presential_services_desc` sem enviar `null`.
- [ ] O seletor global continua hidratando e respeitando o modo somente leitura.
- [ ] Os cards de serviço continuam hidratando, editando e serializando
  `online`, `presential` e `both`, com a matriz de compatibilidade inalterada.
- [ ] O diff de produção fica restrito ao frontend; mudanças concorrentes já
  presentes em `clinic-config.tsx` e em seus testes são preservadas.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts \
  src/pages/clinic-config-api-settings.test.tsx \
  src/pages/clinic-config-service-modality.test.tsx &&
pnpm --dir frontend/artifacts/neurostack run test &&
pnpm --dir frontend/artifacts/neurostack run typecheck &&
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
```

Os dois testes focados atingem diretamente a aba e o contrato por serviço; a
suíte completa protege a página compartilhada entre todas as abas, enquanto
typecheck e build validam o JSX/tipos e o artefato entregue. Não há gate Python,
pois nenhum código, contrato ou documento backend entra no diff planejado.

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Em Operação, alternar entre as três modalidades nunca revela uma lista de
  serviços e o save/reload preserva a seleção global.
- [ ] Em Serviços, cada card continua mostrando e persistindo sua própria
  modalidade com os três rótulos da imagem de referência.
- [ ] Uma configuração antiga com as duas descrições livres abre e salva sem
  erro, sem exibi-las e sem apagá-las implicitamente.
- [ ] Perfis somente leitura veem as modalidades global e específicas, mas não
  conseguem editá-las e não veem as caixas removidas.
- [ ] Demais campos e abas da configuração continuam salvando e carregando sem
  regressão observável.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Escritores externos ainda podem enviar as descrições legadas | Low | Manter backend/modelo/clonagem tolerantes e tratar os campos como inertes; remoção contratual exige inventário e depreciação futuros. |
| Valores legados permanecem no JSONB e podem ser confundidos com fonte ativa por manutenção futura | Medium | Documentar neste wish que são compatibilidade dormente e provar no teste que o frontend não os hidrata nem envia; pipeline já não os consome. |
| `clinic-config.tsx` e seu teste possuem alterações concorrentes no worktree | Medium | Editar somente os hunks deste grupo, nunca reverter mudanças alheias e validar o diff por arquivo antes do handoff. |
| Remover estado/payload pode afetar o grande formulário compartilhado | Medium | Rodar testes focados, suíte frontend completa, typecheck e build de produção. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — 2026-09-28T19:49:02-03:00

- **Context:** plan review somente leitura sobre working tree não commitada.
- **Reviewer:** `/root/plan_review_modality_source`.
- **Reviewed SHA-256:** `b06a9483684d6356d3551304a53449786dcd9c6977d08c9cc87b76a045037575`.
- **HEAD / origin/main:** `96edb2579ded08c3a1b34549bb73d1ee86a77325` / `1b1a2fe20981a44abb2a1ac3d7ccd0f43713349c`.
- **Evidence:** o reviewer confirmou em `origin/main` que a modalidade por serviço e suas validações já existem; que os dois textos legados são usados pela UI, modelo, roteamento e clonagem, mas não pelo pipeline/prompt; e que o PATCH parcial com `exclude_unset=True` sustenta a omissão não destrutiva. Os dois testes e os scripts `test`, `typecheck` e `build` planejados existem.
- **Structure validation:** o repositório não possui `package.json` raiz nem `wishes:lint`; a verificação manual confirmou seções obrigatórias, dependências, validação não-zero e ausência de placeholders, e `git diff --check` não apontou whitespace inválido.
- **Checklist:** problema e escopo PASS; critérios globais e por grupo PASS; grupo pequeno e entregável PASS; dependências PASS; validação proporcional com rationale e rota de falha PASS; aggregate gate PASS; Simplicity Case PASS.
- **Verdict:** SHIP.
- **Gaps:** nenhum CRITICAL ou HIGH; nenhum gap bloqueante.

### Questioner Advisory — 2026-09-28

- A promessa de fonte única se refere à superfície administrativa atual: os campos legados continuam dormentes no backend por compatibilidade, sem consumo pelo runtime.
- A execução deve parametrizar a ausência das caixas para `online`, `presential`, `both` e vazio, cobrindo perfis editável e somente leitura; a omissão no PATCH se apoia no merge parcial backend já verificado.
- O predecessor `clinic-service-modality` está satisfeito em `origin/main`; seu status documental atrasado não cria dependência ativa para este follow-up.

### Execution Review — Group 1 — 2026-09-28T20:30:28-03:00

- **Reviewer:** `/root/review_modality_group1` (read-only, independente do executor).
- **Reviewed WISH SHA-256:** `4b43be73ed55221faf739c4fae8c31198deeed55d787a7729a4ed24ab812f530`.
- **Reviewed source SHA-256:** `9d701ec60e21439405a85ad90550a8c8ed39c7f356dacaa176f1d891bcd2dfd`.
- **Reviewed operation-test SHA-256:** `ba84e32e30e905a9409066b28f87f8653e06b0be6f678c77ef70041c6a64c7d7`.
- **Preserved service-modality-test SHA-256:** `f29385c277b34affea62124decf87ad62b7dc8deb8e625f9e19aca596ecfc03f`.
- **Evidence:** os controles, estados, mock, hidratação e propriedades de payload legados foram removidos; o seletor global, o catálogo por serviço e a matriz de compatibilidade permaneceram intactos. Os testes focados passaram `64/64`, o teste preexistente de modalidade por serviço ficou sem diff, o typecheck passou e `git diff --check` retornou zero.
- **Concurrent work:** os demais hunks já presentes nos dois arquivos revisados não se sobrepõem à remoção de modalidade e foram preservados.
- **Verdict:** SHIP.
- **Non-blocking notes:** a validação agregada ainda estava sob responsabilidade do orquestrador; avisos `act(...)` já existentes permaneceram como débito LOW.

### Quality Review — Group 1 — 2026-09-28T20:24:31-03:00

- **Reviewer:** `/root/quality_modality_group1` (read-only, independente do executor).
- **Evidence:** busca no código de produção encontrou zero referência a `online_services_desc` e `presential_services_desc`; o PATCH omite as duas chaves sem apagamento destrutivo; a matriz parametrizada cobre `online`, `presential`, `both` e vazio, além do perfil somente leitura, sem sleeps ou alteração do teste de modalidade específica.
- **Verdict:** SHIP, sem gaps CRITICAL ou HIGH.
- **Non-blocking note:** os avisos React `act(...)` são MEDIUM herdado, sem baseline limpo que prove regressão deste grupo.

### Fix Loop — 2026-09-28

- A primeira execução focada passou `60/64`; quatro casos novos consultavam o rótulo de modalidade antes do término da hidratação assíncrona.
- `/root/fix_modality_test_hydration` ajustou apenas o helper de teste para aguardar o rótulo com `findByText` e seus call sites para `await`.
- A validação cirúrgica do fix passou `5/5` casos relevantes, e a repetição dos dois arquivos focados passou `64/64`.

### Aggregate Validation — 2026-09-28T21:12:06-03:00

- **Final source/test hashes:** iguais aos hashes aprovados nos reviews acima; nenhuma mutação posterior nos arquivos do grupo.
- **Focused tests:** `pnpm --config.verify-deps-before-run=false --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts --cache=false src/pages/clinic-config-api-settings.test.tsx src/pages/clinic-config-service-modality.test.tsx` — PASS, `2` arquivos e `64/64` testes.
- **Full frontend suite:** `./node_modules/.bin/vitest run --config vitest.config.ts --cache=false` em `frontend/artifacts/neurostack` — PASS, `28` arquivos e `294/294` testes em `641.05s`.
- **Typecheck:** `../../node_modules/.bin/tsc -p tsconfig.json --noEmit --incremental false` — PASS.
- **Production build:** `PORT=3001 BASE_PATH=/ ./node_modules/.bin/vite build --config vite.config.ts` — PASS, `2804` módulos transformados em `1m56s`.
- **Integrity checks:** `git diff --check` passou; o teste preexistente de modalidade por serviço e manifests/lockfiles não possuem diff; o arquivo de produção contém zero referência aos dois campos legados.
- **Environment note:** uma primeira tentativa agregada coincidiu com instalações `pnpm` e alterações de traces de outros agentes no worktree compartilhado. Após o término dessas operações e restauração das dependências já travadas, a suíte completa acima passou sem falhas. Nenhuma mudança alheia foi revertida.
- **Warnings:** Vitest manteve avisos React `act(...)` preexistentes; Vite manteve avisos de sourcemap e chunk grande. Nenhum aviso bloqueou os gates ou foi atribuído a este grupo.
- **Task tracking:** não havia task row utilizável porque `.genie/genie.db` recusou abertura como banco válido; o grupo foi rastreado no WISH e não foi fabricado um `genie task done`.

---

## Files to Create/Modify

```
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
```
