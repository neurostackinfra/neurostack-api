# Design: Vitrine honesta de provedores na aba APIs

| Field | Value |
|-------|-------|
| **Slug** | `api-provider-availability` |
| **Date** | 2026-08-23 |
| **WRS** | 100/100 |

## Problem

A aba APIs oferece entrada de credencial para provedores que não funcionam, e o faz por oito
declarações de disponibilidade espalhadas e incoerentes entre si, de modo que um usuário não técnico
não consegue distinguir "ainda não existe" de "está quebrado". Isso gera credencial digitada que
falha no save, credencial salva que nunca é usada, e chamados de suporte sobre uma indisponibilidade
que na verdade é roadmap.

## Scope

Este é o **primeiro de dois wishes**. Ele entrega a vitrine e o contrato, e por construção **não
altera nenhum caminho de runtime**: nenhuma clínica perde canal, nenhum paciente recebe resposta
diferente. Desligar runtime de provedor não testado é o segundo wish
(`.genie/brainstorms/api-provider-runtime-shutdown/`), que depende do censo produzido aqui.

### IN

- Uma tabela única de disponibilidade por capacidade e provedor, com exatamente dois estados:
  `active` (aceita credencial) e `coming_soon` (nome visível, entrada bloqueada). Espelhada em
  Python e TypeScript, com teste de paridade de wire ids.
- Absorver nessa tabela **as oito declarações de disponibilidade hoje existentes**, nominalmente:
  1. `comingSoon: true` em `PROVIDERS` — `clinic-config.tsx:397`
  2. `UNIMPLEMENTED_WHATSAPP_PROVIDERS` — `api_credentials.py:43`
  3. `IMPLEMENTED_WA_PROVIDERS` — `clinic-config.tsx:316`
  4. `OPERATIONAL_STT_PROVIDERS` (espelho TS) — `clinic-config.tsx:264`
  5. `OPERATIONAL_MULTIMODAL_PROVIDERS` (espelho TS) — `clinic-config.tsx:273`
  6. `SELECTABLE_LLM_PROVIDERS` — `clinic-config.tsx:406`
  7. `const isComingSoon = false` — `clinic-config.tsx:899`, morto e ainda gateando o botão de salvar
     em `:1778-1782`
  8. Os `Literal` de provider dos patches Pydantic — `src/api/schemas/config.py:61, 84, 93, 103,
     124`. São a oitava forma de dizer quem existe, e são avaliados **antes** de qualquer regra de
     domínio.
- Remover o badge `Configuração apenas` (`clinic-config.tsx:1469` e `:1592`, com o comentário que o
  motiva em `:263`): `coming_soon` o substitui, e manter os dois faria o mesmo card exibir dois
  estados.
- Substituir o painel âmbar `UnimplementedProviderNotice` (`clinic-config.tsx:476-494`) pelo card
  `coming_soon`, corrigindo a cópia que hoje recomenda o Evolution.
- Corrigir a mesma recomendação no toast de erro — `clinic-config.tsx:841`. A lista de provedores
  recomendados nas duas cópias passa a ser **derivada da tabela** (os `active` da capacidade), não
  escrita à mão, para não voltar a divergir.
- Remover `groq` e `openai` de `PROVIDER_ALLOWLISTS["multimodal"]` (`api_credentials.py:19`), do
  `Literal` em `schemas/config.py:93` e do type `MultimodalProvider` no TS.
- `PATCH /v1/config/api-credentials` rejeita **mudança de estado efetivo** em capacidade cujo
  provider **resultante do merge** é `coming_soon`, com HTTP 422 `provider_not_available` e zero
  mutação. Regra completa e semântica de comparação na seção *Approach*.
- Consolidar `provider_not_implemented` e `provider_not_operational` no único código
  `provider_not_available`, ajustando `WIRE_ERROR_CODES` (`api_credentials.py:56-57`). Os dois
  `raise` hardcoded de `validate_provider` (`:198-203` para `slm_cortex`, `:204-209` para
  360dialog/Twilio) são **deletados**, não renomeados: a regra da tabela já cobre os três, e mantê-los
  rejeitaria o eco legado antes de a regra rodar. `validate_provider` fica sendo só pertencimento ao
  allowlist. Isso migra 360dialog/Twilio de **HTTP 501 para HTTP 422**.
- Deletar o pré-loop de `validate_provider` e seu `except` em `src/api/routes/config.py:472-486`.
  `merge_credentials` (`api_credentials.py:258-265`) já valida as cinco capacidades e é o único ponto
  que possui `existing` para comparar; o segundo `except` em `:492-497` passa a ser o único
  tradutor. Precedência e o reordenamento entre camadas estão declarados em *Approach*.
