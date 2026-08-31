# Plans Index

## Raw

## Simmering

- [Reparo do baseline da suíte backend](wishes/backend-test-baseline-repair/WISH.md) - IN_PROGRESS; plano ampliado e revisado inclui scheduling gate herdado com contrato de cinco valores.

## Ready

- [FallBack LLM Multimodal](brainstorms/fallback-llm-multimodal/DESIGN.md) - WRS 100/100; uma única contingência `multimodal_*` cobre falha do LLM de texto e falha do transcritor de áudio.
- [Templates Meta para follow-up](brainstorms/meta-templates-followup/DESIGN.md) - Infra de templates da Meta Cloud API para futuros agentes ativos de follow-up.

## Poured

- [Contrato visual da lista de traces](wishes/trace-list-presentation-contract/WISH.md) - IN_PROGRESS; implementação completa, aguardando gate agregado após correção da falha preexistente de locale em API settings.
- [Fila de Inativos](wishes/fila-inativos/WISH.md) - APPROVED; fila tenant-safe da sessão efetiva em handoff permanente, com retomada manual da IA e estados visuais referenciados.

- [Cards de contexto do trace](wishes/trace-context-cards/WISH.md) - APPROVED; cards responsivos por fonte, `config` canônico com alias legado e fallback neutro localizado.
- [Gatilhos de resposta por voz](wishes/voice-response-triggers/WISH.md) - BLOCKED; os 16 caminhos estão em SHIP, mas o `pytest` completo obrigatório reproduz failures/hang preexistentes na base e exige decisão sobre correção upstream ou waiver.
- [Disponibilidade honesta de provedores](wishes/api-provider-availability/WISH.md) - APPROVED; 19 opções visíveis, com seis homologadas e 13 bloqueadas pelo selo pequeno laranja `EM BREVE` / `COMING SOON`.
- [Relatório de auditoria de notas](wishes/rating-audit-report/WISH.md) - APPROVED; seis grupos, do corte do fluxo de sugestão ao relatório do painel master com facetas e acervo de opiniões.
- [Núcleo de origem confiável](wishes/interaction-origin-training-safety/WISH.md) - APPROVED; seis grupos, com Group 1 bloqueado até `billing-usage-metering` incorporar a migração 016.
- [Fallback texto](wishes/fallback-texto/WISH.md) - APPROVED; corrige credenciais de áudio Z-API mortas em produção, depois cadeia STT → multimodal → recondução por texto
- [Central de Provedores na Aba APIs](wishes/api-provider-settings/WISH.md) - DONE; todos os grupos concluídos
- [Nomenclatura única das 5 APIs](wishes/api-config-clarity/WISH.md) - APPROVED; nome curto/descrição por capacidade, alerta único por clínica derivado de readiness e tradutor de erro unificado no save
- [Métrica de usuários novos](wishes/new-users-metric/WISH.md) - APPROVED; próximo passo Group 0A fail-closed
- [Auditoria de rating RLHF](wishes/audit-rlhf-rating/WISH.md) - Rating 1-5 por resposta no simulador, análise IA assíncrona e painel master.
- [Integração de canais WhatsApp](wishes/evolution-whatsapp-integration/WISH.md) - Meta Cloud API completo, Z-API mantido e demais provedores como placeholders.
- [Integração Uazapi WhatsApp](wishes/uazapi-integration/WISH.md) - DONE; canal Uazapi já em produção e débitos de paridade rastreados em `uazapi-parity`.
