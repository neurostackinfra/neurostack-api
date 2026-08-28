# Draft: Disponibilidade honesta de provedores na aba APIs

| Field | Value |
|-------|-------|
| **Slug** | `api-provider-availability` |
| **Date** | 2026-08-23 |
| **WRS** | 40/100 |

## Pedido original

Só as integrações efetivamente testadas ficam ativas para receber credenciais. As demais continuam
visíveis pelo nome, com entrada de credencial **desabilitada** e um rótulo pequeno em vermelho
`(em breve)` — para que um usuário não técnico entenda que é roadmap, não erro nem indisponibilidade.

Lista declarada pelo usuário como testada: Z-API, Uazapi, ElevenLabs, Dropout, Groq.
Pergunta aberta do usuário: testamos a API direta do GPT/OpenAI?
Instrução extra: Microsoft não entra como opção de TTS.

## Estado atual do código (levantado, não presumido)

Hoje existem **três** formas diferentes de dizer "não está pronto", e duas delas enganam o usuário:

| Mecanismo | Onde | Comportamento | Problema |
|---|---|---|---|
| `comingSoon: true` | `PROVIDERS` (LLM `slm_cortex`), `clinic-config.tsx:397` | Card não interativo, badge `Em breve` violeta, nunca enviado no PATCH | É o padrão correto, mas o badge não é vermelho e não é reusado nas outras capacidades |
| `provider_not_implemented` | `api_credentials.py:204`, `UNIMPLEMENTED_WHATSAPP_PROVIDERS` = `dialog360`, `twilio` | UI **mostra o formulário**, usuário digita credencial, e o backend rejeita com 422 no save | Exatamente a armadilha descrita: parece disponível, falha depois |
| `configuration_only` | `credential_status()`, STT sem adapter e multimodal fora do operacional | Formulário aberto, credencial **salva com sucesso**, runtime nunca usa | Parece configurado, nunca funciona |

### Matriz por capacidade

**WhatsApp** — `PROVIDER_ALLOWLISTS["whatsapp"]`

| id | Código | Na lista testada do usuário |
|---|---|---|
| `zapi` | adapter + webhook + capability token | sim |
| `uazapi` | adapter + webhook (INDEX: já em produção) | sim |
| `meta_cloud` | adapter + webhook + HMAC + readiness `signature_pending` | **não citado** |
| `evolution` | `send_evolution_text/audio` + webhook `/v1/webhook/evolution` | **não citado** |
| `dialog360` | 422 `provider_not_implemented`, mas formulário visível | não |
| `twilio` | 422 `provider_not_implemented`, mas formulário visível | não |

**LLM** — `openai`, `dropout`, `outros` operacionais; `slm_cortex` já é `comingSoon`.
Evidência relevante: `multimodal-format-probe.md:154` registra que **não existe `OPENAI_API_KEY` direta
na máquina** — OpenAI só foi alcançada via rota OpenRouter. Ou seja, GPT direto nunca foi testado com
chave própria.

**Multimodal** — `openrouter` | `groq` | `openai`. Evidência viva em
`.genie/wishes/fallback-texto/evidence/multimodal-format-probe.md`:
- `openrouter`: **OPERACIONAL**, transcreveu OGG/Opus do WhatsApp verbatim (HTTP 200, `audio_tokens: 125`).
- `groq`: **INCOMPATÍVEL provado** — o bloco `input_audio` não existe no schema do chat da Groq.
- `openai`: rejeita OGG no enum e no sniff; só aceita wav/mp3, que o WhatsApp nunca entrega.

Conflito a resolver: o usuário lista Groq como testado e não lista OpenRouter. A evidência aponta o
inverso **para multimodal**. A leitura provável é que Groq foi testado como **STT** (`stt_provider =
outros` → `whisper-large-v3` em `/audio/transcriptions`), que é um endpoint diferente e funciona.

**STT** — `outros` (Groq) e `openai_whisper` operacionais; `elevenlabs_scribe`, `google_stt`,
`deepgram_stt`, `microsoft_stt` são `configuration_only`.

**TTS** — `elevenlabs` e `google` têm adapter real em `tts_service.py`. Microsoft **já foi removida**
de `PROVIDER_ALLOWLISTS["tts"]` e do type `TtsProvider`; o pedido do usuário nesse ponto já está
satisfeito, resta confirmar.

## Decisões confirmadas pelo usuário (2026-08-23)

1. **Testados/ativos**, além de Z-API, Uazapi, Dropout e Groq: **Meta Cloud API**, **OpenRouter**
   (multimodal) e **Google Cloud TTS**. Evolution **não** foi aprovado.
2. **ElevenLabs = só TTS.** O Scribe (`elevenlabs_scribe`) fica em breve.
3. **OpenAI:** LLM (`openai`) continua ativo por ser default e já estar em uso; multimodal OpenAI sai
   e o Whisper STT depende de probe (item aberto abaixo).
4. **Legado:** credencial já salva em provedor desativado é **preservada no banco e bloqueada para
   edição**. Nunca apagada.
