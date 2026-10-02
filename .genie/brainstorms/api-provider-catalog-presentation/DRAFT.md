# Draft: Hierarquia da aba APIs

| Campo | Valor |
|---|---|
| **Slug** | `api-provider-catalog-presentation` |
| **Data** | 2026-10-02 |
| **WRS** | 100/100 |

## Problema

A aba APIs renderiza os controles de voz antes das APIs, posiciona STT antes do fallback multimodal
e marca STT como opcional. A hierarquia visual precisa corresponder ao fluxo definido pelo produto
sem alterar disponibilidade, credenciais ou runtime de provider.

## Escopo

### IN

- Renderizar, nesta ordem: API WhatsApp, API LLM, API LLM Fallback Multimodal, Módulo Voz, API STT,
  API TTS, Gatilhos de voz e Outros gatilhos em breve.
- Mostrar WhatsApp, LLM e STT como obrigatórios; Fallback e TTS como opcionais.
- Localizar `Módulo Voz`/`Voice Module` e preservar a tradução inglesa existente dos controles.
- Manter o componente de gatilhos e seus fluxos GET/PATCH sem mudança funcional.
- Adicionar testes de ordem DOM exata e de copy/qualificadores.

### OUT

- Alterar backend, schema, PATCH, readiness, factories ou adapters.
- Ativar/desativar providers ou alterar `API_PROVIDER_AVAILABILITY` (`14 active / 7 coming_soon`).
- Adicionar Dropout ao fallback multimodal ou implementar inbound WhatsApp.
- Reordenar ou relabelar cards individuais de provider.
- Afirmar homologação externa.

## Decisões

1. `API_CAPABILITIES` continua sendo a fonte da ordem dos blocos de API.
2. `ApiCredentialsForm` recebe um slot React opcional para os gatilhos, permitindo a composição
   exata sem acoplar o componente de credenciais ao estado dos gatilhos.
3. O cabeçalho Módulo Voz é injetado imediatamente antes de STT; o slot de gatilhos aparece depois
   de TTS.
4. A matriz e os arrays de providers permanecem byte a byte inalterados.
5. O ID legado `llm/outros` mantém sua copy genérica; não será apresentado como OpenRouter.

## Riscos

| Risco | Mitigação |
|---|---|
| Mover o componente reiniciar ou duplicar requests | Uma única instância é passada pelo slot; testes verificam composição e comportamento. |
| Ordem visual divergir novamente | Teste percorre elementos reais do DOM, sem `.sort()`. |
| Mudança visual alterar disponibilidade | Teste congela a matriz atual e o diff não toca sua declaração. |
| Usuário sem permissão enxergar gatilhos | Regressão existente de visibilidade continua no gate focado. |

## Critérios de aceitação

- A ordem DOM em PT é exatamente API WhatsApp, API LLM, API LLM Fallback Multimodal, Módulo Voz,
  API STT, API TTS, Gatilhos de voz e Outros gatilhos em breve.
- WhatsApp, LLM e STT exibem `obrigatório`; Fallback e TTS exibem `opcional`.
- Em EN, `Voice Module`, `Required` e `Optional` aparecem nas posições equivalentes.
- `API_PROVIDER_AVAILABILITY`, providers, cards, editores e payloads GET/PATCH permanecem funcionais
  e sem mudança de estado.
- Os testes focados, typecheck e build passam.

## Simplicity Gate

A solução completa mais simples é reordenar a lista declarativa, inserir um cabeçalho e mover a
única instância existente dos gatilhos por um slot opcional. Não serão criados registry novo,
estado, endpoint, flag, adapter, migration ou abstração de catálogo.
