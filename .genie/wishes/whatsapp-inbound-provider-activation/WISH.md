# Wish: Ativação inbound dos providers WhatsApp

| Field | Value |
|-------|-------|
| **Status** | DRAFT |
| **Execution** | BLOCKED — aguardando artefato externo de census com zero incumbents |
| **Slug** | `whatsapp-inbound-provider-activation` |
| **Date** | 2026-09-28 |
| **Author** | Lorenzo |
| **Appetite** | large |
| **Branch** | `wish/whatsapp-inbound-provider-activation` |
| **Repos touched** | neurostack-api |
| **Design** | [DESIGN governante](../../brainstorms/api-provider-catalog-presentation/DESIGN.md) — reviewed SHA-256 `025c397a0af4335e5266a3c19230d8a8a0ef405b818991a02ee4d1a9adc529f3` |

## Summary

Este wish entrega mecanismos inbound completos, porém latentes, para Meta Cloud API, Z-API,
Evolution API e Uazapi: origin/callback canônico, digests server-owned, serviço/handler de rotação,
autenticação exact-one, HMAC raw-body, redaction e negativos zero-downstream. Nenhum grupo pode ser
executado até um artefato externo, agregado e verificável provar que os counts de incumbents
Evolution/Uazapi outbound-complete sem capability digest são ambos zero.

Com census zero, a execução segue exatamente o DESIGN: a matriz pública desta árvore permanece
`1 active + 20 coming_soon`, os quatro providers WhatsApp continuam `coming_soon`, PATCH e todas as
rotas públicas de rotate falham fechadas, e os handlers tokenless de Z-API, Evolution e Uazapi
ficam inertes como transição monotônica de segurança. O mecanismo de rotação é provado apenas na
fronteira service/candidate; a primeira adoção pública pertence ao wish do catálogo.

## Problem

Este wish não executa enquanto um census externo, agregado e verificável não provar zero incumbents Evolution/Uazapi outbound-complete sem capability digest.

O runtime outbound já existe nos quatro providers; Meta POST já valida HMAC-SHA256 dos bytes crus
em comparação constant-time; Z-API já possui capability digest, resolução exact-one e rota
tokenless inerte. Ainda faltam autenticação capability para Evolution/Uazapi,
provisionamento/rotação server-owned, callback derivado apenas de `PUBLIC_BASE_URL`, exact-one no
GET Meta, redaction simétrica e negativos zero-downstream.

Esses gaps devem ser fechados sem mudar disponibilidade pública. O input desta árvore é exatamente
`1 active + 20 coming_soon`; o delta público deste wish é `∅`. Um teste interno com snapshot
candidate pode provar o contrato final do serviço, mas não autoriza provider, não altera catálogo
e não substitui a primeira adoção via HTTP. Se o census encontrar qualquer incumbent sem digest, a
premissa para tornar Evolution/Uazapi tokenless inertes é falsa e o plano deve parar, sem inventar
uma migração nesta árvore.

## Scope

### IN

- Impor o gate externo de census descrito abaixo antes de claim, checkout, implementação ou teste
  de qualquer grupo.
- Definir callback a partir de um único origin canônico `PUBLIC_BASE_URL`, sem aceitar `Host`,
  `Forwarded`, `X-Forwarded-*`, origin do browser ou valor enviado pelo cliente.
- Manter Meta em `GET|POST /v1/webhook/meta` e adicionar as rotas capability fixas
  `/v1/webhook/zapi/{capability_token}`, `/v1/webhook/evolution/{capability_token}` e
  `/v1/webhook/uazapi/{capability_token}`.
- Manter `meta_verify_token` como segredo write-only fornecido pelo usuário e os digests inbound de
  Z-API/Evolution/Uazapi como campos internos server-owned, ausentes do PATCH/OpenAPI público.
- Implementar o serviço atômico de provisionamento/rotação e o handler autenticado
  `POST /v1/config/api-credentials/webhooks/{provider}/rotate`, sempre subordinado ao status
  autoritativo do provider.
- Fazer PATCH e rotate públicos falharem fechados para todos os quatro providers WhatsApp enquanto
  estiverem `coming_soon`, antes de lock, CSPRNG, DB write, cache ou callback reveal.
- Provar primeira provisão e rotação apenas por unit/service tests com um
  `ProviderAvailabilitySnapshot` candidate explícito e injetado diretamente na fronteira de
  domínio; nenhum flag, env, header, query, payload ou mutação direta de DB pode habilitá-lo.
- Gerar capability tokens CSPRNG com 32 bytes de entropia, revelar somente a URL one-time no retorno
  do serviço candidate e persistir apenas SHA-256 hexadecimal sob a chave interna do provider.
- Resolver verify token, `phone_number_id` e cada capability digest para exatamente um tenant antes
  de DB write, Redis, rate-limit/replay, mídia/download, egress, handoff ou evento.
- Preservar Meta POST como HMAC-SHA256 dos bytes crus exatos com o app secret do tenant resolvido e
  `hmac.compare_digest`; verify token nunca autentica POST.
