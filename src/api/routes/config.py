"""
NeuroStack API - Config Endpoints
CRUD clinic configuration for the admin panel.
"""

import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
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
from src.handoff.pause_policy import resolve_trigger_reactivation
from src.models.clinic_config import (
    ClinicOperations,
    ClinicScheduling,
    HandoffStandbyPolicy,
)
from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant
from src.observability.metrics import API_CREDENTIALS_CHANGE_EMIT_FAILURES_TOTAL
from src.repositories import get_db_session_dependency
from src.services.api_credentials import (
    STT_FIXED_API_BASES,
    CredentialPolicyError,
    credential_boundary_fields,
    credential_status,
    is_semantic_credentials_echo,
    merge_credentials,
    resolve_stt_credentials,
    validate_provider_availability,
)
from src.services.config_service import ARRAY_BUCKETS, flatten_config, unflatten_patch

logger = logging.getLogger(__name__)
# Structured logger for audit events (URL egress is untrusted — see
# scheduling-link-handoff wish, Group 1 deliverable 5).
_audit_logger = get_structured_logger(__name__)

_HANDOFF_POLICY_FIELDS = (
    "handoff_active",
    "standby_mode",
    "handoff_period",
    "handoff_cycle_hours",
    "handoff_cycle_days",
)
_HANDOFF_POLICY_FIELD_SET = frozenset(_HANDOFF_POLICY_FIELDS)


def _add_effective_reactivation_fields(
    flat: dict[str, Any],
    row: ClinicConfigTable,
) -> None:
    """Expose the same tolerant trigger values consumed by the webhook."""

    reactivation = resolve_trigger_reactivation(
        row.handoff,
        datetime.now(timezone.utc),
    )
    flat["effective_trigger_message"] = reactivation.trigger_message
    flat["effective_reactivation_delay_hours"] = reactivation.delay_hours


