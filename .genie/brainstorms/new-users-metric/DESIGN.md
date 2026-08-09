# Design: Métrica de usuários novos

| Field | Value |
|-------|-------|
| **Slug** | `new-users-metric` |
| **Date** | 2026-08-08 |
| **WRS** | 100/100 |
| **Revision** | Council conditions incorporated after decision `revise` |

## Problem

Clinic_admin e builder não conseguem distinguir quantas identidades tiveram sua primeira interação registrada pela clínica no período nem quanto do volume identificado veio dessas identidades versus usuários antigos. A métrica precisa entregar essa leitura comercial sem alterar contratos existentes, expor identidades, permitir consultas sem limite ou confundir persistência com recebimento da mensagem.

## Scope

### IN

- Classificar uma identidade por `(tenant_id, Session.channel, btrim(Session.user_id))`, com `user_id` aparado e não vazio.
- Definir “primeira interação registrada” como a menor `Event.created_at` de `message.in` persistida pela NeuroStack para a identidade naquela clínica/canal.
- Exibir microcopy explícita de que a fonte é o histórico persistido disponível, que a classificação é por clínica e canal e que os percentuais medem interações, não pessoas.
- Criar `GET /v1/acquisition/clinic` como endpoint JWT-only para `clinic_admin` ou `builder`, com override de tenant submetido ao ownership existente; `super_admin` e API key recebem 403 nesta entrega.
- Limitar o endpoint clinic a no máximo 90 dias civis, interpretados no fuso configurado da clínica e normalizados para `[start, end_exclusive)`.
- Criar `GET /v1/acquisition/builder` como endpoint JWT-only para o builder autenticado, paginado por cursor com `limit` de 1 a 100 clínicas, para a janela fixa `[as_of - 168 horas, as_of)`.
- Validar como ceilings provisórios timeout SQL de 2 segundos no clinic path e 5 segundos no builder path, deadlines totais de 2,5/5,5 segundos e um controlador compartilhado de concorrência por processo; saturação ou timeout retorna indisponibilidade explícita, nunca zeros fabricados.
- Executar Wave 0A antes de código de produto para perfil/SQL/serviço e Wave 0B após o backend mínimo, antes de frontend/release, para endpoint total, capacidade e recuperação.
- Expor apenas agregados, com evidência de performance gerada por allowlist que exclui DSN, host, credenciais, bind values, identidades, amostras de linha e mensagens brutas de conexão.
- Mostrar no dashboard da clínica KPI, percentuais e gráfico diário 100% empilhado, e no builder o resumo paginado de sete dias por clínica.
- Manter frontend role-gated e tolerante a endpoint ausente/indisponível durante rollout, buscar páginas builder sequencialmente com `as_of` estável e registrar evidência visual da matriz completa de estados.
- Executar a implementação em branch/worktree limpo e isolado, com base imutável registrada, sem misturar o worktree atual de `api-provider-settings`.

### OUT

- Alteração de semântica, tipos ou campos de `GET /v1/traces/stats`; `period_start` e `period_end` existentes permanecem intactos.
- Adição dos campos de aquisição a `GET /v1/builder/clinics`; o contrato existente permanece intacto.
- Acesso aos novos analytics por API key de integração.
- Timestamp de recebimento no transporte; esta entrega mede persistência registrada e não adiciona plumbing de `received_at` aos webhooks.
- Identidade global entre clínicas/canais, case folding, aliases, CRM ou normalização retroativa além de `btrim`.
- Migration, índice novo, tabela de coorte, materialized view, cache, backfill, job, fila ou feature flag.
- Supressão de small cells: as contagens exatas são aceitas somente para administradores da clínica e builders proprietários, sob o mesmo boundary de autorização já aplicado aos painéis.
- Campanha, conversão, retenção, churn, atribuição ou gráfico global somando clínicas.

## Approach

### Wave 0A — evidência antes de código de produto

