"""
NeuroStack - Policy Engine - Policy Merger
Merges global and tenant-specific policies into a unified set.
"""

import logging
from typing import Optional
from uuid import UUID

from neurostack_core.policy.global_rules import (
    get_global_constraints,
    get_global_policy,
    get_global_rules,
)
from neurostack_core.policy.models import (
    PolicyConstraints,
    PolicyDefinition,
    PolicyRuleDefinition,
    RuleCategory,
)
from neurostack_core.policy.policy_store import PolicyStore, get_policy_store

logger = logging.getLogger(__name__)


class PolicyMerger:
    """
    Merges global and tenant-specific policies.

    Policy priority order:
    1. Global safety policies (highest priority, cannot be overridden)
    2. Tenant safety policies
    3. Tenant custom policies

    Constraints are accumulated from all active policies.
    """

    def __init__(self, policy_store: Optional[PolicyStore] = None):
        """
        Initialize the merger.

        Args:
            policy_store: Optional PolicyStore instance
        """
        self._store = policy_store or get_policy_store()

    async def get_merged_rules(
        self,
        tenant_id: UUID,
        include_global: bool = True,
    ) -> list[PolicyRuleDefinition]:
        """
        Get all applicable rules for a tenant.

        Args:
            tenant_id: The tenant ID
            include_global: Whether to include global rules

        Returns:
            List of merged PolicyRuleDefinition objects
        """
        all_rules: list[PolicyRuleDefinition] = []

        # Global rules first (highest priority)
        if include_global:
            global_rules = get_global_rules()
            all_rules.extend(global_rules)

        # Tenant-specific rules
        tenant_rules = await self._store.get_active_rules(tenant_id)
        all_rules.extend(tenant_rules)

        # Deduplicate by rule_id (keep first occurrence - higher priority)
        seen_ids = set()
        unique_rules = []
        for rule in all_rules:
            if rule.rule_id not in seen_ids:
                seen_ids.add(rule.rule_id)
                unique_rules.append(rule)

        logger.debug(f"Merged {len(unique_rules)} rules for tenant {tenant_id}")
        return unique_rules

    async def get_merged_policies(
        self,
        tenant_id: UUID,
        include_global: bool = True,
    ) -> list[PolicyDefinition]:
        """
        Get all applicable policies for a tenant.

        Args:
            tenant_id: The tenant ID
            include_global: Whether to include global policies

        Returns:
            List of PolicyDefinition objects sorted by priority
        """
        all_policies: list[PolicyDefinition] = []

        # Global policy first
        if include_global:
            global_policy = get_global_policy()
            all_policies.append(global_policy)

        # Tenant policies
        tenant_policies = await self._store.list_policies(tenant_id, active_only=True)
        all_policies.extend(tenant_policies)

        # Sort by priority (highest first)
        all_policies.sort(key=lambda p: p.priority, reverse=True)

        return all_policies

    async def get_merged_constraints(
        self,
        tenant_id: UUID,
        include_global: bool = True,
    ) -> PolicyConstraints:
        """
        Get merged constraints for prompt generation.

        Args:
            tenant_id: The tenant ID
            include_global: Whether to include global constraints

        Returns:
            Merged PolicyConstraints
        """
        # Start with global constraints
        if include_global:
            merged = PolicyConstraints(
                must_not_say=get_global_constraints().must_not_say.copy(),
                must_include=get_global_constraints().must_include.copy(),
                tone_guidelines=get_global_constraints().tone_guidelines.copy(),
                safety_reminders=get_global_constraints().safety_reminders.copy(),
                custom_instructions=get_global_constraints().custom_instructions.copy(),
            )
        else:
            merged = PolicyConstraints()

        # Add tenant-specific constraints from policies
        tenant_policies = await self._store.list_policies(tenant_id, active_only=True)

        for policy in tenant_policies:
            for rule in policy.rules:
                if not rule.enabled:
                    continue

                # Extract constraints based on rule category
                if rule.category in [
                    RuleCategory.SAFETY_CLINICAL,
                    RuleCategory.SAFETY_LEGAL,
                    RuleCategory.SAFETY_ETHICAL,
                ]:
                    if rule.message not in merged.safety_reminders:
                        merged.safety_reminders.append(rule.message)

                elif rule.category == RuleCategory.TONE:
                    if rule.message not in merged.tone_guidelines:
                        merged.tone_guidelines.append(rule.message)

                elif rule.category == RuleCategory.CONTENT:
                    if rule.message not in merged.must_not_say:
                        merged.must_not_say.append(rule.message)

                elif rule.category == RuleCategory.CUSTOM:
                    if rule.message not in merged.custom_instructions:
                        merged.custom_instructions.append(rule.message)

        return merged

    async def get_rules_by_category(
        self,
        tenant_id: UUID,
        category: RuleCategory,
    ) -> list[PolicyRuleDefinition]:
        """
        Get all rules for a specific category.

        Args:
            tenant_id: The tenant ID
            category: The rule category

        Returns:
            List of matching rules
        """
        all_rules = await self.get_merged_rules(tenant_id)
        return [r for r in all_rules if r.category == category]

    async def get_safety_rules(
        self,
        tenant_id: UUID,
    ) -> list[PolicyRuleDefinition]:
        """
        Get all safety-related rules.

        Args:
            tenant_id: The tenant ID

        Returns:
            List of safety rules
        """
        all_rules = await self.get_merged_rules(tenant_id)
        safety_categories = [
            RuleCategory.SAFETY_CLINICAL,
            RuleCategory.SAFETY_LEGAL,
            RuleCategory.SAFETY_ETHICAL,
        ]
        return [r for r in all_rules if r.category in safety_categories]

    def can_override_global_rule(self, rule_id: str) -> bool:
        """
        Check if a global rule can be overridden.

        Global safety rules cannot be overridden.
        """
        global_rules = get_global_rules()
        for rule in global_rules:
            if rule.rule_id == rule_id:
                # Safety and critical rules cannot be overridden
                if rule.category in [
                    RuleCategory.SAFETY_CLINICAL,
                    RuleCategory.SAFETY_LEGAL,
                ]:
                    return False
        return True


# ============================================
# Singleton Instance
# ============================================

_merger: Optional[PolicyMerger] = None


def get_policy_merger() -> PolicyMerger:
    """Get or create the global PolicyMerger instance."""
    global _merger
    if _merger is None:
        _merger = PolicyMerger()
    return _merger
