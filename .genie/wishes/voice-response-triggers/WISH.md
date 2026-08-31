# Wish: Controle e Gatilhos de Resposta por Voz

| Field | Value |
|-------|-------|
| **Status** | BLOCKED |
| **Slug** | `voice-response-triggers` |
| **Date** | 2026-08-29 |
| **Author** | Codex |
| **Appetite** | medium |
| **Branch** | `wish/voice-response-triggers` |
| **Base** | `29e81ac8f1269b703f5a98c9f5acb765a49b1448`, que integra os commits finais disponíveis de `fallback-llm-multimodal` sobre `origin/main` |
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
- Expor, persistir e validar mestre/política para bundles TTS legados completos, mesmo com o provedor `coming_soon`, sem liberar mutação de provedor, URL, chave, modelo ou voz.
- Aplicar a política às `suggested_response` de Z-API, Evolution, Uazapi e Meta Cloud.
- Capturar áudio original antes da transcrição em Z-API, Uazapi e Meta; manter Evolution text-only com `input_was_audio=false`.
- Manter respostas em texto quando o mestre estiver desligado, a política negar áudio ou síntese/entrega falhar.
- Manter os 19 cards de provedor inertes dentro de `ApiCredentialsForm` e mostrar, em uma seção irmã, o switch mestre, as duas opções exclusivas, o default carregado e “Em breve mais gatilhos de voz”, em PT e EN.
- Testar contrato, preservação de segredo/configuração, decisão compartilhada, integração dos quatro canais e painel.

### OUT

- Suporte inbound de áudio ou STT para Evolution.
- Converter avisos operacionais em áudio: privacidade, fallback STT, áudio longo, revisão de arquivo ou handoff.
- Gatilhos estratégicos, comerciais, por intenção, horário, orçamento, segmento ou consumo.
- Motor genérico/dinâmico de regras, prioridade entre gatilhos, limites de gasto, filas, contadores ou telemetria nova.
- Novos provedores TTS, novos canais ou migração relacional/backfill.
- Reabrir formulário ou edição de provedor, URL, chave, modelo ou voz nos cards `EM BREVE`.
- Tornar a aba Integrações visível para clinic admin; esse papel mantém somente a autorização HTTP preexistente, fora do contrato de UI desta entrega.

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
| 8 | Somente `tts_enabled` e `tts_reply_policy` escapam do bloqueio de disponibilidade, e apenas para bundle legado completo | Esses campos controlam uso/custo sem reconfigurar o provedor. Qualquer payload que altere provedor, URL, chave, modelo ou voz continua recebendo `provider_not_available` de forma atômica. |
| 9 | A UI usa seleção exclusiva com semântica acessível | O controle visual deve corresponder ao enum e nunca produzir estados contraditórios. |
| 10 | O PATCH permite omissão, mas rejeita `null` | `Literal["audio_input", "all_inputs"]` com default de modelo permite PATCH parcial via `exclude_unset` sem criar um terceiro estado persistido. |
| 11 | `fallback-llm-multimodal` é dependência declarada e sua integração fixa a nova base | A wish bloqueadora tinha quatro tarefas concluídas, mas três commits fora da main e sobreposição em schema, serviço, UI e testes. O dono autorizou a integração; os commits mais duas reconciliações de testes pousaram em `29e81ac`, com 40 testes de credenciais e 178 testes fallback/pipeline verdes. Voz deve executar sobre essa base, não sobre o antigo `6339d7a`. |
| 12 | Cada alteração de mestre/política usa PATCH mínimo e imediato | A base `29e81ac` removeu o formulário/save de credenciais. Uma seção separada lê o status e envia apenas `{tts_enabled}` ou `{tts_reply_policy}`, bloqueia durante a requisição e restaura o valor anterior em erro; não existe edição combinada de bundle. |
| 13 | O gate estático bloqueia somente os arquivos Python tocados, enquanto `pytest` continua integral | Em 2026-08-30, `origin/main` falhou antes da feature com 166 arquivos fora do Black e, após Black/isort mecânicos em snapshot isolado, revelou 250 erros Flake8 preexistentes. MyPy também falha antes do trabalho multicanal com 1 erro no contrato e 28 no webhook. O dono autorizou não transformar esta feature em refatoração global: Black, isort e Flake8 devem ficar totalmente verdes nos arquivos afetados, `pytest` continua integral e MyPy fica explicitamente fora deste gate substituto. |
| 14 | `api-provider-availability` mantém domínio sobre os cards; voz adiciona controles fora desse boundary | O teste vigente exige zero controles dentro de `api-settings-section`. `VoiceResponseControls` é uma seção irmã, habilitada somente para `tts_readiness=active`; assim os 19 cards continuam sem fetch, foco, input ou payload, enquanto o requisito explícito do dono controla bundles TTS já salvos. |
| 15 | Investidor read-only recebe prévia estática e zero acesso ao endpoint de credenciais | `isReadOnly` corresponde a investor, papel corretamente recusado por `require_credentials_jwt`. Não ampliar auth evita expor status de segredo/configuração; a prévia comunica a UI sem fingir representar estado persistido. |
| 16 | O editor visual é builder/super admin; clinic admin não ganha navegação nova | A aba `integracoes` é `clinicHidden` na base e o pedido mira o painel builder. O backend continua aceitando clinic admin por contrato existente, mas reabrir toda a aba e seus providers seria outro trabalho. |

