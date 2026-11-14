# Wish: Correção do link de agendamento e da triagem de convênio

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `correcao-link-convenio` |
| **Date** | 2026-11-13 |
| **Author** | Lorenzo |
| **Appetite** | small |
| **Branch** | `fix/correcao-link-convenio` |
| **Repos touched** | neurostack-api (backend `src/`, `tests/`; frontend `frontend/artifacts/neurostack/src/pages/clinic-config.tsx` e seu teste) |
| **Design** | [DESIGN.md](../../brainstorms/correcao-link-convenio/DESIGN.md) |

## Summary

O link de agendamento que a clínica liga em `/config` nunca chega ao paciente, porque o carregador de configuração dos turnos reais não traz o bucket `scheduling`. Além disso, a pergunta "convênio ou particular?", que manda segurar preços, dispara em clínica sem convênio, porque a pipeline lê a string `"no"` como verdadeira. Este wish corrige os dois. Como ligar o link de verdade passa a mandar ao paciente um texto livre da clínica sem trava, ele também passa a checar a mensagem do link ao salvar, e traz a consulta de inventário e o roteiro de QA ao vivo que o owner roda antes de cada deploy. É a etapa 0 do guarda-chuva [subagente-agendamento](../../brainstorms/subagente-agendamento/DRAFT.md) e precisa estar no `origin/main` antes da página de subagentes.

## Scope

### IN

- **Carregador.** `_load_config_from_db` (`src/services/event_service.py:421-485`) devolve `"scheduling": row.scheduling or {}`.
- **Testes do carregador pela costura real.**
  - Teste unitário de `_load_config_from_db`, trocando só `src.repositories.get_db_session` por uma sessão falsa (o import é feito dentro da função). A linha falsa tem `scheduling` preenchido e `api_credentials={"llm_provider": ...}`, para não buscar o builder. O teste verifica `scheduling` igual ao gravado, e `{}` quando a coluna é `None`.
  - Teste de contrato por `EventService._load_clinic_config`, com a sessão falsa e um Redis falso (`src.services.event_service._open_redis` trocado por um fake com `get`/`set`/`delete`/`aclose` assíncronos). Roda uma vez com o cache vazio e outra lendo o payload gravado (ida e volta em JSON). O resultado não é `None` e, como `clinic_config` do `SRMEngine` com `flags={"handoff_confirmation_pending": True}` e mensagem "sim, pode encaminhar" (forma de `tests/srm/test_engine.py:1066-1083`), dá `handoff_verification_status == "link_sent"` com link ligado e URL. Com link desligado, ou ligado sem URL, não dá.
- **Fixtures do carregador.** Em `tests/services/test_event_service_config_cache.py`, as duas linhas de banco falsas que simulam `ClinicConfigTable` (a classe `_ClinicRow`, ~`:550`, e o `SimpleNamespace` de ~`:731`) ganham o atributo `scheduling`, só com linhas novas. Sem isso o carregador levanta `AttributeError` dentro do `try` amplo e devolve `None`, e 3 testes existentes falham (`TestBuilderComposeInDBLoader`: `test_builder_llm_composes_and_whatsapp_stt_stay_clinic_byte_for_byte`, `test_explicit_gemini_provider_skips_owner_and_builder_secret_load`, `test_ambiguous_builder_owner_leaves_clinic_llm_untouched`). A tabela real já tem a coluna; só as fixtures estavam atrasadas. Não se muda o `src` para tolerar atributo ausente.
- **Triagem de convênio.** Nova função pura `clinic_accepts_insurance(value: object) -> bool` em `src/services/price_disclosure_policy.py`, com a regra de `resolve_price_disclosure(...).insurance_active` (`packages/neurostack-core/neurostack_core/prompt/price_policy.py:59-64`). `src/core/pipeline.py:943` usa `clinic_accepts_insurance(custom_vars.get("accepts_insurance"))` no lugar de `bool(...)`.
- **Testes da triagem.**
  - Paridade: `clinic_accepts_insurance(v) == resolve_price_disclosure({"accepts_insurance": v}).insurance_active` para `True, False, None, "", "yes", "no", "No", "NO", " no ", "false", "False", "0", "none", "None", "não", "off", "sim", 1, 0`. O teste importa a regra do módulo do núcleo.
  - Pipeline: com `business_rules.accepts_insurance = "no"`, turno 2 e intenção `agendar`, `custom_vars["ask_convenio_triage"]` é falso; com `"yes"` e modalidade indefinida, é verdadeiro.
  - O espelho `_compute_gating` em `tests/srm/test_engine.py:893-908` passa a usar `clinic_accepts_insurance`. É a única linha de teste existente que pode ser trocada; fora ela, os testes existentes só recebem linhas novas (ver o item de fixtures do carregador acima). `scripts/wish-gates.sh:60-118` trata `tests/srm` como "parity oracle", mas nem o CI nem `make quality` aplicam isso, e o PR #70 já editou essa pasta.
- **Checagem da mensagem do link ao salvar** (`PATCH /v1/config`, `src/api/routes/config.py`, na revalidação do bucket `scheduling`, ~705-735).
  - O detector `detect_scheduling_violation` (`src/observability/scheduling_gate.py:270-291`, PT e EN) roda sobre a mensagem do bucket já mesclado em dois casos:
    - (a) a mensagem mesclada é diferente, por comparação exata, da gravada (`_prev_scheduling`, `config.py:691`);
    - (b) o link fica efetivamente ligado nesta gravação, mesmo com a mensagem igual. "Efetivamente ligado" segue a regra do SRM (`srm/engine.py:166-169`): toggle ligado **e** URL preenchida. Antes, o link conta como ligado só quando o `scheduling_link_enabled` gravado `is True` e a URL gravada não é vazia; qualquer outro valor gravado (chave ausente, `None`, `False`, string como `"no"`) conta como desligado. Depois, conta como ligado quando o bucket validado por `ClinicScheduling` tem o toggle `True` e a URL não vazia. A checagem roda quando está ligado depois e não estava antes. Isso pega a mensagem copiada por clonagem (`src/services/clinic_clone_service.py:42-46`, que zera só a URL e o toggle), a linha antiga com mensagem e sem URL que recebe uma URL, e uma linha com toggle fora do padrão.
  - Mensagem vazia ou `None` não é checada. Num salvamento sem (a) nem (b), a mensagem não é checada, então uma mensagem antiga não trava os outros campos.
  - Num acerto, a rota responde 422 antes do `flush`/`commit` e não grava nada. O `detail` vem no formato de erro do pydantic: `[{"loc": ["body", "scheduling_link_message"], "msg": "...", "type": "scheduling_link_message_promise"}]`. O tradutor da tela (`configSaveErrorFieldNames`, `clinic-config.tsx:191-216`) usa `loc.at(-1)` e o erro passa a nomear "mensagem do link de agendamento" (rótulo em `:128`). A tela não destaca o campo em vermelho, só nomeia.
  - O detector é fail-open (devolve `None` num erro interno). Nesse caso a mensagem é aceita.
- **Aviso na tela.** O `hint` do campo "Mensagem do Link de Agendamento" (`clinic-config.tsx:2306-2310`) ganha uma frase no fim:
  - pt: "Texto enviado ao paciente antes do link. A URL será adicionada logo abaixo. Não diga que a consulta está marcada ou confirmada, nem que há horário livre: quem confere o agendamento é a equipe."
  - en: "Text sent to the patient before the link. The URL will be appended right below. Do not say the appointment is booked or confirmed, or that a time is available: the team checks the booking."
- **Testes da checagem.**
  - Rota:
    - 422 sem gravar para uma mensagem nova com promessa, em pt ("Seu agendamento está confirmado! Acesse o link:") e em en ("Your appointment is confirmed, open the link:").
    - 422 sem gravar ao ligar o link de uma linha que já tem mensagem com promessa gravada e o link desligado. Variações do gravado, todas com 422: chave do toggle ausente; toggle gravado como string `"no"`; toggle `True` sem URL, recebendo a URL agora.
    - 200 ao ligar o link de uma linha com a mensagem neutra gravada.
    - 200 para a mensagem padrão ("Para concluir seu agendamento, acesse o link abaixo:"), para uma neutra ("Clique no link para escolher o melhor horário para você:") e para o placeholder da tela (`clinic-config.tsx:2316-2319`).
    - 200 quando uma mensagem com promessa já gravada, com o link já ligado **e com URL gravada**, é reenviada sem mudança junto com outro campo alterado. Sem a URL gravada, o link não conta como ligado antes, e o 422 seria o resultado certo.
  - Tela (vitest): o `hint` novo aparece em pt e en com o link ligado, e um 422 com esse `loc` produz o erro com o rótulo "mensagem do link de agendamento".
- **Inventário e roteiro de QA ao vivo:** seções "Inventário antes de cada deploy" e "QA Criteria" deste wish. O owner roda; o agente não roda nada em banco de ambiente.

### OUT

