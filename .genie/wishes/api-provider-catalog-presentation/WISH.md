# Wish: Hierarquia da aba APIs

| Field | Value |
|---|---|
| **Status** | IN_PROGRESS |
| **Execution** | COMPLETE — Grupo 1 revisado e validado; handoff para review |
| **Slug** | `api-provider-catalog-presentation` |
| **Date** | 2026-10-02 |
| **Author** | Lorenzo |
| **Appetite** | small |
| **Branch** | `wish/api-provider-catalog-presentation` |
| **Repos touched** | neurostack-api |
| **Design** | [DESIGN.md](../../brainstorms/api-provider-catalog-presentation/DESIGN.md) |

## Summary

Corrige exclusivamente a hierarquia visual da aba APIs: ordem dos blocos, qualificadores,
cabeçalho do módulo de voz e posição dos gatilhos. A matriz atual de providers, os editores e
todo comportamento backend/runtime permanecem inalterados.

## Problem

A aba APIs atualmente não apresenta no DOM a hierarquia visual definida nos Success Criteria em PT e EN nos viewports desktop e mobile.

## Scope

### IN

- Renderizar API WhatsApp → API LLM → API LLM Fallback Multimodal → Módulo Voz → API STT →
  API TTS → Gatilhos de voz → Outros gatilhos em breve.
- Exibir WhatsApp, LLM e STT como obrigatórios e Fallback/TTS como opcionais.
- Localizar Módulo Voz para PT/EN e corrigir a copy portuguesa dos gatilhos.
- Preservar uma única instância dos controles de voz e todos os fluxos existentes.
- Adicionar regressão de ordem DOM e congelamento da matriz `14 active / 7 coming_soon`.

### OUT

- Backend, schema, PATCH, readiness, runtime, egress, telemetria ou persistência.
- Ativar, desativar, adicionar, remover, reordenar ou relabelar cards individuais de provider.
- WhatsApp inbound, Dropout multimodal, OpenRouter explícito para `llm/outros` ou probe live.
- Catálogo dinâmico, endpoint de catálogo, feature flag ou novo estado público.

## Decisions

| # | Decision | Rationale |
|---:|---|---|
| 1 | `API_CAPABILITIES` continua autoritativo para os blocos | Evita registry paralelo. |
| 2 | `ApiCredentialsForm` recebe slot React opcional | Intercala voz sem acoplar seus fetches/estado. |
| 3 | Matriz permanece `14/7` | O pedido é visual, não ativação. |
| 4 | Copy genérica de `llm/outros` permanece | Não alegar OpenRouter sem ID/contrato específico. |
| 5 | Uma wave frontend | Diff pequeno, coeso e sequencial. |

## Simplicity Case

- **Simplest complete design:** reordenar uma lista declarativa, inserir um cabeçalho localizado e
  mover o componente existente por uma prop opcional.
- **Added machinery:** uma única prop React sem estado, necessária para a ordem intercalada.
- **Deferred:** toda mudança de provider fica para um wish futuro com contrato próprio.
- **Removed complexity:** rollout em fases, census, assurance, checker, endpoint policy, telemetria e
  dependências de ativação do plano superseded não pertencem a esta entrega visual.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [x] A ordem DOM PT é exatamente API WhatsApp, API LLM, API LLM Fallback Multimodal, Módulo Voz,
  API STT, API TTS, Gatilhos de voz e Outros gatilhos em breve.
- [x] WhatsApp, LLM e STT mostram `obrigatório`; Fallback e TTS mostram `opcional`.
- [x] EN mostra `Voice Module` e `Required`/`Optional` nas posições equivalentes.
- [x] Existe exatamente um `VoiceResponseControls`, depois de TTS, sem mudança de GET/PATCH.
- [x] `API_PROVIDER_AVAILABILITY` permanece exatamente `14 active / 7 coming_soon`.
- [x] Nenhum provider, card, editor, payload ou comportamento backend/runtime muda.
- [x] A visibilidade por role da aba e dos gatilhos continua protegida.
- [x] Testes focados, typecheck e build passam.

