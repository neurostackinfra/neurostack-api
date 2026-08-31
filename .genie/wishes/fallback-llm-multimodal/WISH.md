# Wish: FallBack LLM Multimodal

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `fallback-llm-multimodal` |
| **Date** | 2026-08-29 |
| **Author** | Lorenzo |
| **Appetite** | large |
| **Branch** | `wish/fallback-llm-multimodal` |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/fallback-llm-multimodal/DESIGN.md) |

## Summary

Transformar o bundle `multimodal_*` existente em uma contingência comum para áudio e texto, sem
ampliar silenciosamente o uso autorizado por clínicas legadas. Com opt-in explícito, uma falha do
LLM principal aciona exatamente uma chamada multimodal text-only; o caminho de áudio atual continua
inalterado. O plano também separa a resposta aceita da contabilização de todas as chamadas reportadas
e apresenta a capacidade no painel como `FallBack LLM Multimodal`.

## Problem

Hoje a configuração multimodal foi autorizada apenas para áudio, o `EventService` não possui uma
segunda rota text-only e a telemetria single-usage não representa o custo reportado de respostas
primárias vazias seguidas de fallback. O defeito fica testável por quatro fronteiras: GET legado
deve materializar opt-in `false`; PATCH inválido deve responder 422 sem mutação parcial; com opt-in,
falha primária deve chamar o fallback uma vez; e primary vazio + retry + fallback deve persistir
três attempts ordenados cujo fold reproduz o total consumido por trace e builder sem double count.

## Scope

### IN

- Persistir `multimodal_text_fallback_enabled` como booleano estrito, não secreto e fail-closed:
  ausente/legado resolve para `false` e somente `true` explícito permite egress de prompt/histórico.
- Preservar `_load_runtime_llm_credentials` com exatamente quatro campos `llm_*` e criar um loader
  DB-only privado do `EventService` para bundles separados `primary` e `multimodal_text`.
- Criar uma factory text-only que valida bundle completo, provider operacional e coerência
  provider↔host↔`multimodal_openrouter`, sem downgrade para `llm_outros`.
- Considerar utilizável somente mapping com `content: str` não vazio após `strip()`; aplicar a
  política existente de até duas tentativas primárias e exatamente uma tentativa de fallback.
- Preservar `suggested_response`, `llm_usage` e `llm_model` como atribuição do resultado aceito e
  adicionar `llm_attempts`/`llm_usage_total` para toda usage válida reportada por todas as chamadas.
- Adaptar trace e estatísticas do builder para os metadados novos sem dupla contagem e com fallback
  compatível para eventos legados.
- Expor no painel o opt-in textual, o título exato `FallBack LLM Multimodal` e descrições PT/EN das
  contingências de LLM e STT.
- Cobrir roteamento, custo, safe-egress, segredo, persistência, compatibilidade legada, áudio e UI.

### OUT

- Fallback para falhas anteriores ou posteriores à geração: policy, SRM, CTX, construção de prompt,
  persistência, entrega WhatsApp ou TTS.
- Promover o multimodal a provedor primário quando nenhum cliente LLM principal estiver configurado.
- Terceiro degrau, round-robin, circuit breaker, fila, retry/backoff novo ou segunda tentativa
  multimodal.
- Handoff automático, mensagem fixa ou nova mutação de sessão quando os dois provedores falharem.
- Segunda chave multimodal, allowlist provider→model, mudança de readiness por modelo ou alteração
  do payload/recondução de áudio.
- Inventar tokens/custo para timeout, exceção ou resposta sem usage válido reportado pelo provedor.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Texto exige `multimodal_text_fallback_enabled is True`; legado/ausente continua somente áudio | Prompt e histórico são um novo uso de dados e não herdam consentimento dado ao fallback STT |
| 2 | O campo é não secreto, estrito e não anulável no PATCH; omissão preserva estado | Mantém PATCH parcial sem criar terceiro estado persistido |
| 3 | `_load_runtime_llm_credentials` continua com quatro campos | O helper também atende RLHF e não deve receber a chave multimodal |
| 4 | Um loader privado do `EventService` devolve bundles separados | Limita segredos por capability sem cache ou serialização pública |
| 5 | A seleção do fallback fica no `EventService` | O serviço já possui mensagens, histórico, resultado aceito e metadados de evento |
| 6 | A factory valida provider operacional↔host↔kind antes de anexar a chave | OpenRouter não pode cair na política genérica `llm_outros` |
| 7 | Não existe allowlist por modelo nem mudança de readiness | Preserva o caminho de áudio existente e evita política sem requisito atual |
| 8 | Resposta utilizável é mapping com `content: str` não vazio após `strip()` | Torna vazio, whitespace, tipo e shape inválidos determinísticos |
| 9 | Conteúdo primário inutilizável usa até duas tentativas; exceção/JSON malformado vai direto ao fallback | Preserva a política existente sem criar retry novo |
| 10 | O fallback é chamado no máximo uma vez e usa as mesmas `messages`/`max_tokens` | Limita custo/latência e impede loops |
| 11 | O trio legado pertence somente ao resultado utilizável aceito | Mantém o contrato de atribuição da resposta |
| 12 | `llm_attempts` registra cada chamada; `llm_usage_total` agrega toda usage válida reportada | Evita perder custo de primário vazio/retry e separa custo observado de resposta aceita |
| 13 | `llm_attempts.outcome` é `response|empty|invalid|error`, independente de `accepted`; `usage_status` é `reported|unavailable|invalid` | Separa qualidade da resposta, aceitação e confiabilidade de custo em enums fechados |
| 14 | Trace/builder preferem os campos novos e recaem no trio legado | Evita dupla contagem e preserva eventos antigos |
| 15 | O card mostra opt-in e o título exato `FallBack LLM Multimodal` | Comunica a capacidade dual e exige autorização explícita para texto |
| 16 | Usage normalizada aceita somente `prompt_tokens`, `completion_tokens`, `total_tokens` inteiros >= 0, excluindo boolean | Impede shape livre e tokens inventados; `total_tokens` só existe quando reportado |
| 17 | `requested_model` e `reported_model` são separados; breakdown usa o reportado quando válido, senão o solicitado | Não confunde modelo pedido com identidade devolvida pelo provedor |
| 18 | `llm_usage_total` é exclusivamente o fold determinístico das usages com status `reported` em `llm_attempts` | Evita duas fontes de verdade e proíbe mutação paralela do agregado |
| 19 | Cada attempt emite log/evento `llm_generation_attempt` e incrementa `neurostack_llm_generation_attempts_total` | Observabilidade verificável com labels limitadas a tier/outcome |

