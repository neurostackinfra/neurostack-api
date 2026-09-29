# Wish: Configuração clara do stand-by pós-handoff

| Field | Value |
|-------|-------|
| **Status** | COMPLETE |
| **Slug** | `handoff-standby-reactivation` |
| **Date** | 2026-09-28 |
| **Author** | Codex |
| **Appetite** | medium |
| **Branch** | `wish/handoff-standby-reactivation` |
| **Repos touched** | `neurostack-api` |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Substituir os controles ambíguos de stand-by pós-handoff por dois estados explicados na própria tela: desligado usa o retorno automático fixo de 24 horas, sem campo editável, e ligado permite escolher reativação somente manual ou automática após um prazo em horas ou dias. A mesma política validada será usada pelo handoff disparado pela IA e pelo takeover humano, eliminando a divergência possível entre frontend, configuração persistida e os dois caminhos atuais do runtime.

## Direct-wish Rationale

O brainstorm é desnecessário porque as cinco capturas fornecidas já fecham a hierarquia, os textos e as transições desejadas, e o código existente reduz a decisão técnica a uma matriz bounded de estados já persistidos (`inactive`, `active/permanent`, `active/cycle`). Este wish não precisa descobrir uma interação nova: ele registra abaixo o contrato visual das referências e decide somente compatibilidade, validação e paridade dos dois caminhos existentes. As capturas são evidência de origem, não dependência de execução; nenhuma precisa ser adicionada ao repositório.

## Scope

### IN

- Reproduzir no bloco de stand-by da aba Handoff os estados ligado/desligado das cinco referências: título, descrição, switch, quadro explicativo, seletor de reativação e prazo condicional.
- Quando o switch estiver desligado, esconder todos os controles de prazo e aplicar sempre 24 horas; as 24 horas aparecem somente como explicação do comportamento, nunca como configuração editável.
- Quando estiver ligado, oferecer `Somente manualmente` e `Após um prazo personalizado`; o segundo revela um inteiro positivo e a unidade `Horas` ou `Dias`.
- Persistir o prazo personalizado sem perder a unidade escolhida, reutilizando `handoff_cycle_days` para dias e adicionando `handoff_cycle_hours` para horas; exatamente um deles fica preenchido no modo personalizado.
- Tipar, validar e documentar o bucket de handoff no `GET/PATCH /v1/config`, preservando as demais chaves existentes e rejeitando combinações inválidas antes de qualquer flush, commit ou invalidação de cache.
- Centralizar a decisão de pausa em uma política pura compartilhada pelo pipeline e pelo processamento de mensagem humana, incluindo compatibilidade com configurações legadas em dias e fallback seguro.
- Cobrir hidratação, edição, serialização, read-only, PT/EN, acessibilidade, limites, expiração e paridade entre os dois caminhos do runtime.

### OUT

- Alterar os gatilhos que criam handoff, classificador/SRM, fila/radar/SLA, lifecycle de `handoff_records` ou confirmação de atendimento humano.
- Alterar o toggle global `bot_active`, o card HITL “EM BREVE”, a frase mestra, seu `reactivation_delay_hours` ou os endpoints da Fila de Inativos.
- Adicionar minutos, calendários, horários comerciais, múltiplas janelas, jobs, WebSocket, cache ou reativação proativa sem nova mensagem do paciente.
- Fazer migration/backfill do JSONB ou remover imediatamente as chaves legadas `handoff_inactive_default_hours` e `handoff_cycle_days`.
- Redesenhar outras partes da aba Handoff ou mudar quais perfis podem acessá-la.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Switch desligado significa pausa temporária fixa de 24 horas | É a regra padrão pedida; remover o input impede que uma configuração que não deveria variar pareça necessária. |
| 2 | Switch ligado expõe somente `manual` ou `prazo personalizado` | Esses são os dois comportamentos mostrados nas referências e substituem os termos internos “permanente/ciclo” na interface. |
| 3 | Manter `standby_mode` e `handoff_period` como chaves internas | Evita uma migração e preserva configurações existentes: desligado → `inactive`; ligado/manual → `active` + `permanent`; ligado/prazo → `active` + `cycle`. |
| 4 | Representar a unidade pela exclusividade entre `handoff_cycle_hours` e `handoff_cycle_days` | Uma única chave nova preserva a unidade escolhida e mantém tenants legados em dias válidos sem estado redundante de unidade. |
| 5 | Horas aceitam 1–720 e dias aceitam 1–365, sempre inteiros | Mantém os limites já apresentados pelos dois inputs atuais e impede duração nula, negativa, fracionária ou excessiva. |
| 6 | Validar o bucket efetivo depois do merge do PATCH, preservando chaves não relacionadas | Um PATCH parcial precisa ser julgado junto do estado persistido; validação antes do merge aceitaria ou rejeitaria combinações pelo fragmento errado. |
| 7 | Uma função pura resolve `should_pause` e duração para ambos os caminhos | Hoje `pipeline.py` e `webhook.py` duplicam precedência, parsing e defaults; a paridade é requisito explícito desta mudança. |
| 8 | `handoff_inactive_default_hours` passa a ser legado não autoritativo | Configurações antigas podem continuar armazenadas sem migration, mas o runtime desligado usa 24 horas e a tela não lê nem envia esse valor. |
| 9 | Reativação manual continua usando os mecanismos existentes | A Fila de Inativos limpa a pausa; a mensagem mestra continua iniciando o cooldown existente. Este wish muda a política inicial, não o contrato desses comandos. |

## Simplicity Case

- **Simplest complete design:** um bloco condicional no formulário, uma chave JSONB adicional para horas e um resolvedor puro reutilizado pelos dois pontos que criam a pausa.
- **Added machinery:** a validação pós-merge é necessária porque a API é PATCH; a segunda chave de duração é necessária para persistir horas sem converter ou perder a unidade escolhida; o resolvedor compartilhado é necessário porque há dois caminhos reais que hoje repetem a regra.
- **Deferred until measured:** minutos ou calendários só entram com caso operacional aprovado; remoção física das chaves legadas só ocorre após inventário mostrar que nenhum tenant depende delas; reativação proativa só entra se a reativação preguiçosa na próxima mensagem violar uma necessidade medida.
- **Complexity removed:** sai o input editável do padrão de 24 horas, saem controles de ciclo quando não se aplicam e saem duas implementações independentes da mesma política; não há tabela, migration, job ou dependência nova.

## Reference UI Contract

Em todos os estados, o bloco mantém nesta ordem: título `Stand-by após handoff`, descrição `Defina por quanto tempo a IA permanece inativa após transferir a conversa para um atendente.`, switch com nome acessível `Ativar stand-by após handoff`, linha de status e quadro informativo. Os textos abaixo são o contrato PT exato; as mesmas estruturas e controles usam traduções equivalentes em EN. Menus abertos são estados temporários do mesmo formulário, não novos valores persistidos.

| Estado da referência | Estrutura e texto visível exatos | Controles visíveis / ausentes |
|----------------------|----------------------------------|------------------------------|
| **1. OFF** | Status `Stand-by desativado`. Quadro: `A IA volta a responder automaticamente 24 horas após o handoff.` | Switch desligado. Não renderizar `Quando reativar a IA?`, seletor de modo, input de prazo, seletor de unidade nem o input legado de 24 horas. |
| **2. ON — manual** | Status `Stand-by ativado`. O quadro comum a todo estado ON contém `Escolha quando a IA poderá voltar a responder:`, bullet `Somente após reativação manual: a IA permanece inativa até ser reativada pela mensagem mestra ou pelo painel de inativos.` e bullet `Após um prazo personalizado: a IA volta a responder automaticamente após o número de horas ou dias definido abaixo.` Abaixo, label `Quando reativar a IA?` e valor selecionado `Somente manualmente`. | Switch ligado e seletor de modo fechado. Não renderizar input de prazo nem seletor de unidade. |
| **3. Menu de reativação aberto** | Status e quadro são, sem alteração, os mesmos três textos do estado ON manual. Abaixo, label `Quando reativar a IA?`; o menu mostra, nesta ordem, `Somente manualmente` e `Após um prazo personalizado`, com a opção corrente indicada. | Switch ligado; listbox de modo aberto e operável por teclado. Input de prazo e seletor de unidade continuam ausentes enquanto a opção customizada ainda não for confirmada. |
| **4. ON — prazo personalizado** | Status e quadro são, sem alteração, os mesmos três textos do estado ON manual. Abaixo, label `Quando reativar a IA?`, valor `Após um prazo personalizado` e label `Prazo para reativação automática`. | Switch ligado; seletor de modo fechado; input numérico inteiro positivo e seletor de unidade fechado visíveis na mesma seção. A unidade hidratada/selecionada é `Horas` ou `Dias`; não existe input separado de 24 horas. |
| **5. Menu de unidade aberto** | Status e quadro são, sem alteração, os mesmos três textos do estado ON manual. Permanecem visíveis `Quando reativar a IA?`, `Após um prazo personalizado` e `Prazo para reativação automática`; o menu de unidade mostra, nesta ordem, `Horas` e `Dias`, com a opção corrente indicada. | Switch ligado; input de prazo visível; listbox de unidade aberto e operável por teclado. O menu de modo permanece fechado. |

Read-only preserva exatamente a estrutura e os textos do estado hidratado, mas switch, seletores e input ficam desabilitados e não abrem menus. Quando o prazo customizado é inválido, o erro localizado fica associado a `Prazo para reativação automática`; ele não altera a estrutura dos cinco estados acima.

## Backend Policy Contract

O resolvedor puro recebe o bucket persistido e `now`, não faz I/O e devolve uma das três decisões: `no_pause`, `pause_forever` ou `pause_until(now + duração)`. `pipeline.py` e `webhook.py` apenas aplicam essa decisão; não repetem parsing, precedência ou defaults. Comparações de enum são case-insensitive depois de `strip`; logs de tolerância identificam campo/motivo e tenant/session id, nunca conteúdo de mensagem, telefone ou valor bruto.

### Runtime resolver matrix

Esta matriz vale para JSONB já persistido, que pode anteceder o novo contrato. “Inválido no PATCH” abaixo significa 422 na fronteira de escrita; o runtime permanece tolerante para não derrubar mensagens enquanto o estado histórico é corrigido.

