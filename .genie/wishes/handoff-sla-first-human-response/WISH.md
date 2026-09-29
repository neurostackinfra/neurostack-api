# Wish: SLA de atendimento confirmado no handoff

| Field | Value |
|-------|-------|
| **Status** | COMPLETE |
| **Slug** | `handoff-sla-first-human-response` |
| **Date** | 2026-09-27 |
| **Author** | Codex |
| **Appetite** | large |
| **Branch** | `wish/handoff-sla-first-human-response` |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/handoff-sla-first-human-response/DESIGN.md) |

## Summary

Tornar o radar da fila de handoff uma medição auditável do tempo até o atendimento explicitamente confirmado, usando a meta vigente quando cada ocorrência entrou. A entrega separa abrir o WhatsApp de confirmar o atendimento, unifica lifecycle e estatísticas no PostgreSQL, usa o fuso da clínica em fila/radar/CSV e cobre a regra real com concorrência e integração PostgreSQL.

## Scope

### IN

- Persistir por handoff o snapshot `sla_target_seconds`, o `updated_at` público e uma versão monotônica interna para CAS.
- Validar a meta configurada entre 1 e 120 minutos e usar fallback seguro de 600 segundos somente para novas ocorrências com configuração legada ausente ou inválida.
- Tornar `HandoffRecord` a fonte canônica e `HandoffService` stateless sobre unit of work, eliminando `_handoffs` como storage funcional.
- Centralizar todas as transições em lifecycle tenant-scoped, com confirmação write-once e concorrência otimista baseada no estado/versão observados.
- Preservar mapper, projeção limitada de mensagens, Event Store, webhooks/delivery status e métricas públicas.
- Calcular radar e contagens disjuntas pela coorte de `created_at`, meta snapshot e um único `as_of` por request.
- Resolver no backend o período civil no fuso IANA da clínica e aplicar o mesmo intervalo UTC semiaberto a `items`, KPI e CSV.
- Separar na UI “Abrir WhatsApp” de “Confirmar atendimento”, removendo thresholds fixos e filtro temporal do navegador.
- Retornar KPIs nulos sem amostra, contagens explicativas e período resolvido no contrato/OpenAPI.
- Conciliar o export já em andamento: preservar suas melhorias compatíveis, usar o fuso da clínica e incluir `in_progress` confirmado.
- Adicionar testes puros, de serviço/rota/frontend, migration, concorrência e query PostgreSQL de produção, com gate que não aceita skip.

### OUT

- Inferir primeira resposta humana de `fromMe`, webhooks de provedor ou abertura do WhatsApp.
- Adicionar composer de mensagens, captura automática de outbound ou novo campo `first_human_response_at`.
- Fazer backfill de meta histórica ou de `updated_at` legado.
- Persistir duração, resultado `within_sla` ou cópia de `recent_messages` em JSONB.
- Criar cache, materialized view, job, outbox, retry distribuído, alerta ou WebSocket.
- Configurar objetivo agregado percentual; a configuração continua representando somente minutos por ocorrência.
- Incluir cancelados/timeouts como linhas do CSV ou criar motivos estruturados de cancelamento.
- Alterar autenticação, teto de 10 mil linhas, BOM, cabeçalho, ordenação ou timestamps UTC compatíveis do export em andamento.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Medir atendimento confirmado manualmente | É o único evento uniforme e verificável entre os provedores atuais. |
| 2 | Abrir WhatsApp não altera o handoff | Abrir uma aba não prova que o atendimento começou. |
| 3 | Reutilizar `data_hora_saida` para a primeira confirmação | O campo já representa a saída da espera; `resolved_at` permanece o encerramento. |
| 4 | Capturar `sla_target_seconds` na criação | Mudanças futuras de configuração não reclassificam o histórico. |
| 5 | Não fazer backfill de SLA | A meta histórica não pode ser reconstruída com segurança. |
| 6 | Persistir `updated_at` e `lifecycle_version` | O primeiro preserva o contrato público; o segundo garante CAS monotônico sem usar relógio como token. |
| 7 | Fixar `status + lifecycle_version` observados em cada CAS | Uma operação que aguardou lock não pode reaplicar a matriz sobre estado produzido pelo concorrente. |
| 8 | PostgreSQL é a única fonte de estado | Fila, endpoints e novas instâncias precisam enxergar o mesmo lifecycle. |
| 9 | O denominador contém confirmados e não respondidos vencidos | A métrica não pode esconder oportunidades que estouraram a meta. |
| 10 | A clínica governa período/fuso | O navegador do operador não pode mudar a coorte gerencial. |
| 11 | `null` significa ausência de amostra | Zero continua reservado para um resultado real de 0%. |
| 12 | O wish de export é dependência e baseline | As mudanças compatíveis são preservadas; somente fuso e elegibilidade de `in_progress` são supersedidos. |

## Simplicity Case

- **Simplest complete design:** três colunas simples, confirmação manual write-once, um serviço stateless sobre repositório/CAS, estatística derivada na leitura e um único período resolvido no backend.
- **Added machinery:** o snapshot impede reclassificação; `updated_at` preserva API após restart; `lifecycle_version` impede dois vencedores; o mapper substitui dados hoje mantidos apenas em memória; o gate PostgreSQL é exigido porque SQLite já divergiu na aritmética temporal.
- **Deferred until measured:** evidência automática, composer interno e hardening cross-provider só entram quando existir evento outbound humano autenticado equivalente; cache/materialização só entram se a query exceder orçamento medido; export assíncrono só entra se o teto atual for insuficiente de forma recorrente.
- **Complexity removed:** sem modo dual de KPI, cache, job, outbox, retry state machine, backfill especulativo, duração/booleano derivados ou objetivo percentual adicional.

## Dependencies

**depends-on:** handoff-export-open-closed
**blocks:** none

## Success Criteria

- [x] Migration upgrade/downgrade adiciona/remove `sla_target_seconds`, `updated_at TIMESTAMPTZ NULL` e `lifecycle_version INTEGER NOT NULL DEFAULT 0`, aceita SLA 60/7200/NULL, rejeita 59/7201 e versão negativa, e não inventa SLA/`updated_at` legado.
- [x] Novos handoffs capturam a meta válida na mesma transação; 0, 121, boolean, float, string e null explícito são rejeitados, enquanto configuração legada ausente/inválida usa 600 segundos com log sem dados do paciente.
- [x] `HandoffService` não mantém `AsyncSession` nem `_handoffs` funcional; instâncias A/B/C criam, transitam e releem o mesmo estado PostgreSQL.
- [x] Toda mutação pública usa CAS tenant-scoped por `status + lifecycle_version` observados, incrementa uma vez e grava `updated_at`; conflitos não retryam nem produzem side effects.
- [x] Duas confirmações concorrentes e confirmação versus cancelamento/timeout, com sessões independentes e mesma versão inicial, produzem exatamente um vencedor; terceira sessão comprova versão, timestamps e efeitos únicos.
- [x] Mapper/round-trip preserva campos enriquecidos e `updated_at`; `lifecycle_version` não aparece no OpenAPI e `recent_messages` vem de projeção tenant-scoped limitada a cinco eventos, sem cópia no JSONB.
- [x] Abrir WhatsApp nunca chama endpoint; confirmar grava `data_hora_saida` uma vez, move para `in_progress`, remove o card somente após 2xx e retorna conflito sem sobrescrever em repetição.
- [x] PATCH genérico não entra em `in_progress`; conclusão exige confirmação e mantém `resolved_at` distinto.
- [x] Event Store, webhook/delivery status e métricas mantêm payloads, labels, ordem relativa e política de falha; rejeições/conflitos não duplicam efeitos.
- [x] `items`, radar e CSV usam o mesmo período `[start_utc, end_exclusive_utc)` no fuso da clínica, incluindo bordas, browser divergente, DST de 23/25 horas e fallback `America/Sao_Paulo` para configuração inválida.
- [x] Com meta 600, confirmações em 599/600/601 segundos são dentro/dentro/fora; no instante do deadline, não respondido já é vencido.
- [x] Categorias do radar são disjuntas e exaustivas; a coorte normativa retorna 2 de 5 e 40,0% com `ROUND_HALF_UP`, sem contar pendente no prazo nem cancelado não confirmado no denominador.
- [x] Sem elegíveis, `pct_within_sla=null`; sem confirmados, `avg_wait_seconds=null`; API/UI não convertem ausência em 0% ou 00:00:00.
- [x] OpenAPI expõe KPIs anuláveis, contagens, snapshot por item e período resolvido com offsets explícitos.
- [x] Cards usam o snapshot próprio: verde abaixo de 50%, amarelo de 50% a 100% inclusive, vermelho acima; legado é neutro, sem textos fixos de 5/10 minutos ou meta de 85%.
- [x] O export preserva autenticação, coorte por `created_at`, limite 10 mil, BOM, sete colunas, ordenação e timestamps UTC; deixa de aceitar fuso do navegador e inclui `in_progress` com confirmação.
- [x] Teste PostgreSQL chama o método de produção, sem copiar SQL; `scripts/check-handoff-sla-postgres.sh` exige a URL, executa o node ID dedicado e falha por exit não zero, skip ou xfail.
- [x] Suites backend/frontend focadas, gate PostgreSQL, typecheck/build, qualidade Python aplicável e `git diff --check` passam sem reverter as mudanças compatíveis do export.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — +2 por migration e estado persistido; testes determinísticos | engineer-standard / high | Fixar schema, configuração, classificador puro e contrato temporal. |

