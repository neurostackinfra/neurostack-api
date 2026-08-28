# Design: Relatório de Auditoria de Notas (1-5)

| Field | Value |
|-------|-------|
| **Slug** | `rating-audit-report` |
| **Date** | 2026-08-25 |
| **WRS** | 100/100 |

**Extends:** [audit-rlhf-rating](../audit-rlhf-rating/DESIGN.md), [rating-in-trace-detail](../../wishes/rating-in-trace-detail/WISH.md)

## Problem

O simulador oferece dois canais de feedback concorrentes — a nota 1-5 e o fluxo "Corrigir / Enviar Feedback" — e o painel master não consegue responder as perguntas básicas sobre as notas recebidas: quantas foram, como se distribuem, qual a média, recortadas por builder, clínica e período.

Importa porque o sinal de qualidade da resposta LLM é a matéria-prima do alinhamento do agente, e hoje ele chega distorcido em três frentes: dividido em dois formatos; com os KPIs calculados no cliente sobre uma página de 100 linhas, de modo que o "total" mostrado ao master mente assim que existirem mais de 100 notas; e sem qualquer registro de qual clínica é o **assunto** da nota — `submit_rating` grava o evento sob o tenant de quem avaliou (`rlhf.py:433`), não da clínica cujo agente produziu a resposta.

## Scope

### IN

**Simulador — remoção do fluxo de sugestão**
- Remover `FeedbackBlock` de `chat.tsx` (definição em `:147` e os dois usos em `:416` e `:801`), incluindo a legenda "(Apenas demonstração, feature em breve)" do path demo
- Remover `POST /v1/rlhf/validate` e `POST /v1/rlhf/feedback` de `src/api/routes/rlhf.py`, junto dos schemas órfãos (`ValidateRequest`, `ValidateResponse`, `ViolationItem`, `FeedbackRequest`, `FeedbackResponse`) e do import `get_policy_engine`
- Remover `GET /v1/admin/rlhf/pending` de `src/api/routes/admin.py:543`
- Remover a aba "Sugestões RLHF" (`SuggestionsTab`) de `rlhf-audit.tsx`, o seletor de abas e o tipo `SuggestionEntry` — a página passa a ter uma visão única
- Ajustar os testes que exercitam os endpoints removidos (`tests/api/test_rlhf.py`, `tests/api/test_admin.py:128`)
- Ajustar também os testes que consomem `GET /v1/rlhf/ratings` como lista crua, que a mudança de shape quebra mesmo sem endpoint removido: `tests/api/test_rlhf.py:117-121, 141, 176-181, 194-201, 206-212, 246, 400` — dois deles afirmam `isinstance(entries, list)` direto

**Widget de nota — faixa do motivo e labels**
- `components/rating-block.tsx:38`: `needsReason` passa de `selected <= 2` para `selected <= 3`
- `src/api/routes/rlhf.py:410`: `triggers_analysis` passa de `body.rating <= 2` para `body.rating <= 3`
- **Regra única de motivo válido**, idêntica nos dois lados: `len(reason.strip()) >= 8`. `rating-block.tsx:39` passa a exigir isso em `canSubmit` (hoje exige apenas `> 0`) e o backend usa o mesmo limiar para disparar a análise. Sem regra única, a UI coletaria um motivo que o backend descarta em silêncio
- Labels alinhadas em `rating-block.tsx` e `rlhf-audit.tsx`: 1 Péssimo / 2 Ruim / 3 Regular / 4 Bom / 5 Ótimo
- Widget permanece onde já está: simulador (`chat.tsx:793`) e traces (`trace-viewer.tsx:368`, oculto para investor)

**Backend — atribuição do assunto da nota**
- `submit_rating` passa a persistir `subject_tenant_id` em `event_data`, derivado do `clinic_tenant_id` que o frontend já envia (`rating-block.tsx:61`) e que hoje só serve para resolver credencial LLM
- **Validação obrigatória antes de persistir:** só grava o valor recebido se ele fizer parse como UUID **e** casar com uma linha de `Tenant`. Caso contrário grava `tenant.tenant_id`. Não basta testar presença: `trace-viewer.tsx:373` passa `viewingClinicId`, que para um `clinic_admin` é o `clinic_id` de `ClinicConfigTable` (`src/api/routes/auth.py:88-93`) — um identificador de negócio, não o UUID do tenant. As reescritas de `layout.tsx:84-114` que o converteriam rodam só sob `isBuilder` / `isSuperAdmin`, e `trace-viewer.tsx:367` renderiza o widget para todo não-investor. Sem a validação, o slug seria gravado literalmente e nunca resolveria no passo 2. Hoje o problema passa despercebido porque `_analyze_rating_async` engole o valor inválido num `try/except ValueError` (`rlhf.py:255-264`)
- `Event.tenant_id` **não** muda de significado: continua sendo o dono do registro e a base do isolamento por tenant. `subject_tenant_id` é sobre quem a nota fala; os dois divergem sempre que um builder ou super admin avalia no simulador
- Os filtros de clínica e de builder do relatório passam a casar com `subject_tenant_id`; linhas antigas sem o campo caem no fallback `Event.tenant_id`

