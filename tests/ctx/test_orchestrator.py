"""
NeuroStack - CTX (Context Orchestrator) Unit Tests
Tests for the Context Orchestrator and all components.
"""

from uuid import uuid4

import pytest

from src.ctx.compression import HistoryCompressor, SlidingWindowCompressor
from src.ctx.context_builder import ContextBuilder, get_context_builder
from src.ctx.models import (
    CognitiveContext,
    ContextMessage,
    ContextRequest,
    KnowledgeChunk,
    MessageRole,
    RelevanceLevel,
)
from src.ctx.orchestrator import ContextOrchestrator, get_context_orchestrator
from src.ctx.relevance import RelevanceFilter, RelevanceScorer, get_relevance_scorer
from src.services import KnowledgeService, MockKnowledgeService

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
def sample_messages():
    """Create sample message history."""
    return [
        ContextMessage(
            role=MessageRole.USER,
            content="Olá, bom dia!",
            intent="greeting_hello",
            confidence=0.95,
        ),
        ContextMessage(
            role=MessageRole.ASSISTANT,
            content="Olá! Como posso ajudá-lo hoje?",
        ),
        ContextMessage(
            role=MessageRole.USER,
            content="Gostaria de agendar uma consulta",
            intent="scheduling_request",
            confidence=0.9,
        ),
        ContextMessage(
            role=MessageRole.ASSISTANT,
            content="Claro! Para quando você gostaria de agendar?",
        ),
        ContextMessage(
            role=MessageRole.USER,
            content="Para amanhã de manhã, se possível",
            intent="scheduling_request",
            confidence=0.85,
        ),
    ]


@pytest.fixture
def context_builder():
    return ContextBuilder()


@pytest.fixture
def relevance_scorer():
    return RelevanceScorer()


@pytest.fixture
def relevance_filter():
    return RelevanceFilter()


@pytest.fixture
def compressor():
    return HistoryCompressor()


@pytest.fixture
def mock_knowledge_service():
    return MockKnowledgeService()


@pytest.fixture
def orchestrator(mock_knowledge_service):
    return ContextOrchestrator(knowledge_service=mock_knowledge_service)


# ============================================
# ContextMessage Tests
# ============================================


class TestContextMessage:
    """Tests for ContextMessage model."""

    def test_message_creation(self):
        """Test basic message creation."""
        msg = ContextMessage(
            role=MessageRole.USER,
            content="Test message",
        )
        assert msg.role == MessageRole.USER
        assert msg.content == "Test message"
        assert msg.timestamp is not None

    def test_to_prompt_format(self):
        """Test conversion to prompt format."""
        msg = ContextMessage(
            role=MessageRole.ASSISTANT,
            content="Response",
        )
        prompt = msg.to_prompt_format()

        assert prompt == {"role": "assistant", "content": "Response"}


# ============================================
# ContextBuilder Tests
# ============================================


class TestContextBuilder:
    """Tests for ContextBuilder."""

    def test_build_simple_context(
        self, context_builder, sample_tenant_id, sample_session_id
    ):
        """Test building a simple context."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Olá",
        )

        context = context_builder.build(request)

        assert context.session_id == sample_session_id
        assert context.tenant_id == sample_tenant_id
        assert len(context.messages) == 1
        assert context.messages[0].content == "Olá"

    def test_build_with_history(
        self, context_builder, sample_tenant_id, sample_session_id, sample_messages
    ):
        """Test building context with message history."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Qual o horário disponível?",
            max_messages=10,
        )

        context = context_builder.build(request, messages=sample_messages)

        # Should include history + current message
        assert len(context.messages) == 6

    def test_build_with_constraints(
        self, context_builder, sample_tenant_id, sample_session_id
    ):
        """Test building context with constraints."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Test",
        )

        constraints = ["Não dar diagnóstico", "Ser educado"]
        context = context_builder.build(request, constraints=constraints)

        assert len(context.constraints) == 2

    def test_build_with_knowledge(
        self, context_builder, sample_tenant_id, sample_session_id
    ):
        """Test building context with knowledge chunks."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Qual o preço?",
        )

        knowledge = [
            KnowledgeChunk(
                id="1",
                content="O preço é R$ 200",
                relevance_score=0.9,
            )
        ]

        context = context_builder.build(request, knowledge_chunks=knowledge)

        assert len(context.knowledge_chunks) == 1

    def test_token_estimation(self, context_builder):
        """Test token estimation."""
        messages = [
            ContextMessage(role=MessageRole.USER, content="Hello world"),
            ContextMessage(role=MessageRole.ASSISTANT, content="Hi there!"),
        ]

        tokens = context_builder._estimate_tokens(messages, None)

        assert tokens > 0
        assert tokens < 100  # Should be a small number

    def test_trim_to_limit(self, context_builder, sample_tenant_id, sample_session_id):
        """Test trimming context to token limit."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Test",
        )

        # Create many long messages
        messages = [
            ContextMessage(
                role=MessageRole.USER,
                content="This is a very long message " * 50,  # Much longer messages
            )
            for _ in range(20)
        ]

        context = context_builder.build(request, messages=messages)
        original_count = len(context.messages)
        context.context_tokens = 50000  # Set very high token count

        trimmed = context_builder.trim_to_token_limit(context, max_tokens=200)

        # Should have trimmed some messages (or kept minimum of 2)
        assert len(trimmed.messages) <= original_count


# ============================================
# RelevanceScorer Tests
# ============================================


class TestRelevanceScorer:
    """Tests for RelevanceScorer."""

    def test_score_message_recent(self, relevance_scorer):
        """Test that recent messages score higher."""
        msg = ContextMessage(
            role=MessageRole.USER,
            content="Hello",
        )

        # Recent message (position 9 of 10)
        recent_score = relevance_scorer.score_message(
            msg, "Hello", position=9, total_messages=10
        )

        # Old message (position 0 of 10)
        old_score = relevance_scorer.score_message(
            msg, "Hello", position=0, total_messages=10
        )

        assert recent_score > old_score

    def test_score_similar_content(self, relevance_scorer):
        """Test that similar content scores higher."""
        msg = ContextMessage(
            role=MessageRole.USER,
            content="Quero agendar uma consulta",
        )

        # Similar query
        similar_score = relevance_scorer.score_message(
            msg, "Quero marcar consulta", position=5, total_messages=10
        )

        # Different query
        different_score = relevance_scorer.score_message(
            msg, "Qual o preço?", position=5, total_messages=10
        )

        assert similar_score >= different_score

    def test_relevance_levels(self, relevance_scorer):
        """Test relevance level mapping."""
        assert relevance_scorer.get_relevance_level(0.9) == RelevanceLevel.CRITICAL
        assert relevance_scorer.get_relevance_level(0.7) == RelevanceLevel.HIGH
        assert relevance_scorer.get_relevance_level(0.5) == RelevanceLevel.MEDIUM
        assert relevance_scorer.get_relevance_level(0.3) == RelevanceLevel.LOW
        assert relevance_scorer.get_relevance_level(0.1) == RelevanceLevel.NONE


# ============================================
# RelevanceFilter Tests
# ============================================


class TestRelevanceFilter:
    """Tests for RelevanceFilter."""

    def test_filter_messages(self, relevance_filter, sample_messages):
        """Test message filtering by relevance."""
        filtered = relevance_filter.filter_messages(
            sample_messages,
            current_message="Quero agendar",
            max_messages=3,
        )

        assert len(filtered) == 3
        # Should keep chronological order
        assert filtered[0].timestamp <= filtered[-1].timestamp

    def test_filter_knowledge(self, relevance_filter):
        """Test knowledge chunk filtering."""
        chunks = [
            KnowledgeChunk(id="1", content="Preço da consulta", relevance_score=0.5),
            KnowledgeChunk(
                id="2", content="Horário de funcionamento", relevance_score=0.9
            ),
            KnowledgeChunk(id="3", content="Endereço da clínica", relevance_score=0.3),
        ]

        filtered = relevance_filter.filter_knowledge(
            chunks,
            current_message="Qual horário funciona?",
            max_chunks=2,
        )

        assert len(filtered) == 2


# ============================================
# HistoryCompressor Tests
# ============================================


class TestHistoryCompressor:
    """Tests for HistoryCompressor."""

    def test_no_compression_needed(self, compressor, sample_messages):
        """Test that short history is not compressed."""
        compressed, summary = compressor.compress(sample_messages, target_tokens=4000)

        assert len(compressed) == len(sample_messages)
        assert summary is None

    def test_compression_needed(self, compressor):
        """Test compression of long history."""
        # Create long history
        messages = [
            ContextMessage(
                role=MessageRole.USER,
                content="This is a long message " * 50,
            )
            for _ in range(20)
        ]

        compressed, summary = compressor.compress(messages, target_tokens=500)

        assert len(compressed) < len(messages)
        # Summary may or may not be generated depending on compression needed

    def test_truncate_long_messages(self, compressor):
        """Test that very long messages are truncated."""
        messages = [
            ContextMessage(
                role=MessageRole.USER,
                content="x" * 1000,  # Very long message
            )
        ]

        truncated = compressor._truncate_messages(messages)

        assert len(truncated[0].content) <= compressor.max_message_length

    def test_sliding_window(self):
        """Test sliding window compressor."""
        window = SlidingWindowCompressor(window_size=3)

        messages = [
            ContextMessage(role=MessageRole.USER, content=f"Message {i}")
            for i in range(10)
        ]

        result = window.compress(messages)

        assert len(result) == 3
        assert result[0].content == "Message 7"


# ============================================
# KnowledgeService Tests
# ============================================


class TestKnowledgeService:
    """Tests for KnowledgeService."""

    @pytest.mark.asyncio
    async def test_mock_service_query(self, mock_knowledge_service, sample_tenant_id):
        """Test mock knowledge service query."""
        results = await mock_knowledge_service.query(
            tenant_id=sample_tenant_id,
            query="Qual o horário de funcionamento?",
        )

        assert len(results) > 0
        assert (
            "horário" in results[0].content.lower()
            or "funciona" in results[0].content.lower()
        )

    @pytest.mark.asyncio
    async def test_mock_service_price_query(
        self, mock_knowledge_service, sample_tenant_id
    ):
        """Test mock knowledge service price query."""
        results = await mock_knowledge_service.query(
            tenant_id=sample_tenant_id,
            query="Qual o preço da consulta?",
        )

        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_real_service_not_connected(self, sample_tenant_id):
        """Test that real service returns empty when not connected."""
        service = KnowledgeService()

        results = await service.query(
            tenant_id=sample_tenant_id,
            query="Test query",
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test health check."""
        service = KnowledgeService()
        health = await service.health_check()

        assert "connected" in health
        assert health["status"] == "not_implemented"


