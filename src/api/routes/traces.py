"""
NeuroStack API - Traces Endpoint
Read-only trace views over the events table.

Traces expose a curated view of message.in events that were processed
through the cognitive pipeline, including SRM decisions, context, and prompts.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Optional, TypeAlias
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jwt as pyjwt
from annotated_types import Ge, Le
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    require_builder_clinic_access,
)
from src.api.schemas.traces import (
    TraceDetailResponse,
    TraceItem,
    TraceListResponse,
    TraceStatsResponse,
)
from src.config.logging import get_logger
from src.config.settings import settings
from src.handoff.sla import resolve_clinic_zone
from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant
from src.observability.pii import mask_carteirinha
from src.repositories import get_db_session_dependency
from src.services.export_window import (
    INVALID_DATE_RANGE,
    ExportWindowError,
    validate_export_window,
)
from src.services.trace_service import (
    _event_to_trace_dict,
    _to_utc,
    daily_utc_window,
    export_traces_csv,
    get_prompt_event_for_trace,
    get_trace,
    get_trace_stats,
    list_traces,
)

_logger = get_logger(__name__)

_SafeTraceDay: TypeAlias = Annotated[
    date,
    Ge(date(1, 1, 2)),
    Le(date(9999, 12, 30)),
]


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
            from sqlalchemy import select as sa_select

            from src.models.tenant import Tenant

            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
                try:
                    payload = pyjwt.decode(
                        token,
                        settings.secret_key,
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

    # Super admin can query any tenant without ownership validation
    if tenant.role == "super_admin":
        return override_tenant_id

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
                token,
                settings.secret_key,
                algorithms=["HS256"],
                options={"require": ["sub", "tenant_id", "exp"]},
            )
            builder_id_str = payload.get("builder_id")
        except (pyjwt.InvalidTokenError, KeyError, ValueError):
            pass

    if not builder_id_str:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Builder ID not found in token",
        )

    builder_id = UUID(builder_id_str)
    _logger.info(
        "builder_clinic_override",
        extra={
            "builder_id": str(builder_id),
            "tenant_id": str(override_tenant_id),
            "endpoint": request.url.path,
        },
    )
    return await require_builder_clinic_access(override_tenant_id, builder_id, db)


async def _load_export_policy(db: AsyncSession, tenant_id: UUID) -> tuple[object, bool]:
    """Load the clinic's raw timezone setting and audit add-on flag."""
    result = await db.execute(
        select(Tenant.audit_extended_access, ClinicConfigTable.operations)
        .select_from(Tenant)
        .outerjoin(ClinicConfigTable, ClinicConfigTable.tenant_id == Tenant.id)
        .where(Tenant.id == tenant_id)
    )
    row = result.one_or_none()
    if row is None:
        return None, False
    extended_access, operations = row
    configured_timezone = (
        operations.get("timezone") if isinstance(operations, dict) else None
    )
    return configured_timezone, bool(extended_access)


def _whole_day_end(value: datetime) -> datetime:
    """Mirror ``_apply_filters``: a midnight end includes its whole day."""
    if value.hour == 0 and value.minute == 0 and value.second == 0:
        return value.replace(hour=23, minute=59, second=59)
    return value


def _export_civil_date(value: datetime, zone: ZoneInfo) -> date:
    """Naive values keep their own date; aware values use the clinic zone."""
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(zone).date()


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
        default=None,
        alias="tenant_id",
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
    return TraceStatsResponse.model_validate(stats)


