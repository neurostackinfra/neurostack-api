# Wish: Dropout como fallback multimodal operacional

| Field | Value |
|-------|-------|
| **Status** | DRAFT |
| **Execution** | BLOCKED — contrato autoritativo de áudio NeuraLake ausente |
| **Slug** | `dropout-multimodal-fallback` |
| **Date** | 2026-09-28 |
| **Author** | Lorenzo |
| **Appetite** | medium |
| **Branch** | `wish/dropout-multimodal-fallback` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |
| **Governing design** | [api-provider-catalog-presentation/DESIGN.md](../../brainstorms/api-provider-catalog-presentation/DESIGN.md) |
| **Governing design reviewed SHA-256** | `025c397a0af4335e5266a3c19230d8a8a0ef405b818991a02ee4d1a9adc529f3` (SHIP) |

## Problem

O catálogo pretende oferecer **Dropout by Neuralake** como a segunda opção de fallback multimodal,
mas o repositório só prova OpenRouter para áudio bruto. A documentação pública NeuraLake/Dropout
observada hoje confirma base OpenAI-compatible, bearer auth, `POST /chat/completions` e uma
capability `model="multimodal"`; ela apresenta Vision & Docs, mas não especifica um contrato
autoritativo para `input_audio` com OGG/Opus bruto, request shape, response e erros. Reutilizar a
shape do OpenRouter em mocks transformaria uma suposição em implementação e não provaria que o
provider aceita o artefato do WhatsApp.

Portanto este wish permanece `DRAFT` e sua execução permanece `BLOCKED` até receber um artefato
autoritativo, versionado e verificável da NeuraLake que feche esse contrato. Nenhum grupo de código,
teste de adapter ou alteração de runtime começa sem o gate; probe live não substitui especificação e
não será executado sem autorização explícita separada.

## Summary

Depois que o contrato ausente passar pelo gate pré-execução, este wish entregará somente o
transporte/runtime interno e as provas determinísticas de Dropout para fallback textual e áudio
bruto. O
[DESIGN governante do catálogo](../../brainstorms/api-provider-catalog-presentation/DESIGN.md)
define `active` como capacidade configurável e executável sem exigir homologação live, mas a adoção
pública, o ID na matriz, PATCH/readiness, assurance de credenciais e a promoção do card pertencem
exclusivamente a `api-provider-catalog-presentation`; essa separação mantém o DAG sem ciclo e o delta
público deste wish igual a zero.

## Scope

### IN

- Gate pré-execução fail-closed para um contrato NeuraLake autoritativo/versionado que declare
  explicitamente suporte a OGG/Opus bruto em `input_audio`, encoding/request shape, response e erros.
- Após o gate, e somente após ele, transporte interno com base canônica
  `https://api.neuralake.cloud/v1`, `POST /chat/completions`, bearer auth, model ID conforme o
  artefato aprovado e egress kind dedicado `multimodal_dropout` pinado a
  `api.neuralake.cloud`.
- Adapter interno de texto e áudio, sem superfície pública nova: texto preserva as mensagens do
  chamador; áudio preserva os bytes do WhatsApp byte a byte, sem transcode, reencode ou egress
  alternativo, exatamente na shape autorizada pelo contrato.
- Parsing e erro interno conforme o artefato: resposta válida, resposta vazia/malformada, HTTP error
  e transport error, com segurança/redaction e nenhuma aceitação de conteúdo inválido.
- Integração interna com os failovers existentes, inacessível a nova adoção pública neste wish:
  seleção Dropout somente por bundle interno completo/coerente; OpenRouter continua inalterado.
- Provas determinísticas de preflight, request, parsing, contenção de falha, redaction e efeitos
  downstream, sem chamada real ao provider.

### OUT

- Adicionar `multimodal/dropout` a `PROVIDER_AVAILABILITY`, `PROVIDER_ALLOWLISTS`, schema PATCH,
  Literals wire, catálogo TypeScript, cards, ordem, labels, bylines, badge ou readiness público.
- Persistir, rotacionar ou expor credenciais Dropout via API pública; validar proteção at-rest,
  backups, least privilege ou restore/key rotation. A adoção e a assurance são gates do catálogo.
- Alterar `src/api/routes/config.py`, `src/api/schemas/config.py` ou o contrato público de
  `src/services/api_credentials.py` neste wish.
- Renomear, reabrir, concluir ou depender de `fallback-texto`; seu estado histórico não integra este
  DAG.
