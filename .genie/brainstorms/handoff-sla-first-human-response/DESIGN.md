# Design: SLA de atendimento confirmado no handoff

| Field | Value |
|-------|-------|
| **Slug** | `handoff-sla-first-human-response` |
| **Date** | 2026-09-27 |
| **WRS** | 100/100 |

## Problem

O radar da fila compara corretamente o limite matemático para handoffs concluídos, mas chama de atendimento humano um checkout disparado antes da resposta, ignora oportunidades vencidas e timeout, reaplica a meta atual ao histórico e lê um ciclo de vida diferente dos endpoints tradicionais. Isso pode mostrar um percentual tecnicamente calculado e operacionalmente falso; o produto precisa medir uma confirmação humana explícita, com meta por ocorrência, fonte de dados única e cobertura verificável de ponta a ponta.

## Scope

### IN

- Definir o KPI como **tempo até atendimento confirmado**, contado de `HandoffRecord.created_at` até `data_hora_saida`.
- Separar “Abrir WhatsApp” de “Confirmar atendimento”: abrir não altera o handoff; confirmar grava o primeiro timestamp e retira o caso da fila de espera.
- Adicionar `handoff_records.sla_target_seconds INTEGER NULL`, capturado na criação a partir de `meta_minutos_atendimento * 60` na mesma transação.
- Persistir `updated_at TIMESTAMPTZ NULL` e um `lifecycle_version` monotônico interno para preservar o contrato público e serializar decisões concorrentes.
- Validar novas configurações de meta entre 1 e 120 minutos; configuração ausente ou legado inválido usa 10 minutos para novas ocorrências sem reescrever o valor antigo.
- Tornar PostgreSQL/`handoff_records` a única fonte de verdade de criação, leitura e transições; remover a dependência funcional do singleton `_handoffs`.
- Aplicar transições atômicas e write-once ao atendimento confirmado, mantendo `resolved_at` como encerramento distinto.
- Calcular média, numerador, denominador e percentual por coorte de `created_at` no período civil da clínica.
- Usar o fuso IANA configurado da clínica como autoridade única para coorte do radar, filtro/renderização dos cards e CSV; devolver o contrato temporal resolvido pela API.
- Incluir confirmados, timeout vencido e abertos vencidos no denominador; excluir abertos ainda dentro do prazo e cancelados sem confirmação.
- Retornar percentual/média nulos quando não houver denominador/amostra e contagens suficientes para explicar o radar e a cobertura legada.
- Fazer cada card usar seu `sla_target_seconds`; remover thresholds fixos 5/10 e o alvo percentual fixo de 85%.
- Cobrir domínio, query PostgreSQL, endpoint e apresentação frontend com testes permanentes.
- Preservar Event Store, entrega/status de webhooks e métricas públicas da iniciação e das transições durante a retirada do storage em memória.
- Alinhar e superseder, nos pontos incompatíveis, o wish em andamento `handoff-export-open-closed`: o CSV passa a usar o fuso da clínica e inclui `in_progress` confirmado.

### OUT

- Alegar que o clique manual prova uma mensagem enviada; a microcopy usa “confirmado”, nunca “comprovado automaticamente”.
- Inferir autoria humana de `fromMe`, pausar/concluir handoff a partir de webhooks ou endurecer webhooks Evolution/Uazapi.
- Capturar resposta outbound humana na Meta Cloud API ou criar um composer de mensagens dentro da NeuroStack.
- Backfill de `sla_target_seconds` ou cópia de `data_hora_saida` para outro conceito em registros legados.
- Alertas de violação, WebSocket, materialização, cache, job periódico ou coluna de duração/resultado derivado.
- Objetivo percentual configurável (por exemplo, 85%); esta entrega configura somente o limite de tempo de cada ocorrência.
- Motivos estruturados de cancelamento ou política de inclusão seletiva por motivo.
- Outbox, cache, job de reconciliação ou garantia transacional distribuída entre PostgreSQL, Event Store e webhook.

## Approach

### 1. Contrato temporal e persistência

Uma migration adiciona três colunas: `sla_target_seconds INTEGER NULL` com `CHECK (sla_target_seconds BETWEEN 60 AND 7200)` para valores não nulos; `updated_at TIMESTAMPTZ NULL`; e `lifecycle_version INTEGER NOT NULL DEFAULT 0` com `CHECK (lifecycle_version >= 0)`. Não há backfill de SLA nem de `updated_at`: linhas anteriores permanecem não mensuradas e conservam `updated_at=null`; `lifecycle_version=0` apenas estabelece a versão inicial necessária ao compare-and-set. O downgrade remove constraint e as três colunas.

