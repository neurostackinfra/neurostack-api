# Draft: Ativação e organização do catálogo de APIs

| Campo | Valor |
|---|---|
| **Slug** | `api-provider-activation-catalog` |
| **Data** | 2026-09-28 |
| **WRS** | 60/100 |
| **Origem** | Correção do contrato entregue em `api-provider-availability` |

## Problema

A aba APIs marcou quase todos os provedores como `EM BREVE`, embora o produto queira permitir a
inserção de credenciais para um conjunto explicitamente definido de integrações. Além disso, os
blocos estão fora da ordem desejada e parte dos nomes/subtítulos não comunica a estratégia comercial.

`Ativa` neste pedido significa que a pessoa pode abrir o card e cadastrar credenciais; não significa
que a NeuroStack já homologou/testou comercialmente a integração. Ainda precisa ser decidido se
`ativa` também exige um caminho de runtime implementado — o contrato atual deliberadamente trata
“salva configuração mas o runtime não usa” como estado enganoso.

## Pedido funcional informado

### Ordem visual

1. API WhatsApp
2. API LLM
3. API Fallback
4. Módulo de voz
5. API STT
6. API TTS
7. Gatilhos de voz

### WhatsApp

- Ativas: Z-API, Uazapi, Meta Oficial e Evolution (Open Source).
- Em breve: 360dialog e Twilio.
- Remover os subtítulos dos cards de WhatsApp; manter apenas os títulos.

### LLM

- Ativas, na ordem: Dropout by Neuralake, GPT by OpenAI, Gemini by Google e Outros by OpenRouter.
- Em breve, antes das ativas: Cortex by NeuroStack.
- Decisão aberta: manter `Outros` como integração genérica/OpenRouter ou expor Claude diretamente.

### Fallback multimodal

- Ativas: Outros by OpenRouter e Dropout by Neuralake.
- Não exibir nomes de modelos do OpenRouter nos cards.

### STT

- Ativas: Whisper by OpenAI, Scribe by ElevenLabs e Outros by OpenRouter.
- Em breve: Deepgram STT by Deepgram, Azure STT by Microsoft e Cloud STT by Google.
- A presença de Microsoft/Google/Deepgram no roadmap é estratégica e deve ser preservada.

### TTS

- Ativas: ElevenLabs, Google Cloud TTS e OpenAI.

## Estado atual confirmado no código

- O catálogo frontend e o gate backend compartilham os estados `active`/`coming_soon`, mas hoje só
  Gemini está `active`; os demais cards ficam inertes e o PATCH devolve
  `provider_not_available`.
- WhatsApp Meta, Z-API, Uazapi e Evolution já possuem campos e caminhos de runtime; 360dialog e
  Twilio não possuem campos próprios.
- LLM OpenAI, Dropout, Gemini e `outros` estão no conjunto operacional do backend e compartilham o
  bundle URL/chave/modelo.
- O fallback multimodal atual aceita apenas `openrouter`; Dropout ainda não pertence ao contrato do
  fallback.
- STT operacional hoje é `outros` e `openai_whisper`. Scribe, Google, Deepgram e Microsoft são
  explicitamente tratados como não suportados no runtime.
- O `outros` de STT atual representa o caminho de transcrição compatível com Groq/OpenAI, não uma
  integração OpenRouter comprovada.
- TTS ElevenLabs e Google têm adapters; OpenAI ainda não pertence ao schema/allowlist atual, embora
  já exista um wish separado para esse provedor.
- A ordem atual é WhatsApp, LLM, STT, fallback multimodal e TTS; os gatilhos de voz são renderizados
  fora da posição solicitada.

## Checagem de tamanho de escopo

O pedido tem um núcleo único de catálogo/credenciais, mas ativar opções sem caminho operacional
introduziria trabalho independente em três integrações: Dropout multimodal, ElevenLabs Scribe e
OpenAI TTS. A proposta de design deve separar:

1. catálogo, textos, ordem e promoção das integrações já operacionais;
2. adapters ausentes, cada um testável e entregável isoladamente;
3. promoção pública do card somente junto do adapter correspondente, caso `active` implique runtime.

### Decomposição confirmada

Este rascunho passa a ser o registro guarda-chuva e não deve virar um único wish. O trabalho foi
separado em quatro entregas:

1. **Catálogo, ordem e contrato público de disponibilidade** — textos, sequência dos blocos/cards,
   remoção dos subtítulos de WhatsApp, política de `active` e promoção coordenada dos pares; primeiro
   brainstorm em `../api-provider-catalog-presentation/DRAFT.md`.
2. **STT OpenRouter + ElevenLabs Scribe** — corrigir o adapter OpenRouter para a resposta JSON atual,
   adicionar Scribe e respectivos gates/testes; bloqueia a promoção desses dois cards.