**Backend — filtros, facetas e agregação**
- `GET /v1/rlhf/ratings` ganha `start_date`, `end_date`, `only_accepted: bool` e `only_with_reason: bool`
- **A janela reusa a convenção de `_apply_filters` (`src/services/trace_service.py:85-100`), que tem três passos não óbvios e precisa ser copiada inteira**, não reinventada: (a) os params são `Optional[datetime]` no FastAPI (como `traces.py:138-142`), aceitando `YYYY-MM-DD` que chega como meia-noite; (b) `end_date` à meia-noite alarga para `23:59:59`, senão o dia final inteiro some — inclusive hoje, que é o `end_date` default do painel; (c) as duas pontas passam por `_to_utc`, que lê datetime ingênuo como BRT (UTC-3) antes de comparar. O passo (c) não é cosmético: `Event.created_at` é `DateTime(timezone=True)` (`src/models/event.py:149-150`), então comparar ingênuo contra tz-aware passa no SQLite dos testes e desloca três horas no Postgres
- **Definição única de "com motivo"**, usada em todo o design: `has_reason := len((reason or "").strip()) >= 8`. É o mesmo limiar da decisão 4 e governa o disparo da análise, o `only_with_reason`, a seção de feedbacks escritos, o `accepted_count` e a guarda 422. Sem uma definição só, "sem motivo" significaria coisas diferentes em cada lugar
- Resposta passa de lista crua para `{ summary, facets, entries }`
  - `summary = { total, average, accepted_count, distribution: { "1": n, ..., "5": n } }`, onde `accepted_count` conta apenas entradas `approved_for_training` **e** com `has_reason` — contar aceites sem motivo faria o KPI superar o acervo navegável. Com `total == 0`, `average` é `0.0` (não `null`): o painel abre num recorte de 30 dias que pode estar vazio, e um número dispensa ramo de renderização
  - `facets = { builders: [{ id, name }], clinics: [{ id, name, builder_id }] }` — os builders e clínicas **presentes no conjunto avaliado**. O `builder_id` em cada clínica é o que permite ao frontend restringir o dropdown de Clínica sem que o servidor encolha as facetas; `builder_id` nulo é possível e significa clínica sem builder
- **Ordem de execução do handler**, numerada para não ficar ambígua:
  1. SQL: filtra `event_type`, isolamento de tenant e janela `start_date`/`end_date` — só colunas reais, sem tocar em `event_data`. "Isolamento de tenant" aqui é **exclusivamente** a cláusula do ramo não-super-admin (`Event.tenant_id == tenant.tenant_id`, `rlhf.py:517`). Os filtros SQL de `tenant_id` e `builder_id` do ramo super-admin (`rlhf.py:506-511`, incluindo o `join(Tenant)`) são **removidos**: eles casam com `Event.tenant_id`, que é o dono do registro, enquanto o recorte do relatório casa com `subject_tenant_id` no passo 4. Manter os dois faria um `AND` entre dono e assunto e devolveria zero linhas justamente no caso principal — a nota que um builder dá no simulador ao agente de uma clínica. Mantém o `order_by(Event.created_at.desc())` de hoje (`rlhf.py:519`)
  2. Python carrega esse conjunto inteiro, preservando a ordem vinda do SQL, e resolve o mapa `tenant_id → (nome, builder_id, nome do builder)` para os `subject_tenant_id` distintos — duas consultas em lote, nunca uma por linha
  3. Python calcula `facets` sobre esse conjunto, antes de qualquer recorte por builder ou clínica
  4. Python aplica o recorte de builder / clínica (casando `subject_tenant_id`)
  5. Python calcula `summary` sobre o resultado do passo 4
  6. Python aplica `only_accepted` e `only_with_reason`
  7. `limit`/`offset` fatiam **depois** do passo 6 — nunca na query, senão a página volta incompleta sem erro nenhum