Na criação do handoff, o serviço lê e normaliza `meta_minutos_atendimento` na mesma sessão/transação que insere `HandoffRecord`. Inteiro real entre 1 e 120 vira segundos; campo ausente ou legado inválido gera 600 segundos e log estruturado sem conteúdo do paciente. O snapshot é imutável depois do INSERT.

O backend resolve uma vez por request o fuso IANA de `operations.timezone` da clínica. Ausência ou valor inválido usa o fallback já estabelecido pelo produto, `America/Sao_Paulo`, com log estruturado; o navegador nunca é autoridade e um parâmetro `timezone` enviado pelo cliente não muda a coorte. Datas civis inclusivas da UI são convertidas nesse fuso para `[period_start_utc, period_end_exclusive_utc)`. A mesma query backend aplica esse intervalo tanto à população do KPI quanto a `items`; não existe segunda filtragem temporal da lista. A resposta da fila/radar devolve `period_timezone`, `period_start` e `period_end_exclusive`, com offsets explícitos. O frontend remove o filtro local `ymd(new Date(item.created_at))` e renderiza exatamente os `items` devolvidos para o período resolvido, usando o contrato da API nos cabeçalhos.

O export usa exatamente o mesmo resolvedor e intervalo. Isso supersede as decisões 2 e 6, critérios, deliverables e QA correspondentes de `.genie/wishes/handoff-export-open-closed/WISH.md` que tornam o fuso do navegador autoridade ou usam UTC na ausência do parâmetro. Antes de implementar este design, o wish de export deve ser alinhado: remover o envio de `Intl.DateTimeFormat().resolvedOptions().timeZone`, consumir o fuso/período resolvido pela API e manter timestamps de células em UTC. O contrato unificado é provado com navegador e clínica em fusos diferentes, bordas de meia-noite e dias de DST de 23 e 25 horas.

`created_at` continua sendo a entrada. `data_hora_saida` passa a ser documentado como a primeira confirmação manual de que o atendimento humano começou. `resolved_at` continua sendo o término. `updated_at` preserva duravelmente o campo público hoje mantido em `_handoffs`: nasce nulo e toda mutação pública de domínio aplicada (status, assignment, notes ou verification) grava o instante timezone-aware do servidor; `lifecycle_version` incrementa na mesma instrução. Bookkeeping posterior de delivery atualiza somente `webhook_sent`, `webhook_response` e a chave JSONB `webhook_status`, sem tocar campos de lifecycle, `updated_at` ou `lifecycle_version`. Nenhuma duração nem booleano `within_sla` é persistido.

Registros legados com `sla_target_seconds IS NULL` não entram no KPI novo, mesmo que tenham `data_hora_saida`. A resposta expõe `unmeasured_legacy_count` dentro da coorte e a UI informa que esses casos não têm meta histórica mensurável.

### 2. Ciclo de vida canônico e arquitetura do serviço

`HandoffService` torna-se stateless e recebe uma factory de sessão/unit of work; o singleton pode guardar dependências imutáveis, mas nunca uma `AsyncSession`. Cada operação abre sua própria sessão/transação, instancia `HandoffRecordRepository` e encerra a unidade de trabalho antes de retornar. `_handoffs` deixa de ser storage funcional. Falha ao inserir o registro SQL interrompe a iniciação com erro tipado, em vez de retornar sucesso e somente registrar warning. Instâncias A, B e C do serviço enxergam a mesma criação e transições via PostgreSQL.

O repositório concentra lookups e compare-and-set tenant-scoped. A leitura que decide a operação captura `observed_status` e `observed_lifecycle_version`. Toda transição executa uma única instrução equivalente a `UPDATE ... SET ..., updated_at=now(), lifecycle_version=lifecycle_version+1 WHERE tenant_id=:tenant_id AND id=:handoff_id AND status=:observed_status AND lifecycle_version=:observed_lifecycle_version [AND precondição específica] RETURNING ...`. Confirmação acrescenta `data_hora_saida IS NULL`; conclusão acrescenta `data_hora_saida IS NOT NULL`; terminais aplicam suas precondições da matriz. `now()` fornece somente o valor público de `updated_at`; nunca é usado como token de concorrência. Métodos retornam resultado tipado `applied(record)`, `not_found`, `invalid_state(current_status)` ou `conflict`.

Se duas transações partem do mesmo estado/versão observados, ambas carregam os mesmos valores esperados. O PostgreSQL pode fazer a segunda aguardar o lock físico da linha, mas, ao acordar, reavalia o predicado original fixo: ela não adota a nova versão/status e não pode virar sucesso tardio. Exatamente uma instrução retorna linha; a outra retorna zero linhas. A leitura diagnóstica posterior distingue ausência/tenant, estado inicialmente inválido e concorrência; se o ID existe com `lifecycle_version` diferente da observada, o resultado é sempre `conflict`, sem reaplicar a matriz ao novo estado. Isso vale para confirmação versus cancelamento/timeout (e qualquer terminal cujas precondições sejam válidas no estado semeado): nunca resulta em `in_progress` com timestamp terminal incoerente. A camada de serviço é a única dona da matriz e traduz resultados; rotas não fazem `get` prévio nem duplicam autorização/transição.