Um profiler read-only pode rodar em staging somente com credencial dedicada. Antes de consultar, lê `current_user`, compara-o em memória com o role esperado fornecido separadamente, exige `transaction_read_only=on`, inicia transação `READ ONLY`, confirma ausência de privilégios INSERT/UPDATE/DELETE nas tabelas envolvidas e aborta fail-closed se qualquer prova falhar. A evidência persiste apenas os booleans dessas verificações, nunca o nome do role. Ela produz somente agregados allowlisted: quantidade de clínicas por builder; percentis de `message.in` por tenant e por janela de 90 dias; identidades distintas aparadas; percentual identificado/não identificado; eventos históricos por identidade ativa; retenção temporal disponível; workers/réplicas; pool efetivo; `max_connections`; limite de memória por worker; e versão/índices do PostgreSQL. Em staging usa `EXPLAIN (FORMAT JSON)` sem `ANALYZE` e nunca cria fixture.

O protótipo SQL/serviço, benchmark destrutivo e `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` rodam exclusivamente em banco cujo nome real termina em `_test`, com variável dedicada sem fallback para `DATABASE_URL`, `ENVIRONMENT=test`, confirmação literal, comparação com `current_database()`, namespace UUID próprio, cleanup limitado e verificação de zero linhas residuais.

Noventa dias, 100 clínicas, 200/500 ms, 2/5 s, 2,5/5,5 s, capacidade quatro total/dois builder e espera de 100 ms são ceilings/targets candidatos, não medições. Wave 0A testa o clinic path com o tenant de maior vetor de custo observado — identidades ativas, eventos no período, todo o histórico retido dessas identidades e skew por identidade — e o builder path com as 100 clínicas de maior custo combinado, ou fixture conservadora equivalente quando o ambiente tiver menos clínicas.

Wave 0A mede dois relógios: (1) execução SQL do servidor pelo plano e (2) fronteira do serviço desde checkout do pool, `SET LOCAL`, execução/fetch até agregação pronta. Cada cenário executa três repetições independentes, cinco warm-ups descartados e pelo menos 50 sucessos, usando nearest-rank para p50/p95 e registrando primeira execução, min/max, dispersão, cache hits/reads, spills e erros por status. Não se subtraem p95s.

A Wave 0A registra deployment efetivo — workers, réplicas, pool, `max_connections` e workload normal — e avalia se o envelope candidato cabe com headroom. Um controlador compartilhado possui teto provisório de quatro cálculos acquisition simultâneos por processo; dentro dele, no máximo dois podem ser builder. As Waves 0 podem apenas reduzir esses tetos; aumentá-los exige nova design review.

Pico de RSS da fronteira de serviço é medido por worker. O no-go de memória é o menor entre 256 MiB e 10% do limite de memória do worker/deployment; qualquer spill inesperado, OOM, crescimento sem retorno após cleanup ou ultrapassagem do orçamento bloqueia a entrega. Se não houver ambiente-alvo autorizado, se o perfil não puder ser produzido sem PII, ou se qualquer gate SQL/serviço/memória/cleanup falhar, a entrega para antes do backend mínimo e retorna a design review. A Wave 0A não pode autorizar índice, cache ou materialização por conta própria.

Wave 0A também exige um registro de aceitação assinado por um owner organizacional de produto/privacidade para contagens exatas de small cells dentro do boundary JWT. Ausência dessa autoridade bloqueia avanço; o pedido funcional do usuário não é tratado automaticamente como aprovação LGPD.

### Backend mínimo e Wave 0B — evidência antes do frontend

Após 0A SHIP, implementa-se somente módulo, schemas e endpoints backend, ainda sem frontend. Wave 0B mede o terceiro relógio — endpoint total desde entrada autenticada, incluindo espera do controlador e serialização — no backend real. Clinic total deve ter p95 <= 200 ms; builder total recebe exceção explícita de batch, p95 <= 500 ms. A carga combina tráfego clinic+builder e workload normal no deployment efetivo.

No limite suportado, a taxa de 503 deve ser zero. Um cenário deliberadamente acima do limite deve produzir 503 tipado em até 150 ms, sem misturar esses tempos ao p95 de 2xx. Após timeout SQL, deadline ou cancelamento, testes PostgreSQL reais provam rollback, cancelamento da query, liberação de permits em `finally`, remoção do timeout local e reutilização saudável da mesma conexão/pool por uma consulta normal subsequente. Falha de SLO, recuperação, taxa de erro ou capacidade bloqueia frontend/release e retorna à design review.

### Cálculo e isolamento

