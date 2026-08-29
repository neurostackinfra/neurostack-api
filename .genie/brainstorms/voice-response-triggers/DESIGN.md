# Design: Gatilhos de resposta por voz

| Field | Value |
|-------|-------|
| **Slug** | `voice-response-triggers` |
| **Date** | 2026-08-29 |
| **WRS** | 100/100 |

## Problem

A clínica precisa manter sua configuração de geração de voz salva e poder interromper temporariamente o uso pago de TTS sem perder credenciais. Quando a voz estiver ativa, o painel e os quatro webhooks precisam concordar sobre quais tipos de mensagem recebida autorizam uma resposta em áudio.

## Scope

### IN

- Manter o `tts_enabled` existente como controle mestre independente das credenciais TTS.
- Persistir uma única política exclusiva de resposta: `audio_input` ou `all_inputs`.
- Usar `audio_input` como default seguro para configuração ausente, inclusive clínicas existentes.
- Mostrar as duas políticas no painel sob “Gatilhos de Voz”, com “Em breve mais gatilhos de voz” em PT e EN.
- Aplicar a política às respostas normais do agente (`suggested_response`) nos webhooks Z-API, Evolution, Uazapi e Meta Cloud, respeitando os tipos de entrada que cada canal já processa.
- Preservar fallback para a resposta original em texto quando síntese ou entrega de áudio falhar.
- Cobrir contrato, painel, decisão compartilhada e integração dos quatro canais com testes.

### OUT

- Gatilhos estratégicos, comerciais, por intenção, horário, orçamento, segmento ou consumo acumulado.
- Um motor genérico ou lista dinâmica de regras de voz.
- Limites de gasto, alertas financeiros, filas, agendamento ou telemetria de custo.
- Novos provedores TTS, novos canais ou mudanças no pipeline STT.
- Suporte novo a áudio recebido pela Evolution; o canal continua text-only e, portanto, `audio_input` nunca autoriza TTS nele até esse suporte existir.
- Converter em áudio avisos operacionais/sistêmicos como privacidade, fallback STT, áudio longo, revisão de arquivo ou handoff.

## Approach

Adicionar `tts_reply_policy: "audio_input" | "all_inputs"` ao mesmo bucket JSON de credenciais/configuração que já contém `tts_enabled`. O status público sempre resolve campo ausente para `audio_input`; a UI carrega essa política e oferece duas opções exclusivas, enquanto o switch mestre continua pausando ou liberando toda geração sem limpar provedor, URL, modelo, voz ou chave.

O helper compartilhado `_send_reply_with_optional_tts` recebe um booleano explícito `input_was_audio`. Ele chama TTS somente quando o mestre está ativo e a política permite: `all_inputs` permite toda entrada já suportada que chegue a uma `suggested_response`; `audio_input` permite apenas quando o webhook reconheceu a mensagem original como áudio. Z-API, Uazapi e Meta capturam esse fato de seus predicados de áudio já existentes antes da transcrição. Evolution continua aceitando somente texto e passa `false` no único caminho que hoje alcança o helper; adicionar áudio/STT nesse canal é outro trabalho.

A política governa somente respostas normais do agente já centralizadas no helper. Avisos de privacidade, recondução de falha STT, áudio longo, arquivo e handoff permanecem texto explícito: eles não são respostas a uma pergunta processada pelo agente e não devem criar custo TTS nem alterar mensagens operacionais.

Alternativas rejeitadas: dois booleanos independentes criariam estados redundantes ou contraditórios; inferir áudio pelos metadados do evento esconderia o contrato e não está uniforme entre canais; um motor genérico de gatilhos adicionaria protocolo sem um segundo gatilho estratégico aprovado.

## Simplicity Case

- **Simplest complete design:** um switch mestre existente, um enum persistido com duas políticas e um booleano normalizado por chamada ao helper compartilhado.
- **Added machinery:** somente `tts_reply_policy` e `input_was_audio`; ambos são exigidos pelas duas experiências confirmadas e pela necessidade de consistência entre quatro canais.
- **Deferred until measured:** introduzir catálogo/motor de regras apenas quando um gatilho adicional com condição diferente for aprovado para runtime; adicionar medição de custo apenas quando existir um requisito de orçamento ou alerta mensurável.
- **Complexity removed:** nenhum par de switches independentes, prioridade entre regras, estado “nenhum gatilho”, cache, fila, contador, migração relacional ou classificação linguística de “pergunta”.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | `tts_enabled` permanece o controle mestre | O backend e o painel já separam ativação de credenciais; desligar deve preservar a configuração e impedir qualquer chamada TTS. |
| 2 | As políticas são mutuamente exclusivas e persistidas em `tts_reply_policy` | Um enum torna os estados válidos explícitos e impede combinações redundantes de dois booleanos. |
| 3 | `audio_input` é o default para campo ausente | É a escolha confirmada e reduz custo/alteração comportamental para clínicas que não selecionaram o modo amplo. |
| 4 | `all_inputs` inclui qualquer tipo já suportado pelo canal que produza uma `suggested_response` | “Todos os tipos” inclui texto e áudio onde o canal já processa ambos; não amplia capacidades de entrada nem classifica se a frase tem forma interrogativa. |
| 5 | O tipo original entra no helper como `input_was_audio` | Z-API, Uazapi e Meta já reconhecem áudio antes da transcrição; Evolution passa `false` em seu caminho text-only. Transmitir o fato explicitamente evita inferência por conteúdo textual. |
| 6 | Política negada pula síntese e envia texto diretamente | Controla custo e mantém a resposta; fallback de erro continua reservado a tentativas TTS que falham. |
| 7 | As opções aparecem como seleção exclusiva, não como dois switches persistidos | A semântica acessível da UI deve corresponder ao enum e nunca permitir ambos ligados ou ambos desligados. |
| 8 | Não há migração de banco | `api_credentials` já é JSON; default no read/runtime dá compatibilidade determinística sem backfill. |
| 9 | Gatilhos TTS cobrem apenas `suggested_response` | É o limite do helper existente e corresponde a respostas do agente; avisos operacionais mantêm cópia e formato determinísticos. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Três provedores identificam áudio em formatos diferentes e Evolution não o processa | High | Reusar os predicados nativos de Z-API, Uazapi e Meta; testar Evolution somente com texto e manter áudio recebido explicitamente OUT. |
| 2 | Uma clínica com TTS ativo hoje envia áudio para toda entrada e passará ao default restrito | Medium | Tornar `audio_input` o default deliberado e visível; somente seleção explícita de `all_inputs` mantém o alcance amplo. |
| 3 | O painel pode enviar configuração TTS inteira ao alterar apenas a política | Medium | Reusar preservação de segredo mascarado e testar mudança de política sem nova chave nem perda dos campos salvos. |
| 4 | “Pergunta” pode ser interpretada como frase interrogativa ou como aviso automático | Low | Definir no contrato como entrada suportada que alcance o pipeline e produza `suggested_response`; avisos operacionais ficam fora. |
| 5 | Áudio transcrito vira texto antes da resposta | Medium | Capturar `input_was_audio` a partir do envelope original, nunca do transcript final. |

## Success Criteria

- [ ] Com `tts_enabled=false`, nenhuma política chama síntese; todas as respostas saem em texto e credenciais permanecem salvas.
- [ ] Com `tts_enabled=true` e `tts_reply_policy=audio_input`, entrada de áudio que produz resposta usa TTS e entrada não-áudio envia texto sem síntese.
- [ ] Com `tts_enabled=true` e `tts_reply_policy=all_inputs`, entradas de texto em todos os quatro canais e entradas de áudio já suportadas por Z-API, Uazapi e Meta que produzam `suggested_response` usam TTS.
- [ ] Campo ausente ou configuração antiga resolve para `audio_input` no GET e no runtime.
- [ ] PATCH rejeita valores fora de `audio_input | all_inputs` e permite trocar política sem reenviar chave TTS salva.
- [ ] Falha de síntese ou de entrega continua enviando a resposta original em texto.
- [ ] Z-API, Uazapi e Meta Cloud passam corretamente áudio/não-áudio à decisão compartilhada; Evolution passa `false` no fluxo text-only e continua descartando áudio recebido como `non-text`.
- [ ] Avisos de privacidade, fallback STT, áudio longo, arquivo e handoff continuam em texto e não chamam TTS.
- [ ] O painel mostra o switch mestre, duas opções exclusivas sob “Gatilhos de Voz” e “Em breve mais gatilhos de voz” em PT e EN.
- [ ] Salvar/recarregar o painel preserva a política e não apaga a configuração do provedor quando a voz é desligada.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `d17d0230efeeaf039b62469e8be005c4df9a1208b6a00ddd5c07acb97c3a4c64`
- **Reviewer:** /root/review_voice_design
- **Reviewed at:** 2026-08-29T20:51:57.000Z
<!-- genie-design-review:end -->
