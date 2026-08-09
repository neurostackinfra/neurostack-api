# Wish: Fallback texto — degradação graciosa da transcrição de áudio

| Field | Value |
|-------|-------|
| **Status** | DRAFT |
| **Slug** | `fallback-texto` |
| **Date** | 2026-08-09 |
| **Author** | Lorenzo |
| **Appetite** | medium |
| **Branch** | `wish/fallback-texto` |
| **Repos touched** | neurostack-api |
| **Design** | [DESIGN.md](../../brainstorms/fallback-texto/DESIGN.md) |

## Summary

Hoje, quando a transcrição de áudio falha, o webhook encerra em silêncio e o paciente nunca
recebe resposta. Este wish corrige primeiro um defeito de credenciais já em produção que torna
todo o caminho de áudio do Z-API inerte, e então constrói a cadeia de degradação: STT principal
→ modelo multimodal opcional como segunda tentativa → recondução da conversa por texto, com uma
mensagem honesta e enviada uma única vez por conversa.

## Scope

### IN

- Correção da fonte de credenciais do handler de áudio Z-API, cobrindo `stt_api_key`,
  `zapi_token`, `zapi_client_token` e `multimodal_*`.
- Adaptador multimodal de transcrição em runtime, com políticas de safe-egress próprias.
- Cadeia de fallback compartilhada entre Z-API e Meta Cloud API.
- Mensagens de recondução em PT e EN, com variantes distintas para "STT nunca configurado" e
  "STT configurado mas falhou", entregues uma vez por conversa.
- Conversa marcada como não lida após a recondução.
- Rótulo `RECOMENDÁVEL` no card multimodal da aba APIs e promoção de `multimodal_readiness`
  para `active` com credenciais completas.

### OUT

- **Fallback de TTS.** O agente não envia áudio em runtime (`tts_readiness` é
  `configuration_only`, `src/services/api_credentials.py:451`; nenhuma chamada de TTS existe em
  `src/`). Não há de que degradar; seria código morto.
- **Roteamento automático em falha do provedor de LLM de texto.** `src/llm/client.py` é de
  provedor único; é outro subsistema e merece wish próprio.
- Adapters STT de ElevenLabs, Google, Deepgram e Microsoft — seguem em
  `UNSUPPORTED_STT_PROVIDERS` (`src/api/routes/webhook.py:59`) com o envelope de skip atual.
- Circuit breaker, retry com backoff ou fila de reprocessamento de áudio.
- Ampliar o allowlist de `runtime_projection` para carregar segredos no cache Redis.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Corrigir a fonte de credenciais como Grupo 1, entregável sozinho | É um bug ativo em produção, independente da feature, e precisa poder sair antes dela |
| 2 | Reusar `_load_tenant_credentials` (`webhook.py:642-651`) em vez de criar loader novo ou ampliar o allowlist do cache | Mantém segredos fora do Redis, preserva o invariante testado em quatro lugares e resolve chave de STT e tokens de envio de uma vez; é o que o canal Meta já faz em `:2360` |
| 3 | Ordem STT → multimodal → texto | STT dedicado é mais barato e preciso; multimodal é rede de segurança, não padrão |
| 4 | Multimodal opcional, rotulado `RECOMENDÁVEL` na UI | Clínicas sem multimodal seguem funcionando com o degrau de texto |
| 5 | Texto é sempre o último degrau, inclusive se o multimodal falhar | Garante que nenhum áudio termine em silêncio, em qualquer combinação de falhas |
| 6 | Mensagens distintas para "nunca configurado" e "falhou agora" | Dizer "no momento não estou conseguindo" para quem nunca configurou STT é falso e cria expectativa errada |
| 7 | Aviso uma vez por conversa, via Redis com TTL e fallback em memória | Evita spam em rajada de áudios; reaproveita o padrão de dedup já em `webhook.py:123-180` |
| 8 | Marcar conversa como não lida, sem handoff nem pausar a IA | Espelha o fluxo de áudio longo (`webhook.py:1086`); a clínica enxerga e a IA segue atendendo por texto |
| 9 | Helper de cadeia único para Z-API e Meta | Os canais já divergiram: o Meta funciona e o Z-API está quebrado |
| 10 | Providers STT sem adaptador continuam saindo antes de qualquer download | Preserva o invariante de `webhook.py:1048-1049`, fechado como CRITICAL no wish `api-provider-settings` |