| Bucket persistido recebido | Decisão runtime | Regra de escrita / prova obrigatória |
|----------------------------|-----------------|--------------------------------------|
| Bucket `handoff` ausente, `null` ou não-objeto | `pause_forever` (`active/permanent`) | Não usar fallback de sete dias. Caso idêntico na matriz pura, pipeline e webhook. |
| Objeto sem os campos de política, inclusive `{}` (portanto `handoff_active` omitido) | Aplicar os defaults legados `handoff_active=yes`, `standby_mode=active` e `handoff_period=permanent`: `pause_forever` | Não usar fallback de sete dias. Se `handoff_active` estiver omitido, mas outro campo de política estiver presente, assumir somente `handoff_active=yes` e resolver os demais campos pela linha correspondente desta matriz. |
| Objeto com `handoff_active` diferente de `yes` (inclui `no`, enum desconhecido, tipo inválido ou `null`) | `no_pause`, independentemente dos demais campos | O PATCH aceita somente enum canônico permitido; valor inválido é 422. |
| `handoff_active=yes` com `standby_mode` fora de `inactive|active` | `pause_forever` e log de configuração tolerada | Enum inválido é 422 no PATCH; não usar fallback de sete dias. |
| `handoff_active=yes`, `standby_mode=inactive`, mesmo com período/durações antigos preenchidos | `pause_until(now + 24 horas)` | Ignorar `handoff_inactive_default_hours`, `handoff_period` e ambas as durações no runtime. Escrita canônica exige `permanent` e duas durações nulas. |
| `handoff_active=yes`, `standby_mode=active`, `handoff_period=permanent`, mesmo com durações antigas preenchidas | `pause_forever` | Ignorar durações no runtime. Escrita canônica exige ambas nulas. |
| `handoff_active=yes`, `standby_mode=active`, `handoff_period` ausente ou fora de `permanent|cycle` | `pause_forever`; log apenas para enum presente e inválido | Enum inválido é 422 no PATCH; ausência histórica equivale a `permanent`; não usar fallback de sete dias. |
| `active/cycle`, `handoff_cycle_hours` é número JSON inteiro entre 1 e 720 e dias está ausente/`null` | `pause_until(now + N horas)` | Forma canônica aceita. |
| `active/cycle`, `handoff_cycle_days` é número JSON inteiro entre 1 e 365 e horas está ausente/`null` | `pause_until(now + N dias)` | Forma canônica aceita. |
| `active/cycle`, dias é string legada somente de dígitos representando inteiro entre 1 e 365 e horas está ausente/`null` | `pause_until(now + N dias)` | Única coerção legada aceita pelo PATCH; persistir como número JSON. Cobrir também espaços, sinal, decimal, booleano e faixa como inválidos. |
| `active/cycle`, duração ausente, `null`, fora da faixa, fracionária, booleana ou malformada; nenhuma outra duração válida | `pause_until(now + 7 dias)` e log sem PII | Este e somente este caso usa o fallback legado de sete dias. O PATCH rejeita em vez de aplicar fallback. |
| `active/cycle`, ambas as durações estão preenchidas e ambas são válidas | Usar horas, ignorar dias e registrar conflito tolerado | O PATCH rejeita a combinação; teste de paridade fixa a precedência de horas no legado. |
| `active/cycle`, ambas preenchidas, mas somente uma é válida | Usar a duração válida e registrar conflito tolerado | O PATCH rejeita a combinação sem tentar saneá-la. Se nenhuma for válida, usar sete dias conforme a linha anterior. |

Cada linha deve existir como caso parametrizado no resolvedor puro e, com o mesmo `now`, como prova de integração nos chamadores de pipeline e takeover humano. Os testes dos chamadores comparam `should_pause`/`paused_until` à saída do resolvedor, inclusive logs/fallbacks relevantes, para impedir matrizes paralelas divergentes.

### PATCH merge, validation and rollout matrix

Os campos de política são `handoff_active`, `standby_mode`, `handoff_period`, `handoff_cycle_hours` e `handoff_cycle_days`. A validação nova é ativada se e somente se o PATCH enviar ao menos um deles. Nesse caso, o serviço faz merge sobre uma cópia do bucket persistido (ou sobre os defaults `yes/active/permanent/null/null` quando ele não existe), valida o bucket efetivo inteiro e só então substitui as cinco chaves canônicas, preservando todas as outras chaves. `handoff_inactive_default_hours` é legado não autoritativo e não ativa essa validação.

| Entrada / estado | Resultado obrigatório |
|------------------|-----------------------|
| PATCH não toca os cinco campos de política, inclusive PATCH global ou apenas `trigger_message`, `reactivation_delay_hours`, `meta_minutos_atendimento`, `sector_defaults`, `default_mode` ou `handoff_inactive_default_hours` | Prosseguir mesmo se a política histórica estiver malformada. Preservar o bucket e seus tipos/valores exatamente, sem validar, preencher defaults, limpar ou canonicalizar qualquer campo de política. |
| PATCH toca política e o bucket persistido está ausente | Fazer merge com `handoff_active=yes`, `standby_mode=active`, `handoff_period=permanent`, durações `null`; validar o resultado. |
| PATCH toca política e existe estado histórico malformado | Validar o estado efetivo pós-merge. O request precisa reparar todos os campos que deixariam a combinação inválida; caso contrário, 422. Não canonicalizar silenciosamente. |
| `handoff_cycle_days` recebido como número inteiro ou string ASCII somente de dígitos, dentro de 1–365 | Aceitar e armazenar número JSON inteiro. Esta é a única string numérica aceita para rollout legado. |
| `handoff_cycle_hours` como número inteiro 1–720 | Aceitar e armazenar número JSON inteiro; string, booleano, decimal ou valor fora da faixa retorna 422. |
| Enum/tipo inválido; `inactive` com período diferente de `permanent` ou qualquer duração não nula; `active/permanent` com duração não nula; `active/cycle` sem exatamente uma duração válida; ambas as durações preenchidas | Retornar 422 com erro de campo/combinação estável. `null` explícito conta como limpeza, ausência mantém o valor persistido no merge. |
| Bucket efetivo válido | Armazenar enums como strings canônicas lowercase, horas/dias como inteiro ou `null`, e exatamente uma duração somente em `active/cycle`; preservar todas as chaves não relacionadas do bucket. |
| Qualquer rejeição | Ocorrer antes de atribuir o JSONB à row e antes de `flush`, `commit`, incremento de versão ou invalidação de cache; provar que row, versão e cache permanecem inalterados. |

O `GET` expõe os tipos persistidos canônicos depois de uma escrita de política bem-sucedida, mas não reescreve dados históricos ao ler. Testes de config cobrem PATCH parcial para cada linha, round-trip, preservação de chaves desconhecidas/não relacionadas e spies ou fakes que provem a ordem atômica da rejeição.

## Dependencies

**depends-on:** handoff-sla-first-human-response
**blocks:** none

## Success Criteria

- [x] Switch desligado renderiza `Stand-by desativado` e `A IA volta a responder automaticamente 24 horas após o handoff.`; seletor, valor, unidade e o antigo input de 24 horas não existem no DOM visível.
- [x] Salvar desligado envia `standby_mode=inactive`, normaliza `handoff_period=permanent`, limpa `handoff_cycle_hours`/`handoff_cycle_days` e não envia `handoff_inactive_default_hours`, tornando manual o default caso o stand-by seja ligado após um reload.
- [x] Todo estado ligado, inclusive menus abertos, mantém o mesmo quadro com `Stand-by ativado`, `Escolha quando a IA poderá voltar a responder:` e as duas bullets exatas do contrato visual; abaixo dele, `Quando reativar a IA?` oferece somente `Somente manualmente` e `Após um prazo personalizado`.
- [x] `Somente manualmente` não mostra input de prazo e salva `standby_mode=active`, `handoff_period=permanent`, `handoff_cycle_hours=null` e `handoff_cycle_days=null`.
- [x] `Após um prazo personalizado` mostra `Prazo para reativação automática`, input numérico e seletor `Horas`/`Dias`; horas salvam somente `handoff_cycle_hours` e dias salvam somente `handoff_cycle_days`, ambos como número JSON inteiro.
- [x] Campo vazio, zero, negativo, decimal, valor acima de 720 horas ou 365 dias impede o PATCH e mostra erro localizado associado ao campo; corrigir o valor remove o erro.
- [x] Alternar desligado/ligado e manual/personalizado nunca deixa controles incompatíveis visíveis nem envia duas durações ao mesmo tempo.
- [x] Hidratação preserva configurações `active/permanent`, `active/cycle` em horas e `active/cycle` legadas em dias; valores ausentes usam defaults determinísticos sem produzir `NaN` nem string numérica no payload.
- [x] O PATCH valida o bucket de handoff efetivo após merge, aceita string inteira legada somente onde necessário para rollout, persiste números canônicos e retorna 422 para enum, tipo, faixa ou combinação inválida sem flush, commit, incremento de versão ou invalidação de cache.
- [x] A validação do bucket não elimina `trigger_message`, `reactivation_delay_hours`, `meta_minutos_atendimento`, `sector_defaults`, `default_mode` nem qualquer outra chave de handoff não alterada pelo PATCH.
- [x] Com `handoff_active` diferente de `yes`, nenhum dos dois caminhos pausa; com `standby_mode=inactive`, ambos pausam por exatamente 24 horas mesmo que exista `handoff_inactive_default_hours` antigo ou configuração de ciclo.
- [x] Com `active/permanent`, ambos criam pausa sem prazo (`paused_until=null`); com `active/cycle`, ambos calculam o mesmo instante para N horas ou N dias a partir de um relógio controlado.
- [x] Configuração legada válida em `handoff_cycle_days` continua funcionando; bucket ausente/não-objeto, objeto vazio/defaults e enums inválidos resolvem exatamente conforme a matriz sem derrubar webhook ou pipeline; o fallback de sete dias com log sem PII ocorre somente em `active/cycle` sem qualquer duração válida.
- [x] A checagem de pausa mantém a reativação preguiçosa: antes do deadline a mensagem é ignorada; no deadline ou depois a pausa é limpa e o pipeline continua.
- [x] No modo manual, a sessão aparece na Fila de Inativos, o comando existente a reativa imediatamente e a mensagem mestra continua seguindo o cooldown existente; nenhum `handoff_record` é reescrito por essas ações.
- [x] O handoff de verificação do link de agendamento continua sem pausar, e os demais gatilhos de handoff preservam resposta, persistência e efeitos existentes além do novo cálculo de prazo.
- [x] Builder e super-admin mantêm edição com tenant selecionado, investor mantém visualização read-only e clinic admin continua sem acesso à aba; todos os controles visíveis têm nome acessível e operação por teclado.
- [x] Todos os textos novos possuem equivalentes PT/EN e os testes frontend cobrem os cinco estados das referências, hidratação, payload, validação, read-only e ausência do input configurável de 24 horas.
- [x] Suites focadas, checks estáticos, testes completos backend/frontend, typecheck e build passam sem sobrescrever as mudanças concorrentes já presentes em `clinic-config.tsx` e no teste de HITL.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — +2 por política que governa estado persistido da sessão | engineer-standard / high | Firmar contrato/validação e substituir os dois cálculos de pausa por uma política backend única. |

