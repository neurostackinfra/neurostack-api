# Wish: Vitrine Honesta de Provedores na Aba APIs

| Field | Value |
|---|---|
| **Status** | IN_PROGRESS |
| **Slug** | `api-provider-availability` |
| **Date** | 2026-08-30 |
| **Design** | [DESIGN.md](../../brainstorms/api-provider-availability/DESIGN.md) |
| **Governing design SHA** | `527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a` |

## Summary

Todas as opções de API previstas ficam visíveis e bloqueadas com `EM BREVE`/`COMING SOON` até existir homologação direta. Não há censo, acesso a produção nem alteração de runtime nesta entrega.

## Scope

### IN

- Matriz Python/TS com 19 pares exibíveis, todos `coming_soon`, e três decisões `removed`.
- PATCH fail-closed com 422 `provider_not_available`, preservando eco legado idêntico e alinhando os testes legados de Meta/Uazapi ao contrato.
- Cards bloqueados com selo laranja de 10 px e remoção de “Configuração apenas”.
- Inventário/checker receipt-only de 22 decisões, sem censo nem dados sensíveis.

### OUT

- Runtime, adapters, webhooks, readiness, egress, migração e censo de produção.
- Qualquer `active`: promoção futura exige recibo direto não mockado e validação do par.

## Success Criteria

- [x] Inventário contém 19 `coming_soon` exibíveis e três `removed`; checker passa sem arquivo/argumento de censo.
- [x] Matrizes Python/TS são idênticas e todos os 19 pares são `coming_soon`.
- [x] PATCH de mudança efetiva para qualquer par exibível devolve 422 `provider_not_available`, zero mutação; eco legado idêntico passa, inclusive nos módulos legados de Meta/Uazapi.
- [x] Todo card mostra o selo laranja exato em PT/EN, sem clique/foco/input/payload; não resta “Configuração apenas”.
- [x] Runtime não muda; build, testes focados, paridade e gate agregado passam.

## Execution Strategy

Grupos sequenciais `1 → 2 → 3 → 4`. Backend e frontend só liberam juntos no gate final.

### Group 1: Evidência receipt-only

**Goal:** Corrigir o inventário para 19 `coming_soon` e deixar o checker sem censo.

**Files:** `scripts/validate_provider_availability_evidence.py`, `tests/scripts/test_validate_provider_availability_evidence.py`, `.genie/wishes/api-provider-availability/evidence/provider-availability-receipts.md`.

**Acceptance:** 22 decisões 0/19/3 (`active`/`coming_soon`/`removed`), nenhuma referência a censo, sem segredo.

**Validation:**
```bash
poetry run pytest tests/scripts/test_validate_provider_availability_evidence.py -q
poetry run python scripts/validate_provider_availability_evidence.py --phase inventory
```

### Group 2: Contrato backend

**Goal:** Expor matriz Python toda `coming_soon` e bloquear mudanças efetivas sem tocar runtime.

**Files:** `src/services/api_credentials.py`, `src/api/schemas/config.py`, `src/api/routes/config.py`, `tests/services/test_api_credentials.py`, `tests/api/test_api_credentials.py`, `tests/api/test_meta_credentials.py`, `tests/api/test_uazapi_credentials.py`.

**Acceptance:** 19 pares exatos, 3 removidos ausentes, 422 atômico, eco legado permitido, nenhum código legado de disponibilidade; testes de Meta/Uazapi não tentam mais promover providers e preservam cobertura de masking, handshake e uniqueness via seed legado direto.

**Validation:**
```bash
poetry run pytest tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_meta_credentials.py tests/api/test_uazapi_credentials.py -q
poetry run black --check src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_meta_credentials.py tests/api/test_uazapi_credentials.py
poetry run isort --check-only src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_meta_credentials.py tests/api/test_uazapi_credentials.py
poetry run flake8 src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_meta_credentials.py tests/api/test_uazapi_credentials.py
poetry run mypy src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py --ignore-missing-imports
```