Um mapper canônico e puro `HandoffRecord + RecentMessageProjection -> HandoffDetail/HandoffListItem` atende create/get/list/session/pending e webhooks, sem dicionário paralelo. Para preservar o contrato atual, a persistência fica explícita: `summary` guarda o texto; colunas guardam status, urgency, assigned/resolution, `webhook_sent`, `updated_at` e `lifecycle_version`; `context` JSONB guarda chaves versionadas `sector`, `sla`, `key_info`, `verification_status`, `funnel_stage`, `turn_count` e `webhook_status`; `webhook_response` guarda resposta/erro sanitizado de delivery. `lifecycle_version` é interno e não entra no schema público; `updated_at` continua opcional e timezone-aware no `HandoffDetail`/OpenAPI e sobrevive a restart.

`recent_messages` não é copiado para `handoff_records`: isso ampliaria retenção e duplicaria PII clínica. O assembler de detalhe/webhook reconstrói uma projeção somente-leitura dos últimos cinco eventos conversacionais da sessão pelo `EventStore` já existente, sob sua política atual de tenant e retenção; limita o conteúdo ao mesmo `role/content` já exposto e não persiste a projeção. Listagens não carregam mensagens. Se os eventos já expiraram ou o Event Store opcional não está configurado, o contrato continua devolvendo `recent_messages=[]`, sem fabricar dados. O mapper aplica defaults compatíveis a registros legados. Testes de round-trip cobrem os campos persistidos; testes da projeção cobrem tenant, limite, ordem e ausência/expiração.

As transições aceitas são:

| Origem | Destino permitido |
|---|---|
| `pending` | `accepted`, `in_progress`, `cancelled`, `timeout` |
| `accepted` | `in_progress`, `cancelled`, `timeout` |
| `in_progress` | `completed`, `cancelled`, `timeout` |
| `completed`, `cancelled`, `timeout` | nenhum |

O endpoint de confirmação aceita `pending`, `accepted` ou `in_progress`, executa update condicional somente quando `data_hora_saida IS NULL`, grava o horário UTC do servidor e deixa o status em `in_progress`. Uma repetição não sobrescreve o timestamp e retorna 409 `attendance_already_confirmed`. A conclusão exige atendimento já confirmado; tentativa de concluir sem `data_hora_saida` retorna 409 `attendance_not_confirmed`, sem inventar quando o atendimento começou.

O PATCH genérico não pode entrar em `in_progress`: retorna 409 `attendance_confirmation_required` e orienta o endpoint de confirmação. Assim toda entrada nesse estado passa pela mesma operação atômica que grava `data_hora_saida`. Os endpoints dedicados `accept`, confirmação, `complete`, `cancel` e `timeout` chamam a mesma camada lifecycle; nenhum método de modelo ou rota aplica uma segunda matriz.

O endpoint legado `/queue/{id}/checkout` pode permanecer como alias documentado e depreciado da confirmação, mas não pode continuar marcando `completed`. A UI usa o nome/contrato de confirmação. Updates condicionais incluem `tenant_id`, estado permitido e timestamp nulo para impedir corrida e escrita cross-tenant.

#### Side effects públicos

Somente o resultado `applied(record)` de um CAS/INSERT dispara efeitos. A ordem é: (1) mutação e commit PostgreSQL; (2) append no Event Store com os mesmos tipos/payloads públicos; (3) envio do webhook criado/atualizado e persistência do resultado de delivery em uma nova unidade de trabalho curta; (4) incremento das métricas Prometheus e retorno. O perdedor da corrida, identificado por zero linhas retornadas para a versão observada, encerra como conflito e não emite evento/webhook nem incrementa sucesso, mesmo que ao acordar encontre um novo estado agora permitido pela matriz. A política de erro existente é preservada: falha do Event Store continua erro da operação já persistida; falha de webhook é não fatal, fica registrada como failed; métricas de erro/sucesso mantêm labels públicas. Não há promessa de atomicidade entre sistemas nem retry novo nesta entrega, portanto outbox não é necessária. Testes de regressão fixam quantidade, ordem relativa, payload/status e ausência de efeitos em conflito para iniciação, aceite, confirmação, conclusão, cancelamento e timeout.

### 3. População e cálculo do radar

