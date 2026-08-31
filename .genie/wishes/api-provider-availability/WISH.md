# Wish: Vitrine Honesta de Provedores na Aba APIs

| Field | Value |
|---|---|
| **Status** | IN_PROGRESS |
| **Slug** | `api-provider-availability` |
| **Date** | 2026-08-30 |
| **Design** | [DESIGN.md](../../brainstorms/api-provider-availability/DESIGN.md) |
| **Governing design SHA** | `527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a` (conteúdo pré-emenda; o design foi emendado em 2026-08-31 e não recarimbado) |

## Summary

Todas as opções de API previstas ficam visíveis e bloqueadas com `EM BREVE`/`COMING SOON` até existir homologação direta, **exceto o provider que o tenant já tem persistido**, que continua editável e exibe `CONFIGURADO`/`INCOMPLETO`. Nenhum par é promovido a `active`. Não há censo, acesso a produção nem alteração de runtime nesta entrega.

## Scope

### IN

- Matriz Python/TS com 19 pares exibíveis, todos `coming_soon`, e três decisões `removed`.
- PATCH fail-closed na adoção com 422 `provider_not_available`, preservando eco legado idêntico.
- Exceção de manutenção (emenda 2026-08-31): o provider já persistido segue editável, inclusive com bundle incompleto, e exibe `CONFIGURADO`/`INCOMPLETO`.
- Cards bloqueados com selo laranja de 10 px para pares não persistidos e remoção de “Configuração apenas”.
- Inventário/checker receipt-only de 22 decisões, sem censo nem dados sensíveis.

### OUT

- Runtime, adapters, webhooks, readiness, egress, migração e censo de produção.
- Qualquer `active`: promoção futura exige recibo direto não mockado e validação do par.

## Success Criteria

- [ ] Inventário contém 19 `coming_soon` exibíveis e três `removed`; checker passa sem arquivo/argumento de censo.
- [ ] Matrizes Python/TS são idênticas e todos os 19 pares são `coming_soon`.
- [ ] PATCH que adota provider não persistido (troca, bootstrap ou limpeza) devolve 422 `provider_not_available`, zero mutação; eco legado idêntico e manutenção do provider persistido passam.
- [ ] Gravar o bundle de outro provider de WhatsApp é 422 mesmo com `whatsapp_provider` intocado, e o tenant continua mantendo o próprio bundle.
- [ ] Ligar `tts_enabled` sobre bundle incompleto é 422 mesmo quando o mesmo patch traz campos de credencial; sobre bundle completo passa.
- [ ] Card de par não persistido mostra o selo laranja exato em PT/EN, sem clique/foco/input/payload; não resta “Configuração apenas”.
- [ ] Card do par persistido mostra `CONFIGURADO`/`CONFIGURED` com bundle completo e `INCOMPLETO`/`INCOMPLETE` com bundle parcial, ambos editáveis; `signature_pending` nunca vira `CONFIGURADO`.
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

**Goal:** Expor matriz Python toda `coming_soon` e bloquear a adoção de provider sem tocar runtime.

**Files:** `src/services/api_credentials.py`, `src/api/schemas/config.py`, `src/api/routes/config.py`, testes de credenciais.

**Acceptance:** 19 pares exatos, 3 removidos ausentes, 422 atômico na adoção, eco legado e manutenção do provider persistido permitidos, adoção por bundle de WhatsApp rejeitada mesmo com `whatsapp_provider` intocado, controles sem credencial (`multimodal_text_fallback_enabled`, `audio_max_seconds`) livres, gatilho de voz exige bundle completo mesmo quando o patch traz campos de credencial, nenhum código legado de disponibilidade.

**Validation:**
```bash
poetry run pytest tests/services/test_api_credentials.py tests/api/test_api_credentials.py -q
make quality
```

### Group 3: Interface `EM BREVE` + manutenção

**Goal:** Espelhar a matriz em TS, bloquear os cards não persistidos e liberar edição do par persistido.

**Files:** `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`, componentes/testes de API settings.

**Acceptance:** selo PT/EN laranja 10 px e zero inputs/payload/seleção para pares não persistidos; `CONFIGURADO`/`INCOMPLETO` editáveis para o par persistido, com segredo nunca pré-preenchido e PATCH só de diffs; zero “Configuração apenas”; paridade com backend.

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

**depends-on:** api-provider-settings, api-config-clarity, voice-response-triggers (`tts_reply_policy`), fallback-llm-multimodal (`multimodal_text_fallback_enabled`)
**blocks:** api-provider-runtime-shutdown

O wish de shutdown fará seu próprio censo read-only antes de migrar/desligar runtime.

## Simplicity Case

Uma matriz literal, um espelho TS, uma regra PATCH, um card e um checker de inventário. Nenhum serviço, censo, registry, health check ou mudança de runtime.

## Review Results

The previous plan/execution reviews were superseded on 2026-08-30 after independent review proved that the six purported active receipts used mock transport. The plan below — every displayable pair `coming_soon`, no pair reachable without a direct receipt — was subsequently reviewed and approved. The 2026-08-31 amendment narrowed the write gate from “provider é `coming_soon`?” to “o tenant já possui este provider?” without touching that availability contract.

### Plan Review — 2026-08-30T00:25:00Z

- **Target:** `.genie/wishes/api-provider-availability/WISH.md`
- **Governing design SHA-256:** `527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a`
- **Reviewer:** `/root/review_all_coming_plan`
- **Verdict:** SHIP
- **Gaps:** no blocking gaps; frontend test files were added to the planned file list for traceability.
- **Checks:** 0/19/3 contract, sequential groups, no censo/runtime work, and risk-proportional validations.

### Amendment — 2026-08-31 (posterior ao SHIP de 2026-08-30)

Bloquear escrita no provider que o tenant **já usa** não protegia ninguém e tornava um bundle parcial irreparável pela própria UI (caso concreto: Meta em `signature_pending`, faltando só `app_secret`). O gate passou a chavear em “o tenant já possui este provider?” em vez de “o provider é `coming_soon`?”.

A garantia de honestidade permanece intacta: `PROVIDER_AVAILABILITY` não muda, nenhum par vira `active`, e o inventário 0/19/3 e o checker de recibos seguem válidos sem alteração. O `Plan Review — 2026-08-30T00:25:00Z` acima cobre apenas o conteúdo pré-emenda; a emenda não foi recarimbada.

### Amendment Review — 2026-08-31 (independente)

Revisão independente da emenda retornou **BLOCK** com dois bypasses reproduzidos, ambos corrigidos e cobertos por teste:

- **Crítico — adoção de WhatsApp por bundle.** `whatsapp_provider` não tem referência de runtime: o webhook resolve o tenant por `meta_phone_number_id` e escolhe a saída pelo token presente. Um tenant `zapi` que gravasse identidade Meta sem tocar no rótulo passava pelo gate e ficava ao vivo na Meta, com `meta_verify_token` cunhado logo depois. Corrigido comparando grupos de campos por provider.
- **Alto — gatilho de voz burlado por bundling.** A checagem de bundle completo só rodava quando o patch era só de controle; anexar qualquer campo de credencial ligava `tts_enabled` sobre bundle incompleto. Corrigido verificando a transição de forma independente, sempre contra o estado armazenado.

## Files to Create/Modify

```text
.genie/INDEX.md
.genie/brainstorms/api-provider-availability/DESIGN.md
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
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-multimodal.test.tsx
frontend/artifacts/neurostack/src/components/api-settings/provider-credential-card.tsx
frontend/artifacts/neurostack/src/components/api-settings/provider-credential-card.test.tsx
```