### Wave 2 (after Wave 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 3 — +2 por formulário condicional stateful e +1 por rework sobre arquivo com mudanças concorrentes | engineer-standard / high | Implementar os estados visuais, hidratação, payload, validação e cobertura frontend conforme as referências. |

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** →
`engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high;
**4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an
independent `final-gate` at the highest justified effort. Codex maps these to
the `genie_*` profiles; other runtimes use their matching native roles. Keep
model and effort in runtime session/agent configuration, never skill frontmatter.

### Aggregate gate (after Wave 2)

```bash
make quality
pnpm --dir frontend/artifacts/neurostack test
pnpm --dir frontend/artifacts/neurostack typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack build
git diff --check
```

O gate documentado `make quality` executa formatação estática, lint, mypy e a suite Python completa; os comandos frontend cobrem toda a aplicação e o build Vite com as variáveis exigidas. Ele permanece separado dos checks focados de cada grupo porque esta mudança atravessa schema de configuração e runtime compartilhado.

## Execution Groups

### Group 1: Contrato e política única de pausa

**Goal:** Fazer API, pipeline e takeover humano consumirem uma única matriz validada de stand-by e reativação.

**Deliverables:**
1. Estender o contrato de configuração com `handoff_cycle_hours`, tipos/enums e validação pós-merge do bucket completo, mantendo chaves existentes e os limites de horas/dias.
2. Mapear a nova chave para o JSONB `handoff`, sem migration, e tornar o formato efetivo observável no OpenAPI do PATCH.
3. Criar um resolvedor puro de política que trate handoff desligado, 24 horas fixas, pausa manual, horas, dias e fallback legado, com relógio injetável no chamador.
4. Substituir pela política comum os branches equivalentes de `CognitivePipeline._apply_handoff_pause` e `_handle_clinic_message`, preservando logs sem PII e o carve-out de link de agendamento.
5. Adicionar testes de config, matriz pura, pipeline, provedores de webhook, expiração e reativação manual; provar ausência de mutação em PATCH rejeitado.

**Acceptance Criteria:**
- [x] O contrato HTTP e o JSONB satisfazem todos os critérios de tipo, faixa, combinação, compatibilidade e preservação de chaves.
- [x] Uma tabela de casos com o mesmo `now` produz decisões idênticas no resolvedor, pipeline e takeover humano.
- [x] Erros legados não escapam como `TypeError`/`ValueError`, e configuração válida não muda handoff, mensagem mestra, fila ou link de agendamento fora do prazo calculado.

**Validation:**
```bash
poetry run pytest \
  tests/handoff/test_pause_policy.py \
  tests/api/test_config.py \
  tests/core/test_pipeline.py \
  tests/api/test_webhook_zapi.py \
  tests/api/test_webhook_evolution.py \
  tests/api/test_webhook_uazapi.py \
  tests/api/test_webhook_meta.py \
  tests/api/test_webhook_egress.py \
  tests/api/test_sessions.py -v
poetry run black --check \
  src/handoff/pause_policy.py src/models/clinic_config.py src/api/schemas/config.py \
  src/api/routes/config.py src/services/config_service.py src/core/pipeline.py \
  src/api/routes/webhook.py tests/handoff/test_pause_policy.py tests/api/test_config.py \
  tests/core/test_pipeline.py tests/api/test_webhook_zapi.py \
  tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py \
  tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py tests/api/test_sessions.py
poetry run isort --check-only \
  src/handoff/pause_policy.py src/models/clinic_config.py src/api/schemas/config.py \
  src/api/routes/config.py src/services/config_service.py src/core/pipeline.py \
  src/api/routes/webhook.py tests/handoff/test_pause_policy.py tests/api/test_config.py \
  tests/core/test_pipeline.py tests/api/test_webhook_zapi.py \
  tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py \
  tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py tests/api/test_sessions.py
poetry run flake8 \
  src/handoff/pause_policy.py src/models/clinic_config.py src/api/schemas/config.py \
  src/api/routes/config.py src/services/config_service.py src/core/pipeline.py \
  src/api/routes/webhook.py tests/handoff/test_pause_policy.py tests/api/test_config.py \
  tests/core/test_pipeline.py tests/api/test_webhook_zapi.py \
  tests/api/test_webhook_evolution.py tests/api/test_webhook_uazapi.py \
  tests/api/test_webhook_meta.py tests/api/test_webhook_egress.py tests/api/test_sessions.py
poetry run mypy \
  src/handoff/pause_policy.py src/models/clinic_config.py src/api/schemas/config.py \
  src/api/routes/config.py src/services/config_service.py src/core/pipeline.py \
  src/api/routes/webhook.py --ignore-missing-imports