## Simplicity Case

- **Simplest complete design:** um booleano de opt-in, um loader privado, uma factory safe-egress e
  um ramo único de fallback no bloco atual de geração.
- **Added machinery:** `llm_attempts` e `llm_usage_total` são exigidos pelo custo real das tentativas;
  o breakdown evita atribuir chamadas rejeitadas ao modelo que produziu a resposta aceita. O toggle
  é o estado mínimo que distingue clínicas legadas de consentimento textual explícito.
- **Deferred until measured:** circuit breaker, backoff, múltiplos fallbacks, preço monetário e
  provedores separados por modalidade só entram quando houver métricas ou requisito de orçamento.
- **Complexity removed:** nenhuma tabela, migração relacional, fila, cache novo, máquina de estados,
  allowlist de modelo, retry adicional ou segundo adaptador de áudio.

## Dependencies

**depends-on:** none
**blocks:** `voice-response-triggers`

O HEAD-base `cf6a6bddc6a5ceaf380e7005e1e468b33b875e1b` já contém o fallback de áudio,
`multimodal_kind_for_url`, safe-egress pinado, readiness por provider e a UI que será renomeada.
Antes da execução, confirmar que esse commit continua ancestral. Não há dependência lifecycle de
`fallback-texto`; seus contratos necessários já estão integrados na árvore-base.

`voice-response-triggers` compartilha `src/api/schemas/config.py`,
`src/services/api_credentials.py`, `frontend/artifacts/neurostack/src/pages/clinic-config.tsx` e
testes de configuração: os dois trabalhos não podem executar concorrentemente. Este wish bloqueia
voz até pousar; depois, `voice-response-triggers` deve rebasear e revalidar os arquivos
compartilhados. `api-config-clarity` é somente baseline já integrado, não dependência ou trabalho
concorrente.

## Success Criteria

- [ ] Config legado/ausente, `false`, `null` ou valor não booleano do opt-in nunca envia
      prompt/histórico ao multimodal; apenas `true` explícito habilita texto.
- [ ] Salvar os demais campos multimodais não ativa o opt-in, e o áudio ignora o gate nos dois estados.
- [ ] GET HTTP de credencial legada sem campo retorna `multimodal_text_fallback_enabled=false` sem
      expor segredo; PATCH `true` e depois `false` faz round-trip em GET.
- [ ] PATCH HTTP com `null`, string ou número no opt-in retorna 422 e não persiste nenhuma outra
      alteração do mesmo payload; PATCH alheio ao campo preserva seu valor anterior.
- [ ] `_load_runtime_llm_credentials` continua retornando exatamente quatro campos `llm_*`; somente
      o loader privado do `EventService` obtém chave/bundle multimodal DB-only.
- [ ] Resposta utilizável do primário chama zero vezes o fallback.
- [ ] `None`, whitespace, non-string ou shape inválido usam no máximo duas tentativas primárias e,
      quando elegível, exatamente uma tentativa multimodal.
- [ ] Exceção HTTP/transporte/safe-egress ou JSON malformado vai diretamente a uma tentativa fallback.
- [ ] Sem cliente primário, sem opt-in ou sem bundle completo/operacional, o fluxo preserva o skip
      atual e não envia credencial a endpoint alternativo.
- [ ] Provider/host divergente, host custom, kind desconhecido ou `llm_outros` falha fechado antes
      de anexar/enviar a chave; clientes primários mantêm sua inferência atual.
- [ ] O fallback recebe as mesmas `messages` e `max_tokens=300` e nunca é repetido.
- [ ] Fallback vazio, inválido ou com exceção não cria resposta aceita, terceira chamada, handoff ou
      mutação extra de sessão.
- [ ] `suggested_response`, `llm_usage` e `llm_model` correspondem somente ao resultado aceito.
- [ ] Cada chamada iniciada gera uma entrada ordenada em `llm_attempts` com tier, ordinal, provider,
      `requested_model`, `reported_model`, outcome enum, `accepted` separado, `usage_status` enum e
      usage normalizada quando reportada; breakdown usa reported model válido, senão requested.
