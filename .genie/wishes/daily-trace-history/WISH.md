# Wish: Histórico diário navegável de traces

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `daily-trace-history` |
| **Date** | 2026-09-28 |
| **Author** | Codex |
| **Appetite** | medium |
| **Branch** | `wish/daily-trace-history` |
| **Repos touched** | `neurostack-api` (API e `frontend/artifacts/neurostack`) |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Transformar `/traces` em um histórico diário navegável: ao entrar, a pessoa vê automaticamente os traces do dia atual e pode escolher outro dia por um botão explícito, mantendo acesso ao detalhe e ao feedback de cada resposta. Cada página exibirá no máximo 50 traces, com paginação numérica, enquanto a exportação CSV continuará usando um período independente e poderá abranger vários dias.

**Problema testável:** hoje a primeira carga de `/traces` não consulta explicitamente o dia civil atual do navegador e a tela não permite aplicar outro dia e percorrer todos os resultados em páginas numéricas de até 50 itens, preservando ao mesmo tempo o detalhe/feedback e o CSV multi-dia; o wish está concluído quando os critérios C1–C16 provarem esses comportamentos.

## Scope

### IN

- Adicionar à listagem `GET /v1/traces` um filtro diário aditivo por `day=YYYY-MM-DD` + timezone IANA do navegador, convertido no backend para uma janela UTC semiaberta.
- Preservar os parâmetros legados `start_date`/`end_date` da listagem para consumidores existentes quando `day` não for usado e rejeitar combinações ambíguas entre os dois contratos.
- Manter a consulta tenant-scoped, os filtros de safety, handoff e canal, o total paginado e a ordenação decrescente, acrescentando desempate determinístico por ID.
- Separar na UI o dia escolhido para a lista do intervalo escolhido para exportação.
- Carregar automaticamente o dia local atual na entrada e oferecer seletor de data + botão `Exibir traces do dia X`, sem consultar uma data apenas digitada antes do clique.
- Exibir até 50 traces por página usando o `limit`/`offset` existentes.
- Substituir a navegação apenas anterior/próximo por paginação numérica compacta, com até cinco páginas consecutivas, primeira/última página, reticências, estado ativo e controles anterior/próximo acessíveis.
- Usar a rolagem vertical normal da página e reposicionar o início da lista ao trocar de página, sem criar uma área de scroll aninhada.
- Preservar o link de cada linha para o detalhe do trace e, por consequência, o fluxo de avaliação já renderizado no detalhe para perfis autorizados.
- Manter a exportação CSV por intervalo, seus filtros, tenant, nome do arquivo e download via Blob sem limitá-la ao dia exibido.
- Atualizar textos PT/EN e cobrir o contrato diário, paginação, resets e regressões da exportação em testes de API e frontend.

### OUT

- Restringir `GET /v1/traces/export` a um único dia, mudar suas colunas ou substituir a exportação CSV por exportação da página atual.
- Alterar `GET /v1/traces/stats`, dashboards, métricas de sete dias ou outras telas analíticas.
- Alterar o detalhe do trace, o `RatingBlock`, regras/notas de feedback, banco de ratings ou painel de auditoria RLHF.
- Criar migração, nova tabela, cache, cursor/keyset, virtualização, prefetch, polling ou processamento em background.
- Persistir dia/página em URL, storage ou histórico de navegação após sair da listagem; a entrega cobre consulta e feedback pontuais, e restaurar serialmente dia/página ao voltar do detalhe permanece OUT até requisito explícito.
- Reescrever ou centralizar os mocks de Investor; essa conta de demonstração mantém o comportamento atual e fica fora das garantias de data/paginação de traces reais.
- Corrigir, neste wish, a semântica legada dos filtros safety/handoff para eventos históricos que não tenham os flags em `event_metadata`.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | O dia exibido é o dia civil do navegador e viaja como `day` + timezone IANA | “O dia em que a pessoa está” é uma noção local; enviar a zona elimina a interpretação fixa UTC−3 hoje feita para datetimes sem fuso e segue o padrão já usado na exportação de handoffs. |
| 2 | `day` é um contrato aditivo somente da listagem | A tela ganha uma coorte diária exata sem quebrar clientes que ainda usam `start_date`/`end_date`; o export continua sendo a superfície de períodos. |
| 3 | O backend converte meia-noite local em `[início, próxima meia-noite)` UTC | A janela semiaberta inclui microssegundos do último segundo e lida corretamente com dias de 23/25 horas; `23:59:59` inclusivo não oferece essa garantia. |
| 4 | `day` exige timezone e não pode coexistir com `start_date`/`end_date` | Falhar explicitamente evita uma precedência silenciosa e uma lista cujo “dia” muda conforme o servidor. |
| 5 | A API mantém o default legado de 20; a tela pede explicitamente `limit=50` | O requisito é da tela e o endpoint já aceita até 100; não há razão para alterar o default de consumidores externos. |
| 6 | Ordenar por `created_at DESC, id DESC` | Offset precisa de desempate determinístico quando dois eventos têm o mesmo timestamp para não duplicar ou omitir linhas entre páginas. |
| 7 | `pendingDay` e `appliedDay` são estados distintos | O texto do botão precisa refletir a escolha, mas a consulta só deve mudar no clique solicitado; aplicar o dia também volta ao offset zero. |
| 8 | O período do CSV tem estado próprio | Trocar o dia visível não pode reduzir nem sobrescrever o relatório multi-dia; safety, handoff, canal e tenant continuam compartilhados como hoje. |
| 9 | Paginação compacta mostra uma janela de até cinco números, além de bordas/reticências | Reproduz `1 2 3 4 5 …` sem renderizar centenas de botões quando um dia tiver muitos traces. |
| 10 | A rolagem é a do documento, com retorno ao topo da lista após paginação | Satisfaz a navegação de até 50 cards sem introduzir scroll aninhado, altura fixa ou virtualização sem medida de necessidade. |
| 11 | O navegador é deliberadamente a autoridade do dia civil | É a única autoridade disponível que representa “o dia em que a pessoa está”; usar `operations.timezone` foi rejeitado porque mudaria o requisito para o dia operacional da clínica. |
| 12 | `day`/`timezone` são parâmetros próprios em vez de reutilizar `start_date`/`end_date` | Reutilizar o intervalo exigiria fabricar duas datas no cliente e não distinguiria no contrato um dia civil de um período arbitrário; o par aditivo mantém o caminho legado intacto. |

## Simplicity Case

- **Simplest complete design:** acrescentar um filtro diário exato ao endpoint já paginado, separar dois pares de estado na página existente e reutilizar `limit`, `offset`, `total`, filtros, cards, detalhe e exportação atuais.
- **Added machinery:** somente o par `day`/timezone para dar significado inequívoco a “um dia”, uma janela numérica local para a paginação pedida e uma âncora de scroll para que a nova página comece visível.
- **Deferred until measured:** cursor/keyset ou virtualização só entram se uma medição com 50 cards violar o SLO de interação aprovado ou se inserções durante a navegação produzirem duplicações observadas; persistência em URL/storage só entra quando houver requisito explícito de restaurar o recorte após sair da tela.
- **Complexity removed:** nenhuma nova tabela, endpoint, dependência, configuração, cache, fila, polling, prefetch, área de scroll própria ou alteração do fluxo de feedback/exportação.
- **Direct-wish rationale:** não há brainstorm porque a mudança é aditiva em uma rota e uma tela existentes, reutiliza paginação e período já disponíveis e não exige persistência, infraestrutura nem decisão arquitetural aberta.

## Dependencies

**depends-on:** none
**blocks:** none

Estas chaves registram somente dependências entre wishes; a ordem interna entre os grupos está declarada em cada grupo abaixo.

## Success Criteria