```

O escopo focado atravessa todos os adaptadores que chegam ao handler compartilhado e os dois escritores de pausa; checks estáticos incluem cada arquivo backend planejado. O gate agregado é obrigatório depois do frontend porque configuração/runtime compartilhados exigem a suite completa.

**depends-on:** none

---

### Group 2: Interface de configuração orientada por estado

**Goal:** Entregar na aba Handoff os estados claros das referências e serializar exatamente o contrato aprovado no Grupo 1.

**Deliverables:**
1. Reestruturar somente o bloco `Stand-by após handoff` com switch, explicações e controles condicionais equivalentes às referências em PT/EN.
2. Remover o estado/input de `handoff_inactive_default_hours`; hidratar modo, valor e unidade a partir de horas/dias e serializar ambos de forma mutuamente exclusiva.
3. Validar o prazo antes do save, exibir erro acessível/localizado e preservar comportamento read-only, tenant selecionado e as mudanças concorrentes do card HITL/API settings.
4. Ampliar `clinic-config-handoff.test.tsx` e a regressão de acesso/config para cobrir DOM, teclado, hidratação, transições, limites e corpo do PATCH nos perfis aplicáveis.

**Acceptance Criteria:**
- [x] Os cinco estados visuais, textos, ausência/presença de controles e comportamento read-only correspondem às referências e aos critérios globais.
- [x] Cada configuração válida faz round-trip sem trocar horas por dias; configurações inválidas não fazem request.
- [x] A aba mantém as regras atuais de perfil e nenhuma mudança HITL/API settings existente é removida ou reescrita.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run \
  --config vitest.config.ts \
  src/pages/clinic-config-handoff.test.tsx \
  src/pages/clinic-config-api-settings.test.tsx
pnpm --dir frontend/artifacts/neurostack typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack build
git diff --check -- \
  frontend/artifacts/neurostack/src/pages/clinic-config.tsx \
  frontend/artifacts/neurostack/src/pages/clinic-config-handoff.test.tsx \
  frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
```

Vitest cobre a máquina de estados e o payload; typecheck e build cobrem os limites React/Vite alcançados. O diff check é estreito para não atribuir ao grupo mudanças paralelas do worktree; o gate agregado continua responsável pela regressão completa.

**depends-on:** 1

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [x] Em builder e super-admin, abrir a aba Handoff com stand-by desligado: aparece somente a explicação de 24 horas, sem input configurável; salvar/recarregar mantém o estado.
- [x] Ligar o stand-by, alternar manual → horas → dias e salvar cada caso; o reload conserva modo, valor e unidade e a Network tab mostra somente o campo de duração correspondente.
- [x] Em modo manual, gerar handoff automático e takeover por mensagem humana; ambos ficam sem deadline, aparecem na Fila de Inativos e podem ser reativados pelo comando existente.
- [x] Em prazo personalizado, verificar com relógio controlado que uma mensagem anterior ao deadline não chama a IA e que uma mensagem no/depois do deadline limpa a pausa e segue o pipeline.
- [x] Com stand-by desligado e uma configuração legada diferente de 24 armazenada, os dois tipos de handoff usam 24 horas e a tela não oferece o valor legado.
- [x] Investor vê a configuração sem poder alterá-la; clinic admin continua sem a aba; teclado e leitor de tela identificam switch, seletor, input, unidade e erro.
- [x] O handoff por link de agendamento, o card HITL e os demais campos da configuração continuam funcionando sem alteração visual ou de payload não planejada.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| O nome interno `standby_mode=inactive` parece contradizer a pausa fixa de 24 horas | Medium | Manter o detalhe fora da UI e cobrir a tabela exata de mapeamento em testes de hidratação, payload e runtime. |
| Tenant legado pode ter `handoff_inactive_default_hours` diferente de 24 | Medium | A decisão de produto torna 24 autoritativo; ignorar sem apagar, documentar no QA e não fazer backfill destrutivo. |
| PATCH parcial pode formar combinação inválida com valores já persistidos | High | Validar o bucket depois do merge e provar rejeição atômica antes de qualquer escrita/cache. |
| Os dois caminhos de pausa podem voltar a divergir | High | Um único resolvedor puro e testes de paridade com relógio controlado; chamadores não recalculam duração. |
| “Mensagem mestra” pode ser entendida como reativação instantânea | Medium | O texto e QA tratam-na como ação manual que inicia o cooldown existente; o delay não é alterado nem exposto por esta tela. |
| `clinic-config.tsx` e seu teste já têm mudanças não relacionadas no worktree | Medium | Preservar os hunks HITL/API provider, revisar diff por arquivo e executar as regressões existentes antes do gate completo. |
| O wish dependente altera os mesmos arquivos de config/runtime | High | Só iniciar execução após `handoff-sla-first-human-response` concluir e reconciliar o baseline antes do Grupo 1. |

---

## Review Results

### Plan review — 2026-09-28T22:55:22Z

