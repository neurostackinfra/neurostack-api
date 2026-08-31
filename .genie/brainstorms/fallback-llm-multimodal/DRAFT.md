# Draft: FallBack LLM Multimodal

| Field | Value |
|-------|-------|
| **Slug** | `fallback-llm-multimodal` |
| **Date** | 2026-08-29 |
| **WRS** | 100/100 |

## Problem

O recurso multimodal está apresentado e implementado como fallback exclusivo do STT, embora a
intenção do produto seja que ele também assuma a geração de texto quando o LLM principal falhar.
Isso deixa respostas de texto sem uma segunda rota e torna o nome atual menor que o contrato real.

## Scope

### IN

- Renomear o recurso e o card para o título exato `FallBack LLM Multimodal`.
- Usar a configuração `multimodal_*` existente como fallback do LLM principal para requisições
  text-only somente com opt-in explícito, e como fallback do transcritor para áudio sem mudança.
- Considerar utilizável somente `content` que seja `str` com `strip()` não vazio. `None`, whitespace,
  non-string e shape inválido seguem as até duas tentativas primárias; exceção de
  transporte/HTTP/egress ou JSON malformado vai direto ao fallback.
- Preservar o caminho de áudio já entregue: STT -> multimodal -> recondução por texto.
- Manter resposta, `llm_usage` e `llm_model` como atribuição compatível do resultado aceito e
  registrar separadamente usage reportado por todas as tentativas iniciadas.
- Cobrir o contrato em testes unitários e de integração do EventService, sem chamadas reais.

### OUT

- Fallback para erros anteriores ou posteriores à geração do LLM (policy, SRM, CTX, prompt,
  persistência, envio WhatsApp ou TTS).
- Terceiro provedor, round-robin, balanceamento, circuit breaker, fila ou retry/backoff novo.
- Alterar o formato, limites ou a política de recondução do fallback de áudio existente.
- Usar dois conjuntos de credenciais multimodais distintos para texto e áudio.

## Decisions

1. `multimodal_*` é uma capacidade única de contingência, não uma configuração STT: com opt-in
   textual recebe o mesmo prompt do LLM principal quando este falha; para áudio continua recebendo
   `input_audio` quando o STT falha, sem consultar o novo gate.
2. A ordem de texto é LLM principal -> LLM multimodal, sem retorno ao principal e sem terceiro
   degrau. A ordem de áudio continua STT -> LLM multimodal -> recondução por texto.
3. O fallback de texto entra no limite do EventService, onde já existem prompt, histórico,
   credenciais cruas, modelo efetivo e atribuição de usage; `LLMClient` continua sendo transporte de um
   único endpoint e não passa a conhecer credenciais de fallback.
4. Para text-only, o fallback reutiliza `LLMClient` com `multimodal_api_url`,
   `multimodal_api_key` e `multimodal_model_name` por uma factory estreita. Antes de anexar a chave,
   ela exige coerência `multimodal_provider`↔host, reutiliza `multimodal_kind_for_url` ou helper puro
   equivalente e, para o provider hoje operacional, só aceita o kind pinado
   `multimodal_openrouter`. O serviço de transcrição multimodal permanece especializado em áudio.
5. Só uma configuração multimodal completa em provider operacional pode ser usada. A elegibilidade
   existente por provider é preservada; não se adiciona allowlist de modelo nem mudança em
   `credential_status`/readiness.
6. Resultado primário inutilizável (`None`, whitespace, non-string ou shape inválido) só aciona o
   fallback depois das até duas tentativas existentes. Exceção ou JSON malformado aciona o fallback
   imediatamente. Resultado multimodal inutilizável falha depois de sua única tentativa.
7. Falha do fallback é observada e preserva a degradação final já existente; não cria mensagem fixa,
   handoff automático nem novo estado de sessão.
8. O título exato é `FallBack LLM Multimodal`; descrições PT/EN explicam as duas funções sem mudar
   os ids de wire `multimodal_*`.
9. Esta decisão substitui a Decisão 4 de `api-config-clarity`, que restringia o multimodal a STT. A
   execução deve coordenar o texto final do card para não introduzir uma renomeação intermediária
   contraditória.
10. O transporte/factory recebe o egress kind multimodal pinado por uma extensão explícita,
    isolada e tipada. `_llm_kind_for_url` continua atendendo os clientes primários, mas não pode
    classificar este fallback como `llm_outros`; mismatch de provider/host, host custom e kind
    desconhecido falham fechado, sem downgrade de política.
