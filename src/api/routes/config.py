"""
NeuroStack API - Config Endpoints
CRUD clinic configuration for the admin panel.
"""

import logging
from typing import Any, Optional
from uuid import UUID, uuid4

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    try:
        target_id = UUID(override)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tenant_id")
    # Super admin can access any clinic without ownership validation
    if tenant.role == "super_admin":
        return TenantContext(
            tenant_id=target_id,
            role=tenant.role,
            user_id=tenant.user_id,
        )
    if tenant.role != "builder":
        raise HTTPException(status_code=403, detail="Only builders can override tenant_id")
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
    # Also return bot_active and clinic_name from the Tenant row
    t_result = await session.execute(
        select(Tenant.name, Tenant.bot_active).where(Tenant.id == tenant.tenant_id)
    )
    t_row = t_result.first()
    if t_row:
        if not flat.get("clinic_name"):
            flat["clinic_name"] = t_row[0]
        flat["bot_active"] = t_row[1] if t_row[1] is not None else True
    return ConfigResponse(**flat)


@router.patch(
    "/bot-toggle",
    summary="Toggle AI Bot Active",
    description=(
        "Atomically toggle `bot_active` for the clinic. "
        "When pausing (False→False), the next webhook message triggers one offline notification per session. "
        "When re-enabling (→True), clears `offline_notified` from all active sessions. "
        "Does NOT affect `is_active` (billing stays on)."
    ),
    tags=["Config"],
)
async def toggle_bot_active(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict:
    """Toggle tenant.bot_active and return the new state."""
    tenant = await _resolve_config_tenant(request, tenant, session)

    t_result = await session.execute(
        select(Tenant).where(Tenant.id == tenant.tenant_id)
    )
    t = t_result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Tenant not found")

    t.bot_active = not t.bot_active
    new_value = t.bot_active

    await session.flush()
    await session.commit()

    logger.info(
        "bot_active_toggled tenant=%s new_value=%s",
        tenant.tenant_id,
        new_value,
    )
    return {"bot_active": new_value}


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

    # Warn when lgpd_enabled but mandatory compliance fields are missing
    lgpd_bucket = row.lgpd or {}
    if lgpd_bucket.get("lgpd_enabled") in (True, "yes", "true"):
        missing = [f for f in ("clinic_legal_name", "dpo_contact") if not lgpd_bucket.get(f)]
        if missing:
            logger.warning(
                "lgpd_enabled but required compliance fields missing — tenant=%s fields=%s",
                tenant.tenant_id,
                missing,
            )

    # Invalidate Redis cache so next pipeline run picks up changes
    from src.services.event_service import invalidate_clinic_config_cache
    await invalidate_clinic_config_cache(tenant.tenant_id)

    flat = flatten_config(row)
    return ConfigResponse(**flat)


# ============================================
# API Credentials (WhatsApp + LLM providers)
# ============================================

SECRET_FIELDS = {
    "zapi_token",
    "zapi_client_token",
    "evolution_api_key",
    "llm_api_key",
    "whisper_api_key",
    "meta_permanent_token",
    "meta_app_secret",
    "uazapi_token",
}

# WhatsApp providers accepted in the UI but without a backend integration yet.
# Saving credentials for these should fail loudly rather than silently succeed.
UNIMPLEMENTED_WHATSAPP_PROVIDERS = {"dialog360", "twilio"}


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
        # Uazapi (wuzapi self-hosted)
        "uazapi_api_url": creds.get("uazapi_api_url"),
        "uazapi_instance_name": creds.get("uazapi_instance_name"),
        "uazapi_token_set": bool(creds.get("uazapi_token")),
        "uazapi_token_masked": _mask(creds.get("uazapi_token")),
        # Meta Cloud API (WhatsApp Business — official)
        "meta_phone_number_id": creds.get("meta_phone_number_id"),
        "meta_waba_id": creds.get("meta_waba_id"),
        "meta_permanent_token_set": bool(creds.get("meta_permanent_token")),
        "meta_permanent_token_masked": _mask(creds.get("meta_permanent_token")),
        "meta_app_secret_set": bool(creds.get("meta_app_secret")),
        "meta_app_secret_masked": _mask(creds.get("meta_app_secret")),
        # Verify token is auto-generated per clinic and must be copyable by the
        # user for the Meta webhook handshake — it is not a secret credential.
        "meta_verify_token": creds.get("meta_verify_token"),
        # LLM
        "llm_api_key_set": bool(creds.get("llm_api_key")),
        "llm_api_key_masked": _mask(creds.get("llm_api_key")),
        "llm_base_url": creds.get("llm_api_url"),
        "llm_model_name": creds.get("llm_model_name"),
        # Whisper (audio transcription)
        "whisper_api_key_set": bool(creds.get("whisper_api_key")),
        "whisper_api_key_masked": _mask(creds.get("whisper_api_key")),
        "whisper_model": creds.get("whisper_model"),
        "audio_max_seconds": creds.get("audio_max_seconds") or 60,
        # STT provider
        "stt_provider": creds.get("stt_provider", "outros"),
        "stt_api_url": creds.get("stt_api_url", ""),
        # Overall status
        "apis_configured": _is_configured(creds),
    }


def _is_configured(creds: dict[str, Any]) -> bool:
    """Check if WhatsApp OR LLM provider credentials are fully set."""
    whatsapp_ok = False
    provider = creds.get("whatsapp_provider", "zapi")
    if provider == "zapi":
        whatsapp_ok = bool(creds.get("zapi_instance_id") and creds.get("zapi_token"))
    elif provider == "meta_cloud":
        whatsapp_ok = bool(
            creds.get("meta_phone_number_id") and creds.get("meta_permanent_token")
        )
    elif provider == "uazapi":
        whatsapp_ok = bool(
            creds.get("uazapi_api_url")
            and creds.get("uazapi_token")
            and creds.get("uazapi_instance_name")
        )
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

    # Guard: providers accepted in the UI but without a backend integration
    # yet must fail loudly instead of silently accepting credentials the
    # webhook layer cannot honor.
    incoming_provider = body.get("whatsapp_provider")
    if incoming_provider in UNIMPLEMENTED_WHATSAPP_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": "provider_not_implemented",
                "provider": incoming_provider,
                "message": (
                    f"WhatsApp provider '{incoming_provider}' is not yet supported. "
                    "Please choose Meta Cloud API, Z-API, or Evolution API."
                ),
            },
        )

    row = await _get_or_create_config(session, tenant)
    creds = dict(row.api_credentials or {})

    # Known credential fields
    known_fields = {
        "whatsapp_provider", "zapi_instance_id", "zapi_token", "zapi_client_token",
        "evolution_api_url", "evolution_instance_name", "evolution_api_key",
        "meta_phone_number_id", "meta_waba_id", "meta_permanent_token", "meta_app_secret",
        "uazapi_api_url", "uazapi_token", "uazapi_instance_name",
        "llm_api_key", "llm_api_url", "llm_provider", "llm_model_name",
        "whisper_api_key", "whisper_model",
        "stt_provider", "stt_api_url",
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

    # Auto-generate the Meta webhook verify token the first time the clinic
    # provides identifying Meta credentials. The token is not a secret and
    # must be copyable by the user for the Meta dashboard handshake.
    if (
        creds.get("meta_phone_number_id")
        or creds.get("meta_permanent_token")
    ) and not creds.get("meta_verify_token"):
        creds["meta_verify_token"] = uuid4().hex

    # Application-level uniqueness check for meta_phone_number_id — the
    # webhook resolves the target tenant by this value, so a collision would
    # route messages across clinics. The Postgres migration adds a partial
    # unique index that closes the race window; this pre-check keeps the
    # error message friendly on the common (non-racing) path.
    new_phone_id = creds.get("meta_phone_number_id")
    if new_phone_id:
        stmt = select(ClinicConfigTable.tenant_id).where(
            ClinicConfigTable.api_credentials["meta_phone_number_id"].astext == str(new_phone_id),
            ClinicConfigTable.tenant_id != tenant.tenant_id,
        )
        conflict = (await session.execute(stmt)).scalar_one_or_none()
        if conflict is not None:
            logger.warning(
                "meta_phone_number_id_conflict tenant=%s attempted_value=%s existing_tenant=%s",
                tenant.tenant_id,
                new_phone_id,
                conflict,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "meta_phone_number_id_conflict",
                    "message": (
                        "This Meta Phone Number ID is already registered by another clinic. "
                        "Each WhatsApp Business number can only be linked to one clinic."
                    ),
                },
            )

    row.api_credentials = creds
    try:
        await session.flush()
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # The database-level partial unique index caught a race the app-level
        # check above missed (two concurrent saves). Surface a friendly error
        # instead of leaking the raw driver message.
        message = str(exc.orig) if exc.orig is not None else str(exc)
        if "meta_phone_number_id" in message:
            logger.warning(
                "meta_phone_number_id_conflict_race tenant=%s attempted_value=%s",
                tenant.tenant_id,
                new_phone_id,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "meta_phone_number_id_conflict",
                    "message": (
                        "This Meta Phone Number ID is already registered by another clinic. "
                        "Each WhatsApp Business number can only be linked to one clinic."
                    ),
                },
            )
        logger.exception(
            "api_credentials_integrity_error tenant=%s",
            tenant.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "credential_conflict",
                "message": "Failed to save credentials due to a data integrity conflict.",
            },
        )

    # Invalidate Redis cache so next pipeline run picks up changes
    from src.services.event_service import invalidate_clinic_config_cache
    await invalidate_clinic_config_cache(tenant.tenant_id)

    return {"ok": True, **_build_credential_status(creds)}
