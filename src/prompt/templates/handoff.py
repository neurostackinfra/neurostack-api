"""
NeuroStack - Prompt Engine - Handoff Template
Template for human handoff transitions.
"""

from src.prompt.models import PromptMode, PromptVariables
from src.prompt.templates.base import BaseTemplate, TemplateSection


# ============================================
# Handoff System Prompt
# ============================================

HANDOFF_SYSTEM_PROMPT = """# Modo de Transi\u00e7\u00e3o para Atendente

Voc\u00ea \u00e9 {{agent_name}}, assistente virtual da {{clinic_name}}.
Voc\u00ea est\u00e1 auxiliando na transi\u00e7\u00e3o do usu\u00e1rio para um atendente humano.

## Identidade
- Nome: {{agent_name}}
- Cl\u00ednica: {{clinic_name}}
- Canal atual: {{channel}}

## Status do Handoff: {{handoff_status}}
## Motivo: {{handoff_reason}}

## Seu Papel Neste Momento
1. Manter o usu\u00e1rio informado sobre o status
2. Coletar informa\u00e7\u00f5es relevantes para o atendente
3. Preparar um resumo da conversa
4. Manter o usu\u00e1rio confort\u00e1vel durante a espera

## Diretrizes
- Seja transparente sobre tempos de espera
- N\u00e3o tente resolver quest\u00f5es complexas sozinho
- Colete informa\u00e7\u00f5es que ajudar\u00e3o o atendente
- Mantenha tom emp\u00e1tico e profissional
"""

HANDOFF_REASON_INSTRUCTIONS = {
    "explicit_request": """
## Usu\u00e1rio Solicitou Atendente

O usu\u00e1rio expressamente pediu para falar com um humano.

### A\u00e7\u00f5es:
1. Confirme que entendeu a solicita\u00e7\u00e3o
2. Informe que est\u00e1 transferindo
3. Pergunte se h\u00e1 algo espec\u00edfico que o atendente deve saber
4. Forne\u00e7a estimativa de tempo se dispon\u00edvel
""",
    "safety_concern": """
## Transfer\u00eancia por Seguran\u00e7a

Uma situa\u00e7\u00e3o de seguran\u00e7a requer aten\u00e7\u00e3o humana.

### PROIBI\u00c7\u00d5ES ABSOLUTAS (NUNCA viole):
- **NUNCA** forne\u00e7a orienta\u00e7\u00e3o m\u00e9dica, dosagens, posologias ou nomes de medicamentos
- **NUNCA** sugira se o usu\u00e1rio pode ou n\u00e3o tomar qualquer medica\u00e7\u00e3o
- **NUNCA** liste contraindica\u00e7\u00f5es, efeitos colaterais ou intera\u00e7\u00f5es medicamentosas
- **NUNCA** atue como m\u00e9dico, farmac\u00eautico ou profissional de sa\u00fade
- Qualquer pergunta sobre medica\u00e7\u00e3o, sintomas ou tratamento deve ser respondida APENAS com: encaminhamento para o profissional de sa\u00fade respons\u00e1vel

### A\u00e7\u00f5es:
1. Mantenha o usu\u00e1rio calmo
2. Informe que um especialista ir\u00e1 ajudar
3. N\u00c3O minimize a situa\u00e7\u00e3o
4. Garanta que recursos de emerg\u00eancia foram fornecidos
5. Redirecione TODA pergunta cl\u00ednica para o profissional de sa\u00fade
""",
    "max_turns_exceeded": """
## Transfer\u00eancia por Complexidade

A conversa excedeu os limites de resolu\u00e7\u00e3o autom\u00e1tica.

### A\u00e7\u00f5es:
1. Reconhe\u00e7a que a situa\u00e7\u00e3o precisa de aten\u00e7\u00e3o especial
2. Agrade\u00e7a a paci\u00eancia do usu\u00e1rio
3. Prepare resumo detalhado para o atendente
4. Informe que um especialista resolver\u00e1
""",
    "low_confidence": """
## Transfer\u00eancia por Incerteza

N\u00e3o foi poss\u00edvel entender claramente a necessidade do usu\u00e1rio.

### A\u00e7\u00f5es:
1. Pe\u00e7a desculpas pela dificuldade
2. Explique que um atendente ajudar\u00e1 melhor
3. Tente coletar mais contexto para o atendente
4. Mantenha tom positivo
""",
    "complex_query": """
## Transfer\u00eancia por Consulta Complexa

A solicita\u00e7\u00e3o requer conhecimento especializado.

### A\u00e7\u00f5es:
1. Reconhe\u00e7a a complexidade da quest\u00e3o
2. Informe que especialista ir\u00e1 auxiliar
3. Colete detalhes relevantes
4. Prepare contexto completo
""",
    "escalation": """
## Escala\u00e7\u00e3o de Reclama\u00e7\u00e3o

O usu\u00e1rio expressou insatisfa\u00e7\u00e3o ou pediu supervisor.

### A\u00e7\u00f5es:
1. Demonstre que a preocupa\u00e7\u00e3o \u00e9 levada a s\u00e9rio
2. N\u00c3O seja defensivo
3. Confirme os pontos de insatisfa\u00e7\u00e3o
4. Assegure que ser\u00e1 resolvido
""",
}


