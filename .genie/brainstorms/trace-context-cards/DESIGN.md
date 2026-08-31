# Design: Cards de contexto do trace

| Field | Value |
|-------|-------|
| **Slug** | `trace-context-cards` |
| **Date** | 2026-08-29 |
| **WRS** | 100/100 |

## Problem

O painel de contexto do detalhe do trace não deixa suficientemente clara a procedência e a hierarquia de cada informação usada pela IA. Além da apresentação genérica em blocos empilhados, a comparação case-sensitive atual reconhece somente `source === "rag"`, embora mocks e respostas da API usem também `RAG`, o que pode anunciar uma fonte de conhecimento como configuração da clínica.

## Scope

### IN

- Redesenhar o painel compartilhado de contexto em `frontend/artifacts/neurostack/src/components/trace-viewer.tsx` conforme a composição da imagem fornecida: cabeçalho geral e cards individuais com cabeçalho de origem, ícone, título e conteúdo.
- Atualizar o título localizado do painel para “Contexto Selecionado” / “Selected Context”.
- Normalizar as origens `RAG`/`rag` e a origem canônica `CONFIG`/`config`, aceitando `CLINIC_CONFIG`/`clinic_config` como alias de compatibilidade e usando apresentação neutra para qualquer valor desconhecido.
- Exibir o título recebido quando presente e informativo; usar o rótulo localizado derivado de `field/key` como fallback e, quando `title`, `field` e `key` estiverem ausentes ou em branco, usar o título neutro localizado “Contexto” / “Context”.
- Renderizar os cards em uma coluna em telas estreitas e duas colunas quando houver largura, mantendo ordem, altura coerente e quebra de texto longo.
- Adicionar as chaves de tradução necessárias em português e inglês.
- Criar testes de componente focados nas origens, fallbacks, conteúdo, ordem e estado vazio.

### OUT

- Alterar seleção, ranking, quantidade, conteúdo ou ordenação do contexto produzido pelo backend.
- Alterar endpoint, schema de API, banco de dados, pipeline RAG ou configuração da clínica.
- Redesenhar os demais painéis do `TraceViewer`, a página inteira de detalhe ou o simulador de chat.
- Exigir equivalência pixel a pixel com a imagem, introduzir imagens no bundle ou adicionar biblioteca visual.
- Adicionar interação de expandir, recolher, copiar, filtrar ou navegar a partir dos cards.

## Approach

Manter o limite da mudança no painel já existente do `TraceViewer`, pois ele atende tanto traces reais quanto os montados pelo simulador e pela conta de demonstração. Antes de escolher a apresentação, uma função pura normaliza `ctx.source` com `trim().toLowerCase()` e resolve um dos três estados visuais: `rag`, `config` ou `unknown`; `clinic_config` é aceito somente como alias de compatibilidade e resolve para `config`. Cada estado fornece rótulo localizado, ícone e tratamento visual; o fallback desconhecido é neutro e não atribui origem clínica indevidamente.

Cada item vira um card com duas regiões. O cabeçalho da fonte usa ícone mais texto para não depender apenas de cor. O corpo usa um ícone de documento e resolve o título pela cadeia `title` não vazio → rótulo localizado derivado de `field/key` não vazio → fallback neutro localizado “Contexto” / “Context”; `ctx.value` permanece completo, com quebra de linha/palavra em vez de truncamento. O container usa `grid-cols-1 md:grid-cols-2`, preservando a ordem original e igualando naturalmente a altura dos cards na mesma linha. A mensagem vazia existente permanece no mesmo painel.

A imagem fornecida é a referência de hierarquia e densidade: “CONTEXTO SELECIONADO”, um card RAG com nome de PDF e um card “CONFIG DA CLÍNICA” com título e descrição. Cores continuam vindo dos tokens do tema claro/escuro já usados pelo produto, evitando uma cópia rígida que degradaria o modo escuro.

Alternativas consideradas: normalizar apenas em `mapTrace` perderia os dados construídos por outros chamadores do componente; aceitar qualquer valor não-RAG como configuração manteria a classificação incorreta; extrair agora um componente genérico de card acrescentaria indireção sem segundo consumidor; equivalência pixel a pixel ignoraria tokens, responsividade e modo escuro do produto.

## Simplicity Case

