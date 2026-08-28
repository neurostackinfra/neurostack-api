# Brainstorm: Programa de origem confiável e segurança de treinamento

| Campo | Valor |
|---|---|
| **Slug** | `interaction-origin-training-safety` |
| **Data** | 2026-08-11 |
| **WRS** | 100/100 |
| **Estado** | Ready |
| **Origem** | Endurecimento complementar a `audit-rlhf-rating` e `lgpd-builder-consents` |

## WRS

`██████████ 100/100`

Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅

## Problema

O sistema não possui uma origem de interação confiável, derivada pelo backend, capaz de impedir que simulações sejam aprovadas para treinamento ou contaminem reclamações e outras métricas operacionais de produção.

## Decomposição do programa

O pedido cruza subsistemas que podem ser entregues e testados separadamente. A revisão consultiva de arquitetura confirmou que até a primeira divisão ainda misturava persistência, rating e várias superfícies analíticas. O programa final fica dividido em:

1. **Núcleo de origem confiável** — o DESIGN atual: schema, contexto autorizado, matriz de ingressos, EventService/EventStore, ID canônico de `message.in`, propagação e legado fail-closed.
2. **Rating e gate de treinamento** — vínculo do rating ao `message.in`, remoção de `clinic_tenant_id` do trust boundary, gate provisório, auditoria e copy de pré-seleção; depende de (1).
3. **Analytics de produção** — filtro server-side em traces, builder/admin/health e UI, com `production` default e canal separado; depende de (1).
4. **Isolamento multi-tenant no PostgreSQL** — papéis separados, RLS fail-closed nos dois pools e integridade composta entre evento/sessão/tenant.
5. **Consentimento e revisão humana auditáveis** — ledgers append-only de autorização, revogação e decisão humana, com ator, finalidade, vigência, versão e hash.
6. **Snapshots de dataset e anonimização** — seleção obrigatória por um único `tenant_id`, anonimização verificável, manifestos imutáveis, hashes por item e hash raiz.
7. **Linhagem de treinamento e modelos** — training runs e artefatos ligados aos snapshots, versões, base model, código, parâmetros e clínicas autorizadas.

Dependências propostas: (2) e (3) dependem de (1). O snapshot (6) depende de (1), (4) e (5), além de consumir as decisões auditáveis produzidas por (2)/(5); ele é a única porta de entrada para treinamento. A linhagem (7) depende de (6). Nenhum consumidor de treinamento deve ser habilitado antes dessas dependências estarem ativas.

O `DESIGN.md` cristaliza apenas o item (1), que cabe em um wish independente. Os requisitos dos demais itens permanecem registrados neste DRAFT para seus brainstorms próprios.

## Escopo deste brainstorm

### IN

- Coluna persistida, imutável e indexada `events.interaction_origin`, com valores fechados `production`, `simulator` e `unknown`; `channel` e provedor de ingresso continuam dimensões separadas.
- Origem definida exclusivamente por um contexto interno do backend, ausente dos schemas de entrada do cliente, e propagada a todos os eventos derivados da mesma execução.
- Classificação inicial: webhook de provedor validado = `production`; `POST /v1/events` com API key tenant-scoped validada = `production`; chat interno do simulador via JWT = `simulator`; caminho não classificável = `unknown`.
- Validação de tenant/sessão antes da classificação. Um evento não recebe origem confiável quando o tenant alvo ou a sessão vierem apenas de payload não validado.
- Rating referencia uma interação canônica resolvida no backend; o servidor deriva dela `tenant_id`, `session_id` e `interaction_origin`. `clinic_tenant_id` e textos reenviados pelo frontend não são autoridades de proveniência.
- Rejeição server-side de qualquer tentativa de aprovar `simulator` ou `unknown`. Para `production`, o gate provisório também exige `builder_slm_training is true` e identidade humana super-admin; o snapshot futuro exigirá o ledger versionado do subprojeto 3.
- Migração conservadora: no estado atual não há evidência histórica suficientemente confiável para promover registros; legado começa como `unknown` e é inelegível.
- Estatísticas de traces, builder/admin dashboards, séries e alertas operacionais filtram `production` por padrão. `simulator`, `unknown` e `all` exigem seleção explícita e preservam o escopo de tenant já autorizado.
- UI exibe a origem efetiva, usa “Somente produção” por padrão e não oferece aprovação para registros inelegíveis.
- Testes PostgreSQL/API/UI cobrindo spoofing, propagação, imutabilidade, aprovação, legado e agregações por origem.

### OUT

- Políticas PostgreSQL RLS, papéis de aplicação/migração e identidade transacional do tenant (subprojeto 2).
- Constraints compostas entre evento, sessão e tenant, inclusive no Event Store secundário (subprojeto 2).
- Ledger definitivo de consentimento e aprovação/revogação (subprojeto 3).
- Materialização de snapshots e anonimização (subprojeto 4).
- Training runs, registro/deploy de artefatos e linhagem de modelo (subprojeto 5).
- Execução de fine-tuning ou mudanças no provedor/modelo.
- Reclassificação heurística de legado por `channel`, metadata ou formato de telefone.
- Alterar cobrança/billing por origem; essa é uma decisão comercial separada.

## Decisões já dadas pelo usuário

- `channel = "whatsapp"` não representa origem e não será usado como substituto definitivo.
- O backend é a única autoridade para definir `interaction_origin`.
- `unknown` é fail-closed: não participa de treinamento.
- Aprovação humana não supera origem ou consentimento inválidos.
- Métricas de reclamação e demais estatísticas operacionais usam apenas produção por padrão.
- A origem da interação avaliada é diferente da superfície onde o feedback foi enviado.

## Abordagens em avaliação

### A. Origem canônica no evento, passada por adaptadores confiáveis — recomendada

Cada ingresso resolve um `InteractionOrigin` interno antes do pipeline. O Event Store exige esse valor, usa `unknown` como fallback seguro e o persiste em coluna com `CHECK`, índice composto e proteção contra alteração. Rating aponta para o evento original e herda sua origem no servidor.

- Estados duráveis novos: 1 coluna enum-like e 1 referência canônica da avaliação à interação.
- Invariantes novas: origem não vem do DTO; eventos derivados preservam a origem; rating e interação têm o mesmo tenant; aprovação exige `production`.
- Recuperação: classificação ou resolução falhou → `unknown`, sem retry, fila ou estado intermediário.
- Índice proposto conforme as consultas reais: `(tenant_id, interaction_origin, event_type, created_at DESC)`.

### B. Origem apenas na sessão

Reduz escrita repetida, mas exige join em toda métrica/dataset, não cobre eventos sem sessão e torna ratings históricos frágeis. Também transforma a sessão numa autoridade mais ampla que a interação efetivamente avaliada. Não recomendada.

### C. Tabela separada de proveniência

Permite múltiplas transições e histórico rico, mas adiciona entidade, joins e invariantes sem requisito atual para mudar a origem após ingestão. Só se justifica se surgir proveniência multiestágio; isso pertence à linhagem de datasets/modelos, não ao classificador de ingresso.

Inferir dinamicamente por `channel` ou metadata foi descartado: ambos podem ser enviados pelo cliente e não distinguem um chat web real do simulador.

## Simplicity Gate — aprovado

A abordagem A é o desenho completo mais simples deste subprojeto: uma origem imutável definida na ingestão, fallback fail-closed, uma referência verificável do rating à interação e um filtro compartilhado nas leituras. Não há necessidade atual de cache, fila, reconciliação periódica, reclassificação heurística, tabela de transições ou configuração de origem por clínica.

O mecanismo adicional de imutabilidade é pago diretamente pelo requisito de proveniência: a migração faz o backfill antes de instalar proteção para impedir mudança posterior da coluna. Ledgers, anonimização, snapshots e linhagem não serão comprimidos nesta entrega; cada um possui estado e recuperação próprios e ficou atrás das dependências mensuráveis acima.

## Critérios iniciais

- [ ] `interaction_origin` enviado no body ou metadata é ignorado/rejeitado e nunca promove um registro.
- [ ] Webhook com autenticidade validada persiste `production`; chat do simulador persiste `simulator`; ingresso não classificável persiste `unknown`.
- [ ] `message.in`, `srm.decision`, `prompt.generated` e demais eventos derivados da mesma execução carregam a mesma origem.
- [ ] Tentativa de alterar `interaction_origin` após a inserção falha no PostgreSQL.
- [ ] Rating com referência inexistente, estrangeira ou de outro tenant falha sem carregar credenciais nem copiar dados; não usa `clinic_tenant_id` do cliente.
- [ ] Rating válido herda tenant, sessão e origem da interação original, mesmo quando enviado pela tela de auditoria.
- [ ] Aprovação de `simulator` ou `unknown` retorna erro de domínio e não altera o registro.
- [ ] Aprovação de `production` sem `builder_slm_training is true` ou sem usuário humano autorizado também falha.
- [ ] Backfill deixa todo legado sem prova persistida confiável como `unknown`; `channel=whatsapp` ou `channel=web` isoladamente nunca promove dados.
- [ ] Dashboard sem filtro explícito calcula reclamações, usuários, atividade, séries e alertas somente sobre `production`, inclusive consultas históricas auxiliares.
- [ ] Auditoria técnica pode consultar `simulator`, `unknown` ou `all` explicitamente sem misturá-los aos KPIs padrão.
- [ ] A UI rotula a origem e desabilita a aprovação de itens inelegíveis, mas testes provam que o backend rejeita a mesma chamada forjada.

## Decisões confirmadas em 2026-08-11

1. **Semântica de consentimento:** `builder_slm_training` é o gate contratual provisório por clínica. Consentimento individual por paciente/interação fica OUT até existir requisito legal/produto explícito; snapshots futuros exigirão o ledger versionado do subprojeto 3.
2. **Contrato do endpoint genérico:** `POST /v1/events` autenticado por API key tenant-scoped e validado contra tenant/sessão é `production`; o fluxo do painel via JWT é `simulator`. Um chat web real futuro deverá usar adaptador ou credencial de produção dedicado, sem depender de `channel="web"`.

## Riscos e perguntas abertas

- `POST /v1/events` usa hoje `tenant_id`/`session_id` do body e a carga/salvamento de sessão busca apenas por ID; origem confiável depende de validação de ownership agora e da constraint/RLS do subprojeto 2.
- `clinic_tenant_id` do rating é controlado pelo frontend e atualmente pode influenciar a resolução de credenciais; precisa ser removido do trust boundary.
- O simulador usa um timestamp local como `message_id`; o backend deve devolver e o frontend deve usar o ID canônico do evento original.
- O mesmo componente de rating avalia simulador e traces de produção; `source="chat_simulator"` descreve a superfície de feedback, não a origem avaliada.
- `builder_slm_training` vive em JSONB mutável, é validado apenas pela UI e não possui ator, versão, vigência ou revogação; serve somente como gate conservador até o ledger do subprojeto 3.
- Eventos são documentados como append-only, mas ratings são atualizados in-place; a aprovação auditável definitiva deve sair do JSON no subprojeto 3.
- Estatísticas estão duplicadas em traces, builder, admin e helpers de saúde; um filtro aplicado só no dashboard principal deixaria reclamações, alertas ou usuários contaminados.
- O Event Store usa um pool separado; o contexto RLS do subprojeto 2 deve cobrir ambos os caminhos.
- Billing também conta `message.in`, mas excluir simulador da cobrança exige decisão comercial explícita e permanece OUT.

## Evidências do código

- `src/api/schemas/events.py`: `channel` e metadata são controlados pelo cliente.
- `src/api/routes/webhook.py`: adaptadores Z-API, Evolution, Uazapi e Meta criam ingressos de produção, mas o provider não chega à coluna persistida.
- `src/services/event_service.py`: persiste os eventos derivados e hoje descarta metadata de origem; sessão é buscada apenas por ID.
- `src/api/routes/rlhf.py`: rating confia em `clinic_tenant_id` e aprovação altera `approved_for_training` sem origem/consentimento.
- `frontend/artifacts/neurostack/src/pages/chat.tsx`: simulador envia `channel="web"` e usa ID local no rating.
- `frontend/artifacts/neurostack/src/components/trace-viewer.tsx`: o mesmo rating também é usado em trace histórico.
- `src/services/trace_service.py`: reclamações são parte de `intent_distribution`; período e histórico auxiliar hoje misturam origens.
- `src/api/routes/builder.py`, `src/api/routes/admin.py` e `src/api/helpers/health.py`: agregações adicionais exigem o mesmo default de produção.
- `alembic/versions/20251231_000000_001_initial_schema.py`: não há origem nem constraint composta tenant/sessão.

## Contexto relacionado

- `.genie/brainstorms/audit-rlhf-rating/DESIGN.md`
- `.genie/wishes/audit-rlhf-rating/WISH.md`
- `.genie/wishes/lgpd-builder-consents/WISH.md`
