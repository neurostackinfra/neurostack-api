# Design: Catálogo, ordem e disponibilidade pública das APIs

| Field | Value |
|-------|-------|
| **Slug** | `api-provider-catalog-presentation` |
| **Date** | 2026-09-28 |
| **WRS** | 100/100 |

## Problem

A aba APIs bloqueia quase todos os provedores como `EM BREVE` e apresenta os blocos fora da ordem de
produto solicitada, mas simplesmente promover adapters conhecidos criaria cards tecnicamente
incompletos ou inseguros. O catálogo precisa usar `active` para um ciclo configurável e executável de
ponta a ponta — sem exigir homologação live — com prova específica por integração, compatibilidade
legada e rollback seguro.

## Scope

### IN

- Definir e impor `active` como card editável, bundle validado/persistível, provisionamento público
  completo e runtime determinístico coberto por testes; homologação live não é pré-condição.
- Para WhatsApp, exigir envio **e** recebimento autenticado configuráveis por uma clínica nova; código
  de runtime ou credenciais outbound isoladas não autorizam promoção.
- Manter somente `active` e `coming_soon` como estados públicos e garantir o invariante
  `active ⊆ operational` em teste automatizado.
- Separar três domínios: catálogo público/adotável, IDs wire reconhecidos (incluindo legado oculto) e
  adapters operacionais; sincronizar cada consumidor com o domínio correto.
- Aplicar a matriz, os textos e a ordem exata de blocos/cards definidos neste design.
- Renderizar WhatsApp → LLM → Fallback → Módulo de voz → STT → TTS → Gatilhos de voz.
- Remover os subtítulos apenas dos cards WhatsApp, preservando a descrição do bloco.
- Usar IDs explícitos por capacidade para OpenRouter, preservando configurações legadas `outros`
  sem apresentá-las como se fossem OpenRouter.
- Mapear `llm/openrouter` para o cliente OpenAI-compatible existente com host `openrouter.ai` pinado;
  não criar um novo protocolo de transporte.
- Tratar `llm/outros` e `stt/outros` como IDs legados ocultos: leitura, execução e eco sem mudança
  continuam aceitos, mas nova adoção, entrada ou reentrada neles permanece bloqueada. Durante a
  Fase A, um tenant incumbent que já esteja em `outros` pode usar a única exceção de saída: troca
  atômica para provider explícito operacional, com bundle fresco completo e a mesma política de endpoint.
- Validar provider↔host e troca atômica do bundle antes de qualquer chamada de rede.
- Canonicalizar bases de providers hospedados; OpenRouter usa exatamente
  `https://openrouter.ai/api/v1`. Bases de credenciais não aceitam query, userinfo, fragmento ou
  porta insegura; Evolution/Uazapi podem preservar path prefix público validado.
- Promover um par dependente somente depois que seu wish estiver `SHIPPED`, tiver execution review
  `SHIP`, contrato final e evidência reproduzível reexecutada contra a árvore integrada.
- Exigir registro de evidência para **todo** par ativo derivado da matriz Python, com caminhos mínimos
  por capacidade; membership em allowlist/conjunto operacional não vale como prova de runtime.
- Emitir telemetria operacional estruturada para toda mudança efetiva de
  provider/credencial/endpoint depois do commit e antes da resposta, sem prometer durabilidade ou
  exactly-once; falha/crash é observável por contador e alerta.
- Tratar manifestos como dados não executáveis: IDs de teste validados, nunca comandos livres
  avaliados por shell.
- Definir a saída atômica incumbent-only de `outros`, census agregado sem dados de tenant e
  rollout/backout que preserve leitura/runtime dos IDs já persistidos.
- Bloquear a ampliação de credenciais até existir assurance referenciada de criptografia de banco e
  backups, least privilege e restore/key rotation, ou um wish separado de remediação concluído.
- Acrescentar testes que protejam ordem, labels, bylines, ausência de subtítulos WhatsApp e posição
  dos gatilhos de voz.
- Substituir a antiga exigência de recibo live para promoção por evidência determinística de contrato,
  adapter, egress, runtime e testes; homologação continua registrável separadamente.

### OUT

- Implementar os adapters de OpenRouter/Scribe STT, Dropout fallback, OpenAI TTS ou o ciclo inbound
  autenticado/provisionável dos quatro providers WhatsApp; são wishes dependentes.
