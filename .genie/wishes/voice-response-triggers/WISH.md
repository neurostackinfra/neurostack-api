# Wish: Controle e Gatilhos de Resposta por Voz

| Field | Value |
|-------|-------|
| **Status** | APPROVED |
| **Slug** | `voice-response-triggers` |
| **Date** | 2026-08-29 |
| **Author** | Codex |
| **Appetite** | medium |
| **Branch** | `wish/voice-response-triggers` |
| **Base** | descendente de `6339d7ac5645b7f29b712cdd22b54c0c1f39250c`, que já contém `api-config-clarity` integrada |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/voice-response-triggers/DESIGN.md) |

## Summary

Separar a configuração TTS de seu uso efetivo: a clínica mantém provedor, chave, modelo e voz salvos, mas pode pausar respostas em áudio sem perder nada. Quando o controle mestre estiver ativo, uma política exclusiva decide se o agente responde em áudio somente após áudio recebido ou após qualquer tipo de entrada já suportado pelo canal, com o primeiro modo como default de menor custo.

## Problem

A clínica hoje consegue ligar ou desligar TTS, mas, quando liga, todas as respostas normais do agente viram áudio; falta um gatilho persistido e explícito que limite o uso sem apagar a configuração nem cobrar síntese em respostas não autorizadas.

## Scope

### IN

- Manter `tts_enabled` como controle mestre independente das credenciais TTS.
- Adicionar `tts_reply_policy` estrito e não anulável com valores `audio_input` e `all_inputs`, usando `audio_input` quando o campo estiver omitido/ausente.
- Expor, persistir e validar a política no contrato de credenciais da clínica sem exigir nova API Key para trocar somente a política.
- Aplicar a política às `suggested_response` de Z-API, Evolution, Uazapi e Meta Cloud.
- Capturar áudio original antes da transcrição em Z-API, Uazapi e Meta; manter Evolution text-only com `input_was_audio=false`.
- Manter respostas em texto quando o mestre estiver desligado, a política negar áudio ou síntese/entrega falhar.
- Mostrar no painel o switch mestre, as duas opções exclusivas, o default carregado e “Em breve mais gatilhos de voz”, em PT e EN.
- Testar contrato, preservação de segredo/configuração, decisão compartilhada, integração dos quatro canais e painel.

### OUT

- Suporte inbound de áudio ou STT para Evolution.
- Converter avisos operacionais em áudio: privacidade, fallback STT, áudio longo, revisão de arquivo ou handoff.
- Gatilhos estratégicos, comerciais, por intenção, horário, orçamento, segmento ou consumo.
- Motor genérico/dinâmico de regras, prioridade entre gatilhos, limites de gasto, filas, contadores ou telemetria nova.
- Novos provedores TTS, novos canais ou migração relacional/backfill.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Reusar `tts_enabled` como controle mestre | O campo já governa o runtime; desligar deve impedir TTS sem apagar o bundle configurado. |
| 2 | Persistir um enum `tts_reply_policy`, não dois booleanos | As opções são exclusivas; um campo impede ambos ligados, ambos desligados e regras de prioridade. |
| 3 | Resolver campo ausente para `audio_input` no GET e no runtime | É o default confirmado e reduz custo sem exigir backfill do JSON existente. |
| 4 | `all_inputs` significa todo tipo que o canal já suporta e que produz `suggested_response` | Não cria classificação linguística de pergunta nem amplia capacidades inbound. |
| 5 | Passar `input_was_audio` explicitamente ao helper comum | Preserva a origem antes que STT transforme áudio em texto e mantém uma única decisão de egress. |
| 6 | Evolution passa sempre `false` neste recorte | O canal descarta não-texto antes do pipeline; adicionar áudio/STT seria outra feature. |
| 7 | Avisos operacionais continuam fora do helper | Não são respostas normais do agente e precisam manter cópia/formato determinísticos sem custo TTS. |
| 8 | Trocar política não exige chave fresca | `_require_fresh_keys` deve continuar reagindo apenas a mudança de provedor/URL; política não altera destino nem segredo. |
| 9 | A UI usa seleção exclusiva com semântica acessível | O controle visual deve corresponder ao enum e nunca produzir estados contraditórios. |
| 10 | O PATCH permite omissão, mas rejeita `null` | `Literal["audio_input", "all_inputs"]` com default de modelo permite PATCH parcial via `exclude_unset` sem criar um terceiro estado persistido. |
| 11 | `api-config-clarity` é baseline, não dependência semântica | Suas mudanças já estão integradas no commit-base; eventual diff posterior na mesma tela exige rebase normal, não bloqueia contrato/runtime. |
| 12 | Edição exclusiva da política usa PATCH mínimo | Um dirty flag próprio evita que selecionar um gatilho persista defaults ou reenvie configuração TTS não editada; se o bundle também for editado, o fluxo existente envia o bundle junto. |

## Simplicity Case

- **Simplest complete design:** o switch mestre existente, um enum persistido com duas opções e um booleano de origem passado às quatro chamadas do helper TTS.
- **Added machinery:** somente `tts_reply_policy`, seu estado/dirty flag de formulário e `input_was_audio`; cada item corresponde diretamente a um comportamento confirmado, preservação do bundle ou paridade entre canais.
- **Deferred until measured:** catálogo/motor de gatilhos somente quando um terceiro comportamento for aprovado; orçamento/telemetria somente quando houver limite ou alerta mensurável solicitado.
- **Complexity removed:** nenhum par de switches independentes no wire, prioridade de regras, estado “nenhum gatilho”, migração, cache, fila, contador ou classificação de intenção.

## Dependencies

**depends-on:** none
**blocks:** none

O branch atual já contém a implementação de `api-config-clarity`; o SHA completo acima fixa a árvore mínima observada durante o planejamento. Antes de executar, confirmar que esse commit é ancestral da base escolhida e resolver normalmente qualquer mudança subsequente na mesma seção TTS.

## Success Criteria

- [ ] `tts_enabled=false` sempre envia texto sem chamar síntese, independentemente da política, e preserva todo o bundle TTS salvo.
- [ ] `tts_enabled=true` + `audio_input` sintetiza apenas `suggested_response` originada de áudio em Z-API, Uazapi e Meta; texto nos quatro canais não chama TTS.
- [ ] `tts_enabled=true` + `all_inputs` sintetiza `suggested_response` originada de texto nos quatro canais e de áudio nos três canais que já o processam.
- [ ] Evolution continua text-only: passa `input_was_audio=false` e mantém áudio recebido como `non-text`.
- [ ] Campo omitido/ausente/legado retorna e executa como `audio_input`; `null` e qualquer valor fora do enum recebem 422 atômico pelo schema estrito.
- [ ] Alterar somente `tts_reply_policy` envia PATCH mínimo sem outro campo `tts_*`, não exige reenviar segredo e não muda provedor, URL, modelo ou voz.
- [ ] Falha de síntese ou entrega de áudio preserva o fallback da resposta original em texto.
- [ ] Avisos de privacidade, fallback STT, áudio longo, arquivo e handoff continuam em texto e não chamam TTS.
- [ ] O painel mostra “Gatilhos de Voz” / “Voice Triggers”, as duas opções exclusivas e “Em breve mais gatilhos de voz” / “More voice triggers coming soon”.
- [ ] Salvar e recarregar preserva mestre, política e configuração; `audio_input` aparece selecionado para status legado sem o campo.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — estado persistido em contrato estrito (+2) | engineer-standard / high | Adicionar e testar o wire contract/default não anulável de `tts_reply_policy`. |

### Wave 2 (parallel after Group 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 4 — routing multicanal (+2), comportamento dependente de estado (+2) | engineer-complex / high | Aplicar a decisão compartilhada às respostas normais dos quatro webhooks. |
| 3 | engineer | 2 — formulário persistido e estados carregamento/save (+2) | engineer-standard / high | Expor switch mestre e seleção exclusiva dos gatilhos no painel. |

Complexity scoring follows the repository Genie rubric. Group 1 must land first because Groups 2 and 3 consume the new wire value/default; after that, runtime and painel editam superfícies distintas e podem avançar em paralelo.

## Execution Groups

### Group 1: Contrato persistido da política de voz

**Goal:** Tornar `tts_reply_policy` um campo estrito, persistido e compatível no wire/armazenamento do bundle TTS, reconhecendo a mudança comportamental deliberada do default.

**Deliverables:**
1. Incluir `tts_reply_policy` em `TTS_FIELDS` e no schema PATCH como `Literal["audio_input", "all_inputs"] = "audio_input"`: omissão continua fora de `model_fields_set`/`exclude_unset`, enquanto JSON `null` é inválido.
2. Fazer `credential_status` expor `audio_input` quando o JSON não tiver valor, preservando o valor explicitamente salvo.
3. Manter `runtime_projection` sem campos TTS e manter a regra de chave fresca limitada a mudanças de provedor/URL.
4. Cobrir merge/status, PATCH/GET, 422 atômico de `null`/valor inválido, default legado e troca de política com segredo mascarado/salvo.