A coorte é definida por `created_at` no intervalo civil selecionado da clínica, convertido para `[period_start_utc, period_end_exclusive_utc)`. O backend usa o fuso configurado da clínica; não depende do fuso do navegador nem de `time.max` inclusivo.

Para cada registro mensurável, `deadline = created_at + sla_target_seconds` e:

- **dentro:** `data_hora_saida IS NOT NULL` e `data_hora_saida - created_at <= sla_target_seconds`;
- **fora após confirmação:** timestamp existe e duração é maior que a meta;
- **fora sem confirmação:** timestamp é nulo, status não é `cancelled` e `as_of >= deadline`;
- **ainda maturando:** timestamp é nulo e `as_of < deadline`, fora do denominador;
- **cancelado não atendido:** timestamp é nulo e status é `cancelled`, fora do denominador e contado separadamente;
- **cancelado após atendimento:** timestamp existe e segue a mesma comparação dos demais atendidos.

As categorias retornadas são disjuntas e exaustivas dentro da coorte: `within_sla_count`, `confirmed_late_count`, `unanswered_overdue_count`, `maturing_count`, `cancelled_unconfirmed_count` e `unmeasured_legacy_count`. Primeiro separa-se legado sem snapshot; entre mensuráveis, confirmação tem precedência sobre status atual, depois cancelado não confirmado, prazo vencido e ainda maturando. Assim legado cancelado aparece somente em `unmeasured_legacy_count`, e cancelado confirmado aparece somente em `within` ou `confirmed_late`; nenhuma linha infla duas contagens.

Assim:

```text
eligible = confirmed_any_time + unanswered_after_deadline
within = confirmed_with_duration_lte_snapshot
pct_within_sla = round_half_up(100 * within / eligible, 1)
```

Timeout não antecipa o deadline: sem confirmação, torna-se violação quando a meta amadurece. Exatamente o limite é sucesso (`<=`); no instante exato sem resposta, o caso já é violação (`as_of >= deadline`). O relógio `as_of` é capturado uma vez por request e injetável no serviço de estatísticas para testes.

`avg_wait_seconds` usa somente ocorrências com confirmação mensurável e pode ser nulo. `pct_within_sla` também é nulo quando `eligible_count == 0`; ausência nunca vira 0%. Percentual usa decimal/numeric e arredondamento explícito `ROUND_HALF_UP` a uma casa, nunca o `round` binário/half-even da linguagem. A API retorna todas as categorias acima, `confirmed_count` e `sla_eligible_count`; invariantes verificáveis são `confirmed_count = within + confirmed_late`, `eligible = within + confirmed_late + unanswered_overdue` e a soma das seis categorias igual à coorte.

Exemplo normativo para meta de 10 minutos:

| Caso | Resultado |
|---|---|
| confirmado em 599s | dentro |
| confirmado em 600s | dentro |
| confirmado em 601s | fora |
| timeout sem resposta, deadline vencido | fora |
| aberto com 601s | fora |
| aberto com 599s | ainda não elegível |
| cancelado sem confirmação | excluído e contado separadamente |

Uma coorte com 2 dentro, 1 confirmado atrasado, 1 timeout vencido, 1 aberto vencido, 1 aberto ainda no prazo e 1 cancelado sem confirmação retorna `within_sla_count=2`, `sla_eligible_count=5` e `pct_within_sla=40.0`.

### 4. API e interface

`HandoffQueueItem` passa a expor `sla_target_seconds`. `HandoffQueueResponse` torna `avg_wait_seconds` e `pct_within_sla` opcionais, adiciona as contagens normativas e o trio `period_timezone`, `period_start`, `period_end_exclusive`. Mudança de schema, nulabilidade e required/optional no OpenAPI são cobertas por contract tests.

O CSV da coorte inclui `pending`, `accepted`, `completed` confirmado e também `in_progress` confirmado. Para este último, `tempo_espera_segundos = data_hora_saida - created_at`, como nos demais confirmados; ele não é tratado como aberto. `in_progress` sem timestamp é estado inválido e não pode ser criado pelos contratos novos. `cancelled` e `timeout` continuam fora do export deste wish, mesmo quando participam da estatística agregada. Essa decisão supersede explicitamente a exclusão total de `in_progress` e os testes correspondentes de `.genie/wishes/handoff-export-open-closed/WISH.md`; o wish deve ser atualizado antes da execução conjunta.

Na página:

- “Abrir WhatsApp” chama apenas `window.open`; falha ou bloqueio do popup não muda estado.
- “Confirmar atendimento” chama o endpoint, bloqueia double-click enquanto a request está em voo e remove o card somente após 2xx.
- A ajuda diz: “Confirme depois de iniciar o atendimento ao paciente.”
- O radar mostra o percentual retornado e “{within} de {eligible} dentro da meta”; com denominador zero mostra “Sem dados elegíveis”.
- O texto fixo `Meta: ≥ 85%` é removido. A UI explica que cada handoff usa a meta vigente na entrada.
- O tempo médio é rotulado como média dos atendimentos confirmados e mostra “Sem dados” quando nulo.
- Cores usam a razão `elapsed / sla_target_seconds`: verde `< 0,5`, amarelo `>= 0,5 e <= 1`, vermelho `> 1`. Portanto, o limite exato ainda é amarelo e dentro da meta.
- Linhas legadas sem snapshot não recebem uma cor de SLA inventada; usam estilo neutro e entram na contagem não mensurada.

### 5. Estratégia de testes

Uma função pura de classificação recebe `created_at`, `confirmed_at`, `target_seconds`, `status` e `as_of`; seus testes sempre rodam e cobrem a matriz completa, categorias disjuntas, invariantes, `ROUND_HALF_UP`, 599/600/601, maturação, timeout, cancelamento, legado nulo e datetimes timezone-aware.

Um teste de integração PostgreSQL usa o padrão existente de banco filho descartável via `POSTGRES_SMOKE_DATABASE_URL`, aplica upgrade/downgrade e chama o método de produção que calcula as estatísticas através do repositório/serviço; é proibido copiar a SQL para o teste. Ele cobre a coorte normativa, tenant, `as_of`, categorias e arredondamento. HTTP/SQLite cobre somente schema/contrato com o serviço injetado e nunca conta como evidência da aritmética temporal.

Como o repositório não possui workflow CI versionado, esta entrega adiciona o gate normativo executável `scripts/check-handoff-sla-postgres.sh`. O script falha antes do pytest se `POSTGRES_SMOKE_DATABASE_URL` estiver ausente/vazia, executa exatamente `poetry run pytest tests/integration/test_handoff_sla_postgres.py::test_handoff_sla_postgres_smoke -q -rs` e valida código zero e exatamente zero skipped/xfailed, reutilizando `scripts/validate-pytest-completion.py` ou checagem equivalente. `tests/scripts/test_check_handoff_sla_postgres.py` prova variável ausente, propagação de falha e rejeição de skipped/xfailed com subprocesso simulado. O comando obrigatório de validação é `POSTGRES_SMOKE_DATABASE_URL='<admin-url-local>' ./scripts/check-handoff-sla-postgres.sh`; não se inventa `.github/workflows`.

Testes de migration cobrem upgrade/downgrade das três colunas, `sla_target_seconds=NULL` legado, `updated_at=NULL` legado, `lifecycle_version=0`, limites SLA 60/7200 aceitos, 59/7201 rejeitados e versão negativa rejeitada. Configuração rejeita `0`, `121`, boolean, float, string e null explícito; ausência/legado inválido usa fallback 600 somente na criação. Contract tests cobrem nulabilidade, contagens, período e `updated_at` timezone-aware no OpenAPI, sem expor `lifecycle_version`.

Concorrência real PostgreSQL usa duas `AsyncSession` independentes: ambas leem o mesmo `status/lifecycle_version`, uma barreira as libera e um timeout impede falso passe. Duas confirmações rendem exatamente um `applied` e um `conflict`; confirmação versus cancelamento/timeout, parametrizada pelos estados de origem válidos, também rende exatamente um vencedor. O perdedor não refaz a operação com a versão nova. Uma terceira sessão comprova `lifecycle_version=versão_inicial+1`, um único conjunto coerente de status/timestamps, `updated_at` do vencedor e exatamente um conjunto de side effects. O teste de restart cria com serviço A, lê/transiciona com B e confirma com C que `updated_at`/estado permanecem duráveis.

Testes de serviço/rota provam snapshot imutável, isolamento de tenant, matriz única, PATCH `in_progress` rejeitado, bloqueio de conclusão sem confirmação, mapper/round-trip dos campos enriquecidos, projeção bounded de mensagens sem cópia no JSONB e efeitos Event Store/webhook/métricas sem duplicação em conflitos. Timezone cobre navegador diferente da clínica, bordas locais, DST de 23h/25h e fallback de configuração inválida; em todos os casos, IDs de `items` e população do KPI vêm do mesmo intervalo backend.

Testes frontend usam promises controladas: abrir WhatsApp não faz POST; double-click em confirmar gera um POST; card permanece até 2xx; 409/500 mantém card e exibe erro; `null` mostra no-data; 2/5 mostra 40%; 66,7% mantém label acessível e 67% visual; cores cobrem imediatamente abaixo de 50%, exatamente 50%, exatamente 100% e acima de 100%. Um caso browser≠clínica prova que a página não executa `ymd(new Date(...))` nem remove `items` já filtrados pelo backend. CSV prova o mesmo período/fuso da clínica e a inclusão de `in_progress` confirmado.

