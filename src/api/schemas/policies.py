"""
NeuroStack API - Policies Schemas
Contracts for policy management endpoints (/v1/policies).
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PolicyType(str, Enum):
    """
    Types of policies.

    - global: Applies to all tenants (system-level)
    - tenant: Specific to a single tenant
    - safety: Safety-related rules (higher priority)
    """

    GLOBAL = "global"
    TENANT = "tenant"
    SAFETY = "safety"


class RuleSeverity(str, Enum):
    """
    Severity levels for policy rules.

    - info: Informational, no action required
    - warning: Should be flagged but not blocked
    - error: Should be blocked with explanation
    - critical: Must be blocked, may require handoff
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RuleAction(str, Enum):
    """
    Actions to take when a rule is triggered.

    - log: Log the occurrence only
    - warn: Include warning in response
    - flag: Flag for review
    - block: Block the action
    - handoff: Trigger human handoff
    """

    LOG = "log"
    WARN = "warn"
    FLAG = "flag"
    BLOCK = "block"
    HANDOFF = "handoff"


class PolicyRule(BaseModel):
    """
    A single policy rule.

    Defines a specific rule with its trigger conditions,
    severity, and action to take when triggered.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "rule_id": "no_medical_diagnosis",
                "rule": "no_medical_diagnosis",
                "message": "Não fornecer diagnósticos médicos",
                "description": "O assistente não deve diagnosticar condições médicas",
                "severity": "critical",
                "action": "block",
                "patterns": ["diagnóstico", "você tem", "isso parece ser"],
                "metadata": {"category": "clinical", "compliance": "CFM"},
            }
        }
    )

    rule_id: Optional[str] = Field(
        default=None,
        description="Unique identifier for the rule",
        json_schema_extra={"example": "no_medical_diagnosis"},
    )
    rule: str = Field(
        ...,
        description="Rule identifier/name",
        json_schema_extra={"example": "no_medical_diagnosis"},
    )
    message: str = Field(
        ...,
        description="Human-readable rule description",
        json_schema_extra={"example": "Não fornecer diagnósticos médicos"},
    )
    description: Optional[str] = Field(
        default=None,
        description="Detailed explanation of the rule",
    )
    severity: RuleSeverity = Field(
        default=RuleSeverity.WARNING,
        description="Severity level of the rule",
        json_schema_extra={"example": "critical"},
    )
    action: RuleAction = Field(
        default=RuleAction.WARN,
        description="Action to take when rule is triggered",
        json_schema_extra={"example": "block"},
    )
    patterns: list[str] = Field(
        default_factory=list,
        description="Patterns/keywords that trigger this rule",
        json_schema_extra={"example": ["diagnóstico", "você tem"]},
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional rule metadata",
    )


class PolicyResponse(BaseModel):
    """
    Policy response model.

    Complete policy information including all rules and metadata.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "d4e5f6a7-b8c9-0123-def0-123456789abc",
                "tenant_id": None,
                "name": "Global Safety Policy",
                "description": "Regras globais de segurança que se aplicam a todos os tenants",
                "policy_type": "global",
                "rules": [
                    {
                        "rule": "no_medical_diagnosis",
                        "message": "Não fornecer diagnósticos médicos",
                        "severity": "critical",
                        "action": "block",
                    }
                ],
                "priority": 1000,
                "is_active": True,
                "version": 1,
                "created_at": "2025-12-31T10:00:00Z",
                "updated_at": "2025-12-31T10:00:00Z",
            }
        }
    )

    id: UUID = Field(
        ...,
        description="Unique policy identifier",
        json_schema_extra={"example": "d4e5f6a7-b8c9-0123-def0-123456789abc"},
    )
    tenant_id: Optional[UUID] = Field(
        default=None,
        description="Tenant ID (null for global policies)",
    )
    name: str = Field(
        ...,
        description="Policy name",
        json_schema_extra={"example": "Global Safety Policy"},
    )
    description: Optional[str] = Field(
        default=None,
        description="Policy description",
    )
    policy_type: PolicyType = Field(
        ...,
        description="Type of policy",
        json_schema_extra={"example": "global"},
    )
    rules: list[PolicyRule] = Field(
        ...,
        description="List of rules in this policy",
    )
    priority: int = Field(
        default=0,
        description="Priority level (higher = applied first)",
        json_schema_extra={"example": 1000},
    )
    is_active: bool = Field(
        default=True,
        description="Whether the policy is active",
    )
    version: int = Field(
        default=1,
        ge=1,
        description="Policy version number",
    )
    created_at: str = Field(
        ...,
        description="ISO 8601 creation timestamp",
        json_schema_extra={"example": "2025-12-31T10:00:00Z"},
    )
    updated_at: str = Field(
        ...,
        description="ISO 8601 last update timestamp",
        json_schema_extra={"example": "2025-12-31T10:00:00Z"},
    )


