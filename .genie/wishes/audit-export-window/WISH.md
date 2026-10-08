# Wish: Janela de Exportação para Auditoria (6 meses / arquivos de 60 dias)

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `audit-export-window` |
| **Date** | 2026-10-07 |
| **Author** | Lorenzo |
| **Appetite** | small |
| **Branch** | `wish/audit-export-window`, em worktree dedicada criada a partir de `origin/main` logo após `git fetch origin` (nunca da árvore compartilhada, que tem mudanças não commitadas em `handoff.py`) |
| **Repos touched** | neurostack-api (backend `src/` + frontend `frontend/artifacts/neurostack`) |
| **Design** | _No brainstorm — direct wish_ |

## Summary

**Problema:** hoje `GET /v1/traces/export` sem datas devolve todo o histórico da clínica e `GET /v1/handoffs/queue/export` aceita 90 dias a qualquer distância no passado, então nenhum dos dois exports respeita a regra de auditoria "até 6 meses atrás, no máximo 60 dias por arquivo".

Os exports de traces (`GET /v1/traces/export`) e de handoffs (`GET /v1/handoffs/queue/export`) passam a obedecer à política de auditoria usada em sistemas de saúde: o usuário consegue buscar dados com retroativo de até **6 meses**, mas cada arquivo exportado cobre no máximo **60 dias**. Ou seja, 6 meses são puxados em "pacotes" de 60 dias. Hoje o export de traces não tem limite nenhum e o de handoffs aceita janelas de 90 dias sem limite retroativo. Clínicas com o add-on **Auditoria Estendida**, ligado por clínica pelo master admin, ficam isentas do limite retroativo de 6 meses (mas continuam limitadas a 60 dias por arquivo). Isso prepara o produto add-on, cuja precificação vem depois.

## Scope

### IN

- Política única de janela de export em `src/services/export_window.py`: `AUDIT_LOOKBACK_MONTHS = 6`, `EXPORT_MAX_DAYS = 60` e uma função pura que valida um intervalo de datas civis da clínica contra a política.
- Coluna `tenants.audit_extended_access` (`bool`, `NOT NULL`, `server_default false`) com migration Alembic; é o add-on por clínica.
- `PATCH /v1/admin/clinics/{clinic_id}` (super admin) aceita `audit_extended_access: Optional[bool]`; listagens de clínicas do master admin expõem o campo.
- `GET /v1/traces/export` aplica a política: retroativo de 6 meses (salvo add-on), no máximo 60 datas civis inclusivas por arquivo e `end < start` rejeitado.
- `GET /v1/handoffs/queue/export` aplica a mesma política; o export passa de 90 para 60 dias e ganha o limite retroativo. O limite de 10.000 linhas permanece.
- Frontend: traces e fila de handoffs bloqueiam no cliente intervalos acima de 60 dias (botão desabilitado + dica), mostram toasts específicos para os novos códigos de erro e exibem uma dica com a regra "até 6 meses atrás, 60 dias por arquivo"; o master admin ganha um switch "Add-on Auditoria Estendida" na linha da clínica (`ClinicRow`), tanto nas clínicas vinculadas a builders quanto nas não vinculadas. Textos em `pt.ts` e `en.ts`.

### OUT

- Venda/cobrança do add-on (catálogo ASAAS, preço, créditos): fica para `asaas-payment-gateway` / `neurostack-credit-system`.
- Política de retenção/expurgo de dados com mais de 6 meses e a ideia de "manter apenas para SLM" (dados antigos só para treino). Isso entra quando a análise de custo for feita. Esta wish **não apaga nada**; ela só limita o acesso pelo export.
- Mudanças na listagem/visualização de traces (`GET /v1/traces`, detalhe, stats, histórico diário) e na tela da fila de handoffs: a fila continua aceitando janelas de 90 dias; só o **export** muda. O limite retroativo de 6 meses vale **só para exports**; as telas continuam mostrando dados com mais de 6 meses, inclusive para clínicas sem o add-on (Decision 11).
- Limite de linhas ou streaming real no export de traces (continua gerando o CSV em memória como hoje).
- Endpoint para o frontend descobrir a janela permitida (data mínima, add-on). O limite retroativo é validado no servidor; veja a Simplicity Case.
- Builder ou admin da clínica alterando o add-on: só o super admin altera.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | 6 meses / 60 dias é o padrão para **todas** as clínicas; o add-on libera acesso **além** de 6 meses | Decisão do usuário (2026-10-07). O custo de manter acesso acima de 6 meses é o que o add-on vai cobrar. |
| 2 | Add-on é uma flag **por clínica** (`tenants.audit_extended_access`), ligada pelo super admin | Decisão do usuário. Não existe catálogo de add-ons hoje; uma coluna tipada é a fronteira limpa que a cobrança futura vai escrever, em vez de uma chave solta no JSONB `config`. |
| 3 | Com o add-on, o limite retroativo **some** (sem teto); o limite de 60 dias por arquivo continua | Nenhum dado é expurgado hoje, então "sem teto" é verdadeiro. Um teto numérico (12/24 meses) depende da análise de custo adiada. Os pacotes de 60 dias protegem o export em memória independentemente do add-on. |
| 4 | 60 dias valem para traces **e** handoffs (handoff cai de 90 para 60) | Decisão do usuário. A fila de handoffs continua aceitando 90 dias na tela; o export rejeita acima de 60. |
| 5 | Datas medidas em **datas civis da clínica**, inclusivas: `end_date − start_date + 1 ≤ 60`. Para traces: datetime **naive** (a UI envia `yyyy-MM-ddTHH:mm` ou `yyyy-MM-dd`) usa a própria parte de data como data civil (`value.date()`), sem conversão; só datetime **com fuso** é convertido para o fuso da clínica (`value.astimezone(zone).date()`). Fuso da clínica = `clinic_config.operations.timezone`, lido com `HandoffRepository.load_clinic_timezone` (`src/repositories/handoff.py:333`) ou consulta equivalente, e resolvido por um resolvedor público exposto em `src/handoff/sla.py` (`resolve_clinic_zone`, envolvendo o privado `_resolve_clinic_zone`, com o mesmo fallback). `OverflowError`/`ValueError` na derivação viram `invalid_date_range` (400, nunca 500) | Igual ao export de handoff (`resolve_clinic_civil_date_range`). A data naive é a mesma que o usuário escolheu na UI, então UI e servidor contam os mesmos dias; converter naive→BRT→fuso da clínica deslocaria um dia fora de BRT. O filtro da consulta de traces continua usando o `_to_utc` existente (naive = BRT); só a contagem de datas segue esta regra. |
| 6 | Limite retroativo = mesma data do mês 6 **meses-calendário** antes de "hoje" na clínica, com o dia limitado ao fim do mês (31/ago → 28 ou 29/fev). `start_date` precisa ser ≥ essa data | "6 meses atrás" no sentido do usuário. A conta de calendário é feita com aritmética stdlib (sem nova dependência) e evita a deriva de uma aproximação fixa como 180 dias. |
| 7 | Códigos de erro (HTTP 400, `detail`): `export_window_60_days`, `export_lookback_6_months`, `invalid_date_range`. O export de handoff **nunca** retorna `max_window_90_days` | Contrato estável que o frontend mapeia para toasts. O código de 90 dias é da tela da fila; expô-lo no export confundiria. |
| 8 | Defaults do export de traces quando faltam datas, iguais ao export de handoff: `start` ausente = início da data civil `hoje_da_clínica − 30` (no fuso da clínica); `end` ausente = agora (data civil = hoje da clínica). Datas parciais mantêm o default do lado que falta; um `end` sozinho anterior ao `start` default retorna 400 `invalid_date_range` | Antes, sem datas, o export trazia **todo** o histórico, o que viola a política. A âncora é a mesma de `resolve_clinic_civil_date_range` (31 datas civis), que cabe nos 60. |
| 9 | Overrides de builder (`tenant_id` alvo) usam a flag da clínica **alvo** | O add-on pertence à clínica cujos dados são exportados. |
| 10 | O frontend valida só os 60 dias (universal); o limite retroativo fica no servidor e aparece como toast | O cliente não sabe se a clínica tem o add-on; uma data mínima no cliente bloquearia indevidamente quem tem o add-on. |
| 11 | O retroativo de 6 meses vale **só para exports**. Listagem, detalhe e stats de traces e a tela da fila de handoffs continuam sem limite retroativo | Decidido pelo usuário 2026-10-07. A wish limita a saída em massa de dados (arquivo), não a consulta na tela; estender o limite às telas fica para quando o add-on for vendido ou a análise de retenção/custo sair (ver Assumptions/Risks). |

## Simplicity Case

- **Simplest complete design:** uma função pura de política + uma coluna booleana por clínica + duas checagens nos endpoints de export existentes + validação de 60 dias e toasts no frontend + um switch no master admin.
- **Added machinery:** (a) a coluna + migration, necessária porque o usuário pediu que a lógica fique atrelada a um add-on por clínica e não há outro lugar tipado para isso; (b) o campo no PATCH de clínica, porque sem ele o add-on não pode ser ligado sem SQL manual.
- **Deferred until measured:** endpoint de "janela permitida" para o cliente pintar a data mínima. Gatilho: reclamação de UX ou a venda do add-on exigir mostrar a data mínima. Teto numérico do add-on e expurgo/"só SLM": gatilho é a análise de custo de retenção. Streaming/limite de linhas em traces: gatilho é um export de 60 dias medido acima de ~30s ou estourando memória.
- **Complexity removed:** nenhum catálogo de add-ons, nenhuma tabela de entitlements, nenhuma data mínima calculada no cliente, nenhuma mudança na janela de 90 dias da fila e nenhum job de expurgo.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [ ] Export de traces com intervalo de 61 datas civis inclusivas retorna 400 `export_window_60_days`; com 60 datas retorna 200 CSV.
- [ ] Export de traces com `start_date` anterior ao limite de 6 meses-calendário retorna 400 `export_lookback_6_months` para clínica sem add-on; a mesma requisição retorna 200 com `audit_extended_access = true`.
- [ ] Export de traces sem datas exporta só a partir de `hoje_da_clínica − 30` (não o histórico inteiro); só `end` anterior a esse default retorna 400 `invalid_date_range`; data impossível de representar (overflow) retorna 400 `invalid_date_range`, nunca 500.
- [ ] Export de handoffs com 61 datas retorna 400 `export_window_60_days` (e com 91+ datas também, nunca `max_window_90_days`); 60 datas retorna 200.
- [ ] Export de handoffs anterior ao limite de 6 meses retorna 400 `export_lookback_6_months` sem add-on e 200 com add-on.
- [ ] `GET /v1/handoffs/queue` (tela da fila) continua aceitando janelas de até 90 dias (sem regressão).
- [ ] Limite de 6 meses no fim de mês: com "hoje" em 31/08 o limite é 28/02 (ou 29/02 em ano bissexto). Coberto por teste unitário da função pura.
- [ ] Super admin liga/desliga `audit_extended_access` via `PATCH /v1/admin/clinics/{id}`, que responde `{"ok": true, "audit_extended_access": <bool gravado>}`; não super admin recebe 403; o campo aparece em `ClinicSlotResponse` (listagem de clínicas) e em `BuilderClinicResponse` (`GET /v1/admin/builders`).
- [ ] Override de builder e override de super admin (`tenant_id` alvo) respeitam a flag da clínica alvo, nos dois exports.
- [ ] Listagem, detalhe e stats de traces e a tela da fila continuam sem limite retroativo (Decision 11).
- [ ] UI de traces e da fila: intervalo de export > 60 dias desabilita o botão de export e mostra a dica; os toasts de `export_window_60_days` e `export_lookback_6_months` aparecem em PT e EN.
- [ ] O master admin mostra e persiste o switch do add-on na linha da clínica (vinculadas e não vinculadas).

