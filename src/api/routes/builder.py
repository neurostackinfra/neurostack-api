"""
NeuroStack API - Builder Endpoints
Builder-scoped routes with SQL-level tenant isolation.
"""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.api.middleware.auth import require_builder
from src.api.schemas.builder import (
    BuilderClinicResponse,
    BuilderProfileResponse,
    BuilderStatsResponse,
    CreateBuilderClinicRequest,
    ToggleClinicStatusRequest,
)
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.event import Event
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories import get_db_session_dependency

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
# GET /clinics - List builder's clinics
# ============================================


@router.get(
    "/clinics",
    summary="List Builder Clinics",
    description="Return clinics owned by the authenticated builder (SQL-level scope isolation).",
)
async def list_clinics(
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    # SQL-level scope isolation: WHERE builder_id = :jwt_builder_id AND is_active = True
    stmt = (
        select(Tenant)
        .options(selectinload(Tenant.clinic_config_row))
        .where(
            Tenant.builder_id == builder_id,
            Tenant.is_active.is_(True),
        )
        .order_by(Tenant.name)
    )
    result = await session.execute(stmt)
    tenants = result.scalars().all()

    return [
        BuilderClinicResponse(
            id=t.id,
            name=t.name,
            slug=t.slug,
            status="active" if t.is_active else "inactive",
            plan_type=t.plan_type,
            is_active=t.is_active,
            created_at=t.created_at,
        ).model_dump(mode="json")
        for t in tenants
    ]


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
        plan_type="pilot",
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
