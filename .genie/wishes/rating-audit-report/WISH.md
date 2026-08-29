# Wish: Relatório de Auditoria de Notas (1-5)

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `rating-audit-report` |
| **Date** | 2026-08-25 |
| **Author** | Lorenzo |
| **Appetite** | medium |
| **Branch** | `wish/rating-audit-report` |
| **Repos touched** | neurostack-api (backend `src/`, frontend `frontend/artifacts/neurostack/`) |
| **Design** | [DESIGN.md](../../brainstorms/rating-audit-report/DESIGN.md) |

## Summary

Consolida o feedback do simulador num canal único: o fluxo "Corrigir / Enviar Feedback" sai inteiro e a nota 1-5 passa a abrir caixa de motivo em 1-3, no simulador e nos traces. O painel master ganha um relatório honesto — total, distribuição, nota média e acervo de opiniões aceitas — filtrável por builder, clínica e período, com os KPIs agregados no servidor em vez de calculados sobre uma página de 100 linhas.

Antes do relatório, conserta a atribuição: hoje `submit_rating` grava o evento sob o tenant de quem avaliou, então a nota que um builder dá no simulador ao agente de uma clínica cai no tenant HQ do builder e o filtro por clínica seria decorativo.

## Scope

### IN

- Remoção completa do fluxo de sugestão: `FeedbackBlock`, aba "Sugestões RLHF", `POST /v1/rlhf/validate`, `POST /v1/rlhf/feedback`, `GET /v1/admin/rlhf/pending`, schemas órfãos e testes
- Caixa de motivo passa a abrir em notas 1-3 (hoje 1-2), com piso único de 8 caracteres espelhado entre `canSubmit` e o disparo da análise
- Labels alinhadas em widget e painel: 1 Péssimo / 2 Ruim / 3 Regular / 4 Bom / 5 Ótimo
- `_build_analysis_prompt` reescrito em termo neutro de nota, preservando a âncora na metodologia SRM NeuroStack
- `subject_tenant_id` persistido em `event_data`, validado como UUID **e** existente em `Tenant`, com fallback para `Event.tenant_id`
- `GET /v1/rlhf/ratings` ganha `start_date`, `end_date`, `only_accepted`, `only_with_reason` e passa a devolver `{ summary, facets, entries }`
- `PATCH /v1/rlhf/ratings/{id}` com `action=approve` carimba `accepted_by` / `accepted_at` e rejeita com 422 entradas sem motivo
- Painel master reescrito: dropdowns alimentados por `facets`, seletor de período, KPIs, gráfico de distribuição e seção de feedbacks escritos com "Aceitar e gravar opinião"
- `DateRangePicker` extraído de `dashboard.tsx` para `components/date-range-picker.tsx`, consumido nos dois lugares

### OUT

- Exportação de arquivo (CSV/JSON) do acervo de opiniões — o pedido a coloca como "um dia"; gatilho na Simplicity Case
- Tabela dedicada para opiniões aceitas — reaproveita a flag `approved_for_training` já existente
- Backfill de `subject_tenant_id` em notas já gravadas — caem no fallback `Event.tenant_id`
- Migração dos registros aprovados antes desta mudança (seguem aceitos, sem carimbo)
- Leitura dos eventos `rlhf.feedback` já gravados — perdem o único leitor e ficam apenas no banco
- Pipeline de fine-tuning que consome o acervo
- Notificação (push/email) para notas baixas
- Paginação com scroll infinito — `limit`/`offset` permanecem

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Remoção completa do fluxo de sugestão (widget, aba, três endpoints, schemas, testes) | Escolha do usuário. Endpoint sem chamador é superfície de manutenção sem dono |
| 2 | Caixa de motivo abre para notas 1-3; análise IA acompanha a mesma faixa, só com motivo válido | Pedido explícito. "Regular" carrega crítica acionável que hoje se perde. Sem texto não há o que a IA julgue |
| 3 | `has_reason := len((reason or "").strip()) >= 8`, definido uma vez e usado no disparo da análise, `only_with_reason`, seção de feedbacks escritos, `accepted_count` e guarda 422; mesmo limiar em `canSubmit` | Corta a nota de uma palavra sem descartar crítica curta legítima em pt-BR. Definição única evita que "sem motivo" signifique uma coisa no gráfico e outra na lista, e evita a UI aceitar texto que o backend descarta em silêncio |
| 4 | Persistir `subject_tenant_id` em `event_data`, mantendo `Event.tenant_id` como dono do registro e base do isolamento | Sem separar assunto de dono, o filtro por clínica é decorativo. Mexer em `Event.tenant_id` quebraria o isolamento por tenant que já está correto |
| 5 | Só persistir o `clinic_tenant_id` recebido se for UUID **e** existir em `Tenant`; senão cair para `tenant.tenant_id` | Para `clinic_admin`, `viewingClinicId` é o `clinic_id` de `ClinicConfigTable` (`auth.py:88-93`), um slug de negócio que `trace-viewer.tsx:373` envia como `clinicTenantId`. Testar só presença gravaria o slug e o filtro nunca resolveria |
| 6 | Endpoint único devolvendo `{ summary, facets, entries }` | KPIs calculados no cliente sobre uma página mentem. Endpoint único impede que resumo, facetas e lista discordem de filtro |
| 7 | Resumo, facetas e todo filtro que vive em `event_data` agregados em Python; `limit`/`offset` fatiam depois de todos eles | `database.py:62` força SQLite nos testes e `:23` só remapeia o DDL — operador de leitura JSON continua dialeto Postgres. Paginar na query antes de um filtro em memória devolve página incompleta sem erro visível |
| 8 | `summary` mede o recorte builder/clínica/período e ignora `only_accepted` / `only_with_reason`; `facets` é calculado antes do recorte de builder e clínica | "Total" e "nota média" descrevem a população avaliada; deixar um filtro de navegação mexer neles tornaria o relatório incomparável entre visões. Faceta que encolhe com a própria seleção deixaria o dropdown sem como voltar atrás |
| 9 | Dropdowns alimentados por `facets`, não por `/admin/builders` nem `/admin/clinics` | `admin.py:621` exige `builder_id` e `is_active`; `admin.py:161` pula tenants só com usuários builder/super admin/investor — os mesmos onde caem as notas de simulador. Os cadastros omitem tenants que o relatório soma |
| 10 | `facets.clinics` carrega `builder_id`; o estreitamento por builder é client-side | O servidor não pode encolher `facets` sem tornar a seleção irreversível, então o dado que permite estreitar tem de viajar junto |
| 11 | Janela reusa `_apply_filters` de `trace_service.py:85-100` (alargamento do dia final + `_to_utc` BRT); default de 30 dias vive no painel | Reimplementar erraria calado no Postgres enquanto passa no SQLite. O default fica na UI porque é preferência de leitura, não regra de domínio |
| 12 | "Aceitar e gravar opinião" reaproveita a ação `approve` e a flag `approved_for_training`, somando `accepted_by` / `accepted_at`; restrito a entradas com `has_reason` | Consolidação escolhida pelo usuário — sem migração e sem segundo lugar para consultar. O ato é sobre feedback escrito; oferecer aceite em entrada sem motivo faria o KPI superar o acervo visível |
| 13 | Prompt reescrito em termo neutro de nota, preservando a âncora na metodologia SRM | `rlhf.py:186` pressupõe insatisfação; com nota 3 a pergunta enviesa a opinião gravada. A âncora é requisito explícito do pedido |
| 14 | Extrair `DateRangePicker` de `dashboard.tsx:88` para componente compartilhado | Segundo consumidor; mesmo caminho já aplicado a `RatingBlock` |

## Simplicity Case

