"""
Tests for clinic config field injection into prompt templates.
Verifies that config fields from pipeline custom_vars propagate
to rendered prompt output (Group 1 — clinic-config-fullstack).
"""

import pytest

from src.prompt.models import PromptMode, PromptVariables
from src.prompt.templates.normal import NormalTemplate


@pytest.fixture
def template():
    return NormalTemplate()


@pytest.fixture
def variables_with_config():
    """PromptVariables with full clinic config custom_vars."""
    return PromptVariables(
        user_message="Olá, gostaria de agendar",
        funnel_stage="lead_novo",
        last_intent="agendar",
        channel="whatsapp",
        turn_count=1,
        custom={
            "agent_name": "Daniela",
            "clinic_name": "Clínica Vida Nova",
            "agent_presentation": "Sou a Daniela, especialista em atendimento digital da Clínica Vida Nova.",
            "use_emojis": True,
            "clinic_history": "Fundada em 2015 com foco em dermatologia.",
            "parking_info": "Estacionamento gratuito no subsolo",
            "scheduling_hours": "Segunda a sexta, 8h às 18h",
            "service_format": "Presencial e teleconsulta",
            "accepts_insurance": True,
            "insurance_list": "Unimed, Amil, SulAmérica",
            "provide_prices": True,
            "language_policy": "Português brasileiro, informal e acolhedor",
            "professionals": "- Dr. João Silva, Dermatologista: 10 anos de experiência\n- Dra. Maria Santos, Ortodontista: especialista em invisalign",
            "clinic_specialties": ["dermatologia", "estética", "odontologia"],
            "working_hours": "Seg-Sex 8h-18h, Sáb 8h-12h",
            "full_address": "Rua das Flores, 123 - Centro",
            "payment_methods": "Cartão, PIX, boleto",
            "differentials": "Tecnologia de ponta e atendimento humanizado",
            "pricing_mode": "RANGE_ALLOWED",
        },
    )


class TestPromptConfigInjection:
    """Test that clinic config fields appear in rendered prompt."""

    def test_agent_presentation_in_system_prompt(self, template, variables_with_config):
        prompt = template.get_system_prompt(variables_with_config)
        assert "Sou a Daniela, especialista em atendimento digital" in prompt

    def test_agent_name_in_system_prompt(self, template, variables_with_config):
        prompt = template.get_system_prompt(variables_with_config)
        assert "Daniela" in prompt
        assert "Clínica Vida Nova" in prompt

    def test_professionals_in_context(self, template, variables_with_config):
        context = template.get_context_instructions(variables_with_config)
        assert "Dr. João Silva" in context
        assert "Dermatologista" in context
        assert "Dra. Maria Santos" in context
        assert "Equipe Profissional" in context

    def test_accepts_insurance_in_context(self, template, variables_with_config):
        context = template.get_context_instructions(variables_with_config)
        assert "Aceita convênio" in context
        assert "Unimed, Amil, SulAmérica" in context

    def test_parking_info_in_context(self, template, variables_with_config):
        context = template.get_context_instructions(variables_with_config)
        assert "Estacionamento gratuito no subsolo" in context

    def test_use_emojis_in_context(self, template, variables_with_config):
        context = template.get_context_instructions(variables_with_config)
        assert "emojis" in context.lower()

    def test_scheduling_hours_in_context(self, template, variables_with_config):
        context = template.get_context_instructions(variables_with_config)
        assert "Segunda a sexta, 8h às 18h" in context

    def test_service_format_in_context(self, template, variables_with_config):
        context = template.get_context_instructions(variables_with_config)
        assert "Presencial e teleconsulta" in context

    def test_language_policy_in_context(self, template, variables_with_config):
        context = template.get_context_instructions(variables_with_config)
        assert "Português brasileiro" in context

    def test_provide_prices_in_context(self, template, variables_with_config):
        context = template.get_context_instructions(variables_with_config)
        # Pricing policy block uses "preço" (singular); check policy marker is present.
        assert "política de preço" in context.lower()

    def test_no_config_returns_empty(self, template):
        """Template works fine with no custom vars."""
        variables = PromptVariables(
            user_message="Olá",
            funnel_stage="lead_novo",
            last_intent="saudacao",
            channel="whatsapp",
            turn_count=1,
        )
        prompt = template.get_system_prompt(variables)
        assert "assistente" in prompt

    def test_partial_config(self, template):
        """Template handles partial config without errors."""
        variables = PromptVariables(
            user_message="Olá",
            funnel_stage="lead_novo",
            last_intent="saudacao",
            channel="whatsapp",
            turn_count=1,
            custom={
                "agent_name": "Ana",
                "clinic_name": "Clínica Teste",
                "accepts_insurance": True,
            },
        )
        context = template.get_context_instructions(variables)
        assert "Aceita convênio" in context