class PolicyCreateRequest(BaseModel):
    """
    Request to create a new policy.

    Policies define the rules that govern agent behavior.
    Global policies (tenant_id=null) apply to all tenants.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "name": "Clinic XYZ Policy",
                "description": "Regras específicas para a Clínica XYZ",
                "policy_type": "tenant",
                "rules": [
                    {
                        "rule": "no_price_mention",
                        "message": "Não mencionar valores de consulta",
                        "severity": "warning",
                        "action": "warn",
                    },
                    {
                        "rule": "professional_tone",
                        "message": "Manter tom profissional e acolhedor",
                        "severity": "info",
                        "action": "log",
                    },
                ],
                "priority": 100,
            }
        }
    )

    tenant_id: Optional[UUID] = Field(
        default=None,
        description="Tenant ID (null for global policies)",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Policy name",
        json_schema_extra={"example": "Clinic XYZ Policy"},
    )
    description: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Policy description",
    )
    policy_type: PolicyType = Field(
        default=PolicyType.TENANT,
        description="Type of policy",
    )
    rules: list[PolicyRule] = Field(
        default_factory=list,
        description="List of rules to include",
    )
    priority: int = Field(
        default=0,
        ge=0,
        le=10000,
        description="Priority level (0-10000)",
    )


class PolicyUpdateRequest(BaseModel):
    """
    Request to update an existing policy.

    All fields are optional - only provided fields will be updated.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Updated Policy Name",
                "rules": [
                    {
                        "rule": "new_rule",
                        "message": "New rule description",
                        "severity": "warning",
                        "action": "warn",
                    }
                ],
                "is_active": True,
            }
        }
    )

    name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="New policy name",
    )
    description: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="New policy description",
    )
    rules: Optional[list[PolicyRule]] = Field(
        default=None,
        description="New list of rules (replaces existing)",
    )
    priority: Optional[int] = Field(
        default=None,
        ge=0,
        le=10000,
        description="New priority level",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description="Activate/deactivate policy",
    )


class PolicyListResponse(BaseModel):
    """
    Response for policy list.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "policies": [],
                "total": 5,
                "global_count": 2,
                "tenant_count": 3,
            }
        }
    )

    policies: list[PolicyResponse] = Field(
        ...,
        description="List of policies",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of policies",
    )
    global_count: int = Field(
        default=0,
        ge=0,
        description="Number of global policies",
    )
    tenant_count: int = Field(
        default=0,
        ge=0,
        description="Number of tenant-specific policies",
    )


class EffectivePolicyResponse(BaseModel):
    """
    Response for effective (merged) policies for a tenant.

    Shows the final merged policy rules after combining
    global and tenant-specific policies by priority.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "effective_rules": [
                    {
                        "rule": "no_medical_diagnosis",
                        "message": "Não fornecer diagnósticos",
                        "severity": "critical",
                        "action": "block",
                        "source": "global",
                    }
                ],
                "source_policies": [
                    {"id": "...", "name": "Global Safety Policy", "priority": 1000}
                ],
                "computed_at": "2025-12-31T10:30:00Z",
            }
        }
    )

    tenant_id: UUID = Field(
        ...,
        description="Tenant for which policies were computed",
    )
    effective_rules: list[dict[str, Any]] = Field(
        ...,
        description="Merged list of rules in priority order",
    )
    source_policies: list[dict[str, Any]] = Field(
        ...,
        description="List of policies that contributed to the merge",
    )
    computed_at: str = Field(
        ...,
        description="When the merge was computed",
    )

