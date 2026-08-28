# Design: Núcleo de Origem Confiável

| Field | Value |
|-------|-------|
| **Slug** | `interaction-origin-training-safety` |
| **Date** | 2026-08-11 |
| **WRS** | 100/100 |

## Problem

O backend não possui uma proveniência confiável para distinguir interações de produção, simulador e origem desconhecida. `channel`, `tenant_id`, `session_id` e metadata atravessam DTOs controlados pelo cliente, enquanto os diferentes ingressos e escritores do Event Store não carregam um contexto canônico comum; por isso qualquer filtro ou gate de treinamento construído hoje seria contornável ou inconsistente.

Este design cria somente o núcleo de proveniência: uma origem imutável no evento, um tenant/sessão autorizados antes do pipeline, um ID canônico de `message.in` e propagação completa no Event Store. Rating, aprovação e analytics serão wishes dependentes, permitindo validação e rollback independentes.

## Scope

### IN

- Adicionar `events.interaction_origin` como coluna `NOT NULL DEFAULT 'unknown'`, restrita por `CHECK` a `production`, `simulator` e `unknown`.
- Criar índice `(tenant_id, interaction_origin, event_type, created_at DESC)` e proteção PostgreSQL `BEFORE UPDATE OF interaction_origin` para impedir reclassificação após insert.
- Backfill conservador: todos os eventos existentes ficam `unknown`; nenhum dado histórico é promovido por `channel`, metadata, telefone ou heurística.
- Definir `InteractionOrigin` e um único `ResolvedInteractionContext` interno com `interaction_event_id: UUID | null`, `effective_tenant_id`, `session_id` e `origin`; o UUID só é alocado quando `request.event_type == 'message.in'`.
- Resolver o tenant efetivo antes do `EventService` com política explícita para API key, JWT de clínica, builder e super-admin; validar a sessão com `(Session.tenant_id, Session.id)` para todos os mecanismos.
- Fazer `POST /v1/sessions` e `POST /v1/events` consumirem o mesmo resolver de tenant/ownership. O serviço recebe apenas o contexto canonicalizado e não usa `tenant_id`/`session_id` brutos do body como autoridade.
- Rejeitar tentativa de enviar `interaction_origin` como campo reservado no body/payload/metadata com erro `422 interaction_origin_server_managed`.
- Classificar ingressos conforme matriz normativa:
  - API key tenant-scoped + tenant/sessão válidos → `production`;
  - JWT do painel + tenant/sessão autorizados → `simulator`, inclusive builder/super-admin operando clínica;
  - Z-API após capability/tenant/instance válidos → `production`;
  - Meta Cloud após HMAC do corpo e tenant/phone-number válidos → `production`;
  - Evolution e Uazapi atuais → `unknown`, pois ainda não autenticam criptograficamente o remetente;
  - caller interno sem prova explícita → `unknown`.
- Preservar a semântica atual de `EventResponse.event_id` como ID de `srm.decision` e adicionar `interaction_event_id: UUID | null`, definido exclusivamente como o ID do `message.in` raiz.
- Quando `request.event_type == 'message.in'`, pré-alocar `interaction_event_id` uma vez no contexto e usá-lo ao persistir esse evento; para qualquer outro tipo, inclusive `session.start`, preservar o tipo solicitado, persistir a origem normalmente e devolver `interaction_event_id=null`. Nunca devolver uma referência que não foi persistida; se o batch do Event Store falhar, preservar o comportamento best-effort existente e devolver `interaction_event_id=null`.
- Propagar o mesmo `ResolvedInteractionContext` pelas duas fases do `EventService`, pelo bundle de eventos derivados e pelos caminhos de handoff; nenhum método recebe cópias independentes de tenant/sessão/origem.
- Persistir o bundle produzido pelo `EventService` com o `append_batch` transacional já existente, garantindo que `message.in`, `srm.decision`, `prompt.generated` e demais eventos do bundle compartilhem tenant, sessão e origem.
- Atualizar todos os INSERTs, SELECTs e reconstruções de `EventData` do Event Store; centralizar a conversão row→`EventData` para evitar que leituras silenciosamente retornem `unknown`.
- Expor `interaction_origin` nas representações base de evento necessárias para round-trip e auditoria técnica, sem ainda mudar defaults ou agregações de dashboard.
- Atualizar OpenAPI/docstrings e documentação de contratos, schema, pipeline, exemplos e integração de builders para separar transporte (`channel`) de proveniência (`interaction_origin`).
- Adicionar testes unitários/API de trust boundary e round-trip, mais smoke test em PostgreSQL real para `CHECK`, índice, trigger e backfill.

### OUT

- Vínculo de rating ao `message.in`, remoção de `clinic_tenant_id`, gate de consentimento/aprovação e copy da auditoria; pertencem a `rating-source-training-gate`.
- Filtro `production` default em traces, dashboards builder/admin/health e UI; pertence a `production-analytics-origin-filter`.
- Autenticação nova para Evolution ou Uazapi; até existir um hardening próprio, ambos permanecem `unknown`.
- RLS, papéis PostgreSQL separados, `SET LOCAL app.tenant_id` nos dois pools e constraints compostas evento/sessão/tenant; pertencem a `postgres-tenant-isolation`.
- Ledger versionado de consentimento/revisão, anonimização, snapshots de dataset e linhagem de modelos.
- Consentimento individual por paciente/interação.
- Este wish não lê nem altera `builder_slm_training`. Somente o futuro `rating-source-training-gate` poderá usá-lo como gate provisório, exigindo simultaneamente origem `production`, `builder_slm_training=true` e aprovação humana por super-admin; consentimento individual permanece OUT até requisito explícito, e o ledger versionado substituirá esse gate provisório.
- Execução de fine-tuning, exportação de dataset ou qualquer consumidor de `approved_for_training`.
- Reclassificação ou promoção manual de eventos legados `unknown`.
- Mudança de cobrança/billing por origem.

## Approach

### 1. Uma autoridade por interação

Após autenticar o caller e autorizar o tenant alvo, a borda cria `ResolvedInteractionContext`:

```text
interaction_event_id  UUID | null; pré-alocado somente se request.event_type == message.in
effective_tenant_id   tenant autorizado, nunca o valor bruto do DTO
session_id            sessão já provada dentro do tenant efetivo
origin                production | simulator | unknown
```

O contexto atravessa `run_pipeline`, `finish_response`, persistência e handoff como uma unidade. Isso evita divergência temporal entre as duas fases atuais do `EventService` e reduz a superfície pública: os módulos internos recebem uma decisão pronta, não reimplementam autenticação ou classificação.

### 2. Tenant e sessão antes da classificação

Um resolver compartilhado aplica estas regras:

| Caller | Tenant efetivo |
|---|---|
| API key | Somente o tenant da credencial; override diferente é `403` |
| JWT clinic_admin | Somente o tenant do token; override diferente é `403` |
| JWT builder | Clínica selecionada somente após provar ownership |
| JWT super_admin | Clínica selecionada permitida pelo papel global existente |
| JWT investor ou qualquer role não listada | Sem tenant efetivo; `403 caller_role_forbidden` |

O resolver valida primeiro que a role JWT está explicitamente listada; `investor` ou qualquer role desconhecida retorna `403 caller_role_forbidden` antes de buscar/criar sessão, mutar estado, chamar LLM ou persistir eventos. Depois do tenant, a sessão é buscada por `Session.id == session_id AND Session.tenant_id == effective_tenant_id`. Ausência ou mismatch retorna o mesmo `404 session_not_found` antes de estado cognitivo, LLM ou Event Store. A origem só é escolhida depois dessas provas.

`POST /v1/sessions` usa o mesmo tenant resolver para impedir que uma sessão estrangeira seja criada por payload. A constraint composta e RLS ainda serão adicionadas pelo wish dedicado, mas o núcleo deixa de construir contexto confiável sobre uma associação não autorizada.

### 3. Matriz explícita de ingressos

| Ingresso | Prova atual | Origem |
|---|---|---|
| `/v1/events` com API key | credencial tenant-scoped + tenant/sessão coincidentes | `production` |
| `/v1/events` com JWT | identidade do painel + tenant/ownership + sessão | `simulator` |
| Z-API | capability digest, tenant e `instanceId` | `production` |
| Meta Cloud | HMAC dos bytes do body, tenant e phone-number ID | `production` |
| Evolution | apenas `instance_name` do payload público | `unknown` |
| Uazapi | apenas identificador do payload público | `unknown` |
| caller interno | nenhuma prova declarada | `unknown` |

Payload inválido de Z-API/Meta continua rejeitado antes de efeitos; não é rebaixado para `unknown`. Um chat web real futuro deverá usar API key/adaptador de produção, nunca `channel="web"` como atalho.

### 4. Persistência e ID canônico

`message.in` é a raiz porque traces e futuras avaliações operam sobre ele. Seu UUID é alocado ao criar o contexto somente quando `request.event_type == 'message.in'` e passado ao Event Store. Outros tipos solicitados, como `session.start`, não são convertidos nem descartados: persistem com a origem resolvida e a resposta traz `interaction_event_id=null`. `event_id` existente continua sendo o ID do `srm.decision`; o novo `interaction_event_id` elimina a ambiguidade sem quebrar `/v1`.

O bundle de eventos criado no `EventService` usa `append_batch`, já implementado, com uma transação. Sucesso permite devolver o UUID persistido; falha mantém o contrato best-effort atual, registra o erro e devolve `interaction_event_id=null`, de modo que consumidores nunca recebam referência órfã. O wish de rating deverá desabilitar avaliação quando o ID estiver ausente.

Todos os escritores que derivam eventos de uma interação recebem o mesmo contexto. Eventos sistêmicos sem interação usam contexto explícito `unknown`; nenhum caller infere origem de `channel` ou metadata.

### 5. Coluna imutável e round-trip completo

A coluna é string com `CHECK`, não enum nativo PostgreSQL, para manter futuras alterações de schema simples. O índice segue as consultas futuras por tenant/origem/tipo/período. A migração aplica `unknown` ao legado antes de instalar o trigger que bloqueia update.

`EventData` inclui a origem em sua representação tipada. O Event Store atual repete raw SQL em append, batch, get e queries; a entrega centraliza row→`EventData` e atualiza toda projeção. Testes fazem round-trip dos três valores por `append`, `append_batch`, `get_event`, `query_by_session` e `query_by_tenant`.

### 6. Rollout observável e fail-closed

O deploy não tenta recuperar história. Um caller esquecido recebe `unknown`, o que preserva segurança e aparece nas leituras de auditoria. Evolution/Uazapi continuam operacionais, mas seus novos eventos ficam `unknown` até autenticação própria ser desenhada.

Este wish não altera filtros de produção nem aprovação; portanto não cria uma janela em que `unknown` seja promovido. Não existe pipeline de dataset/fine-tuning no backend atual. Os wishes dependentes deverão reler esta coluna e manter os bloqueios do programa antes de habilitar qualquer snapshot.

### Alternatives considered

- **Inferir origem por `channel`/metadata:** rejeitado porque o cliente controla esses valores e um chat web real também pode usar `web`.
- **Origem somente na sessão:** rejeitada porque exige join, não cobre eventos sem sessão e amplia a autoridade de uma sessão sobre eventos distintos.
- **Parâmetros separados em cada fase do serviço:** rejeitados porque permitem tenant/origem divergirem entre `run_pipeline`, `finish_response` e handoff.
- **Tabela multiestágio de proveniência:** rejeitada porque origem não muda após ingestão; linhagem de transformações pertence aos datasets/modelos.
- **Falhar toda interação quando Event Store falha:** rejeitado nesta entrega para preservar disponibilidade do contrato atual; `null` mantém a referência fail-closed sem criar retry/idempotência novos.
- **Autenticar Evolution/Uazapi dentro deste wish:** rejeitado por ser uma fronteira independente, com credenciais e rollout próprios.

## Simplicity Case

