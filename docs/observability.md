# NeuroStack - Observability Guide

## Visão Geral

Este documento descreve a stack de observabilidade do NeuroStack, incluindo métricas, logs, alertas e dashboards.

## Stack de Observabilidade

| Componente | Tecnologia | Porta | Descrição |
|------------|------------|-------|-----------|
| Métricas | Prometheus | 9090 | Coleta e armazenamento de métricas |
| Dashboards | Grafana | 3000 | Visualização de métricas e logs |
| Alertas | Alertmanager | 9093 | Roteamento e notificação de alertas |
| Logs | Loki | 3100 | Agregação de logs |
| Log Agent | Promtail | - | Coleta de logs dos containers |

## Quick Start

### Iniciar Stack de Observabilidade

```bash
# Iniciar todos os serviços incluindo observabilidade
docker-compose --profile observability up -d

# Verificar status
docker-compose --profile observability ps
```

### Acessar Serviços

- **Grafana**: http://localhost:3000 (admin/neurostack)
- **Prometheus**: http://localhost:9090
- **Alertmanager**: http://localhost:9093

## Métricas

### Métricas de Infraestrutura

| Métrica | Tipo | Descrição |
|---------|------|-----------|
| `neurostack_request_latency_seconds` | Histogram | Latência de requests por endpoint |
| `neurostack_requests_total` | Counter | Total de requests por status |
| `neurostack_active_requests` | Gauge | Requests ativos no momento |
| `neurostack_cache_hits_total` | Counter | Cache hits |
| `neurostack_cache_misses_total` | Counter | Cache misses |

### Métricas de Negócio

| Métrica | Tipo | Labels | Descrição |
|---------|------|--------|-----------|
| `neurostack_srm_decisions_total` | Counter | tenant_id, flow_mode | Decisões do SRM |
| `neurostack_intent_classifications_total` | Counter | intent, tenant_id | Classificações de intent |
| `neurostack_funnel_stage_total` | Counter | stage, tenant_id | Detecções de estágio do funil |
| `neurostack_safety_triggers_total` | Counter | category, tenant_id | Triggers de safety |
| `neurostack_handoffs_total` | Counter | reason, tenant_id | Handoffs para humanos |
| `neurostack_flow_transitions_total` | Counter | from_mode, to_mode, tenant_id | Transições de flow |
| `neurostack_rag_queries_total` | Counter | tenant_id | Consultas RAG |

### Métricas de Performance

| Métrica | Tipo | Labels | Descrição |
|---------|------|--------|-----------|
| `neurostack_srm_processing_seconds` | Histogram | component | Tempo de processamento do SRM |
| `neurostack_pipeline_total_seconds` | Histogram | tenant_id | Tempo total do pipeline |
| `neurostack_pipeline_stage_seconds` | Histogram | stage | Tempo por estágio |

## Uso das Métricas em Código

```python
from src.observability.metrics import BusinessMetrics

# Registrar decisão do SRM
BusinessMetrics.srm.record_decision(
    tenant_id="tenant_123",
    flow_mode="normal",
    intent="scheduling",
    intent_confidence=0.92,
    funnel_stage="consideration",
    processing_time_ms=45.5,
)

# Registrar safety trigger
BusinessMetrics.safety.record_trigger(
    category="clinical",
    tenant_id="tenant_123",
    confidence=0.87,
    severity="high",
)

# Registrar handoff
BusinessMetrics.handoff.record_handoff(
    reason="explicit_request",
    tenant_id="tenant_123",
    turn_count=5,
)

# Registrar transição de flow
BusinessMetrics.flow.record_transition(
    from_mode="normal",
    to_mode="safety",
    tenant_id="tenant_123",
    duration_seconds=120.5,
)
```

## Dashboards

### Dashboard: NeuroStack - API Overview

**UID:** `neurostack-overview`

Métricas monitoradas:
- Latência p95/p99
- Throughput (req/s)
- Taxa de erro
- Requests ativos
- Cache hit rate
- Latência por endpoint

### Dashboard: NeuroStack - Business Metrics

**UID:** `neurostack-business`

Métricas monitoradas:
- Decisões do SRM (24h)
- Safety triggers por categoria
- Handoffs por motivo
- Distribuição de intents
- Transições de flow
- Estágios do funil
- Taxa de compliance
- Taxa de escalonamento

