# Design: Central de Provedores na Aba APIs

| Field | Value |
|-------|-------|
| **Slug** | `api-provider-settings` |
| **Date** | 2026-08-05 |
| **WRS** | 100/100 |

## Problem

As integracoes por clinica estao fragmentadas entre a aba APIs e o menu "Inteligencia de Voz", enquanto
credenciais possuem mais de uma fronteira publica e os webhooks aceitam egress e callbacks sem uma politica
unica de confianca. A entrega precisa reunir WhatsApp, LLM principal, fallback multimodal, STT e TTS numa
unica tela, preservar STT legado e fechar essas fronteiras sem confundir credencial salva com runtime ativo.

## Scope

### IN

- Reorganizar a aba de id `integracoes` de `clinic-config.tsx`, nesta ordem: WhatsApp, LLM Provider,
  Fallback Multimodal e Modulo Voz.
- Preservar os seis provedores WhatsApp, campos especificos, webhooks e comportamento runtime atuais.
- Preservar os cards LLM atuais: `openai`, `dropout` e `outros` continuam configuraveis e operacionais;
  `slm_cortex` continua como card nao interativo `Em breve`.
- Adicionar configuracao independente de fallback multimodal com OpenRouter, Groq e OpenAI.
- Exibir STT com Groq, ElevenLabs, OpenAI, Deepgram, Google e Microsoft, preservando ids estaveis.
- Adicionar configuracao TTS com ElevenLabs, Microsoft e Google.
- Persistir provider, URL, chave e modelo por capacidade no JSONB `api_credentials` por
  `GET/PATCH /v1/config/api-credentials`, com mascaramento, compatibilidade STT e validacao fail-closed.
- Tornar `/v1/config/api-credentials` o unico reader/writer publico do bucket; impedir leitura ou mutacao de
  credenciais por `GET/PATCH /v1/config`.
- Tipar o PATCH por capacidade, limitar tamanhos, normalizar/validar URLs e aplicar redaction universal.
- Compor fallback LLM somente a partir do builder owner correto, sem substituir WhatsApp ou STT da clinica.
- Autenticar callbacks Meta por assinatura e Z-API por capability URL dedicada, antes de qualquer efeito.
- Exigir `meta_app_secret` para readiness/runtime Meta e limitar callbacks autenticados por tenant+channel.
- Aplicar uma unica politica SSRF/egress a URLs configuradas e midia remota.
- Versionar a entrada do cache existente e armazenar somente a projecao sem segredos.
- Implementar o contrato visual fechado/expandido dos cards e o placeholder desabilitado de gatilhos.
- Remover o item "Inteligencia de Voz" e redirecionar `/voice-ai` para `/config?tab=integracoes`.
- Cobrir contrato backend, autorizacao definida, interacoes frontend, responsividade e caminhos runtime STT.

### OUT

- Implementar uso runtime de fallback multimodal ou TTS.
- Implementar adaptadores runtime STT para ElevenLabs, Google, Deepgram ou Microsoft nesta entrega.
- Implementar gatilhos de voz, prioridade, retries, health checks ou varios providers por capacidade.
- Alterar a politica tenant API-key fora de `/v1/config/api-credentials` ou ampliar os tres roles permitidos.
- Permitir tenant API key em `/v1/config/api-credentials`; esse endpoint e JWT-only.
- Aceitar endpoints privados, loopback, link-local ou metadata em URLs customizadas nesta entrega.
- Criar lock otimista: PATCHes simultaneos de escritores com estado antigo continuam last-write-wins.
- Reimplementar WhatsApp, migrar para cofre externo ou trocar JSONB por tabelas relacionais.
- Criar registry/codegen de metadata Python/TypeScript, cache adicional ou novos adapters.
- Extrair o formulario inteiro para novos hooks/pass-throughs; mover o formulario como unidade e opcional.
- Adicionar estado extra para rotacao Z-API, eliminar o downtime aceito ou prometer dedupe alem de 31 dias.
- Criar rate-limit store/setting/persistencia especificos de webhook.

## Approach

### Fronteira unica e helper de dominio

Cada capacidade conserva seu namespace no `api_credentials`: `llm_*`, `multimodal_*`, `stt_*` e `tts_*`.
`GET/PATCH /v1/config/api-credentials` e o unico reader/writer publico desse bucket. `GET /v1/config` omite
o bucket e todos os campos de credencial; `PATCH /v1/config` rejeita qualquer membro de
`API_CREDENTIAL_FIELDS`, uniao derivada dos modelos tipados, aliases legados e todo campo de credencial hoje
mapeado em `FIELD_TO_BUCKET`. A resposta e HTTP 422 com
`detail.error = "credential_endpoint_required"`, `fields` ordenados, e zero mutacao.

Um modulo puro de dominio, sem FastAPI, DB ou Redis, possui: schema/allowlists; validacao e merge atomico do
PATCH; regra fresh-key; redaction/status; limpeza de aliases; resolucao STT; composicao clinic+builder; e
projecao runtime/cache. Routes traduzem erros tipados para HTTP e controlam transacao; `event_service` apenas
carrega, compoe e projeta; webhooks consultam readiness. Isso evita dois writers e pass-throughs rasos.

Python e TypeScript mantem tabelas locais. Compartilham somente wire ids, readiness e codigos de erro,
fixados por teste de paridade; labels, icones, classes e defaults visuais ficam no TypeScript, enquanto
validacao/persistencia ficam no Python. Nao ha registry remoto, schema gerado ou codegen cross-language. O
componente compartilhado de card e somente apresentacao; nao possui metadata, selecao persistida, regras de
credencial ou disponibilidade runtime.

