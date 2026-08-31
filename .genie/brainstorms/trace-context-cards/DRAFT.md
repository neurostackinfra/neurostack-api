# Brainstorm Draft — Cards de contexto do trace

**Slug:** trace-context-cards
**Date:** 2026-08-29
**WRS:** 100/100

## Problem

O painel “Contexto Utilizado” do detalhe do trace não comunica com clareza qual conteúdo veio do RAG e qual veio da configuração da clínica, além de classificar incorretamente origens `RAG` em maiúsculas porque a renderização compara apenas com `"rag"`. A referência visual enviada pede cards distintos por fonte, com título e conteúdo legíveis lado a lado quando houver espaço.

## Scope

### IN

- Redesenhar somente o painel de contexto de `TraceViewer` conforme a referência visual aberta em `/mnt/c/Users/dell0/Downloads/image (4).png`.
- Exibir cada item como card com cabeçalho de origem, ícone, título e conteúdo.
- Normalizar `RAG`/`rag` e a origem canônica `CONFIG`/`config`, aceitando `CLINIC_CONFIG`/`clinic_config` como alias de compatibilidade antes de decidir rótulo e apresentação.
- Usar duas colunas em largura suficiente e uma coluna em viewport estreita.
- Manter o estado vazio existente e internacionalizar os novos rótulos em português e inglês.
- Adicionar testes de componente para origem, conteúdo, layout semântico e estado vazio.

### OUT

- Alterar o algoritmo que seleciona contexto, o conteúdo retornado, o limite de itens ou a ordem recebida.
- Alterar endpoints, schemas da API, banco, pipeline RAG ou configuração da clínica.
- Redesenhar outros painéis do detalhe do trace ou o simulador de chat.
- Copiar o arquivo de referência visual para o bundle da aplicação.

## Decisions

- Implementar o novo visual dentro de `trace-viewer.tsx`; o painel já é compartilhado pelo detalhe real e pelos traces de demonstração.
- Tratar a origem como um pequeno domínio explícito (`rag`, `clinic_config`, desconhecida) após `trim().toLowerCase()`; origem desconhecida não deve ser anunciada como configuração da clínica.
- Reusar os campos normalizados existentes: título usa `title` quando informativo, cai para o rótulo traduzido de `field/key` e termina no fallback neutro “Contexto” / “Context” quando todos estão ausentes ou em branco; corpo usa `value`.
- Cards RAG e de configuração terão ícones e rótulos distintos, sem depender apenas de cor.
- Usar CSS responsivo existente (Tailwind) e Lucide já instalado; não criar componente genérico, dependência ou configuração nova.

## Simplicity Case

O desenho mínimo completo substitui somente o mapa visual atual por um grid responsivo e uma função pura de normalização de origem. Não há mudança de dados, novo estado, fetch, dependência, configuração ou persistência. Um componente separado só será justificado se outro painel precisar reutilizar exatamente o mesmo contrato ou se o arquivo ficar difícil de manter após a mudança.

## Risks and Assumptions

- A referência visual é tratada como direção de composição, hierarquia e densidade, não como exigência de equivalência pixel a pixel; o painel deve continuar aderente aos tokens de tema claro/escuro existentes.
- `DecisionTrace` pode trazer `config` canônico, `clinic_config` legado, caixa/espaços diferentes e, em integrações futuras, valores desconhecidos; o alias e o fallback neutro precisam ser testados.
- Alguns itens podem não ter `title`, `field` nem `key`; a renderização precisa preservar o rótulo traduzido derivado do campo quando houver e usar “Contexto” / “Context” como fallback final.
- Nomes de documento e conteúdos podem ser longos; cards devem quebrar texto sem truncar informação.

## Acceptance Criteria (draft)

- Um trace com um item `origin/source = "RAG"` e outro `"CONFIG"` mostra dois cards com rótulos e ícones corretos, títulos e conteúdos correspondentes.
- As variantes de caixa/espaços de `rag` e `config` produzem a classificação correta; `clinic_config` e suas variantes resolvem para configuração como alias.
- Uma origem desconhecida recebe rótulo neutro e nunca aparece como “Config da clínica”.
- O painel usa uma coluna em viewport estreita e duas em largura suficiente, preservando a ordem dos itens.
- Título ausente cai para o campo traduzido; se `title`, `field` e `key` estiverem ausentes ou em branco, cai para “Contexto” / “Context”; texto longo quebra no card sem ocultação intencional.
- Lista vazia continua mostrando a mensagem localizada de contexto vazio.
- Testes focados do componente, typecheck e build do frontend passam.

## Evidence from the supplied design

- Imagem aberta: 746 × 302 px.
- Cabeçalho geral: “CONTEXTO SELECIONADO”.
- Card RAG: ícone de base de dados, cabeçalho “RAG”, ícone de documento e nome do PDF.
- Card de configuração: ícone de clínica, cabeçalho “CONFIG DA CLÍNICA”, título “Diferenciais - Facetas de Porcelana” e texto descritivo.
- Composição: dois cards de mesma altura em grid, bordas discretas e hierarquia tipográfica explícita.