### Wave 2 (sequential, after Wave 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 4 — +2 por estado persistido e +2 por coordenação de Event Store/webhook/métricas | engineer-complex / high | Substituir o storage em memória pelo lifecycle PostgreSQL/CAS e preservar efeitos públicos. |

### Wave 3 (sequential, after Wave 2)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 3 — +2 por estado/tempo e +1 por gate de integração versionado | engineer-standard / high | Entregar estatística, fila, endpoints, schemas e prova PostgreSQL obrigatória. |

### Wave 4 (sequential, after Wave 3 and wish dependency)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 3 — +2 por contrato temporal e +1 por rework sobre implementação concorrente | engineer-standard / high | Conciliar o CSV concluído sem perder auth, limite, formato ou regressões existentes. |

### Wave 5 (sequential, after Wave 4)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 5 | engineer | 2 — +2 pela máquina de estado assíncrona da interação | engineer-standard / high | Atualizar fluxo, radar, cards, export e cobertura frontend. |

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

### Group 1: Schema e semântica temporal

**Goal:** Estabelecer persistência e funções puras para snapshot, classificação e período antes de trocar o lifecycle.

**Deliverables:**
1. Nova migration Alembic sobre o head vigente adicionando as três colunas e constraints, sem backfill de SLA/`updated_at`, com downgrade completo.
2. `src/models/handoff.py` atualizado com tipos/defaults coerentes e `src/models/clinic_config.py`/serviços de configuração validando inteiros reais de 1 a 120.
3. Helper puro no domínio de handoff para classificar categorias, calcular `ROUND_HALF_UP` e resolver o intervalo civil da clínica com um único `as_of` injetável.
4. Testes de migration, configuração e matriz pura, incluindo limites, legado, categorias disjuntas, nulabilidade, timezone-aware e DST.

**Acceptance Criteria:**
- [x] Migration e configuração satisfazem os critérios adversariais sem escrever valores históricos inventados.
- [x] A função pura cobre 599/600/601, deadline exato, cancelado, timeout, maturando, legado e arredondamento half-up.
- [x] O resolvedor retorna limites UTC semiabertos e fallback seguro sem usar o fuso do navegador.

**Validation:**
```bash
poetry run pytest tests/handoff/test_sla.py tests/api/test_handoff_sla_migration.py -v
poetry run black --check src/handoff/sla.py src/models/handoff.py src/models/clinic_config.py tests/handoff/test_sla.py tests/api/test_handoff_sla_migration.py
poetry run isort --check-only src/handoff/sla.py src/models/handoff.py src/models/clinic_config.py tests/handoff/test_sla.py tests/api/test_handoff_sla_migration.py
poetry run flake8 src/handoff/sla.py src/models/handoff.py src/models/clinic_config.py tests/handoff/test_sla.py tests/api/test_handoff_sla_migration.py
poetry run mypy src/handoff/sla.py src/models/handoff.py src/models/clinic_config.py --ignore-missing-imports
```

O gate é focado porque este grupo ainda não troca rotas; cobre todo schema e toda regra pura que os grupos seguintes consumirão.

**depends-on:** none

---

### Group 2: Lifecycle PostgreSQL canônico

**Goal:** Remover `_handoffs` como fonte funcional e fazer todas as transições sobreviverem a concorrência e restart sem perder efeitos públicos.

**Deliverables:**
1. `HandoffRecordRepository` com lookup/CAS tenant-scoped e resultados tipados; cada CAS fixa estado/versão observados, incrementa versão e grava `updated_at` no mesmo statement.
2. `HandoffService` stateless com factory de unit of work por operação; criação SQL deixa de ser best-effort e todas as leituras/transições usam o repositório.
3. Mapper único de `HandoffRecord` para DTOs, persistindo os campos enriquecidos e projetando no máximo cinco mensagens do Event Store pelo tenant, sem duplicar PII.
4. Lifecycle único para accept/confirm/complete/cancel/timeout e update permitido, incluindo confirmação write-once, conclusão condicionada e aliases legados.
5. Regressões de Event Store, webhook/delivery status e métricas para sucesso, erro e conflito, além de restart A/B/C e concorrência PostgreSQL real.

**Acceptance Criteria:**
- [x] Nenhum endpoint/pipeline/webhook depende do dicionário `_handoffs` para estado ou detalhe.
- [x] CAS concorrente tem um vencedor e um conflito, sem retry sobre estado novo e sem side effect do perdedor.
- [x] Round-trip preserva campos públicos e `updated_at`; versão interna não vaza.
- [x] A política atual de falha pós-commit do Event Store/webhook permanece explícita e testada.

**Validation:**
```bash
poetry run pytest tests/handoff/test_service.py -v
POSTGRES_SMOKE_DATABASE_URL='<admin-url-local>' poetry run pytest tests/integration/test_handoff_lifecycle_postgres.py -v -rs
poetry run black --check src/repositories/handoff.py src/services/handoff_service.py src/handoff/models.py tests/handoff/test_service.py tests/integration/test_handoff_lifecycle_postgres.py
poetry run isort --check-only src/repositories/handoff.py src/services/handoff_service.py src/handoff/models.py tests/handoff/test_service.py tests/integration/test_handoff_lifecycle_postgres.py
poetry run flake8 src/repositories/handoff.py src/services/handoff_service.py src/handoff/models.py tests/handoff/test_service.py tests/integration/test_handoff_lifecycle_postgres.py
poetry run mypy src/repositories/handoff.py src/services/handoff_service.py src/handoff/models.py --ignore-missing-imports
```

O teste unitário protege mapper/efeitos e o PostgreSQL real é obrigatório para lock, reavaliação do predicado e concorrência entre sessões.

**depends-on:** 1

---

### Group 3: Radar, fila e contrato da API

**Goal:** Fazer fila e radar consumirem a mesma coorte PostgreSQL e expor confirmação, contagens e no-data de forma verificável.

**Deliverables:**
1. Método de produção no repositório/serviço para itens e estatística usando o mesmo intervalo, snapshots e `as_of`, sem fórmula SQL duplicada em teste.
2. Schemas e rotas com `sla_target_seconds`, KPIs anuláveis, contagens disjuntas, período resolvido e contract test OpenAPI.
3. Endpoint de confirmação e alias `/checkout` depreciado; PATCH para `in_progress` e conclusão sem confirmação retornam 409 tipado.
4. Teste PostgreSQL descartável da coorte, isolamento tenant, arredondamento e query real.
5. `scripts/check-handoff-sla-postgres.sh` e seu teste de subprocesso, exigindo URL e recusando skip/xfail/execução incompleta.

**Acceptance Criteria:**
- [x] IDs de `items` e população do KPI usam exatamente o mesmo intervalo backend.
- [x] A API satisfaz as invariantes de categorias, nulabilidade, período e isolamento de tenant.
- [x] O teste PostgreSQL chama o método de produção e o gate obrigatório falha fechado.
- [x] Rotas não duplicam autorização, pre-read ou matriz do lifecycle.

