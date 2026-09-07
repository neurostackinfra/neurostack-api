"""
NeuroStack - Flow Manager Unit Tests
Tests for the Flow Manager and all components.
"""

from uuid import uuid4

import pytest

from src.flow.handlers import (
    HandoffModeHandler,
    NormalModeHandler,
    RagModeHandler,
    SafetyModeHandler,
)
from src.flow.manager import FlowManager, get_flow_manager, process_flow
from src.flow.models import (
    FlowContext,
    FlowMode,
    FlowState,
    FlowTransition,
    TransitionResult,
    TransitionTrigger,
)
from src.flow.state_machine import FlowStateMachine, get_state_machine
from src.flow.transitions import TransitionLogger

# ============================================
# Fixtures
# ============================================


@pytest.fixture
def sample_tenant_id():
    return uuid4()


@pytest.fixture
def sample_session_id():
    return uuid4()


@pytest.fixture
def state_machine():
    return FlowStateMachine()


@pytest.fixture
def transition_logger():
    return TransitionLogger()


@pytest.fixture
def flow_manager():
    return FlowManager()


@pytest.fixture
def sample_state(sample_session_id, sample_tenant_id):
    return FlowState(
        session_id=sample_session_id,
        tenant_id=sample_tenant_id,
    )


@pytest.fixture
def sample_context(sample_session_id, sample_tenant_id):
    return FlowContext(
        session_id=sample_session_id,
        tenant_id=sample_tenant_id,
        current_mode=FlowMode.NORMAL,
        message="Test message",
    )


# ============================================
# FlowState Tests
# ============================================


class TestFlowState:
    """Tests for FlowState model."""

    def test_state_creation(self, sample_session_id, sample_tenant_id):
        """Test basic state creation."""
        state = FlowState(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
        )
        assert state.current_mode == FlowMode.NORMAL
        assert state.transition_count == 0
        assert state.is_locked is False

    def test_time_in_mode(self, sample_state):
        """Test time in mode calculation."""
        time_in_mode = sample_state.get_time_in_mode()
        assert time_in_mode >= 0


# ============================================
# StateMachine Tests
# ============================================


class TestStateMachine:
    """Tests for FlowStateMachine."""

    def test_can_transition_normal_to_rag(self, state_machine):
        """Test NORMAL -> RAG transition."""
        can, target = state_machine.can_transition(
            FlowMode.NORMAL,
            TransitionTrigger.RAG_REQUIRED,
        )
        assert can is True
        assert target == FlowMode.RAG

    def test_can_transition_normal_to_safety(self, state_machine):
        """Test NORMAL -> SAFETY transition."""
        can, target = state_machine.can_transition(
            FlowMode.NORMAL,
            TransitionTrigger.SAFETY_CONCERN_DETECTED,
        )
        assert can is True
        assert target == FlowMode.SAFETY

    def test_can_transition_normal_to_handoff(self, state_machine):
        """Test NORMAL -> HANDOFF transition."""
        can, target = state_machine.can_transition(
            FlowMode.NORMAL,
            TransitionTrigger.HANDOFF_REQUESTED,
        )
        assert can is True
        assert target == FlowMode.HANDOFF

    def test_can_transition_rag_to_normal(self, state_machine):
        """Test RAG -> NORMAL transition."""
        can, target = state_machine.can_transition(
            FlowMode.RAG,
            TransitionTrigger.RAG_COMPLETED,
        )
        assert can is True
        assert target == FlowMode.NORMAL

    def test_can_transition_safety_to_normal(self, state_machine):
        """Test SAFETY -> NORMAL transition."""
        can, target = state_machine.can_transition(
            FlowMode.SAFETY,
            TransitionTrigger.SAFETY_RESOLVED,
        )
        assert can is True
        assert target == FlowMode.NORMAL

    def test_can_transition_handoff_to_normal(self, state_machine):
        """Test HANDOFF -> NORMAL transition."""
        can, target = state_machine.can_transition(
            FlowMode.HANDOFF,
            TransitionTrigger.HANDOFF_COMPLETED,
        )
        assert can is True
        assert target == FlowMode.NORMAL

    def test_invalid_transition(self, state_machine):
        """Test invalid transition."""
        can, target = state_machine.can_transition(
            FlowMode.NORMAL,
            TransitionTrigger.HANDOFF_COMPLETED,  # Invalid from NORMAL
        )
        assert can is False
        assert target is None

    def test_execute_transition(self, state_machine, sample_state):
        """Test executing a transition."""
        result, new_state = state_machine.execute_transition(
            state=sample_state,
            trigger=TransitionTrigger.SAFETY_CONCERN_DETECTED,
            reason="Safety test",
        )

        assert result == TransitionResult.SUCCESS
        assert new_state.current_mode == FlowMode.SAFETY
        assert new_state.previous_mode == FlowMode.NORMAL
        assert new_state.transition_count == 1
        assert len(new_state.transition_history) == 1

    def test_execute_transition_locked(self, state_machine, sample_state):
        """Test transition blocked when locked."""
        sample_state.is_locked = True

        result, _ = state_machine.execute_transition(
            state=sample_state,
            trigger=TransitionTrigger.SAFETY_CONCERN_DETECTED,
        )

        assert result == TransitionResult.BLOCKED

    def test_determine_trigger_safety_priority(self, state_machine):
        """Test that safety has highest priority."""
        trigger = state_machine.determine_trigger(
            safety_concern=True,
            handoff_required=True,
            needs_rag=True,
            current_mode=FlowMode.NORMAL,
        )
        assert trigger == TransitionTrigger.SAFETY_CONCERN_DETECTED

    def test_determine_trigger_handoff_priority(self, state_machine):
        """Test that handoff has second priority."""
        trigger = state_machine.determine_trigger(
            safety_concern=False,
            handoff_required=True,
            needs_rag=True,
            current_mode=FlowMode.NORMAL,
        )
        assert trigger == TransitionTrigger.HANDOFF_REQUESTED

    def test_determine_trigger_rag(self, state_machine):
        """Test RAG trigger."""
        trigger = state_machine.determine_trigger(
            safety_concern=False,
            handoff_required=False,
            needs_rag=True,
            current_mode=FlowMode.NORMAL,
        )
        assert trigger == TransitionTrigger.RAG_REQUIRED

    def test_determine_trigger_continue_normal(self, state_machine):
        """Test continue normal."""
        trigger = state_machine.determine_trigger(
            safety_concern=False,
            handoff_required=False,
            needs_rag=False,
            current_mode=FlowMode.NORMAL,
        )
        assert trigger == TransitionTrigger.CONTINUE_NORMAL


