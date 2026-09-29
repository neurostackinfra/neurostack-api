# Wish: Catálogo, ordem e disponibilidade pública das APIs

| Field | Value |
|-------|-------|
| **Status** | APPROVED |
| **Execution** | BLOCKED — aguardando census WhatsApp e contrato de áudio NeuraLake |
| **Slug** | `api-provider-catalog-presentation` |
| **Date** | 2026-09-28 |
| **Author** | Lorenzo |
| **Appetite** | medium |
| **Branch** | `wish/api-provider-catalog-presentation` |
| **Repos touched** | neurostack-api |
| **Design** | [DESIGN.md](../../brainstorms/api-provider-catalog-presentation/DESIGN.md) |

## Summary

Corrige a aba APIs para que `active` signifique integração publicamente configurável e executável
de ponta a ponta, sem alegar homologação live. O trabalho separa catálogo público, IDs reconhecidos
e adapters operacionais; integra as dependências quando concluídas e conformes; aplica os nomes e a
ordem do produto; e só promove os 16 pares finais depois de evidência determinística, inbound WhatsApp seguro,
assurance de credenciais e gates de rollout.

## Scope

### IN

- Declarar `PUBLIC_PROVIDER_AVAILABILITY`, `LEGACY_PROVIDER_IDS`,
  `RECOGNIZED_PROVIDER_IDS`, `OPERATIONAL_PROVIDER_IDS` e `ACTIVE_PROVIDER_PAIRS`, com os invariantes
  do design. `multimodal` permanece a chave canônica; Fallback é apenas seu label de UI.
- Integrar os contratos finais dos wishes dependentes para WhatsApp inbound, Scribe/OpenRouter STT,
  Dropout multimodal e OpenAI TTS, sem reimplementar seus adapters neste wish.
- Adicionar os IDs explícitos `llm/openrouter` e `stt/openrouter`; manter `llm/outros` e `stt/outros`
  como wire legado oculto, somente para leitura/runtime/eco sem mudança e para a única saída
  incumbent-only, atômica, rumo a provider explícito operacional com bundle fresco completo.
- Implementar `llm/openrouter` sobre o cliente OpenAI-compatible existente, com base canônica,
  egress kind próprio e model ID livre, inclusive `anthropic/...` via OpenRouter.
- Aplicar a mesma política canônica provider↔endpoint no PATCH e nos factories/runtimes; bases
  hospedadas são exatas e Evolution/Uazapi aceitam apenas HTTPS público na porta 443 com path prefix.
- Rejeitar query, userinfo, fragmento, porta insegura e endpoint operacional completo nas bases;
  exigir census agregado igual a zero para bundles legados inseguros antes da ativação.
- Generalizar troca atômica de provider: um bundle fresco e completo precisa validar antes de
  persistência, cache ou rede; nenhuma credencial do provider anterior pode ser herdada.
- Emitir telemetria operacional `api_credentials_changed` at-most-once depois do commit e antes da
  resposta, com payload allowlisted, contador/alerta de falha e sem hostname, URL, modelo ou segredo.
- Definir `COMMON_EVIDENCE_REQUIREMENTS` e `EVIDENCE_REQUIREMENTS_BY_CAPABILITY`; derivar cada
  obrigação dos pares ativos e vincular provas ao commit integrado, source digests e node IDs
  pytest coletados/aprovados.
- Tratar manifests como dados: eles nunca escolhem executável, flags, cwd, shell ou comandos; o
  checker possui argv fixo e rejeita evidência ausente, extra, duplicada-only, skipped, xfailed,
  não coletada ou falha.
- Aplicar a ordem WhatsApp → LLM → Fallback → Módulo de voz → STT → TTS → Gatilhos de
  voz, com os títulos/bylines exatos do design e sem subtítulo nos cards WhatsApp.
- Oferecer a UX entregue pela dependência WhatsApp para copiar callback fixo e provisionar/rotacionar
  URLs capability; manter cards fechados enquanto o ciclo inbound/outbound não estiver provado.
- Executar rollout em duas unidades: Fase A de compatibilidade com novas adoções fechadas e Fase B
  de ativação conjunta backend/frontend; backout da B preserva readers/factories da A.
- Validar um artefato imutável e recente de proteção em repouso, ou manter a Fase B bloqueada por
  `api-credentials-at-rest-protection`.
- Preservar os recibos e validadores históricos de `api-provider-availability` byte a byte; a nova
  definição técnica não reescreve a história de homologação Gemini.

### OUT

- Implementar o ciclo inbound autenticado dos quatro providers WhatsApp, os adapters Scribe e
  OpenRouter STT, Dropout fallback ou OpenAI TTS; esses trabalhos pertencem aos wishes dependentes.
- Executar probes live ou afirmar que qualquer provider foi homologado/validado externamente.
- Criar provider Claude/Anthropic direto, provider arbitrário ou catálogo dinâmico de plugins.
- Implementar envelope encryption neste wish; se a assurance falhar, a remediação é
  `api-credentials-at-rest-protection`.
- Criar auditoria durável/exactly-once, outbox, tabela, migration de schema/tabela, endpoint de catálogo, cache,
  fila, health-check ou terceiro estado público.
- Registrar webhooks automaticamente nos vendors ou alterar a semântica dos gatilhos de voz.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | `active` exige configuração pública e runtime provado, não probe live | Disponibilidade técnica não é homologação externa. |
| 2 | Catálogo, wire reconhecido e operacionalidade são domínios separados | O legado precisa continuar legível sem reaparecer ou virar adotável. |
| 3 | Fallback usa a chave backend `multimodal` | Evita divergência entre schema, runtime, checker e UI. |
| 4 | WhatsApp só promove após outbound e inbound autenticado/provisionável | A capacidade promete receber e enviar. |
| 5 | Todo par ativo cobre um vocabulário fechado de provas positivas e negativas | Membership em allowlist não prova execução. |
| 6 | Hosted providers persistem base canônica exata | Host pinning isolado ainda permite path quebrado. |
| 7 | Legado inseguro precisa ser zero antes da Fase B | Não haverá exceção de runtime que preserve query perigosa. |
| 8 | Telemetria é at-most-once, não auditoria durável | Exactly-once exigiria storage/outbox fora do escopo. |
| 9 | Manifestos são somente dados | Impede command injection por evidência controlável. |
| 10 | Fases A e B são release units separadas | Backout de ativação não pode remover readers de IDs persistidos. |
| 11 | Claude permanece via model ID do OpenRouter | Atende o uso sem novo protocolo ou segredo Anthropic. |
| 12 | Permanecem somente `active` e `coming_soon` | Homologação e assurance são gates, não novos estados públicos. |

## Simplicity Case

- **Simplest complete design:** manter JSONB, PATCH e matriz declarativa existentes; separar seus
  três domínios, integrar adapters concluídos, aplicar a apresentação e validar cada par derivado.
- **Added machinery:** taxonomia/registro de evidência, resolvedor canônico, census agregado,
  telemetria redigida e duas release units. Cada item fecha um gate atual de prova, egress,
  compatibilidade, atribuição ou rollback.
- **Deferred until measured:** Anthropic direto só com BYOK/contrato direto; catálogo dinâmico só
  com provider incompatível com os transports existentes; outbox somente se auditoria durável for
  requisito; envelope encryption somente se a assurance não satisfizer o gate.
- **Complexity removed:** nenhum terceiro estado, endpoint de catálogo, migration de schema/tabela, auto-discovery,
  auto-registro de webhook, health-check, probe live obrigatório ou comando livre em manifesto.

## Dependencies

**depends-on:** whatsapp-inbound-provider-activation, stt-openrouter-scribe-activation, dropout-multimodal-fallback, tts-openai-provider
**blocks:** none