## Simplicity Case

- **Simplest complete design:** um `try/except` em cada handler que, ao falhar, manda uma
  mensagem de texto. Não basta por três razões verificadas: o usuário pediu explicitamente o
  degrau multimodal; no Z-API nem a chave de STT nem os tokens de envio chegam ao handler, então
  a mensagem dispararia sempre e ainda assim não seria entregue; e os dois canais divergiriam.
- **Added machinery:**
  - *Correção de credenciais* — **nenhuma máquina nova**: troca de fonte para uma função que já
    existe e já é usada em seis pontos do mesmo arquivo.
  - *Adaptador multimodal* — pago por requisito explícito do usuário.
  - *Políticas de safe-egress* — pagas pela arquitetura existente: `validate_url(kind=...)`
    levanta exceção para um `kind` desconhecido, então não há como chamar o provedor sem elas.
  - *Chave de aviso com TTL* — paga por rajadas: sem ela, cinco áudios seguidos geram cinco
    avisos idênticos. Usa o padrão de Redis com fallback em memória já presente no arquivo, sem
    estado durável novo nem migração.
- **Deferred until measured:** circuit breaker por clínica, apenas quando houver medição de
  falhas repetidas de STT por tenant; um terceiro degrau de STT; toggle de configuração para
  ligar/desligar o fallback — o padrão opinativo é sempre reconduzir.
- **Complexity removed:** nenhum retry com backoff, nenhuma fila de reprocessamento, nenhum
  estado durável novo em Postgres, nenhum caminho de TTS dormente, nenhuma flag por tenant, e
  nenhum loader de credenciais novo — o design review mostrou que `_load_tenant_credentials` já
  cobria o caso com alcance maior.

## Dependencies

**depends-on:** api-provider-settings
**blocks:** none

O acoplamento é restrito ao Grupo 4: `api-provider-settings` está IN_PROGRESS e é dono de
`src/services/api_credentials.py`. A promoção de `multimodal_readiness` para `active` só entra
depois que aquele wish fechar o Grupo 2. Os Grupos 1 a 3 não dependem dele.

## Success Criteria

- [ ] Com STT configurado e funcionando, o áudio é transcrito e respondido normalmente, sem regressão.
- [ ] Existe um teste do caminho de áudio **do Z-API** que não faz patch de `_load_clinic_config`
      e ainda assim alcança a transcrição, provando que a chave chega ao handler.
- [ ] Com STT que lança exceção e sem multimodal, o paciente recebe a mensagem de falha
      temporária e a conversa é marcada como não lida; o webhook deixa de responder com skip silencioso.
- [ ] Com STT falhando e multimodal configurado, a transcrição multimodal é tentada e, quando
      bem-sucedida, a conversa segue normalmente sem nenhuma mensagem de recondução.
- [ ] Com STT e multimodal falhando, o paciente recebe a mensagem de recondução por texto.
- [ ] Sem nenhum STT configurado, o paciente recebe a variante "nunca configurado", distinta da
      mensagem de falha temporária.
- [ ] Cada mensagem de recondução é verificada como **entregue**, afirmando sobre a chamada de
      envio, e não apenas selecionada.
- [ ] Cinco áudios seguidos com STT quebrado produzem exatamente uma mensagem de recondução.
- [ ] O handoff de áudio longo (`webhook.py:1082-1088`) volta a enviar a mensagem e marcar a
      conversa como não lida, com teste que entra pelo webhook fim a fim.
