# Wish: Métrica de usuários novos

| Field | Value |
|-------|-------|
| **Status** | APPROVED — plan review SHIP; ready for `/work` Group 0A |
| **Slug** | `new-users-metric` |
| **Date** | 2026-08-08 |
| **Author** | Codex + usuário |
| **Appetite** | large |
| **Branch** | `wish/new-users-metric` |
| **Repos touched** | `neurostack-api` (backend, frontend embutido e docs) |
| **Design** | [DESIGN.md](../../brainstorms/new-users-metric/DESIGN.md), SHIP digest `baf59da009d3f9b56aed2a016ab479d02b4c4f1cffd396cb69df87d1dbddc808` |

## Summary

Entregar aos painéis de `clinic_admin` e `builder` uma leitura comercial de aquisição: quantas identidades tiveram sua primeira interação registrada no período e qual percentual das interações identificadas veio de usuários novos versus antigos. A entrega usa endpoints administrativos JWT-only novos, preserva integralmente os contratos atuais, mede viabilidade antes do backend e mede o endpoint real antes de qualquer frontend ou release.

## Scope

### IN

- Classificar a identidade por `(tenant_id, Session.channel, btrim(Session.user_id))` e `first_persisted_at = MIN(Event.created_at)` de `message.in` no histórico persistido disponível.
- Expor `GET /v1/acquisition/clinic` para `clinic_admin` da própria clínica e builder proprietário, com período civil de até 90 dias no fuso efetivo da clínica.
- Expor `GET /v1/acquisition/builder` apenas para builder JWT válido, com janela de 168 horas, keyset assinado, até 100 clínicas por página e ownership revalidado.
- Implementar contagens de identidades novas, interações novas/antigas/identificadas/não identificadas, percentuais complementares e série diária da clínica.
- Mostrar KPI, composição e gráfico diário no dashboard da clínica; mostrar os sete agregados de 168 horas por clínica no painel builder.
- Executar Wave 0A antes de código de produto e Wave 0B sobre o backend mínimo antes de frontend, medindo SQL, serviço, endpoint, capacidade, recuperação e memória.
- Fornecer OpenAPI e referência administrativa JWT-only, estados/microcopy completos, evidência visual e runbook backend-first/frontend-first rollback.
- Executar somente em worktree/branch limpo e isolado, registrando a base imutável e mantendo alterações deste wish separadas de `api-provider-settings`.

### OUT

- Qualquer mudança em schema, campos, semântica temporal ou consumidores de `GET /v1/traces/stats` e `GET /v1/builder/clinics`.
- Acesso por API key, `super_admin`, `investor` ou roles diferentes de `clinic_admin`/`builder`.
- Timestamp de recebimento do transporte; a fonte continua o relógio UTC da aplicação após pipeline/LLM e antes do INSERT.
- Identidade cross-tenant/canal, case folding, alias, CRM, campanha, conversão, retenção, churn ou soma global entre clínicas.
- Migration, índice, tabela `first_seen`, cache, materialized view, backfill, job, fila, flag ou snapshot histórico de ownership.
- Suprimir small cells ou expor identidade bruta em API, log, span, screenshot ou evidência.
- Fazer deploy externo sem autoridade própria; este wish implementa, valida e prepara o rollout, mas não amplia autorização de release.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | “Novo” é por tenant, canal e `btrim(user_id)` | Mantém a leitura dentro da fronteira da clínica sem equivalências inventadas. |
| 2 | Fonte temporal é `Event.created_at` pós-processamento e pré-INSERT | Corresponde a primeira interação registrada, não a recebida. |
| 3 | KPI conta identidades; percentuais contam interações | Separa crescimento de público do volume da coorte. |
| 4 | Toda interação do período de uma identidade estreante no período pertence à coorte nova | Mantém classificação estável no período. |
| 5 | Não identificados ficam fora do denominador e aparecem em contagem própria | Não fabrica classificação e torna cobertura auditável. |
| 6 | Endpoints novos sob `/v1/acquisition` | Evita colisão com `/traces/{trace_id}` e preserva contratos legados. |
| 7 | JWT-only estrito, sem `super_admin` | Limita small cells a clinic_admin próprio e builder proprietário. |
| 8 | Intervalos semiabertos e datas civis clinic-local | Elimina dupla contagem sem alterar a semântica inclusiva existente de traces. |
| 9 | Builder usa keyset por `Tenant.id`, cursor HMAC e nenhum `total` | Limita custo e não promete snapshot de membership que o modelo não registra. |
| 10 | Membership builder é fracamente consistente e ownership é revalidado por página | Transferências concorrentes podem mudar a lista, mas nunca ampliar autorização da query atual. |
| 11 | Round-half-up a uma casa e returning complementar ao new arredondado | Garante `100,0` com denominador e `null/null` sem ele. |
| 12 | Controlador compartilhado candidato 4 total/2 builder, espera 100 ms | Limita aquisição combinada; Wave 0 pode reduzir, não aumentar sem review. |
| 13 | SQL timeout 2/5 s e deadline total 2,5/5,5 s | `statement_timeout` sozinho não cobre espera, pool e serialização. |
| 14 | 503 tipado nunca vira zeros | Indisponibilidade não é ausência de aquisição. |
| 15 | Wave 0A precede backend e Wave 0B precede frontend | Mede o maior risco antes de afundar custo e mede o endpoint real antes da UI. |
| 16 | Staging é read-only; fixture/ANALYZE somente em `_test` | Impede escrita acidental no ambiente-alvo. |
| 17 | Small cells exigem aceite verificável de owner produto/privacidade | O pedido funcional não substitui aprovação organizacional LGPD. |
| 18 | Backend-first e rollback frontend-first | Mantém a UI tolerante a rollout sem estado durável. |

## Normative API Contract

### Clinic request and response

`GET /v1/acquisition/clinic` aceita somente `start_date`, `end_date` e `tenant_id`. As datas são obrigatórias e estritas `YYYY-MM-DD`, inclusivas como datas civis, com `start_date <= end_date` e span máximo de 90 dias. `tenant_id` pode ser omitido/repetir o tenant próprio para clinic_admin; é obrigatório para builder e passa por ownership. Configuração timezone ausente/inválida usa o fallback observável existente `America/Sao_Paulo`.

| Field 200 | Type / default | Contract |
|---|---|---|
| `period_start` | UTC RFC 3339 `str` | Inclusivo, meia-noite civil inicial. |
| `period_end_exclusive` | UTC RFC 3339 `str` | Exclusivo, meia-noite após `end_date`. |
| `timezone` | IANA `str` | Fuso efetivamente usado. |
| `new_users_count` | `int = 0` | Identidades com first persisted no período. |
| `new_user_interactions_count` | `int = 0` | Interações identificadas da coorte nova. |
| `returning_user_interactions_count` | `int = 0` | Interações identificadas da coorte antiga. |
| `identified_user_interactions_count` | `int = 0` | Soma exata new + returning. |
| `unidentified_user_interactions_count` | `int = 0` | Interações sem identidade tenant-safe utilizável. |
| `new_user_interactions_pct` | `float \| null = null` | Round-half-up a uma casa sobre identified. |
| `returning_user_interactions_pct` | `float \| null = null` | `round_half_up(100.0 - new_pct, 1)`. |
| `daily` | `list[AcquisitionDay] = []` | Esparsa e ordenada; dias com qualquer interação. |

`AcquisitionDay` contém `date`, os mesmos cinco counts sem sufixo e os dois percentuais. A soma diária reproduz todos os totais; `new_users_count` diário soma o KPI. Percentuais são `null/null` quando identified é zero e, de outro modo, somam `100,0`.

### Builder request and response

`GET /v1/acquisition/builder` aceita somente `limit`, `cursor`, `as_of`. `limit` default 50, intervalo 1..100. Primeira página omite cursor/as_of; seguintes exigem ambos. `as_of` é UTC truncado a segundos, tolera no máximo +5 s, expira em 10 minutos e é igual a `period_end_exclusive`; `period_start = as_of - 168h`.

Cursor versionado/HMAC carrega builder, as_of e último tenant; usa chave derivada de `SECRET_KEY` com domain separator. A ordem é `Tenant.id ASC`. Entram clínicas ativas/inativas criadas até as_of e atualmente pertencentes ao builder. Não há `total`; erro intermediário descarta a montagem parcial e reinicia da primeira página.

O 200 contém `as_of`, `period_start`, `period_end_exclusive`, `limit`, `next_cursor: string | null` e `items`. Cada item contém `tenant_id`, `tenant_name`, `is_active` e exatamente os sete campos clinic com sufixo `_7d`, preservando defaults e invariantes. JWT builder sem claim válido ou sem registro `Builder.id` recebe 403, nunca lista vazia.

### Errors and compatibility