A consulta SQLAlchemy seleciona as `message.in` do intervalo, une `Event.session_id = Session.id` **e** `Event.tenant_id = Session.tenant_id`, filtra ambos os lados pelos tenants autorizados, usa `btrim(Session.user_id)` não vazio e limita o histórico às identidades presentes no intervalo. Para cada `(tenant_id, channel, user_id_aparado)`, calcula `MIN(Event.created_at)` em todo o histórico retido.

Uma identidade é nova quando `start <= first_persisted_at < end_exclusive`; todas as suas interações identificadas no intervalo pertencem à coorte nova. Se o primeiro evento persistido é anterior, todas pertencem à coorte antiga. Eventos sem sessão tenant-safe ou identidade utilizável ficam fora do denominador e entram em `unidentified_user_interactions_count`.

`Event.created_at` é deliberadamente o timestamp UTC do relógio da aplicação atribuído quando `EventData` é criado, após o pipeline/LLM e antes do INSERT; não é timestamp do banco, commit time nem instante de recepção pelo canal. Esse proxy corresponde ao pedido de “primeira interação registrada” e evita um novo contrato de ingestão; microcopy, docs, monitoramento de sincronização de relógio e testes de fronteira tornam a limitação visível. Se o produto passar a exigir aquisição por recebimento, será necessário outro design com `received_at` canônico.

### Contrato normativo da API

As novas rotas vivem num router estático próprio, `/v1/acquisition`, registrado sem parâmetro curinga e coberto por teste de roteamento; isso evita colisão com `/v1/traces/{trace_id}` e separa analytics administrativo das APIs de integração.

`GET /v1/acquisition/clinic` aceita exatamente estes query params:

- `start_date`: obrigatório, texto estrito `YYYY-MM-DD`, primeira data civil inclusiva;
- `end_date`: obrigatório, texto estrito `YYYY-MM-DD`, última data civil inclusiva;
- `tenant_id`: UUID opcional para `clinic_admin`, que só pode omitir ou repetir seu próprio tenant, e obrigatório para `builder`, que deve provar ownership.

Campos desconhecidos são rejeitados com 422. Datas impossíveis, `start_date > end_date` e spans inclusivos acima de 90 dias também retornam 422. A rota resolve `resolve_clinic_tz`, converte as bordas civis para `[period_start, period_end_exclusive)` e retorna ambas em UTC RFC 3339 com sufixo `Z`, mais o nome IANA efetivamente usado em `timezone`. Configuração ausente ou inválida preserva o fallback observável existente para `America/Sao_Paulo`; testes cobrem ambos os casos. Builder sem `tenant_id` recebe 422; tenant alheio para clinic_admin ou builder recebe 403 sem confirmar se a clínica existe. API key válida, `super_admin`, `investor` e outras roles recebem 403.

O 200 clinic tem forma fechada e defaults explícitos:

```json
{
  "period_start": "2026-08-01T03:00:00Z",
  "period_end_exclusive": "2026-08-08T03:00:00Z",
  "timezone": "America/Sao_Paulo",
  "new_users_count": 12,
  "new_user_interactions_count": 30,
  "returning_user_interactions_count": 70,
  "identified_user_interactions_count": 100,
  "unidentified_user_interactions_count": 4,
  "new_user_interactions_pct": 30.0,
  "returning_user_interactions_pct": 70.0,
  "daily": [
    {
      "date": "2026-08-01",
      "new_users_count": 3,
      "new_user_interactions_count": 5,
      "returning_user_interactions_count": 5,
      "identified_user_interactions_count": 10,
      "unidentified_user_interactions_count": 1,
      "new_user_interactions_pct": 50.0,
      "returning_user_interactions_pct": 50.0
    }
  ]
}
```

Todos os counts são inteiros não negativos e valem zero sem dados. `identified_user_interactions_count` é exatamente a soma de new+returning, e a soma dos counts diários reproduz os totais do período. `daily` é ordenado por data e esparso: inclui cada data civil com pelo menos uma interação identificada ou não identificada; não cria buckets vazios. Percentuais usam somente `identified_user_interactions_count` como denominador, têm uma casa decimal, são ambos `null` quando ele é zero e, caso contrário, `new = round_half_up(100 * new / identified, 1)` e `returning = round_half_up(100.0 - new, 1)`, garantindo soma 100,0. `new_users_count` diário conta identidades cujo `first_persisted_at` caiu naquela data e soma exatamente o KPI do período.