- Consequência declarada dessa ordem: `summary` mede o recorte builder/clínica/período e **não** reage a `only_accepted` nem a `only_with_reason`, que selecionam o que se navega e não o que se mede. `facets` não encolhe ao escolher um builder, senão o dropdown se auto-esvaziaria
- `_serialize_rating_event` (`rlhf.py:467`) passa a devolver `subject_tenant_id`, `subject_clinic_name`, `builder_name`, `accepted_by`, `accepted_at` e `accepted_by_email`
- `accepted_by_email` resolvido por **uma** consulta em lote `select(User.id, User.email).where(User.id.in_(...))` sobre a página já fatiada, com o mapa passado ao serializer — exibir UUID cru repetiria o vício que este design remove do filtro de builder
- `PATCH /v1/rlhf/ratings/{id}` com `action=approve` carimba `accepted_by` e `accepted_at` (ISO-8601 UTC) além de `approved_for_training`, e **rejeita com 422 entradas que não satisfaçam `has_reason`** — aceitar opinião é um ato sobre feedback escrito
- Reescrever `_build_analysis_prompt` (`rlhf.py:178`) em termo neutro de nota: hoje pergunta "essa insatisfação é legítima?", falso para nota 3. Passa a perguntar se a crítica escrita procede dada a nota atribuída, **mantendo a âncora explícita na metodologia SRM NeuroStack**, que é o que o pedido exige. `_parse_analysis_content` mantém o par `agreed`/`disagreed`
- Atualizar a docstring que cita o limiar antigo — `rlhf.py:404` ("For ratings <= 2") é a única ocorrência de `<= 2` em prosa. O cabeçalho de `tests/api/test_rlhf.py:4` também muda, mas por citar o endpoint removido, não o limiar

**Painel master — relatório**
- Filtros: dropdown Builder, dropdown Clínica (restrito ao builder escolhido), seletor de período — todos com opção "Todos". O painel abre com os últimos 30 dias e **sempre** envia `start_date` e `end_date`; o default vive no painel, não no endpoint
- Dropdowns alimentados por `facets` da própria resposta. Nenhuma chamada a `/admin/builders` ou `/admin/clinics`: as facetas só oferecem opções que retornam resultado, e não têm como omitir um tenant que aparece no total
- O estreitamento do dropdown de Clínica é **client-side**, casando `facets.clinics[].builder_id` com o builder selecionado. O servidor não encolhe `facets` (ver ordem de execução), senão a seleção se auto-esvaziaria e não haveria como voltar atrás
- Clínicas com `builder_id` nulo aparecem no dropdown apenas quando Builder = "Todos"; com um builder selecionado elas ficam fora, e suas notas saem do recorte junto
- Extrair `DateRangePicker` de `dashboard.tsx:88` para `components/date-range-picker.tsx` e consumir nos dois lugares
- KPIs: Total de notas, Nota média, % Satisfação (≥4), % Insatisfação (≤2), Opiniões aceitas
- Gráfico de distribuição 1-5 exibindo contagem **e** percentual por nota
- O painel faz **uma** requisição por estado de filtro, sempre com `only_with_reason=true`: KPIs, gráfico e lista saem todos dessa resposta. É consistente porque `summary` e `facets` são calculados antes desse filtro (passos 3 e 5), então medir o todo e navegar só os feedbacks escritos não se contradizem
- Seção "Feedbacks escritos": as `entries` dessa resposta, cada uma com o texto do cliente, a opinião breve da IA (`ai_analysis`, nos estados `pending` / `done` / `failed`) e as ações
- Ação primária renomeada para "Aceitar e gravar opinião" (era "Aprovar p/ Dataset"), **exibida apenas na seção de feedbacks escritos**; "Ignorar" permanece. Entradas que não satisfazem `has_reason` deixam de oferecer aceite — hoje `rlhf-audit.tsx:452` oferece em qualquer entrada, inclusive no ramo "Sem motivo" de `:429`, o que produziria `accepted_count` maior que o acervo visível
- Entradas aceitas exibem o e-mail de quem aceitou e a data; filtro "Somente aceitas" mostra o acervo
- Remover o botão "Exportar Dataset" (`rlhf-audit.tsx:512`), que hoje não tem `onClick`

### OUT
- Exportação de arquivo (CSV/JSON) do acervo de opiniões — o pedido a coloca explicitamente como "um dia"; ver gatilho em Simplicity Case
- Tabela dedicada para opiniões aceitas — descartada em favor da flag já existente no evento
- Backfill de `subject_tenant_id` em notas já gravadas — caem no fallback `Event.tenant_id`, que é o comportamento de hoje
- Migração dos registros aprovados antes desta mudança (seguem aceitos, sem carimbo e possivelmente sem motivo)
- Leitura dos eventos `rlhf.feedback` já gravados: com `GET /admin/rlhf/pending` removido eles perdem o único leitor e ficam apenas no banco. Consequência aceita da decisão 1
- Pipeline de fine-tuning que consome o acervo
- Notificação (push/email) para notas baixas
- Paginação com scroll infinito — `limit`/`offset` permanecem

## Approach

**Remoção antes de adição.** O primeiro movimento é subtrativo: o fluxo de sugestão sai inteiro — widget, aba, três endpoints, schemas e testes. Isso deixa uma superfície única de feedback e elimina a aba que ficaria sem fonte de dados nova. Alternativa considerada: manter a aba como arquivo histórico. Perdeu porque deixaria três endpoints sem chamador — superfície de manutenção sem dono — e uma aba que só envelhece.

**Consertar a atribuição antes de construir o filtro.** Filtrar por clínica só significa alguma coisa se a nota souber de qual clínica ela fala. Hoje não sabe: `submit_rating` grava sob o tenant de quem avaliou, então a nota que um builder dá no simulador ao agente de uma clínica é contabilizada no tenant HQ do builder. Construir o dropdown sobre esse dado produziria um filtro que parece funcionar e mente. Por isso `subject_tenant_id` passa a ser persistido — o valor já chega na requisição, só era descartado. `Event.tenant_id` fica intocado para não mexer no isolamento por tenant que já está correto (`rlhf.py:502-517`).

**Dropdowns derivados dos dados, não de endpoints de cadastro.** A primeira versão deste design alimentava os filtros com `GET /admin/builders` e `GET /admin/clinics`. Ambos perdem tenants que o relatório conta: `admin.py:621` exige `builder_id` e `is_active`, e `admin.py:161` pula tenants cujos usuários são todos builder/super admin/investor — exatamente os tenants HQ onde as notas de simulador aterrissam. O filtro ficaria incapaz de isolar linhas que ele mesmo soma em "Todos". Derivar as facetas do conjunto carregado fecha o buraco por construção, elimina duas chamadas HTTP e garante que toda opção do dropdown devolve resultado.

**Um endpoint, não dois.** Resumo, facetas e lista viajam juntos. A alternativa era um `/summary` separado. Perdeu por correção, não por economia: com dois endpoints o frontend envia o mesmo conjunto de filtros duas vezes, e qualquer divergência faz o gráfico contradizer a lista embaixo dele.

**Agregar em Python, não em SQL.** `src/repositories/database.py:62` aponta os testes para `sqlite+aiosqlite:///:memory:` e `:23` só remapeia `visit_JSONB` para `visit_JSON` no DDL — a tabela nasce, mas operador de leitura JSON continua território de dialeto, e `(event_data->>'rating')::int` quebraria a suíte. Notas são geradas por humanos clicando em botões: a cardinalidade é baixa.

**A mesma parede vale para todo filtro dentro do payload.** `subject_tenant_id`, `only_accepted` e `only_with_reason` moram em `event_data`, então nenhum deles vira cláusula SQL. Daí a ordem numerada no Scope. O erro silencioso a evitar é paginar na query antes de um filtro aplicado em memória: a página voltaria curta sem levantar exceção. É a mesma armadilha em três lugares, e é por isso que a ordem está fixada como contrato e não como detalhe de implementação.

**Isolamento.** `DateRangePicker` sai de dentro de `dashboard.tsx` e vira componente com uma responsabilidade só — mesmo caminho que `RatingBlock` já percorreu. `rlhf-audit.tsx` perde a aba de sugestões e fica com um propósito único: relatar notas.

## Simplicity Case