## Execution Strategy

**Base do branch:** `git fetch origin` e então uma worktree dedicada para `wish/audit-export-window` criada a partir de `origin/main` (o remoto andou desde a revisão — p.ex. `cda3ae3` mexe em `src/locales/pt.ts` — e a árvore compartilhada tem mudanças não commitadas em `src/api/routes/handoff.py`). Na worktree, rodar `pnpm --dir frontend install --frozen-lockfile` antes do Group 2 (o `package.json` e o `pnpm-lock.yaml` do workspace ficam em `frontend/`; não há `package.json` na raiz).

**Wish no branch:** hoje o `WISH.md` e o allowlist dele no `.gitignore` existem **só na árvore compartilhada** (não estão em `origin/main`). O primeiro commit em `wish/audit-export-window` adiciona `.genie/wishes/audit-export-window/WISH.md` e **apenas** as três linhas de allowlist desta wish (hoje `.gitignore:213-215` na árvore compartilhada: `!.genie/wishes/audit-export-window/`, `.genie/wishes/audit-export-window/*`, `!.genie/wishes/audit-export-window/WISH.md`). Não levar entradas de allowlist de outras wishes.

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 3: migration/coluna nova (stateful +2), multi-endpoint com contrato de erro compartilhado (+1) | `engineer-standard` / high | Política, coluna do add-on, PATCH admin, enforcement nos dois exports |

### Wave 2 (after Wave 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 2: três páginas + i18n, contrato fixado pelo Group 1 (+1); toggle stateful leve (+1) | `engineer-standard` / medium | Validação de 60 dias, toasts, dicas e switch do add-on no frontend |

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

### Group 1: Política de janela + add-on por clínica no backend

**Goal:** os dois endpoints de export aplicam 6 meses retroativos / 60 dias por arquivo, com isenção do retroativo controlada por `tenants.audit_extended_access`.

