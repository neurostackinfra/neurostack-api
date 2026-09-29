# Wish: OpenAI TTS — terceiro provedor de voz

| Field | Value |
|-------|-------|
| **Status** | SHIPPED |
| **Slug** | `tts-openai-provider` |
| **Date** | 2026-09-27 |
| **Author** | Lorenzo |
| **Appetite** | small |
| **Branch** | `wish/tts-openai-provider` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Adiciona OpenAI TTS (`api.openai.com/v1/audio/speech`) como terceiro provedor de voz, seguindo o mesmo padrão das opções já existentes (ElevenLabs e Google). Quem já usa a API da OpenAI para LLM passa a ter uma segunda razão para reutilizar as credenciais, sem precisar de conta adicional. O provedor inicia como `coming_soon` — a mesma posição de ElevenLabs e Google — e segue o pipeline de homologação de evidências já estabelecido para ser promovido a `active`.

## Scope

### IN

- Adaptador de síntese `_synthesize_openai()` em `tts_service.py` (POST `/v1/audio/speech`, auth via `Authorization: Bearer`, retorno de áudio binário direto, sem base64)
- Host pinning `api.openai.com` em `safe_egress.py` como `TTS_OPENAI_HOSTS` e política `"tts_openai"`
- Provedor `"openai"` adicionado ao `PROVIDER_AVAILABILITY["tts"]` e ao `PROVIDER_ALLOWLISTS` em `api_credentials.py`; `_tts_required_fields()` retorna `tts_voice_id` para openai (igual ao ElevenLabs)
- Literal `tts_provider` em `TtsCredentialsPatch` atualizado para incluir `"openai"`
- Frontend: `TtsProvider`, `API_PROVIDER_AVAILABILITY.tts`, `TTS_PROVIDERS`, pré-preenchimento de URL via `newProviderFieldDefault()` (modelo: `tts-1`)
- Receptor de evidência adicionado a `provider-availability-receipts.md` (linha `tts | openai | coming_soon`)
- Validador `validate_provider_availability_evidence.py` atualizado: `EXPECTED_DECISIONS`, contagem de pares 22 → 23, `coming_soon` 19 → 20
- Testes de serviço (`test_tts_service.py`) cobrindo: rota happy-path para `openai`, exigência de `tts_voice_id`, erro HTTP do upstream com status exato e sem segredo, pinagem de host
- Teste de parity UI (`clinic-config-api-settings.test.tsx`) confirmando que `TTS_PROVIDERS` está em sincronia com o backend

### OUT

- Promoção do provedor para `active` (exige homologação de evidência, fora deste wish)
- Suporte a parâmetros extras da API (velocidade, formato de saída, `response_format`) — sem evidência de necessidade atual
- Campo de URL editável por provedor no card TTS (URL fixada pelo host pinning; UI pré-preenche mas não impede edição — mesmo comportamento do Gemini LLM)
- Validação de modelos/vozes disponíveis da OpenAI no frontend (enum seria um duplicado frágil da API upstream)

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Iniciar como `coming_soon`, não `active` | Consistente com ElevenLabs e Google; a política de evidências (`api-provider-availability`) exige recibo homologado antes de promover |
| 2 | Reutilizar `tts_voice_id` para o parâmetro `voice` da OpenAI | A OpenAI exige uma voz nomeada (`alloy`, `echo`, `onyx` etc.) — mesmo contrato semântico do ElevenLabs; nenhum campo novo necessário |
| 3 | URL pré-preenchida via `newProviderFieldDefault()` em vez de campo derivado read-only | A host pinning já é o contrato de segurança real; tornar o campo read-only exigiria tornar `credentialFields()` ciente do provedor TTS, o que não é necessário agora. Custo aceito: o campo ainda aceita edição, mas o backend rejeita hosts não-`api.openai.com` |
| 4 | Retorno de áudio tratado como binário direto (igual ao ElevenLabs, sem base64) | A OpenAI TTS devolve bytes de MP3 direto, não JSON com `audioContent` |