- Mudar o formato gravado de `accepts_insurance`, migrar dados ou revalidar `business_rules` no PATCH.
- Trocar a cópia equivalente da regra em `src/services/event_service.py:2195-2201` pela função nova.
- Exportar `resolve_price_disclosure` no pacote do núcleo (fachada travada por `tests/baselines/neurostack-core-public-facade.json`).
- Devolver `post_care` no carregador (sem leitor no runtime).
- Trocar `CONFIG_CACHE_SCHEMA_VERSION`.
- Mudar a montagem ou o envio da mensagem do link, o SRM, o `handoff_decider`, a verificação, os prompts ou a trava de respostas (que continua ignorando resposta determinística, `scheduling-agent-guardrails` decisão 5).
- Validar a mensagem no modelo `ClinicScheduling`: a revalidação do PATCH roda sobre o bucket mesclado, inclusive com a mensagem antiga sem mudança (`config.py:710-718`), e a clonagem roda `ClinicConfig.model_validate` (`clinic_clone_service.py:170`). As duas quebrariam para uma clínica com mensagem antiga.
- Evento de auditoria para mudança da mensagem.
- Destaque em vermelho do campo para erro vindo do servidor.
- Deploy e qualquer consulta em banco de ambiente: são cerimônia do owner.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Carregador devolve `scheduling` | É o bucket que o SRM e a pipeline leem (`srm/engine.py:166`, `pipeline.py:1186`). Mesma correção já feita para `lgpd` (`89c70deb`). |
| 2 | Testes pelo carregador real, trocando só a sessão de banco e o Redis | O defeito sobreviveu porque todo teste injetava `clinic_config` ou trocava o carregador. |
| 3 | Sem troca de versão do cache | Por até 15 min depois do deploy (TTL 900 s), o payload antigo segue sem `scheduling`, que é o comportamento de hoje. |
| 4 | `clinic_accepts_insurance` no lado da API, com paridade testada contra o núcleo | É o módulo que já junta config e regra de preço. Não mexe na fachada pública do núcleo. |
| 5 | Mensagem do link checada ao salvar com o detector da trava, na mudança da mensagem e quando o link é ligado | Escolha do owner ("Checar ao salvar"). O gatilho "ligar o link" fecha o furo da clonagem, que a revisão do design achou na rodada 3. Checar só nesses dois momentos evita travar quem precisa salvar outro campo. |
| 6 | Sem flag nova | Rollback por clínica = desligar o link, que invalida o cache na hora (`config.py:767-769`). |
| 7 | Branch próprio `fix/correcao-link-convenio` a partir de `origin/main`, PR separado | Correção não vai no branch de outro wish. |
| 8 | Inventário em cada ambiente, antes do deploy daquele ambiente | O primeiro deploy em qualquer um dos dois (serviço de staging, chamado de "production" pelo tooling, e `neurostack-prod`) liga o fluxo de verdade ali. |

## Simplicity Case

- **Simplest complete design:** uma chave a mais num dict literal e uma chamada de função no lugar de `bool(...)`, cada uma com o teste que reproduz o defeito.
- **Added machinery:**
  - `clinic_accepts_insurance` (3 linhas), com teste de paridade, porque a regra certa fica fora da fachada do núcleo.
  - A checagem ao salvar, que reaproveita o detector existente, porque ligar o link de verdade manda ao paciente um texto livre sem trava, e a regra de agenda é de tolerância zero (decisão do owner).
- **Deferred until measured:** unificar `event_service.py:2195-2201` (gatilho: mudança na lista de valores "desligado"); `post_care` no carregador (gatilho: primeiro leitor); revalidar `business_rules` (gatilho: um terceiro valor na tela); auditoria da mensagem (gatilho: o owner pedir rastreio).
- **Complexity removed:** nenhuma flag, migração ou troca de versão de cache.

## Dependencies

**depends-on:** none
**blocks:** pagina-subagentes

## Success Criteria

- [ ] Com a sessão falsa, `_load_config_from_db` devolve `scheduling` igual ao gravado, e `{}` para `None`. O teste falha em `origin/main`.
- [ ] O config de `EventService._load_clinic_config`, no cache vazio e no cache gravado, não é `None` e leva o `SRMEngine` a `handoff_verification_status == "link_sent"` na forma de turno do Scope, com link ligado e URL. Com link desligado ou sem URL, não leva. O caso ligado falha em `origin/main`.
- [ ] `clinic_accepts_insurance(v)` é igual a `resolve_price_disclosure({"accepts_insurance": v}).insurance_active` para toda a matriz do Scope.
- [ ] Com `accepts_insurance = "no"`, `ask_convenio_triage` é falso no turno 2 e com `agendar`; com `"yes"` e modalidade indefinida, é verdadeiro. O caso `"no"` falha em `origin/main`.
- [ ] `PATCH /v1/config` responde conforme os testes da checagem do Scope: 422 sem gravar nos casos de mensagem nova com promessa e de ligar o link com mensagem antiga com promessa; 200 nos casos aceitos.
- [ ] A tela mostra o `hint` novo em pt e en, e um 422 com `loc` em `scheduling_link_message` gera o erro com o rótulo do campo.
- [ ] Os testes já existentes do link, da triagem, do cache de config, dos gates, do PATCH de config e da tela de config passam. Em teste existente, a única linha que pode ser trocada é a do espelho `_compute_gating`; o resto só recebe linhas novas, entre elas as fixtures do carregador.
- [ ] Gates de backend: medidos antes da primeira edição e de novo no final, com o venv `/home/lorenzo/.cache/pypoetry/virtualenvs/neurostack-api-dYUOnVPp-py3.12` e `PYTHONPATH=<worktree>/packages/neurostack-core`:
  - `black --check` + `isort --check-only`, `flake8` e `mypy`, com os alvos de `Makefile:122-135`, mais `pytest tests/`;
  - todo gate que saiu 0 no baseline sai 0 no final;
  - no pytest, nenhuma linha `^(FAILED|ERROR)` fora do baseline e contagem de aprovados que não diminui;
  - uma falha nova só é atribuída à mudança depois de 8 execuções no worktree e 8 num worktree limpo em `origin/main` (relógio do WSL); o registro segue a regra do risco do relógio;
  - os baselines de `format`, `lint`, `typecheck` e `fe-typecheck` saem 0 (se não saírem, o trabalho para antes da primeira edição).
- [ ] Gates de frontend: `pnpm run typecheck` e `pnpm run test` em `frontend/artifacts/neurostack`, medidos antes e depois, pela mesma regra.
- [ ] Fora de `.genie/`, a diferença entre `merge-base origin/main HEAD` e a árvore de trabalho só tem: `src/services/event_service.py`, `src/services/price_disclosure_policy.py`, `src/core/pipeline.py`, `src/api/routes/config.py`, `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`, arquivos em `tests/` e arquivos `*.test.tsx` em `frontend/artifacts/neurostack/src/pages/`. Em teste existente, só se adicionam linhas, exceto o espelho `_compute_gating` em `tests/srm/test_engine.py`.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 1 — +1 multi-pacote (teste usa o SRM do núcleo); mudança de uma linha com testes determinísticos | `engineer-trivial` / low | Worktree, baselines e carregador com testes pela costura real |
| 2 | engineer | 1 — +1 multi-pacote (paridade com a regra do núcleo) | `engineer-trivial` / low | Triagem de convênio com paridade e teste da pipeline |
| 3 | engineer | 3 — +2 estado (decide checar pela comparação gravado × mesclado), +1 multi-pacote (backend + frontend) | `engineer-standard` / medium | Checagem da mensagem ao salvar, aviso na tela e gates finais |

Os três grupos rodam no mesmo worktree, um depois do outro, e cabem num PR só.

## Execution Groups

### Group 1: Worktree, baselines e carregador

**Goal:** o carregador dos turnos reais passa a trazer `scheduling`, com testes que passam pelo carregador de verdade.

**Deliverables:**
0. Preparação, antes de qualquer edição:
   - `git worktree add /home/lorenzo/work/correcao-link-convenio -b fix/correcao-link-convenio origin/main`;
   - frontend: `cd frontend && pnpm install --frozen-lockfile --offline --ignore-scripts` (nunca numa cópia que compartilhe `node_modules`);
   - criar `/home/lorenzo/work/correcao-link-convenio-evidence/run-gates.sh` exatamente como abaixo (fora do repo) e rodar `bash run-gates.sh baseline`. O Grupo 3 roda o mesmo script com `final`, então baseline e final usam os mesmos comandos. Cada arquivo sai com `origin <sha>` na primeira linha, stdout e stderr juntos, e `exit <n>` na última.