class _PolicyValidationRoute(APIRoute):
    """Sanitize request-model errors for the extensible config endpoint."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def policy_route_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                violations = []
                for error in exc.errors():
                    location = tuple(error.get("loc", ()))
                    field = next(
                        (
                            str(item)
                            for item in reversed(location)
                            if str(item) in _HANDOFF_POLICY_FIELD_SET
                        ),
                        None,
                    )
                    if field is not None:
                        violations.append(
                            {
                                "field": field,
                                "code": str(error.get("type", "invalid")),
                            }
                        )
                if not violations:
                    raise
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "detail": {
                            "error": "invalid_handoff_policy",
                            "violations": violations,
                        }
                    },
                )

        return policy_route_handler


router = APIRouter(route_class=_PolicyValidationRoute)


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
                endpoint_error = next(
                    (
                        error
                        for error in errors
                        if error.get("type") == "invalid_provider_endpoint"
                        or "invalid_provider_endpoint"
                        in str(error.get("ctx", {}).get("error", ""))
                    ),
                    None,
                )
                detail: dict[str, Any]
                if endpoint_error is not None:
                    context = endpoint_error.get("ctx", {})
                    capability = context.get("capability")
                    provider = context.get("provider")
                    field = context.get("field")
                    if (capability, provider, field) not in {
                        ("llm", "gemini", "llm_api_url"),
                        (
                            "multimodal",
                            "openrouter",
                            "multimodal_api_url",
                        ),
                        ("stt", "elevenlabs_scribe", "stt_api_url"),
                        ("stt", "openrouter", "stt_api_url"),
                    }:
                        capability, provider, field = (
                            "llm",
                            "gemini",
                            "llm_api_url",
                        )
                    detail = {
                        "error": "invalid_provider_endpoint",
                        "capability": capability,
                        "provider": provider,
                        "field": field,
                    }
                elif unsafe_url:
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


def _emit_api_credentials_changed(
    *,
    actor_id: UUID | None,
    actor_role: str | None,
    tenant_id: UUID,
    previous_provider: str,
    new_provider: str,
) -> None:
    """Emit the allowlisted, secret-free post-commit credential event."""

    _audit_logger.info(
        "api_credentials_changed",
        actor_id=str(actor_id) if actor_id is not None else "unknown",
        actor_role=actor_role or "unknown",
        tenant_id=str(tenant_id),
        capability="stt",
        fields=(
            "stt_provider",
            "stt_api_url",
            "stt_api_key",
            "stt_model_name",
        ),
        previous_provider=previous_provider,
        new_provider=new_provider,
        endpoint_class="fixed",
    )


_CANONICAL_SERVICE_FORMATS = frozenset({"online", "presential", "both"})
_HANDOFF_POLICY_DEFAULTS: dict[str, Any] = {
    "handoff_active": "yes",
    "standby_mode": "active",
    "handoff_period": "permanent",
    "handoff_cycle_hours": None,
    "handoff_cycle_days": None,
}


def _validate_handoff_policy_patch(
    patch_data: dict[str, Any], row: ClinicConfigTable | None
) -> dict[str, Any] | None:
    """Return the canonical effective policy when this PATCH touches it.

    Validation is intentionally completed before the caller adds a new row or
    assigns any JSONB column.  Unrelated PATCHes therefore leave malformed
    historical policy values byte-for-byte untouched.
    """

    touched = set(_HANDOFF_POLICY_FIELDS).intersection(patch_data)
    if not touched:
        return None

    persisted = row.handoff if row is not None else None
    if persisted is None:
        effective = dict(_HANDOFF_POLICY_DEFAULTS)
    elif isinstance(persisted, dict):
        effective = {
            **_HANDOFF_POLICY_DEFAULTS,
            **{
                field: persisted[field]
                for field in _HANDOFF_POLICY_FIELDS
                if field in persisted
            },
        }
    else:
        missing = sorted(set(_HANDOFF_POLICY_FIELDS) - touched)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_handoff_policy",
                    "violations": [
                        {"field": field, "code": "repair_required"} for field in missing
                    ],
                },
            )
        effective = {}

    effective.update(
        {
            field: patch_data[field]
            for field in _HANDOFF_POLICY_FIELDS
            if field in patch_data
        }
    )
    try:
        policy = HandoffStandbyPolicy.model_validate(effective)
    except ValidationError as exc:
        violations = []
        for error in exc.errors():
            location = error.get("loc", ())
            field = str(location[-1]) if location else "handoff_policy"
            code = str(error["type"])
            context_error = str(error.get("ctx", {}).get("error", ""))
            for combination_code in (
                "inactive_requires_permanent_without_duration",
                "permanent_forbids_duration",
                "cycle_requires_exactly_one_duration",
            ):
                if combination_code in context_error:
                    code = combination_code
                    break
            violations.append({"field": field, "code": code})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_handoff_policy",
                "violations": violations,
            },
        ) from exc
    return policy.model_dump()


def _validate_service_format_compatibility(
    patch_data: dict[str, Any], row: ClinicConfigTable | None
) -> None:
    """Reject service modalities incompatible with the effective clinic format.

    This is deliberately a PATCH-level check.  Unrelated updates must not
    reinterpret historical modality values, while a service/global modality
    update is checked against the counterpart that will remain persisted.
    """

    if "services" not in patch_data and "service_format" not in patch_data:
        return

    if "service_format" in patch_data:
        clinic_service_format = patch_data["service_format"]
    else:
        clinic_service_format = (
            (row.operations or {}).get("service_format") if row else None
        )

    # Historical/free-text global values stay untouched and impose no
    # compatibility rule until the caller chooses one of the canonical forms.
    if clinic_service_format not in _CANONICAL_SERVICE_FORMATS:
        return

    effective_services = (
        patch_data["services"]
        if "services" in patch_data
        else (row.services if row else [])
    ) or []
    allowed = (
        ["online"]
        if clinic_service_format == "online"
        else (
            ["presential"]
            if clinic_service_format == "presential"
            else ["online", "presential", "both"]
        )
    )
    conflicts = [
        {"index": index, "service_format": service_format}
        for index, service in enumerate(effective_services)
        if isinstance(service, dict)
        and (service_format := service.get("service_format")) is not None
        and service_format not in allowed
    ]
    if conflicts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "service_format_conflict",
                "message": "One or more service modalities are incompatible with the clinic service format.",
                "clinic_service_format": clinic_service_format,
                "allowed_service_formats": allowed,
                "conflicts": conflicts,
                "action": "Change the listed service modalities or broaden the clinic service format.",
            },
        )


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
    _add_effective_reactivation_fields(flat, row)
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
    # Do not create a row until modality validation has accepted the effective
    # patch.  A rejected first write must not flush, commit, or invalidate a
    # config cache entry.
    row = await _load_config(session, tenant)
    _validate_service_format_compatibility(patch_data, row)
    canonical_handoff_policy = _validate_handoff_policy_patch(patch_data, row)
    if row is None:
        row = _new_config(tenant)
        session.add(row)

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
            if bucket_name == "handoff" and not isinstance(existing, dict):
                existing = {}
            merged = {**existing, **cast(dict[str, Any], new_values)}
            if bucket_name == "handoff" and canonical_handoff_policy is not None:
                merged.update(canonical_handoff_policy)
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
    _add_effective_reactivation_fields(flat, row)
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


async def _ensure_meta_phone_number_id_available(
    session: AsyncSession,
    tenant_id: UUID,
    phone_number_id: object,
) -> None:
    """Reject a Meta phone-number ID already assigned to another tenant."""

    stmt = select(ClinicConfigTable.tenant_id).where(
        ClinicConfigTable.api_credentials["meta_phone_number_id"].astext
        == str(phone_number_id),
        ClinicConfigTable.tenant_id != tenant_id,
    )
    conflict = (await session.execute(stmt)).scalar_one_or_none()
    if conflict is None:
        return

    logger.warning(
        "meta_phone_number_id_conflict tenant=%s existing_tenant=%s",
        tenant_id,
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


@credentials_router.patch(
    "/api-credentials",
    summary="Update API Credentials",
    description=(
        "Validate a credential patch against the public provider catalog. "
        "Legacy STT incumbents may atomically leave outros for a fixed Scribe "
        "or OpenRouter bundle."
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
    resolved_existing = resolve_stt_credentials(existing)
    patched_phone_id = patch.get("meta_phone_number_id")
    if is_semantic_credentials_echo(existing, resolved_existing, patch):
        if patched_phone_id:
            await _ensure_meta_phone_number_id_available(
                session, tenant.tenant_id, patched_phone_id
            )
        return {"ok": True, **credential_status(existing)}
    try:
        creds = merge_credentials(existing, patch)
        validate_provider_availability(
            existing,
            creds,
            patch=patch,
            resolved_existing=resolved_existing,
        )
    except CredentialPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.detail,
        ) from exc

    stt_switch_provider = patch.get("stt_provider")
    is_stt_incumbent_switch = (
        stt_switch_provider in STT_FIXED_API_BASES
        and resolved_existing.get("stt_provider") == "outros"
        and creds.get("stt_provider") == stt_switch_provider
    )

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
        await _ensure_meta_phone_number_id_available(
            session, tenant.tenant_id, new_phone_id
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
    except Exception:
        await session.rollback()
        logger.error("api_credentials_commit_failed")
        raise

    # Invalidate Redis cache so next pipeline run picks up changes
    from src.services.event_service import invalidate_clinic_config_cache

    await invalidate_clinic_config_cache(tenant.tenant_id)

    if is_stt_incumbent_switch:
        try:
            _emit_api_credentials_changed(
                actor_id=tenant.user_id,
                actor_role=tenant.role,
                tenant_id=tenant.tenant_id,
                previous_provider="outros",
                new_provider=cast(str, stt_switch_provider),
            )
        except Exception:  # noqa: BLE001 - telemetry cannot undo the durable write
            try:
                API_CREDENTIALS_CHANGE_EMIT_FAILURES_TOTAL.inc()
            except Exception:  # noqa: BLE001 - metric failure cannot fail the request
                pass
            logger.error("api_credentials_change_emit_failed")

    return {"ok": True, **credential_status(creds)}


router.include_router(credentials_router)
