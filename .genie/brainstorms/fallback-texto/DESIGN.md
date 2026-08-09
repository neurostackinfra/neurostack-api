# Design: Fallback texto — degradação graciosa da transcrição de áudio

| Field | Value |
|-------|-------|
| **Slug** | `fallback-texto` |
| **Date** | 2026-08-09 |
| **WRS** | 100/100 |

## Problem

Quando a transcrição de áudio falha, o webhook encerra em silêncio e o paciente não recebe
resposta alguma — ele fica esperando uma mensagem que nunca chega. O agente deve identificar a
falha, tentar um provedor multimodal alternativo quando houver, e reconduzir a conversa por
texto com uma explicação honesta.

Agrava o problema um defeito de plumbing já presente na branch, restrito ao canal Z-API: nem a
chave de STT nem os tokens de envio chegam ao handler de áudio, então **hoje 100% dos áudios do
Z-API já caem no ramo silencioso e nem sequer conseguiriam responder** (ver R1a e R1b).

## Scope

### IN

- Correção do caminho de credenciais do handler de áudio Z-API: usar `_load_tenant_credentials`
  (`src/api/routes/webhook.py:642`) em vez da projeção sem segredos, cobrindo `stt_api_key`,
  `multimodal_*` **e** `zapi_token`/`zapi_client_token` — sem os tokens de envio, nenhuma
  mensagem de recondução chega ao paciente.
- Adaptador multimodal de transcrição em runtime (não existe hoje), com política de
  safe-egress própria.
- Cadeia de fallback compartilhada: STT principal → multimodal (se configurado) → texto.
- Mensagem de recondução enviada **uma vez por conversa**, com variantes distintas para
  "STT nunca configurado" e "STT configurado mas falhou".
- Paridade entre os dois canais de entrada: Z-API e Meta Cloud API.
- Conversa marcada como não lida após a recondução.
- Rótulo `RECOMENDÁVEL` no card multimodal da aba APIs e promoção de `multimodal_readiness`
  para `active` quando as credenciais estiverem completas.

### OUT

- **Fallback de TTS.** O agente não envia áudio em runtime (`tts_readiness` é
  `configuration_only`, `src/services/api_credentials.py:451`; nenhuma chamada de TTS existe em
  `src/`). Um fallback de "não estou conseguindo enviar áudios" não teria de que degradar e
  seria código morto. Volta quando o envio de áudio existir.
- **Roteamento automático em falha do provedor de LLM de texto.** `src/llm/client.py` é de
  provedor único; construir uma cadeia de fallback ali é outro subsistema. Wish separado.
- Adapters STT de ElevenLabs, Google, Deepgram e Microsoft — seguem em
  `UNSUPPORTED_STT_PROVIDERS` (`src/api/routes/webhook.py:59`) e mantêm o envelope de skip atual.
- Circuit breaker, retry com backoff ou fila de reprocessamento de áudio.

## Approach

**Uma cadeia de degradação em três degraus, atrás de um helper único usado pelos dois canais.**

1. **Reaproveitar o loader de credenciais que já existe.** O handler de áudio Z-API
   (`webhook.py:1036-1154`) passa a ler de `_load_tenant_credentials(tenant_id, db)`
   (`webhook.py:642-651`) — uma leitura crua do banco, sem projeção. Nada novo é escrito: essa
   função já é usada em seis pontos (`:1227`, `:1299`, `:1464`, `:1529`, `:1730`, `:2360`),
   inclusive pelo handler de áudio do Meta, e já é chamada dentro da mesma função que contém o
   bloco de áudio, logo adiante em `:1299`. O `db` está em escopo como parâmetro de
   `_process_zapi_authenticated_body` (`webhook.py:1004-1009`, `db` em `:1008`). O allowlist de
   `runtime_projection` fica intocado, então nenhum segredo entra no Redis.

   **Delta de normalização a tratar na implementação.** A projeção roda
   `resolve_stt_credentials` (`api_credentials.py:265-289`) antes de projetar, aplicando
   defaults de `stt_provider` → `"outros"` e `audio_max_seconds` → 60. A leitura crua não faz
   isso. Os fallbacks inline em `webhook.py:1041`, `:1056-1058` e `:1068` já cobrem esses
   campos, e por isso o gate de provedor não suportado (`:1048-1049`) continua válido — mas a
   troca **não** é inerte, e a implementação precisa confirmar cada default em vez de assumir.

