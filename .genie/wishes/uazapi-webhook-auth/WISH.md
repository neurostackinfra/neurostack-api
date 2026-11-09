# Wish: Autenticação do webhook da uazapi pelo token da instância

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `uazapi-webhook-auth` |
| **Date** | 2026-11-09 |
| **Author** | Claude (orquestrador), a pedido do owner |
| **Appetite** | medium |
| **Branch** | `wish/uazapi-webhook-auth` |
| **Repos touched** | `neurostack-api` |
| **Design** | _No brainstorm — direct wish_ |

> **Por que sem brainstorm.** As decisões de desenho foram tomadas pelo owner em 2026-11-09. A primeira foi "Wish novo: sombra, depois aplicar". A segunda, depois da revisão do plano, foi "Medir agora, cobrar antes do L10". A evidência da uazapi (G5 do `cloud-readiness`) e um levantamento de fatos em `origin/main` `83d7094f` substituem a exploração.

## Summary

O webhook da uazapi (`POST /v1/webhook/uazapi`) aceita qualquer chamada que traga um `instanceName` conhecido. Por isso os turnos da clínica entram com origem `UNKNOWN`: não recebem mídia do agente e são medidos como tráfego não autenticado.

Este wish autentica o webhook pelo `token` da instância. Segundo a documentação da uazapi 2.6.0, o envelope de todo callback traz esse token, e a uazapi orienta validá-lo. A igualdade com o token gravado é justamente o que a fase de sombra testa.

O código é um só, com dois modos escolhidos pela flag AppConfig nova `uazapi_webhook_auth_enforced`:
- **Sombra** (flag ausente ou `false`): só mede, e o comportamento continua igual ao de hoje.
- **Aplicação** (flag `true`): recusa o webhook com 401 antes de qualquer efeito quando a verificação falha. Quando passa, o turno fica `PRODUCTION` e o principal de crédito conta como verificado. Com isso a clínica recebe mídia e é **medida** como tráfego autenticado.

A cobrança enforced da uazapi fica para um wish de seguimento, obrigatório antes do L10. Este wish deixa uma guarda para que o L10 não seja armado sem ela.

## Scope

### IN

- **Verificação no `uazapi_webhook`.** A verificação acontece depois da interpretação mínima do corpo que já existe e **antes** do claim do Redis. Ela usa uma leitura da flag por request, feita antes da consulta da instância. Um único resultado dessa verificação decide quatro coisas: o `mode` do log, o 401, a origem e o principal de crédito.
- **Resultados possíveis:** `match`, `mismatch`, `token_missing`, `token_not_configured`, `unknown_instance`, `ambiguous_instance`.
- **Semântica da comparação:**
  - o `token` do envelope que não for `str`, ou ficar vazio depois de `strip()`, conta como `token_missing`;
  - o `uazapi_token` gravado ausente, ou vazio depois de `strip()`, conta como `token_not_configured` e nunca vira `match`;
  - a comparação é feita com `hmac.compare_digest` sobre `.strip().encode("utf-8")` dos dois lados, com o mesmo strip do envio (`webhook.py:4740`, `:4912`);
  - tipo inesperado ou texto não ASCII nunca levantam exceção.
- **Resolução da instância.** Uma consulta exclusiva da uazapi lê **todas** as linhas candidatas com aquele `uazapi_instance_name` (`tenant_id`, `uazapi_token`, `uazapi_api_url`, `whatsapp_provider`) e calcula o resultado sempre com a mesma regra:
  - 0 candidatas: `unknown_instance`.
  - Exatamente 1 candidata cujo token gravado (não vazio) é igual: `match`.
  - 2 ou mais candidatas iguais: `ambiguous_instance`.
  - Nenhuma igual:
    - o token do envelope está ausente ou inválido: `token_missing`;
    - senão, todas as candidatas têm token gravado vazio: `token_not_configured`;
    - senão: `mismatch`.
- **Roteamento por modo:**
  - **Aplicação:** roteia para a única candidata igual. Qualquer outro resultado dá 401.
  - **Sombra:** roteia **pelo `_resolve_tenant_by_credential` intocado**, exatamente como hoje. A consulta de candidatas serve só para calcular o resultado e `routed_matches` (se a linha roteada é a candidata igual). Assim o roteamento atual e os testes que fazem patch nesse resolvedor (`tests/integration/test_conversation_language_postgres.py:529-545`) continuam valendo.
  - `_resolve_tenant_by_credential` não muda, e a Evolution também não.
- **Principal de crédito verificado (opção C da revisão).**
  - Entra um campo explícito na identidade, `inbound_verified: bool`, no `credit_identity("uazapi", …)` usado por `uazapi_ingress` (`credit_metering.py:515-529`).
  - `derive_credit_principal` (`credit_principal.py:129`) passa a tratar `uazapi` com `inbound_verified=True` como autenticado.
  - `inbound_verified=True` só é aceito para o kind `uazapi`. Uma regra de validação recusa esse campo em qualquer outro kind.
  - O `principal_id` continua `uazapi:{tenant}`. A chave do claim continua `(tenant_id, "uazapi", messageid)`.
  - Na sombra, a identidade é a de hoje: `inbound_verified=False`, o claim continua sendo gravado e a linha observacional também.
- **Medição.** O turno verificado entra na medição observacional como tráfego autenticado (`would_debit_mc`, `credit_metering.py:343`). Nada é cobrado enquanto o enforcement não estiver armado. Isso vale para todas as clínicas.
- **Guarda do L10: recusa dura incondicional.** `arm_enforcement` (`credit_rollout.py`, ~`:1612`) **sempre** recusa armar até o wish `uazapi-credit-enforcement` entrar e retirar essa recusa.
  - O check fica antes de `reconcile_orphans()` e também aparece na saída de `status`, para o operador não ver "pronto" e depois tomar um `refused`.
  - O motivo novo entra em `REJECTION_REASONS` (`src/ops/credit_enforcement.py:156-180`) **e** em `REASONS` (`:181-200`). Sem isso o CLI quebra com `ValueError` na hora da recusa (`:252-254`).
  - Esse motivo **nunca** entra em `CUTOFF_REASON_CODES` (`:146-153`), que são os achados que o operador pode aceitar. Por isso o `accept_cutoff` não consegue liberar a armação.
  - O CLI responde `refused` (exit 20, `:709-719`).
  - Ser incondicional fecha o buraco depois de armar: uma clínica uazapi cadastrada depois do L10 ficaria sem cobrança e sem registro.
  - **Ponto de troca só para testes.** A recusa depende de um predicado de módulo, `uazapi_enforced_credit_adapter_available()`, que devolve sempre `False`. Ele não lê env var, config, flag nem banco. Só os testes que provam a armação com sucesso trocam o predicado por um fixture explícito, e hoje são cerca de 60 chamadas em `tests/ops/test_credit_enforcement.py`, `tests/services/test_credit_rollout.py` e três suítes Postgres. Um teste prova que, sem a troca, a armação recusa. Outro prova que nenhum caminho de produção chama o fixture. O `uazapi-credit-enforcement` remove o predicado.
- **Modo aplicação.**
  - Qualquer resultado diferente de `match` devolve 401 `{"error":"invalid_webhook_auth"}`. Nada é criado antes disso: nem claim, nem sessão, nem evento.
  - `match` define `origin = InteractionOrigin.PRODUCTION` e `inbound_verified=True`.
