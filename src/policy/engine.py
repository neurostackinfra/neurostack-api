"""
NeuroStack - Policy & Guardrails Engine
Main engine for policy evaluation and enforcement.
"""

import logging
import time
from typing import Any, Optional
from uuid import UUID

from prometheus_client import Counter, Histogram

from src.policy.models import (
    PolicyContext,
    PolicyDefinition,
    PolicyRuleDefinition,
    PolicyEvaluationResult,
    PolicyConstraints,
    RuleViolation,
    RuleAction,
    RuleSeverity,
    RuleCategory,
)
from src.policy.global_rules import get_global_policy, get_global_constraints
from src.policy.policy_store import PolicyStore, get_policy_store
from src.policy.policy_merger import PolicyMerger, get_policy_merger

logger = logging.getLogger(__name__)


# ============================================
# Prometheus Metrics
# ============================================

POLICY_EVALUATIONS = Counter(
    "neurostack_policy_evaluations_total",
    "Total number of policy evaluations",
    ["tenant_id", "result"],
)

POLICY_VIOLATIONS = Counter(
    "neurostack_policy_violations_total",
    "Total number of policy violations",
    ["category", "severity", "action"],
)

POLICY_LATENCY = Histogram(
    "neurostack_policy_latency_seconds",
    "Policy evaluation latency in seconds",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1),
)


