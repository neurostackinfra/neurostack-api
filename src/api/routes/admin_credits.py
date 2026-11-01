"""Credit operational admin API (Group 5): roles, signed authorizations,
legal hold events and the (disabled) purge.

Mounted at ``/v1/admin/credits``.  A credit surface (``credit_router``): with
the credit switch not effective every route answers ``503 {"code":
"credit_unavailable"}`` before any dependency or database work.

* ``POST /role-assignments`` / ``POST /role-assignments/{id}/revocations`` /
  ``GET /role-assignments`` -- a JWT ``super_admin`` only; a grant is never
  for the caller itself (moved here from Group 1a, with the issuance below).
* ``POST /authorizations`` -- a JWT ``super_admin`` issues one signed,
  single-use authorization to ANOTHER user; an absent or short
  ``credit_authorization_signing_key`` fails closed (503).
* ``POST /holds/{request|activate|release}`` -- the authenticated JWT user
  (the actor; never a body field) with the persisted role and a valid
  authorization; one event per ``SERIALIZABLE`` transaction.
* ``POST /purges`` -- ``409 {"code": "purge_disabled"}`` for every call of a
  JWT ``super_admin``, before reading the body or touching anything.

Refusals answer ``{"code": ...}`` with nothing written.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.api.schemas.admin_credits import (
    AuthorizationCreate,
    AuthorizationResponse,
    CreditBindingStatus,
    CreditCapabilityStatus,
    CreditReconciliationStatus,
    CreditRolloutStatus,
    HoldEventCreate,
    HoldEventResponse,
    RefusalResponse,
    RoleAssignmentCreate,
    RoleAssignmentItem,
    RoleAssignmentResponse,
    RoleAssignmentRevoke,
)
from src.config.settings import settings
from src.repositories.content_retention import ContentRetentionRepository
from src.repositories.credit import CreditRepository
from src.repositories.database import get_db_session_dependency
from src.services import content_retention
from src.services.content_retention import (
    Actor,
    HoldEventType,
    PresentedAuthorization,
    RetentionRefused,
)
from src.services.credit_rollout import CreditRolloutService
from src.services.credit_switch import credit_router

router = credit_router()

_REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"model": RefusalResponse, "description": "Refused (nothing written)."},
    409: {"model": RefusalResponse, "description": "Conflict (nothing written)."},
    503: {"model": RefusalResponse, "description": "credit_unavailable or key."},
}


def _actor(tenant: TenantContext) -> Actor:
    """The caller from the verified JWT; an API key is never an actor."""

    if tenant.api_key or tenant.authentication_mode != "jwt" or tenant.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"code": "jwt_required"}
        )
    return Actor(user_id=tenant.user_id, role=tenant.role, jwt=True)


def _super_admin(tenant: TenantContext = Depends(get_tenant_context)) -> Actor:
    actor = _actor(tenant)
    if not actor.super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "super_admin_jwt_required"},
        )
    return actor


def _jwt_actor(tenant: TenantContext = Depends(get_tenant_context)) -> Actor:
    return _actor(tenant)


def _refusal(error: RetentionRefused) -> JSONResponse:
    return JSONResponse(status_code=error.status, content={"code": error.code})


@router.get(
    "/rollout/reconciliation",
    response_model=CreditReconciliationStatus,
    responses=_REFUSALS,
)
async def credit_reconciliation_status(
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditReconciliationStatus:
    """Technical evidence only; never presents facts or changes ASAAS state."""

    del actor
    report = await CreditRolloutService(session).reconciliation_report()
    await session.rollback()
    return CreditReconciliationStatus(**report.counts(), ready=report.ready)


@router.get(
    "/rollout/bindings/{builder_id}",
    response_model=CreditBindingStatus,
    responses=_REFUSALS,
)
async def credit_binding_status(
    builder_id: UUID,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditBindingStatus:
    """Read a builder's non-authoritative credit source coverage only."""

    del actor
    coverage = await CreditRolloutService(session).binding_coverage(builder_id)
    await session.rollback()
    return CreditBindingStatus(
        builder_id=builder_id, **coverage.__dict__, usable=coverage.usable
    )


@router.get(
    "/rollout/status",
    response_model=CreditRolloutStatus,
    responses=_REFUSALS,
)
async def credit_capability_status(
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditRolloutStatus:
    """Read code readiness; neither records a receipt nor arms enforcement.

    Orphans are the FULL-history scan (set-based, a constant number of
    statements, human-triggered), so an orphan older than the periodic window
    stays visible here until it closes.
    """

    del actor
    rollout = CreditRolloutService(session)
    readiness = await rollout.capability_ready()
    orphan = await rollout.orphan_report()
    enforcement_at = await CreditRepository(session).enforcement_at()
    await session.rollback()
    return CreditRolloutStatus(
        enforcement_at=enforcement_at,
        capability_ready=readiness.ready,
        reconciliation_ready=readiness.reconciliation_complete,
        orphan_effects=orphan.open_count,
    )


@router.get(
    "/rollout/readiness",
    response_model=CreditCapabilityStatus,
    responses=_REFUSALS,
)
async def credit_capability_readiness(
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> CreditCapabilityStatus:
    """Detailed code-only capability calculation, intentionally not recorded."""

    del actor
    readiness = await CreditRolloutService(session).capability_ready()
    await session.rollback()
    return CreditCapabilityStatus(
        environment=readiness.environment,
        billing_fact_schema_digest=readiness.billing_fact_schema_digest,
        inbox_high_watermark=readiness.inbox_high_watermark,
        reconciliation_complete=readiness.reconciliation_complete,
        readback_complete=readiness.readback_complete,
        has_open_gap=readiness.has_open_gap,
        has_backlog=readiness.has_backlog,
        has_alert=readiness.has_alert,
        ready=readiness.ready,
    )


@router.post(
    "/role-assignments",
    status_code=status.HTTP_201_CREATED,
    response_model=RoleAssignmentResponse,
    responses=_REFUSALS,
)
async def grant_role(
    body: RoleAssignmentCreate,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    try:
        assignment_id = await content_retention.grant_assignment(
            session,
            actor,
            role=body.role,
            user_id=body.user_id,
            reason=body.reason,
            expires_at=body.expires_at,
        )
        await session.commit()
    except RetentionRefused as error:
        await session.rollback()
        return _refusal(error)
    return RoleAssignmentResponse(assignment_id=assignment_id)


@router.post(
    "/role-assignments/{assignment_id}/revocations",
    status_code=status.HTTP_201_CREATED,
    response_model=RoleAssignmentResponse,
    responses=_REFUSALS,
)
async def revoke_role(
    assignment_id: UUID,
    body: RoleAssignmentRevoke,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    try:
        revocation = await content_retention.revoke_assignment(
            session, actor, assignment_id=assignment_id, reason=body.reason
        )
        await session.commit()
    except RetentionRefused as error:
        await session.rollback()
        return _refusal(error)
    return RoleAssignmentResponse(assignment_id=revocation)


@router.get(
    "/role-assignments",
    response_model=list[RoleAssignmentItem],
    responses=_REFUSALS,
)
async def list_roles(
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    del actor
    rows = await ContentRetentionRepository(session).list_assignments(
        settings.environment
    )
    items = [
        RoleAssignmentItem(
            id=row.id,
            role=row.role,
            user_id=row.user_id,
            environment=row.environment,
            action=row.action,
            revokes_assignment_id=row.revokes_assignment_id,
            actor_user_id=row.actor_user_id,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )
        for row in rows
    ]
    # Read-only: end the transaction only after the rows were read (a
    # rollback expires them).
    await session.rollback()
    return items


@router.post(
    "/authorizations",
    status_code=status.HTTP_201_CREATED,
    response_model=AuthorizationResponse,
    responses=_REFUSALS,
)
async def issue_authorization(
    body: AuthorizationCreate,
    actor: Actor = Depends(_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    try:
        issued = await content_retention.issue_authorization(
            session,
            actor,
            purpose=body.purpose,
            builder_id=body.builder_id,
            scope=body.scope,
            subject_user_id=body.subject_user_id,
            resource=body.resource,
            fence_generation=body.fence_generation,
            ttl=timedelta(seconds=body.ttl_seconds),
        )
        await session.commit()
    except RetentionRefused as error:
        await session.rollback()
        return _refusal(error)
    return AuthorizationResponse(
        authorization_id=issued.authorization_id,
        signature=issued.signature,
        payload=issued.payload,
    )


async def _hold(
    event_type: HoldEventType,
    body: HoldEventCreate,
    actor: Actor,
    session: AsyncSession,
) -> Any:
    try:
        result = await content_retention.record_hold_event(
            session,
            actor,
            event_type=event_type,
            builder_id=body.builder_id,
            scope=body.scope,
            presented=PresentedAuthorization(
                body.authorization_id, body.authorization_signature
            ),
            reason_code=body.reason_code,
            intent_id=body.intent_id,
            justification=body.justification,
        )
    except RetentionRefused as error:
        return _refusal(error)
    return HoldEventResponse(
        event_id=result.event_id,
        intent_id=result.intent_id,
        event_type=result.event_type,
        generation=result.generation,
    )


@router.post(
    "/holds/request",
    status_code=status.HTTP_201_CREATED,
    response_model=HoldEventResponse,
    responses=_REFUSALS,
)
async def request_hold(
    body: HoldEventCreate,
    actor: Actor = Depends(_jwt_actor),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    return await _hold("hold_requested", body, actor, session)


@router.post(
    "/holds/activate",
    status_code=status.HTTP_201_CREATED,
    response_model=HoldEventResponse,
    responses=_REFUSALS,
)
async def activate_hold(
    body: HoldEventCreate,
    actor: Actor = Depends(_jwt_actor),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    return await _hold("hold_activated", body, actor, session)


@router.post(
    "/holds/release",
    status_code=status.HTTP_201_CREATED,
    response_model=HoldEventResponse,
    responses=_REFUSALS,
)
async def release_hold(
    body: HoldEventCreate,
    actor: Actor = Depends(_jwt_actor),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Any:
    return await _hold("hold_released", body, actor, session)


@router.post(
    "/purges",
    status_code=status.HTTP_409_CONFLICT,
    response_model=RefusalResponse,
    responses=_REFUSALS,
)
async def purge(request: Request, actor: Actor = Depends(_super_admin)) -> Any:
    """Always ``purge_disabled`` (Groups 1a-7): the body is never read; no
    hold, fence, storage, authorization or receipt is touched."""

    del request, actor
    outcome = await content_retention.request_purge()
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"code": outcome.code}
    )


__all__ = ["router"]