- **Simplest complete design:** apagar o fluxo de sugestão; mudar dois limiares de `2` para `3`; persistir um campo que já chega na requisição; acrescentar quatro query params e dois objetos à resposta do endpoint que já existe; carimbar quem/quando na ação de aceite que já existe; reescrever o corpo de uma página de painel. Nenhuma tabela nova, nenhuma migração, nenhum endpoint novo.
- **Added machinery:** `summary` existe porque os KPIs de hoje são calculados no cliente sobre `limit=100` — o total exibido ao master está errado por construção. `facets` existe porque os endpoints de cadastro omitem justamente os tenants onde as notas de simulador caem, e substitui duas chamadas HTTP em vez de somar uma. `subject_tenant_id` existe porque sem ele o filtro por clínica é decorativo. `accepted_by` / `accepted_at` existem porque um acervo de opiniões sem autoria e data não tem valor de auditoria. O piso de 8 caracteres é uma condição de uma linha, replicada no frontend para não criar estado divergente.
- **Deferred until measured:** exportação em arquivo, até haver destino concreto — a flag e o filtro já deixam o conjunto identificável, então é um leitor a mais e não pede mudança de modelo. Agregação em SQL, até o rowset filtrado passar de ~10k linhas. Teto diário de análises por tenant, até passar de 200 análises/dia por clínica.
- **Complexity removed:** três endpoints, cinco schemas Pydantic, um componente React de ~210 linhas com máquina de cinco estados, uma aba inteira, um seletor de abas, um botão decorativo e duas dependências de endpoints de cadastro. Descartados também uma tabela dedicada para opiniões e um segundo endpoint de resumo.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [ ] `POST /v1/rlhf/validate`, `POST /v1/rlhf/feedback` e `GET /v1/admin/rlhf/pending` retornam 404 e nenhum arquivo `.py` ou `.tsx` da árvore principal os referencia (grep excluindo `.worktrees/`)
- [ ] Selecionar nota 3 no simulador e no trace abre a caixa de motivo; selecionar 4 não abre; com 7 caracteres o envio fica desabilitado e com 8 habilita
- [ ] Uma nota dada por um builder no simulador ao agente de uma clínica aparece no relatório sob **aquela clínica**, não sob o tenant HQ do builder
- [ ] `GET /v1/rlhf/ratings` devolve `{ summary, facets, entries }`, com `summary.total` maior que `entries.length` quando houver mais notas que `limit`
- [ ] `summary` não muda ao alternar `only_accepted` ou `only_with_reason`, e `facets` não encolhe ao selecionar um builder
- [ ] Uma nota criada hoje é devolvida quando `end_date` é a data de hoje, e uma nota das 22h BRT do dia D é devolvida com `end_date=D` e não com `end_date=D-1`
- [ ] `PATCH /v1/rlhf/ratings/{id}` com `action=approve` carimba `accepted_by` / `accepted_at` e retorna 422 numa entrada sem motivo
- [ ] O painel exibe visão única com dropdowns de Builder e Clínica por nome, período, KPIs (Total, Nota média, % Satisfação, % Insatisfação, Opiniões aceitas), gráfico 1-5 com contagem e percentual, e seção de feedbacks escritos
- [ ] A suíte backend inteira passa e `npx tsc --noEmit` não acusa erro ausente da linha de base registrada antes da mudança

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — multi-package +1; blast radius atravessa rota, schema, componente e teste +1 | engineer-standard / medium | Remover o fluxo de sugestão inteiro do backend, do simulador e do painel |

### Wave 2 (parallel)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 3 — prompt-skill change +1; multi-package +1; qualidade do prompt sem teste determinístico +1 | engineer-standard / high | Faixa do motivo 1-3, piso de 8 caracteres, labels e prompt neutro |
| 3 | engineer | 2 — stateful work +2 (campo persistido novo com semântica de fallback) | engineer-standard / medium | Persistir `subject_tenant_id` validado em `event_data` |

### Wave 3 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 5 — stateful work +2; multi-package +1 (rota + shape da suíte); divergência SQLite/Postgres sem teste determinístico +1; prior rework +1 | engineer-complex / high | Filtros, facetas, resumo e a ordem de execução do handler |
| 5 | engineer | 2 — stateful work +2 (carimbo persistido e guarda de transição) | engineer-standard / medium | Carimbo de aceite, guarda 422 e exposição na leitura |

### Wave 4 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 6 | engineer | 4 — aceitação subjetiva de layout +2; multi-package +1 (painel, dashboard, componente novo); sem teste determinístico para o visual +1 | engineer-complex / high | Painel master: filtros por faceta, KPIs, gráfico e acervo de opiniões |

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

### Group 1: Remoção do fluxo de sugestão

**Goal:** Apagar o canal de feedback concorrente inteiro — backend, simulador e painel — deixando uma superfície única de feedback.

**Deliverables:**
1. `src/api/routes/rlhf.py`: remover `POST /v1/rlhf/validate` e `POST /v1/rlhf/feedback`, os schemas órfãos (`ValidateRequest`, `ValidateResponse`, `ViolationItem`, `FeedbackRequest`, `FeedbackResponse`) e o import `get_policy_engine`
2. `src/api/routes/admin.py:543`: remover `GET /v1/admin/rlhf/pending`
3. `frontend/.../pages/chat.tsx`: remover a definição de `FeedbackBlock` (`:147`) e os dois usos (`:416` demo, `:801` real), incluindo a legenda "(Apenas demonstração, feature em breve)" do path demo
4. `frontend/.../pages/rlhf-audit.tsx`: remover a aba "Sugestões RLHF" (`SuggestionsTab`), o seletor de abas, o tipo `SuggestionEntry` e o botão decorativo "Exportar Dataset" (`:512`) — a página passa a ter visão única
5. Ajustar `tests/api/test_rlhf.py` (blocos de `/feedback` e `/rlhf/pending`, incluindo o cabeçalho de `:4` que cita o endpoint removido) e `tests/api/test_admin.py:128`

**Acceptance Criteria:**
- [ ] `POST /v1/rlhf/validate`, `POST /v1/rlhf/feedback` e `GET /v1/admin/rlhf/pending` retornam 404
- [ ] `grep -rn "rlhf/validate\|rlhf/feedback\|rlhf/pending\|FeedbackBlock\|SuggestionEntry" src frontend tests --include='*.py' --include='*.tsx'` não retorna nada na árvore principal
- [ ] `chat.tsx` não contém mais `FeedbackBlock` (definição nem usos); no path autenticado não-investor o simulador exibe apenas o widget de nota, e no path demo/investor (`chat.tsx:406-427`) não sobra bloco de interação nenhum
- [ ] O painel exibe visão única, sem seletor de abas e sem o botão "Exportar Dataset"
- [ ] Nenhum schema Pydantic ou import fica órfão em `rlhf.py`
- [ ] A suíte backend passa sem teste órfão apontando para rota inexistente

**Validation:**
```bash
poetry run pytest tests/ -q \
  && cd frontend/artifacts/neurostack \
  && npm run typecheck \
  && npm run test
```
_Escopo: remove rotas compartilhadas e um componente usado em dois lugares, atravessando backend e frontend — alcance de runtime compartilhado, logo o gate completo do repositório (suíte + typecheck + vitest)._

**depends-on:** none

---

### Group 2: Faixa do motivo 1-3, labels e prompt neutro

**Goal:** Abrir a caixa de motivo em 1-3 com piso único de 8 caracteres nos dois lados, alinhar as labels e tirar o viés de insatisfação do prompt sem perder a âncora na metodologia.

**Deliverables:**
1. `components/rating-block.tsx:38`: `needsReason` passa de `selected <= 2` para `selected <= 3`; `:39` `canSubmit` passa a exigir `reason.trim().length >= 8` (hoje `> 0`)
2. Labels alinhadas em `rating-block.tsx` e `rlhf-audit.tsx`: 1 Péssimo / 2 Ruim / 3 Regular / 4 Bom / 5 Ótimo (hoje 4 e 5 divergem — "Boa resposta" / "Ótima")
3. `src/api/routes/rlhf.py:410`: `triggers_analysis` passa a `body.rating <= 3` **e** `has_reason`, com `has_reason := len((reason or "").strip()) >= 8` definido uma vez e reutilizado
4. `_build_analysis_prompt` (`rlhf.py:178`) reescrito em termo neutro de nota — pergunta se a crítica escrita procede dada a nota atribuída, mantendo a âncora explícita na metodologia SRM NeuroStack; `_parse_analysis_content` mantém o par `agreed`/`disagreed`
5. Atualizar a docstring de `rlhf.py:404` que cita "For ratings <= 2"
6. Novo `components/rating-block.test.tsx` cobrindo abertura da caixa e habilitação do envio