- Executar probe live, homologação externa ou inferir suporte de áudio a partir de Vision & Docs,
  compatibilidade textual OpenAI ou comportamento do OpenRouter.
- Adicionar transcode, retry/backoff, circuit breaker, segundo provider multimodal, host alternativo,
  fila, cache, migration, tabela, endpoint novo ou seleção dinâmica/arbitrária.
- Alterar mensagens de recondução, deduplicação ou a ordem STT → multimodal → texto; alterar STT,
  cujo delta pertence a `stt-openrouter-scribe-activation`.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | O DESIGN de `api-provider-catalog-presentation` governa o contrato; o único edge do DAG é `blocks: api-provider-catalog-presentation` | O catálogo consome esta capacidade concluída; fazê-la depender do consumidor criaria ciclo. |
| 2 | O gate exige especificação NeuraLake autoritativa para áudio, não mocks nem analogia com OpenRouter | Mock só prova o código contra a shape que o próprio teste inventou; não prova aceitação do provider. |
| 3 | Sem o artefato, o status permanece `DRAFT`, execution `BLOCKED` e nenhum código/teste de produto é alterado | O request de áudio, parser e matriz de erros ainda não têm interface decidida. |
| 4 | Probe live é proibido sem autorização explícita e não substitui contrato versionado | Evita efeitos/custo externo e uma prova efêmera incapaz de governar manutenção. |
| 5 | O delta público deste wish é exatamente zero | `multimodal/dropout` só entra na matriz/schema/PATCH/readiness no catálogo, na mesma release unit de adoção e assurance. |
| 6 | O delta STT é independente e order-independent | Este wish não muda STT; `stt-openrouter-scribe-activation` pode concluir antes ou depois sem alterar os contratos aqui. |
| 7 | A PR #22 é apenas ancestral histórico; execução parte de snapshot limpo e pinado de `origin/main` | O baseline avançou depois da PR #22 e um merge antigo não identifica a árvore executada. |
| 8 | Depois do gate, texto e áudio reutilizam transports existentes somente se a shape aprovada permitir | Evita SDK/protocolo paralelo, mas não presume que o payload OpenRouter seja compatível. |
| 9 | Preflight reject e provider runtime failure possuem envelopes de efeitos distintos | O loader pode fazer a leitura necessária do bundle; depois dela, config/bundle/endpoint inválido deve ter zero write/invalidation/event/network ou leitura adicional. Falha após request pode gerar somente accounting/log redigido e, no áudio, a recondução textual existente. |
| 10 | Nenhum nome promocional/model ID do OpenRouter entra em card ou configuração Dropout | A apresentação é do catálogo; este wish é interno e provider-specific. |

## Simplicity Case

- **Simplest complete design:** aguardar o contrato que decide a interface; depois adicionar uma
  policy de egress e uma variante interna aos transports/failovers existentes, coberta por mocks que
  espelham o artefato aprovado.
- **Added machinery:** gate de proveniência/digest, egress kind dedicado, validação interna
  provider↔base↔model e fixtures Dropout. O gate existe porque o contrato de áudio não está nas docs;
  os demais itens impedem SSRF, confusão com OpenRouter e vazamento de artefato/segredo.
- **Deferred until measured:** adoção/PATCH/readiness/assurance ficam no catálogo; probe live somente
  com autorização; retry/circuit breaker só com telemetria; transcode somente se um requisito futuro
  documentar formato incompatível.
- **Complexity removed:** nenhum ID público antecipado, migration, endpoint, cache, autodiscovery,
  egress custom, segundo cliente, modelo inventado, alteração de STT ou UI nesta dependência.

## Dependencies

**depends-on:** none
**blocks:** api-provider-catalog-presentation

### Baseline limpo e pinado

O snapshot observado de `origin/main` no início deste fix loop é:

- commit: `be0a6547c6c7462c1cbeeeb39030a7d10ee781b8`
- tree: `4d9e5e78541e3ef789b5e93fc08bcfe5233fc80b`
- subject: `Merge pull request #40 from neurostackinfra-hub/fix/tts-openai-status-errors`

A PR #22 (`fca60f37bf3c03132efbe44a848462a64ce7686d`) é somente ancestral histórico desse
snapshot. Ela explica a origem do baseline OpenRouter, mas não é o subject de execução nem autoriza
reaproveitar um worktree sujo.

Antes do Grupo 1, depois que o contrato externo passar, o orquestrador cria um worktree isolado e
limpo no commit pinado e revalida commit/tree/ancestralidade. Se `origin/main` tiver avançado, este
WISH volta a `DRAFT`, registra o novo commit/tree e refaz a análise de delta antes de dispatch; não
troca o pin silenciosamente.

