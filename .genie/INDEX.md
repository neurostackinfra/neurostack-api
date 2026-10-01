# Plans Index

## Raw

- [Ativação e organização do catálogo de APIs — guarda-chuva](brainstorms/api-provider-activation-catalog/DRAFT.md) - Decomposto em catálogo, STT, fallback Dropout e OpenAI TTS; não vira um único wish.

## Simmering

## Ready

- [Templates Meta para follow-up](brainstorms/meta-templates-followup/DESIGN.md) - Infra de templates da Meta Cloud API para futuros agentes ativos de follow-up.

## Poured

- [Retomada contextual da IA após handoff](wishes/handoff-contextual-resumption/WISH.md) - APPROVED; WRS 100/100, design e plano revisados em SHIP; execução aguarda `neurostack-core-dedomainification`.
- [Catálogo, ordem e disponibilidade pública das APIs](wishes/api-provider-catalog-presentation/WISH.md) - APPROVED; design e plano revisados em SHIP; `work` bloqueado até `stt-openrouter-scribe-activation`, `dropout-multimodal-fallback` e `tts-openai-provider` satisfazerem o gate pré-execução.
- [SLA de atendimento confirmado no handoff](wishes/handoff-sla-first-human-response/WISH.md) - IN_PROGRESS; dependência `handoff-export-open-closed` mergeada em `origin/main` (`bfa18f8`), iniciando o Grupo 1 sobre worktree isolado.
- [Extração do núcleo NeuroStack](wishes/neurostack-core-extraction/WISH.md) - IN_PROGRESS; execução e gate final SHIP concluídos, aguardando commit/PR/CI e merge autorizado; `neurostack-api` depende de `neurostack-core`, e o pacote de núcleo nunca importa a árvore `src` da API.
- [Limite de recursos empacotados do núcleo NeuroStack](wishes/neurostack-core-resource-boundary/WISH.md) - IN_PROGRESS; execução concluída com QA, qualidade e final gate em SHIP; pronto para a etapa separada de review.
- [Desdomainificação do núcleo NeuroStack](wishes/neurostack-core-dedomainification/WISH.md) - IN_PROGRESS; Grupo 1 BLOCKED até a conclusão de `neurostack-core-extraction` e `neurostack-core-resource-boundary`.
- [Auditoria humana sobre RLHF](wishes/rlhf-auditoria-humana/WISH.md) - APPROVED; oito grupos, nota sai de `events` numa troca atômica, canal herdado do trace, parecer humano append-only e saída do Roadmap.
- [Disponibilidade honesta de provedores](wishes/api-provider-availability/WISH.md) - SHIPPED; PR #22 mergeada e QA pós-merge verde com 19 pares `coming_soon`, selo laranja PT/EN e PATCH fail-closed.
- [Catálogo, ordem e disponibilidade pública das APIs](wishes/api-provider-catalog-presentation/WISH.md) - APPROVED e bloqueado; TTS/STT estão SHIPPED, e WhatsApp/Dropout aguardam evidências externas explícitas.
- [OpenAI como provider TTS](wishes/tts-openai-provider/WISH.md) - SHIPPED; PR #40 mergeada com adapter, egress e erros do upstream cobertos.
- [STT OpenRouter e ElevenLabs Scribe](wishes/stt-openrouter-scribe-activation/WISH.md) - SHIPPED via PR #43; delta público vazio e reviews independentes SHIP.
- [Dropout como fallback multimodal](wishes/dropout-multimodal-fallback/WISH.md) - DRAFT/BLOCKED; aguarda contrato autoritativo e versionado da NeuraLake para áudio OGG/Opus bruto.
- [Inbound dos providers WhatsApp](wishes/whatsapp-inbound-provider-activation/WISH.md) - DRAFT/BLOCKED; aguarda census externo agregado com zero incumbents Evolution/Uazapi sem capability digest.
- [Reparo do baseline backend](wishes/backend-test-baseline-repair/WISH.md) - SHIPPED; PR #22 mergeada, QA pós-merge e gate backend agregado verdes.
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