- Tornar Z-API, Evolution e Uazapi tokenless inertes dentro desta dependência e preservar essa
  inércia em todo backout subsequente.
- Redigir segmentos capability dos três providers em logs, exceptions, métricas, traces e headers
  derivados; ampliar o contrato do primeiro edge para os três templates.
- Entregar ao catálogo node IDs estáveis do mecanismo latente, guard público, runtime autenticado,
  auth negativa, zero-downstream e inércia tokenless.
- Preservar exatamente a matriz pública atual `1 active + 20 coming_soon`; delta de disponibilidade
  pública `∅`.

### OUT

- Planejar ou executar migração de incumbents. Count positivo exige reabertura explícita do DESIGN
  e do wish `api-provider-catalog-presentation` por decisão do usuário.
- Preservar qualquer caminho tokenless que processe evento, ou reabri-lo durante backout.
- Permitir nova adoção WhatsApp ou 2xx público de rotate enquanto o provider estiver
  `coming_soon`.
- Executar neste wish a prova HTTP de primeira configuração/rotação de uma clínica nova; ela
  pertence ao release unit do catálogo, sem override interno.
- Promover cards, alterar ordem/copy/matriz, criar frontend de callback/rotate ou declarar provider
  `active`.
- Reimplementar adapters outbound existentes, salvo o wiring mínimo após autenticação inbound
  bem-sucedida.
- Fazer backfill, mutation manual/oculta de digest, registrar webhook em painel de vendor ou afirmar
  homologação externa.
- Criar tabela, armazenar token cru/URL capability, introduzir envelope encryption, outbox, fila,
  cache de resolução, segundo token válido ou grace period de rotação.
- Configurar proxy/CDN externo; o runbook/sentinel é o contrato de no-go do operador desse edge.

## Hard Pre-execution Census Gate

Este gate ocorre fora do repositório e antes da Wave 1. Enquanto o receipt abaixo não existir e não
for validado, o wish permanece `DRAFT`, `Execution: BLOCKED`, nenhum grupo é claimed e nenhum código
ou teste de produto é executado.

### Population and counts

O produtor executa uma leitura consistente e read-only no ambiente-alvo. O payload contém somente
dois inteiros agregados:

- `evolution_unprovisioned_incumbent_count`: rows persistidos com
  `whatsapp_provider=evolution`, `evolution_api_url`, `evolution_instance_name` e
  `evolution_api_key` completos, mas sem `evolution_inbound_token_digest` válido.
- `uazapi_unprovisioned_incumbent_count`: rows persistidos com `whatsapp_provider=uazapi`,
  `uazapi_api_url`, `uazapi_instance_name` e `uazapi_token` completos, mas sem
  `uazapi_inbound_token_digest` válido.

Tenant, URL, instance, segredo, token, digest de capability e row raw são proibidos no artefato,
nos logs e no handoff. Row de provider diferente ou bundle outbound incompleto não integra a
população incumbent.

### Required external receipt

O receipt imutável e externo ao repositório deve registrar, sem campos adicionais de negócio:

| Field | Contract |
|-------|----------|
| `artifact_uri` | Localização do artefato read-only em sistema externo aprovado. |
| `issuer` | Identidade do emissor externo autorizada para o ambiente-alvo. |
| `observed_at` | Timestamp UTC ISO-8601 da leitura consistente. |
| `environment_fingerprint` | Identificador não reversível do ambiente/deployment consultado. |
| `payload_sha256` | SHA-256 do payload canônico agregado, verificado no consumo. |
| `counts.evolution` | Inteiro não negativo; deve ser `0`. |
| `counts.uazapi` | Inteiro não negativo; deve ser `0`. |

Antes de liberar execução, um verificador independente confirma issuer autorizado, digest, schema,
ambiente-alvo, timestamp não futuro e idade máxima de 24 horas. A referência, o issuer, o timestamp
e o digest aceitos são então registrados neste wish e submetidos a nova revisão independente.

### Branching rule

- Se os dois counts forem exatamente zero, a execução pode ser proposta conforme os grupos abaixo,
  mas somente depois de o receipt ser registrado e o plano aprovado; o gate não muda o status por
  inferência nem inicia trabalho automaticamente.
- Se algum count for positivo, a execução para. É obrigatória autorização explícita para reabrir o
  DESIGN e o wish do catálogo; esta árvore não propõe solução de migração.
- Se o artefato estiver ausente, incompleto, stale, com issuer/digest/ambiente divergente ou expuser
  dado proibido, o resultado é inconclusivo e `Execution: BLOCKED` permanece.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | O DESIGN revisado é normativo, mas não é `depends-on` | Este wish bloqueia o catálogo; inverter a aresta criaria ciclo. |
