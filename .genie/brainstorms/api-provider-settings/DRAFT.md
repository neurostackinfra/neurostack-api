# Brainstorm Draft: Central de Provedores na Aba APIs

**Slug:** api-provider-settings
**Date:** 2026-08-05
**Status:** WRS 100 - pronto para nova revisao

## Problema

Consolidar WhatsApp, LLM principal, fallback multimodal, STT, TTS e o placeholder de gatilhos na aba
`integracoes`, removendo a navegacao concorrente sem quebrar STT legado nem apresentar configuracao salva
como suporte runtime.

## Referencias e baseline

- A referencia mostra cards fechados por provider e um unico card expandido por capacidade, com campos
  dentro do card.
- O baseline usa ids LLM operacionais `openai`, `dropout`, `outros` e mostra `slm_cortex` como `Em breve`;
  WhatsApp usa `meta_cloud`, `dialog360`,
  `twilio`, `zapi`, `evolution`, `uazapi`; STT legado ja usa `outros`, `openai_whisper`,
  `elevenlabs_scribe`, `google_stt`.
- O baseline possui aliases STT `whisper_api_key`/`whisper_model`, dois caminhos runtime (Z-API e Meta),
  endpoint JSONB GET/PATCH e cache runtime Redis.
- O id atual da aba de APIs e `integracoes`, nao `apis`.

## Escopo

### IN

- Ordem: WhatsApp, LLM Provider, Fallback Multimodal, Modulo Voz.
- Fallback multimodal OpenRouter/Groq/OpenAI independente; STT com seis providers; TTS com tres.
- Cards fechado/expandido, URL editavel, segredo mascarado e provider/modelo persistidos por capacidade
  operacional; `slm_cortex` permanece nao interativo.
- Resolver STT unico, compatibilidade legado/canonico e comportamento runtime explicitamente limitado.
- Placeholder `GATILHOS DE VOZ` disabled e redirect `/voice-ai` para `tab=integracoes`.
- Validacao fail-closed, autorizacao existente e testes backend/frontend/runtime/responsivos.

### OUT

- Runtime multimodal/TTS e adaptadores STT ElevenLabs/Google/Deepgram/Microsoft.
- Gatilhos de voz, retry, health check, fallback ordenado, cofre, tabela nova ou varios providers ativos.
- Mudancas em autenticacao tenant API-key, autorizacao ou locking otimista.
- Garantia contra PATCHes simultaneos com snapshot antigo; last-write-wins e aceito.

## Abordagem escolhida

Ampliar `api_credentials` com namespaces por capacidade e manter o GET/PATCH como unica fronteira. Metadata e
persistencia pertencem ao pai/backend; o card compartilhado e somente apresentacao. A expansao e estado
local, a selecao e persistida, o status de credencial vem do GET e a disponibilidade runtime e derivada do
adaptador. Nenhum desses quatro estados implica os demais.

### Allowlists e falha atomica

| Capacidade | ids exatos |
|------------|-----------|
| WhatsApp | `meta_cloud`, `dialog360`, `twilio`, `zapi`, `evolution`, `uazapi` |
| LLM UI | `openai`, `dropout`, `outros`, `slm_cortex` (`Em breve`, nao persistivel) |
| Multimodal | `openrouter`, `groq`, `openai` |
| STT | `outros`, `openai_whisper`, `elevenlabs_scribe`, `google_stt`, `deepgram_stt`, `microsoft_stt` |
| TTS | `elevenlabs`, `microsoft`, `google` |

Id invalido retorna 422 `invalid_provider_for_capability`, com capability/provider e nenhuma mutacao.
`slm_cortex` permanece aceito apenas para display; PATCH `llm_provider=slm_cortex` retorna 422
`provider_not_operational`, capability/provider e nenhuma mutacao.

### Matriz LLM

- `openai`, `dropout` e `outros`: cards add/edit, persistem provider/URL/key/model, sao OpenAI-compatible,
  usam esses valores no runtime e, quando completos, podem contribuir ao `apis_configured` existente.
- `slm_cortex`: card nao interativo `Em breve`, sem add/edit, sem persistencia nesta entrega, sem dispatch e
  sem contribuicao a `apis_configured`/`APIs totalmente configuradas`.

### Troca segura de provider/URL

