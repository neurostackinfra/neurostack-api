# Configuração do Tenant e da Clínica

**Versão:** 1.0  
**Alinhamento:** Documentação Técnica MVP v0.5

Este documento descreve onde e como as informações de configuração do tenant e da clínica (ClinicConfig) são armazenadas, a estrutura dos dados e um exemplo completo em JSON.

## Billing ASAAS: variáveis e gates

`ASAAS_API_KEY`, `ASAAS_WEBHOOK_TOKEN`, `ASAAS_CHECKOUT_RETURN_URL`,
`ASAAS_WEBHOOK_URL` e `ASAAS_WEBHOOK_NOTIFICATION_EMAIL` são apenas nomes
documentados em `.env.example`: os valores reais vêm do secret manager ou da
configuração protegida do ambiente e nunca entram no repositório.

- `ASAAS_CHECKOUT_RETURN_URL` deve ser uma URL HTTPS absoluta, sem userinfo,
  query ou fragmento (`src/config/settings.py`). O backend acrescenta
  `?checkout=success|cancel|expired`; aponte-a para a rota `/billing` da UI
  publicada. Sem ela, a criação de checkout retorna 503.
- A assinatura usa somente checkout hospedado mensal no cartão.
- Não há variável de ambiente que abra vendas ou recargas. O estado de rollout
  (`disabled`, `canary_allowlist`, `general`) e `topup_commercial_enabled`
  são controles persistidos e auditados no banco; ambos nascem fechados. A
  recarga comercial está adiada e permanece fechada.
- O flag `financial_egress_enabled` do AppConfig (seção seguinte) e a
  prontidão do webhook também bloqueiam a criação quando ausentes.

Em exemplos e evidências locais use valores vazios ou placeholders, não faça
egress financeiro e não copie tokens, payloads, dados de cartão ou PII.

## Flags de egress pelo AWS AppConfig Agent

A aplicação consulta o cache HTTP local do AWS AppConfig Agent imediatamente
antes do efeito externo. `APPCONFIG_ENABLED=false` desliga o mecanismo.

Antes de definir `APPCONFIG_ENABLED=true` em produção, o gate de ativação exige
evidência sanitizada e somente leitura da revision ativa da task definition, do
Agent marcado como essential e healthy, da imagem/versão implantada, do acesso
IAM ao data plane, dos IDs exatos de application/environment/profile e do
readback bem-sucedido das duas flags. Qualquer evidência ausente ou divergente
mantém `APPCONFIG_ENABLED=false`.

Quando ligado em produção, `APPCONFIG_APPLICATION`, `APPCONFIG_ENVIRONMENT` e
`APPCONFIG_PROFILE` são obrigatórios. `APPCONFIG_ORIGIN` aceita apenas uma
origem HTTP loopback, por exemplo `http://127.0.0.1:2772` ou
`http://localhost:2772`, sem userinfo, path, query ou fragment. O token Bearer
opcional vem de `APPCONFIG_TOKEN` e nunca deve ser colocado em logs ou arquivos
versionados.

Somente `financial_egress_enabled` e `uazapi_egress_enabled` são aceitas, como
decisões independentes no formato `{nome_da_flag: {enabled: boolean}}`. A
aplicação não mantém cache, refresher ou lifecycle próprios: cada consulta
observa o snapshot atual do agente. Configuração incompleta, agente
indisponível, timeout, resposta inválida ou flag ausente resultam em `false` e
zero efeito externo. Proxies do ambiente e redirects não participam dessas
consultas locais.

## Orçamento de conexões PostgreSQL

Cada processo da API possui exatamente um pool SQLAlchemy compartilhado por repositórios, EventStore e HandoffService. A configuração efetiva vem de `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW` e `DATABASE_POOL_TIMEOUT`. Produção usa, por processo, `pool_size=5`, `max_overflow=0` e timeout de 30 segundos.

O manifesto `config/database-connection-budgets.json` registra as topologias aprovadas. O gate calcula `tasks × workers × (pool_size + max_overflow)` e exige que o resultado permaneça estritamente abaixo de 70% de `max_connections`:

| Perfil | Tasks | Workers | Pool | Overflow | Total / max_connections |
|---|---:|---:|---:|---:|---:|
| `vps-compose` | 1 | 4 | 5 | 0 | 20 / 100 |
| `ecs-poc` | 2 | 4 | 5 | 0 | 40 / 100 |

No VPS, o deploy confere separadamente o comando de workers do Dockerfile, a propagação das três variáveis pelo Compose, os valores do `.env` e `max_connections=100` antes de `docker pull` ou `docker compose up`. O perfil `ecs-poc` mantém um orçamento separado e versionado para o deploy ECS.

## Worker do outbox `billing.fact.v1`

O worker periódico que reconcilia, escalona e (quando houver consumidor)
apresenta os fatos `billing.fact.v1` é **desligado por padrão**.
`BILLING_FACT_WORKER_ENABLED=false` não cria task, loop nem acesso ao banco, e
nenhuma métrica do worker recebe valor capaz de disparar alerta. O merge, porém,
não é neutro: ele altera `config/prometheus_rules.yml` e, portanto, o digest de
regras vinculado ao release (ver "Release" abaixo).

O worker **não tem transporte e não entrega nada hoje**: nenhum consumidor de
créditos existe, a produção passa `transport=None` e a apresentação está
bloqueada por uma guarda de código (ver "Apresentação bloqueada" abaixo).