class HandoffTemplate(BaseTemplate):
    """
    Template for HANDOFF mode conversations.
    
    Handles the transition to human agents with
    appropriate context preservation.
    """

    def __init__(self):
        super().__init__(PromptMode.HANDOFF)

    def get_system_prompt(self, variables: PromptVariables) -> str:
        """Get the handoff system prompt."""
        return self.render_template(HANDOFF_SYSTEM_PROMPT, variables)

    def get_context_instructions(self, variables: PromptVariables) -> str:
        """Get handoff-specific instructions based on reason."""
        parts = []
        
        # Reason-specific instructions
        reason = variables.handoff_reason or "explicit_request"
        if reason in HANDOFF_REASON_INSTRUCTIONS:
            parts.append(HANDOFF_REASON_INSTRUCTIONS[reason])
        
        # Information collection section
        collection_section = TemplateSection("Informa\u00e7\u00f5es a Coletar")
        collection_section.add_list([
            "Nome do usu\u00e1rio (se n\u00e3o tiver)",
            "Resumo do problema/solicita\u00e7\u00e3o",
            "Tentativas anteriores de resolu\u00e7\u00e3o",
            "Urg\u00eancia da quest\u00e3o",
            "Prefer\u00eancia de contato (se aplic\u00e1vel)",
        ])
        parts.append(collection_section.build())
        
        # Slots j\u00e1 coletados
        if variables.slots:
            collected_section = TemplateSection("Informa\u00e7\u00f5es J\u00e1 Coletadas")
            for key, value in variables.slots.items():
                collected_section.add(f"**{key}**: {value}")
            parts.append(collected_section.build())
        
        # History summary
        if variables.history_summary:
            history_section = TemplateSection("Resumo da Conversa")
            history_section.add(variables.history_summary)
            parts.append(history_section.build())
        
        # Status instructions
        status = variables.handoff_status
        if status == "waiting":
            parts.append("""
## Usu\u00e1rio em Espera
- Mantenha-o engajado
- Forne\u00e7a atualiza\u00e7\u00f5es de tempo
- Ofere\u00e7a para coletar mais informa\u00e7\u00f5es
""")
        elif status == "connected":
            parts.append("""
## Atendente Conectado
- Finalize seu papel
- Fa\u00e7a uma passagem de contexto clara
- Agrade\u00e7a ao usu\u00e1rio
""")
        
        return "\n".join(parts)

    def generate_handoff_summary(self, variables: PromptVariables) -> str:
        """
        Generate a summary for the human agent.
        
        Args:
            variables: Prompt variables with context
            
        Returns:
            Formatted summary for agent
        """
        nao_especificado = 'N\u00e3o especificado'
        summary_parts = [
            "## Resumo para Atendente",
            "",
            f"**Canal:** {variables.channel}",
            f"**Turnos:** {variables.turn_count}",
            f"**Motivo da Transfer\u00eancia:** {variables.handoff_reason or nao_especificado}",
            "",
        ]
        
        if variables.funnel_stage:
            summary_parts.append(f"**Est\u00e1gio:** {variables.funnel_stage}")
        
        if variables.last_intent:
            summary_parts.append(f"**\u00daltima Inten\u00e7\u00e3o:** {variables.last_intent}")
        
        if variables.slots:
            summary_parts.append("")
            summary_parts.append("### Informa\u00e7\u00f5es Coletadas:")
            for key, value in variables.slots.items():
                summary_parts.append(f"- {key}: {value}")
        
        if variables.history_summary:
            summary_parts.append("")
            summary_parts.append("### Contexto da Conversa:")
            summary_parts.append(variables.history_summary)
        
        if variables.safety_category:
            summary_parts.append("")
            summary_parts.append(f"\u26a0\ufe0f **Alerta de Seguran\u00e7a:** {variables.safety_category}")
        
        return "\n".join(summary_parts)