Mudar provider ou URL credential-bearing em LLM operacional (`openai`/`dropout`/`outros`), multimodal,
STT ou TTS exige `*_api_key` raw nova no mesmo
PATCH. Ausente, vazia ou mascarada retorna 422 `fresh_api_key_required`, reason `provider_changed` ou
`api_url_changed`, e nenhuma mutacao; provider tem precedencia se ambos mudam. Mesmo provider com mudanca
somente de modelo aceita chave omitida/mascarada e preserva o valor. A regra nao se aplica a `slm_cortex`,
que nao e persistivel. A UI compara com o ultimo snapshot,
limpa a mascara, exige e foca API KEY antes do submit.

PATCHes sequenciais fazem merge e preservam outras capacidades. Simultaneidade stale permanece
last-write-wins e nao justifica ETag, versao ou lock.

### Resolver STT e readiness

- Labels/ids: Groq=`outros`, OpenAI=`openai_whisper`, ElevenLabs=`elevenlabs_scribe`,
  Google=`google_stt`, Deepgram=`deepgram_stt`, Microsoft=`microsoft_stt`.
- Resolver unico: canonico `stt_api_key`/`stt_model_name` vence; legado correspondente so e fallback quando
  o canonico falta. Save canonico remove o alias correspondente. Troca de provider/URL exige chave canonica.
- GET, Z-API e Meta usam esse resolver.
- Apenas `outros` (Groq/OpenAI-compatible URL) e `openai_whisper` sao operacionais.
- Quatro STT sem adaptador, todo multimodal e todo TTS exibem `Configuracao apenas`; se completos, exibem
  `Credenciais salvas / uso em breve`, nunca `Ativo`.
- Em Z-API e Meta, depois de carregar tenant e antes de resolver/baixar midia, STT unsupported registra
  warning `stt_provider_not_supported` somente com tenant/provider/channel e retorna HTTP 200 exato
  `{ "ok": true, "skipped": "stt_provider_not_supported" }`; nao ha download, outbound provider request,
  patient reply, handoff ou session mutation. STT operacional mantem o pipeline normal.
- Configuration-only nao conta em `apis_configured`/`APIs totalmente configuradas`; segredos multimodal e
  TTS nao entram na projecao do cache runtime Redis.

### Projecao Redis

DB/GET administrativo retêm todos os campos. O `api_credentials` runtime remove todo `multimodal_*` e
`tts_*`, retém WhatsApp/LLM operacional e sempre retém `stt_provider`/`audio_max_seconds`. Para STT
operacional, serializa somente `stt_api_key`/`stt_model_name`/`stt_api_url` canonicos resolvidos, inclusive
quando a origem e legacy-only. Para STT unsupported, remove key/model/url canonicos e legados:
`stt_api_key`, `whisper_api_key`, `stt_model_name`, `whisper_model`, `stt_api_url`.

### Contrato visual e rota

- Fechado: icone/nome e `+ Adicionar dados da API`, ou `Editar` se configurado.
- Expandido: largura estavel no grid responsivo; labels uppercase `API URL`, `API KEY`, `MODEL NAME` dentro
  do card; somente um expandido por capacidade.
- Trigger: icone de relogio/header `GATILHOS DE VOZ`; retangulo full-width nao interativo disabled, borda
  tracejada, fundo cinza muted, relogio e `Em breve` italico; zero inputs, acoes ou payload.
- `/voice-ai` -> `/config?tab=integracoes`; Tabs leem query em valor controlado/default. Se o papel nao pode
  ver `integracoes`, cai na primeira aba permitida.

### Autorizacao preservada

Clinic-admin continua preso ao proprio tenant; builder pode mirar somente clinica propria; super-admin pode
mirar qualquer tenant. A politica tenant API-key nao muda.

## Decisoes

| Decisao | Racional |
|---|---|
| Namespaces por capacidade e allowlists exatas | Isolamento com falha fechada sem novo modelo de dados |
| Chave nova em troca de provider/URL | Evita reutilizar segredo contra destino diferente |
| Resolver canonico-primeiro com limpeza de alias | Reload e runtime nao divergem nem ressuscitam legado |
| Readiness separado de selecao/expansao/credencial | Configuracao salva nao vira promessa operacional |
| Card compartilhado somente visual | Pai permanece dono de metadata, persistencia e regras |
| Config-only fora dos agregados/cache runtime | Evita falso positivo e exposicao desnecessaria |
| Matriz LLM explicita | Preserva os tres providers operacionais e mantem `slm_cortex` realmente `Em breve` |
| Short-circuit STT antes da midia | Evita custo, side effects e requests em provider sem adaptador |
| Projecao Redis por readiness | Canonicaliza STT operacional e remove todo segredo config-only |
| Redirect pelo id real com fallback por papel | Mantem deep link sem abrir aba nao autorizada |
| Merge apenas para sequencia; concorrencia OUT | Nao adiciona locking sem requisito |