**Deliverables:**
1. `src/services/export_window.py`: `AUDIT_LOOKBACK_MONTHS = 6`, `EXPORT_MAX_DAYS = 60`, `lookback_floor(local_today: date) -> date` (meses-calendário com dia limitado ao fim do mês, Decision 6) e `validate_export_window(start: date, end: date, *, local_today: date, extended_access: bool) -> None`, que lança `ExportWindowError(code)` com `invalid_date_range`, `export_window_60_days` ou `export_lookback_6_months`, nessa ordem de checagem.
2. `src/models/tenant.py`: `audit_extended_access: Mapped[bool]` com `default=False`, `server_default=false()`, `nullable=False`. Migration Alembic nova, encadeada no head atual de `origin/main` (confira `alembic heads` antes; o Codex commita migrations em paralelo). `tests/api/test_tenant_audit_extended_access_migration.py` (novo) segue o padrão de contrato de `tests/api/test_handoff_sla_migration.py`: carrega o arquivo da migration com `importlib.util.spec_from_file_location`, troca o `op` do módulo por um gravador falso (como `OperationRecorder`) e afirma (a) `revision`/`down_revision` encadeados no head anterior, (b) `upgrade()` adiciona em `tenants` a coluna `audit_extended_access` `Boolean`, `nullable=False`, com `server_default` falso, (c) `downgrade()` remove essa coluna, (d) a coluna do ORM em `Tenant` bate com a da migration. O teste em Postgres real daquele arquivo é opcional aqui.
3. `src/api/schemas/admin.py`: `UpdateClinicRequest.audit_extended_access: Optional[bool] = None`; `ClinicSlotResponse` **e** `BuilderClinicResponse` ganham `audit_extended_access: bool = False`, preenchidos nos dois pontos de construção em `src/api/routes/admin.py` (`ClinicSlotResponse(` ~L73 e `BuilderClinicResponse(` ~L709, que alimenta `GET /v1/admin/builders`). `update_clinic` grava o campo quando não é `None` e passa a responder `{"ok": True, "audit_extended_access": <valor gravado no tenant>}` (contrato fixado). Testes que comparam o corpo inteiro com `{"ok": True}` precisam ser atualizados (ver Deliverable 6, `tests/api/test_password_management.py`).
4. `src/api/routes/traces.py` `trace_export`: resolve a clínica efetiva com o `_resolve_tenant` existente (builder e super admin via `tenant_id`), carrega o fuso (Decision 5: `load_clinic_timezone` ou consulta equivalente, resolvido por `resolve_clinic_zone` público de `src/handoff/sla.py`) e a flag da clínica efetiva. Captura o relógio **uma vez** com `datetime.now(timezone.utc)` usando o `datetime` importado no nível do módulo `traces.py` (para os testes poderem fazer `monkeypatch.setattr(traces_route, "datetime", Frozen…)`); `local_today = now.astimezone(zone).date()`. Aplica os defaults da Decision 8, deriva as datas civis pela regra da Decision 5 (naive → parte de data; com fuso → fuso da clínica), chama `validate_export_window` e mapeia `ExportWindowError` para HTTP 400 `detail=<code>`; `OverflowError`/`ValueError` na derivação viram 400 `invalid_date_range`. No **mesmo** bloco `try`, antes de chamar `export_traces_csv`, a rota converte os dois limites para UTC com a mesma regra de `_apply_filters` em `src/services/trace_service.py` (expansão de `end` à meia-noite para 23:59:59 e depois `_to_utc`, naive = BRT); `OverflowError`/`ValueError` dessa pré-conversão também viram 400 `invalid_date_range` (p.ex. naive `9999-12-31T23:00` passa em `validate_export_window` mas estoura no deslocamento BRT→UTC). É só uma checagem prévia; `_apply_filters` não é refatorado. As datas efetivas (inclusive defaults) são as que vão para `export_traces_csv`. Atualiza a descrição OpenAPI com os códigos e os novos defaults. Listagem, detalhe, stats e histórico diário não mudam (Decision 11).
5. `src/api/routes/handoff.py` `export_handoffs_csv`: resolve o período do export **sem** a checagem de 90 dias da fila (p.ex. um `resolve_export_period` em `src/services/handoff_service.py` que chama `resolve_clinic_civil_date_range` com o relógio do `datetime` de `handoff_service`, como `resolve_queue_period` já faz, e mapeia `OverflowError`/`TypeError`/`ValueError` para `invalid_date_range`; `resolve_queue_period` da tela fica intacto). O "hoje" da clínica vem do período resolvido: `local_today = resolved_period.as_of.astimezone(ZoneInfo(resolved_period.timezone_name)).date()` (campos `as_of` e `timezone_name` de `CivilDateRange` em `src/handoff/sla.py`). A rota **não** chama `route.datetime.now()` antes da consulta (o teste de L194 exige que o relógio do route só seja lido depois da consulta). Carrega a flag da clínica efetiva e chama `validate_export_window(resolved_period.start_date, resolved_period.end_date, ...)`. O export nunca retorna `max_window_90_days`. Atualiza docstring/OpenAPI (`max_window_90_days` sai, entram os novos códigos).
6. Testes. Todo teste novo de janela/retroativo congela o relógio com o padrão existente `monkeypatch.setattr(handoff_service, "datetime", Frozen…)` (handoff, ver `test_export_preserves_legacy_partial_date_defaults`) ou `monkeypatch.setattr(traces_route, "datetime", Frozen…)` (traces), com as fronteiras calculadas no fuso da clínica.
   - `tests/services/test_export_window.py` (novo): fronteiras 60/61, limite exato de 6 meses, 31/08→28/02 e 29/02 bissexto, `end < start`, add-on ignorando o retroativo mas não os 60 dias.
   - `tests/api/test_traces_export_window.py` (novo): 60/61 datas; retroativo com e sem add-on; sem datas = desde `hoje − 30`; só `start` mantém `end` = agora; só `end` anterior ao `start` default → `invalid_date_range`; naive usa a própria data (borda 23:30 não desloca o dia) e com fuso converte para o fuso da clínica; overflow → 400 `invalid_date_range`, incluindo `start_date=end_date=9999-12-31T23:00` naive (estoura na conversão BRT→UTC, nunca 500); override de builder e override de super admin usando a flag da clínica **alvo** (via `_resolve_tenant`); e `test_trace_list_and_stats_ignore_export_lookback` (novo): clínica sem add-on, `start_date` 7 meses antes do "hoje" congelado → `GET /v1/traces` e `GET /v1/traces/stats` retornam 200 (Decision 11).
   - `tests/api/test_handoff_queue_export.py`: casos novos (60/61/91 dias sem `max_window_90_days`, retroativo com e sem add-on, override de builder na clínica alvo, e `test_export_super_admin_override_uses_target_clinic_audit_flag` (novo): super admin com `tenant_id` de clínica alvo com 7 meses de retroativo → 400 `export_lookback_6_months` quando a clínica alvo não tem o add-on e 200 quando tem). `test_export_empty_preserves_bom_headers_defaults_and_window_limit` (L150) é **reescrito** para o contrato de 60 dias: a metade aceita hoje cobre 2026-01-01..04-01 (91 datas) e a rejeitada espera `max_window_90_days`; passa a esperar `export_window_60_days` para esse intervalo (ou reduz a metade aceita a 60 datas e congela o relógio), nunca `max_window_90_days`. Os demais testes existentes com datas fixas que caem antes do limite (L194, L464, L528, L707, L736, L852, L897, L938; L625 a partir de 2026-12-01) são congelados via `handoff_service.datetime` ou semeados com `audit_extended_access=True`; L793 já congela. No L194, congelar `handoff_service.datetime` não interfere no `ExportDatetime` do route.
   - `tests/api/test_traces_daily_history.py`: `test_export_still_supports_a_multi_day_range` (2026-09-27..28, quebra a partir de 2027-03-28) congela `traces_route.datetime` ou semeia o add-on. `test_export_omits_intent_category_and_action_columns` (L427) chama `/v1/traces/export` sem datas com um evento em 2026-09-28; pelo default de 30 dias (Decision 8) o evento sai da janela a partir de 2026-10-29. Esse teste congela `traces_route.datetime` com "hoje" dentro da janela de 30 dias do evento (p.ex. 2026-10-01).
   - `tests/api/test_admin.py`: PATCH super admin liga/desliga e resposta com `audit_extended_access`; 403 para não super admin; campo em `ClinicSlotResponse` e em `BuilderClinicResponse`.
   - `tests/api/test_password_management.py`: a asserção de L249 `clinic_changed.json() == {"ok": True}` passa a `{"ok": True, "audit_extended_access": False}`.
   - `tests/api/test_handoff_queue.py`: regressão de 90 dias na tela da fila; e `test_queue_ignores_export_lookback_without_addon` (novo): clínica sem add-on, `from_date` 7 meses antes do "hoje" congelado (`handoff_service.datetime`) → `GET /v1/handoffs/queue` retorna 200 (Decision 11).

