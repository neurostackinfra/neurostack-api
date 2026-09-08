# Wish: Modalidade online/presencial por serviço

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `clinic-service-modality` |
| **Date** | 2026-09-08 |
| **Author** | Codex |
| **Appetite** | medium |
| **Branch** | `wish/clinic-service-modality` |
| **Repos touched** | `neurostack-api` (backend + frontend in-repo) |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Cada item nomeado (serviço ou consulta) que for salvo pela nova tela administrativa passará a registrar se é entregue online, presencialmente ou nas duas modalidades. O servidor continuará aceitando escritores e registros legados sem esse campo. A configuração geral da clínica continuará descrevendo a capacidade global, enquanto uma modalidade explícita do item será a fonte específica usada pela IA ao responder sobre aquele serviço, sem inventar uma opção que não foi confirmada.

## Problem Statement

Ao perguntar por um serviço específico, o paciente deve receber somente as modalidades confirmadas para esse item, mesmo quando a clínica, no conjunto, atende online e presencialmente.

## Scope

### IN

- Acrescentar `service_format` a cada objeto de `services`, com os tokens canônicos já usados pela tela: `online`, `presential` e `both`; consultas cadastradas no mesmo catálogo seguem o mesmo contrato.
- Declarar no PATCH `/v1/config` um DTO aditivo e nullable para `services`: itens objeto validam os três tokens, itens string e modalidade ausente/`null` permanecem compatíveis, chaves extras são preservadas e `services: null` explícito conserva o contrato atual descrito abaixo.
- Validar, antes de qualquer mutação/commit, que uma modalidade explícita do serviço cabe na capacidade global canônica da clínica: `online` aceita somente `online`, `presential` aceita somente `presential` e `both` aceita as três opções.
- Manter compatibilidade com serviços legados sem `service_format`, entradas string legadas e formatos globais históricos não canônicos; nenhum deles será reescrito ou apagado implicitamente.
- Adicionar um seletor bilíngue de modalidade em cada card da aba **Serviços**, inclusive para consultas, com os três rótulos visíveis: Online, Presencial e Online e Presencial.
- Exigir no frontend que todo serviço com nome tenha sua modalidade confirmada antes do save, sem preencher automaticamente registros antigos cuja clínica está configurada como `both`.
- Hidratar, editar, salvar e recarregar a modalidade dentro do array completo de `services`, preservando nome, descrição, preço, diferenciais de autoridade e quaisquer chaves extras recebidas. Na hidratação da nova UI, uma entrada string legada vira apenas uma representação visível em memória (objeto com o texto como nome e modalidade pendente); o valor bruto no banco permanece string até a pessoa escolher explicitamente uma modalidade e salvar.
- Injetar no detalhe de cada serviço do prompt uma modalidade legível em pt-BR e uma regra explícita de precedência para a IA: a modalidade do serviço vence o formato geral da clínica.
- Para serviço legado sem modalidade explícita, usar o formato global somente quando ele for inequivocamente `online` ou `presential`; com `both`, vazio ou valor histórico desconhecido, não atribuir modalidade ao item.
- Preservar os controles e dados globais `service_format`, `online_services_desc` e `presential_services_desc`; eles não substituem a confirmação específica do serviço e não serão removidos nesta wish.
- Documentar o shape aditivo de `services[].service_format`, sua precedência e o comportamento legado no contrato/configuração pública e no exemplo JSON existente; fixar o enum também no OpenAPI do PATCH.
- Cobrir o round-trip da API, rejeições atômicas, prompt, clonagem de clínica e interação/read-only do formulário com testes automatizados.

### OUT