## Simplicity Case

- **Simplest complete design:** três arquivos Python tocados (tts_service, safe_egress, api_credentials), dois arquivos de schema/config (config.py + clinic-config.tsx), mais os arquivos de evidência. Nenhuma nova tabela, nenhuma migração, nenhum campo novo no banco.
- **Added machinery:** nenhuma. O adaptador reutiliza exatamente as primitivas `safe_async_client`, `_raise_for_status`, `_validate_audio` e `_clean_text` já existentes.
- **Deferred until measured:** voz e modelo selecionáveis via UI com enum atualizado (requer que OpenAI congele a lista, o que não aconteceu); rate-limit por provedor; campo de URL derivado read-only (requer refatorar `credentialFields()` para ser provider-aware).
- **Complexity removed:** sem caching de áudio, sem fallback entre vozes, sem proxy de credencial.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [x] `python -m pytest tests/services/test_tts_service.py -x` passa com casos para o provedor `openai` (happy-path, exigência de voice_id, erro upstream, pinagem de host)
- [x] `python -m pytest tests/services/test_api_credentials.py -x -k provider_availability` passa — inclui contagens atualizadas (total 20→21, coming_soon 19→20 no dict `PROVIDER_AVAILABILITY`)
- [x] `python -m pytest tests/services/test_safe_egress.py -x -k tts` passa — incluindo asserção para `tts_openai` host pinning
- [x] `python scripts/validate_provider_availability_evidence.py --phase inventory` sai com código 0 — o recibo agora tem 23 pares (20 coming_soon, 3 removed)
- [x] `cd frontend/artifacts/neurostack && npx vitest run src/pages/clinic-config-api-settings.test.tsx` passa sem regressão — `COMING_SOON_COUNT` atualizado para 21 e `BACKEND_PROVIDER_AVAILABILITY.tts` inclui `openai`
- [x] `python -m pytest tests/api/test_api_credentials.py -x -k tts` passa
- [x] `python -m mypy src/services/tts_service.py src/services/safe_egress.py src/services/api_credentials.py --no-error-summary` sai com código 0
- [x] O card OpenAI aparece na aba API com badge "EM BREVE"; os cards ElevenLabs e Google continuam com "EM BREVE" sem regressão

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — multi-arquivo, contrato de crédito e egress simultâneos | engineer-standard | Backend: adaptador `_synthesize_openai`, host pinning, política de credencial, schema |
| 2 | engineer | 2 — multi-arquivo, sincronização de matriz backend↔frontend↔validador | engineer-standard | Evidência + validador + frontend card + testes de parity |

Grupo 2 depende de Grupo 1 (usa o símbolo `"openai"` exportado de `api_credentials.py`).

## Execution Groups

### Group 1: Backend — adaptador, egress e política de credencial

**Goal:** Fazer o provedor `openai` existir no runtime sem expor nem uma credencial nem uma chamada real ao servidor de evidências.