- Criar um adapter/SDK de transporte próprio para OpenRouter LLM; o cliente compatível existente será
  reutilizado pelo ID explícito.
- Executar probes live ou declarar qualquer integração homologada/validada pela equipe.
- Registrar webhooks automaticamente nos painéis dos vendors; a superfície entrega callbacks
  copiáveis e o usuário os cadastra no provider.
- Criar um provider Claude/Anthropic direto.
- Suportar toda API arbitrária ou construir um sistema dinâmico de plugins/providers.
- Criar tabela, migration de schema/tabela, endpoint de catálogo, cache, fila, health-check ou
  terceiro estado público.
- Implementar envelope encryption neste wish; se a proteção de infraestrutura não for demonstrada,
  essa remediação pertence a um wish próprio e bloqueia a ativação.
- Alterar a semântica dos gatilhos de voz; apenas sua posição visual muda.

## Approach

O catálogo permanece declarativo e sem endpoint novo, mas uma coleção não pode cumprir três papéis
incompatíveis. O contrato Python passa a declarar explicitamente:

1. `PUBLIC_PROVIDER_AVAILABILITY`: somente pares renderizáveis, com `active|coming_soon`;
2. `LEGACY_PROVIDER_IDS`: `llm/outros` e `stt/outros`, reconhecidos mas ausentes do catálogo;
3. `RECOGNIZED_PROVIDER_IDS = public ∪ legacy`: universo aceito por schema/wire;
4. `OPERATIONAL_PROVIDER_IDS`: adapters/factories realmente executáveis em todas as capacidades.

`ACTIVE_PROVIDER_PAIRS` é sempre derivado da matriz pública. As chaves canônicas de capacidade são
`whatsapp`, `llm`, `multimodal`, `stt` e `tts`; **Fallback** é apenas o label de UI de
`PUBLIC_PROVIDER_AVAILABILITY["multimodal"]`. Os invariantes são `public ∩ legacy = ∅`,
`recognized = public ∪ legacy`, `Literal = recognized`, `active ⊆ operational` e `operational ⊆
recognized`.
O espelho TypeScript equivale apenas ao catálogo público. Assim, `outros` pode sair do grid sem
quebrar GET/runtime/eco idêntico e sem voltar a ser adotável.

A matriz pública fechada da Fase A não impede a saída segura de um incumbent: somente um tenant cujo
bundle corrente já seja `llm/outros` ou `stt/outros` pode trocar atomicamente para um provider
explícito operacional, apresentando bundle fresco completo e passando a mesma política de endpoint
do provider alvo. Nenhum tenant pode entrar ou reentrar em `outros`, e a exceção nunca aceita bundle
parcial, herança de campo ou base insegura. Ela é uma atualização normal do JSONB existente, não uma
migration de schema ou tabela, e pode elevar `new_explicit_id_count` durante a Fase A.

`active` significa disponibilidade técnica de ponta a ponta. Cada par ativo precisa ter bundle
completo, política de endpoint, factory/entrypoint e provas determinísticas específicas. O código
declara um vocabulário fechado `COMMON_EVIDENCE_REQUIREMENTS` e o mapa fechado
`EVIDENCE_REQUIREMENTS_BY_CAPABILITY`; ele não declara uma terceira matriz de providers. O conjunto
esperado é derivado mecanicamente como `(ACTIVE_PROVIDER_PAIRS × requisitos comuns e da capacidade)`.

O registro de proveniência deve ter exatamente essas chaves e mapear cada uma para um ou mais node
IDs pytest coletados, mais SHA-256 dos arquivos-fonte cobertos, SHA do commit integrado e digest do
review da dependência quando houver. O checker rejeita chave ausente ou extra, node ID não coletado,
falho, skipped ou xfailed e requisito cuja evidência seja somente a duplicação de nodes atribuídos a
outro requisito. Testes parametrizados contam por node ID expandido e distinto para cada provider.
Ele roda na árvore integrada limpa, depois dos merges de dependências, com argv fixo versionado no
CI; o manifesto só fornece dados e nunca execução, flags, cwd ou shell.

O vocabulário mínimo e simétrico de provas é:

| Domínio | IDs/behaviors obrigatórios para cada par ativo |
|---|---|
| Comum | PATCH completo com round-trip mascarado; PATCH incompleto e troca sem bundle com zero mutação/cache; endpoint incompatível ou inseguro rejeitado antes de rede; sentinela secreta ausente de erro, log, GET, cache e telemetria |
| WhatsApp | outbound válido; inbound válido autenticado; provisionamento e rotação; auth ausente/inválida/rotacionada; resolução de zero/mais de um tenant; zero DB write/Redis/mídia/egress/evento nos casos rejeitados; falha outbound redigida |
| LLM | request/response pelo factory correto; resposta vazia ou malformada; erro HTTP e de transporte; host spoof, query, userinfo, fragmento e porta rejeitados antes de rede |
| Multimodal/Fallback | falha primária seleciona o provider esperado; contingência textual; áudio bruto byte a byte sem transcode/transformação ou egress alternativo; contenção de falha; erro HTTP/transporte e endpoint hostil antes de rede |
| STT | multipart/encoding correto; transcript parseado; MIME não suportado; resposta vazia/malformada; erro HTTP/transporte; endpoint hostil antes de rede |
| TTS | síntese, voice e bytes de áudio; voice/config ausente; resposta vazia/malformada; erro HTTP/transporte; endpoint hostil antes de rede |

O ciclo WhatsApp completo fica em `whatsapp-inbound-provider-activation`. O origin dos callbacks vem
exclusivamente de `PUBLIC_BASE_URL`, configurado como origin HTTPS público canônico, sem path, query,
userinfo, fragmento ou porta diferente de 443; nenhum host enviado pelo cliente participa da URL.
As rotas fixas são `/v1/webhook/meta`, `/v1/webhook/zapi/{capability_token}`,
`/v1/webhook/evolution/{capability_token}` e `/v1/webhook/uazapi/{capability_token}`.

Meta recebe verify token fornecido pelo usuário como campo write-only e exibe o callback fixo. O GET
de handshake resolve exatamente um tenant pelo verify token. O POST resolve exatamente um tenant
pelo `phone_number_id` e valida `X-Hub-Signature-256` como HMAC-SHA256 dos bytes crus com o app secret
daquele tenant, em comparação constant-time; verify token não autentica POST. Z-API, Evolution e
Uazapi usam tokens capability CSPRNG com pelo menos 256 bits, presentes apenas na URL. Um endpoint
autenticado dedicado `POST /v1/config/api-credentials/webhooks/{provider}/rotate` provisiona a
primeira URL ou a rotaciona, revela a URL uma única vez, persiste somente digest e invalida a anterior
imediatamente.

Cada digest/identidade inbound deve resolver exatamente um tenant antes de qualquer DB write,
Redis, download/mídia, egress ou evento. Ausência, ambiguidade e token velho falham sem efeitos; as
rotas tokenless legadas de Z-API, Evolution e Uazapi ficam inertes. Middleware de borda e aplicação
registram somente o template redigido: capability segments e URI original nunca chegam a logs,
traces, métricas ou headers encaminhados. O catálogo apenas promove os quatro pares depois que essa
dependência estiver concluída e revalidada.

As demais dependências são `stt-openrouter-scribe-activation`, `dropout-multimodal-fallback` e
`tts-openai-provider`; esta última precisa reconciliar a antiga exigência de homologação com a nova
semântica técnica. `llm/openrouter` permanece nesta integração por reutilizar o transporte existente,
mas recebe URL canônica exata, kind de egress próprio e provas de request/Claude model passthrough.

O resolver de endpoint usado no PATCH e novamente no factory/runtime obedece à mesma tabela. As
bases fixas aceitam no máximo uma forma equivalente normalizável, persistem sempre o valor canônico e
não aceitam um endpoint de operação completo; o adapter acrescenta o suffix. Bases self-hosted
preservam um path prefix, mas exigem HTTPS público, porta 443 e DNS/IP pinado, sem query, userinfo ou
fragmento.

