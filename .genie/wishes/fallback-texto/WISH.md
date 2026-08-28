# Wish: Fallback texto — degradação graciosa da transcrição de áudio

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `fallback-texto` |
| **Date** | 2026-08-09 |
| **Author** | Lorenzo |
| **Appetite** | medium |
| **Branch** | `wish/fallback-texto`, cortado de `wish/api-provider-settings` — ver Dependencies |
| **Repos touched** | neurostack-api |
| **Design** | [DESIGN.md](../../brainstorms/fallback-texto/DESIGN.md) |
| **Governing design SHA** | `06a2947e6751d543963890fe8bc59ded48805c0d3bb34a0999e9c341e07284b8` (SHIP) |
| **Citations resolvem contra** | `wish/api-provider-settings@7ab570f` |

## Summary

Hoje, quando a transcrição de áudio falha, o webhook encerra em silêncio e o paciente nunca
recebe resposta. Este wish corrige primeiro um defeito de credenciais já em produção que torna
todo o caminho de áudio do Z-API inerte, e então constrói a cadeia de degradação: STT principal
→ modelo multimodal opcional como segunda tentativa → recondução da conversa por texto, com uma
mensagem honesta e enviada uma única vez por janela de 6h por paciente (Decisão 12).

A promessa é delimitada: **nenhum áudio termina em silêncio por falha ou ausência de capacidade
de transcrição, nos canais Z-API e Meta Cloud API.** São dois casos, e os dois entram: o STT foi
tentado e falhou, ou não existe transcrição configurada para tentar. O que fica de fora é a outra
classe de defeito — **falha de aquisição da mídia**, quando os bytes não chegam. Três exclusões,
cada uma por razão declarada na Scope OUT: os provedores de STT sem adaptador (invariante de
segurança, Decisão 10), toda falha de aquisição de mídia, simetricamente nos dois canais
(Decisão 11), e os canais uazapi/evolution, que descartam toda mídia antes de qualquer
processamento.

## Scope

### IN

- Correção da fonte de credenciais do handler de áudio Z-API, cobrindo `stt_api_key`,
  `zapi_token`, `zapi_client_token` e `multimodal_*`.
- Adaptador multimodal de transcrição em runtime, com políticas de safe-egress próprias.
- Cadeia de fallback compartilhada entre Z-API e Meta Cloud API.
- Mensagens de recondução em PT e EN, com variantes distintas para "STT nunca configurado" e
  "STT configurado mas falhou", entregues uma vez por janela de 6h por paciente.
- Conversa marcada como não lida após a recondução.
- Criação do card multimodal na aba APIs — hoje inexistente no front —, com rótulo
  `RECOMENDÁVEL`, e promoção de `multimodal_readiness` para `active` com credenciais completas.

### OUT

- **Fallback de TTS.** O agente não envia áudio em runtime (`tts_readiness` é
  `configuration_only`, `src/services/api_credentials.py:451`; nenhuma chamada de TTS existe em
  `src/`). Não há de que degradar; seria código morto.
- **Roteamento automático em falha do provedor de LLM de texto.** `src/llm/client.py` é de
  provedor único; é outro subsistema e merece wish próprio.
- Adapters STT de ElevenLabs, Google, Deepgram e Microsoft — seguem em
  `UNSUPPORTED_STT_PROVIDERS` (`src/api/routes/webhook.py:58`) com o envelope de skip atual.
- Circuit breaker, retry com backoff ou fila de reprocessamento de áudio.
- Ampliar o allowlist de `runtime_projection` para carregar segredos no cache Redis.
- **Fallback nos canais uazapi e evolution.** O uazapi descarta toda mídia — incluindo `audio`,
  `Audio` e `ptt` — antes de qualquer processamento (`webhook.py:1630-1646`); o evolution nunca lê
  objeto de áudio e sai em `:1396-1408`. Não há caminho de áudio de que degradar. Entra quando esses canais processarem áudio.
- **Recondução em falha de aquisição da mídia, nos dois canais.** No
  Z-API são os cinco `exc.code` do `except UnsafeEgressError` (`webhook.py:1106-1118`):
  `unsafe_api_url`, `audio_too_large`, `unsupported_audio_mime`, `audio_download_failed` e
  `redirect_forbidden`. No Meta são `missing_media_id` (`webhook.py:2438`), `no_credentials`
  (`:2444`), `media_resolve_failed` (`:2462` e `:2471`) e `media_download_failed` (`:2529`). Todos
  os nove seguem com o envelope silencioso de hoje, inalterados. Ver Decisão 11.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Corrigir a fonte de credenciais como Grupo 1, entregável sozinho | É um bug ativo em produção, independente da feature, e precisa poder sair antes dela |
| 2 | Reusar `_load_tenant_credentials` (`webhook.py:642-651`) em vez de criar loader novo ou ampliar o allowlist do cache | Mantém segredos fora do Redis, preserva o invariante testado em quatro lugares e resolve chave de STT e tokens de envio de uma vez; é o que o canal Meta já faz em `:2360` |
| 3 | Ordem STT → multimodal → texto | STT dedicado é mais barato e preciso; multimodal é rede de segurança, não padrão |
| 4 | Multimodal opcional, rotulado `RECOMENDÁVEL` na UI | Clínicas sem multimodal seguem funcionando com o degrau de texto |
| 5 | Texto é sempre o último degrau, inclusive se o multimodal falhar | Garante que nenhum áudio termine em silêncio, dentro dos limites declarados no Summary e nas Decisões 10 e 11 |
| 6 | Mensagens distintas para "nunca configurado" e "falhou agora" | Dizer "no momento não estou conseguindo" para quem nunca configurou STT é falso e cria expectativa errada |
| 7 | Aviso uma vez por janela de 6h por paciente, via Redis com TTL e fallback em memória | Evita spam em rajada de áudios; reaproveita o padrão de dedup já em `webhook.py:123-183`. Chave e TTL fixados na Decisão 12 |
| 8 | Marcar conversa como não lida, sem handoff nem pausar a IA | Espelha o fluxo de áudio longo (`webhook.py:1086`); a clínica enxerga e a IA segue atendendo por texto |
| 9 | Helper de cadeia único para Z-API e Meta | Os canais já divergiram: o Meta funciona e o Z-API está quebrado |
| 10 | Providers STT sem adaptador continuam saindo antes de qualquer download, em silêncio | Preserva o invariante de `webhook.py:1048-1049` (Z-API) e `:2448-2450` (Meta), fechado como CRITICAL no wish `api-provider-settings`. **Carve-out explícito da Decisão 5:** o invariante proíbe download, requisição de STT, resposta, handoff e mutação de sessão, então uma mensagem de recondução o violaria. Clínicas em `elevenlabs_scribe`, `google_stt`, `deepgram_stt` ou `microsoft_stt` seguem com áudio 100% silencioso, mesmo com multimodal configurado, até que exista um adaptador. Não é a única exceção — ver Decisão 11 |
| 11 | Falha de **aquisição da mídia** não entra na cadeia — simétrico nos dois canais | Distinto de falha ou ausência de transcrição, que entram. Estender a cadeia para cobrir aquisição não é uma mudança única: o Z-API mapeia cinco `exc.code` para quatro envelopes (`webhook.py:1106-1118`) e o Meta tem quatro pontos de saída próprios, com forma diferente. Pior, dois dos pontos do Meta — `missing_media_id` (`:2438`) e `no_credentials` (`:2444`) — ficam **antes** do gate de provedor não suportado (`:2448-2450`), então reconduzir ali daria resposta a clínicas em `deepgram_stt` e furaria a Decisão 10. Cobrir só os dois pontos pós-gate do Meta deixaria o paciente com resposta num canal e silêncio no outro pela mesma causa — exatamente a divergência que a Decisão 9 existe para eliminar. Fica tudo fora, dos dois lados, e volta como wish próprio se a telemetria de pós-deploy mostrar volume que justifique |
| 12 | Chave do aviso: `stt_fallback_notice:{tenant_id}:{phone}`, TTL de **21600 segundos** (6h) | O precedente citado usa 5 minutos (`_SENT_ID_TTL`, `webhook.py:75`) e o outro TTL do arquivo usa 31 dias (`REPLAY_TTL_SECONDS`, `:64`); sem fixar o valor, "uma vez por conversa" é ambíguo e o critério dos cinco áudios passa com qualquer escolha. 6h aproxima uma sessão de atendimento sem prender o aviso por dias. A constante literal `21600` é o argumento de `SETEX`, testável por asserção sobre a chamada. `phone` é a chave nos dois canais |
| 13 | Áudio `fromMe` pula antes do download | Depois do Grupo 1 o bloco de áudio passa a alcançar notas de voz da própria clínica, já que ele precede o gate `fromMe` (`webhook.py:1162-1166`). Como não existe TTS em runtime (`tts_readiness` é `configuration_only`), áudio `fromMe` é sempre nota humana da equipe e nunca eco da IA: transcrevê-la gastaria STT para alimentar `_handle_clinic_message` com um transcript que a clínica já conhece |
| 14 | O download do Z-API passa a ser condicionado à **capacidade de transcrição**, não à chave de STT | Hoje `safe_download_audio` (`webhook.py:1105`) está aninhado dentro de `if whisper_key:` (`:1062`), então sem chave de STT não há bytes — e `z_inst`/`z_tok`/`z_ct` (`:1063-1065`) nem chegam a ser ligados, de modo que a recondução não teria como ser enviada daquele ramo. Duas consequências. **(a)** O gate vira `whisper_key or multimodal operante`: uma clínica que configurou multimodal e não tem STT precisa que os bytes sejam baixados, senão o degrau multimodal — razão de existir do Grupo 4 e do rótulo `RECOMENDÁVEL` — nunca dispara. O custo novo de egress é opt-in: só ocorre para quem configurou multimodal deliberadamente. **(b)** Sem nenhum dos dois não há download nenhum: a variante "nunca configurado" é enviada sem baixar áudio, usando tokens de envio lidos fora daquele ramo. **(c)** A verificação de duração (`:1066-1095`) sobe junto com os tokens, para **acima** do gate de capacidade. Hoje ela mora dentro de `if whisper_key:`, enquanto no Meta o handoff de áudio longo (`:2478-2514`) roda antes de qualquer descoberta sobre STT e não depende de chave. Sem içá-la, um áudio de 3 minutos numa clínica sem capacidade nenhuma receberia "vou encaminhar para a equipe" mais handoff real no Meta e "por favor digite" no Z-API — divergência que a Decisão 9 e o critério de paridade proíbem. Içar é barato: a duração vem de `audio_obj["seconds"]`, do próprio payload, sem download. A ordem final no Z-API passa a espelhar a do Meta: gate de provedor não suportado → duração → capacidade → download. **Nada é reordenado no Meta:** ele já baixa antes de transcrever (`:2519-2521`) e `_meta_transcribe_audio` devolve `None` sem chave (`:2028-2029`), então lá o caso "nunca configurado" é pós-download. Essa assimetria residual é de ordem interna e invisível ao paciente; a paridade da Decisão 9 é sobre a mensagem entregue e sua causa, não sobre ter havido download. **"Multimodal operante" é definido uma vez, aqui, e vale para os Grupos 3 e 4:** credenciais multimodais completas **e** `multimodal_provider` em `OPERATIONAL_MULTIMODAL_PROVIDERS` — o mesmo predicado que o Grupo 4 usa no readiness, espelhando `multimodal_complete` (`api_credentials.py:419-427`) |

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

**depends-on:** api-provider-settings (merge)
**blocks:** none

`api-provider-settings` está `DONE` como wish (`.genie/wishes/api-provider-settings/WISH.md:5`,
`wish_status: DONE` em `:1617`), **mas ainda não foi mergeado em `main`**. Verificado:
`git rev-list --left-right --count main...wish/api-provider-settings` devolve `0 7`, e nenhum
destes arquivos existe em `main`:

| Arquivo | Em `main`? | Quem precisa |
|---|---|---|
| `src/services/api_credentials.py` | ausente | Grupos 1, 4 |
| `src/services/safe_egress.py` | ausente | Grupo 2 |
| `tests/api/test_webhook_egress.py` | ausente | Grupos 1, 3 |
| `tests/api/test_webhook_zapi.py` | ausente | Grupos 1, 3 |

`src/api/routes/webhook.py` existe em `main`, mas diverge em 968 linhas — o bloco de áudio
`:1036-1154` que este wish reescreve é daquele branch.

**Regra de execução:** `wish/fallback-texto` é cortado de `wish/api-provider-settings`, ou de
`main` somente depois que aquele branch mergear. Todas as citações `arquivo:linha` deste wish
resolvem contra `wish/api-provider-settings@7ab570f`; cortar de `main` hoje as invalida em bloco.

O código é recente e carrega invariantes de segurança que aquele wish fechou como CRITICAL;
preservá-los é o objeto da Decisão 10.

## Success Criteria

- [ ] Com STT configurado e funcionando, o áudio é transcrito e respondido normalmente, sem regressão.
- [ ] Existe um teste do caminho de áudio **do Z-API** que não faz patch de `_load_clinic_config`
      **nem** de `_load_tenant_credentials` — semeia as credenciais como linha de config real via
      `_seed_zapi_with_stt` (`tests/api/test_webhook_egress.py:69-90`) — e ainda assim alcança a
      transcrição, provando que a chave chega ao handler.
- [ ] Com STT que lança exceção e sem multimodal, o paciente recebe a mensagem de falha
      temporária e a conversa é marcada como não lida; o webhook deixa de responder com skip silencioso.
- [ ] Com STT falhando e multimodal configurado, a transcrição multimodal é tentada e, quando
      bem-sucedida, a conversa segue normalmente sem nenhuma mensagem de recondução.
- [ ] Com STT e multimodal falhando, o paciente recebe a mensagem de recondução por texto.
- [ ] Sem STT e sem multimodal operante, o paciente recebe a variante "nunca configurado",
      distinta da mensagem de falha temporária, e nenhum áudio é baixado.
- [ ] Sem STT mas com multimodal operante, o áudio é baixado e transcrito pelo multimodal, sem
      nenhuma mensagem de recondução — conforme Decisão 14.
- [ ] Cada mensagem de recondução é verificada como **entregue**, afirmando sobre a chamada de
      envio, e não apenas selecionada.
- [ ] Cinco áudios seguidos com STT quebrado produzem exatamente uma mensagem de recondução.
- [ ] O handoff de áudio longo (`webhook.py:1082-1088`) volta a enviar a mensagem e marcar a
      conversa como não lida, com teste que entra pelo webhook fim a fim.
- [ ] STT devolve string vazia: nos dois canais o transcript vazio entra na cadeia em vez de sair
      calado por `{"skipped": "empty"}` (Z-API) ou `{"skipped": "no_whisper_key"}` (Meta).
- [ ] Nenhuma falha de aquisição de mídia muda de comportamento: os cinco `exc.code` do Z-API
      (`webhook.py:1106-1118`) e os quatro pontos de saída de mídia do Meta continuam devolvendo
      os mesmos envelopes silenciosos de hoje, conforme Decisão 11.
- [ ] Quando uma recondução é enviada, o envelope de retorno a identifica como tal e não se
      confunde com uma resposta real da IA.
- [ ] Áudio `fromMe` não chega a baixar, a transcrever, nem a receber recondução, conforme
      Decisão 13 — o guard fica acima do gate de capacidade.
- [ ] Os quatro provedores em `UNSUPPORTED_STT_PROVIDERS` continuam saindo com o envelope de skip
      antes de qualquer download, sem disparar a cadeia de fallback, nos dois gates
      (`webhook.py:1048-1049` e `:2448-2450`) — carve-out declarado na Decisão 10.
- [ ] `runtime_projection` continua sem expor `stt_api_key` nem campos `multimodal_*`, e os
      testes de invariante de cache passam sem alteração.
- [ ] Z-API e Meta Cloud API selecionam e entregam a mesma mensagem, pela mesma causa, no mesmo
      cenário. Marcar como não lida fica fora da paridade: é afordância só do Z-API.
- [ ] As mensagens existem em PT e EN, respeitando `_get_agent_language`.
- [ ] O card multimodal existe na aba APIs, persiste as quatro credenciais via PATCH, exibe
      `RECOMENDÁVEL`, e `multimodal_readiness` retorna `active` apenas para provedores com
      adaptador comprovado no Grupo 2 — nunca para um provedor que o adaptador não consegue servir.

## Execution Strategy

### Wave 1 (parallel)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 3: webhook stateful (+2), corrige regressão de trabalho em andamento (+1) | engineer-standard / high | Trocar a fonte de credenciais do handler de áudio Z-API e cobrir o caminho sem patch |
| 2 | engineer | 2: adaptador de provedor de modelo com egress novo (+2) | engineer-standard / medium | Adaptador multimodal de transcrição e políticas de safe-egress |

Grupos 1 e 2 não se tocam: o Grupo 1 mexe no handler do webhook, o Grupo 2 cria uma função de
transporte nova, sua política de egress e a constante `OPERATIONAL_MULTIMODAL_PROVIDERS` em
`api_credentials.py`. Conjuntos de escrita disjuntos, podem correr em paralelo. Mesma ressalva de
árvore compartilhada das Waves seguintes: a validação do Grupo 1 roda
`tests/services/test_api_credentials.py`, que o Grupo 2 altera — em worktrees isolados não há
conflito; na mesma árvore, o 1 valida antes do 2.

### Wave 2 (parallel)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 4: roteamento de fallback (+2), estado de aviso com TTL (+2) | engineer-complex / high | Cadeia compartilhada Z-API/Meta, mensagens PT/EN e aviso uma vez por janela de 6h |
| 4 | engineer | 3: front + back no mesmo grupo (+1), card de credenciais novo com fiação de PATCH (+1), toca contrato de status consumido pela UI (+1) | engineer-standard / high | Criar o card multimodal com rótulo RECOMENDÁVEL e promover `multimodal_readiness` |

Grupos 3 e 4 correm em paralelo: o 3 mexe em `webhook.py` e nas suítes de webhook, o 4 em
`api_credentials.py` e no front. Conjuntos disjuntos. O Grupo 4 saiu de uma wave própria depois
que o plan review mostrou que a dependência do wish `api-provider-settings` era fantasma.

