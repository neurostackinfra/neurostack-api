"""
NeuroStack API - Traces Endpoint
Read-only trace views over the events table.

Traces expose a curated view of message.in events that were processed
through the cognitive pipeline, including SRM decisions, context, and prompts.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context
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
    start_date: Optional[datetime] = Query(
        default=None, description="Start of period (ISO 8601)"
    ),
    end_date: Optional[datetime] = Query(
        default=None, description="End of period (ISO 8601)"
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> TraceStatsResponse:
    """Get aggregated trace statistics."""
    stats = await get_trace_stats(
        db=db,
        tenant_id=tenant.tenant_id,
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
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> StreamingResponse:
    """Export traces as a CSV download."""
    csv_content = await export_traces_csv(
        db=db,
        tenant_id=tenant.tenant_id,
        start_date=start_date,
        end_date=end_date,
        safety_flag=safety_flag,
        handoff_required=handoff_required,
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
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> TraceDetailResponse:
    """Get detailed information for a single trace."""
    event = await get_trace(db=db, tenant_id=tenant.tenant_id, trace_id=trace_id)

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trace not found",
        )

    trace = _event_to_trace_dict(event)
    event_data = event.event_data or {}
    metadata = event.event_metadata or {}

    return TraceDetailResponse(
        id=trace["id"],
        timestamp=trace["timestamp"],
        user_message=trace["user_message"],
        srm_decision=trace["srm_decision"],
        context_summary=trace["context_summary"],
        prompt_built=trace["prompt_built"],
        processing_time_ms=trace["processing_time_ms"],
        mode=trace["mode"],
        full_context=event_data.get("context"),
        full_prompt=event_data.get("prompt"),
        session_id=event.session_id,
        event_data=event_data,
        event_metadata=metadata,
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
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> TraceListResponse:
    """List traces with pagination and filtering."""
    events, total = await list_traces(
        db=db,
        tenant_id=tenant.tenant_id,
        limit=limit,
        offset=offset,
        start_date=start_date,
        end_date=end_date,
        safety_flag=safety_flag,
        handoff_required=handoff_required,
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
