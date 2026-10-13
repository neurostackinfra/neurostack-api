"""
NeuroStack API - Admin Endpoints
Super-admin-only routes for cross-tenant management (clinics + builders).
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.types import String

from src.api.helpers.health import (
    _compute_alerts,
    _compute_config_completeness,
    _compute_health_status,
    _get_time_series,
)
from src.api.middleware.auth import (
    TenantContext,
    require_super_admin,
    require_super_admin_jwt,
)
from src.api.schemas.admin import (
    AdminAlertResponse,
    AdminDashboardResponse,
    AdminDashboardStatsResponse,
    AdminStatsResponse,
    AdminTimeSeriesPoint,
    BillingCheckoutReservationResponse,
    BillingMigrationAbsenceReadRequest,
    BillingMigrationAbsenceReadResponse,
    BillingMigrationCancelBeginRequest,
    BillingMigrationCancelReconcileRequest,
    BillingMigrationProviderFlipRequest,
    BillingMigrationRecoverCheckoutRequest,
    BillingMigrationReserveAttemptRequest,
    BillingMigrationResponse,
    BillingMigrationRosterRequest,
    BillingMigrationTransitionRequest,
    BillingOperationAssignmentRequest,
    BillingOperationAssignmentResponse,
    BillingOperationMaterialRequest,
    BillingOperationReceiptRequest,
    BillingOperationReceiptResponse,
    BillingProviderEvidenceRequest,
    BillingRolloutControlResponse,
    BillingRolloutReadinessReceiptRequest,
    BillingRolloutReadinessReceiptResponse,
    BillingRolloutTransitionRequest,
    BillingTopupCapabilityDisableRequest,
    BillingTopupCapabilityEnableRequest,
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
from src.config.settings import settings
from src.models.billing import BillingMigration, BillingSubscription
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.coupon import Coupon
from src.models.event import Event
from src.models.tenant import Tenant
from src.models.transaction import Transaction
from src.models.user import User, UserRole
from src.observability.metrics import (
    BILLING_MIGRATION_BACKLOG,
    BILLING_OPERATION_RECEIPTS_TOTAL,
)
from src.repositories import get_db_session_dependency
from src.repositories.billing import (
    BillingRepository,
    BillingRevisionConflict,
    BillingStateUnavailable,
    BillingTransitionRejected,
)
from src.services.billing_capability import (
    BillingCapabilityRejected,
    BillingTopupCapabilityService,
    TopupCapabilityEvidence,
)
from src.services.billing_migration import (
    BillingMigrationRejected,
    BillingMigrationService,
    MigrationTarget,
    MigrationView,
    ProviderEvidenceEnvelope,
)
from src.services.billing_operations import (
    BillingOperationAuthority,
    BillingOperationRejected,
    OperationMaterial,
)
from src.services.billing_rollout import BillingRolloutOperationService
from src.services.media_assets import (
    MediaOwnerTransition,
    MediaStorageCapacityExceeded,
    MediaStorageGuardError,
    detach_builder_media_owners_for_delete,
    lock_builder_users_before_parent,
    lock_clinic_for_delete,
    lock_tenant_users_before_parent,
    transition_tenant_media_owner,
)
from src.services.tenant_classification import (
    is_real_clinic_user_roles,
    real_clinic_tenant_filter,
)
from src.services.user_email import email_in_use

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
    health_status: str | None = None,
    last_trace_at: datetime | None = None,
    config_completeness: int = 0,
) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    if tenant.clinic_config_row:
        identity = tenant.clinic_config_row.identity or {}

    return ClinicSlotResponse(
        clinic_id=tenant.id,
        clinic_name=tenant.name,
        agent_name=identity.get("agent_name"),
        slug=tenant.slug,
        status="active" if tenant.is_active else "inactive",
        plan_type=tenant.plan_type,
        trace_count=trace_count,
        apis_configured=apis_configured,
        users=[
            ClinicUserResponse(id=u.id, email=u.email, role=u.role.value) for u in users
        ],
        health_status=health_status,
        last_trace_at=last_trace_at,
        config_completeness=config_completeness,
        audit_extended_access=tenant.audit_extended_access,
    ).model_dump(mode="json")


# ============================================
# Helper: platform-wide totals (KPI cards)
# ============================================

# Builder subscription states that count as "active" for billing purposes:
# still on a free pilot/trial, or paying. Anything else (e.g. "past_due",
# "canceled") is not active even if the row hasn't been manually deactivated.
_ACTIVE_BUILDER_SUBSCRIPTION_STATUSES = ("trial", "pilot", "active")


async def _compute_platform_totals(session: AsyncSession) -> dict[str, int]:
    """Shared clinic/builder totals for the admin stats + dashboard KPI cards."""
    clinic_filter = real_clinic_tenant_filter()

    total_clinics_result = await session.execute(
        select(func.count(Tenant.id)).where(clinic_filter)
    )
    total_clinics = total_clinics_result.scalar() or 0

    active_clinics_result = await session.execute(
        select(func.count(Tenant.id)).where(Tenant.is_active.is_(True), clinic_filter)
    )
    active_clinics = active_clinics_result.scalar() or 0

    total_builders_result = await session.execute(select(func.count(Builder.id)))
    total_builders = total_builders_result.scalar() or 0

    active_builders_result = await session.execute(
        select(func.count(Builder.id)).where(
            Builder.is_active.is_(True),
            Builder.status == "active",
            Builder.subscription_status.in_(_ACTIVE_BUILDER_SUBSCRIPTION_STATUSES),
        )
    )
    active_builders = active_builders_result.scalar() or 0

    return {
        "total_clinics": total_clinics,
        "active_clinics": active_clinics,
        "total_builders": total_builders,
        "active_builders": active_builders,
    }


# ============================================
# Clinics Endpoints (Group 3)
# ============================================


@router.get(
    "/clinics",
    summary="List All Clinics",
    description="Return all tenants with users, config, and trace counts. Super admin only.",
    response_model=None,
)
async def list_clinics(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[dict[str, Any]]:
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
    users_by_tenant: dict[UUID, list[User]] = {}
    for u in all_users:
        users_by_tenant.setdefault(u.tenant_id, []).append(u)

    # Batch-load trace counts
    trace_stmt = (
        select(Event.tenant_id, func.count(Event.id))
        .where(
            Event.tenant_id.in_(tenant_ids),
            Event.event_type == "message.in",
        )
        .group_by(Event.tenant_id)
    )
    trace_result = await session.execute(trace_stmt)
    trace_counts: dict[UUID, int] = {row[0]: row[1] for row in trace_result.all()}

    # Batch query: last_trace_at per tenant
    last_trace_stmt = (
        select(
            Event.tenant_id,
            func.max(Event.created_at).label("last_trace_at"),
        )
        .where(
            Event.tenant_id.in_(tenant_ids),
            Event.event_type == "message.in",
        )
        .group_by(Event.tenant_id)
    )
    last_trace_result = await session.execute(last_trace_stmt)
    last_trace_map: dict[UUID, datetime | None] = {
        row.tenant_id: row.last_trace_at for row in last_trace_result
    }

    clinics: list[dict[str, Any]] = []
    for t in tenants:
        tenant_users = users_by_tenant.get(t.id, [])
        # Skip tenants where ALL users have non-clinic roles (e.g. builder HQ tenant)
        if not is_real_clinic_user_roles(u.role for u in tenant_users):
            continue

        last_trace = last_trace_map.get(t.id)
        health = _compute_health_status(last_trace, t.is_active)
        completeness = _compute_config_completeness(t.clinic_config_row)
        api_creds = (
            (t.clinic_config_row.api_credentials or {}) if t.clinic_config_row else {}
        )

        clinics.append(
            _build_clinic_slot(
                t,
                tenant_users,
                trace_counts.get(t.id, 0),
                bool(api_creds.get("zapi_instance_id")),
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
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_clinic(
    body: CreateClinicRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    # Check email uniqueness
    if await email_in_use(session, str(body.admin_email)):
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
    response_model=None,
)
async def delete_clinic(
    clinic_id: str,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    try:
        cid = UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    try:
        t = await lock_clinic_for_delete(
            session,
            cid,
            actor_user_id=tenant.user_id,
        )
    except MediaStorageGuardError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not t:
        raise HTTPException(status_code=404, detail="Clinic not found")

    await session.delete(t)
    await session.commit()
    return {"ok": True}


@router.patch(
    "/clinics/{clinic_id}/status",
    summary="Toggle Clinic Status",
    description="Activate or deactivate a clinic. Super admin only.",
    response_model=None,
)
async def toggle_clinic_status(
    clinic_id: str,
    body: dict[str, Any],
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    new_status = body.get("status")
    if new_status not in ("active", "inactive"):
        raise HTTPException(
            status_code=400, detail="status must be 'active' or 'inactive'"
        )

    try:
        cid = UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    result = await session.execute(select(Tenant).where(Tenant.id == cid))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Clinic not found")

    t.is_active = new_status == "active"
    await session.commit()
    return {"ok": True}


@router.patch(
    "/clinics/{clinic_id}",
    summary="Update Clinic",
    description=(
        "Update clinic name, admin email, password, and/or the audit add-on "
        "(`audit_extended_access`, which waives the 6-month export lookback). "
        "Super admin only. Responds with the stored `audit_extended_access`."
    ),
    response_model=None,
)
async def update_clinic(
    clinic_id: str,
    body: UpdateClinicRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    try:
        cid = UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    if body.email is not None or body.password is not None:
        t = await lock_tenant_users_before_parent(session, cid)
        if t is not None:
            await session.refresh(t, attribute_names=["clinic_config_row"])
    else:
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
            select(User).where(
                User.tenant_id == cid, cast(User.role, String) == "clinic_admin"
            )
        )
        user = user_result.scalar_one_or_none()
        if user:
            if body.email is not None and body.email != user.email:
                if await email_in_use(
                    session, str(body.email), exclude_user_id=user.id
                ):
                    raise HTTPException(
                        status_code=409, detail=f"O email {body.email} já está em uso."
                    )
                user.email = body.email
            if body.password is not None:
                user.password_hash = pwd_context.hash(body.password)

    if body.audit_extended_access is not None:
        t.audit_extended_access = body.audit_extended_access
    stored_audit_extended_access = t.audit_extended_access

    await session.commit()
    return {"ok": True, "audit_extended_access": stored_audit_extended_access}


@router.get(
    "/stats",
    summary="Admin Dashboard Stats",
    description="Aggregated statistics for the admin dashboard. Super admin only.",
    response_model=AdminStatsResponse,
)
async def admin_stats(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> AdminStatsResponse:
    # Total processed inbound messages (one trace per message.in event)
    trace_count_result = await session.execute(
        select(func.count(Event.id)).where(Event.event_type == "message.in")
    )
    total_traces = trace_count_result.scalar() or 0

    totals = await _compute_platform_totals(session)

    # Preserve the legacy pilot metric independently from the broader clinic KPI.
    active_pilots_result = await session.execute(
        select(func.count(Tenant.id)).where(
            Tenant.is_active.is_(True),
            Tenant.plan_type.in_(("pilot", "clinic")),
        )
    )
    active_pilots = active_pilots_result.scalar() or 0

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
        active_builders=totals["active_builders"],
        active_managed_clinics=active_managed,
        estimated_mrr=0.0,  # stub — billing not yet implemented
        total_clinics=totals["total_clinics"],
        active_clinics=totals["active_clinics"],
        total_builders=totals["total_builders"],
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
) -> AdminDashboardResponse:
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    # Total traces
    trace_count_result = await session.execute(
        select(func.count(Event.id)).where(Event.event_type == "message.in")
    )
    total_traces = trace_count_result.scalar() or 0

    totals = await _compute_platform_totals(session)

    # Preserve the legacy pilot metric independently from the broader clinic KPI.
    active_pilots_result = await session.execute(
        select(func.count(Tenant.id)).where(
            Tenant.is_active.is_(True),
            Tenant.plan_type.in_(("pilot", "clinic")),
        )
    )
    active_pilots = active_pilots_result.scalar() or 0

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
            Event.event_type == "message.in",
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
        active_builders=totals["active_builders"],
        active_managed_clinics=active_managed,
        estimated_mrr=0.0,
        total_clinics=totals["total_clinics"],
        active_clinics=totals["active_clinics"],
        total_builders=totals["total_builders"],
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
    response_model=None,
)
async def list_circuit_breakers(
    tenant: TenantContext = Depends(require_super_admin),
) -> list[dict[str, Any]]:
    return []


# ============================================
# Builders Endpoints (Group 4)
# ============================================


@router.get(
    "/builders",
    summary="List Builders",
    description="List all builders with linked clinics. Super admin only.",
    response_model=None,
)
async def list_builders(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[dict[str, Any]]:
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
            Tenant.plan_type != "admin",
        )
    )
    tenants_result = await session.execute(tenants_stmt)
    all_tenants = tenants_result.scalars().all()
    tenants_by_builder: dict[UUID | None, list[Tenant]] = {}
    for t in all_tenants:
        tenants_by_builder.setdefault(t.builder_id, []).append(t)

    # Trace counts for builder clinics
    tenant_ids = [t.id for t in all_tenants]
    trace_counts: dict[UUID, int] = {}
    last_trace_map: dict[UUID, datetime | None] = {}
    if tenant_ids:
        trace_stmt = (
            select(Event.tenant_id, func.count(Event.id))
            .where(
                Event.tenant_id.in_(tenant_ids),
                Event.event_type == "message.in",
            )
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
            .where(
                Event.tenant_id.in_(tenant_ids),
                Event.event_type == "message.in",
            )
            .group_by(Event.tenant_id)
        )
        last_trace_result = await session.execute(last_trace_stmt)
        last_trace_map = {row.tenant_id: row.last_trace_at for row in last_trace_result}

    # Load admin emails: builder users + clinic admin users
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

    output: list[dict[str, Any]] = []
    for b in builders:
        clinics: list[BuilderClinicResponse] = []
        for t in tenants_by_builder.get(b.id, []):
            api_creds: dict[str, Any] = {}
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
                    audit_extended_access=t.audit_extended_access,
                )
            )
        output.append(
            BuilderWithClinicsResponse(
                id=b.id,
                name=b.name,
                admin_email=builder_email_map.get(b.id),
                status=b.status,
                subscription_status=b.subscription_status,
                created_at=b.created_at,
                clinics=clinics,
            ).model_dump(mode="json")
        )

    return output


@router.post(
    "/builders",
    summary="Create Builder",
    description="Create a new builder record. Super admin only.",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_builder(
    body: CreateBuilderRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    # Check email uniqueness
    if await email_in_use(session, str(body.admin_email)):
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
    response_model=None,
)
async def builder_subscriptions(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[dict[str, Any]]:
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
    response_model=None,
)
async def list_transactions(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[dict[str, Any]]:
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
    response_model=None,
)
async def update_builder(
    builder_id: str,
    body: UpdateBuilderRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    try:
        bid = UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    if body.email is not None or body.password is not None:
        builder = await lock_builder_users_before_parent(session, bid)
    else:
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
            select(User).where(
                User.builder_id == bid, cast(User.role, String) == "builder"
            )
        )
        user = user_result.scalar_one_or_none()
        if user:
            if body.email is not None and body.email != user.email:
                # Check email uniqueness
                if await email_in_use(
                    session, str(body.email), exclude_user_id=user.id
                ):
                    raise HTTPException(
                        status_code=409, detail=f"O email {body.email} já está em uso."
                    )
                user.email = body.email
            if body.password is not None:
                user.password_hash = pwd_context.hash(body.password)

    await session.commit()
    return {"ok": True}


@router.delete(
    "/builders/{builder_id}",
    summary="Delete Builder",
    description="Hard-delete builder, its tenant, and associated users. Unlinks managed clinics first. Super admin only.",
    response_model=None,
)
async def delete_builder(
    builder_id: str,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    try:
        bid = UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    result = await session.execute(select(Builder).where(Builder.id == bid))
    builder = result.scalar_one_or_none()
    if not builder:
        raise HTTPException(status_code=404, detail="Builder not found")

    try:
        await detach_builder_media_owners_for_delete(
            session,
            bid,
            actor_user_id=tenant.user_id,
        )
    except MediaStorageGuardError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Delete the builder's own tenant (cascade deletes users, sessions, events)
    builder_tenant_stmt = (
        select(Tenant)
        .join(User, User.tenant_id == Tenant.id)
        .where(User.builder_id == bid)
    )
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
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_builder_clinic(
    builder_id: str,
    body: CreateBuilderClinicRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    try:
        bid = UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    # Verify builder exists
    builder_result = await session.execute(select(Builder).where(Builder.id == bid))
    if not builder_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Builder not found")

    # Check email uniqueness
    if await email_in_use(session, str(body.admin_email)):
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
    response_model=None,
)
async def link_clinic_to_builder(
    builder_id: str,
    body: dict[str, Any],
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    try:
        bid = UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    clinic_id = body.get("clinic_id")
    if not clinic_id:
        raise HTTPException(status_code=400, detail="clinic_id is required")

    try:
        cid = UUID(clinic_id)
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
        raise HTTPException(
            status_code=409, detail="Esta clínica já está vinculada a um builder"
        )

    try:
        await transition_tenant_media_owner(
            session,
            MediaOwnerTransition(
                tenant_id=cid,
                expected_builder_id=None,
                destination_builder_id=bid,
            ),
            actor_user_id=tenant.user_id,
        )
    except MediaStorageCapacityExceeded as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except MediaStorageGuardError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {"ok": True}


@router.post(
    "/builders/{builder_id}/unlink-clinic",
    summary="Unlink Clinic from Builder",
    description="Unlink a clinic currently linked to this builder. Super admin only.",
    response_model=None,
)
async def unlink_clinic_from_builder(
    builder_id: str,
    body: dict[str, Any],
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    try:
        bid = UUID(builder_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid builder ID")

    clinic_id = body.get("clinic_id")
    if not clinic_id:
        raise HTTPException(status_code=400, detail="clinic_id is required")

    try:
        cid = UUID(clinic_id)
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

    try:
        await transition_tenant_media_owner(
            session,
            MediaOwnerTransition(
                tenant_id=cid,
                expected_builder_id=bid,
                destination_builder_id=None,
            ),
            actor_user_id=tenant.user_id,
        )
    except MediaStorageCapacityExceeded as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except MediaStorageGuardError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {"ok": True}


# ============================================
# Coupons Endpoints
# ============================================


@router.post(
    "/coupons",
    summary="Create Coupon",
    description="Create a new discount coupon. Super admin only.",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_coupon(
    body: CreateCouponRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    code_upper = body.code.strip().upper()
    existing = await session.execute(select(Coupon).where(Coupon.code == code_upper))
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
    response_model=None,
)
async def list_coupons(
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[dict[str, Any]]:
    result = await session.execute(select(Coupon).order_by(Coupon.created_at.desc()))
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
    response_model=None,
)
async def deactivate_coupon(
    coupon_id: str,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    try:
        cid = UUID(coupon_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid coupon ID")

    result = await session.execute(select(Coupon).where(Coupon.id == cid))
    coupon = result.scalar_one_or_none()
    if not coupon:
        raise HTTPException(status_code=404, detail="Cupom não encontrado")

    coupon.is_active = False
    await session.commit()
    return {"ok": True}


# ============================================
# Billing migration / rollout control plane
# ============================================


def _billing_material(body: BillingOperationMaterialRequest) -> OperationMaterial:
    return OperationMaterial(
        scope=body.scope,
        operation_id=body.operation_id,
        environment=body.environment,
        from_state=body.from_state,
        to_state=body.to_state,
        budget_digest=body.budget_digest,
        deployment_digest=body.deployment_digest,
        expires_at=body.expires_at,
    )


def _billing_signing_key() -> bytes:
    """Read the configured local receipt key without ever serializing it."""

    return settings.billing_operation_receipt_signing_key.get_secret_value().encode(
        "utf-8"
    )


def _billing_actor(tenant: TenantContext) -> UUID:
    # ``require_super_admin_jwt`` establishes this.  Keep a defensive fixed
    # failure at the route boundary if a nonstandard test/request bypasses it.
    if tenant.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="billing_super_admin_jwt_required",
        )
    return tenant.user_id


def _migration_response(view: MigrationView) -> BillingMigrationResponse:
    return BillingMigrationResponse(
        migration_id=view.migration_id,
        subscription_id=view.subscription_id,
        state=view.state,
        attempt_count=view.attempt_count,
        source_roster_revision=view.source_roster_revision,
        renewal_date=view.renewal_date,
        open_by_date=view.open_by_date,
        cutover_by_date=view.cutover_by_date,
    )


def _rollout_response(control: Any) -> BillingRolloutControlResponse:
    return BillingRolloutControlResponse(
        environment=control.environment,
        state=control.state,
        rollout_revision=control.rollout_revision,
        topup_commercial_enabled=control.topup_commercial_enabled,
        topup_revision=control.topup_revision,
    )


async def _consume_migration_quorum(
    session: AsyncSession, body: BillingMigrationTransitionRequest
) -> None:
    material = _billing_material(body)
    if material.scope != "migration":
        raise BillingOperationRejected("billing_migration_receipt_scope_invalid")
    await BillingOperationAuthority(
        session, signing_key=_billing_signing_key()
    ).consume_quorum(material=material)


def _billing_http_error(exc: Exception) -> HTTPException:
    """Map all billing-control failures to fixed, non-sensitive API details."""

    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        code = "billing_operation_conflict"
    if code.startswith("billing_operation_"):
        BILLING_OPERATION_RECEIPTS_TOTAL.labels(
            role="unknown", outcome="rejected"
        ).inc()
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)


@router.post(
    "/billing/operations/assignments",
    response_model=BillingOperationAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a billing operation role",
)
async def create_billing_operation_assignment(
    body: BillingOperationAssignmentRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingOperationAssignmentResponse:
    actor_user_id = _billing_actor(tenant)
    if body.actor_user_id != actor_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="billing_operation_actor_mismatch",
        )
    try:
        assignment = await BillingOperationAuthority(
            session, signing_key=_billing_signing_key()
        ).assign(
            material=_billing_material(body),
            role=body.role,
            actor_user_id=actor_user_id,
        )
        await session.commit()
    except (BillingOperationRejected, IntegrityError) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return BillingOperationAssignmentResponse(
        assignment_id=assignment.id,
        operation_id=assignment.operation_id,
        role=assignment.role,
        actor_user_id=assignment.actor_user_id,
        active=assignment.active,
        expires_at=assignment.expires_at,
        payload_digest=assignment.payload_digest,
    )


@router.post(
    "/billing/operations/receipts",
    response_model=BillingOperationReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append a signed single-use billing receipt",
)
async def create_billing_operation_receipt(
    body: BillingOperationReceiptRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingOperationReceiptResponse:
    actor_user_id = _billing_actor(tenant)
    try:
        receipt = await BillingOperationAuthority(
            session, signing_key=_billing_signing_key()
        ).record_receipt(
            assignment_id=body.assignment_id,
            actor_user_id=actor_user_id,
            nonce=body.nonce,
            material=_billing_material(body),
            signature=body.signature,
            evidence_type=body.evidence_type,
            request_id=body.request_id,
            inbox_high_watermark=body.inbox_high_watermark,
        )
        await session.commit()
    except (BillingOperationRejected, IntegrityError) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return BillingOperationReceiptResponse(
        receipt_id=receipt.id,
        operation_id=receipt.operation_id,
        role=receipt.role,
        actor_user_id=receipt.actor_user_id,
        nonce=receipt.nonce,
        applied=False,
    )


@router.delete(
    "/billing/operations/assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an unused billing operation assignment",
)
async def revoke_billing_operation_assignment(
    assignment_id: UUID,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> None:
    _billing_actor(tenant)
    try:
        await BillingOperationAuthority(
            session, signing_key=_billing_signing_key()
        ).revoke_assignment(assignment_id=assignment_id)
        await session.commit()
    except (BillingOperationRejected, IntegrityError) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc


@router.post(
    "/billing/migrations/roster",
    response_model=BillingMigrationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import one legacy subscription from the canonical roster",
)
async def import_billing_migration_roster(
    body: BillingMigrationRosterRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingMigrationResponse:
    _billing_actor(tenant)
    try:
        view = await BillingMigrationService(session).import_roster_entry(
            builder_id=body.builder_id,
            subscription_id=body.subscription_id,
            renewal=body.renewal_date,
            target=MigrationTarget(
                catalog_version_id=body.target.catalog_version_id,
                offer_id=body.target.offer_id,
                plan=body.target.plan,
                coupon_code=body.target.coupon_code,
                amount_cents=body.target.amount_cents,
                snapshot=body.target.snapshot,
            ),
        )
        await session.commit()
    except (
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _migration_response(view)


@router.get(
    "/billing/migrations",
    response_model=list[BillingMigrationResponse],
    summary="List sanitized migration roster entries",
)
async def list_billing_migrations(
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[BillingMigrationResponse]:
    """Return only opaque IDs, state, counter and São Paulo cutoffs for export."""

    _billing_actor(tenant)
    # Revision equality is not a fence: webhook ingestion may create rows
    # without advancing it.  Bind the global roster lock, capture IDs, then
    # re-read the exact rows while the lock is held.
    roster_revision = await BillingRepository(session).lock_billing_roster()
    id_result = await session.execute(
        select(BillingMigration.id).order_by(
            BillingMigration.created_at, BillingMigration.id
        )
    )
    migration_ids = list(id_result.scalars().all())
    result = await session.execute(
        select(BillingMigration)
        .where(BillingMigration.id.in_(migration_ids))
        .order_by(BillingMigration.created_at, BillingMigration.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    migrations = list(result.scalars().all())
    subscription_ids = [migration.subscription_id for migration in migrations]
    if subscription_ids:
        await session.execute(
            select(BillingSubscription.id)
            .where(BillingSubscription.id.in_(subscription_ids))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if len(migrations) != len(migration_ids):
        raise HTTPException(status_code=409, detail="billing_roster_changed")
    states = (
        "pending",
        "in_progress",
        "legacy_cancel_pending",
        "legacy_cancelled",
        "recreate_pending",
        "migrated",
        "manual_intervention",
    )
    for state in states:
        BILLING_MIGRATION_BACKLOG.labels(state=state).set(
            sum(migration.state == state for migration in migrations)
        )
    return [
        _migration_response(BillingMigrationService._view(migration))
        for migration in migrations
    ]


@router.post(
    "/billing/migrations/cancel-begin",
    response_model=BillingMigrationResponse,
    summary="Begin a legacy cancellation with provider set to unknown",
)
async def begin_billing_migration_cancel(
    body: BillingMigrationCancelBeginRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingMigrationResponse:
    _billing_actor(tenant)
    try:
        await _consume_migration_quorum(session, body)
        view = await BillingMigrationService(session).begin_legacy_cancel(
            migration_id=body.migration_id, operation_id=body.operation_id
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _migration_response(view)


@router.post(
    "/billing/migrations/cancel-reconcile",
    response_model=BillingMigrationResponse,
    summary="Reconcile a legacy cancel observation without provider egress",
)
async def reconcile_billing_migration_cancel(
    body: BillingMigrationCancelReconcileRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingMigrationResponse:
    actor_user_id = _billing_actor(tenant)
    try:
        await _consume_migration_quorum(session, body)
        view = await BillingMigrationService(session).reconcile_legacy_cancel(
            migration_id=body.migration_id,
            actor_user_id=actor_user_id,
            cancelled=body.cancelled,
            observed_at=body.observed_at,
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _migration_response(view)


@router.post(
    "/billing/migrations/recreate-pending",
    response_model=BillingMigrationResponse,
    summary="Quarantine an unresolved recreation",
)
async def mark_billing_migration_recreate_pending(
    body: BillingMigrationTransitionRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingMigrationResponse:
    actor_user_id = _billing_actor(tenant)
    try:
        await _consume_migration_quorum(session, body)
        view = await BillingMigrationService(session).mark_recreate_pending(
            migration_id=body.migration_id, actor_user_id=actor_user_id
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _migration_response(view)


@router.post(
    "/billing/migrations/recreate-reopen",
    response_model=BillingMigrationResponse,
    summary="Reopen recreation only after an absent exact-ID readback",
)
async def reopen_billing_migration_recreate(
    body: BillingMigrationTransitionRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingMigrationResponse:
    actor_user_id = _billing_actor(tenant)
    try:
        await _consume_migration_quorum(session, body)
        view = await BillingMigrationService(
            session
        ).reopen_recreate_after_absent_readback(
            migration_id=body.migration_id,
            actor_user_id=actor_user_id,
            readback_receipt_id=body.readback_receipt_id,
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _migration_response(view)


@router.post(
    "/billing/migrations/recreate-reserve",
    response_model=BillingCheckoutReservationResponse,
    summary="Reserve a numbered recreation attempt without egress",
)
async def reserve_billing_migration_recreate(
    body: BillingMigrationReserveAttemptRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingCheckoutReservationResponse:
    _billing_actor(tenant)
    try:
        await _consume_migration_quorum(session, body)
        checkout = await BillingMigrationService(session).reserve_recreate_attempt(
            migration_id=body.migration_id,
            environment=body.environment,
            absence_receipt_id=body.absence_receipt_id,
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        BillingTransitionRejected,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return BillingCheckoutReservationResponse(
        checkout_session_id=checkout.id,
        attempt_no=checkout.attempt_no,
        state=checkout.state,
        next_due_date=checkout.next_due_date,
    )


@router.post(
    "/billing/migrations/recover-checkout",
    response_model=BillingMigrationResponse,
    summary="Contain a checkout stuck before general rollout",
)
async def recover_billing_migration_checkout(
    body: BillingMigrationRecoverCheckoutRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingMigrationResponse:
    actor_user_id = _billing_actor(tenant)
    try:
        await _consume_migration_quorum(session, body)
        view = await BillingMigrationService(session).recover_stuck_checkout(
            migration_id=body.migration_id,
            checkout_session_id=body.checkout_session_id,
            environment=body.environment,
            actor_user_id=actor_user_id,
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _migration_response(view)


@router.post(
    "/billing/migrations/manual-intervention",
    response_model=BillingMigrationResponse,
    summary="Stop a migration in the manual-intervention lane",
)
async def manually_intervene_billing_migration(
    body: BillingMigrationTransitionRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingMigrationResponse:
    actor_user_id = _billing_actor(tenant)
    try:
        await _consume_migration_quorum(session, body)
        view = await BillingMigrationService(session).mark_manual_intervention(
            migration_id=body.migration_id, actor_user_id=actor_user_id
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _migration_response(view)


def _provider_evidence(body: BillingProviderEvidenceRequest) -> ProviderEvidenceEnvelope:
    return ProviderEvidenceEnvelope(
        external_reference=body.external_reference,
        provider_event_id=body.provider_event_id,
        provider_key_id=body.provider_key_id,
        provider_operation=body.provider_operation,
        provider_signature=body.provider_signature,
        provider_observed_at=body.provider_observed_at,
        provider_expires_at=body.provider_expires_at,
        provider_snapshot_digest=body.provider_snapshot_digest,
        provider_evidence_digest=body.provider_evidence_digest,
    )


@router.post(
    "/billing/migrations/absence-reads",
    response_model=BillingMigrationAbsenceReadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append one provider-signed empty ASAAS read",
)
async def record_billing_migration_absence_read(
    body: BillingMigrationAbsenceReadRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingMigrationAbsenceReadResponse:
    """Evidence only: authority is the provider signature, the actor the JWT.

    The second valid read also appends the ``recreate_authorized`` receipt;
    reopen/reserve stay quorum-gated transitions.
    """

    actor_user_id = _billing_actor(tenant)
    try:
        read = await BillingMigrationService(session).record_absence_read(
            migration_id=body.migration_id,
            actor_user_id=actor_user_id,
            evidence=_provider_evidence(body.evidence),
            observed_at=body.observed_at,
        )
        await session.commit()
    except (
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return BillingMigrationAbsenceReadResponse(
        read_id=read.read_id,
        read_no=read.read_no,
        prior_checkout_session_id=read.prior_checkout_session_id,
        convergence_limit_at=read.convergence_limit_at,
        authorization_receipt_id=read.authorization_receipt_id,
    )


@router.post(
    "/billing/migrations/provider-flip",
    response_model=BillingMigrationResponse,
    summary="Bind ASAAS only with its verified local receipt",
)
async def flip_billing_migration_provider(
    body: BillingMigrationProviderFlipRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingMigrationResponse:
    actor_user_id = _billing_actor(tenant)
    try:
        await _consume_migration_quorum(session, body)
        view = await BillingMigrationService(session).record_provider_flip(
            migration_id=body.migration_id,
            actor_user_id=actor_user_id,
            evidence=_provider_evidence(body.evidence),
            observed_at=body.observed_at,
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingMigrationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _migration_response(view)


@router.post(
    "/billing/rollout/transitions",
    response_model=BillingRolloutControlResponse,
    summary="Apply a signed billing rollout transition",
)
async def transition_billing_rollout_control(
    body: BillingRolloutTransitionRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingRolloutControlResponse:
    _billing_actor(tenant)
    try:
        control = await BillingRolloutOperationService(
            session, signing_key=_billing_signing_key()
        ).transition(
            expected_revision=body.expected_revision,
            material=_billing_material(body),
            canary_builder_id=body.canary_builder_id,
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        BillingTransitionRejected,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _rollout_response(control)


@router.post(
    "/billing/rollout/receipts",
    response_model=BillingRolloutReadinessReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append authenticated rollout readiness evidence",
)
async def create_billing_rollout_readiness_receipt(
    body: BillingRolloutReadinessReceiptRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingRolloutReadinessReceiptResponse:
    _billing_actor(tenant)
    try:
        receipt = await BillingRolloutOperationService(
            session, signing_key=_billing_signing_key()
        ).record_readiness_receipt(
            environment=body.environment,
            receipt_kind=body.receipt_kind,
            rollout_revision=body.rollout_revision,
            roster_revision=body.roster_revision,
            status=body.status,
            content_sha256=body.content_sha256,
            material=_billing_material(body),
            operation_receipt_id=body.operation_receipt_id,
            provider_receipt_id=body.provider_receipt_id,
            rule_digest=body.rule_digest,
            bundle_digest=body.bundle_digest,
            app_digest=body.app_digest,
            observed_at=body.observed_at,
            expires_at=body.expires_at,
        )
        await session.commit()
    except (BillingOperationRejected, IntegrityError) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return BillingRolloutReadinessReceiptResponse(
        **body.model_dump(), receipt_id=receipt.id
    )


@router.post(
    "/billing/rollout/incident-disable",
    response_model=BillingRolloutControlResponse,
    summary="Disable billing rollout for a signed incident",
)
async def disable_billing_rollout_for_incident(
    body: BillingRolloutTransitionRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingRolloutControlResponse:
    _billing_actor(tenant)
    try:
        control = await BillingRolloutOperationService(
            session, signing_key=_billing_signing_key()
        ).disable_for_incident(
            expected_revision=body.expected_revision,
            material=_billing_material(body),
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        BillingTransitionRejected,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _rollout_response(control)


@router.post(
    "/billing/topup-capability/enable",
    response_model=BillingRolloutControlResponse,
    summary="Enable top-up only from complete cross-service capability evidence",
)
@router.post(
    "/billing/topup-capability",
    response_model=BillingRolloutControlResponse,
    summary="Enable top-up only from complete cross-service capability evidence",
)
async def enable_billing_topup_capability(
    body: BillingTopupCapabilityEnableRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingRolloutControlResponse:
    _billing_actor(tenant)
    evidence = body.evidence
    try:
        service = BillingTopupCapabilityService(
            session, signing_key=_billing_signing_key()
        )
        control = await service.enable(
            expected_revision=body.expected_revision,
            material=_billing_material(body),
            evidence=TopupCapabilityEvidence(
                environment=body.environment,
                capability_receipt_id=evidence.capability_receipt_id,
                producer_receipt_id=evidence.producer_receipt_id,
                credit_receipt_id=evidence.credit_receipt_id,
                reconciliation_receipt_id=evidence.reconciliation_receipt_id,
                verification_receipt_id=evidence.verification_receipt_id,
                producer_digest=evidence.producer_digest,
                credit_digest=evidence.credit_digest,
                reconciliation_digest=evidence.reconciliation_digest,
                verification_digest=evidence.verification_digest,
                billing_fact_schema_digest=evidence.billing_fact_schema_digest,
                producer_actor_id=evidence.producer_actor_id,
                credit_actor_id=evidence.credit_actor_id,
                reconciliation_actor_id=evidence.reconciliation_actor_id,
                verification_actor_id=evidence.verification_actor_id,
                verified_at=evidence.verified_at,
                operation_id=evidence.operation_id,
                deployment_digest=evidence.deployment_digest,
                request_id=evidence.request_id,
                inbox_high_watermark=evidence.inbox_high_watermark,
            ),
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingCapabilityRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        BillingTransitionRejected,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _rollout_response(control)


@router.post(
    "/billing/topup-capability/disable",
    response_model=BillingRolloutControlResponse,
    summary="Disable top-up only for health or revocation",
)
async def disable_billing_topup_capability(
    body: BillingTopupCapabilityDisableRequest,
    tenant: TenantContext = Depends(require_super_admin_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BillingRolloutControlResponse:
    _billing_actor(tenant)
    try:
        service = BillingTopupCapabilityService(
            session, signing_key=_billing_signing_key()
        )
        control = await service.disable(
            expected_revision=body.expected_revision,
            material=_billing_material(body),
            reason=body.reason,
        )
        await session.commit()
    except (
        BillingOperationRejected,
        BillingCapabilityRejected,
        BillingRevisionConflict,
        BillingStateUnavailable,
        BillingTransitionRejected,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise _billing_http_error(exc) from exc
    return _rollout_response(control)
