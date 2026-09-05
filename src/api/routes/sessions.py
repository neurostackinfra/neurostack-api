"""
NeuroStack API - Sessions Endpoint
Session management and state retrieval.

Sessions track the cognitive state of a conversation, including:
- Funnel stage (lead_novo, em_qualificacao, qualificado, followup, agendamento_solicitado, agendado, paciente_ativo, pos_atendimento, fidelizacao, inativo, standby)
- Extracted slot values (entities, preferences)
- Intent history
- Safety trigger count
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    resolve_effective_tenant,
)
from src.api.schemas.base import ErrorResponse
from src.api.schemas.sessions import (
    CognitiveState,
    InactiveSessionsResponse,
    SessionCreateRequest,
    SessionEventsResponse,
    SessionListResponse,
    SessionReactivationResponse,
    SessionResponse,
    SessionUpdateRequest,
)
from src.config.logging import get_logger
from src.repositories import get_db_session_dependency
from src.services import get_session_service
from src.services.session_service import SessionReactivationError

_logger = get_logger(__name__)


router = APIRouter()


def _require_queue_role(tenant: TenantContext) -> None:
    """Keep administrative queue endpoints JWT-only and deny by default."""
    if tenant.api_key or tenant.role not in {"clinic_admin", "builder", "super_admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "caller_role_forbidden"},
        )


def _session_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "session_not_found"},
    )


# ============================================
# Endpoints
# ============================================


@router.get(
    "/inactive",
    response_model=InactiveSessionsResponse,
    summary="List Permanently Paused Sessions",
    tags=["Sessions"],
)
async def list_inactive_sessions(
    request: Request,
    tenant_id: Optional[UUID] = Query(
        default=None,
        description="Clinic tenant override for builder or super admin",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> InactiveSessionsResponse:
    """List the effective permanently paused sessions for one tenant."""
    _require_queue_role(tenant)
    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)
    return await get_session_service().list_inactive_sessions(
        tenant_id=effective_tenant_id,
        db=db,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/{session_id}/reactivate",
    response_model=SessionReactivationResponse,
    summary="Reactivate Permanently Paused Session",
    tags=["Sessions"],
)
async def reactivate_session(
    session_id: UUID,
    request: Request,
    tenant_id: Optional[UUID] = Query(
        default=None,
        description="Clinic tenant override for builder or super admin",
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> SessionReactivationResponse:
    """Return control to AI for the authorized effective active session."""
    _require_queue_role(tenant)
    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)
    try:
        response = await get_session_service().reactivate_session(
            tenant_id=effective_tenant_id,
            session_id=session_id,
            db=db,
        )
        await db.commit()
        return response
    except SessionReactivationError as error:
        if error.code == "not_found":
            raise _session_not_found() from error
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.code,
        ) from error


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Session",
    description="""
Create a new conversation session.

Sessions are the primary context container for conversations. Each session
maintains its own cognitive state, including funnel stage, extracted slots,
and conversation history.

## Session Lifecycle

1. **Create**: Use this endpoint to initialize a new session
2. **Process Events**: Send events to `/v1/events` with the session_id
3. **Query State**: Use GET to retrieve current cognitive state
4. **End Session**: Use DELETE to mark session as inactive

## TTL (Time to Live)

Sessions automatically expire after the configured TTL (default: 24 hours).
TTL is reset with each event processed.
    """,
    responses={
        201: {
            "description": "Session created successfully",
            "model": SessionResponse,
        },
        400: {
            "description": "Invalid request",
            "model": ErrorResponse,
        },
        404: {
            "description": "Tenant not found",
            "model": ErrorResponse,
        },
    },
    tags=["Sessions"],
)
async def create_session(
    request: SessionCreateRequest,
    http_request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> SessionResponse:
    """
    Create a new conversation session.

    When a builder creates a session for a clinic they manage,
    the session is created with the clinic's tenant_id (not the builder's).
    """
    effective_tenant_id = await resolve_effective_tenant(
        http_request, tenant, request.tenant_id, db
    )

    service = get_session_service()
    try:
        result = await service.create_session(
            request,
            db,
            tenant_id=effective_tenant_id,
        )
        # Explicit commit — ensures the session row is visible to other
        # connections (e.g. EventStore) before the response is sent.
        await db.commit()
        return result
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_msg,
            )
        elif "not active" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=error_msg,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )
    except HTTPException:
        raise
    except Exception as e:
        _logger.error("Unexpected error creating session", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create session",
        )


@router.get(
    "/{session_id}",
    response_model=SessionResponse,
    summary="Get Session",
    description="""
Retrieve session details and current cognitive state.

Returns the complete session information including:
- Cognitive state (funnel stage, slots, flags)
- Activity timestamps
- Message count
- Handoff status (if applicable)
    """,
    responses={
        200: {
            "description": "Session retrieved successfully",
            "model": SessionResponse,
        },
        404: {
            "description": "Session not found",
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": {
                        "error": "session_not_found",
                        "message": "Session not found or has expired",
                    }
                }
            },
        },
    },
    tags=["Sessions"],
)
async def get_session(
    session_id: UUID,
    request: Request,
    tenant_id: Optional[UUID] = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> SessionResponse:
    """
    Retrieve session details by ID.
    """
    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)
    service = get_session_service()
    try:
        return await service.get_session(session_id, effective_tenant_id, db)
    except ValueError as error:
        raise _session_not_found() from error


@router.get(
    "",
    response_model=SessionListResponse,
    summary="List Sessions",
    description="""
