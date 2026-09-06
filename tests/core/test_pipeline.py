"""
NeuroStack - Core Pipeline Unit Tests
Tests for the CognitivePipeline orchestrator.
"""

from uuid import UUID, uuid4

import pytest
import structlog

from src.core import pipeline as pipeline_module
from src.core.pipeline import (
    CognitivePipeline,
    PipelineInput,
    PipelineResult,
    get_cognitive_pipeline,
    process_event,
)
from src.handoff.summarizer import ConversationSummarizer
from src.handoff.webhook import WebhookDispatcher
from src.services.event_service import EventService
from src.services.handoff_service import HandoffService

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


def _scheduling_link_config():
    return {
        "scheduling": {
            "scheduling_link_enabled": True,
            "scheduling_link_url": "https://clinica.example.com/agendar",
            "scheduling_link_message": "Finalize seu agendamento pelo link:",
        },
        "handoff": {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "permanent",
        },
    }


def _business_hours_config(operations: dict) -> dict:
    """Return a minimal clinic_config exercising only the operations bucket.

    Mirrors the per-bucket shape of ``_scheduling_link_config`` — only the
    fields the assertion depends on are populated so we can pinpoint
    ``custom_vars["working_hours"]`` and its provenance without noise from
    other context sources.
    """
    return {
        "operations": operations,
    }


def _find_working_hours_source(sources: list[dict]) -> dict | None:
    for source in sources:
        if source.get("title") == "Horário de Funcionamento":
            return source
    return None


def _isolated_pipeline():
    handoff_service = HandoffService(
        event_store=None,
        summarizer=ConversationSummarizer(),
        webhook_dispatcher=WebhookDispatcher(),
    )
    return CognitivePipeline(handoff_service=handoff_service)


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
    async def test_scheduling_link_send_turn_dispatches_handoff_and_exact_response(
        self,
        monkeypatch,
        tenant_id,
        session_id,
    ):
        """Link-send turn creates the pending handoff and fixes patient text."""

        def _no_db():
            raise RuntimeError("db disabled in unit test")

        monkeypatch.setattr("src.services.handoff_service.get_db_session", _no_db)
        pipeline = _isolated_pipeline()
        clinic_config = _scheduling_link_config()
        clinic_config["scheduling"][
            "scheduling_link_message"
        ] = "  Finalize seu agendamento pelo link:  "
        expected = "  Finalize seu agendamento pelo link:  \n\nhttps://clinica.example.com/agendar"

        with structlog.testing.capture_logs() as log_records:
            monkeypatch.setattr(
                pipeline_module,
                "logger",
                structlog.get_logger("src.core.pipeline"),
            )
            result = await pipeline.process(
                PipelineInput(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message="sim, pode encaminhar",
                    channel="whatsapp",
                    current_funnel_stage="agendamento_solicitado",
                    turn_count=3,
                    flags={"handoff_confirmation_pending": True},
                    clinic_config=clinic_config,
                )
            )

        assert result.handoff_required is True
        assert result.handoff_reason == "scheduling"
        assert result.should_pause is False
        assert result.deterministic_response == expected
        assert result.state_patch["flags"]["scheduling_link_sent"] is True
        handoff_id = result.state_patch["slots"]["scheduling_link_handoff_id"]
        assert result.handoff_summary["id"] == handoff_id
        assert result.handoff_summary["verification_status"] == "link_sent"
        assert any(
            record.get("event") == "scheduling_link_sent" for record in log_records
        )

        response, *_ = await EventService()._convert_pipeline_result_to_response(
            result,
            user_message="sim, pode encaminhar",
            clinic_config={},
            tenant_id=tenant_id,
        )
        assert response.prompt.suggested_response == expected

    @pytest.mark.asyncio
    async def test_scheduling_link_enrich_turn_updates_same_handoff(
        self,
        monkeypatch,
        tenant_id,
        session_id,
    ):
        """Outcome turn updates the existing handoff and pauses normally."""

        def _no_db():
            raise RuntimeError("db disabled in unit test")

        monkeypatch.setattr("src.services.handoff_service.get_db_session", _no_db)
        pipeline = _isolated_pipeline()

        with structlog.testing.capture_logs() as log_records:
            monkeypatch.setattr(
                pipeline_module,
                "logger",
                structlog.get_logger("src.core.pipeline"),
            )
            send_result = await pipeline.process(
                PipelineInput(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message="sim, pode encaminhar",
                    channel="whatsapp",
                    current_funnel_stage="agendamento_solicitado",
                    turn_count=3,
                    flags={"handoff_confirmation_pending": True},
                    clinic_config=_scheduling_link_config(),
                )
            )
            handoff_id = send_result.state_patch["slots"]["scheduling_link_handoff_id"]

            enrich_result = await pipeline.process(
                PipelineInput(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message="consegui agendar pelo link",
                    channel="whatsapp",
                    current_funnel_stage="agendamento_solicitado",
                    turn_count=4,
                    flags={"scheduling_link_sent": True},
                    slots={"scheduling_link_handoff_id": handoff_id},
                    clinic_config=_scheduling_link_config(),
                )
            )
        detail = await pipeline._handoff.get_handoff(UUID(handoff_id))
        handoffs = await pipeline._handoff.list_handoffs(tenant_id)

        assert enrich_result.handoff_required is False
        assert enrich_result.state_patch["flags"]["scheduling_link_sent"] is False
        assert enrich_result.should_pause is True
        assert detail.verification_status == "concluiu"
        assert len(handoffs) == 1
        assert any(
            record.get("event") == "scheduling_link_verified"
            and record.get("handoff_id") == handoff_id
            and record.get("verification_status") == "concluiu"
            for record in log_records
        )

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

    @pytest.mark.asyncio
    async def test_scheduling_link_send_turn_uses_real_logger(
        self,
        monkeypatch,
        tenant_id,
        session_id,
    ):
        """Regression: the link-send turn must complete under the real structlog logger.

        Previously, tests replaced ``pipeline_module.logger.info`` with a permissive
        ``lambda *args, **kwargs`` that silently swallowed a duplicated ``event`` kwarg.
        The real structlog ``BoundLogger.info`` uses ``event`` as its first positional
        parameter, so ``logger.info("scheduling_link_sent", event="scheduling_link_sent")``
        raises ``TypeError: meth() got multiple values for argument 'event'``. That
        TypeError was then wrapped as :class:`EventServiceError` and the patient never
        received the link.

        This test does NOT monkeypatch ``logger.info`` — reintroducing the ``event=``
        kwarg at the link-send site will fail this test by construction.
        """

        def _no_db():
            raise RuntimeError("db disabled in unit test")

        monkeypatch.setattr("src.services.handoff_service.get_db_session", _no_db)
        pipeline = _isolated_pipeline()
        expected = (
            "Finalize seu agendamento pelo link:\n\nhttps://clinica.example.com/agendar"
        )

        result = await pipeline.process(
            PipelineInput(
                tenant_id=tenant_id,
                session_id=session_id,
                message="sim, pode encaminhar",
                channel="whatsapp",
                current_funnel_stage="agendamento_solicitado",
                turn_count=3,
                flags={"handoff_confirmation_pending": True},
                clinic_config=_scheduling_link_config(),
            )
        )

        assert result.handoff_required is True
        assert result.deterministic_response == expected


