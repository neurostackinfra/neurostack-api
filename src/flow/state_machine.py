"""
NeuroStack - Flow Manager - State Machine
Defines the state machine and valid transitions between flow modes.
"""

import logging
from typing import Optional

from src.flow.models import (
    FlowMode,
    FlowState,
    FlowTransition,
    TransitionResult,
    TransitionTrigger,
)

logger = logging.getLogger(__name__)


# ============================================
# Transition Rules
# ============================================

# Define valid transitions: (from_mode, trigger) -> to_mode
VALID_TRANSITIONS: dict[tuple[FlowMode, TransitionTrigger], FlowMode] = {
    # From NORMAL
    (FlowMode.NORMAL, TransitionTrigger.RAG_REQUIRED): FlowMode.RAG,
    (FlowMode.NORMAL, TransitionTrigger.SAFETY_CONCERN_DETECTED): FlowMode.SAFETY,
    (FlowMode.NORMAL, TransitionTrigger.HANDOFF_REQUESTED): FlowMode.HANDOFF,
    (FlowMode.NORMAL, TransitionTrigger.CONTINUE_NORMAL): FlowMode.NORMAL,
    # From RAG
    (FlowMode.RAG, TransitionTrigger.RAG_COMPLETED): FlowMode.NORMAL,
    (FlowMode.RAG, TransitionTrigger.SAFETY_CONCERN_DETECTED): FlowMode.SAFETY,
    (FlowMode.RAG, TransitionTrigger.HANDOFF_REQUESTED): FlowMode.HANDOFF,
    (FlowMode.RAG, TransitionTrigger.RESET_TO_NORMAL): FlowMode.NORMAL,
    # From SAFETY
    (FlowMode.SAFETY, TransitionTrigger.SAFETY_RESOLVED): FlowMode.NORMAL,
    (FlowMode.SAFETY, TransitionTrigger.HANDOFF_REQUESTED): FlowMode.HANDOFF,
    (FlowMode.SAFETY, TransitionTrigger.RESET_TO_NORMAL): FlowMode.NORMAL,
    # From HANDOFF
    (FlowMode.HANDOFF, TransitionTrigger.HANDOFF_COMPLETED): FlowMode.NORMAL,
    (FlowMode.HANDOFF, TransitionTrigger.HANDOFF_CANCELLED): FlowMode.NORMAL,
    (FlowMode.HANDOFF, TransitionTrigger.SAFETY_CONCERN_DETECTED): FlowMode.SAFETY,
    # Universal transitions
    (FlowMode.NORMAL, TransitionTrigger.SESSION_END): FlowMode.NORMAL,
    (FlowMode.RAG, TransitionTrigger.SESSION_END): FlowMode.NORMAL,
    (FlowMode.SAFETY, TransitionTrigger.SESSION_END): FlowMode.NORMAL,
    (FlowMode.HANDOFF, TransitionTrigger.SESSION_END): FlowMode.NORMAL,
}

# Priority of modes (higher = more priority, overrides lower)
MODE_PRIORITY: dict[FlowMode, int] = {
    FlowMode.NORMAL: 0,
    FlowMode.RAG: 1,
    FlowMode.SAFETY: 3,  # Safety has highest priority
    FlowMode.HANDOFF: 2,
}