### Alternativas rejeitadas

- **Congelar apenas o comportamento atual com mais mocks:** protege o wiring visual, mas não detecta população errada, checkout prematuro ou SQL PostgreSQL divergente.
- **Usar `fromMe` como resposta humana:** Z-API/Evolution/Uazapi apenas indicam saída da conta após exclusão de IDs conhecidos; automações externas podem parecer humanas, Evolution/Uazapi ainda têm lacunas de autenticação e Meta não entrega outbound próprio no fluxo atual.
- **Criar `first_human_response_at` agora:** sem evento uniforme, o campo seria alimentado por proxies de confiança diferente e voltaria a misturar conceitos.
- **Aplicar a meta atual a todo o histórico:** simplifica o schema, mas uma edição reclassifica resultados passados e não responde qual meta valia quando o atendimento entrou.
- **Backfill com 10 minutos ou configuração atual:** inventa uma meta histórica sem evidência.
- **Manter 85% como segunda meta:** não existe configuração/contrato para esse número e ele confunde alvo por ocorrência com objetivo agregado.

## Simplicity Case

- **Simplest complete design:** snapshot de SLA, confirmação manual write-once, `updated_at` durável, uma versão inteira para CAS, serviço stateless, cálculo derivado e fuso da clínica único.
- **Added machinery:** `sla_target_seconds` impede reclassificação histórica; `updated_at` preserva um campo público que desapareceria com `_handoffs`; `lifecycle_version` é o menor mecanismo que impede duas decisões baseadas no mesmo estado de virarem sucesso após espera de lock. Unit-of-work/repositório evitam sessão no singleton; mapper preserva campos; teste/gate PostgreSQL protege intervalo e concorrência reais.
- **Deferred until measured:** origem/evidência automática, `first_human_response_at`, composer interno e hardening dos webhooks só voltam quando houver evento outbound autenticado equivalente em todos os provedores ativos ou envio humano dentro do produto; cache/materialização só volta se a query exceder o orçamento medido.
- **Complexity removed:** nenhum cache, job, outbox, retry state machine, evento cross-provider, duração/booleano derivado, backfill especulativo, modo dual de KPI ou objetivo percentual adicional. Há três colunas simples e nenhum recovery path novo; a versão só cresce em updates e não cria histórico/reconciliação.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Medir atendimento confirmado manualmente | É o único evento uniforme e testável em todos os provedores atuais. |
| 2 | Abrir WhatsApp não encerra o relógio | Abrir uma aba não comprova que o operador iniciou o atendimento. |
| 3 | Reutilizar `data_hora_saida` como saída confirmada da fila | O campo já representa a saída da fila; um novo timestamp sem fonte automática criaria duplicidade. |
| 4 | Snapshot em segundos por ocorrência | Evita reclassificação retroativa e mantém a comparação inteira/exata. |
| 5 | Não fazer backfill | A meta histórica real não é recuperável. |
| 6 | PostgreSQL é a única fonte de verdade | Radar e endpoints precisam observar a mesma ocorrência após restart. |
| 7 | Denominador inclui confirmados e não respondidos vencidos | Remove o viés de considerar apenas sucessos concluídos. |
| 8 | Pendentes no prazo não entram | Ainda existe oportunidade legítima de cumprir a meta. |
| 9 | Cancelados sem confirmação ficam fora e aparecem em contagem | Não há motivo estruturado para classificá-los corretamente; transparência evita silêncio. |
| 10 | Limite exato é sucesso | Mantém `<=`, regra já validada e intuitiva para “até 10 minutos”. |
| 11 | `null` representa ausência de amostra | Zero significa fracasso real e não pode representar falta de dados. |
| 12 | Remover 85% fixo e thresholds 5/10 | Ambos divergem da única meta configurada pelo gestor. |
| 13 | Testar regra pura e SQL PostgreSQL | Uma camada protege semântica; a outra protege população e dialeto real. |
| 14 | Fuso IANA da clínica governa radar, cards e CSV | O browser do operador não pode mudar a coorte gerencial da clínica. |
| 15 | Serviço stateless com unit-of-work por operação | Evita `AsyncSession` compartilhada e torna restart/concorrência determinísticos. |
| 16 | PATCH genérico rejeita entrada em `in_progress` | Obriga toda confirmação a gravar timestamp pelo CAS canônico. |
| 17 | Exportar `in_progress` confirmado como atendido | A confirmação encerra a espera mesmo antes da resolução final. |
| 18 | Preservar efeitos após commit, sem outbox | Mantém observáveis atuais e evita maquinaria de entrega garantida não requerida. |
| 19 | Projetar `recent_messages` do Event Store | Preserva o detalhe sem criar uma segunda retenção de PII no JSONB. |
| 20 | CAS fixa `status + lifecycle_version` observados | Após espera de lock, o perdedor falha em vez de aceitar um estado que não decidiu originalmente. |
| 21 | Persistir `updated_at` timezone-aware | Preserva o campo público e seu valor através de novas instâncias/restarts. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Operador confirma sem responder | Medium | Nome “confirmado”, ajuda explícita e separação do botão WhatsApp; não alegar prova automática. |
| 2 | Usuário espera histórico imediato após rollout | High | Legado não mensurado é contado e explicado; não fabricar meta passada. |
| 3 | Remoção do singleton quebra detalhes enriquecidos | High | Mapear todos os campos persistidos antes da remoção e cobrir create/list/get/update após reinstanciação. |
| 4 | Corrida entre dois operadores sobrescreve o tempo | High | UPDATE condicional atômico e timestamp write-once. |
| 5 | Cancelamento pode ser usado para melhorar a taxa | Medium | Exibir contagem de cancelados sem confirmação; motivo estruturado e política seletiva ficam como follow-up explícito. |
| 6 | Configuração legada fora da faixa gera deadline inválido | Medium | Normalização fail-safe para 600s em novas ocorrências e validação 1..120 nos novos PATCHes. |
| 7 | Fuso incorreto muda a coorte de datas | High | Fuso da clínica é autoridade única; API devolve período resolvido e testes cobrem browser divergente, DST e fallback. |
| 8 | PostgreSQL smoke é ignorado no gate | High | Script versionado exige `POSTGRES_SMOKE_DATABASE_URL`, node ID dedicado e zero skipped; não pressupõe workflow inexistente. |
| 9 | Arquivos da fila têm mudanças concorrentes | Medium | Implementação parte do estado atual, isola o diff do wish e não reverte filtros/exportações em andamento. |
| 10 | Migração do singleton perde campos/efeitos públicos | High | Persistência/mapper explícitos e regressões de Event Store, webhook, delivery status e métricas em cada transição. |
| 11 | Wish de export já aprovado contradiz fuso e `in_progress` | High | Este design supersede os trechos incompatíveis e exige alinhar aquele wish/testes antes da execução. |
| 12 | Copiar mensagens para JSONB amplia retenção de PII clínica | High | Não persistir `recent_messages`; projetar no máximo cinco eventos do Event Store já sujeito à retenção/tenant atuais, retornando vazio quando indisponível. |
| 13 | UPDATE concorrente acorda e aceita estado produzido pelo rival | High | Predicado fixa `status` e `lifecycle_version` observados; zero linhas é conflito sem retry nem side effect. |
| 14 | Remover `_handoffs` faz `updated_at` voltar a nulo após restart | High | Coluna `TIMESTAMPTZ`, escrita no mesmo CAS e mapeada no contrato/OpenAPI. |