| 2 | O census zero é precondição externa dura | Só zero torna segura a remoção monotônica das rotas tokenless sem desenhar migração nova. |
| 3 | O input público permanece `1 active + 20 coming_soon`; delta `∅` | Esta dependência entrega capacidade latente, não disponibilidade. |
| 4 | PATCH e rotate usam o mesmo status autoritativo | Todo provider WhatsApp atual está `coming_soon`; nenhuma rota pública pode criar ou rotacionar estado. |
| 5 | O candidate existe apenas na fronteira unit/service | Prova o contrato final sem criar bypass de produção nem 2xx HTTP prematuro. |
| 6 | `PUBLIC_BASE_URL` é a única fonte de origin | Impede host-header injection e callbacks diferentes por cliente/proxy. |
| 7 | Meta usa verify token no GET e HMAC raw-body no POST | São mecanismos distintos; verify token não autentica entrega. |
| 8 | Z-API, Evolution e Uazapi usam token de 43 chars gerado por `secrets.token_urlsafe(32)` | Reutiliza a forma Z-API e entrega pelo menos 256 bits CSPRNG. |
| 9 | Somente SHA-256 do capability token é persistido | A entropia permite lookup sem token recuperável ou nova tabela. |
| 10 | Rotação substitui um único digest atomicamente, sem grace period | Invalidação imediata evita duas credenciais inbound válidas. |
| 11 | Toda identidade inbound exige `len(rows) == 1` | `.first()` e unicidade provável não são autorização. |
| 12 | Auth/binding precedem qualquer efeito | Rejeição não pode escrever, tocar Redis, baixar mídia, chamar pipeline ou emitir evento. |
| 13 | Capability URL nunca é recuperável por GET | A revelação ocorre uma vez; resposta perdida exige nova rotação quando o catálogo abrir adoção. |
| 14 | Redaction usa template por provider mesmo em paths inválidos/unmatched | 401/404/500 não podem escapar por fallback de observabilidade. |
| 15 | Tokenless é uma transição monotônica para inerte | Uma vez executada após census zero, rollback nunca restaura processamento sem capability. |
| 16 | `active` significa configurável por clínica nova e provado no candidate público | Teste interno latente não autoriza promoção nem equivale a homologação. |

## Simplicity Case

- **Simplest complete design:** reutilizar JSONB, PATCH, rotas e pipelines; generalizar a primitive
  capability da Z-API, acrescentar serviço/handler de rotação atrás do gate existente e impor
  exact-one/redaction simétricos.
- **Added machinery:** `PublicWebhookOrigin`, três chaves digest internas, serviço atômico de
  rotação, `ProviderAvailabilitySnapshot` tipado e redactor comum. Cada item fecha callback, auth,
  rotação ou vazamento sem adicionar estado público.
- **External evidence only:** o census é produzido fora desta árvore; nenhuma query/helper de census
  passa a integrar o produto.
- **Deferred until measured:** índice/tabela só após medida de lookup; envelope encryption exige wish
  próprio; auto-registro depende de requisito/vendor API; auditoria durável exige outbox.
- **Complexity removed:** nenhum toggle/env de ativação, endpoint alternativo, token cliente,
  recuperação de token, grace period, cache, fila ou terceiro estado público.

## Dependencies

- **depends-on:** none
- **blocks:** api-provider-catalog-presentation

### Dependency handoff to `api-provider-catalog-presentation`

Esta dependência entrega código latente e evidência reexecutável, não autorização de adoção. O
handoff identifica node IDs distintos para origin/callback, guard público `coming_soon`, serviço
candidate, runtime autenticado, auth negativa/rotacionada, exact-one, inércia tokenless,
redaction, zero-downstream e outbound existente.

O wish do catálogo continua responsável por:

1. Criar/pinar sua árvore de apresentação `1 active + 21 coming_soon`; esse estado pertence ao
   catálogo e não é o input nem o output público deste wish.
2. Criar/pinar o candidate integrado `16 active + 6 coming_soon`, com Python e TypeScript mudando
   juntos conforme o DESIGN.
3. Provar por HTTP, com JWT e tenant reais de teste, primeira persistência do bundle WhatsApp,
   round-trip mascarado e callback Meta canônico para uma clínica nova.
4. Provar por HTTP a primeira chamada e a rotação de
   `POST /v1/config/api-credentials/webhooks/{provider}/rotate` para Z-API, Evolution e Uazapi,
   incluindo one-time reveal, digest-only, invalidação anterior e isolamento cross-tenant.
5. Reexecutar a matriz positiva/negativa/zero-downstream dos quatro providers e os testes outbound;
   somente essa evidência satisfaz “configurável por clínica nova”.
6. Manter adoção fechada se qualquer provider continuar `coming_soon` ou se qualquer teste usar
   snapshot interno como substituto da API pública.
7. Em qualquer backout de catálogo, fechar novamente apresentação/adoção sem reabrir handler
   tokenless; runtime capability autenticado e sentinels inertes são invariantes preservados.

O catálogo cria e assume o teste público antes de executá-lo; este comando futuro não conclui esta
dependência:

```bash
poetry run pytest -q tests/api/test_whatsapp_phase_b_public_adoption.py tests/api/test_whatsapp_inbound_latent_contract.py tests/services/test_meta_service_egress.py tests/services/test_zapi_service_egress.py tests/services/test_uazapi_service_egress.py
```

