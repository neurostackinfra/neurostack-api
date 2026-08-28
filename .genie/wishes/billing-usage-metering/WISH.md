# Wish: Medição de uso do plano

| Field | Value |
|-------|-------|
| **Status** | DONE |
| **Slug** | `billing-usage-metering` |
| **Date** | 2026-08-11 |
| **Author** | Codex |
| **Appetite** | large |
| **Branch** | `wish/billing-usage-metering` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |
| **Planning base SHA** | `7ab570ff0d10ed459e9e62bec5c8f32f88e94fff` |
| **Handoff card** | `t_msontnyf011833ab` |

O brainstorm foi dispensado porque o contrato, as fronteiras de escopo e a abordagem de entrega já estavam decididos, sem fork de produto ou arquitetura pendente.

## Summary

Este wish entrega observabilidade de consumo do plano Start por builder: traces mensais, armazenamento RAG e a capacidade ainda não disponível de mídia/créditos. Ele incorpora a revisão Alembic `016` com proveniência verificável, expõe o contrato no backend e apresenta o uso nas superfícies de billing e builder, sem cobrar, bloquear ou alterar limites em tempo de execução.

O commit identificado produzido por este workstream é o pré-requisito de `interaction-origin-training-safety`, cuja próxima migração deverá revisar exatamente a `016`.

## Scope

### IN

- Incorporar `alembic/versions/20260810_000001_016_add_knowledge_document_size.py` como head único sobre a `015`, com `knowledge_documents.size_bytes`, backfill UTF-8, constraint não negativa e downgrade simétrico.
- Manter `size_bytes` coerente no bootstrap, ORM e caminhos atuais de criação/alteração de documentos, com cobertura em PostgreSQL real.
- Agregar, por builder autenticado, traces `message.in` do mês-calendário UTC e o total persistido de bytes RAG em todas as clínicas pertencentes ao builder.
- Expor `GET /v1/billing/usage` e incluir o mesmo contrato em `GET /v1/billing/summary`, com limites Start configuráveis e isolamento entre builders.
- Exibir o resumo de uso localizado nas páginas de billing e builder, incluindo excedente, disponibilidade e estados honestos para mídia e compra de créditos.
- Documentar o contrato e produzir um handoff com commit SHA, blob SHA da `016` e evidência de um único head Alembic.
- Executar o trabalho em isolamento a partir do planning base, importando somente paths/hunks explicitamente permitidos e preservando o worktree compartilhado atual.

### OUT

- Enforcement de quota, bloqueio por excedente, compra/persistência de créditos ou mudanças na cobrança AbacatePay.
- Novos planos comerciais, seleção dinâmica de entitlements por plano ou alteração dos preços e limites vigentes.
- Persistência de mídia; até existir uma tabela de assets, o uso real de mídia permanece `0`.
- Correções globais de métricas em admin, health, builder analytics ou dashboard; em especial `src/api/helpers/health.py`, `src/api/routes/admin.py`, `src/api/routes/builder.py`, `frontend/artifacts/neurostack/src/pages/dashboard.tsx` e `dashboard-intents.test.tsx` ficam congelados.
- `interaction_origin`, filtros production/simulator, consentimento, RLS, datasets, treinamento, anonimização ou linhagem de modelos.
- Multimodal, credenciais de API, safe egress, clinic config ou qualquer outro arquivo já staged/dirty fora da allowlist deste wish.
- Garantia para writers SQL/bulk futuros que contornem os caminhos ORM atuais; o aparecimento de outro writer exige uma fronteira própria de sincronização.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Este wish mede uso, mas não aplica entitlement | Observabilidade é o requisito presente; enforcement e cobrança introduziriam estados e falhas não pedidos. |
| 2 | O período de traces é `[início do mês UTC, início do mês seguinte)` | A janela é inequívoca em virada de mês/ano e independe do timezone do cliente. |
| 3 | Um trace de billing é exatamente um evento `message.in` | Conta interações recebidas, não eventos internos derivados da mesma conversa. |
| 4 | O escopo inclui toda clínica com `Tenant.builder_id` correspondente | É a autoridade de ownership existente; estado ativo e `plan_type` continuam próprios do cálculo de preço, não do consumo observacional. |
| 5 | RAG usa bytes UTF-8 persistidos e MB decimal (`1 MB = 1.000.000 bytes`) | Evita carregar conteúdo para cada leitura e mantém um contrato de unidade explícito. |
| 6 | A `016` armazena e faz backfill de `size_bytes`; writers ORM atuais o recalculam | É a menor mudança compatível com o código existente; trigger/generated column fica adiado até existir writer fora da fronteira atual. |
| 7 | Mídia retorna zero e compra de créditos permanece desabilitada/`coming_soon` | Não existe persistência de mídia nem fluxo de compra que sustente outro valor. |
| 8 | Os três limites Start ficam configuráveis e não negativos | O produto atual vende somente Start, e ajustes comerciais não exigem release de código. |
| 9 | `/billing/usage` é builder-only e `/billing/summary` incorpora o mesmo objeto | Preserva uma fonte única de cálculo e atende as duas superfícies existentes. |
| 10 | A UI exibe percentual real de excedente, mas limita a barra acessível a 0–100 | Mantém a magnitude do excesso visível sem gerar um valor ARIA inválido. |
| 11 | A execução usa worktree/índice isolado a partir do SHA de planejamento | O worktree compartilhado contém mudanças alheias; a provenance da `016` não pode absorvê-las. |
| 12 | O card de handoff só termina depois de um commit convencional identificado | `interaction-origin-training-safety` precisa provar o arquivo e o head a partir de Git, não do filesystem local. |
| 13 | O gate backend agregado compara testcases falhos com um JUnit imutável do planning base e exige zero falhas novas | O planning base já possui 198 falhas; exigir exit `0` não distingue dívida preexistente de regressão deste wish. Testes focados continuam obrigados a passar integralmente. |

## Simplicity Case

- **Simplest complete design:** uma coluna de tamanho mantida nos writers atuais, duas agregações SQL builder-scoped, um contrato de resposta reutilizado por dois endpoints e um componente reutilizado por duas páginas.
- **Added machinery:** a migration/backfill é exigida por documentos legados; o smoke PostgreSQL é exigido por `octet_length`, Alembic e `CHECK`; limites configuráveis evitam codificar decisões comerciais; o gate de provenance existe porque a `016` desbloqueia outra cadeia de migrations e o worktree atual está misturado.
- **Deferred until measured:** cache/materialized view somente se a latência das duas agregações ultrapassar o orçamento do endpoint; índice adicional somente após `EXPLAIN` real; trigger/generated column quando surgir writer não ORM; planos múltiplos, mídia e compra de créditos quando houver contrato e persistência próprios.
- **Complexity removed:** nenhuma fila, cron, ledger de cobrança, contador incremental, reset mensal persistido, reclassificação histórica, enforcement, retry ou nova integração de pagamento.

## Shared Backend Aggregate Regression Gate

Este gate substitui somente a interpretação impossível de “`poetry run pytest` deve retornar exit `0`”. Ele não é waiver: cada grupo ainda executa a suíte backend completa fora do sandbox restrito, rejeita erro interno/coleta (`pytest` status maior que `1`) e compara os IDs `classname::testname` de todos os testcases JUnit. Nenhum ID do baseline pode desaparecer, enquanto testes adicionais que passam e falhas preexistentes resolvidas são permitidos; entre os elementos `<failure>`/`<error>`, a diferença `current - baseline` deve ser vazia, portanto nenhuma falha nova é aceita.

O baseline imutável foi produzido no planning base exato `7ab570ff0d10ed459e9e62bec5c8f32f88e94fff`, com o mesmo `poetry.lock` (`ee7555da0c75405a4f8c3579f9314f4629c1200287c88c4ddd995feaf6e4c6cf`): 1.130 casos, 198 falhas, JUnit SHA-256 `ac853995c1ddb3a03903c1b1d79e47bac1f8d4166a96ea1098c1bbc42a27949a`. Se esse artefato deixar de existir, ele deve ser regenerado do mesmo SHA em ambiente equivalente e o novo digest precisa de revisão independente antes de substituir o valor fixado.

Cada invocação usa o bloco exato abaixo, com ambas as variáveis definidas, `BILLING_BASE_JUNIT` apontando para o artefato fixado e `BILLING_CURRENT_JUNIT` para um path inexistente antes da execução corrente e distinto do baseline mesmo após resolver aliases:

```bash
( set -eu
: "${BILLING_BASE_JUNIT:?set BILLING_BASE_JUNIT to the immutable planning-base JUnit}"
: "${BILLING_CURRENT_JUNIT:?set BILLING_CURRENT_JUNIT to a fresh output path}"
BILLING_BASE_JUNIT_REAL=$(realpath -e -- "$BILLING_BASE_JUNIT")
BILLING_CURRENT_JUNIT_REAL=$(realpath -m -- "$BILLING_CURRENT_JUNIT")
test "$BILLING_BASE_JUNIT_REAL" != "$BILLING_CURRENT_JUNIT_REAL"
test ! -e "$BILLING_CURRENT_JUNIT"
test ! -L "$BILLING_CURRENT_JUNIT"
test "$(sha256sum poetry.lock | cut -d' ' -f1)" = "ee7555da0c75405a4f8c3579f9314f4629c1200287c88c4ddd995feaf6e4c6cf"
test "$(sha256sum "$BILLING_BASE_JUNIT_REAL" | cut -d' ' -f1)" = "ac853995c1ddb3a03903c1b1d79e47bac1f8d4166a96ea1098c1bbc42a27949a"
if poetry run pytest --junitxml="$BILLING_CURRENT_JUNIT_REAL"; then
    BILLING_CURRENT_PYTEST_STATUS=0
else
    BILLING_CURRENT_PYTEST_STATUS=$?
fi
test "$BILLING_CURRENT_PYTEST_STATUS" -le 1
test "$(sha256sum "$BILLING_BASE_JUNIT_REAL" | cut -d' ' -f1)" = "ac853995c1ddb3a03903c1b1d79e47bac1f8d4166a96ea1098c1bbc42a27949a"
test -s "$BILLING_CURRENT_JUNIT_REAL"
python3 - "$BILLING_BASE_JUNIT_REAL" "$BILLING_CURRENT_JUNIT_REAL" <<'PY'
import sys
import xml.etree.ElementTree as ET


def testcase_ids(path: str) -> tuple[set[str], set[str]]:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    ids = [
        f"{case.attrib['classname']}::{case.attrib['name']}"
        for case in cases
    ]
    if not ids:
        raise SystemExit(f"empty testcase universe: {path}")
    if len(ids) != len(set(ids)):
        duplicates = sorted(testcase_id for testcase_id in set(ids) if ids.count(testcase_id) > 1)
        raise SystemExit(f"duplicate testcase IDs in {path}: {duplicates}")
    failed = {
        testcase_id
        for testcase_id, case in zip(ids, cases)
        if case.find("failure") is not None or case.find("error") is not None
    }
    return set(ids), failed


baseline_cases, baseline_failures = testcase_ids(sys.argv[1])
current_cases, current_failures = testcase_ids(sys.argv[2])
missing_cases = sorted(baseline_cases - current_cases)
new_failures = sorted(current_failures - baseline_failures)
print(
    f"baseline_testcases={len(baseline_cases)} "
    f"current_testcases={len(current_cases)} missing_testcases={len(missing_cases)}"
)
print(
    f"baseline_failures={len(baseline_failures)} "
    f"current_failures={len(current_failures)} new_failures={len(new_failures)}"
)
for testcase_id in missing_cases:
    print(f"missing_testcase={testcase_id}")
for failure in new_failures:
    print(f"new_failure={failure}")
raise SystemExit(bool(missing_cases or new_failures))
PY
)
```

