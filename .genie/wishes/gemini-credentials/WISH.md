# Wish: Credenciais Gemini para o LLM Principal

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `gemini-credentials` |
| **Date** | 2026-09-06 |
| **Author** | Codex |
| **Appetite** | large |
| **Branch** | `wish/gemini-credentials` |
| **Repos touched** | `neurostack-api` |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Adicionar Gemini como provedor de LLM principal configurável na aba APIs, com o mesmo pacote de credenciais
já usado pelo GPT: URL, API key e modelo. A integração reutiliza o JSONB, o endpoint de credenciais e o cliente
de chat atuais, enquanto fixa o tráfego do provedor Gemini no endpoint compatível com OpenAI publicado pelo
Google. Qualquer identidade Gemini explicitamente persistida pela clínica é autoritativa e fail-closed: nunca
é apagada, completada ou substituída por credenciais do builder. A promoção só ocorre depois de um smoke
direto, executado dentro da mesma imagem OCI imutável que será implantada. O release é exclusivamente o
commit final assinado cujo pai direto é o candidate homologado e cuja única diferença é o receipt Gemini.

Este é um wish direto, sem fork de brainstorm: o alvo já está delimitado ao **Gemini Developer API** com
chave do **Google AI Studio**, e não Vertex AI/OAuth. A abstração LLM existente somada ao contrato oficial
OpenAI-compatible resolve a escolha de desenho sem introduzir outra integração ou superfície de autenticação.

## Problem

Clínicas que usam Gemini não conseguem cadastrar esse provedor como LLM principal com identidade, isolamento
de credencial e egress oficiais na configuração atual.

## Scope

### IN

- Adicionar o wire id `gemini` ao contrato LLM backend/frontend e à matriz de disponibilidade.
- Reutilizar `llm_provider`, `llm_api_url`, `llm_api_key` e `llm_model_name`; nenhuma coluna ou chave
  `gemini_*` será criada.
- Persistir, mascarar, recuperar e compor o bundle Gemini pela fronteira JWT-only
  `GET/PATCH /v1/config/api-credentials`, inclusive no overlay clinic/builder existente.
- Fixar o ownership da composição: a clínica é dona de qualquer bundle que contenha `llm_provider`; somente
  uma clínica sem `llm_provider` pode herdar o bundle completo do único tenant owner do builder resolvido por
  `Tenant.builder_id` + `User(role=builder).tenant_id`. Zero ou múltiplos owners falha fechado.
- Reconhecer exclusivamente a base normalizada constante
  `https://generativelanguage.googleapis.com/v1beta/openai` como egress `llm_gemini`; recusar query,
  fragment, userinfo, porta explícita ou qualquer path/base alternativo.
- Enviar chat completions pelo endpoint compatível com OpenAI do Gemini usando o cliente LLM existente e
  `Authorization: Bearer`, sem SDK Google adicional.
- Exibir o card `Gemini` / `by Google` na capacidade LLM com os campos URL, API key e modelo, preservando as
  regras atuais de segredo vazio, máscara, troca de provedor e isolamento entre clínicas.
- Criar um smoke live sanitizado, sem prompt clínico nem impressão de resposta/segredo, e registrar um recibo
  direto antes de promover Gemini de `coming_soon` para `active`.
- Corrigir `src/api/routes/rlhf.py` para que `clinic_tenant_id` e `message_id` só possam selecionar uma clínica
  autorizada pela resolução tenant/builder/API-key já existente antes de qualquer lookup de credencial.
- Criar `docker-compose.production.yml`, composição separada consumida exclusivamente pelo mesmo
  `scripts/deploy-vps.sh`: sem bind mount nem build de source no VPS e referenciando somente a imagem por
  digest OCI aprovada.
- Tornar `scripts/deploy-vps.sh`, confirmado pelo owner como entrypoint autoritativo de produção, fail-closed:
  o gate de evidência/assinatura executa no início de `main()` antes de qualquer escrita em `.env`, parada de
  containers, build, start ou migration.
- Tratar o próprio `scripts/deploy-vps.sh` como fronteira de confiança de produção, por decisão explícita do
  owner: Operations o executa em checkout confiável, e seu `main()` valida HEAD, assinatura, fingerprint,
  receipt e limpeza como primeira operação antes de qualquer efeito.
- Atualizar os testes backend, frontend, evidência de disponibilidade e documentação de configuração.

### OUT

- Adotar o SDK nativo do Google, a API `generateContent`, File API, grounding, busca ou recursos exclusivos do
  Gemini fora do contrato de chat compatível com OpenAI.
- Usar Gemini como fallback multimodal, STT, TTS ou processador direto de áudio/imagem nesta entrega.
- Criar credenciais `gemini_*`, tabela nova, migração, cofre, cache ou seleção automática de modelo.
- Integrar Vertex AI, OAuth, service accounts ou tornar o base URL configurável por ambiente/tenant.
- Alterar o comportamento, disponibilidade ou credenciais dos provedores GPT/OpenAI, Dropout, Outros e SLM
  Cortex além do acréscimo do novo item ao inventário compartilhado.
- Executar smoke com dados de paciente, persistir a chave de homologação ou ativar Gemini sem recibo direto.
- Criar ou alterar um segundo pipeline de deployment; `scripts/deploy-vps.sh` é a única fronteira de produção
  declarada para este wish.
- Fazer build de source, `docker compose build` ou `--no-cache` no VPS; a imagem é construída uma vez no
  ambiente protegido, homologada e atestada pelo commit assinado do receipt, e apenas puxada pelo digest.
- Aceitar commit posterior ao receipt, cobertura de apenas parte da árvore ou attestation criptográfica paralela
  como autorização de release. Qualquer mudança posterior exige novo candidate, build, smoke, receipt e final.
- Aceitar `clinic_tenant_id`/`message_id` apenas porque o UUID existe, ou permitir que um principal consulte
  credencial de tenant fora do seu ownership/autorização.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Usar `gemini` como wire id da capacidade `llm` | Mantém o provedor distinguível no status, cache e métricas sem criar uma nova capacidade. |
