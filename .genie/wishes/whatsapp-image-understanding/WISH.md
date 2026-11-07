# Wish: Agente entende imagens recebidas pelo WhatsApp e confirma arquivos (OpenRouter)

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `whatsapp-image-understanding` |
| **Date** | 2026-11-07 |
| **Author** | Claude (decisões de produto: Lorenzo, 2026-11-07) |
| **Appetite** | medium |
| **Branch** | `wish/whatsapp-image-understanding` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Hoje toda imagem, documento ou vídeo que o paciente manda pelo uazapi ou Z-API é descartado em
silêncio (`Uazapi webhook: skipping non-text msg_type=media mediaType=image`; em produção, 5 imagens
ignoradas entre 02:39 e 02:48 UTC de 2026-11-07). Este wish faz duas coisas:

- o provedor multimodal OpenRouter que o tenant já configurou no card "API LLM Fallback Multimodal"
  passa a descrever a imagem em texto e a repassar essa descrição ao LLM principal, pelo mesmo
  caminho do áudio;
- todo arquivo que o agente não analisa (documento, vídeo ou imagem não descrita) recebe a mensagem
  de confirmação definida pelo dono, com a opção de encaminhar para a equipe. É o fluxo que o Meta já
  tem, agora igual em uazapi, Z-API e Meta, e com prazo de validade.

## Scope

### IN

- Adaptador `describe_image_multimodal` em `src/services/multimodal_service.py` (bloco `image_url`
  com data URL base64, prompt neutro e não diagnóstico, egress pinado a `multimodal_openrouter`).
- `safe_download_image` em `src/services/safe_egress.py` (novo kind `image_url`), irmão de
  `safe_download_audio`.
- Em `src/api/routes/webhook.py`:
  - cadeia `_describe_image_chain`, composição da mensagem e emissor `image_description_attempt`;
  - o texto de confirmação de arquivo do dono, substituindo o atual do Meta;
  - `pending_file_review` com prazo de validade, nos três canais.
- Imagens de paciente descritas em **uazapi**, **Z-API** e **Meta Cloud**.
- Fluxo "arquivo não analisado" (confirmação → `pending_file_review` → encaminhar se o paciente
  pedir) em **uazapi** e **Z-API** para documento, vídeo e imagem não descrita. No Meta, que já tem
  esse fluxo, entram o texto novo, a regra de não reenvio e o prazo de validade.
- Handoff de arquivo cobrado no Z-API pelo mesmo `run_meta_file_handoff` do Meta, que já é agnóstico
  de canal, provado por teste unitário e pelo gate de integração de crédito.
- Texto de ajuda e descrição do card OpenRouter atualizados no frontend.
- Rollout: aviso aos tenants que já têm o bundle OpenRouter completo, antes do deploy.

### OUT

- **Evolution API**: vai para o wish `whatsapp-image-understanding-evolution` (Decisão 16).
- Ler o conteúdo de documentos (PDF) e vídeos: eles só recebem a confirmação.
- Figurinha (sticker): continua ignorada no uazapi e no Z-API; no Meta segue no fluxo de arquivo de
  hoje, agora com o texto novo.
- Visualização única no Meta: o Meta entrega como `unsupported`, sem sinal confiável de visualização
  única, e segue em silêncio como hoje (Decisão 13).
- Mídia enviada pela própria clínica (`fromMe`): sem descrição e sem confirmação, como hoje.
- O modelo de visão responder direto ao paciente.
- Persistir os bytes da imagem ou criar `media_assets` para ela.
- Interruptor de liga/desliga para imagens, e o opt-in do fallback de texto
  (`multimodal_text_fallback_enabled`), que segue sem controle na UI.
- Validar se o modelo do tenant aceita imagens: se não aceitar, é uma falha comum e cai no fluxo de
  arquivo.
- Melhorar a detecção de intenção de encaminhar (`_META_FORWARD_KEYWORDS`): reusada como está; o
  prazo de validade limita os falsos positivos.
- Pausar a IA depois de um encaminhamento: hoje o encaminhamento do Meta não pausa, e continua assim
  (risco registrado).

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Descrever e repassar: o OpenRouter gera uma descrição e o LLM principal responde | Dono. Mantém persona, RAG, filtros de safety e histórico num caminho só, no padrão do áudio (`_transcribe_audio_chain` → `message_text = transcript`). |
| 2 | Ativação automática quando `_multimodal_operational(creds)` é verdadeiro | Dono. Sem flag nova; o card avisa (Grupo 5) e os tenants afetados são avisados no Rollout (Decisão 15). |
| 3 | Fluxo "arquivo não analisado", igual nos três canais. Documento, vídeo e imagem não descrita (sem capacidade, grande demais, MIME inválido, falha de download, falha do provider, sem conteúdo; no uazapi/Z-API também visualização única) recebem `_FILE_ACK_MSG_*` e ativam `pending_file_review` (Decisão 4). Enquanto a pendência está **ativa**, a próxima mensagem textual do paciente resolve: se `_detect_forward_intent` → `_meta_file_handoff(note="file_pending_review", clear_pending_review=True)` + `_FILE_FORWARDED_MSG_*`; senão, limpa e segue o pipeline. Mensagem textual = mensagem de paciente que **não é mídia**: no uazapi e no Z-API, texto ou áudio transcrito com sucesso (a legenda de imagem, documento ou vídeo — no uazapi espelhada em `message.text` — nunca resolve a pendência); no Meta, só `msg_type == "text"`, como hoje | Dono (2026-11-07: handoff só a pedido; foto sem IA no mesmo fluxo). Reusa o fluxo do Meta (`webhook.py:4431-4486`, `:4622-4670`) e os helpers de sessão, que já são agnósticos de canal. |
| 4 | Prazo de validade: `_meta_set_pending_file_review` também grava `pending_file_review_at` (UTC ISO) no mesmo JSONB. A pendência só é **ativa** se a flag existe e `pending_file_review_at` tem menos de `FILE_REVIEW_WINDOW = timedelta(hours=2)`. Flag sem timestamp (linhas antigas), malformada ou expirada conta como ausente em todo lugar, sem escrita no predicado: a confirmação volta a ser enviada (e `_meta_set_pending_file_review` sobrescreve as duas chaves); na resolução, segue o pipeline sem testar intenção de encaminhar. Enquanto a pendência está ativa, um arquivo novo não reenvia a confirmação (nos três canais, inclusive no Meta) | Correção N-H1 da revisão: sem prazo, uma flag velha deixaria o paciente sem confirmação para sempre e transformaria um "me manda o endereço" semanas depois num encaminhamento falso; isso já é latente no Meta hoje. 2h cobre uma rajada (5 fotos em 9 min) e um paciente que responde mais tarde. Nenhuma migração; leitores `is True` (`test_credit_metering.py:3268`) continuam válidos. |
| 5 | Meta: com capacidade, a imagem é descrita e repassada. Em qualquer outro resultado, segue o ramo de arquivo de hoje: aviso de privacidade se a sessão é nova, confirmação, `pending_file_review`, `action=file_ack`, **sem claim e sem débito** (`test_credit_metering.py:1774` continua valendo). Handoff e débito só pelo caminho de encaminhar a pedido | Dono (mantém o handoff do Meta). Correção H2 da rodada 1. |
| 6 | Texto de confirmação (todos os canais), exatamente: pt = "Recebi seu arquivo! Mas, no momento, não consigo acessar o conteúdo por aqui.\n\nVocê poderia me contar um pouco mais sobre o arquivo e o que precisa? Assim, consigo entender melhor a situação e tentar te orientar.\n\nSe preferir, também posso encaminhar seu caso para nossa equipe avaliar com mais detalhes."; en = "I received your file! But right now I can't access its contents here.\n\nCould you tell me a bit more about the file and what you need? That way I can better understand the situation and try to help.\n\nIf you prefer, I can also forward your case to our team to review in more detail." `_build_meta_file_ack_message` passa a ser `(agent_language: str) -> str`, sem personalização; `_meta_load_lead_name` é removida (único chamador `:4626`); `_FILE_FORWARDED_MSG_*` não muda | Dono (2026-11-07: o texto dele em todos os canais; tradução em inglês aprovada em 2026-11-07). |
| 7 | Crédito. A imagem descrita é cobrada como um turno comum, como o áudio transcrito (sai do pipeline nos canais com enforcement: Z-API e Meta), e a confirmação é grátis. O encaminhar a pedido no Z-API usa `run_meta_file_handoff` com a `WebhookEnforcement` do `precheck_zapi` (claim `zapi`), com `metering = None if request is None or enforcement is not None else zapi_ingress(...)`, espelhando o handler. No uazapi, que não tem enforcement, vai pelo ramo observacional (`enforcement=None`, `record_meta_file_handoff` com `uazapi_ingress`). O rótulo de decisão `"meta_file_handoff"` (`credit_metering.py:990`, `:2389`) é aceito como rótulo nos traces de Z-API e uazapi, sem mudança de código | Dono (2026-11-07). A rodada 2 confirmou: o claim usa `enforcement.source`/`provider_message_id` (`:2435-2443`), `_claimed_answer` é genérico e `enforcement` está em escopo em `:2759`. |
| 8 | Ponto de inserção. **uazapi:** entre o fim do aviso LGPD (`:3585`) e `assert message_text is not None` (`:3587`), que continua depois do bloco de mídia. **Z-API:** as guardas `:2606` e `:2612` deixam passar só mídia de paciente (`not is_from_me`, não sticker), e a inserção fica entre `:2758` e `:2759`. Nos dois canais, isso fica depois de `_check_bot_active`, `_check_pause_state` (Z-API: também `precheck_zapi`), da sessão e do aviso LGPD. **Meta:** dentro do despacho existente (`:4429`, depois de `:4301`/`:4319` e do precheck em `:4337`); `_send_meta_privacy_notice()` sai antes da descrição numa sessão nova e uma única vez por requisição (flag local `privacy_sent`, também respeitada pelo ramo de arquivo `:4638` e pelo aviso compartilhado `:4682`) | Correções H1, M8, N-M1, N-M2 e N-L10. |
| 9 | Deduplicação. **uazapi:** só a imagem sai do claim do topo (`:3319-3330`), como o áudio. Ela faz o claim depois do download e antes de qualquer retorno, e roda `require_uazapi_egress()` antes da criação da sessão, para que um egress fechado volte 503 retentável **antes** de consumir `is_new` (o aviso LGPD não se perde na reentrega). Um `EgressDisabledError` tardio, no próprio download, também volta 503 sem claim. Documento e vídeo continuam no claim do topo. **Z-API e Meta:** o claim do topo já existe (`:2392`, `:4231`); nenhum claim novo | Correções M1, N-L5 e N-L6. |
| 10 | Mensagem que entra no pipeline: `"<legenda>\n\n[Imagem enviada pelo paciente: <descrição>]"` (sem legenda quando não existe; `[Image sent by the patient: …]` quando o idioma do agente é `en`). Legenda: uazapi `content.caption`, com fallback para `message.text`; Z-API `image.caption`; Meta `image.caption` | O LLM principal distingue o que o paciente escreveu do que foi visto, e o contexto sobrevive aos turnos seguintes. |
| 11 | Prompt de descrição: descrição objetiva e transcrição do texto visível, no idioma do agente (pt-BR ou en), proibindo diagnóstico e opinião clínica. Conteúdo não descritível → `[[NO_IMAGE_CONTENT]]`, tratado como falha. Códigos do adaptador: `provider_not_multimodal_capable`, `unsupported_image_mime`, `provider_http_error`, `provider_transport_error`, `invalid_provider_response`, `no_image_content` | Safety em contexto de clínica; repete o `NO_SPEECH_MARKER` do áudio. |
| 12 | Limites: MIME `image/jpeg`, `image/png` e `image/webp`; teto `MEDIA_IMAGE_MAX_BYTES` (5_000_000, `src/services/credit_allocations.py:69`). Acima do teto ou com MIME fora da lista, a imagem nunca vai ao OpenRouter, e nem é baixada quando o canal declara o tamanho (uazapi `content.fileLength`; Meta `file_size`/`mime_type` de `resolve_meta_media`). No uazapi, tamanho e MIME são checados também depois do download (`download_uazapi_media` não tem teto). `safe_download_image` usa o kind `image_url` (política como `audio_url`, `safe_egress.py:379`, e no conjunto de `:82`) e os códigos `unsafe_api_url`, `redirect_forbidden`, `unsupported_image_mime`, `image_too_large`, `image_download_failed`. No Z-API, um CDN que responde `application/octet-stream` é aceito quando `image.mimeType` declara um MIME permitido | Correções M5, M7, L1 e N-L10. |
| 13 | Visualização única: não é descrita nem baixada e vai para o fluxo de arquivo no uazapi (`content.viewOnce == true` ou `messageType` contendo `ViewOnce`) e no Z-API (`image.viewOnce == true`); os dois sinais **não estão verificados** em payload real. No Meta chega como `unsupported`, sem distinguir visualização única de outros tipos não suportados, e segue em silêncio como hoje | Dono (2026-11-07: não descrever e mandar a confirmação). Limite técnico do Meta: confirmar todo `unsupported` responderia também a mensagens que não são arquivo (correção N-M5). |
| 14 | Imagem descrita com pendência ativa (de um arquivo anterior): a flag não é tocada e a legenda não é lida como intenção de encaminhar; a próxima mensagem textual resolve, dentro do prazo da Decisão 4 | Correção M2; o prazo evita a flag eterna (N-H1). |
| 15 | LGPD: o dono aceita o risco (2026-11-07) de imagens de pacientes irem ao OpenRouter com a chave do tenant e de a descrição ficar no histórico. Antes do deploy, os tenants com bundle OpenRouter operacional são avisados (Rollout) | Dono. Correção M8. |
| 16 | Evolution sai deste wish e vai para `whatsapp-image-understanding-evolution` | A divisão foi apresentada ao dono em 2026-11-07, sem objeção; não há evidência nem helper para o Evolution (L9). |
| 17 | Ordem de envio da confirmação e da confirmação de encaminhamento: enviar → `_track_sent_message` (+ `_store_ai_msg_id_to_db` quando houver id) → `_meta_set_pending_file_review` (só na confirmação) → `db.commit()`. No uazapi, o id vem de `_extract_normalised_send_id` (`:1372`, como em `:3656-3660`) antes de `_track_sent_message({"messageid": mid})` e `_store_ai_msg_id_to_db`. A flag só é gravada depois de um envio bem-sucedido; se o envio falha, nada fica pendente | Correção N-M3: o eco de uma mensagem não rastreada no Z-API pausa a sessão (`:1724-1800`), e uma flag gravada sem envio suprimiria as confirmações seguintes. |
| 18 | Corpos e logs. Confirmação suprimida: `{"ok": True, "replied": False, "action": "file_ack_suppressed"}`. Confirmação enviada: `{"ok": True, "replied": True, "action": "file_ack"}`. Egress do uazapi fechado no envio da confirmação: `{"ok": True, "replied": False, "reason": "uazapi_egress_disabled"}`, sem flag (como `:3669-3674`). Documento e vídeo não emitem `image_description_attempt`, mas logam `"<Canal> file ack sent to %s (type=%s)"`, como o Meta | Correção N-L4. |
| 19 | "Pausada" significa só `is_paused` (o que `_check_pause_state` lê). O encaminhamento abre o handoff mas não pausa a IA, como no Meta hoje | Correção N-L9; mudar isso fica fora do escopo (risco registrado). |

