# Draft: Catálogo e armazenamento de Mídias

| Field | Value |
|-------|-------|
| **Slug** | `media-assets-catalog` |
| **Date** | 2026-10-07 |
| **WRS** | 100/100 |

```text
WRS: ██████████ 100/100
 Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅
```

## Problem

A tela de Mídias anuncia vídeos, áudios, PDFs e imagens, mas hoje é um mock: não há upload, catálogo, ownership, persistência, download nem medição real. O objetivo é entregar o catálogo tenant-scoped e uma fundação relacional que a clonagem opcional possa consumir depois, sem implementar clonagem ou runtime de entrega neste wish.

## Scope

### IN

- Modelo/migração `media_assets` tenant-scoped com bytes, metadados limitados, par tipo/MIME canônico, tamanho, SHA-256 e invariantes de banco.
- Upgrade antes de multipart: FastAPI `>=0.142.4,<0.143.0`, `python-multipart>=0.0.32,<0.1.0`, sem pin direto de Starlette; locks regenerados e runtime com versões exatas registradas/assertadas, inclusive Starlette `>=1.0.1`.
- Upload MP4, MP3, PDF, JPEG e PNG até 50.000.000 bytes, com body cap ASGI pré-parser inclusive para chunked, validação de assinatura/MIME e limites de concorrência.
- Listagem keyset sem payload, download autenticado, edição de metadados e exclusão transacional.
- `tenant_id` opcional nos cinco endpoints, sempre resolvido por `resolve_effective_tenant` antes de mídia e seguido de validação de alvo-clínica ativa.
- Central de Mídias real no frontend, usando `viewingClinicId`, com cancelamento e limpeza de estado na troca de clínica.
- Billing por `SUM(size_bytes)` nas clínicas do Builder e teto operacional agregado de 500.000.000 bytes protegido contra uploads concorrentes.
- Backup do repositório cifrado por streaming com `age`, restore testado e rollout de produção condicionado a criptografia em repouso de PostgreSQL/WAL/backups.

### OUT

- Envio manual/automático por canais, classificação de intenção ou execução de `usage_trigger`/`auto_suggest`.
- Transcodificação, thumbnails, antivírus externo, OCR, object storage, CDN, deduplicação, versionamento ou restauração.
- Novos preços, créditos ou cotas comerciais dinâmicas.
- Helper/construtor ou integração de clone; `ajuste-central-clonagem` entregará a cópia relacional depois deste wish.

## Contract

Todos os endpoints (`POST`, lista, download, `PATCH`, `DELETE`) aceitam `tenant_id` opcional. API key e clinic-admin ficam na própria clínica; Builder seleciona somente clínica que possui; super-admin seleciona qualquer clínica válida; investor e papéis ausentes/não suportados são negados. `resolve_effective_tenant` roda antes de acesso a mídia, e alvo inexistente, inativo ou não-clínica é 403. Consultas por ativo usam sempre `id AND tenant_id`, com o mesmo 404 para ausente e cross-tenant.

A lista ordena `(created_at DESC,id DESC)`, default 25/máximo 100, e retorna cursor opaco assinado tenant-bound. Comparação estrita `<` impede duplicata: inserts novos ficam para um restart e deletes são pulados; não há snapshot/total prometido. A UI chaveia cache/cursor pelo `viewingClinicId`, encaminha esse tenant nas cinco operações de Builder/super-admin e, na troca, aborta requests/uploads e limpa arquivo, modal, edição, cursor e dados anteriores.

O middleware conta no máximo 51.000.000 bytes antes do multipart, por `Content-Length` e pelo stream ASGI inclusive chunked; o arquivo continua limitado exatamente a 50.000.000. Há no máximo 4 uploads e 8 downloads simultâneos por processo. Após validar bytes, o upload toma advisory lock transacional por Builder (ou clínica independente), recalcula a soma e só insere se ficar em até 500.000.000 bytes. Isso é proteção atual contra exaustão de RAM/conexões/banco/WAL/backups, não apenas relatório de overage.

Upload, PATCH e DELETE usam `flush`/`commit` explícitos e `rollback` em qualquer falha. Erros estáveis: 413 `media_body_too_large`/`media_size_exceeded`; 415 `media_type_unsupported`/`media_signature_invalid`/`media_type_mismatch`; 422 `media_metadata_invalid`; 400 `media_cursor_invalid`; 403 `media_target_forbidden` ou resolver; 404 `media_asset_not_found`; 409 `media_storage_cap_exceeded`; 503 `media_concurrency_exceeded`; 500 `media_persistence_failed`.

O banco exige `octet_length(payload)=size_bytes`, `0<size_bytes<=50000000`, SHA binário de 32 bytes e pares categoria/MIME fechados. `display_name` é 1–200, `original_filename` 1–255 e `usage_trigger` opcional até 500; NUL/CR/LF são rejeitados. Download é attachment não confiável com MIME canônico, `nosniff`, fallback ASCII seguro e `filename*` UTF-8 percent-encoded. Assinatura reconhece formato, não comprova ausência de malware.

`scripts/backup.sh` exige `age` e recipient público vindo de ambiente/secret manager antes de executar dump. Com `pipefail`, transmite `pg_dump | gzip | age` a um temporário cifrado de permissões restritas e só o renomeia atomicamente para `.sql.gz.age` quando todo o pipeline conclui; falha remove o temporário e jamais cria plaintext durável. Chave privada não entra no repo, app container ou logs; retenção/delete operam somente em `.age`. Teste isolado decripta e restaura em DB descartável, valida conteúdo e limpa temporários. Exclusão de mídia é imediata na base ativa, mas backups cifrados retidos permanecem até a expiração documentada.

## Decisions

1. Persistir payload em `BYTEA`; os limites operacionais e a observação de banco/WAL/backups definem quando migrar para object storage.
2. Usar ownership direto por `tenant_id` e invariantes relacionais; isso basta para cópia posterior sem código de clone sem consumidor.
3. Derivar categoria/MIME do conteúdo; o cliente não escolhe a verdade persistida.
4. Medir billing pela mesma soma lógica usada sob lock no teto operacional, sem contador duplicado.
5. Manter `usage_trigger`/`auto_suggest` apenas como metadados; delivery runtime e clonagem ficam fora.
6. Excluir da base ativa em transação; backups seguem sua retenção e não são apagados imediatamente.
7. Não pinar Starlette diretamente: FastAPI possui a compatibilidade, enquanto lock/runtime devem provar Starlette >=1.0.1 e registrar a versão exata.
8. Produzir backup apenas cifrado por `age`; criptografia em repouso de banco/WAL/destino é gate de rollout, não suposição operacional.

## Security & Operations

- PostgreSQL, WAL e backups contêm mídia autorizada potencialmente sensível. Produção exige evidência de criptografia em repouso, acesso mínimo, backup `.age`, restore comprovado e retenção limitada/documentada.
- Signature sniffing reduz spoofing, mas ativos permanecem anexos não confiáveis, sem render/processamento inline.
- Cap pré-parser, semáforos e lock agregado fecham abuso por corpo chunked e concorrência entre réplicas.
- Headers de filename usam valores gerados/codificados e nunca aceitam controles; logs omitem bytes, nomes autorais, gatilhos e hashes completos.

## Criteria

- Manifests usam FastAPI `>=0.142.4,<0.143.0` e python-multipart `>=0.0.32,<0.1.0`, sem pin direto Starlette; locks/runtime registram versões exatas, Starlette resolve >=1.0.1 e advisory scan passa.
- O framework jump passa testes focados, integração PostgreSQL, startup/OpenAPI/health, `make quality` completo e frontend typecheck/test/build.
- Formatos válidos persistem bytes/hash/tamanho exatos; body/arquivo, tipo, assinatura, mismatch, metadata e invariantes inválidos falham sem linha parcial.
- Testes de todos os papéis provam resolução anterior ao acesso, seleção autorizada e isolamento não enumerável, inclusive alvo não-clínica.
- Paginação cumpre ordem/cursor/default/máximo e semântica definida para insert/delete concorrentes; lista/PATCH não carregam bytes.
- Download devolve conteúdo exato com headers seguros; payload continua não confiável e não é renderizado inline.
- Concorrência de endpoints é limitada; uploads concorrentes não excedem o teto agregado e o erro de cap não persiste linha.
- Mutação faz commit somente no sucesso e rollback completo na falha, com status/código estáveis para todas as classes definidas.
- `backup.sh` falha fechado sem age/recipient, não cria plaintext durável, publica `.age` atomicamente somente após pipeline completo e só retém/remove cifrados; restore em DB descartável e evidência de criptografia em repouso precedem rollout.
- Billing soma exatamente todas as mídias das clínicas do Builder; exclusão reduz base ativa, cap e billing, sem prometer apagar backups cifrados retidos antes da expiração.
- UI PT/EN oferece upload/lista/paginação/download/edição/exclusão e troca de `viewingClinicId` sem request nem estado antigo sobreviver.
- Modelo permanece copiável relacionalmente, mas integração/helper de clone só entra em `ajuste-central-clonagem`.

## Files to Plan

- `pyproject.toml`, `requirements.txt`, `poetry.lock` e demais locks Python versionados.
- Backend/migration e frontend da Central de Mídias, com seus testes focados, PostgreSQL e regressão completa.
- `scripts/backup.sh`, `env.example`, runbook e testes de backup/restore descartável.