```bash
#!/usr/bin/env bash
# uso: bash run-gates.sh baseline|final
set -uo pipefail
P="$1"
WT=/home/lorenzo/work/correcao-link-convenio
EV=/home/lorenzo/work/correcao-link-convenio-evidence
VB=/home/lorenzo/.cache/pypoetry/virtualenvs/neurostack-api-dYUOnVPp-py3.12/bin
export PYTHONPATH="$WT/packages/neurostack-core"
cd "$WT"
ORIGIN="origin $(git rev-parse origin/main)"
T="src packages/neurostack-core/neurostack_core tests"
run() { local name="$1"; shift; { echo "$ORIGIN"; "$@"; echo "exit $?"; } > "$EV/$P-$name.txt" 2>&1; }
run format bash -c "'$VB/black' --check $T && '$VB/isort' --check-only $T"
run lint "$VB/flake8" src packages/neurostack-core/neurostack_core tests
run typecheck "$VB/mypy" src packages/neurostack-core/neurostack_core --ignore-missing-imports
run pytest "$VB/python" -m pytest tests/ -q -p no:cacheprovider
cd frontend/artifacts/neurostack
run fe-typecheck env NO_COLOR=1 pnpm run typecheck
run fe-test env NO_COLOR=1 pnpm run test
```
1. `src/services/event_service.py`: `"scheduling": row.scheduling or {}` no dict de `_load_config_from_db`.
2. Testes novos em `tests/services/test_event_service_config_loader.py` (arquivo novo, nome fixo): o unitário do carregador e o de contrato por `_load_clinic_config` (cache vazio e gravado) → `SRMEngine`, como no Scope.
2b. `tests/services/test_event_service_config_cache.py`: acrescentar o atributo `scheduling` às duas linhas de banco falsas (como descrito em "Fixtures do carregador"), só com linhas novas. Sem isso, 3 testes existentes falham com a mudança do item 1.
3. Prova de reversão: com a linha do item 1 revertida temporariamente, rodar o arquivo novo e salvar a saída em `$EV/g1-revert.txt` (precisa conter `failed`); depois restaurar a linha.

**Acceptance Criteria:**
- [ ] Os 6 arquivos `baseline-{format,lint,typecheck,pytest,fe-typecheck,fe-test}.txt` existem, foram gerados antes da primeira edição, têm `origin <sha>` na primeira linha e `exit <n>` na última. `baseline-pytest.txt` tem a linha de resumo do pytest e `baseline-fe-test.txt` tem "Test Files".
- [ ] Os testes novos passam no worktree, e `$EV/g1-revert.txt` mostra que falham com a linha do item 1 revertida.
- [ ] `tests/services/test_event_service_config_cache.py` passa por inteiro (os 3 testes de `TestBuilderComposeInDBLoader` voltam a passar), e o diff desse arquivo só tem linhas adicionadas.
- [ ] Os baselines de `format`, `lint`, `typecheck` e `fe-typecheck` saem `exit 0`. Se algum não sair, o grupo para e o owner é avisado antes de qualquer edição: um gate já vermelho não poderia ser cobrado no final.

**Validation:**
```bash
set -euo pipefail
WT=/home/lorenzo/work/correcao-link-convenio
EV=/home/lorenzo/work/correcao-link-convenio-evidence
PY=/home/lorenzo/.cache/pypoetry/virtualenvs/neurostack-api-dYUOnVPp-py3.12/bin/python
cd "$WT"
for g in format lint typecheck pytest fe-typecheck fe-test; do
  f="$EV/baseline-$g.txt"; head -1 "$f" | grep -qE '^origin [0-9a-f]{40}$'; tail -1 "$f" | grep -qE '^exit [0-9]+$'
done
for g in format lint typecheck fe-typecheck; do tail -1 "$EV/baseline-$g.txt" | grep -qx "exit 0"; done
grep -qE '[0-9]+ passed' "$EV/baseline-pytest.txt"
grep -q "Test Files" "$EV/baseline-fe-test.txt"
grep -q "failed" "$EV/g1-revert.txt"
PYTHONPATH="$WT/packages/neurostack-core" "$PY" -m pytest -q -p no:cacheprovider tests/services/test_event_service_config_loader.py tests/services/test_event_service_config_cache.py tests/srm/test_engine.py tests/core/test_pipeline.py --deselect tests/core/test_pipeline.py::TestCognitivePipeline::test_process_greeting
git diff --name-only "$(git merge-base origin/main HEAD)" | grep -qx "src/services/event_service.py"
if git diff -U0 "$(git merge-base origin/main HEAD)" -- tests/services/test_event_service_config_cache.py | { grep -E '^-' || true; } | { grep -v -E '^--- (a/|/dev/null)' || true; } | grep -q .; then exit 1; fi
```
Escopo: a mudança é uma chave num dict. Os testes focados cobrem o carregador, o cache e o caminho do link no SRM e na pipeline. O teste instável preexistente `test_process_greeting` (ver a emenda do Grupo 2) fica fora com `--deselect`, porque este bloco não descontaria uma falha dele.

**depends-on:** none

---

### Group 2: Triagem de convênio

**Goal:** clínica sem convênio deixa de receber a pergunta de modalidade, com a mesma regra que o template usa para preços.

**Deliverables:**
1. `src/services/price_disclosure_policy.py`: `clinic_accepts_insurance(value: object) -> bool`, com docstring que aponta para a regra do núcleo.
2. `src/core/pipeline.py:943`: usa a função nova.
3. Teste de paridade em `tests/services/test_price_disclosure_policy.py` (arquivo existente; só adiciona testes) com a matriz do Scope.
4. Teste da pipeline (`tests/core/test_pipeline.py`) com `"no"` e `"yes"`, lendo `variables.custom` como em `tests/core/test_pipeline.py:1253`.
5. `tests/srm/test_engine.py:893-908`: `_compute_gating` usa `clinic_accepts_insurance`. A única linha removida do arquivo é `bool(accepts_insurance)`. Se um teste existente precisar de import novo, o import é local, dentro do teste, para o isort/black não reescrever linhas existentes (a regra de "só adicionar" trata linha reescrita como removida).

**Acceptance Criteria:**
- [ ] Paridade verde para toda a matriz.
- [ ] O teste da pipeline com `"no"` falha com o `bool(...)` antigo e passa com a correção.
- [ ] Os testes existentes de `TestConvenioTriageGating`, `TestAtendimentoTipoSlot` e de preço passam. O comando de validação sai 0, descontando só o teste instável documentado (`test_process_greeting`).

**Validation:**
```bash
set -euo pipefail
WT=/home/lorenzo/work/correcao-link-convenio
PY=/home/lorenzo/.cache/pypoetry/virtualenvs/neurostack-api-dYUOnVPp-py3.12/bin/python
EV=/home/lorenzo/work/correcao-link-convenio-evidence
cd "$WT"
OUT="$EV/g2-validation-final.txt"
PYTHONPATH="$WT/packages/neurostack-core" "$PY" -m pytest -q -p no:cacheprovider --color=no tests/services/test_price_disclosure_policy.py tests/core/test_pipeline.py tests/srm/test_engine.py tests/prompt > "$OUT" 2>&1 || true
grep -qE '[0-9]+ passed' "$OUT"
if grep -q $'\x1b' "$OUT"; then exit 1; fi
touch "$EV/known-flakes.txt"
while IFS= read -r id || [ -n "$id" ]; do
  [ -z "$id" ] && continue
  f="$(grep -l -x -F "nodeid: $id" "$EV"/flake-*.txt 2>/dev/null | head -1 || true)"
  [ -n "$f" ]
  grep -qE '^worktree_passed: [1-8]$' "$f"
  grep -qE '^clean_failed: [1-8]$' "$f"
  if grep -q -F -- "ERROR $id" "$OUT"; then exit 1; fi
  if grep -q -F -- "FAILED $id" "$OUT"; then
    reason="$(grep -m1 '^reason_line: ' "$f" | sed 's/^reason_line: //')"
    [ -n "$reason" ]
    grep -q -F -- "$reason" "$OUT"
  fi
done < "$EV/known-flakes.txt"
NEW="$({ grep -E '^(FAILED|ERROR) tests/' "$OUT" || true; } | sed -E 's/^(FAILED|ERROR) //; s/ - .*//' | sort -u | { grep -v -x -F -f "$EV/known-flakes.txt" || true; })"
if [ -n "$NEW" ]; then echo "$NEW"; exit 1; fi
if grep -n 'bool(custom_vars.get("accepts_insurance"))' src/core/pipeline.py; then exit 1; fi
```
Escopo: a regra é pura. O teste de paridade fixa a semântica contra o núcleo, e os testes da pipeline e do prompt cobrem quem consome a chave. A validação aceita como falha só o teste instável documentado em `$EV/known-flakes.txt`, que precisa ter a evidência de 8 + 8 execuções (ver a emenda de execução do Grupo 2); qualquer outra falha ou erro reprova, e uma falha do teste instável por um motivo diferente do registrado (`reason_line` na evidência) também reprova, e um `ERROR` desse teste (erro de preparação ou de coleta) nunca é descontado. O pytest roda com `--color=no`, e saída com cor reprova (a cor esconderia as linhas `FAILED`).

**depends-on:** Group 1

---

### Group 3: Checagem da mensagem ao salvar e gates finais

**Goal:** a clínica não consegue salvar uma mensagem de link que prometa agenda, e o PR sai com todos os gates no baseline.

**Deliverables:**
1. `src/api/routes/config.py`: a checagem dos casos (a) e (b) do Scope, dentro da revalidação do bucket `scheduling`, antes do `flush`/`commit`, com o `detail` no formato do Scope.
2. Testes de rota em `tests/core/test_config_service.py`, numa classe nova ao lado de `TestSchedulingLinkPatchBoundary` (`:231`), com os casos do Scope.
3. `frontend/artifacts/neurostack/src/pages/clinic-config.tsx`: o `hint` novo do campo da mensagem.
4. Teste vitest em `frontend/artifacts/neurostack/src/pages/clinic-config-scheduling-link.test.tsx` (arquivo novo, nome fixo): `hint` em pt e en, e erro com o rótulo para um 422 com esse `loc`.

