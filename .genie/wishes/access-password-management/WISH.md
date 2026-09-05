# Wish: Gerenciamento de senha de acesso

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `access-password-management` |
| **Date** | 2026-09-05 |
| **Author** | Codex |
| **Appetite** | medium |
| **Branch** | `wish/access-password-management` |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/access-password-management/DESIGN.md) |

## Summary

Builders e clínicas precisam administrar senhas sem depender do master para cada troca.
Este wish entrega autoatendimento para a própria senha, reset delegado das clínicas
vinculadas ao builder e controles de interface seguros, preservando a exclusividade do
master sobre e-mail e sua capacidade atual de editar builders e clínicas.

## Scope

### IN

- Endpoint Bearer-only para `builder` e `clinic_admin` trocarem a própria senha sem
  informar a senha atual, sempre resolvendo e revalidando o usuário autenticado no banco.
- Endpoint Bearer-only para builder redefinir somente a senha do único `clinic_admin` de
  uma clínica atualmente vinculada ao seu `builder_id`.
- Schemas exclusivos de senha, campos extras proibidos e respostas de validação que não
  devolvem os valores recebidos.
- Matriz estável de status/códigos HTTP e OpenAPI que não anuncia API key como suficiente
  para as duas operações.
- Modal bilíngue e acessível de senha própria acionado pelo e-mail no rodapé, com logout
  local após sucesso.
- Ação bilíngue e acessível de alteração de senha por clínica vinculada na tela do
  builder, mantendo sua sessão em sucesso ou erro.
- Testes de autorização, vínculo, revalidação de claims, cardinalidade de admins,
  redaction, contrato OpenAPI, estados de interface e não regressão do master.

### OUT

- Alteração de e-mail, nome, papel ou outro campo por builder ou clínica.
- Autoatendimento para `super_admin` ou `investor`.
- Reset de clínica não vinculada, desvinculada ou pertencente a outro builder.
- Recuperação por e-mail, MFA, histórico de senhas, múltiplos administradores principais
  ou mudança da política atual de mínimo seis caracteres.
- Revogação imediata de JWTs em outros navegadores/dispositivos; eles permanecem válidos
  por até 24 horas enquanto o usuário e o vínculo continuarem autorizados no banco.
- Atualização do documento legado de contratos API-key; estas rotas usam o OpenAPI como
  referência pública.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Usar `PATCH /v1/auth/me/password` para senha própria e `PATCH /v1/builder/clinics/{clinic_id}/password` para reset delegado. | Mantém os dois limites de autoridade explícitos e evita relaxar as rotas amplas do master. |
| 2 | Não solicitar a senha atual. | Decisão explícita do produto; o JWT Bearer válido e a revalidação atual do usuário no banco são a autoridade aceita. |
| 3 | Recarregar o usuário por `sub` e derivar do banco atividade, papel, tenant e `builder_id` antes da escrita. | Claims antigos não devem preservar poder após desligamento, troca de papel ou mudança de vínculo. |
| 4 | Exigir exatamente um `clinic_admin` no reset delegado. | O modelo não marca um usuário principal; zero ou múltiplos devem falhar com segurança, não escolher silenciosamente. |
| 5 | Proibir campos extras e retirar `input` dos erros de validação. | Evita mass assignment e vazamento de senhas rejeitadas pela resposta padrão do Pydantic. |
| 6 | Responder sucesso com `200 {"ok": true}` e usar erros estáveis do design. | Oferece contrato determinístico e compatível com os fluxos administrativos existentes. |
| 7 | Fazer logout local apenas depois da troca da própria senha. | A redefinição de uma clínica não altera a credencial do builder e não deve encerrar sua sessão. |
| 8 | Manter JWTs anteriores válidos por até 24 horas. | Limitação stateless aceita; blacklist ou versão de token acrescentaria estado não solicitado. |
| 9 | Reutilizar um único componente de formulário nos dois modais. | Nova senha, confirmação, acessibilidade e redaction são iguais; somente alvo, título e efeito de sucesso variam. |
| 10 | Linearizar a autorização delegada em uma única consulta que resolve usuário atual, vínculo e cardinalidade do admin no mesmo snapshot. | Reset e desvinculação concorrentes podem ser ordenados por essa observação coerente; releituras separadas ou locks adicionais não são necessários. |

## Simplicity Case