**Acceptance Criteria:**
- [ ] Selecionar nota 3 abre a caixa de motivo; selecionar 4 não abre
- [ ] Com nota 3 e motivo de 7 caracteres o botão "Enviar avaliação" fica desabilitado; com 8, habilita
- [ ] `POST /v1/rlhf/rating` com `rating=3` e motivo ≥8 grava `ai_analysis_status="pending"` e dispara a análise
- [ ] `POST /v1/rlhf/rating` com `rating=3` e motivo <8 grava a nota sem `ai_analysis_status` e não dispara análise
- [ ] `POST /v1/rlhf/rating` com `rating=4` não dispara análise, com ou sem motivo
- [ ] Os labels exibidos são Péssimo / Ruim / Regular / Bom / Ótimo no widget e no painel; `grep -rn "Boa resposta\|Ótima" src/components/rating-block.tsx src/pages/rlhf-audit.tsx` não retorna nada
- [ ] O prompt enviado ao LLM não pressupõe insatisfação e ainda cita a metodologia SRM NeuroStack
- [ ] `grep -rn "<= 2" src/api/routes/rlhf.py` não retorna prosa descrevendo o limiar da análise

**Validation:**
```bash
poetry run pytest tests/api/test_rlhf.py -q \
  && cd frontend/artifacts/neurostack \
  && npm run typecheck \
  && npx vitest run src/components/rating-block.test.tsx \
  && { grep -rn "Boa resposta\|Ótima" src/components/rating-block.tsx src/pages/rlhf-audit.tsx; test $? -eq 1; }
```
_Escopo: muda comportamento de um endpoint e de um componente, sem tocar em contrato compartilhado — testes focados nos dois arquivos alterados mais typecheck, que é o limite que a mudança de labels alcança._

**depends-on:** 1

---

### Group 3: Atribuição — `subject_tenant_id`

**Goal:** Fazer a nota registrar de qual clínica ela fala, para que o filtro por clínica do relatório signifique alguma coisa.

**Deliverables:**
1. `submit_rating` persiste `subject_tenant_id` em `event_data`, derivado do `clinic_tenant_id` que o frontend já envia (`rating-block.tsx:61`)
2. Validação obrigatória antes de persistir: só grava o valor recebido se ele fizer parse como UUID **e** casar com uma linha de `Tenant`; caso contrário grava `tenant.tenant_id`
3. `Event.tenant_id` permanece intocado — continua sendo o dono do registro e a base do isolamento por tenant
4. Testes cobrindo os quatro caminhos de atribuição

**Acceptance Criteria:**
- [ ] `POST /v1/rlhf/rating` com `clinic_tenant_id` de um tenant existente grava `subject_tenant_id` igual a ele, com `Event.tenant_id` inalterado (o tenant de quem avaliou)
- [ ] `POST /v1/rlhf/rating` sem `clinic_tenant_id` grava `subject_tenant_id` igual a `Event.tenant_id`
- [ ] `POST /v1/rlhf/rating` com `clinic_tenant_id` que não é UUID — o caso do `clinic_admin` no trace viewer, que envia o `clinic_id` de `ClinicConfigTable` — grava `subject_tenant_id` igual a `Event.tenant_id` e não persiste o valor recebido
- [ ] `POST /v1/rlhf/rating` com `clinic_tenant_id` que é UUID válido mas não existe em `Tenant` também cai no fallback
- [ ] Notas gravadas antes deste grupo seguem sem o campo e nada no caminho de escrita as toca — a leitura com fallback é responsabilidade do Group 4

**Validation:**
```bash
poetry run pytest tests/api/test_rlhf.py -q
```
_Escopo: escrita de um campo novo dentro de `event_data` num único endpoint, sem alterar contrato de leitura nem tocar no frontend — o arquivo de teste do endpoint cobre os quatro caminhos e é o que pode desprovar a mudança._

**depends-on:** 1

---

### Group 4: Endpoint de listagem — filtros, facetas e resumo

**Goal:** Transformar `GET /v1/rlhf/ratings` num relatório honesto: agregação no servidor, facetas derivadas dos dados e filtros que casam com o assunto da nota, respeitando a parede de dialeto SQLite/Postgres.

**Deliverables:**
1. Novos query params `start_date`, `end_date`, `only_accepted: bool`, `only_with_reason: bool`
2. **Janela de data reusando a convenção de `_apply_filters` (`src/services/trace_service.py:85-100`), copiada inteira e não reinventada:** (a) params `Optional[datetime]` no FastAPI, como `traces.py:138-142`; (b) `end_date` à meia-noite alarga para `23:59:59`, senão o dia final some — inclusive hoje, que é o `end_date` default do painel; (c) as duas pontas passam por `_to_utc`, que lê datetime ingênuo como BRT (UTC-3). O passo (c) não é cosmético: `Event.created_at` é `DateTime(timezone=True)` (`src/models/event.py:149-150`), então comparar ingênuo contra tz-aware passa no SQLite dos testes e desloca três horas no Postgres
3. Resposta passa de lista crua para `{ summary, facets, entries }`, com `summary = { total, average, accepted_count, distribution }` (`average` é `0.0` quando `total == 0`) e `facets = { builders: [{id, name}], clinics: [{id, name, builder_id}] }`
4. **Ordem de execução do handler, fixada como contrato:**
   1. SQL filtra `event_type`, isolamento de tenant e janela de data — só colunas reais, sem tocar em `event_data`. "Isolamento de tenant" é **exclusivamente** a cláusula do ramo não-super-admin (`Event.tenant_id == tenant.tenant_id`, `rlhf.py:517`). Os filtros SQL de `tenant_id` e `builder_id` do ramo super-admin (`rlhf.py:506-511`, incluindo o `join(Tenant)`) são **removidos**: casam com o dono do registro enquanto o recorte do relatório casa com `subject_tenant_id` no passo 4, e manter os dois faria um `AND` entre dono e assunto, devolvendo zero linhas no caso principal. Mantém o `order_by(Event.created_at.desc())` (`rlhf.py:519`)
   2. Python carrega o conjunto inteiro preservando a ordem do SQL e resolve o mapa `tenant_id → (nome, builder_id, nome do builder)` para os `subject_tenant_id` distintos — duas consultas em lote, nunca uma por linha
   3. Python calcula `facets` sobre esse conjunto, antes de qualquer recorte por builder ou clínica
   4. Python aplica o recorte de builder / clínica casando `subject_tenant_id`, **com fallback para `Event.tenant_id` nas linhas que não têm o campo** — sem isso o filtro e as facetas excluiriam calado toda nota anterior ao Group 3
   5. Python calcula `summary` sobre o resultado do passo 4
   6. Python aplica `only_accepted` e `only_with_reason`
   7. `limit`/`offset` fatiam **depois** do passo 6 — nunca na query, senão a página volta incompleta sem erro nenhum
5. `_serialize_rating_event` (`rlhf.py:467`) passa a devolver `subject_tenant_id`, `subject_clinic_name` e `builder_name`
6. Ajustar os testes que consomem a resposta como lista crua: `tests/api/test_rlhf.py:117-121, 141, 176-181, 194-201, 206-212, 246` — dois deles afirmam `isinstance(entries, list)` direto