- **Contador `neurostack_uazapi_webhook_auth_total{mode,result}`.** Vocabulário fechado e sem rótulo de instância. Serve para alarmes. O critério de aplicar (Decisão 9) é julgado pelo **log**, no CloudWatch, porque o contador é por processo.
- **Log `uazapi_webhook_auth`.** Campos: `mode`, `result`, `instance`, `token_present`, `routed_matches`, `provider_matches` (`whatsapp_provider == "uazapi"`), `owner_present`, `from_me` e `base_url_matches` (`BaseUrl` do envelope contra `uazapi_api_url`, normalizados). **Nenhum** valor ou derivado do token entra, nem como hash.
- **Flag `uazapi_webhook_auth_enforced`.**
  - Fica em `SUPPORTED_FLAGS`. Ausente, ilegível ou com poll falho vale `false`, que é sombra.
  - Aqui "fail-closed" significa **sem autenticação**. O rótulo `mode` deixa isso visível.
  - Entra em `_log_transition` (`appconfig.py:385-407`), para que a verificação por worker veja o valor.
- **`media_delivery_provider_ready`.** A função continua pura e síncrona, e recebe as flags como parâmetros (`uazapi_auth_enforced`, `uazapi_egress_enabled`). O chamador, `media_assets.py:885-889`, lê as flags depois do `_rollback`. A uazapi fica pronta só com credenciais completas, autenticação aplicada **e** egress da uazapi aberto. O teste de matriz e o alarme de drift em `tests/api/test_media_assets.py:1400-1450` são atualizados.
- **Comentários e docstrings.** Atualizar `credit_principal.py:18-25`, `api_credentials.py:864-874`, `credit_metering.py:14-21`, `:143-145`, `:515-518` e `:2109-2110`, `media_send_body.py:81`, e a docstring da rota em `webhook.py:4494-4505`.
- **Runbook novo `docs/runbooks/uazapi-webhook-auth.md`**, mais a atualização dos docs desatualizados listados no Group 2.

### OUT

- **Cobrança enforced da uazapi:** precheck, sender enforced e entrega cobrada. Vai para o wish de seguimento `uazapi-credit-enforcement`, obrigatório antes do L10. **Efeito colateral aceito:** a recusa dura e incondicional deixa o L10 do `neurostack-credit-system` bloqueado até esse seguimento entrar, o que é ao pé da letra "cobrar antes do L10".
- **Rótulo `unknown_unauthenticated_traffic` no cutoff** (`credit_rollout.py:110`, `:861-874`). A mudança fica para o `uazapi-credit-enforcement`. Enquanto a recusa incondicional estiver valendo, o rótulo não afeta a armação.
- **Evolution.** Fica como está, com origem `UNKNOWN`.
- **Rate limit no webhook da uazapi.** Gatilho no Simplicity Case.
- **Período de graça na rotação**, ou seja, aceitar o token anterior. A rotação de qualquer forma quebra o envio. O procedimento padrão é rotacionar com a autenticação em sombra.
- **Unicidade de `uazapi_instance_name` no banco.** A verificação por token já protege contra o squat. O runbook ganha um pré-check de duplicatas.
- **Gravar o token como digest.** Ele também é a credencial de envio.
- **Infraestrutura de receipts e cosign** do `cloud-readiness`. Este wish a substitui para a uazapi.
- **Painel e frontend.**
- **Abrir qualquer flag em produção sem o "vai" do owner.** Deploy e escrita na AWS só acontecem com autorização explícita.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Verificar o `token` do envelope comparando-o com o `uazapi_token` gravado. A igualdade dos dois é **hipótese** a confirmar na sombra. | Base: o envelope traz o campo, como mostra a fixture real redigida (`tests/fixtures/uazapi/media-payload.json:124`), e os resumos da documentação 2.6.0 orientam validá-lo. A evidência continua `claim: unproven`, porque os resumos são paráfrases e o valor está redigido. A sombra mede a igualdade antes de qualquer aplicação. |
| 2 | Sombra e aplicação no mesmo código, com o modo dado pela flag AppConfig `uazapi_webhook_auth_enforced`. A flag é lida **uma vez** por request, antes da consulta da instância. | Um deploy só. Na sombra nada muda. Aplicar e voltar atrás não exige imagem nova: os workers releem em ≤60 s, ou 180 s no pior caso. Ler uma vez evita incoerência entre o log, o 401, a origem e o principal. No `sdk` de produção a leitura é em memória (`appconfig.py:417-426`). |
| 3 | Em aplicação, qualquer resultado diferente de `match` dá 401 `invalid_webhook_auth` antes do claim e de qualquer efeito. | Mesmo código de resposta da Z-API e mesmo princípio fixado em `tests/api/test_webhook_auth_order.py`, que é autenticar antes do claim. Diferença assumida: a Z-API autentica antes de interpretar o corpo, e a uazapi depois dos retornos antigos (`invalid_data`, `missing_fields`, `group_chat`). Esses retornos não têm efeito e só geram log (`webhook.py:4518`, `:4566`, `:4570`). |
| 4 | **Medir agora, cobrar antes do L10.** `match` em aplicação marca o principal `uazapi` como verificado (`inbound_verified=True`). O turno é medido como autenticado, e a cobrança enforced vai para `uazapi-credit-enforcement`. Uma recusa dura **incondicional** em `arm_enforcement`, que não pode ser aceita, bloqueia armar o L10 até o seguimento entrar. Isso fecha também o caso de uma clínica uazapi cadastrada depois de armar. | Decisões do owner em 2026-11-09: primeiro "Sim, medir e cobrar", depois, na revisão, "Medir agora, cobrar antes do L10". Hoje só Z-API e Meta têm caminho enforced (`credit_metering.py:1952`, `:2088-2212`; `webhook.py:587-588`). Antes do L10 nenhuma clínica é cobrada (`credit-enforcement-arming.md:40-41`), então nada se perde agora. Sem a guarda, depois de armar, o turno da uazapi seria servido sem cobrança e sem registro (`credit_metering.py:770-771`). |
| 5 | O principal verificado usa um **campo explícito** (`inbound_verified`) na identidade (opção C), e não tira `uazapi` de `_UNAUTHENTICATED_KINDS` nem cria um kind novo. | A opção A autenticaria também a sombra, e retirar a identidade apagaria o claim observacional de hoje (`credit_metering.py:621-640`). A opção B mudaria o `principal_id`, as chaves e os prefixos. Na opção C, o `principal_id`, a chave do claim e a sombra ficam como estão. |
| 6 | Na aplicação, aceitar só quando **exatamente uma** linha candidata tem token igual. Na sombra, rotear pelo resolvedor intocado, como hoje. A consulta de candidatas só calcula o resultado. | Nada garante que `uazapi_instance_name` seja único (`config.py:888-905` só protege a Meta), e qualquer tenant pode gravar o nome (`docs/configuration.md:797`). Exigir uma linha única por nome deixaria um posseiro bloquear a única clínica de produção. Com a verificação por token, quem não tem o token não consegue nem sequestrar nem bloquear. |
| 7 | Semântica da comparação: tipo não `str` ou vazio vira `token_missing`; gravado vazio vira `token_not_configured`; a comparação é em bytes UTF-8 depois de `strip()`; nada levanta exceção. | `compare_digest("","")` é `True`, e texto não ASCII levanta `TypeError` (medido). O token gravado não passa por `strip` (`schemas/config.py:60`), mas o envio faz `strip`. |
| 8 | O token nunca vai para log, métrica, resposta, exceção ou evidência, nem sob a forma de hash. | Ele é a credencial de envio da clínica. |
| 9 | **Critério para aplicar**, um passo do owner julgado pelo log no CloudWatch (`uazapi_webhook_auth`, filtrado por `instance` e `mode=shadow`). "Autenticável" quer dizer evento que chegou à verificação, sem retorno antigo. O critério:<ul><li>pelo menos 50 eventos autenticáveis em pelo menos 48 h;</li><li>100% `match`, sem nenhum `mismatch`, `token_missing` ou `token_not_configured`;</li><li>`routed_matches` sempre `true`;</li><li>pelo menos uma rotação deliberada (`rotateInstanceToken`) observada com a autenticação em sombra, na sequência `match`, `mismatch` e de novo `match` depois de atualizar o painel.</li></ul>O owner pode dispensar o critério, e a dispensa fica registrada. | Isso fecha as duas razões de `capability_unproven` com dados reais. O contador é por processo (`asaas-billing-reconciliation.md:89`, `:136-137`) e por isso não serve como critério. |
| 10 | **Ordem de abertura** (Decisão 12 do `envio-midia-agente`):<ol><li>pré-check de duplicatas de instância;</li><li>pré-check de crédito da clínica, só leitura, com o owner ciente do modo e do saldo;</li><li>fechar `agent_media_send_enabled`;</li><li>abrir `uazapi_webhook_auth_enforced`;</li><li>verificar por worker;</li><li>ler `match` e `PRODUCTION`;</li><li>pré-check SQL de mídia com as contagens da uazapi;</li><li>"vai" do owner para reabrir a mídia;</li><li>QA ao vivo em instâncias de teste, sem nunca abrir a flag de produção "só para QA".</li></ol> | A seleção de mídia exige só origem `PRODUCTION` (`agent-media-delivery.md:731-742`, "before **every** open"). |
| 11 | Toda versão hospedada do AppConfig, a partir da aplicação, precisa levar `uazapi_webhook_auth_enforced: true`. O kill switch da mídia passa a falar em quatro flags. | O sentido aqui é invertido: `false` quer dizer sem autenticação. Implantar uma versão antiga ou o template "fechado" desliga a aplicação sem ninguém perceber. |

## Simplicity Case

- **Design mais simples completo:** uma comparação no handler que já existe, um booleano de modo vindo do AppConfig que já existe e um campo booleano na identidade de crédito. Sem tabela nova e sem migration.
- **Mecanismo acrescentado:**
  - **Flag AppConfig.** Permite voltar atrás sem deploy de algo que pode bloquear a única clínica de produção, e põe a sombra e a aplicação numa release só.
  - **Contador.** Não existe métrica de autenticação de webhook. Ele serve para alarme.
  - **Resolvedor de candidatos.** Necessário porque o nome da instância não é único (Decisão 6).
  - **Campo `inbound_verified`.** É a única forma de autenticar só o turno verificado sem mudar a sombra (Decisão 5).
  - **Guarda do cutoff.** Sem ela, armar o L10 deixaria a clínica de graça sem ninguém ver (Decisão 4).
- **Adiado até haver medida:**
  - **Período de graça na rotação.** Gatilho: uma rotação real que gere 401 por mais de 5 min, ou mais de uma rotação por mês.
  - **Rate limit.** Gatilho: abuso observado, ou o caminho enforced da uazapi entrando.
  - **Evolution.** Gatilho: a primeira clínica de produção nela.
  - **Unicidade da instância no banco.** Gatilho: uma duplicata real no pré-check.
- **Complexidade evitada:** receipts e cosign, digest do token, credencial dupla, mudança de schema, kind novo de principal e adaptador enforced neste wish.

## Dependencies

**depends-on:** envio-midia-agente
**blocks:** none

A dependência é de procedimento: a flag `agent_media_send_enabled` e a Decisão 12. O código do `envio-midia-agente` já está na main (`83d7094f`).

O seguimento `uazapi-credit-enforcement` depende deste wish e precisa entrar antes do L10 do `neurostack-credit-system`.

## Success Criteria

- [ ] **Sombra.** Com a flag ausente ou `false`, a resposta, a origem (`UNKNOWN`), o roteamento (`.first()`), o claim, a linha observacional e a ordem dos efeitos ficam iguais aos de hoje. As únicas mudanças são o log, o contador com `mode="shadow"` e a linha de trace da verificação em `test_webhook_auth_order.py`, que muda por construção.
- [ ] **Resultados.** Os seis resultados são produzidos e testados nos dois modos. Há casos explícitos para token não ASCII, `int`, `dict`, vazio contra vazio, espaço nas pontas (strip) e duplicata com um só token igual.
- [ ] **Token fora de toda saída.** Um teste injeta um token sentinela e confirma que ele não aparece em log, métrica, resposta nem exceção.
- [ ] **Aplicação, recusa.** Com a flag `true`, todo resultado diferente de `match` dá 401 sem claim, sessão nem evento.
- [ ] **Aplicação, `match`.** Com a flag `true`, `match` dá origem `PRODUCTION`, principal autenticado com `principal_id` `uazapi:{tenant}` e a mesma chave de claim. A medição observacional registra o turno como autenticado (`would_debit_mc` > 0, provado em Postgres).
- [ ] **Guarda do L10.** `arm_enforcement` sempre recusa, com o motivo novo em `REJECTION_REASONS` e `REASONS` e fora de `CUTOFF_REASON_CODES`. O check roda antes de `reconcile_orphans()`, aparece no `status` e não pode ser liberado pelo `accept_cutoff`. Há testes em unidade, no CLI (`refused`, exit 20) e em Postgres.
- [ ] **Flag.** A flag é lida uma vez por request, fora de transação. AppConfig ilegível ou poll falho vale sombra, e `_log_transition` registra a flag.
- [ ] **Prontidão.** `media_delivery_provider_ready` fica `True` para a uazapi só com credenciais completas, autenticação aplicada e egress da uazapi aberto. A função continua pura.
- [ ] **Docs.** O runbook e os docs atualizados cobrem as Decisões 9, 10 e 11, a rotação, o rollback, o pré-check de duplicatas e o de crédito, e a mudança de rótulo de origem. A prova mecânica passa.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 8: roteamento e ingresso (+2), custo e crédito (+2), estado (claims e efeitos, +2), rótulo de métrica (+1), retrabalho anterior (G5/G6 do cloud-readiness, +1) | `engineer-complex` / high, mais `final-gate` independente | Verificação por token com sombra e aplicação, principal verificado, guarda do L10 e prontidão de mídia |

### Wave 2 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 1: documentação com prova mecânica (+1) | `engineer-standard` / medium | Runbook e atualização dos docs desatualizados |

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** →
`engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high;
**4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an
independent `final-gate` at the highest justified effort. Codex maps these to
the `genie_*` profiles; other runtimes use their matching native roles. Keep
model and effort in runtime session/agent configuration, never skill frontmatter.

**Ambiente do engineer.** O G1 e o G2 rodam no mesmo worktree próprio, criado a partir de `origin/main`. Ele tem **venv próprio dentro do projeto**, criado por `POETRY_VIRTUALENVS_IN_PROJECT=true poetry install` e preparado pelo orquestrador antes do dispatch. A variável é usada em vez de um `poetry.toml`, que não está no gitignore. O G2 também precisa desse venv, porque `tests/conftest.py:19` importa a app. O venv compartilhado do poetry não serve: o snapshot de `neurostack_core` nele é de `749a3313`, anterior ao #206, e falha com `ImportError: MEDIA_BINDABLE_INTENT_IDS`. Ele é compartilhado com o Codex e não deve ser mexido.

**Passos do owner depois do merge.** Cada passo precisa do "vai":
1. Build e deploy. A sombra entra em produção.
2. Leitura da sombra até o critério da Decisão 9. Inclui uma rotação deliberada numa janela de pouco tráfego, porque as respostas falham até o painel ser atualizado.
3. Ordem da Decisão 10.
4. Seguimento `uazapi-credit-enforcement` antes do L10.

## Execution Groups

### Group 1: Verificação por token, sombra e aplicação

**Goal:** Verificar o `token` do envelope antes do claim. Na sombra, só medir sem mudar nada. Na aplicação, recusar com 401 ou verificar o turno, marcando origem e principal de crédito. Deixar também a guarda do L10.

**Deliverables:**
1. **Verificação em `src/api/routes/webhook.py`, dentro de `uazapi_webhook` (~4478):**
   - ler a flag uma vez, antes da consulta da instância;
   - usar o resolvedor exclusivo da uazapi, que lê todas as linhas candidatas (`tenant_id`, `uazapi_token`, `uazapi_api_url`, `whatsapp_provider`) numa consulta só, com `_resolve_tenant_by_credential` intocado;
   - aplicar a semântica da Decisão 7;
   - calcular o resultado, emitir o log e incrementar o contador.

   Tudo isso antes do claim (`:4671`).

   Na sombra, rotear exatamente como hoje. Na aplicação, aceitar a única linha com token igual ou devolver 401.
2. **Credit principal (`src/services/credit_principal.py`, `src/services/credit_metering.py`):**
   - `inbound_verified: bool = False` na identidade, passado por `uazapi_ingress`;
   - `derive_credit_principal` trata `uazapi` com `inbound_verified` como autenticado;
   - `principal_id` e a chave do claim ficam iguais.
3. **Guarda do L10** (`src/services/credit_rollout.py`, função `arm_enforcement` em ~`:1612`; `src/ops/credit_enforcement.py:156-200`):
   - recusa dura e incondicional, antes de `reconcile_orphans()`;
   - a recusa aparece no `status`;
   - o motivo novo entra em `REJECTION_REASONS` e em `REASONS`, e nunca em `CUTOFF_REASON_CODES`;
   - a recusa não pode ser aceita pelo `accept_cutoff`;
   - um comentário diz que só `uazapi-credit-enforcement` remove a recusa;
   - o predicado `uazapi_enforced_credit_adapter_available()`, que sempre devolve `False`, é o único ponto de troca. Só os testes de armação com sucesso o trocam, por um fixture explícito.

   Testes:
   - `tests/services/test_credit_rollout.py`;
   - `tests/ops/test_credit_enforcement.py`;
   - `tests/integration/test_credit_enforcement_ops_postgres.py`;
   - `tests/integration/test_credit_enforcement_rollout_fence.py`.

   O rótulo do cutoff não muda neste wish (OUT).
4. **Flag:**
   - `uazapi_webhook_auth_enforced` em `SUPPORTED_FLAGS`;
   - helper fail-to-shadow que nunca levanta exceção;
   - a flag em `_log_transition`, com o teste em `tests/infrastructure/test_appconfig.py`;
   - os exemplos JSON fixados por teste (`docs/configuration.md`, `docs/runbooks/asaas-deployment.md`).
5. **`media_delivery_provider_ready`:** continua pura, recebe as flags como parâmetros, e o chamador em `media_assets.py` lê as flags depois do `_rollback`. Atualizar a matriz e o alarme de drift em `tests/api/test_media_assets.py`.
6. **Contador** com vocabulário fechado em `metric_dimensions.py` e `metrics.py`, mais o teste de contrato.
7. **Comentários e docstrings** listados no Scope IN.
8. **Testes:**
   - os seis resultados nos dois modos, mais os casos de tipo, encoding, strip e duplicata;
   - token sentinela fora de toda saída;
   - na aplicação: 401 sem claim, sessão nem evento;
   - `match` dá `PRODUCTION` e principal autenticado;
   - prova em Postgres da medição observacional autenticada;
   - a sombra fiel a hoje;
   - `test_webhook_auth_order.py` atualizado só na linha de trace da verificação;
   - a guarda do L10;
   - prova vermelha por mutação de cada comportamento crítico, em cópia privada.

**Acceptance Criteria:**
- [ ] **Sombra.** A suíte atual da uazapi passa sem mudar nenhuma asserção de comportamento. A única exceção, nomeada, é a linha de trace em `test_webhook_auth_order.py`.
- [ ] **Recusa.** `mismatch`, `token_missing`, `token_not_configured`, `unknown_instance` e `ambiguous_instance` dão 401 na aplicação, sem claim, sessão nem evento. Na sombra, dão 200 com o comportamento de hoje.
- [ ] **Instância duplicada.** Com duas linhas e um só token igual, a aplicação aceita a linha certa.
- [ ] **Medição.** Em Postgres, o `match` aplicado grava a linha observacional com principal autenticado.
- [ ] **Guarda do L10.** `arm_enforcement` sempre recusa, o CLI responde `refused` (exit 20) sem `ValueError`, e o `accept_cutoff` não consegue liberar.
- [ ] **Token fora de toda saída.** O token sentinela está ausente de todas as saídas.
- [ ] **Exceções.** Token não ASCII, `int` ou `dict` não levantam exceção, nem na sombra nem na aplicação.

**Validation:**
```bash
# Escopo: ingresso, autenticação e crédito em produção → testes focados e acoplados, crédito em Postgres sem skip e gate estático. O make quality fica com o orquestrador, conforme decisão do owner sobre o custo.
: "${POSTGRES_SMOKE_DATABASE_URL:?defina um Postgres descartável}" \
  && poetry run pytest tests/api/test_webhook_uazapi.py tests/api/test_webhook_auth_order.py tests/api/test_credit_principal_authorization.py tests/services/test_credit_principal.py tests/services/test_credit_metering.py tests/services/test_credit_rollout.py tests/ops/test_credit_enforcement.py tests/services/test_egress_policy.py tests/infrastructure/test_appconfig.py tests/observability/test_metric_dimension_contract.py tests/api/test_media_assets.py tests/api/test_webhook_media_delivery.py tests/services/test_media_selection.py tests/scripts/test_asaas_production_wiring.py tests/scripts/test_production_migration_window.py tests/scripts/test_asaas_deploy_hook.py -q -p no:cacheprovider \
  && bash scripts/check-credit-integration.sh tests/integration/test_credit_metering_postgres.py tests/integration/test_credit_enforcement_ops_postgres.py tests/integration/test_credit_enforcement_rollout_fence.py tests/integration/test_credit_orphan_reconciliation_postgres.py tests/integration/test_webhook_media_delivery_postgres.py tests/integration/test_conversation_language_postgres.py tests/integration/test_credit_schema_dominance_postgres.py \
  && make format-check && make lint && make typecheck
```

**depends-on:** none

---

### Group 2: Runbook e docs desatualizados

**Goal:** Dar ao owner o procedimento para ler a sombra, aplicar, rotacionar e voltar atrás com segurança, e corrigir os docs que hoje dizem que a uazapi não tem autenticação.

**Deliverables:**
1. **`docs/runbooks/uazapi-webhook-auth.md`**, cobrindo:
   - o que a sombra mede;
   - a consulta no CloudWatch do critério da Decisão 9, com a definição de "autenticável";
   - a rotação deliberada (chamada exata, sequência esperada, falha de envio até atualizar o painel, janela de pouco tráfego, sempre com a autenticação em sombra);
   - o pré-check de duplicatas (`SELECT api_credentials->>'uazapi_instance_name', count(*) … GROUP BY 1 HAVING count(*) > 1`);
   - o pré-check de crédito, só leitura (`tenants.builder_id`, `credit_accounts`, `credit_enforcement_state` e efeitos observacionais);
   - a ordem da Decisão 10;
   - a regra da Decisão 11 (toda versão hospedada leva a flag `true`, e falhar significa ficar sem autenticação);
   - o rollback, que é fechar a flag;
   - o caso de troca de modo, em que um evento visto na sombra e reentregue na aplicação mantém a primeira linha observacional;
   - a mudança do rótulo de origem nas análises.
2. **`docs/runbooks/credit-enforcement-arming.md`:** atualizar `:33-34` e `:187`, acrescentar o pré-requisito "armar exige `uazapi-credit-enforcement`, porque até lá a armação sempre recusa" e pôr o motivo novo na lista de motivos `refused` (`:297`). Esse runbook é lido e executado por `tests/ops/test_credit_enforcement.py` (`:251-260`, `:572-620`).
3. **`docs/agent-media-delivery.md`:**
   - a tabela "Which turns can carry a file";
   - `:47`, `:50-52`, `:76`, `:154` e `:501`;
   - o kill switch, que passa a falar em quatro flags (`:529-533`, `:579-580`);
   - a Decisão 12, que passa a apontar para este runbook.
4. **Outros docs:**
   - `docs/api-contracts.md:248`, `docs/builder-integration-guide.md:121` e `docs/cognitive-pipeline.md:122`;
   - `docs/configuration.md`, na flag nova, se o Group 1 ainda não tiver feito;
   - os textos que ainda falam em três flags, que passam a quatro: `docs/runbooks/asaas-deployment.md:3711-3757` e `docs/runbooks/asaas-rollout-ceremony.md:636`.
5. **Passo do orquestrador, fora deste gate:** nota no Group 6 de `.genie/wishes/cloud-readiness/WISH.md` dizendo que a uazapi segue por este wish.

**Acceptance Criteria:**
- [ ] O runbook traz cada termo verificável exatamente.
- [ ] Os links relativos resolvem.
- [ ] Nenhum doc atualizado continua dizendo que a uazapi não tem autenticação sem citar a flag.
- [ ] Os testes que leem docs passam.

**Validation:**
```bash
# Escopo: documentação → termos exatos, links e testes que leem os docs editados.
R=docs/runbooks/uazapi-webhook-auth.md; for t in '`uazapi_webhook_auth_enforced`' '`agent_media_send_enabled`' '`match`' '`mismatch`' '`token_missing`' '`token_not_configured`' 'rotateInstanceToken' 'credit_enforcement_state' 'HAVING count(*) > 1' 'uazapi-credit-enforcement'; do grep -qF -- "$t" "$R" || { echo "faltando: $t"; exit 1; }; done \
  && grep -qF 'uazapi-credit-enforcement' docs/runbooks/credit-enforcement-arming.md \
  && python3 -c 'import re,sys,pathlib; bad=[(p,l) for p in ["docs/runbooks/uazapi-webhook-auth.md","docs/agent-media-delivery.md","docs/runbooks/credit-enforcement-arming.md"] for l in re.findall(r"\]\((?![a-zA-Z][a-zA-Z0-9+.-]*:)([^)#]+)", pathlib.Path(p).read_text()) if not (pathlib.Path(p).parent/l.strip()).exists()]; print(bad); sys.exit(1 if bad else 0)' \
  && poetry run pytest tests/docs tests/ops/test_credit_enforcement.py tests/scripts/test_asaas_production_wiring.py tests/scripts/test_production_migration_window.py tests/scripts/test_asaas_deploy_hook.py tests/test_billing_migration_surface_removed.py -q -p no:cacheprovider
```

**depends-on:** 1

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] **Sombra em produção.** As mensagens reais da clínica uazapi geram `uazapi_webhook_auth` com `mode=shadow`, e o atendimento continua igual: mesma resposta, origem `UNKNOWN`, nenhum 401.
- [ ] **Aplicação.** Depois de abrir a flag com o "vai", as mensagens reais dão `match`, os turnos ficam `PRODUCTION` e a medição registra o turno como autenticado. Uma chamada forjada com token errado recebe 401.
- [ ] **Regressão.** Meta, Z-API e Evolution não mudam.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| O `token` do envelope pode ser diferente do `uazapi_token` gravado. | High | Por isso existe a sombra. Só aplicar com 100% `match` (Decisão 9). Se der `mismatch`, revisar o desenho antes de aplicar. |
| Uma rotação sem atualizar o painel recusa todas as mensagens na aplicação e quebra o envio mesmo na sombra. | High | Rotacionar sempre com a autenticação em sombra, numa janela de pouco tráfego, e atualizar o painel na hora. Rollback: fechar a flag. Há gatilho para o período de graça. |
| Uma versão AppConfig antiga ou "fechada" desliga a aplicação sem ninguém perceber, e um poll falho volta a aceitar chamadas sem autenticação. | Medium | Decisão 11. O rótulo `mode` aparece no log e no contador. A verificação por worker inclui a flag. |
| Armar o L10 com a uazapi verificada a deixaria de graça e sem registro. | High | Uma recusa dura em `arm_enforcement`, que não pode ser aceita, enquanto houver clínica uazapi configurada. Mais o seguimento `uazapi-credit-enforcement`. Efeito colateral: o L10 espera esse seguimento. |
| Não se sabe como a uazapi reage a um 401, nem quantas tentativas faz antes de desativar. | Medium | A sombra não gera 401. Na aplicação, só tráfego não legítimo deve receber 401. O primeiro dia é acompanhado. |
| Turnos da uazapi `PRODUCTION` com a flag de mídia aberta. | Medium | Ordem da Decisão 10. |
| Nome de instância duplicado. | Low | A verificação por token protege (Decisão 6). Há pré-check de duplicatas no runbook. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### 2026-11-09: revisão do plano, BLOCKED

Relatório completo em `scratchpad/uazapi-plan-review.md`. A revisão foi feita por `genie:reviewer`, só leitura, sobre `origin/main` `83d7094f`.

**Achado crítico:**
- **C1. Cobrança.** Só Z-API e Meta têm caminho de crédito enforced (`credit_metering.py:1952`, `:2088-2212`; `webhook.py:587-588`). Depois de armar o L10, o writer observacional não grava nada (`:770-771`), e um teste fixa isso (`tests/services/test_credit_metering.py:3984-4010`). O owner precisa escolher entre (a) construir o adaptador enforced e (b) medir agora, com seguimento e guarda.

**Achados HIGH:**
- **H1.** Não havia mecanismo para autenticar só o turno verificado.
- **H2.** `ambiguous_instance` daria 401 a partir de uma única linha, o que permitiria a outro tenant bloquear a clínica.
- **H3.** A comparação não estava especificada (`""` contra `""`, `TypeError` com não ASCII, strip).

**Achados MEDIUM:**
- **M1.** Prontidão: falta passar a flag como parâmetro, e `test_media_assets.py` não estava no plano.
- **M2.** Faltavam o pré-check de mídia e a regra de QA.
- **M3.** Kill switch: o doc fala em três flags, o `_log_transition` não lê a flag nova, e o sentido da flag é invertido.
- **M4.** O critério só pode ser julgado pelo log, e a rotação quebra o envio.
- **M5.** Faltavam testes acoplados na validação.
- **M6.** A complexidade do G1 é 8 ou mais, o que pede final-gate.
- **M7.** Docs, docstrings e o rótulo do cutoff estão desatualizados.

**Achados LOW:** L1 a L8.

**Decisão do owner (2026-11-09):** "Medir agora, cobrar antes do L10". Isso resolve o C1 pela opção (b).

**Emenda do orquestrador, que cobre C1, H1 a H3, M1 a M7 e L1, L2, L3, L5, L6, L7 e L8:**
- **Decisões:** a Decisão 4 foi estreitada; entraram as Decisões 5 (`inbound_verified`, opção C), 6 (uma única linha igual, com a sombra roteando como hoje), 7 (semântica da comparação), 9 (critério pelo log, mais a rotação deliberada), 10 (ordem completa) e 11 (sentido invertido da flag).
- **Escopo e grupos:** entraram a guarda do L10 e o rótulo do cutoff; `_log_transition`; a prontidão como função pura com a flag passada por parâmetro; a validação com os testes acoplados; o G1 com complexidade 8 e final-gate; e a lista completa de docs no G2.
- **Prova do G2:** os termos são exatos e os links são checados.
- **Status:** volta a DRAFT para nova revisão.

---

## Files to Create/Modify

```
src/api/routes/webhook.py
src/api/routes/media_assets.py
src/services/credit_principal.py
src/services/credit_metering.py
src/services/credit_rollout.py
src/ops/credit_enforcement.py
src/services/api_credentials.py
src/services/egress_policy.py
src/services/media_send_body.py              (comentário)
src/infrastructure/appconfig.py
src/observability/metric_dimensions.py
src/observability/metrics.py
tests/api/test_webhook_uazapi.py
tests/api/test_webhook_auth_order.py
tests/api/test_credit_principal_authorization.py
tests/api/test_media_assets.py
tests/services/test_credit_principal.py
tests/services/test_credit_metering.py
tests/services/test_credit_rollout.py
tests/services/test_egress_policy.py
tests/infrastructure/test_appconfig.py
tests/observability/test_metric_dimension_contract.py
tests/integration/test_credit_metering_postgres.py
tests/ops/test_credit_enforcement.py
tests/integration/test_credit_enforcement_ops_postgres.py
tests/integration/test_credit_enforcement_rollout_fence.py
docs/runbooks/uazapi-webhook-auth.md            (novo)
docs/runbooks/asaas-rollout-ceremony.md
docs/runbooks/credit-enforcement-arming.md
docs/runbooks/asaas-deployment.md               (exemplos JSON fixados pela lista de flags)
docs/agent-media-delivery.md
docs/configuration.md
docs/api-contracts.md
docs/builder-integration-guide.md
docs/cognitive-pipeline.md
.genie/wishes/cloud-readiness/WISH.md           (nota do orquestrador, não rastreado)
```

### 2026-11-09: re-revisão do plano, FIX-FIRST

Relatório em `scratchpad/uazapi-plan-review.md`, seção "Re-review". Mesmo revisor.

**O que fechou:** C1 (pela opção b do owner), H1, H2 no desenho, H3, M1 a M4, M6, M7 e os LOW. O M5 fechou quase todo. Confirmado: `inbound_verified` chega a `would_debit_mc` (`credit_metering.py:786`, `:788`, `:820`) sem tocar em nenhum caminho enforced.

**R-H1 (HIGH): a guarda do L10 não bloqueia.** Qualquer achado de cutoff pode ser aceito (`credit_rollout.py:222-223`, `:1463-1531`), e o relatório olha só dois meses. Correção: recusa dura em `arm_enforcement` enquanto houver clínica com `uazapi_instance_name`. Ela não pode ser aceita e entra no conjunto fechado de `src/ops/credit_enforcement.py:146-156`, com testes de ops e Postgres.

**R-M1 (MEDIUM):** a consulta de candidatas quebra `test_conversation_language_postgres.py:529-545` e não garante o mesmo `.first()`. Correção: na sombra, rotear pelo resolvedor intocado.

**R-M2 (MEDIUM):** o venv compartilhado tem o `neurostack_core` defasado (`749a3313`) e falha em `ImportError`. Correção: worktree com venv próprio.

**R-L1 (LOW):** o checador de links tratava URL como arquivo.

**Outros LOW:**
- `inbound_verified` só para o kind `uazapi`;
- regra de resultado com várias candidatas;
- pré-check de crédito de volta na Decisão 10;
- docs que ainda falam em três flags (`asaas-deployment.md:3711-3757`, `asaas-rollout-ceremony.md:636`).

**Emenda do orquestrador:** todos os pontos acima foram aplicados.
- **Guarda do L10:** a recusa dura entra no Scope, nas Decisões 4 e 6, no G1 (deliverable 3, critérios e validação), em Riscos e na lista de arquivos.
- **Efeito colateral aceito:** com clínica uazapi configurada, o L10 espera o `uazapi-credit-enforcement`, o que está em linha com a decisão do owner "cobrar antes do L10".
- **Regra das candidatas:** escrita no Scope IN.
- **Checador de links:** ignora URLs com esquema. Testado em `docs/agent-media-delivery.md` e `docs/runbooks/credit-enforcement-arming.md` da main, sem nenhum falso positivo.

### 2026-11-09: re-revisão 2 do plano, SHIP

Relatório em `scratchpad/uazapi-plan-review.md`, seção "Re-review 2". Mesmo revisor, sobre `origin/main` `83d7094f`.

- **Recusa dura:** coerente com `arm_enforcement` e com o CLI. É recusa e não achado, o CLI devolve `refused` (exit 20), e nenhum teste que arma configura instância uazapi.
- **Junção claim↔efeito:** confiável, porque os dois são gravados na mesma transação e `trace_id == interaction_id` (`credit_metering.py:788-829`).
- **Fechados:** R-M1 e R-L1.

**Achados MEDIUM, aplicados pelo orquestrador antes do dispatch:**
- **R2-M1:** o motivo novo vai em `REJECTION_REASONS` e `REASONS`, nunca em `CUTOFF_REASON_CODES`.
- **R2-M2:** a recusa ficou incondicional até o seguimento entrar, o que fecha o buraco de uma clínica uazapi cadastrada depois de armar. A mudança do rótulo do cutoff saiu deste wish e foi para o seguimento.
- **R2-M3:** `tests/ops/test_credit_enforcement.py` entrou na validação do G2, e o motivo novo entrou na lista `refused` do runbook (`:297`).

**Achados LOW aplicados:**
- o check roda antes de `reconcile_orphans()` e aparece no `status`;
- o G2 usa o mesmo venv próprio;
- o venv usa `POETRY_VIRTUALENVS_IN_PROJECT`;
- `test_credit_orphan_reconciliation_postgres.py` entrou no gate de crédito;
- a linha de `would_debit_mc` foi corrigida para `:343`.

**Status:** APPROVED.

### 2026-11-09: esclarecimento do orquestrador antes do dispatch

A recusa incondicional do L10, como foi aprovada, recusaria também os testes que provam a armação com sucesso: são cerca de 60 chamadas em 5 arquivos (`grep` em `83d7094f`).

Fica fixado o ponto de troca só para testes: o predicado de módulo `uazapi_enforced_credit_adapter_available()`.
- Ele sempre devolve `False` e não lê env var, config, flag nem banco.
- Só os testes de armação com sucesso o trocam.
- Um teste prova que a armação recusa sem a troca.

Em produção nada muda, e a recusa continua sem escapatória. O desenho aprovado continua o mesmo; isto só esclarece a parte de testes. O final-gate do G1 confere o ponto de troca.

### 2026-11-09: execução do G1, revisões local e de qualidade, ambas SHIP

- **Engineer** (`engineer-complex`) reportou `done`. Mudou 28 arquivos (+2639/−91) no worktree `/home/lorenzo/work/uazapi-webhook-auth`, sem commit. Fez 9 provas de mutação, todas vermelhas.
- **Validação do orquestrador:** o comando exato do G1 rodou com um Postgres descartável próprio. Resultado: 2448 testes unitários passaram; 322 de 322 testes Postgres passaram, com 0 skip, xfail ou deselected; format, isort, flake8 e mypy (258 arquivos) limpos; `chain_rc=0`. O log do servidor Postgres tinha 17 linhas `ERROR`, que não foram inspecionadas antes da limpeza. Elas serão lidas na revalidação.
- **Revisão de aceite** (`scratchpad/uazapi-g1-review.md`): SHIP. Os 7 critérios estão OK, e o critério 4 foi verificado lendo o código, com a validação do orquestrador confirmando. As 5 escolhas do engineer foram aceitas: o atalho com AppConfig desligado, os helpers em `egress_policy`, o ponto de troca aplicado em 58 casos sem nenhum teste vazio, o `status` e as mudanças por construção. Foram 22 mutações e só uma sobreviveu (L1). Achados LOW:
  - L1: falta afirmar `flag_reads == [True]` no `match`;
  - L2: o caso de erro da consulta de candidatas na sombra não está fixado;
  - L3: `instance=%s` sem escape;
  - L4: `configuration.md`, que fica para o G2;
  - L5: os campos do log não são testados nos estados opostos.
- **Revisão de qualidade** (`scratchpad/uazapi-g1-quality.md`): SHIP.
  - **M1:** a linha `uazapi_webhook_auth` pode ser forjada por `\n` no `instanceName` (reproduzido). Ela é a evidência da Decisão 9.
  - **M2:** na sombra, uma falha própria da consulta de candidatas vira 500 para toda a uazapi.
  - **LOW:**
    - o log monta os campos por posição;
    - o vocabulário dos resultados está definido 3 vezes;
    - o handler está grande;
    - a descrição OpenAPI, o comentário de `_UNAUTHENTICATED_KINDS` e o ponteiro em `UNKNOWN_INGRESS_SOURCES` estão desatualizados;
    - o runbook de armação, que fica para o G2.
  - **Fora do escopo:** a rota da uazapi não tem o limite de corpo de 1 MiB. Isso já é assim hoje e fica registrado como nota para o owner.
- **Rodada de correção 1/1** (fixer): M1, M2, L1, L2, L3, L5, campos nomeados no log, vocabulário único e os comentários desatualizados. Os docs ficam para o G2.
- **Fixer** reportou `done`. As mudanças:
  - `_loggable_instance`: percent-encoding e corte em 128 caracteres nas 3 linhas de log;
  - o log é montado a partir de campos nomeados;
  - na sombra, um erro do loader vira rollback, resultado `check_error` e roteamento como hoje; na aplicação, vira 500;
  - o vocabulário único fica em `metric_dimensions.py`;
  - testes de flag lida uma vez e de estados opostos;
  - os comentários foram atualizados.

  Sete provas vermelhas. O fixer anotou que o `%r` não bastava. Os campos `raw_from`, `msg_type` e `mediaType` da linha antiga (`:4861`) seguem crus, porque já eram assim e essa linha não é evidência da Decisão 9.
- **Revalidação do orquestrador:** o comando exato do G1 rodou com um Postgres descartável próprio, com `chain_rc=0`. Resultado: 2459 testes unitários passaram; 322 de 322 testes Postgres passaram, com 0 skip; format, isort, flake8 e mypy limpos. As 17 linhas `ERROR` do log do servidor foram lidas e todas são recusas intencionais: check constraint, append-only, transação read-only, lock e statement timeout, cancelamento e o `SELECT 1 / 0` de `test_credit_orphan_reconciliation_postgres.py:1191`. Nenhum erro de tipo, de coluna ou de sintaxe.
- **Final-gate do G1** disparado.
- **Final-gate do G1** (`scratchpad/uazapi-g1-final-gate.md`): **SHIP**, nenhum CRITICAL ou HIGH, sem contrariar as outras revisões.
  - **Confirmado:**
    - a sombra é idêntica em Postgres: a sessão chega sem transação, e a consulta de candidatas só seleciona colunas;
    - a mesma cláusula WHERE (`astext ==`) no resolvedor e nas candidatas, então `routed_matches` na sombra equivale ao roteamento na aplicação;
    - um único `auth_enforced` decide tudo;
    - `inbound_verified` só vem de aplicação com `match`;
    - o predicado do L10 é literalmente `return False`;
    - a prontidão não abre a uazapi na sombra.
  - **M1 (residual declarado):** nenhum código impede a aplicação com a flag de mídia aberta. A ordem da Decisão 10 é o único controle e vai para o runbook (G2) como pré-condição dura, com leitura por worker.
  - **L1:** tráfego de terceiros sem token pode travar o 100% `match`. A consulta do G2 separa esses casos por `token_present`, `owner_present` e `base_url_matches`.
  - **L2:** os testes de rota em aplicação cobrem só texto. Aceito como LOW.
  - **L3:** efeitos da virada que vão para o runbook: o consumo "observado" sobe 1000 mC por turno verificado, o `status` mostra o motivo da guarda desde o deploy, e uma reentrega gera uma linha por entrega.
- **G1 concluído** (`genie task done t_mw8k3zdf2dd132df`). O G2 foi disparado.

### 2026-11-09: execução do G2, revisão FIX-FIRST e rodada de correção 1/2

- **Engineer** (`engineer-standard`) reportou `done`. Entregou o runbook novo `docs/runbooks/uazapi-webhook-auth.md` (508 linhas) e 8 docs atualizados (+179/−71).
- **Validação do orquestrador:** o comando exato do G2 deu termos OK e links `[]`, com 1392 testes passando (rc=0).
- **Revisão** (`scratchpad/uazapi-g2-review.md`): FIX-FIRST.
  - **O que passou:** os 11 itens do runbook e as edições obrigatórias dos outros docs. O padrão da consulta Logs Insights foi conferido contra a linha real com seis nomes de instância difíceis. Os pré-checks são só leitura e os endpoints existem.
  - **HIGH:** depois da aplicação, nada acompanha as mensagens recusadas. Casos: token trocado no painel ou instância recriada; um tipo legítimo sem token; um 500 que não grava a linha de auth.
  - **MEDIUM:**
    - a flag é global, mas o critério cobre uma clínica só;
    - religar a aplicação não está amarrado à ordem;
    - a separação de tráfego de terceiros pode esconder mensagem real.
  - **LOW:**
    - `credit_enforcement_at` deveria ser `enforcement_at`;
    - a redação dos 180 s;
    - a guarda só aparece com a chave de crédito ligada;
    - o fim da janela de rotação não é registrado;
    - o runbook cita um caminho de evidência não rastreado;
    - outros listados no relatório.
  - **Desatualizações adjacentes, que o engineer achou e deixou para o owner decidir:**
    - `asaas-deployment.md:3651` diz que o egress da uazapi está fechado;
    - `configuration.md:55`;
    - uma âncora quebrada que já existia em `asaas-rollout-ceremony.md:630`.
- **Rodada de correção 1/2** (fixer): o HIGH, os 3 MEDIUM e os LOW.
- **Fixer, rodada 1/2:** `done`. Ficou assim:
  - **Vigilância pós-aplicação:** seção nova W1 a W4 com cadência (logo após, de hora em hora por 24 h, depois diária por 7 dias) e gatilho de rollback.
  - **Troca de token ou de instância:** sempre volta para a sombra antes.
  - **Inventário por tenant:** cada tenant uazapi configurado sem evidência exige o aceite do owner registrado.
  - **Regra de religar:** toda volta para a aplicação refaz a ordem inteira.
  - **Separação de tráfego:** só explica as linhas, nunca exclui.
  - **Mensagem conhecida:** uma de cada tipo.
  - **LOWs:** L1 a L11 corrigidos.
  - **Fatos medidos rodando a app:** o 500 registra `endpoint: "/uazapi"` e não grava a linha `http_request`; o 401 grava. As consultas foram provadas contra 36 linhas reais.
  - **Validação:** 1392 testes passaram.
- **Decisões do owner (2026-11-09):**
  - "Corrigir neste wish": as 3 desatualizações adjacentes foram para o fixer.
  - "Commitar e abrir o PR" depois do SHIP do G2, com o wish incluído via `git add -f`. O merge é do owner.
- Os relatórios foram copiados para `/home/lorenzo/work/uazapi-webhook-auth-notes/`, que é durável.

### 2026-11-09: re-revisão do G2, FIX-FIRST; decisão do owner sobre log forjável

- **O que fechou:** a re-revisão (`scratchpad/uazapi-g2-review.md`, seção "Re-review") confirma que o HIGH, os 3 MEDIUM, os L1 a L12 e as 4 correções adjacentes fecharam. Também confirmou, medindo, as afirmações sobre o 500 e o `http_request`.
- **HIGH novo (já existia no G1):** a linha `Uazapi webhook:` registra `from`, `type` e `mediaType` crus (`webhook.py:4860`), e a Evolution faz o mesmo (`:4011`). Como o driver de log não tem multiline, um `\n` cria uma linha nova no CloudWatch. O revisor forjou, com um único POST sem token, três linhas aceitas pelas consultas do runbook: um W1 legítimo, um W3 de 500 e um `match` na sombra.
- **MEDIUM:** a `origin/main` andou até `801d6502` (#208), que traz o `ENVIRONMENT=production` e o serviço `neurostack-prod-app`, gerido por Terraform. O runbook não pode deixar fixo `/ecs/neurostack-staging/app`. O worktree foi avançado (fast-forward) para `801d6502`. O #208 não toca em nenhum arquivo deste wish, e as dependências não mudaram.
- **LOW:** 6 itens.
- **Decisão do owner (2026-11-09):** "Escape central neste wish".
  - Um formatter único em `src/config/logging.py` escapa `\r`, `\n` e os outros caracteres de controle em toda linha do logging padrão. Isso fecha o vetor para a uazapi, a Evolution e qualquer outra linha. Efeito colateral aceito: um traceback vira uma linha só.
  - O runbook confere cada gatilho numa fonte que uma requisição não consegue escrever, e as consultas são ancoradas no início da linha.
  - A validação usa os testes de logging mais o gate do G1. O `make quality` só roda se o owner pedir.
- **Correções disparadas:** o escape vai como correção do G1, para um fixer com arquivos de código. A rodada 2/2 do G2 vai para um fixer com arquivos de docs.
- **Rodada 2/2 do G2:** fixer `done`.
  - **Consultas:** todas ancoradas em `^`, inclusive W3 e `http_request` na forma JSON medida.
  - **Seção "Log integrity":** a evidência só conta a partir de `ESCAPING_IMAGE_AT`.
  - **Corroboração por gatilho:**
    - W3: ALB `HTTPCode_Target_5XX_Count`;
    - W4: `list-deployments` do AppConfig;
    - W1: WhatsApp da clínica;
    - contagens: `message.in`, com o limite declarado.
  - **Seção "Target environment":** cita o #208 e `neurostack-prod-app`; as consultas recebem `LOG_GROUP` e `REGION` como parâmetro.
  - **LOWs:** os 6 corrigidos.
  - **Prova:** com o formatter real, um ataque com 8 linhas forjadas dá 0 linhas forjadas em todas as consultas. Na forma antiga, várias casavam.
  - **Testes:** 1513 passaram.
- **Escape central:** fixer `done`.
  - **Implementação:** `SingleLineFormatter` em `src/config/logging.py`, com a tabela de escape fixada e sem nunca levantar exceção. Só é instalado no handler que o `basicConfig` cria.
  - **Testes:** 16 em `tests/config/test_logging_single_line.py`, mais o teste ponta a ponta `test_a_body_value_cannot_forge_an_auth_line_in_the_real_log_output`, com provas vermelhas.
  - **Pendente:** os handlers próprios do uvicorn (stderr, `propagate=False`) não passavam pelo formatter, e o ESC do console de dev aparecia literal. Os dois foram mandados ao mesmo fixer como seguimento.
- **Achado fora do wish:** a `main` `801d6502` (#208) está vermelha.
  - 3 arquivos fora do black;
  - 1 E303 do flake8 em `tests/ops/test_asaas_webhook_cli.py:839`;
  - um teste quebrado, `test_appconfig_settings.py::...[overrides2]`, que ainda espera o `ConfigurationError` que o #208 tirou.

  Confirmado na `main` limpa. O CI do #208 só roda o preflight do ASAAS e as suítes Postgres de billing.
- **Decisão do owner (2026-11-09):** "PR separado de correção". Branch `fix/production-on-ecs-followups`, no worktree `/home/lorenzo/work/fix-208-followups`, para entrar antes do PR deste wish.
- **Seguimento do escape central:** fixer `done`.
  - `SingleLineDelegatingFormatter` nos handlers `uvicorn` e `uvicorn.error`, idempotente. A ordem foi verificada no uvicorn 0.27.1 nos três modos de subida.
  - O ESC fica preservado só no modo console.
  - O teste `test_a_uvicorn_asgi_traceback_is_one_physical_line` sobe um `uvicorn.Server` real. Foram 4 provas vermelhas.
  - Bateria combinada: 2850 testes passaram, 1 skip e 1 falha, que é o teste do #208 corrigido pelo #211.
- **PR #211** (`fix/production-on-ecs-followups`, `cdbc35b6`) foi aberto. Ele corrige o black, o flake8 e o teste do #208.
  - O orquestrador validou e leu o diff de 12 linhas, sem revisor separado por ser mecânico.
  - Validação: black, isort e flake8 limpos, mypy sem erros em 258 arquivos e 439 testes passando.
  - O merge é do owner e precisa vir antes do PR deste wish.
- **Re-revisão 2 do G2:** **SHIP**. O revisor subiu a app como a produção sobe e mandou um POST com 9 linhas forjadas (`\n`, `\r\n`, `\r`, NEL, U+2028, U+2029, VT, FF). Saíram 3 linhas físicas e 0 casamentos forjados em todas as consultas ancoradas, e as linhas genuínas continuaram casando. Os comandos novos existem e são só leitura. Ficaram 5 LOW:
  - a atribuição do #208 em `:37-41` está errada;
  - a W4 ignora worker recém-iniciado e refresher parado, e o backoff chega a 300 s;
  - a notação do escape usa duas barras;
  - falta citar a exceção do ESC no modo console;
  - `agent-media-delivery.md:589-593` e `asaas-deployment.md:3766-3770` usam filtro por termo sem âncora.
- **G2 concluído** (`genie task done t_mw8k3zkg3606af70`). Os LOW entram junto com o resultado da revisão do logging.
- **Revalidação do G1 com o escape:**
  - **Unitários:** 2971 testes passaram. O único skip é o smoke opt-in da imagem Docker, que não rodou porque o C: está em 99%. O único deselect é o caso do #208, já corrigido no #211.
  - **Postgres:** 322 de 322 testes passaram, sem skip.
  - **Estáticos:** black, isort e flake8 nos 29 arquivos do wish, mais mypy, todos limpos.
  - **Log do servidor Postgres:** 17 linhas `ERROR`, todas intencionais.
- **#211 MERGED** em 2026-11-09T13:28Z, como squash `a0b8893b`. Ele é ancestral de `origin/main`. O worktree e o branch local foram removidos; o branch remoto continua.
- **Main andou:** a `origin/main` está em `973c2f5f` (#210, #212, #213). Os #212 e #213 tocam 11 arquivos deste wish, entre eles `webhook.py`, `media_assets.py`, `credit_metering.py`, `api_credentials.py`, `metric_dimensions.py` e testes. Vai precisar de rebase com resolução de conflito.
- **Revisão do escape central** (`notes/uazapi-logging-review.md`): FIX-FIRST.
  - **HIGH-1:** o shim do Fargate corta cada linha em 16 KiB. Um POST anônimo de 33 KB mira o corte e forja um `match` aceito pela consulta ancorada.
  - **MEDIUM-1:** um surrogate sozinho derruba a linha e despeja texto cru no stderr.
  - **MEDIUM-2:** o custo do `translate` com valores grandes.
  - **LOW-1:** o try/except do wrapper do uvicorn não tem teste.
  - **LOW-2:** a ordem de import não é testada.
  - **Conferido e OK:** a ordem do uvicorn, com `--workers 2` real; o JSON byte-idêntico; nenhuma ferramenta lê traceback multilinha.
- **Decisão do owner (2026-11-09):** "Cortar o meio, manter início e fim".
  - Toda linha fica com no máximo 15000 bytes UTF-8, com cabeça de cerca de 8000 bytes, cauda de cerca de 6000 e o marcador `...[N bytes elided]...`.
  - Vale também para o JSON acima do limite.
  - Entram ainda o escape dos surrogates, o pré-corte para limitar a CPU e o caminho rápido para ASCII imprimível.
- **Disparado:** o fixer do código, com o limite, os surrogates, o desempenho, LOW-1 e LOW-2. Em paralelo, o fixer dos docs, com a seção "Log integrity" e os 5 LOW da re-revisão 2.
- **Próximos passos:** commit local, rebase sobre a `main`, revalidação, final-gate do branch inteiro e PR.
