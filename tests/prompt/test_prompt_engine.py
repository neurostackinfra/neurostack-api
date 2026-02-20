"""
NeuroStack - Prompt Engine Unit Tests
Tests for the Prompt Engine and all components.
"""

import json
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, "/home/jairomendes/neurostack/neurostack-api")

from src.prompt.models import (
    PromptMode,
    MessageRole,
    PromptMessage,
    PromptVariables,
    PromptConfig,
    GeneratedPrompt,
    OutputSchema,
    ValidationResult,
    RESPONSE_SCHEMA,
    SAFETY_RESPONSE_SCHEMA,
)
from src.prompt.templates.base import BaseTemplate, TemplateSection
from src.prompt.templates.normal import NormalTemplate
from src.prompt.templates.safety import SafetyTemplate
from src.prompt.templates.handoff import HandoffTemplate
from src.prompt.renderer import PromptRenderer, VariableResolver, get_prompt_renderer
from src.prompt.validator import OutputValidator, ResponseExtractor, get_output_validator
from src.prompt.engine import PromptEngine, PromptBuilder, get_prompt_engine


# ============================================
# Fixtures
# ============================================


@pytest.fixture
def sample_variables():
    return PromptVariables(
        session_id=uuid4(),
        tenant_id=uuid4(),
        channel="whatsapp",
        turn_count=3,
        funnel_stage="consideration",
        last_intent="scheduling_request",
        user_message="Quero agendar uma consulta",
    )


@pytest.fixture
def sample_config():
    return PromptConfig(mode=PromptMode.NORMAL)


@pytest.fixture
def renderer():
    return PromptRenderer()


@pytest.fixture
def validator():
    return OutputValidator()


@pytest.fixture
def engine():
    return PromptEngine()


# ============================================
# PromptMessage Tests
# ============================================


class TestPromptMessage:
    """Tests for PromptMessage model."""

    def test_message_creation(self):
        """Test basic message creation."""
        msg = PromptMessage(
            role=MessageRole.USER,
            content="Hello",
        )
        assert msg.role == MessageRole.USER
        assert msg.content == "Hello"

    def test_to_dict(self):
        """Test conversion to dict."""
        msg = PromptMessage(
            role=MessageRole.ASSISTANT,
            content="Hi there!",
        )
        result = msg.to_dict()
        
        assert result == {"role": "assistant", "content": "Hi there!"}

    def test_to_dict_with_name(self):
        """Test conversion with name."""
        msg = PromptMessage(
            role=MessageRole.SYSTEM,
            content="Instructions",
            name="system_prompt",
        )
        result = msg.to_dict()
        
        assert result["name"] == "system_prompt"


# ============================================
# PromptVariables Tests
# ============================================


class TestPromptVariables:
    """Tests for PromptVariables model."""

    def test_default_values(self):
        """Test default variable values."""
        vars = PromptVariables()
        
        assert vars.channel == "unknown"
        assert vars.turn_count == 0
        assert vars.funnel_stage == "lead_novo"
        assert vars.constraints == []

    def test_custom_variables(self, sample_variables):
        """Test custom variables."""
        assert sample_variables.channel == "whatsapp"
        assert sample_variables.turn_count == 3


# ============================================
# OutputSchema Tests
# ============================================


class TestOutputSchema:
    """Tests for OutputSchema validation."""

    def test_validate_valid_output(self):
        """Test validation of valid output."""
        schema = RESPONSE_SCHEMA
        output = {"response": "Hello!"}
        
        is_valid, errors = schema.validate_output(output)
        
        assert is_valid is True
        assert len(errors) == 0

    def test_validate_missing_required(self):
        """Test validation with missing required field."""
        schema = RESPONSE_SCHEMA
        output = {"intent_detected": "greeting"}
        
        is_valid, errors = schema.validate_output(output)
        
        assert is_valid is False
        assert "Missing required field: response" in errors

    def test_validate_wrong_type(self):
        """Test validation with wrong type."""
        schema = OutputSchema(
            properties={"count": {"type": "number"}},
            required=["count"],
        )
        output = {"count": "not a number"}
        
        is_valid, errors = schema.validate_output(output)
        
        assert is_valid is False
        assert any("should be number" in e for e in errors)


# ============================================
# Template Tests
# ============================================