- **Context:** plan review
- **Reviewed SHA-256:** `ee419e2fd930cc21b7930146292991201aae60ce3305f9b0f03db6c14a339151`
- **Repository HEAD:** `96edb2579ded08c3a1b34549bb73d1ee86a77325`
- **Verdict:** FIX-FIRST
- **HIGH:** delimitar a matriz de fallback para configuração ausente, enums inválidos, duração ausente/malformada, strings legadas e duas unidades preenchidas, sem converter bucket ausente em ciclo de sete dias.
- **HIGH:** tornar executável a validação pós-merge, distinguindo input do PATCH de JSONB legado e impedindo que PATCH não relacionado seja bloqueado por estado histórico malformado.
- **HIGH:** registrar por que o direct wish dispensa brainstorm e tornar os cinco estados visuais autossuficientes sem depender dos anexos temporários da conversa.
- **Checks:** seções/grupos/dependências/validações inspecionados; fluxos atuais de pipeline, webhook e config rastreados; `git diff --check -- .gitignore` passou; linter opcional indisponível por ausência de script no repositório.

### Plan re-review — fix loop 1/2

- **Context:** plan review after first fix
- **Reviewed SHA-256:** `a59845c76fcc70e9b84645e094637db74bbc822c13544b018cc188bad5ff7390`
- **Repository HEAD:** `96edb2579ded08c3a1b34549bb73d1ee86a77325`
- **Verdict:** FIX-FIRST
- **HIGH:** o critério resumido ainda atribuía fallback de sete dias a configuração ausente/malformada em geral e a matriz não explicitava `{}`/`handoff_active` ausente.
- **HIGH:** o contrato visual autossuficiente parafraseava a descrição, o texto OFF, o quadro ON de duas bullets e o label do prazo, permitindo implementação diferente das referências.
- **Resolved:** matriz de PATCH/rollout, justificativa do direct wish, dependências, grupos e validações foram aceitos.

### Plan re-review — 2026-09-28T23:08:37Z (fix loop 2/2)

- **Context:** final plan review
- **Reviewed SHA-256:** `50c3cd200039b6907ede0b27334723d671e4964b3b68c1b6cc27f109c46c3d04`
- **Repository HEAD:** `96edb2579ded08c3a1b34549bb73d1ee86a77325`
- **Verdict:** SHIP
- **Severity findings:** none
- **Evidence:** matriz de runtime delimita defaults e fallback de sete dias; contrato visual contém a cópia PT e os cinco estados; matriz de PATCH é consistente e atômica; escopo, simplicidade, dependências, grupos, validações focadas e gate agregado foram aceitos.
- **Checks:** SHA exato, inspeção integral, buscas de strings/contratos e rastreamento read-only de pipeline, webhook, config, frontend, Makefile e scripts; linter opcional indisponível, sem execução de suites que escreveriam caches.

### Group 1 Execution — 2026-09-29T05:30:26Z (root orchestration)

- **Delivery:** resolvedor puro único, validação PATCH pós-merge/atômica, contrato GET/PATCH documentado, `handoff_cycle_hours`, integração em pipeline/takeover, relógio injetável, lazy unpause e compatibilidade legada foram implementados sem migration.
- **Pre-gate:** a análise read-only encontrou gaps de OpenAPI, erro estável, bucket não-objeto, cooldown legado, expiração e cobertura de providers; todos foram fechados antes da primeira revisão formal.
- **Execution review:** FIX-FIRST por string de dias com 5.000 dígitos, cooldown mestre inteiro não representável e `default:null` contraditório no OpenAPI. Fix loop 1/2 tornou parsing/cooldown totais e manteve enums omitíveis, não-null e sem default inválido; re-review SHIP sem gaps CRITICAL/HIGH/MEDIUM.
- **Quality review:** FIX-FIRST por possível reflexão do corpo/credencial no 422, parsing PATCH não bounded e provas de wiring acopladas a métodos privados. Fix loop 2/2 adicionou envelope sanitizado `invalid_handoff_policy`, parser bounded compartilhado e provas públicas por `CognitivePipeline.process` e endpoint UAZAPI `fromMe`; re-review SHIP sem gaps CRITICAL/HIGH.
- **Validation:** 368 testes focados PASS; Black, isort, Flake8, mypy, compileall e scoped `git diff --check` PASS.
- **Residual accepted:** em payload com erros mistos, o envelope prioriza violações da policy e o erro não-policy reaparece após corrigi-las; o request continua rejeitado antes de mutação e nenhum input bruto/segredo é refletido.
- **Hash drift:** nenhum nos 11 artefatos congelados; Grupo 1 liberado para o frontend sequencial do Grupo 2.

### Group 2 Execution — 2026-09-29T06:12:43Z (root orchestration)

- **Delivery:** os cinco estados visuais, textos PT/EN, controles condicionais, hidratação hours-first/dias legados, validação acessível e payload canônico mutuamente exclusivo foram implementados somente no bloco de stand-by, preservando HITL/API/TTS.
- **Execution review:** SHIP sem gaps CRITICAL/HIGH/MEDIUM; DOM OFF, menus Radix, keyboard, read-only, perfis, tenant selecionado, payload numérico e ausência do campo legado foram aceitos.
- **Quality review:** FIX-FIRST porque `clinic_admin` ainda validava/enviava policy invisível, `detail.violations` não era acionável e os testes não provavam round-trip/resposta pendente. Fix loop 1/2 omitiu integralmente os cinco campos para perfil sem a aba, adicionou labels PT/EN seguros e uma guarda por geração/tenant contra snapback; re-review SHIP sem gaps CRITICAL/HIGH/MEDIUM.
- **Validation:** 88 testes focados PASS (36 Handoff + 52 API settings); TypeScript `--noEmit`, build Vite production, Prettier do teste e scoped `git diff --check` PASS. Warnings de sourcemap/chunk e React `act()` permanecem conhecidos e não bloqueantes.
- **Preservation:** `clinic-config-api-settings.test.tsx` permaneceu byte a byte no hash congelado `ba84e32e…`; package/lock, locales e hunks concorrentes não foram reescritos.
- **Hash drift:** nenhum na página `c4fdefe1…` e teste Handoff `acd0b4c9…`; Grupo 2 liberado para o gate agregado.

