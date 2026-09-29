# Wish: Ativação STT OpenRouter e ElevenLabs Scribe

| Field | Value |
|-------|-------|
| **Status** | APPROVED |
| **Slug** | `stt-openrouter-scribe-activation` |
| **Date** | 2026-09-28 |
| **Author** | Lorenzo |
| **Appetite** | medium |
| **Branch** | `wish/stt-openrouter-scribe-activation` |
| **Repos touched** | neurostack-api |
| **Design** | [DESIGN.md](../../brainstorms/api-provider-catalog-presentation/DESIGN.md) — governing reviewed SHA-256 `025c397a0af4335e5266a3c19230d8a8a0ef405b818991a02ee4d1a9adc529f3` |

## Summary

Entrega integrações STT reais, configuráveis e deterministicamente testáveis para os IDs explícitos
`elevenlabs_scribe` e `openrouter`, com adapters, egress pinado, payloads e parsers específicos de
cada vendor. O trabalho preserva `stt/outros` como legado sem relabeling, permite somente sua saída
incumbent-only durante a compatibilidade e mantém a matriz pública byte a byte até a ativação conjunta
de `api-provider-catalog-presentation`; testes locais não afirmam homologação live.

## Problem

Os IDs `elevenlabs_scribe` e `openrouter` só podem tornar-se operacionais quando configuração canônica, dispatcher, egress, multipart e parser de ambos estiverem conectados e aprovados no mesmo grupo.

O runtime atual aceita apenas `outros` e `openai_whisper`, trata STT como OpenAI-compatible por URL e
lê texto cru. Esse caminho não satisfaz o OpenRouter atual, que responde JSON `.text`, nem o Scribe,
que usa outro path, header, nome de campo de modelo e parser. Ao mesmo tempo, simplesmente tornar os
IDs operacionais ou abrir writes antes de conectar o dispatcher criaria um estado intermediário
configurável porém inexequível; liberar adoção geral também violaria a Fase A e poderia relabelar
bundles `outros` arbitrários como OpenRouter.

O DESIGN governante exige IDs explícitos, bases canônicas, egress por provider, multipart correto,
parsing/erros/redaction e compatibilidade legada. Este wish entrega esse slice como dependência
order-independent: seu delta na matriz pública é exatamente vazio; o catálogo continua como único
dono da promoção agregada depois que todas as dependências, inclusive Dropout, estiverem integradas.

## Scope

### IN

- Definir os IDs canônicos STT `elevenlabs_scribe` e `openrouter`; manter `outros` como ID wire
  legado distinto, sem convertê-lo nem apresentá-lo como OpenRouter.
- Aplicar delta público exato `Δ PUBLIC_PROVIDER_AVAILABILITY = ∅`: nenhuma chave, ordem ou decisão
  `active|coming_soon` muda neste wish, independentemente de Dropout ou outra dependência aterrissar
  antes/depois. O catálogo calcula e publica a matriz integrada em sua própria release unit.
- Criar adapters e provas inertes primeiro; somente no grupo atômico de ativação reconhecer/validar
  `openrouter`, registrar Scribe/OpenRouter como operacionais/readiness e conectar o dispatcher em
  todos os callers, sem janela em que um ID operacional não tenha runtime.
- Classificar incumbency antes do merge pela visão `resolved_existing =
  resolve_stt_credentials(existing)`, nunca pelo raw field isolado. Estado realmente vazio é raw
  sem valor efetivo em `stt_provider`, `stt_api_url`, `stt_api_key`, `stt_model_name`,
  `whisper_api_key` e `whisper_model`, cuja visão resolvida também não possui provider. Legado
  implícito é raw com `stt_provider` ausente/falsy, pelo menos um valor efetivo nos outros cinco
  campos e visão resolvida `outros`; inclui rows alias-only, canonical-only e mistos. Legado
  explícito é raw com `stt_provider == "outros"` e visão resolvida `outros`.
- Permitir durante a compatibilidade uma única transição persistente: legado implícito ou explícito
  resolvido canonicamente como `outros` pode sair para `elevenlabs_scribe` ou `openrouter` com bundle
  fresco completo (`stt_provider`, `stt_api_url`, `stt_api_key`, `stt_model_name`) e base canônica.
- Detectar exact echo/no-op contra a visão canônica antes de aplicar o patch. Tanto o legado implícito
  quanto o explícito podem ecoar `outros`, URL/model resolvidos, secret mask e default resolvido de
  `audio_max_seconds` sem converter aliases, adicionar defaults ou alterar o JSONB raw.
- Rejeitar entrada explícita a partir de estado vazio ou outro provider, manutenção/reentrada em
  `outros`, switch parcial, secret mask/vazio e mutação de bundle explícito. Exact echo/no-op de
  qualquer incumbent reconhecido continua aceito sem write/evento; adoção geral só abre no catálogo.
- Garantir que toda rejeição 4xx acima ocorra sem `add`, `flush`, `commit`, cache invalidation,
  network/egress ou `api_credentials_changed`; rollback também não emite.