**Acceptance Criteria:**
- [ ] Todos os Success Criteria de backend acima passam nos testes novos.
- [ ] Todo teste de export existente cujas datas fixas caem antes do limite de 6 meses (handoff e traces) foi congelado no relógio ou semeado com `audit_extended_access=True`; todo teste existente de export **sem datas** cujas linhas caem fora da nova janela default de 30 dias (Decision 8; p.ex. `test_export_omits_intent_category_and_action_columns`) congela o relógio com "hoje" dentro dessa janela; testes que esperavam `max_window_90_days` ou janelas de 61–90 dias no export foram atualizados para o novo contrato. Nenhum teste foi removido, e a tela da fila mantém 90 dias.
- [ ] `tests/api/test_tenant_audit_extended_access_migration.py` prova upgrade (coluna `NOT NULL`, default falso) e downgrade (coluna removida); há exatamente um head Alembic.
- [ ] Baseline medida no ponto de partida do branch: `poetry run pytest -q tests/ | tail -1` registrado nas notas de execução junto com o SHA de `origin/main` e o nome de cada teste que falha. Se a baseline estiver verde, `make quality` sai com 0. Se não, o gate é rodar os pré-requisitos de `quality` exceto `test` no Makefile do branch (em `origin/main` hoje: `make format-check lint typecheck`; inclua `typecheck-poc-runner` se o alvo existir) saindo com 0 mais um resumo do pytest sem nenhuma falha além das registradas e com contagem de aprovados ≥ a registrada.

**Validation:**
```bash
# Escopo: coluna nova + migration + dois endpoints compartilhados → focados + gate completo do repo
# Antes de qualquer mudança (ponto de partida): registrar SHA + linha final + falhas nas notas de execução
git rev-parse origin/main && poetry run pytest -q tests/ | tail -1
# Depois das mudanças:
poetry run pytest tests/services/test_export_window.py tests/api/test_traces_export_window.py tests/api/test_traces_daily_history.py tests/api/test_handoff_queue_export.py tests/api/test_handoff_queue.py tests/api/test_admin.py tests/api/test_password_management.py tests/api/test_tenant_audit_extended_access_migration.py -v
poetry run alembic heads | wc -l | grep -qx 1
make quality   # se a baseline estava verde; senão: make format-check lint typecheck (+ typecheck-poc-runner se existir) && poetry run pytest -q tests/ | tail -1 (comparar com a baseline)
```

**depends-on:** none

---

### Group 2: Frontend: limites de export, toasts e switch do add-on

**Goal:** o usuário entende e respeita a regra "até 6 meses atrás, 60 dias por arquivo" nas telas de traces e da fila, e o super admin consegue ligar o add-on por clínica.

**Deliverables:**
1. `src/pages/traces.tsx`: calcula os dias civis inclusivos entre `exportStart` e `exportEnd` usando só a parte de data (`yyyy-MM-dd`) de cada valor `yyyy-MM-ddTHH:mm`, a mesma regra do servidor para datetimes naive (Decision 5), para UI e servidor contarem os mesmos dias; acima de 60, desabilita o botão de export e mostra uma dica inline; texto de ajuda fixo com a regra; ao receber 400, lê `detail` e mostra toast específico para `export_window_60_days` / `export_lookback_6_months` (fallback para o toast genérico atual).
2. `src/pages/handoff-queue.tsx`: com as duas datas selecionadas e intervalo acima de 60 dias civis inclusivos, o botão de export fica desabilitado com dica, enquanto a fila continua carregando até 90. Com intervalo parcial (só uma data), o cliente não bloqueia e o servidor decide (defaults da Decision 8); um eventual 400 aparece como toast. Os toasts dos novos códigos ficam ao lado do de `export_limit_10000_exceeded`.
3. `src/pages/master-admin.tsx`: não existe diálogo de edição de clínica; a edição inline (~L1646–1691) só cobre clínicas vinculadas a builders e as não vinculadas (~L1731–1745) não têm caminho de edição. O switch "Add-on Auditoria Estendida" vai no próprio `ClinicRow`, visível com `canManage` (já `isSuperAdmin`), nas **duas** listas (vinculadas e não vinculadas). `audit_extended_access` entra nos tipos `ClinicSlot`, `BuilderClinic` e `ClinicRowData`; o switch inicia pelo valor da listagem, envia `PATCH /v1/admin/clinics/{id}` com `{ audit_extended_access }` e, no sucesso, grava o valor devolvido pela resposta em `setClinics` **e** `setBuilders` (como `handleToggleClinicStatus` faz em ~L1048–1051); em erro, toast e estado inalterado; `isInvestor` segue bloqueado como nas outras ações.
4. `src/locales/pt.ts` e `src/locales/en.ts`: chaves para dica, regra, os dois toasts e o rótulo/descrição do switch.
5. Testes: `traces.test.tsx`, `handoff-queue.test.tsx` e um teste do master admin (novo arquivo `master-admin-audit-addon.test.tsx` ou `master-admin-builder-clinic-search.test.tsx`) cobrindo botão desabilitado com 61 dias e habilitado com 60 (traces com horários que não mudam a contagem por data), handoff com intervalo parcial não bloqueado, toasts por código, e o switch do add-on numa clínica vinculada **e** numa não vinculada (PATCH com `audit_extended_access` e estado refletido nas duas listas).