- [ ] Os quatro provedores em `UNSUPPORTED_STT_PROVIDERS` continuam saindo com o envelope de skip
      antes de qualquer download, sem disparar a cadeia de fallback.
- [ ] `runtime_projection` continua sem expor `stt_api_key` nem campos `multimodal_*`, e os
      testes de invariante de cache passam sem alteração.
- [ ] Z-API e Meta Cloud API produzem o mesmo comportamento de fallback para o mesmo cenário.
- [ ] As mensagens existem em PT e EN, respeitando `_get_agent_language`.
- [ ] O card multimodal exibe `RECOMENDÁVEL` e `multimodal_readiness` retorna `active` com
      credenciais completas.

## Execution Strategy

### Wave 1 (parallel)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 3: webhook stateful (+2), corrige regressão de trabalho em andamento (+1) | engineer-standard / high | Trocar a fonte de credenciais do handler de áudio Z-API e cobrir o caminho sem patch |
| 2 | engineer | 2: adaptador de provedor de modelo com egress novo (+2) | engineer-standard / medium | Adaptador multimodal de transcrição e políticas de safe-egress |

Grupos 1 e 2 não se tocam: o Grupo 1 mexe no handler do webhook, o Grupo 2 cria uma função de
transporte nova e sua política de egress. Podem correr em paralelo.

### Wave 2 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 4: roteamento de fallback (+2), estado de aviso com TTL (+2) | engineer-complex / high | Cadeia compartilhada Z-API/Meta, mensagens PT/EN e aviso uma vez por conversa |

### Wave 3 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 2: front + back no mesmo grupo (+1), arquivo disputado com wish em andamento (+1) | engineer-standard / medium | Rótulo RECOMENDÁVEL e promoção de `multimodal_readiness` |

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

### Group 1: Credenciais chegam ao handler de áudio Z-API

**Goal:** Fazer o bloco de áudio do Z-API ler credenciais cruas do banco, restaurando a
transcrição e a capacidade de enviar mensagens, sem afrouxar o cache.

**Contexto do defeito.** `_load_clinic_config` (`src/services/event_service.py:398-417`) sempre
devolve a projeção de cache, e o allowlist de `runtime_projection`
(`src/services/api_credentials.py:462-476`) não inclui `stt_api_key`, `zapi_token` nem
`zapi_client_token`. O handler lê dessa projeção em `src/api/routes/webhook.py:1040-1041`, então
`whisper_key` é sempre `None` e todo áudio sai por `no_whisper_key` (`:1154`). O handoff de
áudio longo (`:1082-1088`) morre junto, mas **upstream**, em `if whisper_key:` (`:1062`) — a
linha `:1070` nunca é alcançada. Os testes atuais mascaram tudo isso fazendo patch de
`_load_clinic_config` (`tests/api/test_webhook_egress.py:114-119`).

**Deliverables:**
1. Bloco de áudio Z-API (`webhook.py:1036-1154`) passa a obter credenciais de
   `_load_tenant_credentials(tenant_id, db)` (`webhook.py:642-651`); `db` já está em escopo como
   parâmetro de `_process_zapi_authenticated_body` (`webhook.py:1004-1009`, `db` em `:1008`).
2. Deltas de normalização tratados explicitamente, já que a leitura crua não roda
   `resolve_stt_credentials` (`api_credentials.py:265-289`): (a) o default de `stt_provider` →
   `"outros"` é coberto por `_stt_canonical_provider` (`webhook.py:1042`, definição `:390-400`),
   e o gate de provedor não suportado (`:1048-1049`) deve continuar válido; (b) `audio_max_seconds`
   tem fallback inline em `:1068`; (c) `runtime_projection` só projeta `stt_api_url` e
   `stt_model_name` quando o provider está em `OPERATIONAL_STT_PROVIDERS`
   (`api_credentials.py:477-480`), enquanto a leitura crua sempre os carrega — confirmar que a
   resolução de `stt_base_url` (`webhook.py:1136-1141`) não muda para provedores fora desse conjunto.
