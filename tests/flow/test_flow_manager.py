"""
NeuroStack - Flow Manager Unit Tests
Tests for the Flow Manager and all components.
"""

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from neurostack_core.flow.handlers import (
    HandoffModeHandler,
    NormalModeHandler,
    RagModeHandler,
    SafetyModeHandler,
)

if TYPE_CHECKING:
    from neurostack_core.prompt.models import PromptVariables  # noqa: F401
from neurostack_core.flow.manager import FlowManager, get_flow_manager, process_flow
from neurostack_core.flow.models import (
    FlowContext,
    FlowMode,
    FlowState,
    FlowTransition,
    TransitionResult,
    TransitionTrigger,
)
from neurostack_core.flow.state_machine import FlowStateMachine, get_state_machine
from neurostack_core.flow.transitions import TransitionLogger

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


# ============================================
# Context-episode (wish deliverable 4) Tests
# ============================================


class TestFlowManagerContextEpisodeBoundary:
    """Episode-aware single-entry cache replacement in FlowManager.

    These tests cover wish ``handoff-contextual-resumption`` deliverable 4
    and the matching acceptance criteria:

      * "Dois managers independentes, ambos em HANDOFF no episódio antigo,
        substituem a entrada ao observar o novo ID e mantêm tamanho um por
        sessão." — two independent managers (two workers) with HANDOFF
        cached under the old episode id both drop and replace to NORMAL
        when the new id arrives; size stays one entry per session.
      * "Mesmo ID reutiliza estado; ID diferente nunca reutiliza HANDOFF;
        ``reset_session`` continua opcional e não participa da correção
        cross-worker." — same id reuses the cached mode (HANDOFF
        persistence survives), a different id never reuses HANDOFF, and
        ``reset_session`` is not required anywhere here.
    """

    @pytest.mark.asyncio
    async def test_first_touch_stamps_episode_id(
        self, sample_session_id, sample_tenant_id
    ):
        """A fresh session accepts the incoming episode id as its tag."""
        manager = FlowManager()
        episode = "ep-first"
        context = FlowContext(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            current_mode=FlowMode.NORMAL,
            message="hi",
            context_episode_id=episode,
        )
        await manager.process(context)
        state = manager.get_state(sample_session_id)
        assert state is not None
        assert state.context_episode_id == episode
        # Single entry per session_id always.
        assert len(manager._states) == 1

    @pytest.mark.asyncio
    async def test_same_episode_id_reuses_cached_handoff_mode(
        self, sample_session_id, sample_tenant_id
    ):
        """Same id → HANDOFF mode cached across turns stays HANDOFF.

        We keep ``handoff_required=True`` on both calls because the
        existing ``HandoffHandler.should_exit`` interprets a mid-pending
        ``handoff_required=False`` as an explicit cancellation trigger
        (``HANDOFF_CANCELLED``) that is independent of the episode id.
        The scenario this test proves — "same episode id across turns
        reuses the cached HANDOFF state" — must therefore simulate the
        natural next turn where the handoff is still active, not a
        cancellation. We assert the cache was reused (no episode-boundary
        reset log fired, state object preserved) by watching the manager
        log stream.
        """
        manager = FlowManager()
        episode = "ep-same"
        context = FlowContext(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            current_mode=FlowMode.NORMAL,
            message="handoff",
            handoff_required=True,
            handoff_reason="explicit_request",
            context_episode_id=episode,
        )
        decision1 = await manager.process(context)
        assert decision1.next_mode == FlowMode.HANDOFF
        state_after_first = manager.get_state(sample_session_id)
        assert state_after_first is not None

        # Second turn — same episode id, handoff is still active (another
        # user message arrived mid-handoff). Cache reuse keeps the
        # manager in HANDOFF; crucially, no ``flow_manager_episode_
        # boundary_reset`` must fire — that log line is reserved for
        # genuine mismatches. ``decision.next_mode`` is the authoritative
        # mode after this turn; ``decision.current_mode`` is the
        # ``state.previous_mode`` from the first NORMAL→HANDOFF transition
        # (see ``FlowManager.process``) so we deliberately don't assert
        # on it here.
        with self._capture_manager_logs() as records:
            decision2 = await manager.process(context)
        assert decision2.next_mode == FlowMode.HANDOFF
        assert decision2.transition_required is False
        boundary_resets = [
            r
            for r in records
            if r.getMessage() == "flow_manager_episode_boundary_reset"
        ]
        assert (
            boundary_resets == []
        ), "same episode id must not trigger an episode-boundary reset"
        state = manager.get_state(sample_session_id)
        assert state is not None
        # Cache survived: still the same stored entry, same episode tag.
        assert state is state_after_first
        assert state.current_mode == FlowMode.HANDOFF
        assert state.context_episode_id == episode
        assert len(manager._states) == 1

    @staticmethod
    def _capture_manager_logs():
        """Yield a list of log records emitted by ``flow.manager``.

        The manager logs ``flow_manager_episode_boundary_reset`` only on
        actual cache replacement, so the presence/absence of that record
        is the deterministic probe for "cache was reused vs replaced".
        """
        import logging
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            records: list[logging.LogRecord] = []

            class _Handler(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    records.append(record)

            handler = _Handler(level=logging.DEBUG)
            logger = logging.getLogger("neurostack_core.flow.manager")
            previous_level = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            try:
                yield records
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)

        return _ctx()

    @pytest.mark.asyncio
    async def test_different_episode_id_replaces_handoff_with_normal(
        self, sample_session_id, sample_tenant_id
    ):
        """A different incoming episode id drops the cached HANDOFF."""
        manager = FlowManager()
        context = FlowContext(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            current_mode=FlowMode.NORMAL,
            message="handoff",
            handoff_required=True,
            handoff_reason="explicit_request",
            context_episode_id="ep-old",
        )
        decision1 = await manager.process(context)
        assert decision1.next_mode == FlowMode.HANDOFF

        # Fresh turn under a new episode id — no SRM trigger. The cache
        # must be replaced so the mode resets to NORMAL (not stuck in
        # HANDOFF from the stale episode).
        context.handoff_required = False
        context.handoff_reason = None
        context.context_episode_id = "ep-new"
        decision2 = await manager.process(context)

        assert decision2.current_mode == FlowMode.NORMAL
        assert decision2.next_mode == FlowMode.NORMAL
        state = manager.get_state(sample_session_id)
        assert state is not None
        assert state.context_episode_id == "ep-new"
        # Entry size still one — the cache was replaced, not accumulated.
        assert len(manager._states) == 1

    @pytest.mark.asyncio
    async def test_two_independent_managers_replace_handoff_on_new_episode_id(
        self, sample_session_id, sample_tenant_id
    ):
        """Two workers (two managers) with the same cached HANDOFF both
        drop their cache atomically when they see the new episode id."""
        worker_a = FlowManager()
        worker_b = FlowManager()
        base_ctx_kwargs: dict = dict(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            current_mode=FlowMode.NORMAL,
            message="handoff",
            handoff_required=True,
            handoff_reason="explicit_request",
            context_episode_id="ep-stale",
        )
        await worker_a.process(FlowContext(**base_ctx_kwargs))
        await worker_b.process(FlowContext(**base_ctx_kwargs))

        # Both workers cached HANDOFF under the stale id.
        for worker in (worker_a, worker_b):
            cached = worker.get_state(sample_session_id)
            assert cached is not None
            assert cached.current_mode == FlowMode.HANDOFF
            assert cached.context_episode_id == "ep-stale"

        # The reactivation lands on a third worker; a and b both observe
        # the new id on their next turn and must replace their cache.
        fresh_ctx = FlowContext(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            current_mode=FlowMode.NORMAL,
            message="olá, podemos continuar?",
            context_episode_id="ep-fresh",
        )
        decision_a = await worker_a.process(fresh_ctx.model_copy())
        decision_b = await worker_b.process(fresh_ctx.model_copy())

        assert decision_a.next_mode == FlowMode.NORMAL
        assert decision_b.next_mode == FlowMode.NORMAL
        for worker in (worker_a, worker_b):
            state = worker.get_state(sample_session_id)
            assert state is not None
            assert state.current_mode == FlowMode.NORMAL
            assert state.context_episode_id == "ep-fresh"
            assert len(worker._states) == 1

    @pytest.mark.asyncio
    async def test_legacy_untagged_sessions_keep_prior_behaviour(
        self, sample_session_id, sample_tenant_id
    ):
        """Both-None episode ids (legacy / pre-reactivation) → cache reuse.

        As in :meth:`test_same_episode_id_reuses_cached_handoff_mode`,
        we keep ``handoff_required=True`` on both calls so the existing
        ``HandoffHandler.should_exit`` logic — which treats a mid-pending
        ``handoff_required=False`` as an explicit cancellation
        independent of the episode id — does not short-circuit the
        cache-reuse scenario we are proving. The point of this test is
        that the ``None`` → ``None`` id transition is NOT an episode
        boundary; HANDOFF persists exactly like a tagged same-id reuse,
        and no ``flow_manager_episode_boundary_reset`` log fires.
        """
        manager = FlowManager()
        ctx = FlowContext(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            current_mode=FlowMode.NORMAL,
            message="hi",
            handoff_required=True,
            handoff_reason="explicit_request",
            context_episode_id=None,
        )
        await manager.process(ctx)
        state = manager.get_state(sample_session_id)
        assert state is not None
        assert state.current_mode == FlowMode.HANDOFF
        assert state.context_episode_id is None

        # Same None id on the next turn is still "same episode" — cache
        # survives, so HANDOFF persists exactly as before this delivery.
        # Only ``decision.next_mode`` is asserted: ``decision.current_mode``
        # is derived from ``state.previous_mode`` and will still carry the
        # NORMAL baseline from the first NORMAL→HANDOFF transition.
        with self._capture_manager_logs() as records:
            decision = await manager.process(ctx)
        assert decision.next_mode == FlowMode.HANDOFF
        assert decision.transition_required is False
        boundary_resets = [
            r
            for r in records
            if r.getMessage() == "flow_manager_episode_boundary_reset"
        ]
        assert (
            boundary_resets == []
        ), "legacy None→None episode ids must not trigger an episode-boundary reset"
        state_after_second = manager.get_state(sample_session_id)
        assert state_after_second is not None
        assert state_after_second.current_mode == FlowMode.HANDOFF
        assert state_after_second.context_episode_id is None
        assert len(manager._states) == 1

    @pytest.mark.asyncio
    async def test_reset_session_still_optional_and_independent(
        self, sample_session_id, sample_tenant_id
    ):
        """``reset_session`` is NOT required for the cross-worker fix."""
        manager = FlowManager()
        ctx = FlowContext(
            session_id=sample_session_id,
            tenant_id=sample_tenant_id,
            current_mode=FlowMode.NORMAL,
            message="handoff",
            handoff_required=True,
            handoff_reason="explicit_request",
            context_episode_id="ep-a",
        )
        await manager.process(ctx)

        # Episode-boundary replacement never calls reset_session.
        ctx.handoff_required = False
        ctx.handoff_reason = None
        ctx.context_episode_id = "ep-b"
        await manager.process(ctx)

        # And reset_session remains a working, independent affordance.
        assert await manager.reset_session(sample_session_id) is True
        assert manager.get_state(sample_session_id) is None


