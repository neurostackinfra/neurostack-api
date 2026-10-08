# Wish: Catálogo e armazenamento de Mídias

| Field | Value |
|-------|-------|
| **Status** | IN_PROGRESS |
| **Slug** | `media-assets-catalog` |
| **Date** | 2026-10-07 |
| **Author** | Codex |
| **Appetite** | large |
| **Branch** | `wish/media-assets-catalog` |
| **Repos touched** | `neurostack-api` |
| **Design** | [DESIGN.md](../../brainstorms/media-assets-catalog/DESIGN.md) |

## Summary

A Central de Mídias hoje é um mock sem persistência, ownership ou consumo real. Este wish entrega um catálogo tenant-scoped para MP4, MP3, PDF, JPEG e PNG, com bytes em PostgreSQL, CRUD/download seguro, billing verdadeiro, frontend funcional e proteções operacionais; ele também cria a fundação relacional que o wish dependente de clonagem seletiva consumirá.

## Scope

### IN

- Atualizar FastAPI e `python-multipart` para os ranges seguros aprovados, regenerar o lock e executar uma asserção de dependências que rejeita Starlette abaixo de `1.0.1` antes de registrar multipart.
- Criptografar backups PostgreSQL em streaming com `age`, sem dump plaintext durável, validar restauração com identidade isolada e inventariar backups legados sem apagá-los.
- Criar migration/modelo `media_assets` tenant-scoped, com payload `BYTEA`, metadados de tamanho estrito e invariantes relacionais de tamanho, hash e tipo/MIME.
- Extrair o predicado role-based de clínica real usado pelo admin para um helper compartilhado, sem mudar sua semântica nas métricas administrativas; para Mídias, exigir adicionalmente tenant ativo e `plan_type != 'admin'`.
- Implementar cap ASGI pré-parser, uma parte de arquivo e somente campos conhecidos, validação fechada de cinco formatos, limites de concorrência e teto operacional agregado de 500.000.000 bytes sob lock transacional.
- Implementar upload, lista keyset, download attachment, PATCH e DELETE com resolução explícita da clínica efetiva, isolamento cross-tenant, erros estáveis e transações completas.
- Substituir o mock da Central de Mídias por catálogo PT/EN e alinhar rota/navegação aos papéis autorizados.
- Substituir o zero fixo de billing por `SUM(media_assets.size_bytes)` nas clínicas do Builder.
- Entregar preflight de release e evidência operacional durável, sem afirmar que medições externas já passaram.

### OUT

- Integrar Mídias à clonagem; `ajuste-central-clonagem` depende deste wish e fará a cópia relacional.
- Enviar mídia manual ou automaticamente por canais ou executar `usage_trigger`/`auto_suggest`; ambos são apenas metadados de catálogo nesta entrega.
- Transcodificar, gerar thumbnail, executar OCR ou antivírus externo.
- Adicionar object storage, CDN, URLs assinadas, fila, deduplicação, versionamento ou lixeira.
- Alterar preços, créditos ou oferecer planos/cotas comerciais dinâmicos.
- Apagar automaticamente backups legados encontrados; inventário e runbook preservam os artefatos e exigem autorização humana por alvo para futura remediação.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Persistir bytes em PostgreSQL `BYTEA`. | É o menor mecanismo transacional para os limites atuais e evita coordenação banco/object storage. |
| 2 | Limitar arquivo a 50.000.000 bytes, request multipart a 51.000.000 e armazenamento operacional por proprietário a 500.000.000. | Limites de produto por si só não protegem parser, RAM, WAL, banco e backups. |
| 3 | Todo endpoint primeiro chama `resolve_effective_tenant`, depois consulta o predicado canônico de clínica real com os filtros adicionais de ativo/não-admin, e somente então acessa `media_assets`. | Fecha a ordem de autorização e reutiliza a classificação administrativa sem divergência. |
| 4 | Lista usa keyset `(created_at DESC, id DESC)` com cursor opaco tenant-bound. | Evita drift de offset e fecha semântica para inserção/exclusão concorrente. |
| 5 | Tipo/MIME são derivados do conteúdo, mas o arquivo continua não confiável. | Signature sniffing reduz spoofing sem prometer segurança antimalware. |
| 6 | Billing e teto operacional derivam de `SUM(size_bytes)`; uma única guarda transacional em duas fases usa advisory lock antes de qualquer medição de uso/projeção. | Mantém uma fonte de verdade, suporta upload com tamanho conhecido e clone com tamanho inicialmente desconhecido e impede corrida entre réplicas. |
| 7 | Backup passa a ser `pg_dump | gzip | age`, com chave privada externa. | Mídias autorizadas podem ser sensíveis e não podem entrar em dumps plaintext duráveis. |
| 8 | Clonagem não ganha helper antecipado neste wish. | O consumidor será implementado no wish dependente, evitando código morto. |
| 9 | O release é bloqueado por pré-requisito, evidência, skip crítico ou capacidade inconclusiva. | Segurança e recuperabilidade não podem depender de waiver informal. |
| 10 | A aplicação admite no máximo quatro uploads em parsing/persistência e oito downloads ativos por processo; configuração pode apenas reduzir cada limite para inteiro positivo. | Fixa orçamento de recursos, torna saturação previsível e impede aumento operacional não revisado. |
| 11 | Retenção automática reconhece exclusivamente `postgres_*.sql.gz.age`. | Impede que dumps plaintext e arquivos legados não relacionados sejam apagados pelo novo fluxo. |

## Simplicity Case

- **Simplest complete design:** uma tabela `media_assets` tenant-scoped com `BYTEA`, cinco endpoints, catálogo frontend e billing por soma.
- **Added machinery:** cap pré-parser, semáforos com tetos fixos 4/8, guarda transacional em duas fases, invariantes, classificador compartilhado e backup criptografado respondem a riscos presentes; keyset limita carregamento e drift.
- **Deferred until measured:** object storage/CDN permanece fora até que o gate de capacidade abaixo dispare; processamento assíncrono espera evidência de timeout recorrente; scan externo espera requisito de compliance.
- **Complexity removed:** nenhum backend configurável de storage, URL assinada, outbox, retry persistente, contador duplicado, dedupe, transcodificação, versionamento, estado de delivery ou helper de clone sem consumidor.

## Dependencies

**depends-on:** none

**blocks:** ajuste-central-clonagem

**clone dependency contract:** `ajuste-central-clonagem` só pode iniciar depois deste wish passar execução/review. Ele consumirá `media_storage_capacity_guard` diretamente: entra na guarda sem `additional_bytes`, estabiliza por completo o conjunto determinístico de IDs de mídia da origem com `FOR SHARE` e sem payload, mede o total enquanto a guarda permanece aberta, chama `guard.validate_projection(total_bytes)` uma única vez e somente então copia os payloads. Não pode consultar tamanho/uso antes da guarda, derivar owner/chave, readquirir locks nem recalcular o cap fora dessa API.

## Validation and Escalation Policy

Esta regra vale para todos os oito grupos e para o gate agregado: qualquer comando não-zero, pré-requisito ou evidência ausente, skip/xfailed inesperado de cenário crítico, scan inconclusivo, ou falha por budget/timeout bloqueia o grupo. Não há waiver, downgrade de critério nem `|| true`. O executor registra comando, ambiente e saída, mantém o grupo em progresso e escala para trace, correção e re-review. Evidência externa de produção que não esteja disponível resulta em **BLOCKED**, nunca skip.

## Success Criteria

- [ ] `pyproject.toml` e `requirements.txt` declaram FastAPI `>=0.142.4,<0.143.0` e `python-multipart>=0.0.32,<0.1.0`, sem pin direto de Starlette; locks registram a resolução e `scripts/check_media_dependencies.py`, coberto por teste, falha a menos que manifests, ambiente resolvido e ranges coincidam e Starlette seja `>=1.0.1`.
- [ ] O backup falha antes de `pg_dump` sem `age`, `age-keygen` ou recipient público; o teste gera recipient/identity isolados, nunca cria dump plaintext durável, publica somente `.age` após pipeline completo e restaura em banco descartável.
- [ ] A retenção/exclusão automática de produção considera exclusivamente basenames que casam exatamente `postgres_*.sql.gz.age`; testes plantam arquivos envelhecidos `.sql.gz`, `*_data_*.tar.gz`, nomes `.age` fora do padrão e diretórios/paths semelhantes e provam que nunca são elegíveis nem removidos.
- [ ] `.env.example` documenta somente configurações não secretas/placeholders seguros; `docs/evidence/media-legacy-backup-inventory.md` registra localização, owner, tamanho, data e ação de cada legado sem removê-lo.
- [ ] A migration cria ownership, índice keyset e checks de `octet_length(payload)=size_bytes`, `0<size_bytes<=50000000`, SHA-256 de 32 bytes e pares categoria/MIME fechados.
- [ ] `display_name` tem 1–200 caracteres, `original_filename` 1–255, `usage_trigger` é opcional com no máximo 500, `auto_suggest` omitido assume `false` e, quando presente no multipart, aceita somente os bytes ASCII lowercase `true` ou `false`; qualquer outra grafia/valor falha. NUL/CR/LF são rejeitados nos campos textuais, e upload, lista, PATCH e UI preservam exatamente o roundtrip dos quatro campos sem executá-los no runtime.
- [ ] Multipart aceita exatamente uma parte de arquivo e apenas os campos conhecidos; arquivo duplicado, parte desconhecida, tipo, assinatura, mismatch ou metadado inválido falha com o código estável, sem linha parcial.
- [ ] Body cap rejeita `Content-Length` e chunked acima de 51.000.000 antes do parser; arquivo com 50.000.001 bytes falha sem registro parcial. `MEDIA_MAX_CONCURRENT_UPLOADS` tem default/máximo 4 e `MEDIA_MAX_CONCURRENT_DOWNLOADS` tem default/máximo 8; variável ausente usa seu default, configuração explícita aceita somente inteiro positivo menor ou igual ao máximo, e zero, negativo, não inteiro ou valor maior falha fechado no startup.
- [ ] Saturação de upload ou download retorna imediatamente 503 com código `media_concurrency_exceeded`, sem fila; permits são liberados em sucesso, erro de validação, erro de banco, cancelamento da task e, no download, desconexão do cliente.
- [ ] O helper `src/services/tenant_classification.py` contém o predicado role-based exato antes privado do admin; `src/api/routes/admin.py` passa a reutilizá-lo sem mudar totais/listagem, enquanto Mídias acrescenta `Tenant.is_active IS TRUE` e `Tenant.plan_type != 'admin'`.
- [ ] Em cada um dos cinco endpoints, a primeira operação tenant-aware é `resolve_effective_tenant`; em seguida uma query valida o ID efetivo com o predicado canônico e filtros adicionais, antes de qualquer consulta a `media_assets`.
- [ ] Testes aceitam clone de clínica sem usuário e tenant com usuário de papel clínico; rejeitam tenant somente admin, somente builder, somente investor, inativo e `plan_type='admin'`; negam override não pertencente e provam que as métricas admin permanecem idênticas.
- [ ] Cross-tenant e ID inexistente retornam o mesmo 404; lista/PATCH não carregam nem devolvem payload; paginação usa ordem descendente, default 25/máximo 100 e cursor tenant-bound sem duplicatas.
- [ ] Download devolve bytes/MIME exatos como attachment com `nosniff`, fallback ASCII e `filename*` seguro; upload, PATCH e DELETE fazem rollback completo em falha.
- [ ] Logs de todos os caminhos de Mídias excluem bytes, `display_name`, `original_filename`, `usage_trigger` e hashes completos; registram somente IDs, categoria, tamanho e resultado aprovados.
- [ ] `src/services/media_assets.py` expõe uma única guarda/contexto transacional reutilizável `media_storage_capacity_guard` que resolve owner (`builder_id`, senão `tenant_id`) e, ao entrar, adquire na ordem canônica User → Builder opcional → advisory do owner → Tenant revalidado/travado, sem consultar tamanho de fonte nem `SUM(media_assets.size_bytes)`; enquanto a guarda está aberta, o caller pode estabilizar e medir seu conjunto-fonte e então chama uma única vez `guard.validate_projection(additional_bytes)`, que soma o uso protegido e aplica `MEDIA_STORAGE_CAP_BYTES = 500_000_000` antes de qualquer insert. Upload usa a mesma guarda e valida imediatamente seu tamanho já conhecido; o wish posterior de clone a usa com tamanho inicialmente desconhecido, sem reimplementar owner/chave/soma/cap e sem helper de clone antecipado aqui.
- [ ] A chave é exatamente o inteiro PostgreSQL `bigint` assinado formado pelos oito primeiros bytes big-endian de `SHA-256(b"media-storage-owner:v1\0" + owner_uuid.bytes)`: interpretar como unsigned e subtrair `2**64` quando o valor for `>=2**63`; a aquisição é `pg_advisory_xact_lock(key)`.
- [ ] Toda mutação que consome capacidade segue, na mesma transação, a ordem de locks `User` → `Builder` quando existir → advisory do owner → `Tenant` revalidado/travado → estabilização/medição da fonte pelo caller → soma de uso e validação da projeção pela guarda → insert em `media_assets`; nó inexistente, como User para API key ou Builder para clínica independente, é pulado sem reordenar. Testes PostgreSQL de duas sessões provam chave/ordem iguais, ausência de deadlock e cap serializado.
- [ ] O teste PostgreSQL usa um consumidor clone-compatible cujo `additional_bytes` só existe após consumir por completo um conjunto determinístico de IDs `FOR SHARE` sem payload; instrumentação prova que nenhuma query de tamanho da fonte ou uso de destino ocorre antes de a guarda adquirir User/Builder/advisory/Tenant, que o uso é consultado somente por `validate_projection`, e que falha/cancelamento faz rollback e libera todos os locks.
- [ ] Uploads concorrentes entre sessões/réplicas não excedem 500.000.000 bytes por Builder ou clínica independente; `storage.media.used_bytes` soma todas as clínicas do Builder e DELETE reduz base ativa, cap e billing.
- [ ] A rota/nav ativa Mídias para `clinic_admin` no próprio tenant e para Builder/super-admin com clínica selecionada; investor é negado e não vê a navegação; o rótulo de roadmap é removido.
- [ ] A UI permite upload, lista paginada, download, edição e exclusão em PT/EN, envia `viewingClinicId` para Builder/super-admin, usa o tenant próprio para clinic-admin e aborta/limpa estado ao trocar de clínica.
- [ ] `scripts/check_media_release.py` valida pré-requisitos, chama a asserção de dependências, executa e verifica `pip-audit --local`, roda testes focados com JUnit, rejeita skips críticos e escaneia arquivos rastreados e evidências/logs por plaintext e dados sensíveis de mídia; testes plantam falha de execução e achado do audit e exigem bloqueio.
- [ ] `docs/evidence/media-release-evidence.md` registra comandos, owner, ambiente, aprovações, criptografia de DB/WAL/backup, custódia externa/retention da chave age, owners atuais/headroom e deltas/durações medidos com fixture de 500 MB; ausência de prova externa bloqueia release.
- [ ] O rollout é **no-go** e cria um wish de object storage se a projeção do cap de 500 MB para os owners atuais levar DB ou destino de backup a `>=70%` da capacidade alocada, se a fixture levar DB/WAL/backup a `>=70%`, ou se backup ou restore da fixture exceder 30 minutos.
- [ ] Testes PostgreSQL de Mídias falham, sem skip, quando `POSTGRES_SMOKE_DATABASE_URL` está ausente; o gate agregado passa sem skip crítico e sem alegar que evidência operacional não coletada foi aprovada.

## Execution Strategy

### Wave 1 (parallel safety foundations)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 2 — dependência compartilhada e asserção executável | engineer-standard / high | Atualizar framework/parser, lock, advisories e verificador de dependências. |
| 2 | engineer | 3 — backup/restore stateful e operacional | engineer-standard / high | Criptografar backup, restaurar com identidade isolada e inventariar legado. |

### Wave 2 (sequential backend)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 3 | engineer | 3 — schema e domínios canônicos compartilhados | engineer-standard / high | Criar modelo/validadores, classificação de clínica e API única de capacidade/lock. |
| 4 | engineer | 3 — upload protegido e limites stateful | engineer-standard / high | Registrar app/middleware/router e entregar POST autorizado usando a API de capacidade. |
| 5 | engineer | 3 — CRUD/auth/keyset/download | engineer-standard / high | Entregar autorização canônica, lista, download, PATCH e DELETE. |
| 6 | engineer | 2 — billing e contrato público final | engineer-standard / high | Integrar billing e fechar OpenAPI/regressões ASGI do router já registrado. |

### Wave 3 (UI and aggregate)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 7 | engineer | 2 — estado assíncrono e navegação por papel | engineer-standard / high | Substituir mock, alinhar nav/roles e testar troca de clínica. |
| 8 | engineer | 4 — coordenação, capacidade e gate integrado | engineer-complex / high | Executar preflight, produzir evidência mensurável e decidir rollout. |

Cada grupo é revisado independentemente e só libera o dependente após sua validação e re-review; o total planejado é de oito grupos.

## Execution Groups

### Group 1: Runtime multipart seguro e asserção de dependências

**Goal:** Atualizar o stack HTTP/parser e provar por asserção executável que manifests e ambiente resolvido são seguros antes do upload.

**Deliverables:**
1. Atualizar `pyproject.toml`, `requirements.txt`, `requirements-dev.txt` e `poetry.lock` para os ranges aprovados, sem pin direto de Starlette.
2. Criar `scripts/check_media_dependencies.py` e `tests/scripts/test_check_media_dependencies.py` com casos de sucesso e falha para ranges de FastAPI/python-multipart e Starlette resolvida `>=1.0.1`.
3. Manter `pip-audit` fixado no ambiente de desenvolvimento e executar smoke de startup/OpenAPI/health; a automação prometida é o comando de release, pois este plano não altera arquivo de CI.
4. Sob autorização excepcional de execução, migrar somente a fixture local de `tests/services/test_multimodal_transcription.py` para semântica pytest-asyncio 1.x, sem introduzir fixture global de compatibilidade nem alterar o comportamento testado.

**Acceptance Criteria:**
- [ ] O verificador retorna não-zero para manifest fora do range, pacote ausente, ambiente divergente ou Starlette `<1.0.1`; imprimir versões não satisfaz o critério.
- [ ] Advisory scan falha fechado se não executar ou encontrar vulnerabilidade aplicável.
- [ ] Os 47 testes de transcrição multimodal passam com pytest auditado `>=9.0.3` e pytest-asyncio 1.x, sem depender da fixture removida `event_loop` nem contaminar o loop de outros módulos.
- [ ] Suite, qualidade e smokes passam após o salto de framework.

**Validation:**
```bash
poetry install --sync && poetry run pytest tests/scripts/test_check_media_dependencies.py -v && poetry run python scripts/check_media_dependencies.py && poetry run pip-audit --local && poetry run pytest tests/api/test_health.py tests/api/test_middleware_auth.py -v && make quality
```

**depends-on:** none

**blocks:** 3, 4

---

### Group 2: Backup criptografado, configuração e legado inventariado

**Goal:** Impedir novos backups plaintext, provar restore isolado e preservar um inventário operacional dos legados.

**Deliverables:**
1. Alterar `scripts/backup.sh` e criar `scripts/restore_backup.sh` com preflight de `age`/`age-keygen`, temp cifrado restritivo, pipeline com falha propagada, rename atômico e retenção que seleciona somente o basename exato `postgres_*.sql.gz.age`.
2. Cobrir backup/restore com recipient e identity gerados exclusivamente para o teste, banco descartável e limpeza dos temporários.
3. Adicionar `.env.example`, `docs/media-backup-rollout.md` e o artefato durável `docs/evidence/media-legacy-backup-inventory.md`; o inventário nunca exclui backups legados.

**Acceptance Criteria:**
- [ ] Falta de `age`, `age-keygen` ou recipient encerra antes de `pg_dump`; sucesso deixa somente `.age` com permissão restrita.
- [ ] Falha em qualquer estágio não publica backup nem deixa plaintext; logs não expõem recipient, identity, chave ou conteúdo.
- [ ] Teste restaura em PostgreSQL descartável e valida dados sentinela usando material age isolado.
- [ ] Testes de retenção usam arquivos envelhecidos e provam que somente `postgres_*.sql.gz.age` é elegível; `.sql.gz`, `*_data_*.tar.gz`, `.age` fora do padrão, diretórios e nomes semelhantes permanecem intocados.
- [ ] `tests/scripts/test_backup_encryption.py` falha explicitamente, nunca skip, quando `POSTGRES_SMOKE_DATABASE_URL` está ausente; a validação também interrompe antes das ferramentas quando a variável não existe.
- [ ] Inventário registra cada legado e sua ação recomendada sem removê-lo.

**Validation:**
```bash
test -n "$POSTGRES_SMOKE_DATABASE_URL" && command -v age && command -v age-keygen && bash -n scripts/backup.sh scripts/restore_backup.sh && poetry run pytest tests/scripts/test_backup_encryption.py -v && make quality
```

**depends-on:** none

**blocks:** 8

---

### Group 3: Persistência, metadados e classificação canônica

**Goal:** Criar a fonte relacional autoconsistente, uma classificação role-based de clínica real e a única guarda transacional de capacidade em duas fases sem alterar o admin.

**Deliverables:**
1. Adicionar `MediaAsset`, relacionamento e migration com FK, índice keyset, checks aprovados e os quatro metadados exatos.
2. Implementar leitura limitada, hash, classificação dos cinco formatos e validação central de comprimento, booleano estrito e NUL/CR/LF.
3. Mover sem alteração semântica o predicado `_real_clinic_tenant_filter` de `src/api/routes/admin.py` para `src/services/tenant_classification.py`, reutilizá-lo no admin e testar equivalência de totais/listagem.
4. Criar em `src/services/media_assets.py` a guarda/contexto `media_storage_capacity_guard(session, actor_user, subject_tenant)`: `__aenter__` resolve owner e adquire User → Builder opcional → advisory → Tenant revalidado/travado, sem query de tamanho/uso; seu único método de projeção `validate_projection(additional_bytes)` aceita inteiro não negativo, consulta o uso protegido uma vez, aplica o cap e retorna owner/uso/projeção, sem inserir nem conhecer rota ou clone.
5. Fazer `tests/integration/test_media_assets_postgres.py` falhar explicitamente quando `POSTGRES_SMOKE_DATABASE_URL` não existe e cobrir a guarda em PostgreSQL real, incluindo consumidor clone-compatible cujo tamanho é desconhecido até estabilizar integralmente a fonte.

**Acceptance Criteria:**
- [ ] Migration sobe/desce e o banco rejeita payload/tamanho, hash, tipo/MIME e metadados inconsistentes.
- [ ] Validadores impõem `display_name` 1–200, `original_filename` 1–255, `usage_trigger` opcional ≤500, `auto_suggest` omitido como `false`, wire estritamente lowercase `true|false` e rejeição de qualquer outro valor e de NUL/CR/LF.
- [ ] Classificador aceita tenant sem usuário e com papel clínico; rejeita somente admin/builder/investor; os testes admin existentes e novos provam métricas inalteradas.
- [ ] A guarda resolve owner sem medir mídia e adquire canonicamente User `FOR KEY SHARE` quando houver, Builder `FOR UPDATE` quando houver, `pg_advisory_xact_lock` pela chave v1 exata e Tenant `FOR UPDATE` revalidando o mesmo owner/target; mudança entre resolução e revalidação falha/rollback. Nenhuma query de tamanho de fonte ou `SUM(media_assets.size_bytes)` ocorre antes dessa sequência terminar.
- [ ] Testes unitários fixam vetores UUID→bigint da derivação SHA-256; testes PostgreSQL instrumentam a ordem e provam que um consumidor upload-compatible com tamanho conhecido e um consumidor clone-compatible com tamanho inicialmente desconhecido usam a mesma guarda/chave, terminam sem deadlock e não excedem 500.000.000; o endpoint real fica para o Grupo 4.
- [ ] O consumidor clone-compatible entra na guarda sem conhecer bytes, consome integralmente IDs determinísticos `FOR SHARE` sem payload, só então mede o total e chama `validate_projection`; o teste falha se tamanho de fonte/uso for consultado antes da guarda, se uso for consultado fora do método ou se insert ocorrer antes da validação.
- [ ] Integração PostgreSQL ausente falha em vez de pular.

**Validation:**
```bash
test -n "$POSTGRES_SMOKE_DATABASE_URL" && poetry run pytest tests/services/test_media_assets.py tests/services/test_tenant_classification.py tests/api/test_admin.py tests/integration/test_media_assets_postgres.py -v && make quality
```

**depends-on:** 1

**blocks:** 4, 5

---

### Group 4: Upload protegido e fundação de limites

**Goal:** Tornar o POST multipart independentemente utilizável pela aplicação real sob autorização e limites completos.

**Deliverables:**
1. Adicionar cap ASGI para `Content-Length` e chunked e primitivos de concorrência compartilhados; o Grupo 4 é proprietário da criação de `src/config/settings.py`, `src/api/middleware/media_limits.py` e `tests/config/test_media_settings.py`. `MEDIA_MAX_CONCURRENT_UPLOADS` fixa default/máximo 4 e `MEDIA_MAX_CONCURRENT_DOWNLOADS` fixa default/máximo 8; variável ausente usa o default, somente inteiro positivo até o máximo pode reduzi-lo e qualquer valor inválido falha no startup. O upload adquire permit imediatamente antes do primeiro byte de parsing e o mantém até commit/rollback completo.
2. Limitar o parser a uma parte de arquivo mais os quatro campos conhecidos; omissão de `auto_suggest` produz `false` e o wire aceita somente lowercase `true|false`.
3. Registrar o middleware e o router com o POST em `src/api/main.py`, de modo que startup/OpenAPI e cliente da aplicação real exercitem o endpoint neste grupo.
4. No POST, chamar `resolve_effective_tenant`, consultar o predicado canônico com `is_active=true`/`plan_type!='admin'` antes de mídia, validar assinatura/MIME/metadados, entrar em `media_storage_capacity_guard` e chamar imediatamente `validate_projection(size_bytes)` antes do insert.
5. Garantir transação/rollback, erro imediato 503 `media_concurrency_exceeded`, liberação do permit em sucesso, validação, erro de banco e cancelamento, além de logs restritos a IDs, categoria, tamanho e resultado.

**Acceptance Criteria:**
- [ ] Request >51.000.000, arquivo >50.000.000, segunda parte de arquivo e campo desconhecido falham sem parser/persistência indevidos.
- [ ] Exatamente quatro uploads podem permanecer ativos em parsing/persistência por default; o quinto recebe imediatamente 503 `media_concurrency_exceeded` sem consumir body. Configuração 1–4 reduz o limite; ausente/default preserva 4, e zero, negativo, não inteiro ou >4 impede startup.
- [ ] Testes mantêm quatro requests abertas e cobrem liberação do permit após sucesso, erro de validação, erro de banco e cancelamento, provando que uma nova request entra em cada caso.
- [ ] O POST é alcançável no app real e o teste prova a ordem resolver tenant → classificar alvo → entrar na guarda → validar projeção → insert; caller/target proibido recebe 403 antes de consulta a `media_assets`.
- [ ] Os cinco formatos válidos persistem bytes/tipo/MIME/tamanho/hash e os quatro metadados exatos; `auto_suggest` omitido retorna `false`, somente `true|false` lowercase é aceito, e assinatura/mismatch/booleano inválido falha sem linha parcial.
- [ ] Corrida entre sessões usa a chave/ordem da API compartilhada, respeita cap agregado e termina sem deadlock; falhas fazem rollback.
- [ ] Captura de logs prova ausência de bytes, nomes autorais, gatilho e hash completo.

**Validation:**
```bash
test -n "$POSTGRES_SMOKE_DATABASE_URL" && poetry run pytest tests/config/test_media_settings.py tests/api/test_media_assets_upload.py tests/services/test_media_assets.py tests/integration/test_media_assets_postgres.py -v && make quality
```

**depends-on:** 1, 3

**blocks:** 5

---

### Group 5: Catálogo CRUD, autorização, keyset e download

**Goal:** Estender o router POST já registrado com os quatro endpoints restantes, isolamento canônico, metadados roundtrip, paginação e download seguro.

**Deliverables:**
1. Preservar o POST do Grupo 4 e acrescentar GET lista, GET content, PATCH e DELETE ao mesmo router registrado.
2. Nos quatro novos endpoints, resolver tenant efetivo primeiro, consultar o helper canônico com `is_active=true` e `plan_type!='admin'` depois, e só então acessar mídia; o Grupo 5 consome, sem redefinir, settings/primitivos criados no Grupo 4 e é proprietário da integração de download no router/testes. Download adquire sem espera um permit do limitador compartilhado imediatamente antes de abrir/streamar payload e o mantém até terminar ou desconectar.
3. Implementar lista keyset tenant-bound, download attachment, PATCH dos quatro metadados e DELETE definitivo com lookup `id AND tenant_id`.
4. Cobrir matriz completa de caller/target, incluindo clone sem usuário, papéis exclusivos, inativo, clinic user, override não pertencente e cross-tenant indistinguível.

**Acceptance Criteria:**
- [ ] Instrumentação de query prova a ordem resolver → classificar alvo → acessar `media_assets` para POST/list/content/PATCH/DELETE.
- [ ] Exatamente oito downloads podem permanecer ativos por default; o nono recebe imediatamente 503 `media_concurrency_exceeded`. Configuração 1–8 reduz o limite; zero, negativo, não inteiro ou >8 impede startup, e testes provam liberação após sucesso, erro de banco, cancelamento e disconnect do cliente.
- [ ] Alvo userless/clínico ativo não-admin é aceito; admin-only, builder-only, investor-only, inativo, plan admin e override não pertencente recebem 403 antes de mídia.
- [ ] Lista/PATCH/UI-contract preservam os quatro metadados, sem selecionar payload nem executar os campos no runtime.
- [ ] Keyset, headers de download, 404 indistinguível, transações e redaction de logs obedecem ao contrato.

**Validation:**
```bash
test -n "$POSTGRES_SMOKE_DATABASE_URL" && poetry run pytest tests/api/test_media_assets.py tests/api/test_media_assets_authorization.py tests/integration/test_media_assets_postgres.py -v && make quality
```

**depends-on:** 3, 4

**blocks:** 6, 7

---

### Group 6: Billing, contrato OpenAPI final e regressão

**Goal:** Integrar o router já ativo ao billing e fechar o contrato OpenAPI/regressões dos cinco endpoints.

**Deliverables:**
1. Substituir `storage.media.used_bytes` fixo por soma de `media_assets.size_bytes` em todas as clínicas pertencentes ao Builder.
2. Completar e validar a documentação dos cinco endpoints, multipart fechado, query `tenant_id`, schemas/erros e segurança corretos no OpenAPI, sem postergar registro de middleware/router para este grupo.
3. Testar upload/delete contra billing e smokes de startup/health/OpenAPI após o upgrade.

**Acceptance Criteria:**
- [ ] Billing soma exatamente as clínicas do Builder, não mistura owners e muda imediatamente após upload/delete.
- [ ] OpenAPI descreve uma parte de arquivo, somente campos conhecidos, limites e schemas sem anunciar credencial inadequada.
- [ ] Startup, health e contratos existentes não regridem.

**Validation:**
```bash
test -n "$POSTGRES_SMOKE_DATABASE_URL" && poetry run python scripts/check_media_dependencies.py && poetry run pytest tests/api/test_billing_usage.py tests/services/test_billing_usage_service.py tests/api/test_media_assets_openapi.py tests/api/test_health.py tests/integration/test_media_assets_postgres.py -v && make quality
```

**depends-on:** 5

**blocks:** 7, 8

---

### Group 7: Central de Mídias, papéis e navegação

**Goal:** Permitir administração do catálogo somente aos papéis/alvos autorizados, sem estado cruzado.

**Deliverables:**
1. Substituir mock/roadmap por upload, lista paginada, download, edição dos quatro metadados e exclusão em PT/EN.
2. Alterar `layout.tsx` e seus testes: `clinic_admin` vê/acessa Mídias no próprio tenant; Builder/super-admin somente com clínica selecionada; investor não vê e não acessa.
3. Enviar `viewingClinicId` em todas as operações de Builder/super-admin, omitir override para clinic-admin e abortar/limpar requests, cursor, arquivo, modal, edição e cache ao trocar clínica.

**Acceptance Criteria:**
- [ ] Testes de layout/nav cobrem clinic-admin, Builder, super-admin e investor, inclusive acesso direto negado e remoção do rótulo roadmap.
- [ ] Upload/list/PATCH exibem roundtrip exato dos quatro metadados e deixam claro que gatilho/sugestão não executam runtime.
- [ ] Troca de clínica impede flash, cache, cursor ou mutação da anterior; PT/EN e acessibilidade passam.

**Validation:**
```bash
pnpm --dir frontend/artifacts/neurostack exec vitest run --config vitest.config.ts src/pages/midias.test.tsx src/components/layout.test.tsx && pnpm --dir frontend run typecheck && pnpm --dir frontend/artifacts/neurostack run test && pnpm --dir frontend/artifacts/neurostack run build
```

**depends-on:** 5, 6

**blocks:** 8

---

### Group 8: Preflight integrado, capacidade e decisão de rollout

**Goal:** Provar o catálogo completo na mesma árvore e produzir uma decisão operacional mensurável antes de habilitar produção.

**Deliverables:**
1. Criar `scripts/check_media_release.py` e `tests/scripts/test_check_media_release.py`: validar ferramentas/env/evidências, executar e interpretar `pip-audit --local`, executar testes focados com JUnit, rejeitar skips críticos e escanear arquivos rastreados mais logs/evidências.
2. Preencher `docs/evidence/media-release-evidence.md` com comandos, owner, ambiente, aprovações, DB/WAL/backup encryption, custódia externa/retention age, inventário de owners/headroom e medições da fixture de 500 MB.
3. Aplicar os triggers objetivos de capacidade e registrar go/no-go; se disparados, registrar no-go e criar wish separado de object storage antes de rollout.
4. Adicionar `.artifacts/` a `.gitignore`; o runner cria JUnit somente nesse diretório ignorado, remove-o em sucesso e falha, e verifica que nenhum JUnit/log/artefato de release ficou rastreado.

**Acceptance Criteria:**
- [ ] O script falha para ferramenta/env/evidência ausente, dependência inválida, teste não-zero, skip/xfailed crítico, JUnit ausente/malformado, scan inconclusivo ou achado de plaintext/dado sensível.
- [ ] O release executa `pip-audit --local`, verifica status/resultado e bloqueia tanto quando o subprocesso falha quanto quando reporta advisory; testes plantam cada falha e provam que nenhuma é tratada como sucesso.
- [ ] `.artifacts/` está ignorado, o JUnit temporário é removido inclusive nas saídas de erro e `git ls-files .artifacts` permanece vazio.
- [ ] PostgreSQL, backup/restore, auth, concorrência, billing, OpenAPI e frontend passam na mesma árvore sem skip crítico.
- [ ] Evidência registra contagem atual de owners e capacidade alocada/livre, deltas de DB/WAL/backup e duração de backup/restore para fixture 500 MB, com owner e aprovações nomeados no ambiente real.
- [ ] Projeção/fixture `>=70%` de capacidade ou backup/restore `>30 min` produz no-go e cria o wish de object storage; ausência de medição externa produz BLOCKED.

**Validation:**
```bash
poetry install --sync && poetry run pytest tests/scripts/test_check_media_release.py -v && poetry run python scripts/check_media_release.py --evidence docs/evidence/media-release-evidence.md --junit-dir .artifacts/media-release && test ! -e .artifacts/media-release && test -z "$(git ls-files .artifacts)" && make quality && pnpm --dir frontend run typecheck && pnpm --dir frontend/artifacts/neurostack run test && pnpm --dir frontend/artifacts/neurostack run build
```

**depends-on:** 2, 6, 7

**blocks:** ajuste-central-clonagem

---

## QA Criteria

- [ ] Cada formato aceito faz upload, lista, download byte a byte, PATCH dos quatro metadados e exclusão; campos nunca disparam envio/runtime.
- [ ] Uma parte de arquivo/campos conhecidos, comprimentos, booleano e NUL/CR/LF são exercitados em limites e fora deles.
- [ ] `auto_suggest` omitido roundtripa `false`; `true` e `false` lowercase roundtripam; `TRUE`, `False`, `1`, `0`, vazio e qualquer outro valor falham.
- [ ] Builder troca entre duas clínicas sem estado residual; clinic-admin opera a própria; super-admin opera a selecionada; investor é ocultado/bloqueado.
- [ ] Clone sem user e tenant com clinic user passam; admin-only, builder-only, investor-only, inativo, plan admin e unowned falham antes de query de mídia.
- [ ] Corrida de uploads não ultrapassa o teto agregado; billing muda exatamente após upload/delete.
- [ ] O consumidor clone-compatible só mede a fonte após entrar na guarda e travar integralmente os IDs `FOR SHARE`; a projeção usa a mesma soma/cap do upload e nenhuma query de tamanho/uso antecipa a ordem canônica.
- [ ] Defaults permitem exatamente quatro uploads e oito downloads ativos; saturação retorna 503 imediato, redução positiva funciona, aumento/zero/inválido falha no startup e permits são recuperados em sucesso, erros, cancelamento e disconnect.
- [ ] Backup cifrado restaura com identidade isolada e nenhum dump plaintext durável é criado; retenção só alcança `postgres_*.sql.gz.age`, enquanto `.sql.gz`, `*_data_*.tar.gz` e demais nomes envelhecidos permanecem inventariados e intocados.
- [ ] Captura de logs e scan de release não encontram bytes, nomes autorais, gatilhos, hashes completos, chaves ou plaintext.
- [ ] Preflight rejeita ausência de PostgreSQL, age/age-keygen, evidência operacional, JUnit ou cenário crítico executado.
- [ ] Preflight bloqueia falha/achado plantado de `pip-audit --local` e limpa JUnit ignorado sem deixar artefato rastreado.

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Framework jump introduz regressão ampla | High | Lock exato, asserção executável, advisory scan, testes completos e OpenAPI antes do router multipart. |
| `BYTEA` aumenta banco/WAL/backups | High | Limites, cap agregado e trigger de capacidade `>=70%` ou tempo `>30` minutos tornam rollout no-go e abrem wish de object storage. |
| Mídia autorizada contém dado sensível | High | Criptografia em repouso, backup age sem plaintext, acesso mínimo, retenção e evidência externa obrigatória. |
| Arquivo reconhecido ainda é malicioso | High | Attachment + nosniff, sem render/processamento inline e comunicação explícita de conteúdo não confiável. |
| Backups legados plaintext permanecem expostos | High | Inventário read-only e runbook por alvo; nenhuma exclusão neste wish. |
| Divergência entre classificação admin e Mídias | High | Helper único preserva predicado role-based; filtros de ativo/não-admin ficam explicitamente na query de Mídias. |
| Semáforos são por processo, não globais | Medium | Defaults/máximos por processo são fixos em 4 uploads e 8 downloads, saturação é imediata e o cap agregado transacional continua global; ajuste somente para baixo depende de medição posterior. |
| Cursores vazam/reutilizam tenant | Medium | Cursor assinado tenant-bound e rejeição anterior à consulta de mídia. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

### 2026-10-07 — Plan review — FIX-FIRST

- **Reviewer:** `/root/media_plan_review`
- **Target SHA-256:** `350663c126a014ab44147bc651e552f07c3e0846205accf5e059bfaca8849854`
- **Reviewed at:** `2026-10-07T23:20:16Z`
- **Verdict:** FIX-FIRST.
- **HIGH:** metadados `display_name`, `original_filename`, `usage_trigger` e `auto_suggest` não estavam presos a critérios executáveis.
- **HIGH:** o predicado de clínica ativa era ambíguo e não possuía helper compartilhado/owner explícito.
- **HIGH:** o gate PostgreSQL/restore podia passar por skip quando os pré-requisitos não existissem.
- **HIGH:** navegação existente escondia Mídias de `clinic_admin` e mostrava para `investor`, em conflito com a API planejada.
- **HIGH:** API, limites, auth, paginação, billing e OpenAPI estavam concentrados em um grupo grande demais.
- **MEDIUM:** faltavam política fail-closed comum, `.env.example`, inventário/evidência durável e prova operacional agregada.

### 2026-10-07 — Plan re-review — FIX-FIRST