- **Simplest complete design:** apagar o fluxo de sugestão; mudar dois limiares de `2` para `3`; persistir um campo que já chega na requisição; acrescentar quatro query params e dois objetos à resposta do endpoint de listagem que já existe; carimbar quem/quando na ação de aceite que já existe; reescrever o corpo de uma página de painel. Nenhuma tabela nova, nenhuma migração, nenhum endpoint novo.
- **Added machinery:** `summary` existe porque os KPIs de hoje são calculados no cliente sobre `limit=100` — o total exibido ao master está errado por construção. `facets` existe porque os endpoints de cadastro omitem justamente os tenants onde as notas de simulador caem, e substitui duas chamadas HTTP em vez de somar uma. `subject_tenant_id` existe porque sem ele o filtro por clínica é decorativo. `accepted_by` / `accepted_at` existem porque um acervo de opiniões sem autoria e data não tem valor de auditoria, que é o propósito declarado do registro. O piso de 8 caracteres é uma condição de uma linha, replicada no frontend para não criar estado divergente.
- **Deferred until measured:** exportação em arquivo, adiada até haver destino concreto (o pedido diz "um dia"); a flag e o filtro já deixam o conjunto identificável, então a exportação é um leitor a mais e não pede mudança de modelo. Agregação em SQL, adiada até o rowset filtrado passar de ~10k linhas. Teto de análises por tenant, adiado até o gatilho do risco 1.
- **Complexity removed:** três endpoints, cinco schemas Pydantic, um componente React de ~210 linhas com máquina de cinco estados (`idle`/`input`/`analyzing`/`red`/`green`), uma aba inteira, um seletor de abas, um botão decorativo, e — pela escolha das facetas — duas dependências de endpoints de cadastro que este relatório não precisa consultar. Descartados também: uma tabela dedicada para opiniões e um segundo endpoint de resumo.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Remoção completa do fluxo de sugestão — widget, aba, `POST /validate`, `POST /feedback`, `GET /admin/rlhf/pending`, schemas e testes | Escolha do usuário entre as opções apresentadas. Endpoint sem chamador é superfície de manutenção e de ataque sem dono |
| 2 | Caixa de motivo abre para notas 1-3 | Pedido explícito. "Regular" carrega crítica acionável que hoje se perde sem texto |
| 3 | Análise IA acompanha a faixa do motivo (≤3 **com** motivo válido) | A opinião da IA julga a crítica escrita; sem texto não há o que julgar |
| 4 | `has_reason := len((reason or "").strip()) >= 8`, definido uma vez e usado em todos os pontos: disparo da análise, `only_with_reason`, seção de feedbacks escritos, `accepted_count` e guarda 422. Mesmo limiar espelhado no `canSubmit` do frontend | Corta a nota de uma palavra ("ruim", "péssimo") sem descartar crítica curta legítima em pt-BR ("não respondeu" tem 13, "preço errado" tem 12). Definição única evita que "sem motivo" signifique uma coisa no gráfico e outra na lista, e evita o estado em que a UI aceita um texto que o backend ignora em silêncio |
| 5 | Labels 1 Péssimo / 2 Ruim / 3 Regular / 4 Bom / 5 Ótimo | Alinha com o pedido; hoje 4 e 5 divergem ("Boa resposta" / "Ótima") em dois arquivos |
| 6 | Persistir `subject_tenant_id` em `event_data`, mantendo `Event.tenant_id` como dono do registro | `rlhf.py:433` grava sob o tenant de quem avalia, então nota de builder no simulador cai no tenant HQ. Sem separar assunto de dono, o filtro por clínica é decorativo. Mexer em `Event.tenant_id` quebraria o isolamento por tenant que já está correto |
| 6b | Só persistir o `clinic_tenant_id` recebido se ele for UUID **e** existir em `Tenant`; senão cair para `tenant.tenant_id` | Para `clinic_admin`, `viewingClinicId` é o `clinic_id` de `ClinicConfigTable` (`auth.py:88-93`), um slug de negócio, e `trace-viewer.tsx:373` o envia como `clinicTenantId`. Testar só presença gravaria o slug e o filtro nunca resolveria |
| 7 | `GET /v1/rlhf/ratings` devolve `{ summary, facets, entries }` num único endpoint | KPIs calculados no cliente sobre uma página mentem. Endpoint único impede que resumo, facetas e lista discordem de filtro |
| 8 | Resumo agregado em Python, não em SQL JSONB | `src/repositories/database.py:62` força SQLite nos testes e `:23` só remapeia o DDL; operador de leitura JSON continua sendo dialeto Postgres |
| 9 | Todo filtro que vive em `event_data` (`subject_tenant_id`, `only_accepted`, `only_with_reason`) roda em Python, e `limit`/`offset` fatiam depois de todos eles | Mesma parede de dialeto da decisão 8. Paginar na query antes de um filtro em memória devolve página incompleta sem erro visível |
| 10 | `summary` mede o recorte builder/clínica/período e ignora `only_accepted` / `only_with_reason`; `facets` é calculado antes do recorte de builder e clínica | "Total de notas" e "nota média" descrevem a população avaliada; deixar um filtro de navegação mexer nesses números tornaria o relatório incomparável entre visões. Faceta que encolhe com a própria seleção deixaria o dropdown sem como voltar atrás |
| 11 | Dropdowns alimentados por `facets`, não por `/admin/builders` nem `/admin/clinics` | `admin.py:621` exige `builder_id` e `is_active`; `admin.py:161` pula tenants só com usuários builder/super admin/investor — os mesmos onde caem as notas de simulador. Os cadastros omitem tenants que o relatório soma. Facetas fecham o buraco e removem duas chamadas |
| 11b | `facets.clinics` carrega `builder_id`; o estreitamento por builder é client-side | O servidor não pode encolher `facets` sem tornar a seleção irreversível, então o dado que permite estreitar tem de viajar junto. O passo 2 já resolve esse `builder_id`, então não custa consulta extra |
| 12 | Período via `start_date` / `end_date` reusando `_apply_filters` de `trace_service.py:85-100` (alargamento do dia final + `_to_utc` BRT). O default de 30 dias é **do painel**, que sempre envia as duas datas; no servidor, data ausente significa sem limite naquele lado | Copiar a convenção em vez de reimplementá-la: os três passos dela existem para não perder o dia final nem deslocar fuso, e reimplementar erraria calado no Postgres enquanto passa no SQLite. O default fica na UI porque é preferência de leitura, não regra de domínio; o servidor mantém o comportamento aberto de hoje. O teto do conjunto carregado é o volume total de notas, que a decisão 8 já aceita como baixo |
| 13 | Relatório usa filtros próprios com opção "Todos", não o `viewingClinicId` do layout | O seletor global força clínica única e auto-seleciona a primeira; um relatório master precisa começar em "tudo" |
| 14 | Extrair `DateRangePicker` de `dashboard.tsx:88` para componente compartilhado | Segundo consumidor; mesmo caminho já aplicado a `RatingBlock` |
| 15 | "Aceitar e gravar opinião" reaproveita a ação `approve` e a flag `approved_for_training`, somando `accepted_by` e `accepted_at` | Consolidação escolhida pelo usuário. Sem migração e sem segundo lugar para consultar; o carimbo é o que faltava para o acervo ter valor de auditoria |
| 16 | Aceite restrito a entradas com `has_reason`, no backend (422) e na UI; `accepted_count` conta só essas | O ato é sobre feedback escrito. Hoje `rlhf-audit.tsx:452` oferece aceite em qualquer entrada, inclusive no ramo "Sem motivo" de `:429`, o que faria o KPI "Opiniões aceitas" ficar maior que o acervo visível — leitura de bug. Excluir do contador também os aceites legados sem motivo mantém KPI e acervo coerentes |
| 17 | Serializer devolve `accepted_by_email` resolvido por uma consulta em lote sobre a página fatiada | Gravar no PATCH sem expor na leitura deixaria o painel sem autoria. Lote, não por linha, senão são até 500 consultas por request |
| 18 | Prompt reescrito em termo neutro de nota, preservando a âncora na metodologia SRM | `rlhf.py:186` pressupõe insatisfação; com nota 3 a pergunta enviesa a opinião gravada. A âncora na metodologia é requisito explícito do pedido |
| 19 | Ação "Ignorar" permanece | Já existe e é o que permite descartar ruído; sem ela toda entrada não aceita fica parecendo pendente para sempre |
| 20 | Remover o botão "Exportar Dataset" | Não tem `onClick` — promete o que não entrega. Exportação está declarada como gatilho futuro |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Nota 3 com motivo passa a disparar chamada LLM; "Regular" tende a ser a nota mais frequente. **Quem paga é a clínica**: `_analyze_rating_async` resolve as credenciais do tenant da clínica (`rlhf.py:232`, `:290`), não uma chave de plataforma | Medium | Análise é assíncrona (`asyncio.create_task`) e não bloqueia a resposta. Guarda imediata: piso de 8 caracteres (decisão 4). Gatilho medível: acima de 200 análises/dia por clínica, adotar teto diário por tenant |
| 2 | Remover os três endpoints quebra a suíte | Medium | `tests/api/test_rlhf.py` (blocos de `/feedback` e `/rlhf/pending`) e `tests/api/test_admin.py:128` precisam ser ajustados no mesmo grupo de execução, não depois |
| 3 | Notas antigas não têm `subject_tenant_id` e caem no fallback `Event.tenant_id` | Medium | É exatamente o comportamento de hoje, então nada regride; mas notas antigas de simulador seguem atribuídas ao tenant HQ do builder. Backfill declarado OUT; a faceta exibirá esse tenant pelo nome real, sem inventar vínculo |
| 4 | Mudar a resposta de `GET /v1/rlhf/ratings` de lista para objeto é quebra de contrato | Low | Único consumidor de produção é `rlhf-audit.tsx:264`; não há cliente externo nem SDK gerado para essa rota. Dentro do repo, porém, sete pontos de `tests/api/test_rlhf.py` leem a resposta como lista e estão listados no Scope. Confirmar por grep na árvore principal antes de aplicar |
| 5 | Registros aprovados antes desta mudança podem não ter motivo nem carimbo | Low | Os sem motivo ficam **fora** do `accepted_count` (decisão 16), então KPI e acervo não divergem; os com motivo entram e exibem autoria "—". Sem migração por decisão explícita (OUT). A guarda 422 vale só para aceites novos |
| 6 | Agregação em memória degrada se o rowset filtrado crescer | Low | Gatilho medível: acima de ~10k linhas filtradas, migrar resumo e facetas para agregação SQL com operador de dialeto |
| 7 | O path demo/investor do simulador (`chat.tsx:416`) perde seu único bloco de interação | Low | É demo read-only rotulada "(feature em breve)"; remover bloco e legenda juntos. Investor segue sem widget de nota, como já é nos traces |
| 8 | Os novos params precisam respeitar o isolamento por tenant existente | Medium | O isolamento é a cláusula do ramo `else` em `rlhf.py:516-517`; o que fica acima dela (`:506-511`) é o filtro por params do ramo super-admin, que sai por completo (passo 1). Todos os novos filtros entram **depois** desse recorte, e `summary` e `facets` são calculados sobre o conjunto já isolado — nunca antes |
| 9 | A resolução do mapa de tenants e de e-mails pode virar N+1 | Medium | Declarado como consulta em lote em dois pontos: mapa de tenants no passo 2 sobre os `subject_tenant_id` distintos, e-mails sobre a página já fatiada. Critério de sucesso conta as queries |
| 10 | Grep de verificação das remoções pode acusar falso positivo | Low | `.worktrees/new-users-metric/` é cópia de worktree do próprio repo e ainda contém as chamadas antigas; verificar apenas na árvore principal, excluindo `.worktrees/` |
| 11 | "Sem regressão" e "sem erros novos" não são decidíveis sem linha de base | Low | Registrar a saída de `npx tsc --noEmit` e da suíte **antes** de tocar no código; os critérios comparam contra esse registro |
| 12 | `clinic_tenant_id` inválido chegando de um `clinic_admin` no trace viewer | Medium | Coberto pela decisão 6b: valida UUID e existência em `Tenant` antes de persistir, caindo para `Event.tenant_id`. Critério de sucesso exercita exatamente esse caminho |

