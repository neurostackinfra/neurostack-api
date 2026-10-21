# NeuroStack - Guia de Setup Local

Guia completo para rodar o NeuroStack (backend + frontend) na sua maquina.

---

## Pre-requisitos

| Ferramenta | Versao minima | Verificar |
|-----------|---------------|-----------|
| Docker + Docker Compose | 24+ | `docker --version` |
| Python | 3.10+ | `python3 --version` |
| Node.js | 18+ | `node --version` |
| pnpm | 8+ | `pnpm --version` |
| Git | qualquer | `git --version` |

> **Python 3.9 nao funciona** — o projeto usa syntax `str | None` que requer 3.10+.
> Se voce so tem 3.9, instale com `brew install python@3.12` ou pyenv.

### Instalar pnpm (se nao tiver)

```bash
npm install -g pnpm
```

---

## 1. Clonar o repositorio

```bash
git clone <url-do-repo> neurostack
cd neurostack/back-end/neurostack-api
```

---

## 2. Subir a infraestrutura (PostgreSQL, Redis, Qdrant)

O `docker-compose.yml` ja tem tudo configurado. Ele e so de desenvolvimento:
todo comando `docker compose` sobre ele aborta sem `NEUROSTACK_DEV_STACK=1` no
`.env` local (qualquer valor nao vazio habilita o arquivo, ate `0`; por isso
nunca defina essa variavel num host de producao). Crie o `.env`
antes (`cp .env.example .env`, ver secao 3), que ja traz essa linha e os
defaults do Postgres:

```bash
docker compose up -d postgres redis qdrant
```

Isso sobe:
- **PostgreSQL 16** na porta `5434` (mapeada de 5432 interna)
- **Redis 7** na porta `6380` (mapeada de 6379 interna)
- **Qdrant** nas portas `6333` (HTTP) e `6334` (gRPC)

Verificar se estao rodando:

```bash
docker compose ps
```

Os tres servicos devem estar `healthy` ou `running`.

---

## 3. Configurar variaveis de ambiente

```bash
cp .env.example .env
```

Edite o `.env` e preencha a API key do LLM (obrigatorio para respostas do chat):

```env
# Ja vem preenchido com defaults corretos para Docker local:
POSTGRES_USER=neurostack
POSTGRES_PASSWORD=neurostack
POSTGRES_DB=neurostack
DATABASE_URL=postgresql+asyncpg://neurostack:neurostack@localhost:5434/neurostack
REDIS_URL=redis://localhost:6380/0
QDRANT_HOST=localhost
QDRANT_PORT=6333

# Preencher com sua chave:
LLM_BASE_URL=https://api.neuralake.cloud/v1
LLM_API_KEY=<sua-api-key-aqui>
LLM_MODEL=o4-mini
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=1024
```

> **LLM_API_KEY** e obrigatorio para o chat gerar respostas. Sem ela, o pipeline funciona mas retorna `suggested_response: null`.

> O `LLM_BASE_URL` aceita qualquer API OpenAI-compatible (OpenRouter, Together, etc). Basta trocar URL, key e model.

---

## 4. Instalar dependencias Python

```bash
# Criar virtualenv (recomendado)
python3 -m venv .venv
source .venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt
```

O requirements instala `packages/neurostack-core` em modo editavel. O caminho
local deve permanecer no checkout; nao copie apenas `src/` para outro diretorio.

---

## 5. Rodar migrations do banco

No banco local de desenvolvimento:

```bash
make db-migrate
# sem Poetry: python scripts/guard_alembic_upgrade_head.py && alembic upgrade head
```

`make db-migrate` roda `scripts/guard_alembic_upgrade_head.py` antes do Alembic, e o
guard recusa qualquer banco que não seja de desenvolvimento local. Qualquer outro
ambiente é migrado para a revisão aprovada para o ambiente (`alembic upgrade <revisão>`;
`head` só num banco local de desenvolvimento, via `make db-migrate`).

Isso cria todas as tabelas: `tenants`, `events`, `sessions`, `clinic_configs`, `users`, etc.

Para verificar:

```bash
alembic current
# Deve mostrar a ultima revisao como head
```

---

## 6. Popular dados de demonstracao (seed)

```bash
python scripts/seed_demo.py
```

Isso cria:
- 1 tenant "Clinica Demo"
- 3 usuarios de teste
- 1 configuracao de clinica
- 1 sessao + 5 eventos de exemplo

### Usuarios de teste

| Email | Senha | Role |
|-------|-------|------|
| `admin@demo.com` | `demo123` | clinic_admin |
| `super@demo.com` | `demo123` | super_admin |
| `investor@demo.com` | `demo123` | investor |

---

## 7. Subir o backend

```bash
uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
```

> Usamos porta `8001` porque `8000` pode estar ocupada por outros containers Docker.

Verificar:

```bash
curl http://localhost:8001/health
# {"status":"ok"}
```

Documentacao interativa: http://localhost:8001/docs

---

## 8. Instalar e subir o frontend

Em **outro terminal**:

```bash
cd frontend

# Instalar dependencias (primeira vez)
pnpm install

# Subir dev server
cd artifacts/neurostack
PORT=5173 BASE_PATH="/" pnpm dev
```

O frontend roda em http://localhost:5173

> **Nota sobre pnpm-workspace.yaml:** O arquivo original foi clonado do Replit (Linux). Os overrides de plataforma foram removidos para funcionar no macOS ARM. Se voce esta no Linux, nao precisa mudar nada.

---

## 9. Testar o fluxo completo

1. Acesse http://localhost:5173
2. Faca login com `admin@demo.com` / `demo123`
3. Navegue ate o **Chat** e envie uma mensagem
4. A resposta vem do pipeline cognitivo real (SRM + LLM)
5. O **trace** aparece no painel ao lado mostrando a decisao do SRM

### Outras paginas

| Pagina | O que faz |
|--------|-----------|
| **Dashboard** | KPIs com dados reais de `/v1/traces/stats` |
| **Traces** | Lista de decisoes do SRM com filtros e paginacao |
| **Configuracao** | CRUD da config da clinica (persiste no banco) |

---

## Testar via curl (sem frontend)

```bash
# Login
TOKEN=$(curl -s -X POST http://localhost:8001/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@demo.com","password":"demo123"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

echo "Token: $TOKEN"

# Criar sessao
SID=$(curl -s -X POST http://localhost:8001/v1/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"d0000000-0000-0000-0000-000000000001","channel":"web"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

echo "Session: $SID"

# Enviar mensagem pelo pipeline cognitivo
curl -s -X POST http://localhost:8001/v1/events \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"tenant_id\":\"d0000000-0000-0000-0000-000000000001\",\"session_id\":\"$SID\",\"event_type\":\"message.in\",\"payload\":{\"content\":\"Ola, gostaria de agendar uma consulta\",\"channel\":\"web\"}}" \
  | python3 -m json.tool

# Listar traces
curl -s http://localhost:8001/v1/traces \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool

# Ver stats
curl -s http://localhost:8001/v1/traces/stats \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool

# Ver config da clinica
curl -s http://localhost:8001/v1/config \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

---

## Arquitetura

```
Requisicao do usuario
       |
       v
  POST /v1/events
       |
       v
  +----+----+
  |  SRM    |  Classifica intencao, funil, seguranca
  +---------+
       |
       v
  +----+----+
  | Policy  |  Aplica regras e restricoes
  +---------+
       |
       v
  +----+----+
  |  CTX    |  Monta contexto (historico, sessao)
  +---------+
       |
       v
  +----+----+
  | Prompt  |  Gera system prompt + instrucoes
  +---------+
       |
       v
  +----+----+
  |  LLM    |  Chama API externa (OpenAI-compatible)
  +---------+
       |
       v
  EventResponse com:
  - decision (SRM)
  - prompt (sistema + instrucoes)
  - suggested_response (resposta do LLM)
  - context (historico)
```

---

## Endpoints principais

| Metodo | Endpoint | Descricao | Auth |
|--------|----------|-----------|------|
| POST | `/v1/auth/login` | Login (retorna JWT) | Nenhum |
| POST | `/v1/sessions` | Criar sessao de conversa | JWT |
| POST | `/v1/events` | Enviar mensagem pelo pipeline | JWT ou API Key |
| GET | `/v1/traces` | Listar traces com paginacao | JWT |
| GET | `/v1/traces/{id}` | Detalhe de um trace | JWT |
| GET | `/v1/traces/stats` | Estatisticas agregadas | JWT |
| GET | `/v1/traces/export` | Exportar traces como CSV | JWT |
| GET | `/v1/config` | Ler config da clinica | JWT |
| PATCH | `/v1/config` | Atualizar config da clinica | JWT |
| GET | `/health` | Health check | Nenhum |
| GET | `/docs` | Swagger UI | Nenhum |

---

## Troubleshooting

### `Address already in use` na porta 8000/8001

```bash
lsof -i :8001 -P -n
# Kill o processo ou use outra porta
```

### `Cannot find module @rollup/rollup-darwin-arm64`

O `pnpm-workspace.yaml` tinha overrides do Replit excluindo binarios macOS. Se isso acontecer:

```bash
cd frontend
rm -rf node_modules pnpm-lock.yaml
pnpm install
```

### Alembic falha com erro de conexao

Verifique se o PostgreSQL esta rodando:

```bash
docker compose ps postgres
# Deve estar healthy
```

E se a URL no `.env` aponta para a porta correta (`5434` para Docker local).

### Chat retorna "No response generated"

A `LLM_API_KEY` nao esta configurada no `.env`. O pipeline funciona sem LLM, mas `suggested_response` fica `null`.

### Python 3.9 `SyntaxError: str | None`

O projeto requer Python 3.10+. Instale via:

```bash
brew install python@3.12
# ou
pyenv install 3.12
```

---

## Parar tudo

```bash
# Frontend: Ctrl+C no terminal
# Backend: Ctrl+C no terminal
# Infra:
docker compose down
```

Para remover volumes (apaga dados do banco):

```bash
docker compose down -v
```
