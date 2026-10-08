# Design: Catálogo e armazenamento de Mídias

| Field | Value |
|-------|-------|
| **Slug** | `media-assets-catalog` |
| **Date** | 2026-10-07 |
| **WRS** | 100/100 |

## Problem

A Central de Mídias anuncia vídeos, áudios, PDFs e imagens, mas permanece um mock sem persistência, ownership, upload, download ou medição real. Sem um catálogo tenant-scoped, as clínicas não conseguem manter um acervo utilizável e a futura integração de clonagem não terá uma fonte relacional bem definida para copiar.

## Scope

### IN

- Criar a tabela e o modelo `media_assets`, vinculados por `tenant_id`, com identificador próprio, nome de exibição, nome original, categoria (`video`, `audio`, `pdf`, `image`), MIME canônico, `size_bytes`, SHA-256, payload binário, gatilho de uso opcional, flag de sugestão automática e timestamps.
- Antes de expor multipart, atualizar FastAPI para `>=0.142.4,<0.143.0` e `python-multipart` para `>=0.0.32,<0.1.0`, deixar FastAPI resolver sua Starlette compatível sem pin direto desnecessário e exigir Starlette resolvida `>=1.0.1`; atualizar manifests, regenerar locks, registrar/assertar as versões exatas instaladas e verificar advisories sobre o ambiente resolvido.
- Implementar upload multipart de MP4, MP3, PDF, JPEG e PNG, limitado a 50.000.000 bytes reais e validado por MIME fechado mais assinatura do conteúdo.
- Implementar listagem keyset sem payload, download autenticado, atualização somente dos metadados autorais e exclusão definitiva.
- Aceitar `tenant_id` opcional em todos os cinco endpoints, executar `resolve_effective_tenant` antes de qualquer acesso a mídia e aceitar como alvo apenas tenant ativo classificado como clínica pelo contrato compartilhado do domínio.
- Substituir o mock da Central de Mídias por catálogo funcional em PT/EN, com upload, listagem, paginação, edição e exclusão.
- Calcular `storage.media` do Builder pela soma de `media_assets.size_bytes` em todas as clínicas vinculadas.
- Impor limites operacionais de corpo, concorrência e armazenamento agregado para impedir exaustão de memória, banco, WAL e backups.
- Criptografar o caminho de backup do repositório com `age`, sem dump plaintext durável, e tornar criptografia em repouso de PostgreSQL/WAL/backups um pré-requisito comprovado do rollout de produção.

### OUT

- Enviar ativos manual ou automaticamente por Meta, Z-API, Evolution ou Uazapi.
- Executar gatilhos, classificação de intenção ou sugestão automática durante conversas; os campos são apenas catálogo nesta entrega.
- Transcodificar, comprimir, criar thumbnail, executar OCR ou integrar antivírus externo.
- Adicionar S3, MinIO, CDN, URLs pré-assinadas, filas, outbox, deduplicação por hash, versionamento ou lixeira.
- Alterar preços, créditos ou criar planos/cotas comerciais dinâmicos; o teto operacional fixo desta entrega é uma proteção de segurança, não uma nova oferta comercial.
- Integrar a cópia de mídias ao fluxo de clonagem ou adicionar um construtor/helper de clone. Isso será entregue pelo wish dependente `ajuste-central-clonagem`, depois deste wish, consumindo o modelo relacional e seus invariantes.

## Approach

Uma migration adiciona `media_assets` com chave estrangeira para `tenants` e índice `(tenant_id, created_at DESC, id DESC)`. Os bytes ficam em `BYTEA`; `size_bytes` e SHA-256 são calculados do payload realmente lido. O banco reforça `octet_length(payload) = size_bytes`, `0 < size_bytes <= 50000000`, SHA-256 binário com `octet_length(sha256) = 32`, e uma `CHECK` fechada para os pares categoria/MIME canônicos (`video/video/mp4`, `audio/audio/mpeg`, `pdf/application/pdf`, `image/image/jpeg`, `image/image/png`). `display_name` tem 1–200 caracteres, `original_filename` 1–255 e `usage_trigger` opcional no máximo 500; nomes rejeitam NUL, CR e LF. Esses campos e o ownership explícito são o contrato suficiente para o wish posterior copiar linhas dentro da própria transação, sem maquinaria dormente nesta entrega.