List sessions for a tenant with pagination.

Supports filtering by:
- Active status
- Channel
- Date range

Results are ordered by last activity (most recent first).
    """,
    responses={
        200: {
            "description": "Sessions listed successfully",
            "model": SessionListResponse,
        },
    },
    tags=["Sessions"],
)
async def list_sessions(
    request: Request,
    tenant_id: UUID = Query(..., description="Tenant ID to filter by"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    active_only: bool = Query(default=True, description="Only return active sessions"),
    channel: Optional[str] = Query(default=None, description="Filter by channel"),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> SessionListResponse:
    """
    List sessions for a tenant.
    Builder role can pass tenant_id of a clinic they manage.
    Non-builder roles must pass their own tenant_id.
    """
    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)
    service = get_session_service()
    return await service.list_sessions(
        tenant_id=effective_tenant_id,
        db=db,
        page=page,
        page_size=page_size,
        active_only=active_only,
        channel=channel,
    )


@router.patch(
    "/{session_id}",
    response_model=SessionResponse,
    summary="Update Session",
    description="""
Update session metadata or status.

This endpoint allows updating:
- Session metadata
- Active status (to manually end a session)

Note: Cognitive state is updated automatically through event processing.
    """,
    responses={
        200: {
            "description": "Session updated successfully",
            "model": SessionResponse,
        },
        404: {
            "description": "Session not found",
            "model": ErrorResponse,
        },
    },
    tags=["Sessions"],
)
async def update_session(
    session_id: UUID,
    request: SessionUpdateRequest,
    http_request: Request,
    tenant_id: Optional[UUID] = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> SessionResponse:
    """
    Update session metadata or status.
    """
    effective_tenant_id = await resolve_effective_tenant(
        http_request, tenant, tenant_id, db
    )
    service = get_session_service()
    try:
        result = await service.update_session(
            session_id,
            effective_tenant_id,
            request,
            db,
        )
        await db.commit()
        return result
    except ValueError as error:
        raise _session_not_found() from error


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End Session",
    description="""
End/deactivate a session.

This marks the session as inactive. The session data is retained
for analytics and can still be queried, but no new events will
be accepted for this session.
    """,
    responses={
        204: {
            "description": "Session ended successfully",
        },
        404: {
            "description": "Session not found",
            "model": ErrorResponse,
        },
    },
    tags=["Sessions"],
)
async def end_session(
    session_id: UUID,
    request: Request,
    tenant_id: Optional[UUID] = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> None:
    """
    End a session (mark as inactive).
    """
    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)
    service = get_session_service()
    try:
        await service.end_session(session_id, effective_tenant_id, db)
        await db.commit()
    except ValueError as error:
        raise _session_not_found() from error


@router.get(
    "/{session_id}/events",
    response_model=SessionEventsResponse,
    summary="Get Session Events",
    description="""
Retrieve all events for a session.

Returns the complete event history including:
- Event type and payload
- SRM decisions
- Timestamps

Useful for debugging and conversation analysis.
    """,
    responses={
        200: {
            "description": "Events retrieved successfully",
            "model": SessionEventsResponse,
        },
        404: {
            "description": "Session not found",
            "model": ErrorResponse,
        },
    },
    tags=["Sessions"],
)
async def get_session_events(
    session_id: UUID,
    request: Request,
    tenant_id: UUID = Query(..., description="Tenant ID"),
    limit: int = Query(
        default=50, ge=1, le=200, description="Maximum events to return"
    ),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> SessionEventsResponse:
    """
    Retrieve events for a session from Event Store.
    Builder role can pass tenant_id of a clinic they manage.
    """
    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)
    service = get_session_service()
    try:
        return await service.get_session_events(
            session_id=session_id,
            tenant_id=effective_tenant_id,
            db=db,
            limit=limit,
            offset=offset,
        )
    except ValueError as error:
        raise _session_not_found() from error


@router.get(
    "/{session_id}/state",
    response_model=CognitiveState,
    summary="Get Cognitive State",
    description="""
Get just the cognitive state of a session.

Returns only the cognitive state portion of the session,
useful for quick state lookups.
    """,
    responses={
        200: {
            "description": "Cognitive state retrieved successfully",
            "model": CognitiveState,
        },
        404: {
            "description": "Session not found",
            "model": ErrorResponse,
        },
    },
    tags=["Sessions"],
)
async def get_session_state(
    session_id: UUID,
    request: Request,
    tenant_id: Optional[UUID] = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> CognitiveState:
    """
    Get the cognitive state of a session.
    """
    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)
    service = get_session_service()
    try:
        return await service.get_session_state(session_id, effective_tenant_id, db)
    except ValueError as error:
        raise _session_not_found() from error