### PATCH tipado e allowlists

O payload flat atual permanece compativel, mas `ApiCredentialsPatch` e composto por modelos Pydantic por
capacidade, todos com `extra="forbid"`. Provider e enum da allowlist; URLs sao strings HTTPS de ate 2048
caracteres; model names e identificadores possuem 1..255; segredos possuem 1..8192; o digest Z-API e
lowercase hex `[0-9a-f]{64}`; `audio_max_seconds` e inteiro 10..300. Strings nao secretas
sao trimadas; URLs sao normalizadas para scheme/host lowercase, porta default removida e trailing slash
final removida sem alterar path. Segredos nao sao trimados nem ecoados.

Campo desconhecido, tipo/tamanho invalido ou combinacao invalida retorna HTTP 422 com
`detail.error = "invalid_credentials_payload"` e lista deterministica `{field, code}`, sem mutacao. A
validacao completa, allowlist, fresh-key e egress ocorre antes de atribuir `row.api_credentials`.

As allowlists exatas sao:

| Capacidade | ids aceitos |
|------------|-------------|
| WhatsApp | `meta_cloud`, `dialog360`, `twilio`, `zapi`, `evolution`, `uazapi` |
| LLM principal (UI) | `openai`, `dropout`, `outros`, `slm_cortex` (`Em breve`, nao persistivel) |
| Fallback multimodal | `openrouter`, `groq`, `openai` |
| STT | `outros`, `openai_whisper`, `elevenlabs_scribe`, `google_stt`, `deepgram_stt`, `microsoft_stt` |
| TTS | `elevenlabs`, `microsoft`, `google` |

Qualquer id fora da allowlist da propria capacidade retorna HTTP 422 com
`detail.error = "invalid_provider_for_capability"`, inclui `capability` e `provider`, e nao altera nenhum
campo. A allowlist nao torna funcional um provider apenas configuravel; WhatsApp conserva a falha explicita
ja existente para providers ainda sem integracao.

Para LLM, a allowlist de UI preserva `slm_cortex` apenas para selecao/exibicao do card atual. Um PATCH que
tente persistir `llm_provider = "slm_cortex"` retorna HTTP 422 com
`detail.error = "provider_not_operational"`, `capability = "llm"`, `provider = "slm_cortex"` e nenhuma
mutacao. Os unicos ids LLM persistiveis nesta entrega sao `openai`, `dropout` e `outros`.

### Matriz runtime/status do LLM principal

| id | UI e persistencia | runtime e status |
|----|-------------------|------------------|
| `openai` | Card add/edit; persiste provider, URL, key e model | Operacional OpenAI-compatible; runtime usa URL/key/model salvos; credenciais completas podem contribuir para `apis_configured` |
| `dropout` | Card add/edit; persiste provider, URL, key e model | Operacional OpenAI-compatible; runtime usa URL/key/model salvos; credenciais completas podem contribuir para `apis_configured` |
| `outros` | Card add/edit; persiste provider, URL, key e model | Operacional OpenAI-compatible; runtime usa URL/key/model salvos; credenciais completas podem contribuir para `apis_configured` |
| `slm_cortex` | Card nao interativo `Em breve`; sem add/edit e sem persistir provider/URL/key/model neste wish | Sem dispatch runtime e sem contribuicao para `apis_configured`; PATCH de provider falha 422 `provider_not_operational` |

O indicador frontend `APIs totalmente configuradas` reflete o `apis_configured` existente: somente um LLM
operacional com URL, key e model completos pode contribuir. Exibir ou selecionar localmente `slm_cortex`
nao configura o LLM.

### Segredos ao trocar provider ou URL

Para LLM operacional (`openai`, `dropout`, `outros`), multimodal, STT e TTS, mudar o provider persistido ou
a `*_api_url` que carrega sua credencial
exige uma `*_api_key` nova, nao vazia e nao mascarada no mesmo PATCH. Sem ela, o endpoint retorna HTTP 422
com `detail.error = "fresh_api_key_required"`, `capability` e `reason` igual a `provider_changed` ou
`api_url_changed`, sem mutacao parcial. Se ambos mudarem, `provider_changed` tem precedencia no erro.
Uma criacao inicial tambem requer chave nova quando grava provider/URL. A regra de fresh-key nao se aplica a
`slm_cortex`, pois esse id nao pode ser persistido. Atualizacoes do mesmo provider que
mudam somente modelo, ou outros PATCHes sem mudanca de URL, podem omitir a chave ou enviar a mascara e
preservam o segredo existente. A URL continua editavel conforme solicitado.

Antes do PATCH, a UI compara provider e URL com o ultimo snapshot confirmado pelo GET/save. Ao detectar
troca, limpa qualquer mascara, marca API KEY como obrigatoria, foca o campo e impede submit ate receber uma
chave sem mascara. O backend repete a validacao, portanto payload manual nao a contorna.

Preservacao cruzada significa merge de PATCHes sequenciais: salvar uma capacidade e depois outra preserva
campos e segredos nao enviados. Concorrencia simultanea entre escritores com snapshot antigo permanece OUT
e aceita last-write-wins; este wish nao adiciona versao, ETag ou lock.

### Composicao LLM clinic + builder