# ============================================
# Handler Tests
# ============================================


class TestNormalHandler:
    """Tests for NormalModeHandler."""

    @pytest.mark.asyncio
    async def test_handle_normal(self, sample_context):
        """Test normal handling."""
        handler = NormalModeHandler()
        result = await handler.handle(sample_context)

        assert result.mode == FlowMode.NORMAL
        assert result.success is True
        assert result.should_transition is False

    @pytest.mark.asyncio
    async def test_should_exit_safety(self, sample_context):
        """Test exit to safety."""
        handler = NormalModeHandler()
        sample_context.safety_concern = True

        should_exit, trigger = handler.should_exit(sample_context)

        assert should_exit is True
        assert trigger == TransitionTrigger.SAFETY_CONCERN_DETECTED

    @pytest.mark.asyncio
    async def test_should_exit_handoff(self, sample_context):
        """Test exit to handoff."""
        handler = NormalModeHandler()
        sample_context.handoff_required = True

        should_exit, trigger = handler.should_exit(sample_context)

        assert should_exit is True
        assert trigger == TransitionTrigger.HANDOFF_REQUESTED

    @pytest.mark.asyncio
    async def test_should_exit_rag(self, sample_context):
        """Test exit to rag."""
        handler = NormalModeHandler()
        sample_context.needs_rag = True

        should_exit, trigger = handler.should_exit(sample_context)

        assert should_exit is True
        assert trigger == TransitionTrigger.RAG_REQUIRED


class TestRagHandler:
    """Tests for RagModeHandler."""

    @pytest.mark.asyncio
    async def test_handle_rag(self, sample_context):
        """Test RAG handling."""
        handler = RagModeHandler()
        sample_context.needs_rag = True
        result = await handler.handle(sample_context)

        assert result.mode == FlowMode.RAG
        assert result.success is True

    @pytest.mark.asyncio
    async def test_rag_completes(self, sample_context):
        """Test RAG completion when no longer needed."""
        handler = RagModeHandler()
        sample_context.needs_rag = False

        should_exit, trigger = handler.should_exit(sample_context)

        assert should_exit is True
        assert trigger == TransitionTrigger.RAG_COMPLETED