Antes do parser multipart, middleware ASGI específico para a rota de upload rejeita `Content-Length` acima de 51.000.000 bytes e também conta todos os bytes recebidos em mensagens `http.request`, inclusive `Transfer-Encoding: chunked`, interrompendo ao exceder o mesmo teto. O envelope deixa margem apenas para boundary e metadados já limitados; a leitura do `UploadFile` continua em blocos até 50.000.001 bytes para aplicar o limite exato do arquivo. O parser limita uma parte de arquivo e o conjunto conhecido de campos. Semáforos por processo limitam uploads em parsing/persistência a 4 e downloads ativos a 8, com rejeição imediata quando saturados; configurações podem reduzir esses valores, nunca removê-los. Esses controles simples são necessários agora porque arquivos autorizados de 50 MB ainda podem esgotar RAM, conexões, WAL e disco quando concorrentes.

A assinatura fechada reconhece PDF (`%PDF-`), PNG, JPEG, MP3 (ID3 ou frame MPEG válido) e MP4 com `ftyp` reconhecível. Extensão e MIME declarados não bastam; categoria e MIME são derivados pelo servidor, e divergência falha. Reconhecimento de assinatura não demonstra ausência de malware: todo payload continua sendo anexo não confiável, nunca renderizado inline ou processado pelo servidor.

Os endpoints são `POST /v1/media-assets`, `GET /v1/media-assets`, `GET /v1/media-assets/{id}/content`, `PATCH /v1/media-assets/{id}` e `DELETE /v1/media-assets/{id}`. Todos aceitam `tenant_id: UUID | None`, inclusive download/PATCH/DELETE, chamam `resolve_effective_tenant(request, caller, tenant_id, db)` antes de consultar `media_assets` e então validam que o resultado é uma clínica ativa usando a mesma classificação de clínica do domínio. API key e `clinic_admin` só usam a própria clínica e não podem sobrescrever; Builder pode selecionar somente clínica que lhe pertence; `super_admin` pode selecionar qualquer clínica válida; `investor`, papel ausente ou não suportado recebe 403 antes de acesso a mídia. Alvo inexistente, inativo ou não-clínica recebe 403 `media_target_forbidden`. Toda busca por ativo inclui `id AND tenant_id`; inexistência e cross-tenant retornam o mesmo 404.

A listagem ordena por `(created_at DESC, id DESC)`, usa `page_size=25` por padrão e máximo 100, e retorna `items`, `next_cursor` e `has_more`. O cursor opaco, assinado e tenant-bound carrega o último par `(created_at,id)`; a página seguinte usa comparação estrita lexicográfica `<`. Inserções concorrentes anteriores ao cursor não duplicam nem aparecem no percurso já iniciado (o cliente reinicia para vê-las); exclusões são simplesmente puladas, e nenhuma promessa de snapshot ou total exato é feita. Cursor inválido ou de outro tenant falha sem consulta a mídia.

O upload validado abre transação, toma `pg_advisory_xact_lock` por proprietário de armazenamento (`builder_id`, ou o próprio `tenant_id` para clínica independente), soma sob o mesmo lock todas as mídias das clínicas desse proprietário e só insere se `used + size <= 500.000.000` bytes. Assim, uploads concorrentes em réplicas não ultrapassam o teto operacional agregado. PATCH e DELETE também usam transação explícita. Cada mutação faz `flush` e `commit` apenas após todas as validações; qualquer exceção faz `rollback`, não deixa linha parcial e converte falha de persistência para o erro estável. A exclusão efetiva reduz imediatamente tanto a soma operacional quanto o billing lógico.

Lista e mutações não selecionam/retornam `payload`. O download responde com MIME canônico, tamanho exato, `X-Content-Type-Options: nosniff` e `Content-Disposition: attachment`: fallback ASCII gerado pelo servidor, sem controles e sem CR/LF, mais `filename*` UTF-8 percent-encoded conforme RFC 5987. Logs contêm somente IDs, categoria, tamanho e resultado, sem bytes, nomes autorais, gatilhos ou hashes completos.

Erros têm corpo `{ "code": "..." }` estável: 413 `media_body_too_large` ou `media_size_exceeded`; 415 `media_type_unsupported`, `media_signature_invalid` ou `media_type_mismatch`; 422 `media_metadata_invalid`; 400 `media_cursor_invalid`; 403 `media_target_forbidden` ou códigos do resolver; 404 `media_asset_not_found`; 409 `media_storage_cap_exceeded`; 503 `media_concurrency_exceeded`; 500 `media_persistence_failed`. Detalhes de parser, banco e ownership não são devolvidos.

