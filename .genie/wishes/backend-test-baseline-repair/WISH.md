# Wish: Reparar o Baseline da Suíte Backend

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `backend-test-baseline-repair` |
| **Date** | 2026-08-31 |
| **Author** | Codex |
| **Appetite** | medium |
| **Branch** | `wish/backend-test-baseline-repair` |
| **Repos touched** | `neurostack-api` |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Reparar exclusivamente o harness e testes obsoletos que impedem a suíte backend completa de terminar verde na base `29e81ac`. A correção não altera comportamento de produção: semeia o tenant exigido pelo contrato atual, torna o teste de token bucket determinístico, remove o portal cross-thread dos testes locais de observabilidade, estabiliza o isolamento do event loop, alinha a expectativa de política global ao bloqueio contratual e mantém o loop responsivo durante o setup SQLite direto dos testes RLHF.

## Scope

### IN

- Semear um tenant ativo antes do teste legado que cria sessão e hoje recebe o 404 contratual de tenant inexistente.
- Congelar/injetar o relógio no teste HTTP de rate limit para provar consumo sem competir com refill real.
- Converter os três testes locais de observabilidade expostos ao `TestClient` cross-thread para `httpx.AsyncClient` com `ASGITransport`.
- Substituir o override depreciado do `event_loop` por isolamento compatível com pytest-asyncio, eliminando contaminação dependente de ordem.
- Alinhar o teste do scheduling gate ao contrato autoritativo de cinco valores de `_convert_pipeline_result_to_response`.
- Alinhar o teste legado de criação de política global ao contrato explícito de rejeição HTTP 403 da rota.
- Iniciar e encerrar o heartbeat do harness ao redor do setup SQLite direto dos testes RLHF, inclusive durante `init_database()`.
- Remover o deadlock sync/async do novo teste de persistência do Builder, isolar localmente o loop dos testes de cache e desserializar o payload Meta antes de validá-lo.
- Manter o loop responsivo durante todo o lifecycle SQLite direto da fixture de billing.
- Remover mocks import-time de `sys.modules` nos testes de memory e tornar a captura structlog do pipeline independente da ordem de configuração.
- Fazer os nós reproduzidos e a suíte backend completa terminarem normalmente com exit 0.

### OUT

- Alterar validação de tenant, rotas de sessão, algoritmo/token bucket, headers ou middleware de produção.
- Atualizar dependências FastAPI, Starlette, HTTPX, AnyIO ou pytest-asyncio.
- Corrigir warnings de depreciação não necessários ao gate, refatorar fixtures não relacionadas ou acelerar/shardar a suíte.
- Modificar qualquer um dos 16 caminhos de `voice-response-triggers`.
- Alterar a autorização de políticas, o runtime RLHF ou qualquer outro arquivo de produção.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Corrigir testes/harness, não produção | Os failures e o hang foram reproduzidos em `29e81ac`; as rotas e regras de produção estão coerentes com contratos atuais. |
| 2 | Usar o padrão de seed já provado em `test_sessions.py` | Mantém a exigência legítima de tenant existente sem criar bypass de aplicação. |
| 3 | Controlar o relógio somente no teste de rate limit | Refill durante requests de 0,7–0,8s explica 109→109; congelar tempo testa consumo sem redefinir semântica de burst/refill. |
| 4 | Usar `AsyncClient`/`ASGITransport` nos apps locais | Um FastAPI mínimo também trava no portal do `TestClient`, enquanto o transporte ASGI direto passa e preserva o middleware real. |
| 5 | Migrar do fixture `event_loop` customizado para isolamento suportado | pytest-asyncio avisa que o override causará erros; testes já manipulam o loop global e falham por ordem na suíte longa. |
| 6 | Manter o contrato de produção de cinco valores e corrigir o teste | Caller real e testes de fallback usam attempts/totals; projetar ou reverter para três esconderia o accounting persistido. |
| 7 | Preservar o 403 de política global e corrigir a expectativa legada | A rota declara e implementa que políticas globais não podem ser criadas pela API; liberar a operação seria mudança de segurança fora do escopo. |
| 8 | Reusar o heartbeat do próprio harness no setup RLHF direto | O traceback congela em `init_database()`/aiosqlite sem wakeup; manter o loop responsivo remove o deadlock sem reintroduzir loop global compartilhado. |
| 9 | Corrigir os três defeitos test-only revelados pelo gate completo | O teste Builder mistura coroutine com `TestClient`, o módulo de cache depende do estado ambiente quando um predecessor deixa o loop corrente como `None` e o teste Meta compara JSON por whitespace; nenhum dos três exige mudança de produção. |
| 10 | Reusar o heartbeat do harness na fixture SQLite de billing | O terceiro gate completo reproduz hang isolado no setup `engine.begin()` com aiosqlite; o padrão aprovado para RLHF resolve a mesma perda de wakeup sem timeout ou mudança de produção. |
| 11 | Remover a substituição global de `src.models.session` durante collection | O mock import-time de `test_state_store.py` transforma imports dinâmicos reais em `MagicMock` antes da execução; usar `CognitiveStateData` real preserva isolamento e elimina 37 failures em sessions/webhooks. |
| 12 | Renovar apenas o logger do pipeline dentro de `capture_logs()` | Um predecessor materializa o proxy e outro reconfigura structlog; um proxy novo sob o context captura os eventos sem mudar logging de produção. |

## Simplicity Case

- **Simplest complete design:** quatorze ajustes confinados a testes, um para cada causa reproduzida, sem mudanças em runtime ou dependências.
- **Added machinery:** apenas uma fixture de isolamento de event-loop suportada quando necessária; a falha dependente de ordem e o warning explícito do pytest justificam esse boundary.
- **Deferred until measured:** migração ampla de todos os testes async, troca de bibliotecas, relógio injetável em produção e paralelização ficam adiadas; só entram se a suíte continuar falhando após estas correções.
- **Complexity removed:** nenhum fallback de teste, retry, skip, xfail, timeout permissivo ou waiver de baseline será adicionado.