**Deliverables:**
1. `src/services/tts_service.py` — constante `OPENAI_TTS_API_BASE = "https://api.openai.com/v1"`, `_synthesize_openai()` (POST `/audio/speech` appended se ausente, Bearer auth, resposta binária direta como ElevenLabs), `tts_configuration_complete()` e `synthesize_speech()` atualizados
2. `src/services/safe_egress.py` — `TTS_OPENAI_HOSTS: frozenset = frozenset({"api.openai.com"})`, política `"tts_openai": ProviderPolicy(kind="tts_openai", allowed_hosts=TTS_OPENAI_HOSTS)` em `PROVIDER_POLICIES`
3. `src/services/api_credentials.py` — `PROVIDER_AVAILABILITY["tts"]["openai"] = "coming_soon"` (total no dict: 20→21, coming_soon 19→20); `_tts_required_fields()` retorna `base + ("tts_voice_id",)` quando `provider == "openai"` (mesmo ramo do ElevenLabs)
4. `src/api/schemas/config.py` — literal `tts_provider` passa de `Literal["elevenlabs", "google"]` para `Literal["elevenlabs", "google", "openai"]`
5. `tests/services/test_tts_service.py` — casos openai: happy-path (verifica host `api.openai.com`, Bearer header, body `model+input+voice`), ausência de `tts_voice_id` lança `tts_configuration_incomplete`, erro HTTP do upstream mantém o status exato (401 → `tts_upstream_http_error_401`, 429 → `tts_upstream_http_error_429`, códigos distintos e sem key/body)
6. `tests/services/test_safe_egress.py` — adicionar asserção `assert resolve_policy("tts_openai").allowed_hosts == TTS_OPENAI_HOSTS` ao teste `test_every_tts_kind_resolves_with_a_pinned_host`
7. `tests/services/test_api_credentials.py` — atualizar `test_provider_availability_has_sole_gemini_active_overlay`: dict TTS com `"openai": "coming_soon"`, contagem total 20→21, coming_soon 19→20

**Acceptance Criteria:**
- [x] `_synthesize_openai()` envia `POST /v1/audio/speech` com `Authorization: Bearer <key>`, body `{"model": ..., "input": ..., "voice": ...}`, host pinado a `api.openai.com`
- [x] `tts_configuration_complete()` retorna `False` quando `tts_voice_id` está ausente para o provedor `openai`
- [x] Erro HTTP do upstream vira `TtsSynthesisError("tts_upstream_http_error_{status}")` com o status exato (mesmo contrato de ElevenLabs e Google; 401 e 429 geram códigos distintos) — sem body ou key no erro
- [x] `PROVIDER_ALLOWLISTS["tts"]` contém `"openai"`
- [x] `TtsCredentialsPatch(tts_provider="openai")` valida sem erro
- [x] mypy sem erros nos quatro arquivos Python tocados

**Validation:**
```bash
python -m pytest tests/services/test_tts_service.py tests/services/test_api_credentials.py tests/services/test_safe_egress.py tests/api/test_api_credentials.py -x -q
python -m mypy src/services/tts_service.py src/services/safe_egress.py src/services/api_credentials.py src/api/schemas/config.py --no-error-summary
```

**depends-on:** none

---

### Group 2: Evidência, validador e frontend

**Goal:** Manter o validador de inventário verde e exibir o card OpenAI TTS no painel com badge "EM BREVE".

**Deliverables:**
1. `.genie/wishes/api-provider-availability/evidence/provider-availability-receipts.md` — adicionar linha completa de 10 colunas para `tts | openai` com todos os sentinelas `not_run`/`not_available` e timestamp UTC ISO-8601 válido (mesmo padrão das linhas existentes — ver linha `tts | elevenlabs` como referência); total passa de 22 → 23 pares
2. `scripts/validate_provider_availability_evidence.py` — `EXPECTED_DECISIONS` com `("tts", "openai"): "coming_soon"` adicionado; contagem `22-pair inventory` → `23-pair inventory` (linha 215); contagens `coming_soon: 19` → `coming_soon: 20` e total `22` → `23` (linha 220) — `EXPECTED_PROMOTED_MATRIX` é derivado automaticamente de `EXPECTED_DECISIONS`, não precisa de edição
3. `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`:
   - `type TtsProvider` passa de `"elevenlabs" | "google"` para `"elevenlabs" | "google" | "openai"`
   - `API_PROVIDER_AVAILABILITY.tts`: adicionar `openai: "coming_soon"`
   - `TTS_PROVIDERS`: adicionar `{ id: "openai", label: "OpenAI TTS", sub: "gpt-4o-audio / tts-1" }`
   - `newProviderFieldDefault()`: adicionar arm `capability === "tts" && providerId === "openai" && fieldName === "tts_api_url"` retornando `"https://api.openai.com/v1"` (URL base; o adaptador appenda `/audio/speech` internamente); NÃO pré-preencher `tts_model_name` — campo livre, usuário digita `tts-1`, `tts-1-hd` etc.
4. `frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx`:
   - `BACKEND_PROVIDER_AVAILABILITY.tts`: adicionar `openai: "coming_soon"`
   - `const COMING_SOON_COUNT` ou equivalente: atualizar de 20 → 21
   - describe text `"contains the exact 20 backend pairs"` → `"contains the exact 21 backend pairs"`

**Acceptance Criteria:**
- [x] `python scripts/validate_provider_availability_evidence.py --phase inventory` sai com código 0 (23 pares, 20 coming_soon, 3 removed)
- [x] `TTS_PROVIDERS.map(p => p.id).sort()` inclui `"openai"`; `providerCardState("tts", "openai", null)` retorna `"coming_soon"`
- [x] `BACKEND_PROVIDER_AVAILABILITY.tts` no teste de parity inclui `openai: "coming_soon"` e `COMING_SOON_COUNT === 21`
- [x] Testes do frontend passam sem regressão

**Validation:**
```bash
python scripts/validate_provider_availability_evidence.py --phase inventory
cd frontend/artifacts/neurostack && npx vitest run src/pages/clinic-config-api-settings.test.tsx --reporter=verbose
```

**depends-on:** group-1

---

## QA Criteria

- [x] O card "OpenAI TTS" aparece na aba API com badge "EM BREVE" e o editor não abre ao clicar
- [x] O card ElevenLabs e o card Google continuam com badge "EM BREVE" sem regressão
- [x] `TtsCredentialsPatch(tts_provider="openai")` valida sem erro no schema, mas o PATCH operacional permanece bloqueado enquanto o provedor estiver `coming_soon`, até sua promoção/ativação
- [x] Salvar `tts_provider: "openai"` sem `tts_voice_id` retorna `tts_configuration_incomplete` na síntese

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| OpenAI muda a lista de vozes válidas e o frontend fica desatualizado | Low | A voz é campo livre (`tts_voice_id`), não enum; o erro de voz inválida vira `tts_upstream_http_error_400` |
| O validador `--phase ui` usa AST para ler `clinic-config.tsx`; sintaxas TS complexas podem falhar o parse JS-feito-em-Python | Low | O padrão `as const satisfies` já está no arquivo atual e o validador já o lê corretamente para outros provedores |
| `BASE_RECEIPTS_SHA256` no validador é protegido por digest — adicionar uma linha invalida o digest | Medium | O digest `BASE_RECEIPTS_SHA256` é verificado apenas no `--phase release`, não no `--phase inventory` ou `--phase ui`; só bloqueia CI de produção, que exige novo release stamp |

---

## Review Results

### Plan Review 1 — 2026-09-27 — FIX-FIRST (amended)

Reviewer agent `ab1efefecf97bfd6b`. Target: WISH.md at scaffold commit (pre-amendment).

**Gaps found (all resolved by amendment above):**
- CRITICAL: `tests/services/test_api_credentials.py` hardcoda dict TTS e contagens; faltou no plano → adicionado ao Group 1
- CRITICAL: `clinic-config-api-settings.test.tsx` constantes (`COMING_SOON_COUNT`, `BACKEND_PROVIDER_AVAILABILITY.tts`, describe text) não nomeadas → explicitadas no Group 2
- CRITICAL: `--phase ui` NÃO invoca `_validate_product_promotion()` (isso só acontece em `--phase release`); critério de sucesso era falso → substituído por vitest parity test
- HIGH: `tests/services/test_safe_egress.py` não cobria `tts_openai` → adicionado ao Group 1
- HIGH: linha do recibo precisava de 10 colunas especificadas → Deliverable 1 do Group 2 detalhado
- HIGH: `newProviderFieldDefault()` precisava do arm exato e distinção URL vs model → Deliverable 3 do Group 2 corrigido

