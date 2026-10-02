# Design: Hierarquia da aba APIs

| Campo | Valor |
|---|---|
| **Slug** | `api-provider-catalog-presentation` |
| **Data** | 2026-10-02 |
| **WRS** | 100/100 |

## Problem

A aba APIs não segue a hierarquia visual definida pelo produto: os gatilhos de voz aparecem antes
das APIs, STT antecede o fallback multimodal e STT é apresentado como opcional.

## Scope

### IN

- Aplicar a sequência visual exata definida neste design.
- Corrigir os qualificadores obrigatório/opcional e o título do fallback.
- Inserir o divisor localizado Módulo Voz imediatamente antes de STT.
- Posicionar o componente existente de gatilhos depois de TTS e ajustar a copy portuguesa exata.
- Cobrir ordem, copy, qualificação, permissão e ausência de drift da matriz com testes.

### OUT

- Qualquer mudança backend, API, schema, persistência, runtime, egress ou credencial.
- Ativação, desativação, adição ou remoção de provider.
- WhatsApp inbound, Dropout multimodal, probe live ou homologação externa.
- Alteração da ordem/copy dos cards internos de provider.
- Catálogo dinâmico, endpoint de catálogo, feature flag ou terceiro estado público.

## Target hierarchy

| Ordem | Elemento PT | Qualificador | Elemento EN |
|---:|---|---|---|
| 1 | API WhatsApp | obrigatório | API WhatsApp / Required |
| 2 | API LLM | obrigatório | API LLM / Required |
| 3 | API LLM Fallback Multimodal | opcional | API LLM Fallback Multimodal / Optional |
| 4 | Módulo Voz | cabeçalho | Voice Module |
| 5 | API STT | obrigatório | API STT / Required |
| 6 | API TTS | opcional | API TTS / Optional |
| 7 | Gatilhos de voz | controle existente | Voice Triggers |
| 8 | Outros gatilhos em breve | texto terminal | More voice triggers coming soon |

## Current state

- `VoiceResponseControls` é renderizado antes de `ApiCredentialsForm`.
- `API_CAPABILITIES` está em WhatsApp → LLM → STT → multimodal → TTS.
- STT usa `required: false` e multimodal usa `FallBack LLM Multimodal`.
- A matriz pública atual possui `14 active / 7 coming_soon` e não faz parte desta mudança.

## Approach

1. Reordenar `API_CAPABILITIES` para `whatsapp`, `llm`, `multimodal`, `stt`, `tts`.
2. Definir o título multimodal como `API LLM Fallback Multimodal` e STT como obrigatório.
3. Adicionar a `ApiCredentialsForm` um slot React opcional `voiceControls`.
4. Renderizar `Módulo Voz`/`Voice Module` antes do card de capacidade STT e o slot depois de TTS.
5. No uso real da página, passar a única instância existente de `VoiceResponseControls` pelo slot.
6. Ajustar apenas a copy PT do componente para `Gatilhos de voz` e
   `Outros gatilhos em breve`; preservar seu comportamento e a copy EN existente.

O slot é opcional para manter os testes unitários e usos isolados de `ApiCredentialsForm` simples.
Ele não cria estado nem conhece o contrato interno dos gatilhos.

## Alternatives rejected

- **Apenas mover os gatilhos para depois do formulário:** não posiciona Módulo Voz antes de STT.
- **Duplicar STT/TTS fora do formulário:** duplica estado, fetch e lógica de credenciais.
- **Acoplar `VoiceResponseControls` dentro do formulário:** exige propagar role e regras de acesso;
  o slot mantém responsabilidades separadas.
- **Reabrir o antigo rollout de providers:** excede o pedido e reintroduz planos explicitamente
  retirados pelo usuário.

## Decisions

| # | Decisão | Racional |
|---:|---|---|
| 1 | Mudança exclusivamente de apresentação | O pedido define hierarquia, não disponibilidade. |
| 2 | Matriz congelada em `14/7` | Evita ativar ou desativar integrações por efeito colateral. |
| 3 | Slot React opcional | Permite composição exata com baixo acoplamento. |
| 4 | `llm/outros` permanece genérico | Relabeling como OpenRouter seria desonesto sem ID/contrato específico. |
| 5 | Uma única wave frontend | Os arquivos e testes formam uma unidade pequena e coesa. |

## Simplicity Case

- **Solução completa mais simples:** reordenar a declaração, inserir um cabeçalho e mover um
  componente existente por slot.
- **Maquinaria adicional:** apenas uma prop React opcional, paga pela necessidade de intercalar o
  cabeçalho antes de STT e manter os gatilhos depois de TTS.
- **Sem novos estados:** disponibilidade, readiness, editor, requests e persistência não mudam.
- **Diferido:** qualquer alteração de provider exige um wish futuro com contrato próprio.

## Risks & mitigations

| Risco | Severidade | Mitigação |
|---|---|---|
| Ordem testada apenas por constantes | Medium | Teste usa a ordem real dos elementos no DOM. |
| Componente de voz duplicado | High | Parent remove a instância antiga e passa exatamente uma pelo slot. |
| Drift de disponibilidade | High | Matriz não é editada e teste congela `14/7`. |
| Regressão de permissão | Medium | Manter teste que oculta integrações/gatilhos de clinic admins. |
| Mudança funcional do PATCH de voz | High | Testes existentes do componente continuam no gate sem alterar handlers. |

## Success Criteria

- [ ] A ordem DOM PT corresponde exatamente à tabela Target hierarchy, sem `.sort()`.
- [ ] WhatsApp, LLM e STT exibem `obrigatório`; Fallback e TTS exibem `opcional`.
- [ ] EN exibe `Voice Module` e os qualificadores `Required`/`Optional` equivalentes.
- [ ] Existe exatamente uma instância de `VoiceResponseControls`, depois de TTS.
- [ ] `Gatilhos de voz` termina com `Outros gatilhos em breve` em PT.
- [ ] `API_PROVIDER_AVAILABILITY` continua `14 active / 7 coming_soon` e nenhum provider/card muda.
- [ ] Editores, GET/PATCH de credenciais e GET/PATCH dos gatilhos preservam o comportamento atual.
- [ ] Testes focados, typecheck e build passam.

## Next Step

Depois de review independente `SHIP` e stamp verificado, atualizar e revisar o WISH antes de qualquer
dispatch de implementação.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `2e237ae95db26c7d6b0dd390eb4d66f232f18d0b8695c83119784f15670c06c3`
- **Reviewer:** /root/catalog_ui_design_review
- **Reviewed at:** 2026-10-02T17:00:36.000Z
<!-- genie-design-review:end -->