**Validation:**
```bash
poetry run pytest tests/api/test_handoff_queue.py tests/scripts/test_check_handoff_sla_postgres.py -v
POSTGRES_SMOKE_DATABASE_URL='<admin-url-local>' ./scripts/check-handoff-sla-postgres.sh
poetry run black --check src/api/routes/handoff.py src/api/schemas/handoff.py src/repositories/handoff.py tests/api/test_handoff_queue.py tests/integration/test_handoff_sla_postgres.py tests/scripts/test_check_handoff_sla_postgres.py
poetry run isort --check-only src/api/routes/handoff.py src/api/schemas/handoff.py src/repositories/handoff.py tests/api/test_handoff_queue.py tests/integration/test_handoff_sla_postgres.py tests/scripts/test_check_handoff_sla_postgres.py
poetry run flake8 src/api/routes/handoff.py src/api/schemas/handoff.py src/repositories/handoff.py tests/api/test_handoff_queue.py tests/integration/test_handoff_sla_postgres.py tests/scripts/test_check_handoff_sla_postgres.py
poetry run mypy src/api/routes/handoff.py src/api/schemas/handoff.py src/repositories/handoff.py --ignore-missing-imports
```

O grupo alcança SQL temporal e schema público; por isso combina testes HTTP/contrato com o gate PostgreSQL dedicado e checks estáticos das superfícies tocadas.

**depends-on:** 1, 2

---

### Group 4: Conciliação do export

**Goal:** Aplicar o contrato temporal canônico ao CSV concluído pelo wish dependente sem perder suas melhorias compatíveis.

**Deliverables:**
1. Partir da implementação final de `handoff-export-open-closed`, preservando autenticação/overrides, coorte por `created_at`, intervalo semiaberto, BOM, sete colunas, `exported_at`, ordenação, limite e erros acionáveis.
2. Remover a autoridade do parâmetro de timezone do navegador; resolver período pelo fuso da clínica e manter células timestamp em UTC.
3. Incluir `in_progress` com `data_hora_saida` como atendimento confirmado; manter pending/accepted/completed elegíveis e cancelado/timeout fora das linhas.
4. Atualizar regressões CSV para browser diferente da clínica, DST, confirmação em progresso e preservação integral do contrato compatível.

**Acceptance Criteria:**
- [x] O dependency wish está concluído antes do início deste grupo e seu diff atual é tratado como baseline, não revertido.
- [x] CSV, fila e radar selecionam o mesmo dia civil da clínica.
- [x] `in_progress` confirmado usa `data_hora_saida - created_at`; nenhum estado inválido é fabricado.
- [x] Auth, limite, BOM, cabeçalho, ordem e mensagens de erro continuam cobertos.

**Validation:**
```bash
poetry run pytest tests/api/test_handoff_queue_export.py tests/api/test_handoff_queue.py -v
poetry run black --check src/api/routes/handoff.py tests/api/test_handoff_queue_export.py
poetry run isort --check-only src/api/routes/handoff.py tests/api/test_handoff_queue_export.py
poetry run flake8 src/api/routes/handoff.py tests/api/test_handoff_queue_export.py
poetry run mypy src/api/routes/handoff.py --ignore-missing-imports
```

O gate reutiliza toda a matriz do export dependente e a fila para detectar perda de comportamento ou divergência entre as duas coortes.

**depends-on:** 3

---

### Group 5: Fluxo e visualização frontend

**Goal:** Apresentar o SLA real por ocorrência e exigir confirmação explícita sem refiltrar dados do backend.

**Deliverables:**
1. Separar “Abrir WhatsApp” de “Confirmar atendimento”; bloquear duplo clique, manter card durante request/erro e remover somente após 2xx.
2. Remover `ymd(new Date(created_at))`, envio do timezone do navegador no export, meta fixa de 85% e legendas fixas 5/10.
3. Consumir KPIs anuláveis, contagens, período e snapshot por item; renderizar no-data, “within de eligible” e cores por razão, com legado neutro.
4. Atualizar textos PT/EN e testes com promises controladas para sucesso, 409/500, 2/5=40%, 66,7%=67%, limites de cor e browser diferente da clínica.

**Acceptance Criteria:**
- [x] Abrir nunca faz POST; confirmar em voo faz um único POST e não remove o card prematuramente.
- [x] Radar e média distinguem ausência de amostra de zero real.
- [x] Cards respeitam snapshot individual e os limites 0,5/1,0; legado não recebe SLA inventado.
- [x] Export deixa de enviar timezone do browser e preserva download/toasts compatíveis.

**Validation:**
```bash
(cd frontend/artifacts/neurostack && pnpm test -- handoff-queue.test.tsx)
(cd frontend/artifacts/neurostack && pnpm typecheck)
(cd frontend/artifacts/neurostack && pnpm build)
git diff --check -- frontend/artifacts/neurostack/src/pages/handoff-queue.tsx frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx frontend/artifacts/neurostack/src/locales/pt.ts frontend/artifacts/neurostack/src/locales/en.ts
```

Vitest cobre a máquina assíncrona e o mapeamento visual; typecheck/build protegem o contrato React/TypeScript e a integração do bundle.

**depends-on:** 3, 4

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [x] Com meta de 10 minutos, confirmar casos em 9:59, 10:00 e 10:01 e observar dentro/dentro/fora no radar e nas contagens.
- [x] Manter um handoff aberto até o deadline e verificar que ele passa de maturando para vencido sem precisar ser concluído.
- [x] Abrir WhatsApp sem confirmar e verificar que o card/relógio não mudam; depois confirmar e verificar remoção somente no sucesso.
- [x] Alterar a configuração de 10 para 5 minutos e comprovar que handoff antigo mantém 600s e novo recebe 300s.
- [x] Usar clínica/browser em fusos diferentes, bordas de meia-noite e dia DST; cards, KPI e CSV devem selecionar os mesmos IDs.
- [x] Exportar coorte com pending, accepted, in_progress confirmado e completed; conferir formato compatível e exclusões documentadas.
- [x] Simular 409/500 e duplo clique; card permanece, erro aparece e nenhum side effect é duplicado.
- [x] Reiniciar/reinstanciar o serviço entre criação e transição; detalhe, `updated_at`, status e efeitos permanecem coerentes.
- [x] Executar o gate PostgreSQL obrigatório com banco descartável e confirmar zero skipped/xfailed.
- [x] Rodar `make test`, `make format-check`, `make lint`, o teste/typecheck/build frontend e `git diff --check` após os gates focados.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Operador confirma sem de fato responder | Medium | Microcopy fala em confirmação, orienta confirmar após iniciar e não alega prova automática. |
| Legado não tem meta histórica | High | Excluir do KPI, contar como não mensurado e nunca aplicar meta atual retroativamente. |
| Migração do singleton perde campos ou efeitos | High | Mapper/persistência explícitos e regressões de restart, Event Store, webhook e métricas. |
| Concorrente aplica transição depois de aguardar lock | High | CAS fixa status/versão observados; perdedor recebe conflito e não retrya. |
| Fuso divergente muda população | High | Backend resolve uma vez pelo fuso da clínica e devolve período auditável. |
| Wish de export altera os mesmos arquivos | High | Dependência explícita, execução sequencial e baseline final preservado com supersessão limitada a fuso/`in_progress`. |
| Smoke PostgreSQL é ignorado | High | Script fail-closed, node ID dedicado e teste do próprio gate. |
| Efeito externo falha após commit | Medium | Preservar política atual e expô-la em testes; outbox/retry permanece fora do escopo. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — 2026-09-27T22:07:17Z

- **Context:** PLAN REVIEW
- **Target:** `.genie/wishes/handoff-sla-first-human-response/WISH.md`
- **Target SHA-256:** `36cdb6ad714702ab7f9c2645c083c942026fc0ae40754f936b7d337518ce4c05`
- **Reviewer:** `/root/wish_review_loop1`
- **Verdict:** SHIP
- **Gaps:** None.
- **Evidence:** O design vinculado possui selo SHIP válido no digest `e3d3c48edd43ae6f7a83b8316a3ac54b8106c7a71082a5e7997abb51fe0b7174`; escopo, Simplicity Case, dependência externa, DAG `1 → 2 → 3 → 4 → 5`, critérios e validações são completos e proporcionais. O wish `handoff-export-open-closed` permanece a dependência obrigatória e seu resultado final será preservado como baseline, com supersessão limitada a fuso da clínica e elegibilidade de `in_progress` confirmado.

### Execution Review — 2026-09-28T23:29:45Z

- **Context:** EXECUTION REVIEW — Grupo 1, `Schema e semântica temporal`
- **Snapshot:** working tree não commitada sobre `HEAD 96edb2579ded08c3a1b34549bb73d1ee86a77325`
- **Verdict:** FIX-FIRST
- **HIGH — boundary real de configuração:** `ClinicHandoff.meta_minutos_atendimento` valida corretamente `int` estrito entre 1 e 120, mas o PATCH público ainda encaminha extras e persiste `handoff` sem revalidar esse modelo. Assim, novos PATCHes ainda podem gravar `0`, `121`, boolean, float, string e `null`. Corrigir no boundary efetivamente usado antes de merge/flush/commit e adicionar teste de endpoint que prove `422` e ausência de persistência para toda a matriz adversarial.
- **Evidence:** migration/ORM coerentes; cadeia Alembic linear `020 → 021`; helper puro cobre fallback 600, categorias disjuntas, deadline inclusivo, `ROUND_HALF_UP`, timestamps aware, período UTC semiaberto, fallback IANA e DST; `lifecycle_version` não vaza; baseline do export não foi alterado. O teste de migration usa recorder + SQLite e a execução PostgreSQL real permanece no gate posterior.
- **Hash drift:** none. Os seis hashes congelados conferiram antes e depois da revisão.

### Execution Re-review — 2026-09-28T23:36:57Z

- **Context:** EXECUTION REVIEW — Grupo 1, fix loop 1/2
- **Verdict:** SHIP
- **Gaps:** None.
- **Fix evidence:** `ConfigPatchRequest` agora aplica `int` estrito 1..120 no boundary FastAPI; `model_dump(exclude_unset=True)` preserva omissão; o teste HTTP semeia 15, rejeita `0`, `121`, boolean, float, string e `null` com 422 e confirma que 15 permanece persistido. A mudança paralela de OpenAI/TTS foi preservada e não atribuída ao grupo.
- **Acceptance evidence:** cadeia Alembic linear `020 → 021`; migration/ORM coerentes; legado mantém SLA/`updated_at` nulos; helper cobre fallback 600, matriz temporal, categorias disjuntas, `ROUND_HALF_UP`, timezone-aware, período UTC semiaberto, fallback IANA e DST; `lifecycle_version` segue privado; export intacto.
- **Validation:** fixer reportou 110 testes PASS e Black/isort/flake8/mypy/diff-check PASS; revisão independente confirmou controle real, grafo Alembic, DTOs, semântica e whitespace.
- **Hash drift:** none. Os oito hashes congelados permaneceram idênticos.

### Quality Review — 2026-09-28T23:42:48Z

- **Context:** QUALITY REVIEW — Grupo 1, pós Execution Re-review
- **Verdict:** FIX-FIRST
- **Quality fix loop:** 1/1 required.
- **HIGH — elapsed time across DST:** `classify_handoff_sla` subtrai/compara datetimes diretamente; para o mesmo `ZoneInfo`, Python pode calcular relógio civil e inverter SLA no spring-forward/fall-back. Normalizar todos os instantes para UTC antes de duração/deadline e cobrir confirmados/não confirmados nas duas transições.
- **MEDIUM — invalid persisted-domain inputs:** o helper aceita metas fora de 60..7200 e status desconhecido; alinhar ao schema persistido e cobrir limites/extremo/status inválido.
- **MEDIUM — civil date upper bound:** `date.max + 1 day` gera `OverflowError`; rejeitar explicitamente o último dia não representável com erro estável e teste.
- **Positive evidence:** migration/ORM e PATCH continuam coerentes; categorias/half-up/intervalos/fallback são O(1); sem PII/cache; `lifecycle_version` privado; mudança paralela OpenAI/TTS intacta.
- **Hash drift:** none. Os oito hashes permaneceram congelados.

### Quality Re-review — 2026-09-28T23:57:06Z

- **Context:** QUALITY RE-REVIEW — Grupo 1, quality-fix loop 1/1 consumed
- **Verdict:** SHIP
- **Blocking gaps:** None.
- **Fix evidence:** classificação normaliza `created_at`, `confirmed_at` e `as_of` para UTC; testes discriminantes provam spring-forward (`within/maturing`) e fall-back (`late/overdue`) contra o comportamento civil antigo; meta aceita somente `None` ou `int` estrito 60..7200; seis status normativos; `date.max` falha antes da soma com erro contratual.
- **Validation:** fixer reportou 61 testes focados e 94 combinados PASS, além de Black/isort/Flake8/mypy/diff-check; re-review executou casos puros e diff-check read-only.
- **Residual risk (non-blocking):** extremos representacionais fora do fluxo real (`date.min` com offset histórico positivo ou conversão de `datetime.max` para zona positiva) ainda podem gerar `OverflowError`; quando o período ganhar boundary HTTP, limitar faixa civil/normalizar para 422 e cobrir esses extremos.
- **Hash drift:** none. Os oito hashes finais conferiram antes e depois.

### Final Group Validation — 2026-09-28T23:58:53Z

- **Context:** Orchestrator validation — Grupo 1, `Schema e semântica temporal`
- **Result:** PASS
- **Scope rationale:** o grupo altera migration/schema, validação de configuração e helpers puros, sem trocar rotas de handoff/lifecycle; a suíte cobre a matriz temporal, DST, migration contract e o boundary HTTP real de configuração. Checks estáticos abrangem todos os fontes tocados; `alembic heads` comprova cadeia linear.
- **Tests:** `poetry run pytest tests/handoff/test_sla.py tests/api/test_handoff_sla_migration.py tests/api/test_config_credentials_boundary.py -v` — PASS, 94 tests, 3 pre-existing warnings, 5.28s.
- **Static gates:** Black PASS (7 files); isort PASS; Flake8 PASS; mypy PASS (4 source files).
- **Migration/whitespace:** `poetry run alembic heads` → `021 (head)`; scoped `git diff --check` PASS.
- **Final hashes:** migration `860b8240...`; ORM handoff `c33e9071...`; clinic config `d6f36ddf...`; SLA helper `1e02b8d7...`; SLA tests `96050bb6...`; migration tests `4dc6a060...`; config schema `b8f2a2a9...`; config HTTP tests `c652339b...`.

### Execution Review — 2026-09-29T00:33:59Z

- **Context:** EXECUTION REVIEW — Grupo 2, `Lifecycle PostgreSQL canônico`
- **Verdict:** FIX-FIRST
- **HIGH — confirmation write-once:** confirmação aceita apenas `pending/accepted`, rejeita o legado obrigatório `in_progress + data_hora_saida NULL` e transforma repetição em transition error antes do CAS. Permitir os três estados e deixar a precondição write-once/CAS produzir conflito sem overwrite.
- **HIGH — tenant scope:** operações públicas ainda permitem `tenant_id=None` e fazem lookup unscoped; tornar tenant obrigatório, remover o caminho legacy unscoped e encaminhar tenant nos call sites reais, com prova HTTP de isolamento.
- **HIGH — HTTP baseline:** PATCH rejeita `in_progress`, não existe call site produtivo de confirmação, e conclusão prematura deixa erro escapar como 500. Entregar confirmação mínima e mapear transition/conflict para 409 sem alterar o export.
- **HIGH — service-level concurrency evidence:** o PostgreSQL smoke exerce o repository diretamente e não comprova commit orchestration nem efeitos únicos. Executar competidores por instâncias independentes de `HandoffService`, forçar lock real e afirmar um vencedor, um conflito, estado por terceira sessão e um único conjunto de efeitos.
- **Positive evidence:** serviço sem `_handoffs`/sessão retida; insert obrigatório e snapshot no mesmo UoW; CAS repository usa tenant/status/version/precondição/RETURNING sem retry; mapper/projeção bounded; ordem pós-commit explícita; harness observa row lock e tem teardown defensivo.
- **Hash drift:** none. Os sete hashes congelados conferiram antes/depois.

### Compatibility Audit — 2026-09-29T00:38:00Z

- **Verdict:** FIX-FIRST (supplemental read-only audit)
- **HIGH:** novas exceções são traduzidas de forma inconsistente: PATCH converte inclusive persistence/post-commit para 400; accept/complete/cancel deixam transition/conflict virar 500.
- **HIGH:** o singleton captura uma `get_db_session` monkeypatched no construtor e contamina testes/callers posteriores; reprodução ordenada produz 500, enquanto processo limpo passa.
- **HIGH:** Event Store não é injetado nos call sites produtivos; projeções ficam vazias, eventos não são emitidos e injeção tardia pode criar split-brain entre instâncias.
- **MEDIUM:** webhook captura falha da persistência agora obrigatória e ainda pode limpar flag/enviar confirmação de handoff inexistente.
- **Deferred to Group 3:** contrato final/OpenAPI do endpoint, alias checkout e KPI/fila; nenhuma quebra imediata de baseline pode ser adiada.
- **Hash drift:** none.

