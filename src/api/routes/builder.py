"""
NeuroStack API - Builder Endpoints
Builder-scoped routes with SQL-level tenant isolation.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import case, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.types import String

from src.api.middleware.auth import require_builder
from src.api.schemas.builder import (
    BuilderAlertResponse,
    BuilderClinicResponse,
    BuilderDashboardResponse,
    BuilderProfileResponse,
    BuilderStatsResponse,
    CreateBuilderClinicRequest,
    ToggleClinicStatusRequest,
    TraceTimeSeriesPoint,
)
from src.api.helpers.health import (
    CONFIG_COMPLETENESS_BUCKETS,
    _compute_alerts,
    _compute_config_completeness,
    _compute_health_status,
    _get_time_series,
)
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.event import Event
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories import get_db_session_dependency
from src.services.business_hours import summarize_outside_hours
from src.services.trace_service import _is_conversational_trace

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


# ============================================
# GET /me - Builder profile
# ============================================


@router.get(
    "/me",
    summary="Builder Profile",
    description="Return the authenticated builder's profile with clinic count.",
    response_model=BuilderProfileResponse,
)
async def builder_profile(
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    # Fetch builder
    result = await session.execute(
        select(Builder).where(Builder.id == builder_id)
    )
    builder = result.scalar_one_or_none()
    if not builder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder not found",
        )

    # Count clinics scoped to this builder
    count_result = await session.execute(
        select(func.count(Tenant.id)).where(
            Tenant.builder_id == builder_id,
            Tenant.is_active.is_(True),
        )
    )
    clinic_count = count_result.scalar() or 0

    return BuilderProfileResponse(
        id=builder.id,
        name=builder.name,
        status=builder.status,
        clinic_count=clinic_count,
    )


# ============================================
# GET /clinics - List builder's clinics (enriched)
# ============================================


@router.get(
    "/clinics",
    summary="List Builder Clinics",
    description="Return clinics owned by the authenticated builder with health metrics (SQL-level scope isolation).",
)
async def list_clinics(
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    # Fetch tenants with config
    stmt = (
        select(Tenant)
        .options(selectinload(Tenant.clinic_config_row))
        .where(Tenant.builder_id == builder_id)
        .order_by(Tenant.name)
    )
    result = await session.execute(stmt)
    tenants = result.scalars().all()

    if not tenants:
        return []

    tenant_ids = [t.id for t in tenants]
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    # Batch query: last_trace_at per tenant
    last_trace_stmt = (
        select(
            Event.tenant_id,
            func.max(Event.created_at).label("last_trace_at"),
        )
        .where(Event.tenant_id.in_(tenant_ids))
        .group_by(Event.tenant_id)
    )
    last_trace_result = await session.execute(last_trace_stmt)
    last_trace_map = {row.tenant_id: row.last_trace_at for row in last_trace_result}

    # Batch query: trace_count_7d per tenant
    trace_7d_stmt = (
        select(
            Event.tenant_id,
            func.count(Event.id).label("count"),
        )
        .where(
            Event.tenant_id.in_(tenant_ids),
            Event.created_at >= seven_days_ago,
        )
        .group_by(Event.tenant_id)
    )
    trace_7d_result = await session.execute(trace_7d_stmt)
    trace_7d_map = {row.tenant_id: row.count for row in trace_7d_result}

    # Batch query: safety_flags_7d per tenant
    safety_stmt = (
        select(
            Event.tenant_id,
            func.count(Event.id).label("count"),
        )
        .where(
            Event.tenant_id.in_(tenant_ids),
            Event.created_at >= seven_days_ago,
            cast(Event.event_metadata["safety_concern"].as_string(), String) == "true",
        )
        .group_by(Event.tenant_id)
    )
    safety_result = await session.execute(safety_stmt)
    safety_map = {row.tenant_id: row.count for row in safety_result}

    # Batch query: handoff_count_7d per tenant
    handoff_stmt = (
        select(
            Event.tenant_id,
            func.count(Event.id).label("count"),
        )
        .where(
            Event.tenant_id.in_(tenant_ids),
            Event.created_at >= seven_days_ago,
            Event.event_type == "handoff.created",
        )
        .group_by(Event.tenant_id)
    )
    handoff_result = await session.execute(handoff_stmt)
    handoff_map = {row.tenant_id: row.count for row in handoff_result}

    # Batch query: conversational message.in events per authorized tenant
    outside_hours_stmt = select(
        Event.tenant_id,
        Event.created_at,
        Event.event_data,
        Event.event_metadata,
    ).where(
        Event.tenant_id.in_(tenant_ids),
        Event.event_type == "message.in",
        Event.created_at >= seven_days_ago,
    )
    outside_hours_result = await session.execute(outside_hours_stmt)
    conversational_created_ats: dict[UUID, list[datetime]] = {
        tenant_id: [] for tenant_id in tenant_ids
    }
    for row in outside_hours_result:
        if _is_conversational_trace(row.event_data, row.event_metadata):
            conversational_created_ats[row.tenant_id].append(row.created_at)

    clinics = []
    for t in tenants:
        last_trace = last_trace_map.get(t.id)
        health = _compute_health_status(last_trace, t.is_active)
        completeness = _compute_config_completeness(t.clinic_config_row)
        operations = (
            t.clinic_config_row.operations if t.clinic_config_row is not None else None
        )
        outside_hours = summarize_outside_hours(
            conversational_created_ats[t.id],
            operations,
        )

        clinics.append(
            BuilderClinicResponse(
                id=t.id,
                name=t.name,
                slug=t.slug,
                status="active" if t.is_active else "inactive",
                plan_type=t.plan_type,
                is_active=t.is_active,
                created_at=t.created_at,
                last_trace_at=last_trace,
                health_status=health,
                trace_count_7d=trace_7d_map.get(t.id, 0),
                safety_flags_7d=safety_map.get(t.id, 0),
                handoff_count_7d=handoff_map.get(t.id, 0),
                outside_hours_count_7d=outside_hours["outside_hours_count"],
                outside_hours_pct_7d=outside_hours["outside_hours_pct"],
                conversational_trace_count_7d=len(conversational_created_ats[t.id]),
                business_hours_configured=outside_hours["business_hours_configured"],
                config_completeness=completeness,
            ).model_dump(mode="json")
        )
    return clinics


# ============================================
# POST /clinics - Create clinic under builder
# ============================================


@router.post(
    "/clinics",
    summary="Create Clinic",
    description="Create a new clinic (tenant + user + config) linked to the authenticated builder.",
    status_code=status.HTTP_201_CREATED,
)
async def create_clinic(
    body: CreateBuilderClinicRequest,
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    # Check email uniqueness
    existing = await session.execute(
        select(User).where(User.email == body.admin_email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already in use",
        )

    # Create tenant linked to this builder
    tenant_id = uuid4()
    slug = body.clinic_name.lower().replace(" ", "-")[:100]
    new_tenant = Tenant(
        id=tenant_id,
        name=body.clinic_name,
        slug=slug,
        api_key_hash=pwd_context.hash(f"ns_auto_{uuid4().hex[:16]}"),
        is_active=True,
        plan_type="clinic",
        builder_id=builder_id,
        config={},
    )
    session.add(new_tenant)

    # Create admin user for the clinic
    new_user = User(
        id=uuid4(),
        tenant_id=tenant_id,
        email=body.admin_email,
        password_hash=pwd_context.hash(body.admin_password),
        role=UserRole.clinic_admin,
        is_active=True,
    )
    session.add(new_user)

    # Create empty clinic config
    new_config = ClinicConfigTable(
        id=uuid4(),
        tenant_id=tenant_id,
        version=1,
        persona={},
        scheduling={},
        pricing={},
        post_care={},
        handoff={},
        identity={},
        operations={},
        business_rules={},
        lgpd={},
        professionals=[],
        services=[],
        api_credentials={},
    )
    session.add(new_config)

    await session.flush()

    return {"ok": True, "id": str(tenant_id)}


# ============================================
# PATCH /clinics/{id}/status - Toggle clinic status
# ============================================


@router.patch(
    "/clinics/{clinic_id}/status",
    summary="Toggle Clinic Status",
    description="Toggle a clinic's active/inactive status. Only works for clinics owned by this builder.",
)
async def toggle_clinic_status(
    clinic_id: str,
    body: ToggleClinicStatusRequest,
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    try:
        cid = _UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    # SQL-level scope isolation: WHERE id = :clinic_id AND builder_id = :jwt_builder_id
    result = await session.execute(
        select(Tenant).where(
            Tenant.id == cid,
            Tenant.builder_id == builder_id,
        )
    )
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clinic not found",
        )

    tenant.is_active = body.is_active
    await session.flush()

    return {"ok": True, "is_active": tenant.is_active}


# ============================================
# GET /stats - Builder-scoped statistics
# ============================================


@router.get(
    "/stats",
    summary="Builder Stats",
    description="Aggregated statistics scoped to the authenticated builder.",
    response_model=BuilderStatsResponse,
)
async def builder_stats(
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    # Total clinics for this builder
    total_result = await session.execute(
        select(func.count(Tenant.id)).where(Tenant.builder_id == builder_id)
    )
    clinic_count = total_result.scalar() or 0

    # Active clinics
    active_result = await session.execute(
        select(func.count(Tenant.id)).where(
            Tenant.builder_id == builder_id,
            Tenant.is_active.is_(True),
        )
    )
    active_clinics = active_result.scalar() or 0

    # Inactive clinics
    inactive_clinics = clinic_count - active_clinics

    # Total traces across builder's clinics
    tenant_ids_stmt = select(Tenant.id).where(Tenant.builder_id == builder_id)
    trace_result = await session.execute(
        select(func.count(Event.id)).where(Event.tenant_id.in_(tenant_ids_stmt))
    )
    total_traces = trace_result.scalar() or 0

    return BuilderStatsResponse(
        clinic_count=clinic_count,
        active_clinics=active_clinics,
        inactive_clinics=inactive_clinics,
        total_traces=total_traces,
    )


# ============================================
# GET /dashboard - Builder Control Tower
# ============================================


async def _get_builder_stats(
    builder_id: UUID, session: AsyncSession
) -> BuilderStatsResponse:
    """Reusable stats computation."""
    total_result = await session.execute(
        select(func.count(Tenant.id)).where(Tenant.builder_id == builder_id)
    )
    clinic_count = total_result.scalar() or 0

    active_result = await session.execute(
        select(func.count(Tenant.id)).where(
            Tenant.builder_id == builder_id,
            Tenant.is_active.is_(True),
        )
    )
    active_clinics = active_result.scalar() or 0

    tenant_ids_stmt = select(Tenant.id).where(Tenant.builder_id == builder_id)
    trace_result = await session.execute(
        select(func.count(Event.id)).where(Event.tenant_id.in_(tenant_ids_stmt))
    )
    total_traces = trace_result.scalar() or 0

    # Aggregate LLM token usage across all managed clinics
    events_result = await session.execute(
        select(Event.event_metadata).where(
            Event.tenant_id.in_(tenant_ids_stmt),
            Event.event_type == "message.in",
        )
    )
    total_tokens = 0
    model_tokens: dict[str, int] = {}
    for (metadata,) in events_result.all():
        if not metadata:
            continue
        llm_usage = metadata.get("llm_usage", {})
        llm_model = metadata.get("llm_model", "")
        tokens = llm_usage.get("total_tokens", 0) if llm_usage else 0
        if tokens:
            total_tokens += tokens
            if llm_model:
                model_tokens[llm_model] = model_tokens.get(llm_model, 0) + tokens
        # Track audio transcriptions (whisper) — use audio_seconds as the metric unit
        whisper_model = metadata.get("whisper_model")
        if whisper_model and metadata.get("is_audio"):
            audio_sec = int(metadata.get("audio_seconds") or 0)
            model_tokens[f"🎙️ {whisper_model}"] = model_tokens.get(f"🎙️ {whisper_model}", 0) + max(audio_sec, 1)

    model_breakdown = [
        {"model": model, "tokens": toks}
        for model, toks in sorted(model_tokens.items(), key=lambda x: x[1], reverse=True)
    ]

    return BuilderStatsResponse(
        clinic_count=clinic_count,
        active_clinics=active_clinics,
        inactive_clinics=clinic_count - active_clinics,
        total_traces=total_traces,
        total_tokens=total_tokens,
        model_breakdown=model_breakdown,
    )


@router.get(
    "/dashboard",
    summary="Builder Dashboard",
    description="Combined dashboard: stats + 30-day time-series + computed alerts.",
    response_model=BuilderDashboardResponse,
)
async def builder_dashboard(
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    # Get tenant IDs for time-series
    tenant_ids_result = await session.execute(
        select(Tenant.id).where(Tenant.builder_id == builder_id)
    )
    tenant_ids = [row[0] for row in tenant_ids_result.all()]

    stats = await _get_builder_stats(builder_id, session)
    time_series = await _get_time_series(tenant_ids, session)
    alerts = await _compute_alerts(builder_id, session)

    return BuilderDashboardResponse(
        stats=stats,
        time_series=time_series,
        alerts=alerts,
    )