- Criar tabela, entidade ou identificador estável para serviços; o catálogo continua sendo o array JSONB atual e o PATCH continua substituindo o array inteiro.
- Executar migration/backfill que tente adivinhar modalidades de serviços existentes a partir de textos livres ou de uma clínica global `both`.
- Remover, migrar ou reinterpretar os campos globais `service_format`, `online_services_desc` e `presential_services_desc`.
- Derivar automaticamente a modalidade global da clínica a partir do catálogo.
- Configurar modalidade por profissional, unidade/endereço, agenda, convênio, preço ou faixa de horário.
- Criar link/sala de teleconsulta, disponibilidade de agenda ou confirmação automática de agendamento; a modalidade informa oferta, não autoriza a IA a concluir um agendamento.
- Alterar `atendimento_tipo`, que permanece reservado à escolha `convenio`/`particular` da conversa.
- Resolver concorrência de edição do array de serviços; o comportamento last-write-wins já existente permanece.
- Corrigir os débitos preexistentes de `ClinicConfigTable.to_pydantic()` e `TenantRepository.upsert_clinic_config()`, hoje sem round-trip dos buckets modernos; os caminhos desta entrega são `/v1/config`, o snapshot direto usado pelo pipeline e o deep copy do clone.
- Modernizar integralmente a documentação antiga de todos os buckets de `ClinicConfig`; a mudança documental fica limitada ao contrato de modalidade por serviço e ao contexto mínimo necessário para entendê-lo.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | A chave aninhada será `services[].service_format` com `online \| presential \| both` | Reutiliza exatamente os tokens já emitidos pelo seletor global e evita introduzir tradução, alias ou migração para um segundo vocabulário. `atendimento_tipo` não serve: ele representa convênio versus particular. |
| 2 | A modalidade explícita do serviço é a fonte da verdade para perguntas sobre esse serviço; a configuração global é apenas capacidade geral/fallback seguro | Uma clínica `both` pode ter teleconsultas, procedimentos apenas presenciais e serviços híbridos. A informação global sozinha não permite responder corretamente por item. |
| 3 | Ausência/`null` continua aceita pela API, mas a UI nova exige confirmação para todo serviço nomeado antes de salvar | Tornar o campo obrigatório no boundary quebraria registros persistidos e clientes antigos, pois o PATCH substitui o array inteiro. A exigência na superfície nova entrega o fluxo solicitado sem corte incompatível. |
| 4 | O fallback legado só infere modalidade quando o formato global canônico admite uma única resposta (`online` ou `presential`) | Inferir `both` para cada serviço de uma clínica híbrida inventaria capacidade. Formatos históricos como `hybrid` ou `presencial e online` continuam visíveis globalmente, mas não são reinterpretados silenciosamente por item. |
| 5 | O backend rejeita contradições explícitas antes de alterar ou adicionar a linha | Sem essa invariante, uma clínica `online` poderia salvar um serviço `presential`, deixando duas verdades incompatíveis no mesmo prompt. A checagem ocorre somente quando `services` ou o `service_format` global está presente no payload efetivamente enviado, usa o estado proposto mais a linha atual e, inclusive na primeira escrita, não cria novo estado antes de validar. |
| 6 | O DTO do PATCH aceita entradas objeto ou string e usa `extra="allow"`; o modelo persistido `ClinicConfig.services` continua flexível | O runtime já suporta strings legadas e testes de clonagem provam a existência de chaves extras como `details`. Tipar o modelo inteiro agora poderia descartar dados durante `model_dump`; validar somente o boundary alterado preserva a compatibilidade. |
| 7 | Nenhuma migration Alembic será criada | `services` já é JSONB; a nova chave é aditiva. Não há default correto para clínicas `both`, e uma migration não alcançaria de forma uniforme o fallback legado em `tenants.config["clinic_config"]`. |
| 8 | Os campos globais e seus textos livres permanecem declarados, roteados, clonados e editáveis | A solicitação adiciona precisão por serviço, não autoriza ocultar ou apagar dados antigos. A regra de prompt deixa claro que eles não sobrepõem uma modalidade específica. |
| 9 | A modalidade será incluída na linha de `services_detail` e acompanhada por instrução explícita no template | O pipeline já envia objetos e strings legadas como linhas de serviço. Estender esse mesmo canal para ambos é menor e mais auditável que criar RAG, endpoint ou contexto paralelo; o fallback global conservador também se aplica às entradas string. |
| 10 | A tela usará o helper bilíngue `txt(...)` já existente em `clinic-config.tsx` | Evita uma nova superfície de i18n para três strings locais e, nesta árvore, também evita colidir com alterações não relacionadas já presentes em `locales/pt.ts` e `locales/en.ts`. |
| 11 | Clones preservam a modalidade pelo deep copy atual; somente a prova de regressão será adicionada | Não é necessário alterar `clinic_clone_service.py`: o array completo já é copiado profundamente. Um teste com `service_format` torna esse contrato explícito. |
| 12 | O contrato público e o exemplo JSON serão atualizados, sem gerar novo cliente frontend | O PATCH passará a expor o enum no OpenAPI, mas o cliente gerado existente é apenas um placeholder e a tela usa `fetch` direto. Documentar o campo evita que outros escritores tratem o valor como string livre sem expandir o escopo para geração de SDK. |

## Simplicity Case