## Simplicity Case

- **Simplest complete design:**
  - um adaptador (irmão de `transcribe_audio_multimodal`), um downloader (irmão de
    `safe_download_audio`) e uma cadeia `(texto, motivo_da_falha)`;
  - para os arquivos, reuso do fluxo do Meta (helpers de sessão, `_detect_forward_intent`,
    `_meta_file_handoff`, `run_meta_file_handoff`);
  - um ponto de inserção por handler (Decisão 8).
- **Added machinery:**
  - o emissor `image_description_attempt`, porque o diagnóstico de 2026-11-07 mostrou que caminhos
    silenciosos tornam produção opaca;
  - a chave `pending_file_review_at` no JSONB já existente: sem ela, a Decisão 4 transforma uma flag
    velha em silêncio permanente e em encaminhamento falso (N-H1).
- **Deferred until measured:**
  - cache de descrição por hash: adotar se o log mostrar a mesma imagem descrita várias vezes;
  - redimensionar antes do envio: adotar se o OpenRouter recusar por tamanho, ou se o p95 de
    `latency_ms` passar de 10 s;
  - renomear os helpers `_meta_*` para nomes neutros de canal: adotar quando um quarto canal os usar;
  - Evolution: wish separado.
- **Complexity removed (versus a v1):** o aviso de imagem com chave Redis e TTL de 6h. A v3 não tem
  flag nova, coluna, migração, armazenamento de imagem nem função de cobrança nova.

## Dependencies

**depends-on:** none
**blocks:** whatsapp-image-understanding-evolution

## Success Criteria

- [ ] Imagem `image/jpeg` de paciente via uazapi, com OpenRouter operacional, sessão não pausada e IA ligada: exatamente uma chamada ao OpenRouter com bloco `image_url`, e o pipeline recebe `content` com o marcador da Decisão 10 (mais a legenda, quando existe).
- [ ] O mesmo vale para Z-API e Meta, com teste de handler em cada um.
- [ ] Com a IA desligada (`_check_bot_active`) ou a sessão pausada (`is_paused`), nos três canais: nenhum download, nenhuma chamada ao OpenRouter, nenhuma confirmação, nenhuma resposta.
- [ ] Documento, vídeo e imagem não descrita (motivos da Decisão 3) recebem exatamente o texto da Decisão 6 no idioma do agente e ativam a pendência. Com uma pendência ativa, a confirmação não é reenviada (`file_ack_suppressed`); com a pendência expirada ou sem timestamp, é reenviada.
- [ ] Com a pendência ativa, uma resposta com intenção de encaminhar abre o handoff e envia `_FILE_FORWARDED_MSG_*`; qualquer outra resposta limpa a flag e segue o pipeline. Com a pendência expirada, "me manda o endereço" segue o pipeline sem handoff. Vale nos três canais.
- [ ] O eco da confirmação é tratado como `ai_echo` (não pausa a sessão); um envio que falha não deixa pendência.
- [ ] Crédito: a confirmação não gera claim nem efeito nos três canais; a imagem descrita é cobrada como turno comum no Z-API e no Meta (teste em mundo com enforcement); o encaminhar a pedido no Z-API debita via `run_meta_file_handoff` com claim `zapi` (teste unitário + gate de integração de crédito); os testes atuais de crédito do Meta passam sem alteração, exceto a contagem de call sites de `_meta_file_handoff` (G2/G3, por desenho).
- [ ] Uma imagem acima de 5_000_000 bytes ou com MIME fora da lista nunca vai ao OpenRouter; quando o tamanho é declarado, nem é baixada.
- [ ] No uazapi e no Z-API, uma imagem de visualização única não é baixada nem descrita e recebe a confirmação.
- [ ] Uma reentrega do mesmo `messageid` não chama o OpenRouter de novo. No uazapi, egress fechado → 503 sem claim e antes da sessão; na reentrega seguinte, o aviso LGPD de uma sessão nova sai.
- [ ] Numa sessão nova, o aviso LGPD sai antes da primeira descrição, nos três canais, e uma única vez.
- [ ] Toda tentativa de imagem emite `image_description_attempt` com `channel`, `outcome` ∈ {`described`, `no_capability`, `too_large`, `unsupported_mime`, `view_once`, `download_failed`, `provider_failed`, `no_content`}, `mime`, `size_bytes` (pode ser nulo) e `latency_ms` (pode ser nulo). Nenhum log contém bytes da imagem nem o texto da descrição.
- [ ] Sticker, mídia `fromMe` (inclusive imagem e documento da equipe no Z-API, que não pausam a IA) e texto puro mantêm o comportamento atual.
- [ ] O card OpenRouter mostra a ajuda sobre imagens em pt e en, e a descrição do bloco deixa de falar em "duas contingências".
- [ ] O Final Gate passa.