**Acceptance Criteria:**
- [ ] `GET /v1/rlhf/ratings` devolve `{ summary, facets, entries }` com `summary.total` igual ao número de linhas do recorte, comprovadamente maior que `entries.length` quando houver mais notas que `limit`
- [ ] `summary.average` confere com a média aritmética das notas do recorte, arredondada a 1 casa; num recorte sem notas vale `0.0`
- [ ] `summary.distribution` soma exatamente `summary.total`
- [ ] Uma nota criada hoje é devolvida quando `end_date` é a data de hoje — o dia final entra inteiro
- [ ] Uma nota criada às 22h BRT do dia D é devolvida com `end_date=D` e não com `end_date=D-1`, provando a conversão BRT→UTC
- [ ] `start_date` / `end_date` excluem notas fora da janela, no resumo, nas facetas e na lista
- [ ] Uma nota cujo `Event.tenant_id` é o tenant HQ do builder e cujo `subject_tenant_id` é a clínica X é devolvida tanto sob `tenant_id=X` quanto sob `builder_id` do builder de X — prova que os filtros casam com o assunto e que as cláusulas SQL de `rlhf.py:506-511` saíram
- [ ] Uma nota gravada sem `subject_tenant_id` é recortada e facetada por `Event.tenant_id`, aparecendo no relatório em vez de sumir
- [ ] `only_accepted=true` devolve apenas entradas com `approved_for_training=true`
- [ ] `only_with_reason=true` devolve apenas entradas que satisfazem `has_reason`; uma entrada com motivo de 7 caracteres não aparece, qualquer que seja a nota
- [ ] Com `only_with_reason=true`, `limit=10` e mais de 10 entradas com motivo no recorte, a primeira página devolve 10 entradas; o mesmo vale para `only_accepted=true`
- [ ] `summary` não muda ao alternar `only_accepted` ou `only_with_reason`; apenas `entries` muda
- [ ] `facets` não encolhe ao selecionar um builder, e cada item de `facets.clinics` traz `builder_id`, nulo quando a clínica não tem builder
- [ ] `entries` mantém a ordem decrescente por data que o SQL produz
- [ ] Chamado por role não-super-admin, nenhum filtro expõe linhas cujo `Event.tenant_id` não seja o do chamador; `facets` pode listar várias clínicas-assunto dessas linhas
- [ ] Uma requisição com 50 entradas de 10 clínicas distintas não dispara mais que 3 consultas além da principal
- [ ] Nenhum operador de leitura JSON aparece em cláusula SQL — a suíte roda em SQLite e passa

**Validation:**
```bash
poetry run pytest tests/ -q && poetry run mypy src --ignore-missing-imports
```
_Escopo: muda o contrato de resposta de um endpoint lido por toda a suíte e reescreve o caminho de query; alcance de runtime compartilhado com impacto incerto sobre testes existentes, logo a suíte inteira mais o typecheck do backend em vez de um arquivo só._

**depends-on:** 2, 3

---

### Group 5: Aceite de opinião — carimbo, guarda e leitura

**Goal:** Dar valor de auditoria ao acervo: quem aceitou, quando, e só sobre feedback escrito.

**Deliverables:**
1. `PATCH /v1/rlhf/ratings/{id}` com `action=approve` carimba `accepted_by` e `accepted_at` (ISO-8601 UTC) além de `approved_for_training`
2. O mesmo `PATCH` rejeita com 422 entradas que não satisfaçam `has_reason` — aceitar opinião é um ato sobre feedback escrito
3. `_serialize_rating_event` passa a devolver `accepted_by`, `accepted_at` e `accepted_by_email`
4. `accepted_by_email` resolvido por **uma** consulta em lote `select(User.id, User.email).where(User.id.in_(...))` sobre a página já fatiada, com o mapa passado ao serializer — nunca uma consulta por linha
5. `summary.accepted_count` conta apenas entradas `approved_for_training` **e** com `has_reason`, excluindo aceites legados sem motivo
6. A ação `ignore` permanece inalterada

**Acceptance Criteria:**
- [ ] `PATCH /v1/rlhf/ratings/{id}` com `action=approve` grava `accepted_by` e `accepted_at` além de `approved_for_training`
- [ ] `PATCH` com `action=approve` numa entrada sem motivo retorna 422
- [ ] `GET /v1/rlhf/ratings` devolve `accepted_by`, `accepted_at` e `accepted_by_email` nas entradas aceitas
- [ ] Num recorte com N entradas totais e `limit >= N`, `summary.accepted_count` é exatamente igual ao número de entradas retornadas com `only_with_reason=true&only_accepted=true`, e ambos ignoram aceites legados sem motivo
- [ ] Registros aprovados antes desta mudança seguem aceitos e exibem autoria "—", sem migração
- [ ] A resolução de e-mails é uma consulta em lote, não uma por entrada

**Validation:**
```bash
poetry run pytest tests/api/test_rlhf.py -q && poetry run mypy src --ignore-missing-imports
```
_Escopo: acrescenta campos persistidos e uma guarda de transição num endpoint já coberto pelo arquivo de teste do módulo; o typecheck entra porque o serializer muda de assinatura ao receber o mapa de e-mails._

**depends-on:** 4

---

### Group 6: Painel master — relatório

**Goal:** Entregar o relatório pedido: filtros por builder, clínica e período, KPIs honestos, gráfico de distribuição e o acervo de opiniões aceitas.

**Deliverables:**
1. Extrair `DateRangePicker` de `dashboard.tsx:88` para `components/date-range-picker.tsx` e consumir nos dois lugares
2. Filtros: dropdown Builder, dropdown Clínica e seletor de período, todos com opção "Todos"; o painel abre com os últimos 30 dias e **sempre** envia `start_date` e `end_date` — o default vive no painel, não no endpoint
3. Dropdowns alimentados por `facets` da própria resposta; nenhuma chamada a `/admin/builders` ou `/admin/clinics`
4. Estreitamento do dropdown de Clínica é **client-side**, casando `facets.clinics[].builder_id` com o builder selecionado; clínicas com `builder_id` nulo aparecem apenas quando Builder = "Todos"
5. O painel faz **uma** requisição por estado de filtro, sempre com `only_with_reason=true`: KPIs, gráfico e lista saem todos dessa resposta
6. KPIs: Total de notas, Nota média, % Satisfação (≥4), % Insatisfação (≤2), Opiniões aceitas; gráfico de distribuição 1-5 com contagem **e** percentual
7. Seção "Feedbacks escritos" com o texto do cliente e a opinião da IA nos estados `pending` / `done` / `failed`; ação primária renomeada para "Aceitar e gravar opinião", exibida **apenas** nessa seção; "Ignorar" permanece; entradas aceitas exibem e-mail e data
8. Filtro "Somente aceitas" para navegar o acervo
9. Novo `pages/rlhf-audit.test.tsx` cobrindo estreitamento de dropdown, KPIs a partir de `summary` e visibilidade do botão de aceite

**Acceptance Criteria:**
- [ ] O filtro de Builder é um dropdown com nomes (não campo de UUID) e inclui a opção "Todos"
- [ ] Selecionar um builder restringe o dropdown de Clínica às clínicas daquele builder
- [ ] Toda clínica que aparece em `summary.total` é selecionável no dropdown, incluindo tenants HQ de builder que `/v1/admin/clinics` omite
- [ ] Alterar qualquer filtro atualiza KPIs, gráfico e lista a partir da mesma resposta
- [ ] Os KPIs exibem Total, Nota média, % Satisfação (≥4), % Insatisfação (≤2) e Opiniões aceitas
- [ ] O gráfico de distribuição mostra contagem e percentual para cada nota de 1 a 5
- [ ] A seção "Feedbacks escritos" lista apenas entradas com motivo, cada uma com o texto do cliente e a opinião da IA nos estados `pending` / `done` / `failed`
- [ ] O botão "Aceitar e gravar opinião" aparece apenas na seção de feedbacks escritos e a entrada passa a exibir o e-mail de quem aceitou e a data — não um UUID
- [ ] O filtro "Somente aceitas" mostra o acervo de opiniões aceitas
- [ ] A reescrita preserva as labels do Group 2 — `grep -n "Boa resposta\|Ótima" src/pages/rlhf-audit.tsx` não retorna nada e as cinco labels são Péssimo / Ruim / Regular / Bom / Ótimo
- [ ] `components/date-range-picker.tsx` existe e é importado tanto por `dashboard.tsx` quanto pelo painel; o dashboard continua filtrando por período como antes
- [ ] `npx tsc --noEmit` não acusa erro ausente da linha de base registrada antes da mudança

