# Wish: Núcleo de origem confiável

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `interaction-origin-training-safety` |
| **Date** | 2026-08-11 |
| **Author** | Codex |
| **Appetite** | large |
| **Branch** | `wish/interaction-origin-training-safety` |
| **Repos touched** | neurostack-api |
| **Design** | [DESIGN.md](../../brainstorms/interaction-origin-training-safety/DESIGN.md) |
| **Governing design SHA** | `60a66c7bb0129eccb24c132e4479bfda989fb9843325d7c4d89a9bccb2d964a8` (SHIP) |
| **Planning base SHA** | `7ab570ff0d10ed459e9e62bec5c8f32f88e94fff` |
| **Execution base** | `36d5d55f22fbb7a673fd86fb3aed7dd48a181a98` (`INTERACTION_ORIGIN_BASE_SHA`) |

## Summary

Hoje o backend não consegue provar se um evento veio de produção, simulador ou origem desconhecida sem confiar em campos controlados pelo cliente. Este wish entrega a fundação fail-closed: origem imutável no PostgreSQL, tenant e sessão resolvidos antes do pipeline, contexto único por interação, matriz explícita de ingressos e round-trip completo no Event Store. Ele não habilita treinamento nem muda analytics; torna os wishes dependentes capazes de aplicar essas políticas sobre um dado confiável.

## Scope

### IN

- Criar `events.interaction_origin` com valores `production`, `simulator` e `unknown`, default/backfill conservador, índice composto e trigger de imutabilidade.
- Definir `InteractionOrigin` e `ResolvedInteractionContext` como contratos internos únicos; `interaction_event_id` é opcional e existe somente para `message.in`.
- Unificar a resolução de tenant de `POST /v1/sessions` e `POST /v1/events`, validar sessão por `(tenant_id, id)` e negar roles JWT não listadas antes de qualquer efeito.
- Rejeitar `interaction_origin` controlado pelo cliente no body, payload ou metadata com `422 interaction_origin_server_managed`.
- Classificar API key tenant-scoped como `production`, JWT autorizado do painel como `simulator`, Z-API/Meta autenticados como `production` e Evolution/Uazapi/callers sem prova como `unknown`.
- Propagar o contexto pelas duas fases do `EventService`, pelo bundle transacional e pelos dois caminhos de handoff.
- Preservar `EventResponse.event_id` como ID de `srm.decision` e adicionar `interaction_event_id: UUID | null` como referência persistida ao `message.in`.
- Atualizar todos os caminhos de escrita e leitura do Event Store, centralizar row→`EventData` e expor origem nas representações básicas de auditoria.
- Atualizar contratos OpenAPI/documentação e adicionar testes unitários, API, integração e smoke PostgreSQL real.

### OUT

- Rating, vínculo de avaliação ao `message.in`, remoção de `clinic_tenant_id`, aprovação para treinamento e mudanças na UI de auditoria; pertencem a `rating-source-training-gate`.
- Qualquer leitura ou alteração de `builder_slm_training` neste wish. O gate provisório será implementado somente por `rating-source-training-gate` e exigirá `production` + flag exatamente `true` + aprovação humana por super-admin.
- Consentimento individual por paciente/interação até existir requisito legal ou de produto explícito; o ledger versionado substituirá o gate provisório.
- Filtros/defaults por origem em traces, dashboards builder/admin/health ou frontend; pertencem a `production-analytics-origin-filter`.
- RLS, papéis de aplicação/migração, `SET LOCAL app.tenant_id` e integridade composta evento/sessão/tenant; pertencem a `postgres-tenant-isolation`.
- Autenticação nova de Evolution/Uazapi, ledgers, anonimização, snapshots, linhagem de modelos, exportação de dataset ou fine-tuning.
- Reclassificação de legado `unknown`, mudança de billing, retries, filas ou estado de idempotência.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | `events.interaction_origin` é a autoridade persistida | O evento é a unidade auditável e pode existir fora de uma sessão conversacional. |
| 2 | Origem é string fechada por `CHECK`, imutável por trigger | Mantém evolução simples e impede reclassificação administrativa ou acidental. |
| 3 | Todo legado começa `unknown` | `channel`, metadata e formato do telefone não são provas históricas confiáveis. |
| 4 | Um único `ResolvedInteractionContext` atravessa o fluxo | Evita tenant, sessão, origem e ID divergirem entre fases e escritores. |
| 5 | API key validada é `production`; JWT do painel é `simulator` | É a decisão 2A e independe de `channel`; um chat web real deverá usar credencial/adaptador de produção. |
| 6 | JWT usa allowlist: clinic_admin, builder e super_admin | `investor` ou role desconhecida não possui política operacional e recebe `403 caller_role_forbidden`. |
| 7 | Z-API e Meta autenticados são `production`; Evolution/Uazapi são `unknown` | Somente os dois primeiros provam autenticidade do remetente no código atual. |
| 8 | `message.in` é a raiz; outros tipos mantêm `interaction_event_id=null` | Preserva `session.start` e demais contratos sem fabricar uma referência de mensagem. |
| 9 | `event_id` legado não muda de significado | Evita quebra silenciosa de clientes `/v1`; o novo campo tem semântica inequívoca. |
| 10 | Falha best-effort do Event Store devolve ID nulo | Mantém disponibilidade atual sem expor UUID órfão a consumidores futuros. |
| 11 | `builder_slm_training` fica apenas no wish futuro de gate | É a decisão 1A: gate provisório conservador agora, substituído depois por ledger versionado; consentimento individual permanece fora sem requisito explícito. |
| 12 | Rating, analytics e isolamento PostgreSQL são wishes separados | Cada fronteira possui rollout, validação e rollback próprios; este wish fornece somente a proveniência comum. |
| 13 | Gates globais ou transitivos já vermelhos usam comparação fail-closed contra o execution base | É a decisão humana A de 2026-08-11: o comando literal continua obrigatório, mas dívida externa preexistente não força expansão de escopo; qualquer diagnóstico, testcase falho ou ausência nova continua bloqueando. |

## Simplicity Case

- **Simplest complete design:** uma coluna imutável e um contexto interno construído uma vez após autorização, usado por todos os escritores e leitores do evento.
- **Added machinery:** `CHECK` fecha o domínio; o índice atende consultas futuras por tenant/origem/tipo/período; o trigger paga o requisito de imutabilidade; o contexto único elimina quatro autoridades paralelas; o mapper único evita que SQL bruto perca a origem; `interaction_event_id` resolve a ambiguidade real entre `message.in` e `srm.decision`.
- **Deferred until measured:** propósito por API key quando houver uso misto/sandbox; autenticação específica de Evolution/Uazapi quando esses providers oferecerem prova verificável; particionamento ou índices adicionais somente após planos de consulta/medição.
- **Complexity removed:** nenhuma fila, retry, idempotency state, reclassificação histórica, configuração de origem por clínica, filtro analítico, gate de aprovação, ledger ou consumidor de treinamento.

## Shared Baseline-Aware Regression Gate