## Success Criteria

- [ ] O receipt externo aceito registra artifact URI, issuer, timestamp UTC, environment fingerprint,
  payload SHA-256 e counts `{evolution: 0, uazapi: 0}`, sem PII, URL, instance, segredo, token,
  capability digest ou rows; até lá nenhum grupo roda.
- [ ] O DESIGN governante e seu reviewed digest aparecem no header; o DAG continua
  `depends-on: none` / `blocks: api-provider-catalog-presentation`.
- [ ] A árvore deste wish preserva exatamente `1 active + 20 coming_soon`; os quatro providers
  WhatsApp permanecem `coming_soon` e o delta público é `∅`.
- [ ] PATCH e rotate públicos de Meta/Z-API/Evolution/Uazapi retornam
  `provider_not_available` antes de create/lock/CSPRNG/write/flush/commit/cache/reveal.
- [ ] Não existe env, setting, header, query, body, role ou endpoint alternativo que abra o gate; o
  snapshot candidate é um valor tipado passado diretamente apenas a unit/service tests.
- [ ] `PUBLIC_BASE_URL` é canonicalizado como origin HTTPS público sem path, query, userinfo,
  fragmento ou porta diferente de 443; Host/forwarding headers nunca alteram callback.
- [ ] O callback Meta latente é `${PUBLIC_BASE_URL}/v1/webhook/meta`; os callbacks capability
  latentes são `${PUBLIC_BASE_URL}/v1/webhook/{zapi|evolution|uazapi}/{capability_token}`.
- [ ] Digests inbound são server-owned, ausentes do PATCH/OpenAPI/cache/GET; status expõe no máximo
  flags `*_inbound_token_set`, sem digest, token, URL recuperável ou campo masked.
- [ ] Unit/service tests com snapshot candidate exercitam primeira provisão e rotação dos três
  capability providers pela mesma função usada pelo handler, sem HTTP 2xx nem mutation direta de
  DB; tokens têm 32 bytes/43 chars, somente digest é persistido e o anterior é invalidado.
- [ ] Unit/service test candidate do bundle Meta aceita verify token fornecido pelo usuário, troca
  verify token/app secret atomicamente e faz o runtime rejeitar handshake/HMAC anteriores, sem usar
  PATCH público da árvore atual.
- [ ] Concorrência deixa um único digest válido; falha de validação/flush/commit conserva o digest
  anterior, não revela callback e não invalida cache prematuramente.
- [ ] Meta GET resolve verify token para exatamente um tenant; zero/múltiplos retornam o mesmo 403.
- [ ] Meta POST resolve exatamente um tenant por `phone_number_id` e valida
  `X-Hub-Signature-256` sobre bytes crus com o app secret daquele tenant e comparação constant-time;
  verify token nunca autentica POST.
- [ ] Z-API/Evolution/Uazapi exigem token canônico, digest exact-one e binding exato de
  `instanceId`/`instance`/`instanceName`, respectivamente; falhas retornam 401 genérico.
- [ ] As três rotas tokenless são inertes após esta dependência, não consultam instance/tenant e não
  chamam downstream; testes de backout provam que nenhuma reversão restaura processamento tokenless.
- [ ] Meta POST e as três capability routes provam auth ausente, inválida, ambígua, rotacionada ou
  binding divergente com zero DB write/flush/commit, Redis/rate-limit/replay, mídia, pipeline/LLM,
  egress/handoff e evento.
- [ ] Logs, exceptions, métricas, traces e headers derivados usam somente templates redigidos;
  token canônico, inválido ou percent-encoded nunca aparece em 401, 404 ou 500.
- [ ] O handoff enumera evidência entregue e prova HTTP ainda obrigatória no catálogo; nenhum teste
  interno é descrito como clínica nova, provider ativo ou homologação live.
- [ ] Os testes outbound dos quatro providers permanecem verdes; catálogo/frontend/matriz não são
  modificados por este wish.

## Execution Strategy

### Gate before Wave 1

Nenhum grupo abaixo pode ser claimed ou executado enquanto o receipt externo não tiver sido aceito
e esta revisão não tiver sido aprovada independentemente. Count positivo encerra esta execução e
exige reabertura explícita do DESIGN e do wish do catálogo; os grupos não são reinterpretados.

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — state boundaries multi-módulo | engineer-standard / high | Fixar origin, tipos, matriz pública e gates fail-closed. |

### Wave 2 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 2 — serviço stateful e rotação concorrente | engineer-standard / high | Implementar serviço latente e handler sempre fechado no input atual. |

### Wave 3 (parallel after Group 2)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 2 — autenticação stateful e ordem de falha | engineer-standard / high | Completar runtime autenticado e tornar tokenless inerte. |
| 4 | engineer | 1 — dependência determinística de labels OTel | engineer-trivial / low | Unificar redaction application/edge. |

### Wave 4 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 5 | engineer | 1 — integração multi-módulo determinística | engineer-trivial / low | Fixar matriz latente, regressão e handoff do catálogo. |