## Success Criteria

**Remoção**
- [ ] `chat.tsx` não contém mais `FeedbackBlock` (definição nem usos)
- [ ] No path autenticado não-investor, o simulador exibe apenas o widget de nota abaixo de cada resposta do agente; no path demo/investor (`chat.tsx:406-427`) não sobra bloco de interação nenhum, por decisão do risco 7
- [ ] `POST /v1/rlhf/validate`, `POST /v1/rlhf/feedback` e `GET /v1/admin/rlhf/pending` retornam 404; nenhum arquivo `.py` ou `.tsx` da árvore principal os referencia (grep excluindo `.worktrees/`)
- [ ] A suíte inteira passa depois das remoções **e** da mudança de shape de `GET /v1/rlhf/ratings`, sem teste órfão apontando para rota inexistente nem asserção que ainda espere lista crua
- [ ] `grep -rn "<= 2" src/api/routes/rlhf.py` não retorna prosa descrevendo o limiar da análise

**Widget e análise**
- [ ] Selecionar nota 3 no simulador e no trace abre a caixa de motivo; selecionar 4 não abre
- [ ] Os labels exibidos são Péssimo / Ruim / Regular / Bom / Ótimo no widget e no painel
- [ ] Com nota 3 e motivo de 7 caracteres, o botão "Enviar avaliação" fica desabilitado; com 8, habilita
- [ ] `POST /v1/rlhf/rating` com `rating=3` e motivo de ≥8 caracteres grava `ai_analysis_status="pending"` e dispara a análise
- [ ] `POST /v1/rlhf/rating` com `rating=3` e motivo de <8 caracteres grava a nota sem `ai_analysis_status` e não dispara análise
- [ ] `POST /v1/rlhf/rating` com `rating=4` não dispara análise, com ou sem motivo
- [ ] O prompt enviado ao LLM não pressupõe insatisfação e ainda cita a metodologia SRM NeuroStack