A UI usa `viewingClinicId` como chave do catálogo e o envia como `tenant_id` para Builder/super-admin em lista, upload, download, PATCH e DELETE. Ao mudar a clínica, cancela requests/listagens/uploads em andamento com `AbortController`, limpa cursor, arquivo, edição/modal e dados locais da clínica anterior, e recomeça da primeira página; query keys incluem tenant e cursor para impedir flash ou mutação cross-tenant. Para clinic-admin, a UI usa o tenant próprio e não oferece override. Mensagens PT/EN cobrem os códigos estáveis sem expor detalhes internos.

O billing substitui o zero fixo por `SUM(media_assets.size_bytes)` sobre tenants do Builder. Não há contador duplicado: o banco é fonte de verdade, e futuras cópias relacionais serão cobradas naturalmente quando o wish dependente as criar.

O upgrade de dependências é pré-requisito do router multipart. `pyproject.toml` e `requirements.txt` declaram FastAPI `>=0.142.4,<0.143.0` e `python-multipart>=0.0.32,<0.1.0`; Starlette não ganha pin direto, pois FastAPI deve possuir essa compatibilidade. `poetry.lock` e qualquer outro lock Python versionado são regenerados, o ambiente é sincronizado, e CI registra e compara as versões exatas de FastAPI, Starlette e python-multipart, recusando Starlette abaixo de 1.0.1. O ambiente resolvido passa pelo advisory scanner fixado em desenvolvimento/CI. Como o salto de framework afeta toda a superfície ASGI, o gate inclui testes focados de upload/auth/body cap, integração PostgreSQL, smoke de startup/OpenAPI/health, `make quality` completo e, no frontend, `npm run typecheck`, `npm test` e `npm run build`; nenhum endpoint multipart é habilitado se qualquer gate falhar.

`scripts/backup.sh` passa a exigir o binário `age` e um recipient público válido vindo de variável de ambiente alimentada por secret manager. O script valida ambos antes de iniciar `pg_dump`, cria no diretório de backup um arquivo temporário com permissões restritivas e usa pipeline com `pipefail` equivalente a `pg_dump | gzip | age -r "$BACKUP_AGE_RECIPIENT" > arquivo.tmp`; somente o sucesso de todas as etapas permite `fsync` quando disponível e rename atômico para `postgres_<timestamp>.sql.gz.age`. Falha remove somente o temporário cifrado e nunca deixa dump `.sql`/`.gz` durável. A chave privada de decriptação fica fora do repositório, do container da aplicação e dos logs. Retenção e delete selecionam apenas artefatos `.age`; logs não imprimem recipient sensível, chave nem plaintext. Um teste de restore usa credencial de teste isolada para decriptar e restaurar em PostgreSQL descartável, valida conteúdo/integridade e destrói banco e temporários. Produção só recebe rollout com evidência do teste e de criptografia em repouso do PostgreSQL, WAL e destino de backup. Excluir uma mídia remove a linha ativa, mas cópias cifradas já retidas permanecem recuperáveis até expirarem pela retenção documentada.

Alternativas rejeitadas:

- **S3/MinIO agora:** introduziria credenciais, bucket policy, ambiente local adicional, URLs assinadas e compensação banco/objeto sem volume medido que pague esse custo.
- **Base64/JSON ou data URL:** aumenta payload, memória e exposição acidental; multipart + binário é o contrato apropriado.
- **Salvar em filesystem local:** quebra com múltiplas réplicas, deploys efêmeros e isolamento operacional.
- **Contador de cota sem lock:** sofre drift e permite ultrapassagem por concorrência; soma sob advisory lock mantém uma única fonte de verdade.
- **Ativar envio ou clonagem junto:** mistura catálogo com outros runtimes; `ajuste-central-clonagem` fará a integração depois desta fundação.

## Simplicity Case

- **Simplest complete design:** uma tabela tenant-scoped com payload `BYTEA`, cinco endpoints, uma página de catálogo e billing derivado por soma.
- **Added machinery:** body cap pré-parser, limites de concorrência e advisory lock evitam exaustão presente; paginação evita carregar o acervo inteiro; assinatura e SHA-256 sustentam integridade.
- **Deferred until measured:** object storage/CDN quando banco, WAL, backups ou latência excederem metas; processamento assíncrono quando uploads válidos excederem repetidamente o timeout; thumbnails/scan externo quando houver requisito de preview ou compliance.
- **Complexity removed:** nenhum storage externo, URL assinada, fila, retry persistente, dedupe, transcodificação, versionamento, estado de entrega ou helper de clone.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Persistir bytes em PostgreSQL `BYTEA` nesta versão. | É o menor mecanismo transacional para upload, download e billing com o limite atual. |
| 2 | `media_assets` pertence diretamente a `tenant_id` e obedece invariantes no banco. | Ownership e dados autoconsistentes permitem autorização, billing e cópia relacional posterior. |
| 3 | Todo endpoint resolve `tenant_id` opcional e valida alvo-clínica antes de acessar mídia. | Reusa a política existente sem confiar em tenant efetivo implícito. |
| 4 | Aceitar somente MP4, MP3, PDF, JPEG e PNG. | Permite validação fechada de assinatura/MIME, sem prometer segurança antimalware. |
| 5 | Lista e mutações omitem payload; download é attachment em endpoint separado. | Reduz serialização e execução/renderização acidental. |
| 6 | `usage_trigger` e `auto_suggest` são persistidos, mas não executados. | Preserva intenção autoral sem acoplar runtime multicanal. |
| 7 | Exclusão é definitiva na base ativa e transacional. | Lixeira/versionamento adicionariam estados não solicitados. |
| 8 | Billing é `SUM(size_bytes)` e o teto operacional usa a mesma soma sob lock. | Evita contador duplicado e fecha corridas entre uploads. |
| 9 | Paginação é keyset descendente com cursor opaco tenant-bound. | Evita drift de offset e torna a semântica concorrente explícita. |
| 10 | A clonagem apenas consumirá esta fundação em `ajuste-central-clonagem`. | Evita API interna sem consumidor enquanto mantém dados copiáveis. |
| 11 | Backup do repositório é sempre streaming comprimido e cifrado com `age`. | Evita plaintext durável e torna retenção/restore verificáveis sem pôr a chave privada na aplicação. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | `BYTEA` coloca mídia potencialmente sensível e autorizada de clínicas no PostgreSQL, WAL e backups. | High | Rollout de produção exige evidência de criptografia em repouso para banco/WAL/destino; `backup.sh` produz somente `.age`, restauração é testada e retenção é limitada. Exclusão ativa não apaga imediatamente backups cifrados retidos. |
| 2 | Conteúdo reconhecido por assinatura ainda pode ser malicioso. | High | Tratar todo ativo como download não confiável, attachment + `nosniff`, sem render/processamento inline; assinatura valida formato, não malware. |
| 3 | Cross-tenant por override indevido ou ID previsível. | High | Resolver papel/ownership antes de dados, validar alvo-clínica, filtrar `id AND tenant_id` e devolver 404 indistinguível para ativo. |
| 4 | Uploads simultâneos esgotam processo ou armazenamento. | High | Cap ASGI inclusive chunked, semáforos e teto agregado sob advisory lock transacional. |
| 5 | Parser multipart vulnerável fica exposto. | High | FastAPI/python-multipart corrigidos, Starlette resolvida >=1.0.1, locks regenerados, versões exatas assertadas e advisory/full-suite gates antes de registrar a rota. |
| 6 | Nome de arquivo injeta header/caminho. | Medium | Limites, rejeição CR/LF/NUL, nenhum uso como caminho, fallback ASCII e `filename*` codificado. |
| 7 | Catálogo muda durante paginação. | Medium | Keyset estrito com comportamento de insert/delete documentado e restart explícito para novos itens. |
| 8 | Falha de banco deixa estado parcial. | Medium | Transações explícitas com commit/rollback e testes de falha em upload/PATCH/DELETE. |

## Success Criteria

- [ ] Antes de registrar multipart, manifests usam FastAPI `>=0.142.4,<0.143.0` e `python-multipart>=0.0.32,<0.1.0`, sem pin direto de Starlette; locks foram regenerados, instalação sincronizada registra/asserta versões exatas e resolve Starlette `>=1.0.1`, e advisory scan não aponta vulnerabilidade conhecida aplicável.
- [ ] O salto de framework passa testes focados de mídia/auth/body cap, integração PostgreSQL, smoke startup/OpenAPI/health, `make quality` completo e `npm run typecheck`, `npm test`, `npm run build` no frontend.
- [ ] Upload de cada formato aceito persiste bytes, categoria, MIME, tamanho e SHA-256 exatos sob a clínica efetiva, e a migration rejeita violações de tamanho/payload, SHA e par categoria/MIME.
- [ ] Body cap pré-parser rejeita `Content-Length` e corpo chunked acima de 51.000.000; arquivo com 50.000.001 bytes falha sem linha parcial; limites de concorrência de upload/download são exercitados.
- [ ] MIME não permitido, assinatura inválida, combinação divergente, metadado inválido, cursor inválido, alvo inválido, ativo ausente, teto excedido e persistência falha retornam exatamente os status/códigos definidos.
- [ ] Todos os endpoints aceitam `tenant_id` opcional e chamam `resolve_effective_tenant` antes de mídia; testes cobrem API key e clinic-admin próprios/override negado, Builder own/owned/unowned, super-admin com clínica válida e alvo não-clínica, investor e papel não suportado.
- [ ] Clínica A não lista, lê, altera, baixa ou exclui ativo da clínica B; inexistência e cross-tenant são o mesmo 404.
- [ ] Listagem usa `(created_at,id)` descendente, default 25/máximo 100 e cursor opaco tenant-bound; testes demonstram páginas sem duplicata, inserção anterior invisível até restart e exclusão pulada. Lista e PATCH não carregam/retornam payload.
- [ ] Download devolve bytes/MIME exatos como attachment com `nosniff`, fallback ASCII e `filename*` seguro, e rejeita CR/LF/NUL nos nomes.
- [ ] Uploads concorrentes em sessões/réplicas não excedem 500.000.000 bytes por Builder (ou clínica independente); cap excedido não insere. Upload, PATCH e DELETE fazem commit no sucesso e rollback completo na falha.
- [ ] `scripts/backup.sh` falha fechado antes do dump sem `age`/recipient público, faz `pg_dump | gzip | age` para temporário restritivo, publica por rename atômico somente após pipeline completo e nunca deixa plaintext durável; retenção/delete abrangem somente `.age`, e logs não expõem chave/plaintext.
- [ ] Restore test decripta/restaura um backup em DB descartável e valida integridade; rollout de produção anexa evidência de criptografia em repouso para PostgreSQL/WAL/backups. Exclusão remove a linha ativa e reduz cap/billing, mas backups cifrados retidos só somem pela retenção.
- [ ] `storage.media.used_bytes` soma exatamente `size_bytes` de todos os ativos das clínicas pertencentes ao Builder e preserva os demais cálculos de limite/overage.
- [ ] A página Mídias permite upload, catálogo paginado, download, edição e exclusão em PT/EN; encaminha `viewingClinicId` em todas as operações selecionáveis e, ao trocar clínica, aborta requests e limpa estado/cursor/cache da anterior.
- [ ] O modelo e seus invariantes suportam cópia relacional futura, mas esta entrega não contém helper nem integração de clone; essa integração pertence a `ajuste-central-clonagem`.
- [ ] Testes unitários, API, PostgreSQL/migration, billing e frontend cobrem sucesso, limites, assinaturas, autorização, concorrência, transações e regressões.

## Files to Plan

- `pyproject.toml`, `requirements.txt`, `poetry.lock` e outros locks Python versionados: ranges seguros, resolução reproduzível e scanner de advisories.
- `src/api/`, `src/models/`, `src/repositories/`, `src/services/` e migration Alembic correspondentes: catálogo, autorização, limites, invariantes, billing e transações.
- `frontend/artifacts/neurostack/`: catálogo tenant-keyed, mensagens PT/EN e testes de troca/abort de clínica.
- `scripts/backup.sh`, `env.example` e runbook operacional: recipient público secret-managed, pipeline age fail-closed, retenção cifrada, restore e pré-requisitos de rollout.
- `tests/`, incluindo cobertura de script/restore em PostgreSQL descartável: gates focados, integração, concorrência e regressão completa.

## Next Step

After an independent design review returns SHIP, persist the evidence below and verify its content digest before running `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `241efd3bd4b97bea8582e3d4196f53474d2a3396876c0f4cba8e3309981a6311`
- **Reviewer:** /root/media_design_review
- **Reviewed at:** 2026-10-07T23:13:18.000Z
<!-- genie-design-review:end -->