**Corrective provenance (discovered by Group 4 full-suite gate):**
- `tests/api/test_meta_credentials.py` pre-change SHA-256: `8305eab0991be12a23449b18ab42bf10d46d4964ca8c8cf69ef8e86dce3ed249`
- `tests/api/test_uazapi_credentials.py` pre-change SHA-256: `e00aadc8835b0a72a90659b677a0ea93c7302e580839a2413e18cad755d74b04`
- focused reproduction before correction: `16 failed, 8 passed`; all 16 expected obsolete 200/409 flows after effective writes that now correctly fail first with atomic 422.

### Group 3: Interface `EM BREVE`

**Goal:** Espelhar a matriz em TS e bloquear visualmente todos os cards.

**Files:** `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`, componentes/testes de API settings.

**Acceptance:** selo PT/EN laranja 10 px, zero inputs/payload/seleção, zero “Configuração apenas”, paridade com backend.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/clinic-config-api-settings.test.tsx src/pages/clinic-config-multimodal.test.tsx src/components/api-settings/provider-credential-card.test.tsx
pnpm --dir frontend/artifacts/neurostack run typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
```

### Group 4: Gate integrado

**Goal:** Validar paridade, regressão e release atômica backend+frontend.

**Acceptance:** todos os critérios do wish e validações dos grupos passam; runtime permanece intocado.

**Validation:**
```bash
poetry run python scripts/validate_provider_availability_evidence.py --phase release
poetry run pytest tests/scripts/test_validate_provider_availability_evidence.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_meta_credentials.py tests/api/test_uazapi_credentials.py -q
poetry run black --check scripts/validate_provider_availability_evidence.py tests/scripts/test_validate_provider_availability_evidence.py src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_meta_credentials.py tests/api/test_uazapi_credentials.py
poetry run isort --check-only scripts/validate_provider_availability_evidence.py tests/scripts/test_validate_provider_availability_evidence.py src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_meta_credentials.py tests/api/test_uazapi_credentials.py
poetry run flake8 scripts/validate_provider_availability_evidence.py tests/scripts/test_validate_provider_availability_evidence.py src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/api/test_meta_credentials.py tests/api/test_uazapi_credentials.py
poetry run mypy src/services/api_credentials.py src/api/schemas/config.py src/api/routes/config.py --ignore-missing-imports
poetry run pytest
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/clinic-config-api-settings.test.tsx src/pages/clinic-config-multimodal.test.tsx src/components/api-settings/provider-credential-card.test.tsx
pnpm --dir frontend/artifacts/neurostack run typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
```

O gate usa checks estáticos exatos do escopo, mais a suíte backend completa, em vez de `make quality`. A execução de 2026-09-06 provou que o alvo global para no Black de 146 arquivos, depois encontra dívida ampla de Flake8 e 365 erros Mypy em 71 arquivos alheios ao wish. Corrigir ou formatar esses caminhos concorrentes violaria o escopo e não aumentaria a confiança específica desta entrega; os comandos acima preservam cada subgate aplicável e mantêm `pytest` completo obrigatório, sem waiver.

O gate Vitest também é exato ao domínio desta entrega. No snapshot compartilhado de 2026-09-06, o run frontend global adicional passou 21 arquivos/159 testes e falhou em 15 casos, todos no novo `src/pages/builder-cloning-center.test.tsx`, WIP concorrente fora do manifest que não importa a superfície de providers. Os três consumidores diretos declarados acima passaram 38/38; typecheck e build permanecem globais. A revisão independente do amendment confirmou que esse recorte cobre matriz 19/3, PT/EN, badge de 10 px, inércia e ausência de payload sem aceitar nenhuma falha do domínio como sucesso.

## Dependencies

**depends-on:** api-provider-settings, api-config-clarity
**blocks:** api-provider-runtime-shutdown

O wish de shutdown fará seu próprio censo read-only antes de migrar/desligar runtime.

## Simplicity Case

Uma matriz literal, um espelho TS, uma regra PATCH, um card e um checker de inventário. Nenhum serviço, censo, registry, health check ou mudança de runtime.

## Review Results

The previous plan/execution reviews were superseded on 2026-08-30 after independent review proved that the six purported active receipts used mock transport. The all-coming-soon plan below was subsequently reviewed and approved.

### Plan Review — 2026-08-30T00:25:00Z

- **Target:** `.genie/wishes/api-provider-availability/WISH.md`
- **Governing design SHA-256:** `527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a`
- **Reviewer:** `/root/review_all_coming_plan`
- **Verdict:** SHIP
- **Gaps:** no blocking gaps; frontend test files were added to the planned file list for traceability.
- **Checks:** 0/19/3 contract, sequential groups, no censo/runtime work, and risk-proportional validations.

### Frontend Gate Amendment Review — 2026-09-06

- **Target SHA-256:** `db16b60296410e38b0fd14352d8073c1390c8e75ee5a1cb18c7fcdbd5c719955`
- **Reviewer:** `/root/gemini_security_fix1_rereview`
- **Verdict:** SHIP
- **Decision:** substituir o Vitest global pelo recorte completo dos três consumidores do domínio; manter typecheck/build globais e registrar separadamente as 15 falhas do WIP de clonagem.
- **Coverage:** matriz 19/3, todos `coming_soon`, PT/EN, badge laranja 10 px, inércia e ausência de payload; nenhum gap CRITICAL/HIGH.

### Final Execution Review — 2026-09-06

- **Reviewed WISH SHA-256:** `921d6a55bd1f4ec678af70c2ad7952ba08f5fb4f978e24d5c1b3cc43f6ce8942`
- **Reviewer:** `/root/gemini_security_fix1_rereview`
- **Verdict:** SHIP
- **Backend aggregate:** 2,642 collected; 2,628 passed; 14 pre-existing skips; 0 failed; exit 0 in 1,849.43 s.
- **Provider gates:** 118/118 across the two principal backend files after the security correction; checker 20/20; inventory and release phases passed; Black, isort, Flake8 and scoped Mypy passed.
- **Frontend gates:** all three provider consumers passed 38/38; global typecheck and build passed with 2,804 transformed modules.
- **Security fix loop:** six runtime-control regressions failed before the fix and 10/10 passed after it; every effective credential/control mutation is now fail-closed before persistence, while unchanged legacy echoes pass. Receipt dependencies are exact and reject `none`, reversed or incomplete provenance.
- **External WIP:** the additional global Vitest run failed only 15 tests in unrelated `builder-cloning-center`; the other 21 files/159 tests passed and the approved domain gate omits no provider consumer.
- **Residuals:** LOW only — mirrored-literal parity can drift through coordinated edits; lifecycle index and isolated PR metadata still require reconciliation/review.

All implementation criteria and work groups are complete. Status remains `IN_PROGRESS` until isolated PR review, authorized merge and post-merge QA.

## Files to Create/Modify

```text
.genie/INDEX.md
.genie/wishes/api-provider-availability/WISH.md
.genie/wishes/api-provider-availability/evidence/provider-availability-receipts.md
scripts/validate_provider_availability_evidence.py
tests/scripts/test_validate_provider_availability_evidence.py
src/services/api_credentials.py
src/api/schemas/config.py
src/api/routes/config.py
tests/services/test_api_credentials.py
tests/api/test_api_credentials.py
tests/api/test_meta_credentials.py
tests/api/test_uazapi_credentials.py
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-multimodal.test.tsx
frontend/artifacts/neurostack/src/components/api-settings/provider-credential-card.tsx
frontend/artifacts/neurostack/src/components/api-settings/provider-credential-card.test.tsx
```