| Capacidade/provider | Base persistida | Classe |
|---|---|---|
| WhatsApp Meta Cloud API | `https://graph.facebook.com/v18.0` | fixa, controlada pelo serviço |
| WhatsApp Z-API | `https://api.z-api.io` | fixa, controlada pelo serviço |
| WhatsApp Evolution API | origin HTTPS público + path prefix opcional | self-hosted |
| WhatsApp Uazapi | origin HTTPS público + path prefix opcional | self-hosted |
| LLM GPT/OpenAI | `https://api.openai.com/v1` | fixa |
| LLM Dropout | `https://api.neuralake.cloud/v1` | fixa |
| LLM Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | fixa |
| LLM OpenRouter | `https://openrouter.ai/api/v1` | fixa |
| Multimodal OpenRouter | `https://openrouter.ai/api/v1` | fixa |
| Multimodal Dropout | `https://api.neuralake.cloud/v1` | fixa, confirmada pela dependência antes da promoção |
| STT Whisper/OpenAI | `https://api.openai.com/v1` | fixa |
| STT Scribe/ElevenLabs | `https://api.elevenlabs.io/v1` | fixa, confirmada pela dependência antes da promoção |
| STT OpenRouter | `https://openrouter.ai/api/v1` | fixa |
| TTS ElevenLabs | `https://api.elevenlabs.io/v1` | fixa |
| TTS Google Cloud | `https://texttospeech.googleapis.com/v1` | fixa |
| TTS OpenAI | `https://api.openai.com/v1` | fixa |

PATCH novo sempre rejeita query. Antes da fase pública, um census somente agregado por capacidade e
classe (`safe`, `query`, `userinfo`, `fragment`, `port`) deve provar zero bundles legados `outros` com
base insegura; eles saem por troca atômica ou são remediados antes da liberação. A promessa de
compatibilidade de runtime vale apenas para bundles legados que passaram esse gate, sem exceção
oculta que permita query no caminho legado. O census nunca inclui tenant, URL, host ou segredo.
O census também publica separadamente `unsafe_legacy_count` e `new_explicit_id_count`:
`unsafe_legacy_count == 0` é o gate da Fase B; `new_explicit_id_count > 0` não bloqueia a Fase B e
somente proíbe rollback para backend anterior à Fase A.

Mudanças efetivas em credenciais tentam emitir `api_credentials_changed` depois do commit e antes de
concluir a request. Trata-se de telemetria operacional **at-most-once**, não trilha durável: existe
janela de crash entre commit e emissão. O payload usa ator, tenant, capabilities/campos alterados,
provider anterior/novo e `endpoint_class=fixed|self_hosted`; nem hostname nem digest reversível, URL,
query, path, modelo, valor ou segredo entra no evento. Falha de emissão incrementa contador dedicado
e alerta; 4xx, validação, no-op e rollback não emitem. Se auditoria durável/exactly-once se tornar
requisito, ela exige outbox/storage em wish próprio e não pode ser inferida deste evento.

A proteção em repouso é gate externo fail-closed. A release referencia um artefato imutável, com
ambiente e fingerprint do deployment, owner e approver, data com no máximo 90 dias, evidência de
criptografia do banco e backups, provedor/owner da chave e política/data de rotação, least privilege
das identidades de aplicação/backup e resultado/data de restore rehearsal. Referência ausente,
vencida ou de outro ambiente bloqueia a fase pública até `api-credentials-at-rest-protection` estar
`SHIPPED` e revisado `SHIP`.

O rollout tem duas unidades independentes. **Fase A — compatibility:** a matriz pública fica congelada
em exatamente `1 active + 21 coming_soon`; backend reconhece e executa IDs novos e legados seguros,
factories e readers entram, cards/nova adoção continuam fechados, e somente a saída atômica
incumbent-only de `outros` pode persistir ID explícito novo. **Fase B — activation:** depois de
dependências, assurance, `unsafe_legacy_count == 0` e checker final, entram exatamente
`16 active + 6 coming_soon` na matriz pública e no frontend no mesmo release unit.
`new_explicit_id_count > 0` nunca bloqueia B; apenas torna proibido rollback para backend anterior à
Fase A. O backout da Fase B oculta cards e fecha nova adoção, mas preserva a Fase A (schema, GET,
runtime, eco e leitura dos IDs explícitos já persistidos).

### Matriz e ordem alvo

| Bloco | Ordem visual | `active` final | `coming_soon` final | Gate adicional |
|---|---|---|---|---|
| WhatsApp | Meta Cloud API; 360dialog; Twilio; Z-API; Evolution API; Uazapi | Meta Cloud API, Z-API, Evolution API, Uazapi | 360dialog, Twilio | `whatsapp-inbound-provider-activation` + provas outbound/inbound por par |
| LLM | Cortex by NeuroStack; Dropout by Neuralake; GPT by OpenAI; Gemini by Google; Outros by OpenRouter | Dropout, GPT, Gemini, Outros/OpenRouter | Cortex | prova por par; OpenRouter com base canônica |
| Fallback | Outros by OpenRouter; Dropout by Neuralake | ambos | nenhum | texto + áudio bruto por provider; Dropout depende de wish |
| STT | Whisper by OpenAI; Scribe by ElevenLabs; Outros by OpenRouter; Deepgram STT by Deepgram; Azure STT by Microsoft; Cloud STT by Google | Whisper, Scribe e Outros/OpenRouter | Deepgram, Azure e Cloud STT | prova direta Whisper + wish Scribe/OpenRouter |
| TTS | ElevenLabs; Google Cloud TTS; OpenAI | todos | nenhum | prova por par; OpenAI depende de wish reconciliado |

