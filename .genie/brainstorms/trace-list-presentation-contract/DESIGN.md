# Design: Contrato visual da lista de traces

| Field | Value |
|-------|-------|
| **Slug** | `trace-list-presentation-contract` |
| **Date** | 2026-08-30 |
| **WRS** | 100/100 |

## Problem

A lista de traces não preserva o contrato visual da referência de produto: a mensagem que identifica o trace é cortada e a paleta semântica pode divergir do painel por estar duplicada em componentes distintos. Isso prejudica a leitura da função básica de auditoria e já reincidiu porque a correção anterior preservou deliberadamente o `truncate` em vez de proteger o comportamento esperado com testes.

## Scope

### IN

- Restaurar a lista conforme `/mnt/c/Users/dell0/Downloads/image (5).png`: mensagem integral, cards sem corte, coluna de intent estável e composição responsiva.
- Criar um único catálogo frontend para a apresentação de intents, consumido pelos badges de trace e pelo painel de distribuição.
- Preservar fallback neutro para intents desconhecidas recebidas da API.
- Cobrir texto integral, layout semântico, paleta compartilhada e fallback com testes de componente/contrato.
- Documentar no código afetado a fronteira já aplicada: backend fornece dados canônicos; disponibilidade e apresentação são decisões explícitas do consumidor, com comportamento fail-closed para capacidades ainda inativas.

### OUT

- Alterar SRM, nomes de intent, schemas, endpoints ou payloads de traces.
- Criar serviço/plataforma geral de feature flags, flags remotas ou rollout por tenant.
- Remover funcionalidades backend aditivas e isoladas somente porque ainda não têm interface ativa.
- Refazer filtros, exportação, paginação, detalhe do trace ou outros cards do dashboard.
- Buscar equivalência pixel a pixel em toda largura de viewport; o contrato é conteúdo integral, hierarquia, alinhamento, responsividade e semântica visual.

## Approach

Remover a truncagem intencional da mensagem e permitir quebra natural/controlada de linha. Em desktop, manter uma coluna de intent com largura previsível e badge ocupando essa largura; em telas estreitas, preservar o empilhamento sem ocultar texto. Extrair os metadados de apresentação de intents para um módulo frontend puro — identificador, chave de tradução e tokens de cor — e fazer `IntentBadge` e dashboard consumirem essa única fonte, mantendo fallback neutro.

Foram consideradas três alternativas. Manter `truncate` com tooltip repete a solução anterior e falha no requisito explícito de mostrar o nome completo. Enviar cor/layout pelo backend acopla a API a Tailwind e não impede divergência entre consumidores. Criar agora uma plataforma geral de feature flags amplia estados e caminhos de recuperação sem uma segunda necessidade concreta. A abordagem escolhida corrige o limite responsável pela falha e deixa o backend independente da apresentação.

Quanto a funcionalidades futuras, a regra é de isolamento, não de exclusão automática: código backend pode permanecer quando for aditivo, inerte por padrão e protegido antes da mutação/execução; se código incompleto já estiver no caminho ativo ou ampliar risco operacional/segurança sem gate confiável, ele deve sair do caminho ativo até possuir critério de ativação. Esta regra não cria um novo mecanismo neste wish.

## Simplicity Case

- **Simplest complete design:** remover o corte, ajustar as classes responsivas existentes, centralizar os mapas já duplicados e testar o contrato.
- **Added machinery:** um módulo frontend puro de metadados de intent, justificado pela duplicação real entre `trace-viewer.tsx` e `dashboard.tsx` e pelo relato de divergência de cores.
- **Deferred until measured:** uma plataforma geral de feature flags só será reconsiderada quando pelo menos duas capacidades independentes exigirem ativação gradual por ambiente/tenant e gates locais apresentarem duplicação ou falha mensurável.
- **Complexity removed:** nenhum endpoint, persistência, estado de rollout, cache, dependência, payload visual ou caminho de sincronização novo.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Exibir a mensagem completa com quebra de linha e sem `truncate`/`line-clamp` | É o comportamento explícito da referência e torna a informação acessível sem hover. |
| 2 | Interpretar “mesmo tamanho” como colunas, badges e espaçamentos consistentes, não altura fixa | Altura fixa contradiz o requisito de texto integral para mensagens longas. |
| 3 | Centralizar apresentação de intent em um módulo frontend puro | Há duplicação comprovada e risco real de drift entre painel e traces. |
| 4 | API continua enviando apenas o identificador canônico | Cor, tradução e layout pertencem à camada de apresentação e não devem acoplar o backend ao CSS. |
| 5 | Intent desconhecida usa fallback neutro | Compatibilidade aditiva não deve quebrar a lista nem inventar semântica. |
| 6 | Não construir feature flags genéricas neste wish | Não há requisito atual que pague pelos estados e invariantes adicionais. |
| 7 | Backend futuro permanece somente se inerte/fail-closed; código incompleto ativo sai do caminho de execução | Evita ativação acidental sem descartar trabalho isolado e reversível. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Mensagens muito longas aumentam a altura do card | Low | Aceitar altura variável; paginação existente limita a 20 itens e o conteúdo integral é prioritário. |
| 2 | Layout móvel pode sofrer overflow após remover truncagem | Medium | Testar classes de quebra e ausência de overflow/corte nos contêineres relevantes. |
| 3 | A extração do catálogo pode alterar acidentalmente uma cor atual | Medium | Fixar em teste a paleta representativa da referência e comparar consumidores à mesma fonte. |
| 4 | Intents novas chegam antes da interface ser atualizada | Low | Manter rótulo legível e estilo neutro determinístico. |
| 5 | “Mudanças do backend” pode abranger outros recursos não identificados | Medium | Limitar este wish ao trace; aplicar a política de ativação em wishes específicos quando uma capacidade concreta for priorizada. |
| 6 | O worktree contém mudanças não relacionadas | Medium | Planejar arquivos exatos e não reverter nem incorporar alterações alheias durante execução. |

## Success Criteria

- [ ] A mensagem completa de cada trace fica visível na lista, sem `truncate`, ellipsis ou `line-clamp` no elemento ou ancestral que a oculte.
- [ ] Mensagem longa quebra em múltiplas linhas, preserva navegação do card e não causa overflow horizontal em desktop ou mobile.
- [ ] Em desktop, coluna e badge de intent mantêm largura/alinhamento consistentes; em mobile, rótulo, data, mensagem e metadados permanecem legíveis.
- [ ] `pedido_preco`, `interesse_tratamento`, `agendar`, `pos_atendimento_duvida` e `reclamacao` preservam respectivamente azul, violeta, verde, índigo e rosa conforme painel/referência.
- [ ] Dashboard e badges de trace derivam seus metadados da mesma fonte; um mapa visual paralelo volta a falhar no teste de contrato.
- [ ] Intent desconhecida permanece legível com estilo neutro e não recebe cor enviada ou inferida do backend.
- [ ] Nenhum schema, endpoint, classificador ou payload backend é alterado para realizar a correção.
- [ ] Testes focados, typecheck, build e o gate agregado do frontend passam.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `c1fb39096e1e3ef15b444d967d710b3c5b03332395717e60c6d10dafed332cec`
- **Reviewer:** /root/review_trace_list_design
- **Reviewed at:** 2026-08-30T18:16:03.000Z
<!-- genie-design-review:end -->