O Grupo 4 não é acabamento: sem ele o card multimodal não existe, nenhuma clínica consegue
configurar as credenciais e o degrau multimodal do Grupo 3 nunca dispara em produção. Ele declara
`depends-on: 2` — precisa da constante e da evidência de compatibilidade que o Grupo 2 publica — e
`ships-with: 3`, que é restrição de release, não aresta de dependência. Por isso 3 e 4 correm em
paralelo sem se bloquear e mergeiam juntos.

A validação do Grupo 3 roda `tests/services/test_api_credentials.py`, que o Grupo 4 altera. Em
worktrees isolados não há conflito; se os dois rodarem na mesma árvore, o 3 valida antes do 4.

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
   **Carregar uma vez e reaproveitar:** a mesma função já é chamada adiante em `:1299`, então
   içar a chamada para antes do bloco de áudio e reusar o resultado evita um segundo
   round-trip ao banco por mensagem. O tradeoff é real e deve ser medido pelo implementador:
   içar adiciona uma leitura para requisições que hoje retornam antes de `:1299` — `non-text`
   (`:1157`), `empty` (`:1159`), `ai_echo` (`:1166`) e os skips de pausa. Existe ainda uma
   terceira leitura crua na mesma função, SQL inline para `mark_message_read`
   (`webhook.py:1186-1196`), que o mesmo hoist pode colapsar.
2. Deltas de normalização tratados explicitamente, já que a leitura crua não roda
   `resolve_stt_credentials` (`api_credentials.py:265-289`): (a) o default de `stt_provider` →
   `"outros"` é coberto por `_stt_canonical_provider` (`webhook.py:1042`, definição `:390-400`),
   e o gate de provedor não suportado (`:1048-1049`) deve continuar válido; (b) `audio_max_seconds`
   tem fallback inline em `:1068`; (c) `runtime_projection` só projeta `stt_api_url` e
   `stt_model_name` quando o provider está em `OPERATIONAL_STT_PROVIDERS`
   (`api_credentials.py:478-481`), enquanto a leitura crua sempre os carrega — confirmar que a
   resolução de `stt_base_url` (`webhook.py:1136-1141`) não muda para provedores fora desse conjunto;
   (d) **o bloco de áudio roda antes do gate `fromMe`** (`webhook.py:1162-1166`). Hoje todo áudio
   morre em `:1154`, então áudio `fromMe` nunca chega a `:1162`. Depois desta correção, a nota de
   voz de um funcionário da clínica passaria a ser baixada, transcrita com custo real de STT e
   entregue a `_handle_clinic_message` (`:1178-1180`) — comportamento novo em produção. Conforme a
   Decisão 13, **áudio `fromMe` pula antes do download**: mover o teste de `fromMe` para antes do
   bloco de áudio, ou colocar o guard **no topo do bloco interno de `:1039`** — não logo acima de
   `safe_download_audio`. A distinção passa a importar no Grupo 3: a Decisão 14 cria um caminho
   sem download nenhum, e um guard colocado só acima do download satisfaria este critério ao pé da
   letra enquanto mandaria "por favor digite" para a nota de voz da própria clínica.
3. Teste do caminho de áudio Z-API que alcança a transcrição **sem fazer patch de nenhum loader
   de credenciais** — nem `_load_clinic_config`, nem `_load_tenant_credentials`. As credenciais
   precisam vir de uma linha real de `ClinicConfigTable`. O helper já existe no arquivo a ser
   alterado: `_seed_zapi_with_stt` (`tests/api/test_webhook_egress.py:69-90`) semeia via
   `PATCH /v1/config/api-credentials` com `zapi_instance_id`, `zapi_token`,
   `zapi_inbound_token_digest`, `stt_provider` e `stt_api_key`. Os testes atuais já o chamam **e
   ainda assim** fazem patch de `_load_clinic_config` por cima (`:177-181`, `:225-229`, `:333-337`),
   que é o que mascara o defeito. Caminho mínimo: remover esses três patches e apagar o helper
   `_clinic_config_with_stt` (`:109-128`), cujo docstring em `:114-119` vira mentira depois do
   Grupo 1.

   **Não confundir com `tests/api/test_webhook_zapi.py:195`, `:387`, `:501`:** ali o
   `_load_clinic_config` devolve `{}` num `MagicMock()` de EventService cujo `process_event`
   também é mock, dentro de testes de mensagem de **texto**. Com `clinic_config` falsy, o guard
   `if audio_url and clinic_config:` (`webhook.py:1039`) nem entra no bloco de áudio — esses
   testes não mascaram nada e devem ficar como estão.
4. Teste de regressão do handoff de áudio longo entrando pelo webhook fim a fim, com áudio acima
   de `audio_max_seconds`, afirmando sobre a chamada de envio e sobre `mark_conversation_unread`.

**Acceptance Criteria:**
- [ ] Existe teste do caminho Z-API que alcança a transcrição sem patch de `_load_clinic_config`
      **nem** de `_load_tenant_credentials`, com credenciais vindas de linha real semeada.
- [ ] O handoff de áudio longo envia a mensagem e marca a conversa como não lida, provado por
      teste que entra pelo webhook e não injeta credenciais por patch.
- [ ] O allowlist de `runtime_projection` não é alterado e nenhum segredo passa a entrar no cache.
- [ ] Os quatro provedores em `UNSUPPORTED_STT_PROVIDERS` seguem saindo antes de qualquer
      download, sem chamar `safe_download_audio`, nos dois gates: `webhook.py:1048-1049` (Z-API) e
      `:2448-2450` (Meta).
- [ ] Áudio com `fromMe: true` não chama `safe_download_audio` nem o STT, provado por teste que
      afirma zero chamadas em ambos. Depois do Grupo 3, o mesmo teste também afirma zero chamadas
      de envio, para pegar o guard colocado baixo demais.
- [ ] Delta (c) confirmado por consulta, não por raciocínio. `OPERATIONAL_STT_PROVIDERS`
      (`api_credentials.py:34`) e `UNSUPPORTED_STT_PROVIDERS` (`webhook.py:58`) particionam o
      allowlist de `api_credentials.py:20-29`, então só uma linha com `stt_provider` **fora do
      allowlist** — valor legado ou escrito antes da validação — chega a `:1136` com `stt_api_url`
      recém-visível. A consulta:
      ```sql
      SELECT tenant_id, api_credentials->>'stt_provider' AS stt_provider
      FROM clinic_configs
      WHERE btrim(coalesce(api_credentials->>'stt_provider', '')) <> ''
        AND btrim(api_credentials->>'stt_provider') NOT IN (
          'outros','openai_whisper','elevenlabs_scribe',
          'google_stt','deepgram_stt','microsoft_stt'
        );
      ```
      O `btrim`/`<> ''` não é cosmético: `_stt_canonical_provider` normaliza vazio e espaço em
      branco para `"outros"`, que é operacional, então sem o filtro a consulta devolve
      falso-positivo. **Rodar contra produção, não só contra dev** — um valor legado só existiria
      em linhas escritas antes da validação de provider, e é a população de produção que carrega
      essa história.
      **Zero linhas** → registrar a saída da consulta como evidência e seguir, sem teste novo.
      **Uma ou mais linhas** → o delta é real: adicionar teste que fixa a resolução de
      `stt_base_url` (`webhook.py:1136-1141`) para esse `stt_provider`, e anexar a lista de
      tenants afetados ao grupo antes de mergear.
- [ ] O caminho de áudio do Meta permanece inalterado em comportamento.

**Validation:**
```bash
poetry run pytest tests/api/test_webhook_egress.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/services/test_api_credentials.py tests/services/test_event_service_config_cache.py tests/api/test_config_credentials_boundary.py -v
poetry run mypy src/api/routes/webhook.py --ignore-missing-imports 2>&1 | tail -1  # baseline: 36 erros pré-existentes, não pode subir
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
1. `transcribe_audio_multimodal(api_key, audio_bytes, mimetype, model, base_url)` em
   **`src/services/multimodal_service.py`** — arquivo novo, não `groq_service.py`, que é
   nomeado por provedor e serviria openrouter e openai — postando em `{base}/chat/completions`
   com blocos de conteúdo `input_audio` no formato OpenAI. Segue a forma de `transcribe_audio`
   (`src/services/groq_service.py:39-79`), inclusive `safe_async_client`.
2. **Resolver mimetype → `format`, que é o risco real deste grupo.** O bloco `input_audio` da
   OpenAI aceita apenas `wav` e `mp3` no campo `format`, mas o WhatsApp entrega OGG/Opus
   (`webhook.py:1038` usa `"audio/ogg"` como default; `safe_egress.py:66-69` libera todo
   `audio/*`). Determinar, por provedor, o que cada um aceita de fato, e implementar o mapeamento
   ou declarar o provedor incompatível. Sem isto o adaptador passa nos mocks e falha na primeira
   chamada real.
3. Resolver URL → `kind`, análogo a `_stt_kind_for_url` (`src/services/groq_service.py:25-36`):
   `safe_async_client(kind=...)` (`safe_egress.py:545`) levanta exceção para `kind` desconhecido,
   então sem este resolver o adaptador não tem como ser chamado.
4. Políticas `multimodal_openai`, `multimodal_groq` e `multimodal_openrouter` em
   `PROVIDER_POLICIES` (`src/services/safe_egress.py:327-339`), **com host pinado nos três**.
   Diferente de STT, que tem `stt_outros` com `allowed_hosts=None` (`safe_egress.py:335`), o
   allowlist multimodal não tem `outros` (`api_credentials.py:19`): os três provedores são
   conhecidos, então não existe caso "custom" a acomodar e uma política aberta tornaria o critério
   de rejeição de host insatisfazível.
5. **`OPERATIONAL_MULTIMODAL_PROVIDERS` em `src/services/api_credentials.py`**, ao lado de
   `OPERATIONAL_LLM_PROVIDERS` e `OPERATIONAL_STT_PROVIDERS` (`:33-34`), contendo apenas os
   provedores que o Deliverable 2 comprovou aceitarem o áudio do WhatsApp. Este grupo é quem sabe
   a resposta, então é quem publica o conjunto; o Grupo 4 o consome para não anunciar `Ativo` a
   quem o adaptador não consegue servir. Se os três forem comprovados, o conjunto é igual ao
   allowlist e a distinção fica inerte — mas explícita, como já é para LLM e STT.
6. Testes de unidade com transporte mockado, cobrindo sucesso, erro HTTP e resposta vazia.
7. Testes de egress confirmando que cada `kind` novo resolve e que host fora do allowlist é rejeitado.
8. **Registro da verificação ao vivo do Deliverable 2** em
   `.genie/wishes/fallback-texto/evidence/multimodal-format-probe.md`: para cada um dos três
   provedores, o `format` enviado, o mimetype de origem e o resultado cru da chamada. É o
   artefato que a suíte mockada não pode produzir e de que o Grupo 4 depende.

**Acceptance Criteria:**
- [ ] `transcribe_audio_multimodal` devolve o texto transcrito para uma resposta bem-formada.
- [ ] O corpo da requisição é verificado na forma exata — estrutura do bloco de conteúdo e valor
      de `format` derivado do mimetype — e não apenas o valor de retorno; um mock que aceita
      qualquer payload não satisfaz este critério.
- [ ] **Pelo menos um** dos três provedores transcreve um payload OGG/Opus real de ponta a ponta,
      contra o endpoint de verdade. Este critério **não é satisfeito pela suíte mockada** — a
      evidência é o arquivo do Deliverable 8, com a resposta crua do provedor. Se nenhum aceitar,
      o degrau multimodal sai do wish e os Grupos 3 e 4 encolhem junto; o que não pode acontecer é
      os três serem declarados incompatíveis e o grupo passar assim mesmo, deixando a cadeia com
      dois degraus e um card que não serve para nada.
- [ ] `OPERATIONAL_MULTIMODAL_PROVIDERS` existe e contém exatamente os provedores comprovados no
      arquivo de evidência — nem mais, nem menos.
- [ ] Falha de transporte ou resposta inesperada levanta exceção, sem devolver string vazia
      silenciosamente — o degrau seguinte da cadeia depende de distinguir os dois casos.
- [ ] Cada `kind` novo resolve por `resolve_policy` e rejeita host fora do allowlist.
- [ ] Nenhum arquivo do webhook é modificado neste grupo.

**Validation:**
```bash
poetry run pytest tests/services/test_safe_egress.py tests/services/test_multimodal_transcription.py tests/services/test_api_credentials.py -v
poetry run mypy src/services/multimodal_service.py src/services/safe_egress.py src/services/api_credentials.py --ignore-missing-imports
test -s .genie/wishes/fallback-texto/evidence/multimodal-format-probe.md  # verificação ao vivo, Deliverable 8
```
Escopo: o grupo adiciona uma função de transporte, entradas de política e uma constante; os testes
focados dessas unidades bastam porque nenhum consumidor é ligado ainda. `safe_egress` entra por ser
fronteira de segurança compartilhada e `test_api_credentials` pela constante nova. A suíte é toda
mockada por construção, então a checagem do arquivo de evidência é o que impede o critério de
compatibilidade real de passar por vácuo.

**depends-on:** none

---

### Group 3: Cadeia de fallback e recondução por texto

**Goal:** Ligar STT, multimodal e recondução por texto numa cadeia única usada pelos dois canais,
de modo que nenhum áudio termine em silêncio **por falha ou ausência de transcrição** — o limite
da promessa, conforme Decisões 10, 11 e 14.

**Deliverables:**
1. Helper de cadeia com assinatura `(audio_bytes: bytes | None, credenciais) -> (texto,
   motivo_da_falha)`, tentando STT e, se falhar e houver multimodal operante, o adaptador do
   Grupo 2. **`None` é um valor de entrada legítimo, não um erro:** é o caminho sem capacidade
   nenhuma da Decisão 14 (b), em que nada foi baixado. Se o helper exigisse bytes, o caso "nunca
   configurado" teria a mensagem escolhida dentro do helper no Meta e fora dele no Z-API — o
   exato tipo de bifurcação por canal que a Decisão 9 existe para impedir. Com `None`, os dois
   canais escolhem a mensagem no mesmo lugar.
2. Chamada do helper nos dois caminhos: Z-API (`webhook.py:1036-1154`) e Meta — o bloco
   `elif msg_type in _META_AUDIO_TYPES:` inteiro, `webhook.py:2432-2553` —, substituindo as saídas
   `transcription_failed` e `no_whisper_key`. **O gate de provedor não suportado do Meta está em
   `:2448-2450`, dentro dessa faixa e antes de qualquer resolve ou download: é load-bearing e não
   pode ser reordenado.**

   **Separar as duas causas no Meta:** hoje `_meta_transcribe_audio(...) or ""` seguido de
   `if not message_text: return {"skipped": "no_whisper_key"}` (`webhook.py:2550-2551`) torna um
   STT que funcionou mas devolveu texto vazio indistinguível de chave ausente. Enquanto isso não
   for separado, o critério de paridade e a variante "nunca configurado" não podem ambos ser
   atendidos. O formato `(texto, motivo_da_falha)` do Deliverable 1 é o que resolve.

   **O mesmo buraco existe no Z-API e precisa entrar na cadeia:** `transcribe_audio` devolve
   `resp.text.strip()` (`src/services/groq_service.py:79`), que é `""` para áudio silencioso ou
   ininteligível; isso cai em `webhook.py:1158-1159` → `{"skipped": "empty"}`, silencioso e sem
   fallback. Se só o Meta for corrigido, transcript vazio entra na cadeia num canal e sai calado
   no outro — quebrando a paridade e a Decisão 5.

   **Não rotear `:1159` inteiro, e a marca certa não é "tentou transcrever".** `message_text`
   também vem de `text_obj` (`:1031-1033`). Então `:1159` é alcançado por pelo menos três
   caminhos: transcript vazio, mensagem de **texto** com corpo vazio, e áudio sem `audio_url` ou
   sem linha de config. A marca a carregar é **"o corpo de `if audio_url and clinic_config:`
   (`:1039`) foi executado"** — o bloco **interno**, não o externo de `:1036`. Duas razões para
   fixar qual: (i) "houve tentativa de transcrição" seria estreito demais, porque no caso "STT
   nunca configurado" o bloco roda e nenhuma transcrição é tentada, e é justamente esse caso que
   precisa da variante própria da Decisão 6; (ii) marcar no bloco externo seria largo demais,
   porque `audio_obj` presente com `audio_url` ausente, ou sem linha de config, não é falha de
   transcrição — é mídia que nunca existiu ou tenant sem configuração, e reconduzir ali mandaria
   "por favor digite" para um caso sem causa. Condicionar a cadeia à marca do bloco interno.
3. **Gate de capacidade no Z-API, conforme Decisão 14.** Hoje `safe_download_audio` (`:1105`)
   está aninhado em `if whisper_key:` (`:1062`) e o `else` devolve `no_whisper_key` (`:1153-1154`).
   Trocar a condição por `whisper_key or multimodal operante` — predicado definido na Decisão 14 —
   de modo que o degrau multimodal alcance uma clínica sem STT. Sem STT e sem multimodal, nenhum
   download acontece e a variante "nunca configurado" é enviada direto. Duas coisas sobem para
   fora do ramo, e as duas são obrigatórias:
   - **Os tokens de envio** `z_inst`/`z_tok`/`z_ct` (`:1063-1065`), lidos hoje dentro dele; sem
     içá-los o ramo sem capacidade não tem como enviar a recondução.
   - **A verificação de duração** (`:1066-1095`), conforme Decisão 14 (c). No Meta o handoff de
     áudio longo (`:2478-2514`) já roda antes de qualquer descoberta sobre STT; deixá-la dentro do
     gate faria um áudio longo de clínica sem capacidade receber handoff no Meta e "por favor
     digite" no Z-API, quebrando o critério de paridade. A ordem final é: gate de provedor não
     suportado → duração → capacidade → download.

   **O gate de provedor não suportado (`:1048-1049`) continua acima de tudo isso e não se move.**
   O guard `fromMe` da Decisão 13 fica no topo do bloco interno de `:1039`, **acima** do gate de
   capacidade: uma nota de voz da própria clínica não deve gerar download, STT, nem recondução.
4. **Envelope de retorno distinguível.** Quando a cadeia envia uma recondução, o webhook responde
   `{"ok": True, "replied": True, "action": "stt_fallback"}` — a chave `action` segue a convenção
   já usada no arquivo: `"file_handoff"` (`webhook.py:2422-2425`) e `"file_ack"` (`:2599-2603`,
   chave em `:2602`). Sem ela, uma recondução fica indistinguível de uma resposta real da IA no
   log e na asserção de teste, e o critério de paridade entre canais não tem sobre o que afirmar.
5. Mensagens de recondução em PT e EN, resolvidas por `_get_agent_language`, com variantes
   distintas para STT nunca configurado e para falha temporária. **No Meta, o envio exige
   `meta_pnid`** (`webhook.py:2361`). `meta_token` não precisa ser tratado: sua ausência já sai em
   `:2444` com `{"skipped": "no_credentials"}`, envelope que este wish congela, então o caso nunca
   alcança a recondução. Faltando `meta_pnid`, seguir o precedente do próprio caminho de áudio,
   que pula o envio sob `if meta_pnid and meta_token:` (`:2487`), e **não** o do caminho de
   arquivo, que devolve `{"replied": False, "reason": "no_credentials"}` (`:2562-2567`). Nesse
   caso o retorno é `{"ok": True, "replied": False, "action": "stt_fallback_undeliverable"}`:
   a recondução é best-effort, mas o envelope precisa dizer que nada foi entregue — anunciar
   `replied: True` para uma mensagem que não saiu esvaziaria a razão de ser do Deliverable 4.
6. Aviso entregue uma vez por janela de 6h por paciente, na chave e TTL fixados pela Decisão 12,
   com fallback em memória, seguindo
   o padrão de `_track_sent_message` / `_is_ai_sent_message` (`webhook.py:123-183`).
   **Registrar o novo store em memória nas duas fixtures autouse** que limpam `_INMEM_SENT` e
   `_INMEM_INCOMING` nominalmente: `tests/api/test_webhook_egress.py:131-140` (`_clear_state`) e
   `tests/api/test_webhook_zapi.py:144-157` (`_clear_process_state`) — os dois arquivos são
   alterados por este grupo. **`tests/api/test_webhook_meta.py`, que este grupo também altera para
   o teste de paridade, não tem nenhuma fixture `autouse`:** ele limpa `_INMEM_INCOMING` via o
   helper `_clear_replay_store` (`:1418`), chamado de `setup_method` em três classes
   (`:1423`, `:1656`, `:1799`), então o vazamento é entre classes
   sem `setup_method`, e a correção é uma fixture `autouse` nova no módulo. Sem isso, o estado do aviso vaza entre testes: o primeiro passa e os seguintes
   afirmam zero envios. O teste dos cinco áudios também precisa de `messageId` distinto
   por payload, senão a dedup de replay descarta as mensagens antes da cadeia.
7. Conversa marcada como não lida após a recondução, sem handoff nem pausar a IA. **Isto é uma
   afordância só do Z-API:** `mark_conversation_unread` (`src/services/zapi_service.py:74`) tem
   exatamente dois call sites, `webhook.py:1086` e `:1344`. O Meta Cloud API não tem primitiva de
   não lida — seu caminho de áudio longo usa `_meta_initiate_file_handoff` (`webhook.py:2501-2506`).
   **No Meta, a mensagem de recondução é o efeito completo** — nenhum handoff, nenhuma marcação,
   nenhuma mutação de sessão.

**Acceptance Criteria:**
- [ ] STT falha sem multimodal: paciente recebe a mensagem de falha temporária, verificada na
      chamada de envio; no Z-API a conversa também é marcada como não lida.
- [ ] STT falha com multimodal que funciona: a conversa segue normalmente e nenhuma mensagem de
      recondução é enviada.
- [ ] STT e multimodal falham: paciente recebe a mensagem de recondução, verificada na chamada
      de envio.
- [ ] Sem STT **e sem multimodal operante**: paciente recebe a variante "nunca configurado",
      textualmente distinta da falha temporária, verificada na chamada de envio. Nesse caso
      `safe_download_audio` **não** é chamado, provado por asserção de zero chamadas (Decisão 14).
- [ ] **Áudio acima de `audio_max_seconds` numa clínica sem STT e sem multimodal** produz o
      handoff de áudio longo nos **dois** canais — mensagem de encaminhamento, e no Z-API também
      `mark_conversation_unread` —, nunca a variante "nunca configurado". É o caso que a Decisão
      14 (c) resolve içando a verificação de duração; nenhum teste existente o cobre, então é
      teste novo em `test_webhook_zapi.py` e `test_webhook_meta.py`.
- [ ] Sem STT **mas com multimodal operante**: o áudio é baixado, o multimodal transcreve, a
      conversa segue normalmente e nenhuma recondução é enviada — o quadrante que dá sentido ao
      rótulo `RECOMENDÁVEL` do Grupo 4.
- [ ] Nenhuma falha de aquisição de mídia envia mensagem: os seis casos de
      `tests/api/test_webhook_egress.py:149-159` continuam produzindo exatamente os mesmos
      envelopes, incluindo `("unknown_code_xyz", "audio_download_failed")` de `:157`. A
      parametrização em si não é alterada; o corpo do teste pode mudar pelo Grupo 1.
- [ ] Os quatro envelopes de mídia do Meta (`missing_media_id`, `no_credentials`,
      `media_resolve_failed`, `media_download_failed`) permanecem byte a byte como hoje.
- [ ] Uma mensagem de **texto** com corpo vazio continua retornando `{"ok": True, "skipped":
      "empty"}` sem enviar nada — a cadeia só dispara a partir do bloco interno de `:1039`.
- [ ] `audio_obj` presente com `audio_url` ausente, ou sem linha de config, continua saindo por
      `{"ok": True, "skipped": "empty"}` sem enviar nada — o bloco interno de `:1039` não roda,
      logo não há falha de transcrição de que reconduzir.
- [ ] Sem `meta_pnid`, a recondução no Meta não é enviada e o retorno é
      `{"ok": True, "replied": False, "action": "stt_fallback_undeliverable"}` — nem
      `no_credentials`, nem `replied: True`.
- [ ] No Meta, a recondução não dispara handoff nem qualquer mutação de sessão, provado por
      asserção de zero chamadas a `_meta_initiate_file_handoff`.
- [ ] A chave `stt_fallback_notice:{tenant_id}:{phone}` é gravada com TTL de `21600` segundos,
      provado por asserção sobre o argumento do `SETEX`.
- [ ] Depois do TTL da Decisão 12 expirar, um novo áudio com STT quebrado envia um segundo
      aviso — a supressão é por janela, não permanente.
- [ ] STT devolve string vazia: nos **dois** canais o transcript vazio entra na cadeia de
      fallback em vez de sair por `{"skipped": "empty"}` (Z-API, `webhook.py:1159`) ou
      `{"skipped": "no_whisper_key"}` (Meta, `:2551`).
- [ ] Cinco áudios seguidos com STT quebrado produzem exatamente uma mensagem enviada.
- [ ] Z-API e Meta enviam a mesma mensagem, escolhida pela mesma causa, para o mesmo cenário,
      provado por teste de paridade. A paridade é sobre seleção e entrega da mensagem; marcar
      como não lida fica fora dela por ser afordância exclusiva do Z-API (`zapi_service.py:74`).
- [ ] Quando uma recondução é enviada, o webhook responde
      `{"ok": True, "replied": True, "action": "stt_fallback"}`, distinguível de uma resposta real
      da IA. O envelope do caminho de sucesso, `{"ok": True, "replied": True}`, permanece pinado
      como hoje em `tests/api/test_webhook_zapi.py:509`. Asserção sobre o retorno, não só sobre a
      chamada de envio.
- [ ] Os quatro provedores não suportados seguem saindo antes de qualquer download, nos gates
      `webhook.py:1048-1049` e `:2448-2450`.
- [ ] Cada mensagem existe em PT e EN.

**Validation:**
```bash
poetry run pytest tests/api/test_webhook_egress.py tests/api/test_webhook_zapi.py tests/api/test_webhook_meta.py tests/api/test_webhook_uazapi.py tests/services/test_api_credentials.py tests/services/test_event_service_config_cache.py -v
poetry run mypy src/api/routes/webhook.py --ignore-missing-imports 2>&1 | tail -1  # baseline: 36 erros pré-existentes, não pode subir
```
Escopo: o grupo altera o comportamento de resposta dos dois canais de webhook no caminho
compartilhado de mensagens, então rodam todas as suítes de canal, incluindo uazapi para pegar
regressão cruzada no handler comum, mais as suítes de invariante de credenciais.

**depends-on:** 1, 2

---

### Group 4: Card multimodal na aba APIs e readiness

**Goal:** Dar às clínicas uma forma de configurar o multimodal pela UI e marcá-lo como recomendado.

**O card não existe.** O back-end já emite o contrato completo — `credential_status`
(`src/services/api_credentials.py:392-455`) devolve `multimodal_provider`, `multimodal_api_url`,
`multimodal_model_name`, `multimodal_api_key_set`, `multimodal_api_key_masked` e
`multimodal_readiness` — mas o front nunca foi construído: a única ocorrência de "multimodal" em
`frontend/artifacts/neurostack/src/` é `clinic-config.tsx:1371`, uma string de nome de modelo de
STT (`"phi-4-multimodal"`), e a interface `CredentialStatus` (`clinic-config.tsx:175-224`) não
tem nenhum campo `multimodal_*`. Sem este grupo, nenhuma clínica consegue ativar o multimodal, e o
degrau multimodal do Grupo 3 fica inalcançável em produção — este grupo é pré-requisito funcional
da cadeia, não acabamento.

**Decisão resolvida em 2026-08-11 (usuário): URL fixa por provider, campo somente leitura.**
Com os três hosts pinados (Grupo 2, Deliverable 4), um campo `multimodal_api_url` livre deixaria a
clínica salvar um proxy, receber PATCH 200 e falhar em runtime com `UnsafeEgressError` sem nenhum
sinal na UI — exatamente o estado "salvo mas silenciosamente inútil" que o critério 12 de
`api-provider-settings` existe para impedir. Um campo de texto livre que só pode conter
legalmente um de três hostnames exatos não é superfície de configuração, é armadilha. A URL passa
a ser derivada do provider escolhido e renderizada somente leitura, o que **elimina o caminho de
erro em vez de construí-lo**: nenhuma validação nova em `src/api/routes/config.py`, nenhuma lista
de hosts duplicada entre back e front para manter em sincronia.

Custo aceito: diverge dos cards de LLM e STT, que têm URL editável, e uma clínica com endpoint
regional não teria como apontar para ele — mas hoje isso falharia em runtime de qualquer forma,
porque a política de egresso pina o host. Se surgir demanda real por endpoint alternativo, ela
volta como wish próprio junto com a ampliação da política.

**Não há trabalho de back-end além do readiness.** `src/api/schemas/config.py:93-96` já aceita os
quatro campos multimodais e `src/api/routes/config.py:472` já valida o provider contra o allowlist
de `api_credentials.py:19`. O PATCH funciona hoje; falta só quem o chame.

**Padrão de UI a seguir:** os cards de provedor da aba APIs são `<button>` inline, não componentes
— ver o grupo de STT em `clinic-config.tsx:1296-1337`, com o precedente de badge em `:1323-1330`
(`data-testid={\`stt-config-only-${p.id}\`}`). Existe um `ProviderCredentialCard`
(`src/components/api-settings/provider-credential-card.tsx`), importado em `clinic-config.tsx:44`
mas nunca renderizado, e sem afordância de "recomendado". Seguir o padrão inline por consistência;
se em vez disso o componente compartilhado for adotado, ele e seu teste entram na lista de
arquivos alterados.

**Deliverables:**
1. `multimodal_readiness` passa de `configuration_only` para `active` quando as credenciais
   estiverem completas **e** o provider estiver em `OPERATIONAL_MULTIMODAL_PROVIDERS`
   (publicado pelo Grupo 2), espelhando o gate que LLM (`api_credentials.py:414-418`) e STT
   (`:446-450`) já aplicam. Hoje `multimodal_complete` (`:419-427`) não tem gate de operabilidade
   nenhum, então sem esta parte o card anunciaria `Ativo` para um provider que o adaptador não
   consegue servir — o estado "salvo mas silenciosamente inútil" que o critério 12 de
   `api-provider-settings` existe para impedir.
2. Criar o card multimodal em `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`:
   seletor de provider (`openrouter` | `groq` | `openai`, conforme `api_credentials.py:19`),
   `multimodal_api_url`, `multimodal_api_key`, `multimodal_model_name`.
3. Adicionar à interface `CredentialStatus` (`clinic-config.tsx:175-224`) os **seis** campos que
   `credential_status` já emite e que o critério de persistência exige: `multimodal_provider`,
   `multimodal_api_url`, `multimodal_model_name`, `multimodal_api_key_set`,
   `multimodal_api_key_masked` e `multimodal_readiness`; e ligar os campos no payload do PATCH,
   no mesmo padrão dos cards de LLM e STT existentes.
4. **Exigir chave nova ao trocar provider ou URL.** A capacidade multimodal já está registrada em
   `_require_fresh_keys` (`src/services/api_credentials.py:200-227`, entrada `multimodal` em
   `:203-207`): um PATCH que muda `multimodal_provider` ou `multimodal_api_url` sem
   `multimodal_api_key` fresca levanta `CredentialPolicyError("fresh_api_key_required")`. O card
   de STT já trata isso limpando o campo (`clinic-config.tsx:1308-1314`); o multimodal precisa do
   mesmo, senão só o primeiro salvamento funciona.
5. Rótulo `RECOMENDÁVEL` no card, via o helper local `txt(pt, en)` (`clinic-config.tsx:468`), que
   é como toda string deste componente é traduzida — ver o badge "Config only" em `:1328`. **Não
   usar `src/locales/pt.ts` / `en.ts`:** o tradutor `t` só é usado por outro componente (`:1602`).
6. Testes de readiness no back e do card no front.

**Acceptance Criteria:**
- [ ] `multimodal_readiness` retorna `active` com credenciais completas de um provider em
      `OPERATIONAL_MULTIMODAL_PROVIDERS`, `incomplete` sem credenciais completas, e
      `configuration_only` para um provider fora daquele conjunto — um caso de teste por ramo.
- [ ] O card multimodal renderiza, aceita as quatro entradas e as persiste via PATCH; após
      remontar e refazer o fetch, os valores salvos aparecem com a chave mascarada.
- [ ] Editar só a URL de um multimodal já salvo exige chave nova e não devolve
      `fresh_api_key_required` ao usuário como erro cru.
- [ ] O campo `multimodal_api_url` é somente leitura e sua URL deriva do provider selecionado,
      trocando junto com ele; nenhuma entrada de usuário consegue enviar um host fora da política.
- [ ] O card exibe `RECOMENDÁVEL` em PT e `RECOMMENDED` em EN.
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

**depends-on:** 2

**ships-with:** 3

A dependência real é só do Grupo 2, que publica `OPERATIONAL_MULTIMODAL_PROVIDERS` e o arquivo de
evidência de compatibilidade — sem eles o gate de readiness do Deliverable 1 não tem sobre o que
decidir. Com o Grupo 3 não há aresta de dependência: os conjuntos de arquivos são disjuntos e nem
um nem outro lê código do outro. O que existe é uma restrição de **release**, registrada em
`ships-with`: soltar o 4 sem o 3 deixaria clínicas configurando um multimodal que nada chama.
Por isso os dois correm em paralelo na Wave 2 e mergeiam juntos.

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
- [ ] Numa clínica **sem** chave de STT e **com** multimodal configurado, enviar um áudio e
      conferir que ele é transcrito pelo multimodal em vez de receber a mensagem de "nunca
      configurado" — é o caminho que a Decisão 14 abre e o que o rótulo `RECOMENDÁVEL` promete.
- [ ] Configurar o multimodal pela aba APIs de ponta a ponta: preencher provider, url, chave e
      modelo, salvar, recarregar e conferir que os valores persistem com a chave mascarada, que o
      rótulo `RECOMENDÁVEL` aparece e que o status fica `Ativo`.
- [ ] Enviar um áudio silencioso ou ininteligível e conferir que o paciente recebe resposta em vez
      de silêncio, nos dois canais.
- [ ] Nas primeiras horas depois do deploy do Grupo 1, acompanhar o volume de chamadas de STT e a
      taxa de `transcription_failed`: o caminho sai de 100% morto para 100% vivo de uma vez.
- [ ] Confirmar que uma clínica em `deepgram_stt` continua sem resposta a áudio — carve-out
      esperado da Decisão 10, não regressão.
- [ ] Enviar pelo Z-API um áudio acima do teto de bytes e conferir que o comportamento é o mesmo
      de hoje — skip silencioso, sem recondução. Carve-out esperado da Decisão 11, não regressão.
- [ ] Gravar uma nota de voz pelo próprio WhatsApp da clínica e conferir que ela não gera custo de
      STT nem resposta da IA.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| A chave de STT e os tokens de envio não chegam ao handler Z-API; todo áudio já sai por `no_whisper_key` e nenhuma mensagem seria entregue | High | Grupo 1, com testes que semeiam credenciais reais e não fazem patch de nenhum dos dois loaders |
| O handoff de áudio longo morre upstream em `if whisper_key:` (`webhook.py:1062`), não no gate de token; corrigir só os tokens daria falsa confiança | Medium | Critério de regressão exige teste entrando pelo webhook fim a fim, não asserção sobre o gate isolado |
| A leitura crua não aplica os defaults de `resolve_stt_credentials`, então a troca não é inerte | Medium | Deliverable 2 do Grupo 1 enumera os três deltas e exige confirmação de cada um |
| Suporte a `input_audio` em chat completions pode variar entre openrouter, groq e openai | Medium | Verificar por provedor no Grupo 2; o degrau de texto absorve a falha, então o pior caso é o comportamento que já teríamos sem multimodal |
| Áudio em base64 dentro de chat completions incha o payload e pode estourar limite de token ou timeout | Low | O download já é limitado por `audio_max_seconds` e teto de 10 MB (`webhook.py:1100-1103`); o corte de áudio longo acontece antes |
| Grupos 1 e 3 reescrevem o mesmo bloco (`webhook.py:1036-1154`): a estrutura `if whisper_key:` que o Grupo 1 corrige é justamente a que o Grupo 3 substitui pela cadeia | Medium | Ordenação por wave já força 1 antes de 3, e o `+1 prior rework` no score do Grupo 1 reflete isso; o Grupo 3 deve partir do estado pós-Grupo 1, nunca do `main` |
| `wish/api-provider-settings` ainda não mergeou; cortar `wish/fallback-texto` de `main` deixa quatro dos arquivos citados inexistentes e invalida todas as citações de linha | High | Regra de branch declarada em Dependencies: cortar do branch do wish anterior, ou de `main` só depois do merge; header fixa `wish/api-provider-settings@7ab570f` como base das citações |
| Se `api-provider-settings` sofrer rebase ou squash antes de mergear, os offsets de linha deste wish deslocam | Medium | As citações vêm sempre acompanhadas do nome do símbolo; ao iniciar cada grupo, confirmar por `grep` do símbolo antes de confiar no número da linha |
| O card multimodal não existe no front, então o degrau multimodal do Grupo 3 fica inalcançável em produção se o Grupo 4 não sair junto | High | Grupo 4 reescrito para criar o card, não só rotular; nota de pré-requisito na Wave 2 e complexity elevada para 3 |
| Depois do Grupo 1, áudio `fromMe` passa a ser baixado e transcrito com custo real de STT, num caminho que hoje nunca é alcançado | Medium | Delta (d) do Deliverable 2 do Grupo 1 obriga a decidir e testar explicitamente, em vez de herdar o comportamento por acidente de ordenação |
| O código a ser alterado acabou de ser entregue pelo Grupo 5 de `api-provider-settings` e carrega invariantes de segurança fechados como CRITICAL | Medium | Decisão 10 e critérios de aceite preservam o gate de provedor não suportado e o invariante de projeção; as suítes daquele wish entram na validação dos Grupos 1 e 3 |
| O Grupo 1 leva o caminho de áudio Z-API de 100% morto a 100% vivo num commit: todo tenant com chave de STT salva passa a gastar STT, turnos de LLM e handoffs de áudio longo no primeiro deploy | High | Acompanhar volume de chamadas de STT e taxa de `transcription_failed` nas primeiras horas (item de QA). O revert é o do commit inteiro, não de uma linha: o hoist do Deliverable 1 e o guard `fromMe` do delta (d) andam junto com a troca de fonte, então o implementador deve manter o Grupo 1 num commit próprio e revertível sozinho |
| A Decisão 14 troca o gate de download do Z-API de `whisper_key` para `whisper_key or multimodal`, criando egress e custo de download que hoje não existem para clínicas sem STT | Medium | O custo é opt-in e só atinge quem configurou multimodal deliberadamente; sem nenhum dos dois, o Deliverable 3 do Grupo 3 exige zero downloads, com AC de asserção de zero chamadas a `safe_download_audio` |
| A Decisão 14 mexe na mesma estrutura `if whisper_key:` (`webhook.py:1062`) que o Grupo 1 acabou de corrigir e de onde dependem o handoff de áudio longo e os tokens de envio | Medium | O Grupo 3 parte do estado pós-Grupo 1 por ordenação de wave. As suítes existentes **não** cobrem o caso perigoso — nenhuma semeia um tenant sem capacidade nenhuma com áudio longo —, então a Decisão 14 (c) manda içar a verificação de duração e um AC novo do Grupo 3 exige exatamente esse teste nos dois canais |
| Redis indisponível faz o aviso repetir | Low | Fallback em memória cobre o worker; o pior caso é um aviso repetido, nunca um áudio silencioso |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan review — 2026-08-10 — **SHIP**

Seven rounds. Reviewer `a5cba74c1e93227be` (round 7), `a6d7d1b31b52ccd48` (round 6),
`a7ac3828e442e19dd` (round 5). Convergência: 22 achados na rodada 3 → 15 na 4 → 10 na 5 → 4 na 6 →
0 bloqueantes na 7.

**Verificado na rodada 7:** o içamento da verificação de duração restaura a paridade e é seguro —
todo o conjunto de entrada de `webhook.py:1066-1095` (`creds` em `:1040`, `audio_obj` em `:1037`,
`agent_lang` em `:1060`, `audio_seconds` do payload) já está ligado fora do ramo de capacidade, e
nada ali lê `whisper_key` nem bytes baixados. A ordem final do Z-API espelha a do Meta. As quatro
correções da rodada 6 estão consistentes nos quatro lugares em que cada uma aparece, sem
contradição nova nas enumerações de ordem.

**Invariantes de segurança confirmados intactos** (rodada 6): o gate de
`UNSUPPORTED_STT_PROVIDERS` (`webhook.py:1048-1049`, `:2448-2450`) permanece acima do novo gate de
capacidade, fixado em cinco pontos do wish; o allowlist de `runtime_projection`
(`api_credentials.py:462-476`) segue proibido de crescer, fixado em quatro.

**Baselines confirmados contra a árvore de trabalho** (rodada 5): `mypy src/api/routes/webhook.py`
reporta exatamente 36 erros; `git rev-list --left-right --count main...wish/api-provider-settings`
devolve `0 7`; o diff de `webhook.py` contra `main` é de 968 linhas.

**Ressalva conhecida, não bloqueante.** O DESIGN.md carimbado afirma em R3 que o wish
`api-provider-settings` está IN_PROGRESS. Ele está `DONE` desde então, embora ainda não mergeado.
A afirmação não pode ser corrigida sem invalidar a evidência de review do design; este wish
carrega o estado correto em Dependencies e nas Risks.

### Execution review — Grupo 1 — 2026-08-10 — **SHIP**

Engenheiro `engineer-standard` (a4606f6b1590bea96); revisores `ad86256d6be7371b4` (base) e
`a29dd66c3d87628df` (delta do fix loop); fixer `accd9ebcf1dac0c42`. Reviewer ≠ engineer em ambas
as rodadas. Task `t_msnw49sv310b625d` marcada `done`.

**Validação, rodada pelo orquestrador.** A suíte dos seis arquivos dá 116 passed / 18 failed;
`mypy src/api/routes/webhook.py --ignore-missing-imports` dá 35 erros, abaixo do baseline de 36.
As 18 falhas são poluição de event loop pré-existente, todas em
`tests/services/test_event_service_config_cache.py`: reproduzem com o arquivo de teste alterado
**excluído** do run e o arquivo passa 18/18 isolado. Não foram introduzidas por este grupo.

**Invariantes de segurança verificados por terceiro.** `runtime_projection` não foi ampliado;
`src/services/api_credentials.py` e `src/services/event_service.py` continuam limpos em
`git status --porcelain`; os quatro testes que fixam o invariante não foram editados. O gate de
`UNSUPPORTED_STT_PROVIDERS` segue pré-download nos dois canais. Nenhum loader de credencial é
mockado nos testes — os três patches de `_load_clinic_config` e o helper `_clinic_config_with_stt`
foram removidos, e `test_stt_key_read_from_row_not_projection` afirma que a chave que chega ao STT
é a mesma semeada na linha real, o que reprova qualquer reintrodução do patch.

**Desvios do plano, aceitos.** (1) Memo preguiçoso em vez de hoist eager: zero leituras a mais
nos caminhos de early-skip e o caminho de texto cai de 2 para 1 leitura crua, colapsando o SQL
inline de `mark_message_read`. (2) Precedência de chave invertida para `stt_api_key` antes de
`whisper_api_key`, alinhada a `resolve_stt_credentials` — contra a linha crua a ordem antiga
mandaria uma chave legada obsoleta ao STT. (3) Áudio `fromMe` passou a ser roteado para
`_handle_clinic_message` com texto vazio, decisão do usuário em 2026-08-10: nota de voz da
secretária agora pausa a IA como já fazia a mensagem de texto, sem download e sem custo de STT.

**Bug de produção não planejado, corrigido.** `_session_has_ai_msg_id` lia `session.metadata`, mas
o atributo mapeado é `session_metadata` (`src/models/session.py:231-233`) — `Session.metadata` é o
`MetaData` do SQLAlchemy, então `dict(...)` levantava `TypeError` sem `except` acima. O revisor do
delta traçou os call sites e concluiu que **todo handoff humano por texto com sessão ativa estava
devolvendo HTTP 500 em produção**. Corrigido no lado da leitura.

**Pendências registradas, nenhuma bloqueia o grupo.** (a) A consulta SQL do critério 6 continua
por rodar em produção — pré-requisito de merge, ver Assumptions / Risks. (b)
`_store_ai_msg_id_to_db` (`webhook.py:465`, `:473`) tem o mesmo bug de atributo no lado da escrita
e é no-op nos nove call sites; o dedup cross-worker via banco nunca gravou nada e Redis mais
memória cobrem o caso hoje. Deixado dormente de propósito: corrigir ativaria comportamento
adormecido no meio do grupo. (c) Comentários em `webhook.py:451`, `:765-767` e `:782-786` afirmam
que esse store é "autoritativo" — hoje é falso e deve ser corrigido junto com (b).

### Execution review — Grupo 2 — 2026-08-10 — **SHIP**

Engenheiro `engineer-standard` (ab3a1a04dc0e4e40e); revisor `a0e2fcfbb0e70dbd2`; fixer
`a8e0d43e0f30b2a91`. Reviewer ≠ engineer. Task `t_msnw4dh6555c7a0f`.

**Validação, rodada pelo orquestrador.** `pytest tests/services/test_safe_egress.py
tests/services/test_multimodal_transcription.py tests/services/test_api_credentials.py -q` dá
**149 passed**, zero falhas; `mypy` nos três arquivos-fonte dá `Success: no issues found in 3
source files`; `black --check` limpo nos dois arquivos novos. Escopo proporcional: o grupo cria um
adaptador isolado e três políticas de egresso, sem tocar caminho de request existente — os testes
de comportamento do adaptador mais a suíte de egresso e a de credenciais cobrem toda a superfície
alterada.

**Compatibilidade de provedor resolvida empiricamente, não por suposição.** Sondas ao vivo,
registradas em `evidence/multimodal-format-probe.md` sem material de chave: Groq **rejeita o bloco
`input_audio` no nível de schema** (`'messages.0.content.1.type' : value is not one of the allowed
values ['text']`) em `openai/gpt-oss-120b`, `groq/compound` e `qwen/qwen3.6-27b` — logo
`ACCEPTED_FORMATS_BY_KIND["multimodal_groq"]` é o conjunto vazio e o adaptador recusa antes de
gastar request. OpenAI rejeita `ogg` por enum e rejeita bytes OGG rotulados `wav` por sniffing.

**Achado decisivo para o Grupo 3.** OpenRouter aceita **OGG/Opus do WhatsApp direto**, sem
transcodificação: `google/gemini-2.5-flash` transcreveu o áudio de teste com `audio_tokens: 125`,
reproduzido em `mistralai/voxtral-small-24b-2507`. **O Grupo 3 não precisa de dependência de
ffmpeg** — o payload do webhook vai cru para o adaptador.

**Correção do fix loop.** A tabela de capacidades declarava, em docstring, formatos "medidos ao
vivo" que nunca foram medidos. Resolvido separando `OPENROUTER_MEASURED_FORMATS` (`ogg`, `wav` —
os efetivamente sondados) de `OPENROUTER_UNVERIFIED_FORMATS` (`mp3`, `m4a`, `aac`, `flac`,
`webm`), com a linha do provedor sendo a união — conjunto resolvido byte-idêntico, comportamento de
`ogg` inalterado. `test_openrouter_row_separates_measured_from_unverified` fixa a partição. Os
não-verificados passam de propósito: o custo de errar é um 400 do provedor que a cadeia de
fallback absorve, enquanto recusar localmente garantiria falha em áudio que provavelmente
funcionaria.

**Invariantes de segurança.** As três políticas novas em `safe_egress.py:345-353` têm host pinado
(`openrouter.ai`, `api.openai.com`, `api.groq.com`); as entradas `stt_*`/`llm_*` existentes estão
byte-idênticas. `multimodal_kind_for_url` faz pertencimento exato de hostname e levanta
`UnsafeEgressError("unsafe_api_url")` fora do conjunto — sem downgrade para `outros`. Testes
provam com `seen == []` que nenhum request escapa nos caminhos de recusa. Nenhuma chave em código,
teste, log ou no documento de evidência (verificado).

**Pendências, nenhuma bloqueia o grupo.** (a) `PROVIDER_ALLOWLISTS["multimodal"]`
(`api_credentials.py:19`) ainda inclui `groq`, agora provado incompatível — a UI do Grupo 4 pode
oferecer um provedor que sempre falha. (b) Promover `multimodal_readiness` para `"active"` depende
do merge de `api-provider-settings`, conforme Dependencies. (c) Ambas as chaves de API fornecidas
em texto plano nesta sessão devem ser rotacionadas.

### Execution review — Grupo 4 — 2026-08-11 — **SHIP** (após um fix loop)

Engenheiro `engineer-standard` (ac2e1251a72c0d2a5); revisor base `a4ccef2bb2a393b5f`; fixer
`a8f215cdfae5be47d`; revisor do delta `a695d9738fc2df251`. Reviewer ≠ engineer ≠ fixer nas duas
rodadas. Task `t_msnw4dpuc2ddfc0e`.

**Validação, rodada pelo orquestrador.** `pytest tests/services/test_api_credentials.py
tests/api/test_api_credentials.py tests/api/test_config.py -q` dá **59 passed**;
`mypy src/services/api_credentials.py` dá `Success`; `pnpm typecheck` limpo; `pnpm test` dá
**39 passed (5 files)**. Escopo: o grupo cruza back e front e altera um contrato de status
consumido pela UI, então roda o gate completo do pacote de front mais as suítes de credenciais.

**Achado HIGH da primeira rodada, corrigido.** O corpo do PATCH omitia `multimodal_model_name`
quando vazio (`if (multimodalModelName) ...`), e `merge_credentials` preserva o que não vem no
patch. Trocar de provider limpa o campo de modelo; se o operador colasse a chave nova e deixasse o
modelo em branco, **o modelo do provider anterior sobrevivia**. O revisor reproduziu contra as
funções reais: provider `openrouter` com modelo `whisper-large-v3`, e `multimodal_readiness`
devolvendo `active` — exatamente o estado "salvo mas silenciosamente inútil" que o gate deste grupo
existe para impedir. Pior que erro de tela: o predicado `_multimodal_operational` do Grupo 3 checa
os mesmos campos, então os dois grupos concordariam na resposta errada.

**Correção estrutural, não guard.** O fixer recusou de propósito a opção de bloquear o save e
mandou `body["multimodal_model_name"] = multimodalModelName || null` incondicionalmente dentro do
gate `multimodalDirty` — um guard vale só no caminho feliz, enquanto um payload que não consegue
carregar valor obsoleto torna o invariante incondicional. Precisa ser `null` e nunca `""`:
`Identifier` tem `min_length=1` (`schemas/config.py:96` é `Identifier | None`), então `""` daria
422 enquanto `None` atravessa o merge e derruba o readiness para um `incomplete` honesto. Rastreado
nas três camadas: `model_dump(exclude_unset=True)` (`routes/config.py:470`) trata `null` explícito
como *set*, e `merge_credentials` (`api_credentials.py:259-265`) só pula vazios de `SECRET_FIELDS`.

**Evidência verificada, não aceita de palavra.** O revisor base checou que
`git diff -- tests/services/test_api_credentials.py` tem **zero linhas de remoção** (um único hunk
aditivo), provando que as asserções do invariante são byte-idênticas em vez de apenas presentes, e
que `tests/services/test_event_service_config_cache.py` não tem diff nenhum. O fixer mutation-testou
a regressão: restaurou a linha antiga, viu falhar com `toHaveProperty` específico, restaurou o fix.
O revisor do delta enumerou as 32 atribuições a `body[...]` e confirmou que `:782` é a única que
pode virar `null`, e traçou os três únicos pontos que marcam `multimodalDirty` — todos gestos
explícitos do usuário, nenhum efeito de montagem —, descartando o bug inverso de apagar
configuração boa.

**Decisão de UI registrada:** `multimodal_api_url` é somente leitura e deriva do provider. O PATCH
lê `MULTIMODAL_PROVIDER_API_URL[multimodalProvider]`, não o DOM, então nenhum estado obsoleto ou
fetch inicial ruim injeta host fora da política.

**Invariantes de segurança.** `runtime_projection` não projeta nenhum campo `multimodal_*` e não
foi ampliado; os quatro testes que fixam segredos fora do Redis seguem intactos; nenhuma chave real
em arquivo, log, fixture ou documento — só `fake-*`/`synthetic-*`.

**Pendências, decisão do usuário, nenhuma bloqueia o grupo.** (a) URL derivada nunca reconciliada
com uma armazenada fora da UI: só alcançável por PATCH direto, e como o card nunca existiu nenhuma
clínica tem esse valor hoje; o efeito seria um 422 surpresa pedindo chave nova. (b) Assimetria na
detecção de máscara — front trata um `•` como obsoleto, back exige quatro (`is_masked_secret`,
`api_credentials.py:157-159`); falha fechado, cosmético. (c) `ApiInput` (`clinic-config.tsx:431-435`)
renderiza `<label>` sem `htmlFor` e não envolve o input, então leitores de tela veem campo de senha
sem rótulo em toda a aba APIs; o conserto é ~4 linhas com `useId()`, mas o componente é
compartilhado por todos os cards e mexer nele sob editores concorrentes arriscava quebrar
`clinic-config-api-settings.test.tsx` e `provider-credential-card.test.tsx`.

---

## Files to Create/Modify

```
src/api/routes/webhook.py                                   # Grupos 1 e 3
src/services/multimodal_service.py                          # Grupo 2 (novo, adaptador multimodal)
src/services/safe_egress.py                                 # Grupo 2 (políticas multimodal_*)
src/services/api_credentials.py                             # Grupo 2 (OPERATIONAL_MULTIMODAL_PROVIDERS), Grupo 4 (multimodal_readiness)
.genie/wishes/fallback-texto/evidence/multimodal-format-probe.md  # Grupo 2 (novo, verificação ao vivo)
frontend/artifacts/neurostack/src/pages/clinic-config.tsx   # Grupo 4
tests/api/test_webhook_egress.py                            # Grupos 1 e 3
tests/api/test_webhook_zapi.py                              # Grupos 1 e 3
tests/api/test_webhook_meta.py                              # Grupo 3 (paridade + fixture autouse nova)
tests/services/test_multimodal_transcription.py             # Grupo 2 (novo)
tests/services/test_safe_egress.py                          # Grupo 2
tests/services/test_api_credentials.py                      # Grupos 2 e 4
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx  # Grupo 4
```
