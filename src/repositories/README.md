# NeuroStack - Repository Pattern Implementation

Este documento descreve a implementação do padrão Repository para acesso a dados no NeuroStack.

## Visão Geral

O padrão Repository foi implementado para abstrair o acesso ao banco de dados PostgreSQL, fornecendo uma camada de acesso a dados consistente e testável.

## Estrutura

```
src/repositories/
├── __init__.py          # Exports públicos
├── base.py              # BaseRepository (classe base)
├── database.py          # Gerenciamento de sessão do banco
├── tenant.py            # TenantRepository
├── session.py           # SessionRepository
├── event.py             # EventRepository
├── policy.py            # PolicyRepository
├── knowledge.py         # KnowledgeDocumentRepository
└── handoff.py           # HandoffRecordRepository
```

## BaseRepository

A classe `BaseRepository` fornece operações CRUD genéricas:

### Métodos de Criação
- `create(**kwargs)` - Criar um registro
- `create_many(items)` - Criar múltiplos registros

### Métodos de Leitura
- `get_by_id(id, load_relationships)` - Buscar por ID
- `get_all(skip, limit, load_relationships)` - Listar todos
- `count()` - Contar registros
- `exists(id)` - Verificar existência
- `find_one(**filters)` - Buscar um registro
- `find_many(skip, limit, order_by, **filters)` - Buscar múltiplos

### Métodos de Atualização
- `update(id, **kwargs)` - Atualizar por ID
- `update_many(filter_criteria, **kwargs)` - Atualizar múltiplos

### Métodos de Exclusão
- `delete(id)` - Excluir por ID
- `delete_many(filter_criteria)` - Excluir múltiplos

## Repositories Específicos

### TenantRepository

**Métodos adicionais:**
- `find_by_slug(slug)` - Buscar tenant por slug
- `find_by_api_key_hash(api_key_hash)` - Buscar por hash da API key
- `find_active(skip, limit)` - Listar tenants ativos
- `count_active()` - Contar tenants ativos

**Uso:**
```python
from src.repositories import TenantRepository, get_db_session

async with get_db_session() as session:
    repo = TenantRepository(session)
    tenant = await repo.find_by_slug("clinica-exemplo")
```

### SessionRepository

**Métodos adicionais:**
- `find_by_tenant(tenant_id, active_only, skip, limit)` - Buscar por tenant
- `find_active(tenant_id, skip, limit)` - Buscar sessões ativas
- `find_by_user(tenant_id, user_id, active_only, skip, limit)` - Buscar por usuário
- `find_by_channel(tenant_id, channel, active_only, skip, limit)` - Buscar por canal
- `find_recently_active(tenant_id, hours, skip, limit)` - Buscar recentemente ativas
- `count_by_tenant(tenant_id, active_only)` - Contar por tenant
- `end_session(session_id)` - Encerrar sessão
- `update_activity(session_id)` - Atualizar atividade

### EventRepository

**Métodos adicionais:**
- `find_by_session(tenant_id, session_id, skip, limit, event_type)` - Buscar por sessão
- `find_by_tenant(tenant_id, skip, limit, event_type, start_time, end_time)` - Buscar por tenant
- `find_by_type(tenant_id, event_type, skip, limit, start_time, end_time)` - Buscar por tipo
- `find_recent(tenant_id, hours, event_type, limit)` - Buscar recentes
- `count_by_session(tenant_id, session_id, event_type)` - Contar por sessão
- `count_by_type(tenant_id, event_type, start_time, end_time)` - Contar por tipo

**Nota:** Events são append-only. Não há métodos de update/delete.

### PolicyRepository

**Métodos adicionais:**
- `find_by_tenant(tenant_id, active_only, policy_type, skip, limit)` - Buscar por tenant
- `find_global(active_only, policy_type, skip, limit)` - Buscar políticas globais
- `find_active(tenant_id, policy_type, skip, limit)` - Buscar ativas
- `find_by_type(policy_type, tenant_id, active_only, skip, limit)` - Buscar por tipo
- `count_by_tenant(tenant_id, active_only)` - Contar por tenant
- `soft_delete(policy_id, tenant_id)` - Soft delete (marca como inativa)

### KnowledgeDocumentRepository

**Métodos adicionais:**
- `get_by_id_and_tenant(document_id, tenant_id)` - Buscar por ID dentro do tenant (outro tenant = `None`)
- `delete_by_id_and_tenant(document_id, tenant_id)` - Remover por ID dentro do tenant (outro tenant = `False`)
- `find_by_tenant(tenant_id, indexed_only, skip, limit)` - Buscar por tenant
- `find_indexed(tenant_id, skip, limit)` - Buscar indexados
- `find_not_indexed(tenant_id, skip, limit)` - Buscar não indexados
- `find_by_category(tenant_id, category, indexed_only, skip, limit)` - Buscar por categoria
- `count_by_tenant(tenant_id, indexed_only)` - Contar por tenant
- `mark_indexed(document_id, vector_id, chunk_count)` - Marcar como indexado
- `mark_index_failed(document_id, error)` - Marcar indexação como falha