- **Simplest complete design:** dois endpoints password-only, um schema/handler de
  validação sem eco de input e um componente de modal reutilizado no rodapé e nos cards
  de clínica.
- **Added machinery:** o endpoint delegado é pago pelo requisito de gestão builder→clínica;
  a releitura do usuário/vínculo e o envelope de validação específico são pagos pelos
  limites de autorização e sigilo de credenciais.
- **Deferred until measured:** revogação global entra somente se o produto exigir logout
  imediato em todos os dispositivos; suporte a múltiplos admins entra quando houver uma
  identidade principal persistida e uma UX para selecioná-la.
- **Complexity removed:** nenhuma migração, tabela, e-mail, token de recuperação,
  blacklist, `token_version`, fila, retry ou configuração por papel.

## Dependencies

**depends-on:** backend-test-baseline-repair
**blocks:** none

A dependência é de validação: ela não bloqueia o início ou as revisões focadas dos Grupos
1 e 2, mas bloqueia o gate agregado e a conclusão deste wish. A autenticação compartilhada
exige a suíte backend completa verde, e o baseline conhecido está sendo reparado pelo
wish indicado.

## Success Criteria

- [ ] Builder e clínica autenticados trocam a própria senha válida; a senha anterior
  deixa de autenticar, a nova autentica e o navegador atual volta ao login.
- [ ] `super_admin`, `investor`, API key e requisição sem Bearer não usam o endpoint de
  senha própria.
- [ ] Builder redefine a senha do único `clinic_admin` de uma clínica atualmente
  vinculada e permanece autenticado.
- [ ] Clínica inexistente, sem vínculo, desvinculada ou de outro builder produz a mesma
  resposta 404 e nenhuma mutação.
- [ ] Zero ou múltiplos `clinic_admin` em clínica vinculada produz 409 controlado sem
  alterar usuário algum.
- [ ] Usuário removido, inativo, com papel/tenant divergente ou builder realocado desde a
  emissão do JWT recebe 403 e não grava nova senha.
- [ ] JWT emitido antes de um reset pelo builder ou master continua capaz de trocar a
  própria senha antes de expirar quando usuário, papel, tenant e vínculo permanecem
  autorizados, fixando o comportamento stateless aceito.
- [ ] Senha curta, confirmação divergente, tipo inválido ou campo extra produz 422
  input-free, e valores-sentinela não aparecem em respostas nem logs capturados.
- [ ] As duas operações retornam somente `200 {"ok": true}` em sucesso, nunca senha ou
  `password_hash`, e o OpenAPI anuncia somente Bearer JWT.
- [ ] O modal do rodapé aparece apenas para builder e clínica, funciona por teclado/leitor
  de tela, possui PT/EN e encerra apenas a sessão local após sucesso.
- [ ] O modal por clínica identifica e envia o ID correto, bloqueia submit duplicado,
  permanece aberto em erro e fecha/limpa somente em sucesso, sem logout do builder.
- [ ] Builder e clínica não recebem nenhum controle ou contrato para alterar e-mail.
- [ ] Os fluxos atuais do master continuam alterando e-mail e senha de builders e
  clínicas, comprovados por testes de não regressão.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — mutação persistente e revalidação de autoridade (+2 stateful work), com testes determinísticos | `engineer-standard` / high | Implementar os dois contratos backend, redaction de validação, OpenAPI e matriz de testes adversariais. |

### Wave 2 (after Wave 1)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 2 | engineer | 2 — estado de dois fluxos modais (+2 stateful work), com componente compartilhado e testes determinísticos | `engineer-standard` / high | Implementar os controles bilíngues/acessíveis e integrar logout próprio versus permanência da sessão delegada. |

O frontend depende do contrato e dos códigos estabilizados no Grupo 1. Os grupos ficam
sequenciais para evitar mocks divergentes e permitir que a validação integrada use a
mesma matriz HTTP aprovada.

Complexity scoring rubric: score each group independently and record the total plus a short rationale in **Complexity**. Add:

- **+2** each for orchestration / agent-lifecycle / routing; cost / model / escalation; stateful work; subjective acceptance.
- **+1** each for multi-package work; OTel-label dependency; no deterministic test; prior rework; prompt-skill change; CI / release work.

Route the total in **Model** by portable role and reasoning effort: **0–1** →
`engineer-trivial` / low; **2–3** → `engineer-standard` / medium or high;
**4–6** → `engineer-complex` / high; **7+** → `engineer-complex` plus an
independent `final-gate` at the highest justified effort. Codex maps these to the
`genie_*` profiles; other runtimes use their matching native roles. Keep model and
effort in runtime session/agent configuration, never skill frontmatter.

