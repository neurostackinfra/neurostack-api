# Design: Fila de Inativos

| Field | Value |
|-------|-------|
| **Slug** | `fila-inativos` |
| **Date** | 2026-08-30 |
| **WRS** | 100/100 |

## Problem

Sessões colocadas em handoff permanente não têm uma visão operacional própria, então a clínica não consegue localizar com segurança quem está sem atendimento da IA nem devolver cada conversa ao controle da IA manualmente.

## Scope

### IN

- Nova entrada **Fila de Inativos**, entre **Fila de Handoff** e **Histórico de Traces**, disponível para clinic admin, builder e super admin.
- Nova rota protegida e responsiva que lista apenas sessões ativas do tenant efetivo com `is_active = true`, `is_paused = true` e `paused_until IS NULL`.
- Linhas no padrão visual da Fila de Handoff com identificador do contato, código curto da sessão, canal, intenção atual, etapa do funil, última atividade e o switch fornecido nas referências.
- Consulta paginada, ordenada por `last_activity_at DESC`, com atualização REST a cada 30 segundos e estados de carregamento, vazio e erro.
- Reativação manual de sessão ativa, tenant-safe e idempotente, limpando `is_paused` e `paused_until`; após sucesso o item mostra brevemente **IA no controle** em verde e sai da fila.
- Traduções PT/EN e testes focados de API/DTO, autorização de perfil, isolamento de tenant, navegação e interação do switch.

### OUT

- Reativação automática de handoffs permanentes ou mudança das regras que criam pausas.
- Alteração do ciclo de vida ou do histórico em `handoff_records`.
- Nova tabela, coluna `paused_at` ou histórico de ativações.
- Busca, filtros avançados, exportação, ações em massa, atribuição de atendente ou notificações.
- WebSocket/SSE, aplicativo nativo ou visão operacional para investor.

## Approach

Usar `sessions` como fonte canônica: um endpoint paginado consulta o predicado exato `is_active = true AND is_paused = true AND paused_until IS NULL` e um endpoint de comando reativa somente uma sessão ativa dentro do tenant resolvido pelo contexto autenticado. Ambos os endpoints exigem no backend um perfil operacional (clinic admin, builder ou super admin) e retornam 403 para investor ou qualquer outro perfil. A lista usa um DTO mínimo com `session_id`, `contact_identifier` projetado de `user_id`, `channel`, `last_intent` e `funnel_stage` projetados de `cognitive_state`, e `last_activity_at`; o código curto é derivado de `session_id` no cliente e o estado cognitivo completo não é exposto.

A tela segue a composição responsiva da Fila de Handoff, mas troca as ações de checkout pelo switch **Voltar atendimento para IA**. O estado ligado só aparece após resposta 2xx, com o texto verde **IA no controle**; então a linha é removida. Uma falha mantém a linha inativa e mostra feedback de erro. O polling REST de 30 segundos acompanha o padrão da fila existente.

Foram rejeitadas duas alternativas: derivar de `handoff_records`, que omite takeover humano e confunde histórico com estado atual, e criar tabela própria, que duplicaria o estado de pausa e exigiria sincronização/recovery sem benefício presente.

## Simplicity Case