```bash
test "$(git rev-parse HEAD)" = "be0a6547c6c7462c1cbeeeb39030a7d10ee781b8"
test "$(git rev-parse HEAD^{tree})" = "4d9e5e78541e3ef789b5e93fc08bcfe5233fc80b"
git merge-base --is-ancestor fca60f37bf3c03132efbe44a848462a64ce7686d HEAD
git diff --quiet
git diff --cached --quiet
test -z "$(git status --porcelain --untracked-files=all)"
```

### Hard pre-execution gate — contrato de áudio NeuraLake

**Estado atual: BLOCKED.** As páginas públicas `https://www.neuralake.com.br/api` e
`https://www.dropout.ing/` não satisfazem este gate: Vision & Docs e compatibilidade textual não
declaram `input_audio` OGG/Opus raw. Nenhum grupo abaixo pode ser claimed/dispatched e nenhum código,
teste ou fixture de produto pode ser criado enquanto o blocker permanecer.

O gate exige um artefato imutável ou versionado, emitido/publicado pela NeuraLake e com proveniência
verificável, que feche todos os campos seguintes:

1. URI/issuer, versão ou data efetiva, SHA-256 do conteúdo e política explícita de evolução.
2. Base, path HTTP, método, bearer auth e model ID/capability exatos para áudio.
3. MIME/container/codec aceitos, incluindo declaração inequívoca sobre OGG/Opus bruto do WhatsApp,
   limites de bytes/duração e se os bytes devem permanecer sem transcode.
4. JSON request completo: `messages`, content parts, nome/tipo do bloco de áudio, encoding dos bytes,
   campo de formato e campos obrigatórios/opcionais. O gate não presume `input_audio` até o artefato
   confirmá-lo.
5. JSON response completo para sucesso, incluindo onde fica o texto e se content pode ser string,
   lista ou outra shape.
6. Matriz de erros para auth, formato/MIME, size/rate limit, JSON/shape, HTTP e transport, com
   garantia suficiente para definir parsing e redaction sem registrar body, áudio ou segredo.

O owner registra no WISH, via revisão independente, URI, issuer, versão, SHA-256, data observada e um
mapping de cada item 2–6 para a seção do artefato. A verificação mínima de integridade é:

```bash
: "${NEURALAKE_MULTIMODAL_CONTRACT_FILE:?}" "${NEURALAKE_MULTIMODAL_CONTRACT_SHA256:?}"
test -f "$NEURALAKE_MULTIMODAL_CONTRACT_FILE"
test "$(sha256sum "$NEURALAKE_MULTIMODAL_CONTRACT_FILE" | cut -d' ' -f1)" = "$NEURALAKE_MULTIMODAL_CONTRACT_SHA256"
```

Esse comando só prova integridade; a revisão independente prova autoridade e completude semântica.
Se qualquer item faltar ou contradizer OGG/Opus raw, o veredito continua `BLOCKED` e o plano retorna a
design/decisão. Probe live, se futuramente autorizado, é evidência complementar e nunca preenche um
campo ausente do contrato.

## Success Criteria

- [ ] O hard gate contém artefato NeuraLake autoritativo/versionado, digest verificado e mapping
  independente para request de áudio OGG/Opus raw, response e matriz de erros; até lá nenhum grupo
  de código começa.
- [ ] O checkout executado é uma árvore limpa no commit/tree pinados; avanço de `origin/main` reabre
  o plano antes de qualquer dispatch.
- [ ] Este wish produz delta público zero: nenhuma matriz, schema PATCH, Literal wire, readiness,
  card, ordem ou adoção passa a reconhecer `multimodal/dropout`.
- [ ] Depois do gate, o runtime interno resolve Dropout somente para base/host/model/shape exatos do
  contrato e usa `multimodal_dropout`; endpoint incoerente falha antes da rede.
- [ ] Fallback textual envia as mesmas mensagens da tentativa primária, no máximo uma vez, na shape
  aprovada; sucesso primário produz zero requests Dropout.
- [ ] Fallback de áudio preserva exatamente os bytes OGG/Opus recebidos e usa somente a shape
  aprovada; não há transcode, truncamento, reencode, segundo multimodal ou host alternativo.
- [ ] Parser aceita somente responses documentadas; vazio/malformado, HTTP e transport error nunca
  viram sucesso nem conteúdo inventado.
- [ ] Preflight reject permite somente a leitura necessária para carregar o bundle e depois prova
  zero leitura adicional, DB write, cache write/invalidation, evento e request de rede. O teste
  unitário do factory, que já recebe o bundle, prova zero DB/cache read.
- [ ] Falha do provider depois do request não faz retry nem chama OpenRouter/outro host; os únicos
  efeitos permitidos são attempt/accounting e log redigidos e, no áudio, a recondução textual já
  existente. Nenhuma nova mutação/evento é criada.
- [ ] API key/Authorization, bytes, payload e response body brutos não aparecem em exceção, log,
  GET, cache, telemetria ou envelope.
- [ ] OpenRouter permanece byte-idêntico nas asserções existentes; STT e sua matriz não mudam.
- [ ] Nenhum artefato afirma probe live, homologação ou suporte além do contrato versionado.
- [ ] O catálogo continua responsável por introduzir `multimodal/dropout` na matriz, abrir PATCH e
  adoção, verificar assurance at-rest e promover o card na release unit final.

## Execution Strategy

### Wave 0 (blocking gate; no product mutation)

| Gate | Owner | State | Description |
|------|-------|-------|-------------|
| NeuraLake audio contract | provider-contract owner + independent reviewer | BLOCKED | Obter, verificar e mapear o artefato autoritativo definido acima; sem ele nenhuma wave de código inicia. |
| Clean baseline | orchestrator | PENDING after contract | Criar worktree isolado no commit/tree pinados e repetir os comandos de limpeza/ancestralidade. |

### Wave 1 (sequential, only after Wave 0 passes)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2: fronteira de rede/egress stateful (+2) | engineer-standard / high | Implementar policy e adapter interno exatamente conforme o contrato aprovado. |
| 2 | engineer | 3: integração em dois failovers stateful (+2), regressão cruzada de canais (+1 multi-package) | engineer-standard / high | Integrar o runtime inacessível à adoção pública e provar envelopes de efeitos. |

Os grupos são sequenciais porque o Grupo 2 consome request/response/error shapes fechadas no Grupo 1.
A integração STT é independente e pode ocorrer em qualquer ordem; somente o catálogo agrega os
deltas públicos na release unit final.

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

### Group 1: Transporte e parsing internos conforme contrato NeuraLake

**Precondition:** Wave 0 inteira passou e a revisão independente do artefato está persistida neste
WISH. Sem isso, este grupo não pode ser claimed nem modificar arquivo algum.

**Goal:** Implementar o egress, request e parser Dropout exatamente conforme o contrato aprovado,
sem adicionar provider a nenhuma superfície pública.

**Deliverables:**

1. Adicionar `MULTIMODAL_DROPOUT_HOSTS` e `multimodal_dropout` a `safe_egress`, pinados somente à
   base/host autorizados; nunca cair em `llm_dropout`, `multimodal_openrouter` ou custom-public.
2. Estender o adapter interno em `multimodal_service.py` com constantes e request de texto/áudio
   derivados do artefato, citando versão/digest no comentário de contrato; não copiar a shape
   OpenRouter se o artefato não a declarar.
3. Preservar OGG/Opus raw byte a byte na serialização autorizada, sem transcode/reencode e sem request
   auxiliar; o model ID vem do contrato, não de nome promocional ou default OpenRouter.
4. Parsear apenas success responses documentadas e mapear empty/malformed/auth/format/size/rate
   limit/HTTP/transport para códigos estáveis, secret-free, sem body bruto.
5. Cobrir tudo com transporte mockado determinístico derivado do artefato, incluindo URL/Host/auth,
   request completo, round-trip de bytes, response variants e matriz negativa pre-network.

**Acceptance Criteria:**

- [ ] Cada asserção de request/response/error tem referência ao item/seção correspondente do
  contrato pinado; nenhuma fixture contém campo inferido apenas do OpenRouter.
- [ ] `multimodal_dropout` aceita somente a base autorizada e ataques de scheme/host/path/query/
  userinfo/fragment/port falham com lista de requests vazia.
- [ ] O request de áudio reproduz exatamente os bytes OGG/Opus ao decodificar a serialização
  documentada, e somente um request é observado.
- [ ] Parser aceita todas e somente as success shapes documentadas; matriz de erro retorna códigos
  redigidos e não inclui key, Authorization, bytes ou body em `str`/`repr`/logs.
- [ ] Não há alteração em matriz/provider allowlist/schema/PATCH/readiness público nem em payloads
  OpenRouter existentes.