- Emitir uma tentativa at-most-once de `api_credentials_changed` após commit bem-sucedido e antes da
  response somente na saída incumbent-only efetiva. Payload allowlisted contém ator, tenant,
  capability/campos alterados, provider anterior/novo e `endpoint_class="fixed"`; não contém URL,
  host, path, query, model, valor, digest reversível, key ou secret.
- Incrementar `api_credentials_change_emit_failures_total` uma vez quando a emissão pós-commit
  falhar e manter o alerta `ApiCredentialsChangeEmissionFailures` com expressão
  `increase(api_credentials_change_emit_failures_total[15m]) > 0`, `for: 5m`, severity `warning`,
  owner `platform-sre` e
  `runbook_url=https://docs.neurostack.io/runbooks/api-credentials-change-emission-failures`.
  Falha de emissão não desfaz o commit nem vira auditoria.
- Fixar as bases persistidas em `https://api.elevenlabs.io/v1` para Scribe e
  `https://openrouter.ai/api/v1` para OpenRouter; rejeitar porta explícita, query, userinfo,
  fragmento, host/path alternativo ou endpoint de operação completo antes de mutação ou rede.
- Adicionar egress kinds distintos `stt_elevenlabs` e `stt_openrouter`, com allowlists exatas
  `api.elevenlabs.io` e `openrouter.ai`, DNS/IP público pinado, SNI correto e redirects desligados.
- Implementar adapters separados e um dispatcher por ID; manter o adapter OpenAI-compatible atual
  para `openai_whisper` e para o legado `outros` sem mudar seus IDs, parser ou semântica.
- Usar multipart como única representação dos dois adapters, com bytes inalterados, filename/extensão
  determinísticos e `Content-Type` MIME canônico; não usar data URI, JSON/base64 ou transcode.
- Normalizar MIME com `strip`, lowercase do media type e remoção de parâmetros após `;`. Cada
  parâmetro precisa ser um segmento não vazio com exatamente um `=` e nome/valor ASCII token não
  vazio; por exemplo ` Audio/OGG ; codecs=opus ` vira `audio/ogg`, enquanto `audio/ogg;`,
  `audio/ogg;codecs`, `audio/ogg;=opus`, `audio/ogg;codecs=` e segmentos duplicados vazios falham.
- Aceitar somente o mapa canônico MIME→extensão `audio/wav→wav`, `audio/mpeg→mp3`,
  `audio/flac→flac`, `audio/mp4→m4a`, `audio/ogg|application/ogg→ogg`, `audio/webm→webm` e
  `audio/aac→aac`; outro media type ou parâmetro malformado falha antes de DNS/HTTP.
- Para OpenRouter, chamar `POST /audio/transcriptions`, autenticar com `Authorization: Bearer`,
  enviar multipart `file`, `model` e `language` opcional e ler JSON estrito em `.text`.
- Para Scribe, chamar `POST /speech-to-text`, autenticar com `xi-api-key`, enviar multipart `file` e
  `model_id` e ler JSON estrito em `.text`; `stt_model_name` continua configurável e é repassado
  como `model_id` (por exemplo `scribe_v2`), sem versão oculta hardcoded.
- Rotear todos os ingressos de áudio já suportados pelo pipeline ao dispatcher e retirar apenas
  `elevenlabs_scribe` do conjunto sem adapter; Google, Deepgram e Microsoft continuam inertes.
- Cobrir configuração, telemetria/alerta, egress, multipart/auth/parser, MIME parametrizado, falhas e
  runtime com mocks determinísticos, incluindo o gate compartilhado `test_webhook_egress.py`.
- Atualizar documentação operacional sem executar probes live.

### OUT

- Modificar qualquer entrada da matriz pública, promover Scribe/OpenRouter a `active`, reorganizar
  cards ou entregar a UX final “Outros by OpenRouter”; isso pertence ao catálogo bloqueado.
- Fazer este wish aguardar ou inferir a cardinalidade global produzida por Dropout/WhatsApp/TTS; as
  dependências convergem porque deixam a matriz intacta e o catálogo é seu único writer.
- Implementar Google Cloud STT, Deepgram STT, Azure STT, novo fallback multimodal ou provider
  Anthropic/Claude direto.
- Suportar endpoint STT arbitrário pelo ID `openrouter`, transformar `outros` em alias de OpenRouter
  ou permitir entrada/reentrada/manutenção genérica do legado.
- Executar chamadas live, armazenar recibos de homologação ou alegar validação pelo vendor/equipe.
- Transcodificar áudio, aceitar MIME por sniffing, adicionar SDK, retry, fila, cache, health-check,
  migration de schema/tabela ou novo endpoint público.
- Alterar política de duração, ordem STT→multimodal→reroute, textos ao paciente ou semântica dos
  gatilhos de voz.
- Criar auditoria durável/exactly-once, outbox ou storage de eventos; a emissão é telemetria
  operacional at-most-once com janela de crash documentada.