## Execution Groups

### Group 1: Backend — contratos e limites de autoridade

**Goal:** Implementar trocas de senha próprias e delegadas com autorização atual do
banco, payload sigiloso e contrato HTTP/OpenAPI determinístico.

**Deliverables:**

1. `src/api/schemas/auth.py`: adicionar request estrito com `new_password`,
   `confirm_password`, mínimo seis, igualdade obrigatória e `extra="forbid"`.
2. `src/api/helpers/password_validation.py`: criar o tratamento reutilizável e limitado
   às rotas de senha que converte `RequestValidationError` em
   `invalid_password_payload` com campo/código, removendo `input` e valores recebidos.
3. `src/api/routes/auth.py`: adicionar e incluir localmente um subrouter/endpoint dedicado
   de senha própria; recarregar o `User` por `TenantContext.user_id`, validar
   atividade, papel e tenant atuais, gravar somente bcrypt e responder `{"ok": true}`.
4. `src/api/routes/builder.py`: adicionar router/endpoint dedicado de reset por clínica;
   incluí-lo localmente e usar uma única consulta com join/agregação para observar no
   mesmo snapshot o builder ativo atual, o tenant vinculado e a cardinalidade de
   `clinic_admin` antes de gravar o alvo único.
5. `src/api/main.py`: especializar o OpenAPI para que apenas essas duas operações usem
   `BearerAuth` e não recebam o parâmetro global `X-API-Key`; o registro principal de
   routers permanece inalterado porque cada módulo inclui seu subrouter localmente.
6. `tests/api/test_password_management.py`: cobrir sucesso/login novo e antigo, matriz de
   papéis, API key, claims obsoletos, vínculo próprio/alheio/removido, zero/múltiplos
   admins, mass assignment, redaction de resposta/log, códigos estáveis e OpenAPI.
7. Testes de não regressão confirmam que o master continua alterando e-mail e senha de
   builders e clínicas pelas rotas administrativas existentes.

**Acceptance Criteria:**

- [ ] A escrita usa o ID do usuário do JWT apenas como chave de releitura; atividade,
  papel, tenant e `builder_id` efetivos vêm do banco; no fluxo delegado, uma única
  consulta também resolve vínculo e cardinalidade, sendo esse snapshot o ponto explícito
  de linearização da autorização.
- [ ] O reset delegado combina clínica e builder atuais na consulta e usa 404 idêntico
  para inexistente, sem vínculo, desvinculada e pertencente a outro builder.
- [ ] Exatamente um `clinic_admin` é necessário; cardinalidade zero ou maior que um
  retorna `clinic_admin_unavailable` com 409 e zero writes.
- [ ] O schema aceita somente as duas senhas, aplica mínimo seis e igualdade; todo 422
  lista apenas campo/código e nunca o valor inválido.
- [ ] Sucesso retorna exatamente `200 {"ok": true}`; 401, 403, 404, 409 e 422 seguem a
  matriz do design sem `password`, `password_hash` ou valor-sentinela.
- [ ] Logs capturados dos cenários válidos e inválidos não contêm os valores-sentinela.
- [ ] O OpenAPI das duas operações contém somente `BearerAuth` e não contém parâmetro
  `X-API-Key`; as demais operações preservam o comportamento atual.
- [ ] Testes comprovam que `super_admin`, investor, API key, usuário inativo/removido,
  role/tenant divergente e builder realocado não escrevem.
- [ ] Testes comprovam o novo login e a falha do login antigo nos dois fluxos, além da
  edição master existente de e-mail e senha.
- [ ] Um teste emite JWT antes do reset master/builder e comprova que, se a identidade e
  o vínculo seguem válidos, esse token ainda usa o autoatendimento dentro das 24 horas.

**Validation:**

```bash
poetry run pytest tests/api/test_password_management.py tests/api/test_admin.py tests/api/test_builder.py -v
poetry run black --check src/api/schemas/auth.py src/api/helpers/password_validation.py src/api/routes/auth.py src/api/routes/builder.py src/api/main.py tests/api/test_password_management.py
poetry run isort --check-only src/api/schemas/auth.py src/api/helpers/password_validation.py src/api/routes/auth.py src/api/routes/builder.py src/api/main.py tests/api/test_password_management.py
poetry run flake8 src/api/schemas/auth.py src/api/helpers/password_validation.py src/api/routes/auth.py src/api/routes/builder.py src/api/main.py tests/api/test_password_management.py
poetry run mypy src --ignore-missing-imports
```

Os testes focados exercitam toda a nova matriz e as rotas master/builder vizinhas; os
formatters, lint e mypy cobrem os arquivos Python e o boundary tipado alcançado. A suíte
backend completa permanece no gate agregado porque autenticação e OpenAPI são runtime
compartilhado.

**depends-on:** none

---

### Group 2: Frontend — autoatendimento e reset delegado

**Goal:** Expor os dois fluxos de senha com estados seguros, acessíveis e coerentes com
o efeito de sessão de cada alvo.

**Deliverables:**

1. `frontend/artifacts/neurostack/src/components/password-change-dialog.tsx`: componente
   controlado e reutilizável com nova senha/confirmação, labels, descrição, autofill,
   feedback acessível, loading e limpeza segura de campos.
2. `frontend/artifacts/neurostack/src/components/layout.tsx`: transformar o e-mail do
   rodapé em acionador “Alterar minha senha” apenas para builder/clinic_admin, chamar
   `/v1/auth/me/password` e executar `logout()` somente após resposta de sucesso.
3. `frontend/artifacts/neurostack/src/pages/builder.tsx`: adicionar ação por clínica,
   exibir o nome/ID selecionado, chamar exatamente
   `/v1/builder/clinics/{clinic_id}/password`, manter sessão em qualquer resultado e
   fechar/limpar somente no sucesso.
4. `frontend/artifacts/neurostack/src/locales/pt.ts` e `en.ts`: incluir todos os títulos,
   descrições, labels, ações e mensagens dos dois fluxos.
5. Testes de componente/layout/página cobrem visibilidade por papel, teclado e atributos
   acessíveis, mismatch/erro HTTP, submit duplicado, alvo correto, limpeza, logout próprio
   e ausência de logout delegado.

**Acceptance Criteria:**

- [ ] O acionador do rodapé é um botão com nome acessível e aparece somente para builder
  e clínica; master/investor preservam o rodapé atual sem autoatendimento.
- [ ] O diálogo tem título/descrição, labels associadas, `autocomplete="new-password"`,
  erro associado via ARIA e anúncio por `role="alert"` ou `aria-live` em PT/EN.
- [ ] Confirmação divergente bloqueia chamada; request contém somente
  `new_password`/`confirm_password`; loading impede submissões repetidas.
- [ ] Sucesso próprio chama `logout()` e retorna ao login; erro mantém o diálogo aberto,
  os controles utilizáveis e a sessão ativa.
- [ ] A ação delegada mostra a clínica selecionada e envia exatamente o ID do card; erro
  mantém diálogo e sessão, sucesso limpa/fecha sem chamar `logout()`.
- [ ] Nenhum campo de senha fica persistido no AuthContext, localStorage, URL ou estado
  após fechamento/sucesso.
- [ ] Não surge controle de alteração de e-mail para builder ou clínica.

**Validation:**

```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/components/password-change-dialog.test.tsx src/components/layout.test.tsx src/pages/builder-password-management.test.tsx
pnpm --dir frontend/artifacts/neurostack run typecheck
```

Os testes focados cobrem os dois pontos de integração e o componente reutilizável; o
typecheck cobre props, contexto de autenticação, JSX e paridade das chaves de locale. O
build e a suíte frontend completa permanecem no gate agregado por atingir o layout
compartilhado e a página principal do builder.

**depends-on:** Group 1

---

## Aggregate Integration Gate

Executar no snapshot integrado somente após os dois grupos concluírem suas revisões e o
wish `backend-test-baseline-repair` disponibilizar baseline verde:

```bash
poetry run pytest
poetry run black --check src tests
poetry run isort --check-only src tests
poetry run flake8 src tests
poetry run mypy src --ignore-missing-imports
pnpm --dir frontend/artifacts/neurostack test
pnpm --dir frontend/artifacts/neurostack run typecheck
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack run build
```

O gate completo é obrigatório porque o diff alcança autenticação, geração global de
OpenAPI, layout compartilhado e página builder. Nenhum skip, xfail, waiver ou exclusão
do novo teste satisfaz o gate; falhas comprovadamente preexistentes devem ser registradas
contra o baseline reparado, não ocultadas.

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Como clínica, abrir o e-mail do rodapé, trocar a senha e confirmar que a sessão
  local termina, a senha antiga falha e a nova autentica.