## Success Criteria

- [ ] Migration upgrade/downgrade adiciona/remove `sla_target_seconds`, `updated_at TIMESTAMPTZ NULL` e `lifecycle_version INTEGER NOT NULL DEFAULT 0`, com constraints 60..7200 e versão não negativa.
- [ ] A migration aceita SLA 60/7200/`NULL`, rejeita 59/7201, deixa `updated_at` legado nulo, inicializa versão legada em zero e o downgrade restaura o schema anterior.
- [ ] Novo handoff captura a meta vigente na mesma transação; alteração posterior de 10 para 5 minutos não muda o snapshot existente.
- [ ] Configurações novas rejeitam `0`, `121`, boolean, float, string e null explícito; legado ausente/inválido cria novo handoff com fallback 600s e log seguro.
- [ ] Abrir WhatsApp não chama endpoint, não altera status/timestamp e não remove o card.
- [ ] Confirmar atendimento grava `data_hora_saida` uma vez, move para `in_progress`, remove da fila de espera e uma repetição retorna 409 sem mudar o instante.
- [ ] PATCH genérico para `in_progress` retorna 409 `attendance_confirmation_required`; nenhum caminho entra nesse estado sem o CAS que grava `data_hora_saida`.
- [ ] Conclusão e `resolved_at` permanecem distintos; conclusão sem confirmação retorna 409 e não inventa timestamp.
- [ ] `HandoffService` recebe factory de sessão/UoW, não retém `AsyncSession`; criação com instância A, leitura/transição com B e confirmação final com C usam PostgreSQL corretamente.
- [ ] Repositório CAS tenant-scoped retorna resultados tipados; serviço é o único dono da matriz e rotas não repetem pre-read/autorização/transição.
- [ ] Mapper único preserva em round-trip `sector`, `sla`, summary, key_info, verification, assignment/resolution, webhook sent/status/response e `updated_at` timezone-aware, com defaults legados compatíveis; `lifecycle_version` não vaza no contrato público.
- [ ] `handoff_records.context` não persiste `recent_messages`; detalhe/webhook projetam no máximo cinco eventos em ordem pelo tenant correto e retornam lista vazia se ausentes/expirados, sem nova retenção de PII.
- [ ] Criação, listagem, detalhe, aceite, confirmação, conclusão, cancelamento, timeout e enriquecimento de verificação deixam de depender de `_handoffs`.
- [ ] Transições terminais inválidas e ações cross-tenant são rejeitadas sem mutação.
- [ ] Duas sessões concorrentes que leram o mesmo `status/lifecycle_version` produzem exatamente um sucesso e um conflito; terceira sessão lê versão incrementada uma vez e o primeiro timestamp, com barreira/timeout evitando falso passe.
- [ ] Corrida confirmação versus cancelamento/timeout a partir do mesmo estado/versão observado produz exatamente um vencedor; o perdedor não retrya com estado novo e não emite side effects.
- [ ] Event Store, webhooks/delivery status e métricas mantêm evento, payload, labels e política de falha na iniciação/transições; conflito/rejeição não duplica efeito.
- [ ] Para 599/600/601 segundos com meta 600, os resultados são dentro/dentro/fora.
- [ ] A coorte normativa retorna 2 de 5 e 40,0%; pendente com 599s e cancelado sem confirmação não entram no denominador.
- [ ] Timeout/aberto sem confirmação só contam como violação quando `as_of >= deadline`.
- [ ] Categorias são disjuntas/exaustivas; legado cancelado não sobrepõe cancelado, cancelado confirmado entra somente em within/late, e invariantes de soma/denominador passam com `ROUND_HALF_UP`.
- [ ] Mudança de dia/fuso usa `[start, end_exclusive)` no fuso da clínica e não duplica nem perde bordas; browser em outro fuso não muda radar, cards ou CSV.
- [ ] O backend filtra `items` pelo mesmo `[period_start_utc, period_end_exclusive_utc)` usado no KPI; o frontend não reaplica `ymd(new Date(created_at))` nem exclui itens por fuso local.
- [ ] Dias DST de 23h/25h e configuração IANA inválida (fallback `America/Sao_Paulo`) produzem período correto e auditável.
- [ ] Sem elegíveis, API retorna `pct_within_sla=null`; sem confirmados, `avg_wait_seconds=null`; frontend mostra “Sem dados”, não 0%/00:00:00.
- [ ] A API retorna numerador, denominador, legado não mensurado e cancelados excluídos coerentes com o percentual.
- [ ] A API/OpenAPI expõe nulabilidade, `updated_at` timezone-aware, todas as contagens disjuntas e `period_timezone/period_start/period_end_exclusive` com offsets explícitos; `lifecycle_version` permanece interno.
- [ ] Cards usam a meta snapshot: verde abaixo de 50%, amarelo entre 50% e 100% inclusive, vermelho acima; legado é neutro.
- [ ] O texto de 85% fixo e as legendas 5/10 somem; a interface explica meta por entrada e mostra “{within} de {eligible}”.
- [ ] Teste frontend mantém a prova de que 66,7% da API produz gauge acessível 66,7% e texto arredondado 67%.
- [ ] Frontend com promises controladas prova: abrir sem POST; double-click com um POST; card até 2xx; 409/500 preservam card; null/no-data; 2/5; e cores abaixo/igual a 50%, igual a 100% e acima.
- [ ] CSV usa o período/fuso devolvido para a clínica e inclui `in_progress` confirmado como atendido; o wish `handoff-export-open-closed` e seus testes não conservam contratos supersedidos.
- [ ] Teste PostgreSQL real aplica migrations e chama o método de produção de estatísticas, sem SQL copiada; HTTP/SQLite fica restrito ao contrato com serviço injetado.
- [ ] `scripts/check-handoff-sla-postgres.sh`, coberto por `tests/scripts/test_check_handoff_sla_postgres.py`, exige `POSTGRES_SMOKE_DATABASE_URL`, executa o node ID dedicado e falha por variável ausente, exit não zero ou qualquer skipped/xfailed; o comando normativo documentado passa com PostgreSQL local.
- [ ] Suites focadas backend/frontend, typecheck, lint aplicável e `git diff --check` passam sem regressão nos filtros/CSV existentes.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `e3d3c48edd43ae6f7a83b8316a3ac54b8106c7a71082a5e7997abb51fe0b7174`
- **Reviewer:** /root/design_review_loop2
- **Reviewed at:** 2026-09-27T21:57:24.000Z
<!-- genie-design-review:end -->