O usuário autorizou a decisão A em 2026-08-11T19:06:28-03:00. Esta política se aplica somente aos comandos globais ou transitivos que já falham no execution base `36d5d55f22fbb7a673fd86fb3aed7dd48a181a98`, sempre com o mesmo `poetry.lock` SHA-256 `ee7555da0c75405a4f8c3579f9314f4629c1200287c88c4ddd995feaf6e4c6cf`. Gates focados de comportamento, formato/lint dos paths alterados, smoke PostgreSQL, frontend typecheck/test/build e `git diff --check` permanecem absolutos.

Para cada comando mypy group-scoped:

1. Executar e registrar o comando literal aprovado no candidato.
2. Executar o mesmo comando no execution base sobre a interseção dos roots que já existiam, no mesmo ambiente. Preservar multiplicidade em `Counter` keyed por path relativo, linha, coluna opcional, código e mensagem; nenhuma deduplicação por set é permitida.
3. Falhar se qualquer diagnóstico do candidato nomear um source path cumulativamente owned pelo grupo atual ou por grupo concluído, ou se `Counter(candidate_external) - Counter(baseline_external)` não for vazio. Como paths externos permanecem fora do diff autorizado, sua posição é estável; qualquer ocorrência adicional bloqueia.
4. Exigir também exit zero do comando candidato com `--follow-imports=silent` sobre a lista completa e exata de roots source owned pelo grupo, inclusive novos; roots omitidos, `ignore`, suppression ou relaxamento de configuração bloqueiam.

Para o gate agregado do Group 5, `make quality` ainda é executado e seu resultado absoluto é registrado. Como o execution base para no Black com 150 arquivos e sua suíte tem dívida conhecida, cada recipe é também executado separadamente em base e candidato, no mesmo lock/ambiente. Comparações de Black/isort/flake8/mypy preservam multiplicidade em `Counter`, e o gate falha se houver finding candidato adicional, se qualquer path Python cumulativamente alterado falhar seu gate focado, ou se surgir diferença não explicada de coleta/erro interno.

Para pytest, o baseline imutável é `/tmp/interaction-origin-base-36d5d55-junit.xml`, SHA-256 `9048bae9251297e675ac9b47024264131ef258b5d6400db2bab83df3cb7c86a7`: 1.165 IDs únicos, 198 falhos e um skip. A invocação candidata obrigatória é:

```bash
: "${INTERACTION_ORIGIN_CURRENT_JUNIT:?set and record a fresh candidate JUnit path}"
test ! -e "$INTERACTION_ORIGIN_CURRENT_JUNIT"
test ! -L "$INTERACTION_ORIGIN_CURRENT_JUNIT"
test "$(sha256sum poetry.lock | cut -d' ' -f1)" = "ee7555da0c75405a4f8c3579f9314f4629c1200287c88c4ddd995feaf6e4c6cf"
test "$(sha256sum /tmp/interaction-origin-base-36d5d55-junit.xml | cut -d' ' -f1)" = "9048bae9251297e675ac9b47024264131ef258b5d6400db2bab83df3cb7c86a7"
if poetry run pytest tests/ -v --junitxml="$INTERACTION_ORIGIN_CURRENT_JUNIT"; then
  INTERACTION_ORIGIN_PYTEST_STATUS=0
else
  INTERACTION_ORIGIN_PYTEST_STATUS=$?
fi
test "$INTERACTION_ORIGIN_PYTEST_STATUS" -le 1
test -s "$INTERACTION_ORIGIN_CURRENT_JUNIT"
sha256sum "$INTERACTION_ORIGIN_CURRENT_JUNIT"
```

O path candidato resolvido, status e digest são registrados na evidência. O comparador rejeita JUnit ausente/vazio, IDs vazios/duplicados, qualquer ID baseline ausente, qualquer failing/error ID candidato fora do conjunto baseline e qualquer `candidate_skipped - baseline_skipped`; somente o skip já presente no baseline pode continuar skipped. Todo ID novo deve ter status pass — failure, error, skip ou xfail bloqueiam. Se o artefato baseline deixar de existir, ele só pode ser regenerado do execution base exato e seu novo digest exige review independente antes do uso.

No Group 1, a evidência que motivou a decisão é: comando literal candidato com 176 diagnósticos transitivos em 46 arquivos não owned, contra 182 no base para os dois roots preexistentes; zero diagnóstico nos três roots owned e gate `--follow-imports=silent` verde. Essa redução não é crédito de qualidade nem autoriza tocar os arquivos externos; comprova apenas ausência de regressão do grupo.

## Dependencies

**depends-on:** billing-usage-metering
**blocks:** rating-source-training-gate, production-analytics-origin-filter, dataset-snapshot-anonymization

`dataset-snapshot-anonymization` também deverá depender de `postgres-tenant-isolation` e `training-consent-review-ledger`; este wish sozinho não autoriza materialização ou treinamento.

**Dependência confirmada — decisão 3A:** o usuário definiu que a revisão `alembic/versions/20260810_000001_016_add_knowledge_document_size.py` será incorporada primeiro pelo workstream `billing-usage-metering`, rastreado pelo card `t_msontnyf011833ab`. Esse workstream deve entregar um commit identificado que contenha a `016`, mantenha exatamente um head Alembic e registre o SHA no handoff. O Group 1 está bloqueado no board até essa entrega; não pode editar, stagear ou incorporar a `016` por conta própria. Sua nova migração deve declarar exatamente `down_revision = "016"` e executar sobre worktree cortado do commit entregue, exportado como `INTERACTION_ORIGIN_BASE_SHA`.

## Success Criteria

- [ ] A migração cria `events.interaction_origin NOT NULL DEFAULT 'unknown'`, `CHECK` dos três valores, índice `(tenant_id, interaction_origin, event_type, created_at DESC)` e trigger que rejeita alteração da origem.
- [ ] `billing-usage-metering` entrega um commit identificado cujo blob da `016` é registrado, rastreado e forma o head único; o Group 1 parte exatamente desse commit, não altera o blob e a nova migração declara `down_revision = "016"`.
- [ ] Todo evento legado permanece `unknown`; não existe promoção por `channel`, metadata, telefone ou outra heurística.
- [ ] O bootstrap PostgreSQL e o modelo ORM correspondem à migração, e update de campo não relacionado continua permitido.
- [ ] `append`, `append_batch`, `get_event`, `query_by_session` e `query_by_tenant` preservam os três valores; uma única função central reconstrói `EventData` de rows.
- [ ] `POST /v1/sessions` e `POST /v1/events` usam o mesmo resolvedor de tenant; API key/clinic_admin não podem sobrescrever tenant, builder exige ownership e super-admin pode selecionar a clínica.
- [ ] Sessão ausente ou de outro tenant retorna `404 session_not_found` antes de mutar estado, chamar LLM ou gravar evento; override indevido retorna `403`.
- [ ] JWT `investor` e qualquer role desconhecida retornam `403 caller_role_forbidden` antes de buscar/criar sessão ou produzir efeitos.
- [ ] `interaction_origin` enviado em body, payload ou metadata retorna `422 interaction_origin_server_managed` antes do pipeline.
- [ ] API key válida produz `production`; todo JWT operacional autorizado produz `simulator`, independentemente de `channel`.
- [ ] Z-API com capability válida e Meta com HMAC válida produzem `production`; requests inválidos continuam rejeitados sem efeitos.
- [ ] Evolution, Uazapi e callers internos sem prova explícita produzem `unknown`; testes por adapter impedem promoção acidental.
- [ ] O mesmo contexto atravessa `run_pipeline`, `finish_response`, todos os eventos do bundle e handoff, preservando tenant, sessão e origem.
- [ ] `EventResponse.event_id` continua identificando `srm.decision`; `interaction_event_id`, quando não nulo, identifica exatamente o `message.in` persistido do mesmo tenant/sessão.
- [ ] `session.start` e outros tipos não `message.in` preservam seu tipo/origem e retornam `interaction_event_id=null`.
- [ ] Falha do batch não devolve UUID órfão: o contrato best-effort permanece e `interaction_event_id=null`.
- [ ] Eventos sistêmicos sem interação recebem `unknown` explicitamente; nenhum writer infere origem de `channel` ou metadata.
- [ ] OpenAPI e docs distinguem transporte de proveniência e documentam matriz de ingressos, erros, dois IDs e nulabilidade.
- [ ] Smoke em PostgreSQL real prova upgrade/backfill, `CHECK`, índice, trigger, update não relacionado e round-trip do Event Store.
- [ ] Busca no diff confirma zero alteração funcional em rating, analytics, billing, dataset, fine-tuning e `builder_slm_training`.

## Execution Strategy

São seis incrementos executáveis: Groups 1, 2 e 3 são sequenciais; Groups 4A e 4B partem em paralelo depois do Group 3; Group 5 agrega ambos. O ownership abaixo evita caller antecipado: Group 2 entrega apenas o resolvedor compartilhado e `/sessions`; Group 3 entrega `/events`, schemas/responses, integração do caller, `EventService`, pipeline, bundle e compatibilidade de handoff na mesma unidade.

| Wave | Group | Agent | Complexity | Model | Description | Depends on |
|------|-------|-------|------------|-------|-------------|------------|
| 1 | 1 | engineer | 2 — schema/Event Store stateful (+2) | engineer-standard / high | Contexto frozen, migração, bootstrap, ORM, Event Store e smoke destrutivo seguro. | `billing-usage-metering` SHIP + card `t_msontnyf011833ab` done |
| 2 | 2 | engineer | 3 — autorização/sessão stateful (+2), rework formal (+1) | engineer-standard / high | Resolver compartilhado, `/sessions`, schema e response de sessão. | Group 1 |
| 3 | 3 | engineer | 4 — roteamento entre fases (+2), emissão stateful (+2) | engineer-complex / high | `/events`, segurança de leitura, EventService, pipeline, bundle e compatibilidade de handoff. | Group 2 |
| 4 | 4A | engineer | 4 — roteamento por adapters (+2), escrita de eventos stateful (+2) | engineer-complex / high | Matriz de webhooks. | Group 3 |
| 4 | 4B | engineer | 2 — dois writers de handoff stateful (+2) | engineer-standard / high | Handoff síncrono e tardio nos dois módulos duplicados. | Group 3 |
| 5 | 5 | engineer | 1 — documentação e gate de release (+1) | engineer-trivial / low | Contrato, docs e gate agregado. | Groups 4A e 4B |

O SHA atual é somente a base de planejamento. A auditoria de execução começa no commit produzido por `billing-usage-metering`, registrado em `INTERACTION_ORIGIN_BASE_SHA`, para não atribuir as mudanças legítimas da dependência a este wish. Cada executor deve preservar alterações preexistentes e parar se um arquivo de seu grupo tiver conflito não reconciliável.

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

### Group 1: Schema e Event Store

**Goal:** tornar a origem uma propriedade persistida, imutável e preservada em todas as operações do Event Store.

**Deliverables:**
1. Criar `InteractionOrigin` e `ResolvedInteractionContext` frozen, com `interaction_event_id: UUID | null`, em módulo de core sem dependência de rotas; o contexto contém somente IDs resolvidos e origem, nunca credenciais, API keys, JWT, headers ou raw auth.
2. Após o card `t_msontnyf011833ab` concluir, cortar o worktree do commit registrado em `INTERACTION_ORIGIN_BASE_SHA`, provar que a `016` está rastreada e é o único head, e criar uma nova revisão com `down_revision = "016"` sem editar migrações preexistentes; adicionar coluna/default/check/índice/trigger, downgrade simétrico e backfill exclusivamente `unknown`.
3. Alinhar `scripts/init-db.sql` e `src/models/event.py` ao schema migrado.
4. Atualizar todos os INSERTs/SELECTs do `EventStore` e substituir reconstruções duplicadas por um mapper row→`EventData` único.
5. Cobrir imutabilidade do contexto, round-trip dos três valores e smoke PostgreSQL isolado que exercita upgrade a partir da revisão anterior, legado, constraints, índice, trigger e EventStore direto.

**Acceptance Criteria:**
- [ ] Migração, bootstrap e ORM têm o mesmo domínio/default/índice e o downgrade remove somente objetos deste wish.
- [ ] `ResolvedInteractionContext` é frozen, não contém credenciais/raw auth e rejeita mutação; a identidade entre consumidores é provada no Group 3.
- [ ] O trigger impede mudança efetiva da origem e permite update de outras colunas.
- [ ] Legado continua `unknown` e os cinco métodos do Event Store preservam todos os valores.
- [ ] O smoke recusa host não-loopback, URL igual à aplicação, database administrativa fora da allowlist, usuário sem `CREATEDB` e nome filho fora de prefixo/regex interno; deriva a URL filha com `URL.set`, faz quoting seguro de identificador, descarta engines/conexões e executa `DROP DATABASE` em `finally` somente para o nome exato criado.
- [ ] A suíte normal pode marcar o smoke como skip sem env; a invocação explícita com `POSTGRES_SMOKE_DATABASE_URL` é obrigatória no gate.

**Validation:**
```bash
poetry run pytest tests/core/test_interaction_context.py tests/memory/test_event_store.py -v
# Start gate — executar e registrar antes de qualquer edição
INTERACTION_ORIGIN_BASE_SHA="${INTERACTION_ORIGIN_BASE_SHA:?record the billing-usage-metering commit SHA}"
test "$(git rev-parse HEAD)" = "$INTERACTION_ORIGIN_BASE_SHA"
test -z "$(git status --porcelain)"
git cat-file -e "$INTERACTION_ORIGIN_BASE_SHA:alembic/versions/20260810_000001_016_add_knowledge_document_size.py"
BASE_016_BLOB="$(git rev-parse "$INTERACTION_ORIGIN_BASE_SHA:alembic/versions/20260810_000001_016_add_knowledge_document_size.py")"
test "$BASE_016_BLOB" = "$(git hash-object alembic/versions/20260810_000001_016_add_knowledge_document_size.py)"
git show "$INTERACTION_ORIGIN_BASE_SHA:alembic/versions/20260810_000001_016_add_knowledge_document_size.py" | rg -q '^revision: str = "016"$'
git show "$INTERACTION_ORIGIN_BASE_SHA:alembic/versions/20260810_000001_016_add_knowledge_document_size.py" | rg -q '^down_revision: Union\[str, None\] = "015"$'
test "$(poetry run alembic heads)" = "016 (head)"

# Gates pós-implementação
poetry run pytest tests/integration/test_interaction_origin_postgres.py::test_migration_down_revision_matches_preflight_head -v
poetry run pytest tests/integration/test_interaction_origin_postgres.py -v
POSTGRES_SMOKE_DATABASE_URL="${POSTGRES_SMOKE_DATABASE_URL:?set an isolated loopback PostgreSQL admin URL}" poetry run pytest -m integration tests/integration/test_interaction_origin_postgres.py -v
poetry run mypy src/core/interaction_context.py src/models/event.py src/memory/event_store.py --ignore-missing-imports
poetry run isort --check-only src/core/interaction_context.py src/models/event.py src/memory/event_store.py tests/core/test_interaction_context.py tests/memory/test_event_store.py tests/integration/test_interaction_origin_postgres.py
poetry run flake8 src/core/interaction_context.py src/models/event.py src/memory/event_store.py tests/core/test_interaction_context.py tests/memory/test_event_store.py tests/integration/test_interaction_origin_postgres.py
poetry run black --check src/core/interaction_context.py src/models/event.py src/memory/event_store.py tests/core/test_interaction_context.py tests/memory/test_event_store.py tests/integration/test_interaction_origin_postgres.py
git diff --check
```

O primeiro pytest permite skip sem env na suíte normal. O segundo é o smoke obrigatório e falha na expansão do shell se a URL não existir. O teste cria somente um banco filho com prefixo interno e sufixo validado por regex, usa `sqlalchemy.engine.URL.set(database=...)` e quoting de identificador do driver, mantém o nome exato em variável não derivada e só então o remove no `finally`, após `dispose()` de todos os engines. Um teste unitário cobre cada recusa destrutiva. A implementação também deve afirmar que `down_revision` da nova migração é exatamente o único head verificado pelo preflight.

**depends-on:** billing-usage-metering (card `t_msontnyf011833ab`)

---

### Group 2: Resolver compartilhado e `/sessions`

**Goal:** entregar o resolvedor compartilhado e fechar criação/leitura de sessão como incremento autônomo, sem antecipar o caller de `/events`.

**Deliverables:**
1. Extrair um resolvedor compartilhado de tenant efetivo com regras explícitas para API key, clinic_admin, builder, super_admin e deny-by-default para demais roles.
2. Fazer `POST /v1/sessions` criar somente no tenant resolvido e aplicar o mesmo tenant autorizado nas leituras/listagens de sessão.
3. Atualizar `src/api/schemas/sessions.py` e o mapping de `SessionEventResponse` para expor `interaction_origin` e `interaction_event_id` sem alterar `event_id`.
4. Adicionar testes de role/override, ownership e mapping/serialização de respostas de sessão, incluindo eventos legados `unknown`.

**Acceptance Criteria:**
- [ ] Matriz de roles/overrides retorna os códigos exatos; API key/clinic_admin não sobrescrevem tenant, builder exige ownership e super-admin usa a seleção autorizada.
- [ ] Testes com spies provam zero criação/consulta mutante para role ou tenant inválidos.
- [ ] `SessionEventResponse` mapeia a origem do row ORM `Event`; preserva `event_id`, tipo, tenant e sessão; usa `interaction_event_id = event.id` somente para `message.in` e `null` para os demais tipos.
- [ ] Nenhum arquivo de `/events`, `EventService`, pipeline ou handoff é modificado neste grupo.

**Validation:**
```bash
poetry run pytest tests/api/test_middleware_auth.py tests/api/test_builder_clinic_access.py tests/api/test_sessions.py tests/api/test_integration.py -v
poetry run mypy src/api/middleware/auth.py src/api/routes/sessions.py src/api/schemas/sessions.py src/services/session_service.py --ignore-missing-imports
poetry run isort --check-only src/api/middleware/auth.py src/api/routes/sessions.py src/api/schemas/sessions.py src/services/session_service.py tests/api/test_middleware_auth.py tests/api/test_builder_clinic_access.py tests/api/test_sessions.py tests/api/test_integration.py
poetry run flake8 src/api/middleware/auth.py src/api/routes/sessions.py src/api/schemas/sessions.py src/services/session_service.py tests/api/test_middleware_auth.py tests/api/test_builder_clinic_access.py tests/api/test_sessions.py tests/api/test_integration.py
poetry run black --check src/api/middleware/auth.py src/api/routes/sessions.py src/api/schemas/sessions.py src/services/session_service.py tests/api/test_middleware_auth.py tests/api/test_builder_clinic_access.py tests/api/test_sessions.py tests/api/test_integration.py
git diff --check
```

O resolvedor é a interface estável consumida pelo grupo seguinte; `/events` não é caller até o Group 3.

**depends-on:** Group 1

---

### Group 3: `/events`, pipeline, bundle e compatibilidade

**Goal:** integrar o caller `/events` e o serviço que ele chama numa única entrega executável, transportar um contexto único e fechar as leituras públicas contra cross-tenant.

**Deliverables:**
1. Atualizar `/v1/events`, `src/api/schemas/events.py`, response mapping e caller integration juntos: tenant efetivo pelo resolver do Group 2, sessão por `(tenant_id, id)`, campo reservado rejeitado before-model e contexto criado uma vez.
2. Alterar `EventService`, `run_pipeline`/`finish_response` e intermediários para receber e preservar a identidade do mesmo `ResolvedInteractionContext`; usar o UUID pré-alocado no `message.in`, preservar outros tipos e manter `event_id` como `srm.decision`.
3. Persistir o bundle em uma chamada a `append_batch`; em falha best-effort, registrar erro e devolver `interaction_event_id=null`.
4. Manter `HandoffRequest` e a compatibilidade de construtores neste grupo. Fornecer uma única factory/path backward-compatible: quando o contexto estiver ausente, criar uma vez um contexto frozen com origem `unknown`; callers legados passam pelo mesmo path, sem recompor tenant, sessão, origem ou interaction ID em parâmetros separados.
5. Fechar `GET /v1/events/{event_id}` e listagens: resolver tenant efetivo, buscar detail por `(effective_tenant_id, event_id)`, listar somente pelo tenant autorizado, exigir ownership para builder e permitir seleção cross-clinic apenas ao super-admin conforme o resolvedor.
6. Adicionar testes negativos de detail/list cross-tenant, builder sem ownership, role proibida e sessão estrangeira, além de testes de response, pipeline, bundle e factory legada.

**Acceptance Criteria:**
- [ ] Nenhuma fase ou evento derivado recompõe tenant, sessão, origem ou interaction ID de campos independentes; teste por identidade (`is`) prova o mesmo objeto em `run_pipeline`, `finish_response`, bundle e handoff síncrono.
- [ ] Contexto ausente percorre uma única factory e resulta em um contexto frozen `unknown`; callers atuais e construtores legados continuam funcionando até a wave seguinte.
- [ ] Uma execução produz exatamente um batch com origem uniforme e ID raiz correto quando aplicável.
- [ ] Falha injetada no batch preserva o response legado e zera somente `interaction_event_id`.
- [ ] Detail nunca busca por `event_id` isolado; detail/list usam tenant efetivo e testes provam ausência de enumeração cross-tenant para API key, clinic_admin e builder, preservando a seleção autorizada de super-admin.
- [ ] Testes cobrem campo reservado, fluxo normal, `session.start`, handoff preparado, construtores existentes e ausência de referência órfã.