## Execution Strategy

### Wave 1 (parallel)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 5 — custo/modelo (+2), estado (+2: prazo da pendência), prompt (+1) | `engineer-complex` / high | Adaptador, downloader, cadeia, emissor, texto de confirmação, pendência com prazo |
| 5 | engineer | 1 — só frontend (+1); copy pinada | `engineer-trivial` / low | Ajuda e descrição do card OpenRouter |

### Wave 2 (after Group 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 6 — roteamento (+2), custo (+2), estado (+2) | `engineer-complex` / high | uazapi: descrição de imagem e fluxo de arquivo (o canal com tráfego em produção vem primeiro) |

### Wave 3 (after Group 2)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 6 — roteamento (+2), custo/crédito (+2), estado (+2) | `engineer-complex` / high | Meta: descrição no sucesso; ramo de arquivo com texto, não reenvio e prazo |

### Wave 4 (after Groups 2 and 4)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 6 — roteamento (+2), custo/crédito (+2), estado (+2) | `engineer-complex` / high | Z-API: descrição, fluxo de arquivo e handoff cobrado |

- **Arquivos compartilhados:**
  - Os grupos 2, 3 e 4 escrevem `src/api/routes/webhook.py` e `tests/services/test_credit_metering.py`, por isso rodam em sequência (G2 → G4 → G3), nunca em paralelo (regra do `work`: escritores paralelos precisam de arquivos disjuntos).
  - O Grupo 1 já deixa `safe_download_image` na linha de import de `safe_egress` (`webhook.py:85`).
  - Mudanças nos helpers do Grupo 1 estão proibidas nos grupos 2 a 4: se forem necessárias, voltam ao
    Grupo 1 como fix.
- **Final gate:** como os grupos 3 e 4 mexem em cobrança, que foi decisão do dono, a execução termina
  com uma revisão `final-gate` independente sobre o diff agregado.

## Execution Groups

### Group 1: Núcleo de imagem, confirmação de arquivo e pendência com prazo

**Goal:** Deixar prontos, e testados isoladamente, todos os helpers que os handlers de canal vão chamar.

**Deliverables:**
1. `describe_image_multimodal(api_key, image_bytes, mimetype, model, base_url, agent_language)` em `src/services/multimodal_service.py`, com o prompt e os códigos da Decisão 11 e `IMAGE_NO_CONTENT_MARKER = "[[NO_IMAGE_CONTENT]]"`. Recusa URL que não resolve para `multimodal_openrouter`.
2. `safe_download_image(url, *, max_bytes, headers=None, declared_mime=None)` em `src/services/safe_egress.py`: kind `image_url`, códigos e allowlist da Decisão 12, mesma semântica de host, redirect e streaming de `safe_download_audio`.
3. Em `src/api/routes/webhook.py`:
   - `IMAGE_MAX_BYTES = MEDIA_IMAGE_MAX_BYTES` (importado de `src/services/credit_allocations.py`) e `safe_download_image` na linha de import (`:85`);
   - `_describe_image_chain(image_bytes, credentials, *, mime_type, caption, agent_language, channel) -> tuple[str, str]` (`IMAGE_CHAIN_OK` / `IMAGE_CHAIN_NO_CAPABILITY` / `IMAGE_CHAIN_FAILED`) e `_compose_image_message`;
   - `_emit_image_description_attempt(*, channel, outcome, mime, size_bytes: int | None, latency_ms: int | None)`.
4. Texto: `_FILE_ACK_MSG_PT` e `_FILE_ACK_MSG_EN` com o texto da Decisão 6; `_build_meta_file_ack_message(agent_language: str) -> str`; `_meta_load_lead_name` removida, junto com a chamada em `:4626`. Atualizar todos os call sites de teste (`test_webhook_meta.py:1608`, `:1653`, `:1705`, `:1777` e qualquer outro) e inverter por desenho `test_file_ack_personalises_with_lead_name_when_known` (`:1228-1274`).
5. Pendência com prazo (Decisão 4): `FILE_REVIEW_WINDOW = timedelta(hours=2)`; `_meta_set_pending_file_review` grava também `pending_file_review_at`; `_pending_file_review_active(session_id, db, *, now=None) -> bool` (flag e timestamp dentro do prazo; expirada, sem timestamp ou malformada → False, sem escrita); `_file_ack_needed(session_id, db) -> bool` = `not _pending_file_review_active(...)`. `_meta_clear_pending_file_review` remove as duas chaves.
6. Testes:
   - `tests/services/test_multimodal_image.py` (novo): payload, prompt no idioma, data URL, códigos, marcador, egress pinado;
   - casos novos em `tests/services/test_safe_egress.py`: MIME, octet-stream com MIME declarado, teto por `Content-Length` e por streaming, redirect;
   - testes unitários de `_describe_image_chain`, `_compose_image_message`, `_emit_image_description_attempt` (sem bytes nem descrição no log), `_pending_file_review_active` (ativa, expirada, sem timestamp, `now` injetado) e `_file_ack_needed`.

**Acceptance Criteria:**
- [ ] O payload tem exatamente um bloco `image_url` com data URL do MIME normalizado e o prompt no idioma pedido.
- [ ] Uma URL que não resolve para `multimodal_openrouter` é recusada antes de qualquer I/O; credenciais incompletas → `IMAGE_CHAIN_NO_CAPABILITY` sem chamada; resposta vazia ou `[[NO_IMAGE_CONTENT]]` → `IMAGE_CHAIN_FAILED`.
- [ ] `safe_download_image` cumpre a Decisão 12 nos dois caminhos de teto e recusa redirect.
- [ ] A pendência expira em 2h, e uma flag sem timestamp conta como ausente.
- [ ] O Meta envia o texto novo; os testes de áudio multimodal, `safe_download_audio` e handoff de arquivo do Meta passam (alterados só onde o deliverable 4 manda).

**Validation:**
```bash
# Escopo: módulos novos, egress compartilhado, texto e pendência do Meta (os helpers de pendência são lidos por
# test_credit_metering.py:3220/:3268); áudio entra como regressão do vizinho.
poetry run pytest tests/services/test_multimodal_image.py tests/services/test_multimodal_transcription.py tests/services/test_safe_egress.py tests/api/test_webhook_meta.py tests/services/test_credit_metering.py -q
poetry run black --check src/services/multimodal_service.py src/services/safe_egress.py src/api/routes/webhook.py tests/services/test_multimodal_image.py tests/services/test_safe_egress.py tests/api/test_webhook_meta.py
poetry run isort --check-only src/services/multimodal_service.py src/services/safe_egress.py src/api/routes/webhook.py tests/services/test_multimodal_image.py tests/services/test_safe_egress.py tests/api/test_webhook_meta.py
poetry run flake8 src/services/multimodal_service.py src/services/safe_egress.py src/api/routes/webhook.py tests/services/test_multimodal_image.py tests/services/test_safe_egress.py tests/api/test_webhook_meta.py
poetry run mypy src --ignore-missing-imports
```

**depends-on:** none

---

### Group 2: uazapi — descrição de imagem e fluxo de arquivo

**Goal:** No uazapi, uma imagem de paciente é descrita e respondida; documento, vídeo e imagem não descrita recebem a confirmação, e o paciente pode pedir o encaminhamento.

**Deliverables:**
1. Corte de mídia:
   - imagem, documento e vídeo de paciente (não sticker, não `fromMe`) deixam de sair como `non-text`;
   - só a imagem sai do claim do topo, e roda `require_uazapi_egress()` antes da sessão (Decisão 9);
   - documento e vídeo são detectados por `mediaType` `document`/`video` e `messageType` `DocumentMessage`/`DocumentWithCaptionMessage`/`VideoMessage`, ignorando maiúsculas. Esses nomes não estão provados por fixture, então cada um tem teste.
2. Ponto de inserção entre `:3585` e `:3587`, antes do `assert`, na ordem:
   - **mensagem textual (não mídia) de paciente com pendência ativa:** resolve conforme a Decisão 3. O handoff usa `_meta_file_handoff(enforcement=None, metering=uazapi_ingress(...))`; a confirmação de encaminhamento sai por `send_uazapi_text` na ordem da Decisão 17.
   - **imagem:** capacidade → visualização única → `fileLength` e MIME declarados → `download_uazapi_media` → claim → tamanho e MIME de novo → `_describe_image_chain`. No sucesso, `message_text` composto (legenda conforme a Decisão 10).
   - **documento, vídeo ou imagem não descrita:** `_file_ack_needed` → `send_uazapi_text(_FILE_ACK_MSG_*)` na ordem da Decisão 17, com os corpos da Decisão 18. Cada outcome de imagem emite o evento.
