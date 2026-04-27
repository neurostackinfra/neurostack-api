"""
NeuroStack API - Event Service
Business logic for event processing and management.
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.events import (
    ContextOutput,
    EventDetailResponse,
    EventRequest,
    EventResponse,
    EventType,
    FlowMode,
    HandoffSummary,
    MessageHistory,
    PromptOutput,
    SRMDecision,
    StatePatch,
)

from src.memory import EventData, get_event_store
from src.repositories import EventRepository
from src.core.exceptions import EventStoreError
from src.config.logging import get_logger

logger = get_logger(__name__)


class EventService:
    """Service for processing and managing events."""

    def __init__(
        self,
        pipeline: Optional["CognitivePipeline"] = None,
        event_repo: Optional[EventRepository] = None,
    ):
        """
        Initialize the event service.

        Args:
            pipeline: Cognitive pipeline instance
            event_repo: Event repository instance
        """
        # Lazy import to avoid circular dependency
        if pipeline is None:
            from src.core.pipeline import get_cognitive_pipeline
            pipeline = get_cognitive_pipeline()
        self._pipeline = pipeline
        self._event_repo = event_repo

    async def _load_clinic_config(self, tenant_id: UUID) -> Optional[dict]:
        """Load clinic config from DB for the given tenant."""
        try:
            from src.repositories import get_db_session
            from src.models.clinic_config_table import ClinicConfigTable
            from sqlalchemy import select

            async with get_db_session() as db_session:
                stmt = select(ClinicConfigTable).where(
                    ClinicConfigTable.tenant_id == tenant_id
                )
                result = await db_session.execute(stmt)
                row = result.scalar_one_or_none()
                if row:
                    return {
                        "identity": row.identity or {},
                        "persona": row.persona or {},
                        "operations": row.operations or {},
                        "business_rules": row.business_rules or {},
                        "services": row.services or {},
                        "pricing": row.pricing or {},
                        "handoff": row.handoff or {},
                    }
        except Exception as exc:
            logger.warning("Failed to load clinic config", error=str(exc))
        return None

    async def process_event(
        self,
        request: EventRequest,
        session: Optional[AsyncSession] = None,
    ) -> EventResponse:
        """
        Process an incoming event through the cognitive pipeline.

        Args:
            request: Event request
            session: Optional database session

        Returns:
            Event response with cognitive decision
        """
        # Load clinic config for the tenant (provides identity, services, pricing)
        clinic_config = await self._load_clinic_config(request.tenant_id)

        # Build pipeline input
        # TODO: In production, load session state from StateStore (Redis)
        from src.core.pipeline import PipelineInput

        pipeline_input = PipelineInput(
            tenant_id=request.tenant_id,
            session_id=request.session_id,
            message=request.payload.content,
            channel=request.payload.channel,
            user_id=request.payload.user_id,
            # Session context would come from StateStore
            current_funnel_stage="lead_novo",
            turn_count=0,
            intent_history=[],
            slots={},
            flags={},
            message_history=[],
            clinic_config=clinic_config,
        )

        # Process through the complete pipeline
        from src.core.pipeline import PipelineResult
        
        result: PipelineResult = await self._pipeline.process(pipeline_input)

        # Convert pipeline result to API response (pass user message for LLM)
        response = await self._convert_pipeline_result_to_response(
            result, user_message=request.payload.content
        )

        # Save events to Event Store (non-blocking)
        suggested_response = None
        if response.prompt and response.prompt.suggested_response:
            suggested_response = response.prompt.suggested_response
        await self._save_events_to_store(request, result, suggested_response=suggested_response)

        return response

    async def _convert_pipeline_result_to_response(
        self, result: "PipelineResult", user_message: str = ""
    ) -> EventResponse:
        """
        Convert pipeline result to API response.

        If an LLM provider is configured, calls it to generate suggested_response.

        Args:
            result: Pipeline result
            user_message: Original user message for LLM context

        Returns:
            Event response
        """
        decision = SRMDecision(
            intent=result.intent,
            intent_category=result.intent_category,
            funnel_stage=result.funnel_stage,
            action=result.action,
            confidence=result.confidence,
            safety_concern=result.safety_concern,
            safety_category=result.safety_category,
            sentiment_label=result.sentiment_label,
            sentiment_score=result.sentiment_score,
            sentiment_confidence=result.sentiment_confidence,
            needs_rag=result.needs_rag,
            handoff_required=result.handoff_required,
            handoff_reason=result.handoff_reason,
            reasoning=result.reasoning,
        )

        state_patch = StatePatch(
            slots=result.state_patch.get("slots", {}),
            funnel_stage=result.state_patch.get("funnel_stage"),
            flags=result.state_patch.get("flags", {}),
        )

        # Call LLM to generate suggested_response if configured
        suggested_response = None
        try:
            from src.llm import get_llm_client
            llm = get_llm_client()
            if llm and user_message:
                messages = [
                    {"role": "system", "content": result.system_prompt or ""},
                    {"role": "user", "content": user_message},
                ]
                suggested_response = await llm.generate(messages)
                logger.debug("LLM response generated", length=len(suggested_response))
        except Exception as exc:
            logger.warning("LLM call failed, returning without suggested_response", error=str(exc))

        prompt = PromptOutput(
            system=result.system_prompt,
            instructions=result.instructions,
            constraints=result.constraints,
            context_summary=result.context_summary,
            suggested_response=suggested_response,
        )

        recent_messages = [
            MessageHistory(
                role=m.get("role", "user"),
                content=m.get("content", ""),
                timestamp=m.get("timestamp", datetime.utcnow().isoformat()),
            )
            for m in result.recent_messages
        ]

        context = ContextOutput(
            recent_messages=recent_messages,
            session_summary=result.context_summary,
            rag_sources=[],  # TODO: Add when RAG is implemented
            turn_count=result.turn_count,
        )

        # Map mode string to FlowMode enum
        mode_map = {
            "normal": FlowMode.NORMAL,
            "rag": FlowMode.RAG,
            "safety": FlowMode.SAFETY,
            "handoff": FlowMode.HANDOFF,
        }
        flow_mode = mode_map.get(result.mode, FlowMode.NORMAL)

        # Build handoff summary if available
        handoff_summary = None
        if result.handoff_summary:
            handoff_summary = HandoffSummary(
                reason=result.handoff_reason or "User request",
                summary=result.handoff_summary.get("summary", ""),
                key_info=result.state_patch.get("slots", {}),
                urgency=result.handoff_summary.get("urgency", "normal"),
            )

        return EventResponse(
            event_id=result.event_id,
            decision=decision,
            state_patch=state_patch,
            prompt=prompt,
            context=context,
            mode=flow_mode,
            handoff_summary=handoff_summary,
            processed_at=result.processed_at,
            processing_time_ms=result.processing_time_ms,
        )

    async def _save_events_to_store(
        self, request: EventRequest, result: "PipelineResult",
        suggested_response: Optional[str] = None,
    ) -> None:
        """
        Save events to Event Store (non-blocking).

        Args:
            request: Original event request
            result: Pipeline result
            suggested_response: LLM-generated response (if available)
        """
        try:
            event_store = await get_event_store()

            # Save the incoming message event — enriched with SRM decision data
            # so that trace_service can extract decision fields directly from message.in
            message_event = EventData(
                id=uuid4(),
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                event_type=request.event_type.value,
                event_data={
                    "content": request.payload.content,
                    "channel": request.payload.channel,
                    "user_id": request.payload.user_id,
                    "role": "user",
                    # SRM decision data (enrichment for trace_service)
                    "intent": result.intent,
                    "intent_category": result.intent_category,
                    "funnel_stage": result.funnel_stage,
                    "action": result.action,
                    "confidence": result.confidence,
                    "safety_concern": result.safety_concern,
                    "safety_category": result.safety_category,
                    "needs_rag": result.needs_rag,
                    "handoff_required": result.handoff_required,
                    "handoff_reason": result.handoff_reason,
                    "mode": result.mode,
                    "reasoning": result.reasoning,
                    "sentiment_label": result.sentiment_label,
                    "sentiment_score": result.sentiment_score,
                },
                event_metadata={
                    "processing_time_ms": result.processing_time_ms,
                    "mode": result.mode,
                    # Duplicated for trace_service filter queries (JSONB operators)
                    "safety_concern": result.safety_concern,
                    "handoff_required": result.handoff_required,
                    "intent": result.intent,
                    "funnel_stage": result.funnel_stage,
                },
            )
            await event_store.append(message_event)

            # Decision Trace (CTXDecisionOutput / doc MVP v0.5): version, event_id, risk_flags, policy_refs, etc.
            risk_flags = {
                "safety_concern": result.safety_concern,
                "human_handoff_needed": result.handoff_required,
                "abuse_risk": False,
                "compliance_risk": False,
                "reputacao_risco": False,
                "urgencia_clinica": result.safety_category == "clinical_risk" if result.safety_category else False,
                "lead_irritado": False,
                "pedido_promessa_resultado": False,
            }
            policy_refs = (result.state_patch or {}).get("policy_refs") or []
            constraints = result.constraints if hasattr(result, "constraints") and result.constraints else (result.state_patch or {}).get("constraints") or []

            decision_event = EventData(
                id=result.event_id,
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                event_type="srm.decision",
                event_data={
                    "version": 1,
                    "event_id": str(result.event_id),
                    "timestamp": result.processed_at or datetime.now(timezone.utc).isoformat(),
                    "clinic_id": str(request.tenant_id),
                    "user_id": getattr(request.payload, "user_id", None) if hasattr(request, "payload") else None,
                    "channel": getattr(request.payload, "channel", "unknown") if hasattr(request, "payload") else "unknown",
                    "message_id": None,
                    "intent": result.intent,
                    "action": result.action,
                    "funnel_stage_before": getattr(result, "funnel_stage_before", None),
                    "funnel_stage_after": result.funnel_stage,
                    "risk_flags": risk_flags,
                    "policy_refs": policy_refs,
                    "constraints": constraints,
                    "rag": {"needs_rag": result.needs_rag},
                    "srm": {
                        "intent": result.intent,
                        "intent_category": result.intent_category,
                        "confidence": result.confidence,
                        "mode": result.mode,
                    },
                    "handoff": {
                        "handoff_required": result.handoff_required,
                        "handoff_reason": result.handoff_reason,
                        "handoff_summary": result.handoff_summary,
                    } if result.handoff_required else None,
                    "entities": {},
                    "justificativa_curta": result.reasoning,
                    "intent_category": result.intent_category,
                    "funnel_stage": result.funnel_stage,
                    "confidence": result.confidence,
                    "safety_concern": result.safety_concern,
                    "safety_category": result.safety_category,
                    "needs_rag": result.needs_rag,
                    "handoff_required": result.handoff_required,
                    "handoff_reason": result.handoff_reason,
                    "mode": result.mode,
                    "state_patch": result.state_patch,
                    "reasoning": result.reasoning,
                },
                event_metadata={
                    "processing_time_ms": result.processing_time_ms,
                    "processed_at": result.processed_at if result.processed_at else None,
                },
            )
            await event_store.append(decision_event)

            # Save the prompt generation event
            prompt_event = EventData(
                id=uuid4(),
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                event_type="prompt.generated",
                event_data={
                    "system_prompt": result.system_prompt,
                    "instructions": result.instructions,
                    "constraints": result.constraints,
                    "context_summary": result.context_summary,
                    "mode": result.mode,
                    "intent": result.intent,
                    "funnel_stage": result.funnel_stage,
                    "suggested_response": suggested_response,
                },
                event_metadata={
                    "processing_time_ms": result.processing_time_ms,
                    "processed_at": result.processed_at if result.processed_at else None,
                    "stage_timings": result.stage_timings,
                },
            )
            await event_store.append(prompt_event)

            # CRM integration events (doc MVP v0.5): ns.funnel_stage_changed, ns.handoff_created
            if result.state_patch and result.state_patch.get("funnel_stage"):
                funnel_crm_event = EventData(
                    id=uuid4(),
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="ns.funnel_stage_changed",
                    event_data={
                        "ns_stage": result.funnel_stage,
                        "crm_stage_id": None,
                    },
                    event_metadata={},
                )
                await event_store.append(funnel_crm_event)

            if result.handoff_required and result.handoff_summary:
                handoff_crm_event = EventData(
                    id=uuid4(),
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="ns.handoff_created",
                    event_data={
                        "handoff_id": result.handoff_summary.get("id"),
                        "setor": result.handoff_summary.get("sector"),
                        "prioridade": result.handoff_summary.get("sla"),
                        "resumo": result.handoff_summary.get("summary", ""),
                        "urgency": result.handoff_summary.get("urgency"),
                        "status": result.handoff_summary.get("status"),
                    },
                    event_metadata={},
                )
                await event_store.append(handoff_crm_event)
        except EventStoreError as e:
            # Event Store error - log but don't fail the request
            logger.warning(
                "Failed to save events to Event Store",
                tenant_id=str(request.tenant_id),
                session_id=str(request.session_id),
                error=str(e),
                error_type=type(e).__name__,
            )
        except Exception as e:
            # Unexpected error - log but don't fail the request
            logger.error(
                "Unexpected error saving events to Event Store",
                tenant_id=str(request.tenant_id),
                session_id=str(request.session_id),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )

    async def get_event(
        self, event_id: UUID, session: AsyncSession
    ) -> EventDetailResponse:
        """
        Get event details by ID.

        Args:
            event_id: Event ID
            session: Database session

        Returns:
            Event detail response

        Raises:
            ValueError: If event not found
        """
        event_repo = EventRepository(session)

        event = await event_repo.get_by_id(event_id)

        if not event:
            raise ValueError(f"Event {event_id} not found")

        return EventDetailResponse(
            event_id=event.id,
            tenant_id=event.tenant_id,
            session_id=event.session_id,
            event_type=EventType(event.event_type),
            payload=event.event_data,
            decision=event.event_data.get("decision"),
            created_at=event.created_at.isoformat(),
        )

    async def list_events(
        self,
        tenant_id: UUID,
        session: AsyncSession,
        session_id: Optional[UUID] = None,
        event_type: Optional[EventType] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EventDetailResponse]:
        """
        List events with filtering and pagination.

        Args:
            tenant_id: Tenant ID
            session: Database session
            session_id: Optional session ID filter
            event_type: Optional event type filter
            limit: Maximum events to return
            offset: Offset for pagination

        Returns:
            List of event detail responses
        """
        event_repo = EventRepository(session)

        if session_id:
            events_list = await event_repo.find_by_session(
                tenant_id=tenant_id,
                session_id=session_id,
                skip=offset,
                limit=limit,
                event_type=event_type.value if event_type else None,
            )
        else:
            events_list = await event_repo.find_by_tenant(
                tenant_id=tenant_id,
                skip=offset,
                limit=limit,
                event_type=event_type.value if event_type else None,
            )

        return [
            EventDetailResponse(
                event_id=event.id,
                tenant_id=event.tenant_id,
                session_id=event.session_id,
                event_type=EventType(event.event_type),
                payload=event.event_data,
                decision=event.event_data.get("decision"),
                created_at=event.created_at.isoformat(),
            )
            for event in events_list
        ]


# Singleton instance
_event_service: Optional[EventService] = None


def get_event_service() -> EventService:
    """Get or create the global EventService instance."""
    global _event_service
    if _event_service is None:
        _event_service = EventService()
    return _event_service