Verdict pre-amendment: FIX-FIRST. Post-amendment: re-review dispatched.

### Plan Review 2 — 2026-09-27 — SHIP

Reviewer agent `aa6d5755d184255c0`. Target: WISH.md post-amendment.

All six prior gaps confirmed resolved:
- C1: `tests/services/test_api_credentials.py` → Group 1 Deliverable 7 cobre `test_provider_availability_has_sole_gemini_active_overlay` com contagens 20→21 e 19→20
- C2: constantes `COMING_SOON_COUNT`, `BACKEND_PROVIDER_AVAILABILITY.tts`, describe text → Group 2 Deliverable 4 nomeados explicitamente
- C3: critério `--phase ui` falso removido; sincronismo frontend↔backend coberto pelo vitest parity test
- H4: `test_safe_egress.py` `tts_openai` asserção → Group 1 Deliverable 6
- H5: linha de recibo 10 colunas com sentinelas → Group 2 Deliverable 1 detalhado
- H6: arm exato `newProviderFieldDefault()` e distinção URL/model → Group 2 Deliverable 3 corrigido

Contagens verificadas contra o código real: dict 20→21, receipt inventory 22→23, coming_soon 19→20 — todos consistentes com `test_api_credentials.py:64,71` e `validate_provider_availability_evidence.py:215,220`.

**Verdict: SHIP — plano aprovado para `work`.**

---

## Files to Create/Modify

```
src/services/tts_service.py
src/services/safe_egress.py
src/services/api_credentials.py
src/api/schemas/config.py
tests/services/test_tts_service.py
tests/services/test_safe_egress.py
tests/services/test_api_credentials.py
tests/api/test_api_credentials.py
.genie/wishes/api-provider-availability/evidence/provider-availability-receipts.md
scripts/validate_provider_availability_evidence.py
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
```

## Execution Review Results

### Group 1 — Backend — SHIP

- Independent reviewer found no CRITICAL/HIGH/MEDIUM/LOW gaps.
- Verified OpenAI `POST /v1/audio/speech`, Bearer authentication, exact `model`/`input`/`voice` payload, and `api.openai.com` host pinning.
- Verified missing voice handling, secret-free bucketed 4xx errors, provider allowlist/availability, schema acceptance, and focused regression coverage.
- Orchestrator validation: `poetry run pytest tests/services/test_tts_service.py tests/services/test_api_credentials.py tests/services/test_safe_egress.py tests/api/test_api_credentials.py -x -q` — 253 passed.
- Orchestrator validation: `poetry run mypy src/services/tts_service.py src/services/safe_egress.py src/services/api_credentials.py src/api/schemas/config.py --no-error-summary` — exit 0.
- Genie task completion was not recorded because `.genie/genie.db` was unavailable; execution continued under the documented no-task-row fallback.

### Group 2 — Evidence and frontend — SHIP after fix loop 1

- Initial review found one HIGH clarity gap: a test constant named as a coming-soon count held the 21-card total even though only 20 providers are `coming_soon`.
- Fix loop 1 split the values into explicit totals (21 provider cards, 20 coming-soon providers), removed subtract-one arithmetic, and corrected test wording; re-review found no remaining gaps.
- Verified the 10-column OpenAI TTS receipt, 23-pair inventory (20 `coming_soon`, 3 `removed`), third TTS card copy/state, OpenAI URL-only default, free model field, and exact frontend/backend parity.
- Orchestrator validation: `poetry run python scripts/validate_provider_availability_evidence.py --phase inventory` — passed.
- Orchestrator validation: `cd frontend/artifacts/neurostack && npx vitest run src/pages/clinic-config-api-settings.test.tsx --reporter=verbose` — 47 passed; existing non-failing React `act(...)` warnings remain.
- Genie task completion was not recorded because `.genie/genie.db` was unavailable; execution continued under the documented no-task-row fallback.