- **Simplest complete design:** uma chave opcional no objeto JSON de serviço, um `Select` no card existente, validação aditiva no PATCH e a modalidade anexada à linha de contexto que o prompt já consome. Não há endpoint, tabela, coluna, cache ou fluxo de agendamento novo.
- **Added machinery:** (1) um DTO aninhado com `extra="allow"`, exigido para rejeitar tokens inválidos sem apagar extensões existentes; (2) uma checagem de subconjunto contra a modalidade global, exigida para impedir configurações contraditórias; (3) validação local do formulário, exigida para que o usuário confirme cada serviço sem quebrar clientes legados no servidor.
- **Deferred until measured:** IDs de serviço e controle de concorrência somente se houver colisões reais de edição; modalidade por profissional/unidade somente quando uma clínica precisar do mesmo serviço em formatos diferentes por executor/local; normalização dos valores globais históricos somente após inventário dos valores de produção; enforcement obrigatório no servidor somente quando não houver mais escritores legados nem itens sem modalidade.
- **Complexity removed:** nenhum backfill heurístico, alias novo, tabela relacional, job, versionamento de cache, estado de rollout ou derivação automática do formato global será introduzido.

### Writer inventory and adoption trigger

- Escritores in-repo inventariados: a nova tela administrativa grava o array inteiro via `PATCH /v1/config`; `TenantRepository.upsert_clinic_config()` é um caminho legado de criação/atualização (com os débitos de round-trip já declarados em OUT); os fluxos de admin/builder inicializam catálogos vazios; e a clonagem copia profundamente o array existente. Nenhum outro escritor in-repo encontrado deve passar a exigir modalidade nesta wish.
- Assume-se que podem existir escritores externos ainda não inventariados. Por isso o boundary permanece aditivo e aceita ausência, `null` e string. Tornar `service_format` obrigatório no servidor ou normalizar registros automaticamente exige novo inventário de consumidores/escritores, evidência de adoção e um plano explícito de migração; esse é o gatilho de adoção, não parte desta entrega.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [ ] PATCH `/v1/config` aceita e devolve, sem perda, serviços com `service_format` igual a `online`, `presential` e `both`.
- [ ] O PATCH preserva chaves extras aninhadas de um serviço e continua aceitando, devolvendo e persistindo sem alteração estrutural um serviço legado sem modalidade, um objeto com `service_format: null` e uma entrada representada diretamente como string; a string só se torna objeto quando a pessoa confirma uma modalidade na nova UI e salva o array inteiro.
- [ ] `services` permanece nullable no PATCH: chave omitida preserva o catálogo armazenado; `services: []` persiste lista vazia; `services: null` dispara a checagem cruzada com catálogo efetivo vazio, persiste `NULL`/valor nullable como hoje e tanto o response flat do PATCH quanto o GET subsequente expõem `services: []`.
- [ ] Token de modalidade diferente dos três valores canônicos retorna 422 e não altera o catálogo persistido nem invalida o cache.
- [ ] Com formato global canônico `online`, qualquer serviço explicitamente `presential` ou `both` é rejeitado; com global `presential`, `online` ou `both` é rejeitado; com global `both`, os três são aceitos.
- [ ] A checagem usa o estado efetivo: mudar apenas o formato global para um valor incompatível com serviços já explícitos também retorna 422 antes do commit.
- [ ] A checagem cruzada roda somente quando `services` ou o `service_format` global consta em `body.model_dump(exclude_unset=True)`; PATCH não relacionado sobre uma linha historicamente contraditória conserva o comportamento atual e não é bloqueado.
- [ ] Formato global vazio ou histórico não canônico não causa reescrita nem rejeição de serviços; uma modalidade específica válida ainda pode ser salva.
- [ ] Cada card de serviço nomeado exibe um seletor **Modalidade** com Online, Presencial e Online e Presencial, tanto para serviços quanto para itens denominados consulta/avaliação.
- [ ] Um serviço novo começa sem modalidade selecionada; tentar salvar qualquer serviço nomeado sem confirmação mantém a tela editável, não envia PATCH e identifica o card que precisa de correção.
- [ ] Os três valores hidratam após GET, entram no PATCH dentro do item correto e reaparecem após recarregar a configuração.
- [ ] Alterar a modalidade global para uma opção mais restrita sinaliza os serviços incompatíveis e bloqueia o save sem apagar ou trocar escolhas silenciosamente.
- [ ] Quando a API retorna `detail.error = "service_format_conflict"`, a nova UI mostra uma mensagem fixa e segura em PT ou EN, sem ecoar nomes, descrições ou qualquer texto livre dos serviços; erros diferentes continuam no tratamento genérico.
- [ ] Em modo read-only, a modalidade de cada serviço é visível e o seletor fica desabilitado, seguindo os demais campos do card.
- [ ] Nome, descrição, preço, `authority_notes`, ordem dos serviços e chaves extras continuam intactos ao alterar somente a modalidade.
- [ ] Os controles globais atuais de formato e descrições online/presencial continuam presentes e seus dados sobrevivem a PATCHes que alterem serviços.
- [ ] O prompt renderiza `Modalidade: online`, `Modalidade: presencial` e `Modalidade: online e presencial` na linha do respectivo serviço.
- [ ] O prompt instrui que a modalidade específica prevalece sobre o formato geral e que a IA não deve oferecer um canal ausente do serviço.
- [ ] Serviço legado sem modalidade herda somente um global canônico de canal único; sob `both`, vazio ou valor histórico desconhecido, a linha do serviço não ganha uma modalidade inventada.
- [ ] Esse fallback de canal único vale igualmente para objetos sem modalidade e entradas string legadas; nenhuma representação persistida é normalizada pelo pipeline.
- [ ] Clonar uma clínica conserva e isola profundamente `services[].service_format` junto aos demais dados do item.
- [ ] A modalidade não altera regras de confirmação de agenda, `atendimento_tipo`, porcentagem de completude da configuração nem o contrato dos context cards de trace.
- [ ] O schema OpenAPI do PATCH `/v1/config` expõe `services[].service_format` com os três valores canônicos e a documentação/exemplo descrevem precedência e compatibilidade legada.
- [ ] Testes backend focados, suíte backend completa, testes frontend focados/completos, typecheck e build de produção passam.

## Execution Strategy

### Wave 1 (parallel)

Os dois grupos podem executar em paralelo porque o contrato `services[].service_format` está fechado neste documento e o JSONB atual tolera deploy em qualquer ordem: backend antigo já preserva a chave desconhecida; backend novo aceita sua ausência em clientes antigos.

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — validação de estado persistido (+2) | `engineer-standard` / high | Tipar o PATCH de serviços, impedir contradições e propagar modalidade/precedência ao prompt. |
| 2 | engineer | 2 — formulário com estado e validação (+2) | `engineer-standard` / high | Adicionar confirmação por serviço, preservar round-trip e cobrir edição/read-only. |

### Wave 2 (sequential gate)

Após os dois grupos, o orquestrador executa o gate agregado backend + frontend na mesma árvore, confirma que o diff não absorveu as alterações preexistentes de locale e envia o resultado para revisão independente de execução.

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** →
`engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high;
**4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an
independent `final-gate` at the highest justified effort. Codex maps these to
the `genie_*` profiles; other runtimes use their matching native roles. Keep
model and effort in runtime session/agent configuration, never skill frontmatter.

## Execution Groups

### Group 1: Contrato backend e contexto da IA

**Goal:** Persistir uma modalidade específica válida e fazer a IA recebê-la com precedência inequívoca sobre o formato geral.

**Deliverables:**
1. Criar em `src/api/schemas/config.py` um DTO de item de serviço usado pelo campo opcional `services` de `ConfigPatchRequest`, aceitando objetos extensíveis e strings legadas, validando somente `online|presential|both` quando `service_format` estiver presente.
2. Garantir que `model_dump(exclude_unset=True)` não materialize defaults nem remova chaves extras, e que o array validado continue seguindo o replace integral já existente em `unflatten_patch`.
3. Em `src/api/routes/config.py`, derivar primeiro `patch_data = body.model_dump(exclude_unset=True)` e executar a validação cruzada **somente** se a chave `services` ou a chave top-level `service_format` estiver presente nesse dicionário. Quando disparada, validar a combinação do formato global efetivo (valor do PATCH, inclusive `null`, ou `row.operations`) com os serviços efetivos: array do PATCH quando enviado, catálogo vazio quando `services: null` ou `[]`, e `row.services` quando omitido. Declarar `services` nullable no schema; `null` explícito deve persistir o valor nullable como hoje, mas `flatten_config`/`ConfigResponse` devem continuar expondo `services: []`. Cobrir services-only, global-only, ambos, `services=[]`, `services: null` explícito, global histórico e PATCH não relacionado sobre contradição já armazenada; o último não pode ser bloqueado e a omissão não pode apagar serviços.
4. Tornar a primeira escrita atômica: no `PATCH /v1/config`, usar `_load_config()` e construir `_new_config()` apenas como candidato atrasado (ou desenho equivalente que não o adicione à sessão), calcular o estado efetivo e validar antes de `session.add`, `flush`, mutação de bucket, commit ou invalidação de cache. Um primeiro PATCH conflitante deve retornar 422 e deixar zero linhas `ClinicConfigTable` para o tenant, zero commit e zero chamada de invalidação; adicionar teste explícito desse caso.
5. Para uma contradição entre tokens válidos, retornar exatamente `422` com o envelope seguro abaixo, ordenando `conflicts` por índice e sem ecoar nomes, descrições ou qualquer texto livre do serviço. Esse erro customizado é distinto do `422` padrão de FastAPI/Pydantic para token inválido, que conserva a lista `detail` de erros de validação do framework e também não toca persistência/cache:
   ```json
   {"detail":{"error":"service_format_conflict","message":"One or more service modalities are incompatible with the clinic service format.","clinic_service_format":"online","allowed_service_formats":["online"],"conflicts":[{"index":1,"service_format":"presential"}],"action":"Change the listed service modalities or broaden the clinic service format."}}
   ```
6. Em `src/core/pipeline.py`, resolver e renderizar o rótulo pt-BR da modalidade em cada linha de `services_detail`, aplicando o fallback legado conservador definido na Decisão 4 tanto a objetos sem modalidade quanto a entradas string, sem mudar a forma persistida.
7. Em `src/prompt/templates/normal.py`, tornar explícitas a precedência do item, a proibição de inventar canais e a separação entre informação de oferta e autorização de agendamento.
8. Atualizar testes de API/pipeline/prompt para cobrir round-trip, chaves extras, valores inválidos, atomicidade de linha existente e primeira escrita, matriz de compatibilidade/trigger completa, três rótulos, strings legadas, precedência e ausência de inferência sob `both`.
9. Atualizar a fixture/teste unitário de clonagem para provar que `service_format` é preservado e profundamente independente sem mudar a implementação do clone.
10. Atualizar `docs/configuration.md` e `docs/api-contracts.md` com um exemplo real e **flat** de request `PATCH /v1/config`, seu response `200` flat, semântica de substituição do array inteiro, o envelope customizado `422`, compatibilidade com chaves extras/string e uma matriz explícita global/serviço/fallback. Documentar também a distinção entre omissão, `services: []` e `services: null`, inclusive persistência nullable e normalização de `null` para `[]` no response/GET. O JSON aninhado de `docs/examples/tenant-config-with-clinic.json` permanece identificado apenas como exemplo de persistência, não como payload da API.
11. Criar `tests/docs/test_clinic_service_modality_docs.py` como contrato determinístico: conferir blocos/exemplos e matriz pública esperados, analisar o JSON de persistência com `json`, e afirmar no OpenAPI gerado que o request do PATCH referencia `services[].service_format` com enum exato `online|presential|both`. Não substituir isso por busca solta de tokens.

**Acceptance Criteria:**
- [ ] Passam com testes determinísticos as superfícies backend desta wish: round-trip/compatibilidade, nullable/omissão, token inválido, matriz e gatilho de consistência, primeira escrita atômica, prompt/fallback, clone, documentação e OpenAPI.
- [ ] PATCHes sem `services` e sem `service_format` mantêm exatamente o comportamento atual e serviços legados não são normalizados como efeito colateral.
- [ ] PATCH sem `services` **e** sem `service_format` não executa a checagem cruzada, mesmo se a linha armazenada já contradiz a regra; PATCH global-only e services-only executam a checagem contra a outra metade persistida.
- [ ] Há testes distintos provando: omissão preserva serviços e não dispara validação por si só; `services=[]` substitui por lista vazia; `services: null` dispara validação com catálogo efetivo vazio, persiste o valor nullable e retorna `services: []` no PATCH 200 e no GET/reload.
- [ ] O conflito customizado usa exatamente o envelope documentado, enquanto token inválido conserva o envelope FastAPI/Pydantic; ambos são provados separadamente.
- [ ] Um conflito na primeira escrita não adiciona nem faz flush de uma linha, não comita e não invalida cache.
- [ ] Entradas extras existentes, incluindo `details`, sobrevivem ao DTO e ao round-trip byte-equivalente em estrutura/valores.
- [ ] Nenhuma migration, alteração de tabela, cache novo ou mudança em `ClinicConfig.services` é introduzida.

**Validation:**
```bash
poetry run pytest tests/api/test_config.py tests/core/test_pipeline.py tests/srm/test_prompt_injection.py tests/services/test_clinic_clone_service.py tests/docs/test_clinic_service_modality_docs.py -v
python -m json.tool docs/examples/tenant-config-with-clinic.json >/dev/null
make quality
```

O primeiro comando localiza rapidamente falhas no boundary, primeira escrita, montagem do prompt, clonagem e contrato documental/OpenAPI; `json.tool` prova que o exemplo de persistência continua parseável. `make quality` é o gate documentado do repositório e é obrigatório porque este grupo alcança schema de configuração, persistência JSONB e o core compartilhado do prompt.

**depends-on:** none

---

### Group 2: Confirmação de modalidade no catálogo de serviços

**Goal:** Permitir que a pessoa confirme e revise a forma de entrega de cada serviço/consulta sem perder dados existentes.

**Deliverables:**
1. Estender `ClinicService` em `frontend/artifacts/neurostack/src/pages/clinic-config.tsx` com `service_format?: "online" | "presential" | "both" | null` e manter intactos os demais campos/chaves ao hidratar e atualizar um item. Normalizar somente no estado visível da UI cada entrada string legada para um objeto com `name` igual à string, os demais campos editáveis vazios e `service_format` pendente; não disparar escrita durante a hidratação.
2. Adicionar ao card existente um `Select` bilíngue, desabilitado em read-only, com placeholder de confirmação e seletores de teste estáveis por índice.
3. Criar serviços com modalidade vazia e validar antes do PATCH que todo item nomeado — inclusive o objeto visível originado de string legada — foi confirmado e que cada valor cabe no formato global canônico; mostrar erro associado ao card sem trocar valores automaticamente. Assim, a tela permite leitura/edição do legado, mas bloqueia save até confirmação explícita.
4. Serializar `service_format` dentro de cada item filtrado por nome e confirmar o round-trip dos três valores no GET/PATCH; não alterar o payload dos controles globais. Após a confirmação e o save explícito, uma antiga string torna-se intencionalmente objeto no array substituído e recarrega como objeto com a modalidade escolhida.
5. Atualizar `INVESTOR_MOCK_CONFIG` com exemplos que cubram online, presencial e ambos.
6. Mapear localmente o `detail.error === "service_format_conflict"` para copy bilíngue fixa e segura via `txt(...)`, sem renderizar conteúdo arbitrário do backend; manter o tratamento genérico dos demais erros. Testar a mensagem PT/EN e provar que nome/descrição maliciosos presentes no item não aparecem no toast.
7. Criar `clinic-config-service-modality.test.tsx`, reutilizando o harness de autenticação/fetch da página, para cobrir hidratação, inclusão/edição, payload, bloqueios, formatos globais, read-only e preservação dos campos vizinhos. Para string legada, cobrir separadamente: hidratação visível, seletor desabilitado em read-only, bloqueio sem PATCH antes da confirmação, PATCH que converte intencionalmente em objeto após escolha e reload do objeto/modalidade salvo.

**Acceptance Criteria:**
- [ ] Passam as superfícies frontend desta wish: seletor/modalidade nos cards, hidratação e confirmação, matriz global, bloqueios sem PATCH, read-only, round-trip/reload, preservação de dados e copy segura PT/EN para conflito.
- [ ] Nenhum PATCH é disparado quando existe serviço nomeado sem modalidade ou incompatível com a clínica.
- [ ] A validação identifica todos os cards inválidos sem apagar nome, descrição, preço ou notas.
- [ ] A normalização de string é apenas de apresentação até confirmação; GET/hidratação não escreve, save pendente bloqueia, e somente o save confirmado troca a representação persistida para objeto.
- [ ] O conflito customizado do backend produz mensagem frontend localizada e fixa, sem eco de texto livre do serviço.
- [ ] `locales/pt.ts` e `locales/en.ts` não entram no diff desta wish; as mudanças preexistentes nesses arquivos permanecem intocadas.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/clinic-config-service-modality.test.tsx
pnpm --dir frontend/artifacts/neurostack run test
pnpm --dir frontend/artifacts/neurostack run typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
```

O teste focado cobre o comportamento do formulário; a suíte do pacote protege a página compartilhada; typecheck cobre o novo union type e o build valida o artefato frontend entregue.

**depends-on:** none

---

## Aggregate Integration Gate

Executar após os dois grupos, na árvore combinada:

```bash
make quality
poetry run pytest tests/docs/test_clinic_service_modality_docs.py -v
pnpm --dir frontend/artifacts/neurostack run test
pnpm --dir frontend/artifacts/neurostack run typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
python -m json.tool docs/examples/tenant-config-with-clinic.json >/dev/null
git diff --check -- src/api/schemas/config.py src/api/routes/config.py src/core/pipeline.py src/prompt/templates/normal.py tests/api/test_config.py tests/core/test_pipeline.py tests/srm/test_prompt_injection.py tests/services/test_clinic_clone_service.py tests/docs/test_clinic_service_modality_docs.py frontend/artifacts/neurostack/src/pages/clinic-config.tsx frontend/artifacts/neurostack/src/pages/clinic-config-service-modality.test.tsx docs/configuration.md docs/api-contracts.md docs/examples/tenant-config-with-clinic.json
```

Este é um fluxo vertical que cruza frontend, schema de API, persistência e prompt compartilhado; por isso o gate agregado repete as suítes completas dos dois runtimes, typecheck/build e whitespace do slice. O release só está funcionalmente completo com os dois grupos, embora o contrato aditivo permita implantação técnica em qualquer ordem sem quebrar clientes antigos.