**Validation:**
```bash
cd frontend/artifacts/neurostack \
  && npm run typecheck \
  && npm run test \
  && npm run build \
  && { grep -rn "Boa resposta\|Ótima" src/components/rating-block.tsx src/pages/rlhf-audit.tsx; test $? -eq 1; }
```
_Escopo: extrai um componente compartilhado consumido também pelo dashboard, então o alcance passa de uma página só — vitest inteiro para pegar regressão no dashboard, mais o build, que é o gate que prova que a árvore de imports reorganizada resolve._

**depends-on:** 2, 4, 5

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Funcional: avaliar uma resposta com nota 3 no simulador abre a caixa de motivo, exige 8 caracteres e a nota chega ao painel com a opinião da IA preenchida
- [ ] Funcional: no painel master, filtrar por um builder restringe o dropdown de clínicas e os KPIs mudam junto com o gráfico e a lista
- [ ] Funcional: "Aceitar e gravar opinião" carimba a entrada com o e-mail do operador e a data, e ela reaparece sob o filtro "Somente aceitas"
- [ ] Integração: uma nota dada por um builder no simulador ao agente de uma clínica é encontrável no relatório filtrando por aquela clínica
- [ ] Integração: o período default de 30 dias inclui notas criadas hoje, inclusive as do fim da tarde/noite (fuso BRT)
- [ ] Regressão: o simulador segue respondendo normalmente sem o bloco de sugestão, e o path demo/investor não quebra
- [ ] Regressão: o dashboard continua filtrando por período como antes após a extração do `DateRangePicker`
- [ ] Regressão: um `clinic_admin` continua vendo apenas as notas do próprio tenant

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Nota 3 com motivo passa a disparar chamada LLM e "Regular" tende a ser a nota mais frequente; **quem paga é a clínica** — `_analyze_rating_async` resolve as credenciais do tenant da clínica (`rlhf.py:232`, `:290`), não uma chave de plataforma | Medium | Análise é assíncrona e não bloqueia a resposta. Guarda imediata: piso de 8 caracteres. Gatilho medível: acima de 200 análises/dia por clínica, adotar teto diário por tenant |
| Mudar a resposta de `GET /v1/rlhf/ratings` de lista para objeto é quebra de contrato | Medium | Único consumidor de produção é `rlhf-audit.tsx:264`; não há SDK gerado. Dentro do repo, os pontos de `tests/api/test_rlhf.py` que leem lista crua estão nomeados no Group 4 e são ajustados no mesmo grupo |
| Filtro por `subject_tenant_id` em SQL quebraria a suíte, que roda em SQLite | High | Decisão 7 e a ordem de execução do Group 4 fixam agregação e filtragem em Python; critério de aceitação exige que a suíte passe em SQLite |
| Paginar na query antes de um filtro aplicado em memória devolve página incompleta **sem erro visível** | High | Passo 7 da ordem de execução é contrato, não detalhe; dois critérios de aceitação exercitam `limit=10` com mais de 10 entradas filtradas |
| Reimplementar a janela de data em vez de copiar `_apply_filters` erra calado no Postgres enquanto passa no SQLite | Medium | Deliverable 2 do Group 4 exige reuso da convenção; dois critérios cobrem o dia final e a conversão BRT→UTC |
| Notas antigas não têm `subject_tenant_id` e seguem atribuídas ao tenant HQ do builder | Medium | É o comportamento de hoje, então nada regride. Backfill declarado OUT; a faceta exibe esse tenant pelo nome real, sem inventar vínculo |
| A resolução do mapa de tenants e de e-mails pode virar N+1 | Medium | Consulta em lote em dois pontos, com critério de aceitação contando as queries |
| "Sem regressão" não é decidível sem linha de base | Low | Registrar a saída de `npx tsc --noEmit` e da suíte **antes** de tocar no código; os critérios comparam contra esse registro |
| Grep de verificação das remoções pode acusar falso positivo | Low | `.worktrees/new-users-metric/` é cópia de worktree do próprio repo e ainda contém as chamadas antigas; verificar apenas na árvore principal |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Group 1 execution review — 2026-08-25 (BLOCKED)

- **Verdict:** BLOCKED
- **Reviewer:** `/root/review_group1`
- **Verified:** the obsolete routes, `FeedbackBlock`, `SuggestionEntry`, audit tabs, and export control are absent; scoped grep and `git diff --check` passed; focused removal tests (3) and frontend typecheck passed.
- **Blocker:** the mandatory aggregate gate (`poetry run pytest tests/ -q && cd frontend/artifacts/neurostack && npm run typecheck && npm run test`) made progress through 11% of 1293 tests, then produced no output while executing `tests/api/test_events_e2e.py`; it was interrupted after several silent polls (exit 130), so there is no passing aggregate evidence. Group 1 remains in progress and downstream groups are not dispatched.

### Group 1 execution review — 2026-08-26 (SHIP)

- **Verdict:** SHIP
- **Reviewer:** independent `genie:reviewer` subagent (agentId `a5f7463a8dddf98a8`)
- **Unblocks:** the 2026-08-25 BLOCKED verdict was solely about the aggregate gate producing no output partway through the suite; re-run this session completed cleanly in ~65s with no hang (1075 passed, 218 failed), and the reviewer independently reproduced this.
- **Verified:** all 6 acceptance criteria met — 404s on the three removed routes, scoped grep clean in the main tree, `chat.tsx`/`rlhf-audit.tsx` free of `FeedbackBlock`/`SuggestionEntry`/tab switcher/"Exportar Dataset", no orphaned Pydantic schema or import in `rlhf.py`. Focused suite (`test_rlhf.py` + `test_admin.py`) 31/31 pass; frontend typecheck clean; frontend vitest 39/39 pass.
- **Full-suite baseline established:** the 218 failing tests are pre-existing test-order/fixture-pollution (each passes in isolation; none touch rlhf/admin/rating files — they live in `tests/handoff`, `tests/memory`, `tests/policy`, `tests/prompt`, and a few `tests/services/*`). Reviewer recommends downstream groups (2-6) track a failure-delta against this 218 baseline rather than gating on zero aggregate failures.
- **LOW findings (not blocking, left as-is):** orphan comment-banner remnant at `tests/api/test_rlhf.py:271` from the removed "GET /v1/admin/rlhf/pending" section header; `rlhf-audit.tsx:338`'s `justify-between` header wrapper has nothing to space against post-removal (Group 6 rewrites this page anyway).
- **Governance note (out of scope, not a Group 1 blocker):** `src/api/routes/admin.py` carries uncommitted hunks (`list_clinics`, `admin_stats`, `admin_dashboard`, `list_builders` filtering by `event_type == "message.in"`) that predate this session and belong to the concurrent `wish/api-provider-settings` branch this working tree is checked out on — not part of any `rating-audit-report` group's deliverables. Flagged for the user; not touched by this wish's work.

### Group 2 execution review — 2026-08-26 (SHIP)