- **Simplest complete design:** dois endpoints sobre `sessions`, uma página, uma rota/menu e o switch por registro.
- **Added machinery:** paginação limita a consulta; polling de 30 segundos atende a natureza operacional da fila; um estado transitório pós-sucesso materializa a referência visual sem criar estado durável.
- **Deferred until measured:** busca/filtros entram quando uma clínica demonstrar dificuldade para operar páginas de 20 itens; push em tempo real só será reconsiderado se 30 segundos de atualização violarem um SLA operacional acordado.
- **Complexity removed:** nenhuma tabela, migração, cache, fila assíncrona, sincronização com `handoff_records` ou configuração adicional.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Inativo permanente na fila = `is_active=true`, `is_paused=true` e `paused_until IS NULL` | Corresponde às sessões que o lookup ativo do webhook pode voltar a entregar à IA; sessões encerradas ficam fora. |
| 2 | Consultar `sessions`, não `handoff_records` | A sessão representa o controle atual da IA; handoff record é histórico e nem todo takeover cria um registro. |
| 3 | `GET /v1/sessions/inactive` paginado e `POST /v1/sessions/{id}/reactivate` idempotente | Separa leitura de comando, limita cardinalidade e tolera clique/requisição concorrente. |
| 4 | `tenant_id` opcional apenas para builder/super admin | Clinic admin usa o tenant do token; override sempre passa pelo validador de acesso existente. |
| 5 | Cross-tenant, sessão inexistente ou encerrada retornam 404; pausa temporária retorna 409 | Evita enumeração e impede que a ação específica altere uma sessão fora do escopo ativo e permanentemente pausado. |
| 6 | Reativação exige `is_active=true` e define `is_paused=false` e `paused_until=null`, sem tocar handoffs | Restaura a IA somente para uma sessão que o runtime ainda considera ativa, sem reabrir sessão nem reescrever histórico independente. |
| 7 | DTO da linha contém somente `session_id`, `contact_identifier`, `channel`, `last_intent`, `funnel_stage` e `last_activity_at` | Projeta apenas os campos renderizados, deriva o código curto no cliente e não expõe `cognitive_state` completo nem inventa `paused_at`. |
| 8 | Backend de ambas as rotas aceita somente clinic admin, builder e super admin; investor e qualquer perfil não operacional recebem 403 | Ocultar menu não é autorização; leitura e mutação operacionais precisam do mesmo bloqueio no servidor. |
| 9 | Acessibilidade do switch inclui nome, estado, bloqueio durante mutação e foco visível | O visual fornecido precisa continuar operável por teclado e leitor de tela. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Endpoint reutilizar a validação permissiva da listagem genérica de sessões | High | Exigir perfil operacional no backend antes de ambas as rotas, resolver tenant pelo token/override autorizado e testar clinic admin, builder, super admin, investor, outro perfil não operacional e cross-tenant. |
| 2 | Duas pessoas reativarem a mesma sessão | Medium | Comando idempotente; sessão `is_active=true` já sem pausa no mesmo tenant retorna 200 sem nova mutação. |
| 3 | Poll e reativação disputarem a atualização da tela | Medium | Remoção por `session_id`, cancelamento/ignore de resposta obsoleta e switch desabilitado durante a mutação. |
| 4 | `user_id` não ser telefone em canais não WhatsApp | Low | Rotular como identificador do contato e mostrar o canal; não construir link `wa.me`. |
| 5 | Sessão pausada ter sido encerrada | Low | Aplicar `is_active=true` na listagem e na reativação; uma sessão encerrada não aparece, retorna 404 no comando e nunca é apresentada como restaurável sem reabertura. |

## Success Criteria

- [ ] O menu exibe **Fila de Inativos** exatamente entre **Fila de Handoff** e **Histórico de Traces**, com ícone/estado selecionado equivalentes à referência.
- [ ] A rota protegida não é oferecida ao investor e funciona para clinic admin, builder e super admin com a clínica efetiva correta; no backend, ambos os endpoints retornam 403 para investor e qualquer perfil não operacional.
- [ ] `GET /v1/sessions/inactive` retorna somente `is_active=true AND is_paused=true AND paused_until IS NULL`, ordena por `last_activity_at DESC` e pagina com total/continuação coerentes.
- [ ] Clinic admin não consegue listar outro tenant; builder sem vínculo não consegue; super admin só recebe a clínica selecionada no override explícito.
- [ ] Pausas temporárias e sessões encerradas não aparecem, inclusive após atualização de polling.
- [ ] Cada linha recebe somente `session_id`, `contact_identifier`, `channel`, `last_intent`, `funnel_stage` e `last_activity_at`, sem o `cognitive_state` completo, e apresenta **Voltar atendimento para IA** no estado desligado.
- [ ] `POST /v1/sessions/{id}/reactivate` exige `is_active=true`, limpa os dois campos de pausa no tenant autorizado, é idempotente quando a sessão ativa já está sem pausa e retorna 409 para pausa temporária.
- [ ] Reativação cross-tenant, de ID inexistente ou de sessão encerrada retorna 404 e não altera nenhuma sessão.
- [ ] Após 2xx, o switch mostra **IA no controle** em verde e o item é removido; após falha, fica desligado, permanece na lista e exibe erro.
- [ ] A tela cobre loading, vazio, erro, paginação, troca de clínica e layout móvel/desktop sem overflow.
- [ ] Todos os novos textos têm chaves PT/EN; testes frontend cobrem menu, perfil, renderização e sucesso/falha do switch.
- [ ] Testes backend cobrem o filtro triplo com `is_active`, DTO mínimo sem `cognitive_state`, ordenação, paginação, idempotência, pausa temporária, sessão encerrada, isolamento por tenant e 403 para investor/outros perfis não operacionais em listagem e reativação; gates Python e frontend afetados passam.

## Next Step

Após revisão independente SHIP, persistir a evidência digest-bound e converter este design no wish executável `fila-inativos`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `5af261f845acb68c93d1b45e23d5fe607d8c63a565f927fe4a51c1a65b3f0035`
- **Reviewer:** /root/rereview_fila_inativos_design
- **Reviewed at:** 2026-08-30T15:05:49.000Z
<!-- genie-design-review:end -->