### Failure handling and escalation

- Falha em teste focado ou gate dos arquivos tocados é responsabilidade do respectivo grupo: manter o grupo em andamento, corrigir a menor causa raiz e repetir seu bloco completo de validação antes do gate agregado.
- Falha no gate completo que não toca o slice deve ser reproduzida na base limpa correspondente. Se também falhar na base, registrar comando, saída e comparação como dívida preexistente não relacionada; se passar na base, tratar como regressão desta wish e corrigir antes de seguir.
- Falha reproduzível de dependência, permissão, serviço externo, timeout ou ferramenta que impeça uma execução válida deve ser classificada como `env-tool-failure`, com comando/erro e tentativa de reparo ou retry registrados; não é aprovação implícita.
- Nenhum gate agregado pode ser omitido ou silenciosamente dispensado. Resultado diferente de sucesso precisa de evidência, classificação e rota corretiva; a entrega permanece não aprovada até execução válida ou decisão humana explícita sobre um bloqueio ambiental/preexistente.

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Configurar uma clínica `both` com três itens — teleconsulta online, procedimento presencial e acompanhamento em ambas as modalidades —, salvar, recarregar e confirmar os três valores.
- [ ] No simulador, perguntar separadamente pelos três itens e confirmar que a resposta oferece somente os canais registrados para cada um.
- [ ] Em uma clínica `online`, tentar marcar um serviço como presencial/ambos e confirmar bloqueio na UI e 422 equivalente pela API; repetir a matriz para clínica `presential`.
- [ ] Abrir configuração legada com serviço sem modalidade: o catálogo continua carregando, o seletor pede confirmação e nenhuma modalidade é inventada para global `both`.
- [ ] Abrir configuração com uma entrada string legada: confirmar nome visível em editável e read-only, ausência de escrita na hidratação, bloqueio de save até escolha, conversão intencional para objeto após confirmação e permanência da escolha no reload.
- [ ] Confirmar que um serviço legado sem modalidade sob global `online` ou `presential` mantém no prompt o fallback seguro de canal único até ser editado.
- [ ] Verificar como clinic admin e builder que a modalidade edita e persiste; verificar em papel read-only que o valor aparece, mas não pode ser alterado.
- [ ] Clonar uma clínica com as três modalidades e confirmar que o clone conserva os valores sem compartilhar mutações com a origem.
- [ ] Confirmar que preço, descrição, autoridade, controles globais, completude do cadastro, agendamento e triagem convênio/particular não sofreram regressão.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Registros antigos não têm modalidade e uma clínica `both` não permite inferência segura | Medium | Campo opcional no servidor, seletor pendente na UI e ausência explícita de inferência para `both`; nenhum backfill heurístico. |
| Formato global histórico pode usar strings fora do vocabulário atual | Medium | Aplicar a invariante/fallback somente aos três tokens canônicos; preservar e exibir valores históricos sem normalização implícita. |
| DTO aninhado pode descartar extensões de serviço já usadas por outros fluxos | High | `extra="allow"`, `exclude_unset=True` e teste de round-trip com `details` e campos vizinhos. |
| Modalidade global e específica podem divergir durante uma edição | High | Validar o estado efetivo completo antes de mutar a linha e espelhar a mesma matriz no frontend. |
| O LLM pode ignorar uma informação apenas descritiva | Medium | Rótulo na mesma linha do serviço, instrução explícita de precedência/proibição e QA conversacional por modalidade. |
| PATCH de serviços continua last-write-wins | Medium | Aceitar o comportamento atual nesta wish; reabrir para IDs/controle de versão quando houver evidência de colisões reais. |
| A árvore atual contém mudanças de locale e está numa branch de outro wish | Medium | Executar o trabalho em branch/worktree isolada baseada no `origin/main` mais recente e validar que os arquivos de locale não entram no diff. |
| Escritores externos podem continuar enviando serviços sem modalidade ou como string | Medium | Manter o boundary aditivo; só exigir no servidor após inventário/adopção comprovada e plano de migração, conforme o gatilho registrado em Writer inventory. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — 2026-09-08T11:57:08-03:00