**Acceptance Criteria:**
- [ ] Intervalo de 61 dias desabilita o export nas duas telas; 60 habilita; a contagem do cliente usa a parte de data, como o servidor.
- [ ] 400 `export_lookback_6_months` e `export_window_60_days` mostram toasts específicos em PT e EN.
- [ ] A fila de handoffs continua listando janelas de até 90 dias.
- [ ] O switch do master admin, na linha da clínica (vinculadas e não vinculadas), persiste o valor e reflete o estado retornado.

**Validation:**
```bash
# Escopo: três páginas + locales; vitest focado + typecheck do pacote (locales são tipados)
# Pré-condição na worktree: pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/traces.test.tsx src/pages/handoff-queue.test.tsx src/pages/master-admin
pnpm --dir frontend/artifacts/neurostack run typecheck
```

**depends-on:** Group 1

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Rodar `alembic upgrade head` no DB de dev antes de testar (o DB de dev não roda migrations no boot; sem isso a coluna falta e tudo vira 500).
- [ ] Traces: exportar 60 dias funciona; 61 dias fica bloqueado na UI; forçar via URL retorna 400 `export_window_60_days`.
- [ ] Traces/handoffs: início há 7 meses sem add-on mostra toast de retroativo; com o add-on ligado no master admin, o mesmo export baixa o CSV.
- [ ] Fila de handoffs com 90 dias continua listando; só o botão de export bloqueia.
- [ ] Builder exportando de clínica gerenciada respeita a flag dessa clínica.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Datetimes naive de traces são filtrados como BRT (`_to_utc`), mas o fuso da clínica pode ser outro; a borda do filtro pode deslocar algumas horas | Low | A **contagem** de datas usa a parte de data do valor naive (Decision 5), igual à UI, então a validação não desloca o dia; o filtro de linhas mantém o comportamento existente. |
| Telas (listagem, detalhe e stats de traces; fila de handoffs) continuam mostrando dados com mais de 6 meses para clínicas sem o add-on | Low | Decisão do usuário (Decision 11): o limite vale só para exports. Gatilho para estender às telas: venda do add-on ou conclusão da análise de retenção/custo. |
| Consumidores externos do export de traces sem datas passam a receber 30 dias em vez de tudo | Low | O único consumidor conhecido é o frontend, que sempre envia datas; mudança documentada na OpenAPI. |
| O Codex adiciona migration em paralelo e gera dois heads | Medium | A validação exige exatamente um `alembic heads`; rebase no `origin/main` antes de gerar a revisão. |
| Testes de export com datas fixas passam a falhar conforme o relógio real avança além do limite de 6 meses | Medium | Todo teste de export com data fixa congela o relógio (`handoff_service.datetime` / `traces_route.datetime`) ou semeia o add-on (Group 1, Deliverable 6). |
| Clínicas acostumadas a exportar handoffs de 90 dias perdem essa opção | Low | Decisão explícita do usuário; o toast explica a regra dos 60 dias. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### 2026-10-07 — Plan review #1 — FIX-FIRST

- **Target:** `.genie/wishes/audit-export-window/WISH.md` (uncommitted, gitignored; sha256 `ec887a1b…fdcf3`)
- **Commands:** `poetry run alembic heads` → `021 (head)`; `pytest tests/core/test_config_service.py -k TestSchedulingLinkPatchBoundary` → 2 passed (memory baseline stale); `git check-ignore` → wish ignored by `.gitignore:197`.
- **HIGH-1:** the 6-month floor breaks ~9 existing fixed-date tests in `tests/api/test_handoff_queue_export.py` (L150/194/464/528/707/736/852/897/938; L625 from 2026-12-01) and `tests/api/test_traces_daily_history.py` (from 2027-03-28); the L194 test patches `route.datetime.now`. Fix: derive "today" from `resolved_period.as_of`/`timezone_name` in handoff export; trace route takes now from module `datetime`; freeze clock in window tests; reword G1 AC2; add `test_traces_daily_history.py` to validation.
- **HIGH-2:** no clinic "edit dialog" — inline `ClinicRow` editing only for builder-linked clinics (fed by `BuilderClinicResponse`); unlinked clinics have no edit path; PATCH returns `{"ok": true}`. Fix: field on both `ClinicSlotResponse` and `BuilderClinicResponse`; switch on `ClinicRow` for both lists; update `setClinics`/`setBuilders`.
- **MEDIUM-1:** `make quality` vs "known baseline" contradiction; pin measured pass line at branch point.
- **MEDIUM-2:** migration criterion unverified; add contract test like `test_handoff_sla_migration.py`.
- **MEDIUM-3:** trace civil-date derivation underspecified (naive→BRT→clinic zone shifts a day off-BRT; private `_resolve_clinic_zone`; overflow → 500). Fix: naive input uses its own date part; name tz source; public zone resolver; map overflow to `invalid_date_range`.
- **MEDIUM-4:** 6-month limit is export-only; trace list/detail and queue still show older data — confirm with user.
- **LOW-1..6:** Decision 8 default anchor; trace builder-override tests; pnpm instead of npx; branch base = fresh worktree from `origin/main` (remote now `cda3ae3`); gitignore allowlist lines; handoff partial-range wording + one-sentence problem statement.

### 2026-10-07 — Plan review #2 (after fix loop 1) — FIX-FIRST

