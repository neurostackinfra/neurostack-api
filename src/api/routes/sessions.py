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

import jwt as pyjwt

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    require_builder_clinic_access,
)
from src.config.logging import get_logger
from src.config.settings import settings
from src.api.schemas.base import ErrorResponse

_logger = get_logger(__name__)


async def _resolve_tenant(
    request: Request,
    tenant: TenantContext,
    override_tenant_id: Optional[UUID],
    db: AsyncSession,
) -> UUID:
    """Resolve effective tenant_id, supporting builder clinic override."""
    if override_tenant_id is None:
        return tenant.tenant_id

    if tenant.role != "builder":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tenant_id param not allowed for role '{tenant.role}'",
        )

    auth_header = request.headers.get("Authorization", "")
    builder_id_str: Optional[str] = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            payload = pyjwt.decode(
                token, settings.secret_key,
                algorithms=["HS256"],
                options={"require": ["sub", "tenant_id", "exp"]},
            )
            builder_id_str = payload.get("builder_id")
        except (pyjwt.InvalidTokenError, KeyError, ValueError):
            pass

    if not builder_id_str:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Builder ID not found in token")

    builder_id = UUID(builder_id_str)
    _logger.info("builder_clinic_override", extra={"builder_id": str(builder_id), "tenant_id": str(override_tenant_id), "endpoint": request.url.path})
    return await require_builder_clinic_access(override_tenant_id, builder_id, db)
from src.api.schemas.sessions import (
    CognitiveState,
    SessionCreateRequest,
    SessionEventsResponse,
    SessionListResponse,
    SessionResponse,
    SessionUpdateRequest,
)
from src.repositories import get_db_session_dependency
from src.services import SessionService, get_session_service

router = APIRouter()


# ============================================
# Endpoints
# ============================================


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
    # Builder creating session for a clinic: validate ownership
    if tenant.role == "builder" and request.tenant_id != tenant.tenant_id:
        await _resolve_tenant(http_request, tenant, request.tenant_id, db)
        # _resolve_tenant validates ownership; session uses request.tenant_id as-is

    service = get_session_service()
    try:
        result = await service.create_session(request, db)
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
    db: AsyncSession = Depends(get_db_session_dependency),
) -> SessionResponse:
    """
    Retrieve session details by ID.
    """
    service = get_session_service()
    try:
        return await service.get_session(session_id, db)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


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
    # For builders, validate they own the requested tenant
    if tenant.role == "builder" and tenant_id != tenant.tenant_id:
        effective_tenant_id = await _resolve_tenant(request, tenant, tenant_id, db)
    else:
        effective_tenant_id = tenant_id
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
    db: AsyncSession = Depends(get_db_session_dependency),
) -> SessionResponse:
    """
    Update session metadata or status.
    """
    service = get_session_service()
    try:
        result = await service.update_session(session_id, request, db)
        await db.commit()
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


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
    db: AsyncSession = Depends(get_db_session_dependency),
) -> None:
    """
    End a session (mark as inactive).
    """
    service = get_session_service()
    try:
        await service.end_session(session_id, db)
        await db.commit()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


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
    limit: int = Query(default=50, ge=1, le=200, description="Maximum events to return"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> SessionEventsResponse:
    """
    Retrieve events for a session from Event Store.
    Builder role can pass tenant_id of a clinic they manage.
    """
    if tenant.role == "builder" and tenant_id != tenant.tenant_id:
        effective_tenant_id = await _resolve_tenant(request, tenant, tenant_id, db)
    else:
        effective_tenant_id = tenant_id
    service = get_session_service()
    return await service.get_session_events(
        session_id=session_id,
        tenant_id=effective_tenant_id,
        db=db,
        limit=limit,
        offset=offset,
    )


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
    db: AsyncSession = Depends(get_db_session_dependency),
) -> CognitiveState:
    """
    Get the cognitive state of a session.
    """
    service = get_session_service()
    try:
        return await service.get_session_state(session_id, db)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