Complexity scoring rubric: score each group independently and record the total plus a short
rationale in **Complexity**. Add +2 each for orchestration/agent lifecycle, cost/model/escalation,
stateful work or subjective acceptance; add +1 each for multi-package work, OTel-label dependency,
no deterministic test, prior rework, prompt-skill change or CI/release work. Route 0–1 to
`engineer-trivial`, 2–3 to `engineer-standard`, 4–6 to `engineer-complex` and 7+ to
`engineer-complex` plus independent `final-gate`.

## Execution Groups

### Group 1: Origin, tipos e gates públicos

**Goal:** Estabelecer type/state boundaries que mantenham toda adoção WhatsApp fechada e permitam
provar o contrato candidate sem bypass público.

**Deliverables:**

1. Implementar `PublicWebhookOrigin` a partir apenas de `settings.public_base_url`: HTTPS público,
   porta ausente/443 canonicalizada sem porta; rejeitar path, query, userinfo, fragmento,
   localhost e IP não global.
2. Implementar `ProviderAvailabilitySnapshot`; a rota obtém sempre snapshot autoritativo e
   unit/service tests constroem candidate explícito. Não criar env/header/query/body/test endpoint.
3. Preservar o guard de PATCH e aplicá-lo ao rotate antes de carregar row ou gerar entropia. Qualquer
   WhatsApp `coming_soon` retorna `provider_not_available` sem side effect.
4. Tornar `zapi_inbound_token_digest`, `evolution_inbound_token_digest` e
   `uazapi_inbound_token_digest` campos internos; status expõe apenas flags, callback Meta latente e
   `webhook_public_origin_ready`, sem marcar provider/readiness como ativo.
5. Remover auto-geração de `meta_verify_token`; manter verify token/app secret write-only e provar no
   domínio candidate a primeira configuração e a troca atômica dos dois segredos.

**Acceptance Criteria:**

- [ ] Matriz atual afirma exatamente `1 active + 20 coming_soon`, os quatro WhatsApp
  `coming_soon`, delta `∅` e nenhuma mudança em `PUBLIC_PROVIDER_AVAILABILITY`.
- [ ] PATCH e rotate dos quatro providers retornam `provider_not_available` antes de row load,
  lock, CSPRNG, write, cache ou reveal.
- [ ] Origins hostis e headers adversariais não mudam callback; origin inválido não abre readiness.
- [ ] OpenAPI não aceita digest; GET/cache nunca retornam digest/token/capability URL.
- [ ] Snapshot candidate aparece apenas em fixtures unit/service e não é construível de input ou
  runtime configuration.
- [ ] Serviço candidate Meta troca verify token/app secret sem eco cru; falha conserva os
  anteriores e o guard público continua fechado.

**Validation:**

```bash
poetry run pytest -q tests/api/test_whatsapp_webhook_configuration.py tests/api/test_whatsapp_webhook_adoption_gate.py tests/api/test_meta_credentials.py tests/api/test_uazapi_credentials.py tests/api/test_config_credentials_boundary.py
poetry run mypy src/config/settings.py src/api/schemas/config.py src/services/api_credentials.py src/api/routes/config.py --no-error-summary
```

**depends-on:** none

---

### Group 2: Serviço latente e rotação candidate

**Goal:** Implementar provisionamento/rotação completos sem oferecer sucesso pela API pública no
estado atual.

**Deliverables:**

1. Extrair serviço que recebe tenant id, provider, origin e `ProviderAvailabilitySnapshot`, valida
   disponibilidade antes da mutação e revalida sob lock para fechar TOCTOU.
2. No snapshot candidate ativo, exigir origin/outbound completos, serializar a linha
   (`SELECT ... FOR UPDATE` ou CAS), gerar `secrets.token_urlsafe(32)`, persistir só digest e
   substituir o anterior atomicamente; nunca modificar provider/outbound.
3. Adicionar `POST /v1/config/api-credentials/webhooks/{provider}/rotate`, provider
   `zapi|evolution|uazapi`, JWT/tenant-bound e sem token/URL no body. Com a matriz autoritativa atual,
   o handler sempre falha antes de row load/CSPRNG/write/reveal.
4. Manter o resultado interno pronto com shape
   `{"provider": <id>, "callback_url": <one-time URL>, "rotated": <bool>}`; somente service tests
   candidate recebem esse resultado.
5. Testar first provision/rotation candidate pela service/repository boundary, nunca por update
   SQL/JSONB direto; testar o handler público apenas como fail-closed.

**Acceptance Criteria:**

- [ ] Public route atual nunca retorna 2xx para WhatsApp; todos os providers falham antes de carregar
  tenant row ou gerar entropia.
- [ ] Service test candidate gera 43 chars/32 bytes, persiste digest de 64 hex, retorna shape one-time
  e invalida o digest anterior; nenhum raw token entra no estado/cache/log.
- [ ] Concorrência deixa um digest válido; rollback conserva o anterior e não produz resultado
  revelável nem invalidação prematura.