**Acceptance Criteria:**
- [ ] Todos os casos de rota do Scope passam, e o 422 não deixa nada gravado (lido de volta com `flatten_config`, que é o que a rota GET devolve, e com a sessão sem `flush`/`commit`).
- [ ] O teste da tela passa em pt e en.
- [ ] Todos os Success Criteria de gates e de escopo do diff passam.

**Validation:**
```bash
set -euo pipefail
WT=/home/lorenzo/work/correcao-link-convenio
EV=/home/lorenzo/work/correcao-link-convenio-evidence
PY=/home/lorenzo/.cache/pypoetry/virtualenvs/neurostack-api-dYUOnVPp-py3.12/bin/python
cd "$WT"
export PYTHONPATH="$WT/packages/neurostack-core"
"$PY" -m pytest -q -p no:cacheprovider tests/core/test_config_service.py tests/api
( cd frontend/artifacts/neurostack && pnpm exec vitest run --config vitest.config.ts src/pages/clinic-config-scheduling-link.test.tsx src/pages )
bash "$EV/run-gates.sh" final
for g in format lint typecheck pytest fe-typecheck fe-test; do
  if tail -1 "$EV/baseline-$g.txt" | grep -qx "exit 0"; then tail -1 "$EV/final-$g.txt" | grep -qx "exit 0"; fi
done
touch "$EV/known-flakes.txt"
norm() { { grep -E '^(FAILED|ERROR) tests/' "$1" || true; } | sed -E 's/^(FAILED|ERROR) //; s/ - .*//' | sort -u; }
for g in baseline final; do if grep -q $'\x1b' "$EV/$g-pytest.txt"; then exit 1; fi; done
while IFS= read -r id || [ -n "$id" ]; do
  [ -z "$id" ] && continue
  f="$(grep -l -x -F "nodeid: $id" "$EV"/flake-*.txt 2>/dev/null | head -1 || true)"
  [ -n "$f" ]
  grep -qE '^worktree_passed: [1-8]$' "$f"
  grep -qE '^clean_failed: [1-8]$' "$f"
  if grep -q -F -- "ERROR $id" "$EV/final-pytest.txt"; then exit 1; fi
  if grep -q -F -- "FAILED $id" "$EV/final-pytest.txt"; then
    reason="$(grep -m1 '^reason_line: ' "$f" | sed 's/^reason_line: //')"
    [ -n "$reason" ]
    grep -q -F -- "$reason" "$EV/final-pytest.txt"
  fi
done < "$EV/known-flakes.txt"
NEW="$(comm -13 <(norm "$EV/baseline-pytest.txt") <(norm "$EV/final-pytest.txt") | { grep -v -x -F -f "$EV/known-flakes.txt" || true; })"
if [ -n "$NEW" ]; then echo "$NEW"; exit 1; fi
PASS_BASE="$(grep -oE '[0-9]+ passed' "$EV/baseline-pytest.txt" | tail -1 | grep -oE '[0-9]+')"
PASS_FINAL="$(grep -oE '[0-9]+ passed' "$EV/final-pytest.txt" | tail -1 | grep -oE '[0-9]+')"
test "$PASS_FINAL" -ge "$PASS_BASE"
grep -q "Test Files" "$EV/final-fe-test.txt"
if grep -q $'\x1b' "$EV/final-fe-test.txt"; then exit 1; fi
NEWFE="$(comm -13 <(grep -oE 'FAIL +src/.*' "$EV/baseline-fe-test.txt" | sort -u) <(grep -oE 'FAIL +src/.*' "$EV/final-fe-test.txt" | sort -u))"
if [ -n "$NEWFE" ]; then echo "$NEWFE"; exit 1; fi
BASE="$(git merge-base origin/main HEAD)"
# testes existentes: nenhuma linha removida ou trocada fora de tests/srm/test_engine.py
REMOVED="$(git diff -U0 "$BASE" -- tests ':!tests/srm/test_engine.py' ':(glob)frontend/artifacts/neurostack/src/**/*.test.tsx' | { grep -E '^-' || true; } | { grep -v -E '^--- (a/|/dev/null)' || true; })"
if [ -n "$REMOVED" ]; then
  git diff --stat "$BASE" -- tests ':(glob)frontend/artifacts/neurostack/src/**/*.test.tsx'; echo "linha removida ou trocada em teste existente"; exit 1
fi
SRM_REMOVED="$(git diff -U0 "$BASE" -- tests/srm/test_engine.py | { grep -E '^-' || true; } | { grep -v -E '^--- (a/|/dev/null)' || true; } | sed -E 's/^-[[:space:]]*//')"
if [ -n "$SRM_REMOVED" ] && [ "$SRM_REMOVED" != "bool(accepts_insurance)" ]; then echo "tests/srm/test_engine.py: $SRM_REMOVED"; exit 1; fi
OUTSIDE="$( { git diff --name-only "$BASE" -- . ':!.genie'; git ls-files --others --exclude-standard -- . ':!.genie'; } \
  | grep -v -E '^(src/services/event_service\.py|src/services/price_disclosure_policy\.py|src/core/pipeline\.py|src/api/routes/config\.py|frontend/artifacts/neurostack/src/pages/clinic-config\.tsx|tests/.*|frontend/artifacts/neurostack/src/pages/.*\.test\.tsx)$' || true)"
if [ -n "$OUTSIDE" ]; then echo "$OUTSIDE"; exit 1; fi
```
Escopo: a checagem é a única mudança com estado (gravado × mesclado), então os testes de rota cobrem os quatro caminhos. Os gates finais são o agregado do repo (`make quality` em partes, mais o frontend tocado), rodados pelo mesmo script do baseline do Grupo 1: gate que saiu 0 continua 0, nenhuma falha nova de pytest ou vitest, e a contagem de aprovados do pytest não diminui.

**depends-on:** Group 2

---

## Inventário antes de cada deploy

Só leitura. O owner roda no banco de cada ambiente (serviço de staging e `neurostack-prod`), antes do deploy daquele ambiente, e confere duas coisas: nenhuma mensagem pode dizer que a consulta está marcada ou confirmada, nem que há horário livre; e a URL de cada clínica com o link ligado precisa estar certa.

```sql
BEGIN READ ONLY;

-- 1) Link de agendamento: toda linha com URL, mensagem, liga/desliga diferente de false ou bloco que não é objeto
SELECT
  cc.tenant_id,
  cc.identity->>'clinic_name'                              AS clinic_name,
  cc.scheduling->'scheduling_link_enabled'                 AS link_enabled_raw,
  jsonb_typeof(cc.scheduling->'scheduling_link_enabled')   AS link_enabled_type,
  cc.scheduling->>'scheduling_link_url'                    AS link_url,
  length(cc.scheduling->>'scheduling_link_url')            AS link_url_len,
  cc.scheduling->>'scheduling_link_message'                AS link_message
FROM clinic_configs AS cc
WHERE NULLIF(btrim(cc.scheduling->>'scheduling_link_url'), '') IS NOT NULL
   OR NULLIF(btrim(cc.scheduling->>'scheduling_link_message'), '') IS NOT NULL
   OR cc.scheduling->'scheduling_link_enabled' <> 'false'::jsonb
   OR jsonb_typeof(cc.scheduling) <> 'object'
ORDER BY clinic_name NULLS LAST;

-- 2) Triagem de convênio e preços: o que muda para cada combinação de configuração
SELECT
  cc.business_rules->'accepts_insurance'  AS accepts_insurance_raw,
  cc.pricing->>'disclosure_mode'          AS pricing_mode,
  cc.business_rules->'provide_prices'     AS provide_prices_raw,
  count(*)                                AS clinics
FROM clinic_configs AS cc
GROUP BY 1, 2, 3
ORDER BY clinics DESC;

ROLLBACK;
```

Leitura do resultado 1: as linhas com `link_enabled_raw = true` e URL preenchida passam a enviar o link assim que o deploy entra. Qualquer `link_enabled_type` diferente de `boolean` está fora do padrão e deve ser corrigido pela tela antes do deploy. Uma URL só com espaços aparece em branco, mas tem `link_url_len` maior que zero e o SRM a trata como preenchida. Uma linha cujo bloco `scheduling` não é um objeto JSON também aparece aqui e precisa de correção.

**Decisão do owner antes do primeiro deploy (exposição de clínicas reais):** as clínicas reais com o link ligado e URL passam a enviar o link no momento do deploy, antes do QA ao vivo. Duas opções. (1) Aceitar a exposição, depois de revisar as mensagens e as URLs do inventário. (2) Desligar o link dessas clínicas antes do deploy e religar depois dos casos 1 a 3 do QA; religar passa pela checagem (b). Atenção à opção 2: desligar o link pela tela grava a URL e a mensagem como `null` (`clinic-config.tsx:1791-1792`), então copie os valores do inventário antes e digite-os de novo ao religar. Recomendação: opção 2 se o inventário listar clínica real com o link ligado; opção 1 se vier vazio ou só com clínicas de teste.