Os quatro slugs existem e possuem contratos explícitos. `tts-openai-provider` está `SHIPPED`: o PR
#40 foi mergeado em `be0a6547c6c7462c1cbeeeb39030a7d10ee781b8`, e execution review, delta review
e final review retornaram `SHIP`. `stt-openrouter-scribe-activation` está `SHIPPED`: o PR #43 foi
mergeado em `acf7586c91dd2101129da202d4e9b4c2b4c3ca5b`, execution review e hardening re-review
retornaram `SHIP`, e o smoke pós-merge passou 569 testes mantendo `Δ public = ∅`.
`dropout-multimodal-fallback` permanece `DRAFT/BLOCKED` até existir contrato autoritativo e
versionado da NeuraLake para áudio OGG/Opus bruto; `whatsapp-inbound-provider-activation` permanece
`DRAFT/BLOCKED` até receipt externo agregado e verificável provar counts zero de incumbents
Evolution/Uazapi sem capability digest. Nenhum desses dois blockers pode ser satisfeito por mocks,
suposição ou alteração neste wish.

O DESIGN governante está liberado pelo `SHIP` oficial do reviewed-content digest
`025c397a0af4335e5266a3c19230d8a8a0ef405b818991a02ee4d1a9adc529f3`. Ainda assim, **o work dos
grupos deste catálogo permanece bloqueado** até as quatro dependências estarem `SHIPPED` e terem
execution review independente `SHIP`; executar uma dependência aprovada de delta público vazio não
equivale a iniciar os grupos deste wish. Se uma dependência alterar qualquer ID, campo, rota ou
invariante nomeado neste WISH/design, este WISH retorna automaticamente a `DRAFT`, o work para e o
plano passa por nova review antes de qualquer dispatch ou continuidade.

### Pre-work dependency gate

Antes de checkout ou dispatch de qualquer grupo, o orquestrador valida somente artefatos que cada
dependência já possui: status/review, commit/tree digest e os comandos de teste **fixos e
versionados pela própria dependência**. O gate registra em comentário/review externo imutável o URI,
SHA-256, issuer, subject commit e horário observado; não cria JSON no repositório e não chama o
checker deste wish, que ainda não existe. A inspeção confirma diretamente no contrato SHIPPED os
IDs, campos, rotas, invariantes e provas exigidos pelo design: WhatsApp por provider, Dropout texto e
áudio bruto, Scribe/OpenRouter separados e OpenAI TTS determinístico. Comando vindo de manifesto,
teste agregado sem atribuição e qualquer divergência bloqueiam o trabalho.

Depois que o checker for criado no Grupo 5, ele revalida na árvore integrada o conjunto completo de
provas. A eventual dependência `api-credentials-at-rest-protection` é condicional: bloqueia apenas a
ativação da Fase B quando a assurance estiver ausente, vencida ou vinculada a outro ambiente.

## Success Criteria

- [ ] A sequência renderizada é WhatsApp, LLM, Fallback, Módulo de voz, STT, TTS e Gatilhos de voz.
- [ ] Ordem, títulos e bylines correspondem exatamente ao design; WhatsApp não tem subtítulos e
  Fallback/TTS não citam nomes promocionais de modelos.
- [ ] O catálogo final possui 16 pares `active` (4 WhatsApp, 4 LLM, 2 multimodal, 3 STT, 3 TTS) e
  seis `coming_soon` (360dialog, Twilio, Cortex, Deepgram, Azure e Cloud STT).
- [ ] TypeScript equivale ao catálogo público; Literals equivalem a `public ∪ legacy`; `active ⊆
  operational ⊆ recognized`; o legado não aparece no grid.
- [ ] `Fallback` deriva exclusivamente de `PUBLIC_PROVIDER_AVAILABILITY["multimodal"]`.
- [ ] Todo par ativo abre o editor, aceita bundle completo e valida provider↔endpoint no PATCH e
  novamente no factory antes da rede.
- [ ] Todo par `coming_soon` permanece visível, inerte e rejeitado atomicamente pelo PATCH para nova
  adoção; somente um incumbent já em `llm/outros`/`stt/outros` pode sair na Fase A para provider
  explícito operacional, com bundle fresco completo e sem entrada/reentrada no legado.
- [ ] `llm/openrouter` e `stt/openrouter` são explícitos; `outros` legado aceita somente
  GET/runtime/eco idêntico e saída por troca atômica com bundle fresco.
- [ ] Meta, Z-API, Evolution e Uazapi só ficam ativos depois das provas outbound/inbound da
  dependência; callback/capability nunca aparece cru em logs, traces, métricas ou headers.
- [ ] OpenRouter e Dropout multimodal provam fallback textual e áudio WhatsApp bruto byte a byte.
- [ ] Cada hosted provider persiste a base exata do design; self-hosted aceita apenas HTTPS público
  na porta 443 com path prefix e DNS/IP pinado; nenhuma base aceita query/userinfo/fragmento.
- [ ] O census agregado anterior à Fase B prova `unsafe_legacy_count == 0` sem emitir tenant, URL,
  host ou segredo; `new_explicit_id_count > 0` não bloqueia B e somente proíbe rollback anterior à A.
- [ ] Mudança efetiva tenta no máximo um `api_credentials_changed` pós-commit; no-op/4xx/rollback não
  emitem, falha incrementa contador/alerta e nenhuma sentinela secreta cruza a fronteira.
- [ ] O checker deriva todas as obrigações dos pares ativos e rejeita chaves/nodes ausentes, extras,
  duplicate-only, uncollected, skipped, xfailed, falhos ou vinculados a source/tree digest diferente.
- [ ] Manifestos nunca controlam executável, argumentos, cwd ou shell; o argv de coleta/execução é
  fixo no checker/CI.
- [ ] A release Fase A congela a matriz em `1 active + 21 coming_soon`; a Fase B promove
  backend/frontend juntos para `16 active + 6 coming_soon`; backout B preserva A e rollback anterior
  à A exige `new_explicit_id_count == 0`.
- [ ] A Fase B referencia assurance imutável com até 90 dias, environment/deployment fingerprint,
  owner/approver, DB/backup encryption, key owner/rotation, least privilege e restore rehearsal; se
  não, `api-credentials-at-rest-protection` precisa estar `SHIPPED` e revisado `SHIP`.
- [ ] Claude é configurável pelo model ID do OpenRouter sem provider Anthropic direto.
- [ ] Nenhum artefato afirma homologação live não realizada, e o gate histórico Gemini permanece
  byte a byte intacto.
- [ ] Não há migration de schema/tabela, endpoint de catálogo, terceiro estado ou mudança funcional
  nos gatilhos.

## Execution Strategy

A Fase A contém os Grupos 1–5 e congela a matriz pública em `1 active + 21 coming_soon`; a Fase B contém
somente o Grupo 6. Grupos 1–4 produzem código/testes delimitados; o Grupo 5 integra o checker e o
pacote de release A. Entre 5 e 6 há um checkpoint operacional explícito, com pausa obrigatória para
autorização e deploy externos. Aprovação do plano, checkout ou teste local não concedem autoridade
de deployment.

**Producers e freshness de evidência:** jobs separados do CI/orchestrator são os únicos produtores:
o release CI produz runtime e census; o security orchestrator produz assurance a partir da aprovação
do security owner; o deployment orchestrator registra a ação autorizada do release operator.
Reviewers são verificadores read-only e nunca assinam, emitem ou publicam attestations. Runtime,
census e deployment expiram exatamente 24 horas após `observed_at`; assurance expira exatamente 90
dias após `observed_at`; `observed_at` mais de cinco minutos no futuro é inválido. Nova coleta de
census ocorre imediatamente antes da Fase B. Todos os outputs são externos e imutáveis.

## Execution Groups

### Group 1: Domínios de provider, legado e atomicidade — Fase A

**Goal:** Separar catálogo, IDs reconhecidos e operacionalidade, preservando legado seguro e troca
atômica sem abrir adoção nova.

**Deliverables:**

1. Declarar os mapas/invariantes canônicos e manter `multimodal` como única chave de Fallback.
2. Reconhecer IDs explícitos e preservar `outros` em GET/runtime/eco, sem grid, entrada ou reentrada;
   permitir somente ao incumbent já em `outros` sair atomicamente para provider explícito operacional.