**Validation:**
```bash
poetry run pytest tests/api/test_events.py tests/api/test_events_e2e.py tests/api/test_interaction_origin_boundary.py tests/services/test_event_service_emission.py tests/services/test_event_service_origin.py tests/core/test_pipeline.py tests/handoff/test_service.py -v
poetry run mypy src/api/routes/events.py src/api/schemas/events.py src/services/event_service.py src/core/pipeline.py src/handoff/models.py --ignore-missing-imports
poetry run isort --check-only src/api/routes/events.py src/api/schemas/events.py src/services/event_service.py src/core/pipeline.py src/handoff/models.py tests/api/test_events.py tests/api/test_events_e2e.py tests/api/test_interaction_origin_boundary.py tests/services/test_event_service_emission.py tests/services/test_event_service_origin.py tests/core/test_pipeline.py tests/handoff/test_service.py
poetry run flake8 src/api/routes/events.py src/api/schemas/events.py src/services/event_service.py src/core/pipeline.py src/handoff/models.py tests/api/test_events.py tests/api/test_events_e2e.py tests/api/test_interaction_origin_boundary.py tests/services/test_event_service_emission.py tests/services/test_event_service_origin.py tests/core/test_pipeline.py tests/handoff/test_service.py
poetry run black --check src/api/routes/events.py src/api/schemas/events.py src/services/event_service.py src/core/pipeline.py src/handoff/models.py tests/api/test_events.py tests/api/test_events_e2e.py tests/api/test_interaction_origin_boundary.py tests/services/test_event_service_emission.py tests/services/test_event_service_origin.py tests/core/test_pipeline.py tests/handoff/test_service.py
git diff --check
```

Esses testes tornam caller e callee compatíveis no mesmo commit e validam o boundary de leitura, que se torna sensível ao expor origem.

**depends-on:** Group 2

---

### Group 4A: Webhooks

**Goal:** aplicar a matriz normativa de ingressos usando a factory compatível entregue pelo Group 3.

**Deliverables:**
1. Passar contexto `production` somente depois da capability/tenant/instance válida no Z-API e da HMAC/tenant/phone válida no Meta.
2. Passar contexto `unknown` para Evolution e Uazapi sem adicionar autenticação nova nem inferir por provider/channel.
3. Manter requests inválidos de Z-API/Meta rejeitados antes de efeitos, sem downgrade para `unknown`.
4. Adicionar testes por adapter, preservando autenticação, efeitos e contratos preexistentes.

**Acceptance Criteria:**
- [ ] Cada adapter tem teste positivo e negativo que prova classificação após a respectiva prova de autenticidade.
- [ ] Evolution/Uazapi nunca produzem `production` no estado atual.
- [ ] O mesmo objeto de contexto criado após autenticação chega ao pipeline; adapters não recompõem campos separados.
- [ ] Suítes existentes de webhook não sofrem regressão.

**Validation:**
```bash
poetry run pytest tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_evolution.py -v
poetry run mypy src/api/routes/webhook.py --ignore-missing-imports
poetry run isort --check-only src/api/routes/webhook.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_evolution.py
poetry run flake8 src/api/routes/webhook.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_evolution.py
poetry run black --check src/api/routes/webhook.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_evolution.py
git diff --check
```

**depends-on:** Group 3

---

### Group 4B: Handoff

**Goal:** preservar origem nos dois serviços de handoff sem adicionar coluna ou lookup ao `HandoffRecord`.

**Deliverables:**
1. Atualizar `src/services/handoff_service.py` e o módulo duplicado `src/handoff/service.py` para consumir o contexto/factory do Group 3 ao emitir eventos.
2. Na iniciação síncrona derivada da interação, herdar o mesmo contexto. Em accept, complete, timeout, admin ou qualquer ação tardia, usar contexto explícito frozen com origem `unknown`.
3. Não adicionar coluna, serialização, reconstrução ou lookup de contexto/origem em `HandoffRecord`; a fronteira tardia permanece fail-closed.
4. Preservar `HandoffRequest` e compatibilidade de construtores já entregue no Group 3; adicionar teste direto novo para `src/handoff/service.py`, além dos testes do serviço principal.

**Acceptance Criteria:**
- [ ] Iniciação síncrona mantém identidade, tenant, sessão, origin e interaction ID da interação.
- [ ] Accept/complete/timeout/admin tardios persistem `unknown` e nunca inferem origem do record, channel ou metadata.
- [ ] Nenhuma coluna/lookup é adicionado a `HandoffRecord`.
- [ ] Ambos os módulos possuem teste direto de emissão e os construtores legados permanecem compatíveis.

**Validation:**
```bash
poetry run pytest tests/services/test_handoff_service.py tests/handoff/test_service_interaction_origin.py -v
poetry run mypy src/services/handoff_service.py src/handoff/service.py --ignore-missing-imports
poetry run isort --check-only src/services/handoff_service.py src/handoff/service.py tests/services/test_handoff_service.py tests/handoff/test_service_interaction_origin.py
poetry run flake8 src/services/handoff_service.py src/handoff/service.py tests/services/test_handoff_service.py tests/handoff/test_service_interaction_origin.py
poetry run black --check src/services/handoff_service.py src/handoff/service.py tests/services/test_handoff_service.py tests/handoff/test_service_interaction_origin.py
git diff --check
```

**depends-on:** Group 3

---

### Group 5: Contrato, documentação e gate agregado

**Goal:** publicar o contrato semântico e provar o conjunto integrado no alcance total do wish.

**Deliverables:**
1. Atualizar contratos API, schema do banco, pipeline cognitivo, exemplos e guia de builder com origem server-managed, matriz, erros e dois IDs.
2. Documentar que `channel` é transporte; API key é produção; JWT é simulador; Evolution/Uazapi são unknown; analytics/rating continuam inalterados.
3. Executar o gate backend completo, o smoke PostgreSQL explícito e os gates package-scoped do frontend que consome `/v1/events`.
4. Gerar a união completa de paths committed, staged, unstaged e untracked desde `INTERACTION_ORIGIN_BASE_SHA`; o reviewer compara a lista ordenada com `Files to Create/Modify`, registra cada exceção e falha o gate se houver path extra. A denylist de rating, analytics, billing, dataset, fine-tuning e `builder_slm_training` permanece como defesa automática adicional.

**Acceptance Criteria:**
- [ ] Docs e OpenAPI descrevem os mesmos valores, matriz, códigos e nulabilidade implementados.
- [ ] Gate completo de backend, smoke PostgreSQL e typecheck/test/build do app frontend passam.
- [ ] A evidência do review contém a lista ordenada de todos os paths committed/staged/unstaged/untracked e a confirmação manual de correspondência com `Files to Create/Modify`; não existe funcionalidade dos wishes bloqueados.

**Validation:**
```bash
rg -n "interaction_origin|interaction_event_id|caller_role_forbidden|channel" docs/api-contracts.md docs/database-schema.md docs/cognitive-pipeline.md docs/code-examples.md docs/builder-integration-guide.md
make quality
POSTGRES_SMOKE_DATABASE_URL="${POSTGRES_SMOKE_DATABASE_URL:?set an isolated loopback PostgreSQL admin URL}" poetry run pytest -m integration tests/integration/test_interaction_origin_postgres.py -v
pnpm --dir frontend --filter @workspace/neurostack run typecheck
pnpm --dir frontend --filter @workspace/neurostack run test
PORT=3001 BASE_PATH=/ pnpm --dir frontend --filter @workspace/neurostack run build
poetry run isort --check-only src tests
poetry run flake8 src tests
INTERACTION_ORIGIN_BASE_SHA="${INTERACTION_ORIGIN_BASE_SHA:?record the billing-usage-metering commit SHA}"
git merge-base --is-ancestor "$INTERACTION_ORIGIN_BASE_SHA" HEAD
{
  git diff --name-only "$INTERACTION_ORIGIN_BASE_SHA"..HEAD
  git diff --name-only --cached
  git diff --name-only
  git ls-files --others --exclude-standard
} | sort -u
test -z "$(git diff --name-only "$INTERACTION_ORIGIN_BASE_SHA" -- src/api/routes/rlhf.py src/services/trace_service.py src/api/routes/builder.py src/api/routes/admin.py src/api/helpers/health.py src/api/routes/billing.py src/services/billing_usage_service.py frontend/artifacts/neurostack/src)"
! git diff -U0 "$INTERACTION_ORIGIN_BASE_SHA" -- src tests alembic scripts | rg -n "builder_slm_training|approved_for_training|fine.?tun"
git diff --check
```

O gate backend completo é obrigatório porque o wish toca schema, auth, core e webhooks compartilhados. O frontend não muda, mas seu pacote real é validado por consumir a resposta pública aditiva; o gate root não é usado porque referencia pacotes ausentes e não é o contrato documentado deste app.

**depends-on:** Group 4A, Group 4B

---

## QA Criteria

_O agente de QA verifica cada item em ambiente de desenvolvimento após o merge._

- [ ] Em PostgreSQL real, upgrade sobre uma base com evento legado resulta em `unknown`; valor inválido e alteração posterior falham, enquanto update de outro campo passa.
- [ ] Uma interação equivalente via API key e JWT persiste respectivamente `production` e `simulator`, sem depender de `channel`.
- [ ] Tenant/sessão cruzados, `investor`, role desconhecida e campo reservado falham antes de estado, LLM e persistência.
- [ ] Z-API/Meta válidos persistem `production`; Evolution/Uazapi persistem `unknown`; payload autenticado inválido não produz efeitos.
- [ ] `event_id` mantém compatibilidade, `message.in` retorna ID persistido, `session.start` retorna ID nulo e falha do Store nunca cria referência órfã.
- [ ] Bundle, queries e ambos os writers de handoff preservam a mesma origem em leitura posterior.
- [ ] GET detail/list de eventos não revela origem nem existência de eventos cross-tenant; builder sem ownership falha e super-admin segue a seleção autorizada.
- [ ] Iniciação síncrona de handoff herda contexto; accept/complete/timeout/admin tardios são `unknown`, sem persistência de origem em `HandoffRecord`.
- [ ] Smoke destrutivo recusa configurações inseguras e remove somente o banco efêmero exato mesmo após falha.
- [ ] Nenhum default de dashboard, rating, consentimento, billing, dataset ou treinamento muda nesta entrega.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| O commit de `billing-usage-metering` ainda não existe | High — dependência de execução | Decisão 3A: card `t_msontnyf011833ab` incorpora a `016`, publica o commit/head único e só então desbloqueia Group 1; este wish nunca toca a `016`. |
| O Event Store usa pool e SQL bruto separados do ORM | High | Atualizar toda projeção via mapper único e provar round-trip com o Store real no smoke. |
| `webhook.py` e testes Z-API/Meta possuem mudanças locais preexistentes | High | Group 4A é owner único desses paths; reconciliar contra `INTERACTION_ORIGIN_BASE_SHA` sem apagar diffs preexistentes. |
| Detail/list de eventos podem expor origem cross-tenant | High | Group 3 usa tenant efetivo em todas as queries, tuple lookup no detail e testes negativos de ownership. |
| Handoff possui dois serviços e ações tardias sem contexto vivo | High | Group 4B cobre ambos diretamente; síncrono herda contexto e tardio fica `unknown`, sem ampliar `HandoffRecord`. |
| RLS e FK composta ainda não existem | High | Resolver tenant/sessão fail-closed na aplicação agora; manter datasets bloqueados até `postgres-tenant-isolation`. |
| Pytest padrão força SQLite e smoke cria/destroi database | High | URL explícita, loopback, desigual à app, admin DB allowlisted, `CREATEDB`, prefixo/regex, `URL.set`, identifier quoting, dispose e exact-name drop em `finally`. |
| Event Store continua best-effort | Medium | `interaction_event_id` é nulo em falha e o wish futuro de rating será obrigado a recusar item sem fonte. |
| Evolution/Uazapi reais ficam `unknown` | Medium | Comportamento seguro e observável; promoção exige wish próprio de autenticação. |

---

## Review Results

### Plan Review — 2026-08-11T12:30:15.000Z — FIX-FIRST

- **Reviewer:** `codex-reviewer:/root/formal_design_review`
- **Reviewed staged blob:** `0783079f3ac399f5cd10616195b002ffba9ce11a`
- **Governing design:** SHIP, digest `60a66c7bb0129eccb24c132e4479bfda989fb9843325d7c4d89a9bccb2d964a8`
- **Checks:** staged blob/diff, `diff --check`, design evidence/digest, template structure, group/acceptance/validation/dependency counts e ausência de placeholders passaram; testes de implementação não foram executados neste review documental.
- **[HIGH] Migração não determinística:** a revisão local `016` não pertence a nenhum commit/wish conhecido; Group 1 precisa de owner/base rastreada e head Alembic único antes de criar a próxima revisão.
- **[HIGH] Ownership entre Groups 2/3:** `/events` chamava interfaces que só o grupo seguinte alteraria; caller, serviço e compatibilidade de construtores precisam formar incrementos executáveis.
- **[MEDIUM] Resposta de eventos da sessão:** `src/api/schemas/sessions.py` e o mapeamento de `SessionEventResponse` estavam ausentes do ownership/validação.
- **Verdict:** FIX-FIRST.

### Fix Loop 1 — 2026-08-11 — BLOCKED

- Corrigidos no plano: ownership executável Groups 2/3; schema/mapping de sessão; contexto frozen sem auth; segurança cross-tenant de GET/list; split paralelo 4A/4B; semântica síncrona/tardia de handoff; smoke destrutivo; isort/flake8; `WISH_BASE_SHA`; gate determinístico de escopo; dependências, QA, riscos e inventário.
- O histórico da revisão formal permanece acima e o Status continua FIX-FIRST até nova revisão independente.
- **[HIGH] remanescente — missing-context:** `016` ainda precisa ser rastreada numa base identificada ou formalmente descartada pelo owner. Este loop não inventa ownership, não toca a migração e não pode converter a ausência de proveniência em dependência concreta.

### Dependency Decision — 2026-08-11 — 3A

- O usuário escolheu `3A`: a `016` será incorporada primeiro pelo workstream `billing-usage-metering`.
- Dependência declarada no DAG: `billing-usage-metering`; card de handoff: `t_msontnyf011833ab`.
- O Group 1 permanece bloqueado até o card entregar um commit identificado, a `016` rastreada e exatamente um head Alembic; a migração deste wish revisará `016`.
- A decisão resolve a ambiguidade do plano, mas não autoriza este wish a editar, stagear ou commitar a `016`.

### Plan Review — 2026-08-11T13:01:02.000Z — SHIP

- **Reviewer:** `codex-reviewer:/root/formal_design_review`
- **Reviewed staged blob:** `bc7c5f04882901629206d805c65384f2332f1022`
- **Governing design:** SHIP, digest `60a66c7bb0129eccb24c132e4479bfda989fb9843325d7c4d89a9bccb2d964a8`
- **Checks:** blob staged e working copy coincidiram; `diff --check` passou; digest do DESIGN permaneceu válido; o delta final foi relido; estrutura final possui 6 grupos, 6 seções de acceptance, 6 de validation, 7 declarações `depends-on`, 1 `blocks` e nenhum placeholder operacional.
- **Dependency gate:** `billing-usage-metering`/`t_msontnyf011833ab` pode permanecer não executado porque o start-gate do Group 1 exige commit-base exato, árvore limpa, blob `016` intacto e head único antes de qualquer edição.
- **Gaps:** nenhum.
- **Verdict:** SHIP.

### Execution Start Gate — 2026-08-11T17:27:36-03:00 — PASS

- **Lane:** `/tmp/neurostack-interaction-origin.HcaNck`, branch `wish/interaction-origin-training-safety`, clean at `INTERACTION_ORIGIN_BASE_SHA=36d5d55f22fbb7a673fd86fb3aed7dd48a181a98` before any implementation edit.
- **Billing provenance:** tracked `016` exists at the execution base; both committed and filesystem Git blob are `5e81da4f51e19710b4f552224deff580c285f247`, matching the published handoff. Its content declares `revision = "016"` and `down_revision = "015"`.
- **Migration gate:** the lock-matched environment reports exactly `016 (head)`; `poetry.lock` remains SHA-256 `ee7555da0c75405a4f8c3579f9314f4629c1200287c88c4ddd995feaf6e4c6cf`.
- **Dependency:** `t_msontnyf011833ab` is done and the enforced block on Group 1 was removed before dispatch.

### Aggregate Gate Preflight — 2026-08-11T17:39:59-03:00 — BASELINE RED

- **Absolute repository gate:** `make quality` on the untouched execution base exits `2` at its first recipe, `black --check src tests`: 150 preexisting files would be reformatted, so later recipes are not reached by Make.
- **Fresh backend baseline:** the exact execution base and lock-matched environment collected 1,165 unique testcases, 198 failures and one skip in `/tmp/interaction-origin-base-36d5d55-junit.xml`, SHA-256 `9048bae9251297e675ac9b47024264131ef258b5d6400db2bab83df3cb7c86a7`.
- **Interpretation:** this is diagnostic evidence, not a waiver or silent plan change. Focused group gates remain absolute; the Group 5 aggregate gate must still run and any baseline-aware disposition requires explicit diagnosis and authorization.

### Group 1 Implementation and Fix Loops — 2026-08-11 — VALIDATED, POLICY BLOCKED

- **Engineer:** `/root/origin_group1_engineer`; exactly the eight Group 1 paths changed, with `016` preserved at Git blob `5e81da4f51e19710b4f552224deff580c285f247` and `017` as the sole head.
- **Final hashes:** `017` `555110ae9824c21e37d9a9a105b0cb5095ad9ec9510c2c45ae7957317d11b4eb`; init SQL `71fba71e9f4ab23273a951b8b0310e63f89c1ab5e2d9c0f0cb44ed86f86f846b`; context `5431fc87d4a552683e0b5eb3e1e92b6f01a275f951a597625b2a65f24f976887`; ORM `deb5685facd7866c1a7b7eda0d7ae91c69b512763efe9bb29c3c7db91dbc339a`; Event Store `45ddb0a7f103471f5368ca8bf81876db9b12dca12ef310e5c98da9e6c9c7aac6`; context tests `50e5443f879cbab505bf77728411d5c6387652d0ca10323852c9a503a41bb5d2`; Event Store tests `0a86fa79cc56a142d123cf28eb193e4cb9726f5aec95e6855499555c75e1d759`; PostgreSQL tests `8e6e3e34b6488da192afbb2788d3fc02575098e222547676249b8585677e73cf`.
- **Fix loop 1/2; quality loop 1/1:** removed an incidental aggregation delta, restored minimal append `RETURNING`, added Alembic credential redaction, direct identifier/dispose/exact-drop/failure-cleanup tests and downgrade preservation checks. The normal index remains transactional; concurrent/autocommit would create partial recovery states and any timeout would invent an unapproved budget.
- **Fix loop 2/2:** test-only alignment of the revision-016 legacy session seed with its required `is_active` and `message_count` fields after two distinct real-PostgreSQL failures.
- **Final behavior gates:** focused core/Event Store 45/45; normal integration 17/17 plus the single guarded real-smoke skip; mandatory real PostgreSQL smoke 1/1 with 17 deselected; cleanup left only `postgres`, `template0`, `template1`, and the disposable `--rm` container was stopped. isort, flake8, Black and diff-check pass; Alembic reports only `017 (head)`.
- **Mypy evidence:** the exact approved command exits nonzero solely with 176 transitive diagnostics in 46 unowned files; no diagnostic names a Group 1 source file, and `--follow-imports=silent` passes all three owned roots. The comparable execution-base command for the two roots that existed there has 182 diagnostics, so this is preexisting debt rather than a regression.

### Group 1 Acceptance Re-review — 2026-08-11 — SHIP

- **Reviewer:** `/root/origin_group1_acceptance`, independent and read-only; all eight final hashes, exact scope, migration chain, PostgreSQL evidence, destructive safety, redaction, downgrade, five Event Store operations and absence of excluded behavior were verified.
- **Gaps:** none. The reviewer classifies the exact mypy output as preexisting transitive debt because owned-file diagnostics are zero and the scoped command passes.

### Group 1 Quality Re-review — 2026-08-11 — BLOCKED

- **Reviewer:** `/root/origin_group1_quality`, independent and read-only; all code, security, performance and test gaps from the first review are resolved.
- **[HIGH] Sole blocker — ambiguous gate disposition:** the WISH still names the exact mypy command as an absolute gate and no authorization in this wish converts its preexisting transitive failures into a baseline-aware comparison. Authorizing the recorded `182 → 176`, zero-owned-diagnostic and silent-scope contract changes this verdict to `SHIP`; retaining literal exit zero leaves Group 1 blocked on repository-wide type debt outside its scope.
- **[MEDIUM] Non-blocking rollout recommendation:** measure normal index-build duration at production-like event cardinality and approve a maintenance window; design a separate online migration only if the measurement exceeds that window.
- **State:** task `t_msompoj9a76a463c` remains `in_progress`; no dependent group is dispatched until the human gate decision is recorded and independently reviewed.