`GET /v1/acquisition/builder` aceita somente `limit`, `cursor` e `as_of`. `limit` é inteiro, default 50, mínimo 1 e máximo 100. Na primeira página, `cursor` e `as_of` são omitidos; o servidor captura um `as_of` UTC truncado a segundos. Em páginas seguintes, os dois são obrigatórios: `as_of` deve ser exatamente o retornado, não pode estar mais de cinco segundos no futuro nem ter mais de dez minutos, e `period_end_exclusive == as_of`. O cursor é opaco, HMAC-SHA256 assinado com chave derivada de `SECRET_KEY` e domain separator, versionado e carrega builder, `as_of` e último `Tenant.id`; formato, assinatura, versão, builder ou combinação inválida retornam 422 sem revelar IDs internos adicionais. Rotação da chave invalida o cursor de forma segura e a UI reinicia a paginação. JWT builder sem claim válido ou cujo `Builder.id` não existe recebe 403 `acquisition_role_forbidden`, nunca 200 vazio.

A listagem usa keyset `Tenant.id ASC`, nunca offset, e considera clínicas ativas e inativas com `Tenant.builder_id` atual igual ao builder e `Tenant.created_at <= as_of`. `as_of` congela a janela da métrica e exclui clínicas criadas depois dele, mas não promete snapshot histórico de ownership porque o modelo não o registra. Ownership é revalidado em cada página; transferências concorrentes podem acrescentar, remover ou pular clínicas, nunca autorizar clínica que não pertença ao builder no momento da query. Essa consistência fraca é documentada e o contrato deliberadamente não expõe `total`. Páginas são buscadas sequencialmente. Qualquer erro intermediário invalida a montagem parcial; a UI mantém no máximo o último snapshot completo e reinicia da primeira página com novo `as_of`.

O 200 builder retorna `as_of`, `period_start`, `period_end_exclusive`, `limit`, `next_cursor` (`string | null`) e `items`. Cada item contém `tenant_id`, `tenant_name`, `is_active` e exatamente os sete campos `new_users_count_7d`, `new_user_interactions_count_7d`, `returning_user_interactions_count_7d`, `identified_user_interactions_count_7d`, `unidentified_user_interactions_count_7d`, `new_user_interactions_pct_7d` e `returning_user_interactions_pct_7d`, com os mesmos defaults, arredondamento e invariantes do clinic 200. `items: []` com `next_cursor: null` é o único vazio válido e não se confunde com rota 404 ou página parcial.

```json
{
  "as_of": "2026-08-08T15:00:00Z",
  "period_start": "2026-08-01T15:00:00Z",
  "period_end_exclusive": "2026-08-08T15:00:00Z",
  "limit": 50,
  "next_cursor": "v1.opaque-signature",
  "items": [
    {
      "tenant_id": "11111111-1111-4111-8111-111111111111",
      "tenant_name": "Clínica Exemplo",
      "is_active": true,
      "new_users_count_7d": 12,
      "new_user_interactions_count_7d": 30,
      "returning_user_interactions_count_7d": 70,
      "identified_user_interactions_count_7d": 100,
      "unidentified_user_interactions_count_7d": 4,
      "new_user_interactions_pct_7d": 30.0,
      "returning_user_interactions_pct_7d": 70.0
    }
  ]
}
```

### Erros, disponibilidade e controles de custo

O middleware executa antes do router e seu contrato legado não muda. Credencial ausente, token expirado, token inválido e API key inválida retornam 401 com forma exata `{"error":"unauthorized","message":<mensagem existente>,"documentation_url":"https://docs.neurostack.ai/auth"}`; as mensagens existentes são, respectivamente, `Missing Authorization or <API_KEY_HEADER> header`, `Token has expired`, `Invalid token` e `Invalid API key`. Tenant inativo retorna antes do router 403 `{"error":"forbidden","message":"Tenant account is inactive"}`. OpenAPI representa essas variantes sem prometer uma mensagem única, e contract tests garantem que outros endpoints mantêm os mesmos envelopes.

Depois da autenticação, o router converte toda validação e toda falha de domínio para um novo `AcquisitionErrorResponse` fechado, `ConfigDict(extra="forbid")`, com forma exata `{"error": <code>, "message": <texto seguro>, "details": null}`. Ele é separado do `ErrorResponse` geral, que possui `request_id` e `documentation_url` opcionais; respostas acquisition sempre incluem `details` e nunca incluem esses campos adicionais. Parâmetros são recebidos como texto opcional e validados no módulo para impedir o envelope automático divergente do FastAPI. A matriz normativa é:

| HTTP | Codes | Retry | Ação do cliente |
|---|---|---|---|
| 401 pre-router | `unauthorized`, mensagem conforme causa e `documentation_url` | não | Reautenticar. |
| 403 pre-router | `forbidden` para tenant inativo | não | Ocultar o módulo e procurar o administrador. |
| 403 router | `acquisition_role_forbidden`, `acquisition_tenant_forbidden` | não | Ocultar o módulo; não tentar outro tenant. |
| 404 | rota ausente durante rollout/rollback, envelope HTTP existente | após deploy | Mostrar “Métrica ainda indisponível”, nunca estado vazio. A rota ativa não usa 404 para tenant/ownership. |
| 422 | `acquisition_unknown_parameter`, `acquisition_invalid_date`, `acquisition_invalid_range`, `acquisition_period_too_large`, `acquisition_tenant_required`, `acquisition_invalid_limit`, `acquisition_invalid_cursor`, `acquisition_invalid_as_of`, `acquisition_as_of_expired` | não; reiniciar paginação para cursor/as_of | Corrigir filtro ou descartar montagem parcial e recomeçar. |
| 503 | `acquisition_capacity_exceeded` | sim, `Retry-After: 1` | Retry com jitter; manter último snapshot completo. |
| 503 | `acquisition_query_timeout`, `acquisition_deadline_exceeded` | sim, `Retry-After: 5` | Clinic reduz período; builder tenta novamente sem fan-out. |

Mensagens produzidas pelo router são action-oriented, estáveis nos testes e não incluem SQL, tenant alheio, identificadores de usuário, DSN ou exceção interna. OpenAPI documenta todos os query params, schemas 200, `AcquisitionErrorResponse`, envelopes legados 401/403 e respostas 404/422/503, marca segurança exclusivamente JWT e ganha uma referência “Admin analytics: acquisition” separada da documentação de API keys, com exemplo de paginação sequencial e reinício após expiração.

O módulo acquisition encapsula consulta, normalização, controlador compartilhado, deadlines e exceções tipadas; rotas ficam responsáveis apenas por autorização, validação e HTTP. Cada path usa `SET LOCAL statement_timeout` como ceiling SQL. Um deadline `asyncio.timeout` cobre todo o path, inclusive espera de slot, pool, statements e agregação: 2,5 s clinic e 5,5 s builder. A tentativa espera no máximo 100 ms pelo slot e retorna 503 `acquisition_capacity_exceeded` se saturada; timeout SQL retorna 503 `acquisition_query_timeout`; deadline total retorna 503 `acquisition_deadline_exceeded`. Cleanup, rollback e permits vivem em `finally`. Esses controles existem porque rate limiting por request não limita o custo de uma consulta individual.

### Apresentação e microcopy

No painel clinic_admin, o card “Novos usuários registrados” mostra `new_users_count`; a composição diz “% das interações identificadas de usuários novos/antigos” e o gráfico diário 100% empilhado usa as duas séries de interação. A ajuda fixa diz: “Novo = primeira interação registrada no histórico disponível desta clínica e canal. O registro ocorre após o processamento da mensagem. Percentuais medem interações identificadas, não pessoas.” O seletor informa datas civis no fuso retornado.

No painel builder, cada clínica mostra “últimas 168 horas até {as_of local}”, status ativa/inativa e os sete agregados; não há soma global nem total de clínicas prometido. A UI requisita apenas para `clinic_admin` e `builder`; não renderiza ou chama a API para `super_admin`, `investor` e outras roles.

