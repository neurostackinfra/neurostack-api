# Brainstorm Draft — Contrato visual da lista de traces

**Slug:** trace-list-presentation-contract
**Date:** 2026-08-30
**WRS:** 100/100

## Problem

A lista de traces voltou a divergir da referência de produto: a mensagem que identifica o trace é cortada, a largura/altura dos elementos não preserva a composição esperada e a paleta de intents corre risco de divergir do painel porque a mesma semântica de cor está duplicada em componentes distintos. O histórico confirma que o corte é produzido pela classe frontend `truncate`; a correção anterior adicionou apenas um tooltip e registrou explicitamente a manutenção do corte, portanto esta regressão não é causada pelo payload do backend.

## Scope

### IN

- Restaurar a composição da lista de traces conforme `/mnt/c/Users/dell0/Downloads/image (5).png`: cards uniformes, coluna de intent estável e mensagem integral sem ellipsis intencional.
- Definir um único catálogo frontend de apresentação de intents (classe/cor e chave de tradução) consumido tanto pela lista/detalhe de traces quanto pelo painel que apresenta a distribuição de intents.
- Preservar um fallback neutro para intents novas ou desconhecidas vindas da API, sem permitir que o backend escolha classes ou cores visuais.
- Adicionar testes de componente/contrato para texto integral, geometria semântica das linhas, cores canônicas compartilhadas e fallback desconhecido.
- Registrar a fronteira de ativação aplicável: capacidade backend aditiva pode permanecer no código quando estiver inerte e coberta por contrato; exposição na interface ou entrada em fluxo de execução requer disponibilidade explícita e fail-closed.

### OUT

- Alterar classificação SRM, nomes canônicos de intent, payloads ou endpoints de traces.
- Criar uma plataforma genérica de feature flags, serviço remoto de flags ou estados de rollout por tenant.
- Remover do backend funcionalidades já isoladas apenas porque ainda não aparecem na interface.
- Refazer filtros, exportação, paginação, detalhe do trace ou outros cards do dashboard.
- Garantir equivalência pixel a pixel em todas as larguras; a referência fixa o contrato de conteúdo, hierarquia, alinhamento e semântica visual.

## Decisions

- Substituir o `truncate` da mensagem por quebra controlada de linha e permitir que a linha cresça o necessário; cards não terão altura fixa que possa cortar conteúdo.
- Manter a coluna visual do intent com largura estável em desktop e adaptação empilhada em telas estreitas. O badge deve ocupar a largura disponível dessa coluna para que rótulos curtos e longos tenham a mesma geometria externa.
- Extrair a apresentação de intent para um módulo frontend puro e reutilizável; dashboard e `IntentBadge` deixam de manter mapas paralelos.
- Manter cores como decisão de apresentação frontend. O backend fornece apenas o identificador canônico de intent e não ganha campos de cor, layout ou CSS.
- Para capacidades futuras, manter código backend somente quando ele for aditivo, inerte por padrão e protegido no limite de mutação/execução. Se código incompleto já estiver no caminho ativo ou ampliar risco operacional/segurança sem gate confiável, removê-lo do caminho ativo até haver critério de ativação.
- Não criar um framework geral de flags neste wish: a regressão atual é resolvida por contrato visual centralizado e testes; a política de ativação orienta trabalhos futuros e usa gates locais explícitos quando uma capacidade real os exigir.

## Simplicity Case

O desenho mínimo completo remove o corte, ajusta o layout responsivo existente, centraliza um mapa já duplicado e testa o contrato. Não adiciona endpoint, persistência, flag remota, tabela, cache, estado de rollout ou dependência. Um mecanismo geral de feature flags só será reconsiderado quando houver pelo menos duas capacidades independentes que precisem de ativação gradual por ambiente/tenant e os gates locais existentes demonstrarem duplicação ou falha mensurável.

## Risks and Assumptions

- Mensagens arbitrariamente longas aumentam a altura individual do card; isso é aceito porque o requisito prioritário é não ocultar o nome/conteúdo do trace. A paginação existente limita a lista a 20 itens.
- “Mesmo tamanho” é interpretado como geometria consistente das colunas, badges e espaçamentos, não altura fixa de todos os cards quando o conteúdo exigir mais linhas.
- A referência mostra desktop; o comportamento móvel continua empilhado e deve preservar todo o conteúdo sem overflow horizontal.
- O catálogo compartilhado precisa preservar a paleta atual do painel. Um snapshot amplo de classes Tailwind seria frágil; os testes devem afirmar tokens canônicos representativos e que ambos os consumidores usam a mesma fonte.
- Mudanças locais não relacionadas já existem no worktree; o plano e a futura execução não devem revertê-las nem absorvê-las.

## Acceptance Criteria (draft)

- A mensagem completa de cada trace está presente visualmente na lista, sem `truncate`, `text-overflow: ellipsis` ou `line-clamp` no elemento ou ancestral que a oculte.
- Uma mensagem longa quebra em múltiplas linhas, mantém o card navegável e não produz overflow horizontal em desktop ou mobile.
- Em desktop, a coluna de intent e seu badge mantêm largura/alinhamento consistentes entre linhas; em mobile, a composição empilha sem cortar rótulo, data ou mensagem.
- `pedido_preco`, `interesse_tratamento`, `agendar`, `pos_atendimento_duvida` e `reclamacao` usam respectivamente a paleta azul, violeta, verde, índigo e rosa exibida no painel/referência.
- Dashboard e badges de trace derivam cor e rótulo do mesmo catálogo; uma alteração unilateral volta a falhar em teste.
- Intent desconhecida continua legível com estilo neutro e nunca recebe semântica visual inventada a partir do backend.
- Nenhum schema, endpoint, classificador ou payload de backend é alterado para realizar a correção visual.
- Testes focados, typecheck, build e o gate agregado do frontend passam.

## Evidence

- Referência aberta: `/mnt/c/Users/dell0/Downloads/image (5).png`, 1408 × 843 px.
- A referência mostra cards com coluna de intent de largura constante, badges preenchendo essa coluna e mensagens integrais.
- `frontend/artifacts/neurostack/src/pages/traces.tsx` aplica atualmente `truncate` à mensagem.
- O commit `ea2c657` adicionou `title={trace.userMessage}` e manteve explicitamente o corte; o wish anterior `traces-title-tooltip` marcou remover `truncate` como OUT.
- `frontend/artifacts/neurostack/src/components/trace-viewer.tsx` e `frontend/artifacts/neurostack/src/pages/dashboard.tsx` mantêm mapas independentes com a mesma paleta de intents.
