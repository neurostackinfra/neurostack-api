"""
NeuroStack API - Policies Endpoint
Policy management (global and per-tenant).

Policies define the rules that govern agent behavior, including:
- Safety rules (no medical diagnosis, no medication advice)
- Tone and communication guidelines
- Scope limitations (what topics to avoid)
- Compliance requirements
"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from neurostack_core.policy import PolicyCreate, PolicyDefinition, PolicyRuleCreate
from neurostack_core.policy import PolicyType as DomainPolicyType
from neurostack_core.policy import PolicyUpdate
from neurostack_core.policy import RuleAction as DomainRuleAction
from neurostack_core.policy import RuleCategory
from neurostack_core.policy import RuleSeverity as DomainRuleSeverity
from neurostack_core.policy import (
    get_global_policy,
    get_policy_engine,
    get_policy_merger,
    get_policy_store,
)
from src.api.middleware.auth import TenantContext, get_tenant_context
from src.api.schemas.base import ErrorResponse
from src.api.schemas.policies import (
    EffectivePolicyResponse,
    PolicyCreateRequest,
    PolicyListResponse,
    PolicyResponse,
    PolicyRule,
    PolicyType,
    PolicyUpdateRequest,
    RuleAction,
    RuleSeverity,
)
from src.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


# ============================================
# Helper Functions
# ============================================


def convert_to_response(policy: PolicyDefinition) -> PolicyResponse:
    """Convert PolicyDefinition to PolicyResponse."""
    rules = [
        PolicyRule(
            rule_id=r.rule_id,
            rule=r.rule,
            message=r.message,
            description=r.description,
            severity=RuleSeverity(r.severity.value),
            action=RuleAction(r.action.value),
            patterns=r.patterns,
            metadata=r.metadata,
        )
        for r in policy.rules
    ]

    return PolicyResponse(
        id=policy.id,
        tenant_id=policy.tenant_id,
        name=policy.name,
        description=policy.description,
        policy_type=PolicyType(policy.policy_type.value),
        rules=rules,
        priority=policy.priority,
        is_active=policy.is_active,
        version=policy.version,
        created_at=(
            policy.created_at.isoformat()
            if isinstance(policy.created_at, datetime)
            else str(policy.created_at)
        ),
        updated_at=(
            policy.updated_at.isoformat()
            if isinstance(policy.updated_at, datetime)
            else str(policy.updated_at)
        ),
    )


def get_global_policy_response() -> PolicyResponse:
    """Get the global policy as a response model."""
    global_policy = get_global_policy()
    return convert_to_response(global_policy)


# ============================================
# Endpoints
# ============================================


@router.post(
    "",
    response_model=PolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Policy",
    description="""
Create a new policy (global or tenant-specific).

## Policy Types

- **global**: Applies to all tenants (requires admin privileges)
- **tenant**: Applies only to the specified tenant
- **safety**: Safety-related rules (higher priority)

## Policy Merging

When processing events, policies are merged in priority order:
1. Global safety policies (highest priority)
2. Global standard policies
3. Tenant safety policies
4. Tenant standard policies

## Example Use Cases