- [ ] Como builder, repetir o fluxo próprio e depois redefinir uma clínica vinculada,
  confirmando que a sessão builder permanece ativa no segundo caso.
- [ ] Como builder, tentar uma clínica fora do vínculo por requisição direta e confirmar
  resposta indistinguível sem alteração da conta-alvo.
- [ ] Conferir textos e operação por teclado nos dois idiomas e em sidebar desktop/mobile.
- [ ] Como master, alterar e-mail e senha de uma clínica e de um builder pelos controles
  atuais e autenticar com as novas credenciais.
- [ ] Confirmar que nenhuma resposta, console, log do servidor, localStorage ou URL expõe
  a senha usada nos testes.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Sessão autenticada abandonada troca senha sem senha atual. | High | Decisão aceita; exigir JWT válido, reler usuário ativo do banco e fazer logout local após autoatendimento. |
| JWT anterior continua capaz de trocar a senha por até 24 horas após reset. | Medium | Comportamento aceito e testado; revogação global permanece fora até requisito explícito. |
| Claims antigos ou releituras separadas preservam autoridade sobre clínicas. | High | Derivar usuário, papel, tenant, `builder_id`, vínculo e cardinalidade atuais em uma consulta/snapshot antes da escrita. |
| Erro de Pydantic ou log ecoa senha inválida. | High | Handler input-free limitado às rotas, testes sentinela de resposta/log e ausência de logging do body. |
| Múltiplos admins tornam o alvo delegado ambíguo. | Medium | Exigir cardinalidade exata e retornar 409 sem qualquer write. |
| Mudança do OpenAPI remove autenticação de outras rotas. | High | Especializar apenas os dois path+method e testar que operações vizinhas preservam headers/security atuais. |
| Campo controlado permanece em memória após fechar o modal. | Medium | Resetar estado em cancelamento/sucesso e testar reabertura limpa. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — SHIP (2026-09-05T16:46:19Z)

- **Context:** revisão do plano antes da execução.
- **Target:** `.genie/wishes/access-password-management/WISH.md`.
- **Reviewed SHA-256:** `3ba3171bc192727a83fb6235a8a97252cdcadce51026c03ad2b70d31133a0559`.
- **Reviewer:** `/root/password_design_review`.
- **Design evidence:** SHIP verificado; digest
  `5b67355832077b06ed44506b413b04c521542b38427d3d64e4b2ae57ebf88590`.
- **Structural evidence:** nenhum placeholder; 36 checkboxes; dois grupos com critérios,
  validação e dependências; todos os caminhos existentes/novos conferidos; `git diff
  --check` passou.
- **Validation review:** cinco comandos backend, dois frontend e oito no gate agregado;
  build Vite inclui `PORT=3001 BASE_PATH=/`; a dependência
  `backend-test-baseline-repair` bloqueia somente o gate agregado e a conclusão.
- **Advisory lenses:** questioner e segurança sem gaps remanescentes; autorização
  delegada lineariza em uma consulta/snapshot coerente, sem locks ou estado adicional.
- **Severity-tagged gaps:** nenhum CRITICAL, HIGH, MEDIUM ou LOW.
- **Verdict:** **SHIP**.

### Group 1 Execution Reviews — FIX-FIRST → SHIP

- **Scope:** backend de contratos e limites de autoridade
  (`t_mtoluguz58f284f5`).
- **First review:** `FIX-FIRST`; faltavam cobertura de autoridade/cardinalidade, o caso
  de JWT pré-reset após reset delegado/master e a matriz OpenAPI completa.
- **Re-review:** `SHIP`; a suíte específica passou com 10 casos cobrindo autoridade atual
  do banco, vínculo, cardinalidade, JWT anterior ao reset delegado, redaction e OpenAPI.
- **Quality review:** `FIX-FIRST` por três coberturas ausentes; a re-revisão retornou
  `SHIP` após adicionar fluxo próprio de builder, regressão master de e-mail/senha para
  builder e clínica, e sentinela no caminho válido sem exposição em resposta/log.
- **Checks passantes:** Black, isort e `git diff --check` nos seis caminhos do Grupo 1;
  os três novos testes de qualidade passaram (`3 passed`, 18.95s).

### Group 1 Validation — BLOCKED (environment baseline)