class PolicyEngine:
    """
    Main Policy & Guardrails Engine.
    
    Responsibilities:
    - Pre-SRM validation of incoming messages
    - Post-SRM validation before response
    - Constraint generation for prompts
    - Policy enforcement and logging
    
    The engine evaluates messages against:
    1. Global safety policies (cannot be overridden)
    2. Tenant-specific policies
    3. Session-specific flags
    """

    def __init__(
        self,
        policy_store: Optional[PolicyStore] = None,
        policy_merger: Optional[PolicyMerger] = None,
    ):
        """
        Initialize the Policy Engine.
        
        Args:
            policy_store: Optional PolicyStore instance
            policy_merger: Optional PolicyMerger instance
        """
        self._store = policy_store or get_policy_store()
        self._merger = policy_merger or get_policy_merger()

    async def evaluate_message(
        self,
        context: PolicyContext,
    ) -> PolicyEvaluationResult:
        """
        Evaluate a message against all applicable policies.
        
        This is the main pre-SRM validation method.
        
        Args:
            context: Policy evaluation context
            
        Returns:
            PolicyEvaluationResult with any violations
        """
        start_time = time.perf_counter()
        result = PolicyEvaluationResult()
        
        # Get all applicable rules
        rules = await self._merger.get_merged_rules(context.tenant_id)
        
        # Evaluate message against each rule
        for rule in rules:
            if not rule.enabled:
                continue
            
            if rule.matches(context.message):
                # Create violation
                violation = RuleViolation(
                    rule_id=rule.rule_id,
                    rule_name=rule.rule,
                    message=rule.message,
                    category=rule.category,
                    severity=rule.severity,
                    action=rule.action,
                    policy_id=get_global_policy().id,  # TODO: track actual policy
                    policy_name="Global/Tenant Policy",
                    matched_text=context.message[:100],
                )
                
                result.add_violation(violation)
                
                # Record metric
                POLICY_VIOLATIONS.labels(
                    category=rule.category.value,
                    severity=rule.severity.value,
                    action=rule.action.value,
                ).inc()
                
                logger.info(
                    f"Policy violation: {rule.rule_id} ({rule.action.value}) "
                    f"for tenant {context.tenant_id}"
                )
        
        # Calculate evaluation time
        result.evaluation_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        # Generate constraints from non-blocking rules
        constraints = await self.get_constraints(context.tenant_id)
        result.constraints = constraints.to_prompt_constraints()
        
        # Record metric
        POLICY_EVALUATIONS.labels(
            tenant_id=str(context.tenant_id)[:8],
            result="passed" if result.passed else "violated",
        ).inc()
        
        POLICY_LATENCY.observe(result.evaluation_time_ms / 1000)
        
        return result

    async def evaluate_pre_srm(
        self,
        tenant_id: UUID,
        session_id: UUID,
        message: str,
        channel: str = "unknown",
        metadata: Optional[dict] = None,
        clinic_config: Optional[dict] = None,
        user_state: Optional[dict] = None,
    ) -> PolicyEvaluationResult:
        """
        Quick pre-SRM evaluation of incoming message.
        
        This is called before SRM processing to catch obvious violations.
        
        Args:
            tenant_id: Tenant ID
            session_id: Session ID
            message: User message
            channel: Communication channel
            metadata: Optional metadata
            clinic_config: Optional ClinicConfig (doc MVP v0.5)
            user_state: Optional UserState (doc MVP v0.5)
            
        Returns:
            PolicyEvaluationResult
        """
        context = PolicyContext(
            tenant_id=tenant_id,
            session_id=session_id,
            message=message,
            channel=channel,
            metadata=metadata or {},
            clinic_config=clinic_config,
            user_state=user_state,
        )
        return await self.evaluate_message(context)

    async def validate_response(
        self,
        tenant_id: UUID,
        response: str,
    ) -> PolicyEvaluationResult:
        """
        Validate a generated response before sending.
        
        This ensures the LLM response doesn't violate policies.
        
        Args:
            tenant_id: Tenant ID
            response: Generated response text
            
        Returns:
            PolicyEvaluationResult
        """
        # Get all rules
        rules = await self._merger.get_merged_rules(tenant_id)
        result = PolicyEvaluationResult()
        
        # Only check content-related rules for responses
        content_categories = [
            RuleCategory.SAFETY_CLINICAL,
            RuleCategory.SAFETY_LEGAL,
            RuleCategory.CONTENT,
        ]
        
        for rule in rules:
            if not rule.enabled:
                continue
            if rule.category not in content_categories:
                continue
            
            if rule.matches(response):
                violation = RuleViolation(
                    rule_id=rule.rule_id,
                    rule_name=rule.rule,
                    message=f"Response contains prohibited content: {rule.message}",
                    category=rule.category,
                    severity=rule.severity,
                    action=RuleAction.BLOCK,  # Always block response violations
                    policy_id=get_global_policy().id,
                    policy_name="Response Validation",
                )
                result.add_violation(violation)
        
        return result

    async def get_constraints(
        self,
        tenant_id: UUID,
    ) -> PolicyConstraints:
        """
        Get all constraints for prompt generation.
        
        Args:
            tenant_id: Tenant ID
            
        Returns:
            Merged PolicyConstraints
        """
        return await self._merger.get_merged_constraints(tenant_id)

    async def get_safety_rules(
        self,
        tenant_id: UUID,
    ) -> list[PolicyRuleDefinition]:
        """
        Get all safety rules for a tenant.
        
        Args:
            tenant_id: Tenant ID
            
        Returns:
            List of safety rules
        """
        return await self._merger.get_safety_rules(tenant_id)

    async def should_block(
        self,
        result: PolicyEvaluationResult,
    ) -> bool:
        """
        Determine if the message should be blocked.
        
        Args:
            result: Policy evaluation result
            
        Returns:
            True if should block
        """
        return result.should_block

    async def should_handoff(
        self,
        result: PolicyEvaluationResult,
    ) -> bool:
        """
        Determine if handoff is required.
        
        Args:
            result: Policy evaluation result
            
        Returns:
            True if handoff required
        """
        return result.should_handoff

    async def get_violation_message(
        self,
        result: PolicyEvaluationResult,
    ) -> str:
        """
        Get a user-friendly message for violations.
        
        Args:
            result: Policy evaluation result
            
        Returns:
            User-facing message
        """
        if not result.violations:
            return ""
        
        # Get the highest severity violation
        severity_order = [
            RuleSeverity.INFO,
            RuleSeverity.WARNING,
            RuleSeverity.ERROR,
            RuleSeverity.CRITICAL,
        ]
        
        highest_violation = max(
            result.violations,
            key=lambda v: severity_order.index(v.severity),
        )
        
        return highest_violation.message

    async def get_policies_summary(
        self,
        tenant_id: UUID,
    ) -> dict[str, Any]:
        """
        Get a summary of all policies for a tenant.
        
        Args:
            tenant_id: Tenant ID
            
        Returns:
            Summary dictionary
        """
        policies = await self._merger.get_merged_policies(tenant_id)
        rules = await self._merger.get_merged_rules(tenant_id)
        
        return {
            "total_policies": len(policies),
            "total_rules": len(rules),
            "safety_rules": len([r for r in rules if r.category.value.startswith("safety")]),
            "active_policies": len([p for p in policies if p.is_active]),
            "policy_names": [p.name for p in policies],
        }


# ============================================
# Singleton Instance
# ============================================

_engine: Optional[PolicyEngine] = None


def get_policy_engine() -> PolicyEngine:
    """Get or create the global PolicyEngine instance."""
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine


async def evaluate_message(
    tenant_id: UUID,
    session_id: UUID,
    message: str,
    channel: str = "unknown",
) -> PolicyEvaluationResult:
    """
    Convenience function for policy evaluation.
    
    Args:
        tenant_id: Tenant ID
        session_id: Session ID
        message: Message to evaluate
        channel: Communication channel
        
    Returns:
        PolicyEvaluationResult
    """
    engine = get_policy_engine()
    return await engine.evaluate_pre_srm(
        tenant_id=tenant_id,
        session_id=session_id,
        message=message,
        channel=channel,
    )

