# Design: Vitrine honesta de provedores na aba APIs

| Field | Value |
|---|---|
| Slug | `api-provider-availability` |
| Date | 2026-08-30 |

## Problem

A aba APIs apresenta opções cuja disponibilidade não foi homologada diretamente. Usuários precisam ver
o roadmap sem poder inserir credenciais que o produto ainda não validou.

## Scope

### IN

- Uma matriz única Python/TypeScript para os 19 pares exibíveis, todos inicialmente `coming_soon`.
- Cards visíveis, não selecionáveis e sem inputs/payload para cada par `coming_soon`, com selo laranja
  de 10 px: `EM BREVE` em PT e `COMING SOON` em EN.
- Remoção completa de “Configuração apenas”/`isConfigOnly`/`configOnlyLabel` e do aviso especial
  `UnimplementedProviderNotice`.
- Backend fail-closed: PATCH que altere uma capacidade cujo provider resultante seja `coming_soon`
  devolve 422 `provider_not_available` sem mutação; eco idêntico de legado continua permitido.
- Inventário versionado de 22 decisões: 19 exibíveis `coming_soon` e três `removed` (Groq/OpenAI
  multimodal e Microsoft TTS), mais checker determinístico sem rede e sem censo.

### OUT

- Runtime, webhooks, adapters, readiness, políticas de egress e migração de credenciais legadas.
- Censo de produção: é pré-condição exclusiva do futuro `api-provider-runtime-shutdown`.
- Promover qualquer par a `active` sem um recibo direto reproduzível, não mockado, e validação do par.

## Approach

`PROVIDER_AVAILABILITY[capability][provider]` é a única fonte de disponibilidade de entrada. Os
allowlists continuam definindo o universo visível; a matriz decide que nenhum deles é gravável por
enquanto. Python e TypeScript têm o mesmo conjunto de 19 pares e teste de paridade.

O card sempre mostra nome/ícone e o selo laranja, mas não permite seleção, foco, inputs ou payload.
Não há recomendação de provider porque nenhum conjunto `active` existe. O backend repete o bloqueio
após `merge_credentials`, comparando o estado efetivo para não quebrar round-trip legado sem mudança.

Os três `removed` saem de allowlists/schema/types; os demais IDs legados reconhecidos permanecem
visíveis como `coming_soon`. O censo e qualquer mudança de runtime ficam isolados no wish posterior.

## Simplicity Case

- **Simplest complete design:** uma matriz literal em Python, espelho TS, uma regra PATCH e um único
  card visual.
- **Added machinery:** somente um checker de recibos/decisões, necessário para a stop rule de 22 linhas
  e para impedir segredo no Markdown. Não coleta dados nem mantém estado de runtime.
- **Deferred until measured:** censo/migração de runtime e qualquer promoção a `active` exigem novo wish
  ou revisão com homologação direta.
- **Complexity removed:** oito declarações dispersas, estado “Configuração apenas”, recomendações
  manuais e toda a máquina de censo desta entrega.

## Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Dois estados: `active` e `coming_soon` | Evita o estado ambíguo atual. |
| 2 | Todos os 19 exibíveis são `coming_soon` agora | A evidência disponível usa mocks e não autoriza `active`. |
| 3 | Selo laranja, 10 px, PT/EN | Roadmap, não erro. |
| 4 | Censo fora do wish | Não altera disponibilidade e não pode bloquear a UI. |
| 5 | Runtime não muda | Evita derrubar clínicas legadas. |
| 6 | O provider pós-merge decide o gate | Preserva ecos legados sem permitir mudança efetiva. |

## Risks & Assumptions

| Risk | Mitigation |
|---|---|
| Legado parecer desligado | Texto de roadmap; runtime intocado. |
| PATCH bloquear round-trip | Testes de eco/máscara e comparação pós-merge. |
| Matriz Python/TS divergir | Teste de paridade e gate agregado. |
| Badge não ser acessível | Tokens fixos e teste de contraste. |

## Success Criteria

- [ ] A evidência tem 22 decisões: 19 exibíveis `coming_soon` e três `removed`, sem segredo nem censo.
- [ ] Python e TS têm exatamente os mesmos 19 pares, todos `coming_soon`; removidos estão ausentes.
- [ ] Todo PATCH que cria ou altera provider `coming_soon` recebe 422 `provider_not_available` e zero
  mutação, enquanto eco legado idêntico passa.
- [ ] Cada card exibe exatamente o selo laranja de 10 px, não aceita seleção/input/payload e usa locale
  correto; não resta nenhuma variante de “Configuração apenas”.
- [ ] Runtime, adapters, readiness e egress não mudam.
- [ ] O checker de evidência é receipt-only e a ausência de qualquer arquivo de censo não altera seu exit
  code.

## Next Step

Após revisão independente SHIP, executar o wish com quatro grupos: recibos/checker, backend, frontend e
gate integrado. Um par só pode ser promovido em revisão futura com prova direta não mockada.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a`
- **Reviewer:** /root/restamp_all_coming_design
- **Reviewed at:** 2026-08-30T00:20:00.000Z
<!-- genie-design-review:end -->