O comparador é deliberadamente limitado à identidade do testcase, conforme a decisão humana; testes focados, smoke PostgreSQL e revisões de comportamento continuam responsáveis por detectar regressões dentro de casos que já falhavam no baseline.

## Dependencies

**depends-on:** none
**blocks:** interaction-origin-training-safety

O card externo `t_msontnyf011833ab` representa o handoff desta entrega. Ele não substitui os tasks de execução abaixo e só pode ser marcado done após o commit identificado conter a `016` rastreada como head único e publicar os hashes exigidos.

## Success Criteria

- [ ] A revisão `016` está rastreada, declara `down_revision = "015"`, é o único head e não modifica migrations anteriores.
- [ ] Upgrade cria `size_bytes BIGINT NOT NULL DEFAULT 0`, backfill por bytes UTF-8 e `CHECK >= 0`; downgrade remove somente constraint e coluna deste wish.
- [ ] Documento legado multibyte recebe tamanho exato; valor negativo falha; bootstrap e ORM representam o mesmo domínio/default/constraint.
- [ ] Criação e alteração de conteúdo pelos writers ORM atuais recalculam `size_bytes`; nenhuma rota precisa ler todos os conteúdos para somar uso.
- [ ] Traces usam janela mensal UTC half-open e contam somente `message.in` das clínicas do builder autenticado.
- [ ] RAG soma `size_bytes` de todas as clínicas pertencentes ao builder sem revelar nem contar outro builder; MB é decimal.
- [ ] Limite zero, ausência de uso, excedente e virada dezembro/janeiro têm resultados determinísticos e cobertos.
- [ ] Mídia retorna uso zero; créditos extras retornam zero; compra permanece desabilitada e `coming_soon=true`.
- [ ] Limites Start são configuráveis, documentados nos dois exemplos de ambiente e rejeitam valores negativos.
- [ ] `GET /v1/billing/usage` e `/summary` exigem builder válido, retornam `404` para builder inexistente e usam exatamente o mesmo cálculo.
- [ ] A UI PT/EN exibe plano, período, traces, RAG, mídia, disponibilidade e excedente; estados de erro parcial não derrubam dados já disponíveis.
- [ ] A barra de progresso fica em 0–100 para ARIA, enquanto o texto preserva percentuais reais acima de 100; datas/timezones inválidos têm fallback estável.
- [ ] O botão de créditos permanece desabilitado mesmo que o payload traga `purchase_enabled=true`, até existir wish de compra.
- [ ] Billing e builder possuem testes de integração de página para fetch, autenticação, sucesso e falha parcial; mudanças de dashboard permanecem ausentes do diff.
- [ ] Testes backend focados e smoke PostgreSQL real passam; a suíte backend completa é executada e introduz zero testcases falhos contra o JUnit imutável do planning base; frontend typecheck/test/build passam.
- [ ] O commit final contém somente a allowlist aprovada, usa conventional commit e publica `BILLING_USAGE_METERING_COMMIT_SHA`, `BILLING_016_BLOB_SHA` e `INTERACTION_ORIGIN_BASE_SHA` com o mesmo commit.
- [ ] `git cat-file` prova a `016` no commit, o blob publicado coincide com o blob committed e `poetry run alembic heads` retorna exatamente `016 (head)`.

## Execution Strategy

Antes do primeiro dispatch, o orquestrador cria uma lane/worktree dedicada em `7ab570ff0d10ed459e9e62bec5c8f32f88e94fff`. Os diffs locais são evidência de descoberta, não autorização para copiar arquivos mistos; cada grupo reconcilia somente sua allowlist. Os grupos são sequenciais porque schema, contrato backend e consumidores formam uma cadeia, e isso também evita conflitos com o worktree compartilhado.

| Wave | Group | Agent | Complexity | Model | Description | Depends on |
|------|-------|-------|------------|-------|-------------|------------|
| 1 | 1 | engineer | 2 — schema/persistência stateful (+2) | engineer-standard / high | Incorporar `016`, alinhar bootstrap/ORM/writers e provar PostgreSQL real. | none |
| 2 | 2 | engineer | 2 — agregação/API stateful (+2) | engineer-standard / high | Medir uso builder-scoped e publicar o contrato backend. | Group 1 |
| 3 | 3 | engineer | 3 — UI subjetiva (+2), multi-package integration (+1) | engineer-standard / high | Integrar o componente de uso nas páginas billing e builder. | Group 2 |
| 4 | 4 | engineer | 1 — documentação/release gate (+1) | engineer-trivial / low | Documentar e executar o gate agregado pré-commit com auditoria determinística de escopo. | Group 3 |

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

### Group 1: Schema e tamanho persistido

**Goal:** incorporar a `016` com provenance determinística e manter o tamanho UTF-8 coerente em bancos legados, bancos novos e writers atuais.

**Deliverables:**
1. Incorporar a migration `016` sobre `015`, sem editar revisão anterior, com coluna, backfill, constraint e downgrade simétrico.
2. Alinhar `scripts/init-db.sql`, `KnowledgeDocument` e a rota de escrita, mantendo um único helper/path ORM que calcula `len(content.encode("utf-8"))` em create e update.
3. Adicionar testes unitários de persistência e um smoke PostgreSQL que cria dois databases filhos isolados: um atualizado por Alembic a partir da `015` e outro inicializado por `scripts/init-db.sql` com semântica SQL fail-fast.
4. No smoke, provar legado multibyte, `CHECK`, downgrade/upgrade e head único no database migrado; introspectar `knowledge_documents.size_bytes` nos dois databases e comparar tipo, nulabilidade, default e constraint não negativa entre bootstrap, Alembic e contrato ORM.
5. Proteger ambos os databases do smoke destrutivo: URL explícita, host loopback, database administrativa allowlisted, nomes filhos com prefixo interno, quoting seguro e remoção somente dos databases exatos criados em `finally` após dispose.

**Acceptance Criteria:**
- [ ] `016`/bootstrap/ORM têm o mesmo tipo, nulabilidade, default e constraint; migrations antigas permanecem byte-for-byte intactas.
- [ ] Backfill e create/update ORM produzem bytes UTF-8 exatos, inclusive conteúdo vazio e multibyte.
- [ ] Update não relacionado preserva o tamanho; valor negativo é rejeitado pelo PostgreSQL.
- [ ] O smoke recusa configuração destrutiva insegura, sempre limpa somente os dois filhos criados e prova downgrade/upgrade real no database migrado.
- [ ] O segundo database executa `scripts/init-db.sql` com falha imediata em erro SQL; introspecção comprova paridade de tipo, nulabilidade, default e constraint não negativa com o database migrado e o contrato ORM.
- [ ] `poetry run alembic heads` retorna somente `016 (head)`.

**Validation:**
```bash
poetry run pytest tests/models/test_knowledge_document_size.py tests/integration/test_billing_usage_postgres.py -v
POSTGRES_SMOKE_DATABASE_URL="${POSTGRES_SMOKE_DATABASE_URL:?set an isolated loopback PostgreSQL admin URL}" poetry run pytest -m integration tests/integration/test_billing_usage_postgres.py -v
test "$(poetry run alembic heads)" = "016 (head)"
poetry run mypy src/models/knowledge.py src/api/routes/knowledge.py --ignore-missing-imports --follow-imports=silent --disable-error-code=attr-defined --disable-error-code=index
poetry run isort --check-only alembic/versions/20260810_000001_016_add_knowledge_document_size.py src/models/knowledge.py src/api/routes/knowledge.py tests/models/test_knowledge_document_size.py tests/integration/test_billing_usage_postgres.py
poetry run flake8 alembic/versions/20260810_000001_016_add_knowledge_document_size.py src/models/knowledge.py src/api/routes/knowledge.py tests/models/test_knowledge_document_size.py tests/integration/test_billing_usage_postgres.py
poetry run black --check alembic/versions/20260810_000001_016_add_knowledge_document_size.py src/models/knowledge.py src/api/routes/knowledge.py tests/models/test_knowledge_document_size.py tests/integration/test_billing_usage_postgres.py
# Execute o bloco exato de Shared Backend Aggregate Regression Gate.
git diff --check
```

A suíte backend agregada baseline-aware é obrigatória neste grupo porque a mudança alcança schema, bootstrap e um modelo compartilhado; o smoke separado cobre semântica PostgreSQL que a suíte SQLite não consegue provar e compara dois databases guardados, um migrado por Alembic e outro criado pelo bootstrap fail-fast. O mypy silencia somente imports transitivos e os códigos `attr-defined`/`index` já presentes no blob-base de `src/models/knowledge.py`; todos os demais códigos continuam validados nos dois arquivos-alvo. O gate compartilhado preserva a cobertura comportamental agregada sem atribuir ao wish as 198 falhas do planning base.

**depends-on:** none

---

### Group 2: Agregação e API de uso

**Goal:** fornecer uma única fonte builder-scoped para uso corrente e expô-la nos dois contratos de billing.

**Deliverables:**
1. Implementar o agregador com período mensal UTC, `message.in`, soma de `size_bytes`, limites Start, disponibilidade/excedente e estados honestos de mídia/créditos.
2. Adicionar os schemas, `GET /v1/billing/usage` e o campo `usage` em `/summary`, reutilizando o mesmo helper e o boundary `require_builder`.
3. Adicionar e documentar as três configurações não negativas do plano Start em `.env.example` e `env.example`.
4. Cobrir isolamento entre builders, todos os tenants pertencentes ao builder, builder ausente, virada de ano, limite zero, overage, UTF-8, auth e equivalência `/usage`/`summary`.
5. Cobrir o OpenAPI gerado, afirmando que ele contém `/v1/billing/usage` e que `BillingSummaryResponse` expõe a propriedade `usage` com o schema esperado.
6. Eliminar cirurgicamente os sete diagnósticos mypy locais herdados nos arquivos já allowlisted: anotar `_calc_pricing` como `dict[str, int]`; `billing_summary`, `validate_coupon`, `create_checkout` e `abacatepay_webhook` como `BillingSummaryResponse`, `CouponValidationResponse`, `CheckoutResponse` e `dict[str, bool]`, respectivamente; tipar `Settings.validate_secret_key(..., info)` com `pydantic.ValidationInfo`; e trocar o keyword estruturado duplicado `event=event_type` do log por `webhook_event=event_type`. Não adicionar `disable-error-code`, `type: ignore` ou supressão ampla nesses arquivos.

