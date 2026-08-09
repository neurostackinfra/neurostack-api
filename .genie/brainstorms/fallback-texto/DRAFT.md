# DRAFT — Fallback texto (degradação graciosa de áudio)

## WRS

```
WRS: ██████████ 100/100
 Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅
```

Cristalizado em [DESIGN.md](DESIGN.md).

## Pedido original (verbatim)

> Fallback texto. Se a API de ouvir áudio, ou falar falham, ele identifica e reconduz a conversa por texto.
> Ex. "no momento não estou conseguindo ouvir áudios, poderia por gentileza digitar como poderia te ajudar no momento?"
> "Não estou conseguindo enviar áudios no momento, mas vou te explicar tudo aqui por texto..."
> Mas o gatilho de entrada antes de fazer essa recondução, no caso de transcrição, seria enviar para ativação do modelo multimodal de fallback se foi adicionado, pois é opcional — mas vai estar escrito RECOMENDÁVEL — em caso de falha do LLM provider principal de texto e transcrição será feito roteamento automático.

## Estado atual do código (verificado)

| Fato | Evidência |
|---|---|
| Falha de transcrição no Z-API sai em silêncio — paciente não recebe nada | `src/api/routes/webhook.py:1150-1152` → `{"ok": True, "skipped": "transcription_failed"}` |
| Sem chave STT, mesma saída silenciosa | `src/api/routes/webhook.py:1153-1154` → `"no_whisper_key"` |
| Mesmo padrão no caminho Meta Cloud API | `src/api/routes/webhook.py:2548`, `:2551` |
| Provedor STT sem adaptador sai antes de qualquer download | `src/api/routes/webhook.py:1048-1049`, `UNSUPPORTED_STT_PROVIDERS` em `:59` |
| Áudio longo JÁ tem recondução por texto + handoff (precedente de UX) | `src/api/routes/webhook.py:1070-1095` |
| Multimodal é config-only: campos salvos, sem adaptador de runtime | `src/services/api_credentials.py:443-445` → `multimodal_readiness = "configuration_only"` |
| TTS é config-only: campos salvos, sem envio de áudio em runtime | `src/services/api_credentials.py:451`; nenhuma chamada TTS em `src/`|
| LLM client é provedor único, sem cadeia de fallback | `src/llm/client.py:51`, `:179` |

## Problem ✅

Quando a transcrição de áudio falha, o webhook encerra em silêncio: o paciente não recebe
resposta alguma e a clínica não fica sabendo. O paciente fica esperando. Não existe nenhuma
recondução para texto nem tentativa com um provedor alternativo.

## Scope ✅ (decidido com o usuário)

**IN**
- STT principal falha → 2ª tentativa com multimodal, se configurado → recondução por texto.
- Mensagem de recondução enviada **uma vez por sessão**, não a cada áudio.
- Adaptador multimodal de runtime (não existe hoje) para transcrição de áudio.
- Rótulo `RECOMENDÁVEL` no card multimodal da aba APIs.

**OUT**
- Fallback de TTS. O agente não envia áudio em runtime (`tts_readiness = "configuration_only"`),
  então o fallback não teria de que degradar. Volta quando o envio de áudio existir.
- Roteamento automático em falha do provedor de LLM de **texto** — wish separado;
  `src/llm/client.py` é de provedor único e é outro subsistema.

## Risks ✅

### R1 — BLOQUEADOR: a chave STT não chega ao runtime hoje

`_load_clinic_config` (`event_service.py:398-417`) sempre retorna a projeção de cache, e
`runtime_projection` (`api_credentials.py:458-482`) tem um allowlist que **não inclui
`stt_api_key`**. O handler de áudio lê a chave justamente dessa projeção
(`webhook.py:1040-1041`), então em produção `whisper_key` é sempre `None` e **todo áudio sai
por `no_whisper_key`** (`webhook.py:1154`).

Os testes não pegam isso porque fazem patch de `_load_clinic_config` para reinjetar a chave —
declarado explicitamente em `tests/api/test_webhook_egress.py:114-119` e no log do wish
`api-provider-settings` (`WISH.md:1431`).

Consequência para este desenho: se a recondução por texto for construída sobre o estado atual,
ela dispara para 100% dos áudios e o agente passa a dizer "não consigo ouvir áudios" para
sempre — mascarando o defeito de plumbing em vez de corrigi-lo.

Precedente para a correção: `_load_runtime_llm_credentials` (`event_service.py:361`) é o caminho
sancionado para material de chave em runtime, lendo do banco fora do cache. Falta o equivalente
para STT/multimodal.

### R2 — Segredos multimodais também estão fora do runtime

`multimodal_*` é bloqueado da projeção por invariante, com testes que fixam isso
(`tests/services/test_api_credentials.py:220`, `:440`, `:505`;
`test_event_service_config_cache.py:275`). O adaptador multimodal precisa do mesmo loader
DB-only de R1, não de um afrouxamento do allowlist do cache.

### R3 — Conflito com wish em andamento

`api-provider-settings` está IN_PROGRESS e é dono de `api_credentials.py` e da projeção de
cache. Critério 12 do wish: "Providers config-only nunca ficam `Ativo` nem entram no cache
runtime". Promover multimodal a `active` contradiz esse critério enquanto o wish não fechar.

### R4 — Distinguir "não configurado" de "falhou"

`no_whisper_key` (nunca configurado) e `transcription_failed` (configurado, quebrou) merecem
tratamento diferente: sem STT nenhum, a recondução por texto é permanente e deveria ser
mensagem de produto; com STT quebrado, é degradação temporária.

### R5 — Dois caminhos de entrada divergem

Z-API (`webhook.py:1036-1154`) e Meta Cloud API (`webhook.py:2500-2551`) duplicam a lógica de
áudio. A cadeia de fallback precisa valer nos dois, ou o comportamento diverge por canal.

## Decisions ✅ / Criteria ✅

Ver [DESIGN.md](DESIGN.md) — tabela de 10 decisões e 12 critérios testáveis.

## Perguntas abertas — todas resolvidas

1. R1 entra neste wish? → **Sim**, loader DB-only aqui (Decisão 1 e 2).
2. Mensagem de "nunca configurado" vs. falha temporária? → **Distintas** (Decisão 6).
3. Handoff após reconduzir? → **Marcar não lida, sem handoff nem pausar a IA** (Decisão 8).
