# Plans Index

## Raw

## Simmering

## Ready

- [Templates Meta para follow-up](brainstorms/meta-templates-followup/DESIGN.md) - Infra de templates da Meta Cloud API para futuros agentes ativos de follow-up.

## Poured

- [Auditoria humana sobre RLHF](wishes/rlhf-auditoria-humana/WISH.md) - APPROVED; oito grupos, nota sai de `events` numa troca atômica, canal herdado do trace, parecer humano append-only e saída do Roadmap.
- [Disponibilidade honesta de provedores](wishes/api-provider-availability/WISH.md) - IN_PROGRESS; execução SHIP com 19 pares `coming_soon`, selo laranja PT/EN e PATCH fail-closed, aguardando PR/merge/QA.
- [Reparo do baseline backend](wishes/backend-test-baseline-repair/WISH.md) - IN_PROGRESS; seis grupos e gate agregado SHIP, aguardando PR/merge/QA.
- [Filtros de safety flag e intenção na fila de handoff](wishes/filtros-safety-intencao-handoff/WISH.md) - IN_PROGRESS; Grupo 0B aprovado corrige lifecycle de auth/RLHF antes dos filtros funcionais.
- [Central de clonagem de clínicas](wishes/central-clonagem-clinicas/WISH.md) - APPROVED; cinco grupos para réplicas inativas com configuração/conhecimento, sem APIs, link ativo ou administradores.
- [Cinco intenções comerciais no painel](wishes/cinco-intencoes-painel/WISH.md) - APPROVED; colapso só na camada de painel, 29 intenções cruas em 6 buckets, backend intocado e trava de drift preservada.
- [Gerenciamento de senha de acesso](wishes/access-password-management/WISH.md) - APPROVED; autoatendimento de senha para builder/clínica e reset delegado somente para clínicas vinculadas, com master preservado.
- [Gatilhos de resposta por voz](wishes/voice-response-triggers/WISH.md) - APPROVED; switch mestre preserva credenciais e enum exclusivo usa `audio_input` como default de menor custo.
- [Fila de Inativos](wishes/fila-inativos/WISH.md) - IN_PROGRESS; execução aprovada em SHIP, aguardando QA visual pós-merge.
- [Relatório de auditoria de notas](wishes/rating-audit-report/WISH.md) - APPROVED; seis grupos, do corte do fluxo de sugestão ao relatório do painel master com facetas e acervo de opiniões.
- [Núcleo de origem confiável](wishes/interaction-origin-training-safety/WISH.md) - APPROVED; seis grupos, com Group 1 bloqueado até `billing-usage-metering` incorporar a migração 016.
- [Fallback texto](wishes/fallback-texto/WISH.md) - APPROVED; corrige credenciais de áudio Z-API mortas em produção, depois cadeia STT → multimodal → recondução por texto
- [Central de Provedores na Aba APIs](wishes/api-provider-settings/WISH.md) - DONE; todos os grupos concluídos
- [Nomenclatura única das 5 APIs](wishes/api-config-clarity/WISH.md) - APPROVED; nome curto/descrição por capacidade, alerta único por clínica derivado de readiness e tradutor de erro unificado no save
- [Métrica de usuários novos](wishes/new-users-metric/WISH.md) - APPROVED; próximo passo Group 0A fail-closed
- [Auditoria de rating RLHF](wishes/audit-rlhf-rating/WISH.md) - Rating 1-5 por resposta no simulador, análise IA assíncrona e painel master.
- [Integração de canais WhatsApp](wishes/evolution-whatsapp-integration/WISH.md) - Meta Cloud API completo, Z-API mantido e demais provedores como placeholders.
- [Integração Uazapi WhatsApp](wishes/uazapi-integration/WISH.md) - DONE; canal Uazapi já em produção e débitos de paridade rastreados em `uazapi-parity`.