Os estados têm microcopy e comportamento testáveis: loading usa skeleton sem zero; vazio 200 diz “Nenhuma interação registrada no período”; somente não identificados diz “Há interações sem identificação; não é possível calcular a composição”; 403 diz “Você não tem acesso a esta métrica”; 404 diz “Métrica ainda indisponível nesta versão”; capacity 503 diz “Métrica ocupada, tentando novamente”; timeout/deadline 503 diz “A consulta excedeu o tempo; reduza o período ou tente novamente”; paginação expirada diz “Os dados mudaram; atualizando a lista”. `0,0%` é valor real e nunca confundido com vazio. Retry usa os headers normativos, jitter e limite de duas tentativas automáticas; depois disso há botão manual. A matriz visual cobre esses estados, erro intermediário sem parcial, composição mista, dias esparsos, mobile/desktop, claro/escuro, foco, contraste, labels de leitor de tela e ausência de overflow.

### Privacidade e rollout

As contagens exatas de small cells são aceitas tecnicamente porque a superfície é first-party, JWT-only e restrita a clinic_admin da própria clínica ou builder das clínicas que possui; o avanço depende também de aceitação documentada por owner organizacional de produto/privacidade na Wave 0A. A revisão de segurança deve provar 401/403 para API key, super_admin, clinic_admin alheio e builder sem ownership. Logs registram somente endpoint, papel, tenant agregado e outcome; não registram user IDs nem valores da métrica.

O path acquisition desabilita captura de parâmetros/valores no SQLAlchemy/driver/APM e não depende de `engine.echo`; profiler/benchmark recusam execução quando SQL echo ou captura de bind values está ativa. Testes com sentinels inspecionam logs da aplicação, driver, SQLAlchemy, spans OpenTelemetry, exceções encadeadas e erros de conexão.

O backend dos endpoints novos é implantado antes do frontend. O frontend omite totalmente chamada e visualização para super_admin, investor e outras roles proibidas, e trata 403, 404 e 503 como estados explícitos sem afetar os painéis atuais. Rollback remove primeiro o consumo frontend e depois os endpoints, sem mudar dados. O rollout observado registra latência total e as fronteiras SQL/serviço, timeouts, saturação e tamanho da página; ausência de mecanismo de canário exige validação em staging e janela monitorada antes da liberação geral.

Alternativas rejeitadas:

- Adicionar campos a `/v1/traces/stats` e redefinir `period_end` foi rejeitado porque alteraria KPIs/semântica existentes apesar de parecer aditivo.
- Adicionar campos a `/v1/builder/clinics` foi rejeitado porque tornaria a listagem operacional indisponível quando analytics excedesse timeout e não limitaria o número de clínicas calculadas.
- Persistir `received_at` foi rejeitado nesta entrega porque exige plumbing em todos os webhooks e redefine o pedido confirmado de primeira interação registrada; fica para um wish próprio se a semântica de recebimento for requerida.
- Tabela/materialização/índice funcional foram rejeitados antes da Wave 0A; qualquer um deles exige evidência e nova revisão.
- Suprimir small cells foi rejeitado porque tornaria o KPI exato incorreto para os únicos usuários autorizados a ver os dados; a mitigação escolhida é acesso JWT/ownership estrito.

## Simplicity Case

