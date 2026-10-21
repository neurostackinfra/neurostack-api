# NeuroStack Database Migrations

Este diretório contém as migrations de banco de dados gerenciadas pelo Alembic.

## Estrutura

```
alembic/
├── env.py              # Configuração do ambiente Alembic
├── script.py.mako      # Template para novas migrations
├── README.md           # Este arquivo
├── frozen/             # DDL congelada que um downgrade restaura (dados, não migrations)
│   └── 036_downgrade_restores_035.json
└── versions/           # Arquivos de migration
    └── *.py
```

`frozen/` guarda a DDL extraída do catálogo de um banco recém-criado na
revisão anterior (`pg_get_functiondef`, `pg_get_triggerdef`,
`pg_get_constraintdef` e as colunas), para que um downgrade restaure
exatamente o que a migration removeu, sem DDL digitada à mão. O Alembic não
lê esse diretório; cada migration lê o seu arquivo e confere o SHA-256 fixado
nela antes de executar. Para regenerar (somente a partir de um banco novo na
revisão de origem): `python -m scripts.alembic_frozen_ddl --database-url ...`,
e atualizar o SHA-256 da migration; o teste de fidelidade
(`tests/integration/test_migration_036_postgres.py`) compara o arquivo com o
catálogo de um banco novo byte a byte.

## Comandos Principais

### Verificar status atual

```bash
# Ver revision atual do banco
alembic current

# Ver histórico de migrations
alembic history --verbose
```

### Criar nova migration

```bash
# Autogenerate baseado nos modelos
alembic revision --autogenerate -m "Descrição da mudança"

# Migration manual (vazia)
alembic revision -m "Descrição da mudança"
```

### Executar migrations

Migre cada ambiente para a revisão aprovada para o ambiente (`alembic upgrade <revision>`;
`head` só num banco local de desenvolvimento, via `make db-migrate`, que roda
`scripts/guard_alembic_upgrade_head.py` antes e recusa qualquer outro banco).

```bash
# Banco local de desenvolvimento: upgrade para a última versão (com o guard)
make db-migrate

# Upgrade para uma versão específica (a aprovada para o ambiente)
alembic upgrade <revision_id>

# Upgrade um passo
alembic upgrade +1
```

### Reverter migrations

```bash
# Downgrade um passo
alembic downgrade -1

# Downgrade para uma versão específica
alembic downgrade <revision_id>

# Reverter todas as migrations
alembic downgrade base
```

### Comandos úteis

```bash
# Ver SQL que seria executado (sem executar)
alembic upgrade head --sql

# Mostrar diferenças entre modelos e banco
alembic check

# Gerar script SQL de upgrade
alembic upgrade head --sql > migration.sql
```

## Fluxo de Trabalho

1. **Modificar os modelos** em `src/models/`
2. **Gerar migration** com autogenerate:
   ```bash
   alembic revision --autogenerate -m "Add new feature"
   ```
3. **Revisar o arquivo gerado** em `versions/`
4. **Testar localmente** (banco local de desenvolvimento):
   ```bash
   make db-migrate
   ```
5. **Commit** a migration junto com as mudanças do modelo

## Boas Práticas

1. **Sempre revise migrations autogeneradas** - o autogenerate não captura todas as mudanças
2. **Use mensagens descritivas** - facilita entender o histórico
3. **Teste upgrade E downgrade** - garanta que é possível reverter
4. **Nunca modifique migrations já aplicadas em produção** - crie novas
5. **Mantenha migrations pequenas** - uma mudança por migration quando possível

## Modelos Atuais

| Modelo | Tabela | Descrição |
|--------|--------|-----------|
| `Tenant` | `tenants` | Builders e clínicas |
| `Session` | `sessions` | Sessões de conversação |
| `Event` | `events` | Event Store (append-only) |
| `Policy` | `policies` | Regras de comportamento |
| `KnowledgeDocument` | `knowledge_documents` | Base de conhecimento RAG |
| `HandoffRecord` | `handoff_records` | Registros de handoff |

## Troubleshooting

### Erro de conexão

Verifique se o PostgreSQL está rodando e as credenciais em `.env`:

```bash
docker-compose up -d postgres
```

### Migration conflitante

Se houver múltiplas heads:

```bash
# Ver branches
alembic heads

# Criar migration de merge
alembic merge heads -m "Merge branches"
```

### Tabelas já existem

Se o banco foi criado manualmente (via `init-db.sql`):

```bash
# Marcar como aplicado sem executar
alembic stamp head
```

---

*Documentação gerada para NeuroStack MVP - Dezembro 2025*