**Atribuição**
- [ ] `POST /v1/rlhf/rating` com `clinic_tenant_id` de um tenant existente grava `subject_tenant_id` igual a ele, com `Event.tenant_id` inalterado (o tenant de quem avaliou)
- [ ] `POST /v1/rlhf/rating` sem `clinic_tenant_id` grava `subject_tenant_id` igual a `Event.tenant_id`
- [ ] `POST /v1/rlhf/rating` com `clinic_tenant_id` que não é UUID (o caso do `clinic_admin` no trace viewer, que envia o `clinic_id` de `ClinicConfigTable`) grava `subject_tenant_id` igual a `Event.tenant_id` e não persiste o valor recebido
- [ ] `POST /v1/rlhf/rating` com `clinic_tenant_id` que é UUID válido mas não existe em `Tenant` também cai no fallback
- [ ] Uma nota dada por um builder no simulador ao agente de uma clínica aparece no relatório sob **aquela clínica**, não sob o tenant HQ do builder

**Endpoint de listagem**
- [ ] `GET /v1/rlhf/ratings` devolve `{ summary, facets, entries }` com `summary.total` igual ao número de linhas que casam com o recorte, comprovadamente maior que `entries.length` quando houver mais notas que `limit`
- [ ] `summary.average` confere com a média aritmética das notas do recorte, arredondada a 1 casa; num recorte sem notas vale `0.0`
- [ ] Uma nota criada hoje é devolvida quando `end_date` é a data de hoje — o dia final entra inteiro
- [ ] Uma nota criada às 22h BRT do dia D é devolvida com `end_date=D` e não com `end_date=D-1`, provando a conversão BRT→UTC
- [ ] `summary.distribution` soma exatamente `summary.total`
- [ ] `start_date` / `end_date` excluem notas fora da janela, no resumo, nas facetas e na lista
- [ ] Uma nota cujo `Event.tenant_id` é o tenant HQ do builder e cujo `subject_tenant_id` é a clínica X é devolvida tanto sob `tenant_id=X` quanto sob `builder_id` do builder de X — prova que os filtros casam com o assunto e não com o dono, e que as cláusulas SQL de `rlhf.py:506-511` saíram
- [ ] `only_accepted=true` devolve apenas entradas com `approved_for_training=true`; `only_with_reason=true` apenas entradas que satisfazem `has_reason`
- [ ] Uma entrada com motivo de 7 caracteres não aparece sob `only_with_reason=true` e não conta em `accepted_count`, qualquer que seja a nota
- [ ] Com `only_with_reason=true`, `limit=10` e mais de 10 entradas com motivo no recorte, a primeira página devolve 10 entradas — a paginação corre depois do filtro em memória
- [ ] O mesmo vale para `only_accepted=true`
- [ ] `summary` não muda ao alternar `only_accepted` ou `only_with_reason`; apenas `entries` muda
- [ ] `facets` não encolhe ao selecionar um builder — continua listando todos os builders e clínicas do recorte de período
- [ ] Cada item de `facets.clinics` traz `builder_id`, nulo quando a clínica não tem builder
- [ ] `entries` mantém a ordem decrescente por data que o SQL produz
- [ ] Chamado por role não-super-admin, nenhum dos filtros expõe linhas cujo `Event.tenant_id` não seja o do chamador. `facets` pode listar várias clínicas-assunto dessas linhas — um builder vê no dropdown as clínicas que avaliou, ainda que os eventos sejam do seu tenant HQ
- [ ] Uma requisição com 50 entradas de 10 clínicas distintas não dispara mais que 3 consultas além da principal (mapa de tenants, builders, e-mails)

