"""
NeuroStack API - Admin Endpoints
Super-admin-only routes for cross-tenant management (clinics + builders).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.types import String

from src.api.helpers.health import (
    _compute_alerts,
    _compute_config_completeness,
    _compute_health_status,
    _get_time_series,
)
from src.api.middleware.auth import TenantContext, require_super_admin
from src.api.schemas.admin import (
    AdminAlertResponse,
    AdminDashboardResponse,
    AdminDashboardStatsResponse,
    AdminStatsResponse,
    AdminTimeSeriesPoint,
    BuilderClinicResponse,
    BuilderWithClinicsResponse,
    ClinicSlotResponse,
    ClinicUserResponse,
    CreateBuilderClinicRequest,
    CreateBuilderRequest,
    CreateClinicRequest,
    UpdateBuilderRequest,
    UpdateClinicRequest,
)
from src.api.schemas.billing import CouponResponse, CreateCouponRequest
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.coupon import Coupon
from src.models.event import Event
from src.models.tenant import Tenant
from src.models.transaction import Transaction
from src.models.user import User, UserRole
from src.repositories import get_db_session_dependency

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


# ============================================
# Helper: build a ClinicSlotResponse from DB data
# ============================================


def _build_clinic_slot(
    tenant: Tenant,
    users: list[User],
    trace_count: int,
    apis_configured: bool,
    health_status: str = None,
    last_trace_at: datetime = None,
    config_completeness: int = 0,
) -> dict:
    identity = {}
    api_creds = {}
    if tenant.clinic_config_row:
        identity = tenant.clinic_config_row.identity or {}
        api_creds = tenant.clinic_config_row.api_credentials or {}

    return ClinicSlotResponse(
        clinic_id=tenant.id,
        clinic_name=tenant.name,
        agent_name=identity.get("agent_name"),
        slug=tenant.slug,
        status="active" if tenant.is_active else "inactive",
        plan_type=tenant.plan_type,
        trace_count=trace_count,
        apis_configured=bool(api_creds.get("zapi_instance_id")),
        zapi_instance_id=api_creds.get("zapi_instance_id"),
        users=[
            ClinicUserResponse(id=u.id, email=u.email, role=u.role.value)
            for u in users
        ],
        health_status=health_status,
        last_trace_at=last_trace_at,
        config_completeness=config_completeness,
    ).model_dump(mode="json")


# ============================================
# Clinics Endpoints (Group 3)
# ============================================


@router.get(
    "/clinics",
    summary="List All Clinics",
    description="Return all tenants with users, config, and trace counts. Super admin only.",
)
async def list_clinics(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    # Load ALL tenants (including deactivated) with related config
    stmt = (
        select(Tenant)
        .options(selectinload(Tenant.clinic_config_row))
        .order_by(Tenant.name)
    )
    result = await session.execute(stmt)
    tenants = result.scalars().all()

    # Batch-load users for all tenants
    tenant_ids = [t.id for t in tenants]

    users_stmt = select(User).where(User.tenant_id.in_(tenant_ids))
    users_result = await session.execute(users_stmt)
    all_users = users_result.scalars().all()
    users_by_tenant: dict = {}
    for u in all_users:
        users_by_tenant.setdefault(u.tenant_id, []).append(u)

    # Batch-load trace counts
    trace_stmt = (
        select(Event.tenant_id, func.count(Event.id))
        .where(Event.tenant_id.in_(tenant_ids))
        .group_by(Event.tenant_id)
    )
    trace_result = await session.execute(trace_stmt)
    trace_counts = {row[0]: row[1] for row in trace_result.all()}

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

    # Roles that indicate non-clinic tenants (builder HQ, super-admin, investor)
    non_clinic_roles = {UserRole.builder, UserRole.super_admin, UserRole.investor}

    clinics = []
    for t in tenants:
        tenant_users = users_by_tenant.get(t.id, [])
        # Skip tenants where ALL users have non-clinic roles (e.g. builder HQ tenant)
        if tenant_users and all(u.role in non_clinic_roles for u in tenant_users):
            continue

        last_trace = last_trace_map.get(t.id)
        health = _compute_health_status(last_trace, t.is_active)
        completeness = _compute_config_completeness(t.clinic_config_row)

        clinics.append(
            _build_clinic_slot(
                t,
                tenant_users,
                trace_counts.get(t.id, 0),
                False,
                health_status=health,
                last_trace_at=last_trace,
                config_completeness=completeness,
            )
        )
    return clinics


@router.post(
    "/clinics",
    summary="Create Clinic",
    description="Create a new tenant + admin user + empty clinic config. Super admin only.",
    status_code=status.HTTP_201_CREATED,
)
async def create_clinic(
    body: CreateClinicRequest,
    tenant: TenantContext = Depends(require_super_admin),
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

    # Create tenant
    tenant_id = uuid4()
    slug = body.clinic_name.lower().replace(" ", "-")[:100]
    new_tenant = Tenant(
        id=tenant_id,
        name=body.clinic_name,
        slug=slug,
        api_key_hash=pwd_context.hash(f"ns_auto_{uuid4().hex[:16]}"),
        is_active=True,
        plan_type=body.plan_type,
        builder_id=body.builder_id,
        config={},
    )
    session.add(new_tenant)

    # Create admin user
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

    await session.commit()

    return {"ok": True, "id": str(tenant_id)}


@router.delete(
    "/clinics/{clinic_id}",
    summary="Delete Clinic",
    description="Hard-delete a tenant and all associated data (users, sessions, events). Super admin only.",
)
async def delete_clinic(
    clinic_id: str,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    try:
        cid = _UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    result = await session.execute(select(Tenant).where(Tenant.id == cid))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Clinic not found")

    await session.delete(t)
    await session.commit()
    return {"ok": True}


@router.patch(
    "/clinics/{clinic_id}/status",
    summary="Toggle Clinic Status",
    description="Activate or deactivate a clinic. Super admin only.",
)
async def toggle_clinic_status(
    clinic_id: str,
    body: dict,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    new_status = body.get("status")
    if new_status not in ("active", "inactive"):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'inactive'")

    try:
        cid = _UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    result = await session.execute(select(Tenant).where(Tenant.id == cid))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Clinic not found")

    t.is_active = (new_status == "active")
    await session.commit()
    return {"ok": True}


@router.patch(
    "/clinics/{clinic_id}",
    summary="Update Clinic",
    description="Update clinic name, admin email, and/or password. Super admin only.",
)
async def update_clinic(
    clinic_id: str,
    body: UpdateClinicRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    try:
        cid = _UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    result = await session.execute(
        select(Tenant)
        .options(selectinload(Tenant.clinic_config_row))
        .where(Tenant.id == cid)
    )
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Clinic not found")

    if body.clinic_name is not None:
        t.name = body.clinic_name
        # Sync clinic_name into the identity JSONB bucket so the frontend
        # header (which reads clinicConfig.clinic_name) stays in sync.
        if t.clinic_config_row:
            identity = dict(t.clinic_config_row.identity or {})
            identity["clinic_name"] = body.clinic_name
            t.clinic_config_row.identity = identity

    if body.email is not None or body.password is not None:
        user_result = await session.execute(
            select(User).where(User.tenant_id == cid, cast(User.role, String) == "clinic_admin")
        )
        user = user_result.scalar_one_or_none()
        if user:
            if body.email is not None and body.email != user.email:
                existing = await session.execute(select(User).where(User.email == body.email))
                if existing.scalar_one_or_none():
                    raise HTTPException(status_code=409, detail=f"O email {body.email} já está em uso.")
                user.email = body.email
            if body.password is not None:
                user.password_hash = pwd_context.hash(body.password)

    await session.commit()
    return {"ok": True}


@router.get(
    "/stats",
    summary="Admin Dashboard Stats",
    description="Aggregated statistics for the admin dashboard. Super admin only.",
    response_model=AdminStatsResponse,
)
async def admin_stats(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    # Total traces (events)
    trace_count_result = await session.execute(select(func.count(Event.id)))
    total_traces = trace_count_result.scalar() or 0

    # Active pilots
    pilot_result = await session.execute(
        select(func.count(Tenant.id)).where(
            Tenant.is_active.is_(True),
            Tenant.plan_type.in_(["pilot", "clinic"]),
        )
    )
    active_pilots = pilot_result.scalar() or 0

    # Active builders
    builder_result = await session.execute(
        select(func.count(Builder.id)).where(Builder.is_active.is_(True))
    )
    active_builders = builder_result.scalar() or 0

    # Active managed clinics (have a builder_id)
    managed_result = await session.execute(
        select(func.count(Tenant.id)).where(
            Tenant.is_active.is_(True),
            Tenant.builder_id.isnot(None),
        )
    )
    active_managed = managed_result.scalar() or 0

    return AdminStatsResponse(
        total_traces=total_traces,
        total_tokens=0,  # stub — token tracking not yet implemented
        open_tickets=0,  # stub — ticket system not yet implemented
        active_pilots=active_pilots,
        active_builders=active_builders,
        active_managed_clinics=active_managed,
        estimated_mrr=0.0,  # stub — billing not yet implemented
    )


# ============================================
# GET /dashboard - Admin Control Tower
# ============================================


@router.get(
    "/dashboard",
    summary="Admin Dashboard",
    description="Combined admin dashboard: platform-wide stats + 30-day time-series + alerts.",
    response_model=AdminDashboardResponse,
)
async def admin_dashboard(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    # Total traces
    trace_count_result = await session.execute(select(func.count(Event.id)))
    total_traces = trace_count_result.scalar() or 0

    # Active pilots
    pilot_result = await session.execute(
        select(func.count(Tenant.id)).where(
            Tenant.is_active.is_(True),
            Tenant.plan_type.in_(["pilot", "clinic"]),
        )
    )
    active_pilots = pilot_result.scalar() or 0

    # Active builders
    builder_result = await session.execute(
        select(func.count(Builder.id)).where(Builder.is_active.is_(True))
    )
    active_builders = builder_result.scalar() or 0

    # Active managed clinics
    managed_result = await session.execute(
        select(func.count(Tenant.id)).where(
            Tenant.is_active.is_(True),
            Tenant.builder_id.isnot(None),
        )
    )
    active_managed = managed_result.scalar() or 0

    # Safety flags 7d (platform-wide)
    safety_result = await session.execute(
        select(func.count(Event.id)).where(
            Event.created_at >= seven_days_ago,
            cast(Event.event_metadata["safety_concern"].as_string(), String) == "true",
        )
    )
    safety_flags_7d = safety_result.scalar() or 0

    # Handoff count 7d (platform-wide)
    handoff_result = await session.execute(
        select(func.count(Event.id)).where(
            Event.created_at >= seven_days_ago,
            Event.event_type == "handoff.created",
        )
    )
    handoff_count_7d = handoff_result.scalar() or 0

    stats = AdminDashboardStatsResponse(
        total_traces=total_traces,
        total_tokens=0,
        open_tickets=0,
        active_pilots=active_pilots,
        active_builders=active_builders,
        active_managed_clinics=active_managed,
        estimated_mrr=0.0,
        safety_flags_7d=safety_flags_7d,
        handoff_count_7d=handoff_count_7d,
    )

    # Platform-wide time-series (all tenants)
    all_tenant_ids_result = await session.execute(select(Tenant.id))
    all_tenant_ids = [row[0] for row in all_tenant_ids_result.all()]
    time_series_raw = await _get_time_series(all_tenant_ids, session, days=30)
    time_series = [
        AdminTimeSeriesPoint(date=p.date, count=p.count) for p in time_series_raw
    ]

    # Platform-wide alerts (builder_id=None means all clinics)
    alerts_raw = await _compute_alerts(builder_id=None, session=session)
    alerts = [
        AdminAlertResponse(
            type=a.type,
            severity=a.severity,
            clinic_id=a.clinic_id,
            clinic_name=a.clinic_name,
            message=a.message,
            detected_at=a.detected_at,
        )
        for a in alerts_raw
    ]

    return AdminDashboardResponse(
        stats=stats,
        time_series=time_series,
        alerts=alerts,
    )


# ============================================
# Stubs for frontend compatibility (Group 3)
# ============================================


@router.get(
    "/circuit-breakers",
    summary="List Circuit Breakers (stub)",
    description="Stub endpoint for circuit breaker monitoring. Returns empty list.",
)
async def list_circuit_breakers(
    tenant: TenantContext = Depends(require_super_admin),
):
    return []


@router.get(
    "/rlhf/pending",
    summary="Pending RLHF Reviews",
    description=(
        "List RLHF feedback suggestions submitted from the chat simulator. "
        "Super admin only. Optional filter by builder_id."
    ),
)
async def pending_rlhf(
    builder_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    stmt = select(Event).where(Event.event_type == "rlhf.feedback")

    if builder_id:
        try:
            builder_uuid = _UUID(builder_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid builder_id",
            )
        stmt = stmt.join(Tenant, Tenant.id == Event.tenant_id).where(
            Tenant.builder_id == builder_uuid
        )

    stmt = stmt.order_by(Event.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    events = result.scalars().all()

    entries = []
    for e in events:
        data = e.event_data or {}
        entries.append(
            {
                "id": str(e.id),
                "ts": e.created_at.isoformat() if e.created_at else "",
                "tenant_id": str(e.tenant_id),
                "submitted_by": data.get("submitted_by"),
                "user_message": data.get("user_message"),
                "agent_response": data.get("agent_response"),
                "suggested_response": data.get("suggested_response"),
                "status": data.get("status"),
                "intent": data.get("intent"),
            }
        )

    return entries


# ============================================
# Builders Endpoints (Group 4)
# ============================================


@router.get(
    "/builders",
    summary="List Builders",
    description="List all builders with linked clinics. Super admin only.",
)
async def list_builders(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    stmt = select(Builder).where(Builder.is_active.is_(True)).order_by(Builder.name)
    result = await session.execute(stmt)
    builders = result.scalars().all()

    builder_ids = [b.id for b in builders]

    # Load linked tenants
    tenants_stmt = (
        select(Tenant)
        .options(selectinload(Tenant.clinic_config_row))
        .where(
            Tenant.builder_id.in_(builder_ids),
            Tenant.is_active.is_(True),
            Tenant.plan_type != "admin",
        )
    )
    tenants_result = await session.execute(tenants_stmt)
    all_tenants = tenants_result.scalars().all()
    tenants_by_builder: dict = {}
    for t in all_tenants:
        tenants_by_builder.setdefault(t.builder_id, []).append(t)

    # Trace counts for builder clinics
    tenant_ids = [t.id for t in all_tenants]
    trace_counts: dict = {}
    last_trace_map: dict = {}
    if tenant_ids:
        trace_stmt = (
            select(Event.tenant_id, func.count(Event.id))
            .where(Event.tenant_id.in_(tenant_ids))
            .group_by(Event.tenant_id)
        )
        trace_result = await session.execute(trace_stmt)
        trace_counts = {row[0]: row[1] for row in trace_result.all()}

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
        last_trace_map = {
            row.tenant_id: row.last_trace_at for row in last_trace_result
        }

    # Load admin emails: builder users + clinic admin users
    all_relevant_tenant_ids = tenant_ids + [
        u_tid for u_tid in []  # placeholder
    ]
    # Builder user emails (keyed by builder_id)
    builder_user_stmt = select(User.builder_id, User.email).where(
        User.builder_id.in_(builder_ids), cast(User.role, String) == "builder"
    )
    builder_user_result = await session.execute(builder_user_stmt)
    builder_email_map = {row[0]: row[1] for row in builder_user_result.all()}

    # Clinic admin emails (keyed by tenant_id)
    if tenant_ids:
        clinic_user_stmt = select(User.tenant_id, User.email).where(
            User.tenant_id.in_(tenant_ids), cast(User.role, String) == "clinic_admin"
        )
        clinic_user_result = await session.execute(clinic_user_stmt)
        clinic_email_map = {row[0]: row[1] for row in clinic_user_result.all()}
    else:
        clinic_email_map = {}

    output = []
    for b in builders:
        clinics = []
        for t in tenants_by_builder.get(b.id, []):
            api_creds = {}
            if t.clinic_config_row:
                api_creds = t.clinic_config_row.api_credentials or {}
            last_trace = last_trace_map.get(t.id)
            health = _compute_health_status(last_trace, t.is_active)
            completeness = _compute_config_completeness(t.clinic_config_row)
            clinics.append(
                BuilderClinicResponse(
                    clinic_id=t.id,
                    clinic_name=t.name,
                    admin_email=clinic_email_map.get(t.id),
                    slug=t.slug,
                    status="active" if t.is_active else "inactive",
                    plan_type=t.plan_type,
                    trace_count=trace_counts.get(t.id, 0),
                    apis_configured=bool(api_creds.get("zapi_instance_id")),
                    health_status=health,
                    last_trace_at=last_trace,
                    config_completeness=completeness,
                ).model_dump(mode="json")
            )
        output.append(
            BuilderWithClinicsResponse(
                id=b.id,
                name=b.name,
                admin_email=builder_email_map.get(b.id),
                status=b.status,
                created_at=b.created_at,
                clinics=clinics,
            ).model_dump(mode="json")
        )

    return output


@router.post(
    "/builders",
    summary="Create Builder",
    description="Create a new builder record. Super admin only.",
    status_code=status.HTTP_201_CREATED,
)
async def create_builder(
    body: CreateBuilderRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    # Check email uniqueness
    existing = await session.execute(
        select(User).where(User.email == body.admin_email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"O email {body.admin_email} já está em uso. Use um email diferente.",
        )

    builder_id = uuid4()
    new_builder = Builder(
        id=builder_id,
        name=body.name,
        status="active",
        is_active=True,
    )
    session.add(new_builder)

    # Create tenant for the builder
    tenant_id = uuid4()
    new_tenant = Tenant(
        id=tenant_id,
        name=f"{body.name} (Builder)",
        slug=body.name.lower().replace(" ", "-")[:100],
        api_key_hash=pwd_context.hash(f"ns_auto_{uuid4().hex[:16]}"),
        is_active=True,
        plan_type="admin",
        config={},
    )
    session.add(new_tenant)

    # Create builder admin user
    new_user = User(
        id=uuid4(),
        tenant_id=tenant_id,
        email=body.admin_email,
        password_hash=pwd_context.hash(body.admin_password),
        role=UserRole.builder,
        builder_id=builder_id,
        is_active=True,
    )
    session.add(new_user)

    await session.commit()
    return {"ok": True, "id": str(builder_id)}


@router.get(
    "/builders/subscriptions",
    summary="Builder Subscription Overview",
    description="Subscription status for all builders. Super admin only.",
)
async def builder_subscriptions(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    stmt = select(Builder).order_by(Builder.name)
    result = await session.execute(stmt)
    builders = result.scalars().all()
    return [
        {
            "id": str(b.id),
            "name": b.name,
            "subscription_plan": b.subscription_plan,
            "subscription_status": b.subscription_status,
            "billing_cycle": b.billing_cycle,
            "updated_at": b.updated_at.isoformat() if b.updated_at else None,
        }
        for b in builders
    ]


@router.get(
    "/transactions",
    summary="List Payment Transactions",
    description="List all confirmed payment transactions. Super admin only.",
)
async def list_transactions(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    stmt = select(Transaction).order_by(Transaction.created_at.desc()).limit(200)
    result = await session.execute(stmt)
    txs = result.scalars().all()
    return [
        {
            "id": str(tx.id),
            "builder_id": str(tx.builder_id),
            "builder_name": tx.builder_name,
            "amount_cents": tx.amount_cents,
            "method": tx.method,
            "cycle": tx.cycle,
            "plan": tx.plan,
            "coupon_code": tx.coupon_code,
            "external_id": tx.external_id,
            "event_type": tx.event_type,
            "created_at": tx.created_at.isoformat() if tx.created_at else None,
        }
        for tx in txs
    ]


@router.patch(
    "/builders/{builder_id}",
    summary="Update Builder",
    description="Update builder name and/or status. Super admin only.",
)
async def update_builder(
    builder_id: str,
    body: UpdateBuilderRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    try:
        bid = _UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    result = await session.execute(select(Builder).where(Builder.id == bid))
    builder = result.scalar_one_or_none()
    if not builder:
        raise HTTPException(status_code=404, detail="Builder not found")

    if body.name is not None:
        builder.name = body.name
    if body.status is not None:
        builder.status = body.status

    # Update builder user email/password
    if body.email is not None or body.password is not None:
        user_result = await session.execute(
            select(User).where(User.builder_id == bid, cast(User.role, String) == "builder")
        )
        user = user_result.scalar_one_or_none()
        if user:
            if body.email is not None and body.email != user.email:
                # Check email uniqueness
                existing = await session.execute(select(User).where(User.email == body.email))
                if existing.scalar_one_or_none():
                    raise HTTPException(status_code=409, detail=f"O email {body.email} já está em uso.")
                user.email = body.email
            if body.password is not None:
                user.password_hash = pwd_context.hash(body.password)

    await session.commit()
    return {"ok": True}


@router.delete(
    "/builders/{builder_id}",
    summary="Delete Builder",
    description="Hard-delete builder, its tenant, and associated users. Unlinks managed clinics first. Super admin only.",
)
async def delete_builder(
    builder_id: str,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    try:
        bid = _UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    result = await session.execute(select(Builder).where(Builder.id == bid))
    builder = result.scalar_one_or_none()
    if not builder:
        raise HTTPException(status_code=404, detail="Builder not found")

    # Unlink managed clinics (set builder_id = NULL so they aren't orphaned)
    linked_stmt = select(Tenant).where(Tenant.builder_id == bid)
    linked_result = await session.execute(linked_stmt)
    for t in linked_result.scalars().all():
        t.builder_id = None

    # Delete the builder's own tenant (cascade deletes users, sessions, events)
    builder_tenant_stmt = select(Tenant).join(User, User.tenant_id == Tenant.id).where(User.builder_id == bid)
    builder_tenant_result = await session.execute(builder_tenant_stmt)
    builder_tenant = builder_tenant_result.scalar_one_or_none()
    if builder_tenant:
        await session.delete(builder_tenant)

    # Delete the builder record itself
    await session.delete(builder)
    await session.commit()
    return {"ok": True}


@router.post(
    "/builders/{builder_id}/clinics",
    summary="Create Clinic Under Builder",
    description="Create a new clinic (tenant + user + config) linked to a builder. Super admin only.",
    status_code=status.HTTP_201_CREATED,
)
async def create_builder_clinic(
    builder_id: str,
    body: CreateBuilderClinicRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    try:
        bid = _UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    # Verify builder exists
    builder_result = await session.execute(select(Builder).where(Builder.id == bid))
    if not builder_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Builder not found")

    # Check email uniqueness
    existing = await session.execute(
        select(User).where(User.email == body.admin_email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"O email {body.admin_email} já está em uso. Cada clínica precisa de um email diferente.",
        )

    # Create tenant linked to builder
    tenant_id = uuid4()
    slug = body.clinic_name.lower().replace(" ", "-")[:100]
    new_tenant = Tenant(
        id=tenant_id,
        name=body.clinic_name,
        slug=slug,
        api_key_hash=pwd_context.hash(f"ns_auto_{uuid4().hex[:16]}"),
        is_active=True,
        plan_type="clinic",
        builder_id=bid,
        config={},
    )
    session.add(new_tenant)

    # Create admin user
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

    await session.commit()

    return {"ok": True, "id": str(tenant_id)}


@router.post(
    "/builders/{builder_id}/link-clinic",
    summary="Link Existing Clinic to Builder",
    description="Link an existing unlinked clinic to a builder. Super admin only.",
)
async def link_clinic_to_builder(
    builder_id: str,
    body: dict,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    try:
        bid = _UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    clinic_id = body.get("clinic_id")
    if not clinic_id:
        raise HTTPException(status_code=400, detail="clinic_id is required")

    try:
        cid = _UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    builder_result = await session.execute(select(Builder).where(Builder.id == bid))
    if not builder_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Builder not found")

    clinic_result = await session.execute(select(Tenant).where(Tenant.id == cid))
    clinic = clinic_result.scalar_one_or_none()
    if not clinic:
        raise HTTPException(status_code=404, detail="Clínica não encontrada")

    if clinic.builder_id is not None:
        raise HTTPException(status_code=409, detail="Esta clínica já está vinculada a um builder")

    clinic.builder_id = bid
    await session.commit()
    return {"ok": True}


@router.post(
    "/builders/{builder_id}/unlink-clinic",
    summary="Unlink Clinic from Builder",
    description="Unlink a clinic currently linked to this builder. Super admin only.",
)
async def unlink_clinic_from_builder(
    builder_id: str,
    body: dict,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    try:
        bid = _UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    clinic_id = body.get("clinic_id")
    if not clinic_id:
        raise HTTPException(status_code=400, detail="clinic_id is required")

    try:
        cid = _UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    clinic_result = await session.execute(select(Tenant).where(Tenant.id == cid))
    clinic = clinic_result.scalar_one_or_none()
    if not clinic:
        raise HTTPException(status_code=404, detail="Clínica não encontrada")

    if clinic.builder_id is None:
        raise HTTPException(
            status_code=409, detail="Esta clínica não está vinculada a nenhum builder"
        )
    if clinic.builder_id != bid:
        raise HTTPException(
            status_code=409, detail="Esta clínica está vinculada a outro builder"
        )

    clinic.builder_id = None
    await session.commit()
    return {"ok": True}


# ============================================
# Coupons Endpoints
# ============================================


@router.post(
    "/coupons",
    summary="Create Coupon",
    description="Create a new discount coupon. Super admin only.",
    status_code=status.HTTP_201_CREATED,
)
async def create_coupon(
    body: CreateCouponRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    code_upper = body.code.strip().upper()
    existing = await session.execute(
        select(Coupon).where(Coupon.code == code_upper)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cupom '{code_upper}' já existe",
        )

    coupon = Coupon(
        code=code_upper,
        discount_percent=body.discount_percent,
        max_uses=body.max_uses,
        expires_at=body.expires_at,
    )
    session.add(coupon)
    await session.commit()
    return CouponResponse(
        id=str(coupon.id),
        code=coupon.code,
        discount_percent=coupon.discount_percent,
        max_uses=coupon.max_uses,
        uses_count=coupon.uses_count,
        expires_at=coupon.expires_at,
        is_active=coupon.is_active,
        created_at=coupon.created_at,
    ).model_dump(mode="json")


@router.get(
    "/coupons",
    summary="List Coupons",
    description="List all coupons. Super admin only.",
)
async def list_coupons(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    result = await session.execute(
        select(Coupon).order_by(Coupon.created_at.desc())
    )
    coupons = result.scalars().all()
    return [
        CouponResponse(
            id=str(c.id),
            code=c.code,
            discount_percent=c.discount_percent,
            max_uses=c.max_uses,
            uses_count=c.uses_count,
            expires_at=c.expires_at,
            is_active=c.is_active,
            created_at=c.created_at,
        ).model_dump(mode="json")
        for c in coupons
    ]


@router.patch(
    "/coupons/{coupon_id}",
    summary="Deactivate Coupon",
    description="Deactivate an existing coupon. Super admin only.",
)
async def deactivate_coupon(
    coupon_id: str,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    from uuid import UUID as _UUID

    try:
        cid = _UUID(coupon_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid coupon ID")

    result = await session.execute(select(Coupon).where(Coupon.id == cid))
    coupon = result.scalar_one_or_none()
    if not coupon:
        raise HTTPException(status_code=404, detail="Cupom não encontrado")

    coupon.is_active = False
    await session.commit()
    return {"ok": True}