3. Validar bundle fresco completo antes de DB/cache/rede e nunca herdar campos do provider anterior.
4. Manter cards e nova adoção fechados na Fase A. A saída incumbent-only exige bundle fresco completo
   e mesma política de endpoint, pode elevar `new_explicit_id_count`, mas nunca
   `unsafe_legacy_count`; não é migration de schema/tabela.

**Acceptance Criteria:**

- [ ] `public ∩ legacy = ∅`, `recognized = public ∪ legacy`, `Literal = recognized` e
  `active ⊆ operational ⊆ recognized` passam sem chave `fallback` paralela.
- [ ] Leitura/runtime legado seguro continuam; entrada/reentrada e manutenção insegura falham; apenas
  a saída incumbent-only para provider explícito operacional é aceita atomicamente.
- [ ] Switch incompleto falha antes de efeitos e não herda segredo, URL, modelo, voice ou instance.
- [ ] Fase A mantém `1 active + 21 coming_soon`, rejeita nova adoção/base insegura e garante que
  `unsafe_legacy_count` não cresça; `new_explicit_id_count` pode crescer somente pela saída permitida.

**Validation:**

```bash
poetry run pytest -q tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py
poetry run mypy src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py --no-error-summary
```

**depends-on:** whatsapp-inbound-provider-activation, stt-openrouter-scribe-activation, dropout-multimodal-fallback, tts-openai-provider

---

### Group 2: Endpoint policy e OpenRouter — Fase A

**Goal:** Aplicar a tabela canônica do design no PATCH e runtime e integrar LLM OpenRouter pelo
transporte existente, sem promoção pública.

**Deliverables:**

1. Implementar um resolvedor provider↔endpoint compartilhado: bases hosted exatas; Evolution/Uazapi
   HTTPS público porta 443 com path prefix e DNS/IP pinado; sem query/userinfo/fragmento.
2. Revalidar no factory antes da rede e rejeitar endpoint operacional completo nas bases.
3. Integrar `llm/openrouter` com `https://openrouter.ai/api/v1`, egress kind próprio e model ID livre,
   inclusive `anthropic/...`, sem provider Anthropic direto.
4. Cobrir Scribe, STT OpenRouter, Dropout multimodal e OpenAI TTS vindos das dependências sem
   reimplementar seus adapters.

**Acceptance Criteria:**

- [ ] Toda base hosted persiste exatamente a base do design e todo ataque de endpoint falha pre-network.
- [ ] PATCH e factory aplicam o mesmo resolvedor; self-hosted preserva somente path prefix permitido.
- [ ] LLM OpenRouter usa o transporte existente, base/kind corretos e passthrough do model ID.
- [ ] Nenhum par novo muda para `active` neste grupo.

**Validation:**

```bash
poetry run pytest -q tests/llm/test_client.py tests/services/test_safe_egress.py tests/services/test_multimodal_transcription.py tests/services/test_tts_service.py
poetry run mypy src/llm/client.py src/services/safe_egress.py src/services/multimodal_service.py src/services/tts_service.py --no-error-summary
```

**depends-on:** group-1

---

### Group 3: Telemetria e alerta executável — Fase A

**Goal:** Entregar `api_credentials_changed` at-most-once e tornar sua falha operacionalmente
detectável, sem prometer auditoria durável.

**Deliverables:**

1. Emitir depois do commit e antes da resposta somente em mudança efetiva, com payload allowlisted;
   4xx, no-op e rollback não emitem, e a janela de crash fica documentada.
2. Incrementar o Counter Prometheus `api_credentials_change_emit_failures_total` a cada falha.
3. Adicionar a regra `ApiCredentialsChangeEmissionFailures` no arquivo autoritativo montado pelo
   Docker Compose, `config/prometheus_rules.yml`, e manter o espelho byte a byte em
   `src/config/prometheus_rules.yml`: `increase(api_credentials_change_emit_failures_total[15m]) > 0`
   por `5m`, severity `warning`, owner `platform-sre` e
   `runbook_url=https://docs.neurostack.io/runbooks/api-credentials-change-emission-failures`.
4. Testar payload/redaction, cardinalidade do Counter e, em Python, o parse YAML e o contrato exato
   de expressão/labels/anotações; provar paridade byte a byte entre as duas regras. A seção de
   triagem/backout em `docs/api-provider-settings-rollout.md` é a fonte publicada no `runbook_url`.

**Acceptance Criteria:**

- [ ] Há no máximo uma tentativa pós-commit; nenhuma sentinela secreta alcança evento, erro, log ou métrica.
- [ ] Toda falha incrementa exatamente uma vez o Counter e satisfaz a regra após o threshold/window.
- [ ] Regra principal e espelho são idênticos, válidos e têm owner/runbook executáveis.
- [ ] A documentação chama o evento de telemetria at-most-once, nunca auditoria/exactly-once.

**Validation:**

```bash
poetry run pytest -q tests/services/test_api_credentials.py tests/observability/test_api_credentials_change_alert.py
cmp config/prometheus_rules.yml src/config/prometheus_rules.yml
```

**depends-on:** group-1

---

### Group 4: Catálogo e UX frontend — Fase A

**Goal:** Renderizar hierarquia, copy e provisionamento finais, mantendo todos os candidatos novos
fechados até a ativação conjunta.

**Deliverables:**

1. Espelhar tipos/matriz backend e renderizar WhatsApp → LLM → Fallback → Módulo de voz → STT → TTS
   → Gatilhos de voz com títulos/bylines exatos; Fallback lê somente `multimodal`.
2. Omitir subtítulos WhatsApp, remover marketing de Fallback/TTS e preservar gatilhos de voz.
3. Integrar callback Meta e provisionamento/rotação one-time dos capability URLs sem usar Host/browser.
4. Ocultar `outros`, avisar bundle legado sem relabeling e manter na Fase A exatamente
   `1 active + 21 coming_soon`, todos os candidatos fechados visíveis/inertes.

**Acceptance Criteria:**

- [ ] DOM/constantes preservam ordem exata em PT/EN sem `.sort()` e mantêm na Fase A exatamente
  `1 active + 21 coming_soon`.
- [ ] WhatsApp mostra só títulos; bylines e copy seguem o design, sem nomes promocionais proibidos.
- [ ] Bundle legado não aparece no grid; não aceita entrada/reentrada e sua única alteração permitida
  é a saída incumbent-only por troca atômica fresca para provider explícito operacional.
- [ ] Callback/capability seguem revelação e rotação do contrato; todos os candidatos ficam inertes.

**Validation:**

```bash
cd frontend/artifacts/neurostack
pnpm exec vitest run src/pages/clinic-config-api-settings.test.tsx src/pages/clinic-config-multimodal.test.tsx src/components/api-settings/provider-credential-card.test.tsx src/components/api-settings/voice-response-controls.test.tsx
pnpm run typecheck
pnpm run build
```

**depends-on:** group-1, group-2

---

### Group 5: Checker, evidência e pacote de release A

**Goal:** Integrar a release unit A ainda fechada e produzir prova reproduzível sobre um subject
commit/image imutável, sem attestations autorreferentes no repositório.

**Deliverables:**

1. Implementar `COMMON_EVIDENCE_REQUIREMENTS` e `EVIDENCE_REQUIREMENTS_BY_CAPABILITY`; gerar/validar
   markers estáveis de requirement no código, registrá-los em `pyproject.toml` para
   `--strict-markers` e manter a mecânica do registro par × requirement exigido pelo design.
2. O checker chama somente argv fixo de suite root/marker, por exemplo
   `python -m pytest --collect-only -q tests` e `python -m pytest -q tests -m provider_catalog_evidence`;
   node IDs jamais são interpolados como argumento pytest/shell. Ele captura o conjunto completo
   coletado e aprovado e então o compara ao mapping esperado, rejeitando missing/extra,
   duplicate-only, skipped, xfailed, falho, não coletado, source/tree/dependency drift.