- **Simplest complete design:** uma coluna imutável no evento e um contexto interno único, construído após autorização, que transporta tenant, sessão, origem e o ID de `message.in` por todos os escritores/leitores.
- **Added machinery:** `CHECK` fecha valores; índice atende o acesso tenant/origem/período; trigger sustenta imutabilidade; `ResolvedInteractionContext` impede divergência entre fases e módulos; `interaction_event_id` resolve a ambiguidade real de três IDs; mapper row→`EventData` elimina projeções duplicadas. Cada item cobre uma falha observada no código.
- **Deferred until measured:** propósito adicional por API key somente quando houver credenciais de sandbox/uso misto; tabela de transições somente se uma origem puder mudar legitimamente; cache/particionamento somente após medição. RLS, rating, analytics e ledgers não são otimizações: são wishes separados com dependências explícitas.
- **Complexity removed:** nenhuma fila, retry, idempotency state, reclassificação histórica, configuração por clínica, dupla autoridade sessão/evento ou autenticação improvisada para providers inseguros.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | `events.interaction_origin` é a autoridade | Eventos são a unidade persistida e podem existir sem sessão. |
| 2 | `message.in` é a raiz e recebe `interaction_event_id` | É a unidade canônica das views de trace e futuras avaliações. |
| 3 | Preservar `event_id` atual | Evita quebra silenciosa de consumidores `/v1`; o novo campo tem semântica própria. |
| 4 | Contexto resolvido atravessa as duas fases | Impede tenant, sessão, origem e IDs divergirem temporalmente. |
| 5 | API key validada é produção; JWT do painel é simulador | Decisão confirmada pelo usuário e independente de `channel`. |
| 6 | Somente Z-API e Meta são produção hoje | São os únicos adaptadores com prova de autenticidade suficiente no código atual. |
| 7 | Evolution/Uazapi são `unknown` | Resolver tenant por identificador público não autentica o remetente. |
| 8 | String + `CHECK` em vez de enum nativo | Conjunto fechado sem custo extra de evolução do enum PostgreSQL. |
| 9 | Origem é imutável após insert | Proveniência não pode ser reclassificada por PATCH ou bug administrativo. |
| 10 | Todo legado é `unknown` | Não há marcador histórico confiável o bastante para promoção. |
| 11 | Falha de persistência produz ID nulo, não órfão | Mantém disponibilidade existente e falha fechado para futuros ratings. |
| 12 | Rating e analytics são wishes dependentes | Reduz blast radius e permite rollback/testes independentes. |
| 13 | JWT usa allowlist de roles e rejeita `investor`/desconhecida antes de efeitos | Impede que uma role sem política herde acesso ou classificação por fallback. |
| 14 | `builder_slm_training` pertence somente ao gate provisório do wish futuro | Este núcleo não lê/altera a flag; `rating-source-training-gate` exigirá `production` + flag ativa + super-admin humano até o ledger versionado substituí-lo, mantendo consentimento individual fora sem requisito explícito. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Associação cross-tenant ainda é permitida por FKs independentes | High | Resolver tenant/sessão antes do pipeline agora; `postgres-tenant-isolation` adicionará constraint composta e RLS antes de datasets. |
| 2 | Evolution/Uazapi reais ficam `unknown` | Medium | Comportamento intencional fail-closed; criar hardening próprio antes de promovê-los. |
| 3 | Event Store usa pool/raw SQL separados do ORM | High | Atualizar todas as projeções e testar round-trip em cada método. |
| 4 | Escritores em handoff podem perder o contexto | High | Passar `ResolvedInteractionContext` pela interface de handoff e cobrir eventos emitidos fora do `EventService`. |
| 5 | Persistência best-effort pode retornar `interaction_event_id=null` | Medium | Campo é opcional e nunca aponta para linha inexistente; rating futuro desabilita sem fonte. |
| 6 | Pytest padrão usa SQLite e não prova trigger/check/index | Medium | Smoke test em PostgreSQL real via Docker já existente, sem novo framework de containers. |
| 7 | Campo reservado pode chegar escondido em metadata | Medium | Validação before-model procura body, payload e metadata e responde `422 interaction_origin_server_managed`. |
| 8 | API key pode ser usada informalmente para testes | Medium | Contrato atual a considera produção; propósito por credencial será exigido quando uso misto/sandbox existir. |
| 9 | Legado deixa de ter origem útil | Low | Intencional; auditoria continua possível como `unknown`, sem promoção manual. |
| 10 | Alterações locais não relacionadas já existem | Low | Execução futura limita-se aos arquivos declarados e não reverte trabalho preexistente. |

## Success Criteria

- [ ] A migração cria `events.interaction_origin NOT NULL DEFAULT 'unknown'`, `CHECK` dos três valores, índice `(tenant_id, interaction_origin, event_type, created_at DESC)` e trigger que rejeita update da coluna.
- [ ] Todos os eventos legados ficam `unknown`; a migração não contém promoção por `channel`, metadata ou heurística.
- [ ] Campo reservado `interaction_origin` em body, payload ou metadata retorna `422` com `code=interaction_origin_server_managed` e não executa pipeline.
- [ ] API key com tenant e sessão coincidentes produz contexto `production`; override de tenant retorna `403` e sessão de outro tenant retorna `404 session_not_found`.
- [ ] JWT clinic_admin só usa seu tenant; builder precisa ownership; super-admin usa a clínica selecionada; em todos os casos sessão mismatch retorna `404` antes de mutar estado, chamar LLM ou persistir evento.
- [ ] Todo `POST /v1/events` via JWT autorizado produz contexto `simulator`, independentemente de `channel` enviado.
- [ ] JWT `investor` e qualquer role não listada retornam `403` com `code=caller_role_forbidden` antes de buscar/criar sessão, mutar estado, chamar LLM ou persistir evento; teste cobre ao menos `investor` e uma role desconhecida.
- [ ] Z-API autenticado e Meta com HMAC válido produzem `production`; requests inválidos continuam rejeitados antes de efeitos.
- [ ] Evolution e Uazapi produzem `unknown` até ganharem autenticação própria; teste por adapter impede promoção acidental.
- [ ] `EventResponse.event_id` mantém a semântica atual e `interaction_event_id`, quando não nulo, identifica exatamente um `events.event_type='message.in'` persistido no tenant/sessão do contexto.
- [ ] `ResolvedInteractionContext.interaction_event_id` é `UUID | null` e só recebe UUID quando `request.event_type == 'message.in'`; teste com `session.start` prova que o tipo é preservado, a origem é persistida normalmente e a resposta contém `interaction_event_id=null`.
- [ ] Falha do batch do Event Store nunca devolve UUID órfão: a resposta mantém o contrato existente e retorna `interaction_event_id=null`.
- [ ] `message.in`, `srm.decision`, `prompt.generated` e todos os eventos derivados do bundle compartilham tenant, sessão e origem.
- [ ] Eventos de handoff derivados da interação recebem o mesmo contexto; eventos sistêmicos sem interação ficam explicitamente `unknown`.
- [ ] `append`, `append_batch`, `get_event`, `query_by_session` e `query_by_tenant` preservam `production`, `simulator` e `unknown` em round-trip.
- [ ] Tentativa de alterar `interaction_origin` após insert falha no PostgreSQL, sem impedir updates legados de outros campos.
- [ ] OpenAPI e documentação afirmam que `channel` é transporte, origem é server-managed, API key válida é produção, JWT é simulador e Evolution/Uazapi são unknown.
- [ ] Testes focados de backend passam e o smoke test PostgreSQL comprova backfill, `CHECK`, índice e imutabilidade.
- [ ] Nenhum código de rating/gate, filtro de dashboard, dataset, fine-tuning ou billing é criado ou alterado por esta entrega.

## Next Step

Após revisão independente com SHIP e verificação do digest, executar `wish` para planejar `interaction-origin-training-safety` como o núcleo. O wish deve declarar `blocks: rating-source-training-gate, production-analytics-origin-filter, dataset-snapshot-anonymization`. Os dois primeiros dependem diretamente deste núcleo; o snapshot também dependerá de `postgres-tenant-isolation` e `training-consent-review-ledger`.

Ao planejar `rating-source-training-gate`, registrar que somente esse wish futuro lê `builder_slm_training` e implementa o gate provisório `interaction_origin=production` + `builder_slm_training=true` + aprovação humana por super-admin. Consentimento individual continua fora até surgir requisito explícito; `training-consent-review-ledger` deve substituir o gate provisório por consentimento/revisão versionados, sem transferir essa lógica para este núcleo.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `60a66c7bb0129eccb24c132e4479bfda989fb9843325d7c4d89a9bccb2d964a8`
- **Reviewer:** codex-reviewer:/root/formal_design_review
- **Reviewed at:** 2026-08-11T12:15:14.000Z
<!-- genie-design-review:end -->