3. Testes em `tests/api/test_webhook_uazapi.py`, partindo de `media-payload.json` (`image`) e de variantes derivadas (documento com e sem legenda, vídeo, visualização única):
   - sucesso com e sem legenda;
   - cada motivo de falha;
   - IA desligada e sessão pausada (nada acontece);
   - sessão nova (LGPD antes da descrição);
   - reentrega; egress fechado (503 antes da sessão, e LGPD na reentrega);
   - pendência ativa (sem reenvio), expirada (reenvio) e sem timestamp;
   - encaminhar a pedido; descrever depois da confirmação; áudio transcrito resolvendo a pendência; "me manda o endereço" com a pendência expirada → sem handoff;
   - eco da confirmação → `ai_echo`; envio que falha → sem pendência;
   - documento sem legenda (sem 500);
   - imagem com legenda e documento com legenda, com pendência ativa → não resolvem (imagem descrita e flag intacta; documento `file_ack_suppressed`);
   - egress fechado no envio da confirmação → corpo da Decisão 18, sem flag;
   - sticker e `fromMe` inalterados.
   - Inverter por desenho `:896-931` e `:3186-3222` e atualizar a docstring do módulo (`:8`).
   - Atualizar por desenho `tests/services/test_credit_metering.py::test_the_writer_runs_only_at_the_governed_decision_points` (`:2168-2170`): 3 call sites de `_meta_file_handoff`, enclosing `{"meta_webhook","uazapi_webhook"}`. As demais asserções (writer único, `_meta_initiate_file_handoff` só em `_meta_file_handoff`/`initiate`, 4 chamadas de `_run_pipeline_transactionally`) não mudam.

**Acceptance Criteria:**
- [ ] Todos os critérios de sucesso aplicáveis ao uazapi passam nos testes acima.
- [ ] Os testes atuais de áudio e texto do uazapi passam sem alteração (exceto os invertidos por desenho no deliverable 3).

**Validation:**
```bash
# Escopo: handler uazapi, os testes estruturais que fixam call sites de handoff/adapters, e o egress do uazapi.
poetry run pytest tests/api/test_webhook_uazapi.py tests/services/test_uazapi_service_egress.py tests/services/test_multimodal_image.py "tests/services/test_credit_metering.py::test_the_writer_runs_only_at_the_governed_decision_points" "tests/api/test_webhook_zapi.py::test_every_adapter_call_site_passes_the_origin_keywords" -q
poetry run black --check src/api/routes/webhook.py tests/api/test_webhook_uazapi.py tests/services/test_credit_metering.py
poetry run isort --check-only src/api/routes/webhook.py tests/api/test_webhook_uazapi.py tests/services/test_credit_metering.py
poetry run flake8 src/api/routes/webhook.py tests/api/test_webhook_uazapi.py tests/services/test_credit_metering.py
poetry run mypy src --ignore-missing-imports
```

**depends-on:** Group 1

---

### Group 3: Z-API — descrição, fluxo de arquivo e handoff cobrado

**Goal:** Paridade do Grupo 2 no Z-API, com o encaminhar a pedido cobrado como no Meta.

**Deliverables:**
1. Fixture `tests/fixtures/zapi/media-payloads.json` com `image` (`imageUrl`, `caption`, `mimeType`, `viewOnce`), `document` e `video`, nos nomes da documentação Z-API "on-message-received". A URL da documentação vai num `_meta`, marcada "não verificado em payload real".
2. Guardas:
   - `:2606` e `:2612` deixam passar só mídia de paciente (`not is_from_me`, não sticker); a mídia da equipe segue saindo como hoje e não pausa a IA;
   - a URL da imagem é lida de forma tolerante (`imageUrl` ou `url`, como o áudio em `:2484`);
   - um objeto `image` sem URL vira `download_failed` → confirmação, nunca silêncio.
3. Ponto de inserção entre `:2758` e `:2759`, com a ordem do Grupo 2 e sem claim novo (o do topo é `:2392`):
   - imagem baixada com `safe_download_image(url, max_bytes=IMAGE_MAX_BYTES, declared_mime=image.mimeType)`;
   - mensagens enviadas por `send_text_message` na ordem da Decisão 17, com o rastreamento de eco do Z-API.
4. O encaminhar a pedido usa `_meta_file_handoff(enforcement=enforcement, metering=<expressão da Decisão 7>)`, que cai em `credit_metering.run_meta_file_handoff` quando há enforcement. Se aparecer alguma premissa específica do Meta, corrigir em `src/services/credit_metering.py` sem mudar o comportamento do Meta; atualizar as docstrings que dizem `'meta'`. O rótulo `"meta_file_handoff"` fica (Decisão 7).
5. Testes:
   - em `tests/api/test_webhook_zapi.py`: a matriz do Grupo 2, mais imagem e documento da equipe (sem pausa da IA), imagem descrita cobrada como turno comum (mundo com enforcement), confirmação sem claim nem efeito, e eco da confirmação → `ai_echo`;
   - em `tests/services/test_credit_metering.py`: `run_meta_file_handoff` com enforcement `zapi` (claim `zapi`, débito, `duplicate_message` em conflito), e `test_the_writer_runs_only_at_the_governed_decision_points` atualizado por desenho para 4 call sites, acrescentando `_process_zapi_authenticated_body`;
   - em `tests/integration/test_credit_metering_postgres.py`: o mesmo contra Postgres.

**Acceptance Criteria:**
- [ ] Todos os critérios de sucesso aplicáveis ao Z-API passam nos testes acima.
- [ ] O gate de integração de crédito passa (sem nenhum skip); os testes atuais de crédito do Meta passam sem alteração, exceto a contagem de call sites de `_meta_file_handoff` (por desenho).
- [ ] Os testes atuais de áudio e texto do Z-API passam sem alteração, em especial `test_webhook_zapi.py:1447-1474` (áudio sem URL → `empty`) e `:1673-1680` (`audio == {}` → `non-text`), que as guardas retrabalhadas precisam manter.
- [ ] O Z-API só é considerado entregue em produção depois que o primeiro tenant Z-API passar pela QA (risco M7).

**Validation:**
```bash
poetry run pytest tests/api/test_webhook_zapi.py tests/services/test_credit_metering.py tests/services/test_safe_egress.py -q
# Postgres descartável (receita do projeto); o gate do repo recusa qualquer skip ou falha:
: "${POSTGRES_SMOKE_DATABASE_URL:?POSTGRES_SMOKE_DATABASE_URL must point at a disposable Postgres}"
bash scripts/check-credit-integration.sh tests/integration/test_credit_metering_postgres.py
poetry run black --check src/api/routes/webhook.py src/services/credit_metering.py tests/api/test_webhook_zapi.py tests/services/test_credit_metering.py
poetry run isort --check-only src/api/routes/webhook.py src/services/credit_metering.py tests/api/test_webhook_zapi.py tests/services/test_credit_metering.py
poetry run flake8 src/api/routes/webhook.py src/services/credit_metering.py tests/api/test_webhook_zapi.py tests/services/test_credit_metering.py
poetry run mypy src --ignore-missing-imports
```

**depends-on:** Group 2, Group 4

---

### Group 4: Meta — descrição no sucesso, ramo de arquivo com texto, não reenvio e prazo

**Goal:** No Meta, uma imagem com OpenRouter operacional é descrita e repassada; em qualquer outro caso, o ramo de arquivo de hoje roda com o texto novo, a regra de não reenvio e o prazo da pendência.

**Deliverables:**
1. No despacho por tipo, `msg_type == "image"` com `_multimodal_operational(creds)`:
   - checa `file_size` e `mime_type` de `resolve_meta_media` antes de baixar;
   - baixa com `safe_download_image(url, max_bytes=IMAGE_MAX_BYTES, headers={"Authorization": "Bearer …"})`;
   - passa pela cadeia e, no sucesso, monta `message_text` com `image.caption`;
   - numa sessão nova, envia `_send_meta_privacy_notice()` antes da descrição, uma única vez (flag `privacy_sent`, Decisão 8).
2. Qualquer resultado diferente de `IMAGE_CHAIN_OK` cai no ramo de arquivo existente (`:4622-4670`), sem claim e sem débito (Decisão 5), agora condicionado a `_file_ack_needed` e na ordem da Decisão 17. Documento, vídeo e sticker continuam nesse ramo.
3. A resolução do ramo de texto (`:4439`) passa a usar `_pending_file_review_active` no lugar de `_meta_get_pending_file_review`, para que uma pendência expirada siga o pipeline sem testar intenção de encaminhar. Remover `_meta_get_pending_file_review` e reapontar para `_pending_file_review_active` os patches em `test_webhook_meta.py:1356`, `:1431`, `:3357`, `:3604` e `tests/integration/test_conversation_language_postgres.py:581`.
4. Imagem descrita com pendência ativa: a flag não é tocada (Decisão 14). O comentário "never download the payload" (`:4424-4429`) passa a refletir as decisões 5, 13 e 14.
5. Testes em `tests/api/test_webhook_meta.py` e `tests/services/test_credit_metering.py`:
   - sucesso: sem pendência, sem handoff, cobrado como turno comum no mundo com enforcement;
   - cada motivo de falha → ramo de arquivo com `_effects == []` e `_claims == []`;
   - pendência ativa → sem reenvio; expirada → reenvio, e "me manda o endereço" segue o pipeline sem handoff;
   - sessão nova → LGPD antes da descrição e uma única vez;
   - documento, vídeo e sticker inalterados (só o texto novo); `unsupported` inalterado;
   - o token Bearer ausente dos logs de falha.

**Acceptance Criteria:**
- [ ] Todos os critérios de sucesso aplicáveis ao Meta passam nos testes acima.
- [ ] `test_credit_metering.py:1774` ("the ack is free") e o resto dos testes de crédito do Meta passam sem alteração.

**Validation:**
```bash
# Escopo ampliado: toca o caminho com metering de crédito e um patch num teste de Postgres que pula sem a URL.
poetry run pytest tests/api/test_webhook_meta.py tests/services/test_credit_metering.py tests/services/test_safe_egress.py -q
: "${POSTGRES_SMOKE_DATABASE_URL:?POSTGRES_SMOKE_DATABASE_URL must point at a disposable Postgres}"
bash scripts/check-credit-integration.sh tests/integration/test_conversation_language_postgres.py
poetry run black --check src/api/routes/webhook.py tests/api/test_webhook_meta.py tests/services/test_credit_metering.py tests/integration/test_conversation_language_postgres.py
poetry run isort --check-only src/api/routes/webhook.py tests/api/test_webhook_meta.py tests/services/test_credit_metering.py tests/integration/test_conversation_language_postgres.py
poetry run flake8 src/api/routes/webhook.py tests/api/test_webhook_meta.py tests/services/test_credit_metering.py tests/integration/test_conversation_language_postgres.py
poetry run mypy src --ignore-missing-imports
```

