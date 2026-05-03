"""
NeuroStack API - Traces Endpoint
Read-only trace views over the events table.

Traces expose a curated view of message.in events that were processed
through the cognitive pipeline, including SRM decisions, context, and prompts.
"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

import jwt as pyjwt

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    require_builder_clinic_access,
)
from src.config.logging import get_logger
from src.config.settings import settings

_logger = get_logger(__name__)


async def _resolve_tenant(
    request: Request,
    tenant: TenantContext,
    override_tenant_id: Optional[UUID],
    db: AsyncSession,
) -> UUID:
    """Resolve effective tenant_id, supporting builder clinic override.
    For builders without explicit tenant_id, auto-resolve to first managed clinic.
    """
    if override_tenant_id is None:
        # For builders, auto-resolve to first managed clinic
        if tenant.role == "builder":
            from src.models.tenant import Tenant
            from sqlalchemy import select as sa_select
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
                try:
                    payload = pyjwt.decode(
                        token, settings.secret_key,
                        algorithms=["HS256"],
                        options={"require": ["sub", "tenant_id", "exp"]},
                    )
                    bid = payload.get("builder_id")
                    if bid:
                        result = await db.execute(
                            sa_select(Tenant.id)
                            .where(Tenant.builder_id == UUID(bid))
                            .order_by(Tenant.name)
                            .limit(1)
                        )
                        first_clinic = result.scalar_one_or_none()
                        if first_clinic:
                            return first_clinic
                except (pyjwt.InvalidTokenError, KeyError, ValueError):
                    pass
        return tenant.tenant_id

    if tenant.role != "builder":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tenant_id param not allowed for role '{tenant.role}'",
        )

    # Extract builder_id from JWT
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
from src.api.schemas.traces import (
    TraceDetailResponse,
    TraceItem,
    TraceListResponse,
    TraceStatsResponse,
)
from src.repositories import get_db_session_dependency
from src.services.trace_service import (
    _event_to_trace_dict,
    export_traces_csv,
    get_prompt_event_for_trace,
    get_trace,
    get_trace_stats,
    list_traces,
)

router = APIRouter()


# ============================================
# Endpoints
# ============================================


@router.get(
    "/stats",
    response_model=TraceStatsResponse,
    summary="Trace Statistics",
    description="""
Get aggregated statistics for traces within a time period.

Returns counts, averages, and distributions for all processed messages.
Defaults to the last 7 days if no date range is provided.
    """,
    tags=["Traces"],
)
async def trace_stats(
    request: Request,
    start_date: Optional[datetime] = Query(
        default=None, description="Start of period (ISO 8601)"
    ),
    end_date: Optional[datetime] = Query(
        default=None, description="End of period (ISO 8601)"
    ),
    override_tenant_id: Optional[UUID] = Query(
        default=None, alias="tenant_id",
        description="Builder override: view stats for a managed clinic",
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> TraceStatsResponse:
    """Get aggregated trace statistics."""
    effective_tenant_id = await _resolve_tenant(request, tenant, override_tenant_id, db)
    stats = await get_trace_stats(
        db=db,
        tenant_id=effective_tenant_id,
        start_date=start_date,
        end_date=end_date,
    )
    return TraceStatsResponse(**stats)


@router.get(
    "/export",
    summary="Export Traces as CSV",
    description="""
Export traces as a CSV download.

Supports the same filters as the list endpoint. Returns a streaming
CSV response with Content-Disposition header for download.
    """,
    tags=["Traces"],
)
async def trace_export(
    request: Request,
    start_date: Optional[datetime] = Query(
        default=None, description="Filter from date (ISO 8601)"
    ),
    end_date: Optional[datetime] = Query(
        default=None, description="Filter to date (ISO 8601)"
    ),
    safety_flag: Optional[bool] = Query(
        default=None, description="Filter by safety concern flag"
    ),
    handoff_required: Optional[bool] = Query(
        default=None, description="Filter by handoff required flag"
    ),
    channel: Optional[str] = Query(
        default=None, description="Filter by channel (whatsapp, web)"
    ),
    override_tenant_id: Optional[UUID] = Query(
        default=None, alias="tenant_id",
        description="Builder override: export traces for a managed clinic",
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> StreamingResponse:
    """Export traces as a CSV download."""
    effective_tenant_id = await _resolve_tenant(request, tenant, override_tenant_id, db)
    csv_content = await export_traces_csv(
        db=db,
        tenant_id=effective_tenant_id,
        start_date=start_date,
        end_date=end_date,
        safety_flag=safety_flag,
        handoff_required=handoff_required,
        channel=channel,
    )

    def iterate_csv():
        yield csv_content

    return StreamingResponse(
        iterate_csv(),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=traces_export.csv",
        },
    )


@router.get(
    "/{trace_id}",
    response_model=TraceDetailResponse,
    summary="Get Trace Detail",
    description="""