## Simplicity Case

- **Simplest complete design:** o switch mestre existente, um enum persistido com duas opções e um booleano de origem passado às quatro chamadas do helper TTS.
- **Added machinery:** somente `tts_reply_policy`, estado local de mestre/política com PATCH imediato e `input_was_audio`; cada item corresponde diretamente a um comportamento confirmado, preservação do bundle ou paridade entre canais.
- **Deferred until measured:** catálogo/motor de gatilhos somente quando um terceiro comportamento for aprovado; orçamento/telemetria somente quando houver limite ou alerta mensurável solicitado.
- **Complexity removed:** nenhum par de switches independentes no wire, prioridade de regras, estado “nenhum gatilho”, migração, cache, fila, contador ou classificação de intenção.

## Dependencies

**depends-on:** fallback-llm-multimodal, api-provider-availability
**blocks:** none

O código sobreposto de `fallback-llm-multimodal` foi integrado em `origin/main` no commit `29e81ac8f1269b703f5a98c9f5acb765a49b1448` após autorização explícita do dono em 2026-08-30. Sua lifecycle wish permanece `IN_PROGRESS` por QA/gate global externo, mas não há mais writer concorrente: as quatro tarefas estão `done` e o override humano de ordenação permite voz prosseguir somente sobre esse SHA integrado. Antes de executar ou revisar qualquer grupo, confirmar que `29e81ac` é ancestral da árvore efetiva, reconciliar os arquivos compartilhados e repetir as validações declaradas; qualquer novo commit do fallback reabre a pausa e a revisão do plano.

`api-provider-availability` também permanece `IN_PROGRESS`, mas seu contrato backend e a interface inerte já são ancestrais de `29e81ac`. A coordenação é por boundary: voz não altera `ProviderCredentialCard` nem adiciona controle/fetch/payload dentro de `ApiCredentialsForm`; cria somente a seção irmã de controles operacionais legados. Antes do Group 3, confirmar que nenhuma tarefa do provider wish está escrevendo `clinic-config.tsx`; qualquer commit novo nessa superfície pausa e exige reconciliação/revisão.

## Success Criteria

- [ ] `tts_enabled=false` sempre envia texto sem chamar síntese, independentemente da política, e preserva todo o bundle TTS salvo.
- [ ] `tts_enabled=true` + `audio_input` sintetiza apenas `suggested_response` originada de áudio em Z-API, Uazapi e Meta; texto nos quatro canais não chama TTS.
- [ ] `tts_enabled=true` + `all_inputs` sintetiza `suggested_response` originada de texto nos quatro canais e de áudio nos três canais que já o processam.
- [ ] Evolution continua text-only: passa `input_was_audio=false` e mantém áudio recebido como `non-text`.
- [ ] Campo omitido/ausente/legado retorna e executa como `audio_input`; `null` e qualquer valor fora do enum recebem 422 atômico pelo schema estrito.
- [ ] Em bundle TTS legado completo, alterar mestre ou política envia PATCH mínimo do único campo, funciona apesar do provider `coming_soon`, não exige segredo e não muda provedor, URL, modelo ou voz.
- [ ] Alterar qualquer campo de configuração TTS, sozinho ou junto de mestre/política, continua retornando `provider_not_available` sem mutação parcial.
- [ ] Falha de síntese ou entrega de áudio preserva o fallback da resposta original em texto.
- [ ] Avisos de privacidade, fallback STT, áudio longo, arquivo e handoff continuam em texto e não chamam TTS.
- [ ] O painel mantém os 19 cards inertes e mostra fora de `ApiCredentialsForm` “Gatilhos de Voz” / “Voice Triggers”, mestre, duas opções exclusivas e “Em breve mais gatilhos de voz” / “More voice triggers coming soon”.
- [ ] Bundles TTS completos podem alternar e recarregar mestre/política; estado incompleto fica visível/desabilitado; investidor read-only recebe prévia estática desabilitada sem GET de credenciais; `audio_input` aparece selecionado para status legado sem o campo.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — estado persistido em contrato estrito (+2) | engineer-standard / high | Adicionar e testar o wire contract/default não anulável de `tts_reply_policy`. |

### Wave 2 (parallel after Group 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 4 — routing multicanal (+2), comportamento dependente de estado (+2) | engineer-complex / high | Aplicar a decisão compartilhada às respostas normais dos quatro webhooks. |
| 3 | engineer | 2 — componente persistido com loading/PATCH/rollback (+2) | engineer-standard / high | Expor seção irmã com switch mestre e seleção exclusiva sem reabrir cards de provider. |

Complexity scoring follows the repository Genie rubric. Group 1 must land first because Groups 2 and 3 consume the new wire value/default; after that, runtime and painel editam superfícies distintas e podem avançar em paralelo.

## Execution Groups

### Group 1: Contrato persistido da política de voz

**Goal:** Tornar `tts_reply_policy` um campo estrito, persistido e compatível no wire/armazenamento do bundle TTS, reconhecendo a mudança comportamental deliberada do default.

**Deliverables:**
1. Incluir `tts_reply_policy` em `TTS_FIELDS` e no schema PATCH como `Literal["audio_input", "all_inputs"] = "audio_input"`: omissão continua fora de `model_fields_set`/`exclude_unset`, enquanto JSON `null` é inválido.
2. Fazer `credential_status` expor `audio_input` quando o JSON não tiver valor, preservando o valor explicitamente salvo.
3. Manter `runtime_projection` sem campos TTS e alterar a disponibilidade para aceitar somente `tts_enabled`/`tts_reply_policy` quando o estado existente tiver `tts_readiness=active`; qualquer campo de configuração continua bloqueado.
4. Cobrir merge/status, PATCH/GET, 422 atômico de `null`/valor inválido, default legado, mestre/política com segredo salvo e payload misto bloqueado integralmente.

**Acceptance Criteria:**
- [ ] PATCH omitindo o campo preserva o bundle; PATCH com `null` ou valor fora do enum retorna 422 sem alteração parcial.
- [ ] GET/PATCH devolvem a política persistida e devolvem `audio_input` para credencial vazia ou legada sem o campo.
- [ ] Bundle legado completo com provider `coming_soon` aceita PATCH isolado de `tts_enabled` ou `tts_reply_policy`, preservando segredo e todos os demais campos.
- [ ] Bundle incompleto não ganha controles efetivos; qualquer alteração de provider/URL/key/model/voice, inclusive junto de mestre/política, recebe `provider_not_available` sem alteração parcial.
- [ ] `tts_reply_policy` não entra no cache público de runtime e nenhum segredo aparece na resposta/log de teste.
- [ ] Readiness TTS continua baseada na completude do provedor, sem confundir política ou mestre com credenciais completas.

**Validation:**
```bash
poetry run pytest tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py
poetry run black --check src/services/api_credentials.py src/api/schemas/config.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py
poetry run isort --check-only src/services/api_credentials.py src/api/schemas/config.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py
poetry run flake8 src/services/api_credentials.py src/api/schemas/config.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py
```