**Validation:**

```bash
poetry run pytest -q tests/services/test_safe_egress.py tests/services/test_multimodal_transcription.py tests/llm/test_client.py
poetry run mypy src/services/safe_egress.py src/services/multimodal_service.py src/llm/client.py --ignore-missing-imports
```

Escopo proporcional: são as fronteiras internas de policy, request e parser; nenhuma rota pública ou
persistência é alcançada neste grupo.

**depends-on:** none

---

### Group 2: Failovers internos e envelopes de efeitos

**Precondition:** Group 1 passou e o contrato pinado não sofreu drift.

**Goal:** Integrar Dropout internamente aos dois failovers sem abrir adoção pública, provando a
separação entre preflight reject e falha real do provider.

**Deliverables:**

1. Criar uma allowlist/registry **interna de runtime** para Dropout, separada de
   `PROVIDER_AVAILABILITY`, schema e readiness público, e fazer o factory privado exigir bundle
   completo e provider↔base↔model coerentes antes de ler segredo ou construir transporte.
2. Integrar o fallback textual privado: primary success faz zero requests; falha/empty/malformed
   primária seleciona Dropout no máximo uma vez, com as mesmas mensagens; falha Dropout apenas
   registra attempt/accounting/log redigidos, sem retry, segundo multimodal ou host alternativo.
3. Integrar a cadeia interna STT → multimodal → texto em Z-API, Meta e Uazapi sem alterar config
   pública: transcript válido continua o fluxo; empty/malformed/HTTP/transport Dropout alcança apenas
   a recondução textual existente.
4. Provar o envelope **preflight reject** separadamente: o fluxo integrado pode fazer somente a
   leitura necessária para carregar o bundle; depois dela, bundle/endpoint/model incoerente,
   provider desconhecido ou contrato não correspondente causa zero leitura adicional, DB write,
   cache write/invalidation, evento e network. O factory isolado recebe o bundle e prova zero
   DB/cache read.
5. Provar o envelope **provider runtime failure**: um request Dropout pode existir; depois dele não
   há retry, OpenRouter, segundo host, nova mutação/evento ou payload bruto. Os únicos efeitos
   permitidos são attempt/accounting e log redigidos e, somente no áudio, a recondução já existente.
6. Manter as provas OpenRouter byte-idênticas e confirmar delta zero no catálogo público e em STT.

**Acceptance Criteria:**

- [ ] A registry interna permite o runtime Dropout sem modificar `PROVIDER_AVAILABILITY`,
  `PROVIDER_ALLOWLISTS`, Literals/schema PATCH ou readiness.
- [ ] Matriz textual cobre primary success, empty, malformed, HTTP e transport; Dropout recebe as
  mesmas mensagens zero/uma vez e falha não inventa usage/model nem dispara outro provider.
- [ ] Matriz de áudio cobre success, empty/no-speech, malformed, HTTP e transport nos três canais;
  bytes permanecem idênticos e falhas terminam na recondução textual existente.
- [ ] Testes de preflight usam spies para provar a única leitura de bundle permitida e zero leitura
  adicional/write/invalidation/event/network; o factory isolado prova zero DB/cache read.
- [ ] Testes de falha runtime provam exatamente um request Dropout, zero retry/host alternativo/
  segundo multimodal e somente os efeitos allowlisted.
- [ ] Sentinelas não aparecem em logs, erros, envelopes, cache ou accounting; OpenRouter e STT
  continuam sem delta.

**Validation:**

```bash
poetry run pytest -q tests/services/test_event_service_llm_fallback.py tests/services/test_event_service_config_cache.py tests/services/test_multimodal_transcription.py tests/api/test_webhook_egress.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_uazapi.py
poetry run mypy src/services/multimodal_service.py src/llm/client.py src/services/event_service.py src/api/routes/webhook.py --ignore-missing-imports
make quality
```

Escopo proporcional: os testes focados cobrem ambos os failovers, os três consumidores de áudio e
os efeitos negativos; `make quality` permanece obrigatório por alterar runtime compartilhado.

**depends-on:** group-1

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] QA confirma primeiro a referência/digest do contrato NeuraLake; sem ela não executa runtime,
  não cria fixture e retorna `BLOCKED`.
- [ ] Testes mockados mostram texto Dropout zero/uma vez conforme o primary e áudio OGG/Opus com
  bytes idênticos em Z-API, Meta e Uazapi.
- [ ] Preflight reject não toca DB/cache/event/network; falha após request Dropout produz somente
  accounting/log redigidos e, em áudio, a recondução textual existente.