11. Configuração legada foi consentida somente para áudio. Texto exige o novo booleano
    `multimodal_text_fallback_enabled` literalmente `true`; ausente, `false`, `null` ou tipo
    diferente falha fechado. O toggle nasce desmarcado, explica em PT/EN que prompt e histórico
    serão enviados ao provedor e nunca é ativado ao salvar outros campos. Áudio ignora esse gate.
12. `_load_runtime_llm_credentials` continua retornando exatamente os quatro campos `llm_*` usados
    também pelo RLHF. Um loader novo, privado e capability-scoped do `EventService` carrega, DB-only,
    bundles separados de primário e fallback; segredos não entram em cache ou serialização.
13. Resposta utilizável é mapping com `content: str` não vazio depois de `strip`, e o valor aceito
    é o texto stripped. Shape não-mapping, campo ausente, `None`, não-string ou whitespace é
    inutilizável: usa no máximo as duas tentativas primárias atuais; no fallback, uma única chamada
    inválida degrada para o comportamento final atual.
14. Resposta, `llm_usage` e `llm_model` do resultado utilizável aceito são selecionados atomicamente
    e preservam a atribuição legada. Separadamente, `llm_attempts` registra em ordem toda chamada
    iniciada com tier, ordinal, provider/model, outcome, accepted e usage normalizado reportado;
    `llm_usage_total` soma toda usage válida das tentativas primárias e fallback, mesmo vazias ou
    inválidas. Usage válido é mapping com inteiros não negativos, excluindo boolean; exceção,
    timeout ou usage ausente/inválido fica unavailable, sem tokens inventados.
15. `message.in` persiste o trio aceito, `llm_attempts` e `llm_usage_total`; `prompt.generated`
    persiste `suggested_response`. Trace e stats do builder preferem total/breakdown novos sem
    double count e recaem em `llm_usage` + `llm_model` para eventos antigos.

## Simplicity Case

O desenho mínimo completo é um loader privado do EventService carregar os dois bundles sem ampliar
o helper de RLHF e, somente com opt-in textual, construir um segundo `LLMClient` por uma factory pura
que valida provider↔host, resolve `multimodal_openrouter` por `multimodal_kind_for_url` (ou helper
puro equivalente) e passa esse egress kind pinado ao transporte. Ele é chamado uma vez quando a
chamada principal não produzir conteúdo utilizável. Não há nova tabela, cache, fila, retry ou
abstração de múltiplos provedores; o único estado novo é o booleano de consentimento fail-closed. O
adaptador de áudio continua separado porque seu payload e sua validação de formato são específicos.

Circuit breaker, backoff e múltiplos fallbacks ficam adiados até métricas mostrarem falhas repetidas
ou necessidade de um terceiro provedor. Configurações separadas para fallback de texto e áudio só
entram se houver demanda comprovada por provedores diferentes nas duas modalidades.

## Risks

- O provedor multimodal pode falhar junto com o principal; mitigação: uma única tentativa, logs e
  métricas por degrau, preservando a degradação atual.
- Reexecutar o prompt em outro modelo pode produzir resposta semanticamente diferente; mitigação:
  enviar exatamente as mesmas mensagens, limites e contexto e manter policy/handoff fora do
  fallback.
- Usage de primário vazio/retry ou fallback inválido pode desaparecer; mitigação: manter o trio do
  resultado aceito e persistir breakdown/total separados de toda usage válida reportada, sem dupla
  contagem nos consumidores.
- A wish `api-config-clarity` contém o nome antigo `API STT Fall Back`; mitigação: declarar a
  substituição e validar que o texto final da UI não restringe o recurso a STT.
- Credenciais multimodais cruas não podem entrar no cache/projeção pública nem no loader usado por
  RLHF; mitigação: loader EventService-only com allowlist e helper compartilhado fixado em quatro
  campos.
- OpenRouter seria classificado pelo `LLMClient` atual como `llm_outros`, permitindo egress público
  genérico; mitigação: validar `multimodal_provider`↔host, exigir `multimodal_openrouter` via helper
  multimodal e passá-lo explicitamente pela extensão isolada do transporte, sem host custom ou
  downgrade.
- Configuração legada pode enviar texto sem consentimento; mitigação: booleano opt-in estrito,
  ausente/false por padrão, com toggle desmarcado e cópia PT/EN.
- Shape inválido ou whitespace pode ser tratado como sucesso; mitigação: predicado único de
  mapping + `content: str` + `strip`, testado nos dois degraus.
- A WISH gerada antes desta correção pode estar stale; mitigação: manter review PENDING e regenerar
  a WISH somente após novo SHIP, sem executar o plano anterior.

## Success Criteria

- [ ] Com opt-in e bundle válido, falha HTTP/transporte do LLM principal chama o multimodal uma vez
      e devolve sua resposta utilizável.