- **Reviewer:** `/root/plan_review`
- **Reviewed SHA-256:** `665007db67932f39b0349943828b43a1ded1da31473d0db6775e5fc8d3557872`
- **Verdict:** FIX-FIRST
- **Structure:** 20 success criteria, 2 execution groups, 2 validation blocks and all dependency declarations present; manual structure gate passed. The repository has no root `package.json`/`wishes:lint` script, so the official lint command was unavailable (exit 2, no linter diagnostics).
- **HIGH gaps:** specify frontend normalization/confirmation for legacy string services; make consistency validation presence-triggered so unrelated PATCHes remain unblocked; reconcile first-write validation with `_get_or_create_config()` creating/flushing before validation; define the exact conflict 422 contract; document the real flat `/v1/config` whole-array PATCH rather than relying on the nested persistence example.
- **MEDIUM gaps:** narrow the mandatory-modality promise to the new admin UI while the server remains additive; replace token-only docs checks with a deterministic content contract and compatibility matrix; declare failure/escalation handling for focused and aggregate gates.
- **Advisory lenses:** Questioner and DX/docs both returned FIX-FIRST, agreeing that the additive JSONB/prompt approach is the simplest complete design and that the gaps above can be fixed without added infrastructure.

### Plan Re-review — 2026-09-08T12:11:37-03:00