2. **Adaptador multimodal.** `transcribe_audio_multimodal()` posta em `{base}/chat/completions`
   com blocos de conteúdo `input_audio` no formato OpenAI, que openrouter, groq e openai
   aceitam por serem OpenAI-compatíveis. Novas políticas `multimodal_*` em `PROVIDER_POLICIES`
   (`src/services/safe_egress.py:328-339`), seguindo o padrão de host pinado para provedores
   conhecidos e política aberta para custom.

3. **Helper de cadeia compartilhado.** Uma função que recebe os bytes de áudio e as credenciais
   e devolve `(texto, motivo_da_falha)`. Chamada tanto do caminho Z-API
   (`webhook.py:1036-1154`) quanto do Meta (`webhook.py:2500-2551`), eliminando a divergência
   de comportamento entre canais.

**Assimetria entre canais, verificada.** O Meta já lê credenciais cruas em `:2360` e por isso
seu caminho de áudio funciona hoje; o defeito de R1a e R1b é exclusivo do Z-API, que lê da projeção. O
Meta é a prova de que a forma da correção está certa — é literalmente o que o outro canal já
faz. Consequência para os testes: o critério que prova a chegada da chave ao runtime tem de
mirar o **Z-API**, porque um teste equivalente no Meta passaria hoje e não provaria nada.

**Alternativas descartadas.** Afrouxar o allowlist de `runtime_projection` para incluir as
chaves resolveria R1a e R1b em uma linha, mas colocaria segredos no Redis e quebraria o invariante que
quatro testes protegem — perdeu por custo de segurança. Criar um loader novo em
`event_service.py`, espelhando `_load_runtime_llm_credentials` (`:361`), perdeu por duplicação:
`_load_tenant_credentials` já resolve a chave de STT e os tokens de envio de uma vez, enquanto
um loader só de áudio deixaria o segundo buraco aberto. Tratar multimodal como substituto do STT
em vez de fallback perdeu porque desperdiça o STT dedicado, mais barato e mais preciso para
transcrição pura. Duplicar a cadeia nos dois handlers perdeu por R6.

### Design for Isolation

- A leitura de credenciais tem um propósito e já é isolada: `_load_tenant_credentials` entrega o
  registro cru e não sabe nada de transcrição.
- O adaptador multimodal tem um propósito: bytes de áudio para texto via chat completions. Não
  sabe nada de webhook, sessão ou WhatsApp.
- O helper de cadeia orquestra e não fala HTTP diretamente.
- A recondução por texto depende só de `(idioma, motivo)`, testável sem rede.

## Simplicity Case

- **Simplest complete design:** um `try/except` em cada handler que, ao falhar, manda uma
  mensagem de texto. Isso não basta por três razões presentes e verificadas: o usuário pediu
  explicitamente o degrau multimodal; no Z-API nem a chave de STT nem os tokens de envio chegam
  ao handler, então a mensagem dispararia sempre e ainda assim não seria entregue (R1a e R1b); e os
  dois handlers divergiriam (R6).
- **Added machinery:**
  - *Correção de credenciais* — **nenhuma máquina nova**: troca de fonte para uma função que já
    existe e já é usada em seis pontos. Paga por R1a e R1b, sem as quais a funcionalidade nasce
    quebrada.
  - *Adaptador multimodal* — pago por requisito explícito do usuário ("modelo multimodal de
    fallback... será feito roteamento automático").
  - *Políticas de safe-egress* — pagas pela arquitetura existente: toda chamada de saída passa
    por `validate_url(kind=...)` e um `kind` desconhecido levanta exceção.
  - *Chave de aviso com TTL* — paga por rajadas de áudio: sem ela, cinco áudios seguidos geram
    cinco avisos idênticos. Usa o padrão de Redis com fallback em memória que já existe no mesmo
    arquivo (`webhook.py:123-180`), sem estado durável novo nem migração.
- **Deferred until measured:** circuit breaker por clínica (só quando houver medição de falhas
  de STT repetidas por tenant); um terceiro degrau de STT; toggle de configuração para
  ligar/desligar o fallback — o padrão opinativo é sempre reconduzir.
- **Complexity removed:** nenhum retry com backoff, nenhuma fila de reprocessamento, nenhum
  estado durável novo em Postgres, nenhum caminho de TTS dormente, nenhuma flag por tenant, e
  nenhum loader de credenciais novo — o design anterior propunha um, e o review mostrou que
  `_load_tenant_credentials` já cobria o caso com alcance maior.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Corrigir a chegada de credenciais ao handler de áudio Z-API dentro deste wish | Sem isso o fallback dispara para 100% dos áudios e, pior, não consegue nem enviar a mensagem por falta de `zapi_token` (R1a e R1b) |
| 2 | Reusar `_load_tenant_credentials` (`webhook.py:642`) em vez de criar loader novo ou ampliar o allowlist do cache | Mantém segredos fora do Redis, preserva o invariante testado em quatro lugares e resolve chave de STT e tokens de envio de uma só vez; é o que o canal Meta já faz |
| 3 | Ordem STT → multimodal → texto | STT dedicado é mais barato e preciso; multimodal é a rede de segurança, não o padrão |
| 4 | Multimodal opcional, rotulado `RECOMENDÁVEL` na UI | Pedido do usuário; clínicas sem multimodal continuam funcionando com o degrau de texto |
| 5 | Texto é sempre o último degrau, inclusive se o multimodal falhar | Garante que nenhum áudio termine em silêncio, qualquer que seja a combinação de falhas |
| 6 | Mensagens distintas para "nunca configurado" e "falhou agora" | Dizer "no momento não estou conseguindo" para uma clínica que nunca configurou STT é falso e gera expectativa errada |
| 7 | Aviso uma vez por conversa, via chave Redis com TTL e fallback em memória | Evita spam em rajada; reaproveita o padrão de dedup já presente no arquivo |
| 8 | Marcar conversa como não lida, sem handoff nem pausar a IA | Espelha o fluxo de áudio longo (`webhook.py:1086`); a clínica enxerga, e a IA segue atendendo por texto |
| 9 | Helper de cadeia único para Z-API e Meta | R6: a duplicação atual já causou divergência entre canais — o Meta funciona e o Z-API está quebrado |
| 10 | Providers STT sem adaptador continuam saindo antes de qualquer download | Preserva o invariante de segurança de `webhook.py:1048-1049`, que o wish em andamento fechou como CRITICAL |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1a | A chave de STT não chega ao handler Z-API: `runtime_projection` (`api_credentials.py:462-476`) não inclui `stt_api_key`, mas o handler a lê da projeção (`webhook.py:1040-1041`); todo áudio já sai por `no_whisper_key`. Os testes mascaram com patch de `_load_clinic_config` (`test_webhook_egress.py:114-119`, `WISH.md:1431`) | High | Decisão 1 e 2, e teste que **não** faz patch do loader de config |
| 1b | **Os tokens de envio também são estripados.** A projeção inclui `zapi_instance_id` mas não `zapi_token` nem `zapi_client_token`, e todo envio no bloco de áudio é gateado por `if z_inst and z_tok:` (`webhook.py:1071`). Sem corrigir, os critérios de recondução são inalcançáveis: a mensagem seria escolhida e nunca entregue | High | A mesma troca para `_load_tenant_credentials` cobre os dois; critério explícito de **entrega**, afirmando sobre a chamada de envio |
| 1c | Segundo sintoma já em produção: o handoff de áudio longo (`webhook.py:1082-1088`) também não envia nada hoje. **Cuidado com a causa:** ele morre antes, em `if whisper_key:` (`:1062`), então `:1070` nunca é alcançado — não é apenas o gate de token. Corrigir só os tokens não ressuscita esse caminho e daria falsa confiança no critério de regressão | Medium | A troca de fonte de credenciais cura os dois de uma vez, mas o teste de regressão precisa exercitar o caminho inteiro a partir do webhook, não afirmar sobre o gate isolado |
| 2 | Segredos multimodais também estão fora da projeção, com testes fixando o invariante (`tests/services/test_api_credentials.py:220`, `:440`, `:505`; `tests/services/test_event_service_config_cache.py:275`) | Medium | A mesma leitura crua de R1a atende multimodal; o allowlist do cache não é tocado e os testes seguem passando |
| 3 | Ownership compartilhado com o wish `api-provider-settings`, IN_PROGRESS, dono de `api_credentials.py`; seu critério 12 diz que provider config-only nunca fica `Ativo` | Low | Verificado que não há conflito real: os quatro testes de invariante afirmam sobre a **projeção**, que readiness não toca, e nenhum teste fixa `multimodal_readiness` hoje. O próprio critério 12 se justifica por "credencial salva nao promete adapter" (`WISH.md:109`) — satisfeito quando o adaptador existe. Resta só coordenar o merge |
| 4 | Suporte a `input_audio` em chat completions pode variar entre openrouter, groq e openai | Medium | Verificar por provedor na implementação; o degrau de texto (Decisão 5) absorve a falha, então o pior caso é o comportamento que já teríamos sem multimodal |
| 5 | Áudio em base64 dentro de chat completions incha o payload e pode estourar limites de token ou timeout | Low | O download já é limitado por `audio_max_seconds` e teto de 10 MB (`webhook.py:1100-1103`); o corte de áudio longo acontece antes, em `:1070` |
| 6 | Divergência entre Z-API e Meta se a cadeia for aplicada em só um caminho. Os canais já divergem hoje: o Meta lê credenciais cruas em `:2360` e funciona, o Z-API lê da projeção e está quebrado | Medium | Decisão 9: helper único, com teste de paridade cobrindo os dois canais |
| 7 | Assume-se que o aviso "uma vez por conversa" pode viver em Redis com TTL, sem estado durável | Low | Se o Redis cair, o fallback em memória cobre o worker; o pior caso é um aviso repetido, não um áudio silencioso |

## Success Criteria

- [ ] Com STT configurado e funcionando, o áudio é transcrito e respondido normalmente — sem regressão.
- [ ] Com STT configurado que lança exceção e sem multimodal, o paciente recebe a mensagem de
      falha temporária e a conversa é marcada como não lida; a resposta do webhook deixa de ser
      um skip silencioso.
- [ ] Com STT falhando e multimodal configurado, a transcrição multimodal é tentada e, quando
      bem-sucedida, a conversa segue normalmente sem nenhuma mensagem de recondução.
- [ ] Com STT e multimodal falhando, o paciente recebe a mensagem de recondução por texto.
- [ ] Sem nenhum STT configurado, o paciente recebe a variante "nunca configurado", distinta da
      mensagem de falha temporária.
- [ ] Cinco áudios seguidos com STT quebrado produzem exatamente uma mensagem de recondução.
- [ ] Os quatro provedores em `UNSUPPORTED_STT_PROVIDERS` continuam saindo com o envelope de
      skip antes de qualquer download, sem disparar a cadeia de fallback.
- [ ] Existe um teste do caminho de áudio **do Z-API** que **não** faz patch de
      `_load_clinic_config` e ainda assim alcança a transcrição, provando que a chave chega ao
      handler. Tem de ser o Z-API: o mesmo teste no Meta passaria hoje e não provaria nada.
- [ ] Cada mensagem de recondução é verificada como **entregue**, afirmando sobre a chamada de
      envio, e não apenas escolhida — sem `zapi_token` a mensagem seria montada e descartada.
- [ ] O handoff de áudio longo (`webhook.py:1082-1088`) passa a enviar de fato a mensagem e a
      marcar a conversa como não lida. O teste entra pelo webhook com um áudio acima de
      `audio_max_seconds` e sem injetar credenciais por patch, provando que o caminho vive
      desde `if whisper_key:` (`:1062`) até o envio — afirmar sobre o gate de token isolado
      passaria sem que o caminho funcione (R1c).
- [ ] `runtime_projection` continua sem expor `stt_api_key` nem campos `multimodal_*`, e os
      testes de invariante de cache seguem passando sem alteração.
- [ ] Z-API e Meta Cloud API produzem o mesmo comportamento de fallback para o mesmo cenário.
- [ ] As mensagens existem em PT e EN, respeitando `_get_agent_language`.
- [ ] O card multimodal exibe `RECOMENDÁVEL` e `multimodal_readiness` retorna `active` com
      credenciais completas.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `06a2947e6751d543963890fe8bc59ded48805c0d3bb34a0999e9c341e07284b8`
- **Reviewer:** afda3526ba112e70a
- **Reviewed at:** 2026-08-09T19:43:55.000Z
<!-- genie-design-review:end -->
