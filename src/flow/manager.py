"""
NeuroStack - Flow Manager
Main Flow Manager that orchestrates conversation flow modes.
"""

import logging
import time
from typing import Any, Optional
from uuid import UUID

from prometheus_client import Counter, Histogram

from src.flow.handlers import (
    BaseModeHandler,
    HandoffModeHandler,
    NormalModeHandler,
    RagModeHandler,
    SafetyModeHandler,
)
from src.flow.models import (
    FlowContext,
    FlowDecision,
    FlowMode,
    FlowState,
    ModeHandlerResult,
    TransitionResult,
    TransitionTrigger,
)
from src.flow.state_machine import FlowStateMachine, get_state_machine
from src.flow.transitions import TransitionLogger, get_transition_logger

logger = logging.getLogger(__name__)


# ============================================
# Prometheus Metrics
# ============================================

FLOW_TRANSITIONS_TOTAL = Counter(
    "neurostack_flow_transitions_total",
    "Total flow state transitions",
    ["from_mode", "to_mode", "trigger"],
)

FLOW_HANDLER_DURATION = Histogram(
    "neurostack_flow_handler_duration_seconds",
    "Flow handler processing time",
    ["mode"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
)

FLOW_DECISIONS_TOTAL = Counter(
    "neurostack_flow_decisions_total",
    "Total flow decisions made",
    ["mode", "action"],
)


class FlowManager:
    """
    Main Flow Manager.

    Orchestrates conversation flow between different modes:
    - NORMAL: Standard conversation
    - RAG: Knowledge-augmented responses
    - SAFETY: Safety-critical situations
    - HANDOFF: Human agent handoff

    The Flow Manager uses a state machine to manage transitions
    and mode-specific handlers to process messages.
    """

    def __init__(
        self,
        state_machine: Optional[FlowStateMachine] = None,
        transition_logger: Optional[TransitionLogger] = None,
    ):
        """
        Initialize the Flow Manager.

        Args:
            state_machine: State machine for transitions
            transition_logger: Logger for transition events
        """
        self._state_machine = state_machine or get_state_machine()
        self._transition_logger = transition_logger or get_transition_logger()

        # Initialize handlers
        self._handlers: dict[FlowMode, BaseModeHandler] = {
            FlowMode.NORMAL: NormalModeHandler(),
            FlowMode.RAG: RagModeHandler(),
            FlowMode.SAFETY: SafetyModeHandler(),
            FlowMode.HANDOFF: HandoffModeHandler(),
        }

        # Session states (in production, would be stored in Redis)
        self._states: dict[UUID, FlowState] = {}

    async def process(
        self,
        context: FlowContext,
    ) -> FlowDecision:
        """
        Process a message through the flow manager.

        This is the main entry point for flow management.

        Args:
            context: Flow context with current state

        Returns:
            FlowDecision with processing results
        """
        start_time = time.perf_counter()

        # Get or create session state
        state = self._get_or_create_state(
            context.session_id,
            context.tenant_id,
        )

        # Update context with current mode
        context.current_mode = state.current_mode

        # Determine if transition is needed
        trigger = self._state_machine.determine_trigger(
            safety_concern=context.safety_concern,
            handoff_required=context.handoff_required,
            needs_rag=context.needs_rag,
            current_mode=state.current_mode,
        )

        # Attempt transition if not continuing normal
        transition_result = TransitionResult.NO_CHANGE
        if trigger != TransitionTrigger.CONTINUE_NORMAL:
            transition_result, state = await self._execute_transition(
                state=state,
                trigger=trigger,
                context=context,
            )

            # Update stored state
            self._states[context.session_id] = state

        # Get handler for current mode
        handler = self._handlers.get(state.current_mode)
        if not handler:
            logger.error(f"No handler for mode {state.current_mode}")
            handler = self._handlers[FlowMode.NORMAL]

        # Process with handler
        handler_result = await self._run_handler(handler, context)

        # Log handler events
        await self._transition_logger.log_handler_result(
            session_id=context.session_id,
            tenant_id=context.tenant_id,
            mode=state.current_mode,
            success=handler_result.success,
            processing_time_ms=handler_result.processing_time_ms,
            events_to_log=handler_result.events_to_log,
        )

        # Check if handler wants a transition
        if handler_result.should_transition and handler_result.next_trigger:
            transition_result, state = await self._execute_transition(
                state=state,
                trigger=handler_result.next_trigger,
                context=context,
            )
            self._states[context.session_id] = state

        # Build decision
        processing_time = (time.perf_counter() - start_time) * 1000

        decision = FlowDecision(
            current_mode=state.previous_mode or state.current_mode,
            next_mode=state.current_mode,
            transition_required=transition_result == TransitionResult.SUCCESS,
            trigger=trigger if transition_result == TransitionResult.SUCCESS else None,
            transition_result=transition_result,
            handler_action="process",
            handler_params=handler_result.state_updates,
            response_modifiers=handler.get_response_modifiers(),
            decision_reason=self._build_decision_reason(
                state, trigger, transition_result
            ),
            processing_time_ms=processing_time,
        )

        # Record metrics
        self._record_metrics(decision)

        logger.debug(
            f"Flow decision: {decision.current_mode.value} -> {decision.next_mode.value}, "
            f"transition={decision.transition_required}, time={processing_time:.2f}ms"
        )

        return decision

    async def _execute_transition(
        self,
        state: FlowState,
        trigger: TransitionTrigger,
        context: FlowContext,
    ) -> tuple[TransitionResult, FlowState]:
        """Execute a state transition."""
        # Record exit from current mode
        if state.current_mode != FlowMode.NORMAL:
            duration = state.get_time_in_mode()
            await self._transition_logger.log_mode_exit(
                session_id=context.session_id,
                tenant_id=context.tenant_id,
                mode=state.current_mode,
                duration_seconds=duration,
                reason=trigger.value,
            )

        # Execute transition
        result, new_state = self._state_machine.execute_transition(
            state=state,
            trigger=trigger,
            reason=f"Triggered by {trigger.value}",
            metadata={"intent": context.detected_intent},
        )

        # Log transition if successful
        if result == TransitionResult.SUCCESS and new_state.transition_history:
            transition = new_state.transition_history[-1]
            await self._transition_logger.log_transition(
                transition=transition,
                result=result,
            )

            # Log entry to new mode
            await self._transition_logger.log_mode_entry(
                session_id=context.session_id,
                tenant_id=context.tenant_id,
                mode=new_state.current_mode,
                reason=trigger.value,
            )

            # Record metric
            FLOW_TRANSITIONS_TOTAL.labels(
                from_mode=transition.from_mode.value,
                to_mode=transition.to_mode.value,
                trigger=trigger.value,
            ).inc()

        return result, new_state

    async def _run_handler(
        self,
        handler: BaseModeHandler,
        context: FlowContext,
    ) -> ModeHandlerResult:
        """Run a mode handler with timing."""
        with FLOW_HANDLER_DURATION.labels(mode=handler.mode.value).time():
            return await handler.handle(context)

    def _get_or_create_state(
        self,
        session_id: UUID,
        tenant_id: UUID,
    ) -> FlowState:
        """Get or create flow state for a session."""
        if session_id not in self._states:
            self._states[session_id] = FlowState(
                session_id=session_id,
                tenant_id=tenant_id,
            )
        return self._states[session_id]

    def _build_decision_reason(
        self,
        state: FlowState,
        trigger: TransitionTrigger,
        result: TransitionResult,
    ) -> str:
        """Build a human-readable decision reason."""
        if result == TransitionResult.SUCCESS:
            return f"Transitioned to {state.current_mode.value} via {trigger.value}"
        elif result == TransitionResult.BLOCKED:
            return f"Transition blocked in {state.current_mode.value}"
        elif result == TransitionResult.INVALID:
            return f"Invalid transition from {state.current_mode.value}"
        else:
            return f"Continuing in {state.current_mode.value}"

    def _record_metrics(self, decision: FlowDecision) -> None:
        """Record Prometheus metrics."""
        FLOW_DECISIONS_TOTAL.labels(
            mode=decision.next_mode.value,
            action=decision.handler_action,
        ).inc()

    def get_state(self, session_id: UUID) -> Optional[FlowState]:
        """Get flow state for a session."""
        return self._states.get(session_id)

    def get_handler(self, mode: FlowMode) -> BaseModeHandler:
        """Get handler for a mode."""
        return self._handlers.get(mode, self._handlers[FlowMode.NORMAL])

    def get_prompt_additions(self, mode: FlowMode) -> list[str]:
        """Get prompt additions for a mode."""
        handler = self.get_handler(mode)
        return handler.get_mode_prompt_additions()

    async def reset_session(self, session_id: UUID) -> bool:
        """Reset a session to initial state."""
        if session_id in self._states:
            del self._states[session_id]
            logger.info(f"Reset flow state for session {session_id}")
            return True
        return False


# ============================================
# Singleton Instance
# ============================================

_flow_manager: Optional[FlowManager] = None


def get_flow_manager() -> FlowManager:
    """Get or create the global FlowManager instance."""
    global _flow_manager
    if _flow_manager is None:
        _flow_manager = FlowManager()
    return _flow_manager


async def process_flow(
    session_id: UUID,
    tenant_id: UUID,
    message: str,
    detected_intent: Optional[str] = None,
    intent_confidence: float = 0.0,
    safety_concern: bool = False,
    safety_category: Optional[str] = None,
    handoff_required: bool = False,
    handoff_reason: Optional[str] = None,
    needs_rag: bool = False,
    turn_count: int = 0,
    metadata: Optional[dict[str, Any]] = None,
) -> FlowDecision:
    """
    Convenience function for processing flow.

    Args:
        session_id: Session ID
        tenant_id: Tenant ID
        message: Current message
        detected_intent: Detected intent from SRM
        intent_confidence: Intent confidence
        safety_concern: Whether safety concern detected
        safety_category: Category of safety concern
        handoff_required: Whether handoff required
        handoff_reason: Reason for handoff
        needs_rag: Whether RAG is needed
        turn_count: Current turn count
        metadata: Additional metadata

    Returns:
        FlowDecision
    """
    manager = get_flow_manager()

    context = FlowContext(
        session_id=session_id,
        tenant_id=tenant_id,
        current_mode=FlowMode.NORMAL,  # Will be updated by manager
        detected_intent=detected_intent,
        intent_confidence=intent_confidence,
        safety_concern=safety_concern,
        safety_category=safety_category,
        handoff_required=handoff_required,
        handoff_reason=handoff_reason,
        needs_rag=needs_rag,
        turn_count=turn_count,
        message=message,
        metadata=metadata or {},
    )

    return await manager.process(context)