- [ ] API key/role/tenant override falham na boundary existente, sem observar outro tenant.
- [ ] Nenhum teste HTTP bem-sucedido simula provider ativo; primeira adoção pública continua
  reservada ao catálogo.

**Validation:**

```bash
poetry run pytest -q tests/api/test_whatsapp_webhook_adoption_gate.py tests/services/test_whatsapp_webhook_provisioning.py tests/api/test_api_credentials.py tests/api/test_middleware_auth.py
poetry run mypy src/api/schemas/config.py src/services/api_credentials.py src/api/routes/config.py --no-error-summary
```

**depends-on:** group-1

---

### Group 3: Runtime autenticado e tokenless monotonicamente inerte

**Goal:** Autenticar os quatro webhooks e encerrar definitivamente processamento tokenless para os
três capability providers.

**Deliverables:**

1. Generalizar parser/digest/resolver exact-one para Z-API, Evolution e Uazapi; capability route só
   reconhece digest já persistido e nunca provisiona/rotaciona implicitamente.
2. Depois do digest exact-one, exigir binding não vazio/exato de `instanceId` (Z-API), `instance`
   (Evolution) ou `instanceName` (Uazapi) antes de rate-limit/replay/sessão/mídia/pipeline.
3. Fazer as rotas tokenless de Z-API, Evolution e Uazapi apenas consumir o body e retornar o
   sentinel inerte, sem resolver tenant/instance nem chamar serviço de evento. Preservar esse
   comportamento em fixtures de rollback.
4. Alterar Meta GET para materializar no máximo dois matches e aceitar somente um. Meta POST lê raw
   bytes uma vez, extrai exatamente um `phone_number_id`, resolve exact-one e valida
   `sha256=<hex>` com o app secret resolvido e `hmac.compare_digest` antes de classificar evento.
5. Provar runtime positivo somente com estado capability produzido pela service harness do Grupo 2;
   não escrever digest diretamente.
6. Parametrizar spies de DB write/flush/commit, Redis/rate-limit/replay, mídia, event service,
   pipeline/LLM, reply/egress/handoff e evento para todos os negativos e rotas tokenless.

**Acceptance Criteria:**

- [ ] Estado autenticado de cada provider alcança o pipeline existente mesmo com card
  `coming_soon`; runtime inbound não consulta catálogo depois da auth e não cria credencial.
- [ ] Capability path sem digest válido não faz fallback por instance, retorna 401 genérico e tem
  zero calls downstream.
- [ ] Zero/múltiplos matches, auth antiga/incorreta e binding divergente têm envelope indistinguível
  e zero calls nos spies.
- [ ] Meta usa raw body, não JSON reserializado; depois da troca candidate, verify token/app secret
  antigos falham e os novos autenticam.
- [ ] Status-only Meta também autentica antes do sentinel; GET ambíguo não elege `.first()`.
- [ ] As três rotas tokenless são inertes sem lookup ou downstream, inclusive sob cenário de backout;
  nenhum teste ou branch preserva processamento tokenless.

**Validation:**

```bash
poetry run pytest -q tests/api/test_webhook_meta.py tests/api/test_webhook_zapi.py tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py tests/api/test_whatsapp_inbound_zero_downstream.py
poetry run mypy src/api/routes/webhook.py --no-error-summary
```

**depends-on:** group-2

---

### Group 4: Redaction application/edge para capability URLs

**Goal:** Impedir que os três segmentos capability atravessem qualquer sink controlado ou contrato
do primeiro edge.

**Deliverables:**

1. Substituir redactors Z-API duplicados por helper único que mapeia qualquer segmento após
   `/v1/webhook/{zapi|evolution|uazapi}/` para `{capability_token}`, inclusive curto, inválido,
   percent-encoded, unmatched e exception fallback.
2. Aplicar template em access/auth/rate-limit logs, exception reporting, labels Prometheus e hooks
   OpenTelemetry; remover `url.path`/`http.target` crus para request capability.
3. Não copiar `X-Original-URI`, `X-Original-Path`, `X-Real-URL`, `X-Rewrite-URL` ou raw target para
   resposta, contexto, logs, traces ou downstream; headers não participam de auth/callback.
4. Ampliar o runbook do primeiro edge aos três templates/sentinels, mantendo configuração externa
   fora deste wish e qualquer hit como no-go.
5. Testar sinks em 401, 404 e 500 com sentinelas distintas; manter Uvicorn access log desabilitado.

**Acceptance Criteria:**

- [ ] Tabela dos três providers passa em RequestId, performance, exception handler e OpenTelemetry;
  somente template aparece em record/label/span.
- [ ] Paths inválidos/unmatched são redigidos fail-closed, sem depender do parser de auth.
- [ ] Original-URI headers não são refletidos/propagados nem alteram callback.
- [ ] Runbook diferencia sentinel de segurança de homologação live de provider.

**Validation:**

```bash
poetry run pytest -q tests/api/test_observability_redaction.py tests/api/test_whatsapp_webhook_configuration.py
poetry run mypy src/api/main.py src/api/middleware/tracing.py src/api/middleware/performance.py --no-error-summary
```

