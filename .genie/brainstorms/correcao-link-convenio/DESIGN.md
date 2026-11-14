# Design: Correção do link de agendamento e da triagem de convênio

| Field | Value |
|-------|-------|
| **Slug** | `correcao-link-convenio` |
| **Date** | 2026-11-13 |
| **WRS** | 100/100 |
| **Guarda-chuva** | [subagente-agendamento](../subagente-agendamento/DRAFT.md) (etapa 0) |
| **Draft** | [DRAFT.md](DRAFT.md) |

## Problem

Dois defeitos em produção quebram o que a configuração da clínica promete. O link de agendamento ligado em `/config` nunca chega ao paciente, porque o carregador de configuração usado nos turnos reais não traz o bloco `scheduling`. Além disso, a pergunta "convênio ou particular?", que manda segurar preços, aparece em clínica que não aceita convênio, porque a pipeline trata a string `"no"` como verdadeira. Os dois precisam estar certos antes da página de subagentes ir ao ar (ela descreve o envio do link como comportamento de hoje) e antes de qualquer marcação automática. Ligar o link de verdade também expõe um risco novo: a mensagem do link é texto livre da clínica e vai ao paciente sem passar pela trava de agendamento, então a correção inclui uma checagem dessa mensagem ao salvar (decisão do owner em 2026-11-13: "Checar ao salvar").

## Scope

### IN

- **Defeito 1, carregador.** `_load_config_from_db` (`src/services/event_service.py:421-485`) passa a devolver `"scheduling": row.scheduling or {}` junto com os outros buckets.
- **Defeito 1, testes pelo carregador real.**
  - Teste unitário de `_load_config_from_db` com `src.repositories.get_db_session` trocado por uma sessão falsa que devolve uma linha com `scheduling` preenchido e `api_credentials` com `llm_provider`, para não buscar o builder. Ele verifica que o dict devolvido traz `scheduling` igual ao gravado, e `{}` quando a coluna é `None`.
  - Teste de contrato pela costura real: `EventService._load_clinic_config` (que os turnos usam) com a sessão falsa e um Redis falso, uma vez com o cache vazio e outra lendo o payload gravado (ida e volta em JSON). O teste confirma que o resultado não é `None` (o `except` amplo do carregador devolve `None` em silêncio) e o passa como `clinic_config` ao `SRMEngine`. Com o link ligado e URL, `flags={"handoff_confirmation_pending": True}` e a mensagem "sim, pode encaminhar" (forma de `tests/srm/test_engine.py:1066-1083`), a decisão sai com `handoff_verification_status == "link_sent"`. Com o link desligado ou sem URL, não sai.
  - Os dois testes falham em `origin/main` e passam com a correção.
- **Defeito 2, regra única no lado da API.** Nova função pura `clinic_accepts_insurance(value: object) -> bool` em `src/services/price_disclosure_policy.py`, com a mesma regra de `resolve_price_disclosure(...).insurance_active` do núcleo (`packages/neurostack-core/neurostack_core/prompt/price_policy.py:59-64`): falso para vazio e para `"no"`, `"false"`, `"0"`, `"none"` (sem diferenciar maiúsculas). `src/core/pipeline.py:943` passa a usar `clinic_accepts_insurance(custom_vars.get("accepts_insurance"))` no lugar de `bool(...)`.
- **Defeito 2, testes.**
  - Teste de paridade sobre a matriz `True, False, None, "", "yes", "no", "No", "NO", " no ", "false", "False", "0", "none", "None", "não", "off", "sim", 1, 0` (os valores com espaço, acento e "off" pegam uma cópia que "melhore" a regra sozinha): para cada valor, `clinic_accepts_insurance(v) == resolve_price_disclosure({"accepts_insurance": v}).insurance_active`. O teste importa a regra direto do módulo do núcleo.
  - Teste da pipeline: com `business_rules.accepts_insurance = "no"`, no turno 2 e com intenção `agendar`, `custom_vars["ask_convenio_triage"]` é falso; com `"yes"` (e modalidade indefinida) é verdadeiro. O caso `"no"` falha em `origin/main`.
  - O espelho da regra em `tests/srm/test_engine.py:893-907` (`_compute_gating`, que copia o `bool(...)`) passa a usar `clinic_accepts_insurance`. É a única edição permitida em teste existente.