@router.get(
    "/export",
    summary="Export Traces as CSV",
    description="""
Export traces as a CSV download.

Supports the same filters as the list endpoint. Returns a streaming
CSV response with Content-Disposition header for download.

Without dates the export runs from the clinic's current date minus 30 days
through now (31 inclusive civil dates); a missing `start_date` defaults to
30 clinic dates ago and a missing `end_date` to now. Naive datetimes count
by their own date part, while timezone-aware ones are converted to the clinic
timezone first.

One file covers at most 60 inclusive civil dates and starts no earlier than
6 calendar months before the clinic's current date (same day of month,
clamped to month end). The clinic's `audit_extended_access` add-on waives
only the 6-month lookback.
    """,
    responses={
        400: {
            "description": "Invalid or out-of-policy export window",
            "content": {
                "application/json": {
                    "examples": {
                        "invalid_date_range": {
                            "value": {"detail": "invalid_date_range"}
                        },
                        "export_window_60_days": {
                            "value": {"detail": "export_window_60_days"}
                        },
                        "export_lookback_6_months": {
                            "value": {"detail": "export_lookback_6_months"}
                        },
                    }
                }
            },
        },
    },
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
        default=None,
        alias="tenant_id",
        description="Builder override: export traces for a managed clinic",
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> StreamingResponse:
    """Export traces as a CSV download."""
    effective_tenant_id = await _resolve_tenant(request, tenant, override_tenant_id, db)
    configured_timezone, extended_access = await _load_export_policy(
        db, effective_tenant_id
    )
    _, zone = resolve_clinic_zone(configured_timezone)
    now = datetime.now(UTC)
    try:
        local_today = now.astimezone(zone).date()
        effective_start = (
            start_date
            if start_date is not None
            else datetime.combine(
                local_today - timedelta(days=30), time.min, tzinfo=zone
            ).astimezone(UTC)
        )
        effective_end = end_date if end_date is not None else now
        filtered_end = _whole_day_end(effective_end)
        validate_export_window(
            _export_civil_date(effective_start, zone),
            _export_civil_date(filtered_end, zone),
            local_today=local_today,
            extended_access=extended_access,
        )
        # Pre-check the UTC bounds the query will use so an unrepresentable
        # value is a 400 rather than an error inside the query builder.
        _to_utc(effective_start)
        _to_utc(filtered_end)
    except ExportWindowError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.code,
        ) from exc
    except (OverflowError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_DATE_RANGE,
        ) from exc

    csv_content = await export_traces_csv(
        db=db,
        tenant_id=effective_tenant_id,
        start_date=effective_start,
        end_date=effective_end,
        safety_flag=safety_flag,
        handoff_required=handoff_required,
        channel=channel,
    )

    def iterate_csv() -> Iterator[str]:
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
    override_tenant_id: Optional[UUID] = Query(
        None, alias="tenant_id", description="Builder clinic override"
    ),
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

    # Mask carteirinha PII in the raw debug payload surfaced to the trace
    # detail view — the user's message content is the main leak vector.
    if isinstance(event_data.get("content"), str):
        event_data = {**event_data, "content": mask_carteirinha(event_data["content"])}

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
            llm_response = mask_carteirinha(prompt_data.get("suggested_response"))
            selected_context = prompt_data.get("context_sources", [])

    return TraceDetailResponse.model_validate(
        {
            "id": trace["id"],
            "timestamp": trace["timestamp"],
            "user_message": trace["user_message"],
            "srm_decision": trace["srm_decision"],
            "context_summary": trace["context_summary"],
            "prompt_built": prompt_built,
            "processing_time_ms": trace["processing_time_ms"],
            "mode": trace["mode"],
            "full_context": full_context,
            "full_prompt": full_prompt,
            "session_id": event.session_id,
            "event_data": event_data,
            "event_metadata": metadata,
            "llm_response": llm_response,
            "selected_context": selected_context,
        }
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
For an exact civil day, provide both `day` and its IANA `timezone`;
that pair cannot be combined with `start_date` or `end_date`.
    """,
    responses={
        400: {
            "description": "Invalid day/timezone pair, timezone, or date-range combination",
            "content": {
                "application/json": {
                    "examples": {
                        "timezone_required": {
                            "value": {"detail": "timezone_required_with_day"}
                        },
                        "day_required": {
                            "value": {"detail": "day_required_with_timezone"}
                        },
                        "invalid_timezone": {"value": {"detail": "invalid_timezone"}},
                        "date_range_conflict": {
                            "value": {"detail": "day_conflicts_with_date_range"}
                        },
                    }
                }
            },
        },
        422: {
            "description": "Query parameter validation error",
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/HTTPValidationError",
                    }
                }
            },
        },
    },
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
    day: Optional[_SafeTraceDay] = Query(
        default=None,
        json_schema_extra={
            "minimum": "0001-01-02",
            "maximum": "9999-12-30",
        },
        description=(
            "Exact local civil day. Requires timezone and cannot be combined "
            "with start_date or end_date."
        ),
    ),
    timezone: Optional[str] = Query(
        default=None,
        description=(
            "IANA timezone for day (for example, America/Sao_Paulo). Requires "
            "day and cannot be combined with start_date or end_date."
        ),
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
        None, alias="tenant_id", description="Builder clinic override"
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> TraceListResponse:
    """List traces with pagination and filtering."""
    if day is not None and (start_date is not None or end_date is not None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="day_conflicts_with_date_range",
        )
    if day is not None and timezone is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="timezone_required_with_day",
        )
    if timezone is not None and day is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="day_required_with_timezone",
        )

    day_start_utc: Optional[datetime] = None
    day_end_utc: Optional[datetime] = None
    if day is not None and timezone is not None:
        try:
            day_start_utc, day_end_utc = daily_utc_window(day, timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_timezone",
            ) from None

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
        day_start_utc=day_start_utc,
        day_end_utc=day_end_utc,
    )

    items = [TraceItem.model_validate(_event_to_trace_dict(event)) for event in events]

    return TraceListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )
