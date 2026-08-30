"""
NeuroStack API - Business Metrics Module

Métricas de negócio para monitoramento de:
- Decisões do SRM (intent, funnel, safety)
- Transições de flow
- Handoffs para humanos
- Consultas RAG
- Performance do pipeline cognitivo

Todas as métricas são expostas via Prometheus e podem ser
visualizadas no Grafana.
"""

from prometheus_client import Counter, Histogram, Gauge, Summary
from typing import Optional
from enum import Enum

from src.config.logging import get_logger


logger = get_logger(__name__)


# ============================================
# SRM Metrics - Decisões do Small Reasoning Module
# ============================================

# Total de decisões do SRM
SRM_DECISIONS_TOTAL = Counter(
    # ``src.srm.engine`` owns ``neurostack_srm_decisions_total`` for the
    # runtime decision metric.  This legacy business view has different
    # labels, so it needs a distinct Prometheus identity to remain importable.
    "neurostack_business_srm_decisions_total",
    "Total de decisões de negócio tomadas pelo SRM",
    ["tenant_id", "flow_mode"],
)

# Tempo de processamento do SRM
SRM_PROCESSING_TIME = Histogram(
    "neurostack_srm_processing_seconds",
    "Tempo de processamento do SRM em segundos",
    ["component"],  # intent_classifier, funnel_detector, safety_detector, etc.
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0],
)

# Classificação de intents
INTENT_CLASSIFICATIONS_TOTAL = Counter(
    "neurostack_intent_classifications_total",
    "Total de classificações de intent",
    ["intent", "tenant_id"],
)

# Confiança das classificações de intent
INTENT_CONFIDENCE = Gauge(
    "neurostack_intent_confidence",
    "Confiança da última classificação de intent",
    ["intent"],
)

# Detecção de estágio do funil
FUNNEL_STAGE_TOTAL = Counter(
    "neurostack_funnel_stage_total",
    "Total de detecções por estágio do funil",
    ["stage", "tenant_id"],
)


# ============================================
# Safety Metrics - Detecção de Riscos
# ============================================

# Triggers de safety
SAFETY_TRIGGERS_TOTAL = Counter(
    "neurostack_safety_triggers_total",
    "Total de triggers de safety detectados",
    ["category", "tenant_id"],  # clinical, legal, conduct
)

# Confiança de detecção de safety
SAFETY_CONFIDENCE = Histogram(
    "neurostack_safety_confidence",
    "Distribuição de confiança na detecção de safety",
    ["category"],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)

# Eventos de safety por severidade
SAFETY_SEVERITY = Counter(
    "neurostack_safety_severity_total",
    "Total de eventos de safety por severidade",
    ["severity", "category"],  # low, medium, high, critical
)


# ============================================
# Flow Metrics - Transições de Estado
# ============================================

# Transições de flow
FLOW_TRANSITIONS_TOTAL = Counter(
    # ``src.flow.manager`` owns the runtime transition metric with a
    # different label contract; keep this legacy business view distinct.
    "neurostack_business_flow_transitions_total",
    "Total de transições de flow de negócio",
    ["from_mode", "to_mode", "tenant_id"],
)

# Tempo em cada modo de flow
FLOW_MODE_DURATION = Histogram(
    "neurostack_flow_mode_duration_seconds",
    "Duração em cada modo de flow",
    ["mode"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600],
)

# Sessões ativas por modo
ACTIVE_SESSIONS_BY_MODE = Gauge(
    "neurostack_active_sessions_by_mode",
    "Número de sessões ativas por modo",
    ["mode"],
)


# ============================================
# Handoff Metrics - Escalonamento para Humanos
# ============================================

# Total de handoffs
HANDOFFS_TOTAL = Counter(
    "neurostack_handoffs_total",
    "Total de handoffs para humanos",
    ["reason", "tenant_id"],  # explicit_request, safety, max_turns, low_confidence
)

# Tempo até handoff (número de turnos)
HANDOFF_TURN_COUNT = Histogram(
    "neurostack_handoff_turn_count",
    "Número de turnos até o handoff",
    ["reason"],
    buckets=[1, 2, 3, 5, 7, 10, 15, 20, 30, 50],
)

# Tempo de resposta do humano (quando disponível)
HANDOFF_RESPONSE_TIME = Histogram(
    "neurostack_handoff_response_seconds",
    "Tempo de resposta do humano após handoff",
    ["tenant_id"],
    buckets=[30, 60, 120, 300, 600, 1800, 3600, 7200, 14400],
)

# Webhooks de handoff
HANDOFF_WEBHOOKS = Counter(
    "neurostack_handoff_webhooks_total",
    "Total de webhooks de handoff enviados",
    ["status", "tenant_id"],  # success, failed, timeout
)


# ============================================
# RAG Metrics - Knowledge Service
# ============================================

# Consultas RAG
RAG_QUERIES_TOTAL = Counter(
    "neurostack_rag_queries_total",
    "Total de consultas ao knowledge service",
    ["tenant_id"],
)

# Resultados RAG
RAG_RESULTS_COUNT = Histogram(
    "neurostack_rag_results_count",
    "Número de resultados retornados por query RAG",
    ["tenant_id"],
    buckets=[0, 1, 2, 3, 5, 10, 20, 50],
)

# Latência RAG
RAG_LATENCY = Histogram(
    "neurostack_rag_latency_seconds",
    "Latência das consultas RAG",
    [],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)


# ============================================
# Policy Metrics - Aplicação de Políticas
# ============================================

# Políticas aplicadas
POLICIES_APPLIED_TOTAL = Counter(
    "neurostack_policies_applied_total",
    "Total de políticas aplicadas",
    ["policy_type", "tenant_id"],  # global, tenant, session
)

# Violações de política
POLICY_VIOLATIONS_TOTAL = Counter(
    # ``src.policy.engine`` owns the runtime violation metric with a
    # different label contract; keep this legacy business view distinct.
    "neurostack_business_policy_violations_total",
    "Total de violações de política de negócio detectadas",
    ["rule_id", "tenant_id"],
)


# ============================================
# Pipeline Metrics - Pipeline Cognitivo
# ============================================

# Tempo total do pipeline
PIPELINE_TOTAL_TIME = Histogram(
    "neurostack_pipeline_total_seconds",
    "Tempo total de processamento do pipeline cognitivo",
    ["tenant_id"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0],
)

# Tempo por estágio do pipeline
PIPELINE_STAGE_TIME = Histogram(
    "neurostack_pipeline_stage_seconds",
    "Tempo de processamento por estágio do pipeline",
    ["stage"],  # srm, ctx, flow, prompt
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0],
)


# ============================================
# LLM Generation Metrics
# ============================================

# One increment is emitted only after an LLM call has a final outcome.  Keep
# labels deliberately bounded: provider, model and attempt ordinal belong in
# structured logs, never in Prometheus label values.
LLM_GENERATION_ATTEMPTS_TOTAL = Counter(
    "neurostack_llm_generation_attempts_total",
    "Total completed LLM generation attempts by tier and outcome",
    ["tier", "outcome"],
)


# ============================================
# Helper Classes for Metric Collection
# ============================================

class SRMMetrics:
    """Helper class para coletar métricas do SRM."""
    
    @staticmethod
    def record_decision(
        tenant_id: str,
        flow_mode: str,
        intent: str,
        intent_confidence: float,
        funnel_stage: str,
        processing_time_ms: float,
    ) -> None:
        """Registra uma decisão do SRM."""
        try:
            SRM_DECISIONS_TOTAL.labels(
                tenant_id=tenant_id,
                flow_mode=flow_mode,
            ).inc()
            
            INTENT_CLASSIFICATIONS_TOTAL.labels(
                intent=intent,
                tenant_id=tenant_id,
            ).inc()
            
            INTENT_CONFIDENCE.labels(intent=intent).set(intent_confidence)
            
            FUNNEL_STAGE_TOTAL.labels(
                stage=funnel_stage,
                tenant_id=tenant_id,
            ).inc()
            
            SRM_PROCESSING_TIME.labels(component="total").observe(
                processing_time_ms / 1000
            )
            
            logger.debug(
                "SRM decision recorded",
                tenant_id=tenant_id,
                flow_mode=flow_mode,
                intent=intent,
            )
        except Exception as e:
            logger.error(
                "Failed to record SRM metrics",
                tenant_id=str(tenant_id) if tenant_id else None,
                error=str(e),
                error_type=type(e).__name__,
            )
    
    @staticmethod
    def record_component_time(component: str, time_ms: float) -> None:
        """Registra tempo de um componente específico do SRM."""
        SRM_PROCESSING_TIME.labels(component=component).observe(time_ms / 1000)


class SafetyMetrics:
    """Helper class para coletar métricas de safety."""
    
    @staticmethod
    def record_trigger(
        category: str,
        tenant_id: str,
        confidence: float,
        severity: str = "medium",
    ) -> None:
        """Registra um trigger de safety."""
        try:
            SAFETY_TRIGGERS_TOTAL.labels(
                category=category,
                tenant_id=tenant_id,
            ).inc()
            
            SAFETY_CONFIDENCE.labels(category=category).observe(confidence)
            
            SAFETY_SEVERITY.labels(
                severity=severity,
                category=category,
            ).inc()
            
            logger.info(
                "Safety trigger recorded",
                category=category,
                tenant_id=tenant_id,
                confidence=confidence,
                severity=severity,
            )
        except Exception as e:
            logger.error(
                "Failed to record safety metrics",
                tenant_id=str(tenant_id) if tenant_id else None,
                error=str(e),
                error_type=type(e).__name__,
            )


class FlowMetrics:
    """Helper class para coletar métricas de flow."""
    
    @staticmethod
    def record_transition(
        from_mode: str,
        to_mode: str,
        tenant_id: str,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """Registra uma transição de flow."""
        try:
            FLOW_TRANSITIONS_TOTAL.labels(
                from_mode=from_mode,
                to_mode=to_mode,
                tenant_id=tenant_id,
            ).inc()
            
            if duration_seconds is not None:
                FLOW_MODE_DURATION.labels(mode=from_mode).observe(duration_seconds)
            
            logger.debug(
                "Flow transition recorded",
                from_mode=from_mode,
                to_mode=to_mode,
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.error(
                "Failed to record flow metrics",
                tenant_id=str(tenant_id) if tenant_id else None,
                error=str(e),
                error_type=type(e).__name__,
            )
    
    @staticmethod
    def update_active_sessions(mode: str, count: int) -> None:
        """Atualiza contagem de sessões ativas por modo."""
        ACTIVE_SESSIONS_BY_MODE.labels(mode=mode).set(count)


class HandoffMetrics:
    """Helper class para coletar métricas de handoff."""
    
    @staticmethod
    def record_handoff(
        reason: str,
        tenant_id: str,
        turn_count: int,
    ) -> None:
        """Registra um handoff para humano."""
        try:
            HANDOFFS_TOTAL.labels(
                reason=reason,
                tenant_id=tenant_id,
            ).inc()
            
            HANDOFF_TURN_COUNT.labels(reason=reason).observe(turn_count)
            
            logger.info(
                "Handoff recorded",
                reason=reason,
                tenant_id=tenant_id,
                turn_count=turn_count,
            )
        except Exception as e:
            logger.error(
                "Failed to record handoff metrics",
                tenant_id=str(tenant_id) if tenant_id else None,
                error=str(e),
                error_type=type(e).__name__,
            )
    
    @staticmethod
    def record_webhook(status: str, tenant_id: str) -> None:
        """Registra resultado de webhook de handoff."""
        HANDOFF_WEBHOOKS.labels(status=status, tenant_id=tenant_id).inc()
    
    @staticmethod
    def record_response_time(tenant_id: str, response_time_seconds: float) -> None:
        """Registra tempo de resposta do humano."""
        HANDOFF_RESPONSE_TIME.labels(tenant_id=tenant_id).observe(response_time_seconds)


class RAGMetrics:
    """Helper class para coletar métricas de RAG."""
    
    @staticmethod
    def record_query(
        tenant_id: str,
        result_count: int,
        latency_ms: float,
    ) -> None:
        """Registra uma query RAG."""
        try:
            RAG_QUERIES_TOTAL.labels(tenant_id=tenant_id).inc()
            RAG_RESULTS_COUNT.labels(tenant_id=tenant_id).observe(result_count)
            RAG_LATENCY.observe(latency_ms / 1000)
            
            logger.debug(
                "RAG query recorded",
                tenant_id=tenant_id,
                result_count=result_count,
                latency_ms=latency_ms,
            )
        except Exception as e:
            logger.error(
                "Failed to record RAG metrics",
                tenant_id=str(tenant_id) if tenant_id else None,
                error=str(e),
                error_type=type(e).__name__,
            )


class PolicyMetrics:
    """Helper class para coletar métricas de policy."""
    
    @staticmethod
    def record_application(policy_type: str, tenant_id: str) -> None:
        """Registra aplicação de política."""
        POLICIES_APPLIED_TOTAL.labels(
            policy_type=policy_type,
            tenant_id=tenant_id,
        ).inc()
    
    @staticmethod
    def record_violation(rule_id: str, tenant_id: str) -> None:
        """Registra violação de política."""
        POLICY_VIOLATIONS_TOTAL.labels(
            rule_id=rule_id,
            tenant_id=tenant_id,
        ).inc()
        
        logger.warning(
            "Policy violation recorded",
            rule_id=rule_id,
            tenant_id=tenant_id,
        )


class PipelineMetrics:
    """Helper class para coletar métricas do pipeline."""
    
    @staticmethod
    def record_total_time(tenant_id: str, time_ms: float) -> None:
        """Registra tempo total do pipeline."""
        PIPELINE_TOTAL_TIME.labels(tenant_id=tenant_id).observe(time_ms / 1000)
    
    @staticmethod
    def record_stage_time(stage: str, time_ms: float) -> None:
        """Registra tempo de um estágio do pipeline."""
        PIPELINE_STAGE_TIME.labels(stage=stage).observe(time_ms / 1000)


class BusinessMetrics:
    """
    Facade class que agrupa todas as métricas de negócio.
    
    Usage:
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
    """
    
    srm = SRMMetrics()
    safety = SafetyMetrics()
    flow = FlowMetrics()
    handoff = HandoffMetrics()
    rag = RAGMetrics()
    policy = PolicyMetrics()
    pipeline = PipelineMetrics()


def get_metrics_summary() -> dict:
    """
    Retorna um resumo das métricas de negócio atuais.
    
    Útil para endpoints de status e debugging.
    """
    from prometheus_client import REGISTRY
    
    summary = {
        "srm": {},
        "safety": {},
        "flow": {},
        "handoff": {},
        "rag": {},
    }
    
    try:
        # Coletar valores atuais das métricas
        for metric in REGISTRY.collect():
            if metric.name.startswith("neurostack_"):
                for sample in metric.samples:
                    name = sample.name.replace("neurostack_", "")
                    if "_total" in name:
                        # Counters
                        category = name.split("_")[0]
                        if category in summary:
                            if name not in summary[category]:
                                summary[category][name] = 0
                            summary[category][name] += sample.value
    except Exception as e:
        logger.error(
            "Failed to get metrics summary",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
    
    return summary
