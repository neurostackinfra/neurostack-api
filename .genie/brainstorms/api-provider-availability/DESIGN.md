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
- Cards visíveis, não selecionáveis e sem inputs/payload para cada par que o tenant **não** possui
  persistido, com selo laranja de 10 px: `EM BREVE` em PT e `COMING SOON` em EN.
- Exceção de manutenção (emenda 2026-08-31): o par que o tenant já tem persistido continua editável e
  exibe `CONFIGURADO`/`CONFIGURED` quando o bundle está completo, ou `INCOMPLETO`/`INCOMPLETE` quando
  está parcial. Nenhum par é promovido a `active`.
- Remoção completa de “Configuração apenas”/`isConfigOnly`/`configOnlyLabel` e do aviso especial
  `UnimplementedProviderNotice`.
- Backend fail-closed na **adoção**: PATCH que adote um provider que o tenant não possui — troca,
  bootstrap do zero ou limpeza do provider persistido — devolve 422 `provider_not_available` sem
  mutação; eco idêntico de legado e manutenção do provider já persistido continuam permitidos.
- Inventário versionado de 22 decisões: 19 exibíveis `coming_soon` e três `removed` (Groq/OpenAI
  multimodal e Microsoft TTS), mais checker determinístico sem rede e sem censo.

### OUT

- Runtime, webhooks, adapters, readiness, políticas de egress e migração de credenciais legadas.
- Censo de produção: é pré-condição exclusiva do futuro `api-provider-runtime-shutdown`.
- Promover qualquer par a `active` sem um recibo direto reproduzível, não mockado, e validação do par.

## Approach

`PROVIDER_AVAILABILITY[capability][provider]` é a única fonte de disponibilidade de entrada. Os
allowlists continuam definindo o universo visível; a matriz decide que nenhum deles pode ser
**adotado** por enquanto (ver a emenda abaixo, que preserva a manutenção do que já está persistido).
Python e TypeScript têm o mesmo conjunto de 19 pares e teste de paridade.

O card de um par que o tenant não possui mostra nome/ícone e o selo laranja, mas não permite seleção,
foco, inputs ou payload. Não há recomendação de provider porque nenhum conjunto `active` existe. O
backend repete o bloqueio após `merge_credentials`, comparando o estado efetivo para não quebrar
round-trip legado sem mudança.

A emenda de 2026-08-31 troca o eixo do gate de “provider é `coming_soon`?” para “o tenant já possui
este provider?”. A disponibilidade não muda: os 19 pares seguem `coming_soon` e o inventário 0/19/3
continua válido, porque nada é promovido a `active`. O que se abre é manutenção do que já existe —
sem ela um bundle parcial (por exemplo Meta em `signature_pending`, sem `app_secret`) ficaria
permanentemente irreparável pela própria UI que o exibe. Por isso a edição é liberada também para
bundles incompletos: o gate chaveia em “mesmo provider persistido”, não em “bundle completo”.

O selo `CONFIGURADO` exige bundle inteiro, e completude é lida por allowlist (`active` ou
`configuration_only`), nunca por negação de `incomplete` — caso contrário `signature_pending` seria
anunciado como configurado, exatamente o tipo de exagero que este wish existe para eliminar.

“Possuir o provider” não pode ser lido pelo campo `*_provider` no caso do WhatsApp. O runtime não
consulta `whatsapp_provider` em lugar nenhum: o webhook resolve o tenant por `meta_phone_number_id` e
escolhe a saída pelo token presente (`meta_permanent_token`, `zapi_token`, `evolution_api_key`,
`uazapi_token`). Gravar o bundle de outro provider adota esse provider mesmo sem tocar no rótulo — e
a rota ainda emite `meta_verify_token` automaticamente quando identidade Meta aparece, fechando o
handshake. Por isso o gate de WhatsApp compara **grupos de campos por provider**, não o rótulo. As
demais capacidades compartilham um único trio url/chave/modelo, então nelas o rótulo basta.

