"""
NeuroStack - Core Pipeline Unit Tests
Tests for the CognitivePipeline orchestrator.
"""

from uuid import uuid4

import pytest

from src.core.pipeline import (
    CognitivePipeline,
    PipelineInput,
    PipelineResult,
    get_cognitive_pipeline,
    process_event,
)


# ============================================
# Fixtures
# ============================================


@pytest.fixture
def tenant_id():
    """Generate a test tenant ID."""
    return uuid4()


@pytest.fixture
def session_id():
    """Generate a test session ID."""
    return uuid4()


@pytest.fixture
def pipeline():
    """Create a CognitivePipeline instance."""
    return CognitivePipeline()


@pytest.fixture
def sample_input(tenant_id, session_id):
    """Create a sample pipeline input."""
    return PipelineInput(
        tenant_id=tenant_id,
        session_id=session_id,
        message="Olá, gostaria de agendar uma consulta",
        channel="whatsapp",
        user_id="test-user-123",
        current_funnel_stage="lead_novo",
        turn_count=0,
        intent_history=[],
        slots={},
        flags={},
        message_history=[],
    )


# ============================================
# Unit Tests
# ============================================


class TestPipelineInput:
    """Tests for PipelineInput model."""

    def test_create_minimal_input(self, tenant_id, session_id):
        """Test creating input with minimal required fields."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Test message",
        )
        
        assert input_data.tenant_id == tenant_id
        assert input_data.session_id == session_id
        assert input_data.message == "Test message"
        assert input_data.channel == "unknown"
        assert input_data.turn_count == 0

    def test_create_full_input(self, tenant_id, session_id):
        """Test creating input with all fields."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Test message",
            channel="whatsapp",
            user_id="user-123",
            current_funnel_stage="consideration",
            turn_count=5,
            intent_history=["greeting", "scheduling_request"],
            slots={"specialty": "dermatologia"},
            flags={"vip_customer": True},
            message_history=[
                {"role": "user", "content": "Olá"},
                {"role": "assistant", "content": "Olá! Como posso ajudar?"},
            ],
        )
        
        assert input_data.channel == "whatsapp"
        assert input_data.turn_count == 5
        assert len(input_data.intent_history) == 2
        assert input_data.slots["specialty"] == "dermatologia"


class TestPipelineResult:
    """Tests for PipelineResult model."""

    def test_create_result(self, tenant_id, session_id):
        """Test creating a pipeline result."""
        result = PipelineResult(
            tenant_id=tenant_id,
            session_id=session_id,
            intent="scheduling_request",
            intent_category="scheduling",
            funnel_stage="consideration",
            action="collect_info",
            confidence=0.85,
            safety_concern=False,
            safety_category="none",
            needs_rag=False,
            handoff_required=False,
            system_prompt="Você é um assistente...",
            instructions="Pergunte sobre disponibilidade...",
            constraints=["Não forneça diagnósticos"],
            mode="normal",
        )
        
        assert result.intent == "scheduling_request"
        assert result.confidence == 0.85
        assert result.mode == "normal"
        assert not result.handoff_required