### Execution Re-review — 2026-09-29T01:19:54Z

- **Context:** EXECUTION RE-REVIEW — Grupo 2, fix loop 1/2
- **Verdict:** FIX-FIRST
- **HIGH — alternate lifecycle:** `src/handoff/service.py` ainda mantém `_handoffs`/transições/singleton próprios e os mutadores do ORM continuam permitindo bypass sem tenant-CAS, versão ou efeitos. Substituir o módulo legado por shim canônico e remover/desativar os mutadores, com regressão.
- **HIGH — builder/super-admin checkout:** a rota usa o tenant do token e o frontend não envia a clínica selecionada; para operador privilegiado gerenciando outra clínica, checkout retorna 404. Encaminhar `tenant_id` explícito, autorizá-lo pelo resolver existente e cobrir API/frontend.
- **HIGH — checkout/export regression:** checkout agora confirma para `in_progress`, mas o export exclui esse estado. Incluir somente `in_progress` confirmado e adicionar regressão HTTP checkout → export do mesmo registro, preservando formato/auth/limite.
- **Fixes confirmed:** confirmação legado/write-once; tenant obrigatório no serviço canônico; erros HTTP; providers lazy; Event Store default; falha Meta obrigatória; concorrência por instâncias reais com efeitos únicos.
- **Validation evidence:** 185 testes combinados, reprodução ordenada 3/3, migration 7/7, gates estáticos PASS e PostgreSQL real 4/4 PASS, zero skip/xfail.
- **Hash drift:** none nos 11 arquivos congelados; os dois bypasses legados não estavam no conjunto congelado.

### Quality Re-review — 2026-09-28T23:53:52Z

- **Context:** QUALITY RE-REVIEW — Grupo 1, fix final.
- **Reviewer:** `/root/sla_group1_quality_review2`
- **Verdict:** SHIP
- **Gaps:** nenhum CRITICAL/HIGH/MEDIUM.
- **Fix evidence:** `created_at`, `confirmed_at` e `as_of` são normalizados para UTC antes de duração, deadline e classificação; testes de spring-forward e fall-back distinguem tempo absoluto de relógio civil para casos confirmados e abertos; snapshots aceitam 60/7200 e rejeitam 59/7201, booleanos e tipos coercíveis; `date.max` falha com `ValueError` estável antes da soma de um dia.
- **Independent validation:** 13 testes adversariais PASS e `git diff --check` PASS. Os seis arquivos fora do fix preservaram os hashes congelados; somente `src/handoff/sla.py` e `tests/handoff/test_sla.py` mudaram.

### Group 1 Validation — 2026-09-28T23:53:52Z

- **PostgreSQL obrigatório:** 61 PASS, zero skip/xfail, incluindo upgrade `020 → 021`, constraints, downgrade e semântica pura.
- **Compatibilidade focada:** 82 PASS em configuração, clone, credentials boundary, export e application-core imports.
- **Gate agregado:** `full_suite_gate` aceito com 3050 PASS, 17 skips e somente as duas falhas históricas registradas em `tests/baselines/neurostack-core-extraction-known-failures.txt`; completion validator PASS.
- **Qualidade global:** `make format-check` PASS; `make lint` PASS (`flake8` e mypy em 168 arquivos); `git diff --check` PASS.
- **Validation fix review:** a única regressão inicialmente nova era a allowlist exata de `src/handoff/sla.py`; o fix de uma linha recebeu SHIP independente e o teste de fronteira passou 2/2.

### Group 2 Execution Review — 2026-09-29T00:59:29Z

- **Context:** EXECUTION REVIEW — Grupo 2, `Lifecycle PostgreSQL canônico`.
- **Reviewer:** `/root/qa_lens`
- **Verdict:** FIX-FIRST.
- **HIGH:** Event Store não estava ligado na composição real; `src/handoff/service.py` e mutadores ORM ainda ofereciam fontes alternativas sem CAS; `/checkout` fazia pre-read global; faltavam provas da política pós-commit nas transições.
- **Positive evidence:** UoW por operação, snapshot e insert transacionais, CAS tenant-scoped por estado/versão sem retry, write-once e quatro corridas PostgreSQL reais estavam corretos.

### Group 2 Execution Re-review — 2026-09-29T00:59:29Z

- **Context:** EXECUTION RE-REVIEW — fix loop 1/2.
- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM.
- **Fix evidence:** Event Store lazy preserva injeção; serviço antigo virou shim canônico; mutadores ORM CAS-bypass foram removidos; checkout resolve autoridade antes do CAS e retorna 404 uniforme; transições cobrem falha de Event Store, delivery `FAILED`, `WebhookError` e falha de bookkeeping com estado/ordem/métricas explícitos.
- **Concurrency evidence:** quatro testes PostgreSQL usam `NullPool`, sessões independentes, barreira após observação comum, timeouts, terceira sessão e provam um vencedor, um conflito e efeitos únicos.

### Group 2 Validation Re-review — 2026-09-29T00:59:29Z

- **Context:** VALIDATION RE-REVIEW — fix loop 2/2.
- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM.
- **Aggregate fix:** quatro E2E que usavam sessões inexistentes agora semeiam `Tenant`/`Session` reais dentro do lifespan, limpam em ordem segura e comprovam `HandoffRecord` durável `pending` com snapshot 600; nenhum fonte de produção mudou no loop.
- **Focused validation:** 97 PASS em serviço/rota/pipeline; 4 PASS PostgreSQL com zero skip; 16 PASS E2E; 13 PASS import/export.
- **Aggregate gate:** `full_suite_gate` aceito com 3079 PASS, 21 skips e somente as duas falhas históricas registradas; completion validator PASS.
- **Quality:** `make format-check`, `make lint` (mypy em 168 fontes) e `git diff --check` PASS.

### Group 3 Execution Review — 2026-09-29T01:43:14Z

- **Context:** EXECUTION REVIEW — Grupo 3, `Radar, fila e contrato da API`.
- **Reviewer:** `/root/qa_lens`.
- **Verdict:** FIX-FIRST.
- **MEDIUM:** o intervalo extremo `Asia/Tokyo` + `0001-01-01` podia gerar `OverflowError` na conversão UTC e escapar como HTTP 500. O boundary deveria responder o contrato estável `400 invalid_date_range` antes de consultar a coorte.
- **Positive evidence:** fila e KPIs derivam de uma única query tenant-scoped, um único `as_of` e o mesmo período UTC semiaberto; as seis categorias são disjuntas; endpoints de confirmação/checkout compartilham autorização e lifecycle; o CSV permaneceu intacto para o Grupo 4.

### Group 3 Execution Re-review — 2026-09-29T01:43:14Z

- **Context:** EXECUTION RE-REVIEW — fix loop 1/2.
- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM.
- **Fix evidence:** `get_queue_sla` converte `OverflowError`, `TypeError` e `ValueError` em `HandoffSlaPeriodError`; teste HTTP real com clínica `Asia/Tokyo` e data mínima comprova `400 invalid_date_range` e ausência de chamada a `find_sla_cohort`.
- **Focused evidence:** 7 testes de fila PASS; 161 regressões PASS e 1 skip previsto; gate PostgreSQL 1 PASS, zero skip/xfail; checks estáticos, `bash -n` e `git diff --check` PASS.

### Group 3 Validation Re-review — 2026-09-29T01:43:14Z

- **Context:** VALIDATION RE-REVIEW — fix loop 2/2.
- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM.
- **Aggregate fix:** a primeira suíte completa isolou uma única regressão nova no inventário fechado de consumidores do core. `src/services/handoff_service.py` foi incluído explicitamente na allowlist por importar `resolve_clinic_tz` da fachada pública `neurostack_core.prompt`; nenhum wildcard, exclusão ou deep import foi introduzido.
- **Independent evidence:** auditoria AST encontrou exatamente os oito consumidores declarados; teste do boundary 2 PASS; Black/isort/Flake8/diff-check e `bash -n` PASS.

### Group 3 Validation — 2026-09-29T01:43:14Z