**Acceptance Criteria:**
- [ ] Nenhuma query aceita tenant/client selector; ambas partem somente do builder autenticado e não contam dados de outro builder.
- [ ] Trace/RAG seguem as unidades e janelas das Decisions; eventos derivados e documentos de outro builder ficam fora.
- [ ] Builder inexistente retorna `404`; caller sem role builder retorna `401/403` sem executar agregação.
- [ ] `/usage` e `summary.usage` têm schema e valores idênticos para o mesmo snapshot lógico.
- [ ] O OpenAPI gerado publica `/v1/billing/usage` e `BillingSummaryResponse.usage`; os testes falham se rota ou propriedade desaparecerem.
- [ ] Limites e overage são determinísticos para zero, abaixo e acima do entitlement; nenhum endpoint aplica bloqueio ou cobra créditos.
- [ ] O mypy focado segue imports em modo `silent`, cobre os quatro arquivos Python do Grupo 2 e passa sem os sete diagnósticos locais de `type-arg`, `no-untyped-def` e keyword `event` duplicado, sem `disable-error-code`, `type: ignore` ou ignores amplos em `src/api/routes/billing.py` e `src/config/settings.py`.

**Validation:**
```bash
poetry run pytest tests/services/test_billing_usage_service.py tests/api/test_billing_usage.py -v
poetry run mypy src/services/billing_usage_service.py src/api/routes/billing.py src/api/schemas/billing.py src/config/settings.py --ignore-missing-imports --follow-imports=silent
poetry run isort --check-only src/services/billing_usage_service.py src/api/routes/billing.py src/api/schemas/billing.py src/config/settings.py tests/services/test_billing_usage_service.py tests/api/test_billing_usage.py
poetry run flake8 src/services/billing_usage_service.py src/api/routes/billing.py src/api/schemas/billing.py src/config/settings.py tests/services/test_billing_usage_service.py tests/api/test_billing_usage.py
poetry run black --check src/services/billing_usage_service.py src/api/routes/billing.py src/api/schemas/billing.py src/config/settings.py tests/services/test_billing_usage_service.py tests/api/test_billing_usage.py
# Execute o bloco exato de Shared Backend Aggregate Regression Gate.
git diff --check
```

Os testes focados provam agregação, auth e o contrato OpenAPI gerado; a suíte backend agregada é exigida porque `/billing/summary`, settings e schemas compartilhados mudam contratos existentes.

**depends-on:** Group 1

---

### Group 3: Superfícies de uso no frontend

**Goal:** apresentar o mesmo contrato de uso em billing e builder sem transformar ausência parcial em indisponibilidade total.

**Deliverables:**
1. Criar `PlanUsageSummary` localizado PT/EN com período, plano, traces, RAG, mídia, disponibilidade, overage e semântica acessível de progresso.
2. Consumir `summary.usage` na página billing e `/v1/billing/usage` na visão geral do builder, preservando headers/auth e os demais fetches existentes.
3. Manter compra de créditos desabilitada e claramente marcada como futura.
4. Adicionar testes do componente e testes de integração das duas páginas para sucesso, auth/fetch e falha parcial.

**Acceptance Criteria:**
- [ ] O componente formata PT/EN, MB decimal, mês half-open como período humano inclusivo e plano nulo sem lançar erro.
- [ ] Percentual textual preserva overage real; progressbar limita valor a 0–100 e possui nome/valores ARIA coerentes.
- [ ] Timezone inválido cai para UTC; números não finitos não chegam ao progressbar.
- [ ] Billing usa o objeto já embutido em `/summary`; builder faz o fetch dedicado e uma falha apenas de usage não apaga as demais seções carregadas.
- [ ] O botão continua desabilitado/`coming soon`; não existe ação de compra ou mutação financeira.
- [ ] Nenhum arquivo de dashboard/analytics entra no diff deste grupo.

**Validation:**
```bash
pnpm --dir frontend --filter @workspace/neurostack run test -- src/components/plan-usage-summary.test.tsx src/pages/billing-usage.test.tsx src/pages/builder-usage.test.tsx
pnpm --dir frontend --filter @workspace/neurostack run typecheck
pnpm --dir frontend --filter @workspace/neurostack run test
PORT=3001 BASE_PATH=/ pnpm --dir frontend --filter @workspace/neurostack run build
git diff --check
```

Vitest é obrigatório porque o tsconfig exclui testes; typecheck e build cobrem o contrato público e a integração Vite das duas páginas.

**depends-on:** Group 2

---

### Group 4: Contrato e gate pré-commit

**Goal:** documentar a semântica entregue e provar, antes do commit, o conteúdo, o escopo e os gates agregados da lane isolada.

**Deliverables:**
1. Atualizar os contratos de API, schema e integração builder com unidades, janela, escopo, estados indisponíveis e ausência de enforcement.
2. Adicionar um teste determinístico de contrato documental que valide cada arquivo individualmente: `docs/api-contracts.md` exige ambos os endpoints, `BillingSummaryResponse.usage`, auth builder, `message.in`, janela UTC half-open, MB decimal, mídia zero, créditos `coming_soon` e ausência de enforcement; `docs/database-schema.md` exige `size_bytes`, `BIGINT`, `NOT NULL`, default zero, `CHECK >= 0`, bytes UTF-8 e paridade bootstrap/Alembic; `docs/builder-integration-guide.md` exige os dois endpoints, escopo de todas as clínicas do builder, unidades, período, mídia zero, créditos desabilitados/`coming_soon` e ausência de enforcement. Nenhuma alternação permissiva entre arquivos é aceita.
3. Executar o gate backend completo, smoke PostgreSQL e gates package-scoped do frontend antes do commit.
4. Adicionar e executar uma auditoria determinística que materialize a lista ordenada da união de paths alterados desde o planning base, staged, unstaged e untracked; comparar essa lista com a allowlist exata deste wish e rejeitar dashboard, analytics, multimodal, credentials, interaction-origin ou qualquer hunk misto.

**Acceptance Criteria:**
- [ ] Docs, OpenAPI, backend e frontend concordam em campos, unidades, período, escopo e estados futuros.
- [ ] O teste de contrato verifica separadamente `docs/api-contracts.md`, `docs/database-schema.md` e `docs/builder-integration-guide.md`, exigindo em cada um todos os termos e relações enumerados no deliverable 2 em vez de aceitar um match em qualquer arquivo.
- [ ] A suíte backend agregada é executada e introduz zero testcases falhos contra o JUnit imutável do planning base; os gates estáticos focados sobre a allowlist Python final, os gates frontend e o smoke real passam a partir da lane isolada.
- [ ] A evidência pré-commit contém a lista ordenada da união de paths alterados e a auditoria confirma igualdade exata com `Files to Create/Modify`, sem mudanças preexistentes ou alheias.
- [ ] Group 4 termina sem criar commit, publicar hashes ou alterar `t_msontnyf011833ab`; essas ações pertencem exclusivamente ao Orchestrator Finalization Gate.

**Validation:**
```bash
poetry run pytest tests/docs/test_billing_usage_docs.py -v
poetry run python scripts/check_billing_usage_scope.py --base 7ab570ff0d10ed459e9e62bec5c8f32f88e94fff
poetry run mypy src/models/knowledge.py src/api/routes/knowledge.py --ignore-missing-imports --follow-imports=silent --disable-error-code=attr-defined --disable-error-code=index
poetry run mypy alembic/versions/20260810_000001_016_add_knowledge_document_size.py tests/models/test_knowledge_document_size.py tests/integration/test_billing_usage_postgres.py src/config/settings.py src/services/billing_usage_service.py src/api/routes/billing.py src/api/schemas/billing.py tests/services/test_billing_usage_service.py tests/api/test_billing_usage.py scripts/check_billing_usage_scope.py tests/docs/test_billing_usage_docs.py --ignore-missing-imports --follow-imports=silent
poetry run isort --check-only alembic/versions/20260810_000001_016_add_knowledge_document_size.py src/models/knowledge.py src/api/routes/knowledge.py tests/models/test_knowledge_document_size.py tests/integration/test_billing_usage_postgres.py src/config/settings.py src/services/billing_usage_service.py src/api/routes/billing.py src/api/schemas/billing.py tests/services/test_billing_usage_service.py tests/api/test_billing_usage.py scripts/check_billing_usage_scope.py tests/docs/test_billing_usage_docs.py
poetry run flake8 alembic/versions/20260810_000001_016_add_knowledge_document_size.py src/models/knowledge.py src/api/routes/knowledge.py tests/models/test_knowledge_document_size.py tests/integration/test_billing_usage_postgres.py src/config/settings.py src/services/billing_usage_service.py src/api/routes/billing.py src/api/schemas/billing.py tests/services/test_billing_usage_service.py tests/api/test_billing_usage.py scripts/check_billing_usage_scope.py tests/docs/test_billing_usage_docs.py
poetry run black --check alembic/versions/20260810_000001_016_add_knowledge_document_size.py src/models/knowledge.py src/api/routes/knowledge.py tests/models/test_knowledge_document_size.py tests/integration/test_billing_usage_postgres.py src/config/settings.py src/services/billing_usage_service.py src/api/routes/billing.py src/api/schemas/billing.py tests/services/test_billing_usage_service.py tests/api/test_billing_usage.py scripts/check_billing_usage_scope.py tests/docs/test_billing_usage_docs.py
# Execute o bloco exato de Shared Backend Aggregate Regression Gate.
POSTGRES_SMOKE_DATABASE_URL="${POSTGRES_SMOKE_DATABASE_URL:?set an isolated loopback PostgreSQL admin URL}" poetry run pytest -m integration tests/integration/test_billing_usage_postgres.py -v
pnpm --dir frontend --filter @workspace/neurostack run typecheck
pnpm --dir frontend --filter @workspace/neurostack run test
PORT=3001 BASE_PATH=/ pnpm --dir frontend --filter @workspace/neurostack run build
test "$(poetry run alembic heads)" = "016 (head)"
git diff --check
```

Este é o gate agregado pré-commit: o Shared Backend Aggregate Regression Gate executa o backend completo e rejeita qualquer testcase falho novo contra o planning base, e mypy/isort/flake8/black cobrem explicitamente a união de todos os arquivos Python da allowlist dos Grupos 1, 2 e 4, sem incluir arquivos TS/TSX. O engineer entrega docs, teste de conteúdo, manifesto determinístico de escopo e validação, mas não cria commit nem publica provenance. A revisão local independente e a validação precisam estar limpas antes da finalização do orquestrador.

**depends-on:** Group 3

---

## Orchestrator Finalization Gate

**depends-on:** Group 4 concluído pelo engineer, revisão local independente limpa e todos os comandos de validação do Group 4 aprovados.

Este gate não é trabalho do engineer do Group 4. Somente o orquestrador, na lane isolada e depois da revisão/validação pré-commit, pode:

1. Reexecutar a auditoria determinística de escopo e criar um único commit convencional contendo exatamente a allowlist aprovada.
2. Fixar `BILLING_USAGE_METERING_COMMIT_SHA` no commit criado e provar a lista ordenada de paths committed, a presença da `016`, seu blob SHA e o head Alembic único a partir desse estado.
3. Despachar a execução review independente, explicitamente pinned ao commit exato; nenhuma revisão de working tree substitui essa prova.
4. Somente após verdict `SHIP`, publicar `BILLING_USAGE_METERING_COMMIT_SHA`, `BILLING_016_BLOB_SHA` e `INTERACTION_ORIGIN_BASE_SHA` com o mesmo commit e marcar `t_msontnyf011833ab` done. Em qualquer outro verdict, manter o card e `interaction-origin-training-safety` bloqueados.

**Committed proof commands:**
```bash
BILLING_USAGE_METERING_COMMIT_SHA="${BILLING_USAGE_METERING_COMMIT_SHA:?record the identified conventional commit}"
git show --format= --name-only "$BILLING_USAGE_METERING_COMMIT_SHA" | sed '/^$/d' | sort -u
git cat-file -e "$BILLING_USAGE_METERING_COMMIT_SHA:alembic/versions/20260810_000001_016_add_knowledge_document_size.py"
COMMITTED_BILLING_016_BLOB_SHA="$(git rev-parse "$BILLING_USAGE_METERING_COMMIT_SHA:alembic/versions/20260810_000001_016_add_knowledge_document_size.py")"
git cat-file -e "$COMMITTED_BILLING_016_BLOB_SHA^{blob}"
test "$(poetry run alembic heads)" = "016 (head)"
git diff --check "$BILLING_USAGE_METERING_COMMIT_SHA^" "$BILLING_USAGE_METERING_COMMIT_SHA"
```

The execution reviewer receives the exact commit SHA, the ordered committed-path manifest, the `016` blob SHA and the command evidence above. The orchestrator publishes the hashes and completes the handoff card only after that pinned review returns `SHIP`.

---

## QA Criteria

_O agente de QA verifica cada item em ambiente de desenvolvimento após o merge._

- [ ] Upgrade sobre PostgreSQL na `015` preserva documentos legados e calcula bytes UTF-8; downgrade/upgrade é repetível.
- [ ] Dois builders com clínicas e documentos próprios nunca veem nem contam uso um do outro.
- [ ] Virada de mês UTC exclui exatamente o limite superior e a virada dezembro/janeiro funciona.
- [ ] `/usage` e `/summary` apresentam o mesmo snapshot lógico e não alteram checkout, cupons ou webhooks de pagamento.
- [ ] Billing e builder exibem traces/RAG/mídia corretamente em PT e EN; falha parcial preserva o restante da tela.
- [ ] Créditos permanecem “em breve” e nenhum excedente bloqueia interação, upload ou pipeline.
- [ ] O commit de merge mantém a `016` como head único e o handoff registrado desbloqueia a próxima migration sem ambiguidade.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|-----------|
| O worktree compartilhado contém mudanças staged/dirty alheias | High | Executar em lane/worktree dedicada no planning base, usar allowlist e auditar cada path/hunk antes do commit. |
| `init-db.sql` diverge hoje do modelo/migration | High | Group 1 exige paridade e teste de bootstrap antes do handoff. |
| Pytest padrão usa SQLite e não prova `octet_length`, CHECK ou Alembic | High | Smoke PostgreSQL real, explícito e protegido é obrigatório. |
| Contar todas as clínicas pertencentes ao builder pode divergir de regra comercial futura | Medium | Este wish é observacional; filtros por status/plano exigem requisito comercial explícito e testes próprios. |
| Somar eventos/documentos a cada request pode ficar lento | Medium | Medir latência/EXPLAIN; cache ou índice adicional somente após violação de orçamento observada. |
| Listener ORM não cobre writer SQL/bulk futuro | Medium | Inventariar writers atuais agora; ao surgir outro writer, adicionar fronteira DB/serviço antes de usá-lo. |
| `/summary` passa a executar duas agregações extras | Medium | Cobrir regressão e medir; separar endpoint ou cachear apenas se a latência real justificar. |
| Limites Start podem mudar antes do release | Low | Valores são configuração não negativa e exemplos documentam as chaves. |

---

## Review Results

### Plan Review — 2026-08-11T10:30:57-03:00 — FIX-FIRST

- **Reviewer:** `/root/billing_plan_review`
- **Reviewed SHA-256:** `9c463423c490485db90b85e9c9fed2a5af37f33a934285fb7fbed41bf367f257` (matched the supplied working copy)
- **Planning base:** `7ab570ff0d10ed459e9e62bec5c8f32f88e94fff` exists and resolved exactly.
- **Checks:** full WISH inspection, migration ancestry, repository/backend/frontend command availability and static auth/model/aggregation boundaries passed; `git diff --check` passed. Implementation tests were not run during this plan-only review.
- **[HIGH] Circular Group 4 finalization:** engineer acceptance required a commit produced only after its own review. Move commit/provenance/card publication to an orchestrator finalization gate after Group 4 review and pre-commit validation.
- **[HIGH] Bootstrap not validated:** the PostgreSQL plan upgraded from `015` but never executed/introspected `scripts/init-db.sql`. Add a second guarded database and compare bootstrap, migrated schema and ORM contract.
- **[MEDIUM] Weak documentation/OpenAPI gate:** a single alternation `rg` could pass on one token. Add generated OpenAPI assertions and deterministic per-document content-contract validation.
- **Verdict:** FIX-FIRST.

### Questioner Advisory — 2026-08-11

- Preserve the current scope choices as explicit assumptions: all builder-owned tenants, Start entitlements for the current product, decimal MB and ORM writer coverage.
- Revisit before execution if product requires admin/non-admin eligibility, multiple plan mappings or a DB-maintained size invariant; those alternatives materially change this plan.
- Keep whole-file copies from the dirty worktree forbidden; import only audited hunks into the isolated lane.

### Plan Re-review — 2026-08-11T10:39:14-03:00 — SHIP

- **Reviewer:** `/root/billing_plan_rereview1` (independent from fixer `/root/billing_plan_fix1`)
- **Reviewed SHA-256:** `122c288d6bffc9646fd9c416ef60fe0ef48f4583c98e6050dbb5931e8e45546f` (matched the supplied working copy)
- **Checks:** planning base resolved exactly; `git diff --check` passed; zero placeholders; 4 groups with acceptance, validation and dependencies; migration ancestry and repository/backend/frontend commands verified statically. Implementation tests were not run during this plan-only review.
- **Resolved HIGH — finalization cycle:** Group 4 now ends pre-commit; commit, committed proofs, pinned execution review, hash publication and card completion are ordered in the orchestrator-only finalization gate.
- **Resolved HIGH — bootstrap evidence:** Group 1 now compares a migrated PostgreSQL database with a second guarded database created by fail-fast `scripts/init-db.sql`, including ORM/schema introspection.
- **Resolved MEDIUM — docs/OpenAPI:** generated OpenAPI assertions and deterministic per-document content checks replace the permissive cross-file alternation.
- **Remaining gaps:** none.
- **Verdict:** SHIP.

### Task Persistence Note — 2026-08-11

- Four `genie task create` attempts were rejected before mutation because `.genie/genie.db` returned `disk I/O error`; no group row was created.
- Per the wish workflow, this WISH remains executable as the source of truth. Task rows should be created or reconciled after the state DB is repaired, before `$genie:work` dispatch.

### Execution Start — 2026-08-11

- **State recovery:** the healthy base DB was preserved; stale `-shm` and zero-byte `-wal` sidecars were quarantined after byte-identical backup in `/tmp/genie-recovery.PzWFMs`. Full export preserved 42 tasks/119 events; `quick_check=ok` and `foreign_key_check` returned zero rows.
- **Reconciliation:** `.genie/roadmap.json` was materialized and `genie task sync` returned `Board and snapshot are in sync (none)`.
- **Group tasks:** Group 1 `t_msopt6468edb7f75`; Group 2 `t_msopt6h2512dca39`; Group 3 `t_msopt6ss847c8a4a`; Group 4 `t_msopt74j2c3146b8`.
- **Execution lane:** `/tmp/neurostack-billing-usage.nLXHmX`, branch `wish/billing-usage-metering`, exact base `7ab570ff0d10ed459e9e62bec5c8f32f88e94fff`.
- **Isolation:** local billing diffs in the shared checkout are discovery evidence only; execution imports/reimplements audited allowlisted hunks in the clean lane.

### Group 1 Validation Diagnosis — 2026-08-11 — FIX-FIRST

- **Cause:** `ambiguous-spec` / baseline-invalid validation command; implementation remains paused and task `t_msopt6468edb7f75` remains `in_progress` pending plan re-review.
- **Mypy evidence:** the approved command reports 227 errors in 55 files both in the execution lane and against the exact planning-base blobs. The nine local findings in `src/models/knowledge.py` are also identical (`attr-defined`/`index`, with line shifts only); `src/api/routes/knowledge.py` has zero findings and Group 1 introduced zero mypy errors.
- **Correction:** isolate imported diagnostics with `--follow-imports=silent` and suppress only the two pre-existing local error codes. The corrected focused command passed with `Success: no issues found in 2 source files`; PostgreSQL behavior tests and the aggregate backend suite remain mandatory.
- **Flake8 evidence:** the three current `F401` findings (`datetime`, `DateTime`, `func`) are also present in the planning-base blob, but each symbol is mechanically proven unused. The approved flake8 command is unchanged; Group 1 must remove those imports and rerun it exactly.
- **Review requirement:** this validation-only plan delta requires an independent plan review before execution resumes.

### Plan Re-review — 2026-08-11T11:18:28-03:00 — FIX-FIRST

- **Reviewer:** `/root/billing_group1_plan_rereview` (independent from the engineer and baseline scout).
- **Reviewed SHA-256:** `5eccd10cafeefa29e1b7ce781e8ee0142d765bb185d6ea2340b62fb73f99c126` (matched the supplied working copy).
- **Checks:** full 406-line WISH, exact planning base/lane/branch, `git diff --check`, Makefile gate expansion, ORM writer boundary, focused mypy evidence and the Questioner lens.
- **Accepted delta:** the focused mypy command is proportional; it isolates imported diagnostics and only the two proven pre-existing local error codes. Flake8 remains strict and the three unused imports have a safe surgical correction.
- **[HIGH] Aggregate gate impossible:** the prior Makefile quality target exits `2` on the exact-base lane at global `black --check src tests`, reporting 152 files that would be reformatted before lint, mypy or tests run. Reformatting those out-of-scope files would be scope creep. Use `poetry run pytest` in Groups 1, 2 and 4, retain focused static checks over each Python allowlist, and retain the final exact-scope audit. A baseline-aware global static comparison is required only if repository policy mandates global mypy.
- **[MEDIUM] Direct-wish rationale:** explain why brainstorm was unnecessary: contract, boundaries and approach were already decided with no open architecture/product fork.
- **Verdict:** FIX-FIRST; task `t_msopt6468edb7f75` remains `in_progress`.

### Plan Re-review — Fix loop 1/2 — 2026-08-11T11:28:43-03:00 — FIX-FIRST

- **Reviewer:** `/root/billing_group1_plan_rereview` (independent from fixer `/root/billing_plan_gate_fix1`).
- **Reviewed SHA-256:** `cc2896c048748db251c9a328e72e7af9eb7dd2e63680d591114ba1fa3e8f04a9` (matched the supplied working copy).
- **Checks:** full 423-line WISH, exact base/lane, `git diff --check`, all 13 final Python paths, direct-wish rationale and Questioner lens.
- **Resolved:** zero impossible Makefile quality targets; backend aggregate suite appears in Groups 1/2/4; final Python/static, SQL, env, docs and frontend surfaces retain dedicated gates.
- **[HIGH] Group 2/final mypy still impossible:** the planned Group 2 command on its three existing targets reports 234 errors/57 files; `--follow-imports=silent` reduces this to seven direct pre-existing findings in `src/api/routes/billing.py` and `src/config/settings.py`. Add import isolation to Group 2 and the corresponding Group 4 command, and require surgical typing fixes for those seven findings rather than broad code suppression.
- **Verdict:** FIX-FIRST; final narrow plan fix loop 2/2 is required before execution resumes.

### Plan Re-review — Fix loop 2/2 — 2026-08-11T11:35:29-03:00 — SHIP

- **Reviewer:** `/root/billing_group1_plan_rereview` (independent from fixer `/root/billing_plan_gate_fix1`).
- **Reviewed SHA-256:** `3ce23e5bfdbbe15fb2321e6980e3225d43993e9c7c3e8b4c7f447e32088accc6` (matched the supplied working copy).
- **Checks:** full 434-line WISH, exact base/lane/branch, `git diff --check`, zero impossible Makefile targets, all focused/aggregate gates, exact 13-path final Python union and Questioner lens.
- **Seven local fixes:** return/parameter annotations and the structured logging keyword correction fit entirely inside the Group 2 allowlist, match current runtime values, and require no broad suppression or behavior fork.
- **Validation contract:** Group 1/final mypy retains its narrowly evidenced baseline exceptions; Group 2/final mypy isolates imports but requires all local diagnostics to pass. Backend full suite, PostgreSQL smoke, frontend typecheck/test/build and exact-scope audit remain mandatory in execution.
- **Remaining gaps:** none.
- **Verdict:** SHIP; plan transitioned to `APPROVED` before execution resumption.

### Execution Resumed — 2026-08-11

- **Approved state SHA-256:** `ac3e3e07cf6e4f12b36f42e431d8af3f313791b1b510252ab6cbd5ac4e35f2e2` with status `APPROVED` and the SHIP evidence above persisted.
- **Transition:** status returned to `IN_PROGRESS`; Group 1 task `t_msopt6468edb7f75` retained its existing claim and received a heartbeat before engineer continuation.

### Group 1 Execution Review — 2026-08-11T11:47:41-03:00 — FIX-FIRST

- **Reviewer:** `/root/billing_group1_acceptance_review` (read-only and independent from the engineer).
- **Reviewed WISH SHA-256:** `ed5f729afdf186606cba7745de8e65532f14f855af161f9d402713c94f5ccae3`; target was the uncommitted six-path lane at exact base `7ab570ff0d10ed459e9e62bec5c8f32f88e94fff`.
- **Passed evidence:** exact six-path scope; `016` is the sole static head over `015`; revisions `001`–`015` unchanged; column/backfill/constraint/downgrade, bootstrap/ORM parity, current ORM writers, UTF-8 cases and negative rejection matched the criteria. Focused suite reported 12 passed/1 skipped and real PostgreSQL smoke reported 1 passed.
- **[HIGH] Destructive collision cleanup:** the smoke always passed both candidate names to cleanup even when `CREATE DATABASE` had not acknowledged their creation. A collision could therefore terminate sessions and drop a pre-existing internally named database. Track only successfully created names; on first-name collision cleanup receives none, and after one success/second collision it receives only the first. Add deterministic regression tests for both cases.
- **Aggregate gate:** the 1,143-test run was interrupted at approximately 9–10% with no observed failures because its result would be stale after the HIGH fix; it remains mandatory after correction.
- **Other findings:** none.
- **Verdict:** FIX-FIRST; Group 1 task remains `in_progress` for a narrow fix/re-review loop.

### Group 1 Quality/Execution Review — 2026-08-11T12:01:51-03:00 — FIX-FIRST

- **Reviewer:** `/root/billing_group1_plan_rereview` acting as independent quality reviewer after fix loop 1/2.
- **Reviewed WISH SHA-256:** `874cb2e24c2b1120fff0b4b6962ab23aea90f9629b3802728fb44c952bbb61a7`; exact six-path lane/base and hashes were confirmed, with `git diff --check` clean.
- **Resolved prior HIGH:** cleanup authorization now contains only names whose `CREATE DATABASE` returned successfully; both deterministic collision cases pass.
- **[HIGH] Backfill encoding:** `octet_length(content)` follows PostgreSQL `server_encoding`, while the ORM contract is explicitly UTF-8. Use `octet_length(convert_to(content, 'UTF8'))` and add a regression that fixes this SQL contract independently of the environment default.
- **[MEDIUM] Bootstrap credential argv:** the sync URL with visible password is passed to `psql --dbname`, exposing the admin password through process argv. Pass host/port/user/database separately, use a protected environment channel for the password, and prove command/error output does not contain a secret sentinel.
- **Aggregate gate:** a fresh complete backend suite remains mandatory after these changes; the current run was stopped at approximately 5% because its snapshot became stale.
- **Other findings:** none; schema shape, cleanup fix, writers, guards, focused evidence and scope were otherwise sound.
- **Verdict:** FIX-FIRST; proceed to execution fix loop 2/2 without completing the task.

### Group 1 Execution Re-review — Fix loop 1/2 — 2026-08-11T11:57:26-03:00 — BLOCKED

- **Reviewer:** `/root/billing_group1_acceptance_review`.
- **Reviewed WISH SHA-256:** `874cb2e24c2b1120fff0b4b6962ab23aea90f9629b3802728fb44c952bbb61a7`; migration/smoke/model-test hashes and exact six-path scope matched.
- **Resolved:** destructive collision cleanup; both collision boundaries authorize cleanup only for successfully created names. All Group 1 acceptance criteria were otherwise mapped to passing evidence.
- **Blocker:** mandatory aggregate `poetry run pytest` had not completed; no conditional SHIP is allowed.
- **Diagnosis:** `missing-context`; obtain exit `0` on the stable snapshot, then perform evidence-only revalidation.

### Group 1 Execution Re-review — Fix loop 2/2 — 2026-08-11T12:14:27-03:00 — BLOCKED

- **Reviewer:** `/root/billing_group1_plan_rereview` acting as independent quality reviewer.
- **Reviewed WISH SHA-256:** `657b521cc0437c5be822b6e4fc351cd5b45c3b108a116f41cca0cdee68d89d63`; migration `a4579188abe2bb72c1a11265b2472290d74247c5e72835c8baf5bbffdb1a10e5`; smoke `84b9508afc484875ff245b36819eaaeea11a0e0ed30057e53c3cfd340cdcd853`.
- **Resolved:** explicit UTF-8 migration backfill, password-free `psql` argv, environment-only password transport, failure redaction and prior collision cleanup. No code or security gap remains.
- **Direct evidence:** focused 16 passed/1 skipped; PostgreSQL real 1 passed/9 deselected; both exact mypy gates, isort, flake8, Black, Alembic head and diff-check passed.
- **Blocker:** only the mandatory full backend suite remains. Hash-stable exit `0` permits an evidence-only SHIP revalidation; no third code fix is indicated or authorized.
- **Verdict:** BLOCKED (`missing-context`), task remains `in_progress`.

### Group 1 Aggregate-Gate Escalation Diagnosis — 2026-08-11

- **Fix budget:** execution fix loops are exhausted (`2/2`); Group 1 task `t_msopt6468edb7f75` remains `in_progress` and no third implementation fix is authorized.
- **Sandbox evidence:** even a minimal in-memory `aiosqlite` operation hangs under the restricted command sandbox but completes immediately outside it. The full-suite evidence below was therefore collected outside the sandbox; this portion is classified `env-tool-failure` and was repaired by using the functioning execution environment.
- **Planning-base full suite:** exact commit `7ab570ff0d10ed459e9e62bec5c8f32f88e94fff` collected 1,130 tests and finished with 198 failures / 932 passes. JUnit SHA-256: `ac853995c1ddb3a03903c1b1d79e47bac1f8d4166a96ea1098c1bbc42a27949a` (`/tmp/billing-base-junit.xml`).
- **Current Group 1 full suite:** the hash-stable six-path lane collected 1,147 tests and finished with 199 failures / 947 passes / 1 skip. JUnit SHA-256: `4304884ae23e3d40b2f8a25d21854945f87fa881601654d6ba7d8d3ce39b68b5` (`/tmp/billing-current-junit.xml`).
- **Deterministic delta:** exact testcase-set comparison found zero resolved baseline failures and exactly one new failure: `tests.models.test_knowledge_document_size::test_upload_route_uses_shared_size_calculation`.
- **New failure:** in full-suite order, the installed `pytest-asyncio==0.23.8` (allowed by the `^0.23.3` declaration) fails before the test body with `RuntimeError: There is no current event loop in thread 'MainThread'`; the same test passes in the focused Group 1 run. `tests/conftest.py` defines a session-scoped `event_loop`, while `LifespanTestClient` may restore the policy to no current loop, making this new async unit test order-sensitive.
- **Cause:** `ambiguous-spec`. The approved aggregate requirement demands exit `0`, which is impossible on the exact planning base, and the plan does not define whether a baseline-aware no-new-failures comparison may replace it. Converting this one test to a synchronous wrapper is a code/test change after fix loop `2/2`, not an evidence-only revalidation.
- **Corrective route:** obtain an explicit human decision to amend the aggregate gate to exact planning-base JUnit comparison with zero new failures and, if approved, authorize one narrowly scoped test-harness correction followed by a fresh full current run, deterministic failure-set comparison, and independent re-review. No model/effort escalation is eligible.
- **Budget:** attempts=`2/2`; effort escalations=`0/2`; appeal=`none`.

### Group 1 Escalation Review — 2026-08-11 — BLOCKED

- **Reviewer:** `/root/billing_group1_plan_rereview`, independent and read-only.
- **Classification confirmed:** primary cause `ambiguous-spec`; the sandbox-only `aiosqlite` hang was an `env-tool-failure` already bypassed with valid external runs. This is neither `missing-context`, `model-capacity` nor `overdesigned-plan`.
- **Control-flow finding:** the session-scoped custom loop is not necessarily reinstated after `LifespanTestClient` restores `asyncio.set_event_loop(None)`. The async wrapper therefore fails before the new test body; production route logic is not reached. The lifecycle defect is pre-existing, but the new test is a new affected testcase and therefore a real `+1` aggregate delta.
- **Gate finding:** the lane fails both possible unstated interpretations: literal exit `0` is impossible on the planning base, while baseline-aware comparison currently requires eliminating the one new failure. A focused pass alone is insufficient.
- **Human decision required:** either (recommended) amend the gate to exact JUnit comparison requiring zero new failures and authorize one exceptional, test-only third correction followed by full rerun and independent review; accept a documented waiver; or fix the global event-loop lifecycle in a separate wish and keep Group 1 blocked.
- **Verdict:** BLOCKED; no further mutation is authorized until the validation contract and exhausted-fix-budget exception are decided.

### Human Resolution — 2026-08-11T13:00:54-03:00

- **Decision:** the user explicitly answered `autorizo` to the recommended route.
- **Validation contract:** replace the impossible aggregate exit-`0` requirement with the Shared Backend Aggregate Regression Gate above, pinned to the exact planning-base JUnit and requiring zero new failing testcase IDs.
- **Exceptional remediation:** authorize one test-only correction, restricted to `tests/models/test_knowledge_document_size.py`, to remove `test_upload_route_uses_shared_size_calculation` from the order-sensitive `pytest-asyncio` wrapper while retaining that named testcase and its assertion that `repository.create` receives the expected `size_bytes`. A spy/sentinel must additionally prove that the route calls `KnowledgeDocument.calculate_size_bytes`. This is a recorded human exception after execution fix budget `2/2`; it does not reopen a general fix loop or authorize production-code changes.
- **Required sequence:** independent plan-amendment review → isolated test correction → execute the retained named testcase in its focused run and in full-suite order → focused and full aggregate validation → deterministic JUnit comparison with zero missing baseline testcase IDs and zero new failing IDs → independent execution re-review. Group 2 remains blocked until Group 1 receives `SHIP` and the orchestrator marks its task done.
- **Escalation settings:** no model or effort change; effort escalations remain `0/2`.

### Plan-amendment Review — 2026-08-11T14:12:12-03:00 — FIX-FIRST

- **Reviewer:** `/root/billing_group1_plan_rereview`, independent and read-only.
- **Reviewed SHA-256:** `09c4834069f28ef1d4ff73d6b40a3f6cc4fa3bda5605a0923da87471bf372ac8` (matched before and after review).
- **Static evidence:** `git diff --check` passed; planning-base JUnit matched `ac853995...` with 1,130 unique testcases / 198 failures; current evidence had 1,147 unique testcases, no missing baseline IDs and the expected single new failure; base/current `poetry.lock` matched `ee7555da...`.
- **[HIGH] Fail-open shell block:** standalone checksum/status guards can fail and be overwritten by a later comparator exit `0`. Wrap the exact block in a fail-closed subshell (`set -eu`) or terminate explicitly.
- **[HIGH] Incomplete artifact/environment binding:** require both JUnit variables, a fresh current path distinct from the baseline (including aliases), the current `poetry.lock` digest, and the baseline digest before and after pytest.
- **[HIGH] Incomplete test universe:** comparing only failed IDs lets deleted/uncollected baseline testcases disappear. Compare all testcase identities and reject `baseline_testcases - current_testcases`, while permitting extra current tests and resolved failures.
- **Questioner lens:** the amendment addresses the real baseline-invalid gate with no production scope expansion or waiver; the simplest complete correction is to harden this block, not add durable machinery.
- **Verdict:** FIX-FIRST; plan-amendment fix loop `1/2` required before the authorized test-only remediation.

### QA Advisory — 2026-08-11 — FIX-FIRST

- **Reviewer:** `/root/billing_gate_qa_lens`, read-only QA lens; reviewed SHA-256 `09c4834069f28ef1d4ff73d6b40a3f6cc4fa3bda5605a0923da87471bf372ac8`.
- **Confirmed HIGHs:** the shell block is fail-open and the failed-ID-only comparison cannot detect missing/deleted baseline testcases. Read-only probes confirmed the existing artifacts themselves are complete: 1,130 → 1,147 unique cases, zero missing baseline IDs, and exactly one new failure.
- **[MEDIUM] Same-ID drift:** changed failure messages remain invisible by explicit human choice. Keep focused/smoke/review evidence as the owner of touched behaviors; message equality would be noisy and is not a hard gate.
- **[MEDIUM] Remediation strength:** retain the named testcase and `repository.create(size_bytes=...)` assertion, add a spy/sentinel proving the route calls the shared `KnowledgeDocument.calculate_size_bytes` helper, and prove the test executes in both focused and full-suite order. Deletion or weakening must not masquerade as remediation.
- **Recommendation:** FIX-FIRST on the plan block; retain all existing focused, PostgreSQL, frontend and exact-scope gates.

### Plan Re-review — Fix loop 1/2 — 2026-08-11T14:21:16-03:00 — SHIP

- **Reviewer:** `/root/billing_group1_plan_rereview`, independent from plan fixer `/root/billing_gate_plan_fix`.
- **Reviewed SHA-256:** `82b8fd2ba6b860179df5fc44851308e7e86fc6e0608ce81ade8dca1871373b6b` (matched before and after review).
- **Checks:** full 610-line WISH; extracted shell block passed `bash -n`; `git diff --check` passed; immutable baseline remained 1,130 unique cases / 198 failures and the pre-remediation current artifact had 1,147 unique cases, zero missing baseline IDs and exactly the known one new failure. No implementation suite was run during this plan-only review.
- **Resolved:** fail-closed status propagation; fresh/non-aliased artifacts; lock and baseline digest binding; rejection of pytest status `>1`, empty/duplicate universes, missing baseline cases and new failing IDs; identical gate across Groups 1/2/4.
- **Exceptional remediation contract:** exact testcase name, repository `size_bytes` assertion, helper spy/sentinel, focused/full-order execution and production freeze are all explicit.
- **Questioner lens:** this is the simplest complete mechanism for detecting wish-introduced regressions without inheriting 198 baseline failures; remaining same-ID drift is explicitly owned by focused/smoke/review evidence.
- **Severity gaps:** none.
- **Verdict:** SHIP; the approved amendment may proceed to the explicitly authorized test-only remediation while wish status remains `IN_PROGRESS`.

### QA Advisory Re-review — 2026-08-11 — SHIP

- **Reviewer:** `/root/billing_gate_qa_lens`, read-only and independent from the plan fixer.
- **Reviewed SHA-256:** `82b8fd2ba6b860179df5fc44851308e7e86fc6e0608ce81ade8dca1871373b6b`.
- **Evidence:** `bash -n` passed; lock/baseline digests matched; exact parser probe rejected the known `+1` with zero missing cases; all focused, PostgreSQL, frontend and scope gates remain present.
- **Accepted residual risk:** same-ID failure-message drift is intentionally outside the hard comparator and remains covered by focused tests, smoke and behavioral review.
- **Recommendation:** SHIP the amendment; this does not pre-judge Group 1 execution review.

### Group 1 Authorized Remediation — Orchestrator Evidence — 2026-08-11

- **Scope:** only `tests/models/test_knowledge_document_size.py` changed after the prior clean production review, from SHA-256 `f3adb88d...` to `f27a509139190a3f98f6b0243ae59e658e27576a17204a77bad27c159a350157`; the other five Group 1 hashes remained unchanged.
- **Test contract:** the named upload-route test is synchronous, uses and closes a private event loop without installing it as policy-current, records the exact helper input, returns sentinel `424_242` and requires that sentinel in awaited `repository.create(size_bytes=...)`.
- **Focused validation:** 16 passed / 1 skipped; production mypy passed 2 files; artifact mypy passed 3 files; isort, flake8 and Black passed; Alembic returned exactly `016 (head)`; `git diff --check` passed.
- **PostgreSQL:** guarded real smoke passed 1 / 9 deselected against a disposable PostgreSQL 16 instance, which was stopped after validation.
- **Aggregate validation:** fixed JUnit SHA-256 `e46272f37246c7a13f59b9cd13487973cc7f4e38d9044f60b4aeadec70bcdf47` contained 1,147 unique cases / 198 failures / 1 skip. Against immutable base `ac853995...` (1,130 / 198), the fail-closed comparator returned zero missing baseline IDs, zero new failures and zero resolved failures; the named target occurred exactly once and passed.
- **Environment:** the full suite ran outside the restricted sandbox using the lock-matching environment because the sandbox reproducibly hangs on minimal `aiosqlite`; no dependency was installed during remediation.

### Group 1 Final Execution Acceptance Review — 2026-08-11T14:35:53-03:00 — SHIP

- **Reviewer:** `/root/billing_group1_final_acceptance`, independent and read-only.
- **Reviewed WISH SHA-256:** `714bd92d9c62700473d02dcc69225fc363aa618c45a8f7b261e075bea3be9d82`; lane/base and all six artifact hashes matched.
- **Scope/schema/control flow:** exactly six approved paths; revision `015` matched the base blob; `016` was the sole head over `015`; migration/bootstrap/ORM parity, explicit UTF-8 backfill, symmetric downgrade and current-writer recalculation all satisfied the criteria.
- **Smoke/security:** destructive guards, child-name authorization, collision cleanup, credential transport/redaction, legacy backfill, constraint, downgrade/re-upgrade and three-way schema parity remained proven.
- **Exception/gates:** synchronous helper-sentinel test passed in full order; focused, PostgreSQL, static and complete baseline-aware gates matched the exact snapshot.
- **Severity gaps:** none.
- **Verdict:** SHIP; Group 1 may be marked done.

### Group 1 Final Execution-quality Review — 2026-08-11T14:34:42-03:00 — SHIP

- **Reviewer:** `/root/billing_group1_plan_rereview`, independent from all Group 1 fixers.
- **Reviewed WISH SHA-256:** `714bd92d9c62700473d02dcc69225fc363aa618c45a8f7b261e075bea3be9d82`; exact six-path scope and production hashes matched the prior clean review.
- **Test isolation:** the private loop is never policy-current, is closed in `finally`, and drives no background tasks/generators/executor; spy plus distinctive sentinel proves helper argument, helper result and awaited persistence provenance.
- **Regression/security:** prior collision-cleanup, password-free argv/redaction and `convert_to(..., 'UTF8')` findings remained resolved; full JUnit comparison reported zero missing/new failures.
- **Severity gaps:** none.
- **Verdict:** SHIP.

### Group 1 Final QA Advisory — 2026-08-11 — SHIP

- **Reviewer:** `/root/billing_gate_qa_lens`, read-only and independent from the remediation fixer.
- **Pinned evidence:** WISH `714bd92d...`; test `f27a5091...`; base JUnit `ac853995...`; fixed JUnit `e46272f3...`; lock `ee7555da...`.
- **Finding:** the retained named testcase has no async decorator, installs no global loop, closes its private loop, and fails for duplicate/ignored helper logic or incorrect repository propagation. JUnit proves exactly one non-skipped passing occurrence in full-suite order.
- **Severity gaps:** none.
- **Recommendation:** SHIP.

### Group 2 Initial Orchestrator Validation — 2026-08-11

- **Scope:** exactly the eight Group 2 paths; all six Group 1 artifact hashes remained unchanged and `git diff --check` passed.
- **Focused:** 11 tests passed outside the restricted sandbox; the sandbox reproduced the known `aiosqlite` hang.
- **Static:** exact four-file mypy passed; six-file isort, flake8 and Black passed.
- **Aggregate:** the first orchestrator attempt was aborted by output transport before producing JUnit and is not evidence. Independent quality review then produced complete current JUnit `ca1408fe7b92e6afb2dc9f98e8f01a8c30138dd141cf6e8b8a06d4d397da40b9`: 1,158 unique cases / 203 failures / 1 skip; against Group 1 clean artifact `e46272f3...`, missing=`0` and new failures=`5`.