Leitura do resultado 2: as linhas com `"no"`, `"false"`, `"0"` ou `"none"` (sem diferenciar maiúsculas), `false`, `null` ou vazio deixam de receber a pergunta de modalidade; hoje só as `false`/`null`/vazias já não recebiam. Para essas clínicas, a instrução "segure qualquer valor até o paciente escolher" deixa de ser injetada: depois do deploy elas respondem preço conforme `pricing_mode` e `provide_prices_raw` (por exemplo, `exact_allowed` ou `provide_prices = yes` passa a citar valor exato). Confira as combinações antes do deploy.

---

## QA Criteria

_Roteiro ao vivo, pelo WhatsApp real, numa clínica de teste com a pausa por handoff ativa (`resolve_handoff_pause`), no primeiro ambiente que receber o deploy e antes do segundo. O owner roda ou autoriza._

_Antes do caso 1: a configuração fica em cache por até 15 minutos depois do deploy (TTL de 900 s) e o payload antigo não traz `scheduling`. Salve `/config` da clínica de teste (isso invalida o cache na hora) ou espere 15 minutos; sem isso o caso 1 pode falhar e parecer um defeito do fluxo._

- [ ] 1. Link ligado com URL: o paciente pede para agendar, confirma, recebe a mensagem da clínica + a URL, e o handoff pendente aparece na fila.
- [ ] 2. No turno seguinte, "consegui": o handoff é enriquecido com `concluiu` e a IA pausa.
- [ ] 3. Em outra conversa, "não consegui": `nao_concluiu`.
- [ ] 4. Um pedido de cancelamento: não recebe link e segue o handoff normal.
- [ ] 5. Link desligado: o fluxo de hoje, sem link.
- [ ] 6. Clínica com `accepts_insurance = "no"`: nenhuma pergunta "convênio ou particular?".
- [ ] 6b. Na mesma clínica, perguntar o preço de um serviço: a resposta segue o modo de preços da clínica (valor exato, faixa ou "consulte"), sem perguntar "convênio ou particular?" antes. Compare com o resultado 2 do inventário.
- [ ] 7. Em `/config`, salvar a mensagem "Seu agendamento está confirmado, acesse:" é recusado e o erro nomeia "mensagem do link de agendamento"; a mensagem padrão salva normalmente.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| O fluxo do link nunca rodou em produção. O turno de enriquecimento faz commit em sessão separada e chama o webhook de handoff dentro da fase 1 (`src/services/handoff_service.py:836-905`), e um erro ali derruba o turno (`src/core/pipeline.py:577-598`) | High | QA ao vivo no primeiro ambiente antes do segundo; inventário antes de cada deploy; rollback por clínica = desligar o link. |
| Mensagem do link com promessa de agenda (texto livre da clínica, sem a trava de respostas) | High | Checagem ao salvar (mudança e ligar o link) e inventário do estoque. Resíduo aceito: frases que o detector não pega, como "Sua vaga está garantida, acesse:" ou "Agendamento realizado! Link:", o mesmo resíduo da trava de respostas. |
| URL esquecida ou velha em clínica que ligou o link há tempo | Medium | O inventário lista URL e mensagem; o owner decide avisar as clínicas. |
| Linha antiga com `scheduling_link_enabled` gravado como string: `srm/engine.py:167` aplica `bool(...)`, e um `"no"` ligaria o link | Low | O PATCH valida o bucket com `ClinicScheduling` (bool) desde o wish do link; o inventário mostra `link_enabled_type` e pega qualquer linha fora do padrão; a checagem (b) trata esse gravado como desligado e checa a mensagem. |
| Duas cópias da regra "convênio desligado" no lado da API (função nova e `event_service.py:2195-2201`) | Low | A função nova tem paridade testada com o núcleo; a unificação fica em OUT com gatilho. |
| Primeiro deploy expõe clínicas reais ao fluxo do link antes do QA ao vivo | High | Decisão do owner registrada no inventário (opção 1 aceitar com revisão, ou opção 2 desligar antes e religar depois dos casos 1 a 3, com o aviso de que desligar apaga URL e mensagem). |
| Mutantes que a suíte não mata (achados da revisão final, nível baixo) | Low | `or` trocado por `and`, ou só um lado da condição de turno/intenção em `pipeline.py:948-952`: lógica antiga que este wish não mudou; falta um caso "turno 2 + intenção que não é preço nem agendamento → pergunta" e um "turno 3 + `agendar` → pergunta". Coagir o "antes" pelo `ClinicScheduling` na rota passa nos 58 testes de rota: o caso "`\"yes\"` gravado + URL" só existe na tabela-verdade do helper. Acrescentar esses casos exige refazer a rodada final de uma hora; ficam como acompanhamento opcional. |
| Falso positivo do detector recusa uma mensagem legítima | Medium | Mensagem padrão, neutra e placeholder da tela entram nos testes de aceite. A tela só diz "Revise os seguintes campos: mensagem do link de agendamento", sem o motivo, e o aviso fixo do campo explica a regra. A revisão do Grupo 3 mediu recusas indevidas em frases de instrução comuns, como "Para ter sua consulta agendada, acesse o link abaixo:", "Para deixar sua consulta marcada, use o link abaixo:", "Click the link to get your appointment booked:" e "Once your appointment is confirmed by our team, you'll get a message."; a clínica reformula ou usa a mensagem padrão. Mudar o detector fica fora deste wish (serve também à trava de respostas); se a clínica reclamar, o ajuste é lá. Opcional, a decidir pelo owner: mostrar no erro um texto fixo da regra, em vez de só o nome do campo. |
| O detector não pega todas as promessas (mesmo resíduo da trava de respostas) | Medium | Frases medidas na revisão do Grupo 3 que passam: "Tudo certo, você está agendado!", "Está tudo confirmado para amanhã!", "Confirmado! Te esperamos amanhã às 10h", "Horário reservado para você!", "Agendamento efetuado com sucesso!", "tá confirmado", "está 100% confirmado", "You're all booked!", "Your spot is reserved", "We'll see you at 10am tomorrow" e um "está" com acento combinado separado (NFD). O inventário antes de cada deploy e o aviso da tela são a segunda camada. |
| Caminhos que gravam o bloco `scheduling` sem passar pelo PATCH (conferidos na revisão do Grupo 3) | Low | A clonagem copia a mensagem mas desliga o link e zera a URL, então ligar depois passa pelo caso (b). Criação de clínica começa com o bloco vazio. `TenantRepository.upsert_clinic_config` (`src/repositories/tenant.py:87-111`) grava sem checagem, mas nenhum código o chama hoje (bypass latente). Linhas já efetivamente ligadas, inclusive com toggle em string que o `bool()` do SRM lê como ligado, e escrita direta no banco nunca são checadas por desenho: só o inventário as pega. |
| Clínicas sem convênio passam a receber preço conforme a configuração de preços | Low | É o comportamento configurado; a retenção de preço já usa a regra certa. |
| O venv aponta o núcleo para um snapshot de 2026-11-08 sem `prompt/price_policy.py` | Low | Todo comando de teste usa `PYTHONPATH=<worktree>/packages/neurostack-core`. |
| Depois de um rollback de imagem, o cache mantém `scheduling` por até 15 min | Low | Desligar o link invalida o cache na hora. |
| Relógio do WSL deixa testes com tempo instáveis | Low | Uma falha nova no pytest final roda 8 vezes no worktree e 8 num worktree limpo em `origin/main`. O teste só entra em `$EV/known-flakes.txt` (uma linha com o id, como `tests/x.py::test_y`, sem o prefixo `FAILED`) quando passou pelo menos 1 vez no worktree **e** falhou pelo menos 1 vez no limpo. A evidência fica em `$EV/flake-<nome>.txt`, com as linhas `nodeid: <id>`, `worktree_passed: <n>` e `clean_failed: <m>` seguidas de `reason_line`, do resumo das 16 execuções e de um trecho da falha em cada árvore. O Grupo 3 confere a evidência de cada id antes de descontar. Um teste que falha 8 de 8 no worktree nunca entra na lista: é da mudança. Caso já registrado: `test_process_greeting` (ver a emenda do Grupo 2), com `reason_line` na evidência; a evidência foi medida na árvore do Grupo 2 e vale para o Grupo 3 porque este não toca o pipeline nem `tests/core/test_pipeline.py` (só `tests/core/test_config_service.py`, que não exercita o pipeline). A lista `known-flakes.txt` é lida mesmo sem quebra de linha no fim. |

---

## Carried from design review (round 3, SHIP d032f23f)

A revisão do design terminou em SHIP com 1 MEDIUM e 5 LOW, absorvidos aqui e sujeitos à revisão do plano:

- **MEDIUM:** a mensagem copiada por clonagem e linhas antigas sem URL escapavam do inventário e da checagem. Agora a checagem roda também quando o link passa de desligado para ligado (caso b), e o inventário pega URL **ou** mensagem **ou** liga/desliga gravado.
- **LOW:** "destaca o campo" virou "o erro nomeia o campo".
- **LOW:** o exemplo de lacuna do detector foi trocado por frases que ele realmente não pega.
- **LOW:** o motivo para não validar no modelo cita os caminhos reais (PATCH sobre o bucket mesclado e clonagem).
- **LOW:** o OUT foi estreitado para "montagem ou envio da mensagem", e a comparação é exata contra o gravado, sobre o valor mesclado.
- **LOW:** o `hint` existente ganha uma frase em vez de um segundo aviso; os gates de frontend medem contra o baseline, e a receita de instalação está no Grupo 1.

Acrescentado pelo orquestrador, fora da lista da revisão do design: a consulta 2 do inventário (contagem de `accepts_insurance`), só de leitura, para o owner saber quantas clínicas deixam de receber a pergunta de modalidade.

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan review — rodada 1 (2026-11-14T02:45:55Z)

- **Verdict:** SHIP
- **Reviewed SHA-256:** `f94d26ab4196f49dc00cb55194f38dda8b7212b4c5cb91afb7c3342a2c400682`
- **Reviewer:** genie:reviewer (a7181b03)
- **Confirmado:**
  - O gatilho "ligar o link" funciona contra o PATCH real (`_prev_scheduling` capturado antes do merge; a tela manda a mensagem sempre que o link está ligado; a clonagem grava o toggle `False`).
  - O detector foi rodado em todas as frases do wish.
  - A SQL foi validada num PostgreSQL 16 descartável com 12 linhas sintéticas.
  - `run-gates.sh` tem as aspas certas, e os binários do venv existem (black, isort, flake8 e mypy saem 0 em `origin/main`).
  - A projeção do cache mantém todas as chaves.
- **Gaps:**
  - [MEDIUM] condição (b) imprecisa;
  - [MEDIUM] o grep de pytest pegava linhas de log `ERROR    …` como falha nova;
  - [MEDIUM] os grupos 1 e 2 podiam não rodar os testes novos (nome de arquivo aberto) e não nomeavam a prova de reversão;
  - [MEDIUM] gate vermelho no baseline nunca seria cobrado;
  - LOWs: `?` e transação só leitura na SQL, consulta 2 fora da lista, riscos 2b e 5 do design omitidos, diff do Grupo 1 sem merge-base, regra de arquivos de teste sem checagem mecânica, sem caminho para registrar flake do relógio.
- **Correções aplicadas:** todas, nesta revisão do arquivo. A condição (b) agora segue a regra do SRM (toggle `is True` e URL), com testes de chave ausente, string `"no"`, URL adicionada e mensagem neutra. Por mexer na regra da checagem, vai para a rodada 2 antes do `APPROVED`.

### Plan review — rodada 2 (2026-11-14T02:51:38Z)

- **Verdict:** SHIP
- **Reviewed SHA-256:** `05a07bdfe7941bb853610e94c104ba5597745776f22a474226d6f035db3f3553`
- **Reviewer:** genie:reviewer (a7181b03, mesma thread da rodada 1)
- **Confirmado:**
  - A regra (b) é exata e implementável em `config.py:714-734`, lendo o "depois" de `validated_dump`. JSONB `true` carrega como `True`; um `1` ou `"true"` gravado conta como desligado e dispara a checagem (fail-closed). Depois de um salvamento com sucesso o toggle vira bool e a checagem não fica disparando.
  - O builder nunca fica sem saída: recebe o 422 com o campo nomeado, edita ou apaga a mensagem e salva de uma vez.
  - Ancorar o grep em `tests/` não perde erro de coleta nem de setup (pytest 9.1.1 real, com as opções do repo).
  - Nenhum teste existente fixa o texto antigo do `hint`, e toda mensagem de teste existente passa pelo detector.
- **Gaps (não bloqueantes):**
  - [MEDIUM] o desconto de flakes podia esconder uma regressão, e o formato da lista não batia com a comparação;
  - LOWs: linhas removidas começando com `-- ` escapavam da regra de edição de teste; `tests/srm/test_engine.py` estava excluído por inteiro; reflow de import aciona a regra de "só adicionar"; o teste de 200 precisava dizer "link ligado com URL"; "Files to Create/Modify" desatualizado.
- **Emendas pós-SHIP (2026-11-14, pelo orquestrador):** todas aplicadas com as correções que o revisor escreveu.
  - A lista de flakes só aceita teste que passou ao menos 1 vez no worktree e falhou ao menos 1 vez no limpo, com evidência conferida pelo Grupo 3 e ids normalizados dos dois lados.
  - A regra de edição de teste filtra só os cabeçalhos do diff e exige que a única linha removida de `tests/srm/test_engine.py` seja `bool(accepts_insurance)`.
  - Texto do teste de 200, nota do import local e lista de arquivos atualizados.
  - Os trechos de shell novos foram simulados antes de gravar. Mudaram só os blocos de validação, a linha de risco do relógio e esclarecimentos; a regra (b), o escopo e os critérios de produto são os revisados.

### Emenda de execução — Grupo 1 (2026-11-14, pelo orquestrador)

- **O que aconteceu:** o engenheiro do Grupo 1 fez a mudança de uma linha no carregador e parou com `blocked`: 3 testes existentes de `tests/services/test_event_service_config_cache.py` (`TestBuilderComposeInDBLoader`) falham com `assert None is not None`. As linhas de banco falsas dessas fixtures (a classe `_ClinicRow` e um `SimpleNamespace`) definem `lgpd` mas não `scheduling`; ler `row.scheduling` levanta `AttributeError`, o `except` amplo do carregador engole e devolve `None`. Reproduzido pelo orquestrador (`3 failed, 33 passed`). A tabela real tem a coluna, então o código de produção está certo.
- **Diagnóstico:** `ambiguous-spec`. O plano tinha duas frases que não combinavam: um critério dizia que a única edição permitida em teste existente era `_compute_gating`, e outro (e a checagem mecânica do Grupo 3) dizia que em teste existente só se adicionam linhas. Nenhuma das revisões viu as fixtures desatualizadas. Sem escalada de modelo ou esforço; foi o plano que ficou incompleto, não o engenheiro.
- **Rotas descartadas:** tolerar atributo ausente no `src` com `getattr` (esconderia fixtures desatualizadas); deixar os 3 testes vermelhos (viola o critério de que os testes existentes passam).
- **Decisão:** esclarecimento do wish a favor da regra que a checagem mecânica já impõe. Em teste existente só se trocam as linhas do espelho `_compute_gating`; o resto só recebe linhas novas, o que inclui o atributo `scheduling` nas duas fixtures. Novos itens: "Fixtures do carregador" (Scope), deliverable 2b e critério no Grupo 1, e a checagem de que o diff desse arquivo só adiciona linhas.
- **Alcance conferido:** nenhum outro arquivo simula a linha do banco. `tests/api/test_rlhf.py` e `tests/api/test_credit_suspension_webhooks.py` (este usa a linha real do banco em memória) passam com a mudança: 183 testes passados.
- **Medição inicial registrada** (`origin c91cea30`): format, lint, typecheck e typecheck do frontend saem 0; o frontend passa 45 arquivos e 1132 testes; o pytest sai 1 com `22 failed, 16135 passed, 1774 skipped, 104 errors` em 3528 s. Os erros são testes que precisam de Postgres (`test_builder_cloning_postgres`, `test_media_assets_postgres`) e de ferramenta de backup (`test_backup_encryption`); as falhas incluem as conhecidas em `origin/main`. A comparação final desconta só o que está nesse baseline.
- **Aviso ao owner:** esta emenda muda uma frase do plano aprovado depois da revisão. Se o owner preferir outra rota, a mudança é reverter as duas linhas de fixture e este bloco.

### Execution review — Grupo 1 (2026-11-14T04:34:30Z)

- **Verdict:** SHIP
- **Reviewed:** `tests/services/test_event_service_config_loader.py` sha256 `7006de7c0c03d81df89a31f9fd78cd23f17c35333dc3c5a52725cde5050307fa`; `git diff` sha256 `8e18839c7e229489748a4b5f8938032e054bbf79f8fd115693fc8a6ab05f2b39`
- **Reviewer:** genie:reviewer (a54d8e6e; não é o engenheiro do grupo)
- **Confirmado:**
  - Os testes passam pelo carregador real: só `src.repositories.get_db_session` e `_open_redis` são trocados, e a rodada com cache já gravado lê o JSON da primeira rodada (a linha do banco vira `None`).
  - Mutações na cópia de teste, todas com testes vermelhos: apagar a chave no carregador; `build_cache_payload` descartando o bloco; leitura do cache descartando; escrita do cache descartando; cache pulado; carregador devolvendo `{}` fixo. Sem a correção, em `c91cea30` limpo: 5 falhas com `KeyError: 'scheduling'`.
  - Medição inicial íntegra: o pytest começou depois de o arquivo novo sair de `tests/` e não viu o teste novo nem a edição de `src`. O lint refeito tem o formato exato do wrapper e `exit 0`. Black, isort, flake8 e mypy saem 0 numa cópia limpa de `c91cea30`.
  - Vizinhança: 20 arquivos que usam o carregador dão 1448 testes passados tanto na cópia limpa quanto no worktree. Ordem aleatória (5 sementes) e ordem reversa: 36 passam. Nenhuma leitura de relógio. Nada commitado; `.genie/` é ignorado.
- **Gaps (LOW, não bloqueantes):**
  1. As asserções negativas (link desligado e ligado sem URL) só conferiam `!= "link_sent"`; a saída real é `handoff_required=True` e status `None`. **Aplicado** pelo engenheiro depois do SHIP, com o `_decide` devolvendo a decisão inteira.
  2. `baseline-lint.txt` foi refeito às 01:12:26, depois da primeira edição (00:11:21), então não foi gerado "antes da primeira edição". O valor está certo (exit 0, árvore limpa de `c91cea30`). **Registrado aqui:** o primeiro lint (00:11:17–00:11:32) coincidiu com a janela de ~5 s em que o arquivo de teste existiu em `tests/services/`; o refazimento rodou com `git status --short` vazio, no mesmo wrapper.
  3. `g1-revert.txt` não tinha cabeçalho `origin` nem o comando. **Aplicado** pelo engenheiro: refeito com `origin <sha>`, o comando, a linha revertida e o `exit`.
- **Validação do orquestrador:** o bloco de validação do Grupo 1, extraído do wish sem alteração e sem canos, saiu com `exit 0`; 160 testes passaram. Saída completa em `$EV/orchestrator-g1-validation.txt`. Escopo: a mudança é uma chave num dict; os testes focados cobrem o carregador, o cache e o caminho do link no SRM e na pipeline, e o diff de `test_event_service_config_cache.py` só tem linhas adicionadas.
- **Medição inicial (`origin c91cea30`):** format, lint, typecheck e typecheck do frontend `exit 0`; frontend 45 arquivos e 1132 testes; pytest `exit 1` com 22 falhas, 16135 passados, 1774 pulados e 104 erros em 3528 s (erros de Postgres e de ferramenta de backup).

### Emenda de execução — Grupo 2 (2026-11-14, pelo orquestrador)

- **O que aconteceu:** o engenheiro do Grupo 2 implementou tudo e parou com `blocked`: o comando de validação do wish sai 1 por causa de `tests/core/test_pipeline.py::TestCognitivePipeline::test_process_greeting`, que afirmou falhar também na `origin/main` limpa.
- **Conferido pelo orquestrador, contra o relato:**
  - A minha primeira execução limpa passou (965 testes), e a execução com a mudança falhou. Isso contradizia o relato, então medi em vez de aceitar. O teste afirma `result.processing_time_ms > 0`, e o pipeline calcula `int(sum(stage_timings.values()))`: quando o turno leva menos de 1 ms, o valor arredonda para 0.
  - 5 execuções do comando de validação numa cópia limpa e destacada de `origin/main`: o teste falhou em 3. Com a mudança: falhou em 3 de 5.
  - Evidência formal, 8 + 8 execuções (`$EV/flake-test_process_greeting.txt`): com a mudança passou em 3 de 8; na `origin/main` limpa falhou em 4 de 8; nenhuma outra falha nas 16 execuções. Sozinho, passou 5 de 5 no worktree.
  - Na medição inicial da suíte completa ele não aparece entre as 22 falhas: lá passou.
- **Diagnóstico:** teste instável preexistente (`env-tool-failure`, não é do código da mudança). A hipótese de que os testes novos o piorariam não se sustentou: a taxa é a mesma com e sem a mudança. Sem escalada de modelo ou esforço.
- **Rotas descartadas:** consertar o teste (editar teste existente fora do escopo; correção incidental vai em branch próprio); aceitar um `exit 1` na validação (um gate vermelho não prova nada); mudar a ordem dos testes para escondê-lo.
- **Decisão:** o teste entra em `$EV/known-flakes.txt`, com a evidência acima, e a validação do Grupo 2 passa a descontar só esse id. A mesma lista é usada pelo gate final do Grupo 3. Nova validação gravada neste wish. Se o owner preferir consertar o teste, isso fica para um PR separado.
- **Revisão independente do Grupo 2 (2026-11-14T05:39:40Z):** SHIP, diff sha256 `f9b7708a12a5c0204ac90752793d2c9ccd691e368cbe6792cd9b1360edb258c5`. O revisor derrubou a regra com 8 versões erradas na cópia de teste (`bool(value)`, com `strip`, com grafias extras, sempre verdadeira, sempre falsa, sem `.lower()`, sem `"none"`) e todas ficaram vermelhas, e confirmou o teste instável por conta própria (2 de 6 falhas na `origin/main` limpa, 1 de 6 com a mudança, sempre `assert 0 > 0`). O espelho do `test_engine.py` sobrevive a `bool(...)` de propósito (só recebe `True`/`False`).
- **Pontos médios da revisão, aplicados pelo orquestrador depois do SHIP:** (1) o laço da lista de falhas conhecidas não lia a última linha sem quebra de linha, então um id sem evidência passava (reproduzido) — agora `|| [ -n "$id" ]`, nos blocos do Grupo 2 e do Grupo 3; (2) o desconto perdoava qualquer falha do teste instável, mesmo por outro motivo — agora a evidência tem `reason_line: E   AssertionError: assert 0 > 0` e a validação exige essa linha na saída quando o teste falha; (3) duas condições da triagem (modalidade já escolhida; turno e intenção) não tinham teste pelo pipeline real: o engenheiro acrescenta dois casos, só linhas novas. Pontos baixos aplicados: saída com cor reprova e o pytest roda com `--color=no`; os testes de `"no"` e `"yes"` afirmam `is False` e `is True`; `clinic_accepts_insurance` entra em `__all__`; o docstring do módulo passa a dizer "três peças"; o teste de paridade também compara as grafias do módulo com as do núcleo. Essas mudanças pós-SHIP são cobertas pela revisão final do branch inteiro, que recebe a lista delas.
- **Fora do escopo, registrado:** `clinic_config_table.py:145` (`to_pydantic`) coage `accepts_insurance` com o `bool` do pydantic (`"off"` vira `False`, `"não"` é recusado), diferente da regra do núcleo; é o item de revalidar `business_rules` que o wish deixa em OUT, e `Tenant.get_clinic_config` não tem chamadores hoje.
- **Validação do orquestrador depois das emendas:** o bloco do Grupo 2, extraído do wish sem alteração, saiu `exit 0` em 4 de 4 execuções (códigos lidos no terminal; o registro com os códigos gravados está em `$EV/orchestrator-g2-validation-runs.txt`) (`989 passed`; na quarta o teste instável falhou com `assert 0 > 0` e foi descontado: `1 failed, 988 passed`). A lógica nova do bloco foi simulada antes com 10 situações: sem falha, falha do teste instável pelo motivo certo (com e sem quebra de linha no fim da lista), motivo diferente, outro teste falhando, erro de coleta, saída com cor, lista vazia, id sem evidência como última linha e ausência de linha `passed` — todas responderam como esperado. Mutações dos dois casos novos (`$EV/g2-mutations.txt`): sem `not atendimento_tipo`, o caso da modalidade escolhida falha (`1 failed`); sem a condição de turno/intenção, o caso da primeira saudação falha (`1 failed`). O diff de `src/core/pipeline.py` voltou idêntico depois das mutações. A única linha removida em teste existente é `bool(accepts_insurance)`.
- **Resultado do engenheiro, confirmado depois:** `clinic_accepts_insurance` replica a regra do núcleo sem `strip` e sem grafias extras; o teste da pipeline com `"no"` falha com o `bool(...)` antigo (`g2-revert.txt`: `1 failed, 1 passed`, `exit 1`) e passa com a correção.

### Execution review — Grupo 3 (2026-11-14T06:23:03Z)

- **Verdict:** SHIP (nenhuma falha crítica ou alta; 2 médias e 4 baixas)
- **Reviewed:** `git diff` sha256 `194c7ca663e5558b2c9b8cb538715921ed01ad9ccfe842071a8b0deea4676ec7`; `clinic-config-scheduling-link.test.tsx` sha256 `de63e616e76f9def40ab06d45fe28b5de9107d8754eb2010d25e211b742d43c7`
- **Reviewer:** genie:reviewer (a4510959; não é o engenheiro do grupo)
- **Confirmado:** a checagem roda depois da validação do bloco e antes de `setattr`/`flush`/`commit`; blocos anteriores já estão no objeto em memória, mas a sessão nunca faz commit numa exceção (`src/repositories/database.py:165-171`), o mesmo caminho do `ValidationError`→422 que já existia; a ordem dos blocos não importa. O revisor derrubou o código com 9 mutantes (apagar a checagem, tirar o caso (b), (a) sempre verdadeiro, toggle `"no"` como ligado antes, checar depois do `setattr`, falhar fechado, ignorar a URL, comparar mensagem limpa, ecoar a mensagem no `msg` ou no log). Dois sobreviveram e são equivalentes. Caminhos de escrita alternativos: ver a linha de risco. O aviso da tela bate com o wish nos dois idiomas (comparação mecânica), o vitest renderiza `ClinicConfig` com o link ligado, e nenhum teste existente fixava o texto antigo. Importar `scheduling_gate` em `config.py` não duplica métrica nem cria ciclo (3 interpretadores novos); só `importlib.reload` quebra, e ninguém recarrega o módulo.
- **Gaps:**
  1. [MEDIUM] nenhum teste fixava que o lado "depois" lê o bloco validado: um mutante que passa o dicionário bruto sobrevivia. **Aplicado** pelo engenheiro, com dois casos novos e prova de mutação.
  2. [MEDIUM] o detector recusa frases de instrução comuns. **Registrado** na linha de risco, com as frases medidas. Mudar o detector fica fora do escopo.
  3. [LOW] o mutante sem `.strip()` sobrevivia: linha nova na tabela-verdade. **Aplicado.**
  4. [LOW] o teste de fail-open só trocava o detector inteiro. **Aplicado:** um padrão que levanta dentro de `finditer`.
  5. [LOW] sem caso com dois blocos na mesma requisição, e leitura de volta pela função de achatar em vez da rota de leitura. **Aplicado:** caso misto (`timezone` + promessa) afirmando `session.calls == []`.
  6. [LOW] a frase do risco do relógio dizia que o Grupo 3 não toca `tests/core`. **Corrigida.**
