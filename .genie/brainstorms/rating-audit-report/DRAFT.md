# Draft: Relatório de Auditoria de Notas (1-5)

**Slug:** `rating-audit-report`
**Started:** 2026-08-25
**Extends:** [audit-rlhf-rating](../audit-rlhf-rating/DESIGN.md), [rating-in-trace-detail](../../wishes/rating-in-trace-detail/WISH.md)

## WRS

```
WRS: ██████████ 100/100
 Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅
```

Cristalizado em [DESIGN.md](DESIGN.md).

## Pedido original

1. Retirar do simulador o link "Corrigir / Enviar Feedback" (fluxo de sugestão + validação de metodologia).
2. Auditoria de nota 1-5 da resposta LLM tanto no simulador quanto nos traces.
   - 1 Péssimo / 2 Ruim / 3 Regular / 4 Bom / 5 Ótimo
   - Notas **1 a 3** abrem caixa de texto para o motivo.
3. Menu de auditoria do master ganha relatório, filtrável por **builder, clínica e período**:
   - total de notas recebidas
   - % entre as opções 1 a 5 (gráficos)
   - nota média
   - seção de feedbacks escritos, com opinião breve automática da IA baseada na metodologia
   - botão manual humano "aceitar e gravar opinião", guardando esses registros num lugar à parte que possa um dia ser filtrado ou exportado.

## Estado atual do código (levantado)

| Peça | Situação |
|------|----------|
| `POST /v1/rlhf/rating` | ✅ existe (`src/api/routes/rlhf.py:396`) |
| `GET /v1/rlhf/ratings` | ✅ existe, filtra `builder_id` / `tenant_id`, sem filtro de período |
| `PATCH /v1/rlhf/ratings/{id}` | ✅ existe, `approve` / `ignore`, super admin only |
| Análise IA assíncrona | ✅ existe, **dispara só para nota ≤ 2 com motivo** (`rlhf.py:410`) |
| `RatingBlock` (widget 1-5) | ✅ existe em `components/rating-block.tsx`, **textarea só para ≤ 2** (linha 38) |
| Widget no simulador | ✅ `chat.tsx:793` |
| Widget nos traces | ✅ `trace-viewer.tsx:368` (oculto para investor) |
| `FeedbackBlock` (sugestão) | ✅ existe, `chat.tsx:147`; usado em 2 lugares (demo `:416`, real `:801`) |
| Painel `rlhf-audit.tsx` | ✅ 2 abas, KPIs, gráfico de barras, filtro por Builder ID em **campo de texto livre** |
| KPIs / gráfico | ⚠️ calculados no cliente sobre `limit=100` — "total" mente acima de 100 notas |
| Nota média | ❌ não existe |
| Filtro de período | ❌ não existe (nem no backend nem na UI) |
| Filtro de clínica | ❌ não existe na UI (backend aceita `tenant_id`) |
| "Aceitar e gravar opinião" | ⚠️ o mais próximo é "Aprovar p/ Dataset" (`approved_for_training`) |
| "Exportar Dataset" | ⚠️ botão existe sem `onClick` — decorativo |

## Deltas reais (o que este brainstorm precisa decidir)

**D1 — Remover `FeedbackBlock` do simulador.** Orfana: aba "Sugestões RLHF" do painel, `POST /v1/rlhf/validate`, `POST /v1/rlhf/feedback`, `GET /admin/rlhf/pending`. Precisa decidir o destino dessa cauda.

**D2 — Faixa do motivo passa de ≤2 para ≤3.** Toca `rating-block.tsx:38` (`needsReason`) e `rlhf.py:410` (`triggers_analysis`). Consequência: nota 3 com motivo passa a disparar análise IA — mais chamadas LLM.

**D3 — Labels.** Atual "Boa resposta"/"Ótima" vs. pedido "Bom"/"Ótimo". Alinhar em `rating-block.tsx` e `rlhf-audit.tsx`.

**D4 — Agregação honesta.** Total, % por nota e média não podem ser derivados de uma página de 100 linhas. Precisa de agregação no servidor respeitando os mesmos filtros.

**D5 — Filtros de verdade.** Builder e clínica viram dropdowns alimentados por `GET /admin/builders` e `GET /admin/clinics` (não texto livre com UUID). Período é novo no backend.

**D6 — "Aceitar e gravar opinião".** Semântica a confirmar: é o mesmo ato de curadoria que hoje se chama "Aprovar p/ Dataset", ou um registro distinto?

## Perguntas resolvidas

- [x] Q1: destino do fluxo de sugestão RLHF → **retirar tudo** (widget + aba + `POST /validate` + `POST /feedback` + `GET /admin/rlhf/pending`)
- [x] Q2: "aceitar e gravar opinião" → **mesmo ato**, consolida com "Aprovar p/ Dataset"; reaproveita `approved_for_training` somando `accepted_by` / `accepted_at`
- [x] Q3: exportação → **fora de escopo**; o pedido diz "um dia". A flag + o filtro já deixam o conjunto identificável para quando houver destino concreto

## Notas

- Memória relevante: quando a política já fixa os valores válidos, derivar campo em vez de texto livre + validação. Aplica-se direto ao filtro de Builder (hoje input de UUID).
- Simplicity Gate: "para ser um dia filtrado ou exportado" é gatilho futuro explícito — construir exportação agora seria machinery especulativa.