## Riscos e mitigacoes

| Risco | Severidade | Mitigacao |
|---|---|---|
| Chave antiga reutilizada em provider/URL novo | Alta | UI exige/foca raw key; backend 422 atomico |
| Canonico e alias STT divergirem | Alta | Resolver unico, precedencia, limpeza e provas GET/Z-API/Meta |
| Config-only aparecer ativo | Alta | Status exato, readiness separado e exclusao dos agregados |
| Provider invalido persistir | Alta | Allowlists e 422/no-mutation |
| Redirect expor aba proibida | Alta | Primeira aba permitida como fallback |
| Save cruzado apagar dados | Alta | Merge e PATCHes sequenciais testados; stale concurrency OUT |
| Cache runtime carregar segredo sem consumidor | Media | Excluir multimodal/TTS da projecao Redis e testar ausencia |
| Grid saltar/estourar | Media | Contrato de largura estavel e testes desktop/mobile |
| `slm_cortex` parecer configuravel/ativo | Alta | Sem interacao, PATCH 422 e sem contribuicao a status/runtime |
| STT unsupported causar side effects | Alta | Short-circuit antes da midia e testes de ausencia em ambos webhooks |
| Cache misturar alias ou segredo unsupported | Alta | Snapshots operational, legacy-only e unsupported com campos exatos |

## Criterios de aceite

- [ ] As cinco allowlists sao validadas exatamente; id invalido da 422 estavel e zero mutacao.
- [ ] `openai`/`dropout`/`outros` persistem e alimentam runtime/status; `slm_cortex` e nao interativo,
      retorna 422 `provider_not_operational` se enviado e nunca persiste/dispatcha/conta.
- [ ] Troca de provider ou URL sem chave raw da 422 estavel e zero mutacao; UI exige e foca API KEY.
- [ ] Model-only no mesmo provider preserva chave omitida/mascarada; PATCHes sequenciais preservam outras
      capacidades.
- [ ] GET legado e reload usam aliases apenas se canonicos faltam; em conflito canonico vence; save canonico
      remove alias correspondente.
- [ ] Troca STT nao aceita alias como chave nova; Z-API e Meta usam o mesmo resolver.
- [ ] Cada STT unsupported em Z-API/Meta, depois de tenant e antes da midia, loga warning sem segredo e
      retorna 200/body exato; testes negam download, outbound, reply, handoff e session mutation.
- [ ] Multimodal/TTS e STT sem adaptador exibem os textos config-only exatos, nunca `Ativo` e nao contam nos
      agregados.
- [ ] Snapshots Redis operational, legacy-only e unsupported provam remocao de todo multimodal/TTS,
      preservacao WhatsApp/LLM e provider/audio STT, canonicalizacao operational e remocao key/model/url STT
      unsupported.
- [ ] Card fechado, expandido e placeholder de gatilhos correspondem a estrutura visual especificada,
      inclusive largura responsiva e um expandido por capacidade.
- [ ] `/voice-ai` redireciona para `tab=integracoes`; papel sem a aba cai na primeira permitida.
- [ ] Clinic-admin tenant lock, builder ownership e super-admin targeting possuem testes; tenant API-key nao
      muda.
- [ ] WhatsApp e os tres LLM operacionais nao regridem; multimodal/TTS salvam configuracao sem prometer
      runtime; `slm_cortex` permanece somente `Em breve`.

## Simplicity Gate

- **Mais simples completo:** campos JSONB nomeados, endpoint existente, resolver STT e card visual isolado.
- **Mecanismos justificados:** allowlists, regra de chave nova, resolver e readiness separado respondem a
  falhas atuais de integridade, compatibilidade e comunicacao.
- **Estados duraveis novos:** nenhum alem dos campos solicitados; expansao e local e readiness e derivada.
- **Adiado:** adaptadores, retries, health checks, cofre, tabela generica, cache adicional e locking.

## WRS

WRS: ██████████ 100/100

Problem OK | Scope OK | Decisions OK | Risks OK | Criteria OK