- **Rodada final interrompida de propósito:** a rodada final de gates tinha começado às 06:13Z quando a revisão voltou pedindo testes novos. Parei no início (sem arquivo `final-*` gerado, sem processo solto) para não validar uma árvore que ia mudar. Será refeita sobre a árvore final, e a impressão digital da árvore antes e depois da rodada vai para este bloco.

### Final review — branch inteiro (2026-11-14T06:39:50Z)

- **Verdict:** SHIP, condicionado à rodada final de gates ainda em andamento no momento da revisão (nenhuma falha crítica ou alta; 3 médias e várias baixas)
- **Reviewed:** `git diff` sha256 `902a0658e35fec3d1ed94461678535ea5cfa6ab726727fafd1c7bffd15a4ac12`; `tests/services/test_event_service_config_loader.py` `117bff147ac6ac05bd1a3eb2da0b74720af236712afad307f46a11bfc13b43f9`; `clinic-config-scheduling-link.test.tsx` `de63e616e76f9def40ab06d45fe28b5de9107d8754eb2010d25e211b742d43c7`
- **Reviewer:** genie:reviewer (a67496f6; não participou de nenhum grupo)
- **Confirmado:** critérios de sucesso 1 a 6 atendidos; regra de só adicionar em teste existente (a checagem imprime só `bool(accepts_insurance)`); escopo de 12 arquivos permitidos, `.genie` ignorado; critérios dos três grupos atendidos, com a ressalva do desconto do teste instável. Dependem da rodada final: critérios 7 a 9 e o 3 do Grupo 3. A consulta de inventário rodou nas 10 linhas sintéticas do revisor; mutações novas dos casos do Grupo 2 e do Grupo 3 foram tentadas na cópia (os sobreviventes estão na linha de risco).
- **Gaps e tratamento (pelo orquestrador, sem tocar código nem teste, porque a rodada final estava em andamento):**
  1. [MEDIUM] o desconto do teste instável deixava passar um `ERROR` dele sem checar o motivo. **Corrigido** nos blocos do Grupo 2 e do Grupo 3 (`ERROR <id>` reprova sempre), simulado com 10 situações contra o texto real do wish.
  2. [MEDIUM] o QA ao vivo ignorava a janela de 15 minutos do cache. **Corrigido:** aviso antes do caso 1.
  3. [MEDIUM] o primeiro deploy expõe clínicas reais ao fluxo antes do QA. **Registrado como decisão do owner** no inventário, com as duas opções, a recomendação e o aviso de que desligar o link pela tela apaga URL e mensagem (`clinic-config.tsx:1791-1792`).
  4. [LOW] o preço das clínicas com `"no"` não estava no inventário nem no QA. **Corrigido:** a consulta 2 agrupa também por `pricing_mode` e `provide_prices`, e o QA ganhou o caso 6b.
  5. [LOW] mutantes que sobrevivem (`or` por `and` e um só lado da condição de turno/intenção em `pipeline.py:948-952`; coerção do "antes" pelo `ClinicScheduling`). **Registrado** em linha de risco como acompanhamento opcional: acrescentar os casos muda a árvore e refaz a rodada de uma hora.
  6. [LOW] a consulta SQL ignorava bloco que não é objeto e escondia o tamanho de URL só com espaços. **Corrigido** e validado num PostgreSQL 16 descartável (8 linhas sintéticas; a transação `READ ONLY` recusa escrita).
  7. [LOW] deriva do texto do wish: "Em aplicação" virou "Aplicado"; a evidência dos testes instáveis descrita como ela é; "abaixo" virou "acima"; a leitura de volta é pelo `flatten_config`; o bloco do Grupo 1 passou a usar `--deselect` do teste instável, porque não o descontaria; a impressão digital da árvore tem o comando gravado em `$EV/fingerprint.sh`.
  8. [LOW] documentação: `WISH.md:16` linka `subagente-agendamento/DRAFT.md`, que não está no worktree nem em `origin/main`; a linha do INDEX do checkout principal estava desatualizada. **Fica para o commit de documentação**, que o owner decide (incluir o rascunho do guarda-chuva ou deixar o link sem destino).
- **Plano de commits proposto pelo revisor** (conjuntos de arquivos sem sobreposição): (1) `fix(runtime)`: `event_service.py` + testes do carregador e do cache; (2) `fix(pipeline)`: módulo de preço, `pipeline.py`, testes de política, pipeline e `srm`; (3) `feat(config)`: `config.py`, `test_config_service.py`, `clinic-config.tsx` e o teste de tela; (4) `docs(genie)`: wish, design e rascunho com `git add -f`. Cada assunto cita o slug. Nada disso foi executado.

### Final gate — Grupo 3 e branch inteiro (2026-11-14, rodado pelo orquestrador)

- **Resultado:** o bloco de validação do Grupo 3 saiu `validation exit=0` (lido do próprio arquivo de saída, não do código do invólucro). Ele rodou, sobre a árvore definitiva: os testes de rota e de API, os testes de página do frontend, `run-gates.sh final` (formatação, lint, tipos, backend completo, tipos e testes do frontend), a comparação com a medição inicial e as checagens de escopo e de só adicionar linhas.
- **Árvore:** a impressão digital da árvore (`$EV/fingerprint.sh`) antes e depois da rodada é a mesma, `0f212b648b5571030fd3348081201da041532384ce5cfd01ec7569e31fc01e4d`: nada foi editado durante a rodada.
- **Números, medição inicial (`origin c91cea30`) contra o final:**

  | Gate | Inicial | Final |
  |---|---|---|
  | black + isort | exit 0 | exit 0 |
  | flake8 | exit 0 | exit 0 |
  | mypy | exit 0 | exit 0 |
  | typecheck do frontend | exit 0 | exit 0 |
  | pytest | 22 falhas, 16135 passados, 1774 pulados, 104 erros (exit 1) | 22 falhas, 16202 passados, 1774 pulados, 104 erros (exit 1) |
  | frontend (vitest) | 45 arquivos, 1132 testes | 46 arquivos, 1136 testes, tudo passando |

- **Falhas do backend:** os conjuntos de ids de falha e de erro são idênticos nos dois sentidos (126 em cada lado, nenhum só de um lado). Nenhuma falha nova. O teste instável `test_process_greeting` passou na rodada completa e não aparece entre as falhas. A contagem de testes aprovados subiu em 67, os testes novos dos três grupos.
- **Comparação com o texto atual do bloco:** a rodada usou a versão do bloco anterior à guarda de `ERROR`. Depois da emenda, a parte de comparação do bloco atual foi refeita sobre os mesmos arquivos `final-*` e saiu `exit 0` (`$EV/orchestrator-g3-compare-current.txt`).
- **Blocos dos outros grupos, texto atual, códigos gravados** (`$EV/orchestrator-g2-validation-runs.txt`): Grupo 2 em 3 execuções, `exit 0` nas três, com o teste instável falhando e sendo descontado nas três (`1 failed, 988 passed`); Grupo 1 com `--deselect` do teste instável, `exit 0`.
- **Observação:** os horários que aparecem nos arquivos de evidência vêm do relógio do WSL, que anda para trás; nenhuma conclusão deste wish depende de horário de arquivo.
- **Situação do wish:** `IN_PROGRESS`. Nenhum commit, push ou PR foi feito. A execução parou no ponto em que a decisão é do owner: commit e PR, as decisões abertas da linha de risco, o inventário nos dois ambientes, o QA ao vivo e o deploy.

---

## Files to Create/Modify

```
src/services/event_service.py                                  (carregador: + scheduling)
src/services/price_disclosure_policy.py                        (+ clinic_accepts_insurance)
src/core/pipeline.py                                           (linha 943)
src/api/routes/config.py                                       (checagem da mensagem)
frontend/artifacts/neurostack/src/pages/clinic-config.tsx      (hint do campo)
tests/services/test_event_service_config_loader.py             (novo: carregador e contrato com o SRM)
tests/services/test_event_service_config_cache.py              (fixtures: atributo scheduling nas 2 linhas falsas; só adiciona)
tests/services/test_price_disclosure_policy.py                 (paridade; só adiciona)
tests/core/test_pipeline.py                                    (triagem "no"/"yes"; só adiciona)
tests/srm/test_engine.py                                       (_compute_gating: uma linha trocada)
tests/core/test_config_service.py                              (classe nova: checagem da mensagem)
frontend/artifacts/neurostack/src/pages/clinic-config-scheduling-link.test.tsx  (novo: hint e erro)
```