- **API e gate:** 7 testes de fila e 15 testes do script fail-closed PASS; script preservado como executável `755`.
- **PostgreSQL obrigatório:** `POSTGRES_SMOKE_DATABASE_URL='postgresql:///postgres?host=/var/run/postgresql' ./scripts/check-handoff-sla-postgres.sh` — 1 PASS, zero skip/xfail. O teste chama `HandoffService.get_queue_sla` e prova 599/600/601, deadline, coorte normativa 2/5 = 40,0%, categorias `2/1/2/1/1/1`, snapshot histórico, isolamento tenant, limites, no-data, arredondamento 6,3%, fuso divergente e DST de 23 horas.
- **Contrato:** KPIs anuláveis, seis contagens explicativas, snapshot por item e período resolvido estão cobertos; `items` é subconjunto da mesma coorte usada no radar e o default cobre 31 datas civis inclusive.
- **Gate agregado final:** `full_suite_gate` aceito com 3099 PASS, 22 skips e somente as duas falhas históricas registradas; completion validator PASS.
- **Qualidade global:** `make format-check` PASS (293 arquivos); `make lint` PASS (mypy em 168 fontes); `git diff --check` PASS.

### Group 2 Execution Re-review — 2026-09-29T01:45:44Z (root orchestration)

- **Context:** EXECUTION RE-REVIEW — Grupo 2, fix loop 2/2
- **Verdict:** SHIP
- **Gaps:** None CRITICAL/HIGH/MEDIUM.
- **Lifecycle:** o módulo histórico é shim por identidade do serviço/singleton canônico; não possui `_service`, `_handoffs` ou implementação própria. Mutadores ORM CAS-bypass foram removidos e os read helpers permanecem.
- **Checkout/auth:** tenant selecionado é resolvido antes do CAS; clinic admin, builder, super-admin e negação cross-tenant estão cobertos. O frontend envia `effectiveTenantId` via `URLSearchParams`.
- **Export:** inclui `in_progress` somente com `data_hora_saida`; o inválido é excluído. Regressão checkout privilegiado → export comprova a mesma linha e preserva isolamento, coorte, limite, BOM, sete colunas, UTC e CSV security.
- **Prior fixes reconfirmed:** write-once legado, tenant scope, taxonomia HTTP, providers lazy, Event Store produtivo, falha Meta obrigatória e concorrência PostgreSQL por instâncias reais com efeitos únicos.
- **Validation:** 188 Python PASS; 17 frontend PASS; typecheck/Black/isort/Flake8/mypy/diff-check PASS; PostgreSQL 16 efêmero 4/4 PASS, zero skip/xfail, com teardown concluído.
- **Hash drift:** none nos 16 artefatos congelados.

### Group 2 Quality Review — 2026-09-29T01:56:11Z (root orchestration)

- **Context:** QUALITY REVIEW — Grupo 2, pós Execution Re-review SHIP
- **Verdict:** FIX-FIRST
- **Quality fix loop:** 1/1 required.
- **HIGH — Event Store initialization/lifecycle:** o provider global publica instância antes de `connect()` completar, pode devolver objeto desconectado a caller concorrente, fica envenenado após falha e não é fechado no lifespan. Tornar inicialização single-flight, publicação pós-connect, falha limpável/retryable e teardown serializado; fechar Event Store e handoff/dispatcher no lifespan, com testes de corrida/falha/retry/close.
- **MEDIUM — duplicate projection/count:** transição com webhook projeta mensagens duas vezes e cada projeção também executa `COUNT(*)` desnecessário. Projetar uma vez e reutilizar; adicionar leitura limitada sem count e prova de chamadas/queries.
- **MEDIUM — post-commit evidence:** cobrir falha Event Store em transição, delivery `FAILED`, falha de bookkeeping após envio, estado/versão/ordem/payload sanitizado e deltas de métricas.
- **MEDIUM — negative export durations:** timestamps invertidos podem produzir waits negativos. Usar origem temporal consistente e tratamento fail-closed explícito para confirmado invertido e aberto futuro, com regressões; não clamp silencioso.
- **Positive evidence:** tenant/CAS/auth/write-once/shim/mapper/CSV security/harness PostgreSQL permanecem corretos.
- **Hash drift:** none nos 16 artefatos congelados.

### Group 4 Execution Review — 2026-09-29T01:59:30Z

- **Context:** EXECUTION REVIEW — Grupo 4, `Conciliação do export`.
- **Reviewer:** `/root/qa_lens`.
- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM.
- **Baseline:** o wish `handoff-export-open-closed` está mergeado em `bfa18f8` e foi preservado: auth/overrides, coorte por `created_at`, intervalo semiaberto, BOM, sete colunas, timestamps UTC, `exported_at` único pós-query, ordem, limite 10.000, erros e neutralização CSV.
- **Temporal contract:** fila/radar/export chamam o mesmo resolvedor de período no serviço, com defaults civis da clínica, fallback `America/Sao_Paulo` e erro estável para extremos. O parâmetro legado do browser saiu do OpenAPI e extras válidos ou inválidos não mudam a coorte.
- **Eligibility:** `pending`/`accepted` permanecem abertos; `completed`/`in_progress` entram somente com `data_hora_saida` e usam espera fechada; cancelados/timeouts permanecem fora mesmo se tiverem timestamp.
- **Residual risk accepted:** resolução da configuração/período e materialização do CSV usam UoWs separadas, mas o intervalo já está materializado e não sofre segunda interpretação temporal.

### Group 4 Validation — 2026-09-29T01:59:30Z

- **Focused:** `poetry run pytest tests/api/test_handoff_queue_export.py tests/api/test_handoff_queue.py -q` — 19 PASS. A matriz cobre mesmos IDs fila/CSV nas bordas de um dia DST de 23 horas, browser em outro fuso, fallback São Paulo, `in_progress` confirmado, estados inválidos/excluídos e todo o baseline compatível.
- **Related regressions:** 166 PASS em SLA, serviço, sessões e pipeline.
- **PostgreSQL obrigatório:** gate SLA 1 PASS, zero skip/xfail.
- **Gate agregado:** `full_suite_gate` aceito com 3100 PASS, 22 skips e somente as duas falhas históricas registradas; completion validator PASS.
- **Qualidade global:** `make format-check` PASS (293 arquivos); `make lint` PASS (mypy em 168 fontes); `git diff --check` PASS.

### Group 2 Quality Re-review — 2026-09-29T02:16:42Z (root orchestration)

- **Context:** QUALITY RE-REVIEW — quality-fix loop 1/1 consumed.
- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM nos quatro achados autorizados.
- **Event Store lifecycle:** inicialização single-flight, publicação somente após probe, descarte/retry após falha e fechamento serializado; o lifespan fecha handoff/dispatcher, Event Store e banco mesmo quando cleanup anterior falha.
- **Projection:** a leitura recente executa um único `SELECT ... LIMIT`, sem `COUNT(*)`; cada transição calcula uma projeção e a reutiliza no webhook e no DTO.
- **Post-commit evidence:** testes discriminam estado/versão persistidos, ordem commit→evento→webhook→bookkeeping, supressão após falha de evento, delivery `FAILED` sanitizado, envio único antes de falha de bookkeeping e deltas exatos de métricas.
- **Temporal integrity:** mutações usam relógio do banco; export rejeita aberto futuro e confirmação anterior à criação com `400 invalid_handoff_timestamps`, sem clamp ou CSV parcial.
- **Drift:** os dez hashes congelados permaneceram idênticos durante a revisão read-only.

### Group 2 Validation Fix — 2026-09-29T02:19:06Z (root orchestration)

- **First PostgreSQL run:** 1 PASS e 3 FAIL exclusivamente porque o teste de corrida ainda esperava as duas projeções antigas; produção retornou corretamente uma única projeção por transição vencedora.
- **Fix:** somente `tests/integration/test_handoff_lifecycle_postgres.py` foi ajustado para exigir uma chamada `(tenant_id, session_id, 5)`; nenhum fonte de produção mudou. Black, isort e Flake8 do arquivo passaram.

### Group 2 Final Validation — 2026-09-29T02:19:06Z (root orchestration)

- **Affected suites:** 247 PASS; regressão de lifespan existente 2 PASS.
- **PostgreSQL 16 efêmero:** 4/4 PASS, zero skip/xfail, cobrindo instâncias A/B/C, dupla confirmação e confirmação contra cancelamento/timeout; cluster isolado encerrado e descartado.
- **Static gates:** Black (9 arquivos), isort, Flake8, mypy (5 fontes) e `git diff --check` PASS.
- **Result:** Group 2 SHIP. O snapshot primário pode avançar para o port semântico do Grupo 3; evidência posterior de Grupos 3–4 registrada acima pertence à worktree/branch dedicada e ainda precisa ser reconciliada neste checkout.

### Group 5 Execution Review — 2026-09-29T02:43:12Z