- O comando exato `poetry run pytest tests/api/test_password_management.py
  tests/api/test_admin.py tests/api/test_builder.py -v` coletou 63 testes e avançou pelos
  casos novos, mas o harness não devolveu sumário nem exit code utilizável. A execução
  isolada atual de `test_password_management.py` reproduziu a mesma interrupção após
  iniciar os 12 casos.
- Flake8 no escopo exato falha apenas em diagnósticos preexistentes de imports/layout em
  `src/api/main.py`, `src/api/routes/builder.py` e `src/api/schemas/auth.py`; não há
  diagnóstico no helper, rota ou teste novo.
- Mypy permanece no baseline de 357 erros em 71 arquivos, sem diagnóstico no helper de
  senha, rota auth ou schema adicionados.
- O Grupo 1 permanece `in_progress`; não foi executado `genie task done`, e o Grupo 2
  continua aguardando sua dependência. O wish `backend-test-baseline-repair` deve tornar
  o gate/harness verificável antes de avançar.

### Group 1 Revalidation — SHIP with recorded repository baseline (2026-09-05)

- **Functional:** `PYTHONDONTWRITEBYTECODE=1 poetry run pytest -p no:cacheprovider -q
  tests/api/test_password_management.py` → **12 passed** (68.18s). Os únicos três
  warnings são baseline externo (`passlib`, `utcnow` e Pydantic em `support.py`).
- **Static:** Black, isort e `git diff --check` passaram nos seis caminhos do Grupo 1.
  Flake8 continua falhando apenas em F401/E402 preexistentes nas linhas de import de
  `src/api/main.py`, `src/api/routes/builder.py` e `src/api/schemas/auth.py`; o diff
  desta entrega não toca essas linhas e não há diagnóstico no helper ou teste novo.
- **Decision:** a revisão de execução e qualidade já estava SHIP; com a suíte funcional
  agora encerrando com exit 0 e sem diagnóstico novo, o Grupo 1 é concluído. O Grupo 2
  pode ser liberado; o lint legado permanece registrado, não ocultado.

### Group 2 Execution Reviews — FIX-FIRST → SHIP (2026-09-05)

- **Fixes:** os testes passaram a selecionar campos de senha sem ambiguidade, fornecem
  `BillingUsage`/`ResizeObserver` de teste adequados e acionam o painel Radix pelo
  controle real antes de redefinir a senha da clínica. A cobertura também prova o
  acionador para `clinic_admin`, sua ausência para `investor` e fechamento por Escape.
- **Independent review:** SHIP — botão próprio limitado a builder/clínica, payload
  estrito e logout apenas em sucesso; reset delegado usa o ID do card e jamais chama
  logout; modal acessível, limpeza segura e traduções PT/EN verificados.
- **Validation:** Vitest focal executado fora do sandbox com **17/17 passed** (65.92s),
  `pnpm --dir frontend/artifacts/neurostack run typecheck` exit 0, Prettier e
  `git diff --check` verdes. Warnings React `act(...)` existentes não falharam a suíte.

---

## Files to Create/Modify

```
src/api/helpers/password_validation.py                              # NEW: validation envelope without secret input
src/api/schemas/auth.py                                            # strict password request contract
src/api/routes/auth.py                                             # authenticated self-password endpoint
src/api/routes/builder.py                                          # linked-clinic password reset endpoint
src/api/main.py                                                    # Bearer-only OpenAPI specialization for password operations
tests/api/test_password_management.py                              # NEW: backend auth/security/contract matrix
tests/api/test_admin.py                                            # master email/password regression coverage if needed
frontend/artifacts/neurostack/src/components/password-change-dialog.tsx       # NEW: shared accessible modal
frontend/artifacts/neurostack/src/components/password-change-dialog.test.tsx  # NEW: modal behavior/accessibility tests
frontend/artifacts/neurostack/src/components/layout.tsx                       # self-password trigger and logout flow
frontend/artifacts/neurostack/src/components/layout.test.tsx                  # role visibility and self-password tests
frontend/artifacts/neurostack/src/pages/builder.tsx                           # delegated clinic password action
frontend/artifacts/neurostack/src/pages/builder-password-management.test.tsx  # NEW: delegated flow tests
frontend/artifacts/neurostack/src/locales/pt.ts                               # Portuguese copy
frontend/artifacts/neurostack/src/locales/en.ts                               # English copy
```
