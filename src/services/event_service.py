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
        )

        # Process through the complete pipeline
        from src.core.pipeline import PipelineResult
        
        result: PipelineResult = await self._pipeline.process(pipeline_input)

        # Convert pipeline result to API response
        response = self._convert_pipeline_result_to_response(result)

        # Save events to Event Store (non-blocking)
        await self._save_events_to_store(request, result)

        return response

    def _convert_pipeline_result_to_response(
        self, result: "PipelineResult"
    ) -> EventResponse:
        """
        Convert pipeline result to API response.

        Args:
            result: Pipeline result

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

        prompt = PromptOutput(
            system=result.system_prompt,
            instructions=result.instructions,
            constraints=result.constraints,
            context_summary=result.context_summary,
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
        self, request: EventRequest, result: "PipelineResult"
    ) -> None:
        """
        Save events to Event Store (non-blocking).

        Args:
            request: Original event request
            result: Pipeline result
        """
        try:
            event_store = await get_event_store()

            # Save the incoming message event
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
                },
                event_metadata={
                    "processing_time_ms": result.processing_time_ms,
                    "mode": result.mode,
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
        if not self._event_repo:
            self._event_repo = EventRepository(session)

        event = await self._event_repo.get_by_id(event_id)

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
        if not self._event_repo:
            self._event_repo = EventRepository(session)

        if session_id:
            events_list = await self._event_repo.find_by_session(
                tenant_id=tenant_id,
                session_id=session_id,
                skip=offset,
                limit=limit,
                event_type=event_type.value if event_type else None,
            )
        else:
            events_list = await self._event_repo.find_by_tenant(
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