- **Context:** EXECUTION REVIEW — Grupo 5, `Fluxo e visualização SLA no frontend`.
- **Reviewer:** `/root/qa_lens`.
- **Verdict:** FIX-FIRST.
- **HIGH:** datas iniciais ainda vinham do browser e sobrepunham o default civil da clínica; confirmação D1 em voo podia invalidar a busca D2; posteriormente a chave por período permitia segundo POST do mesmo handoff em D2.
- **MEDIUM:** horário de entrada do card ainda era formatado no timezone do browser sob um cabeçalho governado pela clínica.
- **Positive evidence:** ação WhatsApp sem POST, endpoint canônico/tenant efetivo, KPI nullable, contagens, percentuais, cores por snapshot e export sem timezone já estavam corretos.

### Group 5 Execution Re-review — 2026-09-29T02:43:12Z

- **Context:** EXECUTION RE-REVIEW — fix loops 1/2 e 2/2.
- **Verdict final:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM.
- **Clinic period:** GET inicial omite datas, a resposta hidrata os controles pelo período civil da clínica sem loop e troca de tenant reinicia o default backend. Datas editadas são explícitas; export usa o período hidratado/selecionado e nunca envia timezone.
- **Concurrency:** identidade in-flight estável `tenant:id` bloqueia duplicata entre períodos; scope capturado controla apenas mutação/toast obsoleto. Todo 2xx dispara GET autoritativo pós-commit no período atual, invalidando respostas pré-commit e atualizando cards e radar juntos.
- **Presentation:** timestamps usam `period_timezone`; no-data não vira zero; 66,7% permanece no label acessível e 67% no visual; cards usam `<0,5`, `0,5..1,0`, `>1,0` e legado neutro; não restam meta 85% nem thresholds fixos 5/10.
- **Hash drift audit:** formatação posterior ficou restrita aos locales, sem mudança semântica; veredito SHIP mantido.

### Group 5 Validation — 2026-09-29T02:43:12Z

- **Focused:** `vitest ... handoff-queue.test.tsx` — 23 PASS, incluindo promises controladas para abrir/confirmar, D1→D2, deduplicação, refetch autoritativo, 409/500, tenant privilegiado, browser divergente, no-data, percentuais, limites de cor e export.
- **Frontend aggregate:** 27 arquivos / 268 testes PASS; somente warnings preexistentes de `act()`.
- **Type/build:** TypeScript `--noEmit` PASS; Vite production build PASS com `PORT=3001 BASE_PATH=/`, somente warnings conhecidos de sourcemap/chunk.
- **Style/whitespace:** Prettier dos quatro arquivos PASS; `git diff --check` PASS.

---

### Group 3 Quality Fix — 2026-09-29T03:20:11Z (root orchestration)

- **Context:** quality-fix loop 1/1, após o port semântico para o checkout primário.
- **Fixes:** fila e export usam o resolvedor canônico deny-by-default; investor/role desconhecido são negados sem PII; período civil é limitado a 90 dias decorridos; coorte usa projeção estreita em stream e agregação em uma passagem; confirmação responde com a projeção do mesmo resultado commitado, sem requery; OpenAPI ganhou enums, limites e modelos 404/409/503.
- **Performance evidence:** o gate PostgreSQL registra `EXPLAIN` representativo com predicados de tenant/data e estimativa limitada; nenhum índice sem evidência foi adicionado.
- **Validation:** 198 testes focados PASS; PostgreSQL real 1 PASS, zero skip/xfail; Black, isort, Flake8, mypy, compileall, `bash -n` e diff-check PASS.
- **Known Group 4 handoff:** duas expectativas antigas do teste de export ainda esperam `tenant_mismatch`; a produção retorna corretamente o erro canônico `tenant_override_forbidden` e as outras 12 regressões passam.

### Group 3 Quality Re-review — 2026-09-29T03:20:11Z (root orchestration)

- **Context:** QUALITY RE-REVIEW — quality-fix loop 1/1 consumido.
- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM.
- **Independent evidence:** autorização, bounded query/stream, `EXPLAIN`, resposta sem requery pós-commit, schemas OpenAPI e invariantes CAS/write-once do Grupo 2 foram auditados no snapshot exato.
- **Hash drift:** none nos onze artefatos congelados antes/depois da revisão.

### Group 3 Final Validation — 2026-09-29T03:20:11Z (root orchestration)

- **Focused:** 198 PASS em SLA puro, serviço, fila HTTP/OpenAPI e gate fail-closed.
- **PostgreSQL obrigatório:** 1 PASS, zero skip/xfail, usando o método de produção e removendo o banco descartável ao final.
- **Quality:** Black, isort, Flake8, mypy, compileall, `bash -n` e scoped `git diff --check` PASS.
- **Result:** Grupo 3 SHIP no checkout primário. O Grupo 4 deve reconciliar apenas o contrato temporal do export e as duas expectativas de auth legadas, preservando todos os hardenings anteriores.

### Group 4 Execution Review — 2026-09-29T03:36:11Z (root orchestration)

- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM.
- **Temporal contract:** export e fila/radar compartilham `HandoffService.resolve_queue_period`; fuso do browser saiu do parâmetro/OpenAPI; defaults, fallback São Paulo, janela de 90 dias, intervalo UTC semiaberto e DST permanecem cobertos.
- **Compatibility:** autorização canônica, elegibilidade, BOM, sete colunas, timestamps UTC, ordem, sentinel 10.001, neutralização de fórmula, checkout→export e rejeição temporal fail-closed foram preservados.
- **Residual accepted:** resolução da configuração e materialização do CSV usam UoWs distintos, mas o intervalo UTC já resolvido é imutável e não é reinterpretado.

### Group 4 Quality Review — 2026-09-29T03:36:11Z (root orchestration)

- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH/MEDIUM.
- **Security/performance:** tenant é resolvido antes de qualquer leitura; somente cinco colunas estreitas são carregadas; `LIMIT 10001` limita memória; validação completa ocorre antes de construir `StringIO`/`StreamingResponse`, portanto erros não retornam CSV parcial.
- **Hash drift:** none nos três artefatos congelados.

### Group 4 Final Validation — 2026-09-29T03:36:11Z (root orchestration)

- **Focused:** 58 PASS em export e fila.
- **PostgreSQL obrigatório:** 1 PASS, zero skip/xfail.
- **Quality:** Black, isort, Flake8, mypy, compileall e scoped `git diff --check` PASS.
- **Result:** Grupo 4 SHIP no checkout primário; dependência pronta para o port semântico do frontend do Grupo 5.

### Dedicated Branch Reconciliation Review — 2026-09-29T03:51:34Z

- **Context:** reconciliação final dos hardenings aprovados no branch dedicado `wish/handoff-sla-first-human-response`, commits `a011873` e `0474094`.
- **Group 2 lifecycle hardening:** `/root/qa_lens` retornou SHIP sem gaps CRITICAL/HIGH/MEDIUM; Event Store single-flight/retry/close, cleanup de lifespan, projeção única, efeitos pós-commit sanitizados, timestamps do banco e export fail-closed foram preservados no lifecycle genérico CAS.
- **Frontend coherence fix:** `/root/aggregate_preflight` retornou SHIP; período editado não exibe coorte antiga, export fica bloqueado até resposta autoritativa 2xx, confirmação permanece deduplicada por `tenant:id` e a microcopy orienta confirmar somente após iniciar o atendimento.
- **Group 3 quality port:** `/root/group3_port_review` retornou SHIP sem gaps CRITICAL/HIGH/MEDIUM no diff `dff3ac9339b55079abe87edb16dfaf4811511db7f3c1e2a8c0ecb1a20b6877d0`; autorização deny-by-default, stream estreito, agregação em uma passagem, janela 90/91, resposta sem requery, contratos OpenAPI e prova `EXPLAIN` foram portados sem importar a arquitetura lifecycle divergente do checkout compartilhado.
- **Hash drift:** nenhum nos snapshots congelados de cada re-review; branch dedicado ficou limpo após os dois commits.

### Final Feature Validation — 2026-09-29T03:51:34Z

