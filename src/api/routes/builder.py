"""
NeuroStack API - Builder Endpoints
Builder-scoped routes with SQL-level tenant isolation.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional
from typing import cast as type_cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from passlib.context import CryptContext
from pydantic import ValidationError
from sqlalchemy import and_, cast, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlalchemy.types import BigInteger, String

from src.api.helpers.health import (
    CONFIG_COMPLETENESS_BUCKETS as _CONFIG_COMPLETENESS_BUCKETS,
)
from src.api.helpers.health import (
    _compute_alerts,
    _compute_config_completeness,
    _compute_health_status,
    _get_time_series,
)
from src.api.helpers.password_validation import PasswordValidationRoute
from src.api.middleware.auth import TenantContext, get_tenant_context, require_builder
from src.api.schemas.auth import PasswordChangeRequest
from src.api.schemas.builder import (
    BuilderClinicResponse,
    BuilderDashboardResponse,
    BuilderProfileResponse,
    BuilderStatsResponse,
    BuilderSupportTicketResponse,
    ClinicCloneErrorResponse,
    ClinicClonePreviewResponse,
    ClinicCloneRequest,
    ClinicCloneResponse,
    CreateBuilderClinicRequest,
    ToggleClinicStatusRequest,
    UpdateBuilderClinicRequest,
    UpdateBuilderProfileRequest,
    UpdateClinicAdminEmailRequest,
    UpdateTicketStatusRequest,
)
from src.config.logging import get_logger
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.event import Event
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.support_ticket import SupportTicket
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories import get_db_session_dependency
from src.services.business_hours import summarize_outside_hours
from src.services.clinic_clone_service import (
    MEDIA_ASSET_CLONE_FIELDS,
    build_clinic_config_clone,
    build_knowledge_document_clone,
    build_media_asset_clone,
    derive_clone_slug,
    next_clone_name,
)
from src.services.media_assets import (
    MediaStorageCapacityExceeded,
    MediaStorageGuardError,
    media_storage_capacity_guard,
)
from src.services.trace_service import _is_conversational_trace, _llm_usage_for_stats

router = APIRouter()
password_router = APIRouter(route_class=PasswordValidationRoute)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)
logger = get_logger(__name__)

# Compatibility export used by dashboard consumers and tests.
CONFIG_COMPLETENESS_BUCKETS = _CONFIG_COMPLETENESS_BUCKETS

_CLONE_AUTHORITY_ERROR = {"error": "clinic_clone_forbidden"}
_CLONE_SOURCE_ERROR = {"error": "clinic_clone_source_not_found"}
_CLONE_CONFIG_ERROR = {"error": "clinic_clone_invalid"}
_CLONE_REQUEST_ERROR = {"error": "clinic_clone_request_invalid"}
_CLONE_MEDIA_CAP_ERROR = {"error": "clinic_clone_media_storage_cap_exceeded"}
_CLONE_PERSISTENCE_ERROR = {"error": "clinic_clone_persistence_failed"}
# A valid clone request is two boolean flags (< 100 bytes); anything above this
# bound is rejected with the same 422 before it is buffered.
_CLONE_REQUEST_MAX_BYTES = 1024
_MEDIA_CLONE_COLUMNS = tuple(
    getattr(MediaAsset, field) for field in MEDIA_ASSET_CLONE_FIELDS
)
_CLONE_REQUEST_BODY_DOC: dict[str, Any] = {
    "required": False,
    "description": (
        "Optional selective clone request. An absent body (zero bytes) or {} clones "
        "only the configuration. Any JSON present must be this object with boolean "
        "flags and no other fields; null, scalars, arrays, extra fields and "
        "non-boolean flags return 422 clinic_clone_request_invalid."
    ),
    "content": {
        "application/json": {"schema": ClinicCloneRequest.model_json_schema()},
    },
}


class _InvalidCloneConfigError(Exception):
    """Internal marker for redacted clone name/config validation failures."""


class _SourceMediaDriftError(Exception):
    """A share-locked source media row changed; never expected while locked."""


class _PersistedClone(NamedTuple):
    """Flushed clone identity plus the number of copied knowledge documents."""

    id: UUID
    name: str
    rag_documents: int


def _clone_error(status_code: int, detail: dict[str, str]) -> HTTPException:
    """Build a stable clone error without including internal values."""
    return HTTPException(status_code=status_code, detail=dict(detail))


async def _clone_request_body(request: Request) -> ClinicCloneRequest:
    """Materialize the default request only for a body with zero bytes.

    Typing the body as ``ClinicCloneRequest | None`` would collapse an absent
    body and JSON ``null`` after parsing, so absence is decided on the raw bytes
    first. Any JSON present (``null``, scalars, arrays, extra fields, non-boolean
    flags or malformed JSON) gets the same redacted 422 before any DB access.

    The read is bounded: a declared ``Content-Length`` above
    ``_CLONE_REQUEST_MAX_BYTES`` is rejected without reading, and a streamed
    (e.g. chunked) body is abandoned as soon as it exceeds that limit.
    """
    invalid_request = _clone_error(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        _CLONE_REQUEST_ERROR,
    )
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared_bytes = int(declared_length)
        except ValueError:
            raise invalid_request from None
        if declared_bytes > _CLONE_REQUEST_MAX_BYTES:
            raise invalid_request
    raw_body = bytearray()
    async for chunk in request.stream():
        raw_body += chunk
        if len(raw_body) > _CLONE_REQUEST_MAX_BYTES:
            raise invalid_request
    if not raw_body:
        return ClinicCloneRequest()
    try:
        return ClinicCloneRequest.model_validate_json(bytes(raw_body))
    except ValidationError:
        raise invalid_request from None


async def _rollback_clone_safely(session: AsyncSession) -> None:
    """Best-effort rollback that cannot replace a sanitized clone failure."""
    try:
        await session.rollback()
    except Exception:
        pass


async def _current_clone_builder(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    lock: bool,
) -> Builder:
    """Revalidate current Builder authority from DB state, optionally locking it."""
    if (
        tenant.api_key
        or tenant.user_id is None
        or tenant.role != UserRole.builder.value
        or tenant.builder_id is None
    ):
        raise _clone_error(status.HTTP_403_FORBIDDEN, _CLONE_AUTHORITY_ERROR)

    user_stmt = select(User).where(User.id == tenant.user_id)
    if lock:
        user_stmt = user_stmt.with_for_update()
    user_result = await session.execute(user_stmt)
    user = user_result.scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or user.role != UserRole.builder
        or user.tenant_id != tenant.tenant_id
        or user.builder_id is None
        or user.builder_id != tenant.builder_id
    ):
        raise _clone_error(status.HTTP_403_FORBIDDEN, _CLONE_AUTHORITY_ERROR)

    builder_stmt = select(Builder).where(Builder.id == user.builder_id)
    if lock:
        builder_stmt = builder_stmt.with_for_update()
    builder_result = await session.execute(builder_stmt)
    builder = builder_result.scalar_one_or_none()
    if builder is None or not builder.is_active or builder.status != "active":
        raise _clone_error(status.HTTP_403_FORBIDDEN, _CLONE_AUTHORITY_ERROR)
    return builder


async def _clone_source(
    session: AsyncSession,
    source_id: str,
    builder_id: UUID,
    *,
    lock: bool,
) -> Tenant:
    """Load a source only through its current Builder association."""
    try:
        parsed_source_id = UUID(source_id)
    except ValueError:
        raise _clone_error(status.HTTP_404_NOT_FOUND, _CLONE_SOURCE_ERROR) from None

    source_stmt = select(Tenant).where(
        Tenant.id == parsed_source_id,
        Tenant.builder_id == builder_id,
    )
    if lock:
        # The locked read must reflect the locked row even when an earlier
        # unlocked read already placed the source in the identity map.
        source_stmt = source_stmt.with_for_update().execution_options(
            populate_existing=True
        )
    source_result = await session.execute(source_stmt)
    source = source_result.scalar_one_or_none()
    if source is None:
        raise _clone_error(status.HTTP_404_NOT_FOUND, _CLONE_SOURCE_ERROR)
    return source


async def _persist_clone_records(
    session: AsyncSession,
    builder: Builder,
    source: Tenant,
    *,
    include_rag: bool,
) -> _PersistedClone:
    """Build and flush the inactive tenant, its configuration and selected RAG."""
    names_result = await session.execute(
        select(Tenant.name).where(Tenant.builder_id == builder.id)
    )
    try:
        clone_name = next_clone_name(
            source.name,
            list(names_result.scalars().all()),
        )
    except ValueError as error:
        raise _InvalidCloneConfigError from error

    config_result = await session.execute(
        select(ClinicConfigTable).where(ClinicConfigTable.tenant_id == source.id)
    )
    source_config = config_result.scalar_one_or_none()
    if source_config is None:
        raise _InvalidCloneConfigError
    try:
        config_payload = build_clinic_config_clone(source_config, clone_name)
    except ValueError as error:
        raise _InvalidCloneConfigError from error

    # Knowledge documents are read only when the caller selected them.
    source_documents: list[KnowledgeDocument] = []
    if include_rag:
        documents_result = await session.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.tenant_id == source.id)
            .order_by(KnowledgeDocument.id)
        )
        source_documents = list(documents_result.scalars().all())

    clone_id = uuid4()
    cloned_documents = [
        build_knowledge_document_clone(document, clone_id)
        for document in source_documents
    ]
    cloned_tenant = Tenant(
        id=clone_id,
        name=clone_name,
        slug=derive_clone_slug(clone_name, clone_id),
        api_key_hash=pwd_context.hash(secrets.token_urlsafe(32)),
        is_active=False,
        plan_type="clinic",
        builder_id=builder.id,
        contact_phone=None,
        config={},
    )
    cloned_config = ClinicConfigTable(
        id=uuid4(),
        tenant_id=clone_id,
        **config_payload,
    )

    session.add(cloned_tenant)
    session.add(cloned_config)
    session.add_all(cloned_documents)
    await session.flush()
    return _PersistedClone(clone_id, clone_name, len(cloned_documents))


async def _stabilize_source_media(
    session: AsyncSession,
    source_id: UUID,
) -> tuple[tuple[UUID, int], ...]:
    """Share-lock and return the complete payload-free source media set.

    The whole result is consumed before the cap is validated, so every source
    row is locked (PATCH/DELETE wait for this transaction) and the exact source
    total is the sum of these locked sizes.
    """
    rows = (
        await session.execute(
            select(MediaAsset.id, MediaAsset.size_bytes)
            .where(MediaAsset.tenant_id == source_id)
            .order_by(MediaAsset.id)
            .with_for_update(read=True)
        )
    ).all()
    return tuple((row.id, int(row.size_bytes)) for row in rows)


async def _copy_source_media(
    session: AsyncSession,
    source_id: UUID,
    clone_id: UUID,
    source_media: tuple[tuple[UUID, int], ...],
) -> int:
    """Copy the stabilized media set holding at most one payload at a time.

    Every local reference to a row's payload (the fetched row and its copy) is
    dropped once that copy is flushed and detached, so payload N is released
    before payload N+1 is fetched.
    """
    for media_id, size_bytes in source_media:
        source_row = (
            await session.execute(
                select(*_MEDIA_CLONE_COLUMNS).where(
                    MediaAsset.id == media_id,
                    MediaAsset.tenant_id == source_id,
                )
            )
        ).one_or_none()
        if source_row is None or source_row.size_bytes != size_bytes:
            raise _SourceMediaDriftError
        media_clone = build_media_asset_clone(source_row._asdict(), clone_id)
        del source_row
        session.add(media_clone)
        await session.flush()
        # The inserted row stays in this transaction; detaching the copy and
        # dropping the last local reference frees its payload before the next
        # fetch (the identity map alone would not keep it, but the local would).
        session.expunge(media_clone)
        del media_clone
    return len(source_media)


# ============================================
# Contact phone normalization
# ============================================


_CONTACT_PHONE_MIN_DIGITS = 10
_CONTACT_PHONE_MAX_DIGITS = 15


def _normalize_contact_phone(value: Optional[str]) -> Optional[str]:
    """Normalize a builder-supplied clinic contact phone.

    - `None` propagates as `None` (caller decides what that means).
    - Empty or whitespace-only string collapses to `None` (clear the field).
    - Otherwise, strip all non-digit characters and require 10-15 digits;
      raise HTTP 400 when the digit count is out of range.
    """
    if value is None:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits == "":
        return None
    if not (_CONTACT_PHONE_MIN_DIGITS <= len(digits) <= _CONTACT_PHONE_MAX_DIGITS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="contact_phone must contain between 10 and 15 digits",
        )
    return digits


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
) -> BuilderProfileResponse:
    # Fetch builder
    result = await session.execute(select(Builder).where(Builder.id == builder_id))
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
        support_email=builder.support_email,
    )


# ============================================
# GET /profile - Builder profile (with support_email)
# PATCH /profile - Update builder support_email
# ============================================


@router.get(
    "/profile",
    summary="Builder Profile (with support_email)",
    description=(
        "Return the authenticated builder's profile. Includes `support_email`, "
        "the escalation address used to route clinic support tickets."
    ),
    response_model=BuilderProfileResponse,
)
async def get_builder_profile(
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BuilderProfileResponse:
    result = await session.execute(select(Builder).where(Builder.id == builder_id))
    builder = result.scalar_one_or_none()
    if not builder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder not found",
        )

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
        support_email=builder.support_email,
    )


@router.patch(
    "/profile",
    summary="Update Builder Profile",
    description=(
        "Update the authenticated builder's own profile. Currently supports "
        "`support_email` (null or empty string clears the field). Never accepts "
        "a builder id from the request body; always writes to the JWT's own builder."
    ),
    response_model=BuilderProfileResponse,
)
async def update_builder_profile(
    body: UpdateBuilderProfileRequest,
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> BuilderProfileResponse:
    result = await session.execute(select(Builder).where(Builder.id == builder_id))
    builder = result.scalar_one_or_none()
    if not builder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder not found",
        )

    # The schema validator has already normalized empty/whitespace to None
    # and validated non-empty values as EmailStr.
    builder.support_email = body.support_email
    await session.flush()
    await session.commit()

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
        support_email=builder.support_email,
    )


# ============================================
# GET /clinics - List builder's clinics (enriched)
# ============================================


def _builder_access_forbidden() -> HTTPException:
    """Return the stable response for an invalid or stale Builder identity."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "detail": (
                "Builder profile not found. Contact admin to enable builder access."
            ),
            "error_code": "BUILDER_NOT_CONFIGURED",
        },
    )


async def _validated_current_builder_id(
    session: AsyncSession,
    tenant: TenantContext,
    claimed_builder_id: UUID,
) -> UUID:
    """Revalidate the current Builder identity before exposing clinic data."""
    if (
        tenant.api_key
        or tenant.user_id is None
        or tenant.role != UserRole.builder.value
        or tenant.builder_id != claimed_builder_id
    ):
        raise _builder_access_forbidden()

    current_builder_id = await session.scalar(
        select(User.builder_id)
        .join(Builder, Builder.id == User.builder_id)
        .where(
            User.id == tenant.user_id,
            User.is_active.is_(True),
            cast(User.role, String) == UserRole.builder.value,
            User.tenant_id == tenant.tenant_id,
            User.builder_id == claimed_builder_id,
            Builder.is_active.is_(True),
            Builder.status == "active",
        )
    )
    if current_builder_id is None:
        raise _builder_access_forbidden()
    return current_builder_id


@router.get(
    "/clinics",
    summary="List Builder Clinics",
    description="Return clinics owned by the authenticated builder with health metrics (SQL-level scope isolation).",
    response_model=None,
)
async def list_clinics(
    builder_id: UUID = Depends(require_builder),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[dict[str, Any]]:
    builder_id = await _validated_current_builder_id(session, tenant, builder_id)

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

    # Batch query: expose an email only when the clinic has exactly one admin.
    admin_email_stmt = (
        select(
            User.tenant_id,
            func.count(User.id).label("admin_count"),
            func.min(User.email).label("admin_email"),
        )
        .where(
            User.tenant_id.in_(tenant_ids),
            cast(User.role, String) == UserRole.clinic_admin.value,
        )
        .group_by(User.tenant_id)
    )
    admin_email_result = await session.execute(admin_email_stmt)
    admin_email_map: dict[UUID, Optional[str]] = {
        row.tenant_id: row.admin_email if row.admin_count == 1 else None
        for row in admin_email_result
    }

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

    # Batch query: trace_count_7d per tenant
    trace_7d_stmt = (
        select(
            Event.tenant_id,
            func.count(Event.id).label("count"),
        )
        .where(
            Event.tenant_id.in_(tenant_ids),
            Event.event_type == "message.in",
            Event.created_at >= seven_days_ago,
        )
        .group_by(Event.tenant_id)
    )
    trace_7d_result = await session.execute(trace_7d_stmt)
    trace_7d_map: dict[UUID, int] = {row[0]: row[1] for row in trace_7d_result}

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
    safety_map: dict[UUID, int] = {row[0]: row[1] for row in safety_result}

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
    handoff_map: dict[UUID, int] = {row[0]: row[1] for row in handoff_result}

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

    clinics: list[dict[str, Any]] = []
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
                contact_phone=t.contact_phone,
                admin_email=admin_email_map.get(t.id),
                last_trace_at=last_trace,
                health_status=health,
                trace_count_7d=trace_7d_map.get(t.id, 0),
                safety_flags_7d=safety_map.get(t.id, 0),
                handoff_count_7d=handoff_map.get(t.id, 0),
                outside_hours_count_7d=type_cast(
                    int, outside_hours["outside_hours_count"]
                ),
                outside_hours_pct_7d=outside_hours["outside_hours_pct"],
                conversational_trace_count_7d=len(conversational_created_ats[t.id]),
                business_hours_configured=type_cast(
                    bool, outside_hours["business_hours_configured"]
                ),
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
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_clinic(
    body: CreateBuilderClinicRequest,
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    # Check email uniqueness
    existing = await session.execute(select(User).where(User.email == body.admin_email))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already in use",
        )

    # Normalize (and validate) the contact phone before we touch the DB.
    normalized_contact_phone = _normalize_contact_phone(body.contact_phone)

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
        contact_phone=normalized_contact_phone,
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
    # get_db_session_dependency requires an explicit commit for writes to persist.
    await session.commit()

    return {"ok": True, "id": str(tenant_id)}


# ============================================
# GET /clinics/{source_id}/clone-preview - Advisory clone summary
# POST /clinics/{source_id}/clone - Atomic clinic clone
# ============================================


@router.get(
    "/clinics/{source_id}/clone-preview",
    response_model=ClinicClonePreviewResponse,
    summary="Preview Clinic Clone",
    description=(
        "Return an advisory generated name plus the knowledge (RAG) and media "
        "counts/bytes available in a currently Builder-owned source clinic. The "
        "aggregates describe the whole source, independent of the flags later sent "
        "to the clone request; content and payloads are never selected."
    ),
    responses={
        status.HTTP_403_FORBIDDEN: {"model": ClinicCloneErrorResponse},
        status.HTTP_404_NOT_FOUND: {"model": ClinicCloneErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ClinicCloneErrorResponse},
    },
)
async def preview_clinic_clone(
    source_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> ClinicClonePreviewResponse:
    builder = await _current_clone_builder(session, tenant, lock=False)
    source = await _clone_source(session, source_id, builder.id, lock=False)

    names_result = await session.execute(
        select(Tenant.name).where(Tenant.builder_id == builder.id)
    )
    existing_names = list(names_result.scalars().all())
    try:
        suggested_name = next_clone_name(source.name, existing_names)
    except ValueError:
        raise _clone_error(status.HTTP_409_CONFLICT, _CLONE_CONFIG_ERROR) from None

    # Separate, explicitly typed and zero-safe aggregates computed by the
    # database; neither query transfers document content nor media payloads.
    document_count, total_bytes = (
        await session.execute(
            select(
                cast(func.coalesce(func.count(KnowledgeDocument.id), 0), BigInteger),
                cast(
                    func.coalesce(
                        func.sum(func.octet_length(KnowledgeDocument.content)), 0
                    ),
                    BigInteger,
                ),
            ).where(KnowledgeDocument.tenant_id == source.id)
        )
    ).one()
    media_asset_count, media_total_bytes = (
        await session.execute(
            select(
                cast(func.coalesce(func.count(MediaAsset.id), 0), BigInteger),
                cast(func.coalesce(func.sum(MediaAsset.size_bytes), 0), BigInteger),
            ).where(MediaAsset.tenant_id == source.id)
        )
    ).one()
    return ClinicClonePreviewResponse(
        suggested_name=suggested_name,
        document_count=int(document_count),
        total_bytes=int(total_bytes),
        media_asset_count=int(media_asset_count),
        media_total_bytes=int(media_total_bytes),
    )


@router.post(
    "/clinics/{source_id}/clone",
    response_model=ClinicCloneResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Clone Clinic",
    description=(
        "Atomically create an inactive clinic clone after revalidating and locking "
        "current Builder authority and source ownership. The allowlisted "
        "configuration is always copied with API credentials emptied and the "
        "scheduling link disabled. Knowledge documents and media assets are copied "
        "only when include_rag/include_media are true: an absent body or {} creates "
        "a configuration-only clone. Media copies share the owner's 500,000,000-byte "
        "storage cap with uploads. The response reports the counts actually copied."
    ),
    responses={
        status.HTTP_403_FORBIDDEN: {"model": ClinicCloneErrorResponse},
        status.HTTP_404_NOT_FOUND: {"model": ClinicCloneErrorResponse},
        status.HTTP_409_CONFLICT: {
            "model": ClinicCloneErrorResponse,
            "description": (
                "The clone name or configuration is invalid (clinic_clone_invalid) "
                "or the copied media would exceed the owner's storage cap "
                "(clinic_clone_media_storage_cap_exceeded). Nothing is created."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ClinicCloneErrorResponse,
            "description": (
                "A request body is present but is not a strict ClinicCloneRequest "
                "(clinic_clone_request_invalid). Nothing is created."
            ),
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ClinicCloneErrorResponse},
    },
    openapi_extra={"requestBody": _CLONE_REQUEST_BODY_DOC},
)
async def clone_clinic(
    source_id: str,
    clone_request: ClinicCloneRequest = Depends(_clone_request_body),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> ClinicCloneResponse:
    copied_media_assets = 0
    # Guard errors before entry mean the source changed owner or disappeared
    # (404); any other guard error after entry is an unexpected misuse (500).
    guard_entered = False
    # Identifiers only, for the redacted failure warnings below.
    failure_ids: dict[str, str] = {}
    try:
        # Single lock order shared with media uploads: User -> Builder -> owner
        # advisory (media only) -> source Tenant -> source media rows. The
        # configuration-only and RAG-only branches never enter the media storage
        # guard and never take its advisory lock.
        builder = await _current_clone_builder(session, tenant, lock=True)
        failure_ids["builder_id"] = str(builder.id)
        if clone_request.include_media:
            unlocked_source = await _clone_source(
                session, source_id, builder.id, lock=False
            )
            failure_ids["source_tenant_id"] = str(unlocked_source.id)
            async with media_storage_capacity_guard(
                session,
                tenant.user_id,
                unlocked_source,
            ) as capacity:
                guard_entered = True
                source = await _clone_source(session, source_id, builder.id, lock=True)
                source_media = await _stabilize_source_media(session, source.id)
                await capacity.validate_projection(
                    sum(size_bytes for _, size_bytes in source_media)
                )
                persisted = await _persist_clone_records(
                    session,
                    builder,
                    source,
                    include_rag=clone_request.include_rag,
                )
                copied_media_assets = await _copy_source_media(
                    session,
                    source.id,
                    persisted.id,
                    source_media,
                )
        else:
            source = await _clone_source(session, source_id, builder.id, lock=True)
            persisted = await _persist_clone_records(
                session,
                builder,
                source,
                include_rag=clone_request.include_rag,
            )
        # Identifiers, the two selection flags and final counts only: never
        # configuration, content, payloads, filenames, triggers, hashes or secrets.
        log_fields: dict[str, str | bool | int] = {
            "builder_id": str(builder.id),
            "source_tenant_id": str(source.id),
            "clone_tenant_id": str(persisted.id),
            "include_rag": clone_request.include_rag,
            "include_media": clone_request.include_media,
            "copied_rag_documents": persisted.rag_documents,
            "copied_media_assets": copied_media_assets,
        }
        await session.commit()
    except HTTPException:
        await _rollback_clone_safely(session)
        raise
    except _InvalidCloneConfigError:
        await _rollback_clone_safely(session)
        raise _clone_error(status.HTTP_409_CONFLICT, _CLONE_CONFIG_ERROR) from None
    except MediaStorageCapacityExceeded:
        await _rollback_clone_safely(session)
        raise _clone_error(status.HTTP_409_CONFLICT, _CLONE_MEDIA_CAP_ERROR) from None
    except MediaStorageGuardError:
        await _rollback_clone_safely(session)
        if not guard_entered:
            # The source changed owner or disappeared between its resolution and
            # the guard's row lock: indistinguishable from any other
            # inaccessible source.
            raise _clone_error(status.HTTP_404_NOT_FOUND, _CLONE_SOURCE_ERROR) from None
        logger.warning(
            "clinic_clone_failed", reason="guard_unexpected_error", **failure_ids
        )
        raise _clone_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            _CLONE_PERSISTENCE_ERROR,
        ) from None
    except _SourceMediaDriftError:
        await _rollback_clone_safely(session)
        logger.warning(
            "clinic_clone_failed", reason="source_media_drift", **failure_ids
        )
        raise _clone_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            _CLONE_PERSISTENCE_ERROR,
        ) from None
    except SQLAlchemyError:
        await _rollback_clone_safely(session)
        raise _clone_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            _CLONE_PERSISTENCE_ERROR,
        ) from None
    except Exception:
        await _rollback_clone_safely(session)
        raise

    logger.info("clinic_clone_created", **log_fields)
    return ClinicCloneResponse(
        id=persisted.id,
        name=persisted.name,
        is_active=False,
        copied_rag_documents=persisted.rag_documents,
        copied_media_assets=copied_media_assets,
    )


# ============================================
# PATCH /clinics/{id}/status - Toggle clinic status
# ============================================


@router.patch(
    "/clinics/{clinic_id}/status",
    summary="Toggle Clinic Status",
    description="Toggle a clinic's active/inactive status. Only works for clinics owned by this builder.",
    response_model=None,
)
async def toggle_clinic_status(
    clinic_id: str,
    body: ToggleClinicStatusRequest,
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
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
    await session.commit()

    return {"ok": True, "is_active": tenant.is_active}


# ============================================
# PATCH /clinics/{id} - Update clinic contact phone
# ============================================


@router.patch(
    "/clinics/{clinic_id}",
    summary="Update Clinic",
    description=(
        "Update editable clinic fields. Currently supports `contact_phone` "
        "(the WhatsApp contact number surfaced on support screens). "
        "Absent/`null` `contact_phone` leaves the field untouched; an empty "
        "string clears it; otherwise the value is normalized to digits-only "
        "and must be 10-15 digits long. SQL-level scope isolation restricts "
        "the write to the JWT builder's own clinics."
    ),
    response_model=None,
)
async def update_clinic(
    clinic_id: str,
    body: UpdateBuilderClinicRequest,
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    from uuid import UUID as _UUID

    try:
        cid = _UUID(clinic_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clinic ID")

    # Validate/normalize BEFORE scoping fetch so a bad payload never masquerades
    # as a scope miss, but AFTER the UUID parse so we return the same 400 shape
    # as toggle_clinic_status for malformed IDs.
    touch_contact_phone = "contact_phone" in body.model_fields_set
    normalized_contact_phone: Optional[str] = None
    if touch_contact_phone:
        normalized_contact_phone = _normalize_contact_phone(body.contact_phone)

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

    if touch_contact_phone:
        tenant.contact_phone = normalized_contact_phone

    await session.flush()
    await session.commit()

    return {"ok": True, "id": str(tenant.id), "contact_phone": tenant.contact_phone}


def _password_change_forbidden() -> HTTPException:
    """Return the stable response for a stale or non-builder caller."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "password_change_forbidden"},
    )


class _LinkedClinicAdminSnapshot(NamedTuple):
    """Current, database-backed target selected for a delegated credential change."""

    user_id: UUID
    email: str


async def _linked_clinic_admin_snapshot(
    session: AsyncSession,
    tenant: TenantContext,
    clinic_id: str,
) -> _LinkedClinicAdminSnapshot:
    """Authorize a Builder and resolve exactly one linked clinic administrator."""
    if tenant.api_key or tenant.user_id is None:
        raise _password_change_forbidden()

    try:
        requested_clinic_id = UUID(clinic_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "clinic_not_available"},
        ) from None

    builder_user = aliased(User)
    clinic_admin = aliased(User)
    authorization_snapshot = (
        select(
            builder_user.tenant_id.label("builder_tenant_id"),
            builder_user.role.label("builder_role"),
            builder_user.is_active.label("builder_user_active"),
            builder_user.builder_id.label("current_builder_id"),
            Builder.id.label("active_builder_id"),
            Tenant.id.label("clinic_tenant_id"),
            func.count(clinic_admin.id).label("clinic_admin_count"),
            func.min(cast(clinic_admin.id, String)).label("clinic_admin_id"),
            func.min(clinic_admin.email).label("clinic_admin_email"),
        )
        .select_from(builder_user)
        .outerjoin(
            Builder,
            and_(
                Builder.id == builder_user.builder_id,
                Builder.is_active.is_(True),
                Builder.status == "active",
            ),
        )
        .outerjoin(
            Tenant,
            and_(
                Tenant.id == requested_clinic_id,
                Tenant.builder_id == builder_user.builder_id,
            ),
        )
        .outerjoin(
            clinic_admin,
            and_(
                clinic_admin.tenant_id == Tenant.id,
                cast(clinic_admin.role, String) == UserRole.clinic_admin.value,
            ),
        )
        .where(builder_user.id == tenant.user_id)
        .group_by(
            builder_user.tenant_id,
            builder_user.role,
            builder_user.is_active,
            builder_user.builder_id,
            Builder.id,
            Tenant.id,
        )
    )
    snapshot_result = await session.execute(authorization_snapshot)
    snapshot = snapshot_result.mappings().one_or_none()

    if (
        snapshot is None
        or not snapshot["builder_user_active"]
        or snapshot["builder_role"] != UserRole.builder
        or tenant.role != UserRole.builder.value
        or snapshot["builder_tenant_id"] != tenant.tenant_id
        or snapshot["current_builder_id"] is None
        or snapshot["current_builder_id"] != tenant.builder_id
        or snapshot["active_builder_id"] is None
    ):
        raise _password_change_forbidden()

    if snapshot["clinic_tenant_id"] is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "clinic_not_available"},
        )

    if (
        snapshot["clinic_admin_count"] != 1
        or snapshot["clinic_admin_id"] is None
        or snapshot["clinic_admin_email"] is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "clinic_admin_unavailable"},
        )

    return _LinkedClinicAdminSnapshot(
        user_id=UUID(str(snapshot["clinic_admin_id"])),
        email=str(snapshot["clinic_admin_email"]),
    )


@password_router.patch(
    "/clinics/{clinic_id}/password",
    status_code=status.HTTP_200_OK,
    summary="Reset Linked Clinic Password",
    description="Reset the one clinic administrator password for a currently linked clinic.",
    responses={
        200: {"description": "Password changed"},
        401: {"description": "Missing, invalid, or expired Bearer JWT"},
        403: {"description": "Password change is not authorized"},
        404: {"description": "Clinic is not available to this builder"},
        409: {"description": "Clinic does not have exactly one administrator"},
        422: {"description": "Invalid password payload without echoed input values"},
    },
)
async def reset_linked_clinic_password(
    clinic_id: str,
    body: PasswordChangeRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, bool]:
    """Reset a linked clinic password using one authorization snapshot query."""
    snapshot = await _linked_clinic_admin_snapshot(session, tenant, clinic_id)

    await session.execute(
        update(User)
        .where(User.id == snapshot.user_id)
        .values(password_hash=pwd_context.hash(body.new_password))
    )
    await session.commit()
    return {"ok": True}


@router.patch(
    "/clinics/{clinic_id}/email",
    status_code=status.HTTP_200_OK,
    summary="Change Linked Clinic Login Email",
    description="Change the one clinic administrator email for a currently linked clinic.",
    responses={
        200: {"description": "Email changed"},
        401: {"description": "Missing, invalid, or expired Bearer JWT"},
        403: {"description": "Credential change is not authorized"},
        404: {"description": "Clinic is not available to this builder"},
        409: {"description": "Clinic administrator or email is unavailable"},
        422: {"description": "Invalid email payload"},
    },
)
async def update_linked_clinic_email(
    clinic_id: str,
    body: UpdateClinicAdminEmailRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    """Change a linked clinic login email using the shared authority snapshot."""
    snapshot = await _linked_clinic_admin_snapshot(session, tenant, clinic_id)
    stored_email = str(body.email)
    if stored_email == snapshot.email:
        return {"ok": True, "admin_email": snapshot.email}

    conflicting_user_id = await session.scalar(
        select(User.id).where(
            User.email == stored_email,
            User.id != snapshot.user_id,
        )
    )
    if conflicting_user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "email_already_in_use"},
        )

    try:
        await session.execute(
            update(User).where(User.id == snapshot.user_id).values(email=stored_email)
        )
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "email_already_in_use"},
        ) from None

    return {"ok": True, "admin_email": stored_email}


# The parent builder router retains all existing endpoints; the narrow subrouter
# gives password payload validation its own redaction behavior.
router.include_router(password_router)


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
) -> BuilderStatsResponse:
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
        select(func.count(Event.id)).where(
            Event.tenant_id.in_(tenant_ids_stmt),
            Event.event_type == "message.in",
        )
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
        select(func.count(Event.id)).where(
            Event.tenant_id.in_(tenant_ids_stmt),
            Event.event_type == "message.in",
        )
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
        tokens, llm_model_entries = _llm_usage_for_stats(metadata)
        if tokens:
            total_tokens += tokens
        for llm_model, model_tokens_used in llm_model_entries:
            model_tokens[llm_model] = model_tokens.get(llm_model, 0) + model_tokens_used
        # Track audio transcriptions (whisper) — use audio_seconds as the metric unit
        whisper_model = metadata.get("whisper_model")
        if whisper_model and metadata.get("is_audio"):
            audio_sec = int(metadata.get("audio_seconds") or 0)
            model_tokens[f"🎙️ {whisper_model}"] = model_tokens.get(
                f"🎙️ {whisper_model}", 0
            ) + max(audio_sec, 1)

    model_breakdown: list[dict[str, object]] = [
        {"model": model, "tokens": toks}
        for model, toks in sorted(
            model_tokens.items(), key=lambda x: x[1], reverse=True
        )
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
) -> BuilderDashboardResponse:
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


# ============================================
# GET /tickets - List builder-scoped support tickets
# PATCH /tickets/{id}/status - Update ticket status (builder-scoped)
# ============================================


_ALLOWED_TICKET_STATUSES = {"open", "closed"}


@router.get(
    "/tickets",
    summary="List Builder Support Tickets",
    description=(
        "Return support tickets routed to the authenticated builder "
        "(`routed_to_builder_id == jwt.builder_id`). Includes `clinic_name` "
        "and `contact_phone` joined from the originating tenant. Ordered by "
        "`created_at` desc. Optional `?status=open|closed` filter; any other "
        "value is rejected with 400."
    ),
    response_model=list[BuilderSupportTicketResponse],
)
async def list_builder_tickets(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[BuilderSupportTicketResponse]:
    if status_filter is not None and status_filter not in _ALLOWED_TICKET_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="status must be 'open' or 'closed'",
        )

    # SQL-level scope isolation: only tickets routed_to_builder_id == JWT builder_id.
    # Nulls (central-routed) and other builders' tickets are excluded by the WHERE.
    stmt = (
        select(
            SupportTicket,
            Tenant.name.label("tenant_name"),
            Tenant.contact_phone.label("tenant_contact_phone"),
        )
        .select_from(SupportTicket)
        .outerjoin(Tenant, Tenant.id == SupportTicket.tenant_id)
        .where(SupportTicket.routed_to_builder_id == builder_id)
        .order_by(SupportTicket.created_at.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(SupportTicket.status == status_filter)

    result = await session.execute(stmt)
    rows = result.all()

    return [
        BuilderSupportTicketResponse(
            id=ticket.id,
            clinic_id=ticket.tenant_id,
            clinic_name=tenant_name if tenant_name is not None else ticket.clinic_name,
            contact_phone=tenant_contact_phone,
            email=ticket.email,
            tipo=ticket.tipo,
            mensagem=ticket.mensagem,
            filename=ticket.filename,
            file_type=ticket.file_type,
            status=ticket.status,
            created_at=ticket.created_at,
        )
        for ticket, tenant_name, tenant_contact_phone in rows
    ]


@router.patch(
    "/tickets/{ticket_id}/status",
    summary="Update Builder Ticket Status",
    description=(
        "Update `status` (`open`|`closed`) for a support ticket routed to the "
        "authenticated builder. SQL-level scope isolation: filter is "
        "`id = :ticket_id AND routed_to_builder_id = :jwt_builder_id`; a mismatch "
        "returns 404 without touching the row. Any status value outside "
        "`open`/`closed` returns 400."
    ),
    response_model=None,
)
async def update_builder_ticket_status(
    ticket_id: int,
    body: UpdateTicketStatusRequest,
    builder_id: UUID = Depends(require_builder),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    if body.status not in _ALLOWED_TICKET_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="status must be 'open' or 'closed'",
        )

    # SQL-level scope isolation: WHERE id = :ticket_id AND routed_to_builder_id = :builder_id.
    # A 404 here means either "no such ticket" or "belongs to another builder";
    # we don't leak which so cross-builder existence can't be probed.
    result = await session.execute(
        select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.routed_to_builder_id == builder_id,
        )
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )

    ticket.status = body.status
    # get_db_session_dependency never auto-commits; the write is silently lost
    # in real Postgres deployments without this explicit commit.
    await session.commit()

    return {"ok": True, "id": ticket.id, "status": ticket.status}