A clinica e sempre o objeto base. LLM completo significa provider operacional, URL, key e model presentes.
Se o LLM da clinica estiver incompleto e `Tenant.builder_id` existir, o owner e resolvido por
`User.builder_id == Tenant.builder_id` e `User.role == "builder"`; usa-se o unico `User.tenant_id` distinto
resultante. Zero ou mais de um tenant distinto e ambiguidade fail-closed: registra
`builder_llm_owner_ambiguous` sem segredo e nao herda de outro builder.

Somente se o tenant owner possuir LLM completo, copiam-se exatamente `llm_provider`, `llm_api_url`,
`llm_api_key` e `llm_model_name` sobre a clinica. Nunca se substitui o dict inteiro e nunca se herdam
WhatsApp, STT, multimodal ou TTS. A composicao acontece em memoria antes da projecao; o fallback global atual
continua quando nenhum LLM de clinica/builder valido existe. Testes com dois builders provam que a clinica A
nunca recebe campos do builder B e que seus campos nao-LLM permanecem byte-for-byte iguais.

### Compatibilidade e runtime STT

Os rotulos usam os ids estaveis: Groq = `outros`, OpenAI = `openai_whisper`, ElevenLabs =
`elevenlabs_scribe`, Google = `google_stt`, Deepgram = `deepgram_stt` e Microsoft = `microsoft_stt`.

Existe um unico resolver STT, usado pelo GET e pelos dois caminhos runtime Z-API e Meta:

1. `stt_api_key` e `stt_model_name`, quando presentes, vencem seus aliases legados mesmo se ambos existem.
2. Apenas quando o campo canonico correspondente esta ausente, `whisper_api_key` e `whisper_model` servem
   de fallback.
3. Um save canonico bem-sucedido de `stt_api_key` remove `whisper_api_key`; um save canonico bem-sucedido
   de `stt_model_name` remove `whisper_model`. Assim, um valor legado obsoleto nao reaparece.
4. Trocar `stt_provider` ou `stt_api_url` sempre exige `stt_api_key` canonica nova no mesmo PATCH; um alias
   legado nao satisfaz a regra.

Somente `outros` (Groq ou endpoint OpenAI-compatible na URL configurada) e `openai_whisper` sao operacionais
nesta entrega e mantem o pipeline normal. `elevenlabs_scribe`, `google_stt`, `deepgram_stt` e
`microsoft_stt` sao marcados visivelmente `Configuracao apenas` e possuem o mesmo short-circuit exato nos
webhooks Z-API e Meta: depois de carregar as credenciais do tenant e antes de resolver ou baixar a midia,
o handler registra warning `stt_provider_not_supported` com apenas `tenant`, `provider` e `channel` (nenhum
segredo) e retorna HTTP 200 com body exato
`{ "ok": true, "skipped": "stt_provider_not_supported" }`. Nesse caminho nao ha download de midia,
request outbound ao provider, resposta ao paciente, handoff nem mutacao de sessao.

### Autenticidade e replay de webhooks

Meta POST usa o corpo raw. Antes de procurar secret, o handler faz parse minimo e coleta todos os
`phone_number_id` distintos de changes `field="messages"` que contem mensagens. O conjunto deve ter tamanho
exatamente 1; zero ou multiplos ids retornam o mesmo 401 generico `invalid_webhook_signature`, sem efeito. O
id unico deve resolver exatamente um tenant e `meta_app_secret`; zero/multiplos tenants ou secret ausente
retornam o mesmo erro e nao estabelecem tenant confiavel. Entao exige `X-Hub-Signature-256` no formato
`sha256=<64-hex>`, calcula HMAC-SHA256 sobre os bytes exatos e compara com `hmac.compare_digest`.

Assinatura ausente retorna 401 `missing_webhook_signature`; formato/HMAC invalido retorna 401
`invalid_webhook_signature`. Nenhuma falha loga corpo/assinatura/secret ou toca bot/pause/session/media/reply.
Depois do HMAC, o processamento permanece vinculado ao mesmo `phone_number_id` e tenant: qualquer change ou
message fora desse binding e rejeitado integralmente, nunca parcialmente processado. O GET de verificacao
Meta permanece separado.

`meta_app_secret` passa a ser obrigatorio para readiness Meta. Clinica legacy sem secret mostra
`signature_pending` e label exato `Assinatura pendente`, nunca `Ativo` nem contribuicao Meta a
`apis_configured`; o POST falha imediatamente com 401 generico ate configuracao. A interrupcao operacional e
aceita como rollout mais seguro, sem feature flag, dual-mode ou estado de migration.