- **Checagem da mensagem do link ao salvar.** No `PATCH /v1/config` (`src/api/routes/config.py`, na revalidação do bucket `scheduling`, ~705-735), quando a requisição traz `scheduling_link_message` com um valor diferente do gravado, o texto passa por `detect_scheduling_violation` (`src/observability/scheduling_gate.py:270-291`, o mesmo detector da trava, PT e EN). Se o detector apontar uma promessa (consulta marcada ou confirmada, horário livre, cancelamento ou remarcação feitos), a rota responde 422 e não grava nada. O `detail` vem no formato de erro do pydantic: `[{"loc": ["body", "scheduling_link_message"], "msg": "...", "type": "scheduling_link_message_promise"}]`. Com isso, a tela destaca o campo pelo tradutor que já existe (`configSaveErrorFieldNames`, `clinic-config.tsx:191-216`, rótulo em `:128`).
  - Mensagem igual à gravada, vazia ou `None` não é checada. Uma mensagem antiga com promessa não impede salvar os outros campos; quem cobre essas é o inventário.
  - O detector é fail-open por desenho (devolve `None` num erro interno). Nesse caso a mensagem é aceita, como a trava faz com as respostas.
- **Aviso fixo na tela.** Embaixo do campo da mensagem do link (`data-testid="input-scheduling-link-message"`, `clinic-config.tsx:2314-2323`), um texto de ajuda pt/en: "A mensagem não pode dizer que a consulta está marcada ou confirmada, nem que há horário livre. Quem confere o agendamento é a equipe." / "The message must not say the appointment is booked or confirmed, or that a time is available. The team checks the booking."
- **Testes da checagem.**
  - Rota: uma mensagem nova com promessa (pt e en) recebe 422 com `loc` terminando em `scheduling_link_message` e nada é gravado. A mensagem padrão ("Para concluir seu agendamento, acesse o link abaixo:") e uma neutra ("Clique no link para escolher o melhor horário para você:") são aceitas. Com uma mensagem com promessa já gravada e enviada de novo sem mudança, junto com outro campo alterado, a resposta é 200.
  - Tela (vitest): o aviso aparece em pt e en com o link ligado, e um 422 com esse `loc` mostra o rótulo "mensagem do link de agendamento" no erro.
- **Inventário antes de cada deploy (só leitura, rodado pelo owner).** O wish traz uma consulta SQL só de leitura sobre `clinic_configs` que lista, por clínica: `tenant_id`, `identity->>'clinic_name'`, o valor cru de `scheduling->'scheduling_link_enabled'`, `scheduling_link_url` e `scheduling_link_message`. Ela pega toda linha que tenha URL preenchida, não só as que estão com o link ligado. Roda no banco de cada ambiente antes do deploy daquele ambiente: o serviço de staging (que é a plataforma real, chamado de "production" pelo tooling do repo) e o `neurostack-prod` (`app.neurostack.global`). O owner confere as mensagens: nenhuma pode dizer que a consulta está marcada ou confirmada, nem que há horário livre.
- **Roteiro de QA ao vivo** numa clínica de teste, no primeiro ambiente que receber o deploy, pelo WhatsApp real:
  1. link ligado: o paciente pede para agendar, confirma, recebe o link (mensagem da clínica + URL), e o handoff pendente aparece na fila;
  2. no turno seguinte, "consegui": o handoff é enriquecido com `concluiu` e a IA pausa (a clínica de teste tem a pausa por handoff ativa, que vem de `resolve_handoff_pause`);
  3. em outra conversa, "não consegui": `nao_concluiu`;
  4. um pedido de cancelamento: não recebe link e segue o handoff normal;
  5. com o link desligado: o fluxo de hoje;
  6. numa clínica sem convênio: nenhuma pergunta de modalidade;
  7. em `/config`, salvar a mensagem "Seu agendamento está confirmado, acesse:" é recusado com o campo destacado, e a mensagem padrão salva normalmente.

### OUT