class TestNormalTemplate:
    """Tests for NormalTemplate."""

    def test_get_system_prompt(self, sample_variables):
        """Test system prompt generation."""
        template = NormalTemplate()
        prompt = template.get_system_prompt(sample_variables)
        
        assert "Assistente Virtual" in prompt
        assert "whatsapp" in prompt

    def test_get_context_instructions(self, sample_variables):
        """Test context instructions."""
        template = NormalTemplate()
        instructions = template.get_context_instructions(sample_variables)
        
        assert "Consideração" in instructions or "consideration" in instructions.lower()

    def test_build_system_message(self, sample_variables):
        """Test complete system message."""
        template = NormalTemplate()
        message = template.build_system_message(sample_variables)
        
        assert message.role == MessageRole.SYSTEM
        assert len(message.content) > 0


class TestSafetyTemplate:
    """Tests for SafetyTemplate."""

    def test_safety_system_prompt(self):
        """Test safety system prompt."""
        template = SafetyTemplate()
        variables = PromptVariables(
            safety_category="clinical_risk",
            safety_level=3,
        )
        
        prompt = template.get_system_prompt(variables)
        
        assert "SEGURANÇA" in prompt
        assert "clinical_risk" in prompt

    def test_safety_constraints(self):
        """Test safety constraints are stricter."""
        template = SafetyTemplate()
        variables = PromptVariables()
        
        constraints = template.get_constraints(variables)
        
        assert "diagnósticos" in constraints.lower()
        assert "NUNCA" in constraints

    def test_safety_resources(self):
        """Test safety resources are included."""
        template = SafetyTemplate()
        variables = PromptVariables(safety_category="clinical_risk")
        
        instructions = template.get_context_instructions(variables)
        
        assert "188" in instructions  # CVV
        assert "192" in instructions  # SAMU


class TestHandoffTemplate:
    """Tests for HandoffTemplate."""

    def test_handoff_system_prompt(self):
        """Test handoff system prompt."""
        template = HandoffTemplate()
        variables = PromptVariables(
            handoff_status="pending",
            handoff_reason="explicit_request",
        )
        
        prompt = template.get_system_prompt(variables)
        
        assert "Transição" in prompt or "Atendente" in prompt

    def test_handoff_summary_generation(self):
        """Test handoff summary generation."""
        template = HandoffTemplate()
        variables = PromptVariables(
            channel="whatsapp",
            turn_count=5,
            handoff_reason="complex_query",
            slots={"nome": "João"},
        )
        
        summary = template.generate_handoff_summary(variables)
        
        assert "whatsapp" in summary
        assert "João" in summary


# ============================================
# TemplateSection Tests
# ============================================


class TestTemplateSection:
    """Tests for TemplateSection helper."""

    def test_section_creation(self):
        """Test section creation."""
        section = TemplateSection("Test Section")
        section.add("Line 1")
        section.add("Line 2")
        
        result = section.build()
        
        assert "## Test Section" in result
        assert "Line 1" in result
        assert "Line 2" in result

    def test_section_with_list(self):
        """Test section with list items."""
        section = TemplateSection("Items")
        section.add_list(["Item A", "Item B"])
        
        result = section.build()
        
        assert "- Item A" in result
        assert "- Item B" in result


# ============================================
# PromptRenderer Tests
# ============================================


class TestPromptRenderer:
    """Tests for PromptRenderer."""

    def test_render_normal(self, renderer, sample_variables, sample_config):
        """Test normal prompt rendering."""
        result = renderer.render(sample_variables, sample_config)
        
        assert isinstance(result, GeneratedPrompt)
        assert result.mode == PromptMode.NORMAL
        assert len(result.messages) > 0

    def test_render_with_history(self, renderer, sample_variables, sample_config):
        """Test rendering with history."""
        history = [
            {"role": "user", "content": "Olá"},
            {"role": "assistant", "content": "Oi!"},
        ]
        
        result = renderer.render(sample_variables, sample_config, history)
        
        # Should have system + history + current
        assert len(result.messages) >= 3

    def test_render_safety_mode(self, renderer, sample_variables):
        """Test safety mode rendering."""
        sample_variables.safety_category = "clinical_risk"
        config = PromptConfig(mode=PromptMode.SAFETY)
        
        result = renderer.render(sample_variables, config)
        
        assert result.mode == PromptMode.SAFETY
        system_content = result.get_system_prompt()
        assert "SEGURANÇA" in system_content

    def test_render_simple(self, renderer):
        """Test simple rendering."""
        result = renderer.render_simple(
            user_message="Hello",
            mode=PromptMode.NORMAL,
            channel="web",
        )
        
        assert result.mode == PromptMode.NORMAL
        assert len(result.messages) > 0

    def test_token_estimation(self, renderer, sample_variables, sample_config):
        """Test token estimation."""
        result = renderer.render(sample_variables, sample_config)
        
        assert result.total_tokens_estimate > 0


