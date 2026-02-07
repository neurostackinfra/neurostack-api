"""
NeuroStack - Policy Engine - Global Rules
Hardcoded global rules that apply to all tenants.

These rules are critical safety and compliance guidelines that
cannot be overridden by tenant-specific policies.
"""

from src.policy.models import (
    PolicyDefinition,
    PolicyRuleDefinition,
    PolicyType,
    RuleCategory,
    RuleSeverity,
    RuleAction,
    PolicyConstraints,
)


# ============================================
# Clinical Safety Rules
# ============================================

RULE_NO_DIAGNOSIS = PolicyRuleDefinition(
    rule_id="C_MEDICAL_NO_DIAGNOSIS",
    rule="no_medical_diagnosis",
    message=(
        "Não posso fornecer diagnósticos médicos. "
        "Para uma avaliação adequada, por favor consulte um profissional de saúde."
    ),
    description="Proíbe o agente de fornecer diagnósticos médicos",
    category=RuleCategory.SAFETY_CLINICAL,
    severity=RuleSeverity.CRITICAL,
    action=RuleAction.BLOCK,
    patterns=[
        "qual meu diagnóstico",
        "qual é meu diagnóstico",
        "qual o meu diagnóstico",
        "me diga o que eu tenho",
        "o que eu tenho",
        "que doença eu tenho",
        "diagnóstico para mim",
        "diagnosticar",
    ],
    keywords=[
        "diagnóstico",
        "diagnosticar",
        "que doença",
        "o que eu tenho",
    ],
)

RULE_NO_MEDICATION = PolicyRuleDefinition(
    rule_id="C_MEDICAL_NO_MED_CHANGE_POSTCARE",
    rule="no_medication_prescription",
    message=(
        "Não posso recomendar ou prescrever medicamentos. "
        "Apenas um médico pode fazer prescrições após avaliação adequada."
    ),
    description="Proíbe o agente de recomendar ou prescrever medicamentos",
    category=RuleCategory.SAFETY_CLINICAL,
    severity=RuleSeverity.CRITICAL,
    action=RuleAction.BLOCK,
    patterns=[
        "que remédio tomar",
        "que remédio devo",
        "qual remédio devo",
        "qual medicamento tomar",
        "me receita",
        "prescreve para mim",
        "indica um remédio",
        "posso tomar",
        "devo tomar qual",
        "remédio devo tomar",
    ],
    keywords=[
        "prescrição",
        "prescrever",
        "receitar",
        "medicar",
    ],
)

RULE_NO_TREATMENT_GUARANTEE = PolicyRuleDefinition(
    rule_id="C_COMPLIANCE_NO_PROMISE_OF_RESULTS",
    rule="no_treatment_guarantee",
    message=(
        "Não posso garantir resultados de tratamentos. "
        "Os resultados podem variar conforme cada paciente."
    ),
    description="Proíbe promessas de resultados de tratamentos",
    category=RuleCategory.SAFETY_LEGAL,
    severity=RuleSeverity.ERROR,
    action=RuleAction.WARN,
    patterns=[
        "garante que vai curar",
        "certeza que vai funcionar",
        "100% de sucesso",
        "cura garantida",
        "resultado garantido",
        "vai resolver com certeza",
    ],
    keywords=[
        "garante cura",
        "garantia de resultado",
        "certeza de cura",
    ],
)


# ============================================
# Legal & Ethical Rules
# ============================================

RULE_NO_LEGAL_ADVICE = PolicyRuleDefinition(
    rule_id="global_no_legal_advice",
    rule="no_legal_advice",
    message=(
        "Não posso fornecer aconselhamento jurídico. "
        "Para questões legais, consulte um advogado."
    ),
    description="Proíbe o agente de fornecer aconselhamento jurídico",
    category=RuleCategory.SAFETY_LEGAL,
    severity=RuleSeverity.ERROR,
    action=RuleAction.WARN,
    patterns=[
        "posso processar",
        "entrar com processo",
        "devo processar",
        "tem direito a indenização",
        "vou processar vocês",
    ],
    keywords=[
        "processo judicial",
        "advogado",
        "indenização",
        "jurídico",
    ],
)

RULE_NO_FALSE_PROMISES = PolicyRuleDefinition(
    rule_id="global_no_false_promises",
    rule="no_false_promises",
    message=(
        "Não posso fazer promessas sobre resultados específicos. "
        "Cada caso é avaliado individualmente."
    ),
    description="Proíbe promessas falsas ou garantias irreais",
    category=RuleCategory.SAFETY_ETHICAL,
    severity=RuleSeverity.WARNING,
    action=RuleAction.WARN,
    patterns=[
        "prometo que",
        "garanto que",
        "com certeza vai",
        "100% garantido",
    ],
    keywords=[],
)

RULE_PRIVACY_COMPLIANCE = PolicyRuleDefinition(
    rule_id="global_privacy",
    rule="privacy_compliance",
    message=(
        "Suas informações são tratadas com confidencialidade "
        "de acordo com a LGPD e normas de sigilo médico."
    ),
    description="Reforça compliance com privacidade e LGPD",
    category=RuleCategory.COMPLIANCE,
    severity=RuleSeverity.INFO,
    action=RuleAction.LOG,
    patterns=[
        "dados pessoais",
        "privacidade",
        "sigilo",
        "lgpd",
    ],
    keywords=[
        "meus dados",
        "informações pessoais",
    ],
)


# ============================================
# Behavior Rules
# ============================================