- Mudar o formato gravado de `accepts_insurance` (string `"yes"`/`"no"` vinda da tela), migrar dados ou revalidar `business_rules` no PATCH.
- Trocar a cópia equivalente da regra em `src/services/event_service.py:2195-2201`, que já trata `"no"` certo, pela função nova. Fica como limpeza futura.
- Exportar `resolve_price_disclosure` no pacote do núcleo (a fachada pública é travada por `tests/baselines/neurostack-core-public-facade.json`).
- Devolver `post_care` no carregador: não tem leitor no runtime.
- Trocar a versão do cache de config (`CONFIG_CACHE_SCHEMA_VERSION`).
- Qualquer mudança no fluxo do link em si (SRM, `handoff_decider`, mensagem, verificação), nos prompts ou nos gates.
- Deploy e consultas no banco de qualquer ambiente: são cerimônia do owner. O agente escreve a consulta, não a roda.
- Passar a mensagem do link pelo `scheduling_gate` na hora do envio (o gate ignora resposta determinística por decisão de `scheduling-agent-guardrails`). A checagem acontece ao salvar.
- Validar a mensagem dentro do modelo `ClinicScheduling`: o modelo também valida leituras (`clinic_config_table.py:139`), e uma mensagem antiga com promessa quebraria a leitura da config daquela clínica.
- Evento de auditoria para mudança da mensagem (hoje só a URL tem, `config.py:740-750`).
- Qualquer outra mudança de frontend além do aviso fixo e do teste.

## Approach

São duas correções de uma linha cada, com testes que passam pelos caminhos reais que hoje ficam sem cobertura.

No defeito 1, o carregador ganha a chave que falta, do mesmo jeito que `lgpd` ganhou no commit `89c70deb`. O teste novo não troca o carregador por um fake: ele substitui só a sessão de banco, então a lista de buckets devolvidos fica testada. O teste de contrato liga a saída do carregador à entrada do SRM, que é a costura onde o defeito estava escondido.

No defeito 2, a pipeline passa a usar uma função com a mesma regra que o template já usa para os preços. Assim, a pergunta de triagem e a retenção de preço concordam sobre o que é "clínica com convênio".

Alternativas consideradas:

- **Revalidar `business_rules` no PATCH com `ClinicBusinessRules` (que tipa `accepts_insurance: bool`).** Perdeu: não corrige o que já está gravado como `"no"` e mexe no contrato de escrita da tela. A leitura precisa tolerar a string de qualquer jeito.
- **Importar `resolve_price_disclosure` do submódulo do núcleo direto na pipeline.** Perdeu: `src` não importa submódulos do núcleo hoje, e a fachada pública não exporta essa função. A paridade fica garantida pelo teste, sem mudar o contrato do núcleo.
- **Exportar a função no núcleo.** Perdeu: muda a fachada pública travada e o pacote do núcleo para uma correção de uma linha na API.

## Simplicity Case

- **Simplest complete design:** uma chave a mais num dict literal e uma chamada de função no lugar de `bool(...)`, cada uma com o teste que reproduz o defeito.
- **Added machinery:**
  - A função `clinic_accepts_insurance`, de 3 linhas. Ela existe porque a regra certa mora fora da fachada pública do núcleo. O teste de paridade impede que as duas cópias divirjam.
  - A checagem da mensagem ao salvar, reaproveitando o detector que já existe. Ela existe porque ligar o link de verdade passa a mandar ao paciente um texto livre da clínica sem trava, e a regra de agenda é de tolerância zero (decisão do owner).
- **Deferred until measured:** unificar a cópia de `event_service.py:2195-2201` (gatilho: qualquer mudança na lista de valores "desligado"); devolver `post_care` (gatilho: primeiro leitor no runtime); revalidar `business_rules` no PATCH (gatilho: um terceiro valor aparecer na tela); auditoria de mudança da mensagem (gatilho: o owner pedir rastreio de quem mudou o texto).
- **Complexity removed:** nenhuma flag de rollback, nenhuma migração, nenhuma troca de versão de cache. O rollback do link por clínica já existe: desligar em `/config`.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Carregador devolve `scheduling` | É o bucket que o SRM e a pipeline leem (`srm/engine.py:166`, `pipeline.py:1186`) e que a tela já grava validado. Mesma correção feita para `lgpd` (`89c70deb`). |
| 2 | Teste pelo carregador real, trocando só a sessão de banco | O defeito sobreviveu porque todos os testes injetavam `clinic_config` ou trocavam o carregador. O teste precisa exercitar a lista de buckets. |
| 3 | Teste de contrato carregador → SRM | A costura entre o dict carregado e o `clinic_config` lido pelo SRM é onde o defeito se escondeu. |
| 4 | Sem troca de versão do cache | Até 15 min depois do deploy (TTL 900 s), o payload antigo segue sem `scheduling`, que é o comportamento de hoje. Depois disso o cache é recarregado com a chave. |
| 5 | `clinic_accepts_insurance` em `price_disclosure_policy.py`, com paridade testada contra o núcleo | É o módulo que já junta a config da clínica com a regra de preço do núcleo. Evita mexer na fachada pública do núcleo. |
| 6 | Sem flag nova | Rollback do defeito 1 por clínica = desligar o link. O defeito 2 só remove uma pergunta indevida. |
| 7 | Correção num branch próprio a partir de `origin/main`, num PR separado da página | Regra do repo: correção incidental não vai no branch de outro wish. |
| 8 | A mensagem do link é checada ao salvar, com o mesmo detector da trava, e só quando muda; o estoque já gravado é coberto pelo inventário | Escolha do owner ("Checar ao salvar"). O gate na hora do envio continua ignorando resposta determinística (`scheduling-agent-guardrails`, decisão 5). Checar no modelo quebraria leituras. Checar só a mudança evita travar a clínica que tem uma mensagem antiga e precisa salvar outro campo. |
| 9 | Inventário em cada ambiente, antes do deploy daquele ambiente | O primeiro deploy em qualquer um dos dois ambientes (o serviço de staging, chamado de "production" pelo tooling, e o `neurostack-prod`) já liga o fluxo de verdade ali. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | O fluxo do link nunca rodou em produção. Ao corrigir, ele passa a rodar de verdade para toda clínica com o link ligado: envia o link, abre handoff pendente e pausa a IA no turno seguinte. O turno de enriquecimento faz commit em sessão separada e chama o webhook de handoff dentro da fase 1 (`src/services/handoff_service.py:836-905`), e um erro ali derruba o turno inteiro (`src/core/pipeline.py:577-598`). Esse caminho só foi testado com config injetada | High | Roteiro de QA ao vivo (Scope) no primeiro ambiente, antes do segundo. Inventário antes de cada deploy. Rollback por clínica: desligar o link, que invalida o cache na hora (`src/api/routes/config.py:767-769`). |
| 2 | Clínicas que ligaram o link há tempo podem ter URL esquecida ou velha | Medium | O inventário lista URL e mensagem. O owner decide avisar as clínicas antes do deploy. |
| 2a | A mensagem do link é texto livre da clínica (`ClinicScheduling.scheduling_link_message`) e sai ao paciente sem passar pelo `scheduling_gate` (`event_service.py:2210-2217`). Uma mensagem como "seu horário está confirmado" quebraria a regra de tolerância zero | High | Mensagens novas ou alteradas passam pelo detector ao salvar. As já gravadas aparecem no inventário, que o owner confere antes de cada deploy. Sobra o que o detector não pega (o regex tem lacunas conhecidas, como "Consulta marcada!" sem verbo): o owner aceita esse resíduo, que é o mesmo da trava nas respostas. |
| 2b | Uma linha antiga pode ter `scheduling_link_enabled` gravado como string; `srm/engine.py:167` aplica `bool(...)`, e uma string `"no"` ligaria o link | Low | O PATCH valida o bucket com `ClinicScheduling` (bool) desde o wish do link, mas o inventário mostra o valor cru e pega qualquer linha fora do padrão. |
| 3 | Clínicas sem convênio deixam de ouvir a pergunta de modalidade e passam a receber preço conforme a configuração de preços | Low | É o comportamento configurado. A retenção de preço já usa a regra correta (`resolve_price_disclosure`). Só a pergunta indevida some. |
| 4 | O venv local aponta o `neurostack_core` para um snapshot de 2026-11-08, sem `prompt/price_policy.py` | Low | Rodar os testes com `PYTHONPATH=<worktree>/packages/neurostack-core`. É a receita já usada no repo. |
| 5 | Duas cópias da regra "convênio desligado" no lado da API (função nova e `event_service.py:2195-2201`) | Low | A função nova tem paridade testada com o núcleo. A unificação fica em OUT com gatilho. |
| 6 | Relógio do WSL pula para trás, e testes com tempo ficam instáveis | Low | Os testes novos não usam tempo. Uma falha nova na suíte completa só é atribuída à mudança depois de rodar o teste 8 vezes no worktree e 8 vezes num worktree limpo em `origin/main`. |
| 7 | Depois de um rollback de imagem, o cache pode manter `scheduling` por até 15 min | Low | Desligar o link da clínica invalida o cache na hora. |
| 8 | Falso positivo do detector recusa uma mensagem legítima | Low | A mensagem padrão e uma neutra entram nos testes de aceite. A clínica pode usar a padrão. Se aparecer falso positivo, o ajuste é no detector, que serve às duas checagens. |
| 9 | `scripts/wish-gates.sh:60-118` trata `tests/srm` como diretório congelado de "parity oracle" | Low | Nem o CI nem `make quality` aplicam isso, e o PR #70 já editou `tests/srm`. A edição do espelho `_compute_gating` é permitida e fica registrada no wish. |

## Success Criteria

- [ ] Com a sessão de banco falsa, `_load_config_from_db` devolve `scheduling` igual ao gravado, e `{}` quando a coluna é `None`. O teste falha em `origin/main`.
- [ ] O config devolvido por `EventService._load_clinic_config`, no cache vazio e no cache gravado, não é `None` e leva o `SRMEngine` a `handoff_verification_status == "link_sent"` na forma de turno do Scope, com o link ligado e URL. Com o link desligado ou sem URL, não leva. O caso ligado falha em `origin/main`.
- [ ] `clinic_accepts_insurance(v)` é igual a `resolve_price_disclosure({"accepts_insurance": v}).insurance_active` para toda a matriz de valores do Scope.
- [ ] Com `accepts_insurance = "no"`, `ask_convenio_triage` é falso no turno 2 e com intenção `agendar`. Com `"yes"` e modalidade indefinida, é verdadeiro. O caso `"no"` falha em `origin/main`.
- [ ] Os testes já existentes do link, da triagem, do cache de config e dos gates passam. A única edição permitida é o espelho `_compute_gating` em `tests/srm/test_engine.py:893-907`.
- [ ] `PATCH /v1/config` com uma mensagem de link nova com promessa (pt e en) responde 422, com `loc` terminando em `scheduling_link_message`, e não grava nada. A mensagem padrão e a neutra do Scope são aceitas. Uma mensagem com promessa já gravada, reenviada sem mudança junto com outro campo alterado, responde 200.
- [ ] A tela mostra o aviso fixo embaixo do campo da mensagem (pt e en), e um 422 com esse `loc` mostra o rótulo "mensagem do link de agendamento" no erro.
- [ ] O wish traz a consulta SQL do inventário (só leitura, só `SELECT`) e o roteiro de QA ao vivo com os 7 casos do Scope. O owner roda os dois; o agente não roda nada em banco de ambiente.
- [ ] Os quatro gates de `make quality` rodam separados, com o venv canônico `/home/lorenzo/.cache/pypoetry/virtualenvs/neurostack-api-dYUOnVPp-py3.12` (um `poetry run` em worktree novo cria um venv vazio) e `PYTHONPATH=<worktree>/packages/neurostack-core`. São eles: `black --check` + `isort --check-only`, `flake8`, `mypy` (mesmos alvos do `Makefile:122-135`) e `pytest tests/`. Cada um é medido no worktree antes da primeira edição. No final, todo gate que saiu 0 no baseline sai 0; os que já falhavam não ganham falha nova (para o pytest: nenhuma linha `^(FAILED|ERROR)` fora do baseline e contagem de aprovados que não diminui).
- [ ] O diff (fora de `.genie/`) só toca `src/services/event_service.py`, `src/services/price_disclosure_policy.py`, `src/core/pipeline.py`, `src/api/routes/config.py`, `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`, arquivos de teste em `tests/` e o teste de tela correspondente em `frontend/artifacts/neurostack/src/pages/`. O frontend passa em `pnpm --dir frontend/artifacts/neurostack run typecheck` e nos testes vitest tocados.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `d032f23ff3d9c799c6567d1833d71bf113f750ca1aba472a58ee6314a57ca27d`
- **Reviewer:** genie:reviewer a8f6ee3e (3 rodadas)
- **Reviewed at:** 2026-11-14T02:29:57.000Z
<!-- genie-design-review:end -->
