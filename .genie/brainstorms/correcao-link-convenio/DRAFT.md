# Brainstorm: Correção do link de agendamento e da triagem de convênio

| Field | Value |
|-------|-------|
| **Slug** | `correcao-link-convenio` |
| **Date** | 2026-11-13 |
| **Status** | Ready |
| **Guarda-chuva** | [subagente-agendamento](../subagente-agendamento/DRAFT.md) (etapa 0, confirmada pelo owner: "1 - sim") |

## Origem

O mapeamento do código para o subagente de agendamento achou dois defeitos já em produção. O owner decidiu corrigi-los num wish próprio, antes da página de subagentes (que descreve o envio do link como comportamento de hoje).

## Fatos conferidos (origin/main c91cea30)

### Defeito 1: o link de agendamento nunca é enviado em turno real

- A tela salva o link no bucket `scheduling` com validação (`src/api/routes/config.py:705-735`, `ClinicScheduling`).
- O SRM lê `clinic_config["scheduling"]` (`packages/neurostack-core/neurostack_core/srm/engine.py:166-169`) e a pipeline também (`src/core/pipeline.py:1186`).
- O carregador de runtime `_load_config_from_db` (`src/services/event_service.py:421-485`) devolve `identity, persona, operations, business_rules, services, pricing, handoff, professionals, api_credentials, lgpd`. Não devolve `scheduling` (nem `post_care`).
- Todo caminho vivo carrega a config por `_load_clinic_config` → `_load_config_from_db` (`event_service.py:1046-1065`, `:1443`; `src/api/routes/webhook.py:3453, 4223, 5334, 6995`). O cache Redis (`neurostack:clinic_config:{tenant}`, TTL 900 s, marcador de versão 2) tem um único escritor (`event_service.py:1064`) e `build_cache_payload` só troca `api_credentials`.
- Nenhum lugar adiciona `scheduling` ao `clinic_config` depois de carregado.
- Os testes do link injetam `clinic_config` direto (`tests/core/test_pipeline.py:66-80`, `tests/srm/test_engine.py:948`), e os testes do cache trocam `_load_config_from_db` por um fake (`tests/services/test_event_service_config_cache.py:252-399`). Nenhum teste passa pelo carregador real.
- Mesmo tipo de defeito já foi corrigido para `lgpd` no commit `89c70deb`.
- `post_care` não tem leitor no runtime (só escritas em `admin.py`, `builder.py`, `repositories/tenant.py`).
- Ninguém está corrigindo: nenhum commit em qualquer ref nem worktree adiciona `"scheduling": row.scheduling`.

### Defeito 2: a pergunta "convênio ou particular?" dispara em clínica que não aceita convênio

- A tela grava `accepts_insurance` como string `"yes"`/`"no"` (`frontend/artifacts/neurostack/src/pages/clinic-config.tsx:1438, 1796`). O PATCH não revalida `business_rules` (`config.py:710-713` revalida só `scheduling` e `operations`), então o banco guarda a string.
- `src/core/pipeline.py:943` calcula `_accepts_insurance = bool(custom_vars.get("accepts_insurance"))`. `bool("no")` é verdadeiro.
- Com isso `ask_convenio_triage` liga (`pipeline.py:945-949`) e o template injeta o bloco "TRIAGEM CONVÊNIO / PARTICULAR (OBRIGATÓRIO)" (`packages/neurostack-core/neurostack_core/prompt/templates/normal.py:166-173`; texto em `presets/pt_br_clinic/prompt.toml:878-900`), que manda perguntar a modalidade e segurar qualquer valor.
- A regra correta já existe e é a fonte única declarada: `resolve_price_disclosure(custom).insurance_active` (`packages/neurostack-core/neurostack_core/prompt/price_policy.py:59-77`) ignora `"no"`, `"false"`, `"0"`, `"none"`. `event_service.py:2195-2201` também trata `"no"` certo. Só a pipeline diverge.

## Decisões

- Carregador devolve `scheduling` (como `lgpd` em `89c70deb`).
- Triagem usa `resolve_price_disclosure(custom_vars).insurance_active`.
- Sem flag nova: o rollback do defeito 1 por clínica já existe (desligar o link em `/config`).
- Sem troca de versão do cache: por até 15 min depois do deploy o cache antigo segue sem `scheduling`, que é o comportamento de hoje.

## WRS

```
WRS: ██████████ 100/100
 Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅
```
