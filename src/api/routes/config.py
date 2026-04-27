"""
NeuroStack API - Config Endpoints
CRUD clinic configuration for the admin panel.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.api.schemas.config import ConfigPatchRequest, ConfigResponse
from src.models.clinic_config_table import ClinicConfigTable
from src.repositories import get_db_session_dependency
from src.services.config_service import (
    ALL_BUCKETS,
    ARRAY_BUCKETS,
    flatten_config,
    unflatten_patch,
)

router = APIRouter()


async def _get_or_create_config(
    session: AsyncSession,
    tenant_context: TenantContext,
) -> ClinicConfigTable:
    """Load the ClinicConfigTable row for the tenant, or create a default one."""
    stmt = select(ClinicConfigTable).where(
        ClinicConfigTable.tenant_id == tenant_context.tenant_id,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()

    if row is None:
        row = ClinicConfigTable(
            tenant_id=tenant_context.tenant_id,
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
        )
        session.add(row)
        await session.flush()

    return row


@router.get(
    "",
    response_model=ConfigResponse,
    summary="Get Clinic Configuration",
    description="Load all clinic configuration as a flat JSON object.",
    tags=["Config"],
)
async def get_config(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> ConfigResponse:
    """Return the full flat config for the authenticated tenant."""
    row = await _get_or_create_config(session, tenant)
    flat = flatten_config(row)
    return ConfigResponse(**flat)


@router.patch(
    "",
    response_model=ConfigResponse,
    summary="Update Clinic Configuration",
    description=(
        "Partial update — send only the fields you want to change. "
        "Each field is routed to its correct JSONB bucket and merged."
    ),
    tags=["Config"],
)
async def patch_config(
    body: ConfigPatchRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> ConfigResponse:
    """Apply a partial update and return the full flat config."""
    row = await _get_or_create_config(session, tenant)

    # Group incoming fields by bucket
    patch_data = body.model_dump(exclude_unset=True)
    grouped = unflatten_patch(patch_data)

    # Merge each bucket
    for bucket_name, new_values in grouped.items():
        if bucket_name in ARRAY_BUCKETS:
            # Array buckets: replace the whole list
            setattr(row, bucket_name, new_values)
        else:
            # Dict buckets: merge with existing
            existing = getattr(row, bucket_name, None) or {}
            merged = {**existing, **new_values}
            setattr(row, bucket_name, merged)

    await session.flush()
    await session.commit()

    flat = flatten_config(row)
    return ConfigResponse(**flat)
