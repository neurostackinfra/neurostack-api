# NeuroStack API

**Cognitive Infrastructure for AI Agents in Healthcare**

NeuroStack is a pluggable cognitive infrastructure that transforms generic AI agents into agents that reason, maintain context, follow rules, and generate structured data.

---

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Builders / Agent Platforms                    │
│         (Zaia, Botpress, Chatbase, n8n, AIRIA)                  │
└─────────────────────────────┬───────────────────────────────────┘
                              │ Events/Messages
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                  NeuroStack - Cognitive Infrastructure           │
│  ┌─────────────┐  ┌──────────────────┐  ┌─────────────────┐    │
│  │ API Layer   │→ │  Flow Manager    │→ │ Policy Engine   │    │
│  └─────────────┘  └──────────────────┘  └─────────────────┘    │
│         ↓                  ↓                      ↓             │
│  ┌─────────────┐  ┌──────────────────┐  ┌─────────────────┐    │
│  │    SRM      │← │      CTX         │→ │ Prompt Engine   │    │
│  │ (Reasoning) │  │  (Orchestrator)  │  │                 │    │
│  └─────────────┘  └──────────────────┘  └─────────────────┘    │
│         ↓                  ↓                      ↓             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Memory Architecture 2.0                     │   │
│  │  [OLTP: State] → [Event Store] → [OLAP] → [Training Lake]│   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- Poetry (recommended) or pip

### Development Setup

1. **Clone the repository**
   ```bash
   cd neurostack-api
   ```

2. **Copy environment file**
   ```bash
   cp env.example .env
   ```

3. **Start infrastructure (Redis, PostgreSQL, Qdrant)**
   ```bash
   docker compose up -d postgres redis qdrant
   ```

4. **Install dependencies**
   ```bash
   # Using Poetry (recommended)
   poetry install

   # Or using pip
   pip install -r requirements-dev.txt
   ```

5. **Run the API**
   ```bash
   # Using Poetry
   poetry run uvicorn src.api.main:app --reload

   # Or directly
   uvicorn src.api.main:app --reload
   ```

6. **Access the API**
   - API: http://localhost:8000
   - Docs: http://localhost:8000/docs
   - Health: http://localhost:8000/health

### Using Docker Compose (Full Stack)

```bash
# Start all services
docker compose up -d

# Check logs
docker compose logs -f api

# Stop all services
docker compose down
```

---

## 📚 API Endpoints

### Health
- `GET /health` - Health check
- `GET /ready` - Readiness check
- `GET /live` - Liveness probe

### Events (v1)
- `POST /v1/events` - Process an event/message

### Sessions (v1)
- `POST /v1/sessions` - Create a session
- `GET /v1/sessions/{id}` - Get session details
- `GET /v1/sessions` - List sessions
- `DELETE /v1/sessions/{id}` - End session

### Policies (v1)
- `POST /v1/policies` - Create a policy
- `GET /v1/policies/{id}` - Get policy details
- `GET /v1/policies` - List policies
- `PATCH /v1/policies/{id}` - Update policy
- `DELETE /v1/policies/{id}` - Delete policy

---

## 🧪 Testing

```bash
# Run all tests
poetry run pytest

# Run with coverage
poetry run pytest --cov=src --cov-report=html

# Run specific test file
poetry run pytest tests/api/test_health.py

# Run only unit tests
poetry run pytest -m unit

# Run only integration tests
poetry run pytest -m integration
```

---

## 🔧 Development

### Code Quality

```bash
# Format code
poetry run black src tests
poetry run isort src tests

# Lint
poetry run flake8 src tests

# Type check
poetry run mypy src

# Run all checks
poetry run pre-commit run --all-files
```

### Pre-commit Hooks

```bash
# Install hooks
poetry run pre-commit install

# Run manually
poetry run pre-commit run --all-files
```

---

## 📁 Project Structure

```
neurostack-api/
├── src/
│   ├── api/                 # FastAPI application
│   │   ├── routes/          # API endpoints
│   │   └── middleware/      # Middleware (auth, rate limit)
│   ├── srm/                 # Small Reasoning Module
│   ├── ctx/                 # Context Orchestrator
│   ├── flow/                # Flow Manager
│   ├── policy/              # Policy & Guardrails Engine
│   ├── prompt/              # Prompt Engine
│   │   └── templates/       # Prompt templates
│   ├── memory/              # Memory Architecture 2.0
│   ├── knowledge/           # Knowledge Service (RAG)
│   └── handoff/             # Human Handoff Service
├── tests/                   # Test suite
├── config/                  # Configuration
├── scripts/                 # Utility scripts
├── .github/workflows/       # CI/CD
├── docker-compose.yml       # Docker setup
├── Dockerfile               # Container image
├── pyproject.toml           # Python dependencies
└── README.md
```

---

## 🔐 Environment Variables

See `env.example` for all available configuration options.

Key variables:
- `DATABASE_URL` - PostgreSQL connection string
- `REDIS_URL` - Redis connection string
- `QDRANT_HOST` - Qdrant host
- `SECRET_KEY` - API secret key
- `ENVIRONMENT` - development/staging/production

---

## 📊 Observability

### Metrics
- Prometheus metrics at `/metrics`
- Grafana dashboard (optional, use `--profile observability`)

### Logging
- Structured JSON logging
- OpenTelemetry tracing (optional)

---

## 🚢 Deployment

### Docker

```bash
# Build production image
docker build --target production -t neurostack-api:latest .

# Run container
docker run -p 8000:8000 --env-file .env neurostack-api:latest
```

### Kubernetes

See `/deploy/kubernetes/` (coming soon)

---

## 📖 Documentation

- [API Documentation](http://localhost:8000/docs) (Swagger)
- [Documentação Técnica](./docs/README.md) – índice da documentação
- [Pipeline Cognitivo](./docs/cognitive-pipeline.md) – funil 11 etapas, intenções oficiais, ClinicConfig
- [Contratos de API](./docs/api-contracts.md) – eventos CRM (`ns.funnel_stage_changed`, `ns.handoff_created`), Decision Trace, handoff por setor/SLA

### Alinhamento MVP v0.5 (Documentação Técnica)

A API está alinhada à documentação técnica do NeuroStack (MVP v0.5):

- **Funil canônico:** 11 etapas (`lead_novo`, `em_qualificacao`, `qualificado`, `followup`, `agendamento_solicitado`, `agendado`, `paciente_ativo`, `pos_atendimento`, `fidelizacao`, `inativo`, `standby`)
- **Intenções oficiais:** 23 intenções (ex.: `saudacao`, `pedido_preco`, `agendar`, `conduta_inadequada_ameaca`)
- **Catálogo de ações:** ações oficiais (ex.: `iniciar_fluxo_qualificacao_srm`, `handoff_agendamento`, `handoff_urgencia`)
- **Handoff por setor e SLA:** setores (agendamento, financeiro, gerencia, administrativo, clinico, urgencia, fidelizacao) e SLA (imediato, prioritario, rotina)
- **Eventos de integração CRM:** `ns.funnel_stage_changed`, `ns.handoff_created`
- **ClinicConfig:** persona, scheduling, pricing, post_care, handoff (sector_defaults)
- **UserState/FunnelState:** lead, sales (profile, authority_done, objection_active), last (intent, action, policy_refs, constraints), timestamps
- **Políticas:** IDs oficiais C_* (constraints) e POL_* (políticas)

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests and linting
5. Submit a pull request

---

## 📝 License

MIT License - see [LICENSE](LICENSE) for details.

---

## 🙋 Support

For questions or issues, please open a GitHub issue.

