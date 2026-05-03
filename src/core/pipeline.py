"""
NeuroStack - Cognitive Pipeline
Main orchestrator that connects all modules in the end-to-end flow.

This module implements Task 13.0 - End-to-End Integration,
connecting: SRM → Policy → CTX → Flow → Prompt → Handoff
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union
from uuid import UUID, uuid4

from prometheus_client import Counter, Histogram

from src.config.logging import get_logger
from src.core.exceptions import (
    ServiceError,
    PolicyServiceError,
    EventServiceError,
)
from src.srm.engine import SRMEngine, get_srm_engine
from src.srm.models import (
    SRMInput,
    SRMDecision as SRMDecisionModel,
    FunnelStage,
    FlowMode as SRMFlowMode,
    HandoffReason as SRMHandoffReason,
)
from src.policy.engine import PolicyEngine, get_policy_engine
from src.policy.models import PolicyContext, PolicyEvaluationResult
from src.ctx.orchestrator import ContextOrchestrator, get_context_orchestrator
from src.ctx.models import ContextRequest, ContextMessage, MessageRole, CognitiveContext
from src.flow.manager import FlowManager, get_flow_manager
from src.flow.models import FlowContext, FlowDecision, FlowMode as FlowModeModel
from src.prompt.engine import PromptEngine, get_prompt_engine
from src.prompt.models import PromptMode, PromptVariables, GeneratedPrompt
from src.services import HandoffService, get_handoff_service
from src.handoff.models import (
    HandoffRequest,
    HandoffResponse,
    HandoffReason,
    HandoffSector,
    HandoffSLA,
    HandoffUrgency,
)


logger = get_logger(__name__)


# ============================================
# User Journey (contexto para condução pelo funil)
# ============================================

def build_user_journey(
    current_funnel_stage: str,
    intent_history: list[str],
    slots: dict[str, Any],
    turn_count: int,
    last_intent: Optional[str] = None,
) -> str:
    """
    Monta a jornada do usuário no funil para o prompt.
    O contexto do assistente deve ser a jornada (etapas/intenções/dados),
    não o histórico de mensagens, para conduzir o usuário pelas etapas corretas.
    """
    parts = [
        f"**Etapa atual do funil:** {current_funnel_stage}",
        f"**Turno:** {turn_count}",
    ]
    if last_intent:
        parts.append(f"**Última intenção detectada:** {last_intent}")
    if intent_history:
        steps = ", ".join(intent_history[-10:])  # últimas 10
        parts.append(f"**Intenções já percorridas (ordem):** {steps}")
    if slots:
        slot_lines = [f"- {k}: {v}" for k, v in list(slots.items())[:20]]
        parts.append("**Dados já coletados:**\n" + "\n".join(slot_lines))
    return "\n".join(parts)


# ============================================
# Prometheus Metrics
# ============================================

PIPELINE_TOTAL = Counter(
    "neurostack_pipeline_total",
    "Total pipeline executions",
    ["mode", "result"],
)

PIPELINE_LATENCY = Histogram(
    "neurostack_pipeline_latency_seconds",
    "Pipeline execution latency",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

PIPELINE_STAGE_LATENCY = Histogram(
    "neurostack_pipeline_stage_latency_seconds",
    "Latency per pipeline stage",
    ["stage"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1),
)


# ============================================
# Pipeline Models
# ============================================

from pydantic import BaseModel, Field


class PipelineInput(BaseModel):
    """Input for the cognitive pipeline."""
    
    tenant_id: UUID
    session_id: UUID
    message: str
    channel: str = "unknown"
    user_id: Optional[str] = None
    
    # Session context (from StateStore in production)
    current_funnel_stage: str = "lead_novo"
    turn_count: int = 0
    intent_history: list[str] = Field(default_factory=list)
    slots: dict[str, Union[str, int, float, bool]] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    message_history: list[dict[str, str]] = Field(default_factory=list)
    # Doc MVP v0.5: ClinicConfig e UserState (quando carregados)
    clinic_config: Optional[dict] = None
    user_state: Optional[dict] = None


class PipelineResult(BaseModel):
    """Complete result from the cognitive pipeline."""
    
    # Identifiers
    event_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    session_id: UUID
    
    # SRM Decision (Decision Trace doc: funnel_stage_before/after)
    intent: str
    intent_category: str
    funnel_stage: str
    funnel_stage_before: Optional[str] = None
    action: str
    confidence: float
    safety_concern: bool
    safety_category: str
    needs_rag: bool
    handoff_required: bool
    handoff_reason: Optional[str] = None
    reasoning: Optional[str] = None

    # Sentiment
    sentiment_label: str = "neutral"
    sentiment_score: float = 0.0
    sentiment_confidence: float = 0.0
    
    # State Patch
    state_patch: dict[str, Any] = Field(default_factory=dict)
    
    # Prompt
    system_prompt: str
    instructions: str
    constraints: list[str] = Field(default_factory=list)
    context_summary: Optional[str] = None
    
    # Context
    recent_messages: list[dict[str, str]] = Field(default_factory=list)
    context_sources: list[dict[str, Any]] = Field(default_factory=list)
    turn_count: int = 0
    
    # Flow
    mode: str = "normal"
    
    # Handoff
    handoff_summary: Optional[dict[str, Any]] = None
    handoff_user_message: Optional[str] = None

    # Pause (set by handoff engine when session should be paused)
    should_pause: bool = False
    paused_until: Optional[str] = None  # ISO datetime string or None (permanent)
    
    # Metadata
    processed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    processing_time_ms: int = 0
    
    # Stage timings for debugging
    stage_timings: dict[str, float] = Field(default_factory=dict)


# ============================================
# Cognitive Pipeline
# ============================================

class CognitivePipeline:
    """
    Main Cognitive Pipeline Orchestrator.
    
    Connects all NeuroStack modules in the complete processing flow:
    
    1. **Policy Pre-Check**: Validate incoming message against policies
    2. **SRM Processing**: Classify intent, detect safety, decide handoff
    3. **Flow Management**: Determine conversation mode
    4. **CTX Context Building**: Assemble context for LLM
    5. **Prompt Generation**: Create structured prompt
    6. **Handoff (if needed)**: Initiate human handoff
    
    This is the central orchestrator implementing Task 13.0.
    """

    def __init__(
        self,
        srm_engine: Optional[SRMEngine] = None,
        policy_engine: Optional[PolicyEngine] = None,
        context_orchestrator: Optional[ContextOrchestrator] = None,
        flow_manager: Optional[FlowManager] = None,
        prompt_engine: Optional[PromptEngine] = None,
        handoff_service: Optional[HandoffService] = None,
    ):
        """
        Initialize the cognitive pipeline.
        
        Args:
            srm_engine: SRM engine for cognitive decisions
            policy_engine: Policy engine for guardrails
            context_orchestrator: CTX for context building
            flow_manager: Flow manager for state machine
            prompt_engine: Prompt engine for LLM prompts
            handoff_service: Handoff service for human escalation
        """
        self._srm = srm_engine or get_srm_engine()
        self._policy = policy_engine or get_policy_engine()
        self._ctx = context_orchestrator or get_context_orchestrator()
        self._flow = flow_manager or get_flow_manager()
        self._prompt = prompt_engine or get_prompt_engine()
        self._handoff = handoff_service or get_handoff_service()

    async def process(self, input_data: PipelineInput) -> PipelineResult:
        """
        Process a message through the complete cognitive pipeline.
        
        This is the main entry point for end-to-end processing.
        
        Args:
            input_data: Pipeline input with message and context
            
        Returns:
            PipelineResult with complete cognitive decision
        """
        start_time = time.perf_counter()
        stage_timings: dict[str, float] = {}
        
        logger.info(
            "Processing pipeline",
            tenant_id=str(input_data.tenant_id),
            session_id=str(input_data.session_id),
            channel=input_data.channel,
        )
        
        try:
            # Stage 1: Policy Pre-Check
            stage_start = time.perf_counter()
            policy_result = await self._run_policy_check(input_data)
            stage_timings["policy"] = (time.perf_counter() - stage_start) * 1000
            
            # If message is blocked by policy, return early
            if policy_result.should_block:
                return self._build_blocked_response(input_data, policy_result, stage_timings)
            
            # Stage 2: SRM Processing
            stage_start = time.perf_counter()
            srm_decision = await self._run_srm(input_data)
            stage_timings["srm"] = (time.perf_counter() - stage_start) * 1000
            
            # Stage 3: Flow Management
            stage_start = time.perf_counter()
            flow_decision = await self._run_flow_manager(input_data, srm_decision)
            stage_timings["flow"] = (time.perf_counter() - stage_start) * 1000
            
            # Determine effective mode from flow decision
            effective_mode = self._get_effective_mode(srm_decision, flow_decision)
            
            # Stage 4: CTX Context Building
            stage_start = time.perf_counter()
            context_response = await self._run_ctx(input_data, srm_decision, policy_result)
            stage_timings["ctx"] = (time.perf_counter() - stage_start) * 1000
            
            # Stage 5: Prompt Generation
            stage_start = time.perf_counter()
            generated_prompt, context_sources = await self._run_prompt_engine(
                input_data, srm_decision, context_response, effective_mode, policy_result
            )
            stage_timings["prompt"] = (time.perf_counter() - stage_start) * 1000
            
            # Stage 6: Handoff (if required)
            handoff_response = None
            should_pause = False
            paused_until_iso: Optional[str] = None
            if srm_decision.handoff_required:
                stage_start = time.perf_counter()
                handoff_response = await self._run_handoff(input_data, srm_decision)

                # Handoff engine: check config and pause session if allowed
                should_pause, paused_until_iso = await self._apply_handoff_pause(input_data)

                stage_timings["handoff"] = (time.perf_counter() - stage_start) * 1000
            
            # Build final result
            total_time = int((time.perf_counter() - start_time) * 1000)
            
            result = self._build_result(
                input_data=input_data,
                srm_decision=srm_decision,
                generated_prompt=generated_prompt,
                context_response=context_response,
                effective_mode=effective_mode,
                handoff_response=handoff_response,
                stage_timings=stage_timings,
                total_time=total_time,
                context_sources=context_sources,
                should_pause=should_pause,
                paused_until=paused_until_iso,
            )
            
            # Record metrics
            self._record_metrics(result)
            
            logger.info(
                "Pipeline complete",
                event_id=str(result.event_id),
                mode=result.mode,
                intent=result.intent,
                processing_time_ms=total_time,
            )
            
            return result
            
        except (PolicyServiceError, ServiceError) as e:
            logger.error(
                "Pipeline service error",
                tenant_id=str(input_data.tenant_id),
                session_id=str(input_data.session_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            PIPELINE_TOTAL.labels(mode="error", result="service_error").inc()
            raise EventServiceError("Pipeline processing failed") from e
        except Exception as e:
            logger.error(
                "Unexpected pipeline error",
                tenant_id=str(input_data.tenant_id),
                session_id=str(input_data.session_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            PIPELINE_TOTAL.labels(mode="error", result="error").inc()
            raise EventServiceError("Unexpected error in pipeline processing") from e

    async def _run_policy_check(self, input_data: PipelineInput) -> PolicyEvaluationResult:
        """Run policy pre-check on the message."""
        return await self._policy.evaluate_pre_srm(
            tenant_id=input_data.tenant_id,
            session_id=input_data.session_id,
            message=input_data.message,
            channel=input_data.channel,
            clinic_config=getattr(input_data, "clinic_config", None),
            user_state=getattr(input_data, "user_state", None),
        )

    async def _run_srm(self, input_data: PipelineInput) -> SRMDecisionModel:
        """Run SRM decision engine."""
        try:
            fs = FunnelStage(input_data.current_funnel_stage)
        except ValueError:
            fs = FunnelStage.LEAD_NOVO
        srm_input = SRMInput(
            tenant_id=input_data.tenant_id,
            session_id=input_data.session_id,
            message=input_data.message,
            channel=input_data.channel,
            user_id=input_data.user_id,
            current_funnel_stage=fs,
            turn_count=input_data.turn_count,
            intent_history=input_data.intent_history,
            slots=input_data.slots,
            flags=input_data.flags,
            clinic_config=getattr(input_data, "clinic_config", None),
            user_state=getattr(input_data, "user_state", None),
        )
        return self._srm.process(srm_input)

    async def _run_flow_manager(
        self,
        input_data: PipelineInput,
        srm_decision: SRMDecisionModel,
    ) -> FlowDecision:
        """Run flow manager for state transitions."""
        flow_context = FlowContext(
            session_id=input_data.session_id,
            tenant_id=input_data.tenant_id,
            current_mode=FlowModeModel.NORMAL,
            detected_intent=srm_decision.intent,
            intent_confidence=srm_decision.confidence,
            safety_concern=srm_decision.safety_concern,
            safety_category=srm_decision.safety_category.value if srm_decision.safety_concern else None,
            handoff_required=srm_decision.handoff_required,
            handoff_reason=srm_decision.handoff_reason.value if srm_decision.handoff_reason else None,
            needs_rag=srm_decision.needs_rag,
            turn_count=input_data.turn_count,
            message=input_data.message,
        )
        
        return await self._flow.process(flow_context)

    async def _run_ctx(
        self,
        input_data: PipelineInput,
        srm_decision: SRMDecisionModel,
        policy_result: PolicyEvaluationResult,
    ):
        """Run context orchestrator."""
        # Build cognitive context from session state (doc: Clinic Config > UserState)
        cognitive_state = CognitiveContext(
            funnel_stage=srm_decision.funnel_stage.value,
            last_intent=srm_decision.intent,
            confidence_avg=srm_decision.confidence,
            slots=input_data.slots,
            flags=input_data.flags,
            intent_history=input_data.intent_history,
            clinic_config=getattr(input_data, "clinic_config", None),
            user_state=getattr(input_data, "user_state", None),
        )
        
        messages = [
            ContextMessage(
                role=MessageRole(m.get("role", "user")),
                content=m.get("content", ""),
            )
            for m in input_data.message_history
        ]
        
        request = ContextRequest(
            tenant_id=input_data.tenant_id,
            session_id=input_data.session_id,
            current_message=input_data.message,
            needs_rag=srm_decision.needs_rag,
            srm_intent=srm_decision.intent,
            cognitive_state=cognitive_state,
            clinic_config=getattr(input_data, "clinic_config", None),
            user_state=getattr(input_data, "user_state", None),
        )
        
        return await self._ctx.build_context(
            request=request,
            message_history=messages,
            constraints=policy_result.constraints,
        )

    async def _run_prompt_engine(
        self,
        input_data: PipelineInput,
        srm_decision: SRMDecisionModel,
        context_response,
        effective_mode: str,
        policy_result: PolicyEvaluationResult,
    ) -> tuple[GeneratedPrompt, list[dict[str, Any]]]:
        """Run prompt engine."""
        # Map mode to PromptMode
        prompt_mode_map = {
            "normal": PromptMode.NORMAL,
            "rag": PromptMode.RAG,
            "safety": PromptMode.SAFETY,
            "handoff": PromptMode.HANDOFF,
        }
        prompt_mode = prompt_mode_map.get(effective_mode, PromptMode.NORMAL)
        
        # Jornada do usuário no funil (contexto principal: etapas/intenções/dados, não mensagens)
        user_journey = build_user_journey(
            current_funnel_stage=srm_decision.funnel_stage.value,
            intent_history=input_data.intent_history,
            slots=input_data.slots,
            turn_count=input_data.turn_count,
            last_intent=srm_decision.intent,
        )

        # Build custom variables from clinic_config (identity, services, pricing, etc.)
        custom_vars: dict[str, Any] = {}
        if input_data.clinic_config:
            cfg = input_data.clinic_config
            identity = cfg.get("identity", {})
            operations = cfg.get("operations", {})
            business = cfg.get("business_rules", {})
            pricing = cfg.get("pricing", {})
            persona = cfg.get("persona", {})
            custom_vars["agent_name"] = (
                identity.get("agent_name")
                or persona.get("agent_display_name")
                or "assistente"
            )
            custom_vars["clinic_name"] = (
                identity.get("clinic_name")
                or persona.get("clinic_display_name")
                or "nossa clínica"
            )
            raw_specs = identity.get("clinic_specialties", [])
            if isinstance(raw_specs, str):
                raw_specs = [s.strip() for s in raw_specs.split(",") if s.strip()]
            custom_vars["clinic_specialties"] = raw_specs
            custom_vars["working_hours"] = operations.get("working_hours", "")
            custom_vars["full_address"] = operations.get("full_address", "")
            custom_vars["differentials"] = business.get("differentials", "")
            custom_vars["payment_methods"] = business.get("payment_methods", "")
            custom_vars["pricing_mode"] = pricing.get("disclosure_mode", "CONSULT_ONLY")

            # Extended config injection (Group 1 — clinic-config-fullstack)
            custom_vars["agent_presentation"] = identity.get("agent_presentation", "")
            custom_vars["use_emojis"] = identity.get("use_emojis", False)
            custom_vars["language_policy"] = business.get("language_policy", "")
            custom_vars["clinic_history"] = identity.get("clinic_history", "")
            custom_vars["parking_info"] = operations.get("parking_info", "")
            custom_vars["scheduling_hours"] = operations.get("scheduling_hours", "")
            custom_vars["service_format"] = operations.get("service_format", "")
            custom_vars["accepts_insurance"] = business.get("accepts_insurance", False)
            custom_vars["insurance_list"] = business.get("insurance_list", "")
            custom_vars["provide_prices"] = business.get("provide_prices", False)
            custom_vars["initial_consultation"] = business.get("initial_consultation", "")

            # Format professionals array
            professionals = cfg.get("professionals", [])
            if professionals and isinstance(professionals, list):
                lines = []
                for p in professionals:
                    if isinstance(p, dict):
                        name = p.get("name", "")
                        specialty = p.get("specialty", "")
                        desc = p.get("description", "")
                        line = f"- {name}, {specialty}" if specialty else f"- {name}"
                        if desc:
                            line += f": {desc}"
                        lines.append(line)
                custom_vars["professionals"] = "\n".join(lines)
            else:
                custom_vars["professionals"] = ""

            # Format services array (with price and description when available)
            # Check if initial consultation is free (to annotate service lines)
            initial_consult = str(cfg.get("initial_consultation", "")).lower()
            initial_free = any(w in initial_consult for w in ("gratuita", "cortesia", "grátis", "free", "complimentary"))

            services = cfg.get("services", [])
            if services and isinstance(services, list):
                svc_names = []
                svc_lines = []
                for s in services:
                    if isinstance(s, dict):
                        name = s.get("name", "")
                        if not name:
                            continue
                        svc_names.append(name)
                        line = f"- {name}"
                        price = s.get("price", "")
                        if price:
                            line += f" — {price}"
                        # If this looks like a consultation service and initial is free, annotate
                        if initial_free and name.lower().strip() in ("consulta", "consulta inicial", "avaliação"):
                            line += " (⚠️ PRIMEIRA CONSULTA É CORTESIA da clínica — não cobrar)"
                        desc = s.get("description", "")
                        if desc:
                            line += f": {desc}"
                        authority = s.get("authority_notes", "")
                        if authority:
                            line += f" ({authority})"
                        svc_lines.append(line)
                    elif isinstance(s, str) and s:
                        svc_names.append(s)
                        svc_lines.append(f"- {s}")
                custom_vars["services"] = ", ".join(svc_names)
                custom_vars["services_detail"] = "\n".join(svc_lines)
            else:
                custom_vars["services"] = ""
                custom_vars["services_detail"] = ""

        # Collect context sources from non-empty custom_vars
        context_sources: list[dict[str, Any]] = []
        if input_data.clinic_config:
            _FIELD_TITLES: dict[str, tuple[str, str]] = {
                "clinic_name": ("identity.clinic_name", "Nome da Clínica"),
                "agent_name": ("persona.agent_name", "Nome do Agente"),
                "clinic_specialties": ("identity.clinic_specialties", "Especialidades"),
                "working_hours": ("operations.working_hours", "Horário de Funcionamento"),
                "full_address": ("operations.full_address", "Endereço"),
                "differentials": ("business_rules.differentials", "Diferenciais"),
                "payment_methods": ("business_rules.payment_methods", "Formas de Pagamento"),
                "pricing_mode": ("pricing.disclosure_mode", "Modo de Preços"),
                "agent_presentation": ("persona.agent_presentation", "Apresentação do Agente"),
                "use_emojis": ("persona.use_emojis", "Uso de Emojis"),
                "language_policy": ("persona.language_policy", "Política de Linguagem"),
                "clinic_history": ("identity.clinic_history", "História da Clínica"),
                "parking_info": ("operations.parking_info", "Estacionamento"),
                "scheduling_hours": ("operations.scheduling_hours", "Horário de Agendamento"),
                "service_format": ("operations.service_format", "Formato de Atendimento"),
                "accepts_insurance": ("business_rules.accepts_insurance", "Aceita Convênio"),
                "insurance_list": ("business_rules.insurance_list", "Lista de Convênios"),
                "provide_prices": ("business_rules.provide_prices", "Fornecer Preços"),
            }
            for var_key, (field_path, title) in _FIELD_TITLES.items():
                val = custom_vars.get(var_key)
                if val is None or val == "" or val == [] or val is False:
                    continue
                content = ", ".join(val) if isinstance(val, list) else str(val)
                if content and content not in ("assistente", "nossa clínica", "CONSULT_ONLY"):
                    context_sources.append({
                        "origin": "config",
                        "field": field_path,
                        "title": title,
                        "content": content[:200],
                    })

            # Grouped: services
            svc_val = custom_vars.get("services", "")
            if svc_val:
                svc_list = [s.strip() for s in svc_val.split(",") if s.strip()]
                if svc_list:
                    context_sources.append({
                        "origin": "config",
                        "field": "services",
                        "title": f"Serviços ({len(svc_list)})",
                        "content": svc_val[:200],
                    })

            # Grouped: professionals
            prof_val = custom_vars.get("professionals", "")
            if prof_val:
                prof_lines = [l for l in prof_val.split("\n") if l.strip()]
                if prof_lines:
                    prof_names = [l.lstrip("- ").split(",")[0].strip() for l in prof_lines]
                    context_sources.append({
                        "origin": "config",
                        "field": "professionals",
                        "title": f"Profissionais ({len(prof_lines)})",
                        "content": ", ".join(prof_names)[:200],
                    })

        # Build variables
        variables = PromptVariables(
            user_message=input_data.message,
            funnel_stage=srm_decision.funnel_stage.value,
            last_intent=srm_decision.intent,
            channel=input_data.channel,
            turn_count=input_data.turn_count,
            slots=input_data.slots,
            constraints=policy_result.constraints or [],
            user_journey=user_journey,
            custom=custom_vars,
        )
        
        # Add safety context if needed
        if srm_decision.safety_concern:
            variables.safety_category = srm_decision.safety_category.value
            variables.safety_level = 1
        
        # Contexto é a jornada do usuário, não o histórico de mensagens (condução pelo funil)
        history: list[dict[str, str]] = []
        
        generated = await self._prompt.generate(
            mode=prompt_mode,
            variables=variables,
            history=history,
        )
        return generated, context_sources

    async def _run_handoff(
        self,
        input_data: PipelineInput,
        srm_decision: SRMDecisionModel,
    ) -> Optional[HandoffResponse]:
        """Run handoff service if required."""
        # Map SRM handoff reason to Handoff service reason
        reason_map = {
            SRMHandoffReason.USER_REQUEST: HandoffReason.USER_REQUEST,
            SRMHandoffReason.SAFETY_CONCERN: HandoffReason.SAFETY_CONCERN,
            SRMHandoffReason.MAX_TURNS_EXCEEDED: HandoffReason.MAX_TURNS_EXCEEDED,
            SRMHandoffReason.LOW_CONFIDENCE: HandoffReason.LOW_CONFIDENCE,
            SRMHandoffReason.COMPLEX_QUERY: HandoffReason.COMPLEX_QUERY,
            SRMHandoffReason.ESCALATION: HandoffReason.ESCALATION,
        }
        
        reason = reason_map.get(
            srm_decision.handoff_reason,
            HandoffReason.USER_REQUEST,
        )
        
        # Map urgency
        urgency_map = {
            "low": HandoffUrgency.LOW,
            "normal": HandoffUrgency.NORMAL,
            "high": HandoffUrgency.HIGH,
            "critical": HandoffUrgency.CRITICAL,
        }
        urgency = urgency_map.get(srm_decision.handoff_urgency, HandoffUrgency.NORMAL)
        
        # Build recent messages for summary
        recent_messages = [
            {
                "role": m.get("role", "user"),
                "content": m.get("content", ""),
            }
            for m in input_data.message_history[-5:]  # Last 5 messages
        ]
        
        # Setor e SLA (doc MVP v0.5): do SRM quando disponíveis
        sector = None
        sla = None
        if getattr(srm_decision, "handoff_sector", None):
            try:
                sector = HandoffSector(srm_decision.handoff_sector)
            except ValueError:
                pass
        if getattr(srm_decision, "handoff_sla", None):
            try:
                sla = HandoffSLA(srm_decision.handoff_sla)
            except ValueError:
                pass

        request = HandoffRequest(
            tenant_id=input_data.tenant_id,
            session_id=input_data.session_id,
            reason=reason,
            urgency=urgency,
            sector=sector,
            sla=sla,
            funnel_stage=input_data.current_funnel_stage,
            recent_messages=recent_messages,
            key_info=input_data.slots,
        )
        return await self._handoff.initiate_handoff(request)

    async def _apply_handoff_pause(
        self,
        input_data: PipelineInput,
    ) -> tuple[bool, Optional[str]]:
        """Check handoff config and determine if session should be paused.

        Called after SRM detects handoff_required=True.
        Returns (should_pause, paused_until_iso).
        """
        cfg = (input_data.clinic_config or {}).get("handoff", {})
        handoff_active = str(cfg.get("handoff_active", "yes")).lower()
        standby_mode = str(cfg.get("standby_mode", "active")).lower()

        if handoff_active != "yes" or standby_mode != "active":
            logger.info(
                "handoff_pause_skipped",
                extra={
                    "session_id": str(input_data.session_id),
                    "handoff_active": handoff_active,
                    "standby_mode": standby_mode,
                },
            )
            return False, None

        # Calculate paused_until based on handoff_period
        handoff_period = str(cfg.get("handoff_period", "permanent")).lower()
        paused_until_iso: Optional[str] = None

        if handoff_period == "cycle":
            cycle_days = int(cfg.get("handoff_cycle_days", 7))
            paused_until = datetime.now(timezone.utc) + timedelta(days=cycle_days)
            paused_until_iso = paused_until.isoformat()

        logger.info(
            "session_paused",
            extra={
                "event": "session_paused",
                "reason": "auto_handoff",
                "session_id": str(input_data.session_id),
                "tenant_id": str(input_data.tenant_id),
                "handoff_period": handoff_period,
                "paused_until": paused_until_iso,
            },
        )

        # Fire-and-forget: mark WhatsApp conversation as unread
        try:
            api_creds = (input_data.clinic_config or {}).get("api_credentials", {})
            instance_id = api_creds.get("zapi_instance_id")
            token = api_creds.get("zapi_token")
            client_token = api_creds.get("zapi_client_token", "")
            phone = input_data.user_id

            if instance_id and token and phone:
                from src.services.zapi_service import mark_conversation_unread

                await mark_conversation_unread(
                    instance_id=instance_id,
                    token=token,
                    phone=phone,
                    client_token=client_token,
                )
        except Exception as exc:
            logger.warning(
                "mark_unread_fire_and_forget_failed",
                extra={
                    "session_id": str(input_data.session_id),
                    "error": str(exc),
                },
            )

        return True, paused_until_iso

    def _get_effective_mode(
        self,
        srm_decision: SRMDecisionModel,
        flow_decision: FlowDecision,
    ) -> str:
        """Determine the effective mode from SRM and Flow decisions."""
        # Priority: Handoff > Safety > RAG > Normal
        if srm_decision.handoff_required:
            return "handoff"
        if srm_decision.safety_concern:
            return "safety"
        if srm_decision.needs_rag:
            return "rag"
        
        # Use flow decision if it determined a transition
        if flow_decision.transition_required:
            return flow_decision.next_mode.value
        
        return "normal"

    def _build_blocked_response(
        self,
        input_data: PipelineInput,
        policy_result: PolicyEvaluationResult,
        stage_timings: dict[str, float],
    ) -> PipelineResult:
        """Build a response for blocked messages."""
        violation = policy_result.violations[0] if policy_result.violations else None
        
        return PipelineResult(
            event_id=uuid4(),
            tenant_id=input_data.tenant_id,
            session_id=input_data.session_id,
            intent="policy_blocked",
            intent_category="blocked",
            funnel_stage=input_data.current_funnel_stage,
            action="block",
            confidence=1.0,
            safety_concern=True,
            safety_category=violation.category.value if violation else "policy",
            needs_rag=False,
            handoff_required=policy_result.should_handoff,
            reasoning=f"Message blocked by policy: {violation.message if violation else 'Policy violation'}",
            state_patch={},
            system_prompt="Você é um assistente que precisa redirecionar a conversa.",
            instructions="Informe ao usuário que não é possível processar esta mensagem conforme as políticas.",
            constraints=policy_result.constraints or [],
            mode="safety",
            processing_time_ms=int(sum(stage_timings.values())),
            stage_timings=stage_timings,
        )

    def _build_result(
        self,
        input_data: PipelineInput,
        srm_decision: SRMDecisionModel,
        generated_prompt: GeneratedPrompt,
        context_response,
        effective_mode: str,
        handoff_response: Optional[HandoffResponse],
        stage_timings: dict[str, float],
        total_time: int,
        context_sources: Optional[list[dict[str, Any]]] = None,
        should_pause: bool = False,
        paused_until: Optional[str] = None,
    ) -> PipelineResult:
        """Build the final pipeline result."""
        # Extract prompt messages
        system_prompt = ""
        instructions = ""
        for msg in generated_prompt.messages:
            if msg.role.value == "system":
                system_prompt = msg.content
            elif msg.role.value == "assistant" and not instructions:
                instructions = msg.content
        
        # Get constraints from prompt variables if available
        constraints = []
        if hasattr(generated_prompt, 'config') and generated_prompt.config:
            constraints = getattr(generated_prompt.config, 'constraints', [])
        
        # Build recent messages from context
        recent_messages = []
        if context_response.context and context_response.context.messages:
            recent_messages = [
                {
                    "role": m.role.value,
                    "content": m.content,
                    "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                }
                for m in context_response.context.messages
            ]
        
        # Build handoff summary if needed
        handoff_summary = None
        handoff_user_message = None
        if handoff_response:
            s = handoff_response.summary
            u = s.urgency if s else None
            sec = getattr(s, "sector", None) if s else None
            sla_val = getattr(s, "sla", None) if s else None
            handoff_summary = {
                "id": str(handoff_response.id),
                "status": handoff_response.status.value,
                "summary": s.summary_text if s else "",
                "urgency": u.value if u and hasattr(u, "value") else (u or "normal"),
                "sector": sec.value if sec and hasattr(sec, "value") else (sec if isinstance(sec, str) else None),
                "sla": sla_val.value if sla_val and hasattr(sla_val, "value") else (sla_val if isinstance(sla_val, str) else None),
            }
            handoff_user_message = handoff_response.user_message
        
        # Build state patch (Decision Trace: policy_refs, constraints)
        state_patch = {
            "funnel_stage": srm_decision.state_patch.funnel_stage,
            "last_intent": srm_decision.state_patch.last_intent,
            "last_action": srm_decision.state_patch.last_action,
            "slots": srm_decision.state_patch.slots,
            "flags": srm_decision.state_patch.flags,
            "sentiment_label": srm_decision.state_patch.sentiment_label,
            "sentiment_score": srm_decision.state_patch.sentiment_score,
            "sentiment_confidence": srm_decision.state_patch.sentiment_confidence,
            "sales_profile": srm_decision.state_patch.sales_profile,
            "authority_done": srm_decision.state_patch.authority_done,
            "objection_active": srm_decision.state_patch.objection_active,
            "lead_name": srm_decision.state_patch.lead_name,
            "already_patient": srm_decision.state_patch.already_patient,
            "policy_refs": getattr(srm_decision.state_patch, "policy_refs", None) or [],
            "constraints": getattr(srm_decision.state_patch, "constraints", None) or [],
        }
        
        return PipelineResult(
            event_id=uuid4(),
            tenant_id=input_data.tenant_id,
            session_id=input_data.session_id,
            intent=srm_decision.intent,
            intent_category=srm_decision.intent_category.value,
            funnel_stage=srm_decision.funnel_stage.value,
            funnel_stage_before=input_data.current_funnel_stage if isinstance(input_data.current_funnel_stage, str) else (getattr(input_data.current_funnel_stage, "value", None) or str(input_data.current_funnel_stage)),
            action=srm_decision.action.value,
            confidence=srm_decision.confidence,
            safety_concern=srm_decision.safety_concern,
            safety_category=srm_decision.safety_category.value,
            sentiment_label=srm_decision.sentiment_label.value,
            sentiment_score=srm_decision.sentiment_score,
            sentiment_confidence=srm_decision.sentiment_confidence,
            needs_rag=srm_decision.needs_rag,
            handoff_required=srm_decision.handoff_required,
            handoff_reason=srm_decision.handoff_reason.value if srm_decision.handoff_reason else None,
            reasoning=srm_decision.reasoning,
            state_patch=state_patch,
            system_prompt=system_prompt,
            instructions=instructions,
            constraints=constraints,
            context_summary=context_response.context.compressed_history if context_response.context else None,
            recent_messages=recent_messages,
            context_sources=context_sources or [],
            turn_count=input_data.turn_count + 1,
            mode=effective_mode,
            handoff_summary=handoff_summary,
            handoff_user_message=handoff_user_message,
            should_pause=should_pause,
            paused_until=paused_until,
            processing_time_ms=total_time,
            stage_timings=stage_timings,
        )

    def _record_metrics(self, result: PipelineResult) -> None:
        """Record Prometheus metrics."""
        PIPELINE_TOTAL.labels(
            mode=result.mode,
            result="success",
        ).inc()
        
        PIPELINE_LATENCY.observe(result.processing_time_ms / 1000)
        
        for stage, timing in result.stage_timings.items():
            PIPELINE_STAGE_LATENCY.labels(stage=stage).observe(timing / 1000)


# ============================================
# Singleton Instance
# ============================================

_pipeline: Optional[CognitivePipeline] = None


def get_cognitive_pipeline() -> CognitivePipeline:
    """Get or create the global CognitivePipeline instance."""
    global _pipeline
    if _pipeline is None:
        _pipeline = CognitivePipeline()
    return _pipeline


async def process_event(
    tenant_id: UUID,
    session_id: UUID,
    message: str,
    channel: str = "unknown",
    user_id: Optional[str] = None,
    current_funnel_stage: str = "lead_novo",
    turn_count: int = 0,
    slots: Optional[dict] = None,
    flags: Optional[dict] = None,
    message_history: Optional[list[dict]] = None,
) -> PipelineResult:
    """
    Convenience function for processing events through the pipeline.
    
    Args:
        tenant_id: Tenant identifier
        session_id: Session identifier
        message: User message
        channel: Communication channel
        user_id: Optional user identifier
        current_funnel_stage: Current funnel stage
        turn_count: Number of turns in conversation
        slots: Session slot values
        flags: Session flags
        message_history: Recent message history
        
    Returns:
        PipelineResult with complete cognitive decision
    """
    pipeline = get_cognitive_pipeline()
    
    input_data = PipelineInput(
        tenant_id=tenant_id,
        session_id=session_id,
        message=message,
        channel=channel,
        user_id=user_id,
        current_funnel_stage=current_funnel_stage,
        turn_count=turn_count,
        slots=slots or {},
        flags=flags or {},
        message_history=message_history or [],
    )
    
    return await pipeline.process(input_data)