3. Nesta Fase A, rodar o checker apenas contra os pares `active` do subject corrente e provar a
   mecânica da futura matriz 16/6 com fixtures que exercitam missing/extra/duplicate-only e todos os
   status pytest; nunca alegar que pares ainda `coming_soon` já têm runtime aprovado.
4. Implementar `collect-census`, `attest`, `verify` e `publish` em
   `scripts/validate_api_provider_catalog.py`. Cada subcommand possui argv/flags fechados; manifesto,
   node ID e claims nunca escolhem executável, flags, cwd ou shell. `attest --kind runtime` confere
   HEAD/image com o subject informado e executa internamente a coleta/suíte fixa antes de assinar;
   não consome um arquivo de resultado runtime fornecido pelo chamador.
5. Manter `verify` como verificador criptográfico e de kind, mas exigir, sem defaults, os valores
   externos esperados `--expected-subject-commit`, `--expected-image-digest`,
   `--expected-environment`, `--expected-deployment-fingerprint` e `--expected-issuer`, comparando
   cada um fail-closed ao envelope assinado. Para kind `deployment`, exigir ainda
   `--expected-authorization` em enum fechado, incluindo somente `produce_candidate_b` neste fluxo.
   Esses valores vêm da configuração imutável do CI/orchestrator; manifesto, claims e qualquer campo
   da própria attestation jamais controlam argv, flags ou shell.
6. Reusar o protocolo de `scripts/api_provider_preflight.py` sem alterar esse arquivo: schema fechado,
   JSON canônico e assinatura Ed25519 sobre domain separator
   `neurostack/api-provider-catalog/v1/{kind}/{role}/{environment}`. Chaves são separadas por
   role/environment; trust roots públicos e chaves privadas são injetados pelo secret manager. HMAC
   é proibido para attestations (o HMAC Meta inbound é um contrato independente).
7. Assinar os kinds fechados `runtime`, `census`, `assurance` e `deployment`. Todos carregam `kind`,
   `uri`, `digest`, `issuer`, `key_id`, `signature`, `subject_commit`, `image_digest`, `environment`,
   `deployment_fingerprint`, `observed_at`, `expires_at` e claims fechados do kind. `digest` é o
   SHA-256 do payload canônico de claims/subject, sem URI/digest/signature; o URI content-addressed é
   derivado desse digest, e a assinatura cobre o envelope fechado com URI+digest, evitando auto-hash.
   Runtime contém collected/passed set, mapping e source/dependency digests; census contém somente
   contagens agregadas; assurance contém controles/aprovação; deployment contém autorização,
   operator, image implantada, smoke e backout check.
8. Conter staging/store em roots externos explicitamente configurados: rejeitar traversal ou path
   fora do root, symlink em qualquer componente, permissões group/world em chave/staging e qualquer
   store dentro do worktree. `publish` usa criação exclusiva `O_EXCL`, nunca sobrescreve e verifica
   digest+assinatura antes e depois da gravação no store externo content-addressed.
9. Depois de pinado o subject da Fase A, o release CI executa checker/census e produz/publishes
   attestations `runtime` e `census`; reviewers apenas chamam `verify`. Em docs/review posterior
   persistem somente as referências externas verificadas. O census exige
   `unsafe_legacy_count == 0`; `new_explicit_id_count` pode ser maior que zero pela saída incumbent-only.
10. Produzir image/commit imutável da release A, `1 active + 21 coming_soon`, runbook e review `SHIP`,
   preservando byte a byte a evidência histórica Gemini e sem probe/homologação live.

**Acceptance Criteria:**

- [ ] O checker da Fase A deriva exatamente `active` corrente × requisito; fixtures provam a
  derivação/mecânica da matriz 16/6 sem transformar candidatos fechados em evidência aprovada.
- [ ] Markers estáveis estão registrados em `pyproject.toml` e passam com `--strict-markers`.
- [ ] O checker usa argv fixo de raiz/marker, nunca node ID/manifesto como argv, shell, cwd ou flags.
- [ ] Protocolos/kinds têm schemas fechados, JSON canônico, Ed25519 com domain/role/environment
  corretos, TTL exato e trust roots do secret manager; reviewers nunca produzem evidência.
- [ ] `verify` exige e compara fail-closed subject commit, image digest, environment, deployment
  fingerprint e issuer externos; `deployment` exige também autorização no enum fechado. Testes
  usam attestations Ed25519 validamente assinadas e rejeitam separadamente divergências de subject,
  image, environment, fingerprint, issuer e autorização.
- [ ] Testes rejeitam tamper, digest/signature divergente, domain/key/role/environment/subject errado,
  stale, `observed_at` futuro >5min, symlink/traversal/outside-root, store dentro do worktree, mode
  aberto, overwrite e campos proibidos de tenant, URL, host, query, path, segredo, token ou credencial bruta.
- [ ] Attestation é posterior aos testes do subject pinado, imutável, externo e publicada com
  `O_EXCL`; o repositório não contém attestation autorreferente.
- [ ] Release A mantém `1 active + 21 coming_soon` e `unsafe_legacy_count == 0`;
  `new_explicit_id_count > 0` somente proíbe rollback anterior à A; históricos permanecem intactos.
- [ ] Gates focados/agregados passam e nenhum resultado afirma homologação live.

**Validation:**

```bash
: "${CATALOG_SUBJECT_COMMIT:?}" "${CATALOG_IMAGE_DIGEST:?}" "${CATALOG_ENVIRONMENT:?}" "${CATALOG_DEPLOYMENT_FINGERPRINT:?}"
: "${CATALOG_EVIDENCE_STAGING_ROOT:?}" "${CATALOG_EVIDENCE_STORE_ROOT:?}" "${CATALOG_EVIDENCE_BASE_URI:?}"
: "${CATALOG_CENSUS_DSN_FILE:?}" "${CATALOG_RUNTIME_SIGNING_KEY_FILE:?}" "${CATALOG_CENSUS_SIGNING_KEY_FILE:?}" "${CATALOG_ATTEST_TRUST_ROOTS_FILE:?}"
: "${CATALOG_RUNTIME_ISSUER:?}" "${CATALOG_CENSUS_ISSUER:?}"
poetry run pytest -q tests/scripts/test_validate_api_provider_catalog.py
poetry run python scripts/validate_api_provider_catalog.py collect-census --subject-commit "$CATALOG_SUBJECT_COMMIT" --image-digest "$CATALOG_IMAGE_DIGEST" --environment "$CATALOG_ENVIRONMENT" --deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --output "$CATALOG_EVIDENCE_STAGING_ROOT/census-claims.json"
poetry run python scripts/validate_api_provider_catalog.py attest --kind runtime --subject-commit "$CATALOG_SUBJECT_COMMIT" --image-digest "$CATALOG_IMAGE_DIGEST" --environment "$CATALOG_ENVIRONMENT" --deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --base-uri "$CATALOG_EVIDENCE_BASE_URI" --key-file "$CATALOG_RUNTIME_SIGNING_KEY_FILE" --output "$CATALOG_EVIDENCE_STAGING_ROOT/runtime.attestation.json"
poetry run python scripts/validate_api_provider_catalog.py attest --kind census --claims "$CATALOG_EVIDENCE_STAGING_ROOT/census-claims.json" --base-uri "$CATALOG_EVIDENCE_BASE_URI" --key-file "$CATALOG_CENSUS_SIGNING_KEY_FILE" --output "$CATALOG_EVIDENCE_STAGING_ROOT/census.attestation.json"
poetry run python scripts/validate_api_provider_catalog.py verify --kind runtime --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/runtime.attestation.json" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_SUBJECT_COMMIT" --expected-image-digest "$CATALOG_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_RUNTIME_ISSUER"
poetry run python scripts/validate_api_provider_catalog.py verify --kind census --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/census.attestation.json" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_SUBJECT_COMMIT" --expected-image-digest "$CATALOG_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_CENSUS_ISSUER"
poetry run python scripts/validate_api_provider_catalog.py publish --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/runtime.attestation.json" --store-root "$CATALOG_EVIDENCE_STORE_ROOT"
poetry run python scripts/validate_api_provider_catalog.py publish --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/census.attestation.json" --store-root "$CATALOG_EVIDENCE_STORE_ROOT"
make quality
cd frontend && pnpm --filter @workspace/neurostack run test && pnpm --filter @workspace/neurostack run build
```

**depends-on:** group-2, group-3, group-4

---

### Operational Checkpoint A — autorização externa obrigatória

Após o Grupo 5, a execução pausa. Somente um **release operator autorizado** pode executar o
checkpoint; agentes/reviewers não recebem autoridade implícita de deploy.

- **Inputs exatos:** image digest/subject commit imutável da Fase A; reviews `SHIP` dos Grupos 1–5 e
  dependências; attestation externa do checker limitada aos pares `active` correntes; assurance
  externa emitida por Security/Platform e aprovada pelo security owner; census A; runbook de
  deploy/backout.
- **Procedimento:** verificar assinatura/digest e comparar fail-closed subject commit, image digest,
  environment, deployment fingerprint e issuer vindos da configuração imutável do orchestrator;
  assurance expira exatamente 90 dias e cobre DB/backups, key owner/rotation, least privilege e restore rehearsal; implantar a image
  imutável da Fase A no environment alvo; confirmar exatamente `1 active + 21 coming_soon`; executar
  smoke determinístico e census agregado pós-deploy sem tenant/URL/host/segredo. O security
  orchestrator assina/publishes `assurance`; depois da ação do operador, o deployment orchestrator
  assina/publishes `deployment`. Reviewers somente executam `verify`.
- **Output externo imutável:** URI, SHA-256, issuer/release operator, subject commit/image digest,
  `observed_at`, `expires_at`, environment, deployment fingerprint, autorização para produzir o
  candidate B e resultado do backout check. O deploy/census expiram em 24 horas para dispatch do
  Grupo 6; a assurance em 90 dias. O checkpoint não autoriza ativação nem deploy do candidate B.

Ausência, expiração, mismatch ou `unsafe_legacy_count != 0` bloqueia. Um
`new_explicit_id_count > 0` é permitido e apenas proíbe rollback anterior à Fase A. Se a assurance falhar,
`api-credentials-at-rest-protection` deve estar `SHIPPED` e revisado `SHIP`. Um docs/review posterior
pode registrar apenas os campos de referência acima. O Grupo 6 não começa até o operador fornecer a
referência de deploy e autorização explícita.

**Procedure commands:**

```bash
: "${CATALOG_PHASE_A_COMMIT:?}" "${CATALOG_PHASE_A_IMAGE_DIGEST:?}" "${CATALOG_ENVIRONMENT:?}" "${CATALOG_DEPLOYMENT_FINGERPRINT:?}"
: "${CATALOG_CENSUS_DSN_FILE:?}" "${CATALOG_RUNTIME_ATTESTATION:?}" "${CATALOG_CENSUS_ATTESTATION:?}" "${CATALOG_CENSUS_SIGNING_KEY_FILE:?}"
: "${CATALOG_ASSURANCE_CLAIMS:?}" "${CATALOG_ASSURANCE_SIGNING_KEY_FILE:?}" "${CATALOG_DEPLOYMENT_CLAIMS:?}" "${CATALOG_DEPLOYMENT_SIGNING_KEY_FILE:?}"
: "${CATALOG_EVIDENCE_STAGING_ROOT:?}" "${CATALOG_EVIDENCE_STORE_ROOT:?}" "${CATALOG_EVIDENCE_BASE_URI:?}" "${CATALOG_ATTEST_TRUST_ROOTS_FILE:?}"
: "${CATALOG_RUNTIME_ISSUER:?}" "${CATALOG_CENSUS_ISSUER:?}" "${CATALOG_ASSURANCE_ISSUER:?}" "${CATALOG_DEPLOYMENT_ISSUER:?}"
poetry run python scripts/validate_api_provider_catalog.py verify --kind runtime --attestation "$CATALOG_RUNTIME_ATTESTATION" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_PHASE_A_COMMIT" --expected-image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_RUNTIME_ISSUER"
poetry run python scripts/validate_api_provider_catalog.py verify --kind census --attestation "$CATALOG_CENSUS_ATTESTATION" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_PHASE_A_COMMIT" --expected-image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_CENSUS_ISSUER"
poetry run python scripts/validate_api_provider_catalog.py attest --kind assurance --claims "$CATALOG_ASSURANCE_CLAIMS" --base-uri "$CATALOG_EVIDENCE_BASE_URI" --key-file "$CATALOG_ASSURANCE_SIGNING_KEY_FILE" --output "$CATALOG_EVIDENCE_STAGING_ROOT/assurance.attestation.json"
poetry run python scripts/validate_api_provider_catalog.py verify --kind assurance --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/assurance.attestation.json" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_PHASE_A_COMMIT" --expected-image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_ASSURANCE_ISSUER"
poetry run python scripts/validate_api_provider_catalog.py publish --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/assurance.attestation.json" --store-root "$CATALOG_EVIDENCE_STORE_ROOT"
# após o deploy A autorizado e observado pelo deployment orchestrator
poetry run python scripts/validate_api_provider_catalog.py collect-census --subject-commit "$CATALOG_PHASE_A_COMMIT" --image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --environment "$CATALOG_ENVIRONMENT" --deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --output "$CATALOG_EVIDENCE_STAGING_ROOT/postdeploy-a-census-claims.json"
poetry run python scripts/validate_api_provider_catalog.py attest --kind census --claims "$CATALOG_EVIDENCE_STAGING_ROOT/postdeploy-a-census-claims.json" --base-uri "$CATALOG_EVIDENCE_BASE_URI" --key-file "$CATALOG_CENSUS_SIGNING_KEY_FILE" --output "$CATALOG_EVIDENCE_STAGING_ROOT/postdeploy-a-census.attestation.json"
poetry run python scripts/validate_api_provider_catalog.py verify --kind census --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/postdeploy-a-census.attestation.json" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_PHASE_A_COMMIT" --expected-image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_CENSUS_ISSUER"
poetry run python scripts/validate_api_provider_catalog.py publish --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/postdeploy-a-census.attestation.json" --store-root "$CATALOG_EVIDENCE_STORE_ROOT"
poetry run python scripts/validate_api_provider_catalog.py attest --kind deployment --claims "$CATALOG_DEPLOYMENT_CLAIMS" --base-uri "$CATALOG_EVIDENCE_BASE_URI" --key-file "$CATALOG_DEPLOYMENT_SIGNING_KEY_FILE" --output "$CATALOG_EVIDENCE_STAGING_ROOT/deployment-a.attestation.json"
poetry run python scripts/validate_api_provider_catalog.py verify --kind deployment --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/deployment-a.attestation.json" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_PHASE_A_COMMIT" --expected-image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_DEPLOYMENT_ISSUER" --expected-authorization produce_candidate_b
poetry run python scripts/validate_api_provider_catalog.py publish --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/deployment-a.attestation.json" --store-root "$CATALOG_EVIDENCE_STORE_ROOT"
```

---

### Group 6: Ativação conjunta e backout — Fase B

**Goal:** Entregar um candidate imutável com a matriz final backend/frontend e sua attestation para
handoff; qualquer ativação/deploy é ação posterior e separada do release operator autorizado.

**Pre-dispatch gate:** o release CI recebe como inputs externos imutáveis exatos as referências
content-addressed `CATALOG_ASSURANCE_ATTESTATION` e `CATALOG_DEPLOYMENT_ATTESTATION`, mais os valores
esperados Phase A commit/image, environment, deployment fingerprint e issuers configurados pelo
CI/orchestrator. Antes de census, criação ou pin do candidate B, `verify` precisa aceitar ambas
fail-closed e a `deployment` precisa conter exatamente a autorização enum `produce_candidate_b`.
Ausência, mutabilidade, expiração ou mismatch encerra o job. O Operational Checkpoint A é um gate
operacional, não um grupo nem uma dependência de código.

