# Design: FallBack LLM Multimodal

| Field | Value |
|-------|-------|
| **Slug** | `fallback-llm-multimodal` |
| **Date** | 2026-08-29 |
| **WRS** | 100/100 |

## Problem

O recurso multimodal está apresentado e implementado como fallback exclusivo do STT, embora a
intenção do produto seja que ele também assuma a geração de texto quando o LLM principal falhar.
Hoje `EventService` captura uma falha do LLM principal e encerra a geração sem segunda rota
(`src/services/event_service.py:746-839`), enquanto o multimodal só aparece na cadeia de áudio
(`src/api/routes/webhook.py:1151-1350`); o nome `API STT Fall Back` consolida essa lacuna na UI
(`frontend/artifacts/neurostack/src/pages/clinic-config.tsx:432-433`).

## Scope

### IN

- Renomear o recurso e o card para o título exato `FallBack LLM Multimodal`, com descrições PT/EN
  que explicam as contingências de texto e áudio.
- Usar a configuração `multimodal_*` existente como segundo provedor para requisições text-only
  quando o LLM principal falhar, somente depois de opt-in textual explícito.
- Considerar utilizável somente o `content` que seja `str` com `strip()` não vazio. `None`,
  whitespace, non-string e shape inválido seguem as até duas tentativas primárias existentes;
  exceção de transporte/HTTP/egress ou JSON malformado vai direto ao fallback.
- Preservar e validar o caminho de áudio existente: STT -> LLM multimodal -> recondução por texto.
- Manter resposta, `llm_usage` e `llm_model` como atribuição compatível do resultado utilizável
  aceito, e registrar separadamente usage reportado por todas as tentativas iniciadas.
- Manter credenciais cruas restritas ao loader DB-only de runtime e fora do cache e do contrato
  público.

### OUT

- Fallback para erros anteriores ou posteriores à geração do LLM: policy, SRM, CTX, prompt,
  persistência, entrega WhatsApp ou TTS.
- Terceiro provedor, round-robin, balanceamento, circuit breaker, fila ou retry/backoff novo.
- Alterar o formato, os limites ou a mensagem final de recondução do fallback de áudio existente.
- Criar credenciais multimodais distintas para texto e áudio.
- Transformar falha dupla em handoff automático ou em nova mutação de sessão.

## Approach

O limite de orquestração será o `EventService`, que já reúne as mensagens finais, o histórico, as
credenciais cruas de runtime, o modelo selecionado e os dados de uso. O helper compartilhado
`_load_runtime_llm_credentials` (`src/services/event_service.py:361-371`) permanece com exatamente
os quatro campos `llm_*`, porque o fluxo RLHF o importa diretamente
(`src/api/routes/rlhf.py:164,244`). Um loader novo, privado e capability-scoped do `EventService`
faz uma leitura DB-only e devolve bundles separados `primary` e `multimodal_text`; nenhum deles é
cacheado ou reserializado. O bundle de fallback inclui o consentimento não secreto
`multimodal_text_fallback_enabled` e só é utilizável quando esse valor é literalmente `true`.

O rollout é opt-in e fail-closed. Configurações legadas foram consentidas apenas para STT/áudio,
portanto campo ausente, `false`, `null` ou tipo diferente de booleano nunca envia prompt ou
histórico ao provedor multimodal. A UI apresenta um toggle desmarcado, com cópia PT/EN informando
que prompt e histórico textual serão enviados ao provedor configurado; só um PATCH explícito com
`multimodal_text_fallback_enabled=true` habilita texto. Salvar os demais campos não ativa o toggle
implicitamente. O caminho de áudio existente ignora esse gate e continua inalterado.

A geração principal mantém até duas tentativas para resultados sem resposta utilizável e nenhuma
repetição automática quando levanta exceção. "Resposta utilizável" tem um único predicado: o
retorno é um mapping, `content` é `str` e `content.strip()` não é vazio; o valor aceito é a string
após `strip`. Retorno não-mapping, `content` ausente, `None`, não-string ou somente whitespace é
inutilizável e consome a mesma política de no máximo duas tentativas primárias. Exceção ou JSON
malformado encerra as tentativas primárias imediatamente e segue direto ao fallback.