**depends-on:** Group 2

---

### Group 5: Card OpenRouter — ajuda e descrição

**Goal:** O tenant sabe, no próprio card, que a configuração do OpenRouter também é usada para entender imagens.

**Deliverables:**
1. Em `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`, a ajuda no card OpenRouter do bloco "API LLM Fallback Multimodal":
   - pt: "Também usado para entender imagens recebidas no WhatsApp (uazapi, Z-API e Meta). As imagens são enviadas ao OpenRouter com a sua chave."
   - en: "Also used to understand images received on WhatsApp (uazapi, Z-API and Meta). Images are sent to OpenRouter with your key."
2. A descrição do bloco (`:557-558`, "duas contingências") passa a citar imagens além de falha do LLM principal e transcrição de áudio.
3. Caso em `clinic-config-multimodal.test.tsx` verificando a ajuda nos dois idiomas.

**Acceptance Criteria:**
- [ ] A ajuda aparece no card OpenRouter nos estados disponível, incompleto e configurado.
- [ ] Os testes existentes do card multimodal e de configurações de API passam sem alteração.

**Validation:**
```bash
# App-level typecheck: the workspace-root `pnpm run typecheck` (`tsc --build`) is broken on origin/main itself —
# frontend/tsconfig.json references lib/db and lib/api-zod, which are not in the repo (found 2026-11-07, pre-existing,
# out of scope; fix on its own branch).
cd frontend && pnpm --filter @workspace/neurostack run test && pnpm --filter @workspace/neurostack run typecheck
```

**depends-on:** none

---

## Final Gate

Gate agregado do repositório, depois de todos os grupos e antes da revisão `final-gate` independente
(Execution Strategy). Meça `make quality` no ponto de ramificação e trate o resultado como piso: o
repositório recebe commits concorrentes.

```bash
make quality
: "${POSTGRES_SMOKE_DATABASE_URL:?POSTGRES_SMOKE_DATABASE_URL must point at a disposable Postgres}"
bash scripts/check-credit-integration.sh tests/integration/test_credit_metering_postgres.py tests/integration/test_credit_schema_dominance_postgres.py tests/integration/test_conversation_language_postgres.py
cd frontend && pnpm --filter @workspace/neurostack run test && pnpm --filter @workspace/neurostack run typecheck  # root tsc --build is broken on main (see G5)
```

---

## Rollout

1. **Antes do deploy** (Decisão 15): o dono roda, com o próprio "vai", uma consulta read-only no banco
   de produção pela task `-check-db`, e avisa cada tenant listado de que as fotos dos pacientes
   passarão a ser enviadas ao OpenRouter com a chave dele. A consulta espelha
   `_multimodal_operational` e o pin de host `openrouter.ai`:
   ```sql
   SELECT tenant_id FROM clinic_configs
   WHERE api_credentials->>'multimodal_provider' = 'openrouter'
     AND api_credentials->>'multimodal_api_url' LIKE 'https://openrouter.ai/%'
     AND coalesce(api_credentials->>'multimodal_api_key', '') <> ''
     AND coalesce(api_credentials->>'multimodal_model_name', '') <> '';
   ```

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Produção, uazapi (instância `NeuroStack`, OpenRouter preenchido): uma foto com legenda gera `image_description_attempt outcome=described`, seguido de `llm_generation_attempt … accepted=true` e `Uazapi reply sent`, e a resposta considera a imagem.
- [ ] Um PDF recebe o texto exato da Decisão 6. "Pode encaminhar" abre o handoff e recebe "Perfeito! Já encaminhei…". Um segundo PDF antes de responder não repete a confirmação.
- [ ] Com o card OpenRouter incompleto, uma foto recebe a confirmação de arquivo.
- [ ] Com a IA pausada, uma foto não gera download, chamada ao OpenRouter nem mensagem.
- [ ] Regressão: áudio, texto, sticker e mensagens `fromMe` pelo uazapi se comportam como antes; nenhum log contém o texto da descrição.
- [ ] Z-API, primeiro tenant: capturar um webhook real de foto **com legenda** e confirmar que ele não traz um objeto `text`. Se trouxer, o código atual trata a mensagem como texto, e a foto não é descrita nem confirmada. Nesse caso, abrir um fix para a mídia ter precedência sobre `text` antes de dar o Z-API por entregue (final-gate MEDIUM-1).

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Dado de saúde (exame, receita, ferida) vai para o OpenRouter e a descrição fica no histórico | High | Aceito pelo dono (Decisão 15); aviso aos tenants no Rollout; aviso LGPD antes da primeira descrição; prompt sem diagnóstico; nenhum byte persistido; log sem a descrição; visualização única excluída no uazapi e no Z-API. |
| Depois de "Já encaminhei", a IA continua respondendo, porque o encaminhamento não pausa (`is_paused`) | Medium | Comportamento atual do Meta, agora também no uazapi e no Z-API; mudar isso fica fora do escopo (Decisão 19). Se a equipe reclamar, um wish próprio pausa a IA no encaminhamento. |
| A palavra "manda", "envia" ou "equipe" (inclusive dentro de "demanda") na resposta é lida como pedido de encaminhar | Medium | Só vale com a pendência ativa (2h, Decisão 4); fora desse prazo o pipeline segue normal. As palavras-chave são reusadas sem mudança. No Meta, uma resposta em áudio não resolve a pendência (só texto, como hoje), então ela fica ativa até 2h. |
| Rajadas e álbuns: imagens simultâneas num tenant sem capacidade disputam `_file_ack_needed` e podem gerar mais de uma confirmação; com capacidade, um álbum de N fotos gera N descrições, N respostas e N débitos | Medium | Aceito; medir pelo `image_description_attempt` e pelos logs de confirmação antes de adicionar agrupamento. |
| No uazapi, uma imagem com legenda reentregue durante uma pausa grava um evento de captura pausada duplicado (a imagem só faz claim depois de `_check_pause_state`, `:1657-1672`) | Low | Aceito: só o registro duplica; nada é enviado ao paciente nem ao OpenRouter. |
| O modelo multimodal do tenant não aceita imagem | Medium | Vira `provider_failed` → fluxo de arquivo; o `outcome` deixa o caso visível. |
| Os nomes de campo de imagem, documento, vídeo e visualização única no Z-API, e de documento, vídeo e visualização única no uazapi, não foram provados por payload capturado | Medium | Detecção tolerante e testada; um objeto de mídia sem URL vai para a confirmação, nunca para o silêncio; Z-API só é considerado entregue depois da QA do primeiro tenant. |
| Z-API e Meta não podem ser validados em produção (nenhum tráfego em 14 dias) | Medium | A QA de produção cobre o uazapi; Z-API e Meta ficam provados por testes de handler e de crédito. |
| No Meta, uma foto de visualização única continua sem resposta | Low | Limite técnico (Decisão 13); igual a hoje. |
| O webhook uazapi de produção não tem autenticação: um POST forjado com o nome real da instância pode fazer o servidor baixar mídia da própria instância do tenant e mandá-la ao OpenRouter com a chave dele, ou enviar a confirmação para um número qualquer | Medium | Já existe hoje para texto (um POST forjado já gasta um turno de LLM); este wish só aumenta o custo por requisição. Mitigações atuais: claim por `msg_id`, download só por id na instância do tenant (o atacante não fornece bytes nem URL) e teto de 5 MB. A correção real é autenticar o webhook, num trabalho separado (registrado na memória do projeto em 2026-11-06). |
| uazapi: se o egress fechar entre a pré-checagem e o download, o aviso LGPD pode sair duas vezes (a sessão volta atrás com o 503 e a reentrega vê `is_new` de novo) | Low | Janela de milissegundos; aceito (final-gate LOW-2). |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### 2026-11-07 — Plan review, round 1 — FIX-FIRST

- **Target:** WISH.md v1, sha256 `f43bb95ed4e91ec0820f589fa39ff3dc510e1e2cebb31a40e9628872bde10450`; baseline `origin/main` `8d35dacf84fe932ececaa73dd819cad98d62bc19` (fbda835a, em produção, é ancestral e não tem diff nos arquivos tocados). Read-only, nenhum teste executado.
- **Result:** 0 CRITICAL, 2 HIGH, 8 MEDIUM, 10 LOW.
- **HIGH:**
  - H1: a mídia rodava antes do check de IA/pausa/`precheck_zapi`.
  - H2: o Grupo 4 descrevia errado o ramo de confirmação do Meta.
- **MEDIUM:**
  - M1: ordem do claim.
  - M2: `pending_file_review` no caminho de sucesso.
  - M3: crédito da imagem descrita.
  - M4: outcomes anteriores à cadeia.
  - M5: o teto não valia no uazapi e no Z-API.
  - M6: visualização única.
  - M7: tolerância do Z-API.
  - M8: ordem do aviso LGPD e aviso aos tenants.
- **LOW:** L1–L10 (constante, sender do Z-API, import, testes invertidos, copy, gate final, texto do card, idioma e códigos, Evolution, wave do G5).
- **Amendment v2 (2026-11-07):**
  - Todos endereçados; as decisões do dono foram confirmadas por ele.
  - Escopo ampliado a pedido do dono: documento e vídeo com confirmação e encaminhamento.

### 2026-11-07 — Plan review, round 2 — FIX-FIRST

