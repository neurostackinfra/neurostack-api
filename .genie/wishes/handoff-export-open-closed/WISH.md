# Wish: Exportação da Fila de Handoff com Abertos e Atendidos

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `handoff-export-open-closed` |
| **Date** | 2026-09-27 |
| **Author** | Codex |
| **Appetite** | small |
| **Branch** | `wish/handoff-export-open-closed` |
| **Repos touched** | `neurostack-api` |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Alterar o CSV da Fila de Handoff para representar a coorte que entrou no período selecionado, incluindo tanto atendimentos já concluídos quanto atendimentos ainda abertos na fila visual. O arquivo passa a informar o status e o instante de referência de cada linha e, para itens abertos, calcula a idade do handoff até a exportação, tornando o relatório útil para acompanhamento operacional sem criar endpoint, tela ou persistência adicional.

Este é um direct wish porque a necessidade foi confirmada pelo solicitante (abertos e atendidos que entraram no período) e o comportamento atual do endpoint e da tela foi inspecionado no repositório. As decisões restantes são contratos técnicos verificáveis — estados, bordas de data, colunas e limite — e não exigem exploração de produto antes da execução.

## Scope

### IN

- Manter `GET /v1/handoffs/queue/export` e trocar o critério temporal de `data_hora_saida` para `created_at`, com limites inclusivos de calendário no mesmo fuso IANA usado pelo navegador para renderizar/filtrar os cards; converter os limites locais para UTC antes da consulta para manter o predicado sargable/indexável sobre `created_at`.
- Incluir handoffs abertos nos estados `pending` e `accepted` — os mesmos exibidos pela fila visual — além dos atendidos em estado `completed` com `data_hora_saida` preenchida.
- Preservar as cinco colunas existentes na mesma ordem e acrescentar `status` e `exported_at` ao final: `numero_whatsapp,session_id,created_at,data_hora_saida,tempo_espera_segundos,status,exported_at`.
- Para linhas abertas, emitir `data_hora_saida` vazia e calcular `tempo_espera_segundos` como a idade do handoff (`exported_at - created_at`); para linhas concluídas, manter `data_hora_saida - created_at`. O nome legado da coluna é preservado, mas não representa tempo até o primeiro aceite humano.
- Capturar `exported_at` uma única vez por requisição, para que todas as linhas abertas do mesmo arquivo usem o mesmo instante de referência.
- Serializar `created_at`, `data_hora_saida` e `exported_at` normalizados em UTC, no formato ISO-8601 com offset `+00:00`; timestamps naïve devolvidos pelo SQLite de testes são interpretados como UTC antes de qualquer cálculo.
- Ordenar o arquivo por `created_at DESC`, preservando autenticação/isolamento por tenant, BOM UTF-8, CSV compatível com Excel, defaults/combinações parciais de data e a validação atual de janela máxima.
- Consultar no máximo 10.001 registros e rejeitar a exportação com `400 export_limit_10000_exceeded` quando a coorte superar 10.000, em vez de gerar um relatório gerencial silenciosamente truncado.
- Adicionar testes backend específicos do contrato CSV, da seleção por data de entrada, dos estados incluídos/excluídos, do cálculo de espera e do isolamento entre clínicas.
- Fazer a tela enviar `timezone=Intl.DateTimeFormat().resolvedOptions().timeZone` somente no export, para que o CSV represente os mesmos dias locais usados por `ymd(new Date(created_at))`; exibir toast localizado quando o backend retornar `export_limit_10000_exceeded` ou `invalid_timezone`.

### OUT

- Alterar os campos de filtro, o formato visual da fila ou o fluxo de download bem-sucedido; os únicos ajustes frontend são enviar ao export o fuso já usado pelo navegador e tornar visíveis os novos erros acionáveis do export.
- Criar novo endpoint, migration, tabela, coluna persistida de tempo de espera ou job assíncrono de exportação.
- Exportar `in_progress`, `cancelled` e `timeout`; esses estados não aparecem na fila visual atual como abertos e não representam atendimento concluído com baixa pela fila. Alinhar a fila visual com `in_progress` requer uma decisão separada.
- Reparar registros `completed` sem `data_hora_saida`, inclusive conclusões feitas por fluxos que chamam `complete()` em vez do checkout da fila; eles continuam fora até que exista uma regra de saneamento/unificação separada.
- Reconstruir o status histórico “como estava” em um instante passado; o CSV continua refletindo o status atual de cada handoff que entrou no período.
- Adicionar filtros de segurança, intenção, setor ou responsável ao CSV.
- Criar versão paralela/opt-in do contrato CSV; esta alteração expande o endpoint existente de forma aditiva e aceita o risco de consumidores que validam o cabeçalho exato.
- Remover ou paginar o teto de 10.000 linhas.
- Alterar os defaults ou a regra atual de janela máxima quando datas estiverem ausentes/parciais; este wish preserva esses comportamentos e muda somente o campo temporal e o fuso quando a UI envia o novo parâmetro.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Período definido pela entrada (`created_at`) | A pergunta de gestão é “quais atendimentos entraram neste período e qual é a situação deles agora”; filtrar pela saída excluiria os abertos e deslocaria concluídos para outra coorte. |
| 2 | Datas da UI interpretadas no fuso IANA do navegador e convertidas para um intervalo UTC semiaberto `[início, dia seguinte ao fim)` | A própria tela filtra cards pelo calendário local do navegador. Enviar esse fuso ao export faz “a data do filtro” selecionar a mesma coorte, inclusive para builders remotos; chamadas sem `timezone` usam UTC como fallback explícito e determinístico. Fuso IANA inválido retorna `400 invalid_timezone` e orientação localizada na tela. |
| 3 | Estados exportados e valores exatos: `pending`, `accepted`, `completed` | `pending` e `accepted` são os estados que a fila visual consulta hoje; `completed` com `data_hora_saida` representa a baixa. O CSV usa os valores canônicos em inglês, sem tradução ou classificação derivada. `in_progress`, `cancelled` e `timeout` ficam fora. |
| 4 | Preservar o nome `tempo_espera_segundos`, definindo-o para abertos como idade desde a entrada | A fila não armazena `accepted_at` nem `started_at`; portanto não é possível calcular tempo até aceite/início. Para `pending`/`accepted`, o valor mede `exported_at - created_at`, igual à idade exibida pelo contador da fila até o checkout. |
| 5 | Acrescentar `status` e `exported_at` ao final do cabeçalho, sem versionar o endpoint | Mantém posição/formato das cinco colunas existentes, permite distinguir aberto de atendido e torna o cálculo dos abertos auditável. É uma expansão aditiva deliberada do export interno; se surgir consumidor externo com validação de cabeçalho exato, esse fato dispara um contrato versionado separado. |
| 6 | Um único `exported_at` por arquivo e timestamps de saída em UTC ISO-8601 | Evita diferenças entre linhas, permite auditar o cálculo e preserva um formato inequívoco. O filtro usa o calendário local do navegador, mas os valores exportados continuam em UTC com offset explícito. |
| 7 | Preservar defaults e validação de intervalo existentes | A alteração solicitada é aberto+atendido por data de entrada. Sem datas/parâmetros parciais continuam usando as regras atuais, e a checagem existente de diferença maior que 90 dias permanece; corrigir contagem inclusiva/inversão é um bugfix separado. |
| 8 | Falhar quando houver mais de 10.000 linhas | Um CSV parcial sem sinalização produziria totais gerenciais falsos, especialmente porque a inclusão de abertos aumenta o volume. Consultar 10.001 detecta excesso sem remover o teto de segurança; o usuário deve estreitar o período. |
| 9 | Capturar `exported_at` após materializar o resultado | O instante pós-SELECT é posterior aos registros do snapshot normal e serve como referência única antes da serialização, evitando drift por linha sem prometer reconstrução histórica/transacional. |

## Simplicity Case

- **Simplest complete design:** alterar a consulta/serialização do endpoint existente e, na mesma entrega, fazer a tela enviar seu fuso e tratar os dois novos erros acionáveis. O CSV recebe duas colunas ao final; não há mudança de banco, controle ou fluxo de sucesso.
- **Added machinery:** (1) um parâmetro IANA vindo do navegador, necessário para o CSV casar a “data do filtro” que a tela já calcula no fuso local; (2) captura pós-snapshot de `exported_at`, necessária para calcular/auditar a idade dos abertos; (3) uma linha sentinela acima do teto e toasts localizados para limite/fuso inválido, necessários para impedir planilha gerencial parcial ou falha silenciosa. Tudo reutiliza mecanismos existentes.
- **Deferred until measured:** exportação assíncrona/paginada somente se o teto de 10.000 linhas se mostrar insuficiente; seleção dinâmica de colunas somente após pedido de produto; exportação de cancelamentos/timeouts somente após definição do significado gerencial; snapshot histórico somente se houver trilha de transições de status capaz de sustentá-lo.
- **Complexity removed:** sem endpoint novo/versionado, sem migration, sem estado persistido derivado, sem filtro de status na UI, sem dependência nova e sem mudança no mecanismo de download bem-sucedido.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [ ] `GET /v1/handoffs/queue/export` continua exigindo autenticação e `tenant_id`, impedindo exportação cross-tenant para clínica e permitindo apenas os overrides já autorizados para builder/super-admin.
- [ ] O filtro inclusivo `from_date..to_date` é aplicado a `created_at` no fuso IANA enviado pela tela, por limites UTC semiabertos; um registro perto da meia-noite pertence ao mesmo dia no card e no CSV. Sem parâmetro, o endpoint usa UTC; fuso inválido retorna `400 invalid_timezone`.
- [ ] Um handoff criado dentro do período e concluído depois dele é incluído; um handoff criado antes do período e concluído dentro dele é excluído.
- [ ] Um handoff aberto criado antes do período é excluído, mesmo que continue aberto no momento da exportação.
- [ ] Handoffs `pending` e `accepted` criados no período aparecem no CSV com `data_hora_saida` vazia; `status` contém exatamente o valor canônico `pending` ou `accepted`.
- [ ] Handoffs `completed` criados no período e com `data_hora_saida` aparecem com `status=completed`; `in_progress`, `cancelled`, `timeout` e `completed` sem `data_hora_saida` não aparecem.
- [ ] O cabeçalho é exatamente `numero_whatsapp,session_id,created_at,data_hora_saida,tempo_espera_segundos,status,exported_at`, preservando as cinco colunas anteriores e adicionando duas colunas ao final.
- [ ] Para concluídos, `tempo_espera_segundos = data_hora_saida - created_at`; para abertos, a coluna legada mede a idade `exported_at - created_at`, arredondada para uma casa decimal.
- [ ] Todas as linhas de uma mesma resposta repetem o mesmo `exported_at`; todos os timestamps são ISO-8601 normalizados em UTC com offset `+00:00`, inclusive quando o driver de testes devolve datetime naïve.
- [ ] As linhas são ordenadas por `created_at DESC`; uma coorte de até 10.000 linhas é exportada integralmente e uma coorte de 10.001 ou mais retorna `400` com `detail="export_limit_10000_exceeded"`, sem CSV parcial.
- [ ] Resposta vazia contém BOM UTF-8 e somente o cabeçalho; resposta com dados mantém `Content-Type: text/csv; charset=utf-8` e `Content-Disposition` de anexo.
- [ ] Defaults de data, parâmetros parciais e a checagem existente de janela máxima mantêm o comportamento anterior; regressões fixam que esta wish não os redefine.
- [ ] O nome de arquivo do backend permanece `handoffs_{from}_{to}.csv`.
- [ ] Os caminhos autorizados de builder e super-admin continuam funcionando; clínica/API key não consegue trocar de tenant.
- [ ] `export_limit_10000_exceeded` produz toast PT/EN orientando reduzir o período e `invalid_timezone` produz toast PT/EN informando que não foi possível interpretar o fuso; ambos impedem a criação do Blob, enquanto os downloads bem-sucedidos permanecem inalterados.
- [ ] A tela inclui o fuso IANA do navegador somente na URL do export e mantém os filtros locais dos cards, parâmetros operacionais e KPIs inalterados.
- [ ] Testes focados do export passam sem depender de banco externo.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 3 — +2 por estado persistido/relógio/bordas temporais e +1 por alcançar os pacotes Python e frontend; sem migration, dependência nova ou aceitação subjetiva | engineer-standard / high | Entregar de forma coesa o contrato CSV, o fuso da tela, o erro acionável e as regressões backend/frontend |

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

### Group 1: Export misto por data de entrada

**Goal:** Fazer o endpoint existente exportar, para a coorte que entrou no período, tanto os atendimentos ainda abertos quanto os já baixados, com status e tempos coerentes.

**Deliverables:**
1. `src/api/routes/handoff.py` — aceitar `timezone_name: Optional[str] = Query(None, alias="timezone")`, evitando colidir com `datetime.timezone`; quando presente, resolver com `zoneinfo.ZoneInfo`, retornando `400 {"detail":"invalid_timezone"}` para valor inválido; quando ausente, usar UTC. Preservar exatamente as regras atuais que calculam `effective_from`/`effective_to` e validam diferença maior que 90 dias; converter somente os limites efetivos para o intervalo UTC semiaberto correspondente ao fuso.
2. `src/api/routes/handoff.py` — substituir o filtro por saída pelo predicado indexável `created_at >= window_start_utc AND created_at < window_end_utc`, incluindo `pending`/`accepted` e incluindo `completed` somente quando `data_hora_saida IS NOT NULL`; excluir os demais estados, ordenar por `created_at DESC` e consultar `.limit(10001)`.
3. `src/api/routes/handoff.py` — após materializar o resultado, capturar uma única vez `exported_at = datetime.now(timezone.utc)`. Se a linha sentinela 10.001 existir, retornar `400 {"detail":"export_limit_10000_exceeded"}` sem corpo CSV. Caso contrário, normalizar timestamps naïve como UTC, preservar as cinco colunas atuais e acrescentar `status,exported_at` ao final. Atualizar docstring/descrições FastAPI para documentar coorte, fuso, estados, formato UTC, limite e semântica da coluna legada de espera.
4. `tests/api/test_handoff_queue_export.py` (novo) — usar o lifespan e SQLite em memória reais do repositório, semeando `Tenant`, `Session` e `HandoffRecord` com IDs novos por teste. Cobrir BOM/cabeçalho/headers; valores `pending`/`accepted`/`completed`; exclusão de `in_progress`, `cancelled`, `timeout`, tenant alheio e `completed` sem baixa; data de entrada versus saída; aberto anterior; espera aberta/concluída; `exported_at` único/pós-snapshot; normalização naïve; ordenação; resposta vazia; defaults/limite de datas preservados; excesso de 10.000; fuso UTC ausente; fuso IANA não-UTC perto da meia-noite; e `invalid_timezone`.
5. `tests/api/test_handoff_queue_export.py` — cobrir clínica própria, builder autorizado, super-admin e rejeição cross-tenant, reaproveitando padrões existentes de JWT/acesso; controlar o relógio do módulo somente nos casos que afirmam `exported_at`/idade.
6. `frontend/artifacts/neurostack/src/pages/handoff-queue.tsx` — adicionar `timezone` aos parâmetros de `handleExport` usando `Intl.DateTimeFormat().resolvedOptions().timeZone`, sem alterar o filtro local dos cards nem o GET da fila. Em resposta não-OK, ler JSON defensivamente; para `export_limit_10000_exceeded` e `invalid_timezone`, exibir o toast destrutivo localizado correspondente e não criar Blob/link. Preservar o fluxo de sucesso.
7. `frontend/artifacts/neurostack/src/locales/pt.ts` e `en.ts` — adicionar mensagens para reduzir o intervalo quando houver mais de 10.000 atendimentos e para informar fuso inválido.
8. `frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx` — afirmar que a URL do export inclui o fuso IANA, que cada novo erro 400 mostra o toast PT/EN correspondente e não inicia download, e que a exportação bem-sucedida continua criando/revogando o Blob.

**Acceptance Criteria:**
- [ ] Todos os Success Criteria deste wish estão cobertos por assertions do novo arquivo de teste ou por regressões existentes citadas no relatório de execução.
- [ ] O endpoint não consulta nem retorna handoffs de outro tenant e preserva os overrides autorizados de builder/super-admin.
- [ ] Nenhuma migration ou dependência é introduzida; a mudança frontend limita-se ao fuso no export e ao erro acionável.
- [ ] As cinco colunas preexistentes mantêm posição e formato; `status` e `exported_at` são somente anexadas ao final.
- [ ] O cálculo de idade aberta usa um instante único e auditável por resposta; o cálculo de concluídos continua baseado na baixa real.
- [ ] Acima de 10.000 registros o endpoint falha explicitamente, sem entregar arquivo parcial.
- [ ] Cards e CSV classificam `created_at` no mesmo dia do fuso local do navegador.
- [ ] O botão mostra orientação localizada nos erros de limite e fuso inválido e mantém o download atual no sucesso.

**Validation:**
```bash
poetry run pytest tests/api/test_handoff_queue_export.py tests/api/test_handoff_queue.py -v
poetry run black --check src/api/routes/handoff.py tests/api/test_handoff_queue_export.py
poetry run isort --check-only src/api/routes/handoff.py tests/api/test_handoff_queue_export.py
poetry run flake8 src/api/routes/handoff.py tests/api/test_handoff_queue_export.py
poetry run python scripts/check_mypy_regression.py --base fca60f37bf3c03132efbe44a848462a64ce7686d
BASE=$(git merge-base HEAD origin/main)
(cd frontend/artifacts/neurostack && pnpm test -- handoff-queue.test.tsx && pnpm typecheck)
git diff --check "$BASE" -- src/api/routes/handoff.py tests/api/test_handoff_queue_export.py frontend/artifacts/neurostack/src/pages/handoff-queue.tsx frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx frontend/artifacts/neurostack/src/locales/pt.ts frontend/artifacts/neurostack/src/locales/en.ts
```

Esse gate é proporcional ao diff: exerce endpoint, autenticação, banco, relógio, datas, fuso, limites e CSV, além do consumidor frontend, toast e tipos; os checks ficam restritos aos arquivos/superfícies alterados para não herdar dívida global não relacionada.

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Na tela Fila de Handoff, selecionar um período com um atendimento `pending`, outro `accepted` e outro já baixado; o CSV contém as três linhas com `status` canônico e o mesmo `exported_at`.
- [ ] Confirmar no Excel/LibreOffice que acentos, BOM, colunas e células vazias abrem corretamente.
- [ ] Confirmar que um aberto mostra `data_hora_saida` vazia e `tempo_espera_segundos` próximo à idade mostrada pelo contador da fila no instante indicado por `exported_at`; registrar que isso não é tempo até o primeiro aceite humano.
- [ ] Confirmar que um atendido mantém data de saída e espera histórica, sem continuar aumentando em exportações posteriores.
- [ ] Confirmar que alterar os filtros de segurança/intenção dos cards não altera o CSV; somente clínica e período definem o arquivo.
- [ ] Como usuário de uma clínica, tentar exportar outro `tenant_id` e receber 403; como builder autorizado, exportar a clínica selecionada com sucesso.
- [ ] Com mais de 10.000 linhas no período, confirmar erro explícito pedindo um intervalo menor e ausência de download parcial.
- [ ] Em navegador configurado para um fuso IANA não-UTC, criar registros imediatamente antes e depois da meia-noite local e confirmar que card e CSV os incluem/excluem no mesmo dia selecionado.
- [ ] Confirmar que fila, checkout e KPIs continuam operacionais após a mudança do export.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Consumidores que assumem exatamente cinco colunas podem rejeitar as duas novas | Medium | Expansão aditiva aceita para o export interno: anexar `status,exported_at`, preservar integralmente as cinco primeiras e documentar/testar o cabeçalho. Um consumidor externo comprovado dispara versão separada. |
| “Período” pode ser confundido com data de saída ou snapshot histórico | Medium | Fixar em Decisions e testes que a coorte é definida por `created_at` e o status é o atual no momento da exportação. |
| Datas perto da meia-noite ou em transições de horário de verão podem cair no dia errado | Medium | Enviar o fuso IANA resolvido pelo mesmo navegador que executa `ymd`, usar `ZoneInfo` e limites UTC semiabertos; incluir teste de borda não-UTC que compare card e CSV. |
| `tempo_espera_segundos` pode ser lido como tempo até aceite, embora `accepted` não tenha timestamp de transição | Medium | Definir formalmente a coluna legada, para abertos, como idade desde `created_at`; incluir `status`/`exported_at` e cobrir a semântica no teste e na docstring. |
| `in_progress` é considerado ativo pelo modelo, mas não aparece na fila visual nem neste CSV | Medium | Escopo segue deliberadamente o contrato atual de `GET /queue` (`pending`/`accepted`); alinhar o estado `in_progress` exige decisão separada sobre UI e timestamps de transição. |
| Registros `completed` sem `data_hora_saida`, inclusive conclusões fora do checkout, continuam ausentes | Low | Exclusão explícita deste wish; criar saneamento/unificação separado se a incidência for relevante. |
| `Intl.DateTimeFormat().resolvedOptions().timeZone` pode estar ausente ou produzir valor não aceito pelo backend | Low | O frontend só anexa um valor não vazio; chamadas sem parâmetro têm fallback UTC e valores enviados mas inválidos falham com `invalid_timezone` e toast localizado, sem gerar uma coorte silenciosamente incorreta. |
| Mais de 10.000 linhas impedem o download | Medium | Falhar explicitamente sem parcial e orientar redução do período; ocorrência recorrente é o gatilho mensurável para export assíncrono/paginado. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — 2026-09-27T21:04:00Z

- **Context:** PLAN REVIEW
- **Target:** `.genie/wishes/handoff-export-open-closed/WISH.md`
- **Target SHA-256:** `9555909710e0fde1e236b2ced2232a96d7f64f332d848a39906e0a24df6b47fa`
- **Verdict:** SHIP
- **Gaps:** None.
- **Evidence:** The current wish has an explicit direct-wish rationale, concrete IN/OUT scope, one cohesive independently shippable group, declared dependencies, testable criteria, proportional backend/frontend validation, and an executable Simplicity Case. Repository inspection confirmed browser-local `ymd`, separate queue/export paths, current date defaults, queue and checkout state semantics, indexed `created_at`, tenant isolation, timezone data, and available validation tooling. Both `export_limit_10000_exceeded` and `invalid_timezone` now have explicit backend contracts, localized PT/EN frontend handling, no-download assertions, and focused regression coverage. The SHA matched before and after review.

### Execution Review — 2026-09-27T21:32:53Z

- **Context:** EXECUTION REVIEW, Group 1
- **Task:** `t_mukb4d1qbb7c3f4b` (`in_progress`)
- **Target:** uncommitted working tree
- **HEAD:** `96edb2579ded08c3a1b34549bb73d1ee86a77325`
- **Merge base with origin/main:** `54538a0b8824024962fb4667a454f9471b6e0a12`
- **Governing WISH SHA-256:** `6eabb29262a26eae7b12233242e4aee14e6743ca4ced91889af8e2210a602615`
- **Verdict:** BLOCKED
- **Escalation diagnosis:** `missing-context` — exact-tree evidence was unavailable to the read-only reviewer for the differential mypy and Vitest gates; the orchestrator must supply it after the fix loop.
- **Validation:** Backend pytest PASS (8 tests); Black PASS; isort PASS; flake8 PASS; focused read-only mypy PASS; frontend TypeScript PASS; scoped `git diff --check` PASS. Differential mypy and Vitest were not run by the reviewer because they create temporary/cache files.
- **Gaps:** MEDIUM backend boundary/state coverage omissions; MEDIUM missing success-path click/revocation assertions; mandatory exact-tree validation evidence missing.
- **Scope/evidence:** Tenant resolution precedes the semi-open UTC `created_at` query; selection, limit-before-CSV, single post-query `exported_at`, naïve-as-UTC handling, frontend timezone/error early-return, and preservation of the pre-existing gauge test were verified. All six dispatched file hashes were unchanged during review.

### Execution Re-review — 2026-09-27T22:03:19Z

- **Context:** EXECUTION REVIEW, Group 1, fix loop 1/2
- **Task:** `t_mukb4d1qbb7c3f4b` (`in_progress`)
- **Target:** uncommitted working tree
- **HEAD:** `96edb2579ded08c3a1b34549bb73d1ee86a77325`
- **Merge base:** `54538a0b8824024962fb4667a454f9471b6e0a12`
- **Governing WISH SHA-256:** `2bf039643f05f8ba38ad470bb0d0af130b03d76760cfefef3ef897315c809ba8`
- **Verdict:** SHIP
- **Gaps:** None.
- **Acceptance evidence:** Tenant isolation, missing credentials, API-key ownership, clinic, builder and super-admin paths are covered. The implementation uses an inclusive local-date window converted to semi-open UTC bounds over `created_at`; preserves UTC fallback, partial/default dates, seven-column order, BOM, headers, filename and empty output; treats `pending`/`accepted` with residual checkout as open; excludes invalid states and incomplete completions; uses one post-query `exported_at`; normalizes naïve timestamps; preserves descending order; exports exactly 10,000 and rejects 10,001 before CSV construction.
- **Frontend evidence:** Timezone is added only to export; PT/EN limit and actionable timezone errors return before Blob/link creation; success assertions cover Bearer header, response Blob, object URL, anchor fields, append, click, removal and URL revocation. Generated OpenAPI documents the timezone parameter, cohort, states, seven columns, UTC timestamps, wait semantics, limit and actionable errors.
- **Validation:** Backend pytest PASS — 10 tests, 3 pre-existing warnings, 8.32s; Black PASS; isort PASS; flake8 PASS; differential mypy PASS (`base=364`, `current=0`); frontend PASS — 27 files/260 tests, 15 target tests, typecheck exit 0; scoped `git diff --check` PASS.
- **Scope:** No migration, dependency, cache, synchronization state or new abstraction. The pre-existing gauge test and `docs/examples/handoffs-export-atual-exemplo.csv` remain outside wish attribution.
- **Hash drift:** None; all six scoped files matched the hashes frozen for re-review.

### Quality Review — 2026-09-27T22:11:02Z

- **Context:** EXECUTION REVIEW, quality pass adversarial
- **Task:** `t_mukb4d1qbb7c3f4b` (`in_progress`)
- **Target:** uncommitted working tree
- **HEAD / merge-base:** `96edb2579ded08c3a1b34549bb73d1ee86a77325` / `54538a0b8824024962fb4667a454f9471b6e0a12`
- **Governing WISH SHA-256:** `3348b5728548b7f683ddb2d28dcd488369588188165494ae5e5c2b0b3d5f4cd2`
- **Verdict:** FIX-FIRST
- **Hash drift:** None; all six frozen hashes matched before and after review.
- **HIGH:** Valid ISO dates at the representable extremes can raise `OverflowError` while constructing/converting the semi-open UTC window and return `500`; return a stable `400`, document it and add boundary tests.
- **HIGH:** `numero_whatsapp` is emitted directly and can trigger spreadsheet formula injection for cells beginning with formula-control characters; neutralize only the exported cell and test malicious plus ordinary values.
- **MEDIUM:** The query materializes the complete `HandoffRecord`, including unused large Text/JSONB fields, for up to 10,001 rows; project only the five serialized model fields.
- **MEDIUM:** OpenAPI must list the preserved `max_window_90_days` detail and the new date-boundary error alongside the existing 400 details.
- **Positive evidence:** Auth/tenant ordering, sargable tenant-scoped predicate, 10,001 sentinel, DST windows, state/timestamp calculations, CSV compatibility, defaults, frontend flow and all previously recorded validation evidence were confirmed.

### Quality Re-review — 2026-09-27T22:27:33Z

- **Context:** EXECUTION REVIEW, single quality-fix loop
- **Task:** `t_mukb4d1qbb7c3f4b` (`in_progress`)
- **Target:** uncommitted working tree
- **HEAD / merge-base:** `96edb2579ded08c3a1b34549bb73d1ee86a77325` / `54538a0b8824024962fb4667a454f9471b6e0a12`
- **Governing WISH SHA-256:** `2fcfc52abad7fad035cb1442a3f6cd1438a61af032e4ee427f2d84d3db7047db`
- **Verdict:** SHIP
- **Gaps:** None.
- **Hash drift:** None; all six mandatory hashes matched before and after review.
- **Corrections verified:** Extreme dates return `400 invalid_date_range` before the main query/CSV; dangerous spreadsheet prefixes are neutralized only in the exported phone cell; the query projects only the five serialized fields; OpenAPI documents all four stable 400 details.
- **Validation evidence:** Fixer reported backend 13 passed; Black, isort, flake8 and mypy passed; frontend 27 files/260 tests and typecheck passed. Scoped `git diff --check` passed in the reviewer session. Tenant security, DST, UTC/naïve calculations, cap, CSV contract, partial defaults, queue/checkout and frontend flow remain preserved.

### Final Group Validation — 2026-09-27T22:37:46Z

- **Context:** Orchestrator validation, Group 1
- **Task:** `t_mukb4d1qbb7c3f4b` (`in_progress` at validation time)
- **Result:** PASS
- **Scope rationale:** The exact wish gate is proportional to the final diff: focused backend tests exercise HTTP auth, tenant isolation, SQLite persistence, states, clocks, timezones, limits and CSV; Python static gates cover the changed route/test; the repository frontend command exercises the changed consumer plus the full Vitest suite and TypeScript boundary. No migration, dependency, shared core or release configuration changed, so no broader backend suite/release gate is required.
- **Backend:** `poetry run pytest tests/api/test_handoff_queue_export.py tests/api/test_handoff_queue.py -v` — PASS, 13 tests, 3 pre-existing deprecation warnings, 5.60s.
- **Python static:** Black PASS (2 files unchanged); isort PASS; Flake8 PASS; differential mypy PASS (`base=364`, `current=0`).
- **Frontend:** `pnpm test -- handoff-queue.test.tsx` — PASS, 27 files / 260 tests, including 15 handoff tests; `pnpm typecheck` — PASS. The repository script runs the full Vitest suite despite the supplied file argument; pre-existing `act()`/dialog accessibility warnings did not fail the suite.
- **Whitespace:** Merge-base-scoped `git diff --check` over all six owned files — PASS.
- **Final code hashes:** `handoff.py` `216a79fb...`; backend export test `de8d0a65...`; queue page `25cafc83...`; queue test `be294bd5...`; PT locale `73477e26...`; EN locale `d95c3c96...`.