RULE_PROFESSIONAL_TONE = PolicyRuleDefinition(
    rule_id="global_professional_tone",
    rule="professional_tone",
    message="Manter tom profissional e acolhedor em todas as interações",
    description="Define o tom de comunicação do agente",
    category=RuleCategory.TONE,
    severity=RuleSeverity.INFO,
    action=RuleAction.LOG,
    patterns=[],
    keywords=[],
)

RULE_EMERGENCY_REDIRECT = PolicyRuleDefinition(
    rule_id="POL_SAFETY_001_EMERGENCY_POSTCARE_OUT_OF_HOURS",
    rule="emergency_redirect",
    message=(
        "Em caso de emergência médica, ligue imediatamente para o SAMU (192) "
        "ou dirija-se ao pronto-socorro mais próximo."
    ),
    description="Redireciona emergências para serviços apropriados",
    category=RuleCategory.SAFETY_CLINICAL,
    severity=RuleSeverity.CRITICAL,
    action=RuleAction.HANDOFF,
    patterns=[
        "emergência",
        "urgência",
        "passando mal",
        "não consigo respirar",
        "dor muito forte",
        "sangramento intenso",
        "perdi a consciência",
    ],
    keywords=[
        "samu",
        "ambulância",
        "pronto-socorro",
        "emergência médica",
    ],
)


# ============================================
# Global Policy Definition
# ============================================

GLOBAL_SAFETY_POLICY = PolicyDefinition(
    name="NeuroStack Global Safety Policy",
    description="Política de segurança global aplicada a todos os tenants",
    policy_type=PolicyType.GLOBAL,
    priority=1000,  # Highest priority
    is_active=True,
    rules=[
        RULE_NO_DIAGNOSIS,
        RULE_NO_MEDICATION,
        RULE_NO_TREATMENT_GUARANTEE,
        RULE_NO_LEGAL_ADVICE,
        RULE_NO_FALSE_PROMISES,
        RULE_PRIVACY_COMPLIANCE,
        RULE_PROFESSIONAL_TONE,
        RULE_EMERGENCY_REDIRECT,
    ],
)


# Constraint IDs da documentação (sequence_gate / aplicados por contexto no SRM)
CONSTRAINT_SEQUENCE_AUTHORITY_BEFORE_PRICE = "C_SEQUENCE_AUTHORITY_BEFORE_PRICE"
CONSTRAINT_OBJECTION_NO_PASSIVE_CLOSE = "C_OBJECTION_NO_PASSIVE_CLOSE"

# Policy IDs (políticas de fluxo)
POLICY_ONB_001 = "POL_ONB_001_PRESENTATION_OBJECTIVE_NAME_SEQUENCE"
POLICY_QUAL_001 = "POL_QUAL_001_KEY_QUESTION_AND_PROFILE_CLASSIFICATION"
POLICY_OBJ_001 = "POL_OBJ_001_CLARIFY_OBJECTION_BEFORE_CLOSING"
POLICY_PRICE_002 = "POL_PRICE_002_RANGE_FIRST_EXACT_ON_INSISTENCE"
POLICY_HANDOFF_001 = "POL_HANDOFF_001_USER_REQUESTS_HUMAN_OR_AI_DETECTED"


# ============================================
# Global Constraints
# ============================================

GLOBAL_CONSTRAINTS = PolicyConstraints(
    must_not_say=[
        "fornecer diagnósticos médicos",
        "prescrever ou recomendar medicamentos",
        "garantir resultados de tratamentos",
        "dar aconselhamento jurídico",
        "fazer promessas sobre resultados específicos",
        "compartilhar informações de outros pacientes",
        "criticar outros profissionais de saúde",
    ],
    must_include=[],
    tone_guidelines=[
        "Manter tom profissional e acolhedor",
        "Ser empático mas objetivo",
        "Usar linguagem clara e acessível",
        "Evitar jargões médicos desnecessários",
    ],
    safety_reminders=[
        "Em caso de emergência, oriente a ligar para SAMU (192)",
        "Para diagnósticos, sempre recomendar consulta presencial",
        "Medicamentos só podem ser prescritos por médicos",
    ],
    custom_instructions=[
        "C_SEQUENCE_AUTHORITY_BEFORE_PRICE: Preço só após geração de autoridade (authority_done==true).",
        "C_OBJECTION_NO_PASSIVE_CLOSE: Não encerrar passivamente se houver objeção ativa.",
    ],
)


# ============================================
# Helper Functions
# ============================================

def get_global_policy() -> PolicyDefinition:
    """Get the global safety policy."""
    return GLOBAL_SAFETY_POLICY


def get_global_rules() -> list[PolicyRuleDefinition]:
    """Get all global rules."""
    return GLOBAL_SAFETY_POLICY.rules.copy()


def get_global_constraints() -> PolicyConstraints:
    """Get global constraints for prompt generation."""
    return GLOBAL_CONSTRAINTS


def get_clinical_safety_rules() -> list[PolicyRuleDefinition]:
    """Get only clinical safety rules."""
    return [
        r for r in GLOBAL_SAFETY_POLICY.rules
        if r.category == RuleCategory.SAFETY_CLINICAL
    ]


def get_legal_safety_rules() -> list[PolicyRuleDefinition]:
    """Get only legal safety rules."""
    return [
        r for r in GLOBAL_SAFETY_POLICY.rules
        if r.category == RuleCategory.SAFETY_LEGAL
    ]