# ============================================
# ContextOrchestrator Tests
# ============================================


class TestContextOrchestrator:
    """Tests for ContextOrchestrator."""

    @pytest.mark.asyncio
    async def test_build_context_simple(
        self, orchestrator, sample_tenant_id, sample_session_id
    ):
        """Test building simple context."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Olá",
        )

        response = await orchestrator.build_context(request)

        assert response.success
        assert response.context.session_id == sample_session_id
        assert len(response.context.messages) == 1

    @pytest.mark.asyncio
    async def test_build_context_with_history(
        self, orchestrator, sample_tenant_id, sample_session_id, sample_messages
    ):
        """Test building context with history."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Confirmo o agendamento",
        )

        response = await orchestrator.build_context(
            request,
            message_history=sample_messages,
        )

        assert response.success
        assert len(response.context.messages) > 1

    @pytest.mark.asyncio
    async def test_build_context_with_rag(
        self, orchestrator, sample_tenant_id, sample_session_id
    ):
        """Test building context with RAG."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Qual o preço da consulta?",
            needs_rag=True,
            include_knowledge=True,
        )

        response = await orchestrator.build_context(request)

        assert response.success
        # Mock service should return results for price query
        assert len(response.context.knowledge_chunks) > 0

    @pytest.mark.asyncio
    async def test_build_context_with_constraints(
        self, orchestrator, sample_tenant_id, sample_session_id
    ):
        """Test building context with constraints."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Test",
        )

        constraints = ["Não fornecer diagnóstico", "Manter tom profissional"]

        response = await orchestrator.build_context(
            request,
            constraints=constraints,
        )

        assert response.success
        assert len(response.context.constraints) == 2

    @pytest.mark.asyncio
    async def test_build_simple_context_convenience(
        self, orchestrator, sample_tenant_id, sample_session_id
    ):
        """Test convenience method for simple context."""
        context = await orchestrator.build_simple_context(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Hello",
        )

        assert context.session_id == sample_session_id
        assert len(context.messages) == 1

    @pytest.mark.asyncio
    async def test_build_time_recorded(
        self, orchestrator, sample_tenant_id, sample_session_id
    ):
        """Test that build time is recorded."""
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Test",
        )

        response = await orchestrator.build_context(request)

        assert response.build_time_ms is not None
        assert response.build_time_ms >= 0

    @pytest.mark.asyncio
    async def test_create_message_helper(self, orchestrator):
        """Test message creation helper."""
        msg = orchestrator.create_message(
            role="user",
            content="Hello",
            intent="greeting",
            confidence=0.9,
        )

        assert msg.role == MessageRole.USER
        assert msg.content == "Hello"
        assert msg.intent == "greeting"