**Aceite de opinião**
- [ ] `PATCH /v1/rlhf/ratings/{id}` com `action=approve` grava `accepted_by` e `accepted_at` além de `approved_for_training`
- [ ] `PATCH` com `action=approve` numa entrada sem motivo retorna 422
- [ ] `GET /v1/rlhf/ratings` devolve `accepted_by`, `accepted_at` e `accepted_by_email` nas entradas aceitas
- [ ] Num recorte com N entradas totais e `limit >= N`, `summary.accepted_count` é exatamente igual ao número de entradas retornadas com `only_with_reason=true&only_accepted=true`, e ambos ignoram aceites legados sem motivo (decisão 16)

**Painel**
- [ ] O painel exibe uma visão única (sem seletor de abas) e não contém a aba "Sugestões RLHF" nem o botão "Exportar Dataset"
- [ ] O filtro de Builder é um dropdown com nomes (não campo de UUID) e inclui a opção "Todos"
- [ ] Selecionar um builder restringe o dropdown de Clínica às clínicas daquele builder
- [ ] Toda clínica que aparece em `summary.total` é selecionável no dropdown, incluindo tenants HQ de builder que `/v1/admin/clinics` omite
- [ ] Alterar qualquer filtro atualiza KPIs, gráfico e lista a partir da mesma resposta
- [ ] Os KPIs exibem Total, Nota média, % Satisfação (≥4), % Insatisfação (≤2) e Opiniões aceitas
- [ ] O gráfico de distribuição mostra contagem e percentual para cada nota de 1 a 5
- [ ] A seção "Feedbacks escritos" lista apenas entradas com motivo, cada uma com o texto do cliente e a opinião da IA nos estados `pending` / `done` / `failed`
- [ ] O botão "Aceitar e gravar opinião" aparece apenas na seção de feedbacks escritos e a entrada passa a exibir o e-mail de quem aceitou e a data — não um UUID
- [ ] O filtro "Somente aceitas" mostra o acervo de opiniões aceitas
- [ ] `components/date-range-picker.tsx` existe e é importado tanto por `dashboard.tsx` quanto pelo painel; o dashboard continua filtrando por período como antes
- [ ] `npx tsc --noEmit` não acusa erro ausente da linha de base registrada antes da mudança

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `aa909302f803f7e9a7c93a462fcf54f4c54e658748910986992013a3b274749a`
- **Reviewer:** a7719efd8a17e8d54
- **Reviewed at:** 2026-08-26T00:59:45.000Z
<!-- genie-design-review:end -->