**depends-on:** group-2

---

### Group 5: Evidência latente, regressão e handoff do catálogo

**Goal:** Entregar evidência reproduzível sem confundir mecanismo candidate com adoção pública.

**Deliverables:**

1. Criar matriz pytest com node IDs estáveis por `meta_cloud|zapi|evolution|uazapi` e requisito,
   separando `public_adoption_guard`, `candidate_service`, `authenticated_runtime` e
   `tokenless_inert`.
2. Provar por provider: nova adoção fechada; contrato candidate; inbound autenticado; auth
   negativa/rotacionada; zero/múltiplos; zero-downstream; tokenless inerte; redaction; outbound.
3. Fazer todo fixture de estado novo passar por fake repository/service harness candidate nomeada;
   proibir helper que faça update direto do JSONB/digest.
4. Documentar no handoff node IDs entregues e o gap intencional: 2xx HTTP de PATCH/primeira
   provisão/rotação para clínica nova, criado e reexecutado pelo release unit do catálogo.
5. Provar a matriz pública `1 active + 20 coming_soon`, delta `∅`, e que nenhuma alteração de
   catálogo/frontend ocorreu nesta árvore.
6. Rodar egress e gate completo; nenhum resultado usa “active/homologado” para este wish.

**Acceptance Criteria:**

- [ ] Matriz possui casos distintos por provider/requisito; teste genérico único não serve como
  evidência duplicada.
- [ ] Matriz pública permanece 1/20; PATCH e rotate públicos falham sem efeito/revelação.
- [ ] Candidate service e runtime autenticado passam sem override configurável nem mutation direta
  de digest; os node IDs deixam a fronteira explícita no nome/marker.
- [ ] As três rotas tokenless são inertes e continuam inertes em teste de backout.
- [ ] Handoff exige `tests/api/test_whatsapp_phase_b_public_adoption.py` sem fixture interna no
  candidate do catálogo e reexecução da matriz.
- [ ] Egress dos quatro providers e `make quality` passam, sem skip/xfail ou claim live.

**Validation:**

```bash
poetry run pytest -q tests/api/test_whatsapp_webhook_adoption_gate.py tests/services/test_whatsapp_webhook_provisioning.py tests/api/test_whatsapp_inbound_latent_contract.py tests/api/test_whatsapp_inbound_zero_downstream.py tests/api/test_observability_redaction.py tests/services/test_meta_service_egress.py tests/services/test_zapi_service_egress.py tests/services/test_uazapi_service_egress.py
make quality
```

**depends-on:** group-3, group-4

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] O receipt externo aceito é agregado, corresponde ao deployment, tem issuer/digest/timestamp
  válidos e prova zero/zero sem expor campo proibido.
- [ ] A matriz desta árvore permanece `1 active + 20 coming_soon`; os quatro cards WhatsApp ficam
  `coming_soon`, PATCH/new adoption e rotate público retornam `provider_not_available` sem efeitos.
- [ ] Callback Meta derivado de `PUBLIC_BASE_URL` não muda com Host/Forwarded adversarial, mas sua
  presença não torna provider adotável/ativo.
- [ ] Estado com auth válida continua no pipeline; tenant sem digest/identidade válida recebe
  rejeição sem DB write, Redis, mídia, egress, handoff ou evento.
- [ ] Meta handshake/HMAC e capability routes usam somente credencial atual; token velho falha.
- [ ] Z-API, Evolution e Uazapi tokenless retornam o sentinel inerte, sem resolver tenant/instance e
  sem downstream; simulação de backout preserva essa inércia.
- [ ] QA desta dependência não executa nem aprova clínica nova. QA do catálogo executa o teste HTTP
  público no candidate 16/6, sem snapshot/fixture interna, antes de qualquer promoção.
- [ ] Evidência é descrita como capacidade técnica latente/determinística, nunca homologação live.

## Backout Invariant

Backout pode fechar handler de rotação, callback/status ou exposição futura do catálogo, mas nunca
restaura processamento tokenless. Capability routes autenticadas e sentinels tokenless inertes são
o piso de segurança: qualquer reversão deve ser preparada como patch forward ou revert seletivo que
preserve esse piso. O teste de regressão/backout dos Grupos 3 e 5 é gate obrigatório.

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Census ausente, stale ou do ambiente errado | High | Gate externo exige issuer, timestamp, environment fingerprint e digest antes de qualquer claim. |
| Count positivo invalidar a premissa | High | Parar; exigir reabertura explícita do DESIGN e do wish do catálogo; não desenhar migração aqui. |
| Teste candidate ser confundido com adoção pública | High | Markers/nodes separados; API pública continua fail-closed; catálogo exige HTTP real. |
| Snapshot candidate virar bypass de produção | High | Tipo passado diretamente no domínio; nenhuma fonte env/header/query/body ou endpoint alternativo. |
| Backout reabrir tokenless | High | Inércia é monotônica; revert seletivo/patch forward e teste específico preservam o piso. |
| Duas rotações concorrentes revelarem URLs concorrentes | High | Serializar/CAS; só último digest commitado autentica, provado no service test. |
| Resposta one-time se perder após commit futuro | Medium | Não persistir token; quando ativo, operador/usuário executa nova rotação. |
| Meta status-only escapar de autenticação | High | Resolver phone id/HMAC antes de classificar evento. |
| Duplicata/corrupção eleger tenant errado | High | Exact-one para verify token, phone id e todos os digests. |
| Capability vazar em 404/500/OTel | High | Redactor comum fail-closed e teste de sink por provider. |
| Primeiro proxy/CDN manter raw URI | High | Runbook multi-provider e sentinel no-go; edge externo não é claim deste wish. |
| Catálogo promover sem prova pública | High | Handoff nomeia teste obrigatório e proíbe fixture interna como evidência de clínica nova. |
| Testes serem lidos como homologação | Medium | Copy explícita e OUT sem probe live/vendor receipt. |