- Define clinic-specific tone guidelines
- Set up scope limitations (e.g., don't discuss pricing)
- Configure compliance rules for specific regions
    """,
    responses={
        201: {
            "description": "Policy created successfully",
            "model": PolicyResponse,
        },
        400: {
            "description": "Invalid policy configuration",
            "model": ErrorResponse,
        },
        403: {
            "description": "Not authorized to create global policies",
            "model": ErrorResponse,
        },
    },
    tags=["Policies"],
)
async def create_policy(
    request: PolicyCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> PolicyResponse:
    """
    Create a new policy.

    - Global policies (tenant_id=null) apply to all tenants
    - Tenant policies apply only to specific tenant
    - Policies are merged and applied in priority order
    """
    # Determine effective tenant_id: prefer request payload, fallback to authenticated tenant
    effective_tenant_id = request.tenant_id or tenant.tenant_id

    # Global policies cannot be created via API
    if effective_tenant_id is None or request.policy_type == PolicyType.GLOBAL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Global policies can only be managed by system administrators",
        )

    store = get_policy_store()

    # Convert rules
    rules = [
        PolicyRuleCreate(
            rule=r.rule,
            message=r.message,
            description=r.description,
            category=RuleCategory.CUSTOM,
            severity=DomainRuleSeverity(r.severity.value),
            action=DomainRuleAction(r.action.value),
            patterns=r.patterns,
            keywords=[],
            enabled=True,
        )
        for r in request.rules
    ]

    policy_create = PolicyCreate(
        name=request.name,
        description=request.description,
        policy_type=DomainPolicyType(request.policy_type.value),
        rules=rules,
        priority=request.priority,
        is_active=True,
    )

    policy = await store.create_policy(effective_tenant_id, policy_create)

    return convert_to_response(policy)


@router.get(
    "/{policy_id}",
    response_model=PolicyResponse,
    summary="Get Policy",
    description="""
Retrieve a policy by ID.

Returns the complete policy configuration including all rules.
    """,
    responses={
        200: {
            "description": "Policy retrieved successfully",
            "model": PolicyResponse,
        },
        404: {
            "description": "Policy not found",
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": {
                        "error": "policy_not_found",
                        "message": "Policy not found",
                    }
                }
            },
        },
    },
    tags=["Policies"],
)
async def get_policy(policy_id: UUID) -> PolicyResponse:
    """
    Retrieve a policy by ID.
    """
    # Check if it's the global policy
    global_policy = get_global_policy()
    if policy_id == global_policy.id:
        return get_global_policy_response()

    # Try to get from store
    store = get_policy_store()
    policy = await store.get_policy_or_none(policy_id)

    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy {policy_id} not found",
        )

    return convert_to_response(policy)


@router.get(
    "",
    response_model=PolicyListResponse,
    summary="List Policies",
    description="""
List policies with optional tenant filter.

## Filtering

- **tenant_id**: Filter by specific tenant
- **include_global**: Include global policies (default: true)
- **active_only**: Only return active policies (default: true)
- **policy_type**: Filter by policy type

Results are ordered by priority (highest first).
    """,
    responses={
        200: {
            "description": "Policies listed successfully",
            "model": PolicyListResponse,
        },
    },
    tags=["Policies"],
)
async def list_policies(
    tenant_id: Optional[UUID] = Query(default=None, description="Filter by tenant ID"),
    include_global: bool = Query(default=True, description="Include global policies"),
    active_only: bool = Query(default=True, description="Only return active policies"),
    policy_type: Optional[PolicyType] = Query(
        default=None, description="Filter by policy type"
    ),
) -> PolicyListResponse:
    """
    List policies.

    - If tenant_id is provided, returns tenant + global policies
    - If include_global is True (default), includes global policies
    - If active_only is True (default), only returns active policies
    """
    policies = []

    # Add global policy if requested
    if include_global:
        policies.append(get_global_policy_response())

    # Add tenant policies if tenant_id provided
    if tenant_id:
        store = get_policy_store()
        tenant_policies = await store.list_policies(tenant_id, active_only=active_only)
        policies.extend([convert_to_response(p) for p in tenant_policies])

    # Filter by policy type if specified
    if policy_type:
        policies = [p for p in policies if p.policy_type == policy_type]

    return PolicyListResponse(
        policies=policies,
        total=len(policies),
        global_count=1 if include_global else 0,
        tenant_count=len(policies) - (1 if include_global else 0),
    )


@router.patch(
    "/{policy_id}",
    response_model=PolicyResponse,
    summary="Update Policy",
    description="""
Update an existing policy.

All fields are optional - only provided fields will be updated.

Note: Updating a policy creates a new version. The version number
is automatically incremented.
    """,
    responses={
        200: {
            "description": "Policy updated successfully",
            "model": PolicyResponse,
        },
        404: {
            "description": "Policy not found",
            "model": ErrorResponse,
        },
        403: {
            "description": "Cannot modify global policies",
            "model": ErrorResponse,
        },
    },
    tags=["Policies"],
)
async def update_policy(
    policy_id: UUID,
    request: PolicyUpdateRequest,
    tenant_id: UUID = Query(..., description="Tenant ID for authorization"),
) -> PolicyResponse:
    """
    Update an existing policy.
    """
    # Check if it's a global policy
    global_policy = get_global_policy()
    if policy_id == global_policy.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Global safety policies cannot be modified",
        )

    store = get_policy_store()

    # Convert rules if provided
    rules = None
    if request.rules is not None:
        rules = [
            PolicyRuleCreate(
                rule=r.rule,
                message=r.message,
                description=r.description,
                category=RuleCategory.CUSTOM,
                severity=DomainRuleSeverity(r.severity.value),
                action=DomainRuleAction(r.action.value),
                patterns=r.patterns,
                keywords=[],
                enabled=True,
            )
            for r in request.rules
        ]

    policy_update = PolicyUpdate(
        name=request.name,
        description=request.description,
        rules=rules,
        priority=request.priority,
        is_active=request.is_active,
    )

    try:
        policy = await store.update_policy(policy_id, tenant_id, policy_update)
        return convert_to_response(policy)
    except ValueError as e:
        # Policy not found
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy {policy_id} not found: {str(e)}",
        )
    except Exception as e:
        logger.error(
            "Unexpected error updating policy",
            policy_id=str(policy_id),
            tenant_id=str(tenant_id),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update policy: {str(e)}",
        )


@router.delete(
    "/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Policy",
    description="""
Delete a policy (soft delete - marks as inactive).

Deleted policies are retained for audit purposes but are no longer
applied to event processing.

Note: Global safety policies cannot be deleted.
    """,
    responses={
        204: {
            "description": "Policy deleted successfully",
        },
        403: {
            "description": "Cannot delete global policies",
            "model": ErrorResponse,
        },
        404: {
            "description": "Policy not found",
            "model": ErrorResponse,
        },
    },
    tags=["Policies"],
)
async def delete_policy(
    policy_id: UUID,
    tenant_id: UUID = Query(..., description="Tenant ID for authorization"),
) -> None:
    """
    Delete a policy (soft delete).
    """
    # Check if it's a global policy
    global_policy = get_global_policy()
    if policy_id == global_policy.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Global safety policies cannot be deleted",
        )

    store = get_policy_store()
    deleted = await store.delete_policy(policy_id, tenant_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy {policy_id} not found",
        )


@router.get(
    "/tenant/{tenant_id}/effective",
    response_model=EffectivePolicyResponse,
    summary="Get Effective Policies",
    description="""
Get the merged, effective policies for a tenant.

This returns all applicable rules after merging global and tenant
policies by priority. This is the actual rule set that will be
applied during event processing.

## Merge Algorithm

1. Collect all applicable policies (global + tenant)
2. Sort by priority (descending)
3. Merge rules, with higher-priority rules taking precedence
4. Return the final effective rule set
    """,
    responses={
        200: {
            "description": "Effective policies retrieved successfully",
            "model": EffectivePolicyResponse,
        },
        404: {
            "description": "Tenant not found",
            "model": ErrorResponse,
        },
    },
    tags=["Policies"],
)
async def get_effective_policies(tenant_id: UUID) -> EffectivePolicyResponse:
    """
    Get the effective (merged) policies for a tenant.

    This returns global + tenant policies merged by priority.
    """
    merger = get_policy_merger()

    # Get merged policies
    policies = await merger.get_merged_policies(tenant_id)
    rules = await merger.get_merged_rules(tenant_id)

    # Convert to effective rules format
    effective_rules = []
    global_policy = get_global_policy()

    for rule in rules:
        # Determine source
        is_global = any(r.rule_id == rule.rule_id for r in global_policy.rules)

        effective_rules.append(
            {
                "rule_id": rule.rule_id,
                "rule": rule.rule,
                "message": rule.message,
                "severity": rule.severity.value,
                "action": rule.action.value,
                "patterns": rule.patterns,
                "source": "global" if is_global else "tenant",
                "source_policy_id": str(global_policy.id) if is_global else None,
            }
        )

    source_policies = [
        {
            "id": str(p.id),
            "name": p.name,
            "priority": p.priority,
            "type": p.policy_type.value,
        }
        for p in policies
    ]

    return EffectivePolicyResponse(
        tenant_id=tenant_id,
        effective_rules=effective_rules,
        source_policies=source_policies,
        computed_at=datetime.utcnow().isoformat(),
    )


@router.post(
    "/evaluate",
    summary="Evaluate Message Against Policies",
    description="""
Evaluate a message against all applicable policies.

This is useful for testing policy configurations before deployment.
    """,
    responses={
        200: {
            "description": "Evaluation completed",
        },
    },
    tags=["Policies"],
)
async def evaluate_message(
    tenant_id: UUID = Query(..., description="Tenant ID"),
    session_id: UUID = Query(..., description="Session ID"),
    message: str = Query(..., description="Message to evaluate"),
) -> dict[str, Any]:
    """
    Evaluate a message against policies.
    """
    engine = get_policy_engine()
    result = await engine.evaluate_pre_srm(
        tenant_id=tenant_id,
        session_id=session_id,
        message=message,
    )

    return {
        "passed": result.passed,
        "should_block": result.should_block,
        "should_handoff": result.should_handoff,
        "violation_count": len(result.violations),
        "violations": [
            {
                "rule_id": v.rule_id,
                "rule_name": v.rule_name,
                "message": v.message,
                "severity": v.severity.value,
                "action": v.action.value,
            }
            for v in result.violations
        ],
        "warnings": result.warnings,
        "constraints": result.constraints,
        "evaluation_time_ms": result.evaluation_time_ms,
    }