class FlowStateMachine:
    """
    State machine for managing flow transitions.

    Handles validation, execution, and logging of state transitions
    between flow modes (NORMAL, RAG, SAFETY, HANDOFF).
    """

    def __init__(self, max_history: int = 50):
        """
        Initialize the state machine.

        Args:
            max_history: Maximum transition history to keep
        """
        self.max_history = max_history
        self._transitions = VALID_TRANSITIONS
        self._mode_priority = MODE_PRIORITY

    def can_transition(
        self,
        from_mode: FlowMode,
        trigger: TransitionTrigger,
    ) -> tuple[bool, Optional[FlowMode]]:
        """
        Check if a transition is valid.

        Args:
            from_mode: Current mode
            trigger: Transition trigger

        Returns:
            Tuple of (is_valid, target_mode)
        """
        key = (from_mode, trigger)
        if key in self._transitions:
            return True, self._transitions[key]
        return False, None

    def get_target_mode(
        self,
        from_mode: FlowMode,
        trigger: TransitionTrigger,
    ) -> Optional[FlowMode]:
        """Get the target mode for a transition."""
        _, target = self.can_transition(from_mode, trigger)
        return target

    def should_override(
        self,
        current_mode: FlowMode,
        requested_mode: FlowMode,
    ) -> bool:
        """
        Check if a mode should override the current mode.

        Higher priority modes can override lower priority ones.

        Args:
            current_mode: Current flow mode
            requested_mode: Requested new mode

        Returns:
            True if transition should proceed
        """
        current_priority = self._mode_priority.get(current_mode, 0)
        requested_priority = self._mode_priority.get(requested_mode, 0)
        return requested_priority >= current_priority

    def execute_transition(
        self,
        state: FlowState,
        trigger: TransitionTrigger,
        reason: str = "",
        metadata: Optional[dict[str, object]] = None,
    ) -> tuple[TransitionResult, FlowState]:
        """
        Execute a state transition.

        Args:
            state: Current flow state
            trigger: Transition trigger
            reason: Reason for transition
            metadata: Additional metadata

        Returns:
            Tuple of (result, updated_state)
        """
        # Check if state is locked
        if state.is_locked:
            logger.warning(
                f"Transition blocked: state is locked for session {state.session_id}"
            )
            return TransitionResult.BLOCKED, state

        # Validate transition
        is_valid, target_mode = self.can_transition(state.current_mode, trigger)

        if not is_valid:
            logger.debug(
                f"Invalid transition: {state.current_mode} -> ? via {trigger} "
                f"for session {state.session_id}"
            )
            return TransitionResult.INVALID, state

        if target_mode is None:
            logger.error(
                "Valid transition has no target: %s via %s",
                state.current_mode,
                trigger,
            )
            return TransitionResult.INVALID, state

        # Check if it's the same mode
        if (
            target_mode == state.current_mode
            and trigger == TransitionTrigger.CONTINUE_NORMAL
        ):
            return TransitionResult.NO_CHANGE, state

        # Note: We don't block valid transitions by priority.
        # Priority is used for external trigger decisions, not for valid state machine transitions.
        # All transitions in VALID_TRANSITIONS are allowed.

        # Create transition record
        transition = FlowTransition(
            from_mode=state.current_mode,
            to_mode=target_mode,
            trigger=trigger,
            session_id=state.session_id,
            tenant_id=state.tenant_id,
            reason=reason,
            metadata=metadata or {},
        )

        # Update state
        new_state = state.model_copy()
        new_state.previous_mode = state.current_mode
        new_state.current_mode = target_mode
        new_state.transition_count += 1
        new_state.mode_entry_time = transition.timestamp

        # Add to history (maintain max size)
        new_state.transition_history.append(transition)
        if len(new_state.transition_history) > self.max_history:
            new_state.transition_history = new_state.transition_history[
                -self.max_history :
            ]

        logger.info(
            f"Transition: {state.current_mode} -> {target_mode} via {trigger} "
            f"for session {state.session_id} ({reason})"
        )

        return TransitionResult.SUCCESS, new_state

    def determine_trigger(
        self,
        safety_concern: bool,
        handoff_required: bool,
        needs_rag: bool,
        current_mode: FlowMode,
    ) -> TransitionTrigger:
        """
        Determine the appropriate trigger based on conditions.

        Args:
            safety_concern: Whether a safety concern was detected
            handoff_required: Whether handoff is required
            needs_rag: Whether RAG is needed
            current_mode: Current flow mode

        Returns:
            Appropriate transition trigger
        """
        # Priority order: Safety > Handoff > RAG > Normal

        if safety_concern and current_mode != FlowMode.SAFETY:
            return TransitionTrigger.SAFETY_CONCERN_DETECTED

        if handoff_required and current_mode != FlowMode.HANDOFF:
            return TransitionTrigger.HANDOFF_REQUESTED

        if needs_rag and current_mode == FlowMode.NORMAL:
            return TransitionTrigger.RAG_REQUIRED

        if current_mode == FlowMode.RAG and not needs_rag:
            return TransitionTrigger.RAG_COMPLETED

        if current_mode == FlowMode.SAFETY and not safety_concern:
            return TransitionTrigger.SAFETY_RESOLVED

        return TransitionTrigger.CONTINUE_NORMAL

    def get_valid_transitions(self, from_mode: FlowMode) -> list[TransitionTrigger]:
        """Get all valid transitions from a mode."""
        return [
            trigger
            for (mode, trigger), _ in self._transitions.items()
            if mode == from_mode
        ]


# ============================================
# Singleton Instance
# ============================================

_state_machine: Optional[FlowStateMachine] = None


def get_state_machine() -> FlowStateMachine:
    """Get or create the global FlowStateMachine instance."""
    global _state_machine
    if _state_machine is None:
        _state_machine = FlowStateMachine()
    return _state_machine