- **Simplest complete design:** Wave 0A read-only/protótipo, backend mínimo, Wave 0B sobre o endpoint real, dois endpoints JWT-only limitados e apresentação nos painéis existentes.
- **Added machinery:** endpoints separados são pagos por compatibilidade, isolamento de falha e proibição de API key; paginação/limite de período são pagos por custo limitado; timeout e controlador compartilhado são pagos pelo risco de consultas históricas caras que o rate limit atual não controla; gates 0A/0B são pagos pela cardinalidade produtiva ainda desconhecida sem duplicar o endpoint num harness descartável.
- **Deferred until measured:** índice funcional, tabela de first-seen, materialização, cache ou timestamp de recebimento só podem ser propostos se Wave 0A/0B falhar e uma nova design review justificar o mecanismo.
- **Complexity removed:** nenhum estado durável, migration, backfill, invalidação, sincronização, flag, versão do endpoint antigo, compatibilidade dual ou coordenação distribuída.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | “Novo” é por tenant, canal e `btrim(user_id)` | Mantém a métrica dentro da fronteira da clínica e evita aliases implícitos. |
| 2 | Fonte temporal = `Event.created_at` persistido pós-processamento | Corresponde a “registrada na NeuroStack”; recebimento exigiria novo plumbing e política. |
| 3 | O rótulo visível sempre diz “primeira interação registrada” | Impede interpretação como pessoa/lead universalmente novo ou timestamp de recepção. |
| 4 | KPI conta identidades; percentuais contam interações | Separa aquisição de público do volume produzido pela coorte. |
| 5 | Não identificados são excluídos e auditados em contagem própria | Evita classificação inventada e torna o denominador transparente. |
| 6 | Endpoints novos, não campos nos endpoints existentes | Preserva compatibilidade e isola timeout/rollback. |
| 7 | Analytics exige JWT administrativo e rejeita API key | Reduz o valor de uma credencial de integração comprometida e resolve o boundary de small cells. |
| 8 | Small cells exatas são aceitas dentro do ownership JWT | O caso de uso é first-party; supressão destruiria a exatidão do KPI pedido. |
| 9 | Clinic aceita no máximo 90 dias civis | Limita custo e cobre os períodos comerciais usuais 7/30/90. |
| 10 | Builder usa páginas de até 100 clínicas e janela móvel de 168 horas | São ceilings públicos candidatos que limitam trabalho por request sem N+1 por clínica e precisam passar nas Waves 0. |
| 11 | Intervalos são semiabertos e usam campos temporais próprios | Elimina dupla contagem sem redefinir `period_end` legado. |
| 12 | Wave 0A precede backend; Wave 0B precede frontend/release | Evita custo afundado e mede endpoint real sem duplicar implementação num harness descartável. |
| 13 | Timeout SQL, deadline total, controlador compartilhado 4 total/2 builder e espera de 100 ms | Rate limit por request não protege o banco de poucas consultas caras; Waves 0 podem reduzir, mas não elevar, os tetos. |
| 14 | 503 explícito nunca vira zero | Indisponibilidade não pode ser confundida com ausência de aquisição. |
| 15 | Evidência usa allowlist; staging é read-only e benchmark usa banco `_test` fail-closed | Impede vazamento de PII/credenciais e fixture destrutiva no ambiente-alvo. |
| 16 | Backend precede frontend; rollback remove frontend antes do backend | Permite rollout e rollback coordenados sem feature flag. |
| 17 | Work ocorre em worktree limpo com base registrada | Preserva atribuição de regressões diante do worktree atual contaminado. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Histórico retido começa depois do primeiro contato real | High | Microcopy “primeira interação registrada no histórico disponível”; perfil registra profundidade de retenção. |
| 2 | Persistência pós-LLM desloca evento em relação ao recebimento | High | Semântica explícita, testes de fronteira e OUT formal para `received_at`. |
| 3 | Consulta histórica degrada com cardinalidade/skew real | High | Gates 0A/0B bloqueantes, período/página limitados, p95, timeout, capacidade e re-review antes de índice. |
| 4 | Builder com muitas clínicas exige várias páginas e ownership pode mudar entre elas | Medium | Keyset máximo 100, `as_of` congela a janela, ownership é revalidado, não há `total`, consistência fraca é documentada e parcial é descartado. |
| 5 | Célula pequena permite inferência por diferença | Medium | Acesso JWT/ownership, API key proibida, nenhum raw ID e aceite assinado por owner de produto/privacidade antes do backend. |
| 6 | Credencial/DSN vaza em evidência ou erro | High | Allowlist de saída, sanitização de exceções e testes com sentinels de URL/host/password. |
| 7 | Mesmo identificador em grafias diferentes duplica pessoa | Medium | Apenas `btrim`; case folding/aliases ficam OUT e são explicados. |
| 8 | Saturação deixa métrica temporariamente indisponível | Medium | 503 tipado, UI degradável, telemetria de saturação e painéis existentes independentes. |
| 9 | Frontend sem runner de componentes perde regressão visual | Medium | Matriz de screenshots reproduzível em staging para mobile/desktop, temas e todos os estados. |
| 10 | Falhas repo-wide preexistentes mascaram regressão | High | Base imutável registrada e comparação dos mesmos comandos no base e no tree do wish. |
| 11 | Demanda comercial não possui meta quantitativa externa | Low | O pedido do product owner confirma valor; primeira entrega mede disponibilidade/uso sem inventar meta de receita. |

## Success Criteria

