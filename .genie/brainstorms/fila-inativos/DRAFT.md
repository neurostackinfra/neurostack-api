# Brainstorm: Fila de Inativos

| Campo | Valor |
|---|---|
| **Slug** | `fila-inativos` |
| **Data** | 2026-08-30 |
| **WRS** | 100/100 |

## Problema

Sessões colocadas em handoff permanente ficam sem uma visão operacional própria, dificultando localizar pacientes cujo atendimento da IA só deve voltar por uma ação manual da clínica.

## Evidências visuais fornecidas

- O menu lateral deve incluir **Fila de Inativos** imediatamente abaixo de **Fila de Handoff** e acima de **Histórico de Traces**, com ícone de usuário inativo e estado selecionado no mesmo padrão do menu existente.
- A ação por registro deve ser apresentada como um switch **Voltar atendimento para IA**.
- O switch desligado representa uma sessão ainda inativa; o estado acionado comunica **IA no controle** em verde.
- Referências originais: `C:\Users\dell0\Downloads\clickup-attachments-2026-08-30-11-50`.

## Escopo

### IN

- Nova entrada **Fila de Inativos** no menu e nova rota/tela protegida.
- Listagem isolada por clínica das sessões ativas com `is_active = true`, `is_paused = true` e `paused_until = null` (handoff permanente).
- Ação manual por sessão ativa para devolver o atendimento à IA, persistindo `is_paused = false` e mantendo isolamento entre tenants.
- Estados visuais do switch conforme as imagens fornecidas.
- Testes de contrato do filtro e DTO da API, autorização backend por perfil, isolamento por tenant e interação principal da tela.

### OUT

- Reativação automática de handoffs permanentes.
- Alteração da Fila de Handoff existente ou das regras que criam o handoff.
- Ações em massa, atribuição para atendentes ou novos canais de notificação.
- Busca textual, filtros avançados e exportação.
- Novo histórico de pausas ou coluna `paused_at`.

## Abordagens consideradas

1. **Consultar `sessions` diretamente (escolhida):** usa `is_active`, `is_paused` e `paused_until`, que refletem uma sessão ainda atendível e o bloqueio da IA; oferece listagem paginada e reativação idempotente.
2. **Derivar a fila de `handoff_records`:** perde pausas criadas por takeover humano sem registro e pode manter um handoff concluído enquanto a sessão continua pausada.
3. **Criar tabela própria de inativos:** duplica estado, exige sincronização e adiciona recuperação sem requisito atual.

## Decisão mais simples

Reutilizar a entidade `sessions` e seu estado canônico, expondo uma consulta paginada para sessões ativas em pausa permanente e uma mutação idempotente que também exige `is_active = true`. Ambos os endpoints autorizam no backend somente clinic admin, builder e super admin e devolvem 403 para investor ou qualquer perfil não operacional. A listagem retorna apenas `session_id`, `contact_identifier` projetado de `user_id`, `channel`, `last_intent` e `funnel_stage` projetados de `cognitive_state`, e `last_activity_at`, sem expor `cognitive_state` completo; o código curto é derivado de `session_id` no cliente. A tela será uma visão operacional separada e seguirá o padrão responsivo da Fila de Handoff. Polling REST de 30 segundos é suficiente para a atualização operacional e evita WebSocket ou estado distribuído.

## Critérios já identificados

- [ ] Apenas sessões ativas da clínica autenticada em pausa permanente aparecem na Fila de Inativos.
- [ ] Sessões temporariamente pausadas (`paused_until` preenchido) não aparecem.
- [ ] Sessões encerradas não aparecem e não podem ser reativadas por esse comando.
- [ ] A API lista somente `session_id`, `contact_identifier`, `channel`, `last_intent`, `funnel_stage` e `last_activity_at`, sem expor `cognitive_state` completo.
- [ ] Acionar **Voltar atendimento para IA** reativa somente a sessão escolhida.
- [ ] Após sucesso, o item deixa de aparecer na fila e a IA pode voltar a responder às próximas mensagens.
- [ ] A entrada do menu, a rota e os dois estados do switch correspondem às referências visuais.
- [ ] Falha da API não remove o item nem deixa o switch comunicando sucesso incorretamente.
- [ ] A consulta é paginada, ordenada por atividade mais recente e se atualiza em até 30 segundos.
- [ ] Clinic admin atua apenas no próprio tenant; builder e super admin atuam apenas na clínica efetivamente selecionada; o backend de listagem e reativação retorna 403 para investor e qualquer perfil não operacional.

## Decisões fechadas

- Cada linha reutiliza a linguagem visual da Fila de Handoff e recebe somente `session_id`, `contact_identifier`, `channel`, `last_intent`, `funnel_stage` e `last_activity_at`; deriva o código curto de `session_id`, não expõe `cognitive_state` completo e não inventa nome do paciente nem horário de pausa inexistente.
- A primeira entrega inclui paginação e ordenação por `last_activity_at DESC`; busca e filtros ficam fora.
- Clinic admin, builder e super admin podem visualizar e reativar dentro do tenant autorizado; o backend rejeita investor e qualquer perfil não operacional com 403 em ambos os endpoints.
- Em sucesso, o switch apresenta brevemente o estado verde **IA no controle** antes de o item sair; em falha, permanece desligado e exibe erro.

## Riscos e premissas

- “Handoff permanente” nesta fila corresponde a uma sessão atendível pelo runtime: `sessions.is_active = true`, `is_paused = true` e `paused_until = null`; sessões encerradas ficam fora da lista e a reativação delas retorna 404.
- Reativação concorrente deve ser idempotente para evitar erro quando duas pessoas operarem o mesmo item.
- O endpoint não pode aceitar um `tenant_id` arbitrário como autoridade; o tenant deve vir do contexto autenticado.
- A reativação exige `is_active = true`, limpa `is_paused` e `paused_until`, mas não reabre sessões encerradas nem altera registros históricos de handoff.

## WRS

`WRS: ██████████ 100/100`

Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅
