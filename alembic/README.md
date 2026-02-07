# NeuroStack Database Migrations

Este diretório contém as migrations de banco de dados gerenciadas pelo Alembic.

## Estrutura

```
alembic/
├── env.py              # Configuração do ambiente Alembic
├── script.py.mako      # Template para novas migrations
├── README.md           # Este arquivo
└── versions/           # Arquivos de migration
    └── *.py
```

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

```bash
# Upgrade para a última versão
alembic upgrade head

# Upgrade para uma versão específica
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
4. **Testar localmente**:
   ```bash
   alembic upgrade head
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