No serviço ECS de produção o valor vem do parâmetro revisado
`BILLING_FACT_WORKER_ENABLED` em
`infra/production/asaas-production-wiring.params.yaml`: exatamente a string
`'false'` (o valor do repositório) ou `'true'`; qualquer outra grafia, um
booleano YAML ou a ausência bloqueia o plano. Ligar (ou desligar) o worker é
um PR revisado e mergeado que muda essa linha e o teste que fixa o valor
(`test_the_repository_keeps_the_outbox_worker_off`), seguido de uma nova
revisão da task definition (`docs/runbooks/asaas-deployment.md`, "The outbox
worker flag").

| Variável | Padrão | Limites | Efeito |
|---|---:|---|---|
| `BILLING_FACT_WORKER_ENABLED` | `false` | booleano | Liga um loop por processo da API (no ECS: 2 tasks × 4 workers = 8 loops). |
| `BILLING_FACT_WORKER_INTERVAL_SECONDS` | `30` | 5–300 | Intervalo entre ticks e orçamento de tempo de cada tick: depois dele nenhuma nova claim é feita. |
| `BILLING_FACT_WORKER_BATCH_SIZE` | `50` | 1–500 | Máximo de fatos apresentados por tick e tamanho de cada página de reconciliação/escalonamento. |
| `BILLING_FACT_WORKER_MAX_ATTEMPTS` | `10` | 1–50 | Apresentações sem ack antes do estado terminal `blocked`. |
| `BILLING_FACT_WORKER_STALE_AFTER_SECONDS` | `900` | 60–86400 | Idade a partir da qual um fato `pending`/`awaiting_ack` dispara o alerta de atraso (padrão 15 min). |

Garantias com vários processos, todas no PostgreSQL:

- **Claim sem dupla apresentação:** cada fato é reivindicado numa transação
  curta própria com `FOR UPDATE OF billing_fact_deliveries SKIP LOCKED`, e a
  linha fica travada até o commit da tentativa. Outro processo pula a linha em
  vez de esperar, e o backoff persistido (`last_attempt_at`, exponencial de
  60 s até 1 h) adia a próxima apresentação **de uma tentativa já
  commitada**. Ele não cobre uma queda entre o envio e o commit (ver
  "Apresentação bloqueada"). Um fato cujo ack foi commitado antes da releitura
  feita logo após o lock não é reapresentado; um ack commitado depois dessa
  releitura não interrompe a apresentação já em curso (ela acontece uma vez
  mais e o reconciliador converge o fato em seguida).
- **Manutenção serializada:** a reconciliação (página limitada dos fatos não
  `acknowledged`, com cursor que dá a volta) e o escalonamento rodam sob
  `pg_try_advisory_xact_lock`; quem não obtém o lock pula a fase sem esperar.
- **Retry limitado sem migration:** usa `attempt_count`, `last_attempt_at` e o
  estado existente `blocked`. Esgotado o limite, a entrega vira `blocked` e é
  gravado um alerta `backlog` (detalhe `retry cap exhausted`) em
  `billing_fact_reconciliation_alerts`. O fato nunca é apagado nem marcado
  `acknowledged` sem ack válido; um ack válido que chegue depois ainda o
  converge para `acknowledged`.
- **Apresentação bloqueada:** a tentativa só é contada depois que o transporte
  retorna, na mesma transação. Se o processo cair, ou o commit se perder,
  depois de um envio já visível ao consumidor, a entrega continua `pending`
  com `attempt_count` inalterado e é reapresentada no tick seguinte: a cada
  nova queda isso se repete **sem limite**, ignorando o backoff e
  `BILLING_FACT_WORKER_MAX_ATTEMPTS`. Por isso
  `PRESENTATION_PRECONDITIONS_OPEN = True` em `src/services/billing_outbox.py`
  faz `BillingFactWorker(transport=...)`, `publish_next`, e os caminhos legados
  `publish_pending`/`pending` levantarem `PresentationPreconditionsOpen`. A
  constante só pode virar `False` quando as três pré-condições forem cumpridas:
  (1) contar a tentativa num lease commitado **antes** de apresentar;
  (2) existir um caminho de requeue para entregas `blocked`;
  (3) o runbook cobrir o transporte real.
- **Ack válido:** o backlog (`unacked`/`stale`) e o escalonamento `ack overdue`
  ignoram fatos que já têm um ack válido (mesmo schema, mesmo `event_id`,
  `accepted` e digests iguais aos do outbox/handoff), mesmo antes de a página de
  reconciliação marcá-los `acknowledged`. Um ack divergente continua contando
  até o reconciliador mover o fato para `blocked`.
- **Sem consumidor:** hoje nenhum consumidor de créditos existe, então nenhum
  transporte é ligado. Os fatos ficam `pending`, sem tentativas consumidas e
  reapresentáveis quando o consumidor existir; após o limiar, cada fato recebe
  um alerta `backlog` (detalhe `ack overdue`) e o gauge
  `neurostack_billing_fact_backlog{condition="stale"}` dispara
  `BillingFactDeliveryStale`. O worker nunca chama ledger ou lógica de crédito.
- **Shutdown:** o lifespan para o loop antes de fechar o pool; o fato em voo
  termina (timeout de apresentação de 10 s, espera de até 15 s) e nenhum novo é
  reivindicado. Se for preciso cancelar, a transação faz rollback e o fato
  mantém o estado anterior. Um `CancelledError` que não foi dirigido à task do
  loop (por exemplo, levantado pelo código do consumidor ou do driver) é
  registrado em ERROR e contado como tick/tentativa falha, e o loop continua;
  um cancelamento real (shutdown) nunca é engolido.

Alertas em `config/prometheus_rules.yml` (grupo `neurostack_billing_facts`):
`BillingFactDeliveryStale`, `BillingFactDeliveryBlocked` e
`BillingFactWorkerStalled` (nenhum tick completo há mais de 10 min com o worker
ligado). O gauge de backlog é recalculado a partir do banco por todo loop, então
qualquer processo raspado reporta o estado global. Detalhes e primeiros passos:
`docs/runbooks/asaas-billing-reconciliation.md`.

Liveness: ao ligar, `start()` grava o horário de início em
`neurostack_billing_fact_worker_last_tick_timestamp_seconds`, e cada tick
completo o atualiza. Um worker que falha todos os ticks desde o boot (sem
nenhuma amostra de backlog) ainda envelhece esse timestamp e dispara
`BillingFactWorkerStalled` após 10 min; com a flag desligada o gauge fica em
`0`, que a regra ignora. Não há regra sobre a taxa de
`neurostack_billing_fact_worker_ticks_total{outcome="failed"}`: o `/metrics` não
usa o modo multiprocess do `prometheus_client` e a imagem roda 4 workers
uvicorn, então cada scrape lê o contador de um processo diferente e `rate()`
veria resets falsos. O contador serve para diagnóstico, não para alerta.

Release: este grupo mudou o digest de `config/prometheus_rules.yml` (e do
espelho byte-idêntico `src/config/prometheus_rules.yml`) de `5ff2ae8d…` para
`e37147012adaf5e847f6f6171e15443d268f8e51acde261ae728c1265f447dad`. A
limpeza de alertas do Group 10 (E7) mudou de novo: removeu o alerta morto de
intervenção manual da migração legada e as duas métricas da migração que
nada alimentava, e renomeou o alerta de readback para `BillingReadbackBacklog`
(mesma expressão sobre `neurostack_billing_readback_backlog`). O digest atual
é `7464018bfa2d496dddbef2184ffd1a842804c33f614ce90d878fc1eaeeb267f0`; o pin
`_RULES_SHA256` de `tests/test_billing_migration_surface_removed.py` muda
junto com este parágrafo.

Como a rotação funciona hoje:

- O digest é o SHA-256 dos bytes de `config/prometheus_rules.yml` **dentro da
  imagem**. O backend o recalcula ao gravar cada receipt de readiness do
  rollout e de novo na transição para `general`, então um receipt gravado com
  o digest anterior não vincula mais: grave os receipts de readiness depois
  que a imagem nova estiver no ar.
- `BILLING_PROMETHEUS_RULES_DIGEST` só é ligada no
  `docker-compose.production.yml`: ali a API confere contra ela o arquivo de
  regras montado do checkout (`./config/prometheus_rules.yml`, o mesmo que o
  Prometheus do overlay avalia), junto com o acknowledgement do receiver.
  Reemitir a variável e o acknowledgement vale só para esse overlay, e o
  checkout precisa estar no commit da imagem.
- As task definitions da AWS não definem a variável e não avaliam as regras:
  lá a rotação é só a imagem nova.

Ordem do E7 na AWS (o worker implantado é pré-requisito da rotação: código
implantado **e** `BILLING_FACT_WORKER_ENABLED=true`, decisão do owner de
2026-10-22; passos completos no runbook `docs/runbooks/asaas-deployment.md`,
"Open follow-ups recorded by E6"):

1. **Primeiro a flag, numa mudança própria.** O código do worker já está na
   imagem ponte, então `BILLING_FACT_WORKER_ENABLED=true` sai antes, na
   imagem ponte, como mudança revisada e deploy próprios (revisão nova
   `ManagedBy=manual` renderizada pelo planner a partir do parâmetro revisado
   `BILLING_FACT_WORKER_ENABLED`, `'false'` no repositório; o procedimento é
   "The outbox worker flag" no runbook).
2. **Observar.** O worker subiu em cada task, o last-tick avança e o outbox
   está vazio.
3. **Depois, a imagem do E7, nunca na mesma revisão.** Só então a imagem do
   E7 é construída, publicada e implantada, numa revisão posterior.

## Primeiro acesso: bootstrap do super_admin

Um banco novo, depois das migrations, não tem nenhum usuário. O
primeiro acesso ao painel vem de `scripts/bootstrap_admin.py`, o único arquivo de
`scripts/` incluído na imagem `production`. Numa transação, ele cria o tenant da
plataforma (slug `neurostack-admin`, `plan_type=admin`, UUID aleatório, API key
descartada) e o único `super_admin`. `scripts/seed_demo.py` é só para
desenvolvimento e não deve ser usado em ambientes acessíveis pela internet.

Ordem por ambiente, sempre com a mesma imagem (tag imutável) do service:
criação das roles do banco → task de migrations → task one-off de bootstrap. Ao
trocar de imagem, registre uma nova revisão de cada task definition apontando
para a mesma tag. O service pode já estar no ar; nenhum login funciona até o
bootstrap rodar. A role da aplicação precisa de DML nas tabelas criadas pelas
migrations (por exemplo, `ALTER DEFAULT PRIVILEGES` do dono do schema); sem
isso o bootstrap sai com `database error` sem escrever nada.

No banco de produção existente, a task de migrations roda sempre uma revisão
exata aprovada, nunca `upgrade` até o head: a janela 033-035 usa
`alembic upgrade 035` e as migrations 036 e 037 exigem, cada uma, instrução
própria do owner (ver o gate de migrations em
`docs/runbooks/asaas-deployment.md`).

```bash
python -m scripts.bootstrap_admin
```

| Variável | Origem | Regra |
|---|---|---|
| `DATABASE_URL` | secret da aplicação (role só DML) | mesma URL do service, não a de migrations |
| `ENVIRONMENT` | env | igual à do service |
| `BOOTSTRAP_ADMIN_EMAIL` | secret | já em minúsculas, até 255 caracteres; é o login exato, digitado em minúsculas |
| `BOOTSTRAP_ADMIN_PASSWORD` | secret | de 16 caracteres a 72 bytes UTF-8, sem espaço nas pontas nem caracteres não imprimíveis (cuidado com a quebra de linha final de secrets criados a partir de arquivo) |
| `BOOTSTRAP_ADMIN_RESET_PASSWORD` | override do `run-task`, opcional | `true` ou `false` (padrão); não fixe `true` na task definition |

`SECRET_KEY` e `REDIS_URL` não são necessários para essa task. No staging
(`us-east-1`), os secrets são `neurostack/staging/database-url`,
`neurostack/staging/bootstrap-admin-email` e
`neurostack/staging/bootstrap-admin-password`.

A saída de resultado é uma linha `[created|skip|reset] super_admin <email> on
tenant <id>`; a senha e os hashes nunca são impressos. Sem a flag, reexecutar
não escreve nada: imprime `skip` se a senha do secret confere com a do banco e
falha se não confere. Toda recusa é uma linha `[error] <motivo>` em stderr, com
código 1 e nada escrito. Duas execuções simultâneas não criam dois
`super_admin`s: uma sai com `[error] database error: IntegrityError` e a próxima
execução imprime `skip`.

`super_admin` não troca a própria senha pelo painel. Para trocar ou recuperar,
atualize `BOOTSTRAP_ADMIN_PASSWORD` no Secrets Manager e rode a task uma vez com
`BOOTSTRAP_ADMIN_RESET_PASSWORD=true` como override e o mesmo e-mail (saída
`[reset]`). Não ative rotação automática nesse secret: a senha real só muda
quando a task roda com a flag, e a próxima execução sem flag acusa a divergência.

O script só mantém o `super_admin` que ele mesmo criou no tenant de plataforma,
com exatamente o e-mail informado. Desde a migration `025`, o banco não aceita
dois e-mails que diferem só por maiúsculas (índice único em `lower(email)`), e
as rotas de admin e de builder respondem 409 para essa variante. Em bancos
anteriores à `025`, variantes de caixa ao lado do `super_admin` exato não
atrapalham a manutenção dele; sem o e-mail exato, o script recusa criar mais
uma variante.

Estes estados são recusados sempre:

- outro `super_admin` já existe: se for a conta do próprio bootstrap com o
  e-mail anterior (o secret de e-mail foi trocado), restaure o e-mail antigo no
  secret; o erro mostra qual é;
- o e-mail pertence a outro papel ou a uma conta inativa, ou só existem
  variantes de caixa dele: no primeiro bootstrap, basta usar outro e-mail no
  secret;
- o `super_admin` está fora do tenant de plataforma (por exemplo, o criado por
  `seed_demo.py` no tenant `a0000000-0000-0000-0000-000000000001`);
- já existe um tenant com o slug `neurostack-admin` sem `super_admin`.

Os casos sem saída pelo secret se resolvem com SQL manual revisado. A rota de
admin que apaga um tenant (só um `super_admin` pode chamá-la) remove também os
usuários dele; a próxima execução só volta a criar a conta se não sobrar nenhum
`super_admin` em outro tenant.

## Receipts de operação do billing (control plane)

O backend assina os receipts de operação (`POST /v1/admin/billing/operations/receipts`).
O operador envia só o material da operação, o `assignment_id` e o `nonce`; um
campo `signature` no corpo é recusado com 422. Depois de validar o JWT
`super_admin`, o assignment ativo do próprio ator e o nonce, o backend calcula
o HMAC-SHA256 sobre o material, a revisão de rollout (só nas aberturas
`-> canary_allowlist` e `-> general`), o papel, o ator, o assignment e o nonce,
e o grava. Cada consumo da quorum recalcula a assinatura a partir da linha
gravada.

| Variável | Origem | Regra |
|---|---|---|
| `BILLING_OPERATION_RECEIPT_SIGNING_KEY` | secret do service | só o backend a conhece; nunca vai para operador, ferramenta ou ticket; em produção tem pelo menos 32 bytes |
| `BILLING_OPERATION_RECEIPT_MAX_TTL_SECONDS` | env, opcional | inteiro de 1 a 604800 (7 dias), padrão `86400` (24 h); `expires_at` de um assignment ou receipt pode ficar no máximo esse tempo depois da criação (o limite exato é aceito); acima disso a resposta é `409 billing_operation_expiry_too_long`. Receipts de prontidão não podem durar mais que os receipts que citam, então herdam o teto |

Quorums de abertura de rollout se assinam **depois** de o estado atual existir
e nunca ficam em fila entre mudanças de estado: qualquer transição no meio
torna a quorum inútil (`billing_rollout_quorum_stale` ou
`billing_operation_signature_invalid`) e ela precisa ser assinada de novo.

## Frontend servido pela API (`SPA_*`)

A API serve o build do frontend (`frontend/artifacts/neurostack`, saída
`dist/public` do Vite) em caminhos **exatos**, calculados uma vez na
inicialização por `src/api/spa.py`: as rotas do `App.tsx` (e o gêmeo com barra
final, exceto `/` e `/traces/`), `/traces/<id>` com `id` de 1 a 128 caracteres
`[A-Za-z0-9_-]`, cada arquivo regular de `assets/` como `/assets/<nome>` e os
arquivos do topo (`/favicon.png`, `/favicon.svg`, `/opengraph.jpg`). Só GET e
HEAD desses caminhos dispensam autenticação; qualquer variante (barra dupla,
caixa, codificação, `..`, outro método) continua recebendo o 401 de hoje.
`/privacidade/:clinicId` continua sendo a página renderizada pela API.

| Variável | Padrão | Regra |
|---|---|---|
| `SPA_ENABLED` | `true` | `false` desliga o frontend sem mexer na imagem |
| `SPA_DIST_DIR` | vazio | diretório do build (a imagem define `/app/spa`); vazio, inexistente, sem `index.html` regular, com `index.html` fora de UTF-8, com `assets/` ilegível (permissão, symlink, não diretório) ou com `index.html` referenciando um `/assets/...` que não foi carregado não registra nada (um warning `spa_not_installed`); a ausência de `assets/` só é aceita se o `index.html` não referenciar nenhum `/assets/...` |

Falha fechada: se algum caminho cair sob um prefixo reservado (`/v1`, `/api/`,
`/docs`, `/redoc`, `/openapi.json`, `/metrics`, `/health`, `/ready`, `/live`,
`/privacidade/`) ou colidir com uma rota já registrada, nada é servido e o log
`spa_not_installed` (nível error) lista os conflitos. Os caminhos exatos são
testados contra a tabela de rotas; para `/traces/<id>` qualquer rota ou mount
sob `/traces/`, qualquer rota com parâmetro que possa cobrir `/traces/` (por
exemplo `/{x}` ou `/{x}/stats`), um mount na raiz, uma rota sem caminho
analisável (`Host`) ou uma rota com `matches` próprio (na classe ou atribuído
na instância) conta como colisão; rotas de routers incluídos são avaliadas pelo
caminho efetivo (com prefixo) que o FastAPI realmente despacha. Em todos esses
casos a API sobe normalmente (`/health` continua verde) e o frontend fica
desligado: o sinal é o log `spa_not_installed`. Diretórios, arquivos que não
são regulares, symlinks, dotfiles, nomes fora de `[A-Za-z0-9._-]`, source maps,
HTML além do `index.html` e extensões desconhecidas são ignorados e listados
uma vez em `spa_build_entries_skipped`. Nada depende de `ENVIRONMENT` (o
service que atende produção roda com `ENVIRONMENT=staging`).

A mesma verificação roda de novo no fim do startup, depois de todo o lifespan
(o da aplicação, os dos routers incluídos e os handlers `on_startup`) e antes
de o servidor receber `lifespan.startup.complete`. O frontend é desligado ali
mesmo se algum hook tiver acrescentado ou inserido uma rota que colida (por
exemplo `/traces/{x}` num router incluído, ou qualquer rota num caminho exato
do SPA), se a própria verificação falhar (`recheck_failed:<erro>`) ou se o
lifespan tiver sido trocado depois de `install_spa` (um `include_router`
posterior mescla o lifespan do router, cujos hooks rodariam depois da
verificação: `lifespan_replaced_after_install`). Por isso `install_spa` tem de
ser a última coisa em `create_app` a mexer em rotas e lifespan. Desligado,
nenhum caminho do SPA fica público nem é servido: GET anônimo recebe o 401 de
sempre, exceto `/`, que continua público para o middleware de autenticação e
responde 404 (como com o SPA desligado); a requisição autenticada a um caminho
do SPA recebe 404, a menos que a rota que colidiu esteja à frente da do SPA na
tabela (router incluído ou rota inserida no topo), caso em que é essa rota que
responde. Um único log `spa_conflict_after_startup` (nível error) lista os
conflitos e a API continua no ar.

Sem lifespan não há reverificação: com `uvicorn --lifespan off`, com o
`TestClient` usado fora de `with` ou com `httpx.ASGITransport` nenhum hook
roda, e uma rota acrescentada depois de `install_spa` fora de qualquer hook
(por exemplo `app.router.routes.insert(0, ...)` direto no código) não é
verificada por ninguém.

Cabeçalhos só nas respostas do frontend (nunca em `/v1`, `/docs`, `/redoc`):
`index.html` sai da memória com `Cache-Control: no-cache`, ETag forte e 304;
`/assets/*` com `public, max-age=31536000, immutable`; os arquivos do topo com
`public, max-age=86400`; todos com `X-Content-Type-Options: nosniff`,
`Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY` e a
`Content-Security-Policy` aplicada (não report-only) definida e comentada em
`src/api/spa.py`.

---

## Índice

1. [Onde as configurações são armazenadas](#onde-as-configurações-são-armazenadas)
2. [Estrutura do config do tenant](#estrutura-do-config-do-tenant)
3. [ClinicConfig (configuração da clínica)](#clinicconfig-configuração-da-clínica)
4. [Valores permitidos (enums)](#valores-permitidos-enums)
5. [Exemplo completo](#exemplo-completo)
6. [Modalidades dos serviços](#modalidades-dos-serviços)
7. [Uso no pipeline](#uso-no-pipeline)

---

## Onde as configurações são armazenadas

### ClinicConfig (modelo fixo) — tabela SQL

A configuração da clínica (**ClinicConfig**) é um modelo fixo armazenado no **banco SQL**, na tabela **`clinic_configs`** (uma linha por tenant, relação 1:1).

| O quê | Onde |
|-------|------|
| **Persistência** | Tabela **`clinic_configs`** (colunas: `id`, `tenant_id`, `clinic_id`, `version`, `persona`, `scheduling`, `pricing`, `post_care`, `handoff` em JSONB; `created_at`, `updated_at`) |
| **Modelo ORM** | `src/models/clinic_config_table.py` → `ClinicConfigTable` |
| **Modelo Pydantic** | `src/models/clinic_config.py` → `ClinicConfig` |
| **Acesso no código** | `tenant.get_clinic_config()` retorna `ClinicConfig` ou `None` (lê primeiro da tabela, depois fallback em `config` JSON) |
| **Repositório** | `TenantRepository.upsert_clinic_config(tenant_id, clinic_config)`, `get_by_id_with_clinic_config(tenant_id)`, `find_by_api_key_hash_with_clinic_config(api_key_hash)` |

**Retrocompatibilidade:** Se não existir linha em `clinic_configs` para o tenant, `get_clinic_config()` usa o valor em `tenants.config["clinic_config"]` (JSON), quando existir.

### Config geral do tenant (rate limit, webhook, etc.)

A configuração **geral** do tenant (rate limit, webhook, features, branding) continua na tabela **`tenants`**, coluna **`config`** (JSONB).

| O quê | Onde |
|-------|------|
| **Persistência** | Tabela `tenants`, coluna `config` (JSONB) |
| **API / contratos** | `src/api/schemas/tenants.py` (TenantConfig com campo `clinic_config`) |

Cada **tenant** representa um builder ou clínica. A configuração geral fica em `tenants.config`; a configuração da clínica (ClinicConfig) fica na tabela `clinic_configs`.

---

## Estrutura do config do tenant

O campo `config` do tenant é um objeto JSON que pode conter:

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `description` | string | Descrição do tenant/clínica |
| `rate_limit` | int | Requisições por minuto (ex.: 100) |
| `session_ttl_seconds` | int | TTL da sessão em segundos (ex.: 86400) |
| `max_sessions_per_user` | int | Máximo de sessões ativas por usuário |
| `webhook_url` | string | URL para webhooks (handoff, eventos) |
| `features` | object | `rag_enabled`, `handoff_enabled`, `safety_strict_mode` |
| `branding` | object | `primary_color`, `logo_url`, etc. |
| `custom_settings` | object | Configurações customizadas |
| **`clinic_config`** | **object** | (Legado/fallback.) Configuração da clínica; o valor canônico fica na tabela `clinic_configs`. Ver abaixo. |

---

## ClinicConfig (configuração da clínica)

A chave `clinic_config` contém a configuração usada pelo SRM, Policy, CTX e Handoff (documentação MVP v0.5).

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `clinic_id` | UUID (opcional) | Identificador da clínica |
| `version` | int | Versão do schema (ex.: 1) |
| **persona** | object | Nome da clínica, do agente, uso de nome pessoal |
| **scheduling** | object | Agendamento (agente, chave RAG de horários) |
| **pricing** | object | Preços (modo de divulgação, tabelas, condições) |
| **post_care** | object | Pós-atendimento (instruções, avisos de emergência) |
| **handoff** | object | Handoff (modo padrão, SLAs por setor) |

### persona

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `clinic_display_name` | string | Nome exibido da clínica |
| `agent_display_name` | string | Nome exibido do assistente virtual |
| `use_personal_name` | boolean | Usar nome pessoal (ex.: “Dra. Ana”) |

### scheduling

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `has_scheduling_agent` | boolean | Se há agente de agendamento |
| `hours_rag_key` | string | Chave RAG para conteúdo de horários de atendimento |

### pricing

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `disclosure_mode` | enum | `consult_only`, `range_allowed`, `exact_allowed`, `no_prices` |
| `range_table_available` | boolean | Tabela de faixas disponível |
| `exact_table_available` | boolean | Tabela de valores exatos disponível |
| `range_format` | string | Formato de exibição da faixa (ex.: "A partir de R$ {min}") |
| `special_conditions_note_enabled` | boolean | Exibir nota de condições especiais |

### post_care

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `standard_instructions_rag_key` | string | Chave RAG para instruções pós-atendimento |
| `emergency_warning_text_enabled` | boolean | Exibir aviso de emergência |

### handoff

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `default_mode` | enum | `whatsapp_human_takeover`, `crm_task_create` |
| `sector_defaults` | object | SLA por setor (ver [Valores permitidos](#valores-permitidos-enums)) |

**Setores em sector_defaults:** `agendamento`, `financeiro`, `administrativo`, `gerencia`, `clinico`, `urgencia`, `fidelizacao`.

---

## Valores permitidos (enums)

### pricing.disclosure_mode

| Valor | Descrição |
|-------|-----------|
| `consult_only` | Só informar que valores são sob consulta |
| `range_allowed` | Pode informar faixa de preços |
| `exact_allowed` | Pode informar valores exatos |
| `no_prices` | Não divulgar preços |

### handoff.default_mode

| Valor | Descrição |
|-------|-----------|
| `whatsapp_human_takeover` | Transferência para humano (ex.: WhatsApp) |
| `crm_task_create` | Criar tarefa no CRM |

### handoff.sector_defaults (SLA por setor)

| Valor | Descrição |
|-------|-----------|
| `imediato` | Resposta imediata |
| `prioritario` | Prioridade alta |
| `rotina` | Rotina |

## Modalidades dos serviços

O endpoint administrativo `PATCH /v1/config` recebe um corpo **plano**.
`service_format` é o formato global da clínica e `services` substitui o array
inteiro. Cada item de serviço pode ser um objeto extensível ou uma string
legada. Em objetos, `service_format` aceita somente `online`, `presential` ou
`both`; a ausência e `null` permanecem compatíveis com dados existentes.
Omitir `services` preserva o catálogo atual; `services: []` o substitui por uma
lista vazia; e `services: null` persiste `null`. GET e PATCH retornam o último
caso como `services: []`, pois a resposta plana apresenta o catálogo como
array. Strings legadas, por exemplo `"Retorno"`, continuam aceitas.

```json
{
  "service_format": "both",
  "services": [
    {
      "name": "Consulta por vídeo",
      "service_format": "online",
      "frontend_metadata": {"color": "blue"}
    },
    "Retorno"
  ]
}
```

O armazenamento usa a representação aninhada `operations.service_format` e a
coluna JSON `services`; ela não é o formato do request da API. A resposta 200
também é plana e preserva os campos extras do item:

```json
{
  "service_format": "both",
  "services": [
    {
      "name": "Consulta por vídeo",
      "service_format": "online",
      "frontend_metadata": {"color": "blue"}
    },
    "Retorno"
  ]
}
```

Um serviço incompatível retorna 422 com um detalhe estável, sem ecoar nomes ou
descrições de serviços:

```json
{
  "detail": {
    "error": "service_format_conflict",
    "message": "One or more service modalities are incompatible with the clinic service format.",
    "clinic_service_format": "online",
    "allowed_service_formats": ["online"],
    "conflicts": [{"index": 1, "service_format": "presential"}],
    "action": "Change the listed service modalities or broaden the clinic service format."
  }
}
```

### Compatibilidade e fallback no contexto da IA

| Formato global canônico | Modalidades permitidas nos objetos de serviço | Fallback para serviço sem modalidade |
|-------|-------|-------|
| `online` | `online` | `online` |
| `presential` | `presential` | `presential` |
| `both` | `online`, `presential`, `both` | nenhum |
| vazio ou histórico | não impõe validação cruzada | nenhum |

A modalidade explícita do serviço sempre prevalece sobre a global. O contexto
da IA não infere canal para `both`, formato vazio ou legado e não declara
disponibilidade, agendamento ou autorização. A validação cruzada só roda em
PATCH que contenha `services` ou `service_format`; um PATCH sem esses campos
não reinterpreta dados históricos. `services: []` grava uma lista vazia e
`services: null` é mantido como nulo na persistência, embora GET/PATCH o
retornem como `services: []`.

---

## Exemplo completo

Um exemplo completo de `config` do tenant (incluindo `clinic_config`) está em:

**[examples/tenant-config-with-clinic.json](./examples/tenant-config-with-clinic.json)**

Trecho principal:

```json
{
  "description": "Clínica de Dermatologia e Estética - atendimento via WhatsApp",
  "rate_limit": 100,
  "session_ttl_seconds": 86400,
  "max_sessions_per_user": 10,
  "webhook_url": "https://api.minha-clinica.com/webhooks/neurostack",
  "features": {
    "rag_enabled": true,
    "handoff_enabled": true,
    "safety_strict_mode": true
  },
  "branding": {
    "primary_color": "#1a365d",
    "logo_url": "https://cdn.minha-clinica.com/logo.png"
  },
  "custom_settings": {},
  "clinic_config": {
    "clinic_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "version": 1,
    "persona": {
      "clinic_display_name": "Clínica Saúde & Beleze",
      "agent_display_name": "Assistente Virtual",
      "use_personal_name": false
    },
    "scheduling": {
      "has_scheduling_agent": true,
      "hours_rag_key": "horarios-atendimento"
    },
    "pricing": {
      "disclosure_mode": "range_allowed",
      "range_table_available": true,
      "exact_table_available": false,
      "range_format": "A partir de R$ {min} (consulta tipo {tipo})",
      "special_conditions_note_enabled": true
    },
    "post_care": {
      "standard_instructions_rag_key": "pos-atendimento-padrao",
      "emergency_warning_text_enabled": true
    },
    "handoff": {
      "default_mode": "whatsapp_human_takeover",
      "sector_defaults": {
        "agendamento": "prioritario",
        "financeiro": "rotina",
        "administrativo": "rotina",
        "gerencia": "prioritario",
        "clinico": "prioritario",
        "urgencia": "imediato",
        "fidelizacao": "rotina"
      }
    },
    "operations": {
      "service_format": "both"
    },
    "services": [
      {
        "name": "Consulta por vídeo",
        "service_format": "online"
      },
      {
        "name": "Avaliação presencial",
        "service_format": "presential"
      }
    ]
  }
}
```

Use esse JSON como corpo do objeto `config` ao criar ou atualizar um tenant. O bloco `clinic_config` é o que o NeuroStack utiliza para persona, agendamento, preços, pós-atendimento e handoff por setor.

---

## Uso no pipeline

- O **PipelineInput** aceita `clinic_config` opcional (dict). Em produção, carregue o tenant com `get_by_id_with_clinic_config(tenant_id)` ou `find_by_api_key_hash_with_clinic_config(api_key_hash)` e use `tenant.get_clinic_config().model_dump()` para obter o dict (ou `None`).
- O **SRM**, **Policy** e **CTX** recebem `clinic_config` quando disponível e usam para decisões de intent, políticas de preço e handoff (setor/SLA).

## Migração e persistência

- **Criar/atualizar ClinicConfig:** use `TenantRepository.upsert_clinic_config(tenant_id, clinic_config: ClinicConfig)` para gravar na tabela `clinic_configs`.
- **Migração Alembic:** revisão `002` cria a tabela `clinic_configs`. Para aplicar, migre para a revisão aprovada para o ambiente (`alembic upgrade <revisão>`; `head` só num banco local de desenvolvimento, via `make db-migrate`).

## Credenciais e catálogo público de provedores

As credenciais de provedores pertencem à fronteira dedicada
`/v1/config/api-credentials`. A rota genérica de configuração não aceita
campos `llm_*`. Leitura e escrita exigem JWT de `clinic_admin`, `builder` ou
`super_admin`; chaves de API de tenant não podem administrar credenciais. Um
builder só pode selecionar uma clínica que pertence ao seu próprio
`builder_id`. Segredos nunca são retornados: a resposta expõe apenas o estado
`*_set` e a máscara de tamanho constante.

O catálogo público atual é:

| Capacidade | Configuráveis | Ainda `coming_soon` |
|---|---|---|
| WhatsApp | Meta Cloud API (`meta_cloud`), Z-API (`zapi`), Uazapi (`uazapi`) e Evolution API (`evolution`) | 360dialog (`dialog360`) e Twilio (`twilio`) |
| LLM | Dropout (`dropout`), Gemini (`gemini`), GPT/OpenAI (`openai`) e Outros (`outros`) | SLM Cortex (`slm_cortex`) |
| STT | Whisper/OpenAI (`openai_whisper`), Scribe/ElevenLabs (`elevenlabs_scribe`) e Outros by OpenRouter (`openrouter`) | Google Cloud STT (`google_stt`), Deepgram (`deepgram_stt`) e Azure STT (`microsoft_stt`) |
| TTS | ElevenLabs (`elevenlabs`), Google Cloud TTS (`google`) e OpenAI TTS (`openai`) | — |
| Fallback multimodal | — | OpenRouter (`openrouter`) |

Em STT, o card público “Outros by OpenRouter” usa o ID explícito
`openrouter`. O ID genérico `outros` continua reconhecido somente para leitura e
migração de configurações legadas; ele não aparece como opção para uma nova
configuração.

Evolution e Uazapi aceitam hosts configuráveis e, por isso, não podem herdar o
segredo associado a uma URL anterior. Entrar ou trocar para um desses providers
exige o bundle completo no mesmo PATCH: `whatsapp_provider`, URL, nome da
instância e uma chave/token nova. Alterar uma `evolution_api_url` ou
`uazapi_api_url` para outro valor não vazio exige o mesmo bundle e a seleção
explícita do provider, inclusive quando os campos estão dormentes sob outro
provider. Um patch apenas de nome da instância continua válido quando provider
e endpoint não mudam. Ao sair ou descomissionar a integração, a URL pode ser
limpa explicitamente com `null` sem reapresentar o segredo antigo.

O contrato compartilhado de LLM usa exatamente quatro campos:

| Campo | Regra |
|-------|-------|
| `llm_provider` | Identificador wire: `dropout`, `gemini`, `openai` ou `outros` no catálogo público atual |
| `llm_api_url` | Base HTTPS do provedor |
| `llm_api_key` | Segredo write-only |
| `llm_model_name` | Modelo habilitado pelo provedor |

Para Gemini, a única base aceita é
`https://generativelanguage.googleapis.com/v1beta/openai`. Host, caminho e
ausência de porta explícita são fixos: até `:443` é recusado. Query, fragmento,
userinfo, outro host ou outro caminho também são recusados. Não existe override
por variável de ambiente ou tenant para a base, o prompt do smoke ou a política
de saída. O transporte resolve DNS na fronteira, valida todos os endereços como
públicos, fixa o IP da conexão e preserva
`generativelanguage.googleapis.com` em Host/SNI. Redirects são proibidos.

`llm_provider=outros` é o campo aberto para provedores com API compatível com
OpenAI, inclusive OpenRouter. A `llm_api_url` deve ser uma base HTTPS pública e
passa pelas mesmas proteções de saída segura; portanto, um provedor compatível
não precisa ganhar um card próprio para poder ser usado por esse caminho.

Qualquer entrada ou troca para um provider LLM ativo, assim como uma mudança de
`llm_api_url`, exige que os quatro campos sejam enviados juntos com chave nova.
Um patch parcial falha atomicamente com `provider_switch_bundle_required`;
nenhum campo é gravado. Quando provider e endpoint não mudam, um patch apenas
de modelo preserva a URL e a chave existentes. Valores vazios, `null` e máscaras
(`••••…` ou `****…`) nunca apagam nem substituem um segredo salvo; numa troca de
provider ou endpoint eles não contam como chave nova.

Um `llm_provider` presente na clínica é autoritativo mesmo se estiver incompleto
ou inválido. A herança do builder só ocorre quando a clínica não
tem o campo `llm_provider` e existe exatamente um tenant proprietário autorizado
do builder com bundle operacional completo. A resolução não carrega segredos de
outro tenant quando a clínica já declarou provider. No RLHF, `clinic_tenant_id`
e o tenant do `message_id` precisam existir, concordar e ser autorizados para o
clinic admin, builder ou tenant API key antes de qualquer leitura de segredo ou
chamada externa.

Erros públicos são estáveis e não incluem entrada ou segredo:

- `invalid_provider_endpoint`: a base não corresponde ao endpoint canônico de
  um provider fixo (Gemini em LLM; Scribe ou OpenRouter em STT);
- `provider_switch_bundle_required`: entrada ou troca para Evolution/Uazapi,
  mutação não vazia de seus endpoints configuráveis, ou entrada/troca/mudança
  de endpoint em LLM/STT/TTS sem todos os campos obrigatórios e uma nova
  chave/token;
- `provider_not_available`: provider reconhecido, mas indisponível;
- `invalid_provider_for_capability`: wire id desconhecido para a capacidade
  informada;
- `invalid_credentials_payload` / `unsafe_api_url`: payload ou URL recusado na
  fronteira genérica.

### Limitação beta e smoke sanitizado

O endpoint compatível com OpenAI do Gemini está em `v1beta`. Gemini permanece
publicamente configurável, agora junto de Dropout, OpenAI e `outros`. Para o
fluxo de release específico do Gemini, essa disponibilidade de produto não
substitui o gate operacional: apenas o sucessor receipt-only assinado do
candidate homologado passa o gate de produção.

O smoke independente usa somente duas entradas de ambiente privadas:

```bash
docker run --rm --read-only --tmpfs /tmp \
  --env GEMINI_API_KEY --env GEMINI_MODEL "$image_digest" \
  python -m src.llm.gemini_smoke --json \
  --tested-commit "$tested_commit" --image-digest "$image_digest" --run-id "$run_id"
```

A base e o prompt são fixos. A saída é uma única linha JSON canônica contendo
somente `schema`, `result`, `result_code`, `validation_kind`,
`validation_authority`, `timestamp`, `run_id`, `tested_commit`, `image_digest`,
`smoke_module_sha256` e `exit_code`. Ela nunca
imprime chave, prompt, resposta do provedor ou corpo de exceção. Classes de falha
incluem `missing_api_key`, `missing_model`, `provider_http_error`,
`provider_transport_error`, `invalid_provider_response`, `unexpected_response`
e `internal_error`.
Uma resposta sem a estrutura OpenAI esperada (`choices[].message.content`) é
`invalid_provider_response`; uma resposta estruturalmente válida cujo conteúdo
está vazio ou não pode ser usado é `unexpected_response`.

Para troubleshooting do gate de release:

1. `missing_api_key`/`missing_model`: corrija apenas o secret store privado;
2. `provider_http_error`: confirme acesso da chave ao modelo, sem copiar a
   resposta do provedor para logs ou tickets;
3. `provider_transport_error`: confirme DNS e HTTPS de saída para o host fixo;
4. `invalid_provider_response`/`unexpected_response`: confirme a compatibilidade
   beta do modelo;
5. smoke verde não libera sozinho: Operations precisa embutir os bytes e SHA no
   receipt, criar o sucessor assinado receipt-only e manter esse commit como HEAD.

## STT: Scribe e OpenRouter no catálogo público

Os IDs wire explícitos são `elevenlabs_scribe` (ElevenLabs Scribe) e
`openrouter` (OpenRouter). Ambos estão no catálogo público e podem ser
selecionados por tenants novos ou existentes. `outros` permanece um adapter
legado distinto e oculto: não é alias, rótulo alternativo nem fallback de
OpenRouter. Configurações legadas continuam legíveis e podem migrar para um dos
IDs explícitos sem que o card antigo volte ao catálogo.

A promoção pública habilita seleção e persistência das credenciais; ela não é
homologação live de chave, quota, modelo ou formato em produção. Os mocks
determinísticos provam o contrato local de configuração e transporte. Nenhum
probe live foi executado ou é autorizado por esta documentação.

### Bundle operacional e compatibilidade legado

Uma entrada ou troca para qualquer provider STT ativo, assim como uma mudança de
`stt_api_url`, exige no mesmo PATCH um bundle fresco com `stt_provider`,
`stt_api_url`, `stt_api_key` e `stt_model_name`. A chave precisa ser nova;
máscara, vazio e `null` não contam. Quando provider e endpoint não mudam, uma
edição apenas de modelo ou controle pode ser parcial. Para Scribe ou OpenRouter,
a URL precisa ser a base canônica da tabela abaixo. `audio_max_seconds` não
integra o bundle e mantém o default 60 quando ausente.

Um incumbent legado resolvido como `outros` pode repetir exatamente sua visão
canônica sem reescrever o JSON persistido ou migrar com um bundle fresco para
`elevenlabs_scribe` ou `openrouter`. `outros` não pode ser escolhido para uma
configuração nova nem reintroduzido pelo catálogo público.

### Bases, paths e wire contract

| ID | Base persistida exata | Sufixo POST | Autenticação | Campos multipart |
|---|---|---|---|---|
| `elevenlabs_scribe` | `https://api.elevenlabs.io/v1` | `/speech-to-text` | `xi-api-key: <secret>` | `file`, `model_id=<stt_model_name>` |
| `openrouter` | `https://openrouter.ai/api/v1` | `/audio/transcriptions` | `Authorization: Bearer <secret>` | `file`, `model=<stt_model_name>`, `language` quando informado |

As bases são literais: porta explícita (inclusive `:443`), query, fragmento,
userinfo, host/path alternativo ou URL já contendo o sufixo são recusados. O
modelo não é hardcoded: `stt_model_name` é obrigatório e é repassado no campo
próprio de cada provedor.

Ambos enviam os bytes originais em multipart `file`; este contrato escolhe
multipart e não JSON/base64. O MIME recebido é normalizado para lowercase e sem
parâmetros válidos antes do envio (por exemplo,
`audio/ogg; codecs=opus` vira `audio/ogg`), preservando os bytes. Os tipos
fechados são `audio/wav`, `audio/mpeg`, `audio/flac`, `audio/mp4`, `audio/ogg`,
`application/ogg`, `audio/webm` e `audio/aac`; tipo ou parâmetro malformado é
recusado antes da rede. A resposta deve ser JSON objeto e o transcript vem
exclusivamente de `.text`, após trim; texto ausente/inválido ou vazio falha.

A taxonomia pública/configuração permanece fechada:
`invalid_provider_endpoint`, `provider_switch_bundle_required`,
`provider_not_available`, `invalid_provider_for_capability`,
`invalid_credentials_payload` e `unsafe_api_url`. A fronteira de transporte
usa somente `stt_provider_unsupported`, `unsupported_audio_mime`,
`stt_configuration_invalid`,
`stt_provider_http_error`, `stt_provider_transport_error`,
`stt_malformed_response` e `stt_empty_transcript`. Corpo, header, URL, modelo e
segredo externos não são encadeados nesses erros.

Referências oficiais: [OpenRouter — transcription](https://openrouter.ai/blog/tutorials/transcription-on-openrouter/)
e [ElevenLabs — speech-to-text convert](https://elevenlabs.io/docs/api-reference/speech-to-text/convert).

---

*Documentação mantida pela equipe NeuroStack*
