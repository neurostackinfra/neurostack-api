# Brainstorm: Métrica de usuários novos

| Campo | Valor |
|---|---|
| Slug | `new-users-metric` |
| Data | 2026-08-08 |
| Estado | DESIGN SHIP; WISH APPROVED após plan review |
| WRS | 100/100 |

## Ideia original

Mostrar quantos usuários são novos no período e a composição percentual de interações novas versus antigas, com gráfico, para clinic_admin e builder.

## Decisões confirmadas originalmente

- “Novo” é por clínica/tenant.
- A identidade usa telefone ou username persistido em `Session.user_id`, distinguindo canais.
- O KPI conta identidades únicas; os percentuais contam interações.
- Não identificados ficam fora do denominador e aparecem em contagem própria.
- Clínica recebe gráfico diário; builder recebe resumo de sete dias por clínica.

## Council 2026-08-08 — decisão `revise`

As cinco lentes reconheceram o valor comercial, mas condicionaram execução a compatibilidade, disponibilidade, privacidade, fonte temporal e isolamento do worktree. A revisão adota todas as condições:

1. Wave 0A mede cardinalidade, retenção, cobertura, plano SQL, serviço e memória antes do backend; Wave 0B mede endpoint/capacidade antes do frontend.
2. “Registrada” passa a significar explicitamente `Event.created_at` persistido após o processamento/LLM; recebimento fica OUT.
3. Endpoints atuais não mudam. Analytics usa o router administrativo próprio `/v1/acquisition/clinic` e `/v1/acquisition/builder`, sem colisão com `/v1/traces/{trace_id}`.
4. Novos endpoints aceitam apenas JWT de clinic_admin/builder; API keys, super_admin e outras roles não acessam a métrica.
5. Clinic limita período a 90 dias; builder usa janela de 168 horas e keyset de até 100 clínicas, sem prometer `total` ou snapshot histórico de ownership.
6. Timeout SQL, deadline total, controlador compartilhado máximo de 4 total/2 builder, orçamento de memória e 503 tipado protegem o banco de consultas caras.
7. Small cells exatas são risco aceito apenas dentro do ownership JWT; nenhuma identidade bruta sai.
8. Profiling de staging é estritamente read-only; benchmark destrutivo/EXPLAIN ANALYZE roda apenas em banco `_test`. Evidência usa allowlist que também remove DSN, host e credenciais.
9. Microcopy explica histórico disponível, persistência, canal/tenant e percentuais por interação.
10. Backend precede frontend; UI é role-gated, usa keyset/cursor sequencial com `as_of`, descarta montagens parciais, tolera 403/404/503 e possui matriz de screenshots reproduzível.
11. Execução ocorre em worktree limpo/isolado com base imutável, nunca sobre o tree de `api-provider-settings`.
12. Timeout/cancelamento exige rollback, cancel confirmado, permits em `finally` e conexão limpa no pool.
13. Small cells exatas exigem aceite assinado por owner organizacional de produto/privacidade antes do backend.
14. Requests, responses, invariantes, arredondamento, erros e OpenAPI são normativos; 404 de rollout nunca é tratado como vazio.

## Simplicity Gate

A forma mais simples que cobre as condições é manter o cálculo derivado, separar analytics em dois endpoints JWT-only limitados e colocar toda prova de escala numa Wave 0. Não há migration, cache, materialização, tabela, job, flag ou timestamp de recebimento. Endpoints separados, paginação, timeout e concorrência são pagos por riscos concretos encontrados pelo council, não por escala hipotética.

## Alternativas rejeitadas

- Alterar `/v1/traces/stats`: quebra semântica temporal existente e amplia analytics para API key.
- Alterar `/v1/builder/clinics`: acopla listagem operacional ao custo/falha da métrica.
- Adicionar `received_at`: muda o pedido de “registrada” e atravessa todos os webhooks.
- Suprimir small cells: torna o KPI inexato para administradores autorizados.
- Criar índice/materialização agora: Wave 0 ainda não forneceu evidência.

## WRS

`██████████ 100/100`

Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅

## Resultado

DESIGN aprovado no digest `baf59da009d3f9b56aed2a016ab479d02b4c4f1cffd396cb69df87d1dbddc808`. O WISH substituto foi aprovado com Wave 0A como primeiro gate fail-closed e Wave 0B antes do frontend.