- O middleware legado continua responsável pelos 401 e pelo 403 de tenant inativo, incluindo mensagens e `documentation_url` atuais.
- O router usa `AcquisitionErrorResponse` fechado: `{"error": str, "message": str, "details": null}`; `details` tem tipo exclusivo `None`, é sempre incluído e nunca coexistem `request_id`/`documentation_url`.
- 403 router: `acquisition_role_forbidden`, `acquisition_tenant_forbidden`.
- 422: `acquisition_unknown_parameter`, `acquisition_invalid_date`, `acquisition_invalid_range`, `acquisition_period_too_large`, `acquisition_tenant_required`, `acquisition_invalid_limit`, `acquisition_invalid_cursor`, `acquisition_invalid_as_of`, `acquisition_as_of_expired`.
- 503: `acquisition_capacity_exceeded` com `Retry-After: 1`; `acquisition_query_timeout`/`acquisition_deadline_exceeded` com `Retry-After: 5`.
- 404 só significa rota ausente no rollout/rollback e nunca tenant inexistente, ownership inválido ou resposta vazia.

## Performance and Privacy Gates

- Wave 0A mede SQL server e serviço; Wave 0B mede endpoint autenticado total. Percentis nunca são derivados por subtração.
- Cada cenário tem três repetições, cinco warm-ups descartados e pelo menos 50 sucessos; nearest-rank p50/p95, primeira execução, min/max, dispersão, cache hit/read, spills e erros são registrados.
- O envelope extremo cobre identidades ativas, eventos no período, todo o histórico retido dessas identidades, skew e até 100 clínicas de maior custo combinado.
- Targets candidatos: clinic total p95 <= 200 ms, builder total p95 <= 500 ms; SQL ceilings 2/5 s; deadlines 2,5/5,5 s; overload 503 <= 150 ms; zero 503 no limite suportado.
- RSS extra por worker deve ficar abaixo de `min(256 MiB, 10% do limite por worker)`, sem spill, OOM ou crescimento residual.
- Staging exige role esperado, `transaction_read_only=on`, transação `READ ONLY`, nenhum privilégio INSERT/UPDATE/DELETE, `EXPLAIN` sem ANALYZE e zero fixture.
- `_test` exige URL dedicada sem fallback, `ENVIRONMENT=test`, confirmação literal, `current_database()` com sufixo `_test`, namespace próprio, cleanup em `finally` e zero resíduo.
- Evidência usa allowlist e não guarda DSN, host, senha, nome do role DB, bind, linha, tenant ID, user ID, mensagem ou exceção bruta.
- Echo SQL e captura de parâmetros em driver/APM/OTel bloqueiam profiler/benchmark. Sentinels verificam app logs, driver, SQLAlchemy, spans e erros encadeados.
- Small cells só avançam após aceite verificável que cite este DESIGN digest, o boundary de roles, risco de inferência por diferença, owner organizacional, data e referência de assinatura/aprovação.

## Simplicity Case

- **Simplest complete design:** profiler/protótipo 0A, um módulo acquisition profundo, dois endpoints JWT-only limitados, gate 0B e componentes nos painéis existentes.
- **Added machinery:** router separado preserva compatibilidade; cursor limita paginação; timeout/controller limita consultas históricas; Waves 0 resolvem cardinalidade desconhecida; HMAC impede adulteração de cursores sem estado durável.
- **Deferred until measured:** índice, materialização, cache, tabela `first_seen`, `received_at` ou snapshot de ownership só entram após falha documentada e nova design review.
- **Complexity removed:** nenhuma migration, backfill, invalidação, estado de cursor, flag, endpoint legado versionado, job, fila ou dependência frontend nova.

## Dependencies

**depends-on:** none
**blocks:** none

Precondições operacionais do Group 0A, todas fail-closed: worktree limpo/isolado; staging read-only autorizado; banco `_test` autorizado; configuração real de workers/réplicas/pool/memória; aceite de small cells por owner produto/privacidade. Ausência de qualquer uma bloqueia execução antes de código de produto, sem reclassificar como aprovação parcial.

## Success Criteria