## Review Results

### Plan Review — 2026-09-28 — BLOCKED (fix loop 1)

- **Target SHA-256:** `6efd9ef61623e6910a0f3cdd46acbbecfdea7d257bcf0d3dc88f0b0841f31676`
- **Gap:** o plano exigia 2xx público de PATCH/primeira provisão/rotação por clínica nova enquanto o
  DESIGN mantinha os providers WhatsApp indisponíveis.
- **Proposed resolution at that revision:** separar runtime, mecanismo candidate e adoção pública.
  A revisão seguinte, contudo, adicionou uma exceção de migração não aprovada e permaneceu bloqueada.

### Plan Re-review — 2026-09-28 — BLOCKED (fix loop 2 input)

- **Target SHA-256:** `4496880dc98c0b96cd6c5a8e8d61f3f1f475abf0fe3ea3c3d8945fac745b18b0`
- **Gaps:** a revisão anterior reabriu silenciosamente o DESIGN ao introduzir uma exceção de rotate
  para incumbents, manter Evolution/Uazapi tokenless processando eventos e planejar uma migração;
  também tratou `1 active + 21 coming_soon` como input desta árvore e não impôs census externo como
  gate duro antes da execução.
- **Resolution proposed in this revision:** gate externo agregado zero/zero; com zero, execução exata
  do DESIGN, rotate público sempre fechado no estado atual e tokenless monotonicamente inerte; com
  count positivo, parada e reabertura explícita do DESIGN/catálogo; matriz desta árvore corrigida
  para `1 active + 20 coming_soon`, delta `∅`.
- **Residual blocker:** nenhum `artifact_uri`, issuer, `observed_at`, environment fingerprint ou
  `payload_sha256` aceito foi fornecido. Status permanece `DRAFT`, `Execution: BLOCKED`, nenhum grupo
  pode rodar e esta resolução requer re-review independente do novo digest.

### Final Plan Re-review — 2026-09-29T02:02:32Z — BLOCKED on external gate

- **Target SHA-256:** `af1d95f77e627f18264fe14678403eff48b410961801ceecd4c338bc9f80c299`
- **Reviewer:** `/root/whatsapp_wish_review`
- **Internal gaps:** nenhum CRITICAL/HIGH/MEDIUM/LOW; a exceção incompatível foi removida, backout
  preserva inércia tokenless, a árvore mantém `1 active + 20 coming_soon` e a primeira frase de
  Problem é normativa e testável.
- **Residual HIGH externo:** falta o receipt imutável e verificável com counts agregados
  `{evolution: 0, uazapi: 0}`. Ausência, staleness, divergência ou count positivo mantém a execução
  bloqueada e não autoriza correção especulativa no repositório.
- **Outcome:** o plano está internamente pronto, mas permanece `DRAFT`, `Execution: BLOCKED`. Após o
  receipt ser produzido e validado, sua referência deve ser registrada e o novo digest submetido a
  revisão independente antes de qualquer grupo.

## Files to Create/Modify

```text
src/config/settings.py
src/api/schemas/config.py
src/services/api_credentials.py
src/api/routes/config.py
src/api/routes/webhook.py
src/api/main.py
src/api/middleware/tracing.py
src/api/middleware/performance.py
env.example
docs/configuration.md
docs/api-provider-settings-rollout.md
tests/api/test_whatsapp_webhook_configuration.py
tests/api/test_whatsapp_webhook_adoption_gate.py
tests/services/test_whatsapp_webhook_provisioning.py
tests/api/test_whatsapp_inbound_zero_downstream.py
tests/api/test_whatsapp_inbound_latent_contract.py
tests/api/test_meta_credentials.py
tests/api/test_uazapi_credentials.py
tests/api/test_config_credentials_boundary.py
tests/api/test_api_credentials.py
tests/api/test_webhook_meta.py
tests/api/test_webhook_zapi.py
tests/api/test_webhook_evolution.py
tests/api/test_webhook_uazapi.py
tests/api/test_observability_redaction.py
```

O census externo não cria nem modifica arquivo de produto neste repositório.