- Censo de produção como **grupo 1, com stop rule** (números e uso definidos em *Approach*).
- Confirmar por teste que Microsoft não é opção de TTS.

### OUT

- **Qualquer alteração de runtime.** `OPERATIONAL_LLM_PROVIDERS`, `OPERATIONAL_STT_PROVIDERS` e
  `OPERATIONAL_MULTIMODAL_PROVIDERS` no Python (`api_credentials.py:33-42`),
  `UNSUPPORTED_STT_PROVIDERS` (`webhook.py:68`), os gates de capacidade em `webhook.py:1770` e
  `:2539`, o webhook Evolution e `runtime_projection` (`api_credentials.py:492`) ficam intocados.
- **`multimodal_readiness` não muda e não entra em escopo.** Ele compara contra
  `OPERATIONAL_MULTIMODAL_PROVIDERS` (`api_credentials.py:477`), não contra o allowlist, e `groq` e
  `openai` já estão fora daquele frozenset. Uma linha legada é `configuration_only` antes e depois.
  A parametrização que fixa isso (`tests/services/test_api_credentials.py:199-200`) mantém a
  asserção, mas **precisa de uma edição de fixture**: ela monta a linha via `merge_credentials`
  (`:206-215`), ou seja, pelo caminho de escrita que este wish fecha.
- O carve-out de silêncio do STT sem adapter (`webhook.py:1723`) e a janela de supressão de 6h
  (`_STT_FALLBACK_NOTICE_TTL`, `webhook.py:91`) permanecem exatamente como estão.
- Validação ao vivo de credencial, health check ou verificação periódica de chave.
- Construir adapters novos: Scribe, Deepgram, Azure STT, Google STT, SLM Cortex, 360dialog, Twilio.
- Feature flag, dual-mode, whitelist por tenant ou override de disponibilidade por clínica.
- Apagar, migrar ou reescrever credencial legada; migrar clínicas do Evolution.
- Remover as políticas de egress `multimodal_openai` (`safe_egress.py:350`) e `multimodal_groq`
  (`:353`) e seus `ACCEPTED_FORMATS_BY_KIND` (`multimodal_service.py:115,117`). São classificadas por
  **hostname**, não por id de provider, e seguem pagas por uma `multimodal_api_url` legada em
  `api.groq.com` ou `api.openai.com` — que a própria regra de preservação de legado garante que ainda
  existe. Só o comentário em `safe_egress.py:59-63`, que afirma que o allowlist multimodal "names
  exactly these three providers", precisa ser reescrito: a invariante deixa de valer.
- Estender `_whatsapp_readiness`: desnecessário aqui, porque ele chaveia por `whatsapp_provider`
  (`api_credentials.py:384, 405`) — exatamente o que o censo conta —, então censo zero implica que
  nenhuma linha Evolution pode reportar `active`, e 360dialog/Twilio não têm campos.
- Reintroduzir Microsoft em qualquer capacidade.

## Approach

### Uma tabela, dois pontos de aplicação

O módulo puro `src/services/api_credentials.py` ganha uma única fonte de verdade:

```
PROVIDER_AVAILABILITY[capability][provider] -> "active" | "coming_soon"
```

`PROVIDER_ALLOWLISTS` continua sendo o universo **exibível**; a nova tabela decide o que é
**gravável**. As oito declarações enumeradas em Scope IN passam a derivar dela ou deixam de existir.

Aplicação em duas camadas, ambas necessárias: a UI sozinha não basta porque um PATCH manual a
contorna; o PATCH sozinho não basta porque o usuário ainda veria o formulário. A terceira camada,
runtime, é deliberadamente do segundo wish.

**Disponibilidade de entrada e disponibilidade de runtime são conceitos distintos, e este wish só
mexe no primeiro.** Um provedor `coming_soon` deixa de aceitar credencial nova, mas quem já tem
credencial salva continua funcionando exatamente como hoje. É a leitura literal da regra do dono do
produto — "desativadas para inserir credenciais" — e é o que torna este wish incapaz de derrubar
qualquer clínica.

### Matriz de disponibilidade

| Capacidade | `active` | `coming_soon` | Fora da lista |
|---|---|---|---|
| WhatsApp | `meta_cloud`, `zapi`, `uazapi` | `dialog360`, `twilio`, e `evolution` **conforme o censo** | — |
| LLM | `openai`, `dropout`, `outros` | `slm_cortex` | — |
| Multimodal | `openrouter` | — | `groq`, `openai` |
| STT | `outros` (Groq) | `openai_whisper`, `elevenlabs_scribe`, `google_stt`, `deepgram_stt`, `microsoft_stt` | — |
| TTS | `elevenlabs`, `google` | — | `microsoft` (já removido) |

### Removido e `coming_soon` são rejeitados em camadas diferentes, de propósito

- **Removido** (`groq`/`openai` multimodal): o id sai do `Literal` em `schemas/config.py:93`, então
  o FastAPI rejeita na validação de request com o **422 genérico do Pydantic**. Está correto: o id
  deixou de existir no contrato, não é um "ainda não".
- **`coming_soon`**: o id **permanece** no `Literal`, o request parseia, e a regra de domínio devolve
  422 `provider_not_available` com `capability` e `provider`. Para os cinco ids de `stt_provider`
  (`schemas/config.py:103`) isso é **obrigatório**: são os únicos `coming_soon` que um tenant pode de
  fato ter salvo, e narrar o `Literal` rejeitaria o eco legado antes de a regra rodar. Para
  `slm_cortex` (`:84`), `dialog360` e `twilio` (`:61`) nenhum tenant pode existir —
  `validate_provider` sempre os rejeitou (`api_credentials.py:198-209`) e a UI nunca os envia
  (`clinic-config.tsx:745`) —, então ali a razão é só qualidade de mensagem: um `(em breve)`
  explicado vale mais que um 422 genérico do Pydantic.

### O censo e o que cada número decide

Grupo 1, rodado contra produção por quem tem acesso (o `DATABASE_URL` deste ambiente aponta para
`localhost:5434`, dev). Dois números, sobre a coluna JSONB `api_credentials` da tabela de config:

1. **Tenants com `whatsapp_provider = 'evolution'`.** É a stop rule: o resultado preenche a célula do
   Evolution na matriz antes de qualquer código ser escrito.
2. **Tenants com `stt_provider` em `{openai_whisper, elevenlabs_scribe, google_stt, deepgram_stt,
   microsoft_stt}`.** Este número **não decide nada neste wish** — os cinco são `coming_soon`
   incondicionalmente, porque o runtime intocado garante que ninguém para de funcionar. Ele é
   medido aqui porque sai da mesma consulta, e é registrado como o dimensionamento da explosão do
   segundo wish, que é quem de fato desliga esses caminhos.

### A célula do Evolution é decidida por medição, não por suposição

O Evolution é o único caso em que o código contradiz a lista do dono do produto: ele tem handler
completo (`webhook.py:2047-2255`), adapters funcionando (`zapi_service.py:197`, `:237`) e é listado
como implementado em `IMPLEMENTED_WA_PROVIDERS`. Marcá-lo `coming_soon` faria uma clínica que está
rodando nele abrir a aba e ver `(em breve)` no canal que carrega o WhatsApp dela — precisamente a
desonestidade que este design existe para remover.

- **Censo = 0 tenants:** `evolution` vira `coming_soon`. Não há clínica para contradizer, e o card
  passa a dizer a verdade — está previsto, não testado, sem ninguém usando.
- **Censo > 0 tenants:** `evolution` permanece `active` neste wish. Desligá-lo passa a ser decisão do
  segundo wish, junto da migração das clínicas afetadas.

Como a cópia recomendada passa a ser derivada da tabela, os dois ramos ficam consistentes sem
condicional: no ramo zero o Evolution some das recomendações automaticamente; no ramo positivo ele
continua sendo uma recomendação verdadeira.

### PATCH: comparar estado efetivo pós-merge, não o corpo do request

`clinic-config.tsx:706` e `:752-759` enviam os campos de WhatsApp e STT incondicionalmente em todo
save. Rejeitar por **presença** travaria a aba inteira para uma clínica em `openai_whisper`. Mas
rejeitar por "valor diferente do persistido" lido do corpo do request também trava, porque o
round-trip GET→UI→PATCH **não é um eco fiel**:

- `credential_status` sintetiza `"audio_max_seconds": resolved.get("audio_max_seconds", 60)`
  (`api_credentials.py:461`) e a UI reenvia (`clinic-config.tsx:754`) — numa linha sem o campo, `60`
  difere de ausente.
- O GET devolve o **alias canônico**: `"whisper_model": resolved.get("stt_model_name")`
  (`api_credentials.py:465`), e a UI reenvia sob a chave `whisper_model` (`:753`) — numa linha que
  guarda `stt_model_name`, a comparação campo a campo falha sempre.
- A UI substitui `stt_api_url` nulo pelo default do provedor (`:657`) e força `""` para `google_stt`
  (`:759`).
- O segredo volta mascarado (`api_credentials.py:429`) e é reenviado mascarado.

**A capacidade é congelada pelo provider que o merge produz, não pelo que estava salvo.** Chavear no
persistido erra nos dois sentidos: um tenant novo (`api_credentials = {}`) selecionando um
`coming_soon` não seria coberto, e um tenant preso num `coming_soon` nunca conseguiria **sair** dele
— o que contradiz a preservação de legado e a migração das clínicas do Evolution.

A regra, então:

```
merged = merge_credentials(existing, patch)          # preview puro
para cada capacidade:
    se PROVIDER_AVAILABILITY[cap][merged[provider]] != "coming_soon": segue
    se o patch traz valor fresco em SECRET_FIELDS da capacidade:        rejeita
    se estado_canônico(merged, cap) != estado_canônico(existing, cap):  rejeita
```

Um único predicado cobre as cinco formas: seleção nova é rejeitada; eco legado passa; **migrar para
um provedor `active` passa**, porque o merge deixa de ser `coming_soon`; segredo fresco em capacidade
congelada é rejeitado; e linha vazia escolhendo `coming_soon` é rejeitada.

Três definições sustentam o predicado:

1. **A UI omite do corpo todos os campos de uma capacidade congelada** — inclusive o provider e o
   default de URL —, no padrão já provado para multimodal em `clinic-config.tsx:763`
   (`if (multimodalDirty)`). O card `coming_soon` não é selecionável, mas os cards `active` da mesma
   capacidade continuam sendo: é assim que a saída de um provedor congelado permanece possível, e
   assim que a capacidade se descongela — escolhido um `active`, os inputs voltam a renderizar e a
   enviar. Vale também para o WhatsApp, que hoje não tem coerção nenhuma
   (`clinic-config.tsx:618` lê `data.whatsapp_provider ?? "zapi"`, sem o tratamento que o LLM tem em
   `:639-641`) e envia incondicionalmente em `:706`.
2. **`estado_canônico` são os quatro campos canônicos da capacidade** — `{cap}_provider`,
   `_api_url`, `_api_key`, `_model_name` — com ausente ≡ `None` ≡ `""`, os aliases de STT
   **resolvidos e depois removidos dos dois lados**. A remoção é obrigatória:
   `resolve_stt_credentials` (`api_credentials.py:282-306`) só **preenche** o canônico a partir do
   alias, nunca apaga o alias, e a UI manda `whisper_model` sem mandar `stt_model_name`
   (`clinic-config.tsx:753`), então o pop de `merge_credentials:277-278` não dispara e `merged`
   ganharia uma chave que `existing` não tem — diferença espúria, 422 indevido.
   `audio_max_seconds` fica **fora** do conjunto: é limite de duração do pipeline de áudio, não
   credencial, e o default `60` sintetizado pelo GET (`api_credentials.py:461`) travaria toda linha
   que não guarda o campo.
3. **A frescura de segredo é julgada no patch, não no merge.** `merge_credentials` já descarta
   segredo `None`, `""` ou mascarado antes de mesclar (`:270-273`), então máscara e vazio passam por
   construção. Mas comparar só o canônico deixaria escapar uma chave nova enviada pelo **alias**:
   com `stt_api_key` já preenchido, `resolve_stt_credentials` não promove `whisper_api_key`
   — o guard em `:286` exige o canônico vazio —, e a comparação canônica não veria diferença
   nenhuma. Por isso a cláusula de segredo é independente e olha o corpo.

Precedência de erro, agora que `config.py:472-486` some e `merge_credentials` é o único validador:
`invalid_credentials_payload` (campo desconhecido, `:252-257`) → `invalid_provider_for_capability`
(`:258-265`) → `provider_not_available` → `fresh_api_key_required`. Isso exige mover o cálculo do
merge para **antes** de `_require_fresh_keys` (hoje em `:266`, antes de `merged` existir em `:268`);
o merge é puro, então antecipá-lo não custa nada. Nenhum teste atual afirma a ordem entre esses
códigos.

Deletar o pré-loop de `config.py:472-486` também muda uma ordem **entre camadas**, e isso é
deliberado: hoje um provider inválido é rejeitado antes de `_resolve_config_tenant` (`:487`) e
`_load_config` (`:488`), sem ida ao banco. Depois, a resolução de tenant e os erros de autorização
passam a vir primeiro. É a ordem correta — autorização antes de validação de payload — e custa um
round trip a mais num caminho de erro.

`src/api/routes/config.py:539` é o único ponto do backend que grava **valores** de credencial (os
demais — `builder.py:309`, `config.py:210`, `config.py:249`, `admin.py:245`, `admin.py:1005` —
apenas inicializam a coluna com `{}` na criação da linha), então essa regra fecha a fronteira sem
guarda em outro lugar.

### Alternativas descartadas

1. **Só trocar a cor do badge e manter as declarações atuais.** Rejeitada: não fecha as armadilhas —
   `dialog360`/`twilio` continuariam abrindo o formulário inteiro para falhar no save.
2. **Sumir da lista com tudo que não é ativo.** Rejeitada pelo pedido explícito: o nome visível é o
   sinal de roadmap que faz o usuário não técnico entender que aquilo vai existir um dia.
3. **Disponibilidade por tenant, via flag ou whitelist.** Rejeitada pelo Simplicity Gate: máquina
   dormente sem requisito presente.
4. **Rejeitar o PATCH por presença do campo.** Rejeitada por travar a aba para os tenants afetados.
5. **Rejeitar comparando o corpo do request com o persistido.** Rejeitada: o round-trip normaliza,
   sintetiza default e troca alias, então "o mesmo valor" chega diferente e a trava volta.
6. **Congelar a capacidade pelo provider persistido.** Rejeitada: deixa passar a seleção nova numa
   linha vazia e impede para sempre a migração para fora de um `coming_soon`.
7. **Um wish só, com vitrine e runtime juntos.** Rejeitada: o runtime concentra todos os riscos altos
   e a única mudança visível ao paciente, e seguraria a entrega visual — que é a pedida — atrás dela.

## Simplicity Case

- **Simplest complete design:** uma tabela literal de duas colunas no módulo puro que já é a fronteira
  de credenciais, consultada em dois pontos que já existem. Zero estado durável novo, zero tabela
  nova, zero endpoint novo, zero mudança de runtime.
- **Added machinery:** a tabela, e a normalização canônica da seção PATCH. A normalização é o preço
  honesto de um esquema que carrega dois nomes para o mesmo campo de STT: ela compõe
  `merge_credentials` e `resolve_stt_credentials`, que já existem, e acrescenta só o descarte dos
  aliases. As oito declarações são **substituídas**, não somadas; dois códigos de erro viram um;
  dois `raise` hardcoded viram uma regra de tabela; dois badges viram um; um pré-loop redundante é
  deletado.
- **Deferred until measured:** o desligamento de runtime inteiro é o segundo wish, e sua entrada é o
  censo. Validação ao vivo de credencial só entra se o censo mostrar que chave morta é recorrente —
  a `GROQ_API_KEY` do `.env` estar morta é indício de ambiente de desenvolvimento, não medição de
  produção. Adapters novos só entram com demanda de cliente.
- **Complexity removed:** oito declarações de disponibilidade viram uma; dois estados visuais viram
  um; um painel âmbar, um `const` morto e um loop de validação duplicado desaparecem; dois provedores
  multimodais condenados somem do allowlist e do schema.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Dois estados apenas: `active` e `coming_soon` | Um terceiro estado é justamente o que hoje confunde o usuário não técnico |
| 2 | Tabela única em Python, espelhada em TS com teste de paridade | Elimina o drift entre as oito declarações atuais |
| 3 | Disponibilidade de entrada e de runtime são conceitos distintos; este wish só mexe na entrada | É a leitura literal do pedido e o que torna esta entrega incapaz de derrubar clínica |
| 4 | A célula do Evolution é preenchida pelo censo, com as duas saídas pré-definidas | O código contradiz a lista; medir é mais barato que errar em qualquer direção |
| 5 | A capacidade é congelada pelo provider **resultante do merge**, não pelo persistido | Chavear no persistido deixa passar a seleção nova numa linha vazia e prende para sempre quem já está num `coming_soon` |
| 6 | O estado comparado são os quatro campos canônicos, com alias STT resolvido **e removido dos dois lados** | `resolve_stt_credentials` preenche o canônico mas nunca apaga o alias, e a UI manda `whisper_model` sem `stt_model_name` — a chave sobrando viraria diferença espúria |
| 7 | A frescura de segredo é julgada no **patch**, não no merge | Com o canônico já preenchido, uma chave nova enviada pelo alias não é promovida e a comparação canônica não a enxergaria |
| 8 | `audio_max_seconds` fica fora do conjunto policiado | É limite de duração, não credencial, e o GET sintetiza `60` para linhas que não o têm |
| 9 | A UI omite todos os campos de capacidade congelada do corpo, mas mantém os cards `active` selecionáveis | Padrão já provado em `multimodalDirty`; e é o que garante que sair de um provedor congelado sempre seja possível |
| 10 | Os dois `raise` hardcoded de `validate_provider` são deletados, não renomeados | Mantê-los rejeitaria o eco legado antes da regra da tabela rodar; a própria tabela já cobre `slm_cortex`, 360dialog e Twilio |
| 11 | Removido é rejeitado pelo `Literal`; `coming_soon` é rejeitado pela regra de domínio | Um id que não existe mais e um id que ainda não abriu são erros diferentes e merecem mensagens diferentes |
| 12 | A resolução de tenant passa a preceder a validação de provider | Efeito colateral de deletar o pré-loop, e é a ordem correta: autorização antes de validação de payload |
| 13 | `groq` e `openai` multimodal saem da lista em vez de virar `coming_soon` | Provados incompatíveis por evidência viva; "em breve" seria promessa falsa |
| 14 | Uma credencial multimodal legada não é ecoada para o card do OpenRouter | Hoje `clinic-config.tsx:664-668` troca o id por `openrouter` mas mantém a máscara, atribuindo ao OpenRouter uma chave que é da Groq |
| 15 | `provider_not_available` substitui `provider_not_implemented` e `provider_not_operational` | Um conceito, um código de erro |
| 16 | A migração 501 → 422 é deliberada e declarada | Consolidar os códigos muda o status de 360dialog/Twilio; silenciar isso quebraria testes sem explicação |
| 17 | `config.py:472-486` é deletado, não movido | `merge_credentials:258-265` já valida as cinco capacidades e é o único ponto com `existing` em mãos |
| 18 | A cópia de recomendação é derivada dos `active` da tabela, e o mapa de labels vira ciente de capacidade | Hard-coded, a cópia já divergiu uma vez; e o discriminante passa a receber `capability` `llm` e `stt`, não só `whatsapp` |
| 19 | `openai` LLM permanece `active` sem probe | É o default histórico e há clínicas apontando para `api.openai.com`; desligar a entrada quebraria reconfiguração por falta de recibo, não por falha |
| 20 | `openai_whisper` STT vira `coming_soon` sem probe | Sem `OPENAI_API_KEY` o teste não roda; como o runtime fica intocado, marcar `coming_soon` não custa nada a quem já usa |
| 21 | O badge `Configuração apenas` é removido, não somado | Dois rótulos de indisponibilidade no mesmo card reproduzem o problema original |
| 22 | Políticas de egress `multimodal_groq` e `multimodal_openai` **permanecem** | São classificadas por hostname, não por id, e seguem pagas por URLs legadas |
| 23 | Chave morta não vira health check | Detectar rot é problema distinto, sem requisito presente |
| 24 | Censo é o grupo 1, com stop rule; o número de STT é registro, não decisão | Como item de checklist seria satisfeito depois do merge, quando a célula do Evolution já teria sido escrita errado |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Clínica ativa no Evolution ver `(em breve)` no canal que usa | High | Célula decidida pelo censo (Decision 4); com tenant vivo, `evolution` permanece `active` neste wish |
| 2 | Tenant em provedor `coming_soon` ficar travado sem salvar nada na aba | High | Comparação canônica pós-merge (Decisions 6-8), com critérios que exercitam linha **sem** `audio_max_seconds` e **sem** `whisper_model` persistidos |
| 3 | Tenant preso num provedor `coming_soon`, sem conseguir migrar para um `active` | High | O congelamento chaveia no provider **do merge** (Decision 5) e os cards `active` seguem selecionáveis (Decision 9); critério exercita a migração com chave fresca |
| 4 | Segredo novo entrar pelo alias e escapar da comparação canônica | High | Cláusula de segredo independente, decidida no patch (Decision 7), com critério que envia `whisper_api_key` fresca numa linha que guarda `stt_api_key` |
| 5 | Cliente com bundle antigo em cache reenviar default sintetizado e levar 422 | Low | Falha é 422 visível e sem mutação, resolvida por refresh; não há perda de dado |
| 6 | Consolidar códigos de erro quebrar consumidor do contrato | Medium | Inventário em Success Criteria, incluindo o **discriminante** `clinic-config.tsx:837` — que se não for retargetado faz a cópia corrigida virar código morto — e o mapa `WA_PROVIDER_LABEL` em `:838-841`, que passa a receber capacidades não-WhatsApp |
| 7 | Remover `groq`/`openai` do allowlist multimodal quebrar testes existentes | Medium | Quatro superfícies nomeadas nos critérios, incluindo `clinic-config-multimodal.test.tsx` inteiro, adicionado nesta branch e construído sobre groq/openai |
| 8 | Fixture que semeia pelo endpoint de escrita quebrar e derrubar a prova de "zero runtime" | Medium | Os dois seams estão nomeados: `tests/api/test_webhook_zapi.py:677-682` e `tests/services/test_api_credentials.py:206-215`; ambos passam a montar a linha literalmente, padrão já usado em `test_api_credentials.py:241` |
| 9 | Credencial legada de provedor **removido** ser sobrescrita ou exibida como se fosse de outro provedor | Medium | Decision 14 remove o eco da máscara para o card do OpenRouter; critério exige preservação byte a byte e afirma explicitamente o que acontece quando o usuário edita multimodal e `multimodalDirty` abre |
| 10 | "Testado" é retrato datado — um provedor aprovado pode morrer sem a lista mudar | Medium | Aceito explicitamente; a `GROQ_API_KEY` morta do `.env` é o exemplo. Mitigação é o censo, não verificação contínua |
| 11 | Card desabilitado ser lido como erro mesmo com o rótulo | Medium | Rótulo em vermelho, texto literal `(em breve)`, card sem aparência de falha e sem ícone de alerta |
| 12 | Alguém reintroduzir Microsoft no TTS por engano | Low | Teste explícito de ausência em `PROVIDER_ALLOWLISTS["tts"]`, no `Literal` de `schemas/config.py:124` e no type `TtsProvider` |
| 13 | O inventário de testes afetados estar incompleto | Medium | Os critérios nomeiam comportamento e suítes-baseline; a suíte inteira roda como porta de saída |