- **Verdict:** SHIP
- **Reviewer:** independent `genie:reviewer` subagent (agentId `a9747ea3ebd06208b`)
- **Note:** deliverables were already present in the working tree when the engineer picked up the task (shared long-running workspace); both engineer and reviewer independently re-verified all criteria and re-ran validation rather than trusting that state.
- **Verified:** all 8 acceptance criteria met — `needsReason`/`canSubmit` at `rating-block.tsx:43-44` with `MIN_REASON_LENGTH = 8`; labels aligned (Péssimo/Ruim/Regular/Bom/Ótimo) in both `rating-block.tsx:8-14` and `rlhf-audit.tsx:33-39`; `has_reason`/`triggers_analysis` at `rlhf.py:323-324` (`rating <= 3 and has_reason`); `_build_analysis_prompt` neutral wording preserving the SRM anchor; docstrings updated; new `rating-block.test.tsx` with 3 non-vacuous tests. `poetry run pytest tests/api/test_rlhf.py -q` → 17 passed; frontend typecheck clean; `rating-block.test.tsx` → 3 passed; label greps clean.
- **LOW findings (not blocking, left as-is):** redundant `.strip()` in `has_reason` (already-stripped `reason`) — intentional per the wish's exact predicate form, revisit when Group 4/5 lift it into a shared helper; pre-existing unused `get_llm_client` import (predates Group 2, Group 1's cleanup neighborhood); `rlhf-audit.tsx:154`'s `rating <= 2` "% Insatisfação" KPI is a distinct, intentional Group 6 KPI, not leftover threshold logic.
- **Scope discipline confirmed:** no edits outside the five declared files; other uncommitted diffs in the tree belong to the concurrent `wish/api-provider-settings` branch (same governance note as Group 1).

### Group 3 execution review — 2026-08-26 (SHIP)

- **Verdict:** SHIP
- **Reviewer:** independent `genie:reviewer` subagent (agentId `a507f127a76f35a01`)
- **Verified:** all 5 acceptance criteria met via 4 new non-vacuous tests in `TestSubjectTenantIdDerivation` (`tests/api/test_rlhf.py:296-379`) that inspect the raw `Event` row — valid UUID + existing Tenant persisted; missing falls back; non-UUID slug falls back and is never persisted; syntactically-valid-but-orphan UUID falls back. `Event.tenant_id` confirmed untouched (still always `tenant.tenant_id`) in every path. Single-query Tenant-existence check (`SELECT Tenant.id WHERE id = :candidate`), no N+1. `poetry run pytest tests/api/test_rlhf.py -q` → 21 passed (17 prior + 4 new).
- **Scope discipline confirmed:** only the `event_data` derivation block inside `submit_rating` and the new test class; `has_reason`/`triggers_analysis`, `_build_analysis_prompt`, `_serialize_rating_event`, `list_ratings`, `update_rating_action` all untouched.
- **MEDIUM security finding, pre-existing and out of Group 3's charter (flagged for user decision, not fixed here):** `_analyze_rating_async` (`rlhf.py:158-167`, call site `:391`) receives the raw, unvalidated `body.clinic_tenant_id` and uses it directly as `resolve_tenant_id` to load LLM credentials via `_load_runtime_llm_credentials`, **without** the Tenant-existence check Group 3 just added to `submit_rating`. A caller in tenant A who sends tenant B's real UUID as `clinic_tenant_id` causes the async analysis task to load and use tenant B's LLM credentials. Predates this wish (introduced by commit `2760e3e`). Not part of any declared group's deliverables.

### Group 3 security-fix review — 2026-08-26 (SHIP)

- **Verdict:** SHIP
- **Disposition:** user explicitly chose "fix it now as part of Group 3" for the MEDIUM finding above, rather than deferring or tracking separately.
- **Fix:** `_analyze_rating_async` (`src/api/routes/rlhf.py:129-260`) now mirrors `submit_rating`'s hardening — `clinic_tenant_id` must (a) parse as a UUID and (b) match a real `Tenant` row before it is trusted as `resolve_tenant_id`. Priority order preserved: explicit `clinic_tenant_id` (validated) > `message_id` event lookup > submitter's own `tenant_id`. A well-formed-but-orphan UUID, a non-UUID string, or an exception during the Tenant lookup all correctly fall through to the next priority tier rather than silently trusting the caller-supplied value.
- **Tests:** new `TestRatingAnalysisClinicTenantIdValidation` (`tests/api/test_rlhf.py:598-829`, 4 tests) covers: valid+existing clinic_tenant_id used; orphan UUID falls through to submitter (no message_id); orphan UUID falls through to message_id lookup (proves genuine fall-through, not just "stays on submitter" — seeds a *different* real tenant behind message_id and asserts resolution lands there); non-UUID input ignored.
- **Test-infra note:** a genuine cross-event-loop deadlock was found and fixed during validation — `LifespanTestClient` (the `client` fixture) runs its lifespan on a dedicated background thread with its own loop; mixing that with `_analyze_rating_async` awaited directly on the test's own pytest-asyncio loop self-deadlocked (confirmed via `faulthandler`/SIGABRT thread dumps). Fixed with a dedicated `db_ready` fixture (`tests/api/test_rlhf.py:607-628`) that calls `init_database()`/`close_database()` directly on the test's own loop, avoiding `LifespanTestClient` entirely for this class. `poetry run pytest tests/api/test_rlhf.py -q` → 25 passed, confirmed stable across 5 consecutive runs (~1.4s each).
- **Reviewer:** independent `genie:reviewer` subagent (agentId `a2f4ef8ffc17a74aa`) — SHIP, re-derived the priority-order logic independently from the source (confirmed no silent-trust bypass on the exception path), confirmed all 4 tests are non-vacuous, and confirmed `db_ready` doesn't leak DB state into later tests. Only LOW findings: (1) the outer exception handler around the Tenant lookup logs but doesn't surface a metric on persistent DB errors; (2) `test_non_uuid_clinic_tenant_id_falls_through` omits `db_ready` (harmless today since the non-UUID path short-circuits before any DB call, but fragile if that ordering changes); (3)-(4) missing explicit coverage for `clinic_tenant_id=""` and for the Tenant-lookup-raises path (both already behave safely, just untested). None blocking.
- **Full-suite delta:** `poetry run pytest tests/ -q` → 222 failed / 1083 passed, vs the 218-failure baseline recorded in the Group 1 review above. The reviewer independently checked the +4 delta is pre-existing order-dependent flakiness (a "no current event loop in thread MainThread" cascade starting in `tests/api/test_policies.py`, which runs before `test_rlhf.py` and which this session never touched) sweeping up async tests including some of our new ones when unlucky — not a regression introduced by this fix. `tests/api/test_rlhf.py` in isolation is 25/25 stable.

### Group 4 execution review — 2026-08-29 (SHIP, one fix applied during review)