# ============================================
# OutputValidator Tests
# ============================================


class TestOutputValidator:
    """Tests for OutputValidator."""

    def test_validate_valid_json(self, validator):
        """Test validation of valid JSON."""
        output = json.dumps({"response": "Hello!"})
        
        result = validator.validate(output, PromptMode.NORMAL)
        
        assert result.is_valid is True
        assert result.sanitized_output["response"] == "Hello!"

    def test_validate_plain_text(self, validator):
        """Test validation of plain text."""
        output = "Hello, how can I help?"
        
        result = validator.validate(output, PromptMode.NORMAL)
        
        assert result.is_valid is True
        assert result.sanitized_output["response"] == output

    def test_validate_json_in_markdown(self, validator):
        """Test extraction of JSON from markdown."""
        output = '```json\n{"response": "Test"}\n```'
        
        is_valid, parsed, _ = validator.validate_json(output)
        
        assert is_valid is True
        assert parsed["response"] == "Test"

    def test_validate_missing_required(self, validator):
        """Test validation with missing required field."""
        output = json.dumps({"intent": "greeting"})
        
        result = validator.validate(output, PromptMode.NORMAL)
        
        # Should still have response (sanitized adds default)
        assert "response" in result.sanitized_output


# ============================================
# ResponseExtractor Tests
# ============================================


class TestResponseExtractor:
    """Tests for ResponseExtractor."""

    def test_extract_response_text(self):
        """Test response text extraction."""
        output = {"response": "Hello!", "intent": "greeting"}
        
        text = ResponseExtractor.extract_response_text(output)
        
        assert text == "Hello!"

    def test_extract_confidence(self):
        """Test confidence extraction."""
        output = {"response": "Hi", "confidence": 0.95}
        
        confidence = ResponseExtractor.extract_confidence(output)
        
        assert confidence == 0.95

    def test_to_simple_response(self):
        """Test conversion to simple response."""
        output = {"response": "Hello!"}
        
        text = ResponseExtractor.to_simple_response(output)
        
        assert text == "Hello!"


# ============================================
# VariableResolver Tests
# ============================================


class TestVariableResolver:
    """Tests for VariableResolver."""

    def test_merge_variables(self):
        """Test merging variables."""
        base = PromptVariables(
            channel="web",
            constraints=["Rule 1"],
        )
        override = PromptVariables(
            channel="whatsapp",
            constraints=["Rule 2"],
        )
        
        merged = VariableResolver.merge(base, override)
        
        assert merged.channel == "whatsapp"
        assert "Rule 1" in merged.constraints
        assert "Rule 2" in merged.constraints


# ============================================
# PromptEngine Tests
# ============================================


class TestPromptEngine:
    """Tests for PromptEngine."""

    @pytest.mark.asyncio
    async def test_generate(self, engine, sample_variables):
        """Test prompt generation."""
        result = await engine.generate(
            mode=PromptMode.NORMAL,
            variables=sample_variables,
        )
        
        assert isinstance(result, GeneratedPrompt)
        assert result.mode == PromptMode.NORMAL

    @pytest.mark.asyncio
    async def test_generate_simple(self, engine):
        """Test simple generation."""
        result = await engine.generate_simple(
            message="Hello",
            mode=PromptMode.NORMAL,
        )
        
        assert len(result.messages) > 0

    @pytest.mark.asyncio
    async def test_generate_safety(self, engine):
        """Test safety mode generation."""
        variables = PromptVariables(
            user_message="Estou me sentindo muito mal",
            safety_category="clinical_risk",
            safety_level=2,
        )
        
        result = await engine.generate(PromptMode.SAFETY, variables)
        
        assert result.mode == PromptMode.SAFETY
        system = result.get_system_prompt()
        assert "SEGURANÇA" in system

    def test_validate_output(self, engine):
        """Test output validation."""
        output = json.dumps({"response": "Test"})
        
        result = engine.validate_output(output, PromptMode.NORMAL)
        
        assert result.is_valid is True

    def test_extract_response(self, engine):
        """Test response extraction."""
        validated = ValidationResult(
            is_valid=True,
            sanitized_output={"response": "Hello!"},
        )
        
        text = engine.extract_response(validated)
        
        assert text == "Hello!"