| 2 | Reutilizar os quatro campos `llm_*` | O sistema já modela exatamente um LLM principal por tenant; novos campos duplicariam persistência, máscara, overlay e rotação. |
| 3 | Usar Gemini Developer API com chave Google AI Studio pela [compatibilidade OpenAI do Google](https://ai.google.dev/gemini-api/docs/openai), sem Vertex AI/OAuth | O cliente atual já envia `/chat/completions` com bearer token; o contrato oficial resolve a escolha de desenho e evita SDK/auth paralelos. |
| 4 | Fixar a base canônica exata `https://generativelanguage.googleapis.com/v1beta/openai` como constante de código e usar `invalid_provider_endpoint` | A validação inspeciona o valor bruto antes de normalizar e rejeita qualquer porta explicitamente escrita, inclusive `:443`; depois exige igualdade canônica e recusa userinfo, query, fragment, host ou path alternativo sem ecoar input. |
| 5 | Construir uma única imagem OCI no commit candidate limpo, executar o smoke nessa imagem e promover somente seu digest | O objeto testado e o implantado têm os mesmos bytes efetivos. O deploy apenas autentica e puxa `repository@sha256:...`; nunca recompila source. |
| 6 | Manter o modelo editável | A disponibilidade de modelos Gemini muda fora deste repositório; um catálogo embutido ficaria obsoleto e exigiria sincronização sem requisito atual. |
| 7 | Manter o JSONB plaintext-at-rest existente nesta entrega | O risco preexistente é aceito e continua mitigado pelos controles atuais de least privilege e criptografia do banco e backups; migração para vault permanece OUT. |
| 8 | Compor evidência sem reescrever o recibo anterior | O checker preservará o artefato SHA-bound de 22 pares de `api-provider-availability` byte a byte e adicionará um único receipt Gemini com os bytes e o SHA do JSON canônico. |
| 9 | Toda troca de provider envolvendo Gemini substitui o bundle LLM inteiro | Exigir no mesmo PATCH `llm_provider`, `llm_api_url`, `llm_api_key` e `llm_model_name` recém-fornecidos evita que URL/modelo/chave do provider anterior sobrevivam ao merge; edição model-only só preserva chave/URL quando o provider não muda. |
| 10 | Tratar a assinatura nativa do commit final como attestation de Operations sobre a árvore inteira | `git verify-commit HEAD`, o fingerprint protegido, o pai candidate direto e o diff exato de um receipt autenticam simultaneamente o run, o digest e todos os bytes liberados; não há attestation criptográfica paralela nem allowlist no repositório. |
| 11 | Usar `scripts/deploy-vps.sh` como a própria fronteira de confiança | O owner aceitou explicitamente o script como trust boundary e único entrypoint autoritativo. O threat model assume invocação por Operations a partir de checkout confiável; o primeiro passo de `main()` verifica HEAD final exato, clean, assinatura e `GEMINI_RECEIPT_SIGNER_FINGERPRINT` antes de qualquer `.env` ou Docker. |
| 12 | Liberar somente o HEAD final assinado, sem commit posterior nem cobertura parcial | O receipt commit tem como pai direto o candidate e difere dele somente por `.genie/wishes/gemini-credentials/evidence/gemini-llm-receipt.md`; qualquer outro byte ou commit exige nova homologação. |
| 13 | Tornar `llm_provider` clinic explícito a identidade autoritativa | Clinic completo usa somente clinic; clinic incompleto, `coming_soon` ou inválido falha fechado e nunca herda builder. Builder só é owner válido quando clinic não tem provider e há exatamente um tenant builder-owner resolvido. |
| 14 | Resolver a autorização RLHF antes da credencial | `clinic_tenant_id` e o tenant dono de `message_id` precisam pertencer ao tenant do principal ou, para builder, ao conjunto de clínicas desse builder; API key permanece limitada ao tenant resolvido pela chave. ID estrangeiro falha sem chamar loader/factory/transporte. |
| 15 | Usar composição de produção separada sob o mesmo entrypoint | `docker-compose.production.yml` fixa `ENVIRONMENT=production`, `DEBUG=false`, imagem por digest e nenhum bind mount/build; `SECRET_KEY`, origens e demais segredos são obrigatórios e valores dev de JWT/API key são rejeitados. |
| 16 | Construir somente a árvore Git do candidate e implantar somente o HEAD final clean | `git archive` exclui dirty, untracked, ignored e escapes do contexto; candidate e final rejeitam symlink/gitlink, e nenhum commit posterior ao receipt é aceito. |

## Simplicity Case

- **Simplest complete design:** acrescentar um provider id ao bundle LLM existente, um host de egress fixado,
  um card na lista existente e testes/recibo que provem save-load-runtime.
- **Added machinery:** um módulo smoke sanitizado dentro da imagem, um receipt overlay e um release em dois
  commits com gate obrigatório no próprio entrypoint autoritativo,
  exigidos pela política de promoção e pela necessidade de ligar evidência live autêntica à árvore final. O
  smoke lê `GEMINI_API_KEY` e `GEMINI_MODEL`, usa a base constante e um prompt sintético, e emite apenas
  pass/fail. O commit candidato contém todo o código final mas não é implantável enquanto o checker falha; o
  sucessor assinado altera apenas o receipt. `deploy-vps.sh`, aceito como trust boundary, executa o checker
  como primeira operação antes de `.env` ou Docker.
- **Deferred until measured:** SDK nativo ou recursos exclusivos do Gemini só serão considerados quando uma
  história aprovada exigir uma função que a compatibilidade OpenAI não ofereça; descoberta/listagem automática
  de modelos só será considerada se erros por model id se tornarem um problema observado.
- **Complexity removed:** sem schema de banco, provider registry novo, dependência Python, fallback, retry,
  feature flag, cache, job de sincronização, cobertura parcial da árvore, attestation paralela ou commits
  posteriores liberáveis.

## Dependencies

**depends-on:** api-provider-availability, backend-test-baseline-repair
**blocks:** none

## Success Criteria

- [ ] O contrato aceita `llm_provider=gemini` e continua rejeitando ids desconhecidos com erro estável e sem
      ecoar entradas ou segredos.
- [ ] Um bundle Gemini completo usa exclusivamente os campos `llm_*`, mantém a chave fora da resposta e expõe
      somente `llm_api_key_set`/máscara constante; antes da promoção continua não operacional e não produz
      readiness/overlay/runtime ativo, mesmo se já existir no JSONB.
- [ ] Toda troca de provider envolvendo Gemini, tanto outro → Gemini quanto Gemini → outro, exige no mesmo
      PATCH atômico um bundle **completo e recém-fornecido**: `llm_provider`, `llm_api_url`, `llm_api_key` e
      `llm_model_name`. URL/modelo omitidos nunca sobrevivem ao merge: a API rejeita sem mutação com
      `provider_switch_bundle_required`, sem ecoar valores; somente edição model-only que mantém o provider
      pode preservar chave/URL atuais.
- [ ] Um bundle rotulado Gemini aceita somente a base normalizada exata
      `https://generativelanguage.googleapis.com/v1beta/openai`; antes da normalização, a inspeção do valor
      bruto (`mode=before`) rejeita qualquer porta explícita, inclusive `:443`, e depois rejeita host, path,
      query, fragment ou userinfo antes de persistência/chamada externa com o erro input-free
      `invalid_provider_endpoint`, `capability=llm`, `provider=gemini` e `field=llm_api_url`.
- [ ] Na composição, qualquer `llm_provider` explicitamente presente no bundle clinic define a identidade
      autoritativa. Se esse bundle estiver incompleto, inválido ou `coming_soon`, ele permanece clinic-owned e
      falha fechado: nenhum campo LLM do builder é lido, copiado ou usado. Somente a ausência do provider na
      clínica permite herdar os quatro campos do único builder-owner autorizado e completo.
- [ ] `get_tenant_llm_client` consome `llm_provider` e, sempre que houver qualquer `llm_provider` explícito,
      retorna nenhum cliente/erro estático de configuração e faz zero tentativa outbound se o bundle efetivo
      estiver incompleto, não operacional ou incoerente entre provider e endpoint. Isso vale para injeção via
      JSONB/DB/overlay e jamais cai no cliente global; antes da promoção também barra Gemini canônico fora do
      conjunto operacional.
- [ ] Casos com clínicas X/Y e builders A/B provam em composição pura, lookup DB, runtime loader e factory que
      X jamais lê segredo de B/Y, inclusive clinic Gemini explícito incompleto/`coming_soon`, owner ausente ou
      ambíguo; asserts cobrem zero carregamento de segredo e zero outbound cruzado.
- [ ] Em RLHF, `clinic_tenant_id` e o owner de `message_id` são resolvidos e autorizados antes do loader: clinic
      admin só usa seu tenant, builder só clínicas ligadas ao seu `builder_id`, e API key só o tenant resolvido
      pela chave. IDs de outro tenant são rejeitados/falham fechados sem fallback, lookup de segredo ou outbound.
- [ ] O transporte classifica a URL oficial como `llm_gemini`, fixa DNS/Host/SNI nesse host, proíbe redirect e
      envia `/v1beta/openai/chat/completions` com bearer token pelo cliente existente.
- [ ] Teste com transporte mockado prova payload, header e parsing de resposta. Falhas HTTP/transporte são
      traduzidas na fronteira do cliente para uma exceção estática sem segredo antes de callers registrarem
      `exc_info`; `str`, `repr`, log e traceback não contêm chave, URL recebida, payload ou resposta.
- [ ] O card `Gemini` / `by Google` aparece na seção LLM e, quando disponível, abre URL, API key e modelo com o
      mesmo comportamento do card GPT.
- [ ] O editor Gemini preenche a URL com a constante pública canônica e traduz em PT/EN
      `invalid_provider_endpoint` e `provider_switch_bundle_required`, explicando respectivamente a URL oficial
      e a necessidade de informar URL, API key e modelo juntos; nenhum erro cai no genérico “Tente novamente”.
- [ ] Abrir Gemini nunca hidrata URL/modelo/chave pertencentes a outro provedor ou clínica; a chave permanece
      vazia na UI e fora de GET, DOM persistido e payload quando não foi alterada.
- [ ] O checker compõe o recibo base imutável de 22 pares com
      `.genie/wishes/gemini-credentials/evidence/gemini-llm-receipt.md`: o estado inicial global é
      zero `active`, 20 `coming_soon`, três `removed`; o final é um `active`, 19 `coming_soon`, três `removed`.
- [ ] O release candidate é um commit tracked limpo, sem staged, unstaged, untracked, symlink ou submodule,
      contém o estado final de produto e gates, mas não é implantável sem receipt. Em ambiente protegido, uma
      única imagem OCI é construída exclusivamente de `git archive "$tested_commit"`, com buildx,
      provenance e SBOM, e registrada como `repository@sha256:<64-hex>`; não há rebuild nem contexto do worktree.
- [ ] O smoke live usa prompt sintético, lê somente `GEMINI_API_KEY` e `GEMINI_MODEL`, usa a base constante
      não sobrescrevível e roda como `python -m src.llm.gemini_smoke` dentro dessa imagem por digest; os metadados
      públicos entram por argumentos, e chave, prompt completo, resposta, URL, tenant, clínica ou paciente não
      são impressos nem gravados.
- [ ] O smoke retorna códigos estáveis e secret-free para configuração ausente, autenticação/modelo rejeitado,
      transporte e resposta inválida, sempre com exit nonzero e próximo passo documentado, sem exception body
      ou valores submetidos.
- [ ] O smoke gera no diretório transitório protegido criado fora do checkout um JSON UTF-8 canônico
      (chaves ordenadas, separadores compactos, newline final), escrito pelo próprio módulo Python sem
      normalizador externo, com
      somente `schema`, `result`, `result_code`, `validation_kind`, `validation_authority`, `timestamp`, `run_id`,
      `tested_commit`, `image_digest`, `smoke_module_sha256` e `exit_code`; o módulo calcula seu SHA por `__file__`.
- [ ] O único receipt Gemini incorpora o JSON canônico byte a byte e seu `smoke_artifact_sha256`. O checker
      reserializa/verifica esquema, bytes e SHA, exige `result=pass`, `exit_code=0`, candidate igual ao commit
      usado no build, digest OCI idêntico ao usado no smoke/compose, pai candidate direto e diff exato do receipt.
- [ ] O commit final do receipt é assinado por `Production Operations Owner`, tem o candidate como pai direto e
      difere dele somente por `.genie/wishes/gemini-credentials/evidence/gemini-llm-receipt.md`; `HEAD` deve ser
      exatamente esse commit, nunca um commit posterior. A assinatura Git atesta run, digest e árvore final.
- [ ] `docker-compose.production.yml` é usado pelo mesmo entrypoint, não tem `build:` nem bind mount e recebe a
      imagem exclusivamente como digest aprovado. Exige `ENVIRONMENT=production`, `DEBUG=false`, `SECRET_KEY`
      forte e origens explícitas; valores default/dev de JWT, API keys ou secrets são rejeitados.
- [ ] Como trust boundary aceito, `scripts/deploy-vps.sh` chama no início de `main()` um checker que roda
      `git verify-commit HEAD`, extrai e compara o fingerprint com `GEMINI_RECEIPT_SIGNER_FINGERPRINT`, exige
      HEAD igual ao próprio receipt commit final e checkout/index clean, tudo antes de ler/criar `.env`, testar
      permissões ou invocar Docker.
- [ ] Checker e testes do entrypoint cobrem HEAD divergente/posterior, dirty tracked,
      dirty index, untracked, symlink, gitlink, parent/diff do receipt inválidos, JSON/SHA/candidate/image
      divergentes e trust/assinatura ausentes; qualquer falha ocorre antes de `.env` e Docker.
- [ ] Testes focados, `make quality`, suíte frontend, typecheck, build e validação de evidência passam no
      conjunto final.

## Execution Strategy

### Wave 1 (parallel backend/frontend staging, after cross-wish dependencies)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 3: credenciais/egress (+2), contrato compartilhado (+1) | engineer-standard / high | Contrato backend, endpoint exato, guard de factory e smoke sanitizado em `coming_soon` |
| 2 | engineer | 2: estado de credenciais e aceitação visual (+2) | engineer-standard / medium | Card Gemini e paridade visual, ainda não interativo |

### Wave 2 (promotion and release gate after Groups 1 and 2)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 3: homologação externa (+2), gate de release assinado (+1) | engineer-standard / high | Candidate final bloqueado, smoke direto e sucessor apenas com recibo assinado |

Depois de `api-provider-availability` e `backend-test-baseline-repair`, Groups 1 e 2 executam em paralelo sobre
contratos previamente especificados neste wish. Ambos são shippable em estado fail-closed (`coming_soon`);
Group 3 depende dos dois, prepara membership operacional, readiness/runtime ativo e card interativo no candidate,
mas o checker impede sua implantação. A ativação só ocorre no sucessor assinado que tem pai candidate direto,
difere apenas pelo receipt e é o HEAD exato aprovado; qualquer commit posterior reinicia build e homologação.

## Execution Groups

### Group 1: Contrato Gemini, egress e smoke seguro

**Goal:** Preparar contrato, validação, transporte e smoke Gemini mantendo todo bundle Gemini não operacional.

**Deliverables:**
1. Adicionar `gemini` somente ao schema/contrato e inventário LLM como `coming_soon`; **não** adicioná-lo a
   `OPERATIONAL_LLM_PROVIDERS`, readiness, overlay ou runtime ativo nesta etapa.
2. Definir a constante de código `https://generativelanguage.googleapis.com/v1beta/openai`, a policy
   `llm_gemini` e egress com DNS/Host/SNI fixados, redirect proibido e sem override por env/tenant.
3. Em validador `mode=before`, inspecionar o valor bruto e rejeitar qualquer porta explicitamente escrita,
   inclusive `:443`, antes de normalizar. Então exigir igualdade exata com a constante e recusar path
   alternativo, query, fragment, userinfo ou host diferente com `invalid_provider_endpoint` estável,
   input-free e sem persistência.
4. Fazer `get_tenant_llm_client` consumir `llm_provider` e validar membership operacional e endpoint antes da
   construção do cliente. Testar bundles injetados por DB/JSONB/overlay para provar zero tentativa outbound,
   inclusive Gemini canônico pré-promoção e Gemini com path/query não canônicos.
5. Exigir os quatro campos `llm_provider`, `llm_api_url`, `llm_api_key` e `llm_model_name` completos e
   recém-fornecidos no mesmo PATCH atômico em toda transição outro ↔ Gemini. Rejeitar sem mutação com
   `provider_switch_bundle_required` em vez de mesclar campos omitidos; provar nos testes negativos de API e
   factory que URL/modelo/chave de nenhum lado sobrevive à troca. Preservar chave/URL somente em edição
   model-only quando o provider não muda.
6. Cobrir o contrato OpenAI-compatible com transporte mockado e traduzir qualquer falha HTTP/transporte na
   fronteira do cliente para uma exceção estática secret-free, antes dos callers que registram `exc_info`.
   Testar `str(exc)`, `repr(exc)`, registro capturado e traceback completos contra chave/URL/payload/resposta.
7. Criar a implementação importável `src/llm/gemini_smoke.py`, copiada na imagem pelo `COPY src ./src` já
   existente, e manter `scripts/gemini_llm_smoke.py` somente como wrapper de desenvolvimento. Somente
   `GEMINI_API_KEY` e `GEMINI_MODEL` entram pelo ambiente; metadados públicos entram por argumentos. O módulo
   usa o cliente/egress de produção com prompt sintético, calcula seu SHA por `__file__`, serializa JSON canônico
   sem normalizador externo e diferencia por código estável configuração ausente, auth/modelo rejeitado,
   transporte e resposta inválida, sempre com exit nonzero no erro.
8. Atualizar `docs/configuration.md` com fronteira JWT/roles, `GET/PATCH /v1/config/api-credentials`, wire id,
   endpoint exato, campos reutilizados, bundle completo na troca, segredo vazio = preservar no mesmo provider,
   erros estáveis PT/EN, limitação beta, smoke e troubleshooting do gate de release.
9. Alterar a composição para que a presença de `llm_provider` no clinic seja identidade autoritativa: retornar
   somente os campos clinic, ainda que incompletos, `coming_soon` ou inválidos. Herança builder só existe quando
   clinic não contém provider e `_resolve_builder_owner_tenant_id` encontra exatamente um tenant owner por
   `Tenant.builder_id` + `User.role=builder`; owner ausente/ambíguo não carrega bundle algum.
10. Em `src/api/routes/rlhf.py`, autorizar `clinic_tenant_id` e o tenant do Event de `message_id` antes de chamar
    `_load_runtime_llm_credentials`: clinic admin fica no próprio tenant; builder pode apontar somente clínicas
    com o mesmo `builder_id`; API key fica no tenant que a chave resolveu. ID inexistente, malformado, de outro
    tenant ou conflito entre os dois identificadores falha fechado, sem fallback para o ID estrangeiro.

**Acceptance Criteria:**
- [x] Schema reconhece o wire id sem campos novos, mas um bundle Gemini pré-existente permanece excluído de
      operational membership, readiness, overlay e runtime; PATCH continua `provider_not_available` sem mutação.
- [x] Somente a base canônica exata é aceita; testes negativos cobrem wrong path, query, host, userinfo,
      fragment e portas explícitas `:443`/não padrão, confirmando que o validador `mode=before` recebeu o valor
      bruto e rejeitou antes de commit ou construção do cliente.
- [x] Factory barra injeção direta/DB/overlay incompleta, não operacional ou provider/endpoint-incoerente com
      nenhum cliente/erro estático, zero outbound e sem depender da validação PATCH; provider explícito nunca
      recua para o cliente/configuração global.
- [x] Testes mockados verificam URL final, `Authorization: Bearer`, modelo e resposta; falha HTTP/transporte
      produz somente exceção/log/traceback estáticos e secret-free.
- [x] Testes negativos de API **e** factory cobrem trocas parciais outro → Gemini e Gemini → outro, omitindo
      cada parte do bundle completo, e provam rejeição atômica, zero outbound e ausência de fallback global;
      model-only no mesmo provider preserva chave/URL atuais.
- [x] Matriz com tenants clinic X/Y e builder A/B cobre composição, DB loader, overlay e factory: provider
      clinic explícito jamais é sobrescrito/apagado nem lê segredo builder; somente clinic sem provider herda
      o builder-owner único e correto; incompleto/`coming_soon`/owner ambíguo produz zero segredo e outbound.
- [x] Testes RLHF para clinic admin, builder e API key combinam `clinic_tenant_id`/`message_id` próprios e
      cross-tenant e provam que caso não autorizado não consulta `_load_runtime_llm_credentials`, não constrói
      cliente e não produz outbound; nenhum segredo do tenant estrangeiro aparece em retorno, log ou traceback.
- [x] Smoke falha fechado com códigos/next steps estáveis quando variáveis faltam, auth/modelo é rejeitado,
      transporte falha ou resposta é vazia/inválida, e nunca imprime material sensível, inclusive em exceções.
- [x] A referência de configuração cobre o contrato público e o runbook sem expor credenciais.

**Validation:**
```bash
poetry run pytest tests/services/test_api_credentials.py tests/api/test_api_credentials.py tests/services/test_safe_egress.py tests/llm/test_client.py tests/llm/test_gemini_smoke.py tests/services/test_event_service_llm_fallback.py tests/services/test_event_service_config_cache.py tests/api/test_rlhf.py tests/scripts/test_gemini_llm_smoke.py -q
make quality
```

Os testes focados isolam contrato, segredo, transporte e CLI; `make quality` é o gate documentado do
repositório e é necessário porque schema e egress compartilhados alcançam todo o runtime backend.

**depends-on:** none

---

### Group 2: Card e campos Gemini na aba APIs

**Goal:** Oferecer a mesma experiência de configuração do GPT sem cruzar drafts ou segredos entre providers.

**Deliverables:**
1. Adicionar `gemini` ao tipo, catálogo e espelho de disponibilidade LLM como `Gemini` / `by Google`.
2. Reutilizar os campos URL, API key e modelo do bundle LLM e o componente de card existente.
3. Preencher a URL do editor Gemini com a constante pública canônica, preservando o campo/editor existente e
   impedindo que URL de outro provider seja hidratada.
4. Mapear `invalid_provider_endpoint` e `provider_switch_bundle_required` para mensagens acionáveis PT/EN,
   sem exibir URL recebida ou valores do bundle.
5. Cobrir estados `coming_soon`, disponível, incompleto e configurado, hidratação, troca com bundle completo
   recém-informado, edição model-only no mesmo provider, erro backend e mudança de clínica.
6. Manter Gemini fora da matriz TypeScript ativa nesta etapa; fixtures de estado ativo servem apenas para
   testar o componente e a mudança real de matriz pertence ao Group 3.

**Acceptance Criteria:**
- [x] Antes da promoção, o card está visível com `EM BREVE`/`COMING SOON`, sem foco, editor ou payload.
- [x] No estado ativo, o card abre os três campos e salva `llm_provider=gemini` com o bundle informado.
- [x] Segredo nunca é preenchido; provider/clínica alheios não hidratam URL nem modelo no editor Gemini.
- [x] URL canônica aparece preenchida para novo Gemini; endpoint divergente e troca incompleta exibem instrução
      específica em PT/EN, nunca o fallback genérico.
- [x] Badges e demais mensagens funcionam em PT/EN e os cards LLM existentes não mudam de comportamento.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack test -- src/pages/clinic-config-api-settings.test.tsx
pnpm --dir frontend/artifacts/neurostack run typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
```

O teste focado cobre o fluxo alterado; typecheck e build validam o contrato compartilhado da página e o bundle
de produção do frontend.

**depends-on:** none

---

### Group 3: Homologação direta e release em dois commits

**Goal:** Produzir um release cujo HEAD final assinado cobre a árvore inteira, tem pai candidate homologado
direto e difere dele exclusivamente pelo receipt Gemini.

**Deliverables:**
1. Finalizar WISH, produto, matrizes/promoção, módulo smoke, checker, compose, deploy, docs e testes **antes**
   do candidate. Criar o candidate com checkout/index clean e zero untracked; rejeitar entries `120000` e
   `160000`. O WISH e todo arquivo exceto o futuro receipt já estão finais nesse commit.
2. No ambiente protegido de build, criar `tmpdir` por `mktemp -d` fora do checkout e exigir Docker buildx como
   pré-requisito fail-closed. Construir uma vez e somente da árvore Git com o comando canônico:
   `git archive --format=tar "$tested_commit" | docker buildx build --file Dockerfile --target production --provenance=true --sbom=true --push --tag "$GEMINI_IMAGE_REPOSITORY:$tested_commit" --metadata-file "$tmpdir/build-metadata.json" -`.
   Ler diretamente `containerimage.digest` do metadata, exigir `sha256:<64-hex>` e formar
   `image_digest=$GEMINI_IMAGE_REPOSITORY@sha256:<64-hex>` sem resolver ou confiar na tag. Falha de buildx,
   metadata ausente/inválido ou digest divergente encerra o run. Dirty, untracked, ignored e symlink escapes não
   entram no contexto; o VPS não precisa de buildx e nunca faz build.
3. Executar o smoke dentro de `"$image_digest"`, com filesystem read-only e somente `GEMINI_API_KEY` e
   `GEMINI_MODEL` herdados por env. O comando canônico usa o Python do runtime, sem Poetry, e redireciona o
   stdout sanitizado no host para o `tmpdir` externo:
   `docker run --rm --read-only --tmpfs /tmp --env GEMINI_API_KEY --env GEMINI_MODEL "$image_digest" python -m src.llm.gemini_smoke --json --tested-commit "$tested_commit" --image-digest "$image_digest" --run-id "$run_id" >"$tmpdir/gemini-smoke.json"`.
   O módulo usa base/prompt constantes, calcula `smoke_module_sha256` a partir de `__file__` e emite JSON
   canônico UTF-8 com chaves ordenadas, separadores compactos e newline final, sem normalizador externo.
4. O JSON aceita exclusivamente `schema`, `result`, `result_code`, `validation_kind`, `validation_authority`,
   `timestamp`, `run_id`, `tested_commit`, `image_digest`, `smoke_module_sha256` e `exit_code`. Operations embute
   seus bytes exatos e `smoke_artifact_sha256` no único
   `.genie/wishes/gemini-credentials/evidence/gemini-llm-receipt.md`; nenhuma credencial ou resposta entra nele.
   O receipt base SHA-bound de 22 pares permanece byte a byte idêntico e o overlay Gemini é único.
5. Criar e assinar o receipt commit por `Production Operations Owner`. Ele deve ter exatamente um pai, o
   `tested_commit`, e `git diff-tree` entre pai e filho deve mostrar somente o receipt Gemini. O checker exige
   `HEAD == receipt_commit`, nunca commit posterior, `git verify-commit HEAD`, fingerprint igual ao
   `GEMINI_RECEIPT_SIGNER_FINGERPRINT` protegido, JSON/esquema/SHA válidos, `exit_code=0`, candidate-pai em
   igualdade e o mesmo `image_digest` no smoke, receipt e compose. A assinatura Git cobre toda a árvore final e
   atesta o run/digest; não existe attestation criptográfica separada.
6. Aplicar a decisão explícita do owner de que `scripts/deploy-vps.sh` é a própria fronteira de confiança e o
   único entrypoint autoritativo. O threat model assume que Operations inicia esse script em checkout confiável;
   não há launcher/preflight externo. Como primeira operação de `main()`, antes até de permissões ou `.env`, o
   script chama o checker, que exige HEAD igual ao receipt commit vigente, assinatura/fingerprint aprovados,
   pai/diff exatos, checkout/index clean e zero untracked.
7. Criar `docker-compose.production.yml` standalone e usá-lo somente por
   `scripts/deploy-vps.sh -f docker-compose.production.yml`: `image` é obrigatoriamente o
   `image_digest` aprovado, sem `build:` nem bind mounts. Fixar `ENVIRONMENT=production`, `DEBUG=false`; exigir
   `SECRET_KEY`, allowed origins e credenciais de infraestrutura fortes, rejeitando chaves JWT/API defaults,
   vazias ou marcadas dev/test. `tests/scripts/test_production_compose.py` valida o `docker compose config`.
8. Fazer o checker ser a primeira operação de `main()` em `scripts/deploy-vps.sh`, antes inclusive de ler ou
   criar `.env`, testar permissões/Docker ou confirmar. O checker repete HEAD exato, clean, assinatura,
   fingerprint, parent/diff, receipt e digest; só depois o script puxa e sobe
   `"$GEMINI_IMAGE_REPOSITORY@$receipt_digest"`. Nunca usa tag, build ou source do checkout no container.
   O checker permanece executável com a biblioteca padrão pelo `python3` do host; Python ausente falha na própria
   primeira operação, antes de qualquer efeito. Testes invocam diretamente o script e provam que ausência ou
   divergência falha antes de efeitos.
9. Manter `docs/configuration.md` como referência e adicionar links/runbook de recuperação em
   `docs/api-provider-settings-rollout.md` e `docs/troubleshooting.md` para trust/fingerprint ausente, assinatura
   inválida, HEAD/parent/diff divergente, `python3` ausente no VPS, buildx/metadata/digest falhos e receipt stale.
   Substituir no `README.md` o recipe de produção por tag/build de checkout pelo entrypoint assinado por digest e
   indexar o runbook de Operations em `docs/README.md`.
   Qualquer release futuro, inclusive mudança alheia a Gemini, cria novo candidate/build/smoke/receipt; não há
   bypass nem commit posterior aceito.

**Acceptance Criteria:**
- [ ] Candidate/final estão clean e sem symlink/gitlink; `git archive` alimenta buildx diretamente, metadata
      fornece `containerimage.digest` SHA-256 sem tag lookup e o smoke roda no mesmo digest, sem nova build.
- [ ] O receipt único contém bytes/SHA do JSON canônico sem campos extras; checker valida schema, resultado,
      exit code, run id, candidate, SHA do módulo e igualdade do image digest.
- [ ] HEAD é exatamente o receipt commit assinado/fingerprint aprovado; seu pai direto é candidate e seu diff
      único é o receipt Gemini. Nenhum commit posterior é implantável.
- [ ] Após promoção, um novo tenant pode salvar Gemini e o card fecha como configurado; os 22 pares anteriores
      conservam decisões e bytes; somente o overlay Gemini é acrescentado.
- [ ] `deploy-vps.sh`, como trust boundary aceito, executa o checker como primeira operação de `main()`; testes
      diretos cobrem ausência e cada divergência antes de `.env`, checagem de permissões ou Docker.
- [ ] Compose production standalone não contém build/bind mount, rejeita environment/secrets dev e executa
      somente `repository@sha256` do receipt; o deploy puxa esse digest sem recompilar ou attestation paralela.
- [ ] Buildx é requisito apenas do ambiente protegido de build; VPS requer Python 3, Docker Compose, Git e trust
      GPG/SSH. Runbooks tornam Python, buildx/metadata/digest e trust/HEAD/receipt acionáveis, sempre com nova
      homologação.
- [ ] Todos os gates passam no checkout final assinado.

**Validation:**
```bash
poetry run python scripts/validate_provider_availability_evidence.py --phase release
poetry run pytest tests/scripts/test_deploy_vps_gate.py tests/scripts/test_production_compose.py tests/scripts/test_validate_provider_availability_evidence.py tests/llm/test_gemini_smoke.py tests/scripts/test_gemini_llm_smoke.py tests/api/test_middleware_auth.py -q
make quality
pnpm --dir frontend/artifacts/neurostack test
pnpm --dir frontend/artifacts/neurostack run typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
```

O smoke não mockado satisfaz a política de homologação. Os testes do entrypoint provam que HEAD exato,
assinatura/fingerprint, parent/diff, JSON/SHA e digest governam o deploy antes de efeitos.

**depends-on:** 1, 2

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Em development, um clinic admin abre Gemini, informa URL oficial, chave e modelo, salva e vê o card
      `CONFIGURADO` sem a chave reaparecer.
- [ ] Um prompt sintético percorre o mesmo loader/cliente da clínica e retorna texto pelo endpoint Gemini;
      logs, erros, cache e respostas HTTP não contêm a chave.
- [ ] Trocas OpenAI → Gemini e Gemini → OpenAI exigem provider, URL, chave e modelo completos e recém-informados;
      omitir qualquer campo rejeita atomicamente e nunca usa cliente global. Edição posterior somente do modelo,
      sem mudar provider, conserva chave/URL do provider atual.
- [ ] URL oficial vem preenchida no editor e os erros de endpoint/troca incompleta explicam o próximo passo em
      PT/EN sem expor valores.
- [ ] Clinic com provider explícito incompleto/`coming_soon` falha sem herdar builder; clinic sem provider só
      herda o único builder-owner autorizado. Clínicas/builders cruzados não carregam segredo nem fazem outbound.
- [ ] RLHF com clinic admin, builder e API key rejeita `clinic_tenant_id`/`message_id` cross-tenant antes do
      loader de credencial e do transporte.
- [ ] Cards e chamadas dos provedores LLM existentes mantêm o resultado anterior.
- [ ] Em ensaio isolado, a invocação direta de `deploy-vps.sh` recusa candidate, commit posterior,
      assinatura/fingerprint ou HEAD divergente antes de `.env`, permissões ou Docker; somente o receipt HEAD
      exato autenticado alcança o pull do digest aprovado.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| `api-provider-availability` ainda altera as mesmas matrizes/arquivos | High | Torná-lo dependência obrigatória; reconciliar sobre seu estado final e preservar todas as 22 decisões existentes. |
| O gate backend já possui reparo de baseline em andamento | Medium | Depender de `backend-test-baseline-repair` antes de exigir `make quality` verde. |
| Compatibilidade OpenAI do Gemini permanece beta | Medium | Limitar o escopo ao contrato documentado, cobrir parsing/erros e manter o provider fail-closed se o smoke live falhar. |
| Credencial/modelo de homologação não estão no repositório | High | Operations fornece por ambiente; sem ambos o Group 3 não promove nem libera o card. |
| Candidate com código ativo poderia ser confundido com release ativo | High | O checker, como primeira operação do entrypoint autoritativo, exige receipt HEAD exato assinado e rejeita candidate ou commit posterior antes de efeitos. |
| Assinatura ou trust indisponível/incorreto | High | Operations administra o trust GPG/SSH e `GEMINI_RECEIPT_SIGNER_FINGERPRINT` fora do repo; ausência/divergência faz o entrypoint terminar antes de `.env`, permissões ou Docker. |
| O script não pode autenticar seus próprios bytes antes de iniciar | High | Risco aceito explicitamente pelo owner ao declarar `scripts/deploy-vps.sh` a fronteira de confiança; o threat model pressupõe checkout confiável sob controle de Operations e testa todo efeito após a primeira chamada de `main()`. |
| Qualquer byte muda depois do smoke | High | HEAD deve ser exatamente o receipt commit assinado, com pai candidate direto e diff somente do receipt; toda mudança posterior exige novo candidate/build/smoke/receipt. |
| Contexto local contamina a imagem | High | Build recebe apenas `git archive "$tested_commit"`; clean/symlink/gitlink são exigidos e dirty, untracked, ignored e escapes ficam fora do tar. |
| Buildx, metadata ou digest falha | High | Ambiente protegido exige buildx fail-closed e lê `containerimage.digest` SHA-256 diretamente do metadata; sem campo válido não há smoke nem receipt. |
| Tag OCI muda ou VPS recompila source | High | Receipt/compose/deploy aceitam somente `repository@sha256` e proíbem `build`, bind mount e `--no-cache`; a assinatura Git atesta o digest sem attestation paralela. |
| Principal RLHF referencia tenant alheio | High | Autorizar clinic/message contra tenant, builder ownership ou API key antes de qualquer secret loader; testes provam zero leitura/outbound. |
| Model ids mudam fora do repositório | Medium | Manter modelo editável e validar um id fornecido no smoke; não embutir catálogo. |
| URL de marca poderia exfiltrar a chave para destino arbitrário | High | Validar a base canônica exata no save e novamente na factory; usar policy/DNS/Host/SNI fixados no runtime. |
| Segredos LLM permanecem em JSONB plaintext-at-rest | Medium | Risco preexistente aceito nesta entrega; manter least privilege e criptografia existentes do banco/backups. Vault permanece OUT. |

Este wish só supersede a nova decisão `llm/gemini` depois que `api-provider-availability` estiver concluído;
não substitui, reabre nem reescreve nenhuma das 22 decisões ou evidências SHA-bound anteriores.

---

## Review Results

### Plan Review — 2026-09-06T13:33:36Z

- **Target SHA-256:** `a7640986f16f340da5dacba46dac7440398bffa7336595306c3e4758c6b3a19c`
- **Verdict:** `FIX-FIRST`
- **Blocking gaps:** canonical endpoint incompleto e ativação operacional anterior à homologação.
- **Action:** fix loop 1 adicionou base exata, guard runtime, rotação bilateral, evidence overlay separado,
  proveniência, redaction de exceções e staging fail-closed.

### Plan Re-review — Fix Loop 1 — 2026-09-06T13:47:05Z

- **Target SHA-256:** `8e83c4256a09a9d9459db42ceee7ce56f8772a6c7f8e15049255495032f9c08e`
- **Checklist verdict:** `SHIP`; as lentes independentes ainda encontraram dois gaps `HIGH`.
- **Blocking advisory gaps:** troca de provider ainda podia reter URL/modelo e o recibo não estava autenticado
  nem ligado à árvore final liberada.
- **Action:** fix loop 2 passou a exigir bundle completo e definiu candidate imutável + sucessor somente de
  recibo assinado, com identidade de Operations e igualdade da árvore de produto.

### Plan Re-review — Fix Loop 2/2 — 2026-09-06T13:55:34Z

- **Context:** plan re-review final
- **Target SHA-256:** `b686ab9b84f29f6580b58397abbc8592aa38729d69747d15b9d5da437acf9d7e`
- **Repository HEAD:** `7b6e3111179339ae3b3ce528e8d396d3a6db7a23`
- **Verdict:** `BLOCKED`
- **Cause:** `missing-context`
- **Validated:** problema/escopo, endpoint canônico, bundle completo bilateral, factory sem fallback global,
  redaction, evidência overlay, commit candidato, recibo assinado, gates de teste e file list.
- **Remaining HIGH gap:** o candidate com Gemini ativo não está mecanicamente impedido de deploy. O único
  entrypoint encontrado, `scripts/deploy-vps.sh`, executa stop/build/start/migrations sem chamar
  `validate_provider_availability_evidence.py --phase release`, `git verify-commit` ou validar o fingerprint.
  O próprio script declara automatizar apenas parte do deploy, portanto não prova ser a fronteira protegida
  autoritativa.
- **Required unblocking evidence:** identificar o artefato/entrypoint protegido que todo deploy executa. Se
  `scripts/deploy-vps.sh` for autoritativo, uma revisão futura deve incluí-lo no Group 3 e na file list, com o
  gate antes de qualquer efeito e teste provando que candidate sem recibo sai nonzero. Se o deploy for externo,
  fornecer o artefato/evidência equivalente que consome somente o sucessor assinado aprovado.
- **Fix budget:** `2/2`; nenhum terceiro fix loop automático é permitido.

### DX / Public API Advisory — 2026-09-06

- **Target SHA-256:** `03f428c43708dee454ddca70b01a70b84e4982eeb6a06a993eb2e9432876e4f2`
- **Verdict:** `BLOCKED`; não altera a causa principal e acrescenta gaps que devem entrar na revisão humana.
- **HIGH:** comandos frontend planejados usam `npm` e omitem `PORT`/`BASE_PATH`, mas o workspace é pnpm e o
  build Vite exige as duas variáveis. A próxima revisão deve usar comandos pnpm executáveis com
  `PORT=3001 BASE_PATH=/` nos gates focado e agregado.
- **HIGH:** o editor atual abre URL genérica vazia e reduz `invalid_provider_endpoint` a “Tente novamente”. A
  próxima revisão deve exibir/preencher com segurança a base pública canônica, adicionar copy PT/EN acionável
  para endpoint fixo e para troca que exige URL/key/model juntos, e cobrir ambos os locales.
- **MEDIUM:** o smoke precisa de códigos estáveis secret-free por classe de falha e próximos passos, mantendo
  exit nonzero e sem exception body/valores submetidos.
- **MEDIUM:** `docs/configuration.md` deve documentar a fronteira JWT/role, endpoint de credenciais, wire id,
  base exata, bundle completo na troca, semântica de chave vazia, erros estáveis, limitação beta, smoke e
  troubleshooting do release gate.

### Human Resolution / Plan Resumption — 2026-09-06

- **Owner input:** `scripts/deploy-vps.sh` é o entrypoint autoritativo de produção.
- **Resolution:** Group 3 e a file list agora incluem um gate obrigatório no início de `main()`, antes de
  qualquer escrita/ação de deploy, com testes de zero efeitos para candidate bloqueado e passagem do sucessor
  assinado.
- **DX corrections folded in:** comandos pnpm com `PORT=3001 BASE_PATH=/`, URL Gemini canônica preenchida,
  erros PT/EN acionáveis, smoke com códigos estáveis e referência operacional completa.
- **Lifecycle:** o status retornou a `DRAFT` para uma nova plan review independente; os tasks permanecem
  bloqueados até um veredito `SHIP` persistido como `APPROVED`.

### Plan Review Fix — Credential Isolation / Immutable OCI — 2026-09-06

- **Verdict:** `FIXED-PENDING-REVIEW`.
- **Resolved:** clinic provider autoritativo e fail-closed; autorização RLHF antes de segredo; validação raw de
  porta; checkout limpo; imagem OCI build-once/smoke/deploy pelo mesmo digest; receipt canônico com attestation
  de Operations; compose production e runbooks de recuperação.
- **Review scope:** alteração somente deste plano; nenhuma implementação foi executada.

### Plan Fix Loop 2/2 — Exact Signed HEAD / Git-tree Build — 2026-09-06

- **Cause class:** `overdesigned-plan`.
- **Removed:** cobertura parcial/dynamic, commits posteriores liberáveis, attestation paralela, normalizador
  externo e lookup de digest por tag.
- **Resolved:** build exclusivamente de `git archive`; digest vindo de `containerimage.digest`; smoke importável
  dentro da imagem; receipt JSON canônico; final assinado com pai candidate direto e diff somente do receipt;
  trust preflight externo antes de executar o checkout, seguido do checker como primeira operação do único
  entrypoint de deploy.
- **Review scope:** alteração somente deste WISH; nenhuma implementação ou transição de task foi executada.

### Final Plan Re-review — 2026-09-06T14:52:06Z

- **Target SHA-256:** `34f0287dfbfe195bdf9e20de3e5541a372f7b6f89a806aa7aab8acdd13aa7005`
- **Verdict:** `BLOCKED`
- **Cause:** `missing-context`
- **Fix budget:** `2/2`; nenhum terceiro fix loop automático é permitido.
- **Validated:** contrato Gemini, isolamento clinic/builder/RLHF, endpoint/porta raw, bundle bilateral, redaction,
  HEAD final assinado, pai candidate/diff apenas receipt, contexto `git archive`, digest vindo do metadata buildx,
  smoke dentro da imagem exata, compose production por digest, checker interno primeiro e comandos pnpm.
- **Remaining HIGH gap:** o trust preflight obrigatório anterior à execução do checkout não possui artefato/job
  de Operations identificado, versão/digest imutável, enforcement point, configuração protegida nem evidência de
  instalação que prove que todo deploy autentica HEAD/clean/assinatura/fingerprint antes de chamar o script.
- **Required unblocking evidence:** informar o nome e caminho do executor/job protegido que invoca
  `scripts/deploy-vps.sh`, seu artefato/versionamento, comando/política de enforcement e evidência verificável de
  instalação/uso obrigatório. O script continua confirmado como entrypoint autoritativo, mas não pode autenticar
  seus próprios bytes antes de ser executado.
- **Task state:** os três tasks permanecem enforced-blocked até essa evidência ser incorporada e uma nova review
  independente retornar `SHIP`.

### Human Trust-Boundary Resolution — 2026-09-06

- **Owner decision:** o próprio `scripts/deploy-vps.sh` será a fronteira de confiança de produção.
- **Accepted threat model:** Operations inicia o script a partir de checkout confiável; autenticar os bytes do
  próprio script antes da execução não é requisito deste wish. O script continua fail-closed a partir da primeira
  operação de `main()` e valida HEAD, assinatura/fingerprint, receipt, limpeza e digest antes de qualquer efeito.
- **Plan change:** o preflight externo, `GEMINI_APPROVED_RECEIPT_COMMIT` e sua infraestrutura foram removidos;
  os testes agora exercitam diretamente o entrypoint autoritativo.
- **Lifecycle:** status retornou a `DRAFT` para nova review independente; tasks continuam bloqueados até `SHIP`.

### Plan Re-review — Owner Trust Boundary — 2026-09-06T15:01:50Z

- **Target SHA-256:** `38a91032aa764203f2527c84391f1a9d7f447591416d70deac2c80b939c62b27`
- **Verdict:** `SHIP`
- **Security advisory:** `SHIP`; nenhum gap acionável dentro do threat model aceito pelo owner.
- **Validated:** `scripts/deploy-vps.sh` como trust boundary e único entrypoint; checker como primeira operação de
  `main()`; HEAD assinado/pai/diff exatos; checkout clean; build por `git archive`; imagem única por digest;
  smoke na imagem implantada; compose production sem build/bind mount; isolamento clinic/builder/RLHF;
  endpoint/egress/redaction; UI PT/EN; gates backend/frontend e dependências.
- **Lifecycle:** plano aprovado; os enforced blocks dos três tasks podem ser removidos sem iniciar execução.

### Group 1 File-Scope Amendment Review — 2026-09-08

- **Reviewer:** `/root/review_dependency_closeout`.
- **Verdict:** `SHIP`; no CRITICAL/HIGH gaps.
- **Amendment:** add exactly `tests/services/test_event_service_config_cache.py` to Group 1 because its existing
  builder-inheritance fixture contradicts the already-approved explicit-clinic-provider ownership criterion.
- **Required coverage:** keep the no-provider inheritance case, and add an explicit-provider regression that
  proves builder-owner resolution/second builder-row read does not occur, the clinic provider remains unchanged,
  and no builder `llm_api_*` secret appears.
- **Scope:** traceability/test correction only; no new production behavior, abstraction, state, dependency, or
  acceptance criterion. The focused Group 1 gate now names this test file in addition to `make quality`.

### Group 1 Execution Review — Fix Loop 1/2 — 2026-09-08

- **Reviewer:** `/root/review_dependency_closeout`.
- **Verdict:** `FIX-FIRST`.
- **Resolved in loop 1:** added clinic-admin own-tenant and foreign-builder RLHF proofs with zero downstream
  effects; locked the smoke's five-key output/failure classes; separated malformed provider structure as
  `invalid_provider_response` from a structurally valid empty response as `unexpected_response`.
- **Engineer validation:** focused suite `472` passed; `make quality` passed with `2799` tests passed and `14`
  skipped; `git diff --check` passed.

### Group 1 Execution Re-review — Fix Loop 2/2 Required — 2026-09-08T15:56:26-03:00

- **Reviewer:** `/root/review_dependency_closeout`.
- **Verdict:** `FIX-FIRST`; two HIGH gaps remain after loop 1.
- **Target:** HEAD `8481cba5a452786afd0499ed624547d6b36bdeec`; WISH SHA-256
  `5041b34dc2e87dc379c527fd401d420f54c5122baede42de55d915013e259c6a`; ordered Group 1 snapshot SHA-256
  `58b71dadc0879711173e54169e1656a91b9e2d82c43aeaf4d2ab0002495f77dd`.
- **HIGH:** runtime projection discarded an explicit falsey clinic `llm_provider`, allowing `{}` to reach the
  factory and fall back to global credentials instead of failing closed.
- **HIGH:** configured standalone smoke inherited structlog's stdout logger, which could emit the submitted model
  and extra lines before the canonical JSON.
- **Required loop 2:** preserve presence semantics loader-to-factory with zero global/outbound effects, and add a
  real configured smoke boundary proving exactly one canonical secret-free JSON line and no stderr leakage.
- **Orchestrator validation before loop 2:** exact focused suite `472/472`; Black, isort, flake8 and mypy passed;
  full suite `2799` passed / `14` skipped; `git diff --check` passed.

### Group 1 Supply-Chain Quality Review — 2026-09-08T15:59:09-03:00

- **Reviewer:** `/root/gemini_group1_supply_chain` using the `genie:supply-chain` lens.
- **Verdict:** `FIX-FIRST`; independently confirmed the same two HIGH trust-boundary gaps.
- **Target:** base `8481cba5a452786afd0499ed624547d6b36bdeec`; WISH SHA-256
  `5041b34dc2e87dc379c527fd401d420f54c5122baede42de55d915013e259c6a`; composite implementation SHA-256
  `5c79dc05449cf4b2399c00b8d531a5bab25c233a0cc0490345a66adac3d643d5`.
- **Verified safe:** JWT/role and builder/tenant authorization precede credential access; public projections mask
  secrets; Gemini endpoint and egress are exact, DNS-pinned and redirect-free; known client failures are static
  and redacted; RLHF authorizes before loading runtime credentials; Gemini remains non-operational.
- **Required:** preserve falsey explicit provider presence through runtime projection and silence standalone smoke
  logging, with real-boundary tests for zero global/outbound fallback and one canonical JSON stdout line.
- **Accepted residuals:** plaintext JSONB storage is the pre-approved Decision 7 risk; Group 3 release provenance
  remains outside this review. The proposed profile delta was returned read-only and was not persisted.

### Group 1 Final Execution Re-review — 2026-09-08T16:15:50-03:00

- **Reviewer:** `/root/review_dependency_closeout`.
- **Verdict:** `SHIP`; no CRITICAL/HIGH gaps remain after fix loop 2/2.
- **Target:** HEAD `8481cba5a452786afd0499ed624547d6b36bdeec`; WISH SHA-256
  `cd6358aea2fdce9a461d0a0d0775dd6380625d1b3e2eeb471856b3303acbd5dd`; ordered Group 1 snapshot SHA-256
  `418940d28d2867460a0bb2c974de566db14b1fdccd18f03862ed288cf44c7cb1`.
- **Validated:** explicit falsey provider identity survives loader-to-factory and fails before global/client/outbound;
  truly absent provider inheritance still uses the unique builder owner; real-client smoke subprocess failures emit
  one five-key canonical JSON line with empty stderr and no sensitive inputs; the vacuous ambiguity assertion was
  replaced by an exact logger assertion.
- **Orchestrator gates:** focused `479/479`; Black, isort, flake8 and mypy passed; full suite `2806` passed / `14`
  skipped; post-run `git diff --check` passed. The fixer-reported `239` count was rejected and not used.
- **Residuals:** live Gemini authentication/model behavior is intentionally deferred to Group 3; plaintext JSONB
  storage remains the accepted Decision 7 risk.

### Group 1 Final Supply-Chain Re-review — 2026-09-08

- **Reviewer:** `/root/gemini_group1_supply_chain`.
- **Verdict:** `FIX-FIRST`; the two prior HIGH gaps are closed, but one newly surfaced HIGH remains.
- **Target:** WISH SHA-256 `803eb6a674c0698bc9e6a6e4286892caa36bbf9779a7ecee0fb56027041c15c2`;
  implementation SHA-256 `28838725566c5fa653f952d5f5290b5cc6c0adb8e0214ae5dd36f8f92ea56bb4`.
- **HIGH:** when a clinic has no `llm_provider` and builder-owner resolution returns zero or multiple owners, the
  runtime bundle is `{}` and the factory can use process-global credentials, violating Decision 13 and the
  zero-secret/zero-outbound criterion.
- **Required:** preserve the distinction between an unscoped call and failed tenant credential resolution; zero or
  ambiguous owner must reach an authoritative invalid state, while exactly one owner still inherits normally.

### Group 1 Execution Re-review Addendum — 2026-09-08

- **Reviewer:** `/root/review_dependency_closeout`.
- **Verdict:** `BLOCKED`; supersedes the earlier Group 1 `SHIP` verdict.
- **Confirmation:** no downstream guard exists in EventService or RLHF; `{}` reaches `get_llm_client()` and can
  generate through global credentials. Existing tests stop before the loader-to-factory boundary and one client
  test positively locks the conflicting global fallback for genuinely unscoped callers.
- **Diagnosis:** in-scope implementation/coverage defect after fix loop `2/2`; no automatic third fix loop is
  available. Human/orchestrator authorization is required to reopen execution or amend the governing WISH.
- **Lifecycle:** Group 1 is enforced-blocked; Group 3 remains dependency-blocked and no live/provider/release action
  may begin. Group 2 remains complete.

### Human-authorized Remediation After Exhausted Auto-loop — 2026-09-08T16:29:37-03:00

- **Approver:** repository owner, explicit response `autorizo` to the reported third surgical remediation.
- **Resolution:** reopen Group 1 for exactly one manual, tightly scoped remediation of the confirmed
  zero/ambiguous builder-owner → empty runtime bundle → global LLM fallback path.
- **Scope:** distinguish failed tenant credential resolution from a genuinely unscoped factory call; preserve
  unique-owner inheritance; add DB loader → runtime projection → factory regressions for zero and multiple owners
  proving no global secret, client construction, generation, or outbound. No Group 3 or unrelated changes.
- **Process:** this is an explicit human exception after the automatic `2/2` fix-loop budget, not another automatic
  retry. Independent execution and supply-chain re-reviews plus the full Group 1 gates remain mandatory.
- **Lifecycle:** WISH and Group 1 return to `IN_PROGRESS`; Group 3 remains dependency-blocked.

### Group 1 Human-authorized Remediation Review — 2026-09-08T16:40:54-03:00

- **Reviewer:** `/root/review_dependency_closeout`.
- **Verdict:** `SHIP`; no CRITICAL/HIGH findings.
- **Target:** HEAD `8481cba5a452786afd0499ed624547d6b36bdeec`; WISH SHA-256
  `8875cfaa8348616dbfa60c66013a03fd0c5ee4ef4cdfaf3e1e64a511376a7713`; ordered Group 1 snapshot SHA-256
  `1bd9cbbe19a577a4da6dfd6311ec6439ad235fdf1361d42c87367bbcef352c78`.
- **Validated:** omitted/`None` is the only genuinely unscoped global path; every explicit mapping, including `{}`,
  fails closed unless complete and operational. EventService passes explicit mappings for tenant scope, RLHF already
  did so, zero/multiple owners trap all downstream effects, and exactly one owner inherits correctly.
- **Orchestrator gates:** focused `482/482`; Black, isort, flake8 and mypy passed; full suite `2809` passed / `14`
  skipped; post-run `git diff --check` passed.

### Group 1 Human-authorized Supply-Chain Re-review — 2026-09-08T16:38:57-03:00

- **Reviewer:** `/root/gemini_group1_supply_chain`.
- **Verdict:** `SHIP`; no CRITICAL/HIGH gaps and the owner-cardinality → global fallback path is closed end-to-end.
- **Target:** WISH SHA-256 `8875cfaa8348616dbfa60c66013a03fd0c5ee4ef4cdfaf3e1e64a511376a7713`;
  implementation SHA-256 `9d0b285b83d0cce55e13a2bce6aa479dcac1acf44d5bc7677ac117a1c6114c98`.
- **Verified safe:** explicit tenant mappings never consult global credentials; zero/ambiguous owners fail closed in
  both runtime consumers; unique-owner inheritance remains isolated; falsey provider identity and standalone smoke
  output isolation remain enforced.
- **Residuals:** plaintext JSONB remains the accepted Decision 7 risk; live Gemini and immutable release provenance
  remain Group 3 responsibilities. The returned profile delta was read-only and not persisted.

### Group 3 Host-Prerequisite Amendment — 2026-09-08T16:46:49-03:00

- **Discovery:** `/root/gemini_group3_scout` confirmed that the authoritative deploy script must execute the
  stdlib-only Python checker before Docker, while the approved prerequisites named only Compose, Git and signature
  trust.
- **Amendment:** require host `python3`; its absence fails as the first `main()` operation, and add the missing-Python
  recovery case to the runbooks and acceptance criterion.
- **Scope:** documentation, tests and existing Group 3 files only; no dependency, runtime architecture, release
  authority or trust-boundary change. Independent amendment review is required before Group 3 dispatch.

### Group 3 Host-Prerequisite Amendment Review — 2026-09-08T16:50:22-03:00

- **Reviewer:** `/root/review_dependency_closeout` using the plan checklist and questioner lens.
- **Verdict:** `SHIP`; no CRITICAL/HIGH gaps.
- **Exact reviewed WISH SHA-256:** `55fd83a851ce09f2e9bb2fd0aa61ab94f7af4c50bcc4d8bdc3597dafb232fd18`.
- **Validated:** host Python is necessary for the stdlib checker; direct `python3 <checker>` remains the literal first
  attempted operation and fails before any side effect; no pre-check, new package, state, authority, or deployment
  path is introduced.
- **MEDIUM advisory for execution:** document and test Python `3.11+` (the repository floor) or deliberately remove
  newer-language usage; clarify that “no Python dependency” means no third-party/package dependency.

### Group 3 Candidate-readiness Execution Review — 2026-09-08T17:45:52-03:00

- **Reviewer:** `/root/review_dependency_closeout`.
- **Verdict:** `FIX-FIRST`; local gates are green, but the tree is not safe to freeze as candidate.
- **Target:** WISH SHA-256 `93979abd198bb770769f945d0c5c633ed54c146cc78cea37473f52b36eef01c9`;
  base `8481cba5a452786afd0499ed624547d6b36bdeec`; ordered snapshot SHA-256
  `4582c847f4ca21411041c201392da0acd67a10e1a7703d9ba3014372f20ddc9b`.
- **HIGH:** caller-CWD can substitute checker/compose/env; `.env` executes as Bash; SSH-signed commits cannot pass;
  infrastructure images use mutable tags; matrix validation is count-based rather than exact; receipt validation
  newline-normalizes bytes and does not require a newly added receipt absent from the candidate.
- **MEDIUM:** direct divergence tests are incomplete; `invalid_provider_endpoint` lacks `field=llm_api_url`; the
  PATCH description remains pre-promotion.
- **Evidence:** exact base receipt SHA-256 remains
  `02422ad8c8ac3cb101b8dfee56a2f212a2884b25eb9b6ff221db29c385b21d4a`; focused `82/82`; backend `2818`
  passed / `14` skipped plus static gates; frontend `243` tests, typecheck and build; diff-check passed.

### Group 3 Supply-Chain Review — 2026-09-08T17:46:37-03:00

- **Reviewer:** `/root/gemini_group1_supply_chain` using the `genie:supply-chain` lens.
- **Verdict:** `FIX-FIRST`.
- **Target:** WISH SHA-256 `93979abd198bb770769f945d0c5c633ed54c146cc78cea37473f52b36eef01c9`;
  composite implementation SHA-256 `654ee222f05fc7b52c3bbbabbc97b5794c2766654f145fed738a35d60aac12be`.
- **Confirmed:** the same CWD, executable dotenv and mutable-image HIGHs; MEDIUM gaps cover GPG-only signature
  parsing, newline-normalized receipt bytes, weak/default credential checks and count-only matrix binding.
- **Verified foundations:** exact base hash, closed 11-key smoke, full application digest, clean/parent/diff/blob
  checks, argument-vector Git calls, and absent-receipt failure are otherwise fail-closed.

### Group 3 DX and Documentation Review — 2026-09-08T19:08:18-03:00

- **Reviewer:** `/root/gemini_group3_dx_review` using the `genie:dx-docs` lens.
- **Verdict:** `FIX-FIRST`; candidate-readiness is blocked.
- **Target:** WISH SHA-256 `93979abd198bb770769f945d0c5c633ed54c146cc78cea37473f52b36eef01c9`;
  composite implementation SHA-256 `e2ee642004900dbfb3c5277e0e2e5bfe4e17d608d6e335e8c87ed44f96d57a82`.
- **HIGH:** README still documents a production `latest`/checkout-build bypass; the rollout guide is not indexed;
  Operations cannot produce the rigid receipt from one executable how-to; GPG/SSH docs diverge from the checker;
  trusted paths, dotenv, exact bytes/matrix, secret strength, immutable images and endpoint error shape remain open.
- **MEDIUM:** stale OpenAPI/comments and incomplete what/why/next recovery messages.
- **What works:** credential API/UI promotion, fixed endpoint, canonical smoke, Python 3.11+ stdlib checker,
  git-archive/buildx/digest flow and future rehomologation are otherwise aligned.

### Group 3 Documentation File-Scope Amendment — 2026-09-08

- **Proposal:** add exactly `README.md` and `docs/README.md` to Group 3 so the pre-existing mutable-tag production
  recipe is replaced by the authoritative signed-digest entrypoint and the Operations runbook is indexed.
- **Rationale:** leaving a public bypass contradicts the already-approved single-entrypoint criterion; these are
  documentation corrections only, with no new runtime behavior, dependency, trust authority or deployment path.
- **Review requirement:** independent file-scope review must return `SHIP` before fix loop 1 writes either file.

### Group 3 Documentation File-Scope Amendment Review — 2026-09-08T19:21:23-03:00

- **Reviewer:** `/root/review_dependency_closeout` using the plan checklist and questioner lens.
- **Verdict:** `SHIP`; no CRITICAL/HIGH gaps.
- **Exact reviewed WISH SHA-256:** `b6fc2fc84b0ccb1d35ffc08123bbde5a74975f4676f7dcfd19f5b2ec55456d3c`.
- **Validated:** `README.md` is the only public production recipe that still bypasses the signed-digest flow;
  `docs/README.md` is the missing Operations discovery surface; the pair is necessary, sufficient and minimal.
- **MEDIUM validation advisory:** grep the README for removed production build/`:latest` instructions, verify both
  relative links resolve, verify the docs index link, and run `git diff --check`.

### Group 2 Execution Re-review — 2026-09-08T15:34:30-03:00

- **Reviewer:** `/root/review_dependency_closeout`.
- **Verdict:** `SHIP`; no CRITICAL/HIGH gaps after fix loop 1/2.
- **Evidence:** WISH SHA-256 `fda075...`; implementation diff SHA-256 `0780c65...`; focused 45/45 tests,
  TypeScript typecheck, production build, and `git diff --check` passed in the engineer/fixer run.
- **Validated:** stale clinic-A GET/PATCH completions are generation-guarded after every await and cannot mutate
  clinic-B draft, persisted, loading, error, or submit state; out-of-order regressions cover both paths.
- **Residual MEDIUM:** a passive-effect clinic transition can theoretically render the prior clinic briefly before
  activation, but cannot persist or submit stale credentials and is outside the approved acceptance boundary.

### Group 2 Quality Review — 2026-09-08T15:40:37-03:00

- **Reviewer:** `/root/review_dependency_closeout` in a separate review turn.
- **Verdict:** `SHIP`; no CRITICAL/HIGH quality, security, async-correctness, maintainability, performance, or
  regression gaps.
- **Evidence:** focused 45/45 tests, typecheck, production build, and diff check passed; the repository exposes no
  separate frontend lint command. The accepted residual risk is the same non-persistable transient render above.
- **Orchestrator validation:** complete frontend suite passed (`26` files, `243` tests), typecheck passed, production
  build passed, and `git diff --check` passed on 2026-09-08.

### Group 3 Fix Loop 1 Resolution — 2026-09-08

- **Context:** a fix loop closing all HIGH/MEDIUM gaps from the Candidate-readiness Execution Review, the
  Supply-Chain Review, and the DX and Documentation Review (all 2026-09-08) had already landed in the worktree
  by the time this `/genie:work` session resumed; no new production behavior was added beyond what those three
  reviews required. This consumes fix loop 1/2 for Group 3's execution track and 1/1 for its quality track.
- **Resolved:** `scripts/deploy-vps.sh` resolves its own path via `readlink -f` independent of caller CWD and pins
  `PATH`/unsets `DOCKER_HOST*` before any Docker call; `.env` is parsed line-by-line under a strict data-only
  regex and never sourced as Bash; weak/default secrets are rejected with class/length/distinctness checks;
  `scripts/validate_provider_availability_evidence.py` accepts both GPG and SSH signer fingerprints, compares the
  provider matrix and receipt bytes exactly (no counts, no newline normalization), and requires the receipt to be
  a fresh `A` entry absent from the candidate parent; `docker-compose.production.yml` pins postgres/redis/qdrant
  by `@sha256` digest with no `build:`/bind mounts; `README.md`/`docs/README.md`/`docs/api-provider-settings-rollout.md`
  replace the mutable-tag bypass with the signed-digest entrypoint, index the rollout runbook, and give Operations
  one executable how-to matching the checker's exact commands for both GPG and SSH signing;
  `invalid_provider_endpoint` now carries `field=llm_api_url`; the PATCH description reflects the post-promotion
  state.

### Group 3 Execution Re-review — 2026-09-08T21:08:14-03:00

- **Reviewer:** independent reviewer subagent (not the engineer/fixer who applied fix loop 1).
- **Verdict:** `SHIP`; no CRITICAL/HIGH gaps.
- **Validated:** all six HIGH findings and both MEDIUM findings from the Candidate-readiness Execution Review are
  closed with file:line evidence (`deploy-vps.sh:7-12,70-97`, `validate_provider_availability_evidence.py:290-311,
  269-276,338-355,421`, `docker-compose.production.yml:28,44,59`, `src/api/routes/config.py:94,474-479`,
  `src/services/api_credentials.py:262,272,279`); 8 parametrized divergence cases pass
  (`test_git_release_divergences_fail_closed`); README/docs no longer document a bypass and the rollout guide is
  indexed.
- **Orchestrator-run evidence relied upon:** focused suite `99/99` (2 skipped); `make quality` `2835` passed / `16`
  skipped; release checker fails-closed with exactly one error (`Gemini receipt is absent`), the expected
  pre-signing state; frontend `243/243`, typecheck clean, production build passed.

### Group 3 Supply-Chain Re-review — 2026-09-08T21:08:14-03:00

- **Reviewer:** independent reviewer subagent using the `genie:supply-chain` lens (not the engineer/fixer).
- **Verdict:** `SHIP`; no CRITICAL/HIGH gaps, no unmitigated MEDIUM regression, no new gap introduced.
- **Validated:** all three HIGH and four MEDIUM supply-chain findings are closed with file:line evidence; the
  `.env` key allowlist excludes `GEMINI_RECEIPT_SIGNER_FINGERPRINT`/`GEMINI_IMAGE_REPOSITORY`/`GEMINI_API_KEY`/
  `GEMINI_MODEL`/`PATH`/`DOCKER_HOST` so `.env` cannot spoof signer identity or hijack the fingerprint check; the
  data-only regex forbids shell metacharacters that could break Compose variable interpolation; the smoke runs
  `--read-only --tmpfs /tmp` and emits one canonical JSON line; `git archive` builds only from the committed tree,
  guarded by a clean-status check both in the rollout doc and in the checker itself.
- **Accepted residuals (unchanged):** plaintext JSONB storage (Decision 7) and `deploy-vps.sh` not authenticating
  its own bytes before running (explicit owner-accepted trust boundary, Decision 11).

### Group 3 Status — Candidate-ready, release pending Operations — 2026-09-08

- **State:** all Group 3 deliverables that are within agent/repo scope (checker, compose, deploy-gate, docs,
  tests) are implemented, independently reviewed `SHIP` on both execution and supply-chain lenses, and pass every
  automated gate. The release checker correctly fails-closed with a single error because no receipt exists yet.
- **Remaining, explicitly Operations-only (per Decisions 5/10/11 and the Assumptions/Risks table):** freeze the
  candidate commit; build the OCI image once via `git archive` + buildx in the protected environment; run the
  live smoke with real `GEMINI_API_KEY`/`GEMINI_MODEL`; create and sign the receipt-only successor commit as
  `Production Operations Owner`; run `scripts/deploy-vps.sh` against the signed HEAD. None of these can be
  performed by an agent session: they require live provider credentials, OCI registry push access, and
  Operations' signing key.
- **Task tracking:** Group 3's task remains `in_progress` — its acceptance criteria require the signed HEAD state,
  which does not exist yet. It should move to `done` only once Operations completes the release and the final
  signed checkout passes `validate_provider_availability_evidence.py --phase release`.

---

## Files to Create/Modify

```text
README.md
.genie/wishes/gemini-credentials/WISH.md
.genie/wishes/gemini-credentials/evidence/gemini-llm-receipt.md
docs/README.md
docs/configuration.md
docs/api-provider-settings-rollout.md
docs/troubleshooting.md
docker-compose.production.yml
scripts/gemini_llm_smoke.py
scripts/deploy-vps.sh
scripts/validate_provider_availability_evidence.py
src/api/routes/config.py
src/api/routes/rlhf.py
src/api/schemas/config.py
src/llm/client.py
src/llm/gemini_smoke.py
src/services/api_credentials.py
src/services/event_service.py
src/services/safe_egress.py
tests/api/test_api_credentials.py
tests/api/test_middleware_auth.py
tests/llm/test_client.py
tests/llm/test_gemini_smoke.py
tests/services/test_event_service_llm_fallback.py
tests/services/test_event_service_config_cache.py
tests/api/test_rlhf.py
tests/scripts/test_gemini_llm_smoke.py
tests/scripts/test_deploy_vps_gate.py
tests/scripts/test_production_compose.py
tests/scripts/test_validate_provider_availability_evidence.py
tests/services/test_api_credentials.py
tests/services/test_safe_egress.py
frontend/artifacts/neurostack/src/pages/clinic-config.tsx
frontend/artifacts/neurostack/src/pages/clinic-config-api-settings.test.tsx
frontend/artifacts/neurostack/src/locales/en.ts
frontend/artifacts/neurostack/src/locales/pt.ts
```

---