- Implementar census, attestations, checker global ou rollout Fase A/B do catálogo.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | IDs novos são exatamente `stt/elevenlabs_scribe` e `stt/openrouter`; `stt/outros` permanece legado | Evita relabeling falso e permite egress/evidência por provider. |
| 2 | Delta da matriz pública é vazio | Remove cardinalidade/order dependence; somente o catálogo agrega e promove dependencies. |
| 3 | Incumbency usa o predicado completo de `resolve_stt_credentials(existing)` e a origem raw | Distingue vazio real de legados providerless alias-only, canonical-only ou mistos sem reescrever o JSONB. |
| 4 | IDs entram em recognized/operational/readiness/write junto com o dispatcher | Impede estado configurável ou anunciado sem runtime. |
| 5 | Bases são canônicas e imutáveis por provider explícito | Impede host/path spoof e remove a ambiguidade do antigo “Outros”. |
| 6 | Multipart é a única representação OpenRouter implementada | O DESIGN exige prova multipart e o pipeline já possui bytes crus; dois formatos duplicariam caminhos. |
| 7 | Scribe e OpenRouter têm adapters próprios | Auth, path, fields e resposta diferem; inferir protocolo pela URL repetiria o defeito atual. |
| 8 | Resposta de ambos é JSON estrito com `.text` não vazio | Texto cru atual aceitaria contrato inválido silenciosamente. |
| 9 | `stt_model_name` é obrigatório e configurável | Os vendors versionam modelos; o backend não deve esconder uma versão não governada. |
| 10 | MIME parametrizado é normalizado por gramática fechada | Meta envia `audio/ogg; codecs=opus`; multipart precisa de media type canônico e input malformado deve falhar pre-network. |
| 11 | Evento é pós-commit, pre-response e at-most-once | Dá atribuição operacional sem alegar durabilidade/exactly-once. |
| 12 | Testes com `httpx.MockTransport` provam ativação técnica | `active` não significa homologado live segundo o DESIGN. |

### Closed error taxonomy

Nenhum novo caminho externo pode emitir token fora desta tabela. Erros de safe-egress/httpx/vendor
são traduzidos na fronteira; body, headers, URL e chained exception não escapam.

| Token | Boundary | Meaning |
|-------|----------|---------|
| `invalid_provider_for_capability` | PATCH/schema | ID desconhecido para STT. |
| `provider_not_available` | PATCH/policy | Entrada explícita cujo existing resolvido/origem raw não é legado elegível, mutação de explícito ou entrada/reentrada em legado. |
| `provider_switch_bundle_required` | PATCH/policy | Saída `outros` parcial, com mask/vazio ou campo não fresco. |
| `invalid_provider_endpoint` | PATCH/runtime preflight | Base explícita não é a canônica exata. |
| `stt_configuration_invalid` | runtime preflight | Bundle persistido explícito está incompleto/corrompido. |
| `unsupported_audio_mime` | adapter preflight | Media type fora da allowlist ou parâmetro MIME malformado. |
| `stt_malformed_response` | adapter | JSON não objeto ou `.text` ausente/não string. |
| `stt_empty_transcript` | adapter | `.text` vazio após `strip`. |
| `stt_provider_http_error` | adapter | Resposta HTTP não-2xx, sem status/body refletido ao caller. |
| `stt_provider_transport_error` | adapter | Timeout/DNS/conexão após preflight, sem detalhe externo. |
| `stt_provider_unsupported` | webhook skip | Google, Deepgram ou Microsoft permanecem sem adapter e zero-effect. |

Falha de `api_credentials_changed` não altera a resposta nem cria novo token HTTP: incrementa apenas
o Counter/alerta allowlisted. No-op, 4xx, rollback e exact echo não emitem evento nem métrica de falha.

## Simplicity Case

- **Simplest complete design:** acrescentar um dispatcher fino sobre o transporte seguro existente,
  com um adapter por contrato externo, duas policies de host e uma transição incumbent-only no PATCH;
  o pipeline, JSONB e endpoint de credenciais permanecem os mesmos.
- **Added machinery:** módulo STT provider-aware, duas policies, normalizador MIME, errors allowlisted
  e telemetria pós-commit com Counter/alerta. Cada item fecha um requisito atual de protocolo,
  segurança, compatibilidade ou observabilidade do DESIGN/review.
- **Deferred until measured:** retry/backoff, streaming, diarização, timestamps, language detection,
  transcode e SDKs só entram com caso de produto separado; JSON/base64 do OpenRouter só volta se
  multipart deixar de ser suportado ou medição justificar a troca; auditoria exige outbox própria.
- **Complexity removed:** nenhum protocolo dinâmico, endpoint customizável para IDs explícitos,
  auto-discovery, fallback novo, probe live, migration, catálogo duplicado ou alias OpenRouter↔`outros`.

## Dependencies

**depends-on:** none
**blocks:** api-provider-catalog-presentation

O DESIGN do catálogo é contrato governante revisado, não uma aresta `depends-on`. Este wish e
`dropout-multimodal-fallback` convergem em qualquer ordem porque ambos devem manter a matriz pública
intacta; somente `api-provider-catalog-presentation` consome seus contratos e altera a matriz na
árvore integrada. Se o catálogo já tiver alterado a matriz quando este wish for integrado, o delta
deixa de ser isolável e exige rebase + re-review, nunca ajuste por cardinalidade presumida.

O handoff só destrava o catálogo após este wish estar `SHIPPED`, receber execution review independente
`SHIP` e ter os comandos fixos reexecutados na árvore integrada.