Retrieve detailed information for a single trace by ID.

Returns the full trace including context and prompt objects.
Returns 404 if the trace does not exist or belongs to a different tenant.
    """,
    responses={
        404: {
            "description": "Trace not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Trace not found",
                    }
                }
            },
        },
    },
    tags=["Traces"],
)
async def trace_detail(
    trace_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
    override_tenant_id: Optional[UUID] = Query(None, alias="tenant_id", description="Builder clinic override"),
) -> TraceDetailResponse:
    """Get detailed information for a single trace."""
    effective_tenant_id = await _resolve_tenant(request, tenant, override_tenant_id, db)
    event = await get_trace(db=db, tenant_id=effective_tenant_id, trace_id=trace_id)

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trace not found",
        )

    trace = _event_to_trace_dict(event)
    event_data = event.event_data or {}
    metadata = event.event_metadata or {}

    # Fetch the associated prompt.generated event for full prompt/context
    full_context = event_data.get("context")
    full_prompt = event_data.get("prompt")
    prompt_built = trace["prompt_built"]

    llm_response = None
    selected_context: list[dict[str, Any]] = []

    if not full_prompt and event.session_id and event.created_at:
        prompt_event = await get_prompt_event_for_trace(
            db=db,
            tenant_id=effective_tenant_id,
            session_id=event.session_id,
            trace_created_at=event.created_at,
        )
        if prompt_event:
            prompt_data = prompt_event.event_data or {}
            full_prompt = prompt_data
            full_context = prompt_data.get("context_summary")
            if not prompt_built:
                prompt_built = prompt_data.get("system_prompt")
            llm_response = prompt_data.get("suggested_response")
            selected_context = prompt_data.get("context_sources", [])

    return TraceDetailResponse(
        id=trace["id"],
        timestamp=trace["timestamp"],
        user_message=trace["user_message"],
        srm_decision=trace["srm_decision"],
        context_summary=trace["context_summary"],
        prompt_built=prompt_built,
        processing_time_ms=trace["processing_time_ms"],
        mode=trace["mode"],
        full_context=full_context,
        full_prompt=full_prompt,
        session_id=event.session_id,
        event_data=event_data,
        event_metadata=metadata,
        llm_response=llm_response,
        selected_context=selected_context,
    )


@router.get(
    "",
    response_model=TraceListResponse,
    summary="List Traces",
    description="""
List traces with pagination and optional filtering.

Traces are read-only views over processed message events. Each trace
includes the SRM decision, processing metadata, and flow mode.

Supports filtering by date range, safety flag, and handoff requirement.
    """,
    tags=["Traces"],
)
async def trace_list(
    request: Request,
    limit: int = Query(
        default=20, ge=1, le=100, description="Maximum traces to return"
    ),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    start_date: Optional[datetime] = Query(
        default=None, description="Filter from date (ISO 8601)"
    ),
    end_date: Optional[datetime] = Query(
        default=None, description="Filter to date (ISO 8601)"
    ),
    safety_flag: Optional[bool] = Query(
        default=None, description="Filter by safety concern flag"
    ),
    handoff_required: Optional[bool] = Query(
        default=None, description="Filter by handoff required flag"
    ),
    channel: Optional[str] = Query(
        default=None, description="Filter by channel (whatsapp, web)"
    ),
    override_tenant_id: Optional[UUID] = Query(None, alias="tenant_id", description="Builder clinic override"),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> TraceListResponse:
    """List traces with pagination and filtering."""
    effective_tenant_id = await _resolve_tenant(request, tenant, override_tenant_id, db)
    events, total = await list_traces(
        db=db,
        tenant_id=effective_tenant_id,
        limit=limit,
        offset=offset,
        start_date=start_date,
        end_date=end_date,
        safety_flag=safety_flag,
        handoff_required=handoff_required,
        channel=channel,
    )

    items = [
        TraceItem(**_event_to_trace_dict(event))
        for event in events
    ]

    return TraceListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )
