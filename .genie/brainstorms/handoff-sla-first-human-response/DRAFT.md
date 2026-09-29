# Brainstorm: SLA de atendimento confirmado no handoff

| Campo | Valor |
|---|---|
| Slug | `handoff-sla-first-human-response` |
| Data | 2026-09-27 |
| Estado | Pronto para design review |
| WRS | 100/100 |

## Ideia original

Conferir se a meta configurada de tempo de atendimento — por exemplo, atender em até 10 minutos — é comparada corretamente com os atendimentos e representada no radar da fila de handoff, com testes que protejam essa relação.

## Evidência confirmada

- A fórmula PostgreSQL atual trata o limite de forma inclusiva: 599 e 600 segundos entram numa meta de 10 minutos; 601 não entra.
- O frontend consome `pct_within_sla` diretamente no radar; o teste de regressão com 66,7% prova esse mapeamento.
- O relógio atual termina quando o operador abre o WhatsApp/retira o card, antes de qualquer atendimento confirmado.
- O denominador usa somente concluídos com `data_hora_saida`, omitindo filas vencidas e timeout.
- A meta atual é reaplicada a todo o histórico; não há snapshot por ocorrência.
- A fila/radar lê PostgreSQL, mas os endpoints tradicionais alteram um singleton em memória, criando estados divergentes.
- O PATCH genérico aceita `in_progress` sem gravar `data_hora_saida`; há mais de uma matriz de lifecycle.
- O export em andamento usa o fuso do navegador e exclui `in_progress`, contratos incompatíveis com coorte da clínica e atendimento confirmado.
- A migração do singleton precisa preservar Event Store, webhooks/delivery status, métricas e campos enriquecidos hoje só em memória.
- Z-API, Evolution e Uazapi possuem sinais `fromMe` por exclusão de ecos da IA, porém não provam autoria humana; a Meta não entrega outbound próprio nesse fluxo. Não há evento automático canônico entre provedores.

## Alternativas consideradas

1. **Somente adicionar testes ao comportamento atual.** Rejeitada porque consolidaria o viés do denominador, a reclassificação histórica e o encerramento prematuro ao abrir o WhatsApp.
2. **Inferir primeira resposta por webhooks `fromMe`.** Rejeitada nesta entrega porque não há paridade com Meta, alguns webhooks não têm autenticação suficiente e automações externas podem parecer humanas.
3. **Atendimento confirmado manualmente, com PostgreSQL canônico.** Escolhida: é verificável em todos os provedores, separa abrir o canal de confirmar o atendimento e permite testar a relação exata entre meta e ocorrência.

## Decisões

- O KPI será nomeado **atendimento confirmado**, não “primeira resposta humana comprovada”.
- “Abrir WhatsApp” apenas abre o canal e mantém o relógio/card ativos.
- “Confirmar atendimento” grava `data_hora_saida` uma única vez e move o handoff para `in_progress`; conclusão/resolução permanece uma etapa distinta.
- Cada novo handoff guarda `sla_target_seconds`, capturado da meta válida na entrada.
- Registros legados não são retroativamente classificados: meta nula os exclui do KPI novo e aparece como cobertura não mensurada.
- Numerador: ocorrências confirmadas em `data_hora_saida - created_at <= sla_target_seconds`.
- Denominador: confirmadas em qualquer tempo + não confirmadas cujo prazo já venceu; pendentes ainda dentro do prazo e canceladas antes da confirmação ficam fora.
- Timeout sem confirmação passa a ser violação ao atingir o prazo.
- Sem denominador, API e UI mostram “sem dados”, nunca 0%.
- O percentual fixo de 85% sai da interface; ele não é a meta configurada. O radar mostra somente a proporção dentro da meta de tempo de cada ocorrência.
- Cards usam a meta snapshot do próprio handoff: verde antes de 50%, amarelo de 50% até o limite inclusive e vermelho depois do limite.
- `handoff_records` passa a ser a única fonte de verdade para criação e transições; `_handoffs` em memória deixa de governar endpoints.
- `HandoffService` será stateless sobre factory de unit-of-work; repository CAS tenant-scoped retorna resultados tipados e um mapper único recompõe Detail/ListItem.
- O CAS fixa `status + lifecycle_version` observados e incrementa a versão atomicamente; após lock, perdedor não aceita estado novo nem emite efeitos.
- `updated_at TIMESTAMPTZ` passa a ser durável e é escrito no mesmo CAS, preservando o contrato após restart.
- PATCH genérico rejeita `in_progress`; somente a confirmação atômica entra nesse estado.
- O fuso IANA configurado da clínica governa radar, cards e CSV; a API devolve o período resolvido. Browser não redefine a coorte.
- Backend filtra `items` pelo mesmo intervalo do KPI; frontend remove `ymd(new Date(...))` e não refiltra a lista no fuso local.
- O CSV inclui `in_progress` confirmado como atendido; os trechos incompatíveis do wish `handoff-export-open-closed` são supersedidos e devem ser alinhados antes da execução.
- Event Store, webhook/status de delivery e métricas mantêm contratos públicos após commit; não será criado outbox/cache/job.
- `recent_messages` não será duplicado no JSONB: detalhe/webhook projeta no máximo cinco eventos do Event Store sob retenção/tenant já existentes e usa lista vazia quando indisponível.