## Alertas

### Alertas de Performance

| Alerta | Severidade | Condição | Descrição |
|--------|------------|----------|-----------|
| `HighLatencyP95` | warning | p95 > 200ms por 5min | Latência acima do SLO |
| `CriticalLatencyP99` | critical | p99 > 500ms por 2min | Latência crítica |

### Alertas de Erro

| Alerta | Severidade | Condição | Descrição |
|--------|------------|----------|-----------|
| `HighErrorRate5xx` | warning | 5xx > 1% por 5min | Taxa de erro elevada |
| `CriticalErrorRate5xx` | critical | 5xx > 5% por 2min | Taxa de erro crítica |
| `HighErrorRate4xx` | warning | 4xx > 10% por 10min | Muitos erros de cliente |

### Alertas de Disponibilidade

| Alerta | Severidade | Condição | Descrição |
|--------|------------|----------|-----------|
| `APIDown` | critical | up == 0 por 1min | API não respondendo |
| `LowThroughput` | warning | < 0.1 req/s por 10min | Throughput muito baixo |

### Alertas de Negócio

| Alerta | Severidade | Condição | Descrição |
|--------|------------|----------|-----------|
| `HighSafetyTriggerRate` | warning | > 10% por 30min | Alta taxa de safety |
| `HighHandoffRate` | warning | > 25% por 30min | Alta taxa de handoffs |
| `ClinicalSafetyAlert` | critical | Qualquer clinical trigger | Safety clínico detectado |

## Configuração de Alertmanager

### Receivers Configurados

| Receiver | Uso | Canais |
|----------|-----|--------|
| `default-receiver` | Alertas gerais | Webhook |
| `critical-receiver` | Alertas críticos | Webhook, Slack (opcional) |
| `clinical-receiver` | Alertas clínicos | Webhook, PagerDuty (opcional) |
| `backend-receiver` | Alertas de backend | Webhook, Slack (opcional) |
| `product-receiver` | Alertas de produto | Webhook, Slack (opcional) |

### Configurar Slack (Opcional)

Edite `config/alertmanager/alertmanager.yml`:

```yaml
global:
  slack_api_url: 'https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK'

receivers:
  - name: 'critical-receiver'
    slack_configs:
      - channel: '#alerts-critical'
        send_resolved: true
        title: '🚨 CRITICAL: {{ .GroupLabels.alertname }}'
```

### Configurar PagerDuty (Opcional)

```yaml
receivers:
  - name: 'clinical-receiver'
    pagerduty_configs:
      - service_key: 'your-pagerduty-service-key'
        severity: 'critical'
```

## Log Aggregation

### Boas Práticas de Logging

O NeuroStack segue uma arquitetura em camadas onde cada camada é responsável por seus próprios logs:

#### Camada de API (Rotas)
- **Responsabilidade**: Apenas logs mínimos de requisição HTTP (entrada/saída)
- **Não deve**: Fazer logging de lógica de negócio, processamento ou operações de dados
- **Exemplo**: Logs de requisições HTTP são tratados pelo middleware de tracing

#### Camada de Serviço (Business Logic)
- **Responsabilidade**: Logs de operações de negócio, processamento e decisões
- **Onde**: `src/core/pipeline.py`, `src/handoff/service.py`, etc.
- **Exemplo**: Logs de processamento do pipeline, decisões do SRM, handoffs

#### Camada de Dados (Repositories/Stores)
- **Responsabilidade**: Logs de operações de banco de dados e persistência
- **Onde**: `src/repositories/`, `src/memory/event_store.py`
- **Exemplo**: Logs de criação/atualização de registros, operações do Event Store

#### Princípios
1. **Separação de Responsabilidades**: Cada camada registra apenas o que é relevante para ela
2. **Independência**: Logs não devem depender de outras camadas
3. **Contexto Rico**: Incluir informações relevantes (tenant_id, session_id, event_id, etc.)
4. **Níveis Apropriados**: 
   - `DEBUG`: Informações detalhadas para desenvolvimento
   - `INFO`: Operações normais e importantes
   - `WARNING`: Situações que merecem atenção mas não são erros
   - `ERROR`: Erros que precisam de investigação

### Estrutura de Logs

Todos os logs são estruturados em JSON:

```json
{
  "timestamp": "2025-01-01T12:00:00.000Z",
  "level": "INFO",
  "event": "Pipeline complete",
  "request_id": "abc-123",
  "tenant_id": "tenant_456",
  "session_id": "session_789",
  "event_id": "event_012",
  "intent": "scheduling",
  "flow_mode": "normal",
  "processing_time_ms": 45.5
}
```

### Queries no Loki (LogQL)

```logql
# Todos os logs do NeuroStack
{container="neurostack-api"}

# Logs de erro
{container="neurostack-api"} |= "ERROR"

# Logs por tenant
{container="neurostack-api"} | json | tenant_id="tenant_123"

# Logs de safety triggers
{container="neurostack-api"} |= "safety" |= "trigger"

# Latência alta (> 200ms)
{container="neurostack-api"} | json | latency_ms > 200
```

## On-Call Setup

### Escalation Policy

1. **Nível 1 (0-15min)**: Slack notification para canal de alertas
2. **Nível 2 (15-30min)**: Notificação individual via Slack/Email
3. **Nível 3 (30min+)**: PagerDuty/SMS para engenheiro de plantão

### Runbooks

Cada alerta possui um `runbook_url` que aponta para documentação de troubleshooting:

- [High Latency](./troubleshooting.md#high-latency)
- [High Error Rate](./troubleshooting.md#high-error-rate)
- [API Down](./troubleshooting.md#api-down)
- [Clinical Safety](./troubleshooting.md#clinical-safety)

### Checklist de Incidente

1. **Identificar**: Verificar dashboards e logs
2. **Comunicar**: Notificar stakeholders no Slack
3. **Mitigar**: Aplicar fix temporário se necessário
4. **Resolver**: Implementar fix permanente
5. **Documentar**: Criar post-mortem

## SLOs (Service Level Objectives)

| SLO | Target | Medição |
|-----|--------|---------|
| Disponibilidade | 99.9% | `up{job="neurostack-api"}` |
| Latência p95 | < 200ms | `histogram_quantile(0.95, ...)` |
| Latência p99 | < 500ms | `histogram_quantile(0.99, ...)` |
| Taxa de Erro | < 1% | `rate(requests{status=~"5.."}[5m]) / rate(requests[5m])` |

## Manutenção

### Retenção de Dados

| Serviço | Retenção |
|---------|----------|
| Prometheus | 15 dias |
| Loki | 7 dias |
| Alertmanager | 5 dias |

### Backup

```bash
# Backup Prometheus data
docker exec neurostack-prometheus tar -czf /prometheus/backup.tar.gz /prometheus

# Backup Grafana dashboards
docker exec neurostack-grafana tar -czf /var/lib/grafana/backup.tar.gz /var/lib/grafana
```

### Atualização

```bash
# Atualizar stack de observabilidade
docker-compose --profile observability pull
docker-compose --profile observability up -d
```

## Troubleshooting

### Prometheus não está coletando métricas

```bash
# Verificar targets
curl http://localhost:9090/api/v1/targets

# Verificar configuração
docker exec neurostack-prometheus promtool check config /etc/prometheus/prometheus.yml
```

### Alertmanager não está enviando alertas

```bash
# Verificar alertas ativos
curl http://localhost:9093/api/v2/alerts

# Verificar configuração
docker exec neurostack-alertmanager amtool check-config /etc/alertmanager/alertmanager.yml
```

### Loki não está recebendo logs

```bash
# Verificar status do Promtail
docker logs neurostack-promtail

# Testar push manual
curl -X POST http://localhost:3100/loki/api/v1/push \
  -H "Content-Type: application/json" \
  -d '{"streams":[{"stream":{"test":"true"},"values":[["'$(date +%s)000000000'","test log"]]}]}'
```

## Referências

- [Prometheus Documentation](https://prometheus.io/docs/)
- [Grafana Documentation](https://grafana.com/docs/)
- [Loki Documentation](https://grafana.com/docs/loki/)
- [Alertmanager Documentation](https://prometheus.io/docs/alerting/latest/alertmanager/)