3. **Fallback Dropout by Neuralake** — adicionar provider explícito e definir/cobrir as contingências
   de texto e áudio; bloqueia a promoção do card Dropout no fallback.
4. **OpenAI TTS** — executar o wish já aprovado `tts-openai-provider` e alinhar sua promoção à nova
   semântica; bloqueia a promoção do card OpenAI em TTS.

Homologação externa posterior é uma atividade separada e não bloqueia `active`; implementação do
caminho técnico continua sendo uma pré-condição proposta para que “pronta para colocar credenciais”
não signifique configuração morta.

## Evidência externa atualizada

- Desde julho de 2026, o OpenRouter oferece oficialmente
  `POST /api/v1/audio/transcriptions`, inclusive multipart. A resposta padrão é JSON com `text` e
  `usage`; o adapter local envia `response_format=text` e devolve o corpo cru, portanto precisa de
  ajuste antes de ser promovido.
- O OpenRouter expõe modelos Claude por IDs `anthropic/...` usando a mesma API unificada. Um card
  Claude direto não é necessário para a necessidade atual.
- A documentação pública da Neuralake confirma `model="multimodal"`, mas descreve a capacidade como
  visão/documentos e não documenta áudio. A abrangência do fallback Dropout é a decisão ainda aberta.

## Abordagens em avaliação

### A — `active` exige credencial + runtime implementado (recomendada)

Promover imediatamente os pares já implementados e coordenar as integrações ausentes como grupos
isolados. Não exige homologação externa para ativar, mas exige que o produto consiga usar a
credencial. Mantém apenas dois estados públicos e evita configuração morta.

### B — `active` significa apenas formulário gravável

Todos os cards solicitados abrem e salvam credenciais, mesmo quando o runtime ignora o provedor.
É mais rápida, mas recria o antigo estado `configuration_only` com outro nome e pode induzir a
pessoa a acreditar que o serviço está funcionando.

### C — adicionar estado público `beta/configurável`

Separa “aceita credencial” de “runtime pronto”. Comunica melhor a nuance, mas cria um terceiro estado,
mais regras de PATCH, mais copy e mais caminhos de teste, sem ter sido pedido.

## Simplicity Gate preliminar

A solução completa mais simples é manter os dois estados existentes, usar `active` para integração
tecnicamente utilizável (mesmo ainda não homologada) e `coming_soon` para qualquer integração sem
adapter. Não são necessários cache, fila, migração, novo estado persistente ou framework genérico de
plugins. Claude direto e “qualquer API” ficam fora até existir uma necessidade que o OpenRouter ou o
endpoint compatível atual não atendam.

## Riscos e restrições conhecidos

- Rotular STT como “Outros by OpenRouter” pode ser tecnicamente incorreto: o runtime atual não usa
  OpenRouter para transcrição STT.
- Promover Scribe, Dropout fallback ou OpenAI TTS apenas na matriz libera o formulário, mas não cria
  integração funcional.
- `Outros` LLM aceita o mesmo contrato OpenAI-compatible (URL/chave/modelo); isso não garante
  compatibilidade com qualquer API arbitrária.
- Claude direto acrescentaria credenciais, endpoint, validação e egress próprios; via OpenRouter ele
  pode permanecer uma escolha de modelo sem novo card.
- Há alterações não relacionadas já presentes no worktree; este brainstorm não deve sobrescrevê-las.

## Critérios de aceitação preliminares

- A matriz visual e o backend concordam exatamente sobre cada par `active`/`coming_soon`.
- Cards `active` abrem e salvam o bundle de credenciais correto; cards `coming_soon` continuam
  visíveis e inertes.
- A ordem dos blocos é WhatsApp, LLM, Fallback, Módulo de voz, STT, TTS e Gatilhos de voz.
- Cards WhatsApp não exibem subtítulo; os demais exibem exatamente os nomes/bylines aprovados.
- O fallback não revela nomes de modelos do OpenRouter.
- Cada card ativo que exigir novo adapter tem teste de contrato, egress e caminho de runtime antes da
  promoção.
- Testes de paridade impedem divergência entre a matriz Python e TypeScript.

## Decisão necessária para avançar

Confirmar o significado operacional de `ativa`:

- recomendado: aceita credenciais **e** possui caminho de runtime implementado, ainda que não tenha
  sido testada/validada pela equipe;
- alternativo: aceita e salva credenciais mesmo quando o runtime ainda não usa a integração.

## WRS

```
WRS: ██████░░░░ 60/100
 Problem ✅ | Scope ░ | Decisions ░ | Risks ✅ | Criteria ✅
```