**Acceptance Criteria:**
- [ ] PATCH omitindo o campo preserva o bundle; PATCH com `null` ou valor fora do enum retorna 422 sem alteração parcial.
- [ ] GET/PATCH devolvem a política persistida e devolvem `audio_input` para credencial vazia ou legada sem o campo.
- [ ] Trocar somente política preserva `tts_api_key` existente e todos os demais campos TTS.
- [ ] `tts_reply_policy` não entra no cache público de runtime e nenhum segredo aparece na resposta/log de teste.
- [ ] Readiness TTS continua baseada na completude do provedor, sem confundir política ou mestre com credenciais completas.

**Validation:**
```bash
poetry run pytest tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py
make quality
```

O primeiro comando isola o contrato modificado; `make quality` é obrigatório porque o grupo altera schema público e configuração compartilhada, alcançando formatação, lint, tipos e a suíte completa.
Se o gate agregado continuar vermelho por baseline preexistente, capturar comando e saída exatos e manter o grupo não concluído: baseline não é waiver. O trabalho só avança após o gate ficar verde ou uma correção upstream ser formalizada e autorizada separadamente.

**depends-on:** none

---

### Group 2: Gate TTS por origem nos quatro canais

**Goal:** Sintetizar apenas quando mestre e política autorizarem, preservando origem e fallback nos caminhos normais de resposta.

**Deliverables:**
1. Estender `_send_reply_with_optional_tts` com `input_was_audio` e resolver política ausente como `audio_input` antes de qualquer síntese.
2. Cobrir a matriz mestre × política × origem, inclusive “política negada não chama TTS” e os fallbacks atuais de síntese/entrega.
3. Capturar origem pré-transcrição pelos predicados exatos: Z-API `patient_audio_input = bool(audio_obj) and not original_message_text and not is_from_me`; Uazapi `audio_is_transcribable`; Meta `msg_type in _META_AUDIO_TYPES`; Evolution sempre `false`.
4. Adicionar testes de integração focados por canal para `audio_input` e `all_inputs`, mantendo os gates `non-text` e avisos operacionais existentes.

**Acceptance Criteria:**
- [ ] Mestre desligado e política negada retornam pelo `send_text` sem chamar `synthesize_speech` nem `send_audio`.
- [ ] `audio_input` com origem áudio usa TTS; `all_inputs` usa TTS para origem áudio ou não-áudio.
- [ ] Origem permanece áudio depois de STT em Z-API, Uazapi e Meta.
- [ ] Z-API não classifica payload misto (`text` + `audio`) nem áudio malformado/não transcrito como pergunta de áudio autorizada.
- [ ] Texto em Z-API, Evolution, Uazapi e Meta respeita `audio_input` (texto) e `all_inputs` (áudio).
- [ ] Evolution não ganha download/transcrição e continua retornando `non-text` para áudio recebido.
- [ ] Falhas TTS continuam fazendo exatamente um fallback para o texto original.
- [ ] Avisos operacionais citados em Scope OUT permanecem em seus senders de texto e não atravessam o helper.

**Validation:**
```bash
poetry run pytest tests/api/test_webhook_tts.py tests/api/test_webhook_zapi.py tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py
make quality
```

A seleção focada cobre o helper, os quatro adaptadores inbound/egress e os gates de mídia; `make quality` preserva o gate agregado porque `webhook.py` é runtime compartilhado e hot path de todos os canais.
Qualquer falha do gate completo deve ser registrada com saída exata e mantém o grupo aberto; falha preexistente de Black ou outro check exige correção upstream autorizada, nunca dispensa local.

**depends-on:** Group 1

---

### Group 3: Controles de geração e gatilho no painel

**Goal:** Permitir que o builder pause voz e selecione uma das duas políticas sem perder configuração TTS.

**Deliverables:**
1. Adicionar tipo/estado `TtsReplyPolicy` e um dirty flag exclusivo, carregar `audio_input` para status ausente e enviar `tts_reply_policy` após edição explícita da seleção.
2. Preservar o `SwitchRow` mestre “Responder em áudio” como controle independente do provedor e da seleção de política.
3. Renderizar “Gatilhos de Voz” / “Voice Triggers” com seleção exclusiva acessível para “apenas quando receber áudio” e “todos os tipos de pergunta”.
4. Renderizar “Em breve mais gatilhos de voz” / “More voice triggers coming soon” e manter estados read-only, loading e save existentes.
5. Testar carregamento legado, exclusividade, PATCH mínimo do `handleSave`, combinação com edição real do bundle, reload, desligamento sem perda e cópia PT/EN com chave previamente salva.

**Acceptance Criteria:**
- [ ] O controle mestre continua visível e seu off não limpa nem desabilita a edição das credenciais salvas.
- [ ] Exatamente uma política fica selecionada; `audio_input` é exibida para status sem valor.
- [ ] Selecionar `all_inputs` envia o enum correto, salva e reaparece após reload.
- [ ] Alterar a política com chave já salva não exige digitar uma chave nova nem envia a máscara como segredo.
- [ ] Ao trocar somente política, o payload real contém `tts_reply_policy` e nenhum outro campo `tts_*`, tanto com mestre ligado quanto desligado.
- [ ] Quando política e bundle são editados na mesma sessão, o payload inclui a política e os campos do bundle pelo fluxo TTS existente, sem máscara de segredo.
- [ ] PT e EN exibem título, duas descrições e mensagem “em breve” aprovados.
- [ ] Usuário read-only vê o estado, mas não altera mestre, política ou credenciais.
- [ ] As opções expõem `radiogroup`/`radio` ou semântica equivalente, anunciam seleção, aceitam navegação por teclado e ficam bloqueadas em read-only.
- [ ] Para status legado/ausente, comunicar o default significa renderizar `audio_input` selecionado e anunciado pelo controle; não há badge ou cópia adicional implícita.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack test -- src/pages/clinic-config-api-settings.test.tsx
pnpm --dir frontend/artifacts/neurostack typecheck
pnpm --dir frontend/artifacts/neurostack build
```

O teste cobre o formulário e payload diretamente; typecheck e build validam o boundary React/Vite alcançado sem executar pacotes frontend não relacionados.

**depends-on:** Group 1

---

## Aggregate Integration Gate

Depois dos três grupos, executar o gate completo de backend e o pacote frontend afetado:

```bash
make quality
pnpm --dir frontend/artifacts/neurostack test
pnpm --dir frontend/artifacts/neurostack typecheck
pnpm --dir frontend/artifacts/neurostack build
```

Esse gate separado é proporcional à combinação de schema público, estado JSON, hot path multicanal e painel de configuração; ele não substitui as validações focadas de cada grupo.

Nenhuma falha desse gate pode ser tratada como baseline dispensável. Registrar o comando e a saída exatos e bloquear conclusão até o repositório ficar verde ou uma correção upstream, com dono e escopo próprios, ser autorizada e integrada.

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Clínica TTS configurada pode desligar voz, salvar/recarregar e continuar com provedor, URL, chave, modelo e voz intactos.
- [ ] Clínica legada com `tts_enabled=true`, sem política e com pergunta em texto recebe texto sem chamada TTS; o painel mostra `audio_input` selecionado/anunciado e permite optar por `all_inputs`.
- [ ] No modo `audio_input`, texto do paciente recebe texto; áudio do paciente recebe áudio em Z-API, Uazapi e Meta.
- [ ] No modo `all_inputs`, texto recebe áudio nos quatro canais e áudio recebe áudio nos três canais com inbound de áudio.
- [ ] Falha real/simulada do provedor TTS entrega o mesmo texto original uma única vez.
- [ ] Evolution continua rejeitando áudio recebido como `non-text`, sem download ou gasto STT/TTS.
- [ ] Avisos de privacidade, fallback STT, áudio longo, arquivo e handoff continuam em texto.
- [ ] Builder alterna idioma PT/EN e vê a seleção exclusiva e a mensagem “em breve” corretas em desktop e mobile.
- [ ] Super admin selecionando clínica e usuário read-only observam a mesma política persistida, respeitando permissões existentes.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Predicados de áudio divergem entre Z-API, Uazapi e Meta | High | Capturar o booleano onde cada webhook já valida/transcreve áudio e testar o envelope real de cada canal. |
| Default novo reduz o alcance de clínicas com TTS ativo e campo ausente | Medium | Decisão deliberada do dono; expor `audio_input` no GET/UI e exigir seleção explícita do modo amplo. |
| Mudança de política no painel pode reenviar defaults do bundle TTS | Medium | Reusar preservação de segredo e testar que bundle salvo não muda; não enviar máscara nem exigir chave fresca. |
| Resposta operacional pode ser sintetizada acidentalmente em refactor futuro | Medium | Testes negativos mantêm os senders diretos fora do helper e Scope OUT documenta o limite. |
| Mudanças posteriores de `api-config-clarity` podem tocar a mesma tela | Medium | Confirmar que `6339d7ac5645b7f29b712cdd22b54c0c1f39250c` é ancestral e fazer rebase normal antes do Group 3; não há dependência semântica. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### 2026-08-29T21:14:01Z — Plan Review

- **Verdict:** BLOCKED (`ambiguous dependency/order`)
- **Reviewer:** `/root/review_voice_design`
- **Target SHA-256:** `6d20ef04a106c1b2fbe1c2b2ed60b842e825b106b03b4395b473a6da511c0ee4`
- **Design digest:** `d17d0230efeeaf039b62469e8be005c4df9a1208b6a00ddd5c07acb97c3a4c64` (verified)
- **Evidence:** estrutura, três grupos/validações, comandos e `git diff --cached --check` passaram; implementação/testes não foram executados nesta revisão de plano.
- **Blocking gaps:** dependência `api-config-clarity` ainda `IN_PROGRESS`, semântica divergente entre dependência global e waves, e ausência de rota explícita para o baseline vermelho de `make quality`.
- **Resolution in next draft:** dependência removida em favor de commit-base já integrado; waves agora têm somente dependência interna; falha do gate agregado bloqueia e exige saída exata/correção upstream formalizada.

### 2026-08-29 — Questioner Advisory

- **Lens:** `/root/question_voice_plan`
- **Target SHA-256:** `6d20ef04a106c1b2fbe1c2b2ed60b842e825b106b03b4395b473a6da511c0ee4`
- **Applied findings:** rejeitar `null` sem perder PATCH parcial, nomear predicados de áudio por canal, testar mudança comportamental legada e payload UI completo, exigir semântica acessível concreta e distinguir baseline integrado de dependência lifecycle.

### 2026-08-29T21:19:17Z — Plan Re-review

- **Verdict:** SHIP
- **Reviewer:** `/root/review_voice_design`
- **Target SHA-256:** `ea41a28750c24837063a578bacb245c0c4ee8ecadabddbc57ebfd521067619e0`
- **Design digest:** `d17d0230efeeaf039b62469e8be005c4df9a1208b6a00ddd5c07acb97c3a4c64` (verified)
- **Evidence:** estrutura, blobs staged/working tree, ancestralidade de `6339d7a`, arquivos e comandos planejados verificados; nenhum gap oficial.
- **Post-review advisory refinement:** a lente Questioner pediu PATCH mínimo para edição exclusiva de política, interpretação testável da comunicação do default e SHA-base completo. O conteúdo foi refinado depois deste digest; por isso uma revisão final é obrigatória antes de `APPROVED`.

### 2026-08-29T21:23:17Z — Final Plan Review

- **Verdict:** SHIP
- **Reviewer:** `/root/review_voice_design`
- **Target SHA-256:** `5204e8fe449feefc59636a824a6edee580bd0350c518890e4068117b9e198a51`
- **Staged blob:** `fb41d08b4ec7dd5df1aabaa802f88b649993fd56`
- **Design digest:** `d17d0230efeeaf039b62469e8be005c4df9a1208b6a00ddd5c07acb97c3a4c64` (verified)
- **Base:** `6339d7ac5645b7f29b712cdd22b54c0c1f39250c` (ancestral confirmed)
- **Evidence:** `git diff --cached --check`, estrutura, arquivos, comandos, dependências, dirty flag/PATCH mínimo, rollout legado e acessibilidade verificados; nenhum teste de implementação foi executado nesta revisão read-only.
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW.
- **Advisory:** `/root/question_voice_plan` revisou o mesmo SHA e não encontrou gap remanescente.

---

## Files to Create/Modify

```text
src/services/api_credentials.py
src/api/schemas/config.py
src/api/routes/webhook.py
tests/services/test_api_credentials.py
tests/api/test_api_credentials.py
tests/api/test_config_credentials_boundary.py
tests/api/test_webhook_tts.py
tests/api/test_webhook_zapi.py
tests/api/test_webhook_evolution.py
tests/api/test_webhook_uazapi.py
tests/api/test_webhook_meta.py
tests/api/test_webhook_egress.py
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
```
