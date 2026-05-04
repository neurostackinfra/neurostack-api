"""
NeuroStack API - Config Endpoints
CRUD clinic configuration for the admin panel.
"""

import logging
from typing import Any, Optional
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context, require_builder_clinic_access
from src.api.schemas.config import ConfigPatchRequest, ConfigResponse
from src.config.settings import settings
from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant
from src.repositories import get_db_session_dependency
from src.services.config_service import (
    ALL_BUCKETS,
    ARRAY_BUCKETS,
    flatten_config,
    unflatten_patch,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _extract_builder_id(request: Request) -> UUID:
    """Extract builder_id from the JWT Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="Missing authorization")
    token = auth_header[7:]
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=["HS256"],
            options={"require": ["sub", "tenant_id", "exp"]},
        )
        builder_id_str = payload.get("builder_id")
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status_code=403, detail="Invalid token")
    if not builder_id_str:
        raise HTTPException(status_code=403, detail="No builder_id in token")
    return UUID(builder_id_str)


async def _resolve_config_tenant(
    request: Request,
    tenant: TenantContext,
    session: AsyncSession,
) -> TenantContext:
    """If builder passes ?tenant_id= or ?clinic_id=, validate ownership and override."""
    override = request.query_params.get("tenant_id") or request.query_params.get("clinic_id")
    if not override or override == str(tenant.tenant_id):
        return tenant
    if tenant.role != "builder":
        raise HTTPException(status_code=403, detail="Only builders can override tenant_id")
    try:
        target_id = UUID(override)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tenant_id")
    builder_id = _extract_builder_id(request)
    await require_builder_clinic_access(target_id, builder_id, session)
    # Return a new context pointing to the target clinic
    return TenantContext(
        tenant_id=target_id,
        role=tenant.role,
        user_id=tenant.user_id,
    )


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
            api_credentials={},
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
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> ConfigResponse:
    """Return the full flat config for the authenticated tenant."""
    tenant = await _resolve_config_tenant(request, tenant, session)
    row = await _get_or_create_config(session, tenant)
    flat = flatten_config(row)
    # Fallback: if clinic_name not in identity config, use tenant name
    if not flat.get("clinic_name"):
        t_result = await session.execute(
            select(Tenant.name).where(Tenant.id == tenant.tenant_id)
        )
        t_name = t_result.scalar_one_or_none()
        if t_name:
            flat["clinic_name"] = t_name
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
    request: Request,
    body: ConfigPatchRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> ConfigResponse:
    """Apply a partial update and return the full flat config."""
    tenant = await _resolve_config_tenant(request, tenant, session)
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

    # Invalidate Redis cache so next pipeline run picks up changes
    from src.services.event_service import invalidate_clinic_config_cache
    await invalidate_clinic_config_cache(tenant.tenant_id)

    flat = flatten_config(row)
    return ConfigResponse(**flat)


# ============================================
# API Credentials (WhatsApp + LLM providers)
# ============================================

SECRET_FIELDS = {"zapi_token", "zapi_client_token", "evolution_api_key", "llm_api_key"}


def _mask(value: Optional[str]) -> Optional[str]:
    """Return a masked version of a secret, or None if not set."""
    if not value:
        return None
    if len(value) <= 8:
        return "••••••••"
    return value[:3] + "••••" + value[-3:]


def _build_credential_status(creds: dict[str, Any]) -> dict[str, Any]:
    """Build the CredentialStatus response the frontend expects."""
    return {
        # WhatsApp
        "whatsapp_provider": creds.get("whatsapp_provider"),
        "zapi_instance_id": creds.get("zapi_instance_id"),
        "zapi_token_set": bool(creds.get("zapi_token")),
        "zapi_token_masked": _mask(creds.get("zapi_token")),
        "zapi_client_token_set": bool(creds.get("zapi_client_token")),
        # Evolution API
        "evolution_api_url": creds.get("evolution_api_url"),
        "evolution_instance_name": creds.get("evolution_instance_name"),
        "evolution_api_key_set": bool(creds.get("evolution_api_key")),
        "evolution_api_key_masked": _mask(creds.get("evolution_api_key")),
        # LLM
        "llm_api_key_set": bool(creds.get("llm_api_key")),
        "llm_api_key_masked": _mask(creds.get("llm_api_key")),
        "llm_base_url": creds.get("llm_api_url"),
        "llm_model_name": creds.get("llm_model_name"),
        # Overall status
        "apis_configured": _is_configured(creds),
    }


def _is_configured(creds: dict[str, Any]) -> bool:
    """Check if WhatsApp OR LLM provider credentials are fully set."""
    whatsapp_ok = False
    provider = creds.get("whatsapp_provider", "zapi")
    if provider == "zapi":
        whatsapp_ok = bool(creds.get("zapi_instance_id") and creds.get("zapi_token"))
    else:
        whatsapp_ok = bool(
            creds.get("evolution_api_url")
            and creds.get("evolution_instance_name")
            and creds.get("evolution_api_key")
        )
    llm_ok = bool(creds.get("llm_api_key") and creds.get("llm_api_url"))
    return whatsapp_ok or llm_ok

@router.get(
    "/api-credentials",
    summary="Get API Credentials Status",
    description="Return credential configuration status with masked secrets.",
)
async def get_api_credentials(
    request: Request,
    clinic_id: Optional[str] = Query(None),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    tenant = await _resolve_config_tenant(request, tenant, session)
    row = await _get_or_create_config(session, tenant)
    creds = row.api_credentials or {}
    return _build_credential_status(creds)


@router.patch(
    "/api-credentials",
    summary="Update API Credentials",
    description="Save WhatsApp and LLM provider credentials. Empty secret fields preserve existing values.",
)
async def patch_api_credentials(
    request: Request,
    body: dict[str, Any],
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    tenant = await _resolve_config_tenant(request, tenant, session)
    row = await _get_or_create_config(session, tenant)
    creds = dict(row.api_credentials or {})

    # Known credential fields
    known_fields = {
        "whatsapp_provider", "zapi_instance_id", "zapi_token", "zapi_client_token",
        "evolution_api_url", "evolution_instance_name", "evolution_api_key",
        "llm_api_key", "llm_api_url", "llm_provider", "llm_model_name",
    }

    for field in known_fields:
        if field not in body:
            continue
        value = body[field]
        # COALESCE: empty string or masked value → keep existing
        if field in SECRET_FIELDS:
            if not value or "••••" in str(value) or "****" in str(value):
                continue
        creds[field] = value

    row.api_credentials = creds
    await session.flush()
    await session.commit()

    # Invalidate Redis cache so next pipeline run picks up changes
    from src.services.event_service import invalidate_clinic_config_cache
    await invalidate_clinic_config_cache(tenant.tenant_id)

    return {"ok": True, **_build_credential_status(creds)}