Se a geração principal terminar sem conteúdo utilizável ou levantar exceção, e o bundle multimodal
estiver completo e operacional, o `EventService` usa uma factory estreita para criar um segundo
`LLMClient` apontado para `multimodal_api_url`, `multimodal_api_key` e
`multimodal_model_name`. Antes de anexar a chave, a factory exige coerência entre
`multimodal_provider` e o host: reutiliza `multimodal_kind_for_url` ou um helper puro equivalente,
confirma que OpenRouter resolveu exatamente para `multimodal_openrouter` e rejeita provider/host
divergentes, host custom ou kind desconhecido. A elegibilidade continua usando apenas bundle
completo e provider em `OPERATIONAL_MULTIMODAL_PROVIDERS`; não adiciona allowlist por modelo nem
altera `credential_status`/readiness. Não existe fallback para `llm_outros`.

O contrato do transporte/factory recebe esse egress kind já pinado por uma extensão explícita,
isolada e tipada para o caminho multimodal; a inferência genérica `_llm_kind_for_url` continua
intacta para os clientes primários, mas não participa desse fallback. A chamada multimodal ocorre
uma única vez, com as mesmas `messages` e `max_tokens`. Ela aplica o mesmo predicado de resposta
utilizável; shape/tipo inválido, whitespace, JSON malformado ou exceção degrada uma vez para o
comportamento final atual, sem retry do fallback, terceira chamada ou resultado aceito.

`suggested_response`, `llm_usage` e `llm_model` são selecionados atomicamente em memória somente a
partir do resultado utilizável aceito, preservando o contrato legado de atribuição.
Separadamente, `llm_attempts` registra toda chamada iniciada, em ordem, com `tier`
(`primary`/`multimodal_fallback`), ordinal `attempt`, provider, modelo reportado ou solicitado,
`outcome`, `accepted` e usage normalizado quando reportado. Uma entrada é criada antes do await e
finalizada depois dele; exceção, timeout ou usage ausente fica `usage_unavailable`, sem inventar
tokens. Resultado vazio ou estruturalmente inválido conserva no breakdown qualquer usage válido
reportado, mesmo sem poder preencher o trio aceito.

`llm_usage_total` soma, por campo, toda usage válida reportada nas tentativas primárias e no
fallback, inclusive respostas vazias/inválidas; usage válido é mapping com inteiros não negativos,
excluindo boolean. A persistência é comprovada por eventos correlacionados: `message.in` recebe o
trio aceito, `llm_attempts` e `llm_usage_total`; `prompt.generated` recebe
`suggested_response`. `src/services/trace_service.py:434-440` e
`src/api/routes/builder.py:455-463` usam
`llm_usage_total` para o total e `llm_attempts` para breakdown por modelo quando presentes, sem
somá-los de novo; eventos antigos recaem em `llm_usage` + `llm_model`. O transporte `LLMClient`
continua single-endpoint e não passa a conhecer cadeias ou credenciais alternativas.

Para áudio, nenhuma segunda implementação será criada. O caminho existente continua chamando o
serviço especializado `transcribe_audio_multimodal`, porque ele precisa de base64, `input_audio`,
normalização MIME e validação por formato (`src/services/multimodal_service.py:1-284`). A unidade é
de produto e credencial; os dois payloads permanecem isolados.

O predicado operacional continua conservador. Como uma única configuração é prometida para texto
e áudio WhatsApp, ela só fica ativa quando o provider cobre ambas as modalidades. A evidência atual
mantém apenas OpenRouter em `OPERATIONAL_MULTIMODAL_PROVIDERS`
(`src/services/api_credentials.py:35-45`). Groq e OpenAI podem aceitar texto, mas não podem ser
apresentados como contingência completa enquanto não cobrirem o OGG/Opus recebido do WhatsApp.

O título final na UI será exatamente `FallBack LLM Multimodal`. A descrição em português dirá que
ele responde quando o LLM principal falha e interpreta o áudio quando o STT falha; a versão inglesa
expressará o mesmo contrato. Os ids de credencial `multimodal_*` e o endpoint PATCH não mudam; o
formato ganha somente o booleano de consentimento `multimodal_text_fallback_enabled`, exposto sem
segredo no status/GET para que a UI não o reative por engano.
Essa decisão substitui explicitamente a Decisão 4 de `api-config-clarity`, que fixou `API STT Fall
Back` a partir do escopo antigo; a execução coordena a alteração para não preservar documentação ou
testes com a semântica superada.

**Alternativas descartadas.** Colocar o fallback dentro de `LLMClient.generate` perdeu porque
mistura transporte com seleção de credenciais e dificulta atribuir usage/modelo ao degrau correto.
Chamar `transcribe_audio_multimodal` com texto perdeu porque esse serviço codifica um contrato de
transcrição e validação de formato que não existe no caminho text-only. Criar uma segunda chave
perdeu porque o pedido define justamente um único fallback multimodal para as duas situações; o
booleano novo é o estado mínimo exigido para consentimento fail-closed. Repetir a chamada multimodal
perdeu por custo e latência sem evidência de necessidade.