### Aggregate Quality Review — SHIP after fix loop 1

- Initial review found stale validator-test expectations (22/19 instead of 23/20) and a QA statement that contradicted the intentional `coming_soon` availability gate.
- Fix loop 1 updated the dedicated validator tests and clarified that the schema accepts OpenAI while operational PATCH remains blocked until activation; 41 validator tests and inventory validation passed.
- Security, maintainability, performance/reliability, and test-quality re-review found no remaining CRITICAL/HIGH gaps.
- Accepted residual: `BASE_RECEIPTS_SHA256` remains bound to the prior receipt, so release validation stays fail-closed until the separate evidence-rebinding/release-stamp workflow.

### Final Execution Review — 2026-09-28T23:16:09Z — FIX-FIRST

- Base HEAD: `96edb2579ded08c3a1b34549bb73d1ee86a77325`.
- Focused backend/API suite: 253 passed; touched-module mypy exit 0.
- Validator tests: 41 passed; inventory validation passed at 23 pairs / 20 `coming_soon` / 3 `removed`.
- Frontend focused Vitest, typecheck, and production build passed for the OpenAI TTS scope.
- HIGH gap: adapter-to-`tts_openai` policy wiring lacked hostile-host integration coverage.
- MEDIUM gap: the happy path did not explicitly assert POST and a query-free endpoint.
- Verdict: FIX-FIRST.

### Final Execution Re-review — 2026-09-28T23:23:37Z — SHIP

- Fix loop 1 changed only `tests/services/test_tts_service.py`.
- OpenAI happy path now asserts POST and exact query-free `https://api.openai.com/v1/audio/speech`.
- Hostile OpenAI URL is rejected through the adapter's `tts_openai` policy before any transport request.
- OpenAI TTS focused suite: 10 passed; combined backend/API/security suite: 254 passed; Black, isort, and scoped diff-check passed.
- Full Python suite evidence: 3,061 passed, 16 skipped, 4 unrelated dirty-worktree failures outside the wish; every TTS/credentials/egress/validator test passed.
- Non-blocking follow-up: shared binary-response streaming/MIME hardening for all TTS providers.
- Accepted out-of-scope residual: release receipt digest rebinding.
- Verdict: SHIP; no wish-owned CRITICAL/HIGH gaps remain.

### Post-SHIP Amendment — 2026-09-28 — status-specific upstream error code