- [x] **C1** — Ao abrir `/traces` em um perfil com dados reais, a primeira requisição envia o dia local atual, o timezone IANA do navegador, `limit=50` e `offset=0`, além do tenant efetivo quando aplicável.
- [x] **C2** — A listagem diária inclui todos e somente os traces entre a meia-noite local escolhida e a próxima meia-noite, inclusive em uma zona com transição DST, sem perder eventos com microssegundos no fim do dia.
- [x] **C3** — Escolher uma data não dispara consulta; clicar em `Exibir traces do dia X` aplica essa data, volta à página 1 e atualiza título, total, vazio/erro e cards para o novo dia.
- [x] **C4** — Nenhuma página renderiza mais de 50 traces, e o total/número de páginas considera em conjunto dia, tenant, safety, handoff e canal.
- [x] **C5** — A paginação mostra até cinco números consecutivos, primeira/última e reticências quando necessárias; o número ativo tem `aria-current="page"`, e clicar na página N envia `offset=(N-1)*50`.
- [x] **C6** — Trocar dia aplicado, tenant ou qualquer filtro volta ao offset zero; trocar apenas o intervalo de exportação não refaz nem altera a listagem diária.
- [x] **C7** — Anterior/próximo e números respeitam os limites, têm nomes acessíveis PT/EN e levam a rolagem ao início da lista após a troca.
- [x] **C8** — Cada resultado continua abrindo o detalhe tenant-correto e o widget de feedback existente permanece alcançável para perfis autorizados; Investor continua sem feedback real.
- [x] **C9** — A exportação continua aceitando início/fim multi-dia e aplicando tenant/safety/handoff/canal, independentemente do dia exibido, com o mesmo download `neurostack_traces_report.csv`.
- [x] **C10** — `GET /v1/traces` rejeita timezone inválido, `day` sem timezone e mistura de `day` com `start_date`/`end_date`, sem enfraquecer autenticação ou isolamento de tenant.
- [x] **C11** — Empates de `created_at` têm ordem estável por ID em todas as páginas; a resposta mantém `{items, total, limit, offset}`.
- [x] **C12** — Os textos novos e resumos de dia/página existem em português e inglês, sem duplicar headings ou introduzir strings funcionais fixas em um único idioma.
- [x] **C13** — Em duas requisições da lista fora de ordem, B é iniciada depois de A e resolve primeiro; quando A finalmente resolve ou falha, ela não altera `appliedDay`, total, cards, loading nem erro produzidos por B.
- [x] **C14** — O contrato público congela status e `detail`: `day` sem timezone → `400 {"detail":"timezone_required_with_day"}`; timezone sem `day` → `400 {"detail":"day_required_with_timezone"}`; timezone IANA inválido → `400 {"detail":"invalid_timezone"}`; `day` com `start_date` e/ou `end_date` → `400 {"detail":"day_conflicts_with_date_range"}`. `day` sintaticamente inválido retorna 422 com o corpo de validação FastAPI e `detail[*].loc == ["query", "day"]`; testes de rota afirmam status e corpo.
- [x] **C15** — O OpenAPI descreve `day` como `string/date`, `timezone` como string IANA, a obrigatoriedade do par e sua exclusividade com `start_date`/`end_date`, além das respostas 400 e 422; um teste sobre `app.openapi()` fixa esse contrato.
- [x] **C16** — Sem token, 401, cada `detail` 400 conhecido e erro desconhecido da listagem produzem mensagens acionáveis nos dois locales: autenticar-se, renovar sessão, corrigir dia/timezone/intervalo ou tentar novamente/contatar suporte, respectivamente.

Handoff exato de erros da listagem para a UI (os quatro 400 são selecionados por `detail`; qualquer outro `detail` usa o fallback):

| Caso | Português | English |
|------|-----------|---------|
| Sem token | `Faça login para carregar os traces.` | `Sign in to load traces.` |
| 401 | `Sua sessão expirou. Faça login novamente.` | `Your session expired. Sign in again.` |
| `timezone_required_with_day` | `O fuso horário é obrigatório para consultar esse dia. Atualize a página e tente novamente.` | `A timezone is required for this day. Refresh the page and try again.` |
| `day_required_with_timezone` | `Escolha um dia para usar o fuso horário informado.` | `Choose a day to use the supplied timezone.` |
| `invalid_timezone` | `O fuso horário do navegador não foi reconhecido. Atualize as configurações e tente novamente.` | `The browser timezone was not recognized. Update your settings and try again.` |
| `day_conflicts_with_date_range` | `Use um dia para a lista ou um intervalo para o CSV, não ambos na mesma consulta.` | `Use a day for the list or a range for CSV, not both in the same request.` |
| Desconhecido | `Não foi possível carregar os traces. Tente novamente; se persistir, contate o suporte.` | `Could not load traces. Try again; if the problem persists, contact support.` |

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — semântica stateful de janela civil/UTC e paginação | `engineer-standard` / high | Criar o contrato diário aditivo no backend e seus testes de borda/regressão. |

