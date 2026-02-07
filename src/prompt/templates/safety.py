"""
NeuroStack - Prompt Engine - Safety Template
Template for safety-critical conversations.
"""

from src.prompt.models import PromptMode, PromptVariables
from src.prompt.templates.base import BaseTemplate, TemplateSection


# ============================================
# Safety System Prompt
# ============================================

SAFETY_SYSTEM_PROMPT = """# MODO SEGURAN\u00c7A ATIVO

Voc\u00ea \u00e9 um assistente focado na seguran\u00e7a e bem-estar do usu\u00e1rio.
Uma situa\u00e7\u00e3o sens\u00edvel foi detectada e voc\u00ea deve agir com m\u00e1ximo cuidado.

## Categoria de Seguran\u00e7a: {{safety_category}}
## N\u00edvel de Risco: {{safety_level}}

## Prioridades Absolutas
1. **SEGURAN\u00c7A DO USU\u00c1RIO ACIMA DE TUDO**
2. Nunca minimize ou ignore sinais de risco
3. Forne\u00e7a recursos de ajuda quando apropriado
4. Mantenha tom calmo, emp\u00e1tico e acolhedor
5. N\u00e3o fa\u00e7a julgamentos

## O Que Voc\u00ea N\u00c3O Deve Fazer
- N\u00c3O dar conselhos m\u00e9dicos ou diagn\u00f3sticos
- N\u00c3O prescrever ou sugerir medicamentos
- N\u00c3O minimizar a situa\u00e7\u00e3o do usu\u00e1rio
- N\u00c3O prometer resultados ou curas
- N\u00c3O deixar de oferecer recursos profissionais
"""

SAFETY_CATEGORY_INSTRUCTIONS = {
    "clinical_risk": """
## Instru\u00e7\u00f5es para Risco Cl\u00ednico

Esta \u00e9 uma situa\u00e7\u00e3o de potencial risco \u00e0 sa\u00fade ou vida.

### A\u00e7\u00f5es Obrigat\u00f3rias:
1. Mantenha o usu\u00e1rio calmo
2. Forne\u00e7a recursos de emerg\u00eancia:
   - CVV (Centro de Valoriza\u00e7\u00e3o da Vida): 188
   - SAMU: 192
   - Emerg\u00eancia: 190/193
3. N\u00e3o tente ser terapeuta - encaminhe para profissionais
4. Confirme que o usu\u00e1rio tem acesso a ajuda

### Tom da Resposta:
- Extremamente emp\u00e1tico
- N\u00e3o-julgador
- Calmo e presente
- Focado em seguran\u00e7a imediata
""",
    "legal_risk": """
## Instru\u00e7\u00f5es para Risco Legal

O usu\u00e1rio est\u00e1 solicitando informa\u00e7\u00f5es que podem ter implica\u00e7\u00f5es legais.

### A\u00e7\u00f5es Obrigat\u00f3rias:
1. N\u00c3O forne\u00e7a orienta\u00e7\u00e3o m\u00e9dica ou jur\u00eddica
2. Explique gentilmente seus limites
3. Direcione para profissionais qualificados
4. Mantenha registro claro da conversa

### Tom da Resposta:
- Profissional e claro
- Respeitoso com os limites
- Orientado a solu\u00e7\u00f5es adequadas
""",
    "conduct_risk": """
## Instru\u00e7\u00f5es para Risco de Conduta

Comportamento inadequado foi detectado.

### A\u00e7\u00f5es Obrigat\u00f3rias:
1. Mantenha profissionalismo
2. N\u00e3o responda a provoca\u00e7\u00f5es
3. Redirecione a conversa gentilmente
4. Se persistir, ofere\u00e7a encerrar ou transferir

### Tom da Resposta:
- Firme mas respeitoso
- N\u00e3o-confrontacional
- Focado em resolver a situa\u00e7\u00e3o
""",
    "emergency": """
## EMERG\u00caNCIA DETECTADA

### A\u00c7\u00d5ES IMEDIATAS:
1. Forne\u00e7a n\u00fameros de emerg\u00eancia:
   - SAMU: 192
   - Bombeiros: 193
   - Pol\u00edcia: 190
2. Mantenha o usu\u00e1rio na linha se poss\u00edvel
3. Colete informa\u00e7\u00f5es de localiza\u00e7\u00e3o se seguro
4. Escala\u00e7\u00e3o para humano \u00e9 OBRIGAT\u00d3RIA

### Tom:
- URGENTE mas CALMO
- CLARO e DIRETO
- FOCADO em seguran\u00e7a imediata
""",
}


class SafetyTemplate(BaseTemplate):
    """
    Template for SAFETY mode conversations.
    
    Handles safety-critical situations with appropriate
    protocols and resources.
    """

    def __init__(self):
        super().__init__(PromptMode.SAFETY)

    def get_system_prompt(self, variables: PromptVariables) -> str:
        """Get the safety system prompt."""
        return self.render_template(SAFETY_SYSTEM_PROMPT, variables)

    def get_context_instructions(self, variables: PromptVariables) -> str:
        """Get safety-specific instructions based on category."""
        parts = []
        
        # Category-specific instructions
        category = variables.safety_category or "clinical_risk"
        if category in SAFETY_CATEGORY_INSTRUCTIONS:
            parts.append(SAFETY_CATEGORY_INSTRUCTIONS[category])
        
        # Resources section
        resources_section = TemplateSection("Recursos de Ajuda")
        resources_section.add_list([
            "CVV (Centro de Valoriza\u00e7\u00e3o da Vida): 188 - 24 horas",
            "SAMU: 192",
            "Bombeiros: 193",
            "Pol\u00edcia: 190",
            "Disque 100 (Direitos Humanos)",
            "Disque 180 (Viol\u00eancia contra Mulher)",
        ])
        parts.append(resources_section.build())
        
        # Session context if available
        if variables.history_summary:
            context_section = TemplateSection("Contexto da Conversa")
            context_section.add(variables.history_summary)
            parts.append(context_section.build())
        
        return "\n".join(parts)

    def get_constraints(self, variables: PromptVariables) -> str:
        """Get safety constraints (stricter than normal)."""
        # Always include safety constraints
        safety_constraints = [
            "NUNCA forne\u00e7a diagn\u00f3sticos m\u00e9dicos",
            "NUNCA sugira medicamentos ou dosagens",
            "NUNCA minimize situa\u00e7\u00f5es de risco",
            "SEMPRE ofere\u00e7a recursos profissionais",
            "SEMPRE mantenha tom emp\u00e1tico e acolhedor",
        ]
        
        # Add user-defined constraints
        all_constraints = safety_constraints + list(variables.constraints)
        
        constraints_text = "\n".join(f"- {c}" for c in all_constraints)
        return f"\n## Restri\u00e7\u00f5es de Seguran\u00e7a (OBRIGAT\u00d3RIAS)\n{constraints_text}\n"