O primeiro comando isola o contrato modificado; os três seguintes garantem formatação, imports e lint sem aceitar regressão nos arquivos tocados. A suíte backend completa permanece no gate agregado porque o grupo altera schema público e configuração compartilhada; a dispensa deliberada de MyPy está limitada ao baseline reproduzido na Decisão 13.

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
poetry run black --check src/api/routes/webhook.py tests/api/test_webhook_tts.py tests/api/test_webhook_zapi.py tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py
poetry run isort --check-only src/api/routes/webhook.py tests/api/test_webhook_tts.py tests/api/test_webhook_zapi.py tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py
poetry run flake8 src/api/routes/webhook.py tests/api/test_webhook_tts.py tests/api/test_webhook_zapi.py tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py
```

A seleção focada cobre o helper, os quatro adaptadores inbound/egress e os gates de mídia; os três checks estáticos impedem regressões nos arquivos alcançados. Como `webhook.py` é runtime compartilhado e hot path de todos os canais, a suíte backend completa permanece obrigatória no gate agregado; MyPy permanece fora somente pela dispensa explícita da Decisão 13.

**depends-on:** Group 1

---

### Group 3: Controles de geração e gatilho no painel

**Goal:** Permitir que o builder pause voz e selecione uma das duas políticas para um bundle TTS legado completo, sem reabrir os cards de provedor.

**Deliverables:**
1. Criar `VoiceResponseControls` como seção irmã de `ApiCredentialsForm`, carregar status via GET tenant-aware e resolver política ausente para `audio_input`.
2. Renderizar o switch mestre “Responder em áudio” e um `radiogroup` exclusivo para “apenas quando receber áudio” e “todos os tipos de pergunta”.
3. Para cada alteração, enviar PATCH imediato contendo somente `tts_enabled` ou `tts_reply_policy`; bloquear controles durante a requisição e restaurar o valor anterior com erro visível se falhar.
4. Habilitar alterações somente quando `tts_readiness=active` para builder/super admin que alcançam a aba; estado incompleto/loading permanece visível e desabilitado com explicação localizada. Clinic admin não entra no contrato de UI e a navegação existente não muda.
5. Renderizar “Em breve mais gatilhos de voz” / “More voice triggers coming soon” em PT/EN sem alterar `ProviderCredentialCard` nem a seção/testes inertes existentes.
6. Para `isReadOnly`/investor, não executar GET/PATCH de credenciais e renderizar somente uma prévia estática desabilitada (mestre off, `audio_input` selecionado) com explicação localizada, sem afirmar estado persistido.
7. Testar GET alvo, default legado, exclusividade/teclado, PATCH mínimo por controle, rollback de erro, reload, readiness, alcance builder/super admin, zero-fetch do investidor e preservação integral do boundary dos 19 cards; confirmar que nenhuma mudança torna `integracoes` visível a clinic admin.

**Acceptance Criteria:**
- [ ] Os 19 cards seguem dentro de `api-settings-section` com zero input/button/select/textarea/tabindex, zero fetch e zero payload; a seção irmã de voz não altera esse contrato.
- [ ] O controle mestre fica visível; desligá-lo envia somente `{tts_enabled:false}` e não limpa nem tenta editar credenciais salvas.
- [ ] Exatamente uma política fica selecionada; `audio_input` é exibida para status sem valor.
- [ ] Selecionar `all_inputs` envia somente `{tts_reply_policy:"all_inputs"}`, salva e reaparece após reload; nenhuma máscara ou configuração TTS entra no payload.
- [ ] PATCH em andamento impede dupla interação; resposta não-ok restaura o valor anterior e exibe erro sem alterar o outro controle.
- [ ] `tts_readiness=active` habilita controles para builder/super admin; `incomplete` e loading os mantêm visíveis e desabilitados; clinic admin não ganha acesso à aba.
- [ ] PT e EN exibem título, duas descrições e mensagem “em breve” aprovados.
- [ ] Investidor/read-only faz zero GET/PATCH de credenciais e vê somente prévia estática desabilitada, sem acesso ou alegação sobre o estado persistido.
- [ ] As opções expõem `radiogroup`/`radio` ou semântica equivalente, anunciam seleção, aceitam navegação por teclado para editores e ficam bloqueadas na prévia read-only.
- [ ] Para status legado/ausente, comunicar o default significa renderizar `audio_input` selecionado e anunciado pelo controle; não há badge ou cópia adicional implícita.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack test -- src/pages/clinic-config-api-settings.test.tsx src/components/api-settings/voice-response-controls.test.tsx
pnpm --dir frontend/artifacts/neurostack typecheck
pnpm --dir frontend/artifacts/neurostack build
```

Os testes cobrem separadamente o boundary inerte dos cards e o GET/PATCH/estado acessível da seção de voz; typecheck e build validam o boundary React/Vite alcançado sem executar pacotes frontend não relacionados.

**depends-on:** Group 1

---

## Aggregate Integration Gate

Depois dos três grupos, executar a suíte backend completa, checks estáticos em todos os arquivos Python tocados e o pacote frontend afetado:

```bash
poetry run pytest
poetry run black --check src/services/api_credentials.py src/api/schemas/config.py src/api/routes/webhook.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py tests/api/test_webhook_tts.py tests/api/test_webhook_zapi.py tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py
poetry run isort --check-only src/services/api_credentials.py src/api/schemas/config.py src/api/routes/webhook.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py tests/api/test_webhook_tts.py tests/api/test_webhook_zapi.py tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py
poetry run flake8 src/services/api_credentials.py src/api/schemas/config.py src/api/routes/webhook.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_config_credentials_boundary.py tests/api/test_webhook_tts.py tests/api/test_webhook_zapi.py tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py
pnpm --dir frontend/artifacts/neurostack test
pnpm --dir frontend/artifacts/neurostack typecheck
pnpm --dir frontend/artifacts/neurostack build
```

Esse gate separado é proporcional à combinação de schema público, estado JSON, hot path multicanal e painel de configuração: toda a suíte backend e o pacote frontend afetado permanecem obrigatórios, e cada arquivo tocado deve ter zero falhas em Black/isort/Flake8. A exceção ao `make quality` global e a omissão de MyPy são explícitas, limitadas aos baselines reproduzidos e autorizadas pelo dono em 2026-08-30; os 250 erros Flake8 e 29 erros MyPy preexistentes nos caminhos observados continuam como débito independente e não podem mascarar regressão coberta pelos gates substitutos. Antes da revisão de execução, comparar o diff real à lista acima: qualquer novo caminho Python pausa a execução até ser incluído nos operandos de Black/isort/Flake8, além de ser adicionado a `Files to Create/Modify` e revalidado no plano.

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
- [ ] Super admin selecionando clínica observa a política persistida do tenant alvo; investidor vê prévia estática sem qualquer requisição de credenciais.
- [ ] Clínica sem bundle TTS completo vê a seção de voz desabilitada e explicativa; os 19 cards continuam `EM BREVE`/`COMING SOON`, inertes e sem controles.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Predicados de áudio divergem entre Z-API, Uazapi e Meta | High | Capturar o booleano onde cada webhook já valida/transcreve áudio e testar o envelope real de cada canal. |
| Default novo reduz o alcance de clínicas com TTS ativo e campo ausente | Medium | Decisão deliberada do dono; expor `audio_input` no GET/UI e exigir seleção explícita do modo amplo. |
| Controle de voz pode reabrir acidentalmente a configuração de providers `coming_soon` | High | Seção irmã separada; PATCH de um único campo; teste que os 19 cards continuam inertes e que qualquer mutação de provider/URL/key/model/voice recebe 422 atômico. |
| Resposta operacional pode ser sintetizada acidentalmente em refactor futuro | Medium | Testes negativos mantêm os senders diretos fora do helper e Scope OUT documenta o limite. |
| Mudanças posteriores de `fallback-llm-multimodal` podem tocar schema, serviço, testes ou a mesma tela | High | Exigir `29e81ac8f1269b703f5a98c9f5acb765a49b1448` como ancestral, reconciliar todos os caminhos compartilhados e pausar/revisar novamente se o fallback ganhar outro commit. |
| `api-provider-availability` ainda possui tarefas abertas sobre a mesma tela | High | Manter `ApiCredentialsForm`/cards intactos, criar seção irmã, confirmar ausência de writer em `clinic-config.tsx` antes do Group 3 e pausar para qualquer commit concorrente. |
| O gate global está vermelho fora do escopo | High | Rodar `pytest` completo; exigir Black/isort/Flake8 verdes em todos os arquivos Python tocados; auditar operandos contra o diff; registrar a dispensa de MyPy e manter evidência dos baselines de 166 arquivos/250 erros Flake8/29 erros MyPy observados para trabalho próprio. |

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

### 2026-08-30T20:57:30Z — Amended Final Plan Review

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Target SHA-256:** `cfb6aa137f67b7a21edce4714159c3310e6fbf0c88aff5a7090b491a5184b497`
- **Design digest:** `d17d0230efeeaf039b62469e8be005c4df9a1208b6a00ddd5c07acb97c3a4c64` (verified)
- **Base:** `29e81ac8f1269b703f5a98c9f5acb765a49b1448` (`origin/main` verified)
- **Evidence:** escopo e UI coerentes para builder/super admin; investor sem GET/PATCH; exceção de disponibilidade TTS restrita a bundles legados completos; quatro canais, três grupos, 16 caminhos e 12 operandos Python verificados; `git diff --check` do working tree e índice passou.
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW.
- **Execution guard:** o checkout compartilhado está sujo; respeitar a reconciliação e a ausência de writer concorrente antes de cada grupo sobreposto.

### 2026-08-30T21:13:27Z — Group 1 Execution Review (Loop 0)

- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/voice_plan_rereview`
- **Task:** `t_mtev1o0t9b103e6b`
- **Base:** `29e81ac8f1269b703f5a98c9f5acb765a49b1448`
- **WISH SHA-256:** `0ef881b1263a249dd65814512dee2abbc791962f886a4c4cd39d2f351819994a`
- **Five-file diff SHA-256:** `34a879b9c7e5ddec6f1538329aa512920413c6ab73d13591a3a46eaf0d9310df` (`+405/-83`, `git diff --check` verde)
- **[HIGH]:** mutações de URL/chave/modelo/voz podiam passar sem provedor salvo, e um provedor legado desconhecido podia satisfazer o predicado de completude.
- **Fix route:** loop 1/2 do `genie:fix`; falhar fechado sem provedor válido, exigir provedor TTS conhecido no bypass e cobrir serviço/API com atomicidade e não reflexão de segredo.
- **Sound evidence:** enum/null estritos, default legado, round-trip, bypass de bundle conhecido completo, bloqueio de limpeza de provider, preservação/máscara de segredo, exclusão do runtime projection e boundary genérico foram aprovados.

### 2026-08-30T21:23:05Z — Group 1 Execution Re-review (Fix Loop 1/2)

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Task:** `t_mtev1o0t9b103e6b`
- **Base:** `29e81ac8f1269b703f5a98c9f5acb765a49b1448`
- **WISH SHA-256:** `2fb1aff495ce9722609c2429de220088b73858ee8e77004b2b8772567d15c557`
- **Five-file diff SHA-256:** `75e4ed3b36f21f592189ba8abdace665027f73cee33218a48054502c49d6925a` (`+547/-83`, `git diff --check` verde)
- **Evidence:** provedor ausente/desconhecido/removido falha fechado; bypass permanece restrito a bundle armazenado completo e conhecido; testes de serviço e HTTP provam 422 atômico, zero flush/commit/mutação e nenhuma reflexão de segredo; caminho válido preserva configuração e segredo.
- **Validation:** fixer executou 33 testes delta e 149 focados; orchestrator reproduziu os 33 testes delta e Black/isort/Flake8 nos cinco arquivos, todos verdes. MyPy permaneceu dispensado conforme decisão aprovada.
- **Gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW.

### 2026-08-30T21:46:51Z — Group 2 Execution Review

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Task:** `t_mtev1o19fa52321c`
- **Base:** `29e81ac8f1269b703f5a98c9f5acb765a49b1448`
- **WISH SHA-256:** `57cbb8a25af9cdf568185facf06c50ef97a15c54a32e8836c13f3940c1d5e6bb`
- **Seven-file diff SHA-256:** `06344548ef704b5a9c52c584a0f931515882f5a6b1589de6484b16bb0c060484` (`+570/-49`, `git diff --check` verde)
- **Evidence:** default `audio_input`, matriz mestre/política/origem, quatro predicados exatos, fallback textual único, Evolution sem STT, avisos operacionais fora do helper, regressão Uazapi preservada e hot path sem I/O/estado novos foram verificados.
- **Validation:** engineer executou 183 testes focados (`4 warnings`) e Black/isort/Flake8; orchestrator reproduziu os 15 testes da matriz do helper. Tudo verde.
- **Residual [MEDIUM]:** os testes por canal mockam o helper e provam propagação da origem, enquanto a matriz roda no helper; não há matriz end-to-end duplicada para ambas as políticas em cada adaptador, nem caso separado para o alias Meta `voice`. Aceito como não bloqueante pelo reviewer porque composição e call sites satisfazem os critérios atuais.
- **Audit guard [MEDIUM]:** preservar exatamente os hashes do diff revisado ao montar commit/PR sobre a base; o checkout atual não é descendente da base.

### 2026-08-31T00:12:23Z — Group 3 Execution Review on Approved Base

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Task:** `t_mtev1o1qf6bbe8f2`
- **Base:** worktree isolado em `29e81ac8f1269b703f5a98c9f5acb765a49b1448`
- **WISH SHA-256:** `3ce8e77c612d17a9a283e932091bb28d3af4ecddc13360f538e97eb9c4fc0919`
- **Four-path patch SHA-256:** `f95a4572a4ae5600acd8b28a9e1aa33d4e436869554bff94f5202e45d5ccd6c8` (`+587/-2`)
- **Evidence:** 19 cards permanecem inertes; seção irmã, GET tenant-aware, default conservador, readiness/loading, switch/radiogroup acessíveis, PATCH imediato de campo único, lock/rollback/erro, PT/EN, investor sem requests e clinic admin sem Integrações foram verificados; mudanças anteriores da base foram preservadas.
- **Validation:** 27 testes focados, 80 testes frontend completos, TypeScript, build de produção e diff-check verdes no worktree isolado.
- **Residual [MEDIUM]:** PATCH em voo não tem guard de geração/tenant; uma troca de clínica pode exibir temporariamente sucesso/rollback atrasado do tenant anterior, embora a mutação do servidor permaneça endereçada ao tenant correto.
- **Audit guard [MEDIUM]:** preservar os quatro hashes revisados ao integrar com os arquivos backend, que foram excluídos deste parecer.

### 2026-08-31 — Group 1 Integration Re-review on Approved Base

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Base:** worktree isolado em `29e81ac8f1269b703f5a98c9f5acb765a49b1448`
- **Five-path patch SHA-256:** `4cf1e67aad0ee65d11262dc16154eb787cb32bdf14b521425e2875dd0c910545` (`+445/-19`)
- **Evidence:** contrato estrito, exclusão do runtime, bypass somente para bundle pré-existente conhecido/completo, falha fechada e atomicidade/segredo foram revalidados; contrato multimodal da base foi preservado.
- **Validation:** 117 testes focados, 35 testes TTS selecionados e Black/isort/Flake8/diff-check verdes.
- **Gaps/residual:** nenhum.

### 2026-08-31 — Group 2 Integration Re-review on Approved Base

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Base:** worktree isolado em `29e81ac8f1269b703f5a98c9f5acb765a49b1448`
- **Seven-path patch SHA-256:** `b44c623a689e8b2c059cb3ee809fff55eead016436a3e9603b860626a1ed0144` (`+575/-49`)
- **Evidence:** helper, matriz, quatro origens, fallback único, Evolution sem STT e avisos operacionais foram revalidados; bloco de fila Uazapi permaneceu idêntico à base e testes novos não dependem do fixture externo.
- **Validation:** 29 testes de feature, 183 testes focados e Black/isort/Flake8/diff-check verdes.
- **Gaps/residual:** nenhum de produto; fixture histórico Uazapi continua pré-requisito externo dos testes antigos da base.

### 2026-08-31 — Aggregate Execution Review

- **Verdict:** BLOCKED
- **Reviewer:** `/root/voice_plan_rereview`
- **Base:** `29e81ac8f1269b703f5a98c9f5acb765a49b1448`
- **Aggregate scope:** exatamente 16 caminhos planejados, `+1607/-70`
- **Content manifest SHA-256:** `53ef2b713d4bd1354291621d5ae9909746c1852d07c6e4cf6763fc65c772bf87`
- **Feature evidence:** três grupos em SHIP; 117 testes de contrato, 183 de runtime, 27 frontend focados, 80 frontend completos, TypeScript, build e Black/isort/Flake8 nos 12 caminhos Python verdes.
- **[HIGH — blocking]:** `pytest` completo coletou 2.431 testes, mas falhou/travou no baseline: sessão espera 201 e recebe 404; contador de rate-limit permanece 109; teste de observabilidade trava por mais de 180s; nós async apresentam contaminação dependente de ordem.
- **Attribution:** base limpa reproduziu os dois failures determinísticos e o hang; feature não demonstrou regressão nesses pontos. Nós async passam isolados, mas a sequência contaminante da base ainda não foi reduzida.
- **Required route:** autorização explícita para correção upstream separada e novo ciclo de integração/gates, ou decisão humana explícita dispensando o `pytest` completo previamente declarado obrigatório.
- **Hygiene:** symlink temporário Uazapi removido; worktree isolado contém somente os 16 caminhos e `git diff --check` está verde.

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
frontend/artifacts/neurostack/src/components/api-settings/voice-response-controls.tsx
frontend/artifacts/neurostack/src/components/api-settings/voice-response-controls.test.tsx
```