- **Target:** WISH.md sha256 `d766f47d…dff4b`, verified against remote main `cda3ae3`.
- **Commands:** `git check-ignore -q` → exit 1 (tracked via `.gitignore:213-215`); `poetry run alembic heads` → `021 (head)`; read-only greps of routes/services/tests/pages.
- **Review #1 gaps:** HIGH-2, MEDIUM-1..4, LOW-1..6 resolved; HIGH-1 incomplete.
- **HIGH-1:** `tests/api/test_traces_daily_history.py:427` `test_export_omits_intent_category_and_action_columns` exports without dates (event 2026-09-28) and breaks from 2026-10-29 under Decision 8. Fix: list it in G1 D6 (freeze `traces_route.datetime`) and widen G1 AC2 to rows outside the new 30-day default.
- **MEDIUM-1:** `tests/api/test_password_management.py:249` asserts PATCH body `== {"ok": True}`. Fix: add to D6/focused pytest/Files; drop the "won't break anyone" claim.
- **MEDIUM-2:** naive far-future date (`9999-12-31T23:00`) passes validation and overflows in `_to_utc` → 500. Fix: convert bounds to UTC inside the same try block; add the test case.
- **LOW:** `pnpm --dir frontend install --frozen-lockfile` (no root package.json); L150 must be rewritten to the 60-day contract (91 dates), not frozen; name tests for SC79 (screens unrestricted) and the handoff super-admin override; first commit on the wish branch adds WISH.md + only `.gitignore:213-215`.

### 2026-10-07 — Plan review #3 (after fix loop 2) — SHIP

- **Target:** WISH.md sha256 `6c516af27ab25194e8fe7fd684c4a5ac7f605999ebda0fc947c5c3b70f8212c7`, verified against remote main `cda3ae3`.
- **Commands:** `git check-ignore -q` → exit 1; `poetry run alembic heads | wc -l | grep -qx 1` → exit 0 (head `021`); `git grep max_window_90_days cda3ae3` and export-caller greps → only wish-listed files; `command -v pnpm poetry` → both present.
- **Result:** all review #1/#2 gaps resolved; checklist passes; zero CRITICAL/HIGH.
- **Non-blocking notes carried into `work` (apply during execution):**
  - MEDIUM: baseline capture must list failing names — use `poetry run pytest -q tests/ 2>&1 | grep -E '^(FAILED|ERROR) |[0-9]+ (passed|failed)'` instead of `| tail -1`.
  - LOW: `test_queue_ignores_export_lookback_without_addon` must pass `to_date` ≤ 90 days after `from_date` (else the queue returns `max_window_90_days`).
  - LOW: an aware midnight `end` is stretched to 23:59:59 by `_apply_filters`; compute the end civil date after that stretch so a file never spans 61 dates.
  - LOW: Files list should include `.gitignore` and this WISH.md; G2 should add a test that a 61–90-day range still loads the queue with export disabled; record G2 vitest/typecheck exit codes at branch point.
  - LOW (wording): L625 breaks from 2026-12-02; G2 complexity label nits (routing unchanged).

### 2026-10-07 — Group 1 execution review — SHIP