### PR Review — 2026-09-28T21:56:13Z

- **Context:** PR REVIEW — candidate for direct push to `main`
- **Verdict:** FIX-FIRST
- **Head / tree:** `4618e97e5da0c99fc6ded02d42979526627e04bb` / `6d1a2d31daeabf897d9689bb2f7161e14bb5c2e1`
- **Base / merge-base:** `f90c69f9515849a37f7b538ce16ce8cb4d51ebab`
- **Scope:** Exactly the seven wish files; `.gitignore`, the pre-existing gauge test, the old CSV example and all parallel workspace changes were excluded. No secrets or real credentials were found. Commit message is conventional and references `handoff-export-open-closed` in its body.
- **Validation:** `git diff --check`, backend 13 tests, Black/isort/Flake8, focused frontend 14 tests, TypeScript, aggregate frontend suite and differential mypy all passed. The rejected `pnpm exec` auto-install attempt was environmental; the no-download rerun passed.
- **HIGH:** CSV formula neutralization omitted LF (`0x0A`) and full-width formula initiators `＝`, `＋`, `－`, `＠`, allowing spreadsheet-injection bypasses for unrestricted `numero_whatsapp` input.
- **Required fix:** Add those prefixes to the export-only neutralization and regression test, recreate the candidate from `origin/main`, and repeat proportional gates and PR review.

### PR Re-review — 2026-09-28T22:07:43Z

- **Context:** PR REVIEW, fix loop 1/2
- **Verdict:** SHIP
- **Head:** `a7a3ab1ad0d006946ebfad5316687b05ee88c132`
- **Tree:** `a212a6cb27afe1e4ad1828cfaf0ac3c392edbe98`
- **Base / merge-base:** `f90c69f9515849a37f7b538ce16ce8cb4d51ebab`
- **Snapshot:** `/tmp/neurostack-api-handoff-review-v2.XETifS`, detached and clean.
- **Governing WISH SHA-256:** `3fdb4c879c35d6077729fef172aa28b6c94835ab69ae262884036a4a7032093f`
- **Commit:** one conventional commit — `feat(handoff): export open and completed queue entries`; body `Wish: handoff-export-open-closed`.
- **Scope:** exactly the seven expected files. `.gitignore`, the pre-existing gauge test, the old CSV example and all parallel workspace work are absent.
- **Security:** no secret or real credential found; added tokens/API keys are test fixtures. Export-only neutralization covers `=`, `+`, `-`, `@`, TAB, CR, LF and `＝＋－＠`; tests cover all of them, preserve a common phone number and show that persisted data is unchanged.
- **Validation:** backend 13 tests PASS with 3 pre-existing warnings; Black/isort/Flake8 PASS; differential mypy PASS (`base=364`, `current=0`); focused frontend 14 tests PASS; TypeScript PASS; `git diff --check` PASS. The earlier aggregate frontend suite remains applicable because the frontend blobs are identical: 27 files/260 tests PASS.
- **Environment:** the offline shim reconstruction was operational, without download or lockfile, package or source changes; the subsequent direct gates passed.
- **Functional evidence:** cohort by `created_at`/timezone, mixed states, seven columns, single clock, 10,000-row limit, authentication/tenant isolation, localized errors, extreme dates, projected query and CSV security are satisfied.
- **Gaps:** None.
- **Release risks:** residual risks remain declared in this WISH; no new risk was identified.
- **Drift:** none; HEAD/tree and clean worktree were reconfirmed after inspection.
- **Final hashes:** WISH `3fdb4c87...`; `handoff.py` `24419ea1...`; backend test `cb8eafd2...`; queue page `25cafc83...`; queue test `6f2c8e97...`; PT locale `73477e26...`; EN locale `d95c3c96...`.

---

## Files to Create/Modify

```
src/api/routes/handoff.py                  # consulta por entrada, estados mistos, timezone e serialização CSV
tests/api/test_handoff_queue_export.py     # novo contrato integrado do export
frontend/artifacts/neurostack/src/pages/handoff-queue.tsx       # fuso no export e toasts de limite/fuso
frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx  # regressão de fuso, erros e sucesso
frontend/artifacts/neurostack/src/locales/pt.ts                  # mensagens PT
frontend/artifacts/neurostack/src/locales/en.ts                  # mensagens EN
```