Os cards WhatsApp mostram somente os títulos. LLM, Fallback e STT mostram exatamente as bylines da
tabela. Fallback e TTS não exibem nomes promocionais de modelos.

Essa é a matriz final pretendida, não autorização para promoção parcial: qualquer par sem seu gate
fica `coming_soon` no artefato executável até a evidência correspondente existir.

### Alternativas rejeitadas

- **Apenas virar os badges:** recriaria configuração morta para providers sem adapter.
- **Usar `active ⊆ operational` como única prova:** membership declarativo não demonstra request,
  parsing, inbound autenticado ou síntese; cada par precisa de evidência nomeada.
- **Implementar hardening WhatsApp dentro do catálogo:** mistura webhook/security com apresentação e
  credenciais; a dependência dedicada é testável e pode ser revisada isoladamente.
- **Estado público `beta/configurável`:** adicionaria máquina de estado, copy, PATCH e testes sem
  necessidade; “não homologado” é metadado operacional, não disponibilidade técnica.
- **Renomear o ID genérico `outros`:** mentiria para bundles legados que apontam a outros hosts e
  impediria métricas/egress específicos do OpenRouter.
- **Claude direto:** duplicaria credencial, protocolo e adapter; Claude já pode ser escolhido pelo ID
  de modelo no OpenRouter.
- **Executar comandos gravados no manifesto:** cria uma fronteira de command injection; o registro é
  somente dados e os comandos permanecem fixos no checker/CI.
- **Rollback para backend antigo após adoção:** pode tornar IDs persistidos ilegíveis; o backout deve
  ser forward-compatible e fechar somente novas adoções.

## Simplicity Case

- **Simplest complete design:** reusar a matriz de dois estados, o JSONB e o endpoint dedicado atuais;
  separar os três domínios já existentes em conceitos explícitos, integrar dependências concluídas,
  manter um único registro de provas sem estados e ajustar listas, copy e ordem.
- **Added machinery:** IDs explícitos, dependência WhatsApp, registro por requisito/par, URL canônica,
  telemetria redigida e runbook de rollout/backout. Cada item fecha um defeito observado: byline/egress incorretos,
  inbound sem autenticação/provisionamento, promoção sem prova, query vazando, falta de atribuição e
  incompatibilidade após persistir novos IDs.
- **Deferred until measured:** Claude direto só será reconsiderado quando cliente ou parceria exigir
  chave/contrato Anthropic direto; catálogo dinâmico só quando existir um segundo provider não
  OpenAI-compatible que não possa passar pelo OpenRouter. Envelope encryption fica em wish próprio
  somente se a assurance de infraestrutura não satisfizer o gate atual.
- **Complexity removed:** nenhum terceiro estado, endpoint de catálogo, migration de schema/tabela, auto-discovery,
  health-check, auto-registro de webhook, comando livre em manifesto ou acoplamento entre homologação
  live e disponibilidade pública.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | `active` exige configuração pública + runtime provado, não homologação live | Evita configuração morta sem confundir disponibilidade com teste externo. |
