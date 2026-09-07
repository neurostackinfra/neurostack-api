# Wish: Ocultar estágio nas filas de handoff e inativos

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `hide-stage-in-queues` |
| **Date** | 2026-09-06 |
| **Author** | Codex |
| **Appetite** | small |
| **Branch** | `wish/hide-stage-in-queues` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Remover a exibição de estágio dos itens da Fila de Handoff e da Fila de Inativos para que operadores não sejam induzidos por uma classificação ainda sujeita a muitos erros. O estágio continua disponível no histórico/detalhe de trace durante os pilotos, permitindo avaliar sua qualidade antes de decidir por ocultação adicional ou por uma evolução apoiada pelas intenções completas e integrações de CRM e agenda.

Este é um direct wish porque a mudança é determinística, limitada à apresentação e possui fronteiras objetivamente testáveis: ocultar somente nas duas filas e preservar contratos e trace. Não há decisão exploratória que exija brainstorm.

## Scope

### IN

- Remover rótulo, valor e espaço visual de estágio dos cards da Fila de Handoff em todos os modos de dados, inclusive demonstração/investidor.
- Remover cabeçalho, valor e coluna responsiva de etapa do funil da Fila de Inativos.
- Remover as traduções exclusivas desses campos de fila que ficarem sem uso.
- Adicionar regressões automatizadas que comprovem que payloads contendo estágio não o exibem nas duas filas e que o estágio permanece visível no histórico de trace.

### OUT

- Alterar, remover ou renomear os campos `estagio_atual` e `funnel_stage` dos contratos de API, modelos, validação de payload ou persistência.
- Ocultar ou alterar o estágio no histórico/detalhe de trace, no simulador, no CRM ou em qualquer tela fora das filas de handoff e inativos.
- Alterar a exportação CSV; ela permanece como está e atualmente não contém estágio.
- Melhorar a inferência de estágio, completar intenções ou implementar integrações com CRM e agenda neste wish.
- Criar feature flag, configuração por clínica ou mecanismo de rollout para controlar a visibilidade do estágio.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Fazer uma mudança estritamente de apresentação nas duas filas | O problema atual é confiança na informação exibida; preservar o dado e o contrato evita impacto em produtores, integrações e análise dos pilotos. |
| 2 | Manter o estágio no histórico de trace e protegê-lo com teste de regressão | O trace é a superfície escolhida para observar a qualidade da classificação durante os pilotos. |
| 3 | Remover também o espaço de layout e as traduções exclusivas das filas | Evita colunas vazias, lacunas em desktop/mobile e chaves órfãs que sugeririam suporte ainda ativo. |
| 4 | Não criar configuração ou feature flag | A regra é única para todos os usuários e ambientes neste momento; uma opção aumentaria estados e testes sem requisito atual. |
| 5 | Tratar as filas como superfícies operacionais e o trace como superfície diagnóstica | As filas orientam ações e não devem expor uma classificação ainda pouco confiável; histórico e detalhe de trace permanecem disponíveis para avaliar a qualidade durante os pilotos. |
| 6 | Preservar exatamente a apresentação atual do estágio no trace | No detalhe, o valor usa as traduções `stage_*`, embora o cabeçalho seja hardcoded; no histórico, o valor é exibido cru. Este wish não adiciona tradução nem altera esse runtime. |

## Simplicity Case

- **Simplest complete design:** retirar os dois fragmentos de renderização, ajustar a grade da fila de inativos e atualizar testes/traduções do mesmo pacote frontend.
- **Added machinery:** um novo harness `handoff-queue.test.tsx` e assertions de regressão nas suítes existentes de inativos, histórico e detalhe de trace; eles são necessários para distinguir “oculto nas filas” de uma remoção acidental no trace.
- **Deferred until measured:** qualquer ocultação ou correção do estágio no trace fica para um novo wish após os pilotos registrarem se a identificação sem integrações externas é confiável; completar as intenções e conectar CRM/agenda são gatilhos adicionais para reavaliar onde o estágio pode voltar a ser exibido.
- **Complexity removed:** nenhuma migração, mudança de endpoint, transformação de payload, flag, preferência por clínica ou lógica condicional de rollout.

## Dependencies

**depends-on:** none
**blocks:** none

### Ownership overlap preflight

A execução começa verificando os hashes abaixo. Eles registram o baseline observado no planejamento, não uma autorização para sobrescrever mudanças posteriores:

| Arquivo compartilhado | Baseline SHA-256 | Ownership concorrente |
|---|---|---|
| `frontend/artifacts/neurostack/src/components/trace-viewer.test.tsx` | `bd5550174bd3231d5111a4a1c95ce6808921138b4a1e113c417fb066a1716813` | wish `trace-context-cards` |
| `frontend/artifacts/neurostack/src/locales/pt.ts` | `4af10b17809914d49e4eda8b40b2d7bdfa79b021787a34cca1d6150234404e8f` | wishes `trace-context-cards` e `menu-demo` |
| `frontend/artifacts/neurostack/src/locales/en.ts` | `a81ab26e544329eb3d5a65b67af97a4cf896343ff75086bd501c87559a3c9b52` | wishes `trace-context-cards` e `menu-demo` |

Se algum hash divergir no preflight, o executor deve inspecionar e integrar o conteúdo atual, preservando as mudanças desses wishes; não deve restaurar o baseline nem substituir o arquivo inteiro. Essa preservação explícita resolve o overlap sem criar hard dependency entre os wishes.

## Success Criteria

- [ ] A Fila de Handoff não mostra o rótulo “Estágio”/“Stage” nem o valor de `estagio_atual`, mesmo quando o payload ou o mock de investidor contém esse campo.
- [ ] A Fila de Inativos não mostra o cabeçalho “Etapa do funil”/“Funnel stage” nem o valor de `funnel_stage`, mesmo quando a API o envia.
- [ ] A grade desktop da Fila de Inativos passa de sete para seis áreas coerentes, sem coluna vazia, e o layout mobile não reserva um campo para estágio.
- [ ] Os contratos e validadores das filas continuam aceitando os campos de estágio existentes; nenhum arquivo backend ou cliente gerado muda.
- [ ] O detalhe de trace continua renderizando o valor do estágio traduzido via `stage_*` sob o cabeçalho hardcoded existente, enquanto o histórico continua renderizando o valor cru.
- [ ] As chaves `handoff_stage` e `inactive_funnel_stage` são removidas de PT e EN somente se não houver referências restantes.
- [ ] Os testes focados das duas filas e do trace passam.
- [ ] O typecheck do pacote frontend passa sem erros.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 0 — mudança local e determinística, com um novo harness de fila e regressões focadas em suítes existentes | `engineer-trivial` / low | Remover estágio das duas filas, ajustar layout/i18n e proteger a permanência no histórico e detalhe de trace. |

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

### Group 1: Ocultar estágio nas filas e preservar o trace

**Goal:** Remover toda indicação visual de estágio das filas de handoff e inativos sem alterar os dados transportados nem a observabilidade disponível no trace.

**Deliverables:**
1. `frontend/artifacts/neurostack/src/pages/handoff-queue.tsx` sem a linha de estágio dos cards, mantendo `estagio_atual` no tipo e nos dados recebidos.
2. `frontend/artifacts/neurostack/src/pages/inactive-queue.tsx` sem cabeçalho/campo de etapa do funil e com as definições de grid desktop reduzidas de sete para seis colunas, mantendo `funnel_stage` no tipo e no validador da resposta.
3. `frontend/artifacts/neurostack/src/locales/pt.ts` e `frontend/artifacts/neurostack/src/locales/en.ts` sem as chaves exclusivas `handoff_stage` e `inactive_funnel_stage`, preservando todas as chaves `stage_*` usadas pelo trace.
4. Novo teste/harness `frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx` criado para afirmar a ausência do rótulo e do valor mesmo com `estagio_atual` no payload/mock, e teste existente da fila de inativos atualizado com a regressão equivalente.
5. Regressões em `frontend/artifacts/neurostack/src/components/trace-viewer.test.tsx` e `frontend/artifacts/neurostack/src/pages/traces.test.tsx` afirmando que o estágio continua visível, respectivamente, no detalhe e no histórico, sem mudança em `trace-viewer.tsx` ou `traces.tsx`.

**Acceptance Criteria:**
- [ ] Payloads com estágio continuam sendo validados e carregados normalmente nas duas filas.
- [ ] Nenhum rótulo ou valor de estágio é renderizado nos cards/linhas de handoff e inativos, em viewport desktop ou mobile.
- [ ] A fila de inativos mantém alinhamento entre seus seis cabeçalhos e seis campos responsivos.
- [ ] O modo investidor da fila de handoff também não revela `estagio_atual` dos mocks estáticos.
- [ ] O detalhe de trace continua mostrando o valor do estágio via `stage_*` sob o cabeçalho hardcoded existente; o histórico continua mostrando o valor cru, sem tradução nova.
- [ ] Não restam referências a `handoff_stage` ou `inactive_funnel_stage` após a remoção das traduções.

**Validation:**
```bash
set -e
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/handoff-queue.test.tsx src/pages/inactive-queue.test.tsx src/components/trace-viewer.test.tsx src/pages/traces.test.tsx
pnpm --dir frontend/artifacts/neurostack run typecheck
```

O recorte cobre diretamente as duas superfícies alteradas e as fronteiras de histórico e detalhe que devem permanecer intactas; `handoff-queue.test.tsx` será criado como parte do grupo, e o typecheck alcança as chaves tipadas de i18n e as mudanças JSX do pacote.

**depends-on:** none

---

## Aggregate Integration Gate

```bash
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
```

O build do artefato é mantido separadamente como gate agregado do pacote antes da entrega.

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Com uma clínica selecionada e um handoff cujo payload contenha `estagio_atual`, o card não mostra estágio e os demais dados/ações permanecem alinhados e funcionais.
- [ ] Com uma clínica selecionada e uma sessão inativa cujo payload contenha `funnel_stage`, a tabela desktop e o card mobile não mostram estágio nem espaço vazio correspondente.
- [ ] Ao abrir o detalhe de um trace com estágio conhecido, o valor continua usando `stage_*` sob o cabeçalho hardcoded existente.
- [ ] No histórico de traces, o estágio continua visível como valor cru, sem tradução nova.
- [ ] Em PT e EN, nenhuma das filas exibe rótulos de estágio; a apresentação preexistente do trace continua inalterada.
- [ ] A exportação CSV permanece inalterada e sem coluna de estágio.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Remover o JSX sem ajustar o template de colunas pode deixar a fila de inativos desalinhada | Medium | Alterar cabeçalho e linha para a mesma grade de seis colunas e cobrir estrutura/conteúdo em teste. |
| Uma limpeza ampla de traduções pode afetar o trace | Medium | Remover somente as duas chaves exclusivas das filas e testar explicitamente as chaves `stage_*` no trace. |
| Código futuro pode voltar a expor o campo porque ele permanece no payload | Low | Assertions negativas nas duas suítes de fila documentam e travam a política de apresentação. |
| Os pilotos podem concluir que o estágio também não é confiável no trace | Medium | Registrar a conclusão como gatilho para um novo wish; não antecipar essa decisão neste escopo. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — 2026-09-06T14:26:55Z — FIX-FIRST

- **Context:** plan review da working tree não commitada.
- **Reviewed SHA-256:** `1a98423f8e7125214e681b9f406bfa04e071667f1152eec810bca8e935879a35`.
- **Evidence:** escopo, simplicidade, grupo, critérios e contratos foram confrontados com as implementações atuais das duas filas e do trace; `handoff-queue.test.tsx` ainda não existia; o build exige `PORT` e `BASE_PATH`; três arquivos planejados já continham mudanças de outros wishes.
- **Gaps:** HIGH — gate agregado inexequível; MEDIUM — faltavam justificativa do direct wish e protocolo para ownership concorrente; LOW — o novo teste de handoff estava descrito como existente. A lens advisory acrescentou a distinção operacional/diagnóstica, a fronteira do CSV e a cobertura separada de histórico e detalhe.
- **Resolution:** encaminhado ao fix loop 1/2; o plano passou a conter o comando de build completo, preflight com hashes, teste novo identificado, escopo do CSV e regressões para ambas as superfícies de trace.