class TestSafetyHandler:
    """Tests for SafetyModeHandler."""

    @pytest.mark.asyncio
    async def test_handle_safety(self, sample_context):
        """Test safety handling."""
        handler = SafetyModeHandler()
        sample_context.safety_concern = True
        sample_context.safety_category = "clinical_risk"

        result = await handler.handle(sample_context)

        assert result.mode == FlowMode.SAFETY
        assert result.success is True
        assert result.response is not None

    @pytest.mark.asyncio
    async def test_safety_response_clinical(self, sample_context):
        """Test clinical safety response."""
        handler = SafetyModeHandler()
        sample_context.safety_concern = True
        sample_context.safety_category = "clinical_risk"

        result = await handler.handle(sample_context)

        assert "CVV" in result.response or "192" in result.response

    @pytest.mark.asyncio
    async def test_safety_escalation(self, sample_context):
        """Test safety escalation to handoff."""
        handler = SafetyModeHandler()
        sample_context.safety_concern = True
        sample_context.safety_category = "emergency"

        result = await handler.handle(sample_context)

        # Emergency should escalate
        assert result.should_transition is True
        assert result.next_trigger == TransitionTrigger.HANDOFF_REQUESTED


class TestHandoffHandler:
    """Tests for HandoffModeHandler."""

    @pytest.mark.asyncio
    async def test_handle_handoff(self, sample_context):
        """Test handoff handling."""
        handler = HandoffModeHandler()
        sample_context.handoff_required = True
        sample_context.handoff_reason = "explicit_request"

        result = await handler.handle(sample_context)

        assert result.mode == FlowMode.HANDOFF
        assert result.success is True
        assert result.response is not None

    @pytest.mark.asyncio
    async def test_handoff_completes(self, sample_context):
        """Test handoff completion."""
        handler = HandoffModeHandler()
        sample_context.metadata["handoff_status"] = "completed"

        should_exit, trigger = handler.should_exit(sample_context)

        assert should_exit is True
        assert trigger == TransitionTrigger.HANDOFF_COMPLETED


# ============================================
# TransitionLogger Tests
# ============================================


class TestTransitionLogger:
    """Tests for TransitionLogger."""

    @pytest.mark.asyncio
    async def test_log_transition(
        self, transition_logger, sample_session_id, sample_tenant_id
    ):
        """Test logging a transition."""
        transition = FlowTransition(
            from_mode=FlowMode.NORMAL,
            to_mode=FlowMode.SAFETY,
            trigger=TransitionTrigger.SAFETY_CONCERN_DETECTED,
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
        )

        event = await transition_logger.log_transition(
            transition, TransitionResult.SUCCESS
        )

        assert event["event_type"] == "flow.transition"
        assert event["data"]["from_mode"] == "normal"
        assert event["data"]["to_mode"] == "safety"

    @pytest.mark.asyncio
    async def test_log_mode_entry(
        self, transition_logger, sample_session_id, sample_tenant_id
    ):
        """Test logging mode entry."""
        event = await transition_logger.log_mode_entry(
            sample_session_id,
            sample_tenant_id,
            FlowMode.SAFETY,
            reason="test",
        )

        assert "flow.mode.safety.entry" in event["event_type"]

    @pytest.mark.asyncio
    async def test_log_mode_exit(
        self, transition_logger, sample_session_id, sample_tenant_id
    ):
        """Test logging mode exit."""
        event = await transition_logger.log_mode_exit(
            sample_session_id,
            sample_tenant_id,
            FlowMode.SAFETY,
            duration_seconds=10.5,
            reason="resolved",
        )

        assert "flow.mode.safety.exit" in event["event_type"]
        assert event["data"]["duration_seconds"] == 10.5

    @pytest.mark.asyncio
    async def test_buffered_events(self, transition_logger):
        """Test event buffering."""
        # Log some events
        transition = FlowTransition(
            from_mode=FlowMode.NORMAL,
            to_mode=FlowMode.RAG,
            trigger=TransitionTrigger.RAG_REQUIRED,
        )

        await transition_logger.log_transition(transition, TransitionResult.SUCCESS)

        events = transition_logger.get_buffered_events()
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_flush_events(self, transition_logger):
        """Test event flushing."""
        transition = FlowTransition(
            from_mode=FlowMode.NORMAL,
            to_mode=FlowMode.RAG,
            trigger=TransitionTrigger.RAG_REQUIRED,
        )

        await transition_logger.log_transition(transition, TransitionResult.SUCCESS)

        count = await transition_logger.flush_events()
        assert count >= 1
        assert len(transition_logger.get_buffered_events()) == 0


# ============================================
# FlowManager Tests
# ============================================