| 2 | Catálogo público, IDs reconhecidos e adapters operacionais são domínios distintos | O legado precisa ser legível sem reaparecer ou se tornar adotável. |
| 3 | WhatsApp só ativa com envio e inbound autenticado/provisionável | A capacidade promete receber e enviar; outbound isolado é incompleto. |
| 4 | Hardening WhatsApp é uma dependência própria | Webhook/security não cabe num wish de integração visual e contratual. |
| 5 | Todo par ativo cobre o vocabulário fechado de evidência da capacidade | Allowlist e suíte agregada não provam que o adapter certo executa. |
| 6 | Todo fallback ativo prova texto e áudio bruto | O card alimenta as duas contingências; Dropout não admite suporte parcial. |
| 7 | OpenRouter usa ID próprio e base canônica exata | Garante host/path, telemetria e byline honestos sem transporte novo. |
| 8 | PATCH não aceita query e ativação exige zero legado inseguro | Impede endpoint quebrado e segredo em GET/cache/log sem abrir exceção perigosa no runtime legado. |
| 9 | Mudanças efetivas tentam telemetria redigida at-most-once pós-commit | Traz atribuição operacional sem alegar a durabilidade impossível sem outbox. |
| 10 | Manifestos nunca são executáveis | IDs de teste como dados evitam command injection; argv fica no plano/CI. |
| 11 | `outros` oculto aceita GET/runtime/eco e saída atômica para provider explícito | Preserva incumbentes e oferece saída sem nova adoção genérica. |
| 12 | Rollout usa Fase A compatibility e Fase B activation | O backout pode fechar adoção sem retirar readers/factories necessários aos IDs persistidos. |
| 13 | Ampliação depende de assurance imutável e vinculada ao ambiente | Mais providers ampliam o impacto do risco plaintext já conhecido. |
| 14 | Continuam somente dois estados públicos | Evita reintroduzir `configuration_only` sob outro nome. |
| 15 | Claude permanece via OpenRouter | Atende o uso atual sem protocolo/segredo Anthropic adicional. |
| 16 | Python segue autoritativo e TypeScript espelhado | Um endpoint de catálogo continua injustificado; invariantes distinguem os domínios. |
| 17 | Ordem e posição dos gatilhos seguem a referência solicitada | Corrige apresentação sem alterar semântica funcional. |
| 18 | `multimodal` é a chave canônica de Fallback | Evita dois nomes de capacidade divergirem entre backend, evidência e UI. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Meta/Z-API/Evolution/Uazapi ativarem sem inbound seguro e provisionável | High | Dependência WhatsApp dedicada; provas outbound/inbound/config por par antes da promoção. |
| 2 | Membership ser confundido com runtime | High | Registro derivado por requisito/par e reexecução de node IDs coletados contra a árvore integrada. |
| 3 | Dropout funcionar para texto mas não áudio | High | Categoria fallback exige duas provas independentes; ausência mantém `coming_soon`. |
| 4 | Remover `outros` da matriz quebrar wire/eco legado | High | Domínios separados e invariantes `recognized = public ∪ legacy`. |
| 5 | URL fixa com host correto e path/query errado persistir | High | Canonicalização exata antes do save e revalidação fail-closed no factory. |
| 6 | Query carregar segredo para GET/cache/log, inclusive em legado | High | Query proibida em PATCH, census legado inseguro igual a zero e testes com sentinela em todas as fronteiras. |
| 7 | Telemetria vazar segredo, duplicar ou ser confundida com auditoria durável | High | Evento at-most-once pós-commit, payload allowlisted, contador/alerta de falha e janela de crash documentada. |
| 8 | Manifesto executar input controlável | High | Schema de IDs fixos; checker nunca usa shell/eval nem argv do artefato. |
| 9 | Banco/backups sem proteção adequada ampliarem blast radius | High | Artefato imutável, recente e ligado ao ambiente, ou `api-credentials-at-rest-protection` antes da adoção pública. |
| 10 | Rollback para backend antigo não reconhecer IDs novos | High | Fases A/B e backout forward-compatible; rollback anterior à A só com `new_explicit_id_count == 0`. |
| 11 | Migração de `outros` ficar invisível ao usuário | Medium | Alerta de configuração legada e troca atômica com bundle fresco para provider explícito. |
| 12 | Matriz Python/TypeScript ou ordem visual divergirem | Medium | Checker por domínio e testes de sequência sem `.sort()`. |
| 13 | “Ativo” ser lido como homologado | Medium | Copy/documentação separam disponibilidade técnica de homologação externa. |
| 14 | Wishes dependentes ainda não existirem ou não concluírem | High | Wish permanece aprovado porém não executável até todos os gates verificáveis passarem. |

## Success Criteria

- [ ] A sequência renderizada é exatamente WhatsApp, LLM, Fallback, Módulo de voz, STT, TTS e
  Gatilhos de voz.
- [ ] Cada lista de cards tem exatamente a ordem, o título e a byline da matriz alvo; WhatsApp não
  renderiza subtítulos e mantém a descrição do bloco.