- **Reviewer:** `/root/plan_review`
- **Reviewed SHA-256:** `ba60db75d1d15a7c2078f7e7670e8aa44d2c928c3065e429cf65846f72e2f968`
- **Context:** fix loop 1, plan review before work.
- **Verdict:** SHIP
- **Structure:** 24 success criteria, 2 independently deployable groups, 2 non-zero group validation blocks, wish/group dependency declarations, aggregate backend/frontend gate, QA criteria, risk register, bounded file list and no placeholders.
- **Prior-gap closure:** the plan now specifies the complete lifecycle for string services; presence-triggered consistency validation; omission/`[]`/`null`; delayed first-row creation and atomic failure; exact safe conflict 422 plus localized frontend copy; flat public PATCH/200/422 documentation; deterministic docs/OpenAPI contract checks; narrowed rollout promise; writer inventory/adoption trigger; and explicit failure escalation.
- **Validation availability:** backend and frontend gate commands exist. The official `wishes:lint` remains unavailable in this application repository because there is no root `package.json`/script; the command exited 2 without linter diagnostics, while manual structure and `git diff --check` passed.
- **Advisory lenses:** Questioner and DX/docs both returned SHIP with no remaining findings.
- **Gaps:** none.

### Execution Review — 2026-09-08T13:06:00-03:00

- **Groups:** both implementation groups completed and were marked done only after review and validation.
- **Backend review:** initial FIX-FIRST found incorrect prompt labels/fallback and insufficient deterministic contract coverage. The corrective loop added readable `Modalidade: online|presencial|online e presencial` output, conservative legacy fallback, complete API/null/matrix coverage, and structured documentation assertions. Focused re-review found no remaining source defect; the orchestrator then ran the prescribed backend gate successfully: `79 passed`, JSON parsing passed, and `make quality` passed (black, isort, flake8 and mypy; 4 pre-existing deprecation warnings only).
- **Frontend review:** initial FIX-FIRST requested explicit read-only legacy-string coverage, the remaining global incompatibility cases, and multiple-card error coverage. The corrective loop added those tests; the re-review verdict was SHIP with no critical/high gaps. The prescribed frontend commands were run successfully by the group: focused Vitest `12/12`, full Vitest `241/241` in 27 files, typecheck and production build. Non-blocking existing warnings were `act(...)`, sourcemaps and chunk size.
- **Aggregate checks:** repeated frontend validation processes completed, `git diff --check` over every wish-owned path passed, and the backend quality/docs/JSON gates above passed in the combined workspace. No locale file is part of this wish diff.
- **Handoff:** implementation is complete in the working tree and ready for the normal execution/PR review; the wish remains `IN_PROGRESS` pending that lifecycle stage and merge/QA.

---

## Files to Create/Modify

```
src/api/schemas/config.py
src/api/routes/config.py
src/core/pipeline.py
src/prompt/templates/normal.py
tests/api/test_config.py
tests/core/test_pipeline.py
tests/srm/test_prompt_injection.py
tests/services/test_clinic_clone_service.py
tests/docs/test_clinic_service_modality_docs.py
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-service-modality.test.tsx
docs/configuration.md
docs/api-contracts.md
docs/examples/tenant-config-with-clinic.json
```