# ============================================
# Singleton Tests
# ============================================


class TestSingletons:
    """Test singleton pattern implementations."""

    def test_context_builder_singleton(self):
        """Test ContextBuilder singleton."""
        b1 = get_context_builder()
        b2 = get_context_builder()
        assert b1 is b2

    def test_relevance_scorer_singleton(self):
        """Test RelevanceScorer singleton."""
        s1 = get_relevance_scorer()
        s2 = get_relevance_scorer()
        assert s1 is s2

    def test_orchestrator_singleton(self):
        """Test ContextOrchestrator singleton."""
        o1 = get_context_orchestrator()
        o2 = get_context_orchestrator()
        assert o1 is o2


# ============================================
# Integration Tests
# ============================================


class TestIntegration:
    """Integration tests for the CTX module."""

    @pytest.mark.asyncio
    async def test_full_context_flow(
        self, orchestrator, sample_tenant_id, sample_session_id, sample_messages
    ):
        """Test complete context building flow."""
        # Build context with all features
        request = ContextRequest(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            current_message="Qual o horário disponível amanhã?",
            needs_rag=True,
            include_knowledge=True,
            max_messages=5,
            max_tokens=2000,
            cognitive_state=CognitiveContext(
                funnel_stage="consideration",
                last_intent="scheduling_request",
                slots={"date": "tomorrow"},
            ),
        )

        constraints = ["Não fornecer diagnóstico", "Ser profissional"]
        safety = ["Em emergência, ligar SAMU"]

        response = await orchestrator.build_context(
            request=request,
            message_history=sample_messages,
            constraints=constraints,
            safety_reminders=safety,
        )

        # Verify response
        assert response.success
        assert response.context is not None

        # Verify context components
        ctx = response.context
        assert ctx.cognitive.funnel_stage == "consideration"
        assert len(ctx.messages) > 0
        assert len(ctx.constraints) == 2
        assert len(ctx.safety_reminders) == 1

        # Verify token estimation
        assert ctx.context_tokens is not None
        assert ctx.context_tokens > 0