- [ ] `None`, whitespace, non-string e shape inválido seguem até duas tentativas; exceção/JSON
      malformado vai direto ao fallback e o fallback inutilizável falha após uma tentativa.
- [ ] Sucesso do principal nunca chama o multimodal.
- [ ] Somente mapping com `content: str` não vazio após `strip` é utilizável; shape não-mapping,
      ausência, `None`, não-string e whitespace usam no máximo duas tentativas primárias e depois
      exatamente uma chamada de fallback.
- [ ] Fallback inválido, vazio ou com exceção é chamado uma vez, não marca resultado aceito e não
      cria terceiro degrau.
- [ ] Sem credenciais multimodais operacionais, o fluxo de texto preserva o comportamento atual.
- [ ] Ausência, `false`, `null` ou tipo não booleano do opt-in nunca envia prompt/histórico; apenas
      `true` explícito habilita texto, sem mudar o áudio.
- [ ] `multimodal_provider=openrouter` só aceita host que resolva para `multimodal_openrouter` por
      `multimodal_kind_for_url` ou helper puro equivalente; mismatch, host custom, kind desconhecido
      e `llm_outros` falham fechado antes do envio de credenciais.
- [ ] Elegibilidade permanece bundle completo + provider operacional; não há allowlist de modelo
      nem mudança de `credential_status`/readiness.
- [ ] A factory/transporte recebe explicitamente o egress kind multimodal pinado sem enfraquecer a
      política dos clientes primários.
- [ ] Falha dos dois provedores não cria loop, terceira chamada, handoff ou mutação extra de sessão.
- [ ] O fallback recebe as mesmas mensagens e limites da tentativa primária.
- [ ] O trio legado permanece atribuído somente ao resultado aceito; `llm_attempts` registra cada
      chamada iniciada e `llm_usage_total` soma toda usage válida reportada, inclusive de resultados
      vazios/inválidos.
- [ ] Primário vazio com usage + retry com usage + fallback com usage gera três attempts ordenados e
      soma os três exatamente uma vez; trace/builder não duplicam o total e preservam eventos antigos.
- [ ] Exceção, timeout ou usage ausente/inválido fica unavailable, sem inventar tokens.
- [ ] O caminho de áudio continua passando pelos testes STT -> multimodal -> recondução existentes.
- [ ] O card e as descrições PT/EN usam `FallBack LLM Multimodal` e explicam fallback de LLM de
      texto e de transcrição de áudio.
- [ ] Nenhum campo `multimodal_*` secreto passa a aparecer na projeção cacheada ou no GET público.
- [ ] `_load_runtime_llm_credentials` continua com exatamente quatro campos; apenas o loader privado
      do EventService obtém o bundle multimodal DB-only.
- [ ] Toggle opt-in inicia desmarcado, não autoativa ao salvar e explica PT/EN o envio textual;
      GET/PATCH preservam o booleano sem expor segredos.

## Deliverables

- Predicado e roteamento no `EventService`, com trio aceito e `llm_attempts`/`llm_usage_total`
  separados em persistência correlacionada.
- Factory safe-egress com provider operacional e provider↔host↔kind, sem política por modelo.
- Trace/builder compatíveis com eventos novos e legados; cobertura de usage reportado, shapes,
  opt-in, áudio, segredos e contrato visual.

## Acceptance Criteria

- [ ] Todo caso da matriz abaixo está coberto sem enfraquecer testes existentes.
- [ ] Nenhuma chave é anexada antes de provider, host e egress kind serem validados.
- [ ] O limite continua sendo até duas tentativas primárias por conteúdo inutilizável e exatamente
      uma tentativa multimodal.

## Test Matrix

| Entrada/causa | Primário | Multimodal | Prova |
|---------------|----------|------------|-------|
| `str` utilizável | aceita na primeira | não chama | trio aceito + 1 attempt + total |
| vazio com usage + retry com usage + fallback utilizável com usage | 2 | chama 1 | 3 attempts; total soma 3 uma vez; só fallback aceito |
| `None`, whitespace, non-string ou shape inválido com usage | até 2 | chama 1 | usage reportado no breakdown/total, fora do trio aceito |
| exceção ou JSON malformado | 1 | chama 1 direto | attempt primary unavailable; sem tokens inventados |
| conteúdo multimodal inutilizável com usage | conforme causa | 1 | falha final; usage preservado; nenhum accepted |
| provider/host/kind inválido ou provider não operacional | conforme causa | 0 | fail-closed antes da chave, sem regra por modelo |
| resposta fallback utilizável | conforme causa | 1 | trio aceito + eventos de contabilização correlacionados |

## WRS

WRS: ██████████ 100/100
Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅
