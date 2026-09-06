# Wish: Corrigir exemplo de Botox na área RAG

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `rag-botox-post-procedure-copy` |
| **Date** | 2026-09-06 |
| **Author** | Codex |
| **Appetite** | small |
| **Branch** | `wish/rag-botox-post-procedure-copy` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Corrigir o nome do documento de exemplo exibido na Base de Conhecimento (RAG), trocando a classificação incorreta de Botox como contexto pós-operatório por pós-procedimento. A mudança preserva o formato visual atual do nome do arquivo e evita comunicar que Botox é uma cirurgia.

## Scope

### IN

- Alterar o documento mock `Protocolo_Pos_Operatorio_Botox.pdf` para `Protocolo_Pos_Procedimento_Botox.pdf` na tela de Base de Conhecimento (RAG).
- Adicionar cobertura automatizada que confirme o novo exemplo e impeça a reintrodução do termo incorreto nessa tela.

### OUT

- Alterar o funcionamento de ingestão, indexação ou consulta RAG.
- Revisar outros textos sobre pós-atendimento ou renomear documentos reais enviados por usuários.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Substituir apenas `Operatorio` por `Procedimento` no nome mock | Mantém a estrutura e o contexto do exemplo, corrigindo somente a imprecisão apontada. |
| 2 | Fixar o texto em um teste de renderização da página | O texto é conteúdo visível de uma tela React; validar a renderização protege o contrato percebido pelo usuário. |

## Simplicity Case

- **Simplest complete design:** editar uma única string estática no array `mockDocuments` e cobrir sua renderização com um teste focado.
- **Added machinery:** somente um teste de regressão de conteúdo, exigido porque o nome incorreto é visível ao usuário e pode voltar sem uma asserção explícita.
- **Deferred until measured:** centralização ou internacionalização dos nomes dos documentos mock; considerar apenas se novos exemplos localizados forem solicitados.
- **Complexity removed:** nenhuma migração, alteração de API, configuração ou lógica condicional.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [x] A lista de documentos indexados da Base de Conhecimento exibe `Protocolo_Pos_Procedimento_Botox.pdf`.
- [x] A mesma lista não exibe mais `Protocolo_Pos_Operatorio_Botox.pdf`.
- [x] Um teste automatizado da página comprova as duas condições de texto.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 0 — alteração localizada de texto com teste determinístico | engineer-trivial / low | Corrigir o nome mock e adicionar o teste de regressão. |

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

### Group 1: Corrigir exemplo de documento RAG

**Goal:** Exibir o exemplo de Botox como material pós-procedimento, sem classificá-lo como pós-operatório.

**Deliverables:**
1. Nome corrigido no documento mock da página `knowledge-base.tsx`.
2. Teste de renderização cobrindo a presença do novo nome e a ausência do anterior.

**Acceptance Criteria:**
- [x] `Protocolo_Pos_Procedimento_Botox.pdf` aparece na lista renderizada.
- [x] `Protocolo_Pos_Operatorio_Botox.pdf` não aparece na lista renderizada.
- [x] O teste focado da página passa sem alterar o comportamento RAG ou os demais documentos mock.

**Validation:**
```bash
cd frontend/artifacts/neurostack
pnpm exec vitest run src/pages/knowledge-base.test.tsx
```

O teste focado é proporcional ao risco de uma troca de conteúdo estático e valida diretamente o texto entregue pela página. O gate agregado do frontend permanece `pnpm test` para a verificação de integração antes do merge.

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Abrir a Base de Conhecimento (RAG) e confirmar visualmente o nome `Protocolo_Pos_Procedimento_Botox.pdf`.
- [ ] Confirmar que os dois documentos mock continuam sendo exibidos com seus tipos, tamanhos e status.
- [ ] Executar `cd frontend/artifacts/neurostack && pnpm test` antes do merge.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| O texto corrigido permanecer sem cobertura se o teste consultar a constante em vez da UI | Low | Renderizar a página e fazer as asserções pelo texto acessível ao usuário. |

---

## Review Results

### Plan review — 2026-09-06T09:47:21-03:00

```yaml
context: PLAN REVIEW
target: .genie/wishes/rag-botox-post-procedure-copy/WISH.md
target-sha256: 0cc998b4fd1084c8d5d19d4963fd2ff7c0099cf0db2169f5c41fc49814dffe33
repository-head: d5ee36ac3c76f555a2b538f37194b97919b815cc
snapshot: uncommitted working-tree wish; not pinned
verdict: SHIP
gaps: none
```

- A ocorrência em `knowledge-base.tsx` é estática e renderizada diretamente por `mockDocuments`.
- Escopo, exclusões, dependências, critérios, validação focada, gate agregado e arquivos planejados estão completos.
- O linter de wishes prescrito não está disponível na raiz deste repositório; a estrutura foi validada manualmente sem encontrar placeholders ou seções ausentes.
- O revisor foi somente leitura e não alterou arquivos, estado de tarefas ou Git.

### Group 1 implementation attempt — 2026-09-06

```yaml
task: t_mtpszojw659dcce3
worker: rag-copy-engineer
outcome: failed
diagnosis: env-tool-failure
fix-attempts: 0
```

- A implementação ficou restrita aos dois arquivos planejados e a tarefa permanece `in_progress`.
- O comando `cd frontend/artifacts/neurostack && pnpm exec vitest run src/pages/knowledge-base.test.tsx` iniciou o Vitest, mas duas execuções ficaram paradas após `RUN v2.1.9`, sem resultado de teste ou código de saída.
- Rota corretiva: revisar o diff e repetir a validação pelo orquestrador; não há evidência de falha funcional ou de capacidade do modelo.

### Group 1 validation and execution review — 2026-09-06T10:10:05-03:00

```yaml
task: t_mtpszojw659dcce3
context: EXECUTION REVIEW
repository-head: d5ee36ac3c76f555a2b538f37194b97919b815cc
source-sha256: fddd3f40b40725f50c38a268caea34152a124d9d9baf9b7aa6d57bc7865ddc07
test-sha256: e891b61d5648f464148a5fd4381484661a5b4f74a7ba271fe17a9d5a83a6b382
verdict: SHIP
quality-verdict: SHIP
gaps: none
```

- O travamento direto foi rastreado a I/O do WSL 9p em `/mnt/c`, antes da criação de workers do Vitest; um teste existente e pools alternativos reproduziram o mesmo bloqueio.
- Uma cópia exata em `/tmp`, com hashes iguais aos arquivos do workspace e dependências do mesmo lockfile, executou `pnpm exec vitest run src/pages/knowledge-base.test.tsx` com exit `0`: 1 arquivo e 1 teste aprovados em 3,06 s.
- As checagens de contrato textual e `git diff --check` no workspace passaram com exit `0`.
- As revisões funcional e de qualidade não encontraram problemas de correção, segurança, desempenho, manutenibilidade, regressão ou ampliação de escopo.

---

## Files to Create/Modify

```
frontend/artifacts/neurostack/src/pages/knowledge-base.tsx
frontend/artifacts/neurostack/src/pages/knowledge-base.test.tsx
```