### Group 2 Execution Acceptance Review — 2026-08-11T14:56:22-03:00 — FIX-FIRST

- **Reviewer:** `/root/billing_group1_final_acceptance`, independent from Group 2 engineer.
- **Reviewed WISH SHA-256:** `9c5a222ca2f8a525cd85c264400d188da503bca5ea6141508072f85fe68b8b00`; all eight Group 2 hashes and predecessor hashes matched.
- **Production findings:** builder-only authority, SQL-side ownership subquery, trace/RAG math, UTC/December behavior, shared endpoint helper, auth/404 ordering, settings and seven typing fixes satisfy the criteria; no production gap found.
- **[HIGH] Empty usage untested:** add a valid builder with no usage and assert complete zero/full-availability behavior.
- **[HIGH] Boundary test can pass for wrong rows:** distinct start/end multiplicities or separate probes must independently prove start inclusion and end exclusion.
- **[MEDIUM] OpenAPI type fidelity:** assert `BillingSummaryResponse.properties.usage` exactly references `BillingUsageResponse` and preferably assert no query-scope parameters.
- **Verdict:** FIX-FIRST; task remains `in_progress`.

### Group 2 QA Advisory — 2026-08-11 — FIX-FIRST

- **Reviewer:** `/root/billing_gate_qa_lens`, independent and read-only.
- **Confirmed HIGH:** empty usage is unowned; public selector/schema assertions are weak; API tests discard settings limits, so swapped trace/RAG/media wiring could pass.
- **MEDIUM strengthening:** use asymmetric isolation/event fingerprints and an admin-plan document; cover builder JWT without `builder_id` and require failure before aggregation.
- **Positive evidence:** implementation behavior and existing focused cases for UTF-8, overage, auth/404 and summary equivalence are sound.
- **Recommendation:** FIX-FIRST with test-only changes.

### Group 2 Execution Quality Review — 2026-08-11 — FIX-FIRST

- **Reviewer:** `/root/billing_group1_plan_rereview`, independent and read-only.
- **Production review:** ownership/auth, two fixed aggregate queries, UTC half-open period, decimal MB, zero-limit math, shared API contract, payment/webhook compatibility and read-only behavior are correct; no production gap found.
- **[HIGH] Aggregate gate:** current JUnit `ca1408fe...` has five new failures versus Group 1 clean `e46272f3...`.
- **[HIGH] Four new service tests are order-sensitive:** each fails before its body in full-suite order with no current event loop despite focused passes. Make them synchronous/private-loop tests inside the Group 2 test file.
- **[HIGH] RLHF task leak:** one unrelated test newly failed during teardown. No credible billing control-flow link was found; a fresh post-fix aggregate run must clear it or reproduce and trace it rather than waive it.
- **[MEDIUM] Empty aggregate coverage:** add the missing zero-use case.
- **Static evidence correction:** after the review dispatch, the orchestrator supplied exact mypy/isort/flake8/Black results, all exit `0`; no static gap remains.
- **Verdict:** FIX-FIRST; run test-only fix loop `1/2`, then focused/static and fresh aggregate validation.

### Group 2 Test-only Remediation — Fix loop 1/2 — 2026-08-11

- **Fixer:** `/root/billing_group1_test_remediation`; production files remained hash-identical to the initial clean review. Only `tests/services/test_billing_usage_service.py` and `tests/api/test_billing_usage.py` changed, to SHA-256 `d09d6fa5bcc6f1f895e1faa4bc1112b6d6100135ba3cd14f09ab78032ac6954a` and `8819ca9cab35ab0f790cc30f22e538e12499f98a8655dfed7d3ffa9eb4d515b4`, respectively.
- **Order independence:** each database test now creates, drives and closes its own private event loop without installing a policy-current loop, so the assertions run in focused and full-suite order.
- **Acceptance strengthening:** zero-use behavior is complete; month-start and next-month boundaries use asymmetric multiplicities; builder ownership uses asymmetric active/inactive/admin/foreign/standalone fingerprints; environment-derived limits are wired to distinct response fields; builder JWT without `builder_id`, exact OpenAPI `$ref` and absence of scope query parameters are covered.

### Group 2 Post-remediation Orchestrator Validation — 2026-08-11

- **Focused:** all 12 Group 2 tests passed outside the restricted sandbox. Exact two-file test mypy, isort, flake8 and Black checks passed; `git diff --check` passed.
- **Production:** exact four-file mypy, isort, flake8 and Black checks passed. The six production/config hashes remain `5bd9cb8e...`, `f160cd1d...`, `93833a3f...`, `2d2e7500...`, `07783523...` and `644931fb...`, matching the already-reviewed implementation.
- **Aggregate:** the exact fail-closed Shared Backend Aggregate Regression Gate completed outside the restricted sandbox. Current JUnit `/tmp/billing-group2-gate-20260811-junit.xml` has SHA-256 `5cf31614a369e09e7a99b6a77037dbee31925b4ce0dc778c1b57e46ce6c70584`, 1,159 unique cases / 198 failures / 1 skip. Against immutable base `ac853995...` (1,130 / 198), it reports missing baseline cases=`0` and new failures=`0`.
- **RLHF diagnostic:** the unrelated teardown leak seen in the initial aggregate artifact did not recur in either fresh full-suite run; no waiver or out-of-scope code change was applied.

### Group 2 Execution Acceptance Re-review — Fix loop 1/2 — 2026-08-11T15:21:12-03:00 — SHIP

- **Reviewer:** `/root/billing_group1_final_acceptance`, independent and read-only; WISH SHA-256 `d5b0481bffd665c3caa176116aa81d9b5e8784bda1f229b7ccde1fffbbb640a6` and all eight Group 2 hashes matched.
- **Resolved criteria:** complete empty-use behavior, independently discriminating half-open boundaries, asymmetric builder-owned/inactive/admin/foreign/standalone data, private-loop order independence, distinct settings wiring, missing builder identity rejection, exact endpoint equivalence, absence of query selectors and exact nested OpenAPI `$ref` are all proven.
- **Evidence:** focused 12 passed; exact static gates and diff-check passed; JUnit `5cf31614...` independently parsed as 1,159 unique / 198 failures / 1 skip, with zero missing baseline IDs, zero new failures and all 12 Group 2 testcases passing exactly once.
- **Severity gaps:** none. Verdict `SHIP`; Group 2 may be marked done.

### Group 2 Execution-quality Re-review — Fix loop 1/2 — 2026-08-11 — SHIP

- **Reviewer:** `/root/billing_group2_quality_rereview`, independent and read-only; WISH and all eight hashes matched.
- **Control flow:** auth precedes builder lookup and aggregation; both queries remain builder-scoped through `Tenant.builder_id`. Private loops never alter global policy and close session, engine and loop in `finally`.
- **Quality:** no opaque abstraction, cache/state machinery, type suppression or material duplication was introduced. The transient RLHF teardown failure passed; the two current RLHF failures are baseline members.
- **Severity gaps:** none. The explicitly accepted same-ID gate limitation remains unchanged. Verdict `SHIP`.

### Group 2 Final QA Advisory — Fix loop 1/2 — 2026-08-11 — SHIP

- **Reviewer:** `/root/billing_gate_qa_lens`, independent and read-only; pinned WISH `d5b0481b...`, current JUnit `5cf31614...` and baseline JUnit `ac853995...`.
- **Coverage:** zero-use/coalesce, non-canceling ownership and boundary fingerprints, UTF-8 bytes, distinct limit wiring, auth ordering, missing builder identity, no scope parameters and exact schema composition are each owned by deterministic tests.
- **Aggregate:** all 12 Group 2 IDs occur once and pass; current 1,159 / 198 versus baseline 1,130 / 198 has zero missing IDs and zero new failures.
- **Severity gaps:** none. Recommendation `SHIP`.

### Group 3 Initial Implementation — 2026-08-11

- **Engineer:** `/root/billing_group3_engineer`; exactly the six Group 3 paths were changed and all Group 1/2 artifacts remained frozen.
- **Behavior:** `PlanUsageSummary` is localized PT/EN, converts the backend half-open period to a human-inclusive end, preserves textual overage above 100%, clamps accessible progress, normalizes non-finite data, falls back to UTC and keeps credits disabled. Billing consumes only `summary.usage`; builder loads `/v1/billing/usage` independently of profile/dashboard/clinics.
- **Initial environment blocker:** the clean lane omitted ignored-but-required local artifacts (`frontend/lib/api-client-react` and `src/lib/utils.ts`) and pnpm 11 attempted an automatic install. No ignored manifest was copied or added to scope; the task remained `in_progress` pending a reproducible validation route.

### Group 3 Execution Pre-review — 2026-08-11 — FIX-FIRST

- **Reviewer:** `/root/billing_group3_pre_review`, independent and read-only; WISH `9518b7cd...`, exact six-path scope and `git diff --check` matched.
- **Production:** no functional gap found across localization, period math, non-finite/timezone fallback, ARIA behavior, disabled credits, billing reuse or builder partial-failure control flow.
- **Test gaps:** billing did not yet prove partial degradation; builder's usage-failure fixture had no identifiable clinic; ARIA min/max were not locked. Verdict `FIX-FIRST`, restricted to the three Group 3 test files.

### Group 3 Test Remediation — Fix loop 1/2 — 2026-08-11

- **Fixer:** `/root/billing_group3_test_fix1`; production remained unchanged. Billing now covers missing and null usage while retaining subscription content; builder uses an identifiable clinic and proves profile/dashboard/clinic survival across usage failure; progress min/max are asserted.
- **Harness corrections from real execution:** component cleanup prevents DOM leakage; the builder test selects the actual localized `Dashboard` label; the fetch mock has a typed optional `RequestInit`, retaining the authorization-header proof without casts or suppression.
- **Final test hashes:** component `36d63af5404f6eb1641e1010b6f984a67c74ca7da3088bf72cbfa8eb164df176`; billing `32c251bf7836e3d1b39e1c040a7a45445945500c830588272c8848c30b818848`; builder `d2ac9f3d7cca8d34af4e48dae1ee0288bb72497526cd08f4a9f0612f7b1342cd`.

### Group 3 Post-remediation Orchestrator Validation — 2026-08-11

