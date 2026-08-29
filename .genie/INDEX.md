# Plans Index

## Raw

## Simmering

## Ready

- [Gatilhos de resposta por voz](brainstorms/voice-response-triggers/DESIGN.md) - WRS 100/100; duas políticas exclusivas, com `audio_input` como default seguro.
- [Templates Meta para follow-up](brainstorms/meta-templates-followup/DESIGN.md) - Infra de templates da Meta Cloud API para futuros agentes ativos de follow-up.
- [Disponibilidade honesta de provedores](brainstorms/api-provider-availability/DESIGN.md) - Só o testado aceita credencial; o resto fica visível, bloqueado e marcado `(em breve)` em vermelho.

## Poured

- [Relatório de auditoria de notas](wishes/rating-audit-report/WISH.md) - APPROVED; seis grupos, do corte do fluxo de sugestão ao relatório do painel master com facetas e acervo de opiniões.
- [Núcleo de origem confiável](wishes/interaction-origin-training-safety/WISH.md) - APPROVED; seis grupos, com Group 1 bloqueado até `billing-usage-metering` incorporar a migração 016.
- [Fallback texto](wishes/fallback-texto/WISH.md) - APPROVED; corrige credenciais de áudio Z-API mortas em produção, depois cadeia STT → multimodal → recondução por texto
- [Central de Provedores na Aba APIs](wishes/api-provider-settings/WISH.md) - DONE; todos os grupos concluídos
- [Nomenclatura única das 5 APIs](wishes/api-config-clarity/WISH.md) - APPROVED; nome curto/descrição por capacidade, alerta único por clínica derivado de readiness e tradutor de erro unificado no save
- [Métrica de usuários novos](wishes/new-users-metric/WISH.md) - APPROVED; próximo passo Group 0A fail-closed
- [Auditoria de rating RLHF](wishes/audit-rlhf-rating/WISH.md) - Rating 1-5 por resposta no simulador, análise IA assíncrona e painel master.
- [Integração de canais WhatsApp](wishes/evolution-whatsapp-integration/WISH.md) - Meta Cloud API completo, Z-API mantido e demais provedores como placeholders.
- [Integração Uazapi WhatsApp](wishes/uazapi-integration/WISH.md) - DONE; canal Uazapi já em produção e débitos de paridade rastreados em `uazapi-parity`.