5. **Evolution:** desativa a UI **e o runtime** — o webhook passa a recusar.
6. **Groq e OpenAI no multimodal saem da lista**, não viram "em breve": foram testados e provados
   incompatíveis, então prometer futuro seria mentira. A própria evidência do probe já recomendava
   remover `groq` do allowlist.

## Matriz final de disponibilidade

| Capacidade | Ativo (aceita credencial) | Em breve (nome visível, entrada bloqueada) | Removido |
|---|---|---|---|
| WhatsApp | `meta_cloud`, `zapi`, `uazapi` | `evolution`, `dialog360`, `twilio` | — |
| LLM | `openai`, `dropout`, `outros` | `slm_cortex` | — |
| Multimodal | `openrouter` | — | `groq`, `openai` |
| STT | `outros` (Groq) | `elevenlabs_scribe`, `google_stt`, `deepgram_stt`, `microsoft_stt`, e `openai_whisper` **pendente de probe** | — |
| TTS | `elevenlabs`, `google` | — | `microsoft` (já removido) |

## Achados de código que ampliam o escopo

1. **O gate de STT em runtime é cego ao provedor.** `webhook.py:1293` (`has_stt`) dispara a
   transcrição apenas por existir chave salva; nunca consulta `OPERATIONAL_STT_PROVIDERS`. Hoje uma
   clínica com `google_stt` salvo faz o backend POSTar de verdade num endpoint incompatível a cada
   nota de voz. Logo `configuration_only` é só um rótulo de tela — a regra precisa ser aplicada
   também no runtime, senão a UI diz "em breve" enquanto o backend queima request.
2. **A `GROQ_API_KEY` do `.env` está morta** (`401 Invalid API Key`, chave com formato válido `gsk_`,
   56 chars). Groq é o único STT ativo da matriz. Não prova falha em produção — credenciais de
   produção são por clínica no `api_credentials` — mas repete o padrão já registrado pela wish
   `fallback-texto` ("credenciais de áudio Z-API mortas em produção").
3. **"Testado" é um retrato datado, não um estado.** O item 2 é a prova: um provedor aprovado pode
   parar de funcionar sem que a lista mude. Fica como Risco, não como máquina nova — health check e
   retries foram explicitamente OUT no design anterior.

## Whisper — resolvido sem probe

O usuário pediu para testar ao vivo, mas não existe `OPENAI_API_KEY` no `.env` (só `GROQ_API_KEY`,
`OPENROUTER_API_KEY` e `LLM_API_KEY` da Dropout), e o truque do probe anterior não serve: a
OpenRouter só proxia `chat/completions`, não `/audio/transcriptions`. Decisão tomada em 2026-08-23:
**sem recibo de teste, `openai_whisper` fica `coming_soon`** — UI e runtime desligados, igual ao
Evolution. Reversível assim que houver chave.

Harness deixado pronto (throwaway, fora do git) caso alguém queira reverter por evidência:
- `/tmp/probe_stt.py` — espelha o multipart exato de `groq_service.transcribe_audio`
  (`file` + `model` + `language` + `response_format=text`).
- `/tmp/probe_speech.opus` — OGG/Opus real, 8 kHz mono, 5.000 s, 14 061 bytes, header `OggS`+`OpusHead`.
- Comando: `python3 /tmp/probe_stt.py openai whisper-1 /tmp/probe_speech.opus`

## Divisão em dois wishes (2026-08-23)

Duas rodadas de design review devolveram FIX-FIRST. O que elas mudaram no recorte:

1. **Uma afirmação central minha era falsa.** Eu disse que `webhook.py:1293` (`has_stt`, cego ao
   provedor) fazia uma clínica em `google_stt` POSTar de verdade num endpoint incompatível.
   `UNSUPPORTED_STT_PROVIDERS` (`webhook.py:68`) já gateia os quatro nos três canais. O que sobra de
   verdadeiro é menor: o Evolution não tem gate nenhum, e `openai_whisper` está de fato em
   `OPERATIONAL_STT_PROVIDERS` e realmente dispara.
2. **Escopo dividido em dois wishes.** Wish 1 = vitrine (catálogo, UI, contrato de PATCH), com
   **todo** runtime OUT — por construção não derruba clínica. Wish 2 = desligamento de runtime,
   que carrega todos os riscos altos e a única mudança visível ao paciente.
3. **A célula do Evolution virou medição.** Marcar `coming_soon` um canal com clínica viva seria
   exatamente a desonestidade que o design existe para remover. Censo de produção é o grupo 1, com
   stop rule.
4. **A regra de PATCH foi refeita duas vezes.** Rejeitar por presença travaria a aba inteira;
   rejeitar comparando o corpo cru também trava, porque o round-trip GET→UI→PATCH sintetiza default
   (`audio_max_seconds: 60`) e troca alias (`whisper_model` ↔ `stt_model_name`). Regra final:
   comparar estado efetivo pós-`merge_credentials`, normalizado.
5. **Uma oitava declaração de disponibilidade apareceu:** os `Literal` de provider em
   `src/api/schemas/config.py`, avaliados antes de qualquer regra de domínio.

## WRS

```
WRS: ██████████ 100/100
 Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅
```

Cristalizado em `DESIGN.md` (wish 1, vitrine). O wish 2 fica em Simmering até o censo retornar.
