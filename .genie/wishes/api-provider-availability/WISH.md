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
- PATCH fail-closed com 422 `provider_not_available`, preservando eco legado idêntico.
- Cards bloqueados com selo laranja de 10 px e remoção de “Configuração apenas”.
- Inventário/checker receipt-only de 22 decisões, sem censo nem dados sensíveis.

### OUT

- Runtime, adapters, webhooks, readiness, egress, migração e censo de produção.
- Qualquer `active`: promoção futura exige recibo direto não mockado e validação do par.

## Success Criteria

- [ ] Inventário contém 19 `coming_soon` exibíveis e três `removed`; checker passa sem arquivo/argumento de censo.
- [ ] Matrizes Python/TS são idênticas e todos os 19 pares são `coming_soon`.
- [ ] PATCH de mudança efetiva para qualquer par exibível devolve 422 `provider_not_available`, zero mutação; eco legado idêntico passa.
- [ ] Todo card mostra o selo laranja exato em PT/EN, sem clique/foco/input/payload; não resta “Configuração apenas”.
- [ ] Runtime não muda; build, testes focados, paridade e gate agregado passam.

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

**Files:** `src/services/api_credentials.py`, `src/api/schemas/config.py`, `src/api/routes/config.py`, testes de credenciais.

**Acceptance:** 19 pares exatos, 3 removidos ausentes, 422 atômico, eco legado permitido, nenhum código legado de disponibilidade.

**Validation:**
```bash
poetry run pytest tests/services/test_api_credentials.py tests/api/test_api_credentials.py -q
make quality
```

### Group 3: Interface `EM BREVE`

**Goal:** Espelhar a matriz em TS e bloquear visualmente todos os cards.

**Files:** `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`, componentes/testes de API settings.

**Acceptance:** selo PT/EN laranja 10 px, zero inputs/payload/seleção, zero “Configuração apenas”, paridade com backend.

**Validation:**
```bash
cd frontend/artifacts/neurostack && npm test -- --runInBand
npm run build
```

### Group 4: Gate integrado

**Goal:** Validar paridade, regressão e release atômica backend+frontend.

**Acceptance:** todos os critérios do wish e validações dos grupos passam; runtime permanece intocado.

**Validation:**
```bash
poetry run python scripts/validate_provider_availability_evidence.py --phase release
make quality
cd frontend/artifacts/neurostack && npm run build
```

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
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-multimodal.test.tsx
frontend/artifacts/neurostack/src/components/api-settings/provider-credential-card.tsx
frontend/artifacts/neurostack/src/components/api-settings/provider-credential-card.test.tsx
```