- **Reviewer:** `/root/media_plan_review`
- **Target SHA-256:** `bde553e5b8bef93afd76c902577dd961056c1d0baa92e12b6fa299e880235f89`
- **Reviewed at:** `2026-10-07T23:28:54Z`
- **Verdict:** FIX-FIRST.
- **HIGH:** faltava uma única API de domínio reutilizável para owner, chave advisory exata, lock, uso protegido e cap, com ordem canônica compartilhada por upload e clone futuro.
- **HIGH:** o Grupo 4 não era independentemente utilizável no app real porque registro, resolução e classificação do POST estavam postergados.
- **HIGH:** a validação do Grupo 2 não falhava imediatamente sem `POSTGRES_SMOKE_DATABASE_URL`.
- **HIGH:** o preflight agregado não obrigava execução/verificação de `pip-audit --local` nem testes com falhas plantadas.
- **MEDIUM:** trigger de capacidade, wire de `auto_suggest` e lifecycle do JUnit precisavam de contratos únicos e fail-closed.

### 2026-10-07 — Final plan re-review — BLOCKED

- **Reviewer:** `/root/media_plan_review`
- **Target SHA-256:** `7e664b48773ef6c4d6ed43f550dbad28631111fb11a1fd8ea8b2ae86c99e7366`
- **Reviewed at:** `2026-10-07T23:35:51Z`
- **Verdict:** BLOCKED após o ciclo 2/2.
- **HIGH:** `reserve_media_storage_capacity(additional_bytes)` exige bytes antes dos locks, mas o clone só conhece o total depois do advisory, Tenant e conjunto completo `FOR SHARE`; é necessário um guard/contexto em duas fases e teste com consumidor de tamanho inicialmente desconhecido.
- **HIGH:** faltam contratos executáveis para limite padrão de quatro uploads e oito downloads, redução somente positiva, 503 imediato e liberação de permit em sucesso, erro, cancelamento e disconnect.
- **HIGH:** retenção do backup não está limitada a `postgres_*.sql.gz.age`; testes precisam provar que `.sql.gz` e `*_data_*.tar.gz` legados nunca são elegíveis.
- **Escalation:** os reparos são determinísticos, mas os ciclos automáticos foram esgotados; uma autorização humana explícita para emenda/addendum e nova revisão é necessária.

### 2026-10-07 — Human-authorized plan addendum — FIX-FIRST

- **Authorization:** o usuário respondeu explicitamente `autorizo` à solicitação de emendar e revalidar `media-assets-catalog` antes de executar `ajuste-central-clonagem`.
- **Classification:** correção determinística de contrato no plano; não é redesign de escopo nem enfraquecimento de critério.
- **Amendment:** `reserve_media_storage_capacity(additional_bytes)` foi substituída por `media_storage_capacity_guard` em duas fases, com lock canônico anterior a qualquer medição e contrato clone-compatible verificável.
- **Amendment:** concorrência ficou vinculada aos defaults/máximos de quatro uploads e oito downloads, redução somente positiva, 503 imediato e liberação em todos os finais de lifecycle.
- **Amendment:** retenção/exclusão de produção ficou restrita ao basename `postgres_*.sql.gz.age`, com casos negativos obrigatórios para legados e nomes semelhantes.
- **Status:** FIX-FIRST até uma nova revisão independente retornar SHIP; esta autorização não aprova o plano nem inicia implementação.

### 2026-10-07 — Fresh plan review after authorized addendum — SHIP

- **Reviewer:** `/root/media_fresh_plan_review`
- **Target SHA-256:** `16abbcda4465d98a036146b5f8092d7a55d27f8bc186e55a94321e86364d622d`
- **Reviewed at:** `2026-10-07T23:50:32Z`
- **Verdict:** SHIP; nenhum gap CRITICAL/HIGH.
- A revisão confirmou o guard bifásico clone-compatible, limites 4/8 fail-closed, retenção exclusiva `.age`, DAG acíclico de oito grupos, validações proporcionais e escopo completo.

### 2026-10-07 — Group 1 execution review — BLOCKED

- **Engineer/fixers:** `/root/media_g1_runtime`, `/root/media_g1_fix1`
- **Reviewer:** `/root/media_g1_review`
- **Fix loops:** 2/2 esgotados.
- **Verified:** FastAPI 0.142.4, Starlette 1.7.0, multipart 0.0.32, checker fail-closed, `cryptography` explícito, `python-jose`/`ecdsa` removidos, 134 testes focados verdes e `pip-audit --local` sem vulnerabilidades conhecidas.
- **BLOCKED:** pytest auditado exige `pytest>=9.0.3`/pytest-asyncio 1.x, que removeu a fixture legada `event_loop`; `tests/services/test_multimodal_transcription.py` produz 47 erros de setup. O downgrade reintroduz `PYSEC-2026-1845`.
- **Escalation:** requer autorização humana explícita para ampliar o Grupo 1 e migrar o fixture local para semântica pytest-asyncio 1.x, seguida de audit e gate completo.

### 2026-10-07 — Group 2 execution/quality review — BLOCKED

- **Engineer/fixer:** `/root/media_g2_backup`, `/root/media_g2_fix1`
- **Functional reviewer:** `/root/media_g2_review` — SHIP após 10 testes focados e correções de publicação exclusiva, PATH controlado, cleanup de DB e custodiante do inventário.
- **Quality reviewer:** `/root/media_g2_quality` — BLOCKED após o único fix loop de qualidade; 13 testes focados verdes.
- **Residual HIGH:** remover override de banco sistêmico controlável por produção; adicionar `psql -X`; limpar variáveis libpq herdadas conflitantes; impedir traceback/fragmento secreto em URL inválida.
- **Escalation:** requer autorização humana explícita para um fix excepcional de qualidade e nova revisão; o card permanece `in_progress/BLOCKED`.

### 2026-10-07 — Human-authorized exceptional execution fixes

- **Approver:** usuário da sessão.
- **Approved at:** `2026-10-07T21:49:05-03:00`.
- **Group 1 expansion:** autorizado editar `tests/services/test_multimodal_transcription.py` exclusivamente para migrar a fixture local `event_loop` para pytest-asyncio 1.x, preservando a intenção dos 47 testes e exigindo audit/gate completo.
- **Group 2 exception:** autorizado um fix excepcional após o cap de qualidade para remover o override de banco sistêmico, adicionar `psql -X`, limpar variáveis libpq conflitantes e sanitizar falhas de parsing de URL; nova revisão funcional/qualidade permanece obrigatória.
- **Model/effort:** sem escalada; mesmos perfis `genie_fixer` e `genie_reviewer`, pois os gaps são determinísticos e o contexto está completo.

### 2026-10-07 — Group 1 exceptional fix and quality re-review — SHIP

- **Engineer/fixer:** `/root/media_g1_fix1`.
- **Functional reviewer:** `/root/media_g1_review` — SHIP; testes diretos confirmaram a semântica do wrapper local em Python 3.11.15 e 3.12.3, restauração do loop anterior e ausência de vazamento.
- **Quality reviewer:** `/root/media_g1_quality` — SHIP em `2026-10-07T22:00:43-03:00`, target agregado SHA-256 `42399e2cc48ad98ca47e475d99135c34899d369ea248ce67fb942851e541110e`; nenhum gap CRITICAL/HIGH.
- **Validation:** `tests/services/test_multimodal_transcription.py` 47/47, conjunto focado 181/181, checker 19/19, `scripts/check_media_dependencies.py`, `pip-audit --local`, Black focado e `git diff --check` passaram.
- **Aggregate baseline:** `make quality` numa worktree isolada alcançou uma falha Black preexistente no `HEAD` em `src/memory/event_store.py`, `tests/srm/test_metrics_optin.py`, `tests/services/test_api_credentials.py` e `tests/presets/test_preset_loader.py`; normalizando somente esses quatro arquivos na worktree descartável, Flake8 e MyPy passaram. Nenhum desses arquivos pertence ao Grupo 1.
- **Verdict:** SHIP; a exceção autorizada ficou restrita à fixture local e não deixou gap funcional ou de qualidade no escopo.

### 2026-10-07 — Group 2 exceptional fix and executable-mode re-review — SHIP

- **Engineer/fixers:** `/root/media_g2_fix1`, `/root/media_g2_execmode_fix`.
- **Functional reviewer:** `/root/media_g2_review` — SHIP após hardening de publicação, cleanup e custódia do inventário.
- **Quality reviewer:** `/root/media_g2_quality` — SHIP após a exceção autorizada remover override de banco sistêmico, adicionar `psql -X`, limpar variáveis libpq, sanitizar parsing de URL e proteger paths iniciados por hífen.
- **Executable-mode reviewer:** `/root/media_g2_execmode_review` — SHIP; target agregado SHA-256 `2a811008fd442dc2d6e03f1beaf8b62803b5f9f44fc1b86c56b0bb0795ae0072`, nenhum gap CRITICAL/HIGH.
- **Portability fix:** `scripts/backup.sh` mudou de modo Git `100644` para `100755` e `scripts/restore_backup.sh` foi criado como `100755`, sem alteração adicional de conteúdo.
- **Validation:** worktree Linux isolada, PostgreSQL 16, `age` real, Python 3.12.3 e pytest 9.1.1; `stat` confirmou `755`, `bash -n` passou e `tests/scripts/test_backup_encryption.py` concluiu 15/15.
- **Verdict:** SHIP; backup/restore criptografado, retenção exclusiva, inventário não destrutivo e hardening autorizado permanecem satisfeitos.

### 2026-10-07 — Group 3 execution review after fix loops — BLOCKED

- **Engineer/fixers:** `/root/media_g3_persistence`, `/root/media_g3_fix1`.
- **Functional reviewer:** `/root/media_g3_review`; target final SHA-256 `511a1fad5ec8b55578b8d74ced10d25acd73716fe028a108bd8444e3c7352375` — SHIP no target, nenhum gap CRITICAL/HIGH.
- **Quality reviewer:** `/root/media_g3_quality`; digest reproduzível do penúltimo target `ca2d6854e9342f60489f4af90287b372292450bce623b5b915e645bfac1fb310` — SHIP no target após o loop final, nenhum gap CRITICAL/HIGH.
- **Fix loop 1:** fechou sniffing MP3/MP4 permissivo, corrida upload-versus-clone, matriz administrativa, transições de owner link/unlink/delete-builder e cascade sem carga de `BYTEA`.
- **Fix loop 2:** restringiu MP3 a MPEG Layer III e alinhou `delete_clinic`, updates conjuntos de clínica/usuário e Builder/usuário à ordem canônica, com testes PostgreSQL concorrentes.
- **Focused validation:** orquestrador reproduziu 109/109 testes, incluindo 16 cenários PostgreSQL, e Black/isort dos 11 arquivos; fixer registrou Flake8, MyPy em 178 arquivos e diff checks verdes. Sete novos arquivos Python estão `100644` no índice.
- **Isolated aggregate gate:** numa worktree descartável contendo apenas os Grupos 1–3, os quatro arquivos Black preexistentes do `HEAD` foram normalizados somente nessa cópia; `make quality` alcançou a suíte completa com PostgreSQL local/credencial efêmera e concluiu `4044 passed, 1 error`.
- **Residual deterministic failure:** `tests/integration/test_handoff_sla_postgres.py` fixa `alembic_version == "021"`; a migration de Mídias torna a cabeça válida `"022"`. O erro é uma regressão causada pelo Grupo 3, embora esteja num teste legado fora do target inicial.
- **Escalation:** os dois fix loops foram consumidos. É necessária autorização humana explícita para ampliar o Grupo 3 a esse teste de smoke, substituir a expectativa obsoleta por uma asserção de cabeça atual apropriada e repetir o gate agregado/re-review.
- **Verdict:** BLOCKED; o target funcional foi aprovado, mas a política fail-closed impede `task done` enquanto o gate global não tiver exit zero.

### 2026-10-08 — Human-authorized Group 3 exceptional smoke fix

- **Approver:** usuário da sessão.
- **Approved at:** `2026-10-08T07:55:45-03:00`.
- **Authorization:** terceiro fix excepcional restrito a `tests/integration/test_handoff_sla_postgres.py`, para remover a expectativa obsoleta de cabeça Alembic `021` após a inclusão da migration `022` de Mídias.
- **Required evidence:** teste de smoke/migration afetado, gate agregado em worktree isolada, nova revisão funcional e de qualidade; a autorização não permite alterar semântica de handoff nem ampliar escopo.
- **Status:** autorizado para execução; o Grupo 3 continua `in_progress` até exit zero e re-review SHIP.

### 2026-10-08 — Group 3 exceptional smoke fix review and appeal — RESOLVED / SHIP

- **Fixer:** `/root/media_g3_fix1`; delta SHA-256 `450815e3f622f886d13bb1a826b57bc3c7079805a617c11ea6635d4573953f44` em `tests/integration/test_handoff_sla_postgres.py` somente.
- **Change:** a expectativa fixa `021` foi substituída por comparação fail-closed entre `alembic heads` e todas as linhas de `alembic_version`; nenhuma semântica de handoff foi alterada.
- **Focused validation:** 22/22 testes PostgreSQL/migration, Black, isort e diff check passaram.
- **Quality reviewer:** `/root/media_g3_quality` — SHIP no target agregado, digest reproduzível `74e2705b6a3afd74705f734b63a25c5710f699dcd5faab20ac771bde19a5a556`; nenhum gap CRITICAL/HIGH.
- **Functional reviewer:** `/root/media_g3_review` — BLOCKED/HIGH porque `_run_alembic()` continua usando `upgrade head`, que falharia num grafo multi-head antes da nova comparação; propôs `upgrade heads` e teste dedicado.
- **Final gate:** `/root/media_g3_exception_gate` — SHIP; concluiu que multi-head não é requisito do WISH, o grafo real é linear `021 → 022`, `upgrade head` permanece fail-closed se surgir divergência e mudar para `upgrade heads` ampliaria comportamento sem critério autorizado.
- **Contested criterion:** se o smoke precisa suportar e aplicar grafos Alembic multi-head ou apenas validar fail-closed a cabeça do grafo linear atual.
- **Aggregate validation:** worktree isolada com baseline Black normalizada apenas na cópia, PostgreSQL/credencial efêmeros e `make quality` completo — `4045 passed, 229 warnings`, exit 0, em 10m44s; recursos temporários removidos.
- **Appeal:** reviewer ordinário e final gate divergiram sobre suporte multi-head.
- **Human resolution:** em `2026-10-08T08:53:49-03:00`, o usuário confirmou aceitar o SHIP do final gate e não ampliar o escopo para `upgrade heads`/grafo multi-head.
- **Resolved verdict:** SHIP no escopo aprovado; nenhum gap CRITICAL/HIGH permanece e o gate agregado tem exit zero.

### 2026-10-08 — Group 4 execution and re-review — SHIP

- **Engineer/fixer:** `/root/media_g4_upload`, `/root/media_g4_fix1`.
- **Functional reviewer:** `/root/media_g4_review` — SHIP após o fix loop 1; nenhum gap CRITICAL/HIGH.
- **Quality reviewer:** `/root/media_g4_quality` — SHIP; target agregado SHA-256 `521b9631387355a7fd30c220ae01202e3443a2a2d37d990f05aa8e0169bc5ec6`, nenhum gap CRITICAL/HIGH.
- **Fix loop 1:** fechou bypass de booleano multipart via charset UTF-7/UTF-16 e revalidou ownership do Builder na segunda leitura protegida, cobrindo unlink e troca de owner entre resolução e autorização.
- **Focused validation:** orquestrador reproduziu 114/114 testes, incluindo matriz PostgreSQL, e Black/isort/diff check dos arquivos do grupo; os cinco novos arquivos Python estão `100644` no índice.
- **Aggregate validation:** worktree isolada contendo apenas os Grupos 1–4, baseline Black normalizada somente na cópia e PostgreSQL/credencial efêmeros; `make quality` concluiu `4095 passed, 229 warnings`, exit 0, em 7m13s; recursos temporários removidos.
- **Verdict:** SHIP; o POST multipart, limites 4/8 fail-closed, autorização/ordem transacional, validação de formatos e redaction permanecem satisfeitos.

### 2026-10-08 — Group 5 execution and re-review — SHIP

- **Engineer/fixer:** `/root/media_g5_crud`, `/root/media_g5_fix1`.
- **Functional reviewer:** `/root/media_g5_review` — SHIP após o fix loop 1; nenhum gap CRITICAL/HIGH.
- **Quality reviewer:** `/root/media_g5_quality` — SHIP; target agregado SHA-256 `fcb97808814cd7bb72fc73967d8458b99f2d5e45f8739e34b5513a9da6115a84`, nenhum gap CRITICAL/HIGH.
- **Fix loop 1:** passou a exigir claim/ownership para todo caller Builder, inclusive no próprio tenant, e incorporou predicado atômico de autorização nas statements de lista, download, PATCH e DELETE; PATCH revalida tanto no `SELECT FOR UPDATE` quanto no `UPDATE`, sem inserir lock antecipado no POST.
- **Focused validation:** orquestrador reproduziu 130/130 testes com PostgreSQL real, incluindo 18 regressões novas para tenant misto, relink e 16 combinações de revogação concorrente por operação; Black, isort, Flake8, MyPy focado e diff checks passaram.
- **Aggregate validation:** worktree isolada contendo apenas os Grupos 1–5, baseline Black normalizada somente na cópia e PostgreSQL/credencial efêmeros; `make quality` concluiu `4155 passed, 229 warnings`, exit 0, em 5m16s; recursos temporários removidos.
- **Verdict:** SHIP; CRUD, keyset tenant-bound, download concorrente, isolamento/autorização atômica, metadados, headers, transações e redaction permanecem satisfeitos.

### 2026-10-08 — Group 6 execution and re-review — SHIP

- **Engineer/fixers:** `/root/media_g6_billing_openapi`, `/root/media_g6_fix1`, `/root/media_g6_fix2`.
- **Functional reviewer:** `/root/media_g6_review` — SHIP após o fix loop 1; nenhum gap CRITICAL/HIGH no billing ou contrato OpenAPI.
- **Quality reviewer:** `/root/media_g6_quality` — SHIP; target agregado G6 SHA-256 `92ea08f488a1a86758cd5ad1dd3bb385d2e28855ca991e9556dc7f93e6038534`, nenhum gap CRITICAL/HIGH.
- **Fix loop 1:** removeu o relabel inválido de schemas 3.1 como OAS 3.0.3, publicou OpenAPI 3.1.0 no caminho normal/fallback, validou o documento completo pelo modelo OpenAPI do FastAPI, publicou `minProperties: 1` no PATCH e alinhou o 409 a `media_storage_cap_exceeded`.
- **Fix loop 2 / aggregate:** o gate completo revelou alias Base64URL não canônico no cursor G5; a correção exige re-encoding unpadded canônico antes de HMAC/JSON. Re-review funcional/qualidade do cursor retornou SHIP; digest agregado G5 atualizado `81e063598fcad6d950d4196f1759c87efd030f090324cdb5af5c933aa905eabc`.
- **Focused validation:** checker de dependências passou; orquestrador reproduziu 62/62 testes G6 com PostgreSQL real e 61/61 testes API de Mídias após o fix de cursor; Black, isort, Flake8, MyPy focado e diff checks passaram.
- **Aggregate validation:** worktree isolada contendo apenas os Grupos 1–6, baseline Black normalizada somente na cópia e PostgreSQL/credencial efêmeros; após uma reprodução pré-fix de `4161 passed, 1 failed`, o gate corrigido concluiu `4164 passed, 229 warnings`, exit 0, em 5m43s. Um erro de sintaxe somente no trap final foi compensado por limpeza manual auditada; zero papéis e zero DBs temporários permaneceram.
- **Residual MEDIUM:** se o caminho customizado de OpenAPI falhar após a geração-base, o fallback 3.1.0 não reaplica `securitySchemes`/hook de headers; o caminho normal validado, runtime e autenticação não são afetados.
- **Verdict:** SHIP; billing owner-scoped, efeito imediato de upload/delete, OpenAPI 3.1 válido, segurança alternativa e regressões de startup/health permanecem satisfeitos.

### 2026-10-08 — Group 7 execution and fix review — SHIP

- **Engineer:** `/root/media-g7-frontend`; fixer: `/root/media-g7-fix1` (morreu após implementar; mudanças em midias.tsx 11:47, midias.test.tsx 11:57).
- **Reviewer:** `a6c633dd142bd5a04` — SHIP; nenhum gap CRITICAL/HIGH.
- **Fix loop 1:** race GET→POST corrigido via `catalogRevisionRef` (incrementado em POST/PATCH/DELETE, verificado antes de substituir items no GET); `tenant_id` no POST Builder via `mediaUrl()` + assertiva de URL no teste; Bearer em todos os seis fetches com iteração de todas as chamadas no teste.
- **Focused validation (orquestrador):** `vitest run src/pages/midias.test.tsx src/components/layout.test.tsx` → 42/42; `pnpm test` (full suite) → 399/399; `PORT=3001 BASE_PATH=/ pnpm build` → 2804 módulos, exit 0.
- **Minor OBS-LOW (não bloqueantes):** scope string interpola `undefined` literal quando `targetIdentity` não definido (guard `requiresSelectedClinic` previne fetch); `tenantId || clinicId` usado apenas para keying de estado, não afeta rede.
- **Verdict:** SHIP; Central de Mídias, guards de papel/nav, reconciliação de catálogo, roundtrip de quatro metadados, PT/EN e limpeza de troca de clínica satisfeitos.

### 2026-10-08 — Group 8 execution review — SHIP

- **Engineer:** `a05e8844730ce3ced` (media-g8-release).
- **Reviewer:** `a69f78ff1fc41b8bb` — SHIP; nenhum gap CRITICAL/HIGH.
- **Deliverables:** `scripts/check_media_release.py` (8 estágios, exit 0/1/2), `tests/scripts/test_check_media_release.py` (38 testes), `docs/evidence/media-release-evidence.md` (BLOCKED honesto — `POSTGRES_SMOKE_DATABASE_URL` ausente), `.gitignore` com `.artifacts/`.
- **Focused validation (orquestrador):** 38/38 testes; gate real → exit 2 (BLOCKED) com `POSTGRES_SMOKE_DATABASE_URL` ausente; JUnit removido; `git ls-files .artifacts` vazio; Black/isort/flake8/mypy no delta limpos; `make quality` passa em 5 arquivos preexistentes fora do delta (baseline documentado).
- **BLOCKED honesto:** Sem `POSTGRES_SMOKE_DATABASE_URL` e medições externas de capacidade/backup, o gate emite exit 2 — jamais 0 (skip silencioso) nem 1 (falha de código). Operador de produção deve preencher evidência e re-executar.
- **OBS-LOW (não bloqueante):** `scan_tracked_tree` não emite linha `[INFO]` de sucesso — legibilidade apenas; sem impacto em critério.
- **Verdict:** SHIP; gate fail-closed, 8 estágios com testes plantados, JUnit lifecycle, scan de plaintext, BLOCKED policy e estrutura de evidência operacional satisfeitos.

---

## Files to Create/Modify

```text
.env.example
.gitignore
pyproject.toml
requirements.txt
requirements-dev.txt
poetry.lock
src/config/settings.py
src/api/main.py
src/api/middleware/media_limits.py
src/api/routes/admin.py
src/api/routes/media_assets.py
src/api/schemas/media_assets.py
src/models/__init__.py
src/models/media_asset.py
src/models/tenant.py
src/services/tenant_classification.py
src/services/media_assets.py
src/services/billing_usage_service.py
alembic/versions/20261007_000001_022_add_media_assets.py
scripts/backup.sh
scripts/restore_backup.sh
scripts/check_media_dependencies.py
scripts/check_media_release.py
docs/media-backup-rollout.md
docs/evidence/media-legacy-backup-inventory.md
docs/evidence/media-release-evidence.md
tests/api/test_admin.py
tests/api/test_media_assets.py
tests/api/test_media_assets_upload.py
tests/api/test_media_assets_authorization.py
tests/api/test_media_assets_openapi.py
tests/api/test_billing_usage.py
tests/api/test_health.py
tests/services/test_tenant_classification.py
tests/services/test_media_assets.py
tests/services/test_billing_usage_service.py
tests/services/test_multimodal_transcription.py
tests/integration/test_media_assets_postgres.py
tests/config/test_media_settings.py
tests/scripts/test_check_media_dependencies.py
tests/scripts/test_check_media_release.py
tests/scripts/test_backup_encryption.py
frontend/artifacts/neurostack/src/components/layout.tsx
frontend/artifacts/neurostack/src/components/layout.test.tsx
frontend/artifacts/neurostack/src/pages/midias.tsx
frontend/artifacts/neurostack/src/pages/midias.test.tsx
frontend/artifacts/neurostack/src/locales/pt.ts
frontend/artifacts/neurostack/src/locales/en.ts
```
