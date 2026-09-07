"""
NeuroStack - Policy & Guardrails Engine
Enforces safety, compliance, and behavioral policies for AI agents.

The Policy Engine provides:
- Global safety rules (cannot be overridden)
- Tenant-specific custom policies
- Pre-SRM message validation
- Post-response validation
- Constraint generation for prompts

Usage:
    from src.policy import PolicyEngine, get_policy_engine, evaluate_message

    # Using the engine
    engine = get_policy_engine()
    result = await engine.evaluate_pre_srm(
        tenant_id=tenant_id,
        session_id=session_id,
        message="Qual remédio devo tomar?",
    )

    if result.should_block:
        # Handle blocked message
        pass

    # Using the convenience function
    result = await evaluate_message(tenant_id, session_id, message)
"""

from src.policy.engine import PolicyEngine, evaluate_message, get_policy_engine
from src.policy.global_rules import (
    GLOBAL_CONSTRAINTS,
    GLOBAL_SAFETY_POLICY,
    get_clinical_safety_rules,
    get_global_constraints,
    get_global_policy,
    get_global_rules,
    get_legal_safety_rules,
)
from src.policy.models import (  # Enums; Models; API Models
    PolicyConstraints,
    PolicyContext,
    PolicyCreate,
    PolicyDefinition,
    PolicyEvaluationResult,
    PolicyListResponse,
    PolicyResponse,
    PolicyRuleCreate,
    PolicyRuleDefinition,
    PolicyType,
    PolicyUpdate,
    RuleAction,
    RuleCategory,
    RuleSeverity,
    RuleViolation,
)
from src.policy.policy_merger import PolicyMerger, get_policy_merger
from src.policy.policy_store import PolicyStore, PolicyStoreError, get_policy_store

__all__ = [
    # Enums
    "PolicyType",
    "RuleSeverity",
    "RuleAction",
    "RuleCategory",
    # Models
    "PolicyRuleDefinition",
    "PolicyDefinition",
    "RuleViolation",
    "PolicyEvaluationResult",
    "PolicyContext",
    "PolicyConstraints",
    # API Models
    "PolicyRuleCreate",
    "PolicyCreate",
    "PolicyUpdate",
    "PolicyResponse",
    "PolicyListResponse",
    # Global Rules
    "get_global_policy",
    "get_global_rules",
    "get_global_constraints",
    "get_clinical_safety_rules",
    "get_legal_safety_rules",
    "GLOBAL_SAFETY_POLICY",
    "GLOBAL_CONSTRAINTS",
    # Store
    "PolicyStore",
    "PolicyStoreError",
    "get_policy_store",
    # Merger
    "PolicyMerger",
    "get_policy_merger",
    # Engine
    "PolicyEngine",
    "get_policy_engine",
    "evaluate_message",
]
