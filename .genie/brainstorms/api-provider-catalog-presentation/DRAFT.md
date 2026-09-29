# Draft: Catálogo, ordem e disponibilidade pública das APIs

| Campo | Valor |
|---|---|
| **Slug** | `api-provider-catalog-presentation` |
| **Data** | 2026-09-28 |
| **WRS** | 100/100 |
| **Guarda-chuva** | `../api-provider-activation-catalog/DRAFT.md` |

## Problema

A aba APIs bloqueia quase todos os provedores como `EM BREVE`, está fora da ordem de leitura
solicitada e usa nomes/subtítulos que não correspondem ao posicionamento de produto. O catálogo
público precisa diferenciar “tecnicamente configurável” de “homologado pela equipe”: homologação não
é pré-condição para o card ficar ativo.

## Escopo

### IN

- Definir `active` como: card editável, bundle validado e persistível e caminho de runtime
  implementado; teste/homologação externa pode ocorrer depois.
- Manter somente os estados públicos `active` e `coming_soon`.
- Separar e sincronizar três domínios: catálogo público, IDs wire reconhecidos (incluindo legado) e
  adapters operacionais; o espelho frontend cobre apenas o catálogo público.
- Reorganizar os blocos para WhatsApp → LLM → Fallback → cabeçalho Módulo de voz → STT → TTS →
  Gatilhos de voz.
- Renderizar os gatilhos depois dos cards TTS, sem alterar a semântica atual do switch/política.
- Remover somente os subtítulos dos cards WhatsApp; manter o texto descritivo do bloco WhatsApp.
- Fixar nomes, bylines e ordem dos cards conforme a matriz abaixo.
- Usar um ID interno explícito `openrouter` para os cards “Outros by OpenRouter” de LLM e STT,
  preservando IDs legados `outros` sem apresentá-los como OpenRouter quando apontarem para outro host.
- IDs legados `outros` permanecem válidos para leitura, runtime e eco sem mudança, mas ficam ocultos
  e bloqueados para entrada/reentrada. Na Fase A, a única exceção é um incumbent já em
  `llm/outros` ou `stt/outros` sair por troca atômica para provider explícito operacional, com bundle
  fresco completo e a mesma política de endpoint; nunca é permitido entrar ou reentrar no legado.
- Promover pares já operacionais e promover os pares dependentes no mesmo merge em que o adapter
  correspondente passa nos testes de contrato/runtime.
- Exigir evidência determinística específica para cada par promovido, com caminhos mínimos por
  capacidade; fallback exige texto e áudio bruto e WhatsApp exige envio e recebimento autenticado.
- Canonicalizar bases de providers fixos, rejeitar query em URLs de credenciais e registrar mudança
  efetiva de credenciais em evento de auditoria pós-commit sem valores sensíveis.
- Documentar rollout/backout compatível com novos IDs persistidos e a saída incumbent-only de
  bundles `outros`; `new_explicit_id_count > 0` proíbe somente rollback anterior à Fase A.
- Acrescentar testes de ordem exata; os testes atuais verificam apenas membros ordenados por `.sort()`.

### OUT

- Implementar adapters de Scribe, OpenRouter STT, Dropout fallback, OpenAI TTS ou autenticação/
  provisionamento inbound de WhatsApp neste wish; são dependências próprias.
- Executar probes live ou declarar integrações homologadas.
- Criar um provider Claude/Anthropic direto.
- Prometer compatibilidade com qualquer API arbitrária; OpenRouter é o agregador suportado.
- Nova tabela, migration de schema/tabela, cache, fila, health-check periódico ou terceiro estado público.
- Implementar envelope encryption neste wish; a ampliação de credenciais depende de assurance da
  proteção em repouso existente ou de um wish separado de remediação.

## Matriz visual alvo

| Bloco | Ordem dos cards | Estado alvo |
|---|---|---|
| WhatsApp | Meta Cloud API; 360dialog; Twilio; Z-API; Evolution API; Uazapi | Meta, Z-API, Evolution e Uazapi ativos; 360dialog e Twilio em breve |
| LLM | Cortex by NeuroStack; Dropout by Neuralake; GPT by OpenAI; Gemini by Google; Outros by OpenRouter | Cortex em breve; demais ativos |
| Fallback | Outros by OpenRouter; Dropout by Neuralake | ambos ativos após dependências |
| STT | Whisper by OpenAI; Scribe by ElevenLabs; Outros by OpenRouter; Deepgram STT by Deepgram; Azure STT by Microsoft; Cloud STT by Google | três primeiros ativos após dependências; demais em breve |
| TTS | ElevenLabs; Google Cloud TTS; OpenAI | todos ativos após dependência OpenAI |

Os cards WhatsApp exibem só o título. Os cards LLM, Fallback e STT usam exatamente as bylines da
tabela. TTS mantém apenas os nomes acima, sem copy promocional de modelo.

## Decisões tomadas

1. **Claude fica via OpenRouter.** O campo de modelo continua livre e aceita IDs Claude presentes no
   catálogo OpenRouter. Um card Anthropic direto só entra se um cliente/parceria exigir chave e
   contrato diretos com a Anthropic.
2. **OpenRouter é provider explícito, não um sinônimo de qualquer endpoint.** Isso permite host
   pinning, métricas corretas, defaults seguros e copy honesta.
3. **Sem estado `beta/configurável`.** A nuance “ainda não homologado” não cria outra máquina de
   estados; fica na comunicação operacional, não no card.
4. **Sem nomes de modelos no fallback.** O card mostra apenas título e fornecedor; o modelo é campo de
   credencial/configuração.
5. **A ordem WhatsApp permanece a da tela de referência.** O pedido altera status e remove bylines,
   mas não pediu reordenação desse grid.

## Dependências

- `whatsapp-inbound-provider-activation` bloqueia Meta, Z-API, Evolution e Uazapi ativos até que uma
  clínica nova consiga configurar envio e recebimento autenticado pela superfície pública.
- `stt-openrouter-scribe-activation` bloqueia `stt/openrouter` e `stt/elevenlabs_scribe` ativos.
- `dropout-multimodal-fallback` bloqueia `multimodal/dropout` ativo.
- Wish aprovado `tts-openai-provider` bloqueia `tts/openai` ativo.
- Todos os pares ativos, inclusive os já existentes, precisam de evidência nomeada de configuração,
  runtime e egress; membership em allowlist/conjunto operacional não basta.

## Simplicity Gate

### Solução completa mais simples

Reusar a matriz `active`/`coming_soon`, tornar os IDs agregadores explícitos, alterar apenas a ordem e
copy da lista declarativa e exigir adapter antes da promoção. A persistência JSONB e o endpoint único
de credenciais permanecem.

### Custo adicional justificado

- Um ID `openrouter` por capacidade: pago pela necessidade presente de host pinning, observabilidade
  e byline correta.
- Testes de ordem: pagos pelo requisito explícito de sequência dos blocos e cards.
- Validação provider↔host no save: paga pelo fato de o card passar a prometer configuração utilizável.
- Registro de evidência por par, auditoria redigida e rollout compatível: pagos pelos riscos atuais
  de promoção sem prova, redirecionamento de dados e rollback após persistir IDs novos.

### Maquinaria rejeitada/deferida

- Provider SDK genérico, catálogo dinâmico, auto-discovery de modelos e Claude direto ficam fora.
- Claude direto só será reconsiderado quando houver requisito contratual/BYOK de cliente que não possa
  passar pelo OpenRouter.

## Riscos e mitigações