### Wave 2 (after Group 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 2 — estado coordenado de dia, filtros, tenant e paginação | `engineer-standard` / high | Separar lista/exportação na UI, renderizar 50 itens e paginação numérica, com testes PT/EN. |

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** →
`engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high;
**4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an
independent `final-gate` at the highest justified effort. Codex maps these to
the `genie_*` profiles; other runtimes use their matching native roles. Keep
model and effort in runtime session/agent configuration, never skill frontmatter.

### Aggregate integration gate

Depois dos dois grupos, executar o gate completo baseline-aware da API e os gates completos do frontend porque a mudança atravessa uma rota de runtime e a tela entregue; os checks focados abaixo servem para atribuição rápida de falhas. O `poetry run pytest tests/ -q` bruto já é vermelho no baseline, enquanto `full_suite_gate` rejeita regressões novas e aceita somente o inventário de falhas conhecido e a conclusão integral da suíte.

```bash
source scripts/wish-gates.sh
full_suite_gate
CI=true pnpm --dir frontend/artifacts/neurostack run test
CI=true pnpm --dir frontend/artifacts/neurostack run typecheck
CI=true PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
git diff --check
```

## Execution Groups

### Group 1: Contrato diário da listagem de traces

**Goal:** Permitir que a listagem consulte um dia civil exato e pagine esse recorte de forma determinística, sem alterar a exportação por período.

**Deliverables:**
1. Adicionar `day: date | None` e `timezone: str | None` a `GET /v1/traces`, mantendo o contrato legado quando ambos estiverem ausentes.
2. Validar que `day` e timezone sejam fornecidos juntos, que `day` não seja combinado com `start_date`/`end_date` e que o nome IANA seja válido.
3. Converter o dia informado em início UTC inclusivo e próxima meia-noite UTC exclusiva com `ZoneInfo`, aplicando a janela antes do count e do `limit`/`offset`.
4. Manter `/export` no caminho legado de período, sem compartilhar a restrição diária, e preservar auth/tenant/filtros.
5. Acrescentar `Event.id.desc()` como desempate da ordenação.
6. Criar `tests/api/test_traces_daily_history.py` com dados nos limites, microssegundos, DST, mais de 50 linhas, empate, outro tenant, combinações inválidas e exportação multi-dia; ampliar `tests/services/test_trace_service.py` somente onde a unidade isolar melhor a regra.
7. Congelar os quatro erros 400 no formato `{"detail":"<code>"}` definido em C14 e manter `day` inválido como 422 de validação, para que a UI possa localizar códigos sem analisar texto livre.
8. Completar descriptions e responses da rota e validar o schema gerado por `app.openapi()`, incluindo formatos, regra de par e exclusividade.

**Acceptance Criteria:**
- [x] Requisição com `day=2026-09-28&timezone=America/Sao_Paulo` consulta `[2026-09-28T03:00:00Z, 2026-09-29T03:00:00Z)` e não inclui as duas bordas externas.
- [x] Um caso de DST prova que a janela pode ter 23 ou 25 horas sem deixar de representar exatamente um dia civil.
- [x] `limit=50&offset=50` devolve no máximo 50 itens, o `total` completo do recorte e ordem estável em timestamps empatados.
- [x] Filtros e tenant são aplicados antes da contagem/paginação; trace de outro tenant nunca aparece.
- [x] A matriz de erros afirma status e corpo: ausência do par, timezone inválido e conflito com intervalo retornam exatamente os quatro 400 de C14; `day=not-a-date` retorna 422 com `detail` de validação localizado em `query.day`.
- [x] Uma exportação com período de pelo menos dois dias continua retornando linhas de ambos os dias e não recebe a validação diária.
- [x] A matriz legada cobre: nenhum `start_date`/`end_date` (sem filtro temporal), somente `start_date` (`created_at >= início`), somente `end_date` (`created_at <= fim`, preservando a expansão atual de meia-noite até `23:59:59`) e ambos (interseção inclusiva); em todos os casos preserva semântica, defaults e resposta `{items, total, limit, offset}`.
- [x] `app.openapi()` expõe `day` como `type: string, format: date`, `timezone` como string IANA, descriptions da regra de par/exclusividade e responses documentadas 400/422.

**Validation:**
```bash
poetry run pytest tests/api/test_traces_daily_history.py tests/api/test_builder_clinic_access.py tests/services/test_trace_service.py -q
poetry run black --check src/api/routes/traces.py src/services/trace_service.py tests/api/test_traces_daily_history.py tests/services/test_trace_service.py
poetry run isort --check-only src/api/routes/traces.py src/services/trace_service.py tests/api/test_traces_daily_history.py tests/services/test_trace_service.py
poetry run flake8 src/api/routes/traces.py src/services/trace_service.py tests/api/test_traces_daily_history.py tests/services/test_trace_service.py
poetry run python scripts/check_mypy_regression.py --base "$(git merge-base HEAD origin/main)"
```

O recorte combina testes de rota/serviço, auth/tenant e checks estáticos dos arquivos tocados; o mypy diferencial impede dívida nova sem exigir a limpeza global já rastreada no repositório.

**depends-on:** none
**blocks:** Group 2

---

### Group 2: Histórico diário e paginação numérica na UI

**Goal:** Mostrar automaticamente um único dia por vez, permitir aplicar outro dia e navegar em blocos de 50 sem acoplar a lista ao período do CSV.

**Deliverables:**
1. Em `traces.tsx`, substituir o intervalo da listagem por `pendingDay`/`appliedDay`, inicializados uma vez com o dia local atual, e manter estados separados para início/fim da exportação.
2. Enviar `day`, timezone IANA, `limit=50`, offset, filtros e tenant no fetch da lista; impedir resposta obsoleta de uma requisição anterior de sobrescrever o recorte atual.
3. Adicionar input `date` limitado a hoje e botão dinâmico `Exibir traces do dia X` / `Show traces for X`; o clique aplica e reseta a página sem alterar o CSV.
4. Corrigir os resumos para distinguirem “traces do dia” do período exportável e manter estados de loading, erro, vazio e filtros coerentes.
5. Renderizar paginação compacta com janela de cinco números, bordas, reticências, anterior/próximo, `aria-current`, estados disabled e offset derivado; resetar ao mudar dia/filtro/tenant.
6. Manter o scroll do documento e usar uma ref no início da lista para reposicionamento após navegação de página.
7. Preservar cards/links/detalhe e o fluxo existente de exportação; não alterar a ramificação de mocks do Investor.
8. Adicionar chaves PT/EN e ampliar `traces.test.tsx` com relógio/timezone controlados, botão explícito, ausência de fetch no rascunho, 50 por página, números/reticências, offsets, resets, scroll, tenant, filtros, detalhe, independência do CSV, erros localizados e respostas fora de ordem.
9. Ler `detail` nos erros HTTP e mapear os quatro códigos conhecidos de C14 para instruções PT/EN específicas; tratar ausência de token, 401 e qualquer código/erro desconhecido com mensagens acionáveis próprias, sem exibir códigos crus como mensagem principal.

**Acceptance Criteria:**
- [x] O primeiro fetch usa hoje + timezone do navegador, `limit=50` e offset zero; nenhum intervalo de 30 dias é enviado à lista.
- [x] Alterar o input sozinho não chama fetch; o botão mostra a data formatada no locale e seu clique faz exatamente uma consulta do novo recorte na primeira página.
- [x] Com mais de 250 resultados, aparecem páginas `1 2 3 4 5 …`, a página ativa é anunciada e clicar em 3 envia `offset=100`.
- [x] Dia, safety, handoff, canal e clínica efetiva resetam a página; datas da exportação não alteram request, cards ou total da lista.
- [x] A troca de página rola para o início da lista e preserva os filtros ativos.
- [x] Clicar em um card continua navegando ao detalhe correto, sem modificar o widget de avaliação.
- [x] Exportar depois de mudar o dia visível ainda usa o início/fim multi-dia escolhidos no relatório e conserva filtros/tenant/filename.
- [x] Testes cobrem PT e EN e incorporam, sem reverter, as mudanças concorrentes já presentes em `traces.tsx`, `traces.test.tsx`, `pt.ts` e `en.ts`.
- [x] Um teste determinístico usa duas promises controladas: dispara A, dispara B, resolve B primeiro e então resolve/rejeita A; depois de cada etapa, `appliedDay`, total, cards, loading e erro permanecem exatamente no estado de B.
- [x] Em PT e EN, testes separados cobrem: sem token → instrução para autenticar-se; 401 → renovar a sessão; cada 400 conhecido → corrigir o campo/relação correspondente; erro/código desconhecido → tentar novamente e contatar suporte se persistir.

**Validation:**
```bash
CI=true pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/traces.test.tsx
CI=true pnpm --dir frontend/artifacts/neurostack run typecheck
CI=true PORT=3000 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
```

O teste focado cobre a máquina de estados da tela; typecheck e build validam as fronteiras do pacote frontend atingidas pelos novos props, locales e imports.

**depends-on:** Group 1
**blocks:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Entrar em Traces mostra automaticamente somente o dia local atual e até 50 cards, com a data visível no resumo.
- [ ] Selecionar ontem não altera a lista até clicar em `Exibir traces do dia X`; depois do clique, somente traces de ontem aparecem.
- [ ] Um dia com mais de 250 traces permite clicar em `1 2 3 4 5 …`, mantém filtros e leva cada nova página ao topo da lista.
- [ ] Abrir um trace histórico chega ao detalhe tenant-correto e permite enviar feedback com o widget existente para um perfil autorizado.
- [ ] Selecionar um intervalo de vários dias e exportar baixa `neurostack_traces_report.csv` com o período completo, mesmo quando a tela mostra outro dia.
- [ ] Trocar clínica como Builder/Super Admin volta à primeira página e nunca mistura traces da clínica anterior.
- [ ] PT e EN exibem botão, resumo, paginação, vazio e erros no idioma ativo; mobile não cria overflow horizontal.
- [ ] Uma zona com DST inclui corretamente os traces imediatamente antes/depois das duas meias-noites locais.
- [ ] Simular sessão ausente/expirada, os quatro erros 400 e erro desconhecido confirma orientação acionável no idioma ativo, sem mostrar `detail` cru ao usuário.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Timezone do navegador divergir do dia operacional da clínica | Medium | O requisito fala no dia em que a pessoa está e a escolha fica explícita; migrar a autoridade para `operations.timezone` somente com decisão de produto futura. |
| Resposta lenta de um dia anterior sobrescrever a data mais nova | High | Cancelar via `AbortController` ou ignorar por request id e testar respostas fora de ordem. |
| Offset mudar enquanto novos traces entram no dia atual | Medium | Ordenação total por timestamp + ID reduz não determinismo; cursor/polling ficam deferidos até reprodução de duplicação/omissão durante uso real. |
| 50 cards degradarem renderização em dispositivos lentos | Medium | Medir no QA; virtualizar apenas se o SLO aprovado for violado. |
| Clientes externos dependerem da listagem multi-dia atual | High | `day` é aditivo; sem ele, `start_date`/`end_date`, defaults e forma da resposta permanecem compatíveis. |
| Arquivos de traces/locales já têm alterações não commitadas | High | Executor parte do conteúdo corrente, incorpora os hunks existentes e não restaura versões anteriores desses arquivos. |
| O detalhe/feedback evoluir em wishes RLHF concorrentes | Medium | Não tocar nesses componentes; testar somente que o link continua alcançando o contrato presente no momento da execução. |

---

## Review Results

### Plan Review — 2026-09-28T19:26:59-03:00 — FIX-FIRST

- **Context:** initial plan review on the uncommitted working tree.
- **Reviewed SHA-256:** `02222982ee332c87bc5e8c9e9d681142281ac192c1b4ab05d49b4cec1ccf033f`.
- **Validation:** full document/code inspection, structural checks and command availability; runtime suites/builds were not run because this was a pre-implementation plan review. The repository has no root `package.json` or `wishes:lint`, so its established independent structural validation was used.
- **Blocking gap:** the aggregate `poetry run pytest tests/ -q` could not be a green gate because the repository records two known baseline failures; the plan needed the baseline-aware `full_suite_gate`.
- **Other gaps:** add an out-of-order-response criterion, complete per-group `depends-on`/`blocks`, justify the direct wish, freeze public error payloads/OpenAPI, expand legacy compatibility coverage and make list errors actionable in PT/EN.
- **Verdict:** **FIX-FIRST**.

### Fix Loop 1/2 — 2026-09-28 — completed

- Replaced the raw full-suite command with `source scripts/wish-gates.sh && full_suite_gate`, while retaining full frontend test/typecheck/build and diff gates.
- Added C13–C16, exact 400/422 payloads, OpenAPI reference coverage, the complete legacy date-filter matrix, deterministic stale-response coverage and localized actionable errors.
- Completed internal dependency tags and made the direct-wish, browser-timezone and point-feedback assumptions explicit.
- Post-fix SHA-256: `1d7946e3e5daf8ddef6e8195d4fd23e7f71e82ffb733ed9b5709c82f9255115e`.

### Plan Re-review — 2026-09-28T22:34:03Z — SHIP

- **Context:** plan re-review after fix loop 1/2; no snapshot because the wish is uncommitted in a dirty working tree. Verdict is bound to the post-fix SHA above.
- **Evidence:** digest verified twice; WISH, AGENTS instructions, trace route/service/schema, list/detail UI, tests, locales, manifests and `scripts/wish-gates.sh` inspected read-only. `poetry`, `pnpm`, validation scripts and every cited target exist. Runtime suites/builds remain execution-time gates.
- **Checklist:** problem and C1–C16 are testable; IN/OUT are concrete; both groups are bite-sized and sequential; dependency tags are complete; focused validation and the aggregate baseline-aware gate are proportional; no deferred cache/cursor/persistence/polling/virtualization is introduced.
- **Advisory lenses:** Questioner **SHIP** (no remaining ambiguity; assumptions are explicit). DX/API contract **SHIP** (error handoff, OpenAPI and compatibility gaps closed).
- **Gaps:** no CRITICAL, HIGH, MEDIUM or LOW findings.
- **Verdict:** **SHIP**.

### Group 1 Execution Review — 2026-09-28T19:58:44-03:00 — FIX-FIRST

- **Scope:** `src/api/routes/traces.py`, `src/services/trace_service.py`, `tests/api/test_traces_daily_history.py` and `tests/services/test_trace_service.py` on the uncommitted working tree.
- **Positive evidence:** the first focused run passed 35 tests plus Black, isort, flake8, direct mypy and `git diff --check`; the civil-day window, deterministic pagination, tenant scope, legacy list matrix, multi-day export and OpenAPI contract were present.
- **Blocking findings:** syntactically valid extreme dates could raise `OverflowError`/500, and host pseudo-timezones such as `localtime` and `posixrules` were accepted despite the public IANA contract.
- **Additional findings:** remove ordering from the count subquery, strengthen the filter/error/OpenAPI matrix, and normalize the executable bit of the new test where the filesystem supports it.
- **Verdict:** acceptance and quality reviewers both returned **FIX-FIRST**.

### Group 1 Fix Loop 1/2 — 2026-09-28 — completed

- Restricted `day` to the safe inclusive range `0001-01-02..9999-12-30`, keeping invalid/extreme values on FastAPI 422 at `query.day`.
- Validated timezones against the available IANA catalog and rejected host pseudo-keys with the exact `invalid_timezone` contract.
- Built count from the filtered, unordered query and applied `created_at DESC, id DESC` only to the paginated data query.
- Expanded coverage for combined tenant/channel/safety/handoff filters, all date-range conflicts, extreme dates, pseudo-timezones and the full OpenAPI response/schema contract.
- Focused validation after the fix: **44 passed**, Black/isort/flake8 passed, direct mypy reported zero issues in the four files, and `git diff --check` passed.

### Group 1 Execution Re-review — 2026-09-28T20:27:41-03:00 — SHIP

- **Acceptance verdict:** **SHIP**; all eight Group 1 criteria passed, including DST 23/25 hours, half-open boundaries, stable page 2 ordering, filters before count/pagination, exact 400/422 bodies, legacy compatibility, export isolation and OpenAPI.
- **Quality verdict:** **SHIP**; no CRITICAL/HIGH findings, no auth/tenant regression, and compiled SQL confirmed an unordered count plus deterministic paginated ordering.
- **Orchestrator validation:** exact focused pytest command **44 passed, 3 warnings in 9.55s**; Black reported four files unchanged; isort, flake8, direct mypy and scoped `git diff --check` exited 0.
- **Known external MEDIUM:** the repository's differential mypy gate aborts before comparing the diff because its frozen baseline requires 364 errors while the merge-base has 0; direct mypy on the four files and the full current source is clean, so no type regression is attributable to Group 1.
- **Known environmental LOW:** DrvFS/9p exposes the untracked test as mode `100755` and ignores `chmod`; normalize to `100644` during authorized staging on a filesystem/index that preserves mode metadata.
- **Non-blocking quality note:** `available_timezones()` adds a one-time process-import scan; measured cold cost was below one third of a second and can be optimized separately if startup profiling warrants it.

### Group 2 Execution Review — 2026-09-28T21:19:10-03:00 — FIX-FIRST

- **Scope:** `traces.tsx`, `traces.test.tsx` and PT/EN locales on the uncommitted working tree; all pre-existing heading, config and handoff hunks were preserved.
- **Positive evidence:** the daily request, explicit apply button, 50-item paging, localized errors, stale-response guard, independent CSV and tenant-aware detail links were present; the first run passed 28 focused tests, typecheck, build and diff-check.
- **Blocking finding:** after a day/filter/tenant/page change, a previously resolved result could render for one commit under the new live tenant before the effect began loading, allowing an old card to receive a new tenant link.
- **Additional findings:** clamp an out-of-range page, strengthen exact request-count/pagination/Investor/CSV tests and announce asynchronous empty/error states.
- **Verdict:** acceptance returned **SHIP**, but the independent quality reviewer returned **FIX-FIRST**; the group remained in progress.

### Group 2 Fix Loop 1/2 — 2026-09-28 — completed

- Replaced independent data fields with an atomic `ListResult` keyed by request scope and carrying the tenant that produced its cards; a mismatched result now renders loading instead of stale cards, totals, errors or pagination.
- Added corrective refetch to the last valid page before publishing a reduced total.
- Added exact request-count tests, beginning/middle/end pagination, tenant-pending behavior, Investor without network, CSV independence including handoff, and accessible `alert`/`status` states.
- Validation after the fix: **33/33** focused tests, typecheck, production build and diff-check passed.

### Group 2 Re-review — 2026-09-28T21:32:41-03:00 — FIX-FIRST

- Acceptance returned **SHIP** for all observable criteria.
- Quality found one remaining **HIGH** collision: a normal self-scoped user and a Builder/Super Admin whose clinic was not yet resolved both produced an empty tenant key, so the self result could remain visible indefinitely during `tenant-pending`.
- Because this was a new evidenced state-isolation case, the group entered its final allowed fix loop.

### Group 2 Fix Loop 2/2 — 2026-09-28 — completed

- Introduced a structural access scope distinguishing `self`, `tenant-pending`, `tenant:<id>` and `investor`; the list scope now includes day, browser timezone, filters and access, while the request key adds page and offset.
- `applyPendingDay` uses the same scope constructor, avoiding an intermediate request.
- A parametrized Builder/Super Admin regression test reproduced two failures before the patch and now proves immediate stale-data removal, zero network while pending and exactly one tenant-scoped page-1 request after resolution.
- Validation after the final fix: **35/35** focused tests, typecheck, production build and diff-check passed.

### Group 2 Final Execution Re-review — 2026-09-28T21:55:26-03:00 — SHIP

- **Acceptance verdict:** **SHIP**, with all Group 2 criteria and both prior state-isolation gaps closed.
- **Quality verdict:** **SHIP**, with zero CRITICAL/HIGH/MEDIUM/LOW findings after adversarial checks of self/pending/tenant/Investor transitions, stale requests and page clamp behavior.
- **Reviewed hashes:** `traces.tsx` `c70fff67775577810c136c2a211c8c6088e3549fb9ace48a7aaece43a3a8319c`; `traces.test.tsx` `e5421d0c052b8147c254b611cae29d0afcf562409c39497802c7e36572d1de04`; PT `e7a1d022f0caa8abf5e12a9bf62ee3cce89a43f7a41c5c37a8536e1e3201a0b9`; EN `995503b309c661211b75db8cda5d3bbcddb7da461081ba0592ade22420f492e6`.
- **Orchestrator validation:** focused Vitest **35/35 passed** in 103.59s; typecheck exited 0; Vite production build transformed 2,804 modules and exited 0; targeted `git diff --check` exited 0.
- **Environment note:** pnpm 11 considers the shared `node_modules` structure out of sync and otherwise attempts an unauthorized auto-install whose preinstall rejects the user-agent. Gates therefore ran with `pnpm_config_verify_deps_before_run=warn`, executing the same installed Vitest/typecheck/build scripts without changing dependencies. No manifest or lockfile changed.

### Aggregate Integration Gate — 2026-09-28 — external baseline drift

- `source scripts/wish-gates.sh && full_suite_gate` completed all **3,207** API tests: **3,183 passed, 20 skipped, 4 failed** in 386.74s. All daily-trace tests passed.
- The baseline-aware validator expected the two existing `tests/core/test_config_service.py` failures, but rejected two additional `tests/package_artifact/test_archives.py` failures. Those archive failures are caused by pre-existing/unrelated untracked files under `packages/neurostack-core/neurostack_core/presets/` plus `protocol.py`; they are outside this wish's files and were preserved rather than deleted or staged.
- Full frontend gate passed: **28 test files, 301 tests**. The traces suite contributed **35/35** passing cases.
- Frontend typecheck exited 0. The production build transformed **2,804 modules** and exited 0, with only existing sourcemap and chunk-size warnings.
- Repository-wide `git diff --check` exited 0.
- **Conclusion:** both execution groups and every C1–C16 criterion are complete and independently reviewed. The aggregate API command remains red only because the shared working tree contains unrelated package-artifact payload changes not authorized for this wish. Re-run `full_suite_gate` after that owner resolves or isolates those files; do not mark the wish `SHIPPED` before final review/merge/QA.
- **Handoff:** All work groups complete. Run review.

### Task-row note

- `genie task create` was attempted for `group-1` and `group-2`, but the CLI refused the local `.genie/genie.db` as inaccessible/malformed (`unable to open database file`). No task rows were created; this approved WISH remains the executable source of truth.

---

## Files to Create/Modify

```
src/api/routes/traces.py
src/services/trace_service.py
tests/api/test_traces_daily_history.py                         # new
tests/services/test_trace_service.py
frontend/artifacts/neurostack/src/pages/traces.tsx
frontend/artifacts/neurostack/src/pages/traces.test.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
```