**Deliverables:**

1. No início do grupo e antes de qualquer census ou candidate B, verificar as attestations externas
   imutáveis `assurance` e `deployment` contra Phase A commit/image, environment, deployment
   fingerprint e issuers esperados externos. Rejeitar qualquer mismatch; exigir TTL exato de 90d
   para assurance, 24h para deployment e autorização deployment exatamente `produce_candidate_b`.
2. Somente após esse gate, o release CI reroda imediatamente o census no environment/deployment
   fingerprint da Phase A, exigindo `unsafe_legacy_count == 0`; `new_explicit_id_count` pode ser
   maior que zero e nesse caso somente sela a proibição de rollback anterior à A.
3. Criar e pinar o candidate commit/image que altera, na mesma release unit, a matriz Python e o
   espelho TypeScript para exatamente 16 `active` e seis `coming_soon`, sem modificar readers/factories A.
4. Somente depois de o candidate existir, o release CI roda nele o checker de argv fixo, agora
   derivando e comparando o conjunto completo `ACTIVE_PROVIDER_PAIRS` de 16 pares, e emite a
   attestation externa `runtime`, que `verify` compara ao candidate commit/image e aos valores
   esperados de environment, fingerprint e issuer. A attestation `census` é comparada separadamente
   ao Phase A commit/image implantado e ao mesmo environment/fingerprint; reviewer apenas verifica.
5. Entregar ao release operator o candidate digest, attestation, census, assurance e runbook. Apenas
   esse operador, com autorização explícita de ativação, pode verificar os inputs e implantar a image
   exata atestada; o agente do Grupo 6 não executa deploy. Backout fecha badges/adoção e preserva A;
   `new_explicit_id_count > 0` mantém proibido somente o rollback anterior à A.

**Acceptance Criteria:**

- [ ] Census imediatamente anterior à Fase B está ligado ao deployment fingerprint e prova
  `unsafe_legacy_count == 0`; qualquer `new_explicit_id_count` é aceito com rollback anterior a A fechado.
- [ ] Assurance e deployment externos são verificados fail-closed antes do census/candidate B contra
  Phase A commit/image, environment, fingerprint e issuers esperados; deployment autoriza somente
  `produce_candidate_b`, sem valor de manifesto/claim controlando argv ou shell.
- [ ] O candidate 16/6 existe antes da execução do checker; checker e attestation provam esse subject
  exato; runtime é comparado ao candidate commit/image, census ao deploy Phase A, e assurance/checkpoint
  estão válidos e frescos.
- [ ] Python/TypeScript exibem exatamente 16 `active` e seis `coming_soon`, com WhatsApp gated e
  `active ⊆ operational`, bases canônicas e nenhum terceiro estado.
- [ ] O handoff exige que somente o release operator autorizado implante o subject atestado; o grupo
  não exerce autoridade de deploy, e o backout preserva compatibilidade A e gatilhos de voz intactos.
- [ ] Históricos Gemini permanecem byte a byte e nenhum artefato afirma homologação live.

**Validation:**

```bash
: "${CATALOG_PHASE_A_COMMIT:?}" "${CATALOG_PHASE_A_IMAGE_DIGEST:?}" "${CATALOG_ENVIRONMENT:?}" "${CATALOG_DEPLOYMENT_FINGERPRINT:?}"
: "${CATALOG_ASSURANCE_ATTESTATION:?}" "${CATALOG_DEPLOYMENT_ATTESTATION:?}" "${CATALOG_ASSURANCE_ISSUER:?}" "${CATALOG_DEPLOYMENT_ISSUER:?}"
: "${CATALOG_ATTEST_TRUST_ROOTS_FILE:?}"
poetry run python scripts/validate_api_provider_catalog.py verify --kind assurance --attestation "$CATALOG_ASSURANCE_ATTESTATION" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_PHASE_A_COMMIT" --expected-image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_ASSURANCE_ISSUER"
poetry run python scripts/validate_api_provider_catalog.py verify --kind deployment --attestation "$CATALOG_DEPLOYMENT_ATTESTATION" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_PHASE_A_COMMIT" --expected-image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_DEPLOYMENT_ISSUER" --expected-authorization produce_candidate_b
# somente depois de ambas as verificações acima passarem
: "${CATALOG_CENSUS_DSN_FILE:?}" "${CATALOG_CENSUS_SIGNING_KEY_FILE:?}" "${CATALOG_CENSUS_ISSUER:?}"
: "${CATALOG_EVIDENCE_STAGING_ROOT:?}" "${CATALOG_EVIDENCE_STORE_ROOT:?}" "${CATALOG_EVIDENCE_BASE_URI:?}"
poetry run python scripts/validate_api_provider_catalog.py collect-census --subject-commit "$CATALOG_PHASE_A_COMMIT" --image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --environment "$CATALOG_ENVIRONMENT" --deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --output "$CATALOG_EVIDENCE_STAGING_ROOT/pre-b-census-claims.json"
poetry run python scripts/validate_api_provider_catalog.py attest --kind census --claims "$CATALOG_EVIDENCE_STAGING_ROOT/pre-b-census-claims.json" --base-uri "$CATALOG_EVIDENCE_BASE_URI" --key-file "$CATALOG_CENSUS_SIGNING_KEY_FILE" --output "$CATALOG_EVIDENCE_STAGING_ROOT/pre-b-census.attestation.json"
poetry run python scripts/validate_api_provider_catalog.py verify --kind census --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/pre-b-census.attestation.json" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_PHASE_A_COMMIT" --expected-image-digest "$CATALOG_PHASE_A_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_CENSUS_ISSUER"
# somente depois do census passar, criar/pinar o candidate B 16/6; então:
: "${CATALOG_CANDIDATE_COMMIT:?}" "${CATALOG_CANDIDATE_IMAGE_DIGEST:?}" "${CATALOG_RUNTIME_SIGNING_KEY_FILE:?}" "${CATALOG_RUNTIME_ISSUER:?}"
: "${CATALOG_ENVIRONMENT:?}" "${CATALOG_DEPLOYMENT_FINGERPRINT:?}"
poetry run python scripts/validate_api_provider_catalog.py attest --kind runtime --subject-commit "$CATALOG_CANDIDATE_COMMIT" --image-digest "$CATALOG_CANDIDATE_IMAGE_DIGEST" --environment "$CATALOG_ENVIRONMENT" --deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --base-uri "$CATALOG_EVIDENCE_BASE_URI" --key-file "$CATALOG_RUNTIME_SIGNING_KEY_FILE" --output "$CATALOG_EVIDENCE_STAGING_ROOT/candidate-runtime.attestation.json"
poetry run python scripts/validate_api_provider_catalog.py verify --kind runtime --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/candidate-runtime.attestation.json" --trust-roots "$CATALOG_ATTEST_TRUST_ROOTS_FILE" --expected-subject-commit "$CATALOG_CANDIDATE_COMMIT" --expected-image-digest "$CATALOG_CANDIDATE_IMAGE_DIGEST" --expected-environment "$CATALOG_ENVIRONMENT" --expected-deployment-fingerprint "$CATALOG_DEPLOYMENT_FINGERPRINT" --expected-issuer "$CATALOG_RUNTIME_ISSUER"
poetry run python scripts/validate_api_provider_catalog.py publish --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/candidate-runtime.attestation.json" --store-root "$CATALOG_EVIDENCE_STORE_ROOT"
poetry run python scripts/validate_api_provider_catalog.py publish --attestation "$CATALOG_EVIDENCE_STAGING_ROOT/pre-b-census.attestation.json" --store-root "$CATALOG_EVIDENCE_STORE_ROOT"
poetry run pytest -q tests/scripts/test_validate_api_provider_catalog.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/llm/test_client.py tests/services/test_multimodal_transcription.py tests/services/test_tts_service.py
make quality
cd frontend && pnpm --filter @workspace/neurostack run typecheck && pnpm --filter @workspace/neurostack run test && pnpm --filter @workspace/neurostack run build
```