## Dependencies

**depends-on:** none
**blocks:** voice-response-triggers

## Success Criteria

- [x] O teste de criação de sessão cria tenant ativo e recebe 201 sem afrouxar a validação de produção.
- [x] O teste de rate limit prova deterministicamente que duas requisições consomem tokens quando não há tempo de refill.
- [x] Os três testes locais de observabilidade concluem sem thread/portal e continuam verificando redaction e logging.
- [x] Os nós async de auth/rate passam isolados, após seus predecessores e na suíte completa.
- [x] Os 13 nós de scheduling gate desempacotam o contrato de cinco valores e preservam as asserções de bloqueio/agendamento.
- [x] O teste de política global exige o 403 contratual e valida a mensagem estável sem liberar criação global.
- [x] Os testes de validação de `clinic_tenant_id` RLHF inicializam e encerram o banco sem hang nem loop/tarefa vazada.
- [x] O teste de persistência do Builder conclui sem portal sync dentro de coroutine; os 22 testes de cache usam loop local fechado; o teste Meta valida o JSON estruturalmente.
- [x] Os dois testes de billing inicializam, usam e descartam o engine SQLite com heartbeat vivo e cleanup garantido, sem hang nem tarefa vazada.
- [x] A coleta dos testes de memory não muta `sys.modules`/`sys.path`; sessions e webhooks recebem o model ORM real independentemente da ordem.
- [x] Os dois testes de scheduling-link capturam seus eventos após a sequência composta de materialização e reconfiguração do structlog.
- [x] `pytest` completo coleta toda a suíte, conclui normalmente e sai com código 0, sem skip/xfail novo.
- [x] Black, isort e Flake8 passam em todos os arquivos Python tocados; nenhum caminho de voz ou produção é modificado.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 1 — testes determinísticos, sem runtime | engineer-trivial / low | Corrigir seed de sessão e relógio do rate-limit. |

### Wave 2 (sequential, after Wave 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 3 — harness async stateful (+2), prior rework (+1) | engineer-standard / high | Remover portal cross-thread e estabilizar isolamento do event loop sobre o teste de rate limit já estável. |

### Wave 3 (sequential, after aggregate gate discovery)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 2 — contrato de teste + lifecycle async local | engineer-standard / high | Corrigir a expectativa legada de política global e o deadlock SQLite reproduzido pelo gate completo. |

### Wave 4 (sequential, after second aggregate gate discovery)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 4 | engineer | 2 — três regressões test-only isoladas | engineer-standard / high | Remover o hang Builder, tornar o módulo de cache independente de loop implícito e robustecer a asserção JSON Meta. |

### Wave 5 (sequential, after third aggregate gate discovery)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 5 | engineer | 2 — lifecycle SQLite local já provado | engineer-standard / high | Aplicar o heartbeat do harness à fixture de billing que trava isoladamente no setup aiosqlite. |

### Wave 6 (sequential, after fourth aggregate gate discovery)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 6 | engineer | 2 — dois leaks globais de testes com repro mínimo | engineer-standard / high | Remover contaminação de import em memory e renovar o proxy structlog somente nos dois testes de pipeline. |

Os grupos editam arquivos distintos, mas o Grupo 2 executa uma sequência que inclui o teste de rate limit pertencente ao Grupo 1; por isso ele começa somente após o primeiro handoff revisado. A suíte completa é um gate agregado posterior porque somente a ordem global consegue provar a ausência de contaminação async.

## Execution Groups

### Group 1: Contratos determinísticos de sessão e rate limit

**Goal:** Fazer os dois failures determinísticos refletirem os contratos atuais sem alterar a aplicação.

**Deliverables:**
1. Semear `sample_tenant_id` como tenant ativo no setup do fluxo de criação de sessão.
2. Congelar/controlar o relógio do teste HTTP de rate limit para impedir refill entre as duas observações.
3. Manter asserções de 201/create-list e consumo de quota, sem skip, retry ou tolerância frouxa.

**Acceptance Criteria:**
- [x] O teste de sessão falha se o tenant não existir e passa com seed explícito, preservando o 404 real para tenant ausente nos testes contratuais.
- [x] O teste de rate limit observa redução determinística e os testes unitários de consume/refill continuam verdes.
- [x] O diff do grupo toca somente `tests/api/test_integration.py` e `tests/api/test_middleware_rate_limit.py`.

**Validation:**
```bash
poetry run pytest tests/api/test_integration.py::TestAPIIntegration::test_session_creation_flow tests/api/test_sessions.py::TestExistingSessionsContract::test_create_and_list_session tests/api/test_middleware_rate_limit.py
poetry run black --check tests/api/test_integration.py tests/api/test_middleware_rate_limit.py
poetry run isort --check-only tests/api/test_integration.py tests/api/test_middleware_rate_limit.py
poetry run flake8 tests/api/test_integration.py tests/api/test_middleware_rate_limit.py
```

Os nós focados cobrem a falha e os contratos vizinhos; checks estáticos exigem zero regressão nos dois arquivos tocados.

**depends-on:** none

---

### Group 2: Harness assíncrono e observabilidade sem portal

**Goal:** Remover o hang cross-thread e impedir que testes deixem estado de event loop para sucessores.

**Deliverables:**
1. Converter os três apps locais de `test_observability_redaction.py` de `TestClient` para testes async com `ASGITransport`.
2. Remover o override depreciado de `event_loop` e usar o mecanismo suportado de policy/scope do pytest-asyncio, com restauração explícita apenas onde testes manipulam loop global.
3. Cobrir os nós async de auth/rate em sequência representativa e garantir que nenhum thread/tarefa/loop fique pendente.
4. Atualizar o helper e os 12 pontos de unpack de `test_event_service_scheduling_gate.py` para o retorno de cinco valores, ignorando attempts/totals apenas onde o cenário não os inspeciona.

**Acceptance Criteria:**
- [x] Os três testes de observabilidade concluem em execução isolada e verificam os mesmos templates/redactions/logs.
- [x] Nenhum teste novo depende de sleep, retry, skip, xfail ou timeout como sucesso.
- [x] Os testes async de middleware passam isolados e depois dos módulos predecessores reproduzidos.
- [x] pytest-asyncio não emite o warning de override customizado de `event_loop`.
- [x] Os 13 testes de scheduling gate passam contra o contrato de cinco valores sem alterar `event_service.py`.
- [x] O diff do grupo toca somente `tests/conftest.py`, `tests/api/test_observability_redaction.py` e `tests/services/test_event_service_scheduling_gate.py`.

**Validation:**
```bash
poetry run pytest tests/api/test_observability_redaction.py tests/api/test_middleware_auth.py tests/api/test_middleware_rate_limit.py tests/services/test_multimodal_transcription.py tests/services/test_event_service_scheduling_gate.py
poetry run black --check tests/conftest.py tests/api/test_observability_redaction.py tests/services/test_event_service_scheduling_gate.py
poetry run isort --check-only tests/conftest.py tests/api/test_observability_redaction.py tests/services/test_event_service_scheduling_gate.py
poetry run flake8 tests/conftest.py tests/api/test_observability_redaction.py tests/services/test_event_service_scheduling_gate.py
```

A seleção inclui os manipuladores conhecidos de loop e os consumidores que falharam por ordem; a prova definitiva continua no gate agregado.

**depends-on:** Group 1

---

### Group 3: Contrato de política global e setup RLHF responsivo

**Goal:** Corrigir os dois novos defeitos test-only reproduzidos pelo gate agregado sem alterar autorização ou runtime.

**Deliverables:**
1. Fazer o teste de criação de política global exigir HTTP 403 e a mensagem contratual da rota.
2. Iniciar o heartbeat já existente antes de `init_database()` na fixture `db_ready` dos testes RLHF e cancelá-lo/aguardá-lo no cleanup, inclusive em erro.
3. Provar que os dois testes de validação de tenant RLHF concluem e que nenhum loop, thread ou tarefa permanece vivo.

**Acceptance Criteria:**
- [x] O teste falha se a API permitir criação de política global e passa com o bloqueio 403 atual.
- [x] O teste RLHF antes travado termina isoladamente e após módulos predecessores.
- [x] Nenhum timeout, sleep, retry, skip ou xfail é usado como sucesso.
- [x] O diff do grupo toca somente `tests/api/test_policies.py` e `tests/api/test_rlhf.py`.

**Validation:**
```bash
poetry run pytest tests/api/test_policies.py tests/api/test_rlhf.py
poetry run pytest tests/api/test_integration.py tests/api/test_middleware_rate_limit.py tests/api/test_observability_redaction.py tests/api/test_policies.py tests/api/test_rlhf.py tests/services/test_event_service_scheduling_gate.py
poetry run black --check tests/api/test_policies.py tests/api/test_rlhf.py
poetry run isort --check-only tests/api/test_policies.py tests/api/test_rlhf.py
poetry run flake8 tests/api/test_policies.py tests/api/test_rlhf.py
```

**depends-on:** Group 2

---

### Group 4: Compatibilidade do gate completo no Python 3.12

**Goal:** Corrigir os três defeitos test-only seguintes revelados somente pelo novo snapshot agregado, sem alterar Builder, cache, Meta ou qualquer runtime.

**Deliverables:**
1. Usar o cliente HTTP assíncrono no teste async de persistência do Builder, evitando `TestClient`/portal sync dentro da coroutine.
2. Dar a `test_event_service_config_cache.py` um loop local explícito, restaurado e fechado por teste, sem reintroduzir fixture global, com regressão observável do cleanup.
3. Desserializar uma vez o body da requisição Meta e afirmar `type == "audio"` e `audio.id == "MEDIA-1"`, sem depender do espaçamento emitido por `json.dumps`/HTTPX.

**Acceptance Criteria:**
- [x] O nó Builder antes travado termina isoladamente e após seus predecessores.
- [x] Os 22 testes de cache e a nova regressão de cleanup passam após `test_sync_client_restores_registered_current_loop`, que termina deterministicamente com loop corrente `None`, com a depreciação exata promovida a erro; o loop local fecha e restaura o estado anterior tanto em sucesso quanto em exceção.
- [x] Os 12 testes Meta passam e o caso de áudio falha se o payload estrutural não tiver `type: audio` ou `audio.id: MEDIA-1`.
- [x] Nenhuma produção, fixture global, timeout, sleep, retry, skip ou xfail é alterado.
- [x] O diff do grupo toca somente `tests/api/test_builder.py`, `tests/services/test_event_service_config_cache.py` e `tests/services/test_meta_service_egress.py`.

**Validation:**
```bash
poetry run pytest tests/api/test_builder.py::TestBuilderToggleClinicStatus::test_returns_404_for_nonexistent_clinic tests/api/test_builder.py::TestBuilderToggleClinicStatus::test_returns_400_for_invalid_uuid tests/api/test_builder.py::TestBuilderToggleClinicStatus::test_toggle_persists_across_a_fresh_session tests/services/test_meta_service_egress.py
poetry run pytest -W "error:There is no current event loop:DeprecationWarning" tests/api/test_middleware_auth.py::test_sync_client_restores_registered_current_loop tests/services/test_event_service_config_cache.py
poetry run black --check tests/api/test_builder.py tests/services/test_event_service_config_cache.py tests/services/test_meta_service_egress.py
poetry run isort --check-only tests/api/test_builder.py tests/services/test_event_service_config_cache.py tests/services/test_meta_service_egress.py
poetry run flake8 tests/api/test_builder.py tests/services/test_event_service_config_cache.py tests/services/test_meta_service_egress.py
```

**Wave 4 pre-change SHA-256:**
- `tests/api/test_builder.py`: `fa4bcfde25c8845472bc4c4627d0ea0e9045a6246d83752a6d5c35c7838b5302`
- `tests/services/test_event_service_config_cache.py`: `5b9ff89bf60aa096350b3fa2c66ff01198dc51da88faeb156b79e3584180dcfa`
- `tests/services/test_meta_service_egress.py`: `9984743491fccc1743396c1558745dd2c2ffd72173d5b7d511d00ea93e065910`

O reviewer compara o patch incremental contra esses três hashes; mudanças preexistentes no arquivo Builder não pertencem à Wave 4.

**depends-on:** Group 3

---

### Group 5: Lifecycle SQLite responsivo em billing

**Goal:** Fazer a fixture SQLite direta dos testes de billing concluir de forma determinística, sem mudar o serviço de billing.

**Deliverables:**
1. Iniciar `_keep_event_loop_responsive` antes de `engine.begin()` na fixture `usage_db`.
2. Manter o heartbeat vivo durante setup, teste e `engine.dispose()`.
3. Cancelar e aguardar o heartbeat em `finally`, inclusive se setup ou cleanup falhar.

**Acceptance Criteria:**
- [x] O nó antes travado termina isoladamente sem timeout tratado como sucesso.
- [x] O arquivo de billing passa os dois testes e não deixa tarefa ou engine vivo.
- [x] Os dois nós de scheduling-link e `tests/core` continuam verdes sem alteração.
- [x] O diff toca somente `tests/services/test_billing_usage_service.py`; nenhum runtime, sleep, retry, skip ou xfail é alterado.

**Validation:**
```bash
poetry run pytest tests/services/test_billing_usage_service.py
poetry run pytest tests/core/test_pipeline.py::TestCognitivePipeline::test_scheduling_link_send_turn_dispatches_handoff_and_exact_response tests/core/test_pipeline.py::TestCognitivePipeline::test_scheduling_link_enrich_turn_updates_same_handoff
poetry run pytest tests/core
poetry run black --check tests/services/test_billing_usage_service.py
poetry run isort --check-only tests/services/test_billing_usage_service.py
poetry run flake8 tests/services/test_billing_usage_service.py
```

**Pre-change SHA-256:** `tests/services/test_billing_usage_service.py` = `135ed4c260b298115caf4bcd897176e62b670cdb2073d187e6138fce6547781d`.

**depends-on:** Group 4

---

### Group 6: Isolamento de collection e logging

**Goal:** Eliminar os dois vazamentos globais test-only reproduzidos pelo quarto gate completo, sem alterar models, memory stores, logging ou pipeline de produção.

**Deliverables:**
1. Remover `sys.path.insert(...)` e qualquer atribuição a `sys.modules["src.models.session"]` de `test_state_store.py` e `test_event_store.py`; usar o `CognitiveStateData` real quando necessário.
2. Nos dois testes scheduling-link, substituir temporariamente `pipeline_module.logger` por um novo `structlog.get_logger("src.core.pipeline")` criado dentro de `capture_logs()`, com restauração via `monkeypatch`.
3. Provar os predecessores causais: módulos memory incluídos na mesma coleta antes da execução dos nodes API e materialização do logger + `configure_logging()` antes do pipeline.

**Acceptance Criteria:**
- [x] `test_state_store.py` e `test_event_store.py` passam isolados, não mutam `sys.modules`/`sys.path` e usam o model real.
- [x] O primeiro teste de sessions e os fluxos webhook Meta text-flow/Uazapi from-me passam mesmo quando os módulos memory participam da mesma coleta.
- [x] A sequência events → observability configure_logging → dois scheduling-link passa e captura os eventos estruturados exatos.
- [x] O diff toca somente `tests/memory/test_state_store.py`, `tests/memory/test_event_store.py` e `tests/core/test_pipeline.py`; nenhuma produção, timeout, retry, skip ou xfail é alterado.

**Validation:**
```bash
poetry run pytest tests/memory/test_state_store.py tests/memory/test_event_store.py
poetry run pytest tests/api/test_sessions.py::TestInactiveSessionsQueue::test_contract_effective_filter_order_and_pagination tests/api/test_webhook_egress.py::TestZapiFromMeAudioSkips::test_from_me_audio_pauses_session_for_human_takeover tests/api/test_webhook_meta.py::TestMetaWebhookTextFlow::test_text_message_dispatches_pipeline_and_replies tests/api/test_webhook_uazapi.py::TestUazapiFromMeHumanTakeover::test_from_me_pauses_patient_session_resolved_by_chatid tests/memory/test_state_store.py tests/memory/test_event_store.py
poetry run pytest tests/api/test_events.py::TestEventsEndpoint::test_process_event_returns_200 tests/api/test_observability_redaction.py::TestUvicornAccessLogDisabled::test_logger_is_disabled_after_configure_logging tests/core/test_pipeline.py::TestCognitivePipeline::test_scheduling_link_send_turn_dispatches_handoff_and_exact_response tests/core/test_pipeline.py::TestCognitivePipeline::test_scheduling_link_enrich_turn_updates_same_handoff
poetry run black --check tests/memory/test_state_store.py tests/memory/test_event_store.py tests/core/test_pipeline.py
poetry run isort --check-only tests/memory/test_state_store.py tests/memory/test_event_store.py tests/core/test_pipeline.py
poetry run flake8 tests/memory/test_state_store.py tests/memory/test_event_store.py tests/core/test_pipeline.py
! rg -n 'sys\.path\.insert|sys\.modules\[' tests/memory/test_state_store.py tests/memory/test_event_store.py
```

**Pre-change SHA-256:**
- `tests/memory/test_state_store.py`: `e8826e9754fcb1fd586e336d73b488ba222f4649b69f6085916d9be6ea54e99b`
- `tests/memory/test_event_store.py`: `b656c3b30c9bbf3b9b5f13477442148737ea001161a8e17306506f3229db21f4`
- `tests/core/test_pipeline.py`: `3367aff6d3a5c612bd146b1b8adf953f115c3980e87e26450da91f7c1b8c307a`

**depends-on:** Group 5

