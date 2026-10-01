"""Global policy rules backed by the selected content preset."""

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import cast
from uuid import uuid4

from neurostack_core.policy.models import (
    PolicyConstraints,
    PolicyDefinition,
    PolicyRuleDefinition,
    RuleCategory,
)
from neurostack_core.presets import get_preset

_POLICY_PRESET = cast(Mapping[str, object], get_preset().policy)
_RULE_PRESETS = cast(tuple[Mapping[str, object], ...], _POLICY_PRESET["rules"])
_RULE_PRESETS_BY_ID: Mapping[str, Mapping[str, object]] = MappingProxyType(
    {cast(str, rule["rule_id"]): rule for rule in _RULE_PRESETS}
)


def _fresh_rule(rule_id: str) -> PolicyRuleDefinition:
    return PolicyRuleDefinition.model_validate(dict(_RULE_PRESETS_BY_ID[rule_id]))


def _fresh_rules() -> list[PolicyRuleDefinition]:
    return [PolicyRuleDefinition.model_validate(dict(rule)) for rule in _RULE_PRESETS]


RULE_NO_DIAGNOSIS = _fresh_rule("C_MEDICAL_NO_DIAGNOSIS")
RULE_NO_MEDICATION = _fresh_rule("C_MEDICAL_NO_MED_CHANGE_POSTCARE")
RULE_NO_TREATMENT_GUARANTEE = _fresh_rule("C_COMPLIANCE_NO_PROMISE_OF_RESULTS")
RULE_NO_LEGAL_ADVICE = _fresh_rule("global_no_legal_advice")
RULE_NO_FALSE_PROMISES = _fresh_rule("global_no_false_promises")
RULE_PRIVACY_COMPLIANCE = _fresh_rule("global_privacy")
RULE_PROFESSIONAL_TONE = _fresh_rule("global_professional_tone")
RULE_EMERGENCY_REDIRECT = _fresh_rule("POL_SAFETY_001_EMERGENCY_POSTCARE_OUT_OF_HOURS")

_GLOBAL_POLICY_PRESET = cast(Mapping[str, object], _POLICY_PRESET["global_policy"])
_GLOBAL_POLICY_ID = uuid4()
_GLOBAL_POLICY_CREATED_AT = datetime.now()
_GLOBAL_POLICY_UPDATED_AT = datetime.now()


def _fresh_global_policy() -> PolicyDefinition:
    return PolicyDefinition.model_validate(
        {
            **_GLOBAL_POLICY_PRESET,
            "id": _GLOBAL_POLICY_ID,
            "created_at": _GLOBAL_POLICY_CREATED_AT,
            "updated_at": _GLOBAL_POLICY_UPDATED_AT,
            "rules": _fresh_rules(),
        }
    )


GLOBAL_SAFETY_POLICY = _fresh_global_policy()


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

_GLOBAL_CONSTRAINTS_PRESET = cast(Mapping[str, object], _POLICY_PRESET["constraints"])


def _fresh_global_constraints() -> PolicyConstraints:
    return PolicyConstraints.model_validate(_GLOBAL_CONSTRAINTS_PRESET)


GLOBAL_CONSTRAINTS = _fresh_global_constraints()


# ============================================
# Helper Functions
# ============================================


def get_global_policy() -> PolicyDefinition:
    """Get the global safety policy."""
    return _fresh_global_policy()


def get_global_rules() -> list[PolicyRuleDefinition]:
    """Get all global rules."""
    return _fresh_rules()


def get_global_constraints() -> PolicyConstraints:
    """Get global constraints for prompt generation."""
    return _fresh_global_constraints()


def get_clinical_safety_rules() -> list[PolicyRuleDefinition]:
    """Get only clinical safety rules."""
    return [
        rule
        for rule in get_global_rules()
        if rule.category == RuleCategory.SAFETY_CLINICAL
    ]


def get_legal_safety_rules() -> list[PolicyRuleDefinition]:
    """Get only legal safety rules."""
    return [
        rule
        for rule in get_global_rules()
        if rule.category == RuleCategory.SAFETY_LEGAL
    ]