**depends-on:** group-5

---

## QA Criteria

- [ ] Em dev, a aba APIs mostra blocos/cards na ordem, copy e status exatos em PT e EN.
- [ ] Os 16 pares ativos abrem editor e salvam bundles completos; cada runtime determinístico chama
  somente o endpoint/provider esperado.
- [ ] 360dialog, Twilio, Cortex, Deepgram, Azure STT e Cloud STT ficam visíveis e inertes como
  `EM BREVE`/`COMING SOON`.
- [ ] Meta, Z-API, Evolution e Uazapi expõem o callback/provisionamento definido e rejeitam auth
  ausente, inválida ou rotacionada sem efeitos downstream.
- [ ] Configuração legada `outros` não aparece no grid, continua carregável quando segura e só sai
  por troca atômica para provider explícito.
- [ ] Claude pode ser escolhido pelo model ID OpenRouter; não existe card Anthropic direto.
- [ ] Gatilhos de voz aparecem depois de TTS e mantêm switch/políticas/fallback textual atuais.
- [ ] QA não declara homologação live; a rodada externa de providers permanece posterior.

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Dependências não concluídas bloquearem a integração | High | Os quatro wishes existem; work do catálogo continua bloqueado até WhatsApp, STT e Dropout, além do TTS já concluído, chegarem a `SHIPPED` + execution review `SHIP`. |
| `tts-openai-provider` divergir depois do merge SHIPPED | Medium | Pinar o merge `be0a6547c6c7462c1cbeeeb39030a7d10ee781b8`, cujas execution/delta/final reviews são `SHIP`; qualquer mudança de contrato retorna este WISH a `DRAFT`. |
| WhatsApp ativar com inbound incompleto | High | Wish dedicado e obrigações separadas por provider/auth/zero-effect. |
| Evidência nomeada não cobrir o comportamento alegado | High | Taxonomia fechada, node IDs coletados, unique evidence e source/tree digests. |
| Base legada com query quebrar compatibilidade ou vazar segredo | High | `unsafe_legacy_count == 0` antes da Fase B; saída incumbent-only passa pela mesma policy no factory. |
| DB/backups não satisfazerem proteção em repouso | High | Assurance fail-closed ou wish `api-credentials-at-rest-protection`. |
| Backout remover readers de IDs persistidos | High | Backout B preserva A; apenas rollback anterior à A requer `new_explicit_id_count == 0`. |
| Telemetria ser interpretada como auditoria durável | Medium | Contrato at-most-once, janela de crash e failure alert documentados/testados. |
| Usuário interpretar ativo como homologado | Medium | Copy e docs separam disponibilidade técnica de prova live. |
| Attestation entrar no próprio subject ou ficar mutável | High | CI/orchestrator emite fora da árvore com Ed25519/O_EXCL depois dos testes; reviewer somente verifica. |
| Evidência operacional envelhecer ou apontar outro rollout | High | `verify` exige valores externos esperados para subject, image, environment, fingerprint e issuer; deployment exige enum exato `produce_candidate_b`; TTL exato: runtime/census/deployment 24h, assurance 90 dias. |

## Review Results

### Historical Plan Review — 2026-09-28T22:43:39Z — FIX-FIRST

- **Target SHA-256:** `d067bb43832122839aa188b204899fbdfd8020dea67c3bda20654b306c96874a`
- **Reviewer:** `/root/plan_review_provider_catalog`
- **Resolution:** o primeiro loop separou o catálogo do gate histórico Gemini e tornou as três
  dependências então conhecidas reproduzíveis.

### Historical Plan Re-review — 2026-09-28T22:51:59Z — SHIP (superseded)

- **Target SHA-256:** `ff08985e8fa69ce0ef5805ad814574e787e356d3e8e3efa5989bcb805a8281f6`
- **Reviewer:** `/root/plan_rereview_provider_catalog`
- **Resolution:** o plano foi aprovado no escopo anterior; o conselho posterior reabriu o design e
  esta aprovação não autoriza mais `work`.

### Council Re-open — 2026-09-28 — REVISE

- As lentes técnica, produto e risco apoiaram com condições; o dissent apontou promoção baseada em
  membership, inbound WhatsApp não provisionável, legado/endpoint ambíguos e gates de
  assurance/rollback insuficientes.
- O design foi corrigido e recebeu review oficial `SHIP` no digest
  `f1a2c18dffe3654f26cb99d8de89fdf6b2320bb7e8a408d1da7f93ab989fbc05` por
  `/root/official_design_rereview` em `2026-09-28T23:36:33Z`.
- Este WISH permanece `DRAFT` até o novo plan review abaixo ser persistido.

### Standard Plan Review — 2026-09-28T23:45:47Z — SHIP (superseded / pending fix)

- **Target SHA-256:** `da5f048236d47dff6e4de7504573e84ec03280ec8a3a5f9d9767fe908fb249ca`
- **Reviewer:** `/root/official_plan_rereview`
- **Resolution:** o reviewer padrão retornou `SHIP`, mas a transição não foi persistida porque a
  lente questioner posterior encontrou gaps HIGH; não há autorização de `work` nem estado APPROVED.

### Advisory Questioner — BLOCKED / fix loop 1 resolution pending

- O loop 1 removeu o ciclo de pre-work, tornou as dependências hard blockers, moveu attestations
  runtime para fora da árvore, fixou argv/markers do checker, definiu producers/freshness/checkpoint,
  dividiu o plano em seis grupos e tornou o alerta Prometheus executável.
- O WISH permanece `DRAFT` e `BLOCKED` para work até re-review independente deste novo digest; esta
  entrada não é aprovação.

### Official Plan Fix 1 Re-review — 2026-09-29T00:05:43Z — SHIP (superseded)

- **Target SHA-256:** `59092ef93ec9abf0671d6f04cda33a88cbbadafe43a9280912e946ea19513dfe`
- **Reviewer:** `/root/official_plan_rereview`
- **Resolution:** o reviewer oficial retornou `SHIP`, mas a decisão ficou retida e `APPROVED` não foi
  persistido porque a lente questioner concorrente apresentou gaps HIGH adicionais.

### Advisory Questioner — 2026-09-29T00:03:24Z — FIX-FIRST (fix loop 2 resolved)

- O timestamp da lente é anterior ao re-review acima, mas sua mensagem/decisão foi processada depois;
  por isso o `SHIP` de fix 1 está superseded e não autoriza work.
- **Gaps:** Fase A confundia saída incumbent-only com nova adoção e usava
  `new_explicit_id_count == 0` como gate indevido de B; o protocolo de attestation não tinha comandos,
  schemas/assinaturas/paths executáveis e atribuía produção a reviewer.
- **Resolution:** DESIGN/DRAFT/WISH agora separam os dois census, congelam as matrizes A/B e
  especificam o protocolo Ed25519 externo com CI/orchestrator produtor e reviewer read-only. O novo
  design review oficial abaixo validou o helper digest; este WISH segue para re-review independente.
- O status permanece `DRAFT`; `APPROVED` não foi persistido.

### Official Design Re-review — 2026-09-29T00:20:16Z — SHIP

- **Reviewed content SHA-256:** `025c397a0af4335e5266a3c19230d8a8a0ef405b818991a02ee4d1a9adc529f3`
- **Reviewer:** `/root/official_design_rereview_loop2`
- **Resolution:** nenhum gap; foram reproduzidos o digest e as matrizes A `1/21` e B `16/6`, com
  validação explícita de domínios, evidência por par, inbound seguro, OpenRouter, assurance, census,
  saída incumbent-only e semântica de rollback. O `SHIP` autoriza este novo plan review, não `work`.