class TestFlowManager:
    """Tests for FlowManager."""

    @pytest.mark.asyncio
    async def test_process_normal(self, flow_manager, sample_context):
        """Test processing in normal mode."""
        decision = await flow_manager.process(sample_context)

        assert decision.current_mode == FlowMode.NORMAL
        assert decision.next_mode == FlowMode.NORMAL
        assert decision.transition_required is False

    @pytest.mark.asyncio
    async def test_process_transition_to_safety(self, flow_manager, sample_context):
        """Test transition to safety mode."""
        sample_context.safety_concern = True
        sample_context.safety_category = "conduct_risk"  # Use non-escalating category

        decision = await flow_manager.process(sample_context)

        assert decision.next_mode == FlowMode.SAFETY
        assert decision.transition_required is True

    @pytest.mark.asyncio
    async def test_process_transition_to_rag(self, flow_manager, sample_context):
        """Test transition to RAG mode."""
        sample_context.needs_rag = True

        decision = await flow_manager.process(sample_context)

        assert decision.next_mode == FlowMode.RAG
        assert decision.transition_required is True

    @pytest.mark.asyncio
    async def test_process_transition_to_handoff(self, flow_manager, sample_context):
        """Test transition to handoff mode."""
        sample_context.handoff_required = True
        sample_context.handoff_reason = "explicit_request"

        decision = await flow_manager.process(sample_context)

        assert decision.next_mode == FlowMode.HANDOFF
        assert decision.transition_required is True

    @pytest.mark.asyncio
    async def test_get_state(self, flow_manager, sample_context):
        """Test getting session state."""
        await flow_manager.process(sample_context)

        state = flow_manager.get_state(sample_context.session_id)

        assert state is not None
        assert state.session_id == sample_context.session_id

    @pytest.mark.asyncio
    async def test_reset_session(self, flow_manager, sample_context):
        """Test resetting session."""
        await flow_manager.process(sample_context)

        result = await flow_manager.reset_session(sample_context.session_id)

        assert result is True
        assert flow_manager.get_state(sample_context.session_id) is None

    @pytest.mark.asyncio
    async def test_get_prompt_additions(self, flow_manager):
        """Test getting prompt additions."""
        additions = flow_manager.get_prompt_additions(FlowMode.SAFETY)

        assert len(additions) > 0
        assert any("SEGURANÇA" in a.upper() for a in additions)


# ============================================
# Convenience Function Tests
# ============================================


class TestConvenienceFunction:
    """Tests for process_flow convenience function."""

    @pytest.mark.asyncio
    async def test_process_flow(self, sample_session_id, sample_tenant_id):
        """Test process_flow function."""
        decision = await process_flow(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            message="Hello",
        )

        assert decision.current_mode == FlowMode.NORMAL
        assert decision.next_mode == FlowMode.NORMAL

    @pytest.mark.asyncio
    async def test_process_flow_with_safety(self, sample_session_id, sample_tenant_id):
        """Test process_flow with safety concern."""
        decision = await process_flow(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            message="Inappropriate language",
            safety_concern=True,
            safety_category="conduct_risk",  # Non-escalating category
        )

        assert decision.next_mode == FlowMode.SAFETY


# ============================================
# Singleton Tests
# ============================================


class TestSingletons:
    """Test singleton pattern implementations."""

    def test_state_machine_singleton(self):
        """Test FlowStateMachine singleton."""
        sm1 = get_state_machine()
        sm2 = get_state_machine()
        assert sm1 is sm2

    def test_flow_manager_singleton(self):
        """Test FlowManager singleton."""
        fm1 = get_flow_manager()
        fm2 = get_flow_manager()
        assert fm1 is fm2


# ============================================
# Integration Tests
# ============================================


class TestIntegration:
    """Integration tests for the Flow Manager."""

    @pytest.mark.asyncio
    async def test_full_flow_cycle(self, sample_session_id, sample_tenant_id):
        """Test a complete flow cycle: NORMAL -> SAFETY -> NORMAL."""
        manager = FlowManager()

        # Start in normal
        context = FlowContext(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            current_mode=FlowMode.NORMAL,
            message="Hello",
        )

        decision1 = await manager.process(context)
        assert decision1.next_mode == FlowMode.NORMAL

        # Trigger safety
        context.safety_concern = True
        context.safety_category = "conduct_risk"

        decision2 = await manager.process(context)
        assert decision2.next_mode == FlowMode.SAFETY

        # Resolve safety
        context.safety_concern = False
        context.safety_category = None

        decision3 = await manager.process(context)
        assert decision3.next_mode == FlowMode.NORMAL

    @pytest.mark.asyncio
    async def test_rag_to_safety_priority(self, sample_session_id, sample_tenant_id):
        """Test that safety takes priority over RAG."""
        manager = FlowManager()

        # Context for testing
        context = FlowContext(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            current_mode=FlowMode.NORMAL,
            message="Need help",
            needs_rag=True,
            safety_concern=False,
        )

        # First, transition to RAG
        await manager.process(context)

        # Now trigger safety with non-escalating category
        context.safety_concern = True
        context.safety_category = "legal_risk"  # Non-escalating category (level 2)
        decision = await manager.process(context)

        assert decision.next_mode == FlowMode.SAFETY