## Success Criteria

- [ ] O censo de produção roda como grupo 1 e registra os dois números; a célula do Evolution na
      tabela é preenchida pelo resultado, seguindo Decision 4, e o número de STT fica registrado como
      dimensionamento do segundo wish.
- [ ] Existe uma única tabela `PROVIDER_AVAILABILITY` em `api_credentials.py`, e as oito declarações
      nomeadas em Scope IN deixaram de existir ou passaram a derivar dela. Prova mecânica: um grep por
      `comingSoon`, `UNIMPLEMENTED_WHATSAPP_PROVIDERS`, `IMPLEMENTED_WA_PROVIDERS` e
      `SELECTABLE_LLM_PROVIDERS` retorna apenas a tabela, seu espelho TS e o teste de paridade.
- [ ] Teste de paridade prova que a tabela TS declara exatamente os mesmos pares
      capacidade/provedor/disponibilidade que a Python.
- [ ] Para cada provedor `coming_soon`, a UI renderiza ícone e nome, não permite seleção, não
      renderiza nenhum input de credencial e mostra `(em breve)` em vermelho e em corpo menor.
- [ ] Nenhum card exibe `Configuração apenas`; o painel `UnimplementedProviderNotice` não existe mais;
      e a lista de provedores recomendados, tanto inline quanto no toast, é derivada dos `active` da
      tabela — um teste troca a tabela e vê a cópia mudar junto.

**A regra do PATCH, exercitada nas cinco formas:**

- [ ] *Seleção nova.* Tenant em `outros` mudando `stt_provider` para `openai_whisper` recebe 422
      `provider_not_available` com `capability` e `provider`; leitura posterior prova zero mutação.
      Idem para uma linha vazia (`api_credentials = {}`) que seleciona um `coming_soon`.
- [ ] *Eco legado.* Tenant persistido em `openai_whisper` **cujo registro não contém
      `audio_max_seconds` nem `whisper_model`** salva a aba alterando apenas o LLM, com a UI omitindo
      os campos de STT; e um cliente antigo que reenvia `whisper_model` derivado de `stt_model_name`,
      `stt_api_url` default e o segredo mascarado também passa. Nenhum dos dois recebe 422.
- [ ] *Migrar para fora.* O mesmo tenant muda `stt_provider` de `openai_whisper` para `outros` com
      chave fresca e recebe 200; a capacidade deixa de ser congelada porque o provider do **merge** é
      `active`.
- [ ] *Segredo fresco em capacidade congelada.* O mesmo tenant recebe 422 ao enviar `stt_api_url`
      diferente, e recebe 422 ao enviar `whisper_api_key` nova **numa linha que guarda `stt_api_key`
      canônico** — o caso que a comparação canônica sozinha não pegaria. A credencial anterior
      permanece intacta nos dois.
- [ ] *WhatsApp.* Um tenant persistido num WhatsApp `coming_soon` (aplicável apenas se o censo tornar
      `evolution` `coming_soon`) salva outra capacidade sem 422 e consegue migrar para `zapi`.

- [ ] `provider_not_implemented` e `provider_not_operational` não aparecem mais em
      `api_credentials.py:56-57`, nos dois `raise` de `validate_provider` (`:198-209`, deletados), em
      `config.py`, nem em `clinic-config.tsx:827,837` — comentários e docstrings incluídos
      (`test_meta_credentials.py:12`, `test_uazapi_credentials.py:10,186`,
      `test_webhook_uazapi.py:131`, `clinic-config.tsx:738`). O discriminante do toast passa a testar
      `provider_not_available` e o mapa de labels resolve por capacidade, não só `WA_PROVIDER_LABEL`.
      360dialog e Twilio respondem 422, e `test_meta_credentials.py:319,321`,
      `test_uazapi_credentials.py:200,202`, `tests/services/test_api_credentials.py:60,74` e
      `tests/api/test_api_credentials.py:107` foram atualizados.
- [ ] `config.py:472-486` não existe mais e o único tradutor de `CredentialPolicyError` é o `except`
      que envolve `merge_credentials`; um teste afirma a precedência declarada em *Approach*,
      inclusive que `provider_not_available` precede `fresh_api_key_required`.
- [ ] Multimodal aceita apenas `openrouter`, e um PATCH com `multimodal_provider: "groq"` é rejeitado
      pelo `Literal` com o 422 genérico do Pydantic. Superfícies atualizadas:
      `tests/services/test_api_credentials.py:29-46` (igualdade exata de `PROVIDER_ALLOWLISTS`),
      `tests/api/test_api_credentials.py:232-239` (o `parametrize` que alimenta `:250-275`) e
      `:278-314`, e `clinic-config-multimodal.test.tsx` **por inteiro** — o arquivo é construído sobre
      groq/openai desde `:15` (`BACKEND_MULTIMODAL_IDS`) e `:87-88` (`SAVED_GROQ_STATUS`).
      `tests/services/test_event_service_config_cache.py:245` também carrega `groq`, mas monta a linha
      como dict cru e segue verde sem edição.
- [ ] `tests/services/test_api_credentials.py:199-200` mantém a asserção de que
      `multimodal_readiness` de linha legada é `configuration_only`; a fixture em `:206-215` passa a
      montar a linha literalmente em vez de chamar `merge_credentials`, seguindo o padrão de `:241`.
- [ ] `PROVIDER_ALLOWLISTS["tts"]`, o `Literal` em `schemas/config.py:124` e o type `TtsProvider`
      contêm exatamente `elevenlabs` e `google`; um teste falha se Microsoft reaparecer.
- [ ] Uma clínica com credencial legada em provedor `coming_soon` **ou removido** mantém o valor byte a
      byte após um save que não toca a capacidade, e o GET segue devolvendo `*_set` e máscara. No caso
      `coming_soon`, o card desabilitado sinaliza que existe credencial salva. No caso **removido**,
      o card do OpenRouter **não** exibe a máscara alheia (Decision 14), e um teste afirma que a linha
      legada só é sobrescrita quando o usuário de fato edita multimodal e `multimodalDirty` abre.
- [ ] Prova de zero impacto em runtime: `tests/api/test_webhook_egress.py`,
      `tests/api/test_webhook_meta.py`, `tests/api/test_webhook_uazapi.py` e
      `tests/services/test_multimodal_transcription.py` passam **sem nenhuma edição**, e
      `tests/services/test_api_credentials.py:224-226` (`runtime_projection`) permanece intacto.
      `tests/api/test_webhook_zapi.py` passa sem edição **exceto** o seam de fixture em `:677-682`,
      usado por `test_non_operational_multimodal_provider_is_not_capability` (`:976`), que semeia
      `multimodal_provider="groq"` pelo endpoint real e passa a montar a linha diretamente. Nenhuma
      asserção de comportamento nesse arquivo muda.
- [ ] Verificação manual em desktop e mobile: nenhum overflow, sobreposição ou salto de grid entre
      cards `active` e `coming_soon` na mesma capacidade, registrada com screenshot na evidência do
      grupo de UI.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

Carregar para o wish: o par alias/canônico `whisper_*` ↔ `stt_*` (`api_credentials.py:282-306`,
`:463-465`) é a aresta mais afiada deste codebase e toca todas as regras do grupo de PATCH. "Qual
nome de campo a linha persistida realmente guarda" é pergunta de primeira classe naquele grupo, não
detalhe de implementação. Corolário que custou duas rodadas de review para aparecer:
**`merge_credentials` não é um normalizador** — ele preserva chaves de alias que não popou
explicitamente, então "estado efetivo" só é bem definido depois que os aliases são resolvidos **e
removidos**, e a frescura de segredo precisa ser julgada no patch, não no resultado do merge.

O segundo wish (`api-provider-runtime-shutdown`) permanece em Simmering até o censo do grupo 1
retornar, porque a decisão do Evolution e o dimensionamento do gate novo do Meta dependem desse número.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** PENDING
- **Reviewed content SHA-256:** PENDING
- **Reviewer:** PENDING
- **Reviewed at:** PENDING
<!-- genie-design-review:end -->