- [ ] TypeScript equivale ao catálogo público; Literals/IDs wire equivalem a `public ∪ legacy`;
  `active ⊆ operational`; legado não aparece no catálogo.
- [ ] `Fallback` deriva de `PUBLIC_PROVIDER_AVAILABILITY["multimodal"]`; não existe chave paralela
  `fallback` em schema, runtime ou registro de evidência.
- [ ] Cada um dos 16 pares finais `active` cobre todos os requisitos comuns e específicos da sua
  capacidade; o checker rejeita chave ausente/extra e node não coletado, falho, skipped, xfailed ou
  apenas duplicado, e reexecuta os IDs contra o commit integrado e os source digests declarados.
- [ ] Todo par `active` abre o editor, aceita bundle completo e valida provider↔endpoint antes de rede.
- [ ] Todo par `coming_soon` permanece visível, inerte e rejeitado atomicamente pelo PATCH para nova
  adoção; a única exceção é a saída incumbent-only de `llm/outros`/`stt/outros` para provider
  explícito operacional com bundle fresco completo durante a Fase A.
- [ ] `llm/openrouter` e `stt/openrouter` usam IDs explícitos; `outros` legado continua reconhecido sem
  ser apresentado como OpenRouter, não aceita adoção/manutenção e pode sair por troca atômica para
  provider explícito.
- [ ] Meta, Z-API, Evolution e Uazapi só são promovidos após configuração pública de outbound e
  inbound autenticado: Meta verifica HMAC dos bytes crus; os demais usam capability token >=256 bits
  com revelação única, digest e rotação; todas as rotas tokenless legadas são inertes.
- [ ] Identidade/digest inbound resolve exatamente um tenant antes de DB write, Redis, mídia, egress
  ou evento, e capability URLs aparecem apenas como templates redigidos em toda fronteira de log.
- [ ] Fallback OpenRouter e Dropout têm provas separadas de contingência textual e áudio bruto
  WhatsApp; Dropout permanece `coming_soon` se qualquer uma faltar.
- [ ] Cada provider hospedado persiste exatamente a base canônica da tabela; bases self-hosted aceitam
  path prefix público, mas nenhuma base aceita query, userinfo, fragmento ou porta diferente de 443,
  e PATCH/factory aplicam o mesmo resolver antes da rede.
- [ ] O census agregado prova `unsafe_legacy_count == 0` antes da Fase B, sem registrar tenant, URL,
  host ou segredo; `new_explicit_id_count > 0` somente proíbe rollback anterior à Fase A.
- [ ] Mudança efetiva tenta no máximo um evento pós-commit redigido antes da resposta; 4xx, rollback e
  no-op não emitem, falha incrementa contador/alerta e nenhuma sentinela aparece em GET, cache, logs
  ou telemetria. A documentação não o apresenta como auditoria durável/exactly-once.
- [ ] Manifestos contêm somente IDs/digests/proveniência validados e nenhum conteúdo controla shell,
  executável, flags ou cwd.
- [ ] O runbook congela Fase A em `1 active + 21 coming_soon` e Fase B em
  `16 active + 6 coming_soon`; backout da B preserva A e rollback anterior à A exige
  `new_explicit_id_count == 0`.
- [ ] A release referencia assurance imutável, com no máximo 90 dias e vinculada ao ambiente, cobrindo
  criptografia de banco/backups, owner/rotação de chave, least privilege e restore rehearsal; se ela
  faltar, divergir ou vencer, `api-credentials-at-rest-protection` bloqueia a fase B.
- [ ] Claude é configurável por model ID no card OpenRouter sem provider Anthropic direto.
- [ ] OpenAI TTS só é promovido depois que `tts-openai-provider` estiver executado e operacional.
- [ ] A política de evidência aceita promoção baseada em contrato/testes determinísticos e não exige
  probe live; nenhum artefato afirma homologação não realizada.
- [ ] Não há migration de schema/tabela, endpoint de catálogo, terceiro estado ou mudança funcional
  nos gatilhos.

## Next Step

Após nova revisão independente SHIP, persistir a evidência abaixo, verificar o digest e revisar o
wish de integração com os gates de WhatsApp, evidência por par, segurança e rollback.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `025c397a0af4335e5266a3c19230d8a8a0ef405b818991a02ee4d1a9adc529f3`
- **Reviewer:** /root/official_design_rereview_loop2
- **Reviewed at:** 2026-09-29T00:20:16.000Z
<!-- genie-design-review:end -->