## Execution Strategy

Uma única wave sequencial executa o Grupo 1. O grupo é pequeno, mas toca composição e testes da
mesma tela; separá-lo criaria ownership sobreposto sem ganho de paralelismo. Review independente,
quality review e validação do orquestrador precedem a conclusão.

## Execution Groups

### Group 1: Hierarquia e copy da aba APIs

**Goal:** Aplicar a hierarquia visual solicitada sem alterar disponibilidade ou comportamento.

**Complexity:** 2 — composição React com regressão de role/requests; `engineer-standard`.

**Deliverables:**

1. Reordenar `API_CAPABILITIES` para WhatsApp, LLM, multimodal, STT e TTS; corrigir o título do
   fallback e tornar STT obrigatório.
2. Adicionar slot React opcional a `ApiCredentialsForm`, renderizar `Módulo Voz`/`Voice Module`
   antes de STT e o slot depois de TTS.
3. Passar a instância existente de `VoiceResponseControls` pelo slot e remover sua posição anterior.
4. Ajustar a copy PT para `Gatilhos de voz` e `Outros gatilhos em breve`.
5. Atualizar testes para ordem DOM real, qualificadores, matriz `14/7`, copy, role e requests,
   incluindo uma asserção explícita da hierarquia no spec Playwright existente.
6. Atualizar intencionalmente os dez baselines Playwright desktop/mobile diretamente afetados
   pela nova hierarquia e validar o spec novamente sem o modo de atualização de snapshots.

**Acceptance Criteria:**

- [x] A ordem e os textos PT/EN correspondem aos Success Criteria sem ordenar artificialmente o DOM.
- [x] Há exatamente uma instância dos gatilhos e ela preserva seus handlers e requests.
- [x] A declaração de `API_PROVIDER_AVAILABILITY` e os arrays de providers não mudam.
- [x] Testes existentes de cards, editores, multimodal, voz e permissão continuam verdes.
- [x] Nenhum arquivo backend é alterado.

**Validation:**

```bash
cd frontend/artifacts/neurostack
pnpm exec vitest run src/pages/clinic-config-api-settings.test.tsx src/pages/clinic-config-multimodal.test.tsx src/components/api-settings/provider-credential-card.test.tsx src/components/api-settings/voice-response-controls.test.tsx
pnpm run typecheck
PORT=3001 BASE_PATH=/ pnpm run build
PORT=3001 BASE_PATH=/ pnpm exec playwright test tests/e2e/api-provider-settings.spec.ts --update-snapshots
PORT=3001 BASE_PATH=/ pnpm exec playwright test tests/e2e/api-provider-settings.spec.ts
```

Escopo proporcional: o diff é frontend e alcança composição, copy e props; os testes focados cobrem
os comportamentos afetados, enquanto typecheck e build validam a fronteira React/Vite completa.
Durante a implementação, a atualização de snapshots deve ser intencional e revisada; o comando E2E
final, sem `--update-snapshots`, comprova os baselines desktop/mobile já consolidados.

Se qualquer gate falhar, o Grupo 1 permanece incompleto: diagnosticar e corrigir a causa e então
reexecutar o gate completo do grupo. Se a falha indicar uma regressão frontend mais ampla, escalar a
validação para a suíte Vitest completa do pacote com `pnpm run test` antes de nova revisão.

**depends-on:** none

**File scope:**

```text
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-multimodal.test.tsx
frontend/artifacts/neurostack/src/components/api-settings/voice-response-controls.tsx
frontend/artifacts/neurostack/src/components/api-settings/voice-response-controls.test.tsx
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/01-closed-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/01-closed-mobile.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/02-meta-expanded-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/02-meta-expanded-mobile.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/03-llm-expanded-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/03-llm-expanded-mobile.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/04-stt-expanded-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/04-stt-expanded-mobile.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/05-zapi-configured-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/05-zapi-configured-mobile.png
```

## QA Criteria

- [x] PT e EN exibem a mesma hierarquia definida em viewport desktop e mobile; a hierarquia é
  independente do viewport e não há reordenação responsiva.
- [x] Cards mantêm seus estados atuais; `14 active / 7 coming_soon` permanece.
- [x] Editor de provider continua abrindo/salvando e gatilhos continuam carregando/salvando.
- [x] Clinic admin continua sem acesso à aba Integrações.

## Assumptions / Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Slot duplicar gatilhos | High | Remover a instância antiga e testar cardinalidade/ordem. |
| Ordem regressar | Medium | Teste de DOM real sem `.sort()`. |
| Matriz mudar por scope creep | High | Congelamento `14/7` e review de diff. |
| Prop afetar usos isolados | Low | Slot opcional. |

## Review Results

### Plan Review — Loop 1 — 2026-10-02T17:05:42.000Z

- **Reviewer:** `/root/catalog_ui_design_review`.
- **Reviewed SHA-256:** `85f0bf4be520083cd7ce5f48b2b8e194f82004f38785355b321b1bc62b36b0f7`.
- **Verdict:** FIX-FIRST.
- **Resolved gaps:** build sem as variáveis obrigatórias do Vite; gate Playwright desktop/mobile e
  seus dez baselines ausentes do escopo; política de falha e escalonamento não especificada.

### Plan Review — Loop 2 — 2026-10-02T17:11:51.000Z

- **Reviewer:** `/root/catalog_ui_design_review`.
- **Reviewed SHA-256:** `e083c1f139db0912cf9028c1cfdde5699c8e2d10cecfe659e334e51f6883445f`.
- **Linked design SHA-256:** `2e237ae95db26c7d6b0dd390eb4d66f232f18d0b8695c83119784f15670c06c3`.
- **Verdict:** SHIP, sem gaps CRITICAL, HIGH ou MEDIUM.
- **Evidence:** build configurado, teste focado, typecheck, Playwright desktop/mobile, dez
  baselines, asserção explícita de hierarquia, fluxo de snapshots e escalonamento foram verificados
  contra os scripts, config e arquivos reais do repositório.

### Questioner Advisory — 2026-10-02T14:11:24-03:00

- **Reviewer:** `/root/catalog_ui_plan_questioner`.
- **Reviewed SHA-256:** `e083c1f139db0912cf9028c1cfdde5699c8e2d10cecfe659e334e51f6883445f`.
- **Verdict:** SHIP, sem gaps.
- **Evidence:** um único grupo continua sendo o menor plano completo; atualizar dez baselines é
  proporcional aos cinco estados full-page em dois viewports; nenhuma ativação ou dependência
  excluída retornou ao escopo; a matriz permanece congelada em `14 active / 7 coming_soon`.

### Superseded plan retirement — 2026-10-02

- O plano anterior de ativação de providers foi invalidado quando o usuário excluiu
  `whatsapp-inbound-provider-activation` e `dropout-multimodal-fallback`.
- Este WISH substitui o plano anterior por uma entrega exclusivamente visual; o histórico integral
  permanece recuperável no Git.

### Execution Review — Group 1 — 2026-10-02

- **Reviewer:** `/root/catalog_ui_design_review`.
- **Base HEAD:** `e01691e5ffaa1061c90f3da9d729204946fcf045`.
- **Owned diff SHA-256:** `57b984f83f91ea48945099516716b55b2bcfc560e3e416b0f9e9a0ae80c09798`.
- **Owned tree manifest:** `e8e3ec2f520421d7101bf852127731d3df733e7b7276f4ff98132d46a8e29403`.
- **Verdict:** SHIP, sem gaps CRITICAL, HIGH, MEDIUM ou LOW.
- **Evidence:** ordem, qualificadores, slot único, requests/roles, matriz `14/7`, arrays e ausência
  de backend foram verificados no diff; o relatório Playwright final tinha 22 passes, quatro skips
  mobile intencionais, zero unexpected e zero flaky.