### Design for Isolation

- `LLMClient` mantém um propósito: transportar uma chamada OpenAI-compatible para um endpoint,
  usando o egress kind pinado que a factory multimodal validou sem selecionar fallback.
- `EventService` seleciona atomicamente o trio do resultado aceito e contabiliza todas as tentativas
  em `llm_attempts`/`llm_usage_total` separados.
- `multimodal_service` mantém um propósito: transformar bytes de áudio em transcrição.
- A factory/política multimodal define, sem banco ou HTTP, se o bundle é completo, o provider é
  operacional e provider↔host↔egress kind são coerentes, sem criar política por modelo.
- O card apresenta e persiste o consentimento textual; o `EventService` aplica o gate em runtime.

## Simplicity Case

- **Simplest complete design:** um loader privado do `EventService` carrega bundles separados sem
  ampliar o helper de RLHF; com consentimento textual literalmente `true`, constrói um segundo
  `LLMClient` e o chama uma vez quando a geração principal não produzir conteúdo utilizável.
- **Added machinery:** uma factory/predicado puro para validar provider↔host, resolver
  `multimodal_openrouter` por `multimodal_kind_for_url` (ou helper puro equivalente) e passá-lo por
  uma extensão estreita do transporte, além de um ramo de fallback no ponto de geração. Ambos são
  pagos pelos requisitos presentes de cobrir falhas de texto sem enfraquecer safe-egress; nenhuma
  coordenação adicional é adicionada além do opt-in exigido.
- **Deferred until measured:** circuit breaker, backoff, múltiplos fallbacks e credenciais separadas
  só serão reconsiderados quando métricas demonstrarem falhas repetidas, necessidade de um terceiro
  provedor ou demanda real por provedores diferentes entre texto e áudio.
- **Complexity removed:** nenhuma tabela, cache, fila, máquina de estados, retry adicional,
  migração de banco ou adaptação do payload de áudio; o único estado novo é o booleano de
  consentimento exigido para não ampliar egress silenciosamente.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | `multimodal_*` representa uma única capacidade de contingência para texto e áudio, mas texto exige opt-in | Reflete a intenção do produto sem reutilizar silenciosamente o consentimento anterior de áudio |
| 2 | Texto segue LLM principal -> LLM multimodal; áudio continua STT -> LLM multimodal -> recondução | Mantém o provedor especializado como primeira opção e usa o multimodal como rede de segurança comum |
| 3 | A seleção do fallback fica no `EventService`, não no `LLMClient` | O serviço já possui credenciais, prompt, histórico, usage e modelo; o cliente permanece transporte isolado |
| 4 | Text-only reutiliza `LLMClient`; áudio continua no `multimodal_service` | Os endpoints são compatíveis, mas os payloads e validações são diferentes |
| 5 | Conteúdo primário inutilizável usa até duas tentativas; exceção/JSON malformado vai direto ao fallback | Preserva a política atual em vez de criar retry novo |
| 6 | O multimodal é chamado no máximo uma vez por geração | Limita latência/custo e impede loops sem exigir circuit breaker |
| 7 | Apenas bundle completo em provider operacional pode ser chamado | Preserva a elegibilidade/readiness existente sem impor restrição nova por modelo |
| 8 | O trio legado pertence ao resultado aceito; `llm_attempts` e `llm_usage_total` cobrem toda usage válida reportada | Preserva compatibilidade de atribuição sem subcontabilizar respostas vazias/inválidas |
| 9 | Falha dupla preserva a degradação final atual | Handoff ou mensagem fixa seriam novas decisões de produto fora do pedido |
| 10 | O título exato é `FallBack LLM Multimodal`, sem renomear ids `multimodal_*` | Corrige a comunicação sem migração de wire/schema |
| 11 | Esta decisão substitui o enquadramento apenas-STT de `api-config-clarity` | O requisito atual é posterior e explicitamente amplia o contrato |
| 12 | O fallback text-only valida `multimodal_provider`↔host e passa `multimodal_openrouter` explicitamente ao transporte | Reutilizar a inferência normal de `LLMClient` classificaria OpenRouter como `llm_outros` e perderia o pin de host |
| 13 | Texto exige `multimodal_text_fallback_enabled is True`; legado/ausente permanece somente áudio | Prompt e histórico são novo egress de dados e não podem herdar consentimento dado ao STT |
| 14 | Resposta utilizável é mapping com `content: str` não vazio após `strip` | Torna vazio, whitespace e shapes/tipos inválidos determinísticos e testáveis |
| 15 | `_load_runtime_llm_credentials` continua com exatamente quatro campos; um loader privado atende apenas o `EventService` | Evita ampliar acidentalmente o fluxo RLHF e limita segredos por capacidade |
| 16 | Resultado multimodal inutilizável falha depois de sua única tentativa, mas conserva usage reportado em `llm_attempts`/total | Separa resultado aceito de custo observado |
| 17 | Nenhuma allowlist provider→model ou mudança de `credential_status`/readiness é introduzida | Evita quebrar o caminho de áudio existente fora do requisito |