## Success Criteria

- [ ] `elevenlabs_scribe` e `openrouter` tornam-se IDs STT explícitos, reconhecidos e operacionais no
  mesmo commit/grupo que conecta o dispatcher; `outros` permanece legado separado.
- [ ] O diff deste wish não adiciona, remove, reordena nem altera estado em
  `PUBLIC_PROVIDER_AVAILABILITY`/matriz pública ou espelho frontend; `Δ public = ∅` independentemente
  da ordem das demais dependencies.
- [ ] Elegibilidade é calculada por `resolve_stt_credentials(existing)` mais origem raw: somente
  providerless com qualquer configuração STT efetiva (alias-only, canonical-only ou mista) ou
  `stt_provider="outros"` explícito, todos resolvidos como `outros`, podem trocar para
  Scribe/OpenRouter com quatro campos frescos e base canônica.
- [ ] Estado realmente vazio (visão resolvida sem provider), outro provider, bundle parcial,
  mask/vazio, manutenção de explícito e entrada/reentrada em `outros` retornam 422.
- [ ] Echo canônico de fixtures providerless alias-only, canonical-only e mista, além do legado
  explícito, retorna 200 sem write/evento e preserva o mapping JSONB raw exatamente, inclusive
  aliases e ausência de defaults persistidos.
- [ ] Cada 4xx acima prova zero add/flush/commit/cache/network/event e preserva o JSONB byte a byte;
  rollback e no-op/exact echo também não emitem.
- [ ] A transição aceita commita uma vez, tenta um `api_credentials_changed` depois do commit e antes
  da response e usa payload allowlisted/redigido; falha de emissão incrementa o Counter exatamente
  uma vez e não desfaz o commit.
- [ ] Alerta principal e espelho são byte a byte idênticos, parseáveis e possuem expressão, janela,
  severity `warning`, owner `platform-sre` e
  `runbook_url=https://docs.neurostack.io/runbooks/api-credentials-change-emission-failures` exatos.
- [ ] GET/eco/runtime de `stt/outros` incumbente permanecem byte a byte; nunca há rewrite/relabel.
- [ ] Scribe usa base `https://api.elevenlabs.io/v1`, `/speech-to-text`, `xi-api-key`, multipart
  `file` + `model_id` e parser JSON `.text`.
- [ ] OpenRouter usa base `https://openrouter.ai/api/v1`, `/audio/transcriptions`, Bearer, multipart
  `file` + `model` + `language` opcional e parser JSON `.text`.
- [ ] ` Audio/OGG ; codecs=opus ` e o valor real Meta `audio/ogg; codecs=opus` normalizam para
  `audio/ogg`; multipart envia `audio/ogg` e bytes originais. Parâmetros malformados falham pre-DNS.
- [ ] Egress kinds distintos pinam host/SNI/IP público; host spoof, query, userinfo, fragmento, porta
  ou base/path divergente falham antes de rede.
- [ ] Todo erro observável pertence à taxonomia fechada; malformed/empty, HTTP e transporte não
  expõem status/body/header/key/URL/áudio nem chained exception.
- [ ] Todos os handlers alcançáveis selecionam o adapter pelo ID; Google/Deepgram/Microsoft mantêm
  `stt_provider_unsupported` e zero-effect.
- [ ] `openai_whisper` e `outros` mantêm request/parser/fallback atuais; falhas operacionais válidas
  dos novos providers seguem STT→multimodal→reroute.
- [ ] `tests/api/test_webhook_egress.py`, gates por canal e `make quality` passam sem rede.
- [ ] Documentação registra contratos, ownership do catálogo e que mocks não são homologação live.

## Execution Strategy

O plano usa três ondas sequenciais. O Grupo 1 entrega somente primitives de transporte privadas e
sem caller de produção: pode ser integrado sem mudar recognized/operational/readiness/write. O Grupo
2 é a única unidade de ativação e liga atomicamente IDs, política incumbent-only, telemetria,
dispatcher e todos os callers. O Grupo 3 documenta e reexecuta o contrato integrado. Nenhuma onda
altera a matriz pública, depende da cardinalidade global ou executa deployment/probe live.

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 1 — primitives privadas com mocks determinísticos | `engineer-trivial` / low | Adapters, MIME, errors e egress sem registrar IDs operacionais. |

### Wave 2 (after Wave 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 2 — estado credencial/runtime e atomicidade (+2) | `engineer-standard` / high | Ativação atômica, incumbent-only, telemetria e webhooks. |

### Wave 3 (after Wave 2)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 0 — docs e gates determinísticos | `engineer-trivial` / low | Documentação, prova integrada e handoff ao catálogo. |

Não há mudança de prompt, CI/release, multi-package, OTel-label ou aceitação subjetiva.

## Execution Groups

### Group 1: Primitives privadas de adapter e egress

**Goal:** Implementar e provar transporte Scribe/OpenRouter sem tornar nenhum ID recognized,
operational, persistível ou alcançável por produção.

**Deliverables:**

1. Criar `src/services/stt_service.py` com adapters privados e dispatcher ainda sem caller; manter
   `src/services/groq_service.py` intocado como caminho de produção nesta onda.
