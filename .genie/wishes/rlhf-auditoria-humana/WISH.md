# Wish: Auditoria humana sobre RLHF

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `rlhf-auditoria-humana` |
| **Date** | 2026-09-06 |
| **Author** | Claude |
| **Appetite** | large |
| **Branch** | `wish/rlhf-auditoria-humana` |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/rlhf-auditoria-humana/DESIGN.md) |

## Summary

O painel de auditoria RLHF já mede e já opina, mas não fecha o ciclo humano: o master lê uma
nota 2 e não consegue abrir o trace que a originou, registrar o próprio parecer, marcar o que
já revisou nem separar o que falta. Este wish tira a nota do event store dos traces para uma
tabela própria, resolve canal e trace na escrita, cria o parecer humano append-only com
histórico, e entrega os filtros de canal e de revisão — encerrando o item no Roadmap do
frontend.

## Scope

### IN

**Backend**

- Tabelas `rlhf_ratings` e `rlhf_audit_reviews`, com `ondelete` explícito em toda FK e
  `session_id` deliberadamente sem FK.
- Migração nova (`020` se o head continuar em `019` na execução), sem cópia e sem remoção do
  legado em `events`.
- `POST /v1/rlhf/rating` grava em `rlhf_ratings`, resolve `channel` e o par
  `trace_event_id`/`trace_tenant_id` na escrita e devolve `rating_id`.
- `subject_tenant_id` ganha o degrau do meio e passa a usar a mesma cadeia de três níveis da
  resolução de credenciais LLM.
- `_analyze_rating_async` recebe o evento já resolvido em vez de reconsultá-lo.
- `_update_rating_status` grava `ai_analysis` na tabela nova, preservando a opinião da IA.
- `GET /v1/rlhf/ratings` preserva o contrato de 7 passos e ganha `channel` (passo 4, todos os
  papéis) e `reviewed` (passo 6, super admin), mais `summary.reviewed_count` contando as
  revisadas dentre as notas com motivo qualificante.
- Parecer, autoria, histórico, `reviewed_count` e o filtro `reviewed` servidos apenas a
  `super_admin`.
- `POST /v1/rlhf/ratings/{id}/review` registra parecer append-only com `revisado_por_email`.
- `PATCH /v1/rlhf/ratings/{id}` portado para a tabela nova, preservando guard 422,
  exclusividade approve/ignore, `accepted_count` e carimbos `accepted_by`/`accepted_at`.
- Mascaramento `mask_carteirinha` e isolamento por tenant preservados na serialização nova.
- Teste de integração PostgreSQL provando que `DELETE /v1/admin/clinics/{id}` continua 200 com
  notas e pareceres existentes.

**Frontend**

- Filtro de canal (Todos / Chat / WhatsApp / Não classificado) e filtro Revisado.
- Campo de observação, botão REVISADO e histórico de pareceres por feedback escrito.
- Botão "Ver trace" quando há `trace_event_id`, com texto explicativo quando não há.
- `/traces/:id` lê `?tenant_id=` da URL, honrado só para os papéis que já encaminham o
  parâmetro, com `viewingClinicId` como fallback.
- Tenant do trace desce como prop até `TraceViewer` e `RatingBlock` nas **duas** chamadas do
  componente (`trace-detail.tsx:168` e `chat.tsx:629`).
- Dropdowns de builder e clínica ocultos fora do super admin; para `investor`, ocultos também o
  dropdown Revisado, o KPI `reviewed_count` e todos os controles de escrita.
- `/rlhf-audit` sai de `roadmapItems` e entra em `navItems`, preservando
  `hideFor: ["clinic_admin", "builder"]`.

### OUT

- **Captura de nota pelo WhatsApp.** Produto novo (mensagem ativa, parsing de resposta,
  política de quando perguntar); este wish só classifica o canal do que já é avaliado
  internamente.
- **Cópia ou remoção do legado `rlhf.rating` em `events`.** Decisão do usuário: começar do
  zero. As linhas antigas ficam sem leitor, como já aconteceu com `rlhf.feedback`.
- **Backfill de canal.** Sem cópia do legado, não há linha antiga para preencher.
- **Exportação CSV/JSON** do acervo.
- **DELETE ou UPDATE de parecer.** Reverter é registrar parecer novo com `revisado=false`.
- **Facet dinâmica de canal.** Conjunto fechado, dropdown fixo.
- **Corrigir o `msgId` do simulador para apontar a um `message.in`.**
- **Merge em `main`.** O wish termina em PR verde; merge é humano.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Grupo 0 é só cobertura, sem produção, e precede qualquer mudança de armazenamento | Risco 4 do design: remover a consulta de dentro de `_analyze_rating_async` sem repassar o resultado apagaria o degrau do meio das credenciais sem sintoma visível. Só um teste escrito **antes** detecta isso. |
| 2 | O teste PostgreSQL mora no Grupo 1, junto da migração | Ele é a única evidência que sustenta a Decisão 5f do design (toda FK declara `ondelete`); separá-lo da migração deixaria a decisão sem prova no mesmo commit. |
| 3 | O critério vira `tests/integration/test_rlhf_audit_postgres.py`, e não checklist manual | O repo já tem a trilha: `test_builder_cloning_postgres.py`, `test_builder_support_columns_postgres.py` e `test_interaction_origin_postgres.py` criam banco filho descartável, aplicam um arquivo de migração nomeado e afirmam contra Postgres real. |
| 4 | Os três grupos que tocam `src/api/routes/rlhf.py` (2, 3, 4) são estritamente sequenciais | Um agente Codex commita no mesmo branch em paralelo; grupos concorrentes no mesmo arquivo multiplicariam conflito sem ganho de tempo real. |
| 5 | O frontend de threading de tenant (Grupo 5) roda em paralelo com a migração (Grupo 1) | Conjuntos de arquivos disjuntos e nenhuma dependência de contrato: a prop de tenant não depende da API de RLHF. É o único paralelismo honesto do plano. |
| 6 | Nenhum passo condiciona-se a `git diff --quiet` | O Codex trabalha o mesmo branch; árvore suja é o estado normal, não sinal de trabalho pendente. |
| 7 | Regressão é provada por diferença de conjunto de testes falhando | A suíte backend já chega vermelha no branch; contagem bruta não distingue regressão nova de falha herdada. |
| 8 | A prova do degrau do meio é de **nível de endpoint**, não de chamada direta | Depois que o Grupo 2 tira a consulta de `_analyze_rating_async`, uma chamada direta só consegue afirmar "a função usou o evento que eu entreguei" — vacuidade. O modo de falha do Risco 4 é `submit_rating` esquecer de repassar o resultado, e só o endpoint vê isso. |
| 9 | Teste de endpoint é seguro desde que semeie e leia **pelo loop do cliente** | O deadlock de `tests/api/test_rlhf.py:1000-1009` vem de misturar o loop do teste com o do cliente, não de usar o cliente: `TestSubjectTenantIdDerivation` (`:291-380`) já semeia, submete e lê de volta inteiramente via `client._submit`/`client.post`. |
| 10 | `_update_rating_status` é item de primeira classe do Grupo 2, não consequência | Ele lê `events` por id e, ao não achar, só loga dentro de um `except` nu (`rlhf.py:326-345`). Depois da troca de armazenamento isso silencia a opinião da IA sem erro visível — e os testes atuais mockam a função (`test_rlhf.py:925, 970, 1056`), então nada acusaria. |
| 11 | Grupo 1 exige `POSTGRES_SMOKE_DATABASE_URL` em vez de aceitar skip | A lane PostgreSQL existente pula com código 0 quando a variável falta; sem exigi-la, os critérios de FK seriam tão vazios quanto em SQLite, só que por outro motivo. |
| 12 | O Grupo 2 é dono de **todo** leitor e escritor do armazenamento de nota; o Grupo 4 só ganha capacidade nova | O conjunto é fechado: escrita (`rlhf.py:413`), `list_ratings` (`:562`), `_update_rating_status` (`:316-345`), PATCH (`:776-781`), mais os helpers `_load_event` e `_seed_rating_event` (34 pontos de chamada). Deixar qualquer um para outra onda torna `test_super_admin_can_approve` (`tests/api/test_rlhf.py:451-487`) insatisfazível: com o seeder em `events` o GET portado não acha a nota; com o seeder portado o PATCH não portado responde 404. |
| 13 | A baseline de não-regressão é colhida no Grupo 0, não no Grupo 7 | Só é honesta se colhida antes de o wish tocar em qualquer coisa. Colhida no fim, já contém as falhas que o wish porventura introduziu. |

## Simplicity Case

- **Simplest complete design:** duas tabelas novas, uma migração sem cópia de dado, um SELECT
  na submissão que resolve canal e trace de uma vez, um endpoint novo de parecer, duas
  consultas em lote para o estado de revisão, dropdowns fixos e uma linha movida em
  `layout.tsx`.
- **Added machinery:**
  - Segunda tabela (`rlhf_audit_reviews`): paga pelo requisito de histórico de pareceres de
    vários humanos, escolhido pelo usuário sobre a alternativa de coluna única.
  - `channel` persistido: pago pelo filtro de canal pedido.
  - O SELECT de resolução sobe para o caminho síncrono de toda submissão — mas **não** vira
    consulta dupla, porque o evento resolvido é repassado a `_analyze_rating_async`.
  - `reviewed_count`: pago por "quanto falta revisar", que o filtro do passo 6 não responde.
  - `revisado_por_email`: pago por manter a autoria legível quando a FK vira NULL.
  - Grupo 0 encolheu para o que de fato falta: **um** teste de endpoint que afirma os kwargs da
    chamada de análise. Os degraus da cadeia de credenciais já têm cobertura de chamada direta
    em HEAD, então escrevê-los de novo seria trabalho nulo; o que não existe é a asserção no
    nível onde o Risco 4 se manifesta. O comentário no teste de `subject_tenant_id` é
    dispositivo de visibilidade de diff, não mitigação de risco.
- **Deferred until measured:** captura de nota pelo WhatsApp (quando existir decisão de produto
  sobre quando perguntar ao paciente); exportação do acervo (quando alguém precisar levar o
  dataset para fora do painel); facet dinâmica de canal (se surgir um terceiro canal); edição
  de parecer já registrado (se aparecer o caso real de corrigir texto).
- **Complexity removed:** nenhum backfill, nenhuma cópia, nenhuma deleção de dado, nenhum
  endpoint de DELETE, nenhum estado de rascunho de parecer, nenhuma facet de canal calculada no
  servidor, nenhum job de reprocessamento, nenhuma construção SQL específica de Postgres,
  nenhum índice sem consulta que o justifique.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [ ] `alembic upgrade head` cria as duas tabelas; `alembic downgrade <RLHF_DOWN_REVISION>` as remove sem tocar em `events`
- [ ] Contagem de linhas `rlhf.rating` em `events` é idêntica antes e depois da migração
- [ ] `DELETE /v1/admin/clinics/{id}` continua 200 em PostgreSQL real com notas e pareceres existentes
- [ ] Nota do detalhe de trace WhatsApp grava `channel='whatsapp'` e `trace_event_id` igual ao id do trace
- [ ] Nota do painel lateral do simulador (`msgId` de `srm.decision`) grava `channel='web'` e `trace_event_id IS NULL`
- [ ] Nota com `msgId` não-UUID grava `channel='web'`; com UUID órfão grava `channel='unknown'`
- [ ] Nota é persistida mesmo quando a resolução levanta exceção, com `channel='unknown'`
- [ ] Atribuição e credenciais usam a mesma cadeia de três degraus e coincidem nos três casos
- [ ] `_analyze_rating_async` não emite consulta própria por `message_id`
- [ ] O caminho de escrita nunca produz `trace_event_id` e `trace_tenant_id` com nulidade divergente
- [ ] `POST /v1/rlhf/rating` não cria linha nova em `events` com `event_type='rlhf.rating'` e devolve `rating_id`
- [ ] `channel` estreita o `summary` sem encolher as facets, e vale para chamador não-super
- [ ] `reviewed` filtra sem alterar `summary` nem facets; `reviewed_count` conta as revisadas dentre as notas com motivo qualificante
- [ ] A opinião da IA continua sendo gravada depois da troca de armazenamento, na linha de `rlhf_ratings`
- [ ] Parecer, autoria, histórico e `reviewed_count` não aparecem para `builder`, `clinic_admin` nem API key
- [ ] Parecer é append-only: segundo parecer com `revisado=false` desmarca e as duas linhas permanecem
- [ ] Listar N notas com pareceres dispara número de consultas independente de N
- [ ] `mask_carteirinha` e o isolamento por tenant sobrevivem à serialização nova
- [ ] PATCH preserva guard 422, exclusividade approve/ignore, `accepted_count` e carimbos
- [ ] Nota submetida de `/traces/{id}?tenant_id=X` com clínica Y selecionada grava `subject_tenant_id=X`
- [ ] `clinic_admin` com `?tenant_id=` forjado continua gravando o próprio tenant
- [ ] `/rlhf-audit` aparece em `navItems` sem badge e some de `roadmapItems`, seguindo invisível para `clinic_admin` e `builder`
- [ ] Diferença do conjunto de testes falhando contra a baseline do branch é vazia

## Execution Strategy

### Wave 1 (sequential — rede de segurança antes de qualquer mudança)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 0 | engineer | 2 — tarefa em background e resolução de credenciais por tenant (+2 stateful), com asserts determinísticos | `engineer-standard` / high | Confirmar por inventário a cobertura que já existe e instalar o tripwire de endpoint que falta — o único que sobrevive ao Grupo 2. |

### Wave 2 (parallel after Wave 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — schema e migração são estado durável (+2 stateful), com prova determinística em Postgres | `engineer-standard` / high | Criar as duas tabelas com `ondelete` explícito e provar que a exclusão de clínica continua 200. |
| 5 | engineer | 2 — recorte por papel no parâmetro da URL (+2 stateful de sessão/seleção), com testes determinísticos | `engineer-standard` / medium | Descer o tenant do trace como prop nas duas chamadas de `TraceViewer` e mover `/rlhf-audit` do Roadmap para a navegação. |

### Wave 3 (sequential — mesmo arquivo `src/api/routes/rlhf.py`)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 5 — troca de armazenamento atômica entre escrita e leitura, somada a ciclo de vida da tarefa de análise e a uma renomeação de campo de resposta (+2 stateful, +2 orquestração/lifecycle, +1 alcance de renomeação) | `engineer-complex` / high | Mover a nota para `rlhf_ratings` em escrita **e** leitura de uma vez, resolvendo canal, trace e atribuição, sem deixar round-trip vermelho para o Grupo 4. |

### Wave 4 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 2 — escrita append-only com autoria denormalizada (+2 stateful), acceptance mecânica | `engineer-standard` / high | Criar o endpoint de parecer humano com histórico e gate de super admin. |

### Wave 5 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 3 — contrato de 7 passos com filtros em degraus distintos (+2 stateful, +1 rework prévio no mesmo handler) | `engineer-standard` / high | Encaixar `channel` e `reviewed` nos passos certos e servir parecer só a super admin. |

### Wave 6 (sequential — consome o contrato do Grupo 4 e a URL do Grupo 5)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 6 | engineer | 3 — estados assíncronos de filtro, parecer e histórico (+2 stateful, +1 sem cobertura prévia de papel no painel) | `engineer-standard` / high | Entregar filtros, campo de observação, REVISADO, histórico, "Ver trace" e o recorte por papel no painel. |

### Wave 7 (gate agregado)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 7 | engineer | 1 — gate de CI/integração (+1 CI/release), sem implementação nova | `engineer-trivial` / low | Rodar a validação agregada e provar ausência de regressão por diferença de conjunto. |

O Grupo 0 é rede de segurança e não pode ser paralelizado com nada que toque armazenamento.
O paralelismo do Wave 2 é honesto porque Grupo 1 (backend/migração) e Grupo 5 (frontend de
trace) não compartilham arquivo nem contrato. Grupos 2, 3 e 4 editam o mesmo
`src/api/routes/rlhf.py` e ficam em série de propósito, já que um agente Codex commita no
mesmo branch em paralelo.

## Execution Groups

### Group 0: Fixar a cadeia de credenciais antes de mexer no armazenamento

**Goal:** Instalar o tripwire de nível de endpoint que **não existe hoje** e que sobrevive ao
Grupo 2, depois de confirmar por inventário o que a suíte já cobre. Sem alterar produção.

**A cobertura de chamada direta já existe.** Confirmar antes de escrever qualquer linha, porque
os critérios óbvios já passam em HEAD e um grupo cego se daria por pronto sem trabalho algum:

| Comportamento | Teste existente |
|---|---|
| Degrau do meio: `message_id` resolve o tenant das credenciais | `tests/api/test_rlhf.py:1136` `test_orphan_clinic_tenant_id_falls_through_to_message_id_lookup`, afirmando `seen["resolve_tenant_id"] == real_clinic_id` |
| Degrau do topo: `clinic_tenant_id` validado tem precedência | `:1062` |
| Degrau de baixo: cai no tenant de quem avaliou | `:1098` |
| Nota 4-5 não dispara análise | `:222` `test_rating_four_with_long_reason_does_not_trigger_analysis` |
| `subject_tenant_id` em dois degraus | `:323` `test_missing_clinic_tenant_id_falls_back_to_event_tenant` |

**Deliverables:**
1. Inventário verificado: rodar a suíte e confirmar que os cinco testes acima passam em HEAD.
   Se algum não existir mais, escrevê-lo. Nenhum deles é reescrito aqui.
2. **O teste novo — o único entregável real do grupo.** Um teste de endpoint que afirma os
   **kwargs** com que `submit_rating` chama `_analyze_rating_async`. Hoje nenhum teste faz
   isso: os testes de endpoint que dão patch na função (`:112`, `:138`, `:165`, `:188`, `:211`,
   `:227`) afirmam só `mock_analyze.called`. Hoje o kwarg relevante é `message_id` igual ao
   `msgId` submetido; no Grupo 2 ele vira o evento já resolvido. **É esse teste que pega o modo
   de falha do Risco 4** — `submit_rating` remover a consulta e esquecer de repassar o
   resultado —, porque depois do Grupo 2 uma chamada direta a `_analyze_rating_async` só pode
   afirmar "a função usou o evento que eu entreguei", o que é vacuidade.
3. Comentário no teste de `subject_tenant_id` (`:323`) marcando-o como ponto de atualização
   deliberada do Grupo 2.

**Acceptance Criteria:**
- [ ] `baseline-before.txt` existe, foi colhido **antes** de qualquer edição do wish, e lista o conjunto de testes que já falham no branch
- [ ] Existe teste de endpoint que afirma os kwargs passados a `_analyze_rating_async`, e não apenas que ela foi chamada
- [ ] Esse teste falha se `submit_rating` deixar de repassar o que a análise precisa para alcançar o tenant certo
- [ ] Os cinco testes do inventário passam sem terem sido reescritos
- [ ] Nenhum arquivo sob `src/` é modificado neste grupo

**Padrão obrigatório do teste novo:** tudo pelo loop do cliente —
`client._submit(_seed_tenant(...))` para semear, `client.post` para submeter,
`client._submit(_load_event(...))` para ler de volta, exatamente como
`TestSubjectTenantIdDerivation` (`tests/api/test_rlhf.py:291-380`) já faz, com
`_analyze_rating_async` sob `AsyncMock`. O deadlock descrito em `:1000-1009` vem de **misturar**
o loop do teste com o do cliente — que é o que a fixture `db_ready` faz ao inicializar o banco
no loop próprio —, não de usar o cliente. Não inicializar banco fora do cliente neste teste.

**Validation:**