- [ ] Wave 0A gera perfil agregado allowlisted e protótipo SQL/serviço do envelope candidato 90 dias/100 clínicas, cobrindo todo o histórico das identidades ativas; falta de ambiente, aceite small-cell ou falha SQL/serviço/memória/cleanup interrompe antes do backend.
- [ ] O digest/commit da base limpa e o diff exclusivo do wish são registrados antes de execução.
- [ ] A identidade e o join obedecem tenant + canal + `btrim(user_id)`, com testes adversariais cross-tenant e whitespace.
- [ ] `first_persisted_at = MIN(Event.created_at)` é documentado/testado como timestamp do relógio da aplicação pós-processamento e pré-INSERT, inclusive sincronização e fronteiras de meia-noite.
- [ ] `GET /v1/traces/stats` e `GET /v1/builder/clinics` permanecem byte-for-byte compatíveis em schema e semanticamente inalterados.
- [ ] `GET /v1/acquisition/clinic` rejeita API key, super_admin, roles não autorizadas, ownership inválido, query param desconhecido, intervalo invertido e período acima de 90 dias.
- [ ] O clinic path usa `[start, end_exclusive)` no fuso da clínica e retorna seus próprios limites UTC ISO-8601.
- [ ] `GET /v1/acquisition/builder` revalida ownership, ordena por `Tenant.id`, pagina por cursor HMAC no máximo 100, não expõe `total` e exige o mesmo `as_of` válido em todas as páginas sequenciais.
- [ ] Requests, responses, defaults, invariantes, round-half-up, consistência fraca de membership, envelopes legados pre-router e `AcquisitionErrorResponse` correspondem byte-for-byte ao contrato normativo e ao OpenAPI JWT-only.
- [ ] Os endpoints retornam contagens/percentuais exatos, `null` sem denominador e série diária esparsa/somável conforme o contrato normativo.
- [ ] Timeout ou saturação retorna 503 tipado e nunca payload de métricas zeradas.
- [ ] Wave 0A registra workers/réplicas/pool/max_connections; Wave 0B testa workload normal + clinic/builder misto; o controlador final não excede 4 total/2 builder sem nova review.
- [ ] Testes provam espera máxima de 100 ms, deadline total 2,5/5,5 s e `statement_timeout` 2/5 s, separando métricas 2xx de 503.
- [ ] Wave 0B prova clinic total p95 <= 200 ms e builder total <= 500 ms por nearest-rank em três repetições; Wave 0A mede SQL/serviço separadamente sem subtração de percentis.
- [ ] Pico de RSS da fronteira permanece abaixo de `min(256 MiB, 10% do limite por worker)`, sem spill/OOM/crescimento residual.
- [ ] Staging aborta sem role efetivamente read-only; benchmark aborta fora de banco `_test` ou com echo/captura de parâmetros ativa.
- [ ] Após timeout/cancelamento, rollback, cancel da query, permit e conexão são recuperados; uma consulta normal subsequente passa sem timeout residual.
- [ ] Evidência, logs e erros não contêm identidade, conteúdo, DSN, host, password, bind values ou amostras de linhas.
- [ ] Microcopy define “registrada”, tenant/canal, histórico disponível, percentuais por interação, período clinic civil e builder 168 horas.
- [ ] Frontend omite chamada/renderização para roles proibidas e trata loading, 403, 404, cada 503, vazio, somente não identificados, `0%`, composição mista, dias esparsos, cursor expirado e erro intermediário sem apresentar parcial.
- [ ] Evidência visual reproduzível cobre mobile/desktop, claro/escuro e todos os estados sem overflow ou falha de acessibilidade nominal.
- [ ] Rollout backend-first e rollback dos endpoints/UI são validados sem migration ou restauração de dados.
- [ ] Latência total e fronteiras SQL/serviço, timeout, saturação e tamanho de página são observados em staging e na janela monitorada de release.

## Next Step

After an independent design review returns SHIP, persist new digest-bound evidence and rebuild the executable wish with Wave 0A as the first hard gate and Wave 0B before frontend.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `baf59da009d3f9b56aed2a016ab479d02b4c4f1cffd396cb69df87d1dbddc808`
- **Reviewer:** /root/council_product
- **Reviewed at:** 2026-08-09T03:53:28.403Z
<!-- genie-design-review:end -->