2. Registrar `STT_ELEVENLABS_HOSTS`, `STT_OPENROUTER_HOSTS` e policies `stt_elevenlabs`/
   `stt_openrouter` em `safe_egress`, sem fallback custom-public.
3. Implementar normalização MIME: strip/lowercase do media type, validação fechada de cada parâmetro,
   descarte dos parâmetros válidos e lookup no mapa MIME→extensão. Multipart sempre usa o MIME
   canônico sem parâmetros e bytes crus.
4. Implementar contratos oficiais: OpenRouter com `/audio/transcriptions`, Bearer,
   `file`/`model`/`language`; Scribe com `/speech-to-text`, `xi-api-key`, `file`/`model_id`; ambos
   parseiam JSON `.text`.
5. Implementar somente os erros adapter/preflight da taxonomia e redigir/chaining externo.
6. Testar via `httpx.MockTransport` método, URL, host/SNI, auth, multipart real, bytes, filename,
   content-type, fields, parse e negativos. Incluir `audio/ogg; codecs=opus`, case/whitespace e cada
   parâmetro malformado; provar zero DNS/client/HTTP para MIME/base rejeitados.

**Acceptance Criteria:**

- [ ] Nenhum schema, recognized/operational set, readiness, PATCH ou webhook referencia os adapters
  ao final deste grupo; produção mantém comportamento anterior.
- [ ] Requests observados correspondem exatamente aos dois contratos e não usam JSON/base64.
- [ ] Bytes da parte `file` são idênticos aos de entrada; filename e Content-Type usam o MIME
  canônico derivado.
- [ ] `audio/ogg; codecs=opus` normaliza para `audio/ogg`; segmentos vazios, sem `=`, com nome/valor
  vazio ou mais de um `=` retornam `unsupported_audio_mime` antes de DNS/HTTP.
- [ ] Cada kind aceita apenas seu host exato e redirects permanecem desligados.
- [ ] Response/error tests cobrem todos os tokens adapter da taxonomia e nenhuma sentinela aparece
  em exception/caplog.

**Validation:**
```bash
poetry run pytest -q tests/services/test_stt_service.py tests/services/test_safe_egress.py
poetry run mypy src/services/stt_service.py src/services/safe_egress.py --no-error-summary
```

O escopo prova as primitives privadas sem alterar comportamento de runtime.

**depends-on:** none

---

### Group 2: Ativação atômica, incumbent-only e telemetria

**Goal:** Conectar IDs, configuração, dispatcher, callers e observabilidade numa única unidade
shippable, mantendo delta público vazio e todos os demais writes fail-closed.

**Deliverables:**

1. No mesmo diff, adicionar `openrouter` ao universo STT recognized/schema, adicionar Scribe e
   OpenRouter a operational/readiness/runtime projection e trocar `_stt_transcribe_audio` para o
   dispatcher. Não tocar na matriz pública/espelho frontend.
2. Resolver `existing` uma vez com `resolve_stt_credentials` antes de classificar a request; preservar
   separadamente a origem raw para distinguir empty real, providerless efetivo (alias-only,
   canonical-only ou misto) e `outros` persistido.
   Implementar esta matriz antes do merge/persistência:

   | Existing raw / visão canônica | Request efetiva | Resultado |
   |--------------------------------|------------------|-----------|
   | `{}` ou raw sem valor efetivo nos seis campos STT / resolved sem `stt_provider` | entrada em Scribe/OpenRouter | `provider_not_available`; não incumbent |
   | Providerless com qualquer valor efetivo em `stt_api_url`, `stt_api_key`, `stt_model_name`, `whisper_api_key` ou `whisper_model`; resolved `outros` | echo canônico de `outros`, mesmos valores resolvidos, mask e default 60 | 200; raw JSONB idêntico; zero write/evento |
   | Mesmo universo providerless efetivo, incluindo alias-only, canonical-only e misto; resolved `outros` | Scribe/OpenRouter + quatro campos fresh/canônicos | aceita atomicamente; evento anterior=`outros` |
   | raw `stt_provider="outros"` / resolved `outros` | exact echo canônico | 200; raw JSONB idêntico; zero write/evento |
   | raw `stt_provider="outros"` / resolved `outros` | Scribe/OpenRouter + quatro campos fresh/canônicos | aceita atomicamente; evento anterior=`outros` |
   | qualquer legado elegível / resolved `outros` | switch parcial, mask/vazio, base inválida ou manutenção de `outros` | erro específico; zero effects |
   | provider explícito ou qualquer outro / resolved não `outros` | entrada em Scribe/OpenRouter ou entrada/reentrada em `outros` | `provider_not_available` |
   | qualquer recognized incumbent | exact echo/no-op da própria visão canônica | 200 idêntico; zero write/evento |