- [ ] Base commit/tree e worktree isolado são registrados; o diff do wish não contém mudanças de `api-provider-settings`.
- [ ] Wave 0A produz perfil staging allowlisted e protótipo SQL/serviço no envelope extremo, ou bloqueia antes do backend sem fabricar evidência.
- [ ] Aceite small-cell verificável e vinculado ao DESIGN digest existe antes do backend.
- [ ] Identidade usa tenant + canal + `btrim(user_id)` e o join exige igualdade de tenant nos dois lados.
- [ ] `first_persisted_at` usa o relógio UTC da aplicação pós-LLM/pré-INSERT e testes cobrem limites/midnight/clock skew.
- [ ] Interação exatamente em start entra; exatamente em end_exclusive não entra.
- [ ] New + returning = identified; não identificados não mudam percentuais; percentuais são null/null ou somam 100,0.
- [ ] Daily é esparsa, ordenada e somável para todos os counts.
- [ ] `/v1/traces/stats` e `/v1/builder/clinics` permanecem byte-for-byte compatíveis em OpenAPI/golden responses e semanticamente inalterados.
- [ ] `/v1/acquisition/clinic` implementa request/200/error normativos, fuso/fallback, 90 dias e autorização clinic/builder.
- [ ] `/v1/acquisition/builder` implementa 168h, keyset/HMAC, expiração, consistência fraca documentada, ownership por página e no máximo 100.
- [ ] API key válida, super_admin, investor, builder inválido e tenant alheio recebem 403 seguro; middleware 401/403 legado permanece intacto.
- [ ] Timeout/saturação retorna 503 tipado com Retry-After e nunca zeros.
- [ ] Controlador final não excede 4 total/2 builder por processo e Waves 0 provam headroom com workload normal misto.
- [ ] Wave 0B prova p95 total clinic <= 200 ms e builder <= 500 ms, zero 503 no limite e overload 503 <= 150 ms.
- [ ] RSS fica abaixo do orçamento e não há spill/OOM/crescimento residual.
- [ ] Timeout SQL, deadline e cancelamento provam rollback, cancel, release em finally, timeout local removido e conexão/pool reutilizável.
- [ ] Evidência e telemetria passam sentinels sem credencial, identidade, bind, linha ou payload bruto.
- [ ] OpenAPI expõe segurança JWT-only, schemas fechados, erros e receita de paginação/reinício em referência administrativa separada.
- [ ] Clinic_admin vê KPI, composição e gráfico do período; builder vê os sete agregados por clínica e janela/as_of.
- [ ] Frontend não chama/renderiza acquisition para roles proibidas e não apresenta página parcial como completa.
- [ ] Loading, 403, 404, cada 503, vazio, somente não identificado, 0%, mista, dias esparsos e cursor expirado usam a microcopy normativa.
- [ ] Matriz visual reproduzível cobre mobile/desktop, claro/escuro, foco, contraste, leitor de tela e overflow.
- [ ] Rollout backend-first e rollback frontend-first são validados sem restauração de dados.
- [ ] Testes focados, regressões legadas, format/lint/typecheck e build frontend passam sem nova falha versus a base registrada.

## Execution Strategy

### Wave 0A (sequential hard gate)

| Group | Agent | Complexity | Model | Description |
|---|---|---|---|---|
| 0A | engineer + independent final-gate | 5 — stateful DB +2; subjective owner/evidence acceptance +2; environment-dependent measurement +1 | `engineer-complex` / high; `final-gate` / high | Preflight, profiler staging read-only, protótipo/benchmark `_test`, small-cell acceptance e go/no-go. |

### Wave 1 (after 0A SHIP)

| Group | Agent | Complexity | Model | Description |
|---|---|---|---|---|
| 1 | engineer | 3 — stateful timeout/cursor/controller +2; prior council rework +1 | `engineer-standard` / high | Módulo profundo, schemas, router, autorização e contratos backend, sem frontend. |

### Wave 0B (after Group 1; hard gate)

| Group | Agent | Complexity | Model | Description |
|---|---|---|---|---|
| 0B | engineer + independent final-gate | 6 — stateful recovery/capacity +2; subjective evidence +2; environment-dependent +1; CI/release gate +1 | `engineer-complex` / high; `final-gate` / high | Endpoint total, capacidade, memória, timeout/cancel recovery e go/no-go pré-frontend. |

### Wave 2 (after 0B SHIP)

| Group | Agent | Complexity | Model | Description |
|---|---|---|---|---|
| 2 | engineer + final-gate | 4 — frontend package +1; subjective visual acceptance +2; rollout/docs +1 | `engineer-complex` / high; `final-gate` / high | Dashboards clinic/builder, client/retry, docs, visual evidence e rollout validation. |

Os quatro grupos são estritamente sequenciais. 0A ou 0B FIX-FIRST/BLOCKED impede checkout dos grupos seguintes. Aumentar limites, adicionar índice/cache/materialização ou mudar semântica exige voltar ao DESIGN/council.

## Execution Groups

### Group 0A: Preflight, evidência e go/no-go

**Goal:** provar que a consulta derivada é segura e viável no envelope real antes de criar código de produto.