- **Residual MEDIUM não bloqueante:** badges e labels de cards estreitos já se sobrepunham no
  `HEAD`; `provider-credential-card.tsx`, grid, labels e larguras ficaram byte a byte inalterados.

### Quality Review — Group 1 — 2026-10-02T14:53:22-03:00

- **Reviewer:** `/root/catalog_ui_quality_review`.
- **Reviewed 16-file aggregate SHA-256:**
  `e8e3ec2f520421d7101bf852127731d3df733e7b7276f4ff98132d46a8e29403`.
- **Verdict:** SHIP, sem gaps CRITICAL ou HIGH.
- **Evidence:** a única API nova é um `ReactNode` opcional sem estado, effects ou fetch; controles,
  payloads e segredos permaneceram protegidos; fixtures são sintéticas e nenhum segredo apareceu
  nos dez PNGs.
- **MEDIUM não bloqueante:** os baselines de 2026-08-09 estavam defasados em relação ao catálogo
  já commitado em 2026-09-30; o refresh repara esse gate, mas não isola visualmente só este diff.
- **MEDIUM preexistente:** sobreposição interna label/badge em mobile e no Google Cloud TTS desktop;
  correção responsiva e asserção de bounding boxes pertencem a um follow-up próprio.
- **LOW:** comentários legados do spec ainda citam o fluxo antigo de URL Z-API e um nome de teste
  descreve “active” onde a asserção prova expansão única de editor.

### Aggregate Validation — 2026-10-02

- **Orchestrator:** `/root` na árvore final revisada.
- **Focused Vitest:** PASS, quatro arquivos e `79/79` testes; somente avisos React `act(...)`
  preexistentes.
- **Typecheck:** PASS, `tsc -p tsconfig.json --noEmit` com exit 0.
- **Production build:** PASS, 2.804 módulos em 3m24s; avisos preexistentes de sourcemap e chunk
  maior que 500 kB.
- **Playwright update:** PASS, 22 passes e quatro skips intencionais; digest agregado dos dez PNGs
  permaneceu `b7787c31be6f2bc77487dbf2b68fa539a280a272fb1a4ff6be743cf64ca6e3cc`.
- **Playwright normal:** PASS, 22 passes e quatro skips intencionais em desktop/mobile.
- **Integrity:** `git diff --check` passou; manifests, lockfiles, backend, matriz e arrays não têm
  diff do grupo. O Corepack usou o pnpm 12.3.4 declarado pelo repositório com
  `strict-dep-builds=false`; nenhum manifest ou lockfile mudou.
- **Scope rationale:** focused tests cobrem composição, copy, requests, roles e cards; typecheck e
  build cobrem a fronteira React/Vite; o spec Playwright completo cobre os cinco estados em ambos
  os viewports. A suíte Vitest integral não foi necessária porque os reviewers atribuíram a dívida
  visual ao `HEAD` inalterado, não a uma regressão ampla deste grupo.

## Files to Create/Modify

```text
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-multimodal.test.tsx
frontend/artifacts/neurostack/src/components/api-settings/voice-response-controls.tsx
frontend/artifacts/neurostack/src/components/api-settings/voice-response-controls.test.tsx
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/01-closed-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/01-closed-mobile.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/02-meta-expanded-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/02-meta-expanded-mobile.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/03-llm-expanded-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/03-llm-expanded-mobile.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/04-stt-expanded-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/04-stt-expanded-mobile.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/05-zapi-configured-desktop.png
frontend/artifacts/neurostack/tests/e2e/api-provider-settings.spec.ts-snapshots/05-zapi-configured-mobile.png
```