3. Usar pelo menos estas fixtures providerless, todas resolvidas como `outros`: alias-only
   `{"whisper_api_key":"legacy-stt-secret","whisper_model":"legacy-whisper-model"}`;
   canonical-only
   `{"stt_api_key":"canonical-stt-secret","stt_model_name":"canonical-model","stt_api_url":"https://speech.example/v1"}`;
   e mista
   `{"stt_api_key":"canonical-stt-secret","whisper_model":"legacy-model","stt_api_url":"https://speech.example/v1"}`.
   Para cada uma, o echo envia a visão canônica (`stt_provider="outros"`, valores resolvidos, mask
   e default 60), mas o teste exige deep equality do JSONB raw e zero
   `add/flush/commit/cache/network/emitter`; switch fresh completo é aceito e switch parcial é
   rejeitado com os mesmos spies zerados. Uma quarta fixture com `stt_provider="outros"` cobre o
   legado explícito; `{}` cobre vazio real. Casos com somente um dos cinco campos efetivos também
   entram parametrizados para provar o predicado completo do resolver.
4. Validar bundle/base antes de `add`/`flush`/`commit`, cache, network ou evento; não herdar URL,
   key/model. Cobrir SessionSpy, cache spy, network spy e emitter spy em cada linha negativa.
5. Emitir `api_credentials_changed` após commit e antes da response somente na transição aceita,
   com payload fechado/redigido. No-op, 4xx, rollback e commit failure emitem zero vezes.
6. Incrementar `api_credentials_change_emit_failures_total` exatamente uma vez por falha de emissão;
   adicionar `ApiCredentialsChangeEmissionFailures` nos dois arquivos de regras idênticos e teste
   Python de YAML/expressão/labels/anotações, incluindo o literal
   `https://docs.neurostack.io/runbooks/api-credentials-change-emission-failures`. A response do
   PATCH permanece sucesso após commit.
7. Retirar `elevenlabs_scribe` de `UNSUPPORTED_STT_PROVIDERS`, reconhecer `openrouter` e manter
   Google/Deepgram/Microsoft no early return zero-effect.
8. Preservar STT→multimodal→reroute para erros operacionais válidos e manter OpenAI/`outros` no
   adapter atual sem cruzar novos auth/parsers.
9. Atualizar testes Z-API, Meta, Uazapi, Evolution e `test_webhook_egress.py`: dispatch correto,
   transcript, MIME Meta parametrizado, error redaction, providers ainda unsupported e ausência de
   reply/session/handoff/download nos early returns contratados.

**Acceptance Criteria:**

- [ ] Não existe commit testável onde ID esteja operational/readiness/write sem dispatcher e callers.
- [ ] O diff público é vazio e testes não dependem de contagem/ordem global.
- [ ] Incumbency nunca lê somente `existing.get("stt_provider")`: empty real, fixtures providerless
  alias-only, canonical-only e mistas, casos single-field e `outros` explícito exercitam
  `resolve_stt_credentials(existing)` e origem raw.
- [ ] Echos canônicos de todas as classes providerless e do legado explícito preservam deep equality
  do JSONB raw, inclusive aliases/ausência de defaults, com spies DB/cache/network/emitter zerados.
- [ ] A matriz de transição acima é exaustiva; cada negativo prova zero
  add/flush/commit/cache/network/event e JSONB idêntico.
- [ ] Switch aceito persiste bundle fresco completo, não herda campo e emite uma tentativa pós-commit
  pre-response com payload allowlisted.
- [ ] Falha do emitter incrementa Counter uma vez, mantém commit/response e satisfaz alerta; no-op,
  4xx, rollback e commit failure não incrementam nem emitem.
- [ ] Nenhuma sentinela de key/URL/path/query/model/value/digest aparece em evento, Counter label,
  alerta, GET, cache, erro ou caplog.
- [ ] Scribe/OpenRouter alcançam somente seu adapter; Google/Deepgram/Microsoft mantêm envelope
  `stt_provider_unsupported` antes de qualquer downstream effect.
- [ ] OpenAI/`outros` e fallbacks mantêm regressões atuais; Meta encaminha MIME parametrizado que o
  adapter normaliza sem transcode.

**Validation:**
```bash
poetry run pytest -q tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py tests/observability/test_api_credentials_change_alert.py tests/api/test_webhook_egress.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_evolution.py
poetry run mypy src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py src/services/stt_service.py src/services/groq_service.py src/services/safe_egress.py src/api/routes/webhook.py src/observability/metrics.py --no-error-summary
cmp config/prometheus_rules.yml src/config/prometheus_rules.yml
```

Este grupo toca schema, persistência, runtime compartilhado e observabilidade; por isso valida todas
as fronteiras afetadas juntas. O gate completo permanece no Grupo 3.

**depends-on:** group-1

---

### Group 3: Documentação, regressão integrada e handoff

**Goal:** Publicar o contrato reproduzível e entregar ao catálogo uma dependência auditável sem
promover UI ou alegar homologação.

**Deliverables:**