3. Teste do caminho de áudio Z-API que **não** faz patch de `_load_clinic_config` e alcança a
   transcrição.
4. Teste de regressão do handoff de áudio longo entrando pelo webhook fim a fim, com áudio acima
   de `audio_max_seconds`, afirmando sobre a chamada de envio e sobre `mark_conversation_unread`.

**Acceptance Criteria:**
- [ ] Existe teste do caminho Z-API sem patch de `_load_clinic_config` que alcança a transcrição.
- [ ] O handoff de áudio longo envia a mensagem e marca a conversa como não lida, provado por
      teste que entra pelo webhook e não injeta credenciais por patch.
- [ ] O allowlist de `runtime_projection` não é alterado e nenhum segredo passa a entrar no cache.
- [ ] Os quatro provedores em `UNSUPPORTED_STT_PROVIDERS` seguem saindo antes de qualquer
      download, sem chamar `safe_download_audio`.
- [ ] O caminho de áudio do Meta permanece inalterado em comportamento.

**Validation:**
```bash
poetry run pytest tests/api/test_webhook_egress.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/services/test_api_credentials.py tests/services/test_event_service_config_cache.py tests/api/test_config_credentials_boundary.py -v
poetry run mypy src/api/routes/webhook.py --ignore-missing-imports
```
Escopo: a mudança está no caminho compartilhado de request do webhook e toca resolução de
credenciais, então rodam as três suítes de canal mais as duas que fixam o invariante de que
segredos não entram no cache, além da suíte de fronteira de credenciais — são exatamente os
testes que reprovariam se o allowlist fosse afrouxado por engano.

**depends-on:** none

---

### Group 2: Adaptador multimodal de transcrição

**Goal:** Transformar bytes de áudio em texto através de um modelo multimodal via
chat completions, com política de egress própria e sem tocar no webhook.

**Deliverables:**
1. `transcribe_audio_multimodal(api_key, audio_bytes, mimetype, model, base_url)` postando em
   `{base}/chat/completions` com blocos de conteúdo `input_audio` no formato OpenAI, que
   `openrouter`, `groq` e `openai` aceitam por serem OpenAI-compatíveis. Segue a forma de
   `transcribe_audio` (`src/services/groq_service.py:39-79`), inclusive `safe_async_client`.
2. Políticas `multimodal_openai`, `multimodal_groq` e `multimodal_openrouter` em
   `PROVIDER_POLICIES` (`src/services/safe_egress.py:328-339`), com host pinado para provedores
   conhecidos e política aberta para custom, espelhando o par `stt_*` / `llm_*`.
3. Testes de unidade com transporte mockado, cobrindo sucesso, erro HTTP e resposta vazia.
4. Testes de egress confirmando que cada `kind` novo resolve e que host fora do allowlist é rejeitado.

**Acceptance Criteria:**
- [ ] `transcribe_audio_multimodal` devolve o texto transcrito para uma resposta bem-formada.
- [ ] Falha de transporte ou resposta inesperada levanta exceção, sem devolver string vazia
      silenciosamente — o degrau seguinte da cadeia depende de distinguir os dois casos.
- [ ] Cada `kind` novo resolve por `resolve_policy` e rejeita host fora do allowlist.
- [ ] Nenhum arquivo do webhook é modificado neste grupo.

**Validation:**
```bash
poetry run pytest tests/services/test_safe_egress.py tests/services/test_multimodal_transcription.py -v
poetry run mypy src/services/groq_service.py src/services/safe_egress.py --ignore-missing-imports
```
Escopo: o grupo adiciona uma função de transporte e entradas de política; os testes focados
dessas duas unidades bastam porque nenhum consumidor é ligado ainda. `safe_egress` entra por ser
fronteira de segurança compartilhada.