- **Artifact hashes:** component `af4707fb50958fee89b3a3fef0f20723c6bbc9573eea15c1e081fc7c49a6042f`; component test `36d63af5...`; billing `ae551eddfc9ec06a529381971505a61067724558c9b38e229667f8738fedae34`; billing test `32c251bf...`; builder `71d82578d5f5a6307ab37d7d7571491825b25a8b5464d2f0e59e3f7d80ca73f5`; builder test `d2ac9f3d...`.
- **Focused:** three files / eight tests passed with real jsdom assertions. Package typecheck passed with no diagnostics.
- **Aggregate frontend:** five files / 29 tests passed with the normal Vitest pool. A diagnostic single-thread run exposed five DOM-leak failures in one planning-base-identical legacy test; rerunning that file with the normal pool passed 7/7, proving the diagnostic option—not the wish—caused the failures.
- **Build:** production Vite build passed, transforming 2,795 modules and emitting the app. Existing source-map and chunk-size warnings remained non-fatal.
- **Environment/isolation:** validation used temporary ignored links to the existing lock-installed `node_modules`, ignored workspace package and ignored `src/lib`; pnpm auto-install was disabled. All links were removed afterward; build output and Vite cache were moved to `/tmp`, and `git status` plus `git diff --check` show only the six approved frontend paths with no dashboard/analytics change.

### Group 3 Execution Acceptance Re-review — Fix loop 1/2 — 2026-08-11T16:41:23-03:00 — SHIP

- **Reviewer:** `/root/billing_group3_pre_review`, independent and read-only; WISH `99bf7d502da4d6fc4008439eb0edc1a36033ef6a6f905ef198c6b88eee4bcab6` and all six hashes matched.
- **Resolved:** missing/null billing usage preserves billing content; builder preserves identifiable profile/dashboard/clinic; accessible progress min/max/now is locked. Auth, no duplicate billing fetch, localization, period, finite/timezone fallback, null plan and disabled credits all match acceptance.
- **Scope/gates:** no dashboard/analytics path; focused 8, full 29, typecheck, build and diff-check passed. Severity gaps none; verdict `SHIP`.

### Group 3 Execution-quality Review — Fix loop 1/2 — 2026-08-11 — SHIP

- **Reviewer:** `/root/billing_group3_quality_review`, independent and read-only; WISH/base and all six artifact hashes matched.
- **Control flow:** builder usage failure cannot clear the three core result sets; current auth routing unmounts the page on logout, so the uncancelled request has no reachable material stale-data exposure. Billing remains single-source and no financial action exists.
- **Quality/isolation:** finite/date/accessibility logic is bounded; tests restore fetch, local storage, mocks and DOM; no suppression, `any` escape hatch or unrelated diff. Temporary ignored links were removed.
- **Residual risk:** future in-place account/token switching should add cancellation or generation guards to both existing builder request paths. No current severity gap; verdict `SHIP`.

### Group 3 Final QA Advisory — Fix loop 1/2 — 2026-08-11 — SHIP

- **Reviewer:** `/root/billing_gate_qa_lens`, independent and read-only; WISH and all six hashes matched.
- **Coverage topology:** PT/EN and decimal formatting, exact end-minus-1ms period, null/invalid/non-finite fallbacks, textual overage versus ARIA clamp, hostile enabled-credit payload, embedded billing use/absence and independently failing builder usage are each behavior-owned.
- **Evidence:** focused 8/8, normal-pool full 29/29, package typecheck, Vite build and diff-check passed; diagnostic single-thread legacy failures were correctly excluded as an artificial pool mode.
- **Severity gaps:** none. Recommendation `SHIP`.

### Group 4 Implementation and Engineer Validation — 2026-08-11

- **Engineer:** `/root/billing_group4_engineer`; exactly the five Group 4 paths changed, with no Git/Genie/commit/provenance/handoff mutation.
- **Hashes:** API docs `a4b9cf20a8658cf8a7d8f115866f98d3d006ce283440683618d172c61ab4260e`; DB docs `d71b2c03f69640e318d15f42c8bbb873d452f21c66b413b75dac627f22f2883f`; builder guide `ba7087f36a39d05ccbb9d6d4acb4b05f54696207e093b1e492106340f6926f9e`; scope auditor `62b1cf9cdbbfe54e19c435648e520fd3a53536dcc23da58715b8469564d7f3e7`; docs tests `5775c0a1cadbf65a7e60e007d6ff17764862ce717689ff051e73890e223dc7b9`.
- **Focused/static:** six docs/scope tests passed; exact focused and union mypy, 13-file isort/flake8/Black and diff-check passed.
- **Scope audit:** exact base accepted; changed-path union contained 25 sorted allowlisted paths, with missing/unexpected/duplicates/violations all empty. Its bounded hunk manifest records source states and explicitly leaves semantic mixed-hunk review as required and non-automated.

### Group 4 Aggregate Orchestrator Validation — 2026-08-11

- **Backend:** exact fail-closed aggregate gate produced `/tmp/billing-final-gate-20260811-junit.xml`, SHA-256 `1b2539211fc7be2d7e1eb58335e30cdf9bec7ff42cc2f83f93c13bc543a5f45e`: current 1,165 unique / 198 failures / 1 skip versus baseline 1,130 / 198, with zero missing baseline IDs and zero new failures.
- **PostgreSQL/Alembic:** guarded real smoke passed 1 / 9 deselected against a disposable loopback PostgreSQL 16; `alembic heads` returned exactly `016 (head)`. The `--rm` container was stopped and confirmed removed afterward.
- **Frontend:** package typecheck passed; normal-pool full suite passed 29/29; production Vite build passed with the same non-fatal source-map/chunk-size warnings. Temporary ignored dependency links were removed and build output moved to `/tmp`.
- **Final focused/static:** docs 6/6, both exact mypy commands, isort, flake8, Black and `git diff --check` passed from the complete 25-path tree.

### Group 4 Execution Acceptance Review — 2026-08-11T17:06:36-03:00 — SHIP

- **Reviewer:** `/root/billing_group1_final_acceptance`, independent and read-only; WISH `1883c8d047426fd99dd00f285f15d516cf70a3a21182065cb238a05817d483e3` and all five Group 4 hashes matched.
- **Contracts/tests:** each document independently owns its complete required contract through a unique bounded section and file-specific requirement dictionary; no cross-file alternation exists.
- **Scope/manual hunks:** live audit returned the exact 25-path union with no violations. Manual inspection assigned all six/eight/six/five paths to Groups 1–4 and found no dashboard, analytics, multimodal, credentials, interaction-origin, payment/enforcement or unrelated semantics.
- **Aggregate:** final JUnit hash/count/delta, PostgreSQL smoke, sole Alembic head, frontend gates and diff-check were independently verified. Severity gaps none; verdict `SHIP`.

### Group 4 Execution-quality/Security Review — 2026-08-11 — SHIP

- **Reviewer:** `/root/billing_group3_quality_review`, independent and read-only; WISH and Group 4 hashes matched.
- **Auditor:** Git uses argument arrays, `shell=False`, `check=True`, strict NUL/UTF-8 parsing and no stderr disclosure. Pinned base/ancestry, source union, duplicate states, rename/path/artifact safety, exact allowlist, bounded hashes/hunk headers and pre/postcommit behavior are correct.
- **Manual scope/docs:** no mixed semantics; all docs agree with the implementation. Recorded gates are complete.
- **[MEDIUM] Non-blocking recommendation:** future hardening should add malformed-NUL, Git failure, ancestry, duplicate-state, symlink/nonregular and temporary-repository pre/postcommit negative tests. Current implementation/live audit are correct; verdict `SHIP`.

### Group 4 Final QA Advisory — 2026-08-11 — SHIP

- **Reviewer:** `/root/billing_gate_qa_lens`, independent and read-only; WISH, five hashes, exact 25-path list and final JUnit independently matched.
- **Semantic review:** modified hunks and untracked forbidden-domain scan contain only the approved billing-usage work; existing dashboard words occur solely in the required builder partial-failure evidence, not a dashboard production change.
- **Gates/cleanup:** docs/static/backend/PostgreSQL/Alembic/frontend/diff evidence remains bound to the unchanged tree; no frontend symlink/cache/build artifact remains. Container absence relies on orchestrator evidence because the reviewer lacked Docker socket access.
- **Provenance:** HEAD remained the planning base and the handoff stayed ready. Severity gaps none; `SHIP` authorizes only the orchestrator finalization gate.

### Orchestrator Finalization Gate — 2026-08-11T17:20:18-03:00 — SHIP

- **Conventional commit:** `36d5d55f22fbb7a673fd86fb3aed7dd48a181a98` (`feat(billing): meter plan usage`) has the sole parent/planning base `7ab570ff0d10ed459e9e62bec5c8f32f88e94fff`.
- **Committed scope:** the postcommit auditor reports exactly the sorted 25-path allowlist, exclusively in committed state, with no missing, unexpected, duplicate, rename/path/artifact or other violations; the lane is clean and `git diff --check` passes.
- **Migration provenance:** `016` exists in the commit, revises `015`, and has Git blob `5e81da4f51e19710b4f552224deff580c285f247`. The lock-matched command `VIRTUAL_ENV=/home/lorenzo/.cache/pypoetry/virtualenvs/neurostack-api-L9nFPJwB-py3.12 poetry run alembic heads` returns exactly `016 (head)`.
- **Final validation:** aggregate JUnit SHA-256 `1b2539211fc7be2d7e1eb58335e30cdf9bec7ff42cc2f83f93c13bc543a5f45e` contains 1,165 unique cases, 198 baseline failures and one skip, with zero missing baseline IDs and zero new failures. PostgreSQL smoke, frontend 29/29, typecheck/build, documentation and static gates passed.
- **Pinned reviews:** `/root/billing_group1_final_acceptance` returned acceptance `SHIP`; `/root/billing_group3_quality_review` returned provenance/security `SHIP`; neither found a severity-tagged gap.
- **Published handoff:** `BILLING_USAGE_METERING_COMMIT_SHA=36d5d55f22fbb7a673fd86fb3aed7dd48a181a98`, `BILLING_016_BLOB_SHA=5e81da4f51e19710b4f552224deff580c285f247`, and `INTERACTION_ORIGIN_BASE_SHA=36d5d55f22fbb7a673fd86fb3aed7dd48a181a98` were reported on `t_msontnyf011833ab`; the card was marked done and the satisfied dependency block on `t_msompoj9a76a463c` was removed without starting that wish.

---

## Files to Create/Modify

```text
# Group 1
alembic/versions/20260810_000001_016_add_knowledge_document_size.py
scripts/init-db.sql
src/models/knowledge.py
src/api/routes/knowledge.py
tests/models/test_knowledge_document_size.py
tests/integration/test_billing_usage_postgres.py

# Group 2
.env.example
env.example
src/config/settings.py
src/services/billing_usage_service.py
src/api/routes/billing.py
src/api/schemas/billing.py
tests/services/test_billing_usage_service.py
tests/api/test_billing_usage.py

# Group 3
frontend/artifacts/neurostack/src/components/plan-usage-summary.tsx
frontend/artifacts/neurostack/src/components/plan-usage-summary.test.tsx
frontend/artifacts/neurostack/src/pages/billing.tsx
frontend/artifacts/neurostack/src/pages/billing-usage.test.tsx
frontend/artifacts/neurostack/src/pages/builder.tsx
frontend/artifacts/neurostack/src/pages/builder-usage.test.tsx

# Group 4
docs/api-contracts.md
docs/database-schema.md
docs/builder-integration-guide.md
scripts/check_billing_usage_scope.py
tests/docs/test_billing_usage_docs.py
```