1. Atualizar `docs/configuration.md` com IDs, transition matrix, bundles, bases/suffixes, modelo,
   MIME normalization, auth, taxonomia, legado e links para as docs oficiais de
   [OpenRouter](https://openrouter.ai/blog/tutorials/transcription-on-openrouter/) e
   [ElevenLabs](https://elevenlabs.io/docs/api-reference/speech-to-text/convert).
2. Atualizar `docs/api-provider-settings-rollout.md` com semântica at-most-once, janela de crash,
   triagem do Counter/alerta, o runbook publicado exatamente em
   `https://docs.neurostack.io/runbooks/api-credentials-change-emission-failures` e backout que
   preserva readers/runtime dos IDs persistidos.
3. Registrar explicitamente que mocks demonstram contrato técnico, não homologação live; matriz,
   UI/card e adoção geral continuam ownership exclusiva do catálogo.
4. Rodar juntos testes de configuração, telemetria, transporte/runtime, `test_webhook_egress.py`, o
   teste frontend que protege ausência de promoção e o gate `make quality`.
5. No handoff/review, fornecer commit/tree digest e comandos fixos; manifesto nunca escolhe
   executável, argumentos, cwd ou shell.

**Acceptance Criteria:**

- [ ] Docs e código concordam em IDs, transições, bases, paths, auth, multipart/MIME, `.text`,
  taxonomia e telemetria.
- [ ] Documento diferencia operacionalidade, saída incumbent-only, promoção pública e homologação.
- [ ] Nenhum arquivo de matriz/UI possui mudança deste wish; o teste frontend continua passando.
- [ ] Suíte integrada, paridade das regras e `make quality` passam sem rede.
- [ ] Handoff é order-independent e suficiente para o pre-work gate do catálogo reexecutar a prova.

**Validation:**
```bash
poetry run pytest -q tests/services/test_stt_service.py tests/services/test_safe_egress.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py tests/observability/test_api_credentials_change_alert.py tests/api/test_webhook_egress.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_evolution.py
cmp config/prometheus_rules.yml src/config/prometheus_rules.yml
cd frontend/artifacts/neurostack && pnpm exec vitest run src/pages/clinic-config-api-settings.test.tsx && cd ../../..
make quality
```

Runtime/schema/observabilidade compartilhados justificam o gate completo; Vitest protege a fronteira
de ownership com o catálogo.

**depends-on:** group-2

---

## QA Criteria

_QA é determinístico e sem credenciais reais; valida contrato e regressão, não homologação live._

- [ ] MockTransport observa Scribe com `xi-api-key`, `file` e `model_id`; OpenRouter com Bearer,
  `file`, `model` e language, sempre no path/host exato.
- [ ] Fixture OGG com `audio/ogg; codecs=opus` chega byte a byte à parte multipart como `audio/ogg`
  e transcript `.text` volta ao pipeline.
- [ ] Cada MIME parametrizado malformado, endpoint hostil e response inválida falha com token fechado,
  no limite pre-network aplicável e sem sentinela em erro/log/response/cache/evento.
- [ ] Fixtures empty real, providerless alias-only, canonical-only, mistas/single-field e `outros`
  explícito provam classificação pela visão canônica mais origem raw, nunca pelo campo provider
  ou pelos aliases isoladamente.
- [ ] Apenas providerless efetivo ou legado explícito resolvido `outros` troca com bundle fresco;
  todas as demais linhas negativas provam zero DB/cache/network/event.
- [ ] Echo canônico de cada classe providerless e do legado explícito preserva raw JSONB por deep
  equality e não emite; switch aceito emite pós-commit uma vez.
- [ ] Counter/alerta disparam em falha de emissão sem rollback do commit e sem cardinalidade secreta.
- [ ] Scribe/OpenRouter continuam sem promoção pública até a release unit do catálogo.
- [ ] Google, Deepgram e Microsoft permanecem zero-effect no runtime atual.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| OpenRouter documenta JSON/base64 como preferido, além de multipart OpenAI-style | Medium | Escolher somente multipart, exigido pelo DESIGN e aceito oficialmente; MockTransport captura request real. |
| Adapter atual presume texto cru OpenAI-compatible | High | Não reutilizar seu parser nos IDs novos; adapters específicos exigem JSON `.text`. |
| Scribe usa auth/path/model field diferentes | High | Policy/adapter próprio com `xi-api-key`, `/speech-to-text` e `model_id`; testes rejeitam cruzamento. |
| Dropout ou catálogo aterrissar em outra ordem | High | Delta público vazio; se o catálogo já for writer da matriz, rebase/re-review em vez de cardinalidade global. |
| Incumbency usar só o raw `stt_provider` ou apenas aliases e excluir providerless canônico/misto | High | Aplicar o predicado completo do resolver e testar empty, alias-only, canonical-only, misto/single-field e `outros` explícito com deep equality e spies. |
| Emissão pós-commit falhar ou ser interpretada como auditoria | High | At-most-once, Counter/alerta, janela de crash documentada; sem rollback nem promessa durable. |
| MIME Meta parametrizado ser tratado como desconhecido | High | Normalizador fechado cobre case/whitespace/`codecs=opus`; malformed params têm negativos pre-network. |
| Modelo vigente mudar (`scribe_v2` ou ID OpenRouter) | Medium | `stt_model_name` é obrigatório e repassado; docs usam exemplo sem hardcode runtime. |
| Erro HTTP carregar corpo/header sensível | High | Taxonomia fechada sem chaining externo; sentinelas em body/header/key/URL nos testes. |
| ID explícito quebrar `outros` | High | Ativação atômica, nenhum rewrite/relabel e regressões separadas para adapter legado. |
| Mock ser interpretado como homologação | Medium | WISH/docs/review proíbem essa afirmação; probe live permanece OUT. |

Premissas externas verificáveis:

- OpenRouter documenta `POST /api/v1/audio/transcriptions`, Bearer, multipart OpenAI-style aceito e
  resposta JSON `.text`; este wish escolhe multipart, não JSON/base64.
- ElevenLabs documenta `POST /v1/speech-to-text`, `xi-api-key`, multipart `file` + `model_id` e
  resposta síncrona JSON `.text`.
- “Operacional” significa contrato local coberto por testes; não prova credenciais, quota, modelos ou
  formatos homologados em produção.

---

## Review Results

### Plan Review — FIX-FIRST (loop 1)

- **Target SHA-256:** `34fcb5a9d7fe2c52a47518c90fa3086d1194e31b6700f404c80499e80f2358ac`
- **Gaps:** cardinalidade global order-dependent; policy de escrita não limitada ao incumbent;
  ausência de telemetria/alerta; grupos permitiam ID operacional antes do dispatcher; MIME
  parametrizado não normalizado; gate runtime sem `test_webhook_egress.py`; taxonomia aberta;
  ausência de Problem e link governante no header.
- **Resolution:** este draft usa `Δ public = ∅`, matriz de transição exaustiva, evento pós-commit com
  Counter/alerta, primitives privadas antes de uma ativação atômica, gramática MIME parametrizada,
  gate egress explícito, taxonomia fechada, Problem e DESIGN digest-bound. O status permanece
  `DRAFT`; somente re-review independente pode persistir outro verdict.

### Plan Re-review — FIX-FIRST (loop 1, resolution loop 2)

- **Target SHA-256:** `253306125f43c49f16059d0f036c07e1d532effcd275ebdb9e8653703b5ae234`
- **Gaps:** incumbency ainda consultava o raw `stt_provider` e excluía legado implícito resolvido;
  a primeira frase de Problem não era normativa/testável; alerta/teste não fixavam o runbook literal;
  Wave 3 continha separador de tabela duplicado no target revisado.
- **Resolution:** incumbency agora combina `resolve_stt_credentials(existing)` com origem raw e uma
  matriz/fixtures para empty real, legacy-only `whisper_*` e `outros` explícito; exact echo canônico
  preserva o raw JSONB por deep equality e zera DB/cache/network/emitter; Problem abre com regra
  normativa; alerta/teste fixam
  `https://docs.neurostack.io/runbooks/api-credentials-change-emission-failures`; Wave 3 possui um
  único separador. O status permanece `DRAFT`; somente nova re-review independente pode aprovar.

### Escalation diagnosis after loop 2 — 2026-09-29T01:49:33Z — BLOCKED resolved in plan

- **Target SHA-256:** `a32dc3f411231a7f03e437fecf846dd8abaed2fa18fc5d3daa4a0ced72557a5b`
- **Residual HIGH diagnosed:** a definição de legado implícito ainda citava apenas `whisper_*`,
  embora o resolver também canonize rows providerless com `stt_api_url`, `stt_api_key` ou
  `stt_model_name`, inclusive combinações mistas.
- **Manual orchestration resolution:** o contrato agora usa exatamente o predicado completo do
  resolver e exige fixtures alias-only, canonical-only, mistas, single-field, explícitas e empty
  para echo, switch fresh e rejeição zero-effect. Não foi aberto um terceiro fix loop automático;
  um final gate independente deve decidir se o plano pode sair de `DRAFT`.

### Final Plan Gate — 2026-09-29T01:56:02Z — SHIP

- **Target SHA-256:** `61cba5bc5356c4e13588e4769081038947b9f9c2266541256750b6322275dddb`
- **Gaps:** nenhum; zero findings CRITICAL/HIGH/MEDIUM/LOW.
- **Evidence:** predicado completo de incumbency coincide com `resolve_stt_credentials`; fixtures
  cobrem empty, alias-only, canonical-only, misto, single-field e `outros` explícito; echo preserva
  JSONB raw, switch fresh é atômico, rejeições provam zero efeitos e grupos/gates são executáveis.
- **Outcome:** plano promovido de `DRAFT` para `APPROVED`; execution review independente continua
  obrigatória antes de `SHIPPED`.

---

## Files to Create/Modify

```text
src/services/api_credentials.py
src/api/schemas/config.py
src/api/routes/config.py
src/services/safe_egress.py
src/services/groq_service.py
src/services/stt_service.py
src/api/routes/webhook.py
src/observability/metrics.py
config/prometheus_rules.yml
src/config/prometheus_rules.yml
tests/services/test_api_credentials.py
tests/api/test_api_credentials.py
tests/api/test_config_credentials_boundary.py
tests/services/test_safe_egress.py
tests/services/test_stt_service.py
tests/observability/test_api_credentials_change_alert.py
tests/api/test_webhook_egress.py
tests/api/test_webhook_zapi.py
tests/api/test_webhook_meta.py
tests/api/test_webhook_uazapi.py
tests/api/test_webhook_evolution.py
docs/configuration.md
docs/api-provider-settings-rollout.md
```