```bash
# PRIMEIRO comando do grupo, antes de escrever qualquer linha: captura a baseline
# que o Grupo 7 vai usar como conjunto "antes". Nenhum outro grupo a produz, e ela
# so e defensavel se colhida aqui, antes de o wish tocar em qualquer coisa.
poetry run pytest -q 2>&1 | tee .genie/wishes/rlhf-auditoria-humana/baseline-before.txt; true
poetry run pytest tests/api/test_rlhf.py -v
poetry run black --check tests/api/test_rlhf.py
```

Escopo: teste focado sobre o comportamento que o Grupo 2 vai mover, mais a captura da baseline.
A suíte inteira roda aqui só para registrar o conjunto de falhas herdadas — o resultado não é
gate, por isso o `; true`. Não puxa gate completo de correção porque o grupo não altera runtime
algum. O critério "nenhum arquivo sob `src/`" é verificado
por inspeção do commit do próprio grupo, e **não** por `git diff` contra `origin/main`: o
Codex commita em `src/` no mesmo branch, então uma comparação de branch acusaria trabalho
alheio como violação.

**depends-on:** none

---

### Group 1: Tabelas, migração e prova de exclusão em PostgreSQL

**Goal:** Criar `rlhf_ratings` e `rlhf_audit_reviews` com integridade referencial explícita, sem
quebrar a exclusão de clínica.

**Deliverables:**
1. Modelos SQLAlchemy das duas tabelas com todas as colunas do design, incluindo o par
   `trace_event_id`/`trace_tenant_id`, `channel` com CHECK, `revisado_por` nullable e
   `revisado_por_email` NOT NULL.
2. `ondelete` explícito em toda FK: CASCADE em `tenant_id` e `subject_tenant_id`; SET NULL em
   `trace_event_id`, `trace_tenant_id`, `submitted_by`, `accepted_by` e `revisado_por`; CASCADE
   em `rlhf_audit_reviews.rating_id`; `session_id` sem FK.
3. Migração nova (confirmar `alembic heads` antes de escolher o número) com `upgrade` e
   `downgrade` completos, sem cópia nem remoção de dado. Anotar o `down_revision` escolhido
   como `RLHF_DOWN_REVISION` para a validação usar — a reversão é sempre por revisão explícita,
   nunca por `-1`.
4. Índices `(created_at DESC)`, `(tenant_id, created_at DESC)` em `rlhf_ratings` e
   `(rating_id, revisado_em DESC, id DESC)` em `rlhf_audit_reviews`.
5. `tests/integration/test_rlhf_audit_postgres.py` no formato dos testes PostgreSQL existentes:
   banco filho descartável, aplicação do arquivo de migração nomeado, e asserts contra Postgres
   real.

**Acceptance Criteria:**
- [ ] `alembic upgrade head` cria as duas tabelas e `alembic downgrade <RLHF_DOWN_REVISION>` as remove sem tocar em `events` — reversão por revisão explícita, nunca por `-1`
- [ ] Contagem de linhas `events WHERE event_type='rlhf.rating'` é idêntica antes e depois do `upgrade` e do `downgrade` — a migração não copia nem remove legado
- [ ] Toda FK das duas tabelas declara `ondelete`, verificável por inspeção do arquivo de migração
- [ ] Em PostgreSQL real, excluir um tenant que possui notas e pareceres remove as duas junto e **não** levanta violação de FK
- [ ] Em PostgreSQL real, excluir um tenant que aparece só como `trace_tenant_id` de uma nota de outra clínica anula o par e preserva a nota
- [ ] Os três índices existem após a migração

**Validation:**

```bash
test -n "$POSTGRES_SMOKE_DATABASE_URL" || { echo "ERRO: sem Postgres, os criterios de FK deste grupo nao sao provados"; exit 1; }
poetry run pytest tests/integration/test_rlhf_audit_postgres.py -v -rs 2>&1 | tee /tmp/rlhf-pg.txt
grep -qE "[0-9]+ skipped" /tmp/rlhf-pg.txt && { echo "ERRO: skip deixa os criterios de FK vazios"; exit 1; } || true
# NAO usar `downgrade -1`: o Codex commita migracoes neste branch e `-1` e relativo ao head do momento.
poetry run alembic upgrade head
poetry run alembic downgrade "$RLHF_DOWN_REVISION" && poetry run alembic upgrade head
poetry run pytest tests/api/test_rlhf.py -q
poetry run black --check src/models tests/integration/test_rlhf_audit_postgres.py
poetry run mypy src/models
```

Escopo: migração é estado durável e configuração de schema, então a validação sobe para
PostgreSQL real — a suíte padrão roda em SQLite, que ignora `ON DELETE` (nada em `src/` ou
`tests/` liga `PRAGMA foreign_keys`), e um critério em SQLite passaria enquanto produção
quebra.

Três armadilhas fecham aqui:

- **A lane PostgreSQL pula em silêncio.** Todos os testes existentes guardam com
  `pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")`
  (`tests/integration/test_interaction_origin_postgres.py:178-180` e irmãos), e um skip sai
  com código 0. Sem a variável, os três critérios de FK ficariam vazios — a mesma vacuidade do
  SQLite, só que mudada de lugar. Por isso a variável é exigida antes, e **qualquer skip
  derruba o passo**: exigir só a variável não basta se o teste novo ganhar uma segunda guarda.
- **O critério de `alembic upgrade`/`downgrade` não tinha comando.** Agora tem, com a revisão
  de origem **fixada em `RLHF_DOWN_REVISION`** em vez de `-1`. `-1` é relativo ao head do
  momento e o Codex commita migrações neste mesmo branch: se a dele entrar depois, `-1`
  reverteria o trabalho alheio e o teste afirmaria sobre a revisão errada. O passo volta a
  `head` porque o dev DB não roda alembic no boot e o drift aparece como 500 opaco.
- **`src/models/__init__.py` é estado global de mapper.** Uma `relationship` ou FK malformada
  quebra toda a suíte que toca banco, e nem `mypy` nem `black` pegam isso; por isso entra a
  suíte da rota RLHF como canário.

**depends-on:** Group 0

---

### Group 2: Troca de armazenamento, atômica entre escrita e leitura

**Goal:** Mover a nota de `events` para `rlhf_ratings` **sem deixar o repositório num estado em
que a escrita e a leitura discordam**, resolvendo canal, trace e atribuição na submissão.

**Este grupo é dono de TODO leitor e escritor do armazenamento de nota.** O conjunto é fechado
e cabe aqui inteiro — quatro em produção e dois helpers de teste:

| Consumidor | Onde |
|---|---|
| Escrita da nota | `src/api/routes/rlhf.py:413` |
| `list_ratings` passo 1 | `rlhf.py:562` (`Event.event_type == "rlhf.rating"`) |
| `_update_rating_status` | `rlhf.py:316-345` |
| **PATCH `/ratings/{id}`** | `rlhf.py:776-781`, mesmo `select(Event)` |
| Helper de leitura dos testes | `_load_event`, `tests/api/test_rlhf.py:282-288` |
| Helper de seed dos testes | `_seed_rating_event`, `:1306-1352`, insere linha `Event` — **34 pontos de chamada** |

Dividir esse conjunto entre ondas rasga o repositório. Concretamente: se o PATCH ficasse para
depois, `test_super_admin_can_approve` (`:451-487`) ficaria impossível de satisfazer nas duas
escolhas — mantendo `_seed_rating_event` em `events`, o GET já portado não acha a nota e
`assert matched` quebra; portando o seeder, o PATCH ainda em `events` responde 404 (`:783-787`).
Seis testes de `TestPatchRating`/`TestPatchRatingAcceptAudit` (`:459, 539, 581, 592, 603, 642`)
fazem seed → PATCH → GET.

Por isso o **porte inteiro** é deste grupo, e o Grupo 4 fica só com **capacidade que não
existia** (filtros `channel`/`reviewed`, parecer, `reviewed_count`).

**Deliverables:**
1. `POST /v1/rlhf/rating` grava em `rlhf_ratings` e deixa de criar linha `rlhf.rating` em
   `events`; a resposta devolve `rating_id`.
1b. `list_ratings` passa a ler `rlhf_ratings` no passo 1, preservando os 7 passos e o
   comportamento atual de isolamento — **sem** os filtros novos, que são do Grupo 4.
1c. **`_serialize_rating_event` (`rlhf.py:446-511`)** passa a emitir `channel`, `trace_event_id`
   e `trace_tenant_id`. O Grupo 6 consome os dois últimos para montar o "Ver trace": sem este
   item ele nasce bloqueado. **Não mexer em `RatingEntry` achando que é o contrato**: aquela
   classe está morta — só aparece na própria definição (`rlhf.py:59`), `@router.get("/ratings")`
   (`:515`) não declara `response_model`, e as entradas saem como dicts do serializador
   (chamada em `:756`, retorno em `:762`). Prova de que está defasada: não tem `accepted_by`/`accepted_at`/`accepted_by_email`,
   que o endpoint já devolve. Editar `RatingEntry` deixaria testes, `mypy` e `black` verdes com
   o corpo da resposta inalterado.
1d. `RatingResponse.event_id` (`rlhf.py:56`) vira `rating_id`, com as referências de teste
   atualizadas junto — a renomeação alcança muitos pontos e não pode ficar meio feita.
1e. `PATCH /v1/rlhf/ratings/{id}` portado para `rlhf_ratings` (`rlhf.py:776-781`), preservando
   guard 422, exclusividade approve/ignore, `accepted_count` e carimbos
   `accepted_by`/`accepted_at`.
1f. Helpers de teste portados juntos: `_load_event` vira `_load_rating`, e `_seed_rating_event`
   passa a inserir em `rlhf_ratings`. São 34 pontos de chamada; portar meio helper deixa a
   suíte incoerente.
2. Resolução `message_id → Event` no caminho síncrono, com os quatro casos da tabela do design:
   `message.in`, outro tipo de evento, não-UUID, UUID órfão.
3. `trace_event_id` e `trace_tenant_id` preenchidos juntos e apenas quando o evento resolvido é
   `message.in`.
4. `subject_tenant_id` com os três degraus: `clinic_tenant_id` validado > `Event.tenant_id`
   resolvido > tenant de quem avaliou.