Pelo mesmo motivo, ligar resposta por voz (`tts_enabled` de falso para verdadeiro) é verificado
isoladamente contra o bundle armazenado: condicionar o teste ao formato do patch permitiria escapar
dele apenas anexando um campo de credencial ao mesmo request.

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
| 7 | O gate proíbe **adoção**, não manutenção (emenda 2026-08-31) | Bloquear escrita em provider já persistido não protege ninguém — o tenant já o usa — e deixaria bundles parciais irreparáveis. |
| 8 | Bundle incompleto continua editável | O gate chaveia em “mesmo provider persistido”; exigir bundle completo criaria um beco sem saída para quem está no meio da configuração. |
| 9 | `CONFIGURADO` por allowlist de readiness | `signature_pending` é bundle parcial; negar apenas `incomplete` anunciaria como pronto um Meta sem `app_secret`. |
| 10 | Controles sem credencial ficam livres | `multimodal_text_fallback_enabled` e `audio_max_seconds` não adotam provider algum, então continuam graváveis por tenant sem provider. |
| 11 | WhatsApp é gated por grupo de campos, não pelo rótulo | `whatsapp_provider` não tem autoridade de runtime; o despacho é por credencial presente, então só o bundle revela a adoção real. |
| 12 | O gatilho de voz é checado por transição, não por formato do patch | Condicionar ao patch “só de controle” deixava ligar voz sobre bundle incompleto anexando qualquer campo de credencial. |

## Risks & Assumptions

| Risk | Mitigation |
|---|---|
| Legado parecer desligado | Texto de roadmap; runtime intocado. |
| PATCH bloquear round-trip | Testes de eco/máscara e comparação pós-merge. |
| Matriz Python/TS divergir | Teste de paridade e gate agregado. |
| Badge não ser acessível | Tokens fixos e teste de contraste. |
| Manutenção virar porta de adoção | O gate compara o provider persistido com o pós-merge; troca, bootstrap e limpeza seguem 422 e atômicos. |
| `CONFIGURADO` exagerar prontidão | Allowlist `active`/`configuration_only`; `signature_pending` e `incomplete` viram `INCOMPLETO`. |
| Adoção por bundle sem tocar no rótulo | Grupos de campos por provider no WhatsApp; escrever bundle alheio é 422 mesmo com `whatsapp_provider` intocado. |
| Rótulo `*_provider` órfão, sem credenciais, virar porta de entrada | Resíduo aceito e não medido (censo está fora do wish): nas capacidades não-WhatsApp um tenant com rótulo persistido e bundle vazio pode montar bundle completo do par `coming_soon`. Revisitar em `api-provider-runtime-shutdown`, que fará o censo. |

## Success Criteria

- [ ] A evidência tem 22 decisões: 19 exibíveis `coming_soon` e três `removed`, sem segredo nem censo.
- [ ] Python e TS têm exatamente os mesmos 19 pares, todos `coming_soon`; removidos estão ausentes.
- [ ] Todo PATCH que adota provider não persistido — troca, bootstrap ou limpeza — recebe 422
  `provider_not_available` e zero mutação, enquanto eco legado idêntico e manutenção do provider já
  persistido passam.
- [ ] Escrever o bundle de outro provider de WhatsApp recebe 422 mesmo sem tocar em
  `whatsapp_provider`, e ligar `tts_enabled` sobre bundle incompleto recebe 422 mesmo quando o patch
  também carrega campos de credencial.
- [ ] Cada card de par não persistido exibe exatamente o selo laranja de 10 px, não aceita
  seleção/input/payload e usa locale correto; não resta nenhuma variante de “Configuração apenas”.
- [ ] O par persistido exibe `CONFIGURADO`/`CONFIGURED` com bundle completo e `INCOMPLETO`/`INCOMPLETE`
  com bundle parcial, e ambos abrem edição; `signature_pending` nunca aparece como configurado.
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
- **Amended after review:** 2026-08-31 — maintenance exception (decisions 7-10). The SHIP verdict above
  covers the pre-amendment content only; the amended design has not been independently reviewed.
<!-- genie-design-review:end -->