## Deliverables

- Aplicar o predicado e roteamento no `EventService`, selecionando o resultado aceito e acumulando
  `llm_attempts`/`llm_usage_total` em memória para persistência correlacionada.
- Criar a factory text-only safe-egress em `src/llm/client.py`, preservando a elegibilidade existente
  por provider e validando provider↔host↔kind antes de anexar a chave.
- Adaptar trace e stats do builder para preferir total/breakdown novos e recair no contrato legado;
  cobrir shapes, usage reportado, opt-in, segredos, áudio e UI nos testes listados pela wish.

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | O multimodal pode falhar junto com o LLM principal | Medium | Uma única tentativa, logs/métricas por degrau e preservação da degradação atual |
| 2 | Outro modelo pode responder semanticamente diferente | Medium | Enviar as mesmas mensagens, histórico, limites e contexto; policy e handoff não são reexecutados |
| 3 | Usage de primário vazio/retry ou fallback inválido pode desaparecer | High | Preservar o trio aceito e persistir breakdown/total separados de toda usage válida reportada; consumidores não somam ambos |
| 4 | Credenciais multimodais podem vazar pela projeção cacheada ou pelo loader usado no RLHF | High | Loader EventService-only com allowlist por capability; helper RLHF segue exatamente quatro campos; manter testes de fronteira |
| 5 | `api-config-clarity` pode restaurar o nome apenas-STT | Medium | Registrar a substituição, coordenar dependência e validar texto final por teste de conteúdo |
| 6 | OpenAI/Groq funcionam em texto mas não no áudio WhatsApp | Medium | Manter readiness completo restrito a providers medidos nas duas modalidades; hoje, OpenRouter |
| 7 | Assume-se que falha dupla pode manter o comportamento final atual | Low | Está explicitamente OUT criar handoff/mensagem; reconsiderar em wish próprio se produto exigir |
| 8 | O fallback de texto pode herdar `llm_outros` e aceitar host custom | High | Resolver o kind com `multimodal_kind_for_url` ou equivalente, exigir `multimodal_openrouter`, validar provider↔host e testar rejeição fail-closed |
| 9 | Clínicas legadas podem enviar prompt/histórico sem terem consentido com esse novo uso | High | Booleano opt-in estrito, ausente/false por padrão; toggle desmarcado com cópia PT/EN e nenhum auto-enable ao salvar |
| 10 | Shape inválido ou whitespace pode ser aceito e impedir o fallback | High | Predicado único mapping + `content: str` + `strip`, aplicado a ambos os degraus e coberto por matriz de tipos |
| 11 | A WISH gerada antes desta correção pode manter o contrato revisado antigo | High | Manter a evidência do design em PENDING e regenerar a WISH somente após novo review SHIP; não executar o plano stale |

## Success Criteria

- [ ] Com opt-in e bundle válido, falha HTTP, de transporte ou de safe-egress do LLM principal chama
      o multimodal exatamente uma vez e devolve a resposta utilizável dele.
- [ ] Com opt-in e bundle válido, `None`, whitespace, non-string e shape inválido do principal
      percorrem até duas tentativas e chamam o multimodal uma vez; exceção ou JSON malformado
      chama-o diretamente.
- [ ] Resposta utilizável do principal nunca chama o multimodal.
- [ ] Resposta utilizável é somente mapping com `content` string não vazia após `strip`; retorno
      não-mapping, campo ausente, `None`, não-string e whitespace percorrem no máximo duas
      tentativas primárias e então uma chamada de fallback.
- [ ] Fallback com qualquer shape/tipo acima inválido, whitespace ou exceção é chamado exatamente
      uma vez, não produz resposta aceita e preserva a degradação final atual.
- [ ] Sem bundle multimodal completo e operacional, o fluxo de texto preserva o comportamento atual
      e não tenta endpoint alternativo.