---

## Aggregate Integration Gate

Executar somente após os seis grupos concluírem suas revisões, no mesmo snapshot integrado:

```bash
poetry run pytest
poetry run black --check tests/api/test_integration.py tests/api/test_middleware_rate_limit.py tests/api/test_observability_redaction.py tests/api/test_policies.py tests/api/test_rlhf.py tests/conftest.py tests/services/test_event_service_scheduling_gate.py tests/api/test_builder.py tests/services/test_event_service_config_cache.py tests/services/test_meta_service_egress.py tests/services/test_billing_usage_service.py tests/memory/test_state_store.py tests/memory/test_event_store.py tests/core/test_pipeline.py
poetry run isort --check-only tests/api/test_integration.py tests/api/test_middleware_rate_limit.py tests/api/test_observability_redaction.py tests/api/test_policies.py tests/api/test_rlhf.py tests/conftest.py tests/services/test_event_service_scheduling_gate.py tests/api/test_builder.py tests/services/test_event_service_config_cache.py tests/services/test_meta_service_egress.py tests/services/test_billing_usage_service.py tests/memory/test_state_store.py tests/memory/test_event_store.py tests/core/test_pipeline.py
poetry run flake8 tests/api/test_integration.py tests/api/test_middleware_rate_limit.py tests/api/test_observability_redaction.py tests/api/test_policies.py tests/api/test_rlhf.py tests/conftest.py tests/services/test_event_service_scheduling_gate.py tests/api/test_builder.py tests/services/test_event_service_config_cache.py tests/services/test_meta_service_egress.py tests/services/test_billing_usage_service.py tests/memory/test_state_store.py tests/memory/test_event_store.py tests/core/test_pipeline.py
```

`pytest` deve coletar toda a suíte, concluir normalmente e retornar exit 0. Nenhum novo skip, xfail, retry, timeout tratado como sucesso ou waiver de baseline satisfaz o gate. Os três checks estáticos exigem zero erro nos quatorze caminhos tocados. Se a implementação exigir outro arquivo Python, o plano pausa para incluir o novo operando e passar por nova revisão.

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] A criação de sessão continua exigindo tenant real e retorna 201 após seed válido.
- [ ] Headers de rate limit continuam presentes e o teste de consumo não depende da duração do handler.
- [ ] Redaction de capability token e request ID continuam observáveis sem hang.
- [ ] Suíte backend completa termina com exit 0 duas vezes consecutivas ou uma vez após reprodução limpa sem contaminação.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Remover fixture global de loop expõe testes que dependiam de estado compartilhado | High | Rodar seleção dos manipuladores conhecidos e a suíte completa; corrigir dependência de teste, não restaurar compartilhamento implícito. |
| Seed do tenant vaza entre testes | Medium | Usar sessão/fixture já isolada e confirmar contratos de tenant ausente. |
| Freeze de relógio mascara refill | Medium | Manter testes unitários separados de refill e congelar somente o teste de consumo HTTP. |
| AsyncClient muda semântica de lifespan | Low | Apps locais não têm startup necessário; manter middleware e rota reais no ASGI transport. |
| Ajuste de unpack mascara regressão no accounting | Medium | Manter helper tipado com cinco valores e validar testes de fallback existentes no gate completo. |
| Heartbeat local vaza tarefa quando o setup falha | Medium | Criar antes do init e cancelar/aguardar em `finally`, cobrindo caminho normal e erro. |

---

## Review Results

### Final Execution Review — 2026-09-06

- **Reviewed WISH SHA-256:** `2d07cbe174fb44e5977ada8ec2a1caa0d1f6308ef3fbb5eff8dcea8e9f929277`
- **Reviewer:** `/root/gemini_security_fix1_rereview/qa_lens_group6`
- **Verdict:** SHIP
- **Aggregate pytest:** 2,634 collected; 2,620 passed; 14 pre-existing skips; 0 failed; exit 0 in 1,809.52 s.
- **Static gates:** Black, isort and Flake8 passed on all 14 paths; the import-mutation guard and `git diff --check` passed.
- **Scope:** exactly 14 test paths against governing base `29e81ac`; no production or `voice-response-triggers` changes attributable to this wish; no new skip, xfail, retry, timeout-as-success or waiver.
- **Residual:** MEDIUM non-blocking — exceptional `engine.dispose()`/heartbeat cancellation paths lack direct instrumentation; normal lifecycle and nested `finally` passed the aggregate gate. LOW lifecycle — isolate the 14 test paths and governing metadata in the future PR.

Execution criteria and all six groups are complete. The four QA-after-merge criteria remain open; status stays `IN_PROGRESS` until PR review, authorized merge and post-merge QA.

### 2026-08-31 — Plan Review

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Target SHA-256:** `a7d81bd2e82047f9c60e696d898928b0b3b090b7adc63c9ced7a8e5f31c67b2b`
- **Evidence:** quatro arquivos test-only, duas waves sequenciais, causas ligadas aos três traces, `blocks: voice-response-triggers`, validações focadas e gate agregado completo verificados; `git diff --check` verde.
- **Gate contract:** `pytest` completo deve terminar com exit 0; sem skip, xfail, retry, timeout-as-success ou waiver; qualquer novo caminho Python exige replanning.
- **Gaps:** nenhum.

### 2026-08-31 — Group 1 Execution Review (Loop 0)

- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/voice_plan_rereview`
- **Task:** `t_mth8bwnz291cbc71`
- **Scoped patch SHA-256:** `593f8a904c616239068e329ca8735f3b75c746958b9d8414c546091902e5e905` (`+28/-1`)
- **Approved:** seed de tenant ativo, preservação do 404 real, ausência de scope creep/skip/retry/xfail e checks focados/estáticos.
- **[HIGH]:** monkeypatch de `rate_limit_module.time.time` muta o módulo stdlib singleton e congela timing global durante requests.
- **Fix route:** loop 1/2; substituir o binding `rate_limit_module.time` por proxy local após o primeiro request, congelando somente o refill do bucket antes do segundo.

### 2026-08-31 — Group 1 Execution Re-review (Fix Loop 1/2)

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Task:** `t_mth8bwnz291cbc71`
- **Scoped patch SHA-256:** `479fa8a07b33b127803dcb003edbd5b45d2ad5af5a4ac778165fc3afa2db14b3` (`+35/-2`)
- **Evidence:** primeiro request usa relógio real; proxy substitui apenas `rate_limit_module.time`, fixa `bucket.last_refill` e prova `remaining2 == remaining1 - 1`; stdlib/refill unitário intactos; seed de tenant continua test-only.
- **Validation:** fixer executou 27/27; orchestrator reproduziu os 15 nós exatos do wish; Black/isort/Flake8/diff-check verdes.
- **Gaps:** nenhum.

### 2026-08-31 — Amended Plan Re-review

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Target SHA-256:** `4294eccb34050352380921878bc349ba90f52b524de8e7ca432550fe91461485`
- **Evidence:** contrato produtivo/caller/testes fallback usam cinco valores; helper de scheduling e 12 unpacks estavam obsoletos em três; quinto caminho test-only incluído em escopo, validação focada e gate agregado.
- **Guard:** produção, dependências e 16 caminhos de voz continuam excluídos; `pytest` completo permanece obrigatório sem waiver/skip/xfail/retry/timeout-as-success.
- **Gaps:** nenhum.

### 2026-08-31 — Group 2 Execution Review (Loop 0)

- **Verdict:** FIX-FIRST
- **Reviewer:** `/root/voice_plan_rereview`
- **Task:** `t_mth8bx0d42f883e4`
- **Three-file patch SHA-256:** `9277b8e5e46f06f62b1e848cd95883ee48f3aff649c00b3a5d1f3054e12addf7` (`+80/-106`)
- **Approved:** 3 apps locais async, remoção do override `event_loop`, loop local do scheduling, 12 unpacks de cinco valores, 111 testes e checks estáticos.
- **[HIGH]:** o quarto caso usa o app real com `ASGITransport` sem lifespan; `raise_app_exceptions=False` e ausência de status podem esconder 500 de lifecycle.
- **Fix route:** loop 1/2; usar lifespan assíncrono explícito/fixture existente no caso real e exigir status esperado antes das asserções de log/redaction.

### 2026-08-31 — Group 2 Execution Re-review (Fix Loop 1/2)

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Task:** `t_mth8bx0d42f883e4`
- **Three-file patch SHA-256:** `e74ed8d47d34198dca0e30e4c2f316e0ff9f2879e2bd048b86aef9083820279b` (`+89/-116`)
- **Evidence:** app real usa fixture async com lifespan/DB/heartbeat, exige 401 antes de redaction; três ASGI transports restantes são apps locais sem lifecycle; override global removido; scheduling usa cinco valores e loop local fechado.
- **Validation:** 19/19 observabilidade, 111/111 gate exato, Black/isort/Flake8/scope/diff verdes; orchestrator reproduziu 19/19.
- **Gaps/residual:** nenhum no Grupo 2.

### 2026-08-31 — Group 3 Amended Plan Review

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Target SHA-256:** `ce433f7562c232a4bd5ff452ef23d206bf05025d060ecd6ad27fc84124f370d9`
- **Evidence:** os dois defeitos reproduzem na base limpa `29e81ac`; política global preserva o 403 fail-closed e heartbeat local cobre `init_database()`/cleanup da fixture RLHF.
- **Guard:** somente `tests/api/test_policies.py` e `tests/api/test_rlhf.py`; nenhuma produção, autorização, dependência, timeout, retry, skip, xfail ou waiver.
- **Gaps:** nenhum.

### 2026-08-31 — Group 3 Execution Review

- **Verdict:** SHIP
- **Reviewer:** `/root/voice_plan_rereview`
- **Task:** `t_mthlax795bd95ce6`
- **Scoped patch SHA-256:** `c728c79ac7449da91d9cbb5769253f1850750441303812ec95c47eeb6e3f154c` (`+37/-18`)
- **Evidence:** política exige status/detalhe 403 exatos; heartbeat nasce antes do init, permanece durante uso/close e é cancelado/aguardado em `finally`, respeitando ownership do banco e falhas parciais.
- **Validation:** 61/61 no escopo, 121/121 na sequência representativa, Black/isort/Flake8/diff-check verdes; somente os dois caminhos autorizados.
- **Gaps/residual:** nenhum no Grupo 3; gate completo continua obrigatório.

### 2026-09-05 — Group 2 Revalidation — FIX-FIRST (fix-loop exhausted)

- **Trigger:** o gate agregado `poetry run pytest -q` voltou a ficar sem progresso; a sequência representativa reproduziu o bloqueio logo após os primeiros testes de observabilidade.
- **Fix loop 1:** `tests/api/test_observability_redaction.py` foi migrado de quatro usos de `TestClient` para `AsyncClient`/`ASGITransport`; o caso do app real passou a usar `async_client` com lifecycle explícito e exige `401`. O nó isolado passou **19/19**.
- **Fix loop 2:** a fixture customizada `event_loop` foi removida de `tests/conftest.py`; o warning de override do `pytest-asyncio` deixou de ocorrer no nó isolado. Black, isort, Flake8 e `git diff --check` passaram nos caminhos revisados.
- **Re-review independente:** FIX-FIRST. `poetry run pytest -q tests/services/test_event_service_scheduling_gate.py` falha **13/13**: `_run_convert()` devolve cinco valores, mas 12 callers ainda desempacotam três (`ValueError: too many values to unpack`). O helper também cria/instala um loop global sem lifecycle fechado/restaurado e emite `DeprecationWarning: There is no current event loop`.
- **Status:** não liberar o gate agregado; o Grupo 2 do repair requer novo corretivo focado em `tests/services/test_event_service_scheduling_gate.py`. A tarefa histórica estava indevidamente marcada como `done`, por isso não foi possível reivindicá-la no CLI durante esta revalidação.
- **Diagnosis:** `missing-context` — o estado de tarefa/review dizia que os 12 unpacks e o lifecycle de loop já haviam sido corrigidos, mas o snapshot real contém o contrato obsoleto. Novo trabalho deve receber o diff e os 13 failures acima; não há evidência para escalar modelo/esforço.

### 2026-09-06 — Group 5 Amended Plan Review

- **Verdict:** SHIP
- **Reviewer:** `/root/baseline_billing_execution_review`
- **Target SHA-256:** `d9b082d3bfed2bd621e8f8d85052c67bdc1bcf60ccd620acd06736394a11a359`
- **Evidence:** o hang reproduzido está limitado ao lifecycle SQLite da fixture de billing; a emenda reutiliza o heartbeat test-only já aprovado, começa antes de `engine.begin()`, cobre setup/uso/dispose e mantém produção, dependências e loop global fora do escopo.
- **Validation contract:** dois testes de billing, dois nós de scheduling-link, `tests/core`, Black/isort/Flake8 no arquivo tocado e `pytest` backend completo; sem sleep, retry, skip, xfail, timeout-as-success ou waiver.
- **Scope:** somente `tests/services/test_billing_usage_service.py`; Wave 5 depende do Grupo 4 e o gate agregado inclui os onze caminhos autorizados.
- **Gaps:** nenhum.

### 2026-09-06 — Group 5 Execution Re-review

- **Verdict:** SHIP
- **Reviewer:** `/root/baseline_billing_execution_review`
- **QA lens:** `/root/baseline_billing_qa_lens` — SHIP
- **Task:** `t_mtq31zrm69a5c8a6`
- **Scoped patch SHA-256:** `620bcf5f67b35953daab431edfbed605c2b1a8a26e71c50c7722ef3bf5c7994f` (`+18/-8`), result `6e6634fb41efbc1befed60c6e7b9328b0157532a9a5c07e1265c16da357156d2`.
- **Evidence:** heartbeat criado antes de `engine.begin()`, vivo durante schema/sessão/teste/dispose e cancelado/aguardado por `finally` aninhado mesmo em falha; helper preexistente e nenhum runtime, estado global, sleep, retry, timeout, skip ou xfail alterado.
- **Validation:** billing 2/2, scheduling-link 2/2, `tests/core` 46/46, Black/isort/Flake8 e `git diff --check` verdes.
- **Residual [MEDIUM]:** um teste instrumental futuro poderia afirmar diretamente dispose/cancelamento nos caminhos de exceção; não há defeito observado na implementação atual.
- **Gaps bloqueantes:** nenhum.

### 2026-09-06 — Group 6 Amended Plan Review

- **Verdict:** SHIP
- **Reviewer:** `/root/baseline_aggregate_qa_lens`
- **Target SHA-256:** `2131122224b2d74c543000d02efc74d09e412861976ec78443328b41d90c725f`
- **Evidence:** os dois vazamentos globais foram reproduzidos e o plano os corrige na origem: remove mutação de imports durante collection e renova o proxy structlog somente dentro de `capture_logs()`; nenhuma fixture/reset global ou produção é alterada.
- **Scope:** somente `tests/memory/test_state_store.py`, `tests/memory/test_event_store.py` e `tests/core/test_pipeline.py`, com `depends-on: Group 5`.
- **Validation contract:** duas sequências causais, testes memory isolados, Black/isort/Flake8, guard negativo de imports e gate backend agregado nos quatorze caminhos; sem skip, xfail, retry, timeout-as-success ou waiver.
- **Residual [LOW]:** há um critério duplicado na seção de QA pós-merge; não afeta escopo nem execução.
- **Gaps bloqueantes:** nenhum.

### 2026-09-06 — Group 6 + Aggregate Execution Re-review

- **Verdict:** SHIP
- **Reviewer:** `/root/baseline_aggregate_execution_review`
- **QA lens:** `/root/baseline_aggregate_qa_lens` — SHIP
- **Current reviewed WISH SHA-256:** `db31773ca390590efcc02a4892f8f4efa54da42f2923f760055cb02eb345880e`
- **Approved Group 6 plan target:** `2131122224b2d74c543000d02efc74d09e412861976ec78443328b41d90c725f`
- **Backend snapshot:** `3efc4f288ac9c5522addd5296fe510d4226646c47480a7b1cdd0684249d1dc13`.
- **Aggregate validation:** 2620 passed, 14 skips condicionais de PostgreSQL, 0 failed, exit 0 em 1816,54 s; Black/isort/Flake8 nos quatorze caminhos, guard de import e `git diff --check` verdes.
- **Scope:** quatorze caminhos test-only; nenhuma produção, dependência, cache, estado de retry, skip/xfail ou timeout-as-success foi introduzido pelo repair.
- **Residual [MEDIUM]:** cleanup excepcional de dispose/heartbeat não tem fault injection direto; o `finally` aninhado está correto e o lifecycle normal passou no gate completo.
- **Residual [LOW]:** o PR futuro deve isolar os quatorze caminhos e metadados do repair; há um critério QA pós-merge duplicado.
- **Gaps bloqueantes:** nenhum.

### 2026-09-06 — Aggregate Integration Gate Evidence

- **Stable backend snapshot:** `3efc4f288ac9c5522addd5296fe510d4226646c47480a7b1cdd0684249d1dc13`, calculado sobre `src`, `tests`, `alembic`, `pyproject.toml` e `poetry.lock` antes e depois da execução; identidade inalterada.
- **Full suite:** `timeout 7200s poetry run pytest -q --tb=short` concluiu normalmente com exit 0 em 1816,54 s: **2620 passed, 14 skipped, 0 failed**, 56 warnings. Os skips são as integrações externas condicionais já existentes; nenhum skip/xfail/retry/timeout-as-success foi adicionado pelo repair.
- **Static scope:** Black deixou os quatorze arquivos inalterados; isort e Flake8 retornaram exit 0 nos mesmos quatorze caminhos; o guard negativo de `sys.path`/`sys.modules` e `git diff --check` retornaram exit 0.
- **Concurrency diagnosis:** duas passagens anteriores foram invalidadas, uma por edição concorrente comprovada e outra por sobreposição de processos pytest. Meta, Sessions e Webhook passaram integralmente na execução exclusiva; traces focados também passaram 13/13, 23/23, 26/26, 87/87, 106/106, 154/154 e 203/203. Nenhuma correção sem causa foi aplicada.
- **Review state:** emenda do Grupo 6 aprovada; evidência agregada pronta para re-review de execução pós-aprovação. O WISH permanece `IN_PROGRESS`.

### 2026-09-06 — Frozen Snapshot Aggregate Evidence

- **Reason:** o checkout compartilhado continuou recebendo alterações do wish concorrente de providers após o gate anterior. Para impedir novo drift, o backend atual foi copiado de forma coerente para `/tmp/neurostack-baseline-UO3ivV`, sem `.git`, frontend ou caches, antes da execução.
- **Source identity at freeze:** recipe Git oficial `a27d3c21ad51c204f0f6f66062c3cdeba951c6ae5c002d3dc1a3d843eafeb774`; digest de conteúdo equivalente `ef063accd002efa1489c88387a85af69155b8c8014a26df428b211aefd7d604e` em origem e cópia após `rsync`.
- **Frozen identity after gate:** `ef063accd002efa1489c88387a85af69155b8c8014a26df428b211aefd7d604e`, inalterado.
- **Full suite:** `timeout 7200s poetry run pytest -q --tb=short`, usando o mesmo ambiente Poetry por `.venv` symlink, concluiu com exit 0 em 1836,47 s: **2639 collected, 2625 passed, 14 skipped, 0 failed**, 56 warnings.
- **Skip topology:** doze casos parametrizados de clonagem PostgreSQL, um smoke de support-columns e um de interaction-origin, todos condicionados à ausência de `POSTGRES_SMOKE_DATABASE_URL`; nenhum pertence aos quatorze caminhos do repair.
- **Static scope:** Black deixou os quatorze arquivos inalterados; isort e Flake8 retornaram exit 0; guard negativo de import retornou exit 0. `git diff --check` no checkout de origem retornou exit 0 antes do freeze.
- **Scope/safety:** nenhum arquivo do workspace foi revertido ou alterado para produzir o snapshot; a cópia temporária somente leitura para o gate impede que novos writers invalidem a evidência.
- **Review state:** supersede o recibo agregado de `3efc4f...`; aguarda execution/final re-review do snapshot congelado `a27d3c...`/`ef063a...`.

### 2026-09-06 — Frozen Aggregate Execution Re-review

- **Verdict:** SHIP
- **Reviewer:** `/root/baseline_aggregate_execution_review`
- **QA lens:** `/root/baseline_aggregate_qa_lens` — SHIP
- **Frozen target:** `/tmp/neurostack-baseline-UO3ivV`; recipe Git `a27d3c21ad51c204f0f6f66062c3cdeba951c6ae5c002d3dc1a3d843eafeb774`; digest de conteúdo `ef063accd002efa1489c88387a85af69155b8c8014a26df428b211aefd7d604e`.
- **Aggregate:** 2639 collected, 2625 passed, 14 skips condicionais de PostgreSQL, 0 failed, exit 0 em 1836,47 s; Black/isort/Flake8 nos quatorze caminhos, guard de import e `git diff --check` verdes.
- **Identity:** os quatorze arquivos do repair correspondem byte a byte entre snapshot e checkout; manifest SHA-256 `13235a6921f8e5ef150eb878b73de48b5b72d50fe3567f65b298b9ab0c64e502`.
- **Scope:** nenhum skip, xfail, retry, timeout-as-success, produção, dependência, cache ou estado de sincronização foi introduzido pelo repair.
- **Residual [MEDIUM]:** cleanup excepcional de billing ainda não tem fault injection direto; o fluxo de `finally` é correto e o lifecycle normal está coberto.
- **Residual [MEDIUM]:** o verdict vale para a identidade congelada; qualquer PR com conteúdo diferente exige revalidação.
- **Gaps bloqueantes:** nenhum.

### 2026-09-06 — Frozen Baseline Final Gate

- **Verdict:** SHIP
- **Reviewer:** `/root/baseline_aggregate_final_gate`
- **Target:** backend congelado `/tmp/neurostack-baseline-UO3ivV`, recipe de origem `a27d3c21ad51c204f0f6f66062c3cdeba951c6ae5c002d3dc1a3d843eafeb774`, conteúdo `ef063accd002efa1489c88387a85af69155b8c8014a26df428b211aefd7d604e`.
- **Evidence:** 2639 collected, 2625 passed, 14 skips PostgreSQL explicados, 0 failed; quatorze gates estáticos, guard de import e diff check verdes; todas as nove tarefas concluídas e reviews pós-aprovação persistidos.
- **Dependency transition:** o baseline satisfaz a dependência da Central. O bloqueio de `t_mtpy3zmi922ecc5e` pode ser removido desde que o Grupo 5 execute sobre o backend `ef063acc...` e repita as integrações PostgreSQL com zero skips.
- **Residual [MEDIUM]:** fault injection excepcional de billing continua não bloqueante.
- **Gaps bloqueantes:** nenhum.

---

## Files to Create/Modify

```text
tests/api/test_integration.py
tests/api/test_middleware_rate_limit.py
tests/api/test_observability_redaction.py
tests/api/test_policies.py
tests/api/test_rlhf.py
tests/conftest.py
tests/services/test_event_service_scheduling_gate.py
tests/services/test_event_service_config_cache.py
tests/services/test_meta_service_egress.py
tests/api/test_builder.py
tests/services/test_billing_usage_service.py
tests/memory/test_state_store.py
tests/memory/test_event_store.py
tests/core/test_pipeline.py
```