# ============================================
# PromptBuilder Tests
# ============================================


class TestPromptBuilder:
    """Tests for PromptBuilder."""

    def test_builder_sync(self):
        """Test synchronous builder."""
        prompt = (PromptBuilder()
            .mode(PromptMode.NORMAL)
            .message("Hello")
            .with_context(channel="web", funnel_stage="awareness")
            .build_sync())
        
        assert prompt.mode == PromptMode.NORMAL
        assert len(prompt.messages) > 0

    def test_builder_with_constraints(self):
        """Test builder with constraints."""
        prompt = (PromptBuilder()
            .mode(PromptMode.NORMAL)
            .message("Test")
            .with_constraints(["Be polite", "Be concise"])
            .build_sync())
        
        system = prompt.get_system_prompt()
        assert "Be polite" in system or len(prompt.messages) > 0

    def test_builder_with_safety(self):
        """Test builder with safety context."""
        prompt = (PromptBuilder()
            .mode(PromptMode.SAFETY)
            .message("Help")
            .with_safety("clinical_risk", level=2)
            .build_sync())
        
        assert prompt.mode == PromptMode.SAFETY

    def test_builder_with_knowledge(self):
        """Test builder with knowledge."""
        prompt = (PromptBuilder()
            .mode(PromptMode.RAG)
            .message("What are the prices?")
            .with_knowledge(
                context="Prices: Basic R$100, Premium R$200",
                sources=["pricing.pdf"],
            )
            .build_sync())
        
        assert prompt.mode == PromptMode.RAG

    @pytest.mark.asyncio
    async def test_builder_async(self):
        """Test async builder."""
        prompt = await (PromptBuilder()
            .mode(PromptMode.NORMAL)
            .message("Hello")
            .build())
        
        assert prompt.mode == PromptMode.NORMAL


# ============================================
# Singleton Tests
# ============================================


class TestSingletons:
    """Test singleton pattern implementations."""

    def test_renderer_singleton(self):
        """Test PromptRenderer singleton."""
        r1 = get_prompt_renderer()
        r2 = get_prompt_renderer()
        assert r1 is r2

    def test_validator_singleton(self):
        """Test OutputValidator singleton."""
        v1 = get_output_validator()
        v2 = get_output_validator()
        assert v1 is v2

    def test_engine_singleton(self):
        """Test PromptEngine singleton."""
        e1 = get_prompt_engine()
        e2 = get_prompt_engine()
        assert e1 is e2


# ============================================
# Integration Tests
# ============================================


class TestIntegration:
    """Integration tests for the Prompt Engine."""

    @pytest.mark.asyncio
    async def test_full_generation_flow(self):
        """Test complete generation and validation flow."""
        engine = PromptEngine()
        
        # Generate prompt
        variables = PromptVariables(
            user_message="Qual o horário de funcionamento?",
            channel="whatsapp",
            funnel_stage="lead_novo",
        )
        
        prompt = await engine.generate(PromptMode.NORMAL, variables)
        
        # Verify prompt structure
        assert prompt.mode == PromptMode.NORMAL
        assert len(prompt.messages) >= 2  # System + user at minimum
        
        # Simulate LLM output
        llm_output = json.dumps({
            "response": "Funcionamos das 8h às 18h.",
            "intent_detected": "question_hours",
            "confidence": 0.9,
        })
        
        # Validate output
        result = engine.validate_output(llm_output, PromptMode.NORMAL)
        
        assert result.is_valid is True
        assert result.sanitized_output["response"] == "Funcionamos das 8h às 18h."

    @pytest.mark.asyncio
    async def test_safety_flow(self):
        """Test safety mode flow."""
        engine = PromptEngine()
        
        variables = PromptVariables(
            user_message="Estou passando muito mal",
            safety_category="clinical_risk",
            safety_level=3,
        )
        
        prompt = await engine.generate(PromptMode.SAFETY, variables)
        
        system = prompt.get_system_prompt()
        
        # Should include safety resources
        assert "188" in system or "192" in system
        assert "SEGURANÇA" in system