**Deliverables:**
1. Iniciar no worktree `wish/new-users-metric` limpo, registrar `git rev-parse HEAD`, tree e gates de base, e abortar se houver diff alheio ao wish.
2. Criar `scripts/new_users_metric_probe.py` com subcomandos `profile-staging` e `benchmark-test`, URLs/envs separadas sem fallback e schema allowlisted de evidência.
3. Em staging, validar role esperada/read-only/privileges, coletar distribuições agregadas, cobertura/retenção, deployment/pool/memória e `EXPLAIN (FORMAT JSON)` sem ANALYZE/fixture.
4. Em `_test`, criar fixture conservadora pelo namespace da execução, medir SQL/serviço clinic+builder com todo o histórico ativo, `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, RSS e cleanup zero.
5. Criar testes adversariais para guard de ambiente/banco, read-only fail-closed, echo/capture, allowlist/redaction, nearest-rank, memória, falha injetada e cleanup.
6. Validar manualmente o aceite small-cell verificável e persistir somente a referência não sensível no relatório go/no-go.
7. Persistir `.genie/evidence/new-users-metric/wave0a.json` com decisão SHIP/FIX-FIRST/BLOCKED. Qualquer falha encerra o wish antes do Group 1 e retorna a design review.

**Acceptance Criteria:**
- [ ] Staging não executa escrita/ANALYZE e evidence não contém role name, DSN, host, bind, row, tenant ou user ID.
- [ ] Benchmark recusa banco sem `_test`, variável errada, confirmação ausente, echo/capture e deixa zero fixture após sucesso/falha.
- [ ] Três repetições/5 warm-ups/50 sucessos e os dois relógios seguem exatamente o protocolo.
- [ ] Envelope 90 dias/até 100 clínicas e RSS passam targets candidatos com headroom, ou o resultado é não-SHIP sem produto.
- [ ] Aceite small-cell é verificável, pertence a owner organizacional e cita o digest do DESIGN.

**Validation:**
```bash
poetry run pytest tests/scripts/test_new_users_metric_probe.py -v
poetry run black --check scripts/new_users_metric_probe.py tests/scripts/test_new_users_metric_probe.py
poetry run isort --check-only scripts/new_users_metric_probe.py tests/scripts/test_new_users_metric_probe.py
poetry run flake8 scripts/new_users_metric_probe.py tests/scripts/test_new_users_metric_probe.py
poetry run mypy scripts/new_users_metric_probe.py --ignore-missing-imports
test -n "${NEUROSTACK_ACQUISITION_PROFILE_DATABASE_URL:-}"
test -n "${NEUROSTACK_ACQUISITION_PROFILE_EXPECTED_ROLE:-}"
poetry run python scripts/new_users_metric_probe.py profile-staging --output .genie/evidence/new-users-metric/staging-profile.json
test -n "${NEUROSTACK_ACQUISITION_BENCHMARK_DATABASE_URL:-}"
poetry run python scripts/new_users_metric_probe.py benchmark-test --confirm NEW_USERS_METRIC_TEST_ONLY --output .genie/evidence/new-users-metric/wave0a.json
git diff --check
```

**depends-on:** none

---

### Group 1: Backend administrativo de aquisição

**Goal:** implementar o contrato backend mínimo aprovado sem alterar endpoints legados ou adicionar frontend.

**Deliverables:**
1. Criar `src/services/user_acquisition.py` como fronteira profunda para query tenant-safe, normalização, agregação, cursor HMAC, controlador compartilhado, deadlines, rollback e exceções tipadas.
2. Criar `src/api/schemas/acquisition.py` com requests/responses fechados, `AcquisitionDay`, item builder e `AcquisitionErrorResponse` exatos.
3. Criar `src/api/routes/acquisition.py`, incluir o router em `src/api/main.py` e implementar dependência JWT estrita para clinic_admin/builder, validando registro Builder e ownership sem 404/leak.
4. Validar query params manualmente para normalizar 422; aplicar fuso/fallback, 90 dias, janela 168h, keyset/as_of e invariantes de arredondamento.
5. Aplicar `SET LOCAL statement_timeout`, deadline total, espera 100 ms e controller 4 total/2 builder, com cleanup em `finally` e respostas 503 normativas.
6. Desabilitar/recusar captura de bind/echo no path e instrumentar somente timings/counts/outcomes allowlisted.
7. Criar testes unitários/API/contrato para identidade, coorte, temporalidade, DST, cross-tenant, roles, ownership, params, cursor adulterado/expirado, erros e OpenAPI.
8. Adicionar golden regressions em `test_acquisition_contracts.py` e executar as suítes legadas reais que provem `/v1/traces/stats`, `/v1/builder/clinics` e middleware auth byte-for-byte inalterados.

**Acceptance Criteria:**
- [ ] Todos os schemas, defaults, invariantes, envelopes e headers correspondem ao contrato normativo.
- [ ] Uma query clinic e uma query em lote por página builder evitam N+1 e sempre filtram tenants autorizados em cada CTE/subquery.
- [ ] API key válida e roles/ownership inválidos falham sem executar a query de aquisição.
- [ ] Cursor HMAC não revela payload, não cruza builders/as_of e key rotation resulta em 422/restart seguro.
- [ ] Endpoints e OpenAPI legados não mudam.

**Validation:**
```bash
poetry run pytest tests/services/test_user_acquisition.py tests/api/test_acquisition.py tests/api/test_acquisition_contracts.py tests/api/test_traces_outside_hours.py tests/services/test_trace_service.py tests/api/test_builder.py tests/api/test_middleware_auth.py -v
poetry run black --check src/services/user_acquisition.py src/api/schemas/acquisition.py src/api/routes/acquisition.py src/api/main.py tests/services/test_user_acquisition.py tests/api/test_acquisition.py tests/api/test_acquisition_contracts.py
poetry run isort --check-only src/services/user_acquisition.py src/api/schemas/acquisition.py src/api/routes/acquisition.py src/api/main.py tests/services/test_user_acquisition.py tests/api/test_acquisition.py tests/api/test_acquisition_contracts.py
poetry run flake8 src/services/user_acquisition.py src/api/schemas/acquisition.py src/api/routes/acquisition.py src/api/main.py tests/services/test_user_acquisition.py tests/api/test_acquisition.py tests/api/test_acquisition_contracts.py
poetry run mypy src/services/user_acquisition.py src/api/schemas/acquisition.py src/api/routes/acquisition.py --ignore-missing-imports
git diff --check
```

**depends-on:** Group 0A SHIP

---

### Group 0B: Endpoint, capacidade e recuperação

**Goal:** provar o backend real sob carga e falha antes de qualquer consumo frontend.

**Deliverables:**
1. Assumir ownership sequencial de `scripts/new_users_metric_probe.py` após o Group 0A e estendê-lo, sem reverter seus guards/testes, com `benchmark-endpoints` para medir entrada autenticada até resposta/serialização e separar 2xx de 503.
2. Rodar clinic+builder mistos com workload normal no deployment efetivo, três repetições/5 warm-ups/50 sucessos e limite suportado documentado.
3. Testar overload deliberado, espera do controller, zero 503 no limite e 503 em até 150 ms acima dele.
4. Criar teste PostgreSQL real para timeout SQL, deadline e cancelamento, provando rollback, cancel, release dos permits, timeout local removido e consulta saudável subsequente no mesmo pool.
5. Medir RSS, spills e retorno após cleanup; testar pool/workers/réplicas/headroom e registrar ceilings finais, que só podem ser menores ou iguais aos candidatos.
6. Rodar sentinels sobre app/driver/SQLAlchemy/APM/OTel/exceções e persistir `.genie/evidence/new-users-metric/wave0b.json` allowlisted.
7. Retornar SHIP/FIX-FIRST/BLOCKED; qualquer não-SHIP impede Group 2/release e volta a design review.

**Acceptance Criteria:**
- [ ] Clinic total p95 <= 200 ms e builder total p95 <= 500 ms por nearest-rank, sem misturar 503.
- [ ] Zero 503 no limite suportado; overload retorna capacity 503 em <=150 ms.
- [ ] SQL timeout 2/5 s, deadline 2,5/5,5 s e slot wait 100 ms são provados no endpoint real.
- [ ] Todas as falhas recuperam transação, permits e pool; consulta subsequente passa.
- [ ] RSS/headroom/redaction passam e a evidência contém somente allowlist.

**Validation:**
```bash
poetry run pytest tests/integration/test_acquisition_recovery.py tests/load/test_new_users_metric_endpoints.py -v
poetry run pytest tests/scripts/test_new_users_metric_probe.py -v
poetry run black --check scripts/new_users_metric_probe.py tests/scripts/test_new_users_metric_probe.py tests/load/test_new_users_metric_endpoints.py tests/integration/test_acquisition_recovery.py
poetry run isort --check-only scripts/new_users_metric_probe.py tests/scripts/test_new_users_metric_probe.py tests/load/test_new_users_metric_endpoints.py tests/integration/test_acquisition_recovery.py
poetry run flake8 scripts/new_users_metric_probe.py tests/scripts/test_new_users_metric_probe.py tests/load/test_new_users_metric_endpoints.py tests/integration/test_acquisition_recovery.py
poetry run mypy scripts/new_users_metric_probe.py --ignore-missing-imports
test -n "${NEUROSTACK_ACQUISITION_BENCHMARK_DATABASE_URL:-}"
test -n "${NEUROSTACK_ACQUISITION_BASE_URL:-}"
poetry run python scripts/new_users_metric_probe.py benchmark-endpoints --confirm NEW_USERS_METRIC_TEST_ONLY --output .genie/evidence/new-users-metric/wave0b.json
poetry run pytest tests/api/test_acquisition.py tests/api/test_acquisition_contracts.py -v
git diff --check
```

**depends-on:** Group 1 SHIP

---

### Group 2: Painéis, documentação e rollout

**Goal:** tornar a métrica visível e compreensível para clinic_admin/builder somente após os gates backend.

**Deliverables:**
1. Criar `frontend/artifacts/neurostack/src/lib/acquisition.ts` com tipos, parsing seguro, fetch clinic, paginação builder sequencial, cursor/as_of, Retry-After+jitter/duas tentativas e descarte de parcial.
2. Atualizar `dashboard.tsx` com card “Novos usuários registrados”, composição, gráfico Recharts diário 100% empilhado, tooltip counts, nota não identificados, fuso e microcopy normativa.
3. Atualizar `builder.tsx` com os sete agregados por clínica, ativa/inativa, janela 168h/as_of, cursor sequencial, último snapshot completo e estados normativos.
4. Implementar role-gating antes do fetch/render para clinic_admin/builder e provar ausência de request para super_admin/investor/outras roles.
5. Atualizar `locales/pt.ts` e `locales/en.ts` com rótulos/microcopy equivalentes, acessíveis e sem confundir pessoas com interações.
6. Criar `docs/admin-acquisition-api.md` com OpenAPI JWT-only, exemplos, erros, paginação/restart, consistência fraca e proibição de API key; não inserir a rota no guia de integração API key.
7. Criar `docs/new-users-metric-visual-qa.md` com fixtures sintéticas não-PII, passos reproduzíveis e matriz de screenshots mobile/desktop, claro/escuro e todos os estados.
8. Validar runbook backend-first, janela monitorada, frontend-first rollback e endpoint removal; não efetuar deploy sem autoridade externa.

**Acceptance Criteria:**
- [ ] Clinic e builder mostram exatamente os campos/períodos corretos sem somar clínicas nem apresentar parcial como total.
- [ ] UI não chama acquisition para roles proibidas e distingue loading, vazio, 0%, only-unidentified, 403, 404 e cada 503.
- [ ] Retry segue headers/jitter/limite e expiração reinicia do zero conservando no máximo snapshot completo anterior.
- [ ] Visual matrix não tem overflow, contraste/foco/labels nominais falhos e não usa PII real.
- [ ] Typecheck/build e regressões backend passam sem dependência frontend nova.

**Validation:**
```bash
cd frontend
pnpm run typecheck
pnpm --filter @workspace/neurostack run build
cd ..
poetry run pytest tests/api/test_acquisition.py tests/api/test_acquisition_contracts.py tests/api/test_traces_outside_hours.py tests/services/test_trace_service.py tests/api/test_builder.py -v
git diff --check
```

**depends-on:** Group 0B SHIP

---

## Aggregate Validation

```bash
poetry run pytest tests/ -v
make format-check
make lint
make typecheck
cd frontend && pnpm run typecheck && pnpm --filter @workspace/neurostack run build
cd ..
git diff --check
```

Falhas repo-wide só podem ser classificadas como baseline quando o mesmo comando falha no commit/tree registrado pelo Group 0A e nenhum caminho tocado pelo wish introduz falha nova.

## QA Criteria

- [ ] Primeira interação no período incrementa uma única identidade nova; mensagens seguintes elevam apenas interações da coorte nova.
- [ ] O mesmo identificador em tenant/canal diferente não cruza coorte; whitespace aparado e case preservado.
- [ ] Usuário com histórico anterior é returning por todo o período; histórico ausente é explicitado na microcopy.
- [ ] Datas civis, dia final, meia-noite, DST e fallback `America/Sao_Paulo` correspondem ao response.
- [ ] Somente não identificados produz counts reais e percentuais null; 0% real permanece 0,0%.
- [ ] Builder vê somente clínicas atuais próprias, pagina sequencialmente e reinicia após cursor/as_of expirado sem parcial.
- [ ] API key, super_admin, investor, builder inválido e tenant alheio nunca obtêm agregados.
- [ ] Timeout/capacity mostra indisponibilidade e não zera a métrica.
- [ ] Matriz visual completa passa em mobile/desktop e claro/escuro com teclado/leitor de tela nominal.
- [ ] Responses, logs, traces, screenshots e evidências não contêm identidade/credencial/binds.
- [ ] Endpoints legados produzem os mesmos contratos e números de fronteira anteriores.

## Assumptions / Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Histórico disponível começa após o primeiro contato real | High | Rotular primeira interação registrada no histórico disponível; perfil mede retenção. |
| Persistência pós-LLM desloca aquisição em relação ao recebimento | High | Semântica/microcopy/testes explícitos; `received_at` fica OUT. |
| Query degrada por histórico/skew/cardinalidade | High | 0A/0B bloqueantes, limites públicos e re-review antes de estrutura nova. |
| Small cell permite inferência por diferença | High | JWT/ownership, sem API key/raw IDs e aceite owner produto/privacidade. |
| Timeout/cancel deixa conexão ou permit contaminado | High | Finally/rollback/cancel e integração PostgreSQL com consulta subsequente. |
| Evidência/log vaza credencial ou identidade | High | Allowlist, echo/capture fail-closed e sentinels multissink. |
| Ownership muda durante paginação | Medium | Keyset, revalidação por página, sem total, consistência fraca documentada e partial discard. |
| Frontend compartilhado chama endpoint para role proibida | High | Gate antes do fetch/render e prova de zero request. |
| Worktree atual mistura wishes | High | Checkout isolado/clean base obrigatório no 0A. |
| Runner visual automatizado não existe | Medium | Harness/fixtures documentadas e screenshots reproduzíveis em staging, revisadas por final-gate. |
| Base repo-wide já possui falhas | High | Comparação comando-a-comando com base imutável e gates focados dos arquivos tocados. |

## Review Results

### Plan Review — 2026-08-09

- **Verdict:** SHIP
- **Reviewer:** `/root/new_users_final_plan_gate`
- **Reviewed at:** `2026-08-09T04:08:02Z`
- **Reviewed WISH SHA-256:** `4bf4d97c1981f4d32392bf18a9f132817c7a497d031674a1bdbc827346c9e7a2`
- **Design authority:** SHIP digest `baf59da009d3f9b56aed2a016ab479d02b4c4f1cffd396cb69df87d1dbddc808`
- **Prior pass:** FIX-FIRST em `b7663468c2ae18ffc6523093eb3a806663f997e392d6aace6d28010fa907ef4f`; todos os gaps foram corrigidos antes desta revisão fresca.
- **Verified fixes:** `details` null-only; testes/quality do probe repetidos em 0B; ownership orchestrator/probe explícito; regressões apontam para arquivos reais.
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou bloqueante.

**Status:** `APPROVED`. O hash revisado corresponde ao conteúdo anterior somente a este bloco e à transição mecânica DRAFT → APPROVED; qualquer outra edição material exige nova plan review.

## Files to Create/Modify

O DESIGN selado não pertence a nenhum grupo de implementação. Este WISH é orchestrator-only para status/evidência de review; engenheiros não o editam. `scripts/new_users_metric_probe.py` e seu teste pertencem ao Group 0A e passam sequencialmente ao Group 0B, que deve preservar os guards anteriores.

```text
# Orchestrator-only
.genie/wishes/new-users-metric/WISH.md

# Group 0A, depois Group 0B
.genie/evidence/new-users-metric/staging-profile.json
.genie/evidence/new-users-metric/wave0a.json
.genie/evidence/new-users-metric/wave0b.json
scripts/new_users_metric_probe.py
tests/scripts/test_new_users_metric_probe.py

# Group 1
src/api/main.py
src/api/middleware/auth.py
src/api/routes/acquisition.py
src/api/schemas/acquisition.py
src/services/user_acquisition.py
tests/api/test_acquisition.py
tests/api/test_acquisition_contracts.py
tests/api/test_builder.py
tests/api/test_middleware_auth.py
tests/api/test_traces_outside_hours.py
tests/services/test_trace_service.py
tests/services/test_user_acquisition.py

# Group 0B
tests/integration/test_acquisition_recovery.py
tests/load/test_new_users_metric_endpoints.py

# Group 2
frontend/artifacts/neurostack/src/lib/acquisition.ts
frontend/artifacts/neurostack/src/pages/dashboard.tsx
frontend/artifacts/neurostack/src/pages/builder.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
docs/admin-acquisition-api.md
docs/new-users-metric-visual-qa.md
```
