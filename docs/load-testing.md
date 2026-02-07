# Testes de Carga e Performance - NeuroStack API

## Visão Geral

Este documento descreve a estratégia de testes de carga e performance para a NeuroStack API, incluindo cenários de teste, targets de performance e guias de otimização.

## Targets de Performance (SLOs)

| Métrica | Target | Crítico |
|---------|--------|---------|
| Latência p95 | < 200ms | < 500ms |
| Latência p99 | < 500ms | < 1000ms |
| Taxa de Erro | < 0.1% | < 1% |
| Throughput | ≥ 1000 req/s | ≥ 500 req/s |

## Ferramentas

### Locust

Utilizamos [Locust](https://locust.io/) para testes de carga. Locust é uma ferramenta Python que permite definir cenários de teste como código Python.

**Instalação:**
```bash
poetry install  # Já incluído nas dependências de dev
```

## Cenários de Teste

### 1. Smoke Test
- **Usuários:** 10
- **Duração:** 1 minuto
- **Objetivo:** Validação rápida da API

```bash
make load-test-smoke
```

### 2. Load Test (Padrão)
- **Usuários:** 100
- **Duração:** 5 minutos
- **Objetivo:** Teste de carga típico

```bash
make load-test
```

### 3. Stress Test
- **Usuários:** 500
- **Duração:** 10 minutos
- **Objetivo:** Testar limites do sistema

```bash
make load-test-stress
```

### 4. Benchmark
- **Usuários:** 200
- **Target:** 1000 req/s
- **Duração:** 10 minutos
- **Objetivo:** Validar capacidade máxima

```bash
make load-test-benchmark
```

### 5. Endurance Test
- **Usuários:** 100
- **Duração:** 30 minutos
- **Objetivo:** Detectar memory leaks e degradação

```bash
python tests/load/run_load_tests.py --scenario endurance
```

## Executando Testes

### Modo Headless (CI/CD)

```bash
# Smoke test rápido
make load-test-smoke

# Teste completo
make load-test

# Com parâmetros customizados
python tests/load/run_load_tests.py \
    --scenario load \
    --host http://staging.neurostack.ai \
    --users 150 \
    --duration 600
```

### Modo Web UI (Desenvolvimento)

```bash
make load-test-ui
# Abrir http://localhost:8089 no navegador
```

### Diretamente com Locust

```bash
# Headless
locust -f tests/load/locustfile.py \
    --host=http://localhost:8000 \
    --headless \
    --users 100 \
    --spawn-rate 10 \
    --run-time 5m \
    --html=report.html

# Com Web UI
locust -f tests/load/locustfile.py --host=http://localhost:8000
```

## Análise de Resultados

### Analisador de Bottlenecks

Após executar os testes, use o analisador para identificar problemas:

```bash
make load-analyze
# ou
python tests/load/bottleneck_analyzer.py --csv-dir load_test_results/
```

O analisador identifica:
- **Endpoints Lentos:** p95 > 200ms
- **Alta Variância:** p99/avg > 3x
- **Taxa de Erro:** > 0.1%

### Relatórios Gerados

Após cada teste, os seguintes arquivos são gerados em `load_test_results/`:

| Arquivo | Descrição |
|---------|-----------|
| `report_*.html` | Relatório visual interativo |
| `stats_*_stats.csv` | Estatísticas por endpoint |
| `stats_*_failures.csv` | Detalhes de falhas |
| `stats_*_stats_history.csv` | Métricas ao longo do tempo |
| `results_*.json` | Análise estruturada |

## Endpoints Testados

### Críticos (Alta Carga)
| Endpoint | Peso | Descrição |
|----------|------|-----------|
| `POST /v1/events` | 70% | Pipeline cognitivo principal |
| `GET /live` | 5% | Liveness probe |

### Secundários
| Endpoint | Peso | Descrição |
|----------|------|-----------|
| `POST /v1/sessions` | 15% | Criação de sessões |
| `GET /v1/sessions` | 5% | Listagem de sessões |
| `GET /health` | 3% | Health check completo |
| `GET /ready` | 2% | Readiness probe |

## Otimizações Recomendadas

### 1. Configuração do Servidor

```python
# config/settings.py
workers: int = 4  # CPU cores para produção
```

```bash
# Uvicorn com múltiplos workers
uvicorn src.api.main:app --workers 4
```

### 2. Connection Pooling

```python
# PostgreSQL
database_pool_size: int = 20
database_max_overflow: int = 40

# Redis
redis_max_connections: int = 50
```

### 3. Caching

```python
# Implementar cache para:
# - Policies (TTL: 5 min)
# - Session state (TTL: 5 min)
# - Health checks (TTL: 10s)
```

### 4. Rate Limiting

```python
# Ajustar conforme testes
rate_limit_requests_per_minute: int = 1000
rate_limit_burst: int = 100
```

## Checklist de Performance

### Antes de Deploy

- [ ] Executar smoke test
- [ ] Verificar p95 < 200ms
- [ ] Verificar error rate < 0.1%
- [ ] Analisar relatório de bottlenecks
- [ ] Verificar logs de erro

### Para Produção

- [ ] Executar load test completo
- [ ] Executar benchmark (1000 req/s)
- [ ] Validar sob stress
- [ ] Documentar baseline de performance
- [ ] Configurar alertas de monitoramento

## Troubleshooting

### Latência Alta

1. Verificar métricas de database
2. Analisar connection pool
3. Profile código com `py-spy`
4. Verificar I/O blocking

### Taxa de Erro Alta

1. Verificar logs de aplicação
2. Checar rate limiting
3. Analisar timeouts
4. Verificar saúde de dependências

### Throughput Baixo

1. Aumentar workers
2. Escalar horizontalmente
3. Otimizar queries pesadas
4. Implementar cache

## Integração CI/CD

### GitHub Actions

```yaml
- name: Load Test
  run: |
    make docker-up
    sleep 10  # Aguardar startup
    make load-test-smoke
    make load-analyze
```

### Critérios de Aceite

O pipeline deve falhar se:
- p95 > 200ms
- Error rate > 0.1%
- Throughput < 100 req/s (smoke)

## Referências

- [Locust Documentation](https://docs.locust.io/)
- [FastAPI Performance](https://fastapi.tiangolo.com/deployment/)
- [Uvicorn Deployment](https://www.uvicorn.org/deployment/)