- **Verdict:** SHIP
- **Reviewer:** orchestrator, performing the independent review directly rather than dispatching a `genie:reviewer` subagent. An independent-reviewer dispatch was attempted first and failed on an account-wide session limit (resets ~20h out); the user was asked and explicitly chose orchestrator-performed review over waiting or retrying. Reviewer ≠ engineer still holds — the orchestrator did not write Group 4's implementation, `group4-engineer` did.
- **Verified against source, not the engineer's self-report:** read the full rewritten `list_ratings` (`rlhf.py:490-712`) and `_serialize_rating_event` (`rlhf.py:440-491`) line by line against the wish's 7-step contract — SQL filters only real columns with the super-admin branch's old `tenant_id`/`builder_id`/`join(Tenant)` clauses confirmed removed; exactly 2 batched queries resolve the tenant/builder map (confirmed via `TestListRatingsQueryCount::test_batched_tenant_and_builder_lookups`, which instruments `before_execute` over 50 events across 10 clinics); facets computed before narrowing (`TestListRatingsFacets::test_facets_do_not_shrink_when_builder_selected`); narrowing matches `subject_tenant_id` with fallback to `Event.tenant_id` (`TestListRatingsSubjectFiltering`, both tests); summary computed pre-only-filters and confirmed literally unchanged across all four toggle combinations (`test_summary_unchanged_by_only_filters`); pagination slices last in Python. Spot-read `TestListRatingsResponseShape` (6 tests) and `TestListRatingsTenantIsolation` — all non-vacuous, each seeds real conflicting/adjacent data and asserts both presence and absence. No JSON read operator appears in any SQL clause — `event_data` is only ever read in Python after `session.execute`. Confirmed the BRT→UTC date-window tests (`TestListRatingsDateWindow`, 3 tests) exercise the exact `_to_utc`/midnight-widening convention copied from `trace_service.py`, including the 22h-BRT boundary test that fails without the conversion.
- **Finding (MEDIUM, fixed during this review, not deferred):** the wish's Decision #3 requires `has_reason := len((reason or "").strip()) >= 8` to be "defined once" and reused at every site (analysis trigger, `only_with_reason`, later `accepted_count`/422 guard/written-feedback section) — Group 2's `submit_rating` even left a comment inviting the next group to extract a shared helper (`rlhf.py:356-358` pre-fix). Group 4 instead duplicated the one-liner as a second, independent local `_has_reason(event)` inside `list_ratings` rather than reusing it. Functionally identical today, but exactly the divergence risk the decision calls out, and Group 5/6 would otherwise have added a third and fourth copy. **Fix applied:** promoted `_has_reason(reason: Optional[str]) -> bool` to a module-level function (`rlhf.py:101-105`); `submit_rating` now calls it directly (`rlhf.py:363`); `list_ratings`'s local helper renamed `_row_has_reason(event)` and reduced to extracting the reason string and delegating to the shared predicate (`rlhf.py:667-669`), with its one call site updated (`rlhf.py:705`). Re-ran `poetry run pytest tests/api/test_rlhf.py -q` after the fix: 45 passed, no regressions. Re-ran `poetry run mypy src/api/routes/rlhf.py --ignore-missing-imports`: same 3 pre-existing errors (line numbers shifted by the 5 inserted lines), none in the touched code.
- **Verified safe, no finding:** `_serialize_rating_event`'s and `list_ratings`' `_subject_of`'s `UUID(str(subject_raw))` calls only ever see JSON-decoded values in `event_data` (str/int/float/bool/None/list/dict after the Postgres/SQLite JSON round-trip); `str()` on any of these never raises, so a malformed value always lands in the already-caught `ValueError` from `UUID(...)`, never an uncaught exception.
- **Verified safe, no finding:** the `client` fixture (`tests/conftest.py:178`) is function-scoped, so each new test gets its own in-memory DB — no cross-test pollution risk despite several tests sharing `TEST_TENANT_ID`.
- **Independent validation (orchestrator, before and after the fix):** `poetry run pytest tests/api/test_rlhf.py -q` → 45 passed both times. `poetry run mypy src/api/routes/rlhf.py --ignore-missing-imports` → 3 pre-existing errors both times, confirmed via `git show 4f587ac:src/api/routes/rlhf.py` to predate Group 4 (all three sit inside `_analyze_rating_async`/`submit_rating`, untouched by this group). Full suite: `poetry run pytest tests/ -q` → 222 failed / 1103 passed — the +20 passed matches Group 4's 20 new tests exactly, and the failure count matches the Group 3 security-fix review's already-documented 222 baseline with zero new failures attributable to Group 4.
- **Scope discipline confirmed:** only `_serialize_rating_event`, `list_ratings`, and the has_reason refactor's two call sites changed in `rlhf.py`; `_analyze_rating_async`, `submit_rating`'s body, and `update_rating_action` untouched beyond the one-line `has_reason` call-site swap.

### Group 5 execution review — 2026-08-29 (SHIP)

- **Verdict:** SHIP
- **Engineer:** independent `genie:engineer-standard` subagent (`group5-engineer`, agentId `a2fd76169c678c9cd`), dispatched with the full Group 5 deliverables/ACs and the shared `_has_reason` helper from the Group 4 fix pre-baked into the brief so it wouldn't reintroduce a third copy.
- **Reviewer:** orchestrator, independently against source — a fresh `genie:reviewer` subagent dispatch was not attempted this round per the user's standing decision for this session (see Group 4 review) to have the orchestrator review directly given the session-limit risk; the orchestrator is not Group 5's engineer.
- **Verified against source:** `update_rating_action` (`rlhf.py:764-823`) — `approve` now raises 422 via `_has_reason(data.get("reason"))` (the shared Group 4 predicate, not a new copy) before any mutation, then stamps `data["accepted_by"] = str(tenant.user_id)` and `data["accepted_at"] = datetime.now(timezone.utc).isoformat()`; `ignore` branch untouched, no guard, no stamp. `_serialize_rating_event` (`rlhf.py:445-511`) takes a new `accepted_email_map` param and returns `accepted_by`/`accepted_at`/`accepted_by_email`, all `None` when unstamped. `list_ratings` step 5's `accepted_count` now requires `_is_accepted(event) and _row_has_reason(event)` (`rlhf.py:714`) — cut/facets/filters/pagination confirmed unchanged around it. Step 7 adds exactly one batched `select(User.id, User.email).where(User.id.in_(...))` over the distinct `accepted_by` ids on the already-sliced page (`rlhf.py:739-752`), not the full cut.
- **Tests spot-verified as non-vacuous:** `TestPatchRatingAcceptAudit` (3 tests) — stamps land and are readable via GET keyed off the JWT's actual `sub`; all three reason-floor edge cases (7-char, whitespace-only, missing) 422 and leave `approved_for_training`/`accepted_by` untouched; `ignore` on a reasonless entry still succeeds with no stamp. `TestListRatingsAcceptedByEmail` (3 tests) — batched email resolves correctly; a legacy row seeded with `approved_for_training=True` and no stamp surfaces all three new fields as `None`/"—" with no migration; a 20-user worst-case batching test instruments `before_execute` and asserts `query_count <= 6` while also confirming all 20 emails actually resolved (not a silently-empty batch). `TestListRatingsAcceptedCount` (1 test) — mixes 3 qualifying-accepted, 2 legacy no-reason-accepted, 1 short-reason-accepted, and 1 unapproved-with-reason row, and asserts `summary.accepted_count == 3 == len(only_with_reason&only_accepted entries)`.
- **Verified safe, no finding:** all three new test classes exercise the HTTP surface only (`client.get/post/patch`), per the engineer's own design note — none mix `client._submit` with a direct `await`, so the Group 3 cross-event-loop deadlock class of bug does not apply here.
- **Independent validation (orchestrator):** `poetry run pytest tests/api/test_rlhf.py -q` → 52 passed (45 + 7 new). `poetry run mypy src --ignore-missing-imports` (full-`src` scope, per this group's validation command, since the serializer's signature changed) → 366 errors, same 3 pre-existing `rlhf.py` errors as Group 4 (lines shifted only), confirmed unrelated to this group's diff. Full suite: `poetry run pytest tests/ -q` → 223 failed / 1124 passed. Investigated the +1 failed / +21 passed delta against Group 4's 222/1103 baseline: the one newly-failing test beyond baseline membership churn, `TestRatingAnalysisRuntimeCredentials::test_analyze_marks_failed_when_no_credentials`, plus 4 more `test_rlhf.py` tests that failed in this specific run, all show the identical pre-existing signature (`RuntimeError: There is no current event loop in thread 'MainThread'`, `pytest_asyncio/plugin.py` → `asyncio.get_event_loop()`) already documented in the Group 3/4 reviews as an order-dependent cascade from `tests/api/test_policies.py`. All 5 are pre-existing async-direct-call tests (`TestRatingAnalysisRuntimeCredentials`, `TestRatingAnalysisClinicTenantIdValidation`) that this file already flagged as susceptible; none are Group 5's new tests, which are HTTP-only and unaffected. `tests/api/test_rlhf.py` in isolation: 52/52 passing, zero flakiness observed across the runs performed this session.
- **Scope discipline confirmed:** only `update_rating_action`, `_serialize_rating_event`, and `list_ratings`' `accepted_count`/step-7 additions changed in `rlhf.py`; the 7-step filter/facet/narrowing contract from Group 4 is untouched.

### Group 6 execution review — 2026-08-29 (SHIP)

- **Verdict:** SHIP
- **Engineer:** independent `genie:engineer-complex` subagent (`group6-engineer`, agentId `a0d7ffb94d6fb95ca`).
- **Reviewer:** independent `genie:reviewer` subagent (agentId `a8b8f01c67c0f2a2f`) — a fresh reviewer dispatch was attempted this round and succeeded without the session-limit error seen for Group 4, so this group returns to the normal reviewer≠engineer subagent flow rather than orchestrator-direct review. The orchestrator additionally re-verified the reviewer's own findings independently before accepting the verdict (see below), consistent with "trust but verify" applying to reviewer subagents too, not just engineers.
- **Reviewer's verified findings (re-checked line-by-line by the orchestrator against the actual diff, not taken on faith):** all 12 acceptance criteria traced to source with line citations — Builder/Clínica dropdowns fed only by `facets` (`rlhf-audit.tsx:322-372`), zero calls to `/admin/builders` or `/admin/clinics` (grep confirmed exit 1), client-side clinic narrowing via `facets.clinics[].builder_id` with null-builder clinics hidden once a builder is selected (`rlhf-audit.tsx:303-306`), `KpiCards`/`DistributionChart` taking `summary` as their sole prop with no read of `entries` (`rlhf-audit.tsx:164,226`), exactly one `customFetch` call per filter-state change always carrying `only_with_reason=true` with `only_accepted=true` added only when toggled (`rlhf-audit.tsx:548-573`), the accept button gated by a local `hasQualifyingReason` (`MIN_REASON_LENGTH = 8`, mirroring the backend's `_has_reason`) and rendered exclusively inside `WrittenFeedbackList` (`rlhf-audit.tsx:73-77,489-500`), accepted entries showing `accepted_by_email`/formatted `accepted_at` and never a raw UUID (`:481-482`), and the `RATING_META` labels unchanged with the grep guard passing (exit 1).
- **Orchestrator's own independent re-verification (not just re-reading the reviewer's report):** read the entirety of the rewritten `rlhf-audit.tsx` (664 lines) and `rlhf-audit.test.tsx` (267 lines) directly; ran `git status`/`git diff` myself to confirm the file scope (`dashboard.tsx` modified, `rlhf-audit.tsx` modified, `date-range-picker.tsx` and `rlhf-audit.test.tsx` new — nothing else touched); confirmed `dashboard.tsx`'s diff is a clean, byte-for-byte extraction (one import line added, the local `DateRangePicker` function deleted, nothing else) and that `date-range-picker.tsx` is a verbatim copy of the original function plus its two imports; independently re-ran the label guard and the `/admin/builders|admin/clinics` guard myself (both exit 1, confirming zero matches). Read the test fixture in `rlhf-audit.test.tsx:49-134` myself and confirmed the KPI-non-derivation test is genuinely non-vacuous: `summary.total=41`/`average=3.7`/`accepted_count=4` are deliberately impossible to produce from the 3-entry fixture page (which would yield total=3, average=2.0), so the assertions at `:192-203` could only pass if the KPIs read from `summary`.
- **Findings (both LOW, informational, not blocking — matches reviewer's report, independently confirmed):** (1) `WrittenFeedbackList`'s list-membership filter (`rlhf-audit.tsx:395`, `reason.trim().length > 0`) is looser than the `has_reason` (`>= 8`) predicate from Decision #3 — inert in practice since the request always sends `only_with_reason=true` so the backend already excludes short-reason entries, and the accept button's own gate correctly uses the strict `>= 8` threshold; noted for consistency, not required by any acceptance criterion. (2) the clinic-name fallback for a missing `subject_clinic_name` shows a truncated tenant UUID (`:463`) — AC's "não um UUID" language targets the `accepted_by` field specifically (correctly email/"—" there), so this is a judgment call rather than a defect.
- **Independent validation (orchestrator, re-run from scratch, not reused from either subagent's report):** `cd frontend/artifacts/neurostack && npm run typecheck` → 0 errors. `npm run test` → 60/60 passed (7 files), including `rlhf-audit.test.tsx` (6/6). Plain `npm run build` fails at `vite.config.ts:10` ("PORT environment variable is required") — confirmed via reading `vite.config.ts:7-27` that this is a pre-existing hard guard unrelated to this diff; `PORT=3000 BASE_PATH=/ npm run build` succeeds (`built in 1m 16s`, `dist/public/` emitted). Label guard: `grep -rn "Boa resposta\|Ótima" src/components/rating-block.tsx src/pages/rlhf-audit.tsx; test $? -eq 1` → exit 0 (i.e. grep found nothing, guard satisfied).
- **Scope discipline confirmed:** no backend file touched (`src/api/routes/rlhf.py` untouched by this group, consistent with the group's stop condition); only the four declared frontend files changed.
- **This is the final group of the wish.** All 6 execution groups (1-5 previously, 6 here) are now reviewed SHIP and validated independently by the orchestrator.

- **Verdict:** SHIP
- **Reviewer:** a68c6e2967ec9f1bd (round 2); round 1 was a68... predecessor `ae42df47fb44c36d9` (FIX-FIRST)
- **Round 1 (FIX-FIRST)** raised three MEDIUM and two LOW findings, all fixed before round 2: the read-side fallback criterion sat in Group 3 which cannot implement it while Group 4 never stated it (risking a filter that silently drops every pre-change rating); Groups 4/5 consumed `has_reason` without declaring Group 2 as a predecessor; Group 6 rewrote `rlhf-audit.tsx` without depending on Group 2 or protecting its labels; plus a missing `only_accepted` criterion and an unreachable label check.
- **Round 2 (SHIP)** verified all five fixes, confirmed the DAG (1←∅; 2←1; 3←1; 4←2,3; 5←4; 6←2,4,5) is acyclic and consistent with the wave table, and confirmed every validation command resolves in this repo.
- **Post-SHIP delta:** the reviewer's two LOW parity suggestions were applied — the label guard was appended to Group 6's chain, and both guards hardened from `! grep` to `{ grep …; test $? -eq 1; }` so a renamed path (grep exit 2) fails instead of passing vacuously. Exit semantics verified live: labels present → 1, absent → 0, bad path → 1.

**Design review evidence consumed:** SHIP, digest `aa909302f803f7e9a7c93a462fcf54f4c54e658748910986992013a3b274749a`, reviewer `a7719efd8a17e8d54`, verified immediately before scaffolding.

---

## Files to Create/Modify

```
# Backend
src/api/routes/rlhf.py                                   # M — remove 2 endpoints + schemas; ordem de execução; filtros; facetas; resumo; serializer; prompt
src/api/routes/admin.py                                  # M — remove GET /v1/admin/rlhf/pending
tests/api/test_rlhf.py                                   # M — remove blocos órfãos; ajusta leitura de lista crua; cobre atribuição, janela e aceite
tests/api/test_admin.py                                  # M — ajusta o teste do endpoint removido

# Frontend
frontend/artifacts/neurostack/src/pages/chat.tsx                       # M — remove FeedbackBlock (definição + 2 usos + legenda demo)
frontend/artifacts/neurostack/src/pages/rlhf-audit.tsx                 # M — remove aba/seletor/SuggestionEntry/Exportar Dataset; reescreve o relatório
frontend/artifacts/neurostack/src/pages/dashboard.tsx                  # M — passa a importar o DateRangePicker extraído
frontend/artifacts/neurostack/src/components/rating-block.tsx          # M — needsReason <= 3, canSubmit >= 8, labels
frontend/artifacts/neurostack/src/components/date-range-picker.tsx     # A — extraído de dashboard.tsx:88
frontend/artifacts/neurostack/src/components/rating-block.test.tsx     # A — caixa de motivo e habilitação do envio
frontend/artifacts/neurostack/src/pages/rlhf-audit.test.tsx            # A — dropdowns por faceta, KPIs e visibilidade do aceite
```