- Decision (user): OpenAI TTS must expose the exact upstream HTTP status, aligned with ElevenLabs and Google (`tts_upstream_http_error_{status}`), instead of a bucketed `tts_upstream_http_error_4xx`.
- Scope of prior verdicts: the Group 1 SHIP ("secret-free bucketed 4xx errors"), the Aggregate Quality Review and the Final Execution Re-review cover the PRE-amendment adapter only. They do not cover this delta.
- Change: fixer removed the 4xx branch in `_synthesize_openai` (`src/services/tts_service.py`) so HTTP errors flow through the shared `_raise_for_status`; `tests/services/test_tts_service.py::test_openai_upstream_error_keeps_status_and_is_secret_free` asserts 401 → `_401`, 429 → `_429`, distinct codes, and no key/body in `repr`. Wish text (Scope IN, Deliverable 5, Group 1 acceptance criterion) reworded to the status-specific contract.
- Orchestrator validation: `poetry run pytest tests/services/test_tts_service.py tests/services/test_api_credentials.py tests/services/test_safe_egress.py tests/api/test_api_credentials.py -x -q` — 254 passed; `poetry run mypy src/services/tts_service.py src/services/safe_egress.py src/services/api_credentials.py src/api/schemas/config.py --no-error-summary` — exit 0.
- Delta-only independent review (reviewer agent `acedc82dadf9e7f1b`, read-only): code and test correct and non-vacuous; error path identical to ElevenLabs/Google; `webhook.py` logs only `reason=<code>`; no `tts_upstream_http_error_4xx` references remain repo-wide; same 254 passed / mypy exit 0 reproduced. Gaps: two MEDIUM stale-wording hits in this WISH.md (Scope IN, Deliverable 5) — fixed by the orchestrator in the same pass; one LOW on the Risks row citing `_400`, judged a non-issue (an invalid OpenAI voice is a genuine upstream 400, which the status-specific contract still reports as `_400`). The reviewer's headline verdict was FIX-FIRST on those wording gaps only; no code or test gap was raised, and the wording gaps are closed. Wording-only fix was not re-reviewed.
- The Group 1 line "secret-free bucketed 4xx errors" above is retained as the historical record of what was reviewed at the time.
- Integration status (verified 2026-09-28 against `origin/main` 5e4950c): the OpenAI TTS scope shipped via PR #38 (`feat/tts-openai-provider`, merge 5e4950c) in its PRE-amendment form (bucketed `tts_upstream_http_error_4xx`). The `Branch` field above (`wish/tts-openai-provider`) never existed.
- Status-specific delta isolated on `fix/tts-openai-status-errors` (worktree `.worktrees/tts-openai-status-errors`) and committed as `0d9d7a7`: 2 files — `src/services/tts_service.py` (-2 lines) and `tests/services/test_tts_service.py`; PR #40 está aberto sobre `main`. Validation in that worktree: gate + validator tests 295 passed, mypy exit 0, `--phase inventory` passed. The OpenAI hunks still sitting in the `wish/neurostack-core-dedomainification` working tree duplicate what is already on `main`.
- Validator script: the formatting-only reflow hunks were reverted from the dedomainification working tree at the user's request; note `main` already carries that reflow (PR #38), so the two differ only in formatting.

### Status-specific Delta Re-review — 2026-09-29T00:42:20Z — SHIP

- **Commit:** `0d9d7a7f30600977e2f7977a3e93155d6f380e26`; PR #40; conventional commit
  `fix(tts): keep exact upstream status in OpenAI errors`.
- **Reviewer:** `/root/tts_status_delta_reviewer`; no CRITICAL/HIGH/MEDIUM/LOW gaps.
- **Evidence:** independent 10-test OpenAI TTS suite, scoped mypy and diff-check passed; the test is
  non-vacuous, keeps 401/429 distinct, proves redaction and preserves POST/query-free/hostile-host
  coverage. Normative wording no longer describes bucketed errors.

### Completion Gate — 2026-09-29T00:51:43Z — SHIP, merged

- **Reviewer:** `/root/tts_completion_final_gate`; merge candidate over `origin/main`
  `c6ed5948f9cb905290de45a83a92a15a33208cf3` had no gaps or conflicts.
- **Orchestrator evidence:** 254 focused tests and 41 validator tests passed; scoped mypy, inventory,
  Black, isort, flake8 and full mypy passed. The full Python gate reached 3,079 passed and 16 skipped;
  its two `SchedulingLinkPatchBoundary` failures were reproduced on current `main`, touch no TTS file
  and are therefore a pre-existing baseline failure rather than a PR #40 regression.
- **Delivery:** PR #40 foi integrado pelo workflow server-side em `origin/main` no merge commit
  `be0a6547c6c7462c1cbeeeb39030a7d10ee781b8`; `0d9d7a7` é ancestral do commit publicado.
- **Post-merge smoke:** no commit publicado, 254 testes focados e 41 testes do validador passaram;
  scoped mypy, inventory e diff-check saíram com código 0. O wish está `SHIPPED`.
- **Superseded delivery checkpoint:** `0d9d7a7` foi inicialmente aberto como PR #40 aguardando
  integração; o Completion Gate acima registra a integração posterior e é a fonte atual de verdade.
  A árvore de trabalho `neurostack-core-dedomainification` permaneceu intencionalmente intocada.