A documentacao oficial da Z-API sobre
[`Client-Token`](https://developer.z-api.io/en/security/client-token) e
[`On receive`](https://developer.z-api.io/en/webhooks/on-message-received) descreve o token para requests
enviados a Z-API e callback HTTPS, mas nao assinatura/header autenticado de callbacks. Portanto o design nao aceita `Client-Token` inbound nem
afirma suporte inexistente. A URL registrada passa a ser `/v1/webhook/zapi/{capability_token}`. Web Crypto
gera exatamente 32 random bytes e codifica base64url sem padding: o wire token canonico tem exatamente 43
ASCII e regex `[A-Za-z0-9_-]{43}`. Percent-encoding, padding, Unicode, tamanho ou forma nao canonica e
rejeitada antes de lookup. O digest persistido e SHA-256 lowercase hex dos bytes UTF-8 do segmento exato.

Existe somente `zapi_inbound_token_digest`. O browser mantem raw somente em memoria, envia por PATCH apenas
o digest e aguarda success confirmado; o PATCH substitui o digest atomicamente e invalida o anterior de
imediato. Depois do success, a URL raw aparece uma unica vez como texto inerte. Nunca entra em `href`, router,
query, history, localStorage, sessionStorage, analytics ou network telemetry; clipboard exige acao explicita
posterior a confirmacao. Fechar/unmount/reload zera todo estado raw. Falha de PATCH tambem zera e exige token
novo. O downtime curto entre a troca e o operador atualizar a URL no painel Z-API e aceito/documentado.

No primeiro edge controlado, o request target vira o route template antes de log/telemetria. Access/app/error
logs, metrics labels, spans/APM, exception reporting, proxy e gateway nunca recebem o segmento raw. O route
valida forma canonica usando o raw path, calcula digest e exige exatamente uma instancia correspondente;
zero ou multiplas correspondencias retornam 401 `invalid_webhook_auth`. Depois, `instanceId` do body deve
corresponder ao `zapi_instance_id` dessa instancia. A rota legacy `/v1/webhook/zapi`, token invalido ou
instance mismatch falha antes de tenant confiavel/session/media. Z-API fica bloqueada ate o operador
registrar a URL; nao ha fallback de autenticidade.

Depois da autenticidade e tenant resolvido, aplica-se rate limit antes de replay/media/egress. Reutiliza-se
`rate_limit_store` com key `webhook:{tenant_id}:{channel}`, quota
`settings.rate_limit_requests_per_minute` e burst `settings.rate_limit_burst`; `rate_limit_enabled=false`
mantem a semantica existente. Excesso retorna HTTP 429 com body exato
`{ "error": "rate_limited", "message": "Rate limit exceeded. Try again in {retry_after} seconds.",
"retry_after": retry_after }` e headers `X-RateLimit-Limit`, `X-RateLimit-Remaining: 0`,
`X-RateLimit-Reset`, `Retry-After`. Nao ha nova persistencia; a garantia continua por processo, como hoje.

Em seguida, Z-API e Meta exigem `messageId` e reutilizam `_claim_incoming_message(source, tenant_id,
message_id)` antes de session/media/reply. Id ausente retorna 200 `missing_message_id`; replay retorna 200
`duplicate_message`. Em producao o claim e Redis `SET NX EX 2678400` (31 dias); erro/indisponibilidade Redis
falha fechado com HTTP 503 `{ "error": "replay_store_unavailable" }`. Fallback memory e permitido somente em
test/development, `workers=1`, explicitamente sem garantia duravel/distribuida. Os 31 dias cobrem a janela de
30 dias em que a Z-API documenta disponibilidade de media, mais um dia; durante a retencao o app garante
dedupe enquanto Redis retiver a key. Replay apos 31 dias, eviction/data loss ou no fallback dev e risco
aceito e explicitamente nao recebe promessa vitalicia.

### Politica unica SSRF/egress

Toda URL configurada e todo `audio.url` passa pelo mesmo validador/cliente de egress:

- HTTPS absoluto apenas, sem userinfo nem fragment; porta deve ser 443;
- egress operacional conhecido usa hosts exatos: LLM OpenAI=`api.openai.com`,
  Dropout=`api.neuralake.cloud`; STT OpenAI=`api.openai.com`; Meta=`graph.facebook.com` e
  Z-API=`api.z-api.io`. LLM/STT `outros`, Evolution/Uazapi e `audio.url` usam a regra custom-publica;
- multimodal, TTS inteiro e quatro STT sem adapter sao config-only: URL armazenada recebe validacao generica
  HTTPS-publica no save, mas nao entra em runtime allowlist nem pode disparar egress. Em particular Microsoft
  TTS permanece sem dispatch e sem regra de conexao especifica;
- `outros` e `audio.url` aceitam apenas destinos publicos por default; endpoints privados sao OUT;
- resolver todos os A/AAAA imediatamente antes da conexao e rejeitar se qualquer resultado for loopback,
  RFC1918/ULA, link-local, multicast, reserved ou metadata. O transport conecta somente a um IP desse conjunto
  validado, preservando hostname original para Host/SNI/certificado e sem segunda resolucao DNS;
- redirects ficam desabilitados; cada nova URL exigiria nova validacao, portanto resposta 3xx e rejeitada;
- download de audio e streaming, timeout connect 5s/read 30s, e para ao exceder
  `min(audio_max_seconds * 16 KiB, 10 MiB)`; Content-Length acima do limite falha antes do body;
- aceitar somente `audio/*` e `application/ogg`; MIME divergente ou ausente falha controladamente;
- API keys/tokens sao anexados somente ao origin final fixo/validado que os exige; `audio.url` nao recebe
  credencial de provider.

URL configurada insegura retorna 422 `unsafe_api_url` sem mutacao. Audio inseguro, excedido, MIME invalido ou
falha de timeout/download retorna respectivamente HTTP 200
`{ "ok": true, "skipped": "unsafe_audio_url" | "audio_too_large" | "unsupported_audio_mime" |
"audio_download_failed" }` antes de transcricao, reply, handoff ou session mutation.

### Estados e disponibilidade

Quatro estados permanecem independentes:

1. provider selecionado e persistido;
2. card expandido apenas no estado local da UI;
3. credencial salva/status `*_api_key_set` retornado pelo GET;
4. disponibilidade runtime derivada de adaptador suportado, nunca da mera presenca de segredo.

Multimodal e TTS inteiros exibem `Configuracao apenas`. Quando suas credenciais estiverem completas, o
status e exatamente `Credenciais salvas / uso em breve`, nunca `Ativo`. O mesmo vale para os quatro STT sem
adaptador. Esses blocos nao contribuem para `apis_configured` no backend nem para o indicador frontend
`APIs totalmente configuradas`. Salvar configuracao nao promete suporte runtime.

### Redaction universal e cache versionado

O JSONB administrativo continua como fonte completa; respostas publicas nunca contem segredo raw. Uma lista
unica `SECRET_FIELDS` cobre tokens WhatsApp/Meta/Z-API/Uazapi, todas as `*_api_key`, aliases legacy e qualquer
novo segredo. GET retorna somente `*_set`/mascara; PATCH success/error, validacao, exceptions e logs passam
pela mesma redaction. O capability token Z-API raw existe apenas no browser e callback path redigido; digests
nao sao exibidos. Testes geram sentinel raw distinto por segredo/caminho e varrem todos os records,
responses e bytes Redis; nenhum sentinel pode sair.

O cache Redis existente recebe top-level `config_cache_schema_version = 2`. Entrada sem marker, invalida ou
com versao diferente exige `DEL` antes do DB load. Se DEL falha, a entrada nunca e reutilizada nem renovada:
o request usa DB direto e nao executa SET. Se DEL passa e DB load falha, a key permanece ausente. Somente
depois de load, composicao clinic+builder e projecao bem-sucedidos escreve-se v2. Nao ha dual-read, migration
de cache, stale fallback ou cache novo.

O `api_credentials` cacheado e estritamente nao secreto:

- remove todo campo cujo nome comece por `multimodal_` ou `tts_`;
- preserva somente ids/URLs/modelos/identificadores nao secretos necessarios de WhatsApp e LLM operacional;
- sempre preserva `stt_provider` e `audio_max_seconds`;
- para `outros` ou `openai_whisper`, inclui `stt_model_name` e `stt_api_url` canonicos resolvidos, nunca key;
- para `elevenlabs_scribe`, `google_stt`, `deepgram_stt` ou `microsoft_stt`, exclui `stt_api_key`,
  `whisper_api_key`, `stt_model_name`, `whisper_model` e `stt_api_url`.

Consumidores que precisam de key/token usam loader interno JWT-free e DB-only, compoem o owner correto e
mantem raw apenas em memoria pelo tempo da chamada; nunca reserializam no cache. Config legacy-only resolve
para canonico em memoria. A projecao nao adiciona cache/criptografia/secret store; restringe o cache atual.

### Contrato visual e navegacao

- Card fechado: icone e nome do provider, mais a acao `+ Adicionar dados da API`; se houver credencial
  salva, a acao passa a `Editar`.
- Card expandido: ocupa largura estavel no grid responsivo, sem deslocamento/overflow, e contem dentro dele
  os labels uppercase `API URL`, `API KEY` e `MODEL NAME`. Somente um card fica expandido por capacidade;
  expandir outra capacidade nao altera provider nem fechamento das demais.
- O Modulo Voz mostra uma secao com icone de relogio e header `GATILHOS DE VOZ`. Abaixo ha um retangulo
  full-width nao interativo e disabled, borda tracejada, fundo cinza muted, icone de relogio e texto italico
  `Em breve`. Ele nao possui inputs, acoes, estado persistido nem campos no GET/PATCH.

`/voice-ai` redireciona para `/config?tab=integracoes`. `ClinicConfig` le a query e usa um valor
controlado/default em `Tabs`; se `integracoes` nao estiver nas abas permitidas para o papel atual, seleciona
a primeira aba permitida sem renderizar conteudo proibido. Trocas de aba atualizam o estado controlado de
forma coerente.

### Autorizacao

`GET/PATCH /v1/config/api-credentials` e JWT-only e permite exatamente `clinic_admin`, `builder` e
`super_admin`. Tenant API key, JWT investor, role ausente ou outro role recebe 403
`credentials_jwt_required`/`credentials_role_forbidden` antes de ler o bucket. Clinic-admin permanece preso
ao proprio tenant; builder so aponta `clinic_id`/`tenant_id` de clinica com `Tenant.builder_id` igual ao
`builder_id` do JWT; super-admin pode apontar tenant valido. O loader interno de runtime nao e endpoint.

## Simplicity Case

- **Design mais simples completo:** uma unica fronteira JWT de credenciais, helper puro de politica, JSONB
  existente, loader DB efemero de segredos, cache existente sem segredos e card de apresentacao.
- **Mecanismos pagos por risco presente:** typed PATCH/allowlists/fresh-key fecham bypass; resolver STT elimina
  tres leituras divergentes; egress bloqueia SSRF por URL/audio controlado; HMAC/capability URL impedem
  callback forjado; cache marker evita que entradas antigas com raw sejam aceitas.
- **Estado duravel minimo:** apenas campos solicitados e um digest Z-API. Rotacao substitui e aceita downtime;
  replay reutiliza Redis/in-memory existentes com TTL 31 dias pago pela janela de media de 30 dias.
- **Um wish:** UI, endpoint, loader/cache e webhooks mudam a mesma fronteira de credenciais/integracoes e
  seus testes end-to-end. Nao ha adapter novo nem subsistema independente que justifique decomposicao.
- **Adiado:** adaptadores multimodal/TTS/STT restantes, retries, health checks, varios providers, cofre e
  controle de concorrencia so entram com historia e evidencia proprias. Endpoints privados exigem historia
  futura com allowlist administrada e threat model proprio.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Um provider persistido por capacidade e um card expandido local por capacidade | Separa configuracao duravel de interacao visual |
| 2 | Allowlists exatas validadas no backend com 422 atomico | Falha fechada impede ids cruzados ou desconhecidos |
| 3 | Troca de provider/URL exige chave raw nova no mesmo PATCH | Segredo anterior nao e assumido valido em outro destino |
| 4 | Mesmo provider/model-only preserva segredo omitido ou mascarado | Mantem edicao segura sem redigitar chave desnecessariamente |
| 5 | Resolver STT canonico-primeiro e compartilhado por GET, Z-API e Meta | Evita divergencia entre reload e runtime |
| 6 | Save canonico remove o alias legado correspondente | Impede ressurgimento de segredo/modelo obsoleto |
| 7 | Apenas `outros` e `openai_whisper` sao STT operacionais | Reflete adaptadores existentes; os demais sao configuration-only |
| 8 | Multimodal/TTS e STT sem adaptador nunca exibem `Ativo` | Credencial salva nao equivale a disponibilidade runtime |
| 9 | Card compartilhado e somente apresentacao | Metadata, persistencia e readiness continuam sob o pai/backend |
| 10 | `/voice-ai` aponta para `tab=integracoes`, com fallback por papel | Usa o id real e nao burla visibilidade de abas |
| 11 | Merge garante preservacao sequencial; concorrencia stale e last-write-wins OUT | Evita locking sem requisito |
| 12 | Cache runtime usa projecao nao secreta por readiness | Remove todo multimodal/TTS e qualquer key/token; canonicaliza apenas URL/model STT operacional |
| 13 | `openai`, `dropout`, `outros` sao LLM operacionais; `slm_cortex` e somente `Em breve` | Mantem o dispatch atual sem anunciar/persistir suporte inexistente |
| 14 | `/api-credentials` e unico reader/writer publico JWT-only | Elimina bypass do PATCH generico e raw no GET generico |
| 15 | Helper puro possui merge/redaction/resolver/projecao | Uma politica, sem dependencia HTTP/DB/cache e sem pass-through |
| 16 | Fallback builder sobrepoe somente quatro `llm_*` do owner exato | Impede vazamento cross-builder e preserva canais da clinica |
| 17 | Meta vincula um phone id/tenant e usa HMAC raw; Z-API usa um digest capability | Autenticidade fail-closed sem inventar header do fornecedor |
| 18 | Replay reutiliza `_claim_incoming_message` por 31 dias e falha fechado em prod | Cobre retries/media presente sem estado novo nem promessa vitalicia |
| 19 | Egress unico, HTTPS publico e redirect negado | Define SSRF fora de existencia e torna private endpoints explicitamente OUT |
| 20 | Cache marker 2 usa DEL-before-load e projecao sem segredo | Stale nunca e reaproveitado; DB failure apos delete nao ressuscita key |
| 21 | Python/TS compartilham somente wire contract | Evita drift testado sem registry/codegen |
| 22 | Rotacao Z-API substitui um digest e aceita downtime | Remove estados/transicoes de rotacao sem enfraquecer autenticidade |
| 23 | Rate limit reutiliza settings/store existentes apos autenticidade | Limita abuso autenticado sem persistence/knob novo |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Reutilizar chave antiga apos troca de provider/URL | High | Chave raw obrigatoria, UI foca campo e backend responde 422 sem mutacao |
| 2 | Alias legado divergir do canonico entre GET, Z-API e Meta | High | Resolver unico canonico-primeiro, limpeza de alias e testes de conflito/runtime |
| 3 | Credencial config-only parecer integracao ativa | High | Status exato, readiness separado e exclusao dos agregados/cache runtime |
| 4 | Provider invalido contaminar JSONB | High | Allowlists por capacidade, 422 estavel e atomicidade testada |
| 5 | Redirect abrir aba proibida para o papel | High | Tabs controladas e fallback para primeira aba permitida |
| 6 | Save de uma capacidade sobrescrever outra | High | Merge e testes com PATCHes sequenciais; simultaneidade stale explicitamente OUT |
| 7 | Cards quebrarem o grid responsivo | Medium | Largura estavel e provas desktop/mobile dos estados fechado/expandido |
| 8 | Qualquer segredo raw vazar no cache runtime | High | Projecao nao secreta, loader DB efemero e sentinel para todo key/token |
| 9 | Editar `clinic-config.tsx` aprofundar acoplamento | Medium | Card de apresentacao isolado; pai conserva metadata e efeitos |
| 10 | `slm_cortex` parecer configuravel ou contribuir ao status | High | Card nao interativo, PATCH 422 atomico e matriz runtime/status testada |
| 11 | STT unsupported executar efeitos antes de falhar | High | Short-circuit antes de media resolution e testes negativos de download/request/reply/handoff/session |
| 12 | Cache misturar aliases ou segredo STT unsupported | High | Serializar projecoes operational, unsupported e legacy-only e comparar campos exatos |
| 13 | PATCH/GET generico contornar politica ou expor raw | High | Fronteira unica, 422 no bypass e omissao total no flatten publico |
| 14 | Builder errado substituir WhatsApp/STT da clinica | High | Resolver por builder_id exato e overlay somente dos quatro `llm_*` |
| 15 | Callback forjado/replayed criar session ou egress | High | HMAC/capability token e claim antes de qualquer efeito |
| 16 | Token Z-API aparecer em access log | High | Route-template logging, redaction e sentinel de path; rota legacy removida |
| 17 | URL custom/audio acessar rede interna ou metadata | High | HTTPS publico, DNS/IP validation, sem redirect e streaming limitado |
| 18 | Tenant API key administrar segredos | High | Dependency JWT/role explicita e 403 antes do bucket |
| 19 | Cache antigo manter raw apos deploy | High | Marker 2; absent/mismatch sempre miss/reload/project |
| 20 | Schema flat aceitar typo/tamanho abusivo | Medium | Pydantic por capacidade, extra forbid, limites e erro 422 estavel |
| 21 | Metadata Python/TS divergir | Medium | Paridade de wire ids/readiness/errors; visual permanece local |
| 22 | Formulario monolitico crescer | Low | Movimento integral e opcional para feature module; nenhum hook/pass-through novo |
| 23 | Capability token vazar por URL/telemetria/browser | High | Canonical wire, route-template no primeiro edge e varredura de todos os sinks |
| 24 | Rotacao Z-API interromper callbacks ate update externo | Medium | Downtime curto aceito e aviso explicito antes do PATCH |
| 25 | Meta legacy sem app secret parar | High | `signature_pending`, fail-closed imediato e interrupcao aceita sem dual-mode |
| 26 | Body Meta misturar tenants sob um HMAC/candidato | High | Exatamente um phone id/tenant e binding integral depois do HMAC |
| 27 | Redis de replay indisponivel ou retencao expirar | High | 503 fail-closed em prod; 31 dias; risco posterior/eviction documentado |
| 28 | Rate limit existente ser apenas per-process | Low | Mesmo storage/limites atuais; trigger para distribuido e abuso atraves de workers medido |
| 29 | Cache stale sobreviver a rollout/DB failure | High | DEL antes do DB, sem SET quando DEL falha, DB failure deixa key ausente |

## Success Criteria

- [ ] A aba `integracoes` apresenta WhatsApp, LLM Provider, Fallback Multimodal e Modulo Voz na ordem; o
      menu nao mostra "Inteligencia de Voz".
- [ ] `/voice-ai` redireciona para `/config?tab=integracoes`; `ClinicConfig` controla Tabs pela query e usa
      a primeira aba permitida quando o papel nao pode ver `integracoes`.
- [ ] Backend valida as cinco allowlists exatas; cada id invalido retorna 422
      `invalid_provider_for_capability` e uma leitura posterior prova zero mutacao.
- [ ] `GET /v1/config` nao contem bucket/campo de credencial; cada tentativa por `PATCH /v1/config` retorna
      422 `credential_endpoint_required`, fields ordenados e zero mutacao.
- [ ] Somente `/v1/config/api-credentials` le/escreve o bucket; testes tentam bypass com cada prefixo/alias.
- [ ] PATCH tipado rejeita extra, tipo, tamanho, enum, combinacao e URL invalidos com envelope 422 estavel,
      antes de atribuir/flush; URLs validas retornam forma normalizada.
- [ ] LLM `openai`, `dropout` e `outros` persistem provider/URL/key/model, o runtime consome os quatro
      valores salvos e credenciais completas podem contribuir ao `apis_configured` existente.
- [ ] `slm_cortex` aparece apenas como card nao interativo `Em breve`, sem add/edit, persistencia, dispatch
      ou contribuicao a `apis_configured`; PATCH retorna 422 `provider_not_operational` exato e zero mutacao.
- [ ] Trocar provider ou URL sem chave raw nova retorna 422 `fresh_api_key_required`, com reason exata e
      zero mutacao; trocar ambos prioriza `provider_changed`.
- [ ] A UI limpa mascara, exige e foca API KEY ao trocar provider/URL; com a chave nova, salva e recarrega.
- [ ] Mesmo provider com alteracao somente de modelo aceita chave omitida ou mascarada e preserva o segredo.
- [ ] Dois PATCHes sequenciais em capacidades diferentes preservam os campos do primeiro; nenhum criterio
      promete protecao para escritores simultaneos stale.
- [ ] Se LLM clinic e incompleto, owner segue `Tenant.builder_id -> User.builder_id -> User.tenant_id` e
      sobrepoe exatamente quatro `llm_*`; testes com dois builders provam zero cross-tenant e preservacao
      byte-for-byte de WhatsApp/STT/multimodal/TTS da clinica.
- [ ] Owner ausente/ambiguo ou LLM builder incompleto nao herda outro tenant e registra somente ids/status.
- [ ] STT exibe Groq=`outros`, OpenAI=`openai_whisper`, ElevenLabs=`elevenlabs_scribe`,
      Google=`google_stt`, Deepgram=`deepgram_stt` e Microsoft=`microsoft_stt`.
- [ ] GET de registro somente legado projeta `whisper_api_key`/`whisper_model` como status/modelo STT; reload
      da UI mantem provider, URL, status mascarado e modelo resolvido.
- [ ] Em conflito, GET e runtime usam `stt_api_key`/`stt_model_name`; salvar cada canonico remove seu alias
      correspondente, e GET posterior nao volta ao legado.
- [ ] Troca de STT com apenas `whisper_api_key` falha; exige `stt_api_key` canonica raw no PATCH.
- [ ] Testes Z-API e Meta provam o mesmo resolver canonico-primeiro para `outros` e `openai_whisper`.
- [ ] Para cada id STT unsupported, Z-API e Meta carregam tenant e, antes de resolver/baixar midia, registram
      warning `stt_provider_not_supported` com tenant/provider/channel e sem segredo, depois retornam HTTP
      200 `{ "ok": true, "skipped": "stt_provider_not_supported" }`.
- [ ] Para cada webhook/id unsupported, testes provam ausencia de media resolution/download, outbound
      provider request, patient reply, handoff e session mutation; `outros`/`openai_whisper` preservam o
      pipeline normal.
- [ ] Meta coleta todos os phone ids de changes com messages e exige conjunto unitario, tenant/secret unico e
      HMAC raw valido; zero/multi/cross-tenant mixed body retorna 401 generico e zero efeito.
- [ ] Meta legacy sem `meta_app_secret` exibe `signature_pending`/`Assinatura pendente`, nunca `Ativo`, e POST
      falha fechado imediatamente; testes aceitam a interrupcao sem flag/migration state.
- [ ] Z-API aceita somente token canonico `[A-Za-z0-9_-]{43}` derivado de 32 bytes; percent-encoded/padding/
      Unicode/tamanho invalido falha antes de lookup; digest e lowercase SHA-256 do segmento UTF-8 exato.
- [ ] Exatamente uma instancia deve casar `zapi_inbound_token_digest` e body instanceId; zero/multi/mismatch e
      rota legacy retornam 401 antes de tenant confiavel/session/media.
- [ ] Rotacao PATCH substitui um unico digest e invalida o antigo imediatamente; teste prova downtime aceito,
      sem qualquer segundo digest ou estado/transicao adicional.
- [ ] UI mostra raw URL somente como texto inerte apos PATCH confirmado, clipboard explicito, e zera raw em
      failure/close/unmount/reload; history/location/storage/href/router/analytics/network telemetry ficam limpos.
- [ ] Sentinel raw nao aparece em access/app/error logs, metrics, spans/APM, exception reporting, proxy ou
      gateway; o primeiro edge controlado registra apenas route template.
- [ ] Z-API e Meta exigem messageId e usam claim existente antes de session/media/reply; ausencia/replay
      retorna skips exatos; Redis usa NX/31 dias, falha gera 503 em prod e memory so existe em test/dev worker 1.
- [ ] Testes provam dedupe durante retencao e documentam replay aceito apos 31 dias/eviction/data loss, sem
      promessa vitalicia; TTL e justificado pela janela Z-API de media por 30 dias.
- [ ] Depois de autenticidade/tenant e antes de replay/media/egress, rate limit usa key tenant+channel,
      quota/burst/settings/store atuais e retorna envelope/headers 429 exatos; nenhum estado novo e criado.
- [ ] Tabela de egress testa scheme/userinfo/fragment/porta, hosts conhecidos, custom publico, DNS misto,
      loopback/private/link-local/metadata/IPv6, 3xx negado, timeout, bytes e MIME; falha nunca envia segredo.
- [ ] Multimodal, TTS e STT sem adaptador exibem `Configuracao apenas`; quando completos exibem exatamente
      `Credenciais salvas / uso em breve`, nunca `Ativo`.
- [ ] Multimodal/TTS e STT sem adaptador nao contam para `apis_configured` nem `APIs totalmente configuradas`.
- [ ] Cache sem marker/invalido/version mismatch sofre DEL antes do DB; DEL failure impede reuse/renew/SET,
      DB failure apos DEL deixa key ausente, e somente compose/project bem-sucedido escreve v2.
- [ ] Testes serializam cache operacional canonico, legacy-only e unsupported: todo secret sentinel,
      `multimodal_*`/`tts_*` e key/token/alias raw somem; ids/URLs/modelos nao-secretos permitidos e sempre
      `stt_provider`/`audio_max_seconds` permanecem.
- [ ] Runtime carrega keys/tokens somente do DB interno para memoria, compoe owner correto e nao os reinsere
      no cache; GET/PATCH success/error e todos os logs tambem ficam sem sentinel raw.
- [ ] O card fechado mostra icone/nome e `+ Adicionar dados da API` ou `Editar`; o expandido mantem largura
      estavel e mostra dentro dele `API URL`, `API KEY`, `MODEL NAME`, com um expandido por capacidade.
- [ ] `GATILHOS DE VOZ` possui icone/header e o retangulo disabled full-width, tracejado, cinza muted, com
      relogio e `Em breve` italico, sem input, acao ou payload.
- [ ] Testes de autorizacao provam clinic-admin preso ao tenant, builder restrito a clinicas proprias e
      super-admin capaz de apontar tenant; tenant API key, investor e role ausente recebem 403 antes da leitura.
- [ ] Teste de paridade prova wire ids/readiness/error codes iguais entre metadata Python/TS, sem exigir que
      labels/defaults visuais sejam compartilhados.
- [ ] WhatsApp conserva seis cards, campos, webhooks e comportamento atuais; desktop/mobile nao apresentam
      overflow, sobreposicao ou salto incoerente entre card fechado e expandido.

## Next Step

Submeter novamente este conteudo a revisao independente de arquitetura, seguranca e plano. A evidencia atual
fica stale por design; somente novo SHIP pode ser estampado pelo orquestrador e consumido por `wish`. O wish
existente deve ser reconciliado com a fronteira unica, autenticidade, egress, auth JWT e cache sem segredos
antes de qualquer execucao.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `cbd37ed4b86ca158465ce6a2ce3d684b080651c7ff347ba91467af0170286478`
- **Reviewer:** /root/wish_plan_review
- **Reviewed at:** 2026-08-06T00:14:47.000Z
<!-- genie-design-review:end -->