- **Simplest complete design:** substituir somente o mapa visual do painel por um grid responsivo e resolver a origem com uma pequena função pura no próprio módulo.
- **Added machinery:** um terceiro estado visual `unknown` é necessário porque o contrato atual recebe strings e o comportamento vigente rotula todo valor não reconhecido como configuração; novas chaves i18n são necessárias para o título e o fallback acessível.
- **Deferred until measured:** extrair um componente reutilizável apenas quando outro painel consumir exatamente o mesmo contrato ou quando o tamanho/complexidade do arquivo ultrapassar os gates do repositório; interações como expandir/copiar somente mediante história de usuário própria.
- **Complexity removed:** nenhum estado React novo, fetch, persistência, alteração de contrato, dependência, configuração visual, truncamento com tooltip ou lógica duplicada nos dois mapeadores.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Normalizar a origem no limite de renderização do `TraceViewer` | O componente recebe traces da API, da conta demo e do simulador; corrigir só um mapper deixaria os demais sujeitos à mesma divergência de caixa. |
| 2 | Modelar `rag`, `config` e `unknown` explicitamente, mapeando `clinic_config` para `config` como compatibilidade | `config` é o valor canônico da API; o alias mantém traces legados e valores desconhecidos não podem ser anunciados como configuração da clínica. |
| 3 | Usar ícone e texto no cabeçalho da fonte | A origem continua distinguível sem depender de cor e reproduz a hierarquia da referência. |
| 4 | Preferir `title` não vazio, cair para o label de `field/key` e por fim para “Contexto” / “Context” | A referência exige nomes amigáveis como o PDF e “Diferenciais”; o fallback final mantém um título localizado e neutro quando todos os identificadores estão ausentes ou em branco. |
| 5 | Preservar o conteúdo integral com quebra de texto | Nomes de arquivo e descrições clínicas podem ser longos; truncar retiraria justamente a evidência que o painel deve auditar. |
| 6 | Grid de uma coluna com promoção para duas em `md` | Replica os dois cards lado a lado quando há espaço sem comprometer leitura móvel nem criar medição JavaScript. |
| 7 | Atualizar o título para “Contexto Selecionado” / “Selected Context” | Alinha a semântica à referência e ao fato de que o painel mostra o subconjunto escolhido pelo pipeline. |
| 8 | Manter implementação no módulo atual | Há um único consumidor visual e a mudança é pequena; extrair abstração agora adicionaria navegação sem reuso real. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | A imagem foi fornecida como referência de design, mas não especifica comportamento móvel ou modo escuro | Medium | Preservar tokens existentes e aplicar grid responsivo; validar estrutura e classes em teste e o build completo do frontend. |
| 2 | `source` pode chegar vazio ou com um novo valor | Medium | Resolver para `unknown`, com rótulo/ícone neutros e teste que impeça atribuição falsa a `clinic_config`. |
| 3 | `title`, `field`, `key` ou `value` podem estar ausentes ou vazios | Medium | Usar cadeia explícita de fallback para título e renderização segura de conteúdo; cobrir `title`/`field`/`key` simultaneamente ausentes ou em branco, além da lista vazia. |
| 4 | Texto muito longo pode aumentar desigualmente a altura dos cards | Low | Aceitar altura guiada por conteúdo, esticar os cards da mesma linha e quebrar palavras; não truncar nem fixar altura. |
| 5 | Testes baseados somente em classes podem ser frágeis | Low | Priorizar queries por rótulo, título, conteúdo e `data-testid`; limitar assert de classe ao contrato responsivo essencial. |

## Success Criteria

- [ ] O painel exibe o título localizado “Contexto Selecionado” em PT e “Selected Context” em EN.
- [ ] Um trace com origens `RAG` e `CONFIG` mostra dois cards na ordem recebida, cada um com ícone, rótulo, título e conteúdo corretos.
- [ ] As variantes de caixa e espaços externos de `rag` e do canônico `config` produzem as classificações corretas; `clinic_config` e suas variantes de caixa/espaços produzem a mesma classificação de `config` como alias de compatibilidade.
- [ ] Origem vazia ou desconhecida usa apresentação neutra e não contém o rótulo de configuração da clínica.
- [ ] `title` vazio cai para o rótulo derivado de `field/key`; quando `title`, `field` e `key` estão todos ausentes ou em branco, o título final é “Contexto” em PT e “Context” em EN; `value` e nomes longos permanecem visíveis e quebráveis, sem truncamento intencional.
- [ ] O container declara uma coluna por padrão e duas a partir do breakpoint definido, preservando ordem e cards esticados.
- [ ] Lista de contexto vazia continua exibindo `ctx_empty` localizado.
- [ ] O painel mantém contraste e semântica no tema claro/escuro por meio dos tokens existentes e diferencia as fontes por texto/ícone, não apenas por cor.
- [ ] Testes focados do `TraceViewer`, typecheck e build do frontend passam sem alteração de contratos backend.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `4a75d7c723e857bc35432e0704e6108a59c8e0bfa0ca811641b40f85386ca686`
- **Reviewer:** codex:/root/review_trace_context_design_v2
- **Reviewed at:** 2026-08-30T03:16:14.000Z
<!-- genie-design-review:end -->