| Risco | Mitigação |
|---|---|
| Relabel de `outros` esconder configuração customizada legada | Novo ID explícito `openrouter`; legado continua reconhecido, mas não é apresentado com byline falsa. |
| Frontend e backend divergirem | Parity gate já existente + asserções de ordem e labels. |
| Um card dependente ficar ativo antes do adapter | Dependência de wish e teste que proíbe `active` fora do conjunto operacional. |
| WhatsApp aceitar credenciais de envio sem inbound autenticado/provisionável | Dependência dedicada; `active` exige envio e recebimento configuráveis por uma clínica nova. |
| `outros` legado ser rejeitado ao sair da matriz pública | Separar catálogo público, IDs reconhecidos e adapters operacionais. |
| URL com query vazar segredo por GET/cache/log ou formar rota inválida | Rejeitar query em bases de credenciais e fixar path canônico nos providers hospedados. |
| Manifesto de evidência virar comando injetável | Manifestos carregam IDs de teste como dados; execução usa argv fixo, nunca `shell`/`eval`. |
| Novos IDs/segredos impedirem rollback seguro | Release de compatibilidade antes do catálogo, census legado e backout que preserva leitores/runtime. |
| Ampliação de segredos aumentar impacto do JSONB plaintext | Gate de assurance de banco/backups/IAM ou wish separado de proteção em repouso. |
| “Ativo” ser confundido com homologado | Definição documentada: tecnicamente configurável, homologação posterior. |
| Gatilhos quebrarem ao mover de posição | Manter componente/endpoint e testar apenas composição/ordem e comportamento existente. |
| Dropout não aceitar áudio no fallback | Manter `coming_soon` até existirem provas separadas de texto e áudio bruto WhatsApp. |

## Critérios de aceitação

- A sequência renderizada dos blocos é exatamente WhatsApp, LLM, Fallback, Módulo de voz, STT, TTS
  e Gatilhos de voz.
- Cada lista de cards aparece exatamente na ordem e com o texto da matriz visual alvo.
- Nenhum card WhatsApp possui subtítulo; o bloco mantém sua descrição de canal.
- Cada par `active` abre o editor, salva um bundle completo e tem provider↔host validado antes de
  qualquer chamada de rede.
- Cada par `coming_soon` permanece visível, inerte e rejeitado pelo PATCH para nova adoção; a única
  exceção é a saída incumbent-only de `outros` para provider explícito operacional, com bundle fresco.
- A matriz TypeScript equivale ao catálogo público; Literals/IDs wire equivalem a
  `public ∪ legacy`; todo ativo é operacional e todo par ativo possui evidência específica.
- Há testes de ordem exata para `API_CAPABILITIES` e para cada lista de provedores.
- Claude pode ser escolhido por model ID no card OpenRouter, sem card/credencial Anthropic separado.
- O fallback não exibe nomes de modelos.
- Nenhuma promoção declara ou pressupõe homologação live.
- Meta, Z-API, Evolution e Uazapi só ficam ativos após envio e recebimento autenticado serem
  provisionáveis pela superfície pública, com tokens inbound write-only/revelados uma vez.
- Bases fixas usam URL canônica sem query; mudanças efetivas geram auditoria redigida pós-commit.
- A Fase A congela a matriz pública em `1 active + 21 coming_soon`; a Fase B usa
  `16 active + 6 coming_soon`. `unsafe_legacy_count == 0` é gate de B; IDs explícitos novos podem
  existir por saída incumbent-only e apenas tornam rollback anterior à A proibido. Backout B preserva A.
- A saída de `outros` atualiza o JSONB existente e não cria migration de schema/tabela.

## Decisão final confirmada

O fallback atual atende duas contingências com o mesmo provider: falha do LLM textual e falha do STT
com áudio bruto. Por confirmação do usuário em 2026-09-28, **Dropout só fica `active` quando houver
contrato e caminho técnico implementado para as duas contingências**. Até lá, o card permanece
`coming_soon`, mesmo que o fallback textual isolado já funcione. Homologação live continua fora dessa
pré-condição.

## Reabertura após Council

O conselho de 2026-09-28 concluiu `revise`: o design de apresentação estava correto, mas confundia
membership em allowlist com prova de runtime e não fechava provisionamento inbound, segurança de URL,
auditoria e rollback. A revisão mantém a matriz visual e adiciona gates proporcionais:

1. três domínios de provider separados (`public`, `recognized`, `operational`);
2. dependência dedicada para o ciclo WhatsApp completo;
3. evidência determinística por todos os pares ativos, com categorias por capacidade;
4. endpoints canônicos sem query e auditoria redigida;
5. assurance de credenciais em repouso e rollout/backout compatível antes da ativação.
6. census com semânticas separadas: legado inseguro bloqueia B; ID explícito novo bloqueia apenas
   rollback anterior à compatibilidade, sem impedir a ativação.

Esses gates não significam homologação live e não criam terceiro estado público.

## WRS

```
WRS: ██████████ 100/100
 Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅
```