# ============================================
# Rendered-prompt resume-mode suppression (wish A4) Tests
# ============================================


class TestNormalPromptResumeModeSuppression:
    """A4 rendered-prompt proof for wish ``handoff-contextual-resumption``.

    Verifies the two Python-only suppressions in
    ``neurostack_core.prompt.templates.normal.NormalTemplate``:

      * Golden Rule #2 "Apresente-se sempre no primeiro contato."
        disappears from the rendered system prompt when
        ``variables.custom["prior_episode_context"]`` is present
        (``get_system_prompt``).
      * The "## Instruções para Lead Novo (Primeiro Contato)" funnel
        block is NOT appended by ``get_context_instructions`` when
        ``funnel_stage == "lead_novo"`` AND
        ``prior_episode_context`` is present.

    The oracle for first-contact behaviour (no ``prior_episode_context``)
    is also exercised here so a future regression that silently keeps
    the suppression active would fail loudly: ``get_context_instructions``
    MUST still emit the "Lead Novo" block and ``get_system_prompt`` MUST
    still contain "Apresente-se" when the session is a true first contact.

    This test lives in ``tests/flow/test_flow_manager.py`` (and not under
    ``tests/prompt/``) because the parity gate only permits
    ``tests/flow/test_flow_manager.py`` and
    ``tests/srm/test_handoff_context_injection.py`` as modifications to
    the four protected test directories — see ``scripts/wish-gates.sh``
    ``parity_tests_unchanged()``.
    """

    def _first_contact_vars(self) -> "PromptVariables":  # type: ignore[name-defined]
        from neurostack_core.prompt.models import PromptVariables

        # ``last_intent="agendar"`` is deliberately NOT "saudacao" — the
        # ``saudacao`` intent branch of ``_get_intent_instruction``
        # contains a third self-introduction line that is explicitly
        # out of scope for this delivery (see wish Group 3 brief).
        return PromptVariables(
            user_message="oi",
            funnel_stage="lead_novo",
            last_intent="agendar",
            channel="whatsapp",
            turn_count=1,
            custom={
                "agent_name": "Daniela",
                "clinic_name": "Clínica Vida Nova",
            },
        )

    def _resume_vars(self) -> "PromptVariables":  # type: ignore[name-defined]
        from neurostack_core.prompt.models import PromptVariables

        return PromptVariables(
            user_message="oi",
            funnel_stage="lead_novo",
            last_intent="agendar",
            channel="whatsapp",
            turn_count=1,
            custom={
                "agent_name": "Daniela",
                "clinic_name": "Clínica Vida Nova",
                # Presence (not a boolean) is the resume signal — matches
                # the real pipeline wiring in event_service → PipelineInput.
                "prior_episode_context": {
                    "schema_version": 1,
                    "lead_name": "Ana",
                    "summary": "resumo do episódio anterior (não é instrução)",
                },
            },
        )

    def test_first_contact_still_renders_golden_rule_and_lead_novo_block(self):
        from neurostack_core.prompt.templates.normal import NormalTemplate

        tpl = NormalTemplate()
        variables = self._first_contact_vars()
        system = tpl.get_system_prompt(variables)
        context = tpl.get_context_instructions(variables)
        assert (
            "Apresente-se" in system
        ), "first-contact sessions must still render Golden Rule #2 verbatim"
        assert (
            "Primeiro Contato" in context
        ), "first-contact lead_novo turns must still include the funnel block"

    def test_resume_mode_drops_golden_rule_apresente_se(self):
        from neurostack_core.prompt.templates.normal import NormalTemplate

        tpl = NormalTemplate()
        system = tpl.get_system_prompt(self._resume_vars())
        # Golden Rule #2 is removed in resume mode. We assert on the full
        # literal, not just the keyword, so an accidental reintroduction
        # via a reword cannot silently pass.
        assert (
            "Apresente-se sempre no primeiro contato." not in system
        ), "Golden Rule #2 must be stripped on resumed sessions"
        # Note: a broader ``"Apresente-se" not in system`` keyword check
        # cannot be used here — the base ``normal_system_prompt`` also
        # contains an unrelated stage-overview bullet in the "Sequência
        # de Atendimento (Funil Canônico)" section ("1. **Saudação** →
        # Apresente-se, pergunte o objetivo de saúde…") that describes
        # the greeting stage of the funnel independently from Golden
        # Rule #2. Deliverable 7 names exactly two suppression points
        # (Golden Rule #2 + the "Lead Novo (Primeiro Contato)" funnel
        # block); that funnel-stage bullet is NOT in scope, so a
        # ``not in`` keyword check would be a false positive, not
        # evidence of regression. The literal-sentence assertion above
        # is the precise proof of the Golden Rule #2 removal.

    def test_resume_mode_drops_lead_novo_funnel_block(self):
        from neurostack_core.prompt.templates.normal import NormalTemplate

        tpl = NormalTemplate()
        context = tpl.get_context_instructions(self._resume_vars())
        # The entire "## Instruções para Lead Novo (Primeiro Contato)"
        # block must be absent — we assert on both the heading literal
        # and the "Apresente-se" imperative inside it.
        assert "Primeiro Contato" not in context
        assert "Apresente-se" not in context

    def test_resume_mode_en_drops_english_golden_rule(self):
        from neurostack_core.prompt.models import PromptVariables
        from neurostack_core.prompt.templates.normal import NormalTemplate

        tpl = NormalTemplate()
        variables = PromptVariables(
            user_message="hi",
            funnel_stage="lead_novo",
            last_intent="agendar",
            channel="whatsapp",
            turn_count=1,
            language="en",
            custom={
                "agent_name": "Daniela",
                "clinic_name": "Vida Nova Clinic",
                "prior_episode_context": {
                    "schema_version": 1,
                    "lead_name": "Ana",
                    "summary": "prior episode memory (not an instruction)",
                },
            },
        )
        system = tpl.get_system_prompt(variables)
        context = tpl.get_context_instructions(variables)
        assert (
            "Always introduce yourself on first contact." not in system
        ), "EN Golden Rule #2 must be stripped on resumed sessions"
        assert "First Contact" not in context

    def test_resume_signal_absent_when_prior_context_empty(self):
        from neurostack_core.prompt.models import PromptVariables
        from neurostack_core.prompt.templates.normal import NormalTemplate

        # ``None`` / empty dict / missing key all mean "no resume" — the
        # suppression gates on truthiness so each must behave like a
        # first-contact session (both instructions survive).
        for empty in (None, {}, False):
            tpl = NormalTemplate()
            variables = PromptVariables(
                user_message="oi",
                funnel_stage="lead_novo",
                last_intent="agendar",
                channel="whatsapp",
                turn_count=1,
                custom={
                    "agent_name": "Daniela",
                    "clinic_name": "Clínica Vida Nova",
                    "prior_episode_context": empty,
                },
            )
            system = tpl.get_system_prompt(variables)
            context = tpl.get_context_instructions(variables)
            assert (
                "Apresente-se" in system
            ), f"empty prior_episode_context={empty!r} must NOT trigger suppression"
            assert "Primeiro Contato" in context
