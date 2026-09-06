# Wish: CTA comercial no menu demo do investidor

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `menu-demo` |
| **Date** | 2026-09-06 |
| **Author** | Codex |
| **Appetite** | small |
| **Branch** | `wish/menu-demo` |
| **Repos touched** | `frontend/artifacts/neurostack` |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Adicionar ao menu lateral do perfil investidor um CTA amarelo “Falar com comercial”, imediatamente abaixo da logo da NeuroStack, seguindo a referência visual fornecida. O CTA abre o WhatsApp comercial da NeuroStack no número informado (`11 2110-5486`), permitindo que quem explora a demo inicie uma conversa comercial sem procurar outro canal.

## Problem

Investidores na demo não têm um caminho imediato, restrito ao seu papel e posicionado abaixo da logo, para iniciar contato com o WhatsApp comercial.

## Scope

### IN

- Renderizar o CTA somente quando o papel autenticado for `investor`, tanto no sidebar desktop quanto no menu móvel que reutiliza o mesmo conteúdo.
- Posicionar o CTA entre a área da logo e a seção “Clínica Ativa”, com fundo amarelo/âmbar, texto e ícone de conversa pretos, largura disponível do sidebar, cantos levemente arredondados e rótulo centralizado em negrito, conforme a imagem de referência `/mnt/c/Users/dell0/Downloads/image (6).png`.
- Direcionar o CTA para `https://wa.me/551121105486`, abrindo em nova aba com as proteções adequadas para link externo.
- Adicionar traduções do rótulo ao catálogo existente: “Falar com comercial” em português e “Talk to sales” em inglês.
- Cobrir visibilidade por papel, destino externo, segurança do link e conteúdo localizado com testes do componente de layout.

### OUT

- Criar endpoint, persistência, painel de configuração ou variável de ambiente para o número comercial.
- Adicionar mensagem pré-preenchida, parâmetros de campanha, analytics ou rastreamento de cliques.
- Exibir o CTA para `clinic_admin`, `builder` ou `super_admin`.
- Alterar os demais itens, permissões ou ordem de navegação do sidebar.
- Verificar ou provisionar a conta de WhatsApp associada ao número informado; o escopo garante somente a formação e abertura correta do link.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Usar o link direto `https://wa.me/551121105486` | O formato `wa.me` exige apenas dígitos com código do país; normaliza o número fornecido como Brasil (`55`) + DDD `11` + `2110-5486` sem adicionar fluxo próprio. |
| 2 | Condicionar o CTA a `isInvestor` no conteúdo compartilhado do sidebar | O layout já expõe esse sinal e a mesma função renderiza desktop e mobile, evitando duplicação e impedindo exposição para os outros papéis. |
| 3 | Abrir em nova aba com `target="_blank"` e `rel="noopener noreferrer"` | Mantém a demo aberta durante o contato e protege a página de origem ao navegar para um serviço externo. |
| 4 | Usar o sistema de i18n existente | O menu já alterna entre português e inglês; adicionar uma chave em ambos os catálogos evita introduzir texto fixo e preserva a experiência internacional da demo. |
| 5 | Manter número e apresentação no próprio componente | Há um único CTA e nenhum requisito atual de edição em runtime; uma configuração administrativa acrescentaria estado e falhas sem benefício presente. |

## Simplicity Case

- **Simplest complete design:** um único link estilizado dentro de `layout.tsx`, logo abaixo da logo, protegido por `isInvestor`, usando um ícone Lucide já instalado, uma chave de tradução e o URL fixo do WhatsApp.
- **Added machinery:** apenas duas entradas nos catálogos de tradução e assertions no teste existente do layout; ambas são exigidas pelo comportamento bilíngue e pela restrição de visibilidade por papel.
- **Deferred until measured:** tornar número, mensagem ou campanha configuráveis somente quando existir um segundo destino comercial ou uma solicitação explícita de edição sem deploy; adicionar analytics somente quando houver uma métrica de conversão definida.
- **Complexity removed:** nenhum endpoint, estado React, feature flag, dependência, rota interna, formulário intermediário ou componente novo.

## Dependencies

**depends-on:** none
**blocks:** none

## Success Criteria

- [ ] Um usuário com papel `investor` vê o CTA imediatamente abaixo da logo e antes de “Clínica Ativa” no sidebar desktop.
- [ ] O mesmo usuário vê o CTA no menu móvel, pois ambos os shells reutilizam o mesmo conteúdo do sidebar.
- [ ] O CTA apresenta fundo amarelo/âmbar, ícone de conversa preto, texto preto em negrito, largura disponível com respiro lateral e cantos levemente arredondados, preservando o estilo mostrado na referência.
- [ ] Em português, o texto visível é exatamente “Falar com comercial”; em inglês, é “Talk to sales”.
- [ ] O CTA possui `href="https://wa.me/551121105486"`, `target="_blank"` e `rel="noopener noreferrer"`.
- [ ] O CTA não é renderizado para os papéis `clinic_admin`, `builder` e `super_admin`.
- [ ] O CTA é acionável por teclado e seu texto visível fornece nome acessível inequívoco.
- [ ] Os testes do layout, o typecheck, a suíte frontend e o build de produção passam sem regressão.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — aceitação visual baseada em referência requer julgamento subjetivo; mudança permanece em um pacote e tem testes determinísticos | `engineer-standard` / medium | Implementar, localizar e validar o CTA comercial condicionado ao perfil investidor no layout compartilhado. |

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

### Group 1: CTA comercial do investidor

**Goal:** Disponibilizar um acesso visualmente fiel e exclusivo do perfil investidor ao WhatsApp comercial da NeuroStack em todos os formatos do menu.

**Deliverables:**
1. `src/components/layout.tsx` atualizado com o link externo condicionado a `isInvestor`, posicionado abaixo da logo e estilizado conforme a referência.
2. `src/locales/pt.ts` e `src/locales/en.ts` atualizados com o rótulo localizado do CTA.
3. `src/components/layout.test.tsx` atualizado com cobertura positiva para investidor e negativa para os demais papéis, além dos atributos do link e textos localizados.

**Acceptance Criteria:**
- [ ] O conteúdo compartilhado do sidebar renderiza exatamente um CTA comercial por instância do menu quando `isInvestor === true` e nenhum quando `isInvestor === false`.
- [ ] No DOM, o contêiner do CTA sucede a área da logo e antecede a seção identificada por `data-testid="active-clinic-name"`.
- [ ] O link usa exatamente `https://wa.me/551121105486`, abre em nova aba e declara `noopener noreferrer`.
- [ ] O CTA segue a hierarquia visual da referência: amarelo/âmbar de alto contraste, ícone de conversa e rótulo pretos, texto centralizado em negrito, largura do menu com margem lateral, altura confortável e raio discreto.
- [ ] O rótulo é obtido via `t(...)` e resolve para “Falar com comercial” em `pt` e “Talk to sales” em `en`.
- [ ] Testes parametrizados ou equivalentes comprovam ausência para `clinic_admin`, `builder` e `super_admin`.
- [ ] O elemento interativo é um link semântico alcançável por teclado e com nome acessível derivado do rótulo visível.
- [ ] Nenhuma chamada de API, estado local ou dependência nova é introduzida.

**Validation:**
```bash
# O teste focado prova o contrato do CTA; typecheck valida a paridade das chaves i18n.
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/components/layout.test.tsx
pnpm --dir frontend/artifacts/neurostack typecheck

# Como Layout envolve o shell compartilhado da aplicação, a suíte completa e o build preservam o gate agregado do frontend.
pnpm --dir frontend/artifacts/neurostack test
PORT=3001 BASE_PATH=/ pnpm --dir frontend/artifacts/neurostack build
```

**depends-on:** none

---

## QA Criteria

_What must be verified on dev after merge. The QA agent tests each criterion._

- [ ] Entrar como `investor`, abrir o sidebar desktop e comparar posição, cor, contraste, espaçamento, ícone e peso do texto com `/mnt/c/Users/dell0/Downloads/image (6).png`.
- [ ] Reduzir para viewport móvel, abrir o menu e confirmar o mesmo CTA entre a logo e “Clínica Ativa”, sem corte ou overflow horizontal.
- [ ] Com locale `pt`, confirmar “Falar com comercial”; alternar para `en` e confirmar “Talk to sales”.
- [ ] Ativar o CTA e confirmar que uma nova aba tenta abrir `https://wa.me/551121105486`, mantendo a demo aberta.
- [ ] Navegar por Tab e acionar o CTA por teclado, confirmando foco visível e nome acessível.
- [ ] Entrar separadamente como `clinic_admin`, `builder` e `super_admin` e confirmar que o CTA não aparece e o restante do sidebar mantém ordem e espaçamento anteriores.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| O número informado não estar habilitado no WhatsApp | Medium | Tratar o destino fornecido e normalizado como entrada aceita sob responsabilidade do produto e validar somente a formação e abertura de `https://wa.me/551121105486`, sem verificação externa da conta. |
| A referência está fora do repositório e pode não acompanhar outro ambiente | Low | Registrar no wish os atributos visuais observáveis e os critérios de posição, contraste e espaçamento necessários para implementação e QA. |
| Um CTA externo comprometer a navegação da demo | Low | Abrir em nova aba com `noopener noreferrer`, preservando a sessão atual. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### Plan Review — 2026-09-06T09:40:29-03:00

- **Context:** before-work plan review.
- **Reviewed target:** `.genie/wishes/menu-demo/WISH.md` at SHA-256 `3487d138c998007d4983b72561df24048d78652a655852882204b11426c93389`.
- **Reference evidence:** `/mnt/c/Users/dell0/Downloads/image (6).png`, SHA-256 `a04462c46d012f9d202b273386485e136ada45869cec1d85e275b3411cce7174`, valid 360×281 PNG showing the yellow CTA below the logo.
- **Repository evidence:** `isInvestor` maps to `role === "investor"`; desktop and mobile reuse `renderSidebarContent()`; English defines the translation-key type and Portuguese must remain in parity; package scripts expose Vitest, TypeScript, and Vite gates.
- **Validation inspection:** planned commands were inspected statically but not executed by the read-only reviewer because product behavior is not implemented yet and Vite writes build output.
- **Verdict:** **FIX-FIRST**.
- **Gaps:** [HIGH] build command omitted required `PORT` and `BASE_PATH`; [MEDIUM] no explicit one-sentence problem statement; [MEDIUM] external WhatsApp verification conflicted with Scope OUT; [LOW] reference image path was not absolute.
- **Correction:** fix loop 1 added the problem statement, made the image path absolute, treated account availability as an accepted external risk, supplied deterministic Vite environment values, and corrected the focused Vitest invocation after local evidence showed the prior command launched the full suite.

### Plan Re-review — 2026-09-06T09:49:48-03:00

- **Context:** before-work plan review, fix loop 1/2.
- **Reviewed target:** `.genie/wishes/menu-demo/WISH.md` at SHA-256 `f0bdf2efa8e6551c88f9077f277aa227b2decfa1efa7b469bb221f4a4027c0b9`.
- **Evidence:** the problem is explicit; the 360×281 reference image exists at the recorded absolute path; `isInvestor` maps exactly to the investor role; desktop and mobile share `renderSidebarContent()`; locale types enforce English/Portuguese key parity; and all focused, aggregate, typecheck, and environment-complete build commands are valid and proportional.
- **Prior gaps:** all resolved — explicit problem, absolute reference path, accepted external account risk, truly focused Vitest command, and required Vite environment values.
- **Questioner advisory:** no blocking finding; the `55` country-code normalization remains an accepted product assumption derived from the supplied Brazilian-format number.
- **Severity-tagged gaps:** none.
- **Verdict:** **SHIP**.

### Group 1 Execution Review — 2026-09-06T10:37:27-03:00

- **Task:** `t_mtpsq0ce91b39c18` (`cta-comercial-investidor`).
- **Reviewed target:** four-file diff at SHA-256 `0e5bfa0fa22086e502b74fd5f816df304cc4a49bcef6adaff9b02fef300b4ac7`.
- **Acceptance evidence:** investor-only role guard; shared desktop/mobile rendering; CTA directly between logo and Active Clinic; exact WhatsApp URL and external-link protections; localized labels; accessible anchor/focus styling; and negative coverage for all excluded roles.
- **Validation:** `git diff --check` passed; focused Layout Vitest passed `18/18`; typecheck passed with no diagnostics; production build passed with 2,804 modules; isolated aggregate suite (`HEAD` + only the reviewed menu-demo changes) passed `18/18` files and `147/147` tests.
- **Isolation rationale:** the main checkout acquired unrelated concurrent changes to Master Admin and `ctx_empty`; its aggregate failure was excluded by validating the captured feature diff in `/tmp/neurostack-menu-demo-validation.aW7Fo5`. Those concurrent changes were neither modified nor attributed to this wish.
- **Severity-tagged gaps:** none.
- **Verdict:** **SHIP**.

### Group 1 Quality Review — 2026-09-06T13:05:49Z

- **Reviewed target:** four-file diff at SHA-256 `0e5bfa0fa22086e502b74fd5f816df304cc4a49bcef6adaff9b02fef300b4ac7`.
- **Evidence:** secure external link, correct role gating and shared responsive placement, native keyboard accessibility with visible focus, existing i18n conventions, deterministic coverage, negligible runtime impact, and no state/API/dependency/configuration additions.
- **Severity-tagged gaps:** none.
- **Residual risk:** WhatsApp account availability remains the accepted external product risk; browser-level visual comparison remains post-merge QA.
- **Verdict:** **SHIP**.

---

## Files to Create/Modify

```
frontend/artifacts/neurostack/src/components/layout.tsx
frontend/artifacts/neurostack/src/components/layout.test.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
```
