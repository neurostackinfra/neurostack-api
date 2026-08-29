# Brainstorm Draft — Gatilhos de resposta por voz

**Slug:** voice-response-triggers
**Date:** 2026-08-29
**WRS:** 100/100

## Problem

A clínica precisa manter o provedor TTS configurado e suas credenciais salvas, mas poder pausar respostas em áudio para controlar custo sem perder a configuração. Quando a voz estiver ativa, o produto também precisa definir com precisão quais mensagens recebidas autorizam uma resposta em áudio.

## Scope

### IN

- Manter o controle mestre `tts_enabled` separado das credenciais TTS já salvas.
- Exibir no painel uma seção “Gatilhos de Voz”.
- Permitir resposta em áudio somente após pergunta em áudio e/ou após qualquer tipo de pergunta, conforme a decisão pendente abaixo.
- Exibir “Em breve mais gatilhos de voz” no painel.
- Aplicar o gatilho nos quatro canais de WhatsApp com TTS operacional: Z-API, Evolution, Uazapi e Meta Cloud.
- Preservar o fallback em texto quando a síntese ou o envio do áudio falhar.
- Cobrir persistência, UI e decisão de runtime com testes.

### OUT

- Gatilhos estratégicos, comerciais, por orçamento, horário, intenção ou segmento.
- Limites de gasto, contadores de consumo, filas, agendamento ou automações de custo.
- Novos provedores TTS ou novos canais de mensagem.

## Decisions

- Reusar `tts_enabled` como controle mestre: desligar muda somente o comportamento, não apaga provedor, modelo, voz, URL ou chave.
- O runtime decide o formato da resposta antes de chamar o provedor TTS; quando o gatilho não autoriza áudio, envia o texto original sem sintetizar e sem custo TTS.
- Não criar motor genérico de regras. O primeiro recorte terá somente o menor estado persistido necessário para o comportamento escolhido.
- Os dois gatilhos são políticas mutuamente exclusivas, persistidas em um único campo; `audio_input` é o padrão seguro e `all_inputs` é a opção explícita de maior alcance.

## Simplicity Case

O desenho mínimo completo é um controle mestre já existente mais uma escolha persistida de política de resposta. O webhook recebe explicitamente se a mensagem de entrada era áudio e a função compartilhada decide entre áudio e texto. Não há motor de regras, lista dinâmica, prioridade, cache ou telemetria nova. Gatilhos futuros só justificam uma abstração maior quando um segundo gatilho com condição diferente for aprovado para runtime.

## Risks and Assumptions

- Os quatro webhooks representam áudio de entrada de formas diferentes; a implementação deverá normalizar isso no limite de cada canal sem inferir pelo texto transcrito.
- Os dois controles descritos se sobrepõem: “todas as perguntas” inclui perguntas em áudio. Sem exclusividade/prioridade definida, ambos ligados geram um estado redundante e ambos desligados podem contradizer o controle mestre ativo.
- O estado default precisa ser compatível com clínicas existentes para não ampliar custo ou alterar respostas sem ação do usuário.
- Assumimos que “pergunta” significa qualquer mensagem de entrada que chega ao pipeline e produz uma resposta do agente, não uma classificação linguística de frase interrogativa.

## Acceptance Criteria (draft)

- Desligar geração de voz preserva integralmente a configuração TTS e todas as respostas saem em texto, sem chamada de síntese.
- Com a política “somente após áudio”, uma entrada de áudio que produz resposta usa TTS, enquanto uma entrada textual que produz resposta não usa TTS.
- Com a política “todos os tipos”, toda entrada suportada que produz resposta usa TTS.
- Falha de síntese ou entrega continua enviando a resposta original em texto.
- O painel mostra “Gatilhos de Voz” e “Em breve mais gatilhos de voz” em português e inglês.
- Configurações antigas recebem um default seguro e determinístico.

## Confirmed Decision

Em 2026-08-29, o dono confirmou duas opções mutuamente exclusivas, com `audio_input` (“responder em áudio apenas quando receber áudio”) como padrão inicial. A opção `all_inputs` (“responder em áudio em todos os tipos de pergunta”) inclui entradas de áudio e exige seleção explícita.