- **Target:** WISH.md v2, sha256 `52ccfae4374a4ea8e60a9a3cdddb2881f0cffb811ef374fcb25d733d48a4b8f1`; mesmo baseline. Read-only.
- **Round 1:** 16 resolvidos, 4 parciais (M6, L4, L6, M8).
- **New:** 0 CRITICAL, 1 HIGH, 5 MEDIUM, 10 LOW.
  - **HIGH:** N-H1, `pending_file_review` sem prazo + não reenvio → silêncio permanente e encaminhamento falso.
  - **MEDIUM:**
    - N-M1: inserção no uazapi depois do `assert`.
    - N-M2: guardas `:2606`/`:2612` do Z-API.
    - N-M3: rastreamento de eco e ordem de envio.
    - N-M4: o gate de Postgres com `tee` escondia falhas.
    - N-M5: visualização única no Meta.
  - **LOW:**
    - N-L1: gate final sem Postgres.
    - N-L2: personalização do Meta.
    - N-L3: rótulo `meta_file_handoff`.
    - N-L4: corpos e eventos.
    - N-L5: claim de documento e vídeo.
    - N-L6: LGPD perdido na reentrega.
    - N-L7: SQL do rollout.
    - N-L8: waves e `test_credit_metering`.
    - N-L9: definição de "pausada".
    - N-L10: detalhes (aviso de privacidade, legenda, kind, metering).
- **Confirmado pela revisão:** reuso dos helpers do Meta, `run_meta_file_handoff` agnóstico, `enforcement` em escopo no Z-API, stub do Evolution coerente.
- **Amendment v3 (2026-11-07, pelo autor):**
  - Todos os 16 endereçados (Decisões 3, 4, 6–9, 12, 13, 17–19; G1–G4; Final Gate; Rollout; Risks).
  - Tradução em inglês aprovada pelo dono.
  - Aguarda a rodada 3, a última do loop antes de BLOCKED.

### 2026-11-07T04:44Z — Plan review, round 3 — SHIP

- **Target:** WISH.md v3, sha256 `b93f9af8c1accfabeb25d106d22a78ba82a4a684372c60ecb8b763c42c7ef393`; baseline `origin/main` `8d35dacf84fe932ececaa73dd819cad98d62bc19`. Read-only, lente questioner.
- **Round 2:** 15 resolvidos e 1 parcial (N-L8). Dos parciais da rodada 1, M6/L6/M8 resolvidos e L4 parcial.
- **Result:** 0 CRITICAL, 0 HIGH, 4 MEDIUM, 9 LOW.
  - **MEDIUM:**
    - R3-M1: o teste estrutural `test_credit_metering.py:2168-2170` fixa os call sites de `_meta_file_handoff`.
    - R3-M2: G2 e G4 em paralelo no mesmo `webhook.py`.
    - R3-M3: no uazapi, a legenda espelhada em `message.text` resolveria a pendência.
    - R3-M4: `_meta_get_pending_file_review` morto, com patches em testes, um deles de Postgres.
  - **LOW:** R3-L1 a R3-L9.
- **Decisões do autor:** julgadas sólidas no mérito (4, 3, 9, 13, 17, 18, 19); `pending_file_review_at` justificado por SC 4/5.
- **Post-SHIP fold (2026-11-07, pelo orquestrador):** as correções prescritas pelo revisor foram aplicadas como ele as escreveu, sem mudar a abordagem:
  - R3-M1: G2 d3/validação, G3 d5/AC, SC 7;
  - R3-M2: waves em sequência G2 → G4 → G3, e G4 passa a depender do G2;
  - R3-M3: definição de mensagem textual na Decisão 3 e testes do G2;
  - R3-M4: G4 d3, validação e Final Gate;
  - LOW: R3-L1 (predicado sem escrita), R3-L2, R3-L3, R3-L4, R3-L5, R3-L6, R3-L7 (aceito em Risks), R3-L8 e R3-L9.
- **Status:** APPROVED.

### 2026-11-07 — Execution review, Group 5 — SHIP

- **Target:** uncommitted `frontend/` diff in `/home/lorenzo/work/neurostack-whatsapp-image` (base `origin/main` 8d35dacf). 4 files: `provider-credential-card.tsx` (+ optional `help` prop), its test, `clinic-config.tsx`, `clinic-config-multimodal.test.tsx`.
- **Reviewer:** independent `reviewer`.
- **Result:** 0 CRITICAL/HIGH.
  - Help strings byte-identical (NFC).
  - DOM unchanged without `help`.
  - Help only on the multimodal OpenRouter card (not the STT one), chosen by locale.
  - Root `tsc --build` failure confirmed pre-existing: `frontend/tsconfig.json` references `lib/db` and `lib/api-zod`, which are absent on main.
- **Gaps:** MEDIUM-1 (page test asserted before load: `act(...)` warnings, single state), LOW-1 (`txt` helper), LOW-2 (block description untested), LOW-3 (no change).
- **Fix:** the engineer applied MEDIUM-1, LOW-1 and LOW-2 (6 page cases pt/en × available/incomplete/configured, waiting for `data-card-state`; exact description strings).
- **Orchestrator validation:**
  - `pnpm --filter @workspace/neurostack run test` → 41 files / 868 tests passed;
  - app typecheck exit 0;
  - multimodal test file alone 10/10, with 0 `act(`/`Warning` lines in its output.
- **Group 5 task done.**

### 2026-11-07T06:18Z — Execution review, Group 1 — SHIP

- **Target:** uncommitted `src`/`tests` diff in `/home/lorenzo/work/neurostack-whatsapp-image` (HEAD 8d35dacf); `git diff HEAD -- src tests` sha256 `9d8e93f9315d538e…`. 5 modified files + 2 new tests (`tests/services/test_multimodal_image.py`, `tests/api/test_webhook_image_helpers.py`).
- **Reviewer:** independent `reviewer`.
- **Validation re-run by the reviewer:** focused modules 419 passed + `test_credit_metering.py` 147 passed (= 566); black/isort/flake8 clean on the 7 files; `mypy src` clean (206 files).
- **Floor at 8d35dacf** (detached snapshot, measured by the engineer): black already fails on `src/api/routes/billing.py` and `tests/scripts/test_asaas_production_wiring.py`; isort fails on the latter; flake8 and mypy clean. Unchanged after Group 1.
- **Result:** 0 CRITICAL/HIGH/MEDIUM.
  - Copy byte-exact.
  - D4: aware UTC timestamp, read-only predicate, clear removes both keys, `_meta_get_pending_file_review` kept for G4.
  - Egress pin enforced lexically before any DNS or client.
  - D12 downloader cap enforced on both Content-Length and stream; redirects refused; octet-stream only with a declared MIME; audio downloader unchanged.
  - No bytes or description in logs; scope clean.
- **LOW gaps:**
  - LOW-1: no test pins the `image_url` kind.
  - LOW-2: no shared mapping from download error to event outcome, needed by G3/G4.
  - LOW-3: `noqa: F401` covers the whole multi-line import.
  - LOW-4: the helper test file is missing from the wish's file list and validations (fixed in WISH.md by the orchestrator).
- **INFO:** a future-dated timestamp counts as absent; the description has no `max_tokens`; the Meta text branch reads the old getter until G4.
- **Open:** LOW-1..3 not applied — the Group 1 engineer was stopped by the owner before receiving them. The after-change full run (PID 57235) was still running at 15%. The detached snapshot `/home/lorenzo/work/wiu-base-8d35dacf` still exists. The G1 task stays `in_progress` (claimed by engineer-g1) until the owner decides.
- **Fix and close (2026-11-07):**
  - The owner authorised a new agent. A fixer applied LOW-1 (test pins the `image_url` policy; mutation to `audio_url` fails it), LOW-2 (`_IMAGE_DOWNLOAD_ERROR_OUTCOMES` + `_image_download_outcome`, 6 parametrized cases) and LOW-3 (the `noqa` sits on its own `safe_download_image` import line). The optional `max_tokens` was skipped because the audio adapter sets none.
  - Orchestrator validation: focused pytest 573 passed; black/isort/flake8 clean on the 7 files; `mypy src` clean (206 files).
  - **Group 1 task done.** The full-suite after-run (PID 57235) is still running as extra evidence; its failures will be triaged against the 8d35dacf snapshot.
- **Full-suite after-run (G1+G5, first worktree, 2026-11-07):** `poetry run pytest -q -p no:randomly` →
  `2 failed, 13642 passed, 1359 skipped, 665 warnings, 97 errors in 3842.24s`. All 99 failing/erroring ids, re-run on the
  clean 8d35dacf snapshot `/home/lorenzo/work/wiu-base-8d35dacf` with the same venv, give the **identical** set (2 failed,
  97 errors): every one is pre-existing, so there is no regression.
  - Pre-existing set (the floor for the Final Gate):
    - `tests/integration/test_builder_cloning_postgres.py` (36 E) and `tests/integration/test_media_assets_postgres.py` (46 E), which error instead of skipping without Postgres;
    - `tests/scripts/test_backup_encryption.py` (15 E);
    - `tests/repositories/test_database.py::test_only_database_application_module_creates_async_engines` (F);
    - `tests/scripts/test_guard_alembic_upgrade_head.py::test_dev_stack_field_leaves_project_services_and_volumes_alone` (F).
  - Pass-count floor: 13642.
- **Group 2 workspace:** detached worktree `/home/lorenzo/work/neurostack-wiu-g2` at 8d35dacf with the G1+G5 diff applied (patch sha256 `64f05fce…`; all 11 files byte-identical to the first worktree), so G2 does not disturb the running full suite.

### 2026-11-07T11:05Z — Execution review, Group 2 (uazapi) — SHIP

- **Target:** `/home/lorenzo/work/neurostack-wiu-g2` (detached 8d35dacf + G1/G5/G2 uncommitted). G2 differs from the G1+G5 tree in exactly 3 files: `src/api/routes/webhook.py`, `tests/api/test_webhook_uazapi.py`, and `tests/services/test_credit_metering.py` (one structural test).
- **Reviewer:** independent `reviewer`.
- **Validation:**
  - Reviewer: scoped pytest 356 passed; neighbours (meta, zapi, credit_metering) 282 passed; black/isort/flake8 clean; `mypy src` clean.
  - Orchestrator: scoped 356 passed, same lint/type results.
  - Reviewer mutation probes: 6. The control mutants were killed, as was the flag-before-send mutant. M1 (no claim on pre-download refusals) and M2 (egress pre-check above the AI-off/pause gates) survived and became LOW-1/LOW-2. M6 survived but is harmless.