- **Backend aggregate:** `full_suite_gate` aceito com 3122 PASS, 22 skips e somente as duas falhas históricas registradas em `tests/baselines/neurostack-core-extraction-known-failures.txt`; completion validator PASS.
- **PostgreSQL obrigatório:** 18 PASS, zero skip/xfail, cobrindo migration real, quatro corridas lifecycle A/B/C e a coorte SLA de produção com DST 23/25 horas e `ANALYZE`/`EXPLAIN` representativo.
- **Frontend aggregate:** 27 arquivos / 273 testes PASS; teste focado da fila 28 PASS; TypeScript `--noEmit` e build Vite production PASS, somente warnings conhecidos de sourcemap/chunk/`act()`.
- **Focused reconciliation:** E2E 16 PASS e agregado lifecycle 190 PASS; Group 3 com 27 API, 71 service, 53 regressões relacionadas e 15 testes do gate fail-closed PASS.
- **Quality:** Black/isort em 294 arquivos, Flake8 e mypy em 168 fontes PASS; Prettier dos quatro artefatos frontend e `git diff --check` PASS.
- **Result:** todos os Success Criteria e QA Criteria possuem evidência automatizada; branch final `0474094` está pronto para revisão global e handoff.

### Final Global Review — 2026-09-29T04:16:31Z

- **Initial verdict:** FIX-FIRST por dois gaps HIGH: o alias depreciado `/checkout` havia perdido o contrato legado `HandoffCheckoutResponse`, e um export já em voo podia concluir após troca de período ou clínica.
- **Fixes:** `/checkout` volta a adaptar o registro vencedor do CAS sem nova leitura ou mutação, enquanto `/confirm` permanece `HandoffDetail`; o frontend captura tenant, escopo e geração monotônica e revalida a identidade após cada fronteira assíncrona antes de produzir o download.
- **Temporal compatibility:** a espera do checkout preserva a subtração bruta do contrato legado e não mascara timestamps invertidos com `abs()`.
- **Final verdict:** `/root/final_feature_gate` retornou SHIP sem gaps CRITICAL/HIGH/MEDIUM e confirmou que o commit `87a55f3ce93ba328ec6e3c1a7eb8eb1ddc8893fa` contém exatamente os sete arquivos revisados, com hashes idênticos e parent `04740948c3f1bb1499d855473af7e6bda0b3b77b`.

### Final Snapshot Validation — 2026-09-29T04:16:31Z

- **Backend aggregate:** `full_suite_gate` aceito no snapshot final com 3124 PASS, 22 skips e somente as duas falhas históricas registradas em `tests/baselines/neurostack-core-extraction-known-failures.txt`; completion validator PASS.
- **Focused:** 86 PASS na rota e serviço de handoff; os testes finais de runtime/OpenAPI e compatibilidade temporal passaram no re-review independente.
- **PostgreSQL obrigatório:** 18 PASS, zero skip/xfail, cobrindo migration, quatro corridas lifecycle com sessões independentes e a coorte SLA real com bordas, snapshot, isolamento, DST e plano de query representativo.
- **Frontend:** 27 arquivos / 277 testes PASS; fila focada 32 PASS; TypeScript `--noEmit` e build Vite production PASS. Os avisos de sourcemap, chunk e `act()` são preexistentes e não bloqueantes.
- **Quality:** Black/isort em 294 arquivos, Flake8 e mypy em 168 fontes, Prettier e `git diff --check` PASS.
- **Result:** branch `wish/handoff-sla-first-human-response` limpa em `87a55f3`; implementação e evidências completas, pronta para PR/merge.

### Group 5 Primary-Checkout Reconciliation — 2026-09-29T04:42:18Z (root orchestration)

- **Execution loop 1:** corrigiu a coerência de escopo carregado, limpou cards/KPIs ao editar período ou clínica, bloqueou export até a resposta 2xx autoritativa e restaurou a orientação para confirmar somente após iniciar o atendimento humano.
- **Execution loop 2:** eliminou a corrida de export em voo com geração monotônica e revalidação de tenant, escopo solicitado e escopo carregado após cada fronteira assíncrona, inclusive troca de clínica A→B→A e criação de object URL.
- **Execution verdict:** SHIP sem gaps CRITICAL/HIGH/MEDIUM após os dois loops previstos; confirmação segue deduplicada por `tenant:id`, respostas obsoletas não removem cards nem emitem toasts e downloads obsoletos não são produzidos.
- **Quality verdict:** revisão independente SHIP sem gaps CRITICAL/HIGH/MEDIUM; privacidade, acessibilidade, KPIs nulos, labels percentuais, snapshots de SLA, localização PT/EN e ausência de timezone do browser foram preservadas.
- **Validation:** 35 testes focados e 319 testes frontend agregados PASS; TypeScript `--noEmit`, build Vite production, Prettier do slice e `git diff --check` PASS. A falha de Prettier nos arquivos completos de locale permanece herdada fora do slice de handoff.
- **Hash drift:** nenhum nos quatro artefatos congelados (`handoff-queue.tsx` `3bd3c797…`, teste `8e01c0cc…`, PT `f587dd8c…`, EN `771255f8…`).

### Primary-Checkout Final Contract — 2026-09-29T04:42:18Z (root orchestration)

- **Backend contract:** `/confirm` retorna o `HandoffDetail` canônico após uma única transição; `/checkout` mantém o `HandoffCheckoutResponse` legado a partir do mesmo registro vencedor, sem requery pós-commit nem segunda mutação e com espera bruta assinada.
- **Independent verdict:** SHIP sem gaps CRITICAL/HIGH/MEDIUM; autorização tenant-scoped, CAS, confirmação write-once, coorte SLA estreita, janela máxima de 90 dias, compatibilidade do export e schemas OpenAPI distintos permanecem corretos.
- **Validation:** 133 testes focados PASS; Black, isort, Flake8, mypy, compile e `git diff --check` PASS.
- **Snapshot:** hashes finais do backend e frontend conferidos novamente no checkout primário, sem drift; a dependência está concluída e liberada para `handoff-standby-reactivation`.

### Post-main PR Reconciliation Review — 2026-09-29T22:12:00Z

- **Context:** PR review do merge de `origin/main` `4cb8ad0b88526c54b365933767d5a4b0f3794746` na branch `wish/handoff-sla-first-human-response`; snapshot pré-stamp revisado em `git diff --cached MERGE_HEAD`, digest `9f1e3b10fac0576409573a6efa853a93459b4aa6f36b05bf47a805ed889df664`.
- **Initial lenses:** segurança retornou SHIP sem gap CRITICAL/HIGH e confirmou isolamento tenant/session, autorização fail-closed e projeção estreita do radar; performance identificou um HIGH porque `NOT VALID` e `VALIDATE` ainda dividiam a mesma transação Alembic.
- **Fix loop 1/2:** a migration 021 passou a confirmar os dois constraints `NOT VALID` antes de entrar em `autocommit_block()` para os `VALIDATE`; o teste offline exige `CHECK 1 < CHECK 2 < COMMIT < VALIDATE 1 < VALIDATE 2 < BEGIN`.
- **Re-review:** as lentes de performance e a revisão global independente retornaram SHIP sem gaps CRITICAL/HIGH/MEDIUM. O manifesto foi ampliado com os cinco arquivos de teste/baseline reconciliados; o único residual LOW é a ausência de benchmark de cardinalidade produtiva, sem regressão observada.
- **Validation:** gate backend final 3543 PASS / 22 skips com completion validator válido; PostgreSQL real 25/25 e smoke obrigatório 1/1; migration final 21/21; frontend 28 arquivos / 355 testes, TypeScript e build production; Black, isort, Flake8, mypy, Prettier e `git diff --check` PASS.
- **Verdict:** SHIP. O commit de merge deve registrar o slug `handoff-sla-first-human-response` para fechar a rastreabilidade.

## Files to Create/Modify

```text
alembic/versions/*_021_add_handoff_sla_lifecycle.py
src/models/handoff.py
src/models/clinic_config.py
src/handoff/sla.py
src/handoff/models.py
src/repositories/handoff.py
src/services/handoff_service.py
src/services/config_service.py
src/services/clinic_clone_service.py
src/core/pipeline.py
src/api/routes/handoff.py
src/api/routes/webhook.py
src/api/schemas/handoff.py
tests/api/test_handoff_sla_migration.py
tests/api/test_config.py
tests/api/test_events_e2e.py
tests/api/test_handoff_lifespan.py
tests/api/test_handoff_queue.py
tests/api/test_handoff_queue_export.py
tests/baselines/neurostack-core-extraction-known-failures.txt
tests/handoff/test_sla.py
tests/handoff/test_service.py
tests/integration/test_handoff_lifecycle_postgres.py
tests/integration/test_handoff_sla_postgres.py
tests/memory/test_event_store.py
tests/scripts/test_check_handoff_sla_postgres.py
scripts/check-handoff-sla-postgres.sh
frontend/artifacts/neurostack/src/pages/handoff-queue.tsx
frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
```
