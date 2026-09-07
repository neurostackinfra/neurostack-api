"""
NeuroStack - Policy Engine - Policy Store
Manages storage and retrieval of tenant-specific policies.
"""

import logging
from typing import Optional
from uuid import UUID

from src.policy.models import (
    PolicyCreate,
    PolicyDefinition,
    PolicyRuleCreate,
    PolicyRuleDefinition,
    PolicyType,
    PolicyUpdate,
)

logger = logging.getLogger(__name__)


class PolicyStoreError(Exception):
    """Custom exception for PolicyStore operations."""

    pass


class PolicyStore:
    """
    In-memory store for policies.

    In production, this would be backed by PostgreSQL using the Policy model.
    For now, we use an in-memory dict for simplicity and testing.
    """

    def __init__(self) -> None:
        self._policies: dict[UUID, PolicyDefinition] = {}
        self._tenant_policies: dict[UUID, list[UUID]] = {}

    async def create_policy(
        self,
        tenant_id: UUID,
        policy_data: PolicyCreate,
    ) -> PolicyDefinition:
        """
        Create a new policy for a tenant.

        Args:
            tenant_id: The tenant ID
            policy_data: Policy creation data

        Returns:
            The created PolicyDefinition
        """
        # Convert rules
        rules = [
            PolicyRuleDefinition(
                rule=r.rule,
                message=r.message,
                description=r.description,
                category=r.category,
                severity=r.severity,
                action=r.action,
                patterns=r.patterns,
                keywords=r.keywords,
                enabled=r.enabled,
            )
            for r in policy_data.rules
        ]

        policy = PolicyDefinition(
            tenant_id=tenant_id,
            name=policy_data.name,
            description=policy_data.description,
            policy_type=policy_data.policy_type,
            rules=rules,
            priority=policy_data.priority,
            is_active=policy_data.is_active,
        )

        self._policies[policy.id] = policy

        if tenant_id not in self._tenant_policies:
            self._tenant_policies[tenant_id] = []
        self._tenant_policies[tenant_id].append(policy.id)

        logger.info(f"Created policy {policy.id} for tenant {tenant_id}")
        return policy

    async def get_policy(
        self,
        policy_id: UUID,
        tenant_id: Optional[UUID] = None,
    ) -> PolicyDefinition:
        """
        Get a policy by ID.

        Args:
            policy_id: The policy ID
            tenant_id: Optional tenant ID for validation

        Returns:
            The PolicyDefinition

        Raises:
            PolicyStoreError: If policy not found or tenant mismatch
        """
        policy = self._policies.get(policy_id)

        if not policy:
            raise PolicyStoreError(f"Policy {policy_id} not found")

        if tenant_id and policy.tenant_id != tenant_id:
            raise PolicyStoreError(
                f"Policy {policy_id} does not belong to tenant {tenant_id}"
            )

        return policy

    async def get_policy_or_none(
        self,
        policy_id: UUID,
        tenant_id: Optional[UUID] = None,
    ) -> Optional[PolicyDefinition]:
        """Get a policy by ID, returning None if not found."""
        try:
            return await self.get_policy(policy_id, tenant_id)
        except PolicyStoreError:
            return None

    async def update_policy(
        self,
        policy_id: UUID,
        tenant_id: UUID,
        updates: PolicyUpdate,
    ) -> PolicyDefinition:
        """
        Update a policy.

        Args:
            policy_id: The policy ID
            tenant_id: The tenant ID
            updates: Update data

        Returns:
            The updated PolicyDefinition
        """
        policy = await self.get_policy(policy_id, tenant_id)

        if updates.name is not None:
            policy.name = updates.name
        if updates.description is not None:
            policy.description = updates.description
        if updates.priority is not None:
            policy.priority = updates.priority
        if updates.is_active is not None:
            policy.is_active = updates.is_active
        if updates.rules is not None:
            policy.rules = [
                PolicyRuleDefinition(
                    rule=r.rule,
                    message=r.message,
                    description=r.description,
                    category=r.category,
                    severity=r.severity,
                    action=r.action,
                    patterns=r.patterns,
                    keywords=r.keywords,
                    enabled=r.enabled,
                )
                for r in updates.rules
            ]

        policy.version += 1
        from datetime import datetime

        policy.updated_at = datetime.now()

        self._policies[policy_id] = policy
        logger.info(f"Updated policy {policy_id}")
        return policy

    async def delete_policy(
        self,
        policy_id: UUID,
        tenant_id: UUID,
    ) -> bool:
        """
        Delete a policy.

        Args:
            policy_id: The policy ID
            tenant_id: The tenant ID

        Returns:
            True if deleted, False if not found
        """
        policy = await self.get_policy_or_none(policy_id, tenant_id)

        if not policy:
            return False

        del self._policies[policy_id]

        if tenant_id in self._tenant_policies:
            self._tenant_policies[tenant_id] = [
                pid for pid in self._tenant_policies[tenant_id] if pid != policy_id
            ]

        logger.info(f"Deleted policy {policy_id}")
        return True

    async def list_policies(
        self,
        tenant_id: UUID,
        active_only: bool = True,
        policy_type: Optional[PolicyType] = None,
    ) -> list[PolicyDefinition]:
        """
        List policies for a tenant.

        Args:
            tenant_id: The tenant ID
            active_only: Only return active policies
            policy_type: Filter by policy type

        Returns:
            List of PolicyDefinition objects
        """
        policy_ids = self._tenant_policies.get(tenant_id, [])
        policies = []

        for pid in policy_ids:
            policy = self._policies.get(pid)
            if not policy:
                continue

            if active_only and not policy.is_active:
                continue

            if policy_type and policy.policy_type != policy_type:
                continue

            policies.append(policy)

        # Sort by priority (highest first)
        policies.sort(key=lambda p: p.priority, reverse=True)
        return policies

    async def get_active_rules(
        self,
        tenant_id: UUID,
    ) -> list[PolicyRuleDefinition]:
        """
        Get all active rules for a tenant.

        Args:
            tenant_id: The tenant ID

        Returns:
            List of active PolicyRuleDefinition objects
        """
        policies = await self.list_policies(tenant_id, active_only=True)
        rules = []

        for policy in policies:
            rules.extend([r for r in policy.rules if r.enabled])

        return rules

    async def add_rule_to_policy(
        self,
        policy_id: UUID,
        tenant_id: UUID,
        rule: PolicyRuleCreate,
    ) -> PolicyDefinition:
        """Add a rule to an existing policy."""
        policy = await self.get_policy(policy_id, tenant_id)

        new_rule = PolicyRuleDefinition(
            rule=rule.rule,
            message=rule.message,
            description=rule.description,
            category=rule.category,
            severity=rule.severity,
            action=rule.action,
            patterns=rule.patterns,
            keywords=rule.keywords,
            enabled=rule.enabled,
        )

        policy.rules.append(new_rule)
        policy.version += 1

        self._policies[policy_id] = policy
        return policy

    async def remove_rule_from_policy(
        self,
        policy_id: UUID,
        tenant_id: UUID,
        rule_id: str,
    ) -> PolicyDefinition:
        """Remove a rule from a policy."""
        policy = await self.get_policy(policy_id, tenant_id)

        original_count = len(policy.rules)
        policy.rules = [r for r in policy.rules if r.rule_id != rule_id]

        if len(policy.rules) == original_count:
            raise PolicyStoreError(f"Rule {rule_id} not found in policy {policy_id}")

        policy.version += 1
        self._policies[policy_id] = policy
        return policy

    def clear(self) -> None:
        """Clear all policies (for testing)."""
        self._policies.clear()
        self._tenant_policies.clear()

    @property
    def policy_count(self) -> int:
        """Get total number of policies."""
        return len(self._policies)


# ============================================
# Singleton Instance
# ============================================

_store: Optional[PolicyStore] = None


def get_policy_store() -> PolicyStore:
    """Get or create the global PolicyStore instance."""
    global _store
    if _store is None:
        _store = PolicyStore()
    return _store
