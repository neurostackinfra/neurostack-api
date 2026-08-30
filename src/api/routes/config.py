"""
NeuroStack API - Config Endpoints
CRUD clinic configuration for the admin panel.
"""

import logging
from collections.abc import Callable, Coroutine
from typing import Any, cast
from uuid import UUID, uuid4

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    require_credentials_jwt,
)
from src.api.schemas.config import (
    ApiCredentialsPatch,
    ConfigPatchRequest,
    ConfigResponse,
)
from src.config.logging import get_logger as get_structured_logger
from src.config.settings import settings
from src.models.clinic_config import ClinicOperations, ClinicScheduling
from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant
from src.repositories import get_db_session_dependency
from src.services.api_credentials import (
    CredentialPolicyError,
    credential_boundary_fields,
    credential_status,
    merge_credentials,
    validate_provider_availability,
)
from src.services.config_service import ARRAY_BUCKETS, flatten_config, unflatten_patch

logger = logging.getLogger(__name__)
# Structured logger for audit events (URL egress is untrusted — see
# scheduling-link-handoff wish, Group 1 deliverable 5).
_audit_logger = get_structured_logger(__name__)

router = APIRouter()


class _CredentialValidationRoute(APIRoute):
    """Return stable, input-free validation errors for the credential boundary."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def credential_route_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                errors = exc.errors()
                fields = sorted(
                    {(str(error["loc"][-1]), str(error["type"])) for error in errors}
                )
                unsafe_field = next(
                    (
                        field
                        for field, _ in fields
                        if field.endswith(("_api_url", "_base_url"))
                    ),
                    None,
                )
                unsafe_url = unsafe_field and any(
                    "unsafe_api_url" in str(error.get("ctx", {}).get("error", ""))
                    for error in errors
                )
                detail: dict[str, Any]
                if unsafe_url:
                    detail = {"error": "unsafe_api_url", "field": unsafe_field}
                else:
                    detail = {
                        "error": "invalid_credentials_payload",
                        "fields": [
                            {"field": field, "code": code} for field, code in fields
                        ],
                    }
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={"detail": detail},
                )

        return credential_route_handler


credentials_router = APIRouter(route_class=_CredentialValidationRoute)


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
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "target_tenant_forbidden"},
        )
    try:
        return UUID(builder_id_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "target_tenant_forbidden"},
        ) from exc


async def _resolve_config_tenant(
    request: Request,
    tenant: TenantContext,
    session: AsyncSession,
) -> TenantContext:
    """If builder passes ?tenant_id= or ?clinic_id=, validate ownership and override."""
    override = request.query_params.get("tenant_id") or request.query_params.get(
        "clinic_id"
    )
    if not override or override == str(tenant.tenant_id):
        return tenant
    try:
        target_id = UUID(override)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_target_tenant"},
        ) from exc
    if tenant.role not in {"builder", "super_admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "target_tenant_forbidden"},
        )
    target_query = select(Tenant.id).where(Tenant.id == target_id)
    if tenant.role == "builder":
        target_query = target_query.where(
            Tenant.builder_id == _extract_builder_id(request)
        )
    target = (await session.execute(target_query)).scalar_one_or_none()
    if target is None:
        error = (
            "target_tenant_not_found"
            if tenant.role == "super_admin"
            else "target_tenant_forbidden"
        )
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if tenant.role == "super_admin"
                else status.HTTP_403_FORBIDDEN
            ),
            detail={"error": error},
        )
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


async def _load_config(
    session: AsyncSession,
    tenant_context: TenantContext,
) -> ClinicConfigTable | None:
    """Load a credential bucket without creating or flushing a missing row."""

    result = await session.execute(
        select(ClinicConfigTable).where(
            ClinicConfigTable.tenant_id == tenant_context.tenant_id,
        )
    )
    return result.scalar_one_or_none()


def _new_config(tenant_context: TenantContext) -> ClinicConfigTable:
    """Construct, but do not add or flush, an empty tenant configuration."""

    return ClinicConfigTable(
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
) -> dict[str, Any]:
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
    patch_data = body.model_dump(exclude_unset=True)
    blocked_fields = credential_boundary_fields(patch_data)
    if blocked_fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "credential_endpoint_required",
                "fields": blocked_fields,
            },
        )
    tenant = await _resolve_config_tenant(request, tenant, session)
    row = await _get_or_create_config(session, tenant)

    # Group incoming fields by bucket
    grouped = unflatten_patch(patch_data)

    # Capture the previous scheduling-link URL up-front so we can emit an
    # audit event AFTER a successful commit if the value actually changed.
    # The clinic-controlled URL is delivered verbatim to every patient, so
    # any mutation of it is untrusted-egress that ops needs to see.
    _prev_scheduling = row.scheduling or {}
    _prev_link_url = _prev_scheduling.get("scheduling_link_url")

    # Merge each bucket
    for bucket_name, new_values in grouped.items():
        if bucket_name in ARRAY_BUCKETS:
            # Array buckets: replace the whole list
            setattr(row, bucket_name, new_values)
        else:
            # Dict buckets: merge with existing
            existing = getattr(row, bucket_name, None) or {}
            merged = {**existing, **cast(dict[str, Any], new_values)}
            # Revalidate buckets with field-level rules after merging.  The
            # request schema allows extra fields, so this is the enforcement
            # point for scheduling URLs and structured business hours.
            bucket_model = {
                "scheduling": ClinicScheduling,
                "operations": ClinicOperations,
            }.get(bucket_name)
            if bucket_model is not None:
                try:
                    validated = bucket_model(**merged)
                except ValidationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=jsonable_encoder(exc.errors()),
                    )
                validated_dump = validated.model_dump()
                dropped_keys = sorted(set(merged) - set(validated_dump))
                if dropped_keys:
                    logger.warning(
                        "config bucket validation dropped undeclared keys "
                        "tenant=%s bucket=%s keys=%s",
                        tenant.tenant_id,
                        bucket_name,
                        dropped_keys,
                    )
                # Persist the validated/normalized form (e.g. empty strings
                # collapsed to None by validators).
                merged = validated_dump
            setattr(row, bucket_name, merged)

    await session.flush()
    await session.commit()

    # Audit: scheduling_link_url mutations. Emitted post-commit so the
    # event reflects a durable state change. `_prev_link_url` may be None
    # (never set) — treat that as a "created" transition.
    _new_scheduling = row.scheduling or {}
    _new_link_url = _new_scheduling.get("scheduling_link_url")
    if _new_link_url != _prev_link_url:
        _audit_logger.info(
            "scheduling_link_url_changed",
            tenant_id=str(tenant.tenant_id),
            previous_url=_prev_link_url,
            new_url=_new_link_url,
        )

    # Warn when lgpd_enabled but mandatory compliance fields are missing
    lgpd_bucket = row.lgpd or {}
    if lgpd_bucket.get("lgpd_enabled") in (True, "yes", "true"):
        missing = [
            f for f in ("clinic_legal_name", "dpo_contact") if not lgpd_bucket.get(f)
        ]
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


@credentials_router.get(
    "/api-credentials",
    summary="Get API Credentials Status",
    description="Return credential configuration status with masked secrets.",
)
async def get_api_credentials(
    request: Request,
    tenant: TenantContext = Depends(require_credentials_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    tenant = await _resolve_config_tenant(request, tenant, session)
    row = await _get_or_create_config(session, tenant)
    creds = row.api_credentials or {}
    return credential_status(creds)


@credentials_router.patch(
    "/api-credentials",
    summary="Update API Credentials",
    description=(
        "Validate a credential patch. Effective writes to providers that are "
        "not available yet are rejected; empty secret fields preserve existing values."
    ),
)
async def patch_api_credentials(
    request: Request,
    body: ApiCredentialsPatch,
    tenant: TenantContext = Depends(require_credentials_jwt),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    patch = body.model_dump(exclude_unset=True)
    tenant = await _resolve_config_tenant(request, tenant, session)
    row = await _load_config(session, tenant)
    existing = row.api_credentials or {} if row else {}
    try:
        creds = merge_credentials(existing, patch)
        validate_provider_availability(existing, creds)
    except CredentialPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.detail,
        ) from exc

    if row is None:
        row = _new_config(tenant)
        session.add(row)

    # Auto-generate the write-only Meta webhook verify token on first setup.
    if (
        creds.get("meta_phone_number_id") or creds.get("meta_permanent_token")
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
            ClinicConfigTable.api_credentials["meta_phone_number_id"].astext
            == str(new_phone_id),
            ClinicConfigTable.tenant_id != tenant.tenant_id,
        )
        conflict = (await session.execute(stmt)).scalar_one_or_none()
        if conflict is not None:
            logger.warning(
                "meta_phone_number_id_conflict tenant=%s existing_tenant=%s",
                tenant.tenant_id,
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
                "meta_phone_number_id_conflict_race tenant=%s",
                tenant.tenant_id,
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
        logger.error(
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

    return {"ok": True, **credential_status(creds)}


router.include_router(credentials_router)