- [ ] Primário vazio com usage + retry com usage + fallback com usage soma as três chamadas exatamente
      uma vez em `llm_usage_total`; trace e builder não duplicam o total.
- [ ] `llm_usage_total` é recalculado somente como fold dos attempts `usage_status=reported`, nunca
      mutado em paralelo; aceita apenas os três token fields inteiros >=0 e não sintetiza
      `total_tokens` quando o provedor não o reporta.
- [ ] Timeout/exceção/usage ausente fica `unavailable`; shape, booleano, negativo ou não inteiro fica
      `invalid`, sem tokens inventados e sem invalidar conteúdo textual utilizável.
- [ ] Cada attempt emite um log/evento `llm_generation_attempt` e incrementa
      `neurostack_llm_generation_attempts_total` com labels somente `tier`
      (`primary|multimodal_fallback`) e `outcome` (`response|empty|invalid|error`); attempt,
      provider/model aparecem só no log/evento, nunca prompt, resposta, chave ou Authorization.
- [ ] Eventos antigos sem os campos novos continuam contabilizados por `llm_usage`/`llm_model`.
- [ ] Cache, GET/PATCH público e logs não expõem `llm_api_key`, `multimodal_api_key`, Authorization,
      prompt nem resposta.
- [ ] Testes existentes STT -> multimodal -> recondução passam sem mudança de comportamento.
- [ ] Registry e card usam exatamente `FallBack LLM Multimodal`; PT/EN mencionam falha do LLM e STT.
- [ ] O painel mostra o toggle textual desmarcado para legado, com cópia PT/EN sobre envio de prompt
      e histórico, preservando máscara de chave, providers, readiness e edição read-only.

## Execution Strategy

### Wave 1

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 4 — contrato HTTP (+2), loader/factory segura (+2) | engineer-standard / high | Fundação: contrato, loader capability-scoped e factory safe-egress |

### Wave 2 (Groups 2 e 3 em paralelo, after Group 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 3 — estado de formulário (+2), rework prévio (+1) | engineer-standard / high | Implementar opt-in e comunicação bimodal no painel |
| 3 | engineer | 5 — hot path/tentativas (+2), persistência (+2), schema de usage (+1) | engineer-complex / high | Roteamento e persistência de attempts/total |

### Wave 3 (after Group 3)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 4 — dois consumidores (+2), observabilidade (+2) | engineer-standard / high | Consumidores trace/builder e observabilidade concreta |

O backend de fundação pousa primeiro. Na Wave 2, Group 2 é exclusivamente frontend e Group 3
continua backend, portanto são disjuntos. `src/services/event_service.py` passa sequencialmente por
Group 1 (somente loader), Group 3 (roteamento/persistência) e Group 4 (emissão observável);
`tests/services/test_event_service_llm_fallback.py` passa de Group 3 para Group 4 apenas para
acrescentar asserções de observabilidade. Nenhum grupo é liberado isoladamente: o resultado só está
pronto depois do gate agregado e depois que Groups 2 e 4 concluírem.

## Execution Groups

### Group 1: Fundação de contrato, loader e factory

**Goal:** Fixar o opt-in HTTP fail-closed e preparar credenciais/factory de fallback sem ainda
alterar o hot path de geração.

**Deliverables:**
1. Adicionar `multimodal_text_fallback_enabled` ao schema/merge/status de credenciais como booleano
   estrito, default legado `false`, PATCH parcial e rejeição atômica de `null`/tipo inválido.
2. Cobrir em `tests/api/test_api_credentials.py`: GET legado=false e secret-free; PATCH true/false
   round-trip; `null`/string/número retornam 422 sem mutação parcial; PATCH alheio preserva o gate.
3. Preservar `_load_runtime_llm_credentials` com os quatro campos atuais e criar loader privado do
   `EventService` com bundles allowlisted `primary`/`multimodal_text`, sem cache ou reserialização.
4. Provar em `tests/api/test_rlhf.py` que o consumidor RLHF continua recebendo exatamente quatro
   campos e nunca a chave/bundle multimodal.
5. Estender `LLMClient` por caminho interno tipado de egress kind e criar factory multimodal que
   exige opt-in, bundle completo, provider operacional e provider↔host↔`multimodal_openrouter`.
6. Cobrir loader/factory, boundary de cache/GET e safe-egress sem alterar readiness por modelo ou
   comportamento de áudio.

**Acceptance Criteria:**
- [ ] Os seis casos HTTP do Deliverable 2 passam e comprovam default, round-trip, 422 atômico,
      preservação em PATCH alheio e resposta sem segredo.
- [ ] Loader RLHF mantém exatamente quatro campos; loader privado retorna somente os campos dos dois
      bundles e não vaza outra capability.
- [ ] Factory recusa provider/host/kind divergente, custom, desconhecido e `llm_outros` antes da chave.
- [ ] O grupo não altera roteamento, `credential_status`/readiness por modelo nem caminho de áudio.

**Validation:**
```bash
poetry run pytest tests/api/test_api_credentials.py tests/api/test_rlhf.py tests/llm/test_client.py tests/services/test_api_credentials.py tests/services/test_event_service_config_cache.py tests/services/test_safe_egress.py tests/api/test_config_credentials_boundary.py -v
poetry run mypy src/api/schemas/config.py src/llm/client.py src/services/api_credentials.py src/services/event_service.py --ignore-missing-imports
```

**Owned files:** `src/api/schemas/config.py`, `src/services/api_credentials.py`, `src/llm/client.py`,
região exclusiva dos loaders em `src/services/event_service.py`, `tests/api/test_api_credentials.py`,
`tests/api/test_rlhf.py`, `tests/llm/test_client.py`, `tests/services/test_api_credentials.py`,
`tests/services/test_event_service_config_cache.py`, `tests/services/test_safe_egress.py` e
`tests/api/test_config_credentials_boundary.py`.

**depends-on:** none

---

### Group 2: Painel do FallBack LLM Multimodal

**Goal:** Expor autorização textual fail-closed e comunicar corretamente as contingências de texto
e áudio sem alterar os campos, providers ou comportamento de áudio existentes.

**Deliverables:**
1. Adicionar estado/dirty flag para `multimodal_text_fallback_enabled`, carregando `false` quando
   ausente e enviando PATCH mínimo somente após edição explícita.
2. Renderizar toggle acessível com cópia PT/EN explicando que prompt e histórico textual serão
   enviados ao provedor configurado; preservar read-only, loading, save e máscara de segredo.
3. Trocar registry, card e textos relacionados para o título exato `FallBack LLM Multimodal` e
   descrições PT/EN que cubram falha do LLM principal e do STT.
4. Atualizar testes de API settings e multimodal para legado, toggle, PATCH mínimo, reload,
   acessibilidade, chave salva, título e descrições.

**Acceptance Criteria:**
- [ ] Status legado mostra toggle desmarcado e salvar outros campos não o ativa.
- [ ] Ativar/desativar envia somente o booleano quando nenhum outro campo foi editado e persiste após reload.
- [ ] Usuário read-only vê o estado e a explicação, mas não altera o toggle nem credenciais.
- [ ] A cópia PT/EN informa envio de prompt/histórico e as duas contingências.
- [ ] Registry e card exibem exatamente `FallBack LLM Multimodal` sem mudar ids, providers,
      readiness, opcionalidade, testids ou fresh-key behavior.
- [ ] Nenhuma máscara é reenviada como segredo e uma chave salva não precisa ser redigitada.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack test -- src/pages/clinic-config-api-settings.test.tsx src/pages/clinic-config-multimodal.test.tsx
pnpm --dir frontend/artifacts/neurostack run typecheck
pnpm --dir frontend/artifacts/neurostack run build
```

Escopo: testes focados fixam o contrato visual/formulário; typecheck e build cobrem o estado e a tela
compartilhada completos.

**Owned files:** `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`,
`frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx` e
`frontend/artifacts/neurostack/src/pages/clinic-config-multimodal.test.tsx`.

**depends-on:** Group 1

---

### Group 3: Roteamento e persistência de attempts/total

**Goal:** Implementar a cadeia principal→fallback e o contrato canônico de contabilização sem
alterar os consumidores agregados nesta onda.

**Deliverables:**
1. Construir `messages` uma vez, aplicar o predicado aprovado, manter até duas tentativas primárias
   por conteúdo inutilizável e chamar o fallback no máximo uma vez com as mesmas
   `messages`/`max_tokens=300`.
2. Fixar cada `llm_attempts` como: `tier=primary|multimodal_fallback`, ordinal `attempt`, provider,
   `requested_model`, `reported_model`, `outcome=response|empty|invalid|error`, `accepted` booleano,
   `usage_status=reported|unavailable|invalid` e usage normalizada.
3. Normalizar somente `prompt_tokens`, `completion_tokens`, `total_tokens` inteiros >=0 excluindo
   boolean; campo ausente não é sintetizado e shape/valor inválido marca `usage_status=invalid`.
4. Derivar `llm_usage_total` exclusivamente por fold determinístico dos attempts `reported` depois
   de coletados; nenhum ramo o incrementa ou muta em paralelo.
5. Persistir em `message.in` o trio aceito, attempts e total, mantendo `prompt.generated` como dono
   da resposta; modelo de breakdown é `reported_model` válido ou, na falta, `requested_model`.
6. Cobrir a matriz completa, inclusive primary vazio com usage + retry com usage + fallback com
   usage, fallback inválido com usage e timeout/erro unavailable.

**Acceptance Criteria:**
- [ ] Roteamento, chamadas exatas, mensagens/limite idênticos e falha dupla cumprem a matriz global.
- [ ] `outcome` nunca codifica aceitação; `accepted` é separado e no máximo uma entrada é true.
- [ ] Cada chamada iniciada produz uma entrada mesmo quando o await termina em timeout/exceção.
- [ ] Resultado vazio/inválido não entra no trio aceito, mas sua usage `reported` entra no fold.
- [ ] `total_tokens` agregado soma somente valores `total_tokens` reportados e nunca é inferido de
      prompt + completion.
- [ ] Sem cliente primário, opt-in ou bundle operacional, o comportamento anterior é preservado.

**Validation:**
```bash
poetry run pytest tests/services/test_event_service_llm_fallback.py tests/services/test_event_service_config_cache.py tests/services/test_multimodal_transcription.py -v
poetry run mypy src/services/event_service.py --ignore-missing-imports
```

**Owned files:** região de geração/persistência em `src/services/event_service.py` após o Group 1 e
`tests/services/test_event_service_llm_fallback.py`. O loader do Group 1 é consumido, não redesenhado.

**depends-on:** Group 1

---

### Group 4: Consumidores e observabilidade

**Goal:** Consumir attempts/total sem dupla contagem e tornar cada chamada observável com
cardinalidade limitada e sem dados sensíveis.

**Deliverables:**
1. Fazer trace e stats do builder preferirem `llm_usage_total` para total e `llm_attempts` para
   breakdown; eventos antigos recaem uma vez em `llm_usage`/`llm_model`.
2. Emitir um evento/log estruturado `llm_generation_attempt` por attempt com tier, ordinal,
   provider, requested/reported model, outcome, accepted e usage_status, sem prompt/resposta/chave.
3. Criar `neurostack_llm_generation_attempts_total` com labels exclusivamente `tier` e `outcome`;
   attempt/provider/model nunca viram label.
4. Cobrir consumers novo/legado, modelo reportado→solicitado, counter/log por outcome e asserções
   negativas para prompt, resposta, API key e Authorization.

**Acceptance Criteria:**
- [ ] Total de trace/builder coincide com `llm_usage_total` e não soma attempts novamente.
- [ ] Breakdown usa `reported_model` válido, senão `requested_model`; legado permanece compatível.
- [ ] Cada attempt incrementa o counter uma vez com tier/outcome fechados e emite um log/evento.
- [ ] Counter tem somente duas labels; logs/eventos não contêm prompt, resposta ou segredo.

**Validation:**
```bash
poetry run pytest tests/services/test_trace_service.py tests/api/test_builder.py tests/services/test_event_service_llm_fallback.py tests/observability/test_llm_generation_metrics.py -v
poetry run mypy src/services/event_service.py src/services/trace_service.py src/api/routes/builder.py src/observability/metrics.py --ignore-missing-imports
```

**Owned files:** `src/services/trace_service.py`, `src/api/routes/builder.py`,
`src/observability/metrics.py`, `tests/services/test_trace_service.py`, `tests/api/test_builder.py`,
`tests/observability/test_llm_generation_metrics.py`; depois do Group 3, somente o ponto de emissão
em `src/services/event_service.py` e as asserções observáveis no teste de fallback.

**depends-on:** Group 3

---

## Aggregate Integration Gate

Depois dos quatro grupos e respeitando o bloqueio de `voice-response-triggers`:

```bash
make quality
pnpm --dir frontend/artifacts/neurostack test
pnpm --dir frontend/artifacts/neurostack run typecheck
pnpm --dir frontend/artifacts/neurostack run build
```

O gate completo é proporcional à combinação de schema público, hot path de geração, contabilização,
safe-egress e painel. Qualquer falha mantém o grupo/wish aberto; baseline vermelho não é waiver.

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Clínica legada mantém fallback de áudio, mas falha do LLM principal não envia texto ao multimodal.
- [ ] Após opt-in explícito, falha controlada do LLM principal entrega a resposta multimodal uma vez.
- [ ] LLM principal saudável nunca cria/chama o fallback.
- [ ] Duas respostas primárias vazias mais fallback bem-sucedido geram três attempts e total correto.
- [ ] Falha dupla preserva a degradação atual sem loop, handoff ou terceira chamada.
- [ ] Áudio com STT indisponível continua usando a recondução multimodal existente nos dois estados do toggle.
- [ ] Painel PT/EN mostra título, descrição e consentimento corretos; read-only e reload preservam estado.
- [ ] Eventos, trace, builder, logs e métricas não expõem segredo, prompt ou resposta.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Clínica legada envia texto sem consentimento | High | Default false, PATCH explícito e testes negativos de runtime/UI |
| OpenRouter herda `llm_outros` | High | Factory pinada e testes de mismatch/custom/unknown antes da chave |
| Usage de retry/fallback desaparece ou duplica | High | Trio aceito separado de attempts/total e testes nos dois consumidores |
| Chave multimodal alcança RLHF/cache/GET/log | High | Loader privado allowlisted e suítes de fronteira/redaction |
| Shape inválido impede fallback ou causa crash posterior | High | Predicado único nos dois degraus e matriz de tipos/shapes |
| Opt-in altera caminho de áudio | Medium | Gate somente no text-only e regressão STT→multimodal→recondução |
| Nome antigo volta por conflito concorrente | Medium | Critério negativo e testes exatos de conteúdo PT/EN |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — FIX-FIRST

- **Reviewed content SHA-256:** `dc57fde2d426dfc01c5fede8a6e4d049f7ce7f90c49dbe5513740160245498cc`
- **Evidence captured at:** `2026-08-29T22:22:59Z`
- **Sources:** plan reviewer + Questioner handoff
- **Commands replayed:** `sha256sum .genie/wishes/fallback-llm-multimodal/WISH.md`;
  `git diff HEAD --check`; targeted `rg -n` over groups, commands, dependencies, schemas and files.
- **Verdict:** `FIX-FIRST`
- **Gaps:** HTTP opt-in/RLHF proofs and files absent; Group 1 not bite-sized; attempt/usage schema
  open-ended; observability unnamed; `voice-response-triggers` conflict unblocked; frontend commands
  used npm; Problem was implicit rather than testable.
- **Disposition:** remediated in this DRAFT; status remains DRAFT until independent re-review.

### Plan Re-review — SHIP

- **Reviewed content SHA-256:** `7e844444dc73c9084e564e6de093a099d342950d9fab7fe7a020f671e303a520`
- **Evidence captured at:** 2026-08-29T22:45:00Z
- **Reviewer:** genie:review (independent re-review after FIX-FIRST remediation)
- **Design digest reference:** DESIGN.md bounded design-review evidence block digest
  `7d1d4e31f10676a628b0b2c545f97602f06273c55c1e7eaef55477ba2f077b4e` (SHIP-stamped)
- **Base commit:** `cf6a6bd` (verified ancestor at HEAD)
- **Commands / checks run:**
  - `sha256sum .genie/wishes/fallback-llm-multimodal/WISH.md .genie/brainstorms/fallback-llm-multimodal/DESIGN.md`
  - `ls -la` on all 21 Files-to-Create/Modify — 19 exist; 2 (`tests/services/test_event_service_llm_fallback.py`,
    `tests/observability/test_llm_generation_metrics.py`) correctly declared as new
  - `ls -la frontend/pnpm-lock.yaml frontend/artifacts/neurostack/package.json` — both present; no root
    `package.json`/`package-lock.json`, so wish-linter no-op is correct
  - `git log --oneline -1 cf6a6bd` — base commit exists at HEAD
  - Read of `src/observability/metrics.py` — existing `neurostack_*_total` Prometheus Counter conventions
    confirm the new `neurostack_llm_generation_attempts_total` with `["tier","outcome"]` labels fits the
    module's style
  - Read of `.genie/wishes/voice-response-triggers/WISH.md` (Status APPROVED) — confirms real overlap on
    `src/api/schemas/config.py`, `src/services/api_credentials.py`,
    `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`, config tests; blocking is the correct
    resolution
  - Cross-check of WISH Decisions 1-19 against DESIGN Decisions 1-17 and Risks 1-11 — no contradiction;
    no provider→model allowlist reintroduced (WISH Decision 7, Scope OUT, Success Criterion); WISH
    #12-19 are legitimate implementation-level elaborations of DESIGN #8/16 and Risk #3
  - Internal consistency: Execution Strategy (Wave 1: Group 1; Wave 2: Groups 2+3 parallel; Wave 3: Group 4)
    matches Execution Groups depends-on chain (2→1, 3→1, 4→3)
- **Verdict:** SHIP
- **Gaps:** none at CRITICAL/HIGH/MEDIUM/LOW
- **Remediation verification of prior FIX-FIRST gaps:**
  1. HTTP opt-in/RLHF proofs — CLOSED (Group 1 Deliverable 2: six HTTP cases in `tests/api/test_api_credentials.py`;
     Deliverable 4: four-field RLHF proof in `tests/api/test_rlhf.py`)
  2. Group 1 bite-sized — CLOSED (foundation only; routing deferred to Group 3; disjoint from Group 2 frontend)
  3. Attempt/usage schema open-ended — CLOSED (Decisions 13/16/17/18 define closed enums and closed token fields)
  4. Observability unnamed — CLOSED (Decision 19 + Group 4 name log/event `llm_generation_attempt` and counter
     `neurostack_llm_generation_attempts_total` with labels `tier`/`outcome`)
  5. `voice-response-triggers` conflict — CLOSED (Dependencies declares `blocks`; shared surfaces enumerated;
     rebase mandated)
  6. Frontend commands use npm — CLOSED (all frontend commands use `pnpm --dir frontend/artifacts/neurostack ...`)
  7. Problem implicit — CLOSED (Problem section defines four testable boundaries)
- **Next step:** orchestrator advances wish to `APPROVED` and dispatches `work`.

### Execution Review — Group 1 — SHIP

- **Reviewed commit:** `e268311` (branch `wish/fallback-llm-multimodal`)
- **Base commit:** `cf6a6bd` (verified ancestor)
- **Reviewer:** genie:review (read-only execution review, quality pass included)
- **Evidence captured at:** 2026-08-29
- **Commands / checks run:**
  - Diff-scope enforcement: exactly the 11 declared owned files touched, zero scope creep
  - Byte-for-byte confirmation `_load_runtime_llm_credentials` and hot-path `_convert_pipeline_result_to_response` untouched
  - Confirmation `SECRET_FIELDS` and `runtime_projection()`'s allowed set remain uncontaminated by `multimodal_text_fallback_enabled`
  - Step-by-step trace of the new factory's fail-closed check ordering (gate → provider → base_url → egress kind → redundant provider check → bundle/key presence last)
  - Validation of the HTTP test cases (default/secret-free, round-trip, atomic 422, unrelated-PATCH preservation)
  - Orchestrator-supplied evidence: `poetry run pytest tests/api/test_api_credentials.py tests/api/test_rlhf.py tests/llm/test_client.py tests/services/test_api_credentials.py tests/services/test_event_service_config_cache.py tests/services/test_safe_egress.py tests/api/test_config_credentials_boundary.py -v` — 267 passed, 4 warnings in 10.28s; `poetry run mypy src/api/schemas/config.py src/llm/client.py src/services/api_credentials.py src/services/event_service.py --ignore-missing-imports` — 27 pre-existing errors unrelated to this diff (`config.py`/`api_credentials.py` mypy-clean)
- **Verdict:** SHIP
- **Gaps:** none (CRITICAL / HIGH / MEDIUM / LOW all clean)
- **Non-gaps noted (by design):** redundant defensive provider check in the factory is intentional future-proofing; the new private loader has zero call sites yet — correctly deferred to Group 3
- **Acceptance Criteria for Group 1 (all met):**
  1. The six HTTP cases from Deliverable 2 pass and prove default, round-trip, atomic 422, preservation on unrelated PATCH, and secret-free response
  2. RLHF loader keeps exactly four fields; private loader returns only the two bundles' fields and leaks no other capability
  3. Factory rejects divergent/custom/unknown provider-host-kind and `llm_outros` before the key
  4. The group changes no routing, `credential_status`/per-model readiness, or audio path
- **Next step:** orchestrator may proceed with Wave 2 (Groups 2 and 3 in parallel)

### Execution Review — Group 3 — SHIP

- **Reviewer:** independent `genie:review` execution re-review
- **Evidence captured at:** 2026-08-29
- **Validation:** `poetry run pytest tests/services/test_event_service_llm_fallback.py tests/services/test_event_service_config_cache.py tests/services/test_multimodal_transcription.py -v` — 108 passed, 4 warnings; `git diff --check` passed.
- **Type check:** the required mypy invocation reports 25 diagnostics, all verified pre-existing outside Group 3 additions.
- **Verdict:** SHIP
- **Gaps:** none. The review confirmed bounded primary/fallback routing, canonical attempts/usage fold, accepted-result ownership, and skip preservation.

### Execution Review — Group 4 — SHIP

- **Reviewer:** independent `genie:review`, followed by one targeted fix/re-review
- **Evidence captured at:** 2026-08-29
- **Validation:** focused consumer/metrics suite — 89 passed; targeted redaction test — 4 passed; `git diff --check` passed.
- **Type check:** the required mypy scope reports 47 diagnostics, all verified pre-existing outside Group 4 additions.
- **Verdict:** SHIP
- **Gaps closed:** explicit negative structured-event assertions for both `response` and `Authorization`; counter labels verified as exactly `tier` and `outcome`.

### Execution Review — Group 2 — BLOCKED

- **Code review:** SHIP after removing an added test id that violated the no-testid-change criterion.
- **Validation blocker:** each required `pnpm --dir frontend/artifacts/neurostack` command fails before its script starts with `ERR_PNPM_WORKSPACE_PKG_NOT_FOUND`: the declared and lockfile-pinned `@workspace/api-client-react@workspace:*` package expects `frontend/lib/api-client-react`, which is absent from this worktree.
- **Disposition:** task remains `in_progress`; restoring/providing that workspace package or equivalent approved validation environment is required before the aggregate gate.
- **Equivalent-environment attempt:** user authorized a temporary fixture from the primary checkout plus a frozen-lockfile install using a dedicated `/tmp` pnpm store. All 433 packages were already cached (zero downloads), but pnpm could not complete linking on the mounted worktree (`ENOTEMPTY` under `frontend/node_modules/.pnpm`) before any Vitest/typecheck/build script started. The fixture and transient modules were removed; `git diff --check` remains clean.

### Group 2 — Environment Blocker Resolved — SHIP

- **Evidence captured at:** 2026-08-30
- **Root cause:** `frontend/lib/api-client-react` and `frontend/artifacts/neurostack/src/lib/utils.ts` are both caught by the repo's blanket `.gitignore` `lib/` rule, so a fresh git worktree never receives them even though they are legitimate committed-elsewhere source/generated content the frontend build depends on. This is a pre-existing worktree/gitignore gap, not a Group 2 code defect.
- **Fix:** rsynced both directories from the primary checkout into the worktree (untracked, correctly left uncommitted since they are intentionally gitignored in the primary checkout too), then `pnpm install --ignore-scripts` followed by a normal `pnpm install` to clear a first-install lifecycle-script quirk.
- **Validation (fresh run against current committed state, commit `76e1211`):**
  - `pnpm --dir frontend/artifacts/neurostack test -- src/pages/clinic-config-api-settings.test.tsx src/pages/clinic-config-multimodal.test.tsx` — 7 test files, 65 passed
  - `pnpm --dir frontend/artifacts/neurostack run typecheck` — clean, zero errors
  - `PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build` — built in 1m6s (only pre-existing sourcemap/chunk-size warnings, non-fatal)
- **Verdict:** SHIP — the prior BLOCKED verdict was an environment gap, now resolved; no code changes were needed in Group 2's owned files.

### Cross-cutting fix — pytest session event-loop poisoning — HIGH gap closed

- **Evidence captured at:** 2026-08-30
- **Finding:** an independent reviewer covering Groups 2/3/4 together (widening scope to include `tests/core/test_pipeline.py`, which calls the Group 3-modified `_convert_pipeline_result_to_response`) found that running the fallback test files ahead of `test_pipeline.py` in one pytest session caused 15 failures in `test_pipeline.py` with `RuntimeError: There is no current event loop in thread 'MainThread'`.
- **Root cause:** `tests/services/test_multimodal_transcription.py` (pre-existing, unrelated to this wish) calls `asyncio.run(...)` directly in ~30 sync test bodies. `asyncio.run()` clears the process-wide current event loop on exit (`asyncio.set_event_loop(None)`), which poisons any later test in the same session relying on `asyncio.get_event_loop()` resolving `tests/conftest.py:158`'s session-scoped `event_loop` fixture. The new Group 3 test file shifted alphabetical collection order so this pre-existing landmine now sits ahead of `test_pipeline.py` in the wish's own composite validation commands.
- **Fix:** added an autouse fixture in `tests/services/test_multimodal_transcription.py` that restores the session event loop after each affected test — commit `76e1211`. Independently reproduced the 15 failures before the fix and confirmed 0 failures after (`177 passed`) running the exact composite: `poetry run pytest tests/services/test_event_service_llm_fallback.py tests/services/test_event_service_config_cache.py tests/services/test_multimodal_transcription.py tests/services/test_trace_service.py tests/api/test_builder.py tests/observability/test_llm_generation_metrics.py tests/core/test_pipeline.py -q`.
- **Scope note:** the touched file is outside every group's declared "Owned files," but the bug it fixes is a pre-existing, repo-wide test-isolation hazard exposed (not caused) by this wish's new test file; fixing it in place was judged lower-risk than leaving a HIGH-severity gap open or expanding into `tests/conftest.py`'s shared fixture.

### Aggregate Integration Gate — pre-existing full-suite failures (not wish-caused)

- **Evidence captured at:** 2026-08-30
- **Finding:** `make quality` (via its `test` target, `poetry run pytest tests/ -v`) currently reports 251 failed / 1186 passed on this branch.
- **Isolation check:** `git stash` of the event-loop fix reproduced the identical 251 failures (same test names), ruling out that fix as the cause. A disposable detached worktree at `cf6a6bd` — the wish's own verified base commit, before any Group 1-4 change — was built (`poetry run pytest tests/services/test_event_service_emission.py tests/services/test_meta_service_egress.py tests/services/test_event_service_config_cache.py -q`) and already shows 19 of the same failures there, confirming these are pre-existing, repo-wide (likely cross-test DB/fixture pollution when the full suite runs together) and unrelated to this wish's diff.
- **Correction:** an earlier version of this entry reported `black`/`mypy`/frontend-test numbers gathered after a `cd` outside the repo caused the shell to reset to the primary checkout (`wish/api-config-clarity` branch) instead of this worktree — those numbers described the wrong branch and are superseded below.
- **Format-check — real, in-scope gap, now fixed:** re-run correctly scoped to this worktree, `poetry run black --check src tests` showed 168/239 files needing reformatting, but 11 of them were wish-owned (`src/api/routes/builder.py`, `src/observability/metrics.py`, `src/services/event_service.py`, `src/services/trace_service.py`, and 7 owned test files) — not pre-existing debt but the wish's own unformatted diff. Fixed with `poetry run black`/`isort` on exactly those 11 files (commit `233614a`), then re-ran every group's declared pytest/mypy validation command unchanged (Group 1: 267 passed; Group 3: 112 passed; Group 4: 89 passed; mypy 27/25/47 pre-existing diagnostics, identical counts before/after) to confirm the reformat was purely cosmetic. `black --check` and `isort --check-only` are now clean on all 11 wish-owned files. The remaining 157 non-wish files are unrelated pre-existing debt (`tests/load/`, `tests/srm/`, `tests/prompt/`, `tests/handoff/`, `tests/policy/`, `tests/memory/`, etc.).
- **Type-check — pre-existing, unrelated:** correctly scoped, `poetry run mypy src --ignore-missing-imports` reports 369 errors in 74 files repo-wide; the wish-owned files' share (47, across `event_service.py`/`trace_service.py`/`builder.py`/`metrics.py`) was already verified pre-existing per each group's own execution review.
- **Frontend full suite — confirmed clean:** correctly scoped, `pnpm --dir frontend/artifacts/neurostack test` (no file filter, all 7 test files) — 65/65 passed, matching Group 2's scoped run exactly.
- **Disposition:** the four groups' own declared, risk-scoped validation commands all pass cleanly, `format-check` is now clean for every wish-owned file, and the full frontend suite is green. The literal `make quality` full-suite `test` step (`poetry run pytest tests/ -v`, 251 failed) and the remaining non-wish `mypy`/`black` debt are red for reasons outside this wish's scope and predating it (see the isolation check above against base commit `cf6a6bd`). Per this wish's own stated policy ("baseline vermelho não é waiver"), this is **not** treated as passed — it is a genuine open blocker on the Aggregate Gate's `make quality` command requiring an explicit human decision (fix the pytest full-suite isolation debt as separate work, scope the gate down to changed files, or explicitly accept/waive it) before the wish can move to SHIPPED.

---

## Files to Create/Modify

```
src/api/schemas/config.py
src/llm/client.py
src/services/api_credentials.py
src/services/event_service.py
src/services/trace_service.py
src/api/routes/builder.py
src/observability/metrics.py
tests/api/test_api_credentials.py
tests/api/test_rlhf.py
tests/llm/test_client.py
tests/services/test_api_credentials.py
tests/services/test_event_service_config_cache.py
tests/services/test_event_service_llm_fallback.py
tests/services/test_trace_service.py
tests/services/test_safe_egress.py
tests/api/test_builder.py
tests/api/test_config_credentials_boundary.py
tests/observability/test_llm_generation_metrics.py
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-multimodal.test.tsx
```