class TestWorkingHoursProvenance:
    """Wiring tests for ``custom_vars['working_hours']`` and its context card.

    The pipeline generates the "Horário de atendimento" line from the
    structured ``operations.business_hours`` when configured, and falls back
    to the legacy free-text ``operations.working_hours`` otherwise. The
    audit-facing context card must name the source that actually produced
    the value, never the other one.
    """

    @pytest.mark.asyncio
    async def test_business_hours_populates_working_hours_from_schedule(
        self,
        tenant_id,
        session_id,
    ):
        # Hand-written expected phrase — the test states what the rendered
        # string looks like independently of ``format_business_hours``, so a
        # regression in either the renderer or the wiring surfaces here.
        expected_phrase = "Seg–Sex: 09:00–19:00 | Sáb: 09:00–14:00 | Dom: fechado"

        clinic_config = _business_hours_config(
            {
                "business_hours": {
                    "mon": {"open": "09:00", "close": "19:00"},
                    "tue": {"open": "09:00", "close": "19:00"},
                    "wed": {"open": "09:00", "close": "19:00"},
                    "thu": {"open": "09:00", "close": "19:00"},
                    "fri": {"open": "09:00", "close": "19:00"},
                    "sat": {"open": "09:00", "close": "14:00"},
                },
                # A legacy value that MUST be ignored when the schedule is
                # populated — proves the fallback did not fire.
                "working_hours": "texto legado que nao deveria aparecer",
            }
        )

        result = await CognitivePipeline().process(
            PipelineInput(
                tenant_id=tenant_id,
                session_id=session_id,
                message="Qual o horário de vocês?",
                channel="whatsapp",
                clinic_config=clinic_config,
            )
        )

        # ``custom_vars['working_hours']`` is inlined into the system prompt
        # via ``normal.py:927`` as ``**Horário de atendimento:** <phrase>``.
        assert f"**Horário de atendimento:** {expected_phrase}" in result.system_prompt
        assert "texto legado que nao deveria aparecer" not in result.system_prompt

        source = _find_working_hours_source(result.context_sources)
        assert source is not None, "Horário de Funcionamento card missing"
        assert source["field"] == "operations.business_hours"
        assert source["content"] == expected_phrase

    @pytest.mark.asyncio
    async def test_legacy_working_hours_populates_via_fallback(
        self,
        tenant_id,
        session_id,
    ):
        legacy_text = "Seg-Sex das 8h às 18h"
        clinic_config = _business_hours_config({"working_hours": legacy_text})

        result = await CognitivePipeline().process(
            PipelineInput(
                tenant_id=tenant_id,
                session_id=session_id,
                message="Qual o horário de vocês?",
                channel="whatsapp",
                clinic_config=clinic_config,
            )
        )

        assert f"**Horário de atendimento:** {legacy_text}" in result.system_prompt

        source = _find_working_hours_source(result.context_sources)
        assert source is not None, "Horário de Funcionamento card missing"
        # The card must name the source the value actually came from — the
        # legacy free-text field, not the (empty) structured schedule.
        assert source["field"] == "operations.working_hours"
        assert source["content"] == legacy_text

    @pytest.mark.asyncio
    async def test_neither_business_hours_nor_working_hours_leaves_value_empty(
        self,
        tenant_id,
        session_id,
    ):
        clinic_config = _business_hours_config({"timezone": "America/Sao_Paulo"})

        result = await CognitivePipeline().process(
            PipelineInput(
                tenant_id=tenant_id,
                session_id=session_id,
                message="Qual o horário de vocês?",
                channel="whatsapp",
                clinic_config=clinic_config,
            )
        )

        # No structured schedule and no legacy string → the section line
        # driven by ``normal.py:926 if custom.get("working_hours")`` never
        # renders, and the context card is suppressed by the empty-value
        # filter at ``pipeline.py:737``.
        assert "**Horário de atendimento:**" not in result.system_prompt
        assert _find_working_hours_source(result.context_sources) is None


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