### Official Plan Re-review — 2026-09-29T00:25:33Z — FIX-FIRST

- **Target SHA-256:** `510fac43cd1e3e627a15961e5903e13dcedb5722041b7fdcac21b39c67c1fc5c`
- **Reviewer:** `/root/official_plan_rereview_loop2`
- **HIGH:** o Grupo 6 declara revalidar `assurance` e `deployment`, mas seus comandos não recebem nem
  verificam essas attestations e o contrato de `verify` não compara de forma fechada subject, image,
  environment, deployment fingerprint, issuer e autorização esperados.
- **Outcome:** o plano não foi aprovado e não autoriza `work`.

### Advisory Questioner Final — 2026-09-29T00:26:42Z — FIX-FIRST

- **Target SHA-256:** `510fac43cd1e3e627a15961e5903e13dcedb5722041b7fdcac21b39c67c1fc5c`
- **Reviewer:** `/root/questioner_plan_final`
- **Resolution:** reproduziu o mesmo gap HIGH, sem novo gap agregado; os demais gates reabertos
  passaram, inclusive migração incumbent-only, census separados, matrizes A/B, candidate antes do
  checker, fronteira producer/reviewer, storage, Ed25519 e backout.

### Final Lifecycle Gate — 2026-09-29T00:28:30Z — BLOCKED

- **Target SHA-256:** `510fac43cd1e3e627a15961e5903e13dcedb5722041b7fdcac21b39c67c1fc5c`
- **Reviewer:** `/root/final_gate_plan_catalog`
- **Diagnosis:** `ambiguous-spec`; uma attestation pode ser criptograficamente válida e ainda apontar
  para outro subject/image/deployment ou autorização, pois falta o vínculo executável com a intenção
  externa do rollout.
- **Required change:** tornar a verificação fail-closed contra valores esperados, receber/verificar
  `assurance` e `deployment` antes do candidate B e cobrir attestations assinadas porém semanticamente
  divergentes em testes negativos.
- **Outcome:** os dois fix loops foram consumidos; o status permanece `DRAFT` e uma nova correção
  deliberada seguida de review independente é obrigatória.

### User-authorized Resume — 2026-09-29 — proposed BLOCKED resolution

- O usuário autorizou explicitamente a retomada de `api-provider-catalog-presentation`; os dois
  autofix loops anteriores permanecem históricos e não foram apagados nem reinterpretados.
- **Proposed resolution:** `verify` permanece responsável por assinatura/kind e agora exige valores
  esperados externos fail-closed para subject commit, image digest, environment, deployment
  fingerprint e issuer; `deployment` exige também o enum exato `produce_candidate_b`. O Grupo 5
  implementa e testa divergências Ed25519 validamente assinadas; o Grupo 6 verifica assurance e
  deployment imutáveis antes de census/candidate B, compara runtime ao candidate e census à Phase A.
- O status permanece `DRAFT`; esta proposta resolve o BLOCKED somente se o re-review independente
  retornar `SHIP`, e não autoriza `work` ou deploy.

### User-authorized Plan Re-review — 2026-09-29T01:07:32Z — SHIP

- **Target SHA-256:** `8bb6ed96d3ced59fcd912413edfe0496c06dd160b9da3a924d2c71e42e2212a5`
- **Reviewer:** `/root/catalog_resumed_official_review`
- **Resolution:** nenhum gap; as 11 chamadas de `verify`, os negativos semanticamente divergentes,
  a ordem fail-closed do Grupo 6, os TTLs/roles e os seis grupos foram validados. O plano pode ser
  `APPROVED`, mas os três slugs ausentes continuam bloqueando qualquer `work`.

### User-authorized Questioner — 2026-09-29T01:06:36Z — SHIP

- **Target SHA-256:** `8bb6ed96d3ced59fcd912413edfe0496c06dd160b9da3a924d2c71e42e2212a5`
- **Reviewer:** `/root/catalog_resumed_questioner`
- **Resolution:** nenhuma brecha encontrada na vinculação fail-closed; census Phase A e runtime do
  candidate não podem ser trocados, e manifesto/claims não controlam argv ou shell.

### User-authorized Final Gate — 2026-09-29T01:09:41Z — SHIP

- **Target SHA-256:** `8bb6ed96d3ced59fcd912413edfe0496c06dd160b9da3a924d2c71e42e2212a5`
- **Reviewer:** `/root/catalog_resumed_final_gate`
- **Resolution:** zero gaps agregados. O status foi persistido como `APPROVED`; isso não autoriza
  checkout, dispatch, candidate B ou deploy enquanto as três dependências ausentes não estiverem
  `SHIPPED` e com execution review independente `SHIP`.

### Dependency Continuation Re-review — 2026-09-29T02:09:09Z — BLOCKED externally

- **Target SHA-256 after doc fixes:**
  `abf47ee0ea6522cac3061059ccd8def2f6c919e850189f358990f4aae281f7ea`.
- **Reviewer:** `/root/catalog_dependency_gate`; os dois gaps documentais MEDIUM receberam re-review
  `SHIP`, sem gaps internos restantes.
- **Dependency state:** TTS está `SHIPPED`; STT está `APPROVED`/plan `SHIP` e pode executar
  isoladamente com `Δ public = ∅`; WhatsApp aguarda o census externo zero/zero; Dropout aguarda o
  contrato autoritativo NeuraLake para áudio OGG/Opus.
- **Outcome:** o WISH do catálogo permanece `APPROVED`, porém nenhum de seus grupos pode iniciar.
  O estado agregado é `BLOCKED` somente pelas duas evidências externas, sem impedir a execução da
  dependência STT. A redação histórica “três dependências ausentes” acima é superseded por esta
  fotografia e pela seção Dependencies atual.

### Post-STT Dependency Snapshot — 2026-09-29 — BLOCKED externally

- **STT:** `stt-openrouter-scribe-activation` está `SHIPPED`; PR #43 mergeado em
  `acf7586c91dd2101129da202d4e9b4c2b4c3ca5b`, reviews independentes `SHIP` e smoke pós-merge com
  569 testes verdes. A matriz pública permaneceu inalterada.
- **Satisfied dependencies:** `tts-openai-provider` e `stt-openrouter-scribe-activation`.
- **Remaining hard gates:** `whatsapp-inbound-provider-activation` ainda exige receipt externo
  agregado, verificável e recente com counts `{evolution: 0, uazapi: 0}`; `dropout-multimodal-fallback`
  ainda exige contrato NeuraLake autoritativo/versionado para áudio OGG/Opus bruto.
- **Outcome:** nenhum Grupo 1–6 do catálogo pode ser claimed ou iniciado. O próximo avanço legítimo
  é fornecer e revisar esses dois artefatos externos; mocks, inferência ou alteração local não os
  substituem.

## Files to Create/Modify

```text
src/services/api_credentials.py
src/api/schemas/config.py
src/api/routes/config.py
src/api/helpers/health.py
src/llm/client.py
src/services/safe_egress.py
src/services/multimodal_service.py
src/services/groq_service.py
src/services/tts_service.py
src/observability/metrics.py
pyproject.toml
config/prometheus_rules.yml
src/config/prometheus_rules.yml
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-multimodal.test.tsx
frontend/artifacts/neurostack/src/components/api-settings/provider-credential-card.tsx
frontend/artifacts/neurostack/src/components/api-settings/provider-credential-card.test.tsx
frontend/artifacts/neurostack/src/components/api-settings/voice-response-controls.test.tsx
scripts/validate_api_provider_catalog.py
tests/services/test_api_credentials.py
tests/api/test_api_credentials.py
tests/api/test_config_credentials_boundary.py
tests/llm/test_client.py
tests/services/test_safe_egress.py
tests/services/test_multimodal_transcription.py
tests/services/test_tts_service.py
tests/observability/test_api_credentials_change_alert.py
tests/scripts/test_validate_api_provider_catalog.py
docs/configuration.md
docs/api-provider-settings-rollout.md
```