- [ ] Config legado/ausente, `false`, `null` ou valor não booleano de
      `multimodal_text_fallback_enabled` nunca envia prompt/histórico ao multimodal; somente `true`
      explícito habilita texto, e o fluxo de áudio permanece idêntico nos dois estados.
- [ ] `multimodal_provider=openrouter` só cria o cliente text-only quando o host resolve por
      `multimodal_kind_for_url` (ou helper puro equivalente) para `multimodal_openrouter`; mismatch,
      host custom ou qualquer resolução `llm_outros` falha fechado antes de enviar credenciais.
- [ ] A elegibilidade permanece bundle completo + provider operacional; nenhuma allowlist de modelo
      ou alteração de `credential_status`/readiness é introduzida.
- [ ] O transporte/factory recebe explicitamente o egress kind multimodal pinado, sem alterar a
      inferência dos clientes primários nem permitir downgrade para a política pública genérica.
- [ ] Falha dos dois provedores não cria loop, terceira chamada, handoff ou mutação extra de sessão.
- [ ] O fallback recebe as mesmas mensagens e `max_tokens` da tentativa primária.
- [ ] Resultado multimodal inutilizável falha após sua única tentativa; não preenche o trio aceito,
      mas seu usage válido reportado sobrevive em `llm_attempts` e `llm_usage_total`.
- [ ] O resultado aceito é selecionado atomicamente; `message.in` persiste o trio compatível,
      `llm_attempts` ordenado e `llm_usage_total`, enquanto `prompt.generated` persiste a resposta.
- [ ] Cada chamada iniciada gera exatamente uma entrada com tier, ordinal, provider/model,
      outcome, accepted e usage normalizado quando reportado; no máximo uma fica aceita.
- [ ] Primário vazio com usage + retry com usage + fallback com usage soma as três tentativas uma
      vez em `llm_usage_total`; trace e builder usam esse total e o breakdown sem double count, com
      fallback `llm_usage`/`llm_model` para eventos antigos.
- [ ] Usage ausente, não-mapping, booleano, negativo ou não inteiro é marcado unavailable e não gera
      tokens inventados; isso não rejeita conteúdo textual que satisfaça o predicado de resposta.
- [ ] Logs e métricas distinguem `primary` de `multimodal_fallback` sem chave, Authorization ou
      conteúdo de prompt/resposta.
- [ ] Os testes existentes do caminho STT -> multimodal -> recondução passam sem alteração de
      comportamento.
- [ ] O card usa o título exato `FallBack LLM Multimodal`; descrições PT/EN mencionam falha do LLM
      principal e falha do STT.
- [ ] Nenhum segredo `multimodal_*` aparece na projeção cacheada, no GET público ou em logs.
- [ ] `_load_runtime_llm_credentials` e seus testes continuam retornando exatamente os quatro campos
      `llm_*`; somente o loader privado do `EventService` pode obter o bundle multimodal DB-only.
- [ ] O toggle de opt-in inicia desmarcado, não é ativado ao salvar outros campos e explica em PT/EN
      o envio de prompt e histórico textual; GET/PATCH preservam o booleano sem expor segredos.
- [ ] Groq/OpenAI continuam indisponíveis como contingência completa enquanto não houver evidência
      de suporte ao áudio WhatsApp; OpenRouter permanece operacional para texto e áudio.

## Test Matrix

| Caso | Primário | Fallback | Prova esperada |
|------|----------|----------|----------------|
| `str` não vazio após `strip()` | aceita na primeira | 0 | trio aceito + 1 attempt + total |
| vazio com usage + retry vazio com usage + fallback utilizável com usage | 2 | 1 | 3 attempts ordenados; total soma 3 uma vez; só fallback aceito |
| `None`, whitespace, non-string ou shape inválido com usage | até 2 | 1 | não entra no trio aceito; usage reportado entra no breakdown/total |
| exceção HTTP/transporte/egress ou JSON malformado | 1 | 1 direto | attempt primary unavailable; sem tokens inventados |
| fallback inutilizável com usage | conforme causa | 1 | falha final; usage reportado preservado, nenhum accepted |
| provider/host/kind inválido ou provider não operacional | conforme causa | 0 | fail-closed antes da chave, sem regra por modelo |
| fallback utilizável | conforme causa | 1 | trio aceito e eventos de contabilização correlacionados |

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `7d1d4e31f10676a628b0b2c545f97602f06273c55c1e7eaef55477ba2f077b4e`
- **Reviewer:** genie:reviewer
- **Reviewed at:** 2026-08-29T22:08:25.423Z
<!-- genie-design-review:end -->