### Human Gate Decision — 2026-08-11T19:06:28-03:00 — A

- O usuário autorizou a política baseline-aware fail-closed registrada em **Shared Baseline-Aware Regression Gate** para mypy transitivo e para as recipes agregadas já vermelhas no execution base.
- Comandos literais e resultados absolutos continuam registrados; a autorização aceita somente findings externos comprovadamente preexistentes e bloqueia qualquer finding novo, diagnóstico em path owned, testcase baseline ausente, failing ID novo, coleta/erro interno, smoke/build/focused gate vermelho ou suppression.
- A emenda requer review independente antes de converter o quality verdict do Group 1 ou liberar o Group 2.

### Baseline Policy Review and Hardening — 2026-08-11T19:11:40-03:00 — FIX LOOP 1

- **Final-gate initial verdict:** `/root/origin_baseline_policy_gate` returned `SHIP` for WISH SHA-256 `6c5b7c3eafe1e8d80908c9e618f40d442f7d20d6b4982451a746701b3363c1d9`.
- **Quality initial verdict:** `/root/origin_group1_quality` returned `FIX-FIRST`: a set could hide duplicate mypy diagnostics, JUnit did not deterministically reject a pass/fail becoming skip/xfail, and the candidate artifact was not bound to a fresh named invocation.
- **Resolution:** no verdict overrides the other. The policy now uses occurrence-preserving Counters, zero cumulative-owned diagnostics, an exact fresh candidate JUnit invocation with recorded path/status/digest, `candidate_skipped - baseline_skipped = ∅`, and pass-only status for every new test ID. Both reviewers must re-review this amended text before Group 1 can complete.

### Baseline Policy Re-reviews and Group 1 Completion — 2026-08-11T19:15:55-03:00 — SHIP

- **Quality re-review:** `/root/origin_group1_quality` returned `SHIP` on WISH SHA-256 `b88a9062c54c0310142e260f5749f9c2eb99abcfdda92b8f753d535dca8ea8a0`; the occurrence-aware external mypy delta is empty, owned diagnostics are zero, and every prior policy/code gap is resolved.
- **Adversarial final-gate:** `/root/origin_baseline_policy_gate` independently returned `SHIP` on the same staged artifact; literal commands, immutable base/lock/JUnit, fresh candidate provenance, skip/xfail handling, multiplicity and absolute focused/smoke/frontend gates are fail-closed.
- **State:** task `t_msompoj9a76a463c` was marked done only after implementation acceptance `SHIP`, quality `SHIP`, final-gate `SHIP` and orchestrator validation. The transactional index timing remains a non-blocking production rollout measurement, not a hidden waiver.

### Group 2 Implementation, Fix Loops and Completion — 2026-08-11T20:25:06-03:00 — SHIP

- **Final scope and hashes:** exactly the eight Group 2 paths changed: auth middleware `2a3c2bf31c917cb4e4e72829a7d7c57c238ca73b6c70bf51e68cf5f458e63b5d`; sessions route `f25b63bee997b2fdec803af7a79982e74529dfe2438d72b11d375f889885323a`; sessions schema `dcb488cdf634c863e090175bb221eea6215eaf7254b2be1b37536671632e478a`; session service `22fe3ee36280f4156dcef2d93ac5dffa33cfd653454f923205e026eb560aac2c`; auth tests `beec9c225b18fc6adfc038d9a7690755f061cec90d4d26603b7a91515f713ae7`; builder tests `76b99b79ace0c5ca058952138c2c7827c832169de91125379ebb1f2cd7753e3f`; session tests `733b5e89f507a42956d82a087fe8c2dcd73e0cb0d69e39f2f1a9dd3f395ebb52`; integration tests `d6c045003289c400ddb3906b2a976e7d14e265045d8710cbf7617b48afff836d`. Group 1 hashes remained intact and no `/events`, EventService, pipeline or handoff path changed.
- **Behavior:** API key and `clinic_admin` remain bound to their tenant; builder selection requires ownership; super-admin may select an explicit tenant; other roles fail with `caller_role_forbidden`. Session create, detail, list, state, events, update and delete authorize before service/DB effects, and reads/mutations use the effective tenant. Foreign-session mutation returns 404.
- **Response contract:** `SessionEventResponse.interaction_origin` uses the closed `InteractionOrigin`; all three values serialize and invalid values fail validation. `interaction_event_id` is the ORM event ID only for `message.in`, otherwise null, while legacy identity fields remain unchanged.
- **Normal fix loop 1/2:** closed preexisting PATCH/DELETE tenant bypasses, moved detail/state service creation after authorization, added negative side-effect spies and direct tenant-scoping/foreign-session tests.
- **Quality fix loop 1/1:** replaced the sole new `type: ignore[arg-type]` in the negative enum test with `SessionEventResponse.model_validate`; no ignore, suppression, skip or xfail was introduced.
- **Final validation:** exact focused suite 83/83; isort, flake8, Black and `git diff --check` pass. The cold-cache literal mypy comparison is base 226 diagnostics/54 files, including seven owned diagnostics, versus candidate 219/52 with zero owned diagnostics and an empty occurrence-aware external delta; the exact owned roots also pass `--follow-imports=silent`.
- **Reviews and state:** independent acceptance and quality reviewers returned `SHIP` on the final snapshot with no gaps. The orchestrator then reported evidence and marked task `t_msompoup417a18c8` done; no commit or dependent implementation preceded this gate.

---

## Files to Create/Modify

```text
# Group 1
alembic/versions/ (somente nova revisão add_interaction_origin após o blocker; nunca editar 016)
scripts/init-db.sql
src/core/interaction_context.py
src/models/event.py
src/memory/event_store.py
tests/core/test_interaction_context.py
tests/memory/test_event_store.py
tests/integration/test_interaction_origin_postgres.py

# Group 2
src/api/middleware/auth.py
src/api/routes/sessions.py
src/api/schemas/sessions.py
src/services/session_service.py
tests/api/test_middleware_auth.py
tests/api/test_builder_clinic_access.py
tests/api/test_sessions.py
tests/api/test_integration.py

# Group 3
src/api/routes/events.py
src/api/schemas/events.py
src/services/event_service.py
src/core/pipeline.py
src/handoff/models.py
tests/api/test_events.py
tests/api/test_events_e2e.py
tests/api/test_interaction_origin_boundary.py
tests/services/test_event_service_emission.py
tests/services/test_event_service_origin.py
tests/core/test_pipeline.py
tests/handoff/test_service.py

# Group 4A
src/api/routes/webhook.py
tests/api/test_webhook_zapi.py
tests/api/test_webhook_meta.py
tests/api/test_webhook_uazapi.py
tests/api/test_webhook_evolution.py

# Group 4B
src/handoff/service.py
src/services/handoff_service.py
tests/services/test_handoff_service.py
tests/handoff/test_service_interaction_origin.py

# Group 5
docs/api-contracts.md
docs/database-schema.md
docs/cognitive-pipeline.md
docs/code-examples.md
docs/builder-integration-guide.md
```