**depends-on:** none

---

### Group 3: Cadeia de fallback e recondução por texto

**Goal:** Ligar STT, multimodal e recondução por texto numa cadeia única usada pelos dois canais,
de modo que nenhum áudio termine em silêncio.

**Deliverables:**
1. Helper de cadeia que recebe bytes de áudio e credenciais e devolve `(texto, motivo_da_falha)`,
   tentando STT e, se falhar e houver multimodal configurado, o adaptador do Grupo 2.
2. Chamada do helper nos dois caminhos: Z-API (`webhook.py:1036-1154`) e Meta
   (`webhook.py:2500-2551`), substituindo as saídas `transcription_failed` e `no_whisper_key`.
3. Mensagens de recondução em PT e EN, resolvidas por `_get_agent_language`, com variantes
   distintas para STT nunca configurado e para falha temporária.
4. Aviso entregue uma vez por conversa, via chave Redis com TTL e fallback em memória, seguindo
   o padrão de `_track_sent_message` / `_is_ai_sent_message` (`webhook.py:123-180`).
5. Conversa marcada como não lida após a recondução, sem handoff nem pausar a IA.

**Acceptance Criteria:**
- [ ] STT falha sem multimodal: paciente recebe a mensagem de falha temporária, verificada na
      chamada de envio, e a conversa é marcada como não lida.
- [ ] STT falha com multimodal que funciona: a conversa segue normalmente e nenhuma mensagem de
      recondução é enviada.
- [ ] STT e multimodal falham: paciente recebe a mensagem de recondução.
- [ ] Sem STT configurado: paciente recebe a variante "nunca configurado", textualmente distinta
      da falha temporária.
- [ ] Cinco áudios seguidos com STT quebrado produzem exatamente uma mensagem enviada.
- [ ] Z-API e Meta produzem o mesmo comportamento para o mesmo cenário, provado por teste de paridade.
- [ ] Os quatro provedores não suportados seguem saindo antes de qualquer download.
- [ ] Cada mensagem existe em PT e EN.

**Validation:**
```bash
poetry run pytest tests/api/test_webhook_egress.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_uazapi.py tests/services/test_api_credentials.py tests/services/test_event_service_config_cache.py -v
poetry run mypy src/api/routes/webhook.py src/services/groq_service.py --ignore-missing-imports
```
Escopo: o grupo altera o comportamento de resposta dos dois canais de webhook no caminho
compartilhado de mensagens, então rodam todas as suítes de canal, incluindo uazapi para pegar
regressão cruzada no handler comum, mais as suítes de invariante de credenciais.

**depends-on:** 1, 2

---

### Group 4: RECOMENDÁVEL na UI e readiness do multimodal

**Goal:** Refletir na aba APIs que o multimodal agora tem adaptador de runtime e é recomendado.

**Deliverables:**
1. `multimodal_readiness` passa de `configuration_only` para `active` quando as credenciais
   estiverem completas (`src/services/api_credentials.py:443-445`).
2. Rótulo `RECOMENDÁVEL` no card multimodal em `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`.
3. Strings em `src/locales/pt.ts` e `src/locales/en.ts`.
4. Testes de readiness no back e do card no front.

**Acceptance Criteria:**
- [ ] `multimodal_readiness` retorna `active` com credenciais completas e `incomplete` sem elas.
- [ ] O card multimodal exibe `RECOMENDÁVEL` em PT e o equivalente em EN.
- [ ] `runtime_projection` continua sem projetar campos `multimodal_*`, e os quatro testes de
      invariante seguem passando sem alteração — readiness e projeção são coisas distintas.
- [ ] `tts_readiness` permanece `configuration_only`.

**Validation:**
```bash
poetry run pytest tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/services/test_event_service_config_cache.py tests/api/test_config.py -v
poetry run mypy src/services/api_credentials.py --ignore-missing-imports
cd frontend/artifacts/neurostack && pnpm typecheck && pnpm test && pnpm build
```
Escopo: o grupo cruza back e front e altera um contrato de status consumido pela UI, então roda o
gate completo do pacote de front (typecheck, testes, build) mais as suítes de credenciais que
protegem o invariante de projeção que este grupo chega perto de tocar.

**depends-on:** 2, api-provider-settings

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Enviar um áudio normal por WhatsApp no Z-API e receber resposta da IA — prova que o Grupo 1
      restaurou o caminho que está morto hoje.
- [ ] Enviar um áudio com a chave de STT inválida e receber a mensagem pedindo para digitar,
      seguida de atendimento normal por texto.
- [ ] Enviar três áudios seguidos com STT quebrado e conferir que só uma mensagem de recondução chegou.
- [ ] Enviar um áudio longo, acima do limite configurado, e conferir que a mensagem de
      encaminhamento chega e a conversa aparece como não lida no painel da clínica.
- [ ] Repetir o cenário de falha de STT no canal Meta e obter o mesmo comportamento do Z-API.
- [ ] Conferir na aba APIs que o card multimodal mostra `RECOMENDÁVEL` e fica `Ativo` com
      credenciais completas.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| A chave de STT e os tokens de envio não chegam ao handler Z-API; todo áudio já sai por `no_whisper_key` e nenhuma mensagem seria entregue | High | Grupo 1, com testes que não fazem patch de `_load_clinic_config` |
| O handoff de áudio longo morre upstream em `if whisper_key:` (`webhook.py:1062`), não no gate de token; corrigir só os tokens daria falsa confiança | Medium | Critério de regressão exige teste entrando pelo webhook fim a fim, não asserção sobre o gate isolado |
| A leitura crua não aplica os defaults de `resolve_stt_credentials`, então a troca não é inerte | Medium | Deliverable 2 do Grupo 1 enumera os três deltas e exige confirmação de cada um |
| Suporte a `input_audio` em chat completions pode variar entre openrouter, groq e openai | Medium | Verificar por provedor no Grupo 2; o degrau de texto absorve a falha, então o pior caso é o comportamento que já teríamos sem multimodal |
| Áudio em base64 dentro de chat completions incha o payload e pode estourar limite de token ou timeout | Low | O download já é limitado por `audio_max_seconds` e teto de 10 MB (`webhook.py:1100-1103`); o corte de áudio longo acontece antes |
| Conflito de merge com `api-provider-settings`, IN_PROGRESS e dono de `api_credentials.py` | Medium | Isolado no Grupo 4, declarado como `depends-on`; Grupos 1 a 3 não tocam o arquivo |
| Redis indisponível faz o aviso repetir | Low | Fallback em memória cobre o worker; o pior caso é um aviso repetido, nunca um áudio silencioso |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

---

## Files to Create/Modify

```
src/api/routes/webhook.py                                   # Grupos 1 e 3
src/services/groq_service.py                                # Grupo 2 (adaptador multimodal)
src/services/safe_egress.py                                 # Grupo 2 (políticas multimodal_*)
src/services/api_credentials.py                             # Grupo 4 (multimodal_readiness)
frontend/artifacts/neurostack/src/pages/clinic-config.tsx   # Grupo 4
frontend/artifacts/neurostack/src/locales/pt.ts             # Grupo 4
frontend/artifacts/neurostack/src/locales/en.ts             # Grupo 4
tests/api/test_webhook_egress.py                            # Grupos 1 e 3
tests/api/test_webhook_zapi.py                              # Grupos 1 e 3
tests/api/test_webhook_meta.py                              # Grupo 3 (paridade)
tests/services/test_multimodal_transcription.py             # Grupo 2 (novo)
tests/services/test_safe_egress.py                          # Grupo 2
tests/services/test_api_credentials.py                      # Grupo 4
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx  # Grupo 4
```