## Simplicity Gate

O menor desenho completo adiciona três colunas simples: snapshot `sla_target_seconds`, `updated_at` público durável e `lifecycle_version` monotônico para CAS. Reutiliza `created_at`/`data_hora_saida`, centraliza o lifecycle no PostgreSQL e deriva duração/percentual na leitura. Não adiciona cache, job, outbox, fila, materialização, evento de provedor, coluna de duração nem campo de “dentro da meta”.

Automação de primeira resposta, origem/evidência da resposta e um objetivo percentual configurável ficam adiados. Só voltam ao desenho quando todos os provedores ativos tiverem evento outbound autenticado equivalente ou quando a resposta humana for enviada pelo próprio produto.

## Riscos fechados

- **Clique sem resposta real:** a cópia exige confirmação deliberada após iniciar o atendimento; a métrica é explicitamente “confirmada”, não “comprovada”.
- **Histórico sem meta:** não inventar dados; retornar cobertura não mensurada.
- **Mudança da meta:** snapshots impedem reclassificação retroativa.
- **Corrida/double click:** update condicional write-once; tentativa repetida não altera o timestamp.
- **Corrida confirmação/terminal:** predicado fixa status e versão observados; exatamente um vence e somente ele emite efeitos.
- **Perda de `updated_at` após restart:** coluna timezone-aware é atualizada no CAS e reconstruída pelo mapper.
- **SQLite:** regra pura roda sempre e a query temporal recebe teste PostgreSQL real.
- **Cancelamento usado para melhorar o KPI:** cancelados sem confirmação ficam fora, mas a exclusão deve aparecer em contagem auditável; motivo estruturado fica para trabalho futuro.
- **Fuso divergente:** clínica é autoridade para todas as superfícies; testes cobrem browser diferente, DST 23h/25h e fallback inválido.
- **Perda de efeitos/campos na remoção do singleton:** mapper/persistência explícitos e regressões de Event Store, webhook, delivery status e métricas.
- **Retenção nova de PII:** mensagens não são copiadas para `handoff_records`; projeção bounded reutiliza Event Store e sua retenção atual.

## Critérios essenciais

- Meta de 10 minutos: 599s e 600s dentro; 601s fora.
- Com 2 no prazo, 1 atrasado, 1 timeout vencido e 1 aberto vencido, o radar mostra 40%; aberto com 9 minutos e cancelado sem confirmação não alteram o denominador.
- Abrir WhatsApp não chama checkout nem remove o card; confirmar atendimento grava uma vez e remove da fila de espera.
- Alterar a configuração de 10 para 5 minutos não reclassifica ocorrências já criadas.
- Sem casos elegíveis, o radar mostra “sem dados”.
- Cores dos cards seguem a meta snapshot, inclusive o limite exato ainda dentro da meta.
- Reiniciar/reinstanciar o serviço não perde handoffs nem diverge entre endpoints e fila.
- PATCH não entra em `in_progress`; concorrência com duas sessões/mesma versão confirma exatamente uma vez, corrida contra terminal tem um vencedor e terceira sessão prova versão/timestamps/efeitos.
- `updated_at` permanece timezone-aware e idêntico após criar/transicionar com serviços A/B e reler com C; versão interna não aparece no OpenAPI.
- Categorias do radar são disjuntas/exaustivas, com `ROUND_HALF_UP`; PostgreSQL chama o método de produção e o gate versionado recusa variável ausente ou teste skipped.
- Radar, cards e CSV compartilham período/fuso da clínica; `in_progress` confirmado aparece no export como atendido.
- `items` e KPI usam o mesmo intervalo backend, sem filtro `ymd(new Date(...))` no frontend; mensagens recentes não ganham nova cópia persistida.
- Gate normativo `scripts/check-handoff-sla-postgres.sh` tem teste unitário e roda com `POSTGRES_SMOKE_DATABASE_URL='<admin-url-local>' ./scripts/check-handoff-sla-postgres.sh`.

## WRS

`██████████ 100/100`

Problem 20/20 | Scope 20/20 | Decisions 20/20 | Risks 20/20 | Criteria 20/20

## Resultado

O desenho cristalizado está em `DESIGN.md` e segue para revisão independente antes da criação do wish executável.