5. Evento resolvido repassado para `_analyze_rating_async`, que deixa de consultar por conta
   própria; a cadeia de credenciais continua com os três degraus.
6. **`_update_rating_status` (`src/api/routes/rlhf.py:316-345`) reapontado para
   `rlhf_ratings`.** Hoje ele faz `select(Event).where(Event.id == event_id)` e, quando não
   acha, apenas loga `"Rating event vanished before analysis update"` e retorna — tudo dentro
   de um `except Exception` nu (`:339`). Sem este item, a nota deixa de ser `Event` no Grupo 2
   e a opinião da IA para de ser gravada **sem sintoma nenhum**: sem erro, sem 500, só um
   warning. É a capacidade que o usuário pediu para preservar ("a IA já dar uma prévia de
   opinião").
7. Resolução fail-open: exceção grava `channel='unknown'`, par de trace nulo, e a nota persiste.
8. Atualização deliberada do teste de atribuição do Grupo 0 para o comportamento de três
   degraus, com o motivo no corpo do teste.
9. Atualização do tripwire de endpoint do Grupo 0 para a forma nova: afirmar que
   `submit_rating` chama `_analyze_rating_async` com o **evento já resolvido**, e não com um
   `message_id` cru. O nível da asserção não muda; só o kwarg esperado.
10. Teste que fecha a cadeia de ponta a ponta: submissão sem `clinic_tenant_id` e com
    `message_id` que resolve deve levar `_load_runtime_llm_credentials` a receber o
    `Event.tenant_id`. As chamadas diretas do Grupo 0 podem continuar existindo para os degraus
    de topo e de baixo, mas **o degrau do meio passa a ser provado aqui**: depois que a consulta
    sai de `_analyze_rating_async`, uma chamada direta não consegue mais distinguir "resolveu
    certo" de "recebeu o que eu entreguei".

    **Os três patches são obrigatórios juntos**, como no único precedente da casa
    (`tests/api/test_rlhf.py:1032-1055`): `_load_runtime_llm_credentials` para capturar o
    tenant, `get_tenant_llm_client` para não chamar LLM de verdade, e `_update_rating_status`
    para não escrever. E a análise roda em `asyncio.create_task`: o teste precisa **aguardar a
    corrotina**, não torcer pelo agendamento. Sem as duas coisas o teste é flaky ou vazio — e
    ele é o único guarda restante do degrau do meio.

**Acceptance Criteria:**
- [ ] Nota do detalhe de trace WhatsApp grava `channel='whatsapp'` e `trace_event_id` igual ao id do trace
- [ ] Nota cujo `msgId` é um `srm.decision` grava `channel='web'` e `trace_event_id IS NULL`
- [ ] Nota com `msgId` não-UUID grava `channel='web'` **e `trace_event_id IS NULL`**; com UUID válido inexistente grava `channel='unknown'` e par de trace nulo
- [ ] Evento resolvido sem `channel` conhecido grava `channel='unknown'`
- [ ] Nota persiste com `channel='unknown'` quando a resolução levanta exceção
- [ ] Nota sem `clinic_tenant_id` cujo `message_id` resolve grava `subject_tenant_id = Event.tenant_id`, e a análise usa as credenciais do mesmo tenant
- [ ] Nota sem `clinic_tenant_id` e com `message_id` que não resolve grava o tenant de quem avaliou
- [ ] Nota do simulador com `clinic_tenant_id=effectiveTenantId` grava `subject_tenant_id` igual a ele **inclusive quando a resolução do `message_id` falha**
- [ ] `clinic_tenant_id` válido continua tendo precedência sobre o `message_id`
- [ ] `_analyze_rating_async` não emite consulta própria por `message_id`
- [ ] `trace_event_id` e `trace_tenant_id` nunca saem da escrita com nulidade divergente
- [ ] Nenhuma linha `rlhf.rating` nova aparece em `events`
- [ ] Nota 1-3 com motivo qualificante termina com `ai_analysis` e `ai_analysis_status='done'` gravados **na linha de `rlhf_ratings`**, verificado lendo a linha e não mockando `_update_rating_status`
- [ ] Falha na análise grava `ai_analysis_status='failed'` na linha de `rlhf_ratings`
- [ ] Nenhum caminho de análise ainda consulta `events` por `event_id`
- [ ] **Teste de endpoint prova o degrau do meio**: submissão sem `clinic_tenant_id` e com `message_id` que resolve faz o patch de `_load_runtime_llm_credentials` receber `Event.tenant_id`
- [ ] O tripwire de endpoint do Grupo 0 segue existindo, agora afirmando que o evento resolvido é repassado; nenhuma versão dele afirma apenas que a função foi chamada
- [ ] Nota com `clinic_tenant_id` válido cujo `message_id` resolve para um `message.in` de **outro** tenant grava `subject_tenant_id` = `clinic_tenant_id` **e** `trace_tenant_id` = `Event.tenant_id`, com a análise usando as credenciais de `clinic_tenant_id`
- [ ] Os testes do Grupo 0 sobre nota 4-5 e sobre a precedência do `clinic_tenant_id` seguem verdes
- [ ] **Toda `tests/api/test_rlhf.py` segue verde ao fim do grupo**: nenhum round-trip submeter→listar e nenhum dos 34 pontos de `_seed_rating_event` fica vermelho esperando um grupo posterior
- [ ] `TestPatchRating` e `TestPatchRatingAcceptAudit` passam com seed, PATCH e GET todos sobre `rlhf_ratings`
- [ ] PATCH mantém guard 422, exclusividade approve/ignore, `accepted_count` e carimbos
- [ ] Nenhum consumidor de armazenamento de nota ainda referencia `Event` — os quatro de produção e os dois helpers de teste foram portados juntos
- [ ] `GET /v1/rlhf/ratings` não consulta mais `events` por `event_type='rlhf.rating'`
- [ ] `entries[]` traz `channel`, `trace_event_id` e `trace_tenant_id`
- [ ] A renomeação `event_id` → `rating_id` está completa: nenhum teste ou schema ainda espera `event_id` na resposta do POST
- [ ] Isolamento por tenant do GET continua idêntico ao de hoje — não-super só vê as próprias notas

**Validation:**

```bash
poetry run pytest tests/api/test_rlhf.py -v
poetry run black --check src/api/routes/rlhf.py tests/api/test_rlhf.py
poetry run mypy src/api/routes/rlhf.py
```

Escopo: o grupo troca o armazenamento de **todos** os leitores e escritores de nota e mexe no
ciclo de vida da tarefa de análise, então além do arquivo de teste inteiro da rota entram type
check e format check. `tests/api/test_rlhf.py` precisa fechar verde por completo aqui — é o
sinal de que o porte não ficou pela metade. Não puxa o gate completo porque o alcance segue
sendo um arquivo de rota e seu teste.

**depends-on:** Group 1

---

### Group 3: Endpoint de parecer humano

**Goal:** Permitir que o master registre parecer sobre uma nota, com histórico append-only e
autoria que sobrevive à exclusão do usuário.

**Deliverables:**
1. `POST /v1/rlhf/ratings/{id}/review` com `require_super_admin`, gravando `observacao`
   (opcional), `revisado`, `revisado_por`, `revisado_por_email` e `revisado_em`.
2. `revisado_por_email` resolvido por `SELECT` em `users` a partir de `tenant.user_id`.
3. Sem DELETE e sem UPDATE: cada chamada insere uma linha nova.
4. Testes em `tests/api/test_rlhf_review.py`.

**Acceptance Criteria:**
- [ ] Parecer com observação vazia marca a nota como revisada
- [ ] Segundo parecer com `revisado=false` devolve a nota para não revisada, e as duas linhas permanecem na tabela
- [ ] Dois pareceres com o mesmo `revisado_em` produzem estado corrente idêntico entre requests repetidos
- [ ] Responde 403 para `builder`, `clinic_admin` e `investor`
- [ ] `revisado_por_email` é gravado a partir da linha em `users` e continua legível quando `revisado_por` é anulado
- [ ] Parecer sobre `rating_id` inexistente responde 404

**Validation:**

```bash
poetry run pytest tests/api/test_rlhf_review.py -v
poetry run black --check src/api/routes/rlhf.py tests/api/test_rlhf_review.py
poetry run mypy src/api/routes/rlhf.py
```

Escopo: endpoint novo com escrita em tabela de auditoria; teste focado mais format e type check
do arquivo tocado. Sem gate completo porque não altera contrato existente.

**depends-on:** Group 2

---

### Group 4: Filtros novos e parecer no relatório

**Goal:** Acrescentar ao relatório — já inteiramente sobre `rlhf_ratings` desde o Grupo 2 — os
filtros `channel` e `reviewed` nos degraus corretos e a serialização do parecer, sem distorcer
métrica nem facets.

**Nenhum porte é deste grupo.** O Grupo 2 moveu todos os leitores e escritores de uma vez.
Aqui entra só capacidade que não existia, preservando a ordem de execução dos 7 passos.

**Deliverables:**
1. `channel` estreita no passo 4, para **todos** os papéis, incluindo os que hoje pulam o
   estreitamento por escopo.
2. `reviewed` filtra no passo 6, exclusivo de `super_admin`.
3. `summary.reviewed_count` calculado no passo 5, contando as notas **revisadas** dentre as que
   têm motivo qualificante — mesmo denominador de `accepted_count` (`rlhf.py:709-715`), para
   que "quanto falta revisar" não seja inflado por notas 4-5 que ninguém pode revisar.
4. Estado do parecer lido em lote com `ORDER BY rating_id, revisado_em DESC, id DESC` e redução
   em Python — sem `DISTINCT ON` e sem função de janela.
5. Parecer, autoria, histórico e `reviewed_count` serializados apenas para `super_admin`.
6. `mask_carteirinha` e o isolamento por tenant preservados na serialização ampliada.

**Acceptance Criteria:**
- [ ] `channel=whatsapp` estreita `summary` mas mantém `facets.builders` e `facets.clinics` idênticas à consulta sem filtro
- [ ] `channel=unknown` devolve as notas não classificadas
- [ ] Chamador `builder` ou `clinic_admin` com notas em mais de um canal recebe o recorte de `channel` aplicado
- [ ] O mesmo chamador continua com `tenant_id`, `builder_id` e `reviewed` ignorados
- [ ] `reviewed=false` devolve notas sem parecer ou com parecer mais recente `revisado=false`, sem alterar `summary` nem facets
- [ ] `reviewed_count` conta as notas **revisadas** entre as que têm motivo qualificante, ignorando as demais mesmo quando elas estão no cut
- [ ] Parecer sobre uma nota sem motivo não altera `reviewed_count`, e revisar todas as notas exibíveis leva `reviewed_count` ao total exibível
- [ ] `entries[].id` é o `rlhf_ratings.id`, e o painel usa esse id para registrar parecer e para aceitar/ignorar
- [ ] `GET` por `builder`, `clinic_admin` ou API key não devolve `observacao`, autoria, histórico nem `reviewed_count`
- [ ] Listar N notas com pareceres dispara número de consultas independente de N
- [ ] Não-super só enxerga as próprias notas
- [ ] `reason`, `agent_response` e `user_message` saem com carteirinha mascarada
- [ ] O PATCH portado no Grupo 2 segue verde depois da serialização ampliada
- [ ] Nenhuma consulta usa `DISTINCT ON` ou função de janela

**Validation:**

```bash
poetry run pytest tests/api/test_rlhf.py tests/api/test_rlhf_review.py -v
poetry run black --check src/api/routes/rlhf.py tests/api/test_rlhf.py
poetry run mypy src/api/routes/rlhf.py
```

Escopo: o handler é o contrato que o painel consome e carrega isolamento de tenant e
mascaramento de PII, então a validação cobre os dois arquivos de teste da rota mais type e
format check. Não puxa gate completo porque o alcance segue sendo a rota RLHF.

**depends-on:** Group 3

---

### Group 5: Tenant do trace como prop e saída do Roadmap

**Goal:** Fazer o trace ter uma única fonte de verdade sobre a clínica dele, e promover
`/rlhf-audit` de item de Roadmap para item de navegação.

**Deliverables:**
1. `trace-detail.tsx` resolve o tenant a partir do parâmetro da URL, honrado só quando
   `(isBuilder || isSuperAdmin)`, com `viewingClinicId` como fallback, e usa esse valor tanto no
   fetch quanto na prop.
2. `TraceViewer` recebe o tenant por prop e deixa de ler `viewingClinicId` do `useAuth()`.
3. `chat.tsx:629` passa `effectiveTenantId` na mesma prop.
4. `RatingBlock` recebe `clinicTenantId` a partir dessa prop nas duas superfícies.
5. `/rlhf-audit` sai de `roadmapItems` e entra em `navItems` em `layout.tsx`, sem badge de
   versão e preservando `hideFor: ["clinic_admin", "builder"]`.
6. Testes em `trace-viewer.test.tsx`, `layout.test.tsx` e um `trace-detail.test.tsx` novo.

**Acceptance Criteria:**
- [ ] `/traces/:id?tenant_id=X` carrega o trace do tenant X mesmo com outra clínica selecionada no topo
- [ ] Nota submetida de dentro desse trace envia `clinic_tenant_id=X`, e não a clínica selecionada
- [ ] `clinic_admin` abrindo `/traces/{id-próprio}?tenant_id=<outra>` continua carregando o próprio trace e envia o próprio tenant
- [ ] Nota submetida pelo painel lateral do simulador envia `clinic_tenant_id=effectiveTenantId`
- [ ] `TraceViewer` não referencia mais `viewingClinicId`
- [ ] `/rlhf-audit` aparece em `navItems` sem badge e não aparece em `roadmapItems`
- [ ] `/rlhf-audit` segue invisível para `clinic_admin` e `builder`

**Validation:**

```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/components/trace-viewer.test.tsx src/components/layout.test.tsx src/components/rating-block.test.tsx src/pages/trace-detail.test.tsx
pnpm --dir frontend/artifacts/neurostack run typecheck
```

Escopo: mudança de assinatura de componente atinge duas páginas, então entra type check junto
dos testes focados — é o check que pega uma das duas chamadas esquecida. Sem build completo
porque nenhuma rota ou dependência muda.

**depends-on:** Group 0

---

### Group 6: Painel de auditoria com filtros, parecer e link de trace

**Goal:** Entregar no painel os filtros de canal e revisão, o parecer humano com histórico, o
botão "Ver trace" e o recorte por papel.

**Deliverables:**
1. Dropdown de canal fixo (Todos / Chat / WhatsApp / Não classificado) e dropdown Revisado
   (Todos / Revisados / Não revisados).
2. Campo de observação e botão REVISADO em cada feedback escrito, com histórico de pareceres
   (autor, data, observação).
3. Botão "Ver trace" quando há `trace_event_id`, navegando para
   `/traces/{trace_event_id}?tenant_id={trace_tenant_id}`; texto explicativo no lugar do botão
   quando não há.
4. KPI `reviewed_count` no painel.
5. Recorte por papel: dropdowns de builder e clínica ocultos fora do super admin; para
   `investor`, ocultos também o dropdown Revisado, o KPI `reviewed_count`, a observação, o
   REVISADO, o aceitar/ignorar e o "Ver trace".
6. Strings PT/EN nos dois arquivos de locale.
7. Testes em `rlhf-audit.test.tsx`.

**Acceptance Criteria:**
- [ ] Filtro de canal envia `channel` e reflete o recorte no KPI, sem encolher os dropdowns de builder e clínica
- [ ] Filtro Revisado envia `reviewed` e não altera os KPIs de distribuição
- [ ] Entrada com trace mostra "Ver trace" apontando para `/traces/{trace_event_id}?tenant_id={trace_tenant_id}`
- [ ] Entrada sem trace mostra a explicação no lugar do botão
- [ ] Registrar parecer atualiza o estado de revisão da entrada e acrescenta linha ao histórico
- [ ] Marcar REVISADO sem escrever observação é aceito
- [ ] `investor` não vê dropdown Revisado, KPI `reviewed_count`, observação, REVISADO, aceitar/ignorar nem "Ver trace"
- [ ] Dropdowns de builder e clínica não são renderizados fora do super admin
- [ ] Nenhuma string nova fica fora de PT e EN

**Validation:**

```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/rlhf-audit.test.tsx
pnpm --dir frontend/artifacts/neurostack run typecheck
pnpm --dir frontend/artifacts/neurostack run build
```

Escopo: o painel consome contrato de API novo e ganha recorte por papel, então além do teste
focado e do type check entra o build — é a página com mais superfície de render condicional do
wish e o build pega quebra de importação ou de locale.

**depends-on:** Group 4, Group 5

---

### Group 7: Gate agregado e prova de não-regressão

**Goal:** Provar que o wish inteiro fecha sem regressão contra a baseline vermelha do branch.

**Deliverables:**
1. Execução da suíte backend completa e comparação **por conjunto** com
   `baseline-before.txt`, colhido pelo Grupo 0 antes de o wish tocar em qualquer coisa. Se o
   arquivo não existir, a não-regressão não é demonstrável e o grupo para.
2. Execução da suíte frontend completa mais typecheck e build.
3. Registro dos dois conjuntos de falhas no PR, não apenas a contagem.

**Acceptance Criteria:**
- [ ] Diferença do conjunto de testes falhando (depois menos antes) é vazia
- [ ] Suíte frontend, typecheck e build passam
- [ ] Nenhuma falha nova é justificada por "já estava vermelho" sem constar da baseline registrada

**Validation:**

```bash
test -f .genie/wishes/rlhf-auditoria-humana/baseline-before.txt || { echo "ERRO: baseline do Grupo 0 ausente, a nao-regressao nao e provavel"; exit 1; }
poetry run pytest -q 2>&1 | tee .genie/wishes/rlhf-auditoria-humana/baseline-after.txt; true
diff <(grep -oE "^FAILED [^ ]+" .genie/wishes/rlhf-auditoria-humana/baseline-before.txt | sort -u) \
     <(grep -oE "^FAILED [^ ]+" .genie/wishes/rlhf-auditoria-humana/baseline-after.txt | sort -u)
pnpm --dir frontend/artifacts/neurostack run test
pnpm --dir frontend/artifacts/neurostack run typecheck
pnpm --dir frontend/artifacts/neurostack run build
```

Escopo: gate agregado do repositório, obrigatório porque o wish altera schema, contrato de API
e navegação. A comparação é por conjunto porque a suíte backend já chega vermelha no branch e
contagem bruta não distingue regressão nova de falha herdada.

**depends-on:** Group 6

---

## QA Criteria

_O que verificar no dev depois do merge._

- [ ] Abrir `/rlhf-audit` como master: KPIs, gráfico colorido e lista carregam sem 500
- [ ] Filtrar por canal WhatsApp muda a média e o total, mas não some com clínicas do dropdown
- [ ] Filtrar por "Não revisados", registrar um parecer e ver a entrada sair da lista
- [ ] Clicar "Ver trace" numa nota de outra clínica com uma clínica diferente selecionada no topo abre o trace certo
- [ ] Avaliar dentro desse trace e conferir, no painel, que a nota caiu na clínica do trace
- [ ] Nota do simulador continua sem botão "Ver trace", com a explicação no lugar
- [ ] `/rlhf-audit` aparece na navegação principal e não aparece mais no Roadmap
- [ ] Login como `clinic_admin` e `builder`: `/rlhf-audit` continua invisível
- [ ] Avaliar uma resposta no simulador e conferir que a nota aparece no painel com canal Chat

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Remover a consulta de dentro de `_analyze_rating_async` sem repassar o evento apaga o degrau do meio das credenciais sem sintoma visível | High | Grupo 0 escreve a cobertura **antes** do Grupo 2 mexer no armazenamento; o Grupo 2 só é dado por pronto com esses testes verdes. |
| A opinião da IA para de ser gravada porque `_update_rating_status` continua procurando um `Event` | High | Deliverable 6 e três critérios no Grupo 2, com o teste lendo a linha de `rlhf_ratings` em vez de mockar a função — o mock atual (`test_rlhf.py:925, 970, 1056`) é justamente o que esconderia a perda. |
| Isolamento por tenant e mascaramento de PII se perdem na serialização nova | High | Critérios explícitos no Grupo 4, não deixados para o gate final. |
| O painel zera na virada: notas antigas não são copiadas | Medium | Verificado no dev (12 notas, 0 aceitas, nada posterior a 2026-08-02). **Produção não verificada**: contar `events WHERE event_type='rlhf.rating'` em produção antes do deploy e reabrir a decisão se houver opinião aceita. |
| Dev DB atrasa em relação às migrações; a nova não roda sozinha no boot | Medium | `alembic upgrade head` como primeiro passo de validação; drift aparece como 500 opaco, então conferir `alembic_version` antes de culpar a rota. |
| SQLite não aplica `ON DELETE`, então a cascata passa na suíte e quebra em produção | Medium | A prova de integridade referencial mora no teste PostgreSQL do Grupo 1; os demais critérios afirmam invariantes do caminho de escrita, não de FK. |
| O número da migração é alvo móvel: o head `019` ainda está sem commit na árvore | Low | Reconferir `alembic heads` no início do Grupo 1 em vez de confiar no número escrito aqui. |
| Codex trabalha o mesmo branch em paralelo | Low | Nunca condicionar passo a `git diff --quiet`; grupos que tocam `rlhf.py` ficam em série; baseline de teste é piso, não contagem. |
| Suíte de backend já chega vermelha no repositório | Medium | Grupo 7 prova ausência de regressão por diferença de conjunto, com os dois conjuntos registrados no PR. |
| Uma quarta superfície de captura muda a regra de resolução | Low | A regra depende do tipo do evento resolvido, não de adivinhar a superfície; os quatro casos ficam fixados por teste no Grupo 2. |

**Assumptions**

- Um parecer por clique é suficiente; ninguém precisa editar parecer já registrado.
- `investor` mantém a leitura das métricas, mas não do parecer.
- O `msgId` do simulador continua sendo o id do `srm.decision`; se virar o id do `message.in`,
  a superfície 2 ganha "Ver trace" legitimamente, sem quebrar nada.

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan review — 2026-09-06 — **SHIP**

- **Reviewer:** `ae71398f064838c7c` (independent, read-only)
- **Rodadas:** 6 (FIX-FIRST ×5 → SHIP), severidade decrescente: 2 HIGH → 1 HIGH → 2 HIGH → 1 HIGH → 1 MEDIUM → 0

**Achados fechados ao longo das rodadas**

| # | Severidade | Achado | Correção persistida |
|---|---|---|---|
| 1 | HIGH | `_update_rating_status` (`rlhf.py:316-345`) nunca era reapontado; após a virada o parecer da IA pararia de ser gravado em silêncio, dentro de um `except` nu (`:339`) | Entregável 6 do Grupo 2 + 3 critérios que exigem **ler a linha**, nunca mockar o escritor; Decisão 10; risco alto registrado |
| 2 | HIGH | Fixação do degrau do meio de `subject_tenant_id` por chamada direta é insatisfazível depois que o Grupo 2 remove a consulta de `_analyze_rating_async` | Grupo 0 vira tripwire de **kwargs em nível de endpoint**; Decisões 8 e 9 reescritas — o deadlock vem de *misturar* loops, não de usar o client |
| 3 | MEDIUM | Critérios do Grupo 0 já verdes no HEAD — o grupo fecharia sem escrever nada | Grupo 0 reformulado como *verificar-e-estender*, com tabela de inventário dos cinco testes existentes |
| 4 | MEDIUM | `alembic downgrade -1` é relativo ao head do momento e o Codex commita migrações neste branch | `RLHF_DOWN_REVISION` explícito, registrado como entregável |
| 5 | MEDIUM | Trilha Postgres passava verde por `skip` | Env var exigida + qualquer `skipped` derruba o passo |
| 6 | HIGH | Troca de armazenamento dividida entre ondas: o Grupo 2 parava de escrever enquanto `list_ratings` ainda lia `events` até o Grupo 4 | Grupo 2 passa a possuir também a porta de leitura |
| 7 | HIGH | `trace_event_id` / `trace_tenant_id` consumidos pelo Grupo 6 sem grupo que os criasse | Dobrados no Grupo 2 |
| 8 | HIGH | PATCH deixado no Grupo 4 tornaria `test_super_admin_can_approve` insatisfazível sob qualquer escolha de seeder | PATCH + os dois helpers de teste movidos para o Grupo 2; Grupo 4 reduzido a capacidade nova; Decisão 12 reescrita com a prova de rasgo |
| 9 | MEDIUM | Entregável 1c nomeava `RatingEntry`, schema morto (`grep -n "RatingEntry"` → só `rlhf.py:59`, a própria definição) | Repontado para `_serialize_rating_event` (`:446-511`), com a evidência de obsolescência inline |
| 10 | LOW | 35 → 34 pontos de chamada de `_seed_rating_event`; numeração do Grupo 4 com lacuna | Corrigido nos quatro lugares; entregáveis do Grupo 4 renumerados 1-6 |

**Julgamento explícito do revisor sobre as cinco rodadas FIX-FIRST:** não indicam decomposição quebrada. As rodadas 1-2 acharam defeitos independentes que ficaram corrigidos; as rodadas 3-4 compartilhavam uma única causa raiz — o conjunto de consumidores do armazenamento da nota é fechado (quatro sítios de produção + dois helpers de teste) e distribuí-lo entre ondas rasga o repositório. Mover a fronteira uma vez, por inteiro, resolveu.

**Âncoras conferidas contra a fonte viva:** escrita `:413`, passo 1 do `list_ratings` `:562`, PATCH `:776-781`, `_update_rating_status` `:316` com `except` nu em `:339`, docstring de 7 passos `:540-556`, salto do passo 4 para não-super `:679-683`, `accepted_count` `:709-715`.

**Nit não bloqueante aceito:** a citação `(:763)` para "as entradas saem como dicts do serializador" aponta a chamada em `:756` e o retorno em `:762`.

### Execução — Group 0 — 2026-09-07 — **SHIP**

- **Engineer:** `ac753f75d2e1dfe9a` (`genie:engineer-standard`)
- **Reviewer:** `a98da6766b34a43d9` (`genie:reviewer`, independente do engineer) — SHIP, 5/5 critérios com evidência
- **Validação do orquestrador (re-executada, não apenas conferida):**
  - `poetry run pytest tests/api/test_rlhf.py -v` → `54 passed, 3 warnings in 6.19s`
  - `poetry run black --check tests/api/test_rlhf.py` → `All done! 1 file would be left unchanged.`
- **Entregáveis:** `TestSubmitRatingAnalysisPassthrough::test_submit_rating_forwards_message_id_kwarg_to_analysis` (`tests/api/test_rlhf.py:260-306`) afirmando `kwargs.get("message_id")`, não apenas `.called`; comentário de marcação no teste de `subject_tenant_id` (`:372`); `baseline-before.txt` colhido antes de qualquer edição (`2 failed, 2628 passed, 14 skipped` — as duas falhas são de `test_knowledge_document_metadata.py`, sem relação com RLHF).
- **Nenhum arquivo sob `src/` modificado por este grupo** — os diffs em `src/` no branch compartilhado são do agente Codex em paralelo e não tocam o call site de `_analyze_rating_async` do qual o teste novo depende.
- **Task:** `t_mtqfubv00eec1ec6` → `done`

### Execução — Group 1 — 2026-09-07 — **SHIP** (1 ciclo de correção)

- **Engineer:** `genie:engineer-standard` (tabelas, migração `020`, `tests/integration/test_rlhf_audit_postgres.py`)
- **Reviewer (1ª rodada) — FIX-FIRST:** achado confirmado em `alembic/versions/20260907_000001_020_rlhf_audit_tables.py`: uso de `sa.dialects.postgresql.UUID(as_uuid=True)` / `sa.dialects.postgresql.JSONB()` via `import sqlalchemy as sa` puro (11 + 1 sítios), que só resolve porque `alembic/env.py` importa `src.models` antes de rodar `upgrade()` — reproduzido o `AttributeError` sem esse efeito colateral. Convenção do repositório é o import explícito (`alembic/versions/20260501_000001_006_add_api_credentials.py:14`).
- **Fixer:** adicionou `from sqlalchemy.dialects.postgresql import JSONB, UUID` e trocou os 12 sítios para `UUID(...)`/`JSONB()`; nenhuma outra linha tocada (colunas, constraints, índices e `downgrade()` idênticos).
- **Reviewer (2ª rodada, agente independente do fixer) — SHIP:** confirmou zero ocorrências residuais de `sa.dialects.postgresql`; reproduziu import isolado do módulo de migração sem `src.models` pré-carregado (removendo `sqlalchemy.dialects.postgresql` de `sys.modules` antes de importar) sem `AttributeError`; reexecutou a suíte Postgres (12 passed, 0 skipped); confirmou `src/models/rlhf_rating.py` e `src/models/rlhf_audit_review.py` fora do escopo da correção.
- **Validação do orquestrador (re-executada de ponta a ponta, banco dev real em `localhost:5434/neurostack`):**
  - `POSTGRES_SMOKE_DATABASE_URL=...localhost:5434/postgres RLHF_DOWN_REVISION=019 poetry run pytest tests/integration/test_rlhf_audit_postgres.py -v -rs` → `12 passed, 3 warnings`, sem `skipped`
  - `poetry run alembic upgrade head` → já em `020 (head)`
  - `poetry run alembic downgrade 019` → `Running downgrade 020 -> 019` (sucesso, revisão explícita, nunca `-1`)
  - `poetry run alembic upgrade head` → `Running upgrade 019 -> 020` (sucesso); `alembic current` confirma `020 (head)`
  - `poetry run pytest tests/api/test_rlhf.py -q` → `54 passed, 3 warnings`
  - `poetry run black --check src/models tests/integration/test_rlhf_audit_postgres.py` → `All done! 19 files would be left unchanged.`
  - `poetry run mypy src/models` → `Success: no issues found in 18 source files`
- **Entregáveis:** `src/models/rlhf_rating.py`, `src/models/rlhf_audit_review.py`, `src/models/__init__.py` (registro dos dois novos modelos), `alembic/versions/20260907_000001_020_rlhf_audit_tables.py` (`RLHF_DOWN_REVISION=019`), `tests/integration/test_rlhf_audit_postgres.py`.
- **Task:** `genie task` indisponível nesta sessão (`.genie/genie.db` malformado, incompatibilidade WSL2/DrvFs suspeitada — reparo tentado e abandonado por ser infraestrutural, não bloqueante conforme a skill `work`); grupo rastreado só por este bloco.

### Execução — Group 2 — 2026-09-07 — **SHIP**

- **Engineer:** `genie:engineer-complex` / opus (troca de armazenamento nos 6 consumidores fechados: escrita, `list_ratings` passo 1, `_update_rating_status`, PATCH, e os dois helpers de teste `_load_rating`/`_seed_rating_event`). O agente atingiu o limite de sessão antes de reportar de volta — sem auto-relato do engineer, então o orquestrador e o revisor verificaram o trabalho de forma independente, sem confiar em nenhuma alegação do engineer.
- **Reviewer (independente do engineer):** `genie:reviewer` / opus — **SHIP**. Verificou, lendo o código e os testes linha a linha (não apenas os nomes), todos os 25 critérios de aceitação, incluindo os cinco formatos de par canal/trace, a exclusão do `RatingEntry` morto, a renomeação completa `event_id`→`rating_id`, e isolamento de tenant inalterado. Verificação aprofundada do item de maior risco (degrau do meio): `TestSubmitRatingMiddleRungAttribution::test_message_id_resolution_pins_credential_tenant_end_to_end` (`tests/api/test_rlhf.py:799-916`) confirmado como teste de ponta a ponta via `client.post` real, com os três patches exigidos (`_load_runtime_llm_credentials`, `get_tenant_llm_client`, `_update_rating_status`) mais um quarto para aguardar a `create_task` deterministicamente, e asserção nas duas direções (`seen["resolve_tenant_id"] == original_event_owner` **e** `!= TEST_TENANT_ID`) — mais rigoroso que o mínimo pedido.
- **Achados não bloqueantes (informativos, não afetam o veredito):** comentário obsoleto `# PATCH /v1/rlhf/ratings/{event_id}` em `tests/api/test_rlhf.py:1123` (parâmetro real é `{rating_id}`); nota explicativa sobre `RatingEntry` morto vive no docstring do serializer (`rlhf.py:556-559`) em vez de na própria classe — ambos cosméticos, correção barata para depois.
- **Validação do orquestrador (re-executada, independente do relato do engineer):**
  - `poetry run pytest tests/api/test_rlhf.py -v` → `68 passed, 3 warnings`
  - `poetry run black --check src/api/routes/rlhf.py tests/api/test_rlhf.py` → `All done! 2 files would be left unchanged.`
  - `poetry run mypy src/api/routes/rlhf.py` → `Success: no issues found in 1 source file`
- **Entregáveis:** `src/api/routes/rlhf.py`, `tests/api/test_rlhf.py` — porte completo dos 4 consumidores de produção e dos 2 helpers de teste (34 pontos de chamada) para `rlhf_ratings`; resolução de canal/trace/`subject_tenant_id` em 3 degraus; `_update_rating_status` e `_analyze_rating_async` repontados; `event_id`→`rating_id` na resposta do POST; `RatingEntry` deixado como código morto documentado.
- **Task:** sem rastreio via `genie task` (mesmo motivo do Grupo 1); grupo rastreado só por este bloco.

### Execução — Group 5 — 2026-09-07 — **SHIP**

- **Engineer:** `genie:engineer-standard` (prop `clinicTenantId` em `TraceViewer`/`RatingBlock`, resolução via `?tenant_id=` em `trace-detail.tsx`, promoção `/rlhf-audit` de `roadmapItems` para `navItems`).
- **Reviewer (independente do engineer):** `genie:reviewer` — **SHIP**. Confirmou remoção total da leitura interna de `viewingClinicId` via `useAuth()` em `trace-viewer.tsx` (zero ocorrências residuais por grep), guarda `isBuilder || isSuperAdmin` antes de honrar o parâmetro de URL em `trace-detail.tsx` (clinic_admin sempre cai para `viewingClinicId`, nunca lê `?tenant_id=`), preservação do guard `isInvestor` que oculta `RatingBlock` por completo, e a promoção do item de navegação sem badge de versão restrita a `super_admin`/`investor`. Achado não bloqueante: a alegação do brief de dispatch de que `rating-block.test.tsx` já cobria o encaminhamento de `clinicTenantId` para o corpo da requisição estava incorreta — esse arquivo cobre apenas o limiar de 8 caracteres do motivo, não o efeito da prop na requisição; fora do escopo de arquivos de teste declarado no grupo, não afeta o veredito.
- **Validação do orquestrador (re-executada, não apenas conferida):**
  - `pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/components/trace-viewer.test.tsx src/components/layout.test.tsx src/components/rating-block.test.tsx src/pages/trace-detail.test.tsx` → `49 passed`
  - `pnpm --dir frontend/artifacts/neurostack run typecheck` → limpo, sem erros
- **Entregáveis:** `frontend/artifacts/neurostack/src/pages/trace-detail.tsx` (resolução de `resolvedTenantId` via `useSearch`), `src/components/trace-viewer.tsx` (prop `clinicTenantId?: string | null`), `src/pages/chat.tsx` (encaminha `effectiveTenantId ?? null`), `src/components/layout.tsx` (`/rlhf-audit` movido de `roadmapItems` para `navItems`), `src/components/trace-viewer.test.tsx`, `src/components/layout.test.tsx`, `src/pages/trace-detail.test.tsx` (novo).
- **Task:** sem rastreio via `genie task` (mesmo motivo do Grupo 1); grupo rastreado só por este bloco.

### Execução — Group 3 — 2026-09-07 — **SHIP**

- **Engineer:** `genie:engineer-standard` / opus (endpoint `POST /v1/rlhf/ratings/{id}/review`, schema `RatingReviewRequest`, `tests/api/test_rlhf_review.py`). O agente atingiu o limite de sessão antes de reportar de volta (interrompido em plena depuração de um problema de coerção de UUID no SQLite de teste) — sem auto-relato do engineer, então o orquestrador e o revisor verificaram o trabalho de forma independente a partir do diff.
- **Reviewer (independente do engineer):** `genie:reviewer` / opus — **SHIP**. Confirmou os 6 critérios de aceitação com evidência de linha: `observacao` ausente/nulo grava com sucesso (`rlhf.py:91`, `:948`); segundo parecer com `revisado=false` insere linha nova em vez de UPDATE (só `session.add`+`commit` em `rlhf.py:954-955`, nenhum `update()`/`delete()` contra `RlhfAuditReview` no arquivo inteiro) e ambas as linhas sobrevivem (`test_rlhf_review.py:352,360`); tie-break determinístico por `id DESC` quando `revisado_em` empata, testado com dois `id`s ordenados propositalmente (`test_rlhf_review.py:369-430`); 403 para `builder`, `clinic_admin` **e** `investor` via teste parametrizado (`:204-207`), com prova de que o gate de auth roda antes do lookup 404 (UUID inexistente ainda 403 para papel errado); `revisado_por_email` resolvido por `SELECT User.email` real (`rlhf.py:938-944`, não lido de claim do JWT) e comprovadamente sobrevive a `revisado_por` anulado via UPDATE direto seguido de releitura em sessão nova (`test_rlhf_review.py:438-480`); 404 para `rating_id` inexistente (`rlhf.py:924-930`). Checagem de escopo: diff do arquivo é só 1 import + 1 schema + 1 endpoint nesta rodada — `list_ratings`, `_analyze_rating_async`, `_update_rating_status` e o PATCH seguem bit-a-bit idênticos ao Grupo 2; nenhum endpoint GET/histórico foi criado (reservado ao Grupo 4/6).
- **Validação do orquestrador (re-executada, independente do relato do engineer):**
  - `poetry run pytest tests/api/test_rlhf_review.py tests/api/test_rlhf.py -q` → `77 passed, 3 warnings` (0 regressão nos 68 testes pré-existentes do Grupo 2)
  - `poetry run black --check src/api/routes/rlhf.py tests/api/test_rlhf_review.py` → 1 arquivo precisou de reformatação (`black` aplicado, depois `--check` limpo em ambos)
  - `poetry run mypy src/api/routes/rlhf.py` → `Success: no issues found in 1 source file`
- **Entregáveis:** `src/api/routes/rlhf.py` (import `RlhfAuditReview`, schema `RatingReviewRequest`, endpoint `submit_rating_review`), `tests/api/test_rlhf_review.py` (novo — 9 testes cobrindo auth, 404, append-only, tie-break e sobrevivência do e-mail).
- **Task:** sem rastreio via `genie task` (mesmo motivo do Grupo 1); grupo rastreado só por este bloco.

### Execução — Group 4 — 2026-09-07 — **SHIP**

- **Engineer:** `genie:engineer-standard` / opus (filtros `channel`/`reviewed`, `reviewed_count`, serialização de parecer/histórico em `list_ratings`, tudo em `src/api/routes/rlhf.py` + `tests/api/test_rlhf.py`). Auto-relato completo e íntegro desta vez — verificado de forma independente mesmo assim.
- **Reviewer (independente do engineer):** `genie:reviewer` / opus — **SHIP**. Confirmou as 14 evidências com linha: `channel` estreita `summary` mas preserva `facets` idênticos (facets no passo 3, antes do estreitamento — `rlhf.py:761-790` vs `:807-813`); `channel=unknown` devolve não classificadas; `builder`/`clinic_admin` recebem o recorte de canal mas continuam ignorando `tenant_id`/`builder_id`/`reviewed`; `reviewed=false` usa genuinamente o degrau MAIS RECENTE (teste deliberado com uma nota tendo 2 pareceres — mais antigo `revisado=true`, mais novo `revisado=false` — confirmando que a redução por `ORDER BY rating_id, revisado_em DESC, id DESC` decide corretamente, não um seed raso de 1 parecer só); `reviewed_count` e `accepted_count` compartilham o mesmo portão `_row_has_reason` (`rlhf.py:879-889`), tornando divergência estruturalmente impossível; parecer sem motivo não move `reviewed_count`, saturar leva ao total exibível; `entries[].id` é o mesmo `rlhf_ratings.id` do PATCH/review; ausência real das chaves `review_history`/`reviewed_count` para não-super (testado por `not in`, não por `is None`); consulta de parecer roda em lote (1 query, `<=6` mesmo com 20 notas × 2 pareceres = 40 linhas) e só dispara quando `is_super` (sem trabalho desperdiçado); isolamento por tenant, mascaramento de carteirinha e o PATCH do Grupo 2 seguem intactos; nenhuma `DISTINCT ON` ou função de janela. Achados não bloqueantes: notas de super_admin sem parecer emitem `review_history: []` em vez de omitir a chave (documentar para o Grupo 6 tratar `[]` como "sem parecer"); o teste de contagem de consultas mede em N fixo (20) em vez de comparar dois N — suficiente para pegar regressão real, mas não o padrão mais forte possível.
- **Validação do orquestrador (re-executada, independente do relato do engineer):**
  - `poetry run pytest tests/api/test_rlhf.py tests/api/test_rlhf_review.py -q` → `93 passed, 3 warnings` (77 pré-existentes + 16 novos, 0 regressão)
  - `poetry run black --check src/api/routes/rlhf.py tests/api/test_rlhf.py` → `All done! 2 files would be left unchanged.`
  - `poetry run mypy src/api/routes/rlhf.py` → `Success: no issues found in 1 source file`
- **Entregáveis:** `src/api/routes/rlhf.py` (`list_ratings` com `channel`/`reviewed`, passo 4b de resolução em lote de parecer/histórico, `reviewed_count`, `_serialize_rating_event` com `review_history` opcional), `tests/api/test_rlhf.py` (16 testes novos cobrindo cada critério, incluindo o teste de contagem de consultas seguindo o padrão já existente em `:2690-2735`).
- **Task:** sem rastreio via `genie task` (mesmo motivo do Grupo 1); grupo rastreado só por este bloco.

### Execução — Group 6 — 2026-09-07 — **SHIP**

- **Engineer:** `genie:engineer-standard` / opus (dropdowns de canal/revisado, `ReviewPanel` com observação + toggle REVISADO + histórico, `TraceLink`, KPI `reviewed_count`, recorte por papel via `useAuth()` introduzido pela primeira vez neste arquivo, 26 strings PT/EN novas). Auto-relato completo e íntegro — verificado de forma independente mesmo assim.
- **Reviewer (independente do engineer):** `genie:reviewer` / opus — **SHIP**. Confirmou todos os 9 critérios de aceitação com evidência de linha: canal envia `channel` sem encolher builder/clínica (contagem de `<option>` idêntica antes/depois — teste dedicado); revisado envia `reviewed` só quando `showReviewed` (`isSuperAdmin`); `TraceLink` aponta para `/traces/{trace_event_id}?tenant_id={trace_tenant_id}` com guarda ambos-ou-nenhum (`rlhf-audit.tsx:484`); toggle lê `history[0]?.revisado` (mais recente primeiro, por convenção do backend) e posta o booleano oposto; observação em branco serializa como `null`, nunca `""` (`:524-531`); recorte de `investor` oculta dropdown Revisado, KPI `reviewed_count`, painel de parecer, `TraceLink` (inclusive o texto de "sem trace") e aceitar/ignorar — tudo sob um único `showSuperControls={isSuperAdmin}` (`:917`), predicado mais forte que `!isInvestor` porque não depende de só duas roles existirem hoje; builder/clínica gated em `isSuperAdmin` explicitamente (`:899`), não em `!isInvestor`; paridade de chaves PT/EN confirmada por diff vazio entre os dois conjuntos de chaves `rlhf_audit_*`. Achado não bloqueante, decisão consciente e não escopo indevido: os badges somente-leitura "No Dataset"/"Ignorado" (que expõem `accepted_by_email`) foram agrupados na mesma coluna `showSuperControls` que os botões de ação — a wish só exige ocultar os controles de escrita, mas ocultar também o status de leitura evita vazar identidade do revisor para `investor` e está alinhado ao espírito do DESIGN ("controles de auditoria... ocultos para investor"). Os 6 testes pré-existentes não foram enfraquecidos (um foi reforçado, passando a também afirmar ausência de `channel=`/`reviewed=` no carregamento padrão).
- **Validação do orquestrador (re-executada, independente do relato do engineer):**
  - `pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/rlhf-audit.test.tsx` → `15 passed`
  - `pnpm --dir frontend/artifacts/neurostack run typecheck` → limpo, sem erros
  - `PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build` → `built in ~55s` (só avisos pré-existentes de sourcemap em componentes shadcn, sem relação com este grupo; `PORT`/`BASE_PATH` exigidos pelo `vite.config.ts` do repo, não relacionados à mudança)
- **Entregáveis:** `frontend/artifacts/neurostack/src/pages/rlhf-audit.tsx` (tipos `ReviewHistoryRow`, extensão de `RatingEntry`/`RatingSummary`, dropdowns de canal/revisado, `TraceLink`, `ReviewPanel`, `FeedbackEntryCard` extraído, KPI `reviewed_count`, recorte por papel), `frontend/artifacts/neurostack/src/pages/rlhf-audit.test.tsx` (mocks de `@/context/auth` e `wouter`, fixtures estendidas, 9 testes novos + 6 pré-existentes intactos), `src/locales/en.ts` e `src/locales/pt.ts` (26 chaves `rlhf_audit_*` cada, paridade exata).
- **Task:** sem rastreio via `genie task` (mesmo motivo do Grupo 1); grupo rastreado só por este bloco.

### Execução — Group 7 — 2026-09-07 — **SHIP**

- **Engineer:** nenhum — grupo sem entregável de código; executado diretamente pelo orquestrador via shell, conforme a tabela de papéis do skill `work` ("Quick validation: shell diretamente, sem subagent").
- **Reviewer:** dispensado pelo mesmo motivo — não há diff de código para revisar; a evidência é a própria saída determinística dos comandos de validação, reproduzida abaixo na íntegra.
- **Validação do orquestrador (execução direta, saída completa registrada):**
  - `test -f .genie/wishes/rlhf-auditoria-humana/baseline-before.txt` → presente (169 linhas, colhido pelo Grupo 0).
  - `poetry run pytest -q 2>&1 | tee .genie/wishes/rlhf-auditoria-humana/baseline-after.txt` → `2683 passed, 14 skipped, 48 warnings in 82.15s (0:01:22)`.
  - Conjunto de falhas **antes** (baseline do Grupo 0, pré-existente e alheio a este wish):
    - `FAILED tests/integration/test_knowledge_document_metadata.py::test_metadata_mutators_handle_preflush_default_and_malformed_values`
    - `FAILED tests/integration/test_knowledge_document_metadata.py::test_metadata_mutators_persist_copy_on_write_after_reload`
  - Conjunto de falhas **depois:** vazio (nenhuma linha `FAILED`).
  - `diff <(...antes...) <(...depois...)` → só linhas `<` (as 2 falhas de antes, ausentes depois — corrigidas externamente, fora do escopo deste wish); nenhuma linha `>` (nenhuma falha nova). Diferença "depois menos antes" é vazia, conforme exigido.
  - `pnpm --dir frontend/artifacts/neurostack run test` → `Test Files 23 passed (23)`, `Tests 194 passed (194)` (inclui os 15 de `rlhf-audit.test.tsx` do Grupo 6 e os 49 de `trace-viewer`/`layout`/`rating-block`/`trace-detail` do Grupo 5, todos já verificados isoladamente antes, agora confirmados juntos sem interferência cruzada).
  - `pnpm --dir frontend/artifacts/neurostack run typecheck` → limpo, sem erros.
  - `PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build` → `✓ 2804 modules transformed`, `built in 53.89s` (só avisos pré-existentes de sourcemap em componentes shadcn e de tamanho de chunk, sem relação com este wish; `PORT`/`BASE_PATH` exigidos pelo `vite.config.ts` do repo).
- **Entregáveis:** `.genie/wishes/rlhf-auditoria-humana/baseline-after.txt` (novo — suíte backend completa pós-wish), atualização deste bloco em `WISH.md`. Nenhum arquivo de aplicação foi tocado por este grupo.
- **Task:** sem rastreio via `genie task` (mesmo motivo do Grupo 1); grupo rastreado só por este bloco.

---

## Files to Create/Modify

```
# Criar
alembic/versions/<novo>_020_rlhf_audit_tables.py
src/models/rlhf_rating.py
src/models/rlhf_audit_review.py
tests/integration/test_rlhf_audit_postgres.py
tests/api/test_rlhf_review.py
frontend/artifacts/neurostack/src/pages/trace-detail.test.tsx

# Modificar
src/api/routes/rlhf.py
src/models/__init__.py
tests/api/test_rlhf.py
frontend/artifacts/neurostack/src/pages/rlhf-audit.tsx
frontend/artifacts/neurostack/src/pages/rlhf-audit.test.tsx
frontend/artifacts/neurostack/src/pages/trace-detail.tsx
frontend/artifacts/neurostack/src/pages/chat.tsx
frontend/artifacts/neurostack/src/components/trace-viewer.tsx
frontend/artifacts/neurostack/src/components/trace-viewer.test.tsx
frontend/artifacts/neurostack/src/components/layout.tsx
frontend/artifacts/neurostack/src/components/layout.test.tsx
frontend/artifacts/neurostack/src/components/rating-block.tsx
frontend/artifacts/neurostack/src/components/rating-block.test.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
```