### Plan Re-review — 2026-09-06T14:34:26Z — SHIP

- **Context:** plan review da working tree não commitada.
- **Reviewed SHA-256:** `021ff04a88a95b1d7bff2b63a44e367933ce4b756687611cfe436a25531a74b0`.
- **Evidence:** estrutura completa e sem placeholders; baselines concorrentes conferidos; manifest distingue o teste novo; comportamento atual das filas, trace e CSV confirmado; quatro suítes focadas, typecheck e build com ambiente completo formam validação proporcional e executável; `git diff --check` passou.
- **Validation note:** os testes de runtime futuros não foram executados durante o review read-only porque a implementação ainda não existe; o `wishes:lint` oficial permanece indisponível neste repositório.
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW.
- **Verdict:** SHIP. Plano aprovado para execução.

### Group 1 Execution Review — 2026-09-06T14:59:28Z — SHIP

- **Context:** task `t_mtpwigv7c34cc7b9`, working tree não commitada, base `a7dcf8fb856cc96b32910820af5335d4845141f2`.
- **Evidence:** os oito hashes pós-engineer foram confirmados; `estagio_atual` e `funnel_stage` permanecem nos tipos, mocks e validação de payload, mas não renderizam nas filas; header e rows de inativos usam seis áreas; detalhe e histórico de trace preservam seus comportamentos distintos; nenhuma fonte backend, CSV, cliente gerado ou runtime de trace mudou.
- **Validation evidence reviewed:** engineer registrou handoff 2/2, inactive 24/24, trace detail 18/18 e trace history 3/3, além de typecheck, build e diff-check com exit 0.
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW.
- **Verdict:** SHIP.

### Group 1 Quality Review — 2026-09-06T14:59:48Z — SHIP

- **Evidence:** assertions negativas usam marcadores exclusivos depois de provar que payload/mock carregou; contratos permanecem; timers/fetch não ganharam vazamentos; mudanças concorrentes de `trace-context-cards` e `menu-demo` foram preservadas; nenhuma abstração, configuração ou estado novo foi introduzido.
- **Finding:** LOW — o teste de inativos verifica a classe de seis áreas na row, enquanto a contagem do header foi confirmada por inspeção. Hardening futuro com `data-testid` no header é opcional e não bloqueia o critério atual.
- **Verdict:** SHIP.

### Orchestrator Validation — 2026-09-06T15:13:37Z — PASS

- **Focused tests:** `./node_modules/.bin/vitest run --config vitest.config.ts src/pages/handoff-queue.test.tsx src/pages/inactive-queue.test.tsx src/components/trace-viewer.test.tsx src/pages/traces.test.tsx` — 4 arquivos, 47/47 testes, exit 0.
- **Typecheck:** `frontend/node_modules/.bin/tsc -p frontend/artifacts/neurostack/tsconfig.json --noEmit` — exit 0.
- **Aggregate build:** `PORT=3001 BASE_PATH=/ ./node_modules/.bin/vite build --config vite.config.ts` no pacote `frontend/artifacts/neurostack` — exit 0; somente warnings preexistentes de sourcemap e chunk size.
- **Scope checks:** `git diff --check` passou nos arquivos rastreados; o novo teste não contém erros de whitespace; `rg` encontrou zero referências a `handoff_stage|inactive_funnel_stage`; hashes finais permanecem iguais ao snapshot revisado.
- **Environment note:** a invocação inicial via wrapper `pnpm --dir` tentou reparar o workspace e falhou no hook `preinstall` porque o wrapper não propagou `npm_config_user_agent`; os mesmos binários locais declarados pelos scripts foram executados diretamente e passaram, sem alterar lockfiles ou dependências.

---

## Files to Create/Modify

```
# Create
frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx

# Modify
frontend/artifacts/neurostack/src/pages/handoff-queue.tsx
frontend/artifacts/neurostack/src/pages/inactive-queue.tsx
frontend/artifacts/neurostack/src/pages/inactive-queue.test.tsx
frontend/artifacts/neurostack/src/components/trace-viewer.test.tsx
frontend/artifacts/neurostack/src/pages/traces.test.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
```