### Aggregate Validation — 2026-09-29T03:27:00-03:00 (root orchestration)

- **Backend static gate:** `make quality` concluiu Black (302 arquivos inalterados), isort, Flake8 e mypy antes da suite; os checks focados posteriores da integração de clone também passaram em Black, isort, Flake8, mypy e `git diff --check`.
- **Backend full suite:** a execução completa terminou com 3.363 testes passando, 21 pulados e 48 warnings. O único failure atribuível a este wish foi a ausência dos cinco campos canônicos no allowlist de clone; a correção preserva `handoff_active`, `standby_mode`, `handoff_period`, `handoff_cycle_hours` e `handoff_cycle_days`, passou em 60 testes de clone/policy e recebeu re-review independente **SHIP** sem gaps.
- **Contaminação externa explicitada:** a repetição dos cinco nodes originalmente falhos deixou o node de clone verde e exatamente quatro failures externos: os dois nodes de scheduling-link já catalogados em `tests/baselines/neurostack-core-extraction-known-failures.txt` e dois contratos de archive causados pelas mudanças concorrentes preexistentes `neurostack_core/protocol.py`, `neurostack_core/presets/**` e `packages/neurostack-core/pyproject.toml`. Nenhum desses artefatos foi alterado por este wish; portanto o processo global permanece non-zero, mas não resta failure da entrega.
- **Frontend full gate:** Vitest completo passou com 28 arquivos / 353 testes; TypeScript `--noEmit` passou; Vite production transformou 2.804 módulos e concluiu o build. Foram usados os executáveis locais fixados do workspace com o mesmo cwd/config dos scripts, pois o wrapper global `pnpm --dir` tenta reinstalar o workspace e é recusado pelo `preinstall` deste repositório.
- **Warnings não bloqueantes:** permanecem os warnings React `act()`, sourcemap dos componentes UI e chunk maior que 500 kB já observados nas suites anteriores.
- **Integrity:** `git diff --check` global passou. Hashes finais relevantes: policy `0fb9816f…`, config route `1d4ed759…`, pipeline `436f2f0f…`, webhook `b18e75ce…`, clone service `8a85f7ac…`, clone test `7ad20140…`, página `c4fdefe1…`, teste Handoff `acd0b4c9…` e regressão API settings preservada `ba84e32e…`.

### Final aggregate review — 2026-09-29T03:32:50-03:00

- **Verdict:** SHIP; o wish pode ser marcado **COMPLETE**.
- **Severity findings:** nenhum CRITICAL, HIGH ou MEDIUM.
- **Independent proof:** 119 testes backend de policy/config/clone e 88 testes frontend focados passaram no snapshot revisado; hashes de produção/testes, atomicidade do PATCH, paridade pipeline/takeover, compatibilidade legada, estados/accessibilidade da UI, perfis/tenant, clone e preservação de mudanças concorrentes foram verificados.
- **Aggregate classification:** o node de clone desta entrega permanece verde; os quatro failures globais restantes foram confirmados como dois baselines catalogados de scheduling-link e dois contratos de archive pertencentes às mudanças concorrentes de `neurostack-core`, sem defeito aberto deste wish.
- **Closure:** todos os 32 critérios foram satisfeitos por testes automatizados, inspeção independente e evidência congelada acima.

---

## Files to Create/Modify

```
src/handoff/pause_policy.py                                      # novo resolvedor puro da política
src/models/clinic_config.py                                     # contrato validado do bucket handoff
src/api/schemas/config.py                                       # campos tipados/documentados no PATCH
src/api/routes/config.py                                        # validação pós-merge do bucket
src/services/config_service.py                                  # mapeamento de handoff_cycle_hours
src/core/pipeline.py                                             # consumo da política no handoff da IA
src/api/routes/webhook.py                                        # consumo da política no takeover humano
src/services/clinic_clone_service.py                             # preservação da política ao clonar clínica
tests/handoff/test_pause_policy.py                               # matriz pura e legado
tests/api/test_config.py                                         # round-trip e rejeição atômica
tests/core/test_pipeline.py                                      # integração do handoff automático
tests/api/test_webhook_zapi.py                                   # regressão de provedor
tests/api/test_webhook_evolution.py                              # regressão de provedor
tests/api/test_webhook_uazapi.py                                 # takeover e paridade temporal
tests/api/test_webhook_meta.py                                   # regressão de provedor
tests/api/test_webhook_egress.py                                 # mídia/fromMe no handler comum
tests/api/test_sessions.py                                       # expiração e reativação manual
tests/services/test_clinic_clone_service.py                      # round-trip exato da política no clone
frontend/artifacts/neurostack/src/pages/clinic-config.tsx        # UI, hidratação, validação e payload
frontend/artifacts/neurostack/src/pages/clinic-config-handoff.test.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
```
