"""
NeuroStack - Prompt Engine - Normal Template
Template for standard conversation flow.
"""

from src.prompt.models import PromptMode, PromptVariables
from src.prompt.templates.base import BaseTemplate, TemplateSection


# ============================================
# System Prompt Template
# ============================================

NORMAL_SYSTEM_PROMPT = """# Assistente Virtual NeuroStack

Voc\u00ea \u00e9 um assistente virtual inteligente, projetado para oferecer atendimento de alta qualidade.

## Sua Personalidade
- Profissional e cordial
- Emp\u00e1tico e atencioso
- Claro e objetivo nas respostas
- Proativo em oferecer ajuda

## Diretrizes Gerais
1. Responda sempre em portugu\u00eas brasileiro
2. Mantenha um tom conversacional mas profissional
3. Seja conciso, mas completo nas informa\u00e7\u00f5es
4. Se n\u00e3o souber algo, admita e ofere\u00e7a alternativas
5. Nunca invente informa\u00e7\u00f5es

## Contexto para Condu\u00e7\u00e3o
O contexto que voc\u00ea recebe \u00e9 a **jornada do usu\u00e1rio no funil**, n\u00e3o o hist\u00f3rico de mensagens. Use a jornada para conduzir o usu\u00e1rio pelas etapas corretas do funil, na ordem adequada.

- Canal: {{channel}}
- Est\u00e1gio atual do funil: {{funnel_stage}}
- Turno: {{turn_count}}
"""

# Instruções por etapa do funil canônico (11 etapas MVP v0.5)
FUNNEL_INSTRUCTIONS = {
    "lead_novo": """
## Instruções para Lead Novo
- Primeiro contato; verificar se já é paciente (pergunta de direcionamento)
- Seja acolhedor; apresente nome e clínica; pergunte objetivo e depois nome
""",
    "em_qualificacao": """
## Instruções para Em Qualificação
- Fluxo de venda em andamento; aplicar sequência de perguntas para qualificação
- Faça a Pergunta-Chave sobre experiência prévia para classificar perfil (Novidade/Decepção/Continuidade)
""",
    "qualificado": """
## Instruções para Qualificado
- Lead pronto para decisão de agendamento; direcionar para agendamento ou esclarecer dúvidas
""",
    "followup": """
## Instruções para Follow-up
- Reengajamento após qualificado; retomar intenção, resolver objeções, conduzir para agendamento
""",
    "agendamento_solicitado": """
## Instruções para Agendamento Solicitado
- Usuário informou preferências (dia/turno); confirmar se finalizou agendamento
""",
    "agendado": """
## Instruções para Agendado
- Pessoa confirmou agendamento; dúvidas operacionais (confirmar, cancelar) ou perguntar se já foi atendido
""",
    "paciente_ativo": """
## Instruções para Paciente Ativo
- Usuário já é paciente; pode entrar em nova venda/qualificação ou dúvidas operacionais
""",
    "pos_atendimento": """
## Instruções para Pós-Atendimento
- Dúvidas ou suporte após atendimento; identificar riscos (safety_concern) e encaminhar se necessário
""",
    "fidelizacao": """
## Instruções para Fidelização
- Gatilho de reengajamento; handoff ou agente de fidelização
""",
    "inativo": """
## Instruções para Inativo
- Sem interação por x dias; handoff ou agente de inativo
""",
    "standby": """
## Instruções para Standby
- Automação desligada; atendimento 100% humano
""",
    # Fallbacks para compatibilidade
    "awareness": """
## Instruções para Fase de Conhecimento
- O usuário está conhecendo o serviço; seja acolhedor e informativo
""",
    "consideration": """
## Instruções para Fase de Consideração
- O usuário está avaliando; forneça informações detalhadas
""",
    "decision": """
## Instruções para Fase de Decisão
- O usuário está pronto para decidir; facilite o próximo passo
""",
    "retention": """
## Instruções para Fase de Retenção
- Usuário já é cliente; foco em satisfação e suporte
""",
}


class NormalTemplate(BaseTemplate):
    """
    Template for NORMAL mode conversations.
    
    Handles standard conversation flow with context-aware
    instructions based on funnel stage and intent.
    """

    def __init__(self):
        super().__init__(PromptMode.NORMAL)

    def get_system_prompt(self, variables: PromptVariables) -> str:
        """Get the base system prompt."""
        return self.render_template(NORMAL_SYSTEM_PROMPT, variables)

    def get_context_instructions(self, variables: PromptVariables) -> str:
        """Get context-specific instructions based on funnel stage and user journey."""
        parts = []

        # Jornada do usuário no funil (contexto principal para condução)
        if variables.user_journey:
            journey_section = TemplateSection("Jornada do Usu\u00e1rio no Funil")
            journey_section.add(
                "Use as informa\u00e7\u00f5es abaixo para conduzir o usu\u00e1rio pela pr\u00f3xima etapa correta. "
                "N\u00e3o repita etapas j\u00e1 percorridas; avance conforme o funil."
            )
            journey_section.add(variables.user_journey)
            parts.append(journey_section.build())
        
        # Funnel stage instructions
        funnel_stage = variables.funnel_stage.lower()
        if funnel_stage in FUNNEL_INSTRUCTIONS:
            parts.append(FUNNEL_INSTRUCTIONS[funnel_stage])
        
        # Intent-specific instructions
        if variables.last_intent:
            intent_instruction = self._get_intent_instruction(variables.last_intent)
            if intent_instruction:
                parts.append(intent_instruction)
        
        # Slots information
        if variables.slots:
            slots_section = TemplateSection("Informa\u00e7\u00f5es Coletadas")
            for key, value in variables.slots.items():
                slots_section.add(f"**{key}**: {value}")
            parts.append(slots_section.build())
        
        # Knowledge context (if available)
        if variables.knowledge_context:
            knowledge_section = TemplateSection("Contexto de Conhecimento")
            knowledge_section.add(variables.knowledge_context)
            if variables.knowledge_sources:
                knowledge_section.add("\nFontes:")
                knowledge_section.add_list(variables.knowledge_sources)
            parts.append(knowledge_section.build())
        
        return "\n".join(parts)

    def _get_intent_instruction(self, intent: str) -> str:
        """Get instruction for specific intent."""
        intent_instructions = {
            "scheduling_request": """
## Objetivo: Agendamento
- Colete data, hor\u00e1rio e tipo de servi\u00e7o
- Confirme disponibilidade
- Forne\u00e7a op\u00e7\u00f5es se necess\u00e1rio
""",
            "question_price": """
## Objetivo: Informa\u00e7\u00e3o de Pre\u00e7os
- Forne\u00e7a valores claros
- Mencione condi\u00e7\u00f5es de pagamento se relevante
- Ofere\u00e7a detalhes adicionais
""",
            "question_location": """
## Objetivo: Informa\u00e7\u00e3o de Localiza\u00e7\u00e3o
- Forne\u00e7a endere\u00e7o completo
- Mencione pontos de refer\u00eancia
- Ofere\u00e7a informa\u00e7\u00f5es de acesso
""",
            "cancellation_request": """
## Objetivo: Cancelamento
- Confirme o que ser\u00e1 cancelado
- Explique o processo
- Ofere\u00e7a alternativas se apropriado
""",
            "complaint_general": """
## Objetivo: Tratamento de Reclama\u00e7\u00e3o
- Demonstre empatia
- Entenda o problema completamente
- Ofere\u00e7a solu\u00e7\u00e3o ou encaminhamento
""",
        }
        
        return intent_instructions.get(intent, "")