- **Result:** 0 CRITICAL/HIGH/MEDIUM.
  - D3/D4/D8/D9/D10/D12/D13/D14/D17/D18/D7/D19 verified with `file:line`.
  - Freeze rule respected.
  - `_claim_audio_message` → `_claim_deferred_message` is a pure rename.
- **LOW gaps:**
  - LOW-1: no redelivery test for pre-download refusals.
  - LOW-2: nothing pins the egress pre-check below the AI-off/pause gates.
  - LOW-3: the `size_bytes` semantics of pre-chain refusals (declared `fileLength`) are undocumented.
- **Engineer's open points, all accepted:**
  - (a) the forward answers `file_handoff` even if the confirmation send fails, as Meta does;
  - (b) a `*ViewOnce*` message of unknown kind gets the ack;
  - (c) declared `fileLength` as `size_bytes` (rule for G3/G4: report the channel's declared size the same way);
  - (d) the "Meta" wording in `_meta_file_handoff` and its log stays.
- **Hazards passed on to G3/G4:**
  - do not port uazapi's deferred `claim` callback; Z-API and Meta already claim at the top, and a second `SET NX` claim returns False;
  - the 503 egress pre-check is uazapi-specific;
  - G4 switches Meta's text branch to `_pending_file_review_active`;
  - G3 moves the structural test to 4 call sites;
  - reproduce D17 with each channel's own id tracking.
- **Status:** LOW-1..3 sent back to the G2 engineer and applied (2026-11-07):
  - LOW-1: redelivery test for view-once and declared-oversize images;
  - LOW-2: the AI-off and pause gates run before the egress check;
  - LOW-3: docstring sentence.
  - Both new tests fail under their mutant (verified, restored byte-identical).
- **Orchestrator validation after the fixes:** G2 pytest 360 passed; neighbours (meta, zapi, credit_metering) 282 passed; black/isort/flake8 clean on the 3 files; `mypy src` clean (206 files); still exactly 3 files differ from the G1+G5 tree.
- **Group 2 task done.**

### 2026-11-07 — Execution review, Group 4 (Meta) — SHIP

- **Target:** canonical worktree `/home/lorenzo/work/neurostack-wiu-g2` (8d35dacf + G1/G5/G2/G4 uncommitted). G4 files: `webhook.py` (Meta), `test_webhook_meta.py`, `test_credit_metering.py`, and `test_conversation_language_postgres.py` (patch repoint).
- **Reviewer:** independent `reviewer`.
- **Validation:**
  - Reviewer: 533 passed (meta/credit/egress/helpers); 226 passed (uazapi+zapi); lint/types clean; ast comparison of 39 frozen symbols all byte-identical; zero `_meta_get_pending_file_review` hits.
  - Orchestrator: 533 passed; black/isort/flake8 clean; `mypy src` clean; Postgres gate `check-credit-integration.sh tests/integration/test_conversation_language_postgres.py` on a disposable cluster → 7 of 7, 0 skipped; cluster torn down.
- **Result:** 0 CRITICAL/HIGH.
  - The file-branch move is behaviour-preserving (only G1's copy and the new suppression differ).
  - No claim or effect on any fallback, in both worlds.
  - The described image is charged once (enforced).
  - `privacy_sent` is correct on every path.
  - The end-to-end test goes through the real resolve, download and adapter.
- **Gaps:**
  - MEDIUM-1: no Meta image test for AI-off/paused sessions.
  - LOW-1: `_uazapi_declared_size` used for Meta; rename after the wish.
  - LOW-2: a Meta **sticker** during an active review is now suppressed too (Deliverable 2/D4 vs the "sticker unchanged" criterion); needs the owner's confirmation.
  - LOW-3: no test where no MIME is declared anywhere.
  - LOW-4: the Bearer token may go to any public HTTPS host the Graph resolve returns. Pre-existing for audio; follow-up to pin Meta CDN hosts.
  - LOW-5: `test_credit_schema_dominance_postgres.py` exercises the moved code; added to the Final Gate Postgres run (above) by the orchestrator.
- **Engineer's 5 choices:** all accepted.
- **Hazards passed on to G3:**
  - put Z-API's image path after `precheck_zapi` and the session, not at the audio position;
  - guard Z-API's single privacy-notice call;
  - track and store the ack id before the flag (Z-API echoes `fromMe`);
  - no new claim;
  - port the free-ack test to both worlds;
  - the structural count becomes 4;
  - patching `_pending_file_review_active` also flips `_file_ack_needed`.
- **Integration note:** `origin/main` has moved to 60fc5299, and #185 touches `clinic-config.tsx` (overlaps G5).
- **Status:** MEDIUM-1 and LOW-3 tests sent back to the G4 engineer and applied (2026-11-07):
  - `TestMetaImageGates` (bot_off/paused → nothing; fails under the "describe before the gates" mutant);
  - a `no_mime_declared_anywhere` case (`declared_mime=None`; fails under a mutant).
  - `webhook.py` was unchanged by these fixes.
- **Owner decision on LOW-2 (2026-11-07):** a sticker follows the same no-resend rule as files on Meta. The "sticker unchanged" success criterion reads as "same file-branch behaviour, new copy, no-resend within the window".
- **Orchestrator validation after the fixes:** G4 pytest 536 passed; black/isort/flake8 clean on the 4 files.
- **Group 4 task done.**

### 2026-11-07T13:37Z — Execution review, Group 3 (Z-API) — SHIP

- **Target:** canonical worktree `/home/lorenzo/work/neurostack-wiu-g2`.
  - G3 files: `webhook.py` (only `_process_zapi_authenticated_body` + 7 new Z-API helpers; 146 other symbols identical by AST); `credit_metering.py` (docstrings only, AST-equal without docstrings, verified by the reviewer and the orchestrator).
  - Tests: `tests/fixtures/zapi/media-payloads.json` (from https://developer.z-api.io/webhooks/on-message-received-examples, marked "não verificado"), `test_webhook_zapi.py`, `test_credit_metering.py`, `test_credit_metering_postgres.py`.
- **Reviewer:** independent `reviewer`.
- **Validation:**
  - Reviewer: 521 passed; neighbours (uazapi + Meta) 309 passed; lint/types clean; 13/13 mutants killed.
  - Orchestrator: 521 passed; black/isort/flake8 clean; `mypy src` clean; Postgres gate on a disposable cluster, `check-credit-integration.sh` over `test_credit_metering_postgres.py`, `test_credit_schema_dominance_postgres.py` and `test_conversation_language_postgres.py` → **94 of 94, 0 skipped**; cluster torn down.
- **Result:** 0 CRITICAL/HIGH.
  - Guards admit only patient media; staff media never pauses.
  - Insertion point is after `precheck_zapi` and the session.
  - The enforced forward creates a `zapi` claim and a debit; redelivery gives `duplicate_message` with no second debit.
  - The ack is free in both worlds.
  - Echo tracking matches the reply path.
- **Gaps:**
  - MEDIUM-1: Z-API group/channel chats entered the new media flow (third-party images to OpenRouter, a forward posted in a group).
  - LOW-1: the `no_credentials` ack branch is untested.
  - LOW-2: the "Meta file handoff initiated" log is accepted.
- **Departures:** (a) to (c) and (e) accepted; (d) became MEDIUM-1.
- **Owner decisions (2026-11-07):**
  - MEDIUM-1: Z-API media from groups/channels (`isGroup`, `isNewsletter`, `-group` phone) is ignored like uazapi; sent back to the G3 engineer with LOW-1.
  - The pre-existing Z-API answering of group TEXT is fixed **outside this wish**, on branch `fix/zapi-skip-group-chats` (worktree `/home/lorenzo/work/neurostack-zapi-groups`, off origin/main 126165f5), per the isolate-unrelated-fixes rule. Expect an overlap in `_process_zapi_authenticated_body` when both land.
- **Integration note:** origin/main is now 126165f5 (#185 and #192 touch `clinic-config.tsx`, which overlaps G5).
- **G3 close (2026-11-07):**
  - MEDIUM-1 applied: `_zapi_patient_file_kind` returns `None` for `isGroup`, `isNewsletter` or a `-group` phone; 9 cases; mutation checks per condition.
  - LOW-1 applied: `no_credentials` test.
  - Orchestrator validation: G3 pytest 531 passed; black/isort/flake8 clean; `mypy src` clean.
  - **Group 3 task done.**
- **Merge interaction with `fix/zapi-skip-group-chats`** (reviewed SHIP separately):
  - The fix adds a route-level `group_chat` skip as the first statement of `_process_zapi_authenticated_body`.
  - `webhook.py` merges cleanly (simulated with `git merge-file`). `tests/api/test_webhook_zapi.py` has 5 append-only conflict hunks: keep both blocks.
  - Whichever lands second must change this wish's `test_group_and_channel_media_keep_the_non_text_exit` expectation from `non-text` to `{"ok": True, "skipped": "group_chat"}`, and may delete the now-unreachable group/newsletter branch in `_zapi_patient_file_kind`.

### 2026-11-07 — Final Gate (orchestrator) — PASS against the floor

Run in the canonical worktree `/home/lorenzo/work/neurostack-wiu-g2` (8d35dacf + all 5 groups, uncommitted). The tree was unchanged during the gate: no file under src/tests/packages is newer than the G3 validation log.

| Check | Result | Floor (8d35dacf) |
|---|---|---|
| `black --check src packages/neurostack-core/neurostack_core tests` | 2 would reformat (`src/api/routes/billing.py`, `tests/scripts/test_asaas_production_wiring.py`), 601 unchanged | same 2 files |
| `isort --check-only` (same paths) | only `tests/scripts/test_asaas_production_wiring.py` | same file |
| `flake8 src packages/neurostack-core/neurostack_core tests` | 0 | 0 |
| `mypy src packages/neurostack-core/neurostack_core` | no issues in 249 files | clean |
| full `pytest -q -p no:randomly` | **13870 passed, 1360 skipped, 2 failed, 97 errors** (see note) | 13642 passed + the same 99 pre-existing fail/error |
| `check-credit-integration.sh` on `test_credit_metering_postgres.py` + `test_credit_schema_dominance_postgres.py` + `test_conversation_language_postgres.py` (disposable PG) | 94 of 94, 0 skipped | — |
| frontend `pnpm --filter @workspace/neurostack run test` + app typecheck | 41 files / 868 tests passed; typecheck exit 0 | root `tsc --build` broken on main (pre-existing) |

- **Note on the full run:** it died at 93% when the session restarted (no summary printed). The 93% part counts 12827 passed, 1359 skipped, 2 F and 97 E. Its F/E match the floor exactly per file:
  - `test_builder_cloning_postgres.py` 36 E;
  - `test_media_assets_postgres.py` 46 E;
  - `test_database.py` 1 F;
  - `test_backup_encryption.py` 15 E;
  - `test_guard_alembic_upgrade_head.py` 1 F.

  The 39 files it never finished were then run separately: `1043 passed, 1 skipped`, 0 failures.
- **Verdict:** no regression; pass count +228 over the G1+G5 floor (new G2/G4/G3 tests).

### 2026-11-07 — Final-gate review (independent) — FIX-FIRST

- **Target:** canonical worktree `/home/lorenzo/work/neurostack-wiu-g2` (8d35dacf + 5 groups); wish sha256 `5a000f1c…`; `origin/main` 126165f5, 9 commits ahead (#184–#192).
- **Reviewer runs:** focused suite 949 passed; gate order verified per channel by line; 3-way `git merge-file` on every overlapping file.
- **Success Criteria:** 1–14 proven by tests (SC 9 with an unverified view-once signal; SC 6 Meta echo partly by review). SC 15 (Final Gate) is proven against 8d35dacf only. QA is post-merge by design.
- **Findings:**
  - **CRITICAL-1:** `webhook.py:68` and `test_webhook_image_helpers.py:112` import `MEDIA_IMAGE_MAX_BYTES` from `credit_allocations`, but #184 moved it to `src/services/media_assets.py`. The merge has no conflict, yet the router fails at import, which takes down every webhook route. Fix: repoint both imports, then re-run the Final Gate on the rebased tree.
  - **MEDIUM-1:** on Z-API, a `text` object takes precedence over `image`. If a real captioned-image payload carries `text`, the image is handled as text. Mitigated by the QA step added above.
  - **MEDIUM-2:** the unauthenticated uazapi webhook can now trigger OpenRouter calls paid by the tenant. Risks row added above.
  - **LOW-1:** 2 textual conflicts in `provider-credential-card.tsx` with #190 (resolution: keep both `sub?` and `help?`).
  - **LOW-2:** duplicate LGPD notice in a millisecond egress race (accepted; Risks row).
  - **LOW-3:** one extra PK read per text turn on uazapi and Z-API.
  - **LOW-4:** pre-existing items confirmed not widened (Meta Bearer host, `str(exc)` in the Meta ack body).
- **Cross-channel differences, shared state and credits:** consistent and documented. The 4-site structural test is the right invariant. Order of landing with `fix/zapi-skip-group-chats` doesn't matter.
- **Remediation in progress:**
  - The orchestrator replayed the full wish diff (patch sha256 `531116f2…` + 3 untracked files) onto `origin/main` 126165f5 in a new worktree `/home/lorenzo/work/neurostack-wiu` with `git apply --3way` (only `provider-credential-card.tsx` conflicted), then unstaged it.
  - A fixer is applying CRITICAL-1 and LOW-1 there.
  - Next: re-run the Final Gate on that tree (floor re-measured at 126165f5), then a final-gate re-review.
  - `/home/lorenzo/work/neurostack-wiu` becomes the canonical worktree.

### 2026-11-07 — Rebase onto origin/main 126165f5 + Final Gate re-run (orchestrator)

- **Fixes applied by `fixer-final` in `/home/lorenzo/work/neurostack-wiu`:**
  - CRITICAL-1: `webhook.py:88` and `test_webhook_image_helpers.py:112` now import `MEDIA_IMAGE_MAX_BYTES` from `src.services.media_assets`. There is no cycle, and no wish line uses any other symbol that main moved.
  - LOW-1: `provider-credential-card.tsx` keeps both `sub?` and `help?` and both conditional spans; no conflict markers remain anywhere.
  - The fixer's backend run was killed by a session restart before it finished; the orchestrator re-ran everything below.
- **Floor at 126165f5** (clean detached snapshot `/home/lorenzo/work/wiu-base-126165f5`): black 601 unchanged, i.e. #184 and the other merged PRs fixed the two files that used to fail; isort clean; flake8 clean; mypy clean on 249 files. The floor is now fully green.
- **Rebased tree** (`/home/lorenzo/work/neurostack-wiu`): router import OK; `black --check` 603 unchanged; isort 0 errors; flake8 0; mypy clean on 249 files; frontend 42 files / 891 tests plus app typecheck exit 0; credit Postgres gate (3 modules) 94 of 94, 0 skipped.
- **Full suite** (`poetry run pytest -q -p no:randomly -rfE`): **13900 passed, 1360 skipped, 2 failed, 97 errors** in 47:27. The 99 failing/erroring ids, re-run on the clean 126165f5 snapshot with the same venv, give an **identical** set (2 failed, 97 errors): the same pre-existing modules as before (`test_builder_cloning_postgres.py` 36 E, `test_media_assets_postgres.py` 46 E, `test_backup_encryption.py` 15 E, `test_database.py` 1 F, `test_guard_alembic_upgrade_head.py` 1 F). No regression.
- **Final Gate on 126165f5: PASS.** It still needs the independent final-gate re-review.
- **origin/main moved again** to ac71dda5 (#193: media UI plus `src/api/routes/media_assets.py` and tests). It touches none of the wish's files, and `src/services/media_assets.py` (the new import source) is unchanged. At PR time, replay onto the then-current main and re-run at least the router import, the webhook suites and the credit Postgres gate.

### 2026-11-07T16:33Z — Final-gate review, round 2 (independent) — SHIP

- **Target:** `/home/lorenzo/work/neurostack-wiu` at 126165f5 plus the wish, uncommitted. Wish patch sha256 `9b2f254a…`. Local origin/main ac71dda5 (#193).
- **Replay check against the round-1 tree:** 12 of 15 files have byte-identical diffs. The other 3 differ only by the import repoint (`webhook.py:88`) and by main's own #190/#191 context lines (card, `clinic-config.tsx`). Of the untracked files, 2 are identical and `test_webhook_image_helpers.py` differs only at line 112 (import).
- **Moved symbols:** none of the symbols moved by #184–#193 is used by the wish (zero grep hits). The only change on the frontend side is `TTS_PROVIDERS` `sub?`, which the wish doesn't touch.
- **Commands run by the reviewer:**
  - router import OK;
  - focused backend suites 902 passed;
  - black/isort/flake8 clean on the 13 changed Python files;
  - frontend card and page tests 21 passed;
  - the wish patch applies cleanly onto ac71dda5 (dry run, temporary index).
- **Round-1 resolution:**
  - CRITICAL-1 resolved;
  - LOW-1 resolved;
  - MEDIUM-1 adequately handled by the QA step (wording matches the code at `webhook.py:2972`);
  - MEDIUM-2 adequately handled by the Risks row (mitigations verified in code);
  - LOW-2 accepted.
- **New findings:** none blocking. LOW, informational, pre-existing: `download_uazapi_media` has no HTTP-layer byte cap; the 5 MB limit is enforced after download, as for audio today. Optional future fix: a streaming `max_bytes`.
- **Verdict: SHIP.** Execution is complete.
- **Next, needs the owner:**
  - commit plus PR (replay onto the then-current main first; re-run the router import, the webhook suites, `test_webhook_image_helpers` and the credit Postgres gate);
  - the Rollout tenant notice before deploy;
  - production QA.
- **Status:** stays `IN_PROGRESS` until merge plus QA.

---

## Files to Create/Modify

```
src/services/multimodal_service.py                       # G1 describe_image_multimodal, marker, prompt, codes
src/services/safe_egress.py                              # G1 safe_download_image, kind image_url
src/api/routes/webhook.py                                # G1 helpers/copy/pendência/import; G2 uazapi; G3 zapi; G4 meta
src/services/credit_metering.py                          # G3 only if run_meta_file_handoff needs a zapi fix (+ docstrings)
tests/services/test_multimodal_image.py                  # G1 (new)
tests/api/test_webhook_image_helpers.py                  # G1 (new) — unit tests of the webhook helpers; re-run by G2–G4
tests/services/test_safe_egress.py                       # G1
tests/api/test_webhook_meta.py                           # G1 copy/signature; G4
tests/api/test_webhook_uazapi.py                         # G2
tests/fixtures/zapi/media-payloads.json                  # G3 (new)
tests/api/test_webhook_zapi.py                           # G3
tests/services/test_credit_metering.py                   # G1 (read), G2, G4, G3 — never in parallel
tests/integration/test_credit_metering_postgres.py       # G3
tests/integration/test_conversation_language_postgres.py # G4 (patch repointed)
frontend/artifacts/neurostack/src/pages/clinic-config.tsx                 # G5
frontend/artifacts/neurostack/src/components/api-settings/provider-credential-card.tsx # G5 (optional `help` prop; scope widened by the orchestrator 2026-11-07 — the card had no help-text slot)
frontend/artifacts/neurostack/src/pages/clinic-config-multimodal.test.tsx # G5
```