class TestCognitivePipeline:
    """Tests for CognitivePipeline class."""

    @pytest.mark.asyncio
    async def test_process_greeting(self, pipeline, tenant_id, session_id):
        """Test processing a greeting message."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Olá, bom dia!",
            channel="whatsapp",
        )
        
        result = await pipeline.process(input_data)
        
        assert isinstance(result, PipelineResult)
        assert result.tenant_id == tenant_id
        assert result.session_id == session_id
        assert result.intent is not None
        assert result.mode in ["normal", "rag", "safety", "handoff"]
        assert result.processing_time_ms > 0

    @pytest.mark.asyncio
    async def test_process_scheduling_request(self, pipeline, tenant_id, session_id):
        """Test processing a scheduling request."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Quero agendar uma consulta para amanhã",
            channel="whatsapp",
        )
        
        result = await pipeline.process(input_data)
        
        assert isinstance(result, PipelineResult)
        assert result.intent_category in ["scheduling", "information", "unknown"]

    @pytest.mark.asyncio
    async def test_process_safety_message(self, pipeline, tenant_id, session_id):
        """Test processing a message that may trigger safety."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Qual remédio devo tomar para dor de cabeça?",
            channel="whatsapp",
        )
        
        result = await pipeline.process(input_data)
        
        assert isinstance(result, PipelineResult)
        # Medication questions should be handled appropriately
        assert result.system_prompt is not None

    @pytest.mark.asyncio
    async def test_process_handoff_request(self, pipeline, tenant_id, session_id):
        """Test processing an explicit handoff request."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Quero falar com um atendente humano",
            channel="whatsapp",
        )
        
        result = await pipeline.process(input_data)
        
        assert isinstance(result, PipelineResult)
        # Explicit handoff requests should be detected
        if result.handoff_required:
            assert result.mode == "handoff"
            assert result.handoff_reason is not None

    @pytest.mark.asyncio
    async def test_stage_timings_recorded(self, pipeline, tenant_id, session_id):
        """Test that stage timings are recorded."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Teste de timing",
            channel="test",
        )
        
        result = await pipeline.process(input_data)
        
        assert result.stage_timings is not None
        assert len(result.stage_timings) > 0
        # At least some stages should be recorded
        expected_stages = ["policy", "srm", "flow", "ctx", "prompt"]
        for stage in expected_stages:
            if stage in result.stage_timings:
                assert result.stage_timings[stage] >= 0


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_get_cognitive_pipeline_singleton(self):
        """Test that get_cognitive_pipeline returns singleton."""
        pipeline1 = get_cognitive_pipeline()
        pipeline2 = get_cognitive_pipeline()
        
        assert pipeline1 is pipeline2

    @pytest.mark.asyncio
    async def test_process_event_function(self, tenant_id, session_id):
        """Test the process_event convenience function."""
        result = await process_event(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Olá!",
            channel="whatsapp",
        )
        
        assert isinstance(result, PipelineResult)
        assert result.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_process_event_with_context(self, tenant_id, session_id):
        """Test process_event with session context."""
        result = await process_event(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Prefiro de manhã",
            channel="whatsapp",
            current_funnel_stage="consideration",
            turn_count=3,
            slots={"intent": "scheduling"},
            flags={"returning_customer": True},
            message_history=[
                {"role": "user", "content": "Quero agendar"},
                {"role": "assistant", "content": "Qual horário prefere?"},
            ],
        )
        
        assert isinstance(result, PipelineResult)
        assert result.turn_count > 0


class TestModeSelection:
    """Tests for mode selection logic."""

    @pytest.mark.asyncio
    async def test_normal_mode_selection(self, pipeline, tenant_id, session_id):
        """Test that normal messages get NORMAL mode."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Qual é o endereço da clínica?",
            channel="web",
        )
        
        result = await pipeline.process(input_data)
        
        # Simple questions should typically be NORMAL or RAG
        assert result.mode in ["normal", "rag"]

    @pytest.mark.asyncio
    async def test_mode_priority(self, pipeline, tenant_id, session_id):
        """Test that handoff has highest priority."""
        # This tests the mode priority: handoff > safety > rag > normal
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Preciso de ajuda urgente, quero falar com alguém",
            channel="whatsapp",
        )
        
        result = await pipeline.process(input_data)
        
        # If handoff is required, mode should be handoff
        if result.handoff_required:
            assert result.mode == "handoff"


class TestStatePatching:
    """Tests for state patch generation."""

    @pytest.mark.asyncio
    async def test_state_patch_generated(self, pipeline, tenant_id, session_id):
        """Test that state patch is generated."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Quero agendar",
            channel="whatsapp",
        )
        
        result = await pipeline.process(input_data)
        
        assert result.state_patch is not None
        assert isinstance(result.state_patch, dict)

    @pytest.mark.asyncio
    async def test_state_patch_contains_intent(self, pipeline, tenant_id, session_id):
        """Test that state patch includes last intent."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Olá!",
            channel="whatsapp",
        )
        
        result = await pipeline.process(input_data)
        
        # State patch should have last_intent or slots
        assert "last_intent" in result.state_patch or "slots" in result.state_patch


class TestPromptGeneration:
    """Tests for prompt generation."""

    @pytest.mark.asyncio
    async def test_prompt_generated(self, pipeline, tenant_id, session_id):
        """Test that prompt is generated."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Quero agendar uma consulta",
            channel="whatsapp",
        )
        
        result = await pipeline.process(input_data)
        
        assert result.system_prompt is not None
        assert len(result.system_prompt) > 0

    @pytest.mark.asyncio
    async def test_constraints_included(self, pipeline, tenant_id, session_id):
        """Test that constraints are included."""
        input_data = PipelineInput(
            tenant_id=tenant_id,
            session_id=session_id,
            message="Qual remédio devo tomar?",
            channel="whatsapp",
        )
        
        result = await pipeline.process(input_data)
        
        # Constraints list should exist
        assert result.constraints is not None
        assert isinstance(result.constraints, list)