- **Worktree:** `.worktrees/audit-export-window` (branch `wish/audit-export-window`, base `origin/main` `0be3a4c`, changes uncommitted). `origin/main` later moved to `380c5b2` (PR #58, frontend + `.gitignore`, no migration); `git merge-tree` showed no conflicts.
- **Baseline (before any change, `0be3a4c`):** `poetry run pytest -q tests/` → `4115 passed, 28 skipped, 118 warnings`; zero `FAILED`/`ERROR` lines (the two `TestSchedulingLinkPatchBoundary` failures from the older memory baseline did not reproduce). `alembic heads` → `021 (head)`. Baseline was green, so the gate is `make quality`.
- **Independent reviewer:** SHIP, no CRITICAL/HIGH. 155 focused tests passed; `lookback_floor` matched `dateutil` `relativedelta(months=6)` on 109,573 days (1900–2200); overflow cases (`9999-12-31T23:00` naive, `0001-01-01Z`) → 400 `invalid_date_range`; authz, overrides and migration checked.
- **Orchestrator validation:** `make quality` → `EXIT=0`, `4164 passed, 28 skipped` (+49 vs baseline, 0 failures; log has no `FAILED`/`ERROR`). Run on the tree *before* the small fix below.
- **Fix after review (user-approved):** `"021"`→`"022"` in `tests/integration/test_handoff_sla_postgres.py:142` and `tests/integration/test_conversation_language_postgres.py:149` (Postgres smoke tests, skipped without `POSTGRES_SMOKE_DATABASE_URL`, so the new asserts did **not** run); traces export docstring/comment reworded to "clinic today minus 30 days through now (31 inclusive dates)".
- **Re-validation after the fix (orchestrator):** 8 focused files → `155 passed`; `alembic heads` → exactly one (`022`); `make format-check lint typecheck` → exit 0 (mypy clean, 177 files). The full `make quality` was **not** re-run after the fix.
- **Carried LOW notes:** `HandoffService.resolve_queue_period` is now unused and its docstring ("queue/export window") is stale — remove or fix in a follow-up; `test_upgrade_backfills_existing_rows_as_false` checks the default on new inserts, not a backfill of existing rows; the missing-`end` default (`now`) also gets the midnight stretch (one second per day at 00:00:00 UTC, edge only for API callers).

### 2026-10-07 — Group 2 execution review — SHIP

- **Worktree:** `.worktrees/audit-export-window`, HEAD `3f4809c` (Group 1 commit, rebased on `origin/main` `ef1a9fe`); Group 2 changes uncommitted: 7 modified + 2 new files, all under `frontend/artifacts/neurostack/src/` (`lib/export-window.ts`, `pages/{traces,handoff-queue,master-admin}.tsx`, `pages/master-admin-audit-addon.test.tsx`, tests, `locales/{pt,en}.ts`). No backend, `package.json` or lockfile changes.
- **Frontend baseline (engineer, before edits):** focused vitest exit 0 (3 files, 74 tests); typecheck exit 0; full suite exit 0 (29 files, 379 tests).
- **Independent reviewer:** SHIP, no CRITICAL/HIGH/MEDIUM. Date-part day count checked against an independent calculation: 146,000 date pairs × 6 timezones, 0 mismatches. Backend contract (codes, PATCH body/response, listing fields) matches `src/api/routes/{traces,handoff,admin}.py`. No test lines deleted.
- **Orchestrator validation:** `vitest run … src/pages/traces.test.tsx src/pages/handoff-queue.test.tsx src/pages/master-admin` → `VITEST_EXIT=0`, 4 files / 90 tests passed; `run typecheck` → `TC_EXIT=0`. The full frontend suite (engineer: 395 passed) and Playwright were **not** re-run by the orchestrator or reviewer.
- **Install workaround:** `pnpm --dir frontend install --frozen-lockfile` fails in this workspace on the root `preinstall` guard ("Use pnpm instead"); the engineer used `--ignore-scripts` (no tracked file changed). The reviewer could not reproduce the empty user-agent explanation (`npm_config_user_agent` was set in a scratch probe) — environment-specific; track separately if it recurs.
- **Carried LOW notes (non-blocking):** (1) no test for the investor block / switch hidden for non-super-admins; (2) the add-on switch is in the row's expandable section, so its state is not visible while collapsed (optional: collapsed-header badge); (3) investor toast text `"Modo Visualização: ação desabilitada."` is hardcoded, same as three existing handlers; (4) traces export with start after end is not blocked client-side (server answers `invalid_date_range` → generic toast); (5) `invalid_date_range` has no specific toast on the handoff export (practically unreachable).

### 2026-10-07 — Whole-wish PR review — SHIP

- **Target:** branch `wish/audit-export-window`, HEAD `97c31c8` (2 commits on `origin/main` `ef1a9fe`); `origin/main` later moved to `6b60344` (PR #63, 5 frontend files outside this diff, no migration → no overlap, no second Alembic head).
- **Independent reviewer:** SHIP, no CRITICAL/HIGH/MEDIUM. Checked end to end: backend error codes vs frontend toast mapping; PATCH request/response vs the master-admin handler; `audit_extended_access` in both admin listings and the TS types; client date-part day count vs server civil dates; queue 90 days vs export 60; target-clinic flag for overrides; super-admin-only write path; migration 022 (NOT NULL + `server_default false`, chained on 021, one head); no secrets/TODOs; commit messages reference the wish.
- **Final gates on `97c31c8` (orchestrator, read from logs):** `make quality` → exit 0, `4189 passed, 28 skipped`, zero `FAILED`/`ERROR`, mypy clean (177 files) — above the 4164 of the earlier run because the rebase brought in upstream tests. Full frontend unit suite → 30 files / 395 tests passed, no failures. Playwright e2e was not run. This WISH.md update is the only change after those gates.
- **PR body must state (deploy order):** (1) run `alembic upgrade head` **before** `up -d` — `scripts/deploy-vps.sh:145-147` starts the new containers first, and until 022 runs every full-entity `select(Tenant)` (21 places in 8 modules, incl. the API-key lookup) returns 500; (2) deploy backend before frontend, otherwise the add-on switch loses its controlled value and shows an error toast; (3) backward compatible: old code ignores the new column, rollback of the backend is safe.
- **Follow-ups (not in this wish):** stale comment `src/handoff/sla.py:13` ("queue/export windows"); `HandoffService.resolve_queue_period` is unused and its docstring is stale; rename `test_upgrade_backfills_existing_rows_as_false` (it checks the insert default); widen `test_non_super_admin_cannot_set_addon` to builder headers and assert the value stays `False`; investor-block test and hardcoded investor toast; traces start>end not blocked client-side; retention copy "90 dias" in `docs/troubleshooting.md:629` and `frontend/artifacts/neurostack/src/pages/seguranca.tsx:274` contradicts Decision 3 — hand to the retention/cost wish.

---

## Files to Create/Modify

```
src/services/export_window.py                      (new)
src/models/tenant.py
alembic/versions/<next>_add_tenant_audit_extended_access.py   (new)
src/api/schemas/admin.py
src/api/routes/admin.py
src/api/routes/traces.py
src/api/routes/handoff.py
src/services/handoff_service.py                    (resolve_export_period sem a checagem de 90 dias)
src/handoff/sla.py                                 (resolve_clinic_zone público)
tests/services/test_export_window.py               (new)
tests/api/test_traces_export_window.py             (new)
tests/api/test_tenant_audit_extended_access_migration.py   (new)
tests/api/test_traces_daily_history.py
tests/api/test_handoff_queue_export.py
tests/api/test_handoff_queue.py
tests/api/test_admin.py
tests/api/test_password_management.py
frontend/artifacts/neurostack/src/pages/traces.tsx
frontend/artifacts/neurostack/src/pages/handoff-queue.tsx
frontend/artifacts/neurostack/src/pages/master-admin.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
frontend/artifacts/neurostack/src/pages/traces.test.tsx
frontend/artifacts/neurostack/src/pages/handoff-queue.test.tsx
frontend/artifacts/neurostack/src/pages/master-admin-*.test.tsx
frontend/artifacts/neurostack/src/lib/export-window.ts   (new)
frontend/artifacts/neurostack/src/pages/master-admin-audit-addon.test.tsx   (new)
tests/integration/test_handoff_sla_postgres.py             (head revision 021 -> 022)
tests/integration/test_conversation_language_postgres.py   (head revision 021 -> 022)
.gitignore                                         (allowlist for this wish)
.genie/wishes/audit-export-window/WISH.md
```