- [ ] Não existe card/configuração/PATCH/readiness Dropout neste delta, e a ordem pública permanece
  inalterada independentemente do delta STT.
- [ ] Nenhum log/resposta contém key, Authorization, áudio, payload ou response body brutos.
- [ ] Nenhum teste, UI ou documentação declara probe live ou homologação.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| O contrato autoritativo de áudio NeuraLake não existe ou não cobre OGG/Opus raw | High | Blocker atual; nenhum código inicia. Retornar a design/decisão ou escolher outra capacidade, sem inferir shape. |
| O artefato contradizer a shape `input_audio` usada pelo OpenRouter | High | Implementar somente a shape documentada ou replanejar; mocks são derivados do contrato, nunca o inverso. |
| Runtime interno ser confundido com adoção pública | High | Delta público zero com testes de invariância; catálogo introduz ID/PATCH/readiness/card e assurance depois. |
| Endpoint Neuralake cair na policy LLM Dropout já existente | High | Kind dedicado `multimodal_dropout` e testes negativos kind↔host. |
| Segredo ou áudio vazar em falha HTTP | High | Erros estáticos, redaction com sentinelas e proibição de body/header/contexto bruto. |
| Falha real disparar retry ou OpenRouter silenciosamente | High | Spies de request e critérios de exatamente um request/zero host alternativo; só recondução textual do áudio é permitida. |
| `origin/main` avançar antes da execução | Medium | Reabrir DRAFT, atualizar commit/tree e revalidar o delta antes de dispatch. |
| O wish antigo parecer dependência necessária | Medium | Baseline é o snapshot limpo de `origin/main`; `fallback-texto` não é dependência nem é editado. |

---

## Review Results

### Historical Plan Review — 2026-09-28 — BLOCKED / FIX-FIRST

- **Target SHA-256:** `07e9934e814124653eecf1620a73ed63fe57198020e971fb09be9cd23354b00c`
- **Finding:** o plano assumia `input_audio` OGG/Opus raw a partir de compatibilidade OpenAI e
  pretendia usar mocks como prova, embora as docs públicas atuais só descrevam Vision & Docs.
- **Finding:** antecipava ID/matriz/PATCH/readiness e assurance pública na dependência, confundindo
  adapter interno com a release unit de adoção do catálogo.
- **Finding:** pinava a PR #22 como baseline atual, misturava efeitos de preflight com falha runtime e
  não registrava proveniência completa do design governante.
- **Resolution neste fix loop:** adicionados Problem, design link+digest, snapshot limpo atual de
  `origin/main`, delta público zero/order-independent, envelopes de efeitos separados, remoção de
  config/PATCH/readiness/assurance e hard gate autoritativo antes de qualquer código.
- **Blocker restante:** o contrato NeuraLake autoritativo/versionado dos itens 1–6 ainda não foi
  fornecido. Este WISH e toda execução permanecem `DRAFT/BLOCKED`; esta resolução de plano não alega
  que o blocker externo foi satisfeito.

### Plan Re-review — 2026-09-29T01:37:48Z — BLOCKED

- **Target SHA-256:** `2df496a2b55048ca45574ace6dc0231b3d5a5a89aee97a9c4e3453c1358de8db`
- **Resolved:** proveniência do DESIGN, adoção/PATCH/readiness/assurance fora deste wish, delta
  público zero, baseline limpo atual e separação entre preflight e falha runtime.
- **Residual HIGH:** falta o artefato NeuraLake autoritativo/versionado que feche OGG/Opus raw,
  request/encoding, response e erros; mocks ou analogia com OpenRouter não satisfazem o gate.
- **Residual MEDIUM resolved after review:** o envelope agora permite a leitura integrada necessária
  do bundle e exige zero efeito/leitura adicional; somente o factory isolado afirma zero DB/cache
  read.

---

## Files to Create/Modify

Somente depois do hard gate e da revalidação do baseline:

```text
src/services/safe_egress.py
src/services/multimodal_service.py
src/llm/client.py
src/services/event_service.py
src/api/routes/webhook.py
tests/services/test_safe_egress.py
tests/services/test_multimodal_transcription.py
tests/llm/test_client.py
tests/services/test_event_service_llm_fallback.py
tests/services/test_event_service_config_cache.py
tests/api/test_webhook_egress.py
tests/api/test_webhook_zapi.py
tests/api/test_webhook_meta.py
tests/api/test_webhook_uazapi.py
```