### HandoffRecordRepository

**Métodos adicionais:**
- `find_by_tenant(tenant_id, status, skip, limit)` - Buscar por tenant
- `find_by_session(tenant_id, session_id, status, skip, limit)` - Buscar por sessão
- `find_active(tenant_id, skip, limit)` - Buscar ativos
- `find_by_status(tenant_id, status, skip, limit)` - Buscar por status
- `find_by_urgency(tenant_id, urgency, active_only, skip, limit)` - Buscar por urgência
- `count_by_tenant(tenant_id, status)` - Contar por tenant
- `accept_handoff(handoff_id, agent_id)` - Aceitar handoff
- `complete_handoff(handoff_id, notes)` - Completar handoff
- `cancel_handoff(handoff_id, notes)` - Cancelar handoff

## Gerenciamento de Sessão

### Inicialização

```python
from src.repositories import init_database, close_database

# No startup da aplicação
await init_database()

# No shutdown da aplicação
await close_database()
```

### Uso com FastAPI

```python
from fastapi import Depends
from src.repositories import get_db_session_dependency, TenantRepository

@app.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: UUID,
    session: AsyncSession = Depends(get_db_session_dependency),
):
    repo = TenantRepository(session)
    tenant = await repo.get_by_id(tenant_id)
    return tenant
```

### Uso Manual

```python
from src.repositories import get_db_session, TenantRepository

async with get_db_session() as session:
    repo = TenantRepository(session)
    tenant = await repo.get_by_id(tenant_id)
    await session.commit()
```

## Integração com Rotas Existentes

As rotas atuais têm TODOs indicando que precisam ser implementadas. Exemplo de como integrar:

### Antes (TODO):
```python
@router.post("/sessions")
async def create_session(request: SessionCreateRequest):
    # TODO: Implement session creation
    return SessionResponse(...)
```

### Depois (com Repository):
```python
from src.repositories import SessionRepository, get_db_session_dependency

@router.post("/sessions")
async def create_session(
    request: SessionCreateRequest,
    session: AsyncSession = Depends(get_db_session_dependency),
):
    repo = SessionRepository(session)
    new_session = await repo.create(
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        channel=request.channel,
        cognitive_state={},
        session_metadata=request.metadata or {},
    )
    return SessionResponse.from_orm(new_session)
```

## Métodos Não Implementados

### TenantRepository
✅ Todos os métodos necessários implementados

### SessionRepository
✅ Todos os métodos necessários implementados

### EventRepository
✅ Todos os métodos necessários implementados

### PolicyRepository
✅ Todos os métodos necessários implementados

### KnowledgeDocumentRepository
✅ Todos os métodos necessários implementados

### HandoffRecordRepository
✅ Todos os métodos necessários implementados

## Próximos Passos

1. **Integração com Rotas**: Atualizar as rotas em `src/api/routes/` para usar os repositories
2. **Testes**: Criar testes unitários para cada repository
3. **Inicialização**: Adicionar `init_database()` no startup da aplicação (`src/api/main.py`)
4. **Migração do PolicyStore**: Migrar `PolicyStore` in-memory para usar `PolicyRepository`
5. **Documentação**: Adicionar exemplos de uso em cada rota

## Notas de Implementação

- Todos os repositories usam `AsyncSession` do SQLAlchemy
- Métodos de busca suportam paginação via `skip` e `limit`
- Ordenação é suportada via parâmetro `order_by` (use `-` prefix para DESC)
- Relacionamentos podem ser carregados via `load_relationships`
- Eventos são append-only (sem update/delete)
- Políticas usam soft delete (marca como inativa)

## Exemplo Completo

```python
from uuid import UUID
from src.repositories import (
    get_db_session,
    TenantRepository,
    SessionRepository,
    EventRepository,
)

async def example_usage():
    async with get_db_session() as session:
        # Buscar tenant
        tenant_repo = TenantRepository(session)
        tenant = await tenant_repo.find_by_slug("clinica-exemplo")
        
        if not tenant:
            raise ValueError("Tenant not found")
        
        # Criar sessão
        session_repo = SessionRepository(session)
        new_session = await session_repo.create(
            tenant_id=tenant.id,
            user_id="user-123",
            channel="whatsapp",
            cognitive_state={},
        )
        
        # Criar evento
        event_repo = EventRepository(session)
        event = await event_repo.create(
            tenant_id=tenant.id,
            session_id=new_session.id,
            event_type="session.start",
            event_data={"channel": "whatsapp"},
        )
        
        # Commit transação
        await session.commit()
        
        return new_session
```

---

*Documentação gerada para NeuroStack MVP - Dezembro 2025*
