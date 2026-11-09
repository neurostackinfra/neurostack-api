"""Authorized tenant-scoped Media Assets catalog endpoints.

The per-category size limits (image 5 MB, PDF 20 MB, audio/video 16 MB) hold in
every credit mode, so the published contract never depends on the rollout
state.  M4A is stored as ``audio/mp4`` under the audio limit.

Credits (neurostack-credit-system Group 4a, :mod:`src.services.credit_inventory`).
With the credit switch off nothing changes.  With it on and before
``credit_enforcement_at``, uploads and downloads keep ``main``'s allowlist and
answers and are measured observationally after their own transaction.  After
the enforcement: an upload debits its media reservation in the same
transaction, and a download debits its egress overage in a short transaction
BEFORE it is answered; a standalone tenant or a quarantine hold is 402
``credit_pending`` before anything is stored or any byte is sent.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from python_multipart.multipart import parse_options_header
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Receive, Scope, Send

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    resolve_effective_tenant,
)
from src.api.middleware.media_limits import MEDIA_MULTIPART_MAX_BYTES, ImmediatePermit
from src.api.schemas.media_assets import (
    TRIGGER_INTENTS_SCHEMA,
    MediaAssetAuthErrorResponse,
    MediaAssetErrorResponse,
    MediaAssetForbiddenResponse,
    MediaAssetListResponse,
    MediaAssetPatchRequest,
    MediaAssetResponse,
    MediaAssetValidationErrorResponse,
    MediaBindableIntentsResponse,
    MediaTopic,
    media_category_byte_limits,
)
from src.config.logging import get_logger
from src.config.settings import settings
from src.interaction_context import InteractionOrigin, classify_interaction_origin
from src.models.clinic_config_table import ClinicConfigTable
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.repositories import get_db_session_dependency
from src.services import credit_inventory, credit_lifecycle
from src.services.api_credentials import (
    media_delivery_provider,
    media_delivery_provider_ready,
)
from src.services.credit_allocations import reservation_effect_id
from src.services.credit_metering import (
    CREDIT_ENFORCEMENT_UNAVAILABLE_CODE,
    CreditEnforcementUnavailable,
    CreditTenantGone,
)
from src.services.credit_switch import request_credit_switch
from src.services.egress_policy import agent_media_send_enabled
from src.services.media_assets import (
    MEDIA_AUDIO_VIDEO_MAX_BYTES,
    MEDIA_BINDABLE_INTENT_SEQUENCE,
    MEDIA_FILE_EXTENSIONS,
    MEDIA_FILE_MAX_BYTES,
    MEDIA_IMAGE_MAX_BYTES,
    MEDIA_IMPLICIT_INTENT_SEQUENCE,
    MEDIA_PDF_MAX_BYTES,
    MEDIA_TOPIC_INTENTS,
    MEDIA_TRIGGER_INTENTS_MAX_ITEMS,
    MediaAssetDomainError,
    MediaStorageCapacityExceeded,
    MediaStorageGuardError,
    media_payload_sha256,
    media_storage_capacity_guard,
    read_media_payload_limited,
    validate_media_classification,
    validate_media_metadata,
    validate_trigger_intents,
)
from src.services.media_send_body import sendable_mime_types
from src.services.tenant_classification import real_clinic_tenant_filter

router = APIRouter()
logger = get_logger(__name__)

_FILE_FIELD = "file"
_TEXT_FIELDS = frozenset(
    {"display_name", "original_filename", "usage_trigger", "auto_suggest"}
)
# The one repeatable multipart field: one part per bound intent, at most
# MEDIA_TRIGGER_INTENTS_MAX_ITEMS of them.
_TRIGGER_INTENTS_FIELD = "trigger_intents"
_CURSOR_VERSION = 1
_MEDIA_LIMIT_SUMMARY = (
    f"image {MEDIA_IMAGE_MAX_BYTES:,} bytes, PDF {MEDIA_PDF_MAX_BYTES:,} bytes, "
    f"audio and video {MEDIA_AUDIO_VIDEO_MAX_BYTES:,} bytes"
)
_METADATA_COLUMNS = (
    MediaAsset.id,
    MediaAsset.tenant_id,
    MediaAsset.display_name,
    MediaAsset.original_filename,
    MediaAsset.category,
    MediaAsset.mime_type,
    MediaAsset.size_bytes,
    MediaAsset.usage_trigger,
    MediaAsset.auto_suggest,
    MediaAsset.trigger_intents,
    MediaAsset.created_at,
    MediaAsset.updated_at,
)
# Name search folds case and the accents of Portuguese (and the few Spanish and
# French ones clinics use) on both sides, so "relatorio" finds "Relatório". The
# strings are applied with SQL ``translate`` after ``lower``; uppercase accented
# letters are listed too because ``lower`` leaves them untouched on a database
# whose ctype is C. Combining marks (U+0300-U+036F) have no counterpart, so
# ``translate`` deletes them: a name stored decomposed (NFD, as macOS hands file
# names over) folds to the same letters as its composed form.
_NAME_SEARCH_ACCENTED = "áàâãäåéèêëíìîïóòôõöúùûüçñÁÀÂÃÄÅÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑ"
_NAME_SEARCH_PLAIN = "aaaaaaeeeeiiiiooooouuuucnaaaaaaeeeeiiiiooooouuuucn"
_NAME_SEARCH_COMBINING = "".join(chr(code) for code in range(0x0300, 0x0370))
_NAME_SEARCH_TABLE = str.maketrans(
    _NAME_SEARCH_ACCENTED, _NAME_SEARCH_PLAIN, _NAME_SEARCH_COMBINING
)
_NAME_SEARCH_MAX_LENGTH = 200
MediaCategoryFilter = Literal["video", "audio", "pdf", "image"]
_MEDIA_SECURITY: list[dict[str, list[str]]] = [
    {"ApiKeyAuth": []},
    {"BearerAuth": []},
]
_TENANT_ID_DESCRIPTION = (
    "Effective clinic tenant. API keys and clinic administrators are restricted "
    "to their own tenant; builders must own an override; super administrators "
    "may select a clinic."
)
_AUTHENTICATION_RESPONSE = {
    "description": "Missing, expired or invalid API key or Bearer JWT.",
    "model": MediaAssetAuthErrorResponse,
}
_FORBIDDEN_RESPONSE = {
    "description": "Caller role or effective clinic tenant is not authorized.",
    "model": MediaAssetForbiddenResponse,
}
_VALIDATION_RESPONSE = {
    "description": (
        "Invalid path/query/JSON input or a stable media validation error such as "
        "media_multipart_invalid, media_metadata_invalid, "
        "media_trigger_intent_unknown, media_signature_invalid, "
        "media_type_unsupported, media_type_mismatch or, for an OGG-Opus that "
        "is not mono, media_audio_not_mono."
    ),
    "model": MediaAssetValidationErrorResponse,
}
_UNAVAILABLE_RESPONSE = {
    "description": (
        "Storage is unavailable or the immediate upload/download concurrency budget "
        "is exhausted (media_storage_unavailable or media_concurrency_exceeded)."
    ),
    "model": MediaAssetErrorResponse,
}


def _media_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


_CREDIT_PENDING_RESPONSE = {
    "description": (
        "After credit_enforcement_at only: the tenant has no credit account "
        "(standalone) or its builder is on a quarantine hold (credit_pending), "
        "or, on upload, its builder is exhausted and the upload needs new "
        "capacity (credit_category_exhausted), or, on an API key download, its "
        "builder is suspended after its 72 hours of debt (credit_suspended); "
        "nothing was stored and no byte was sent."
    ),
    "model": MediaAssetErrorResponse,
}


async def _credit_mode(
    request: Request, session: AsyncSession
) -> credit_inventory.CreditMode:
    """Read once, before any row is loaded (switch off: no SQL)."""

    try:
        return await credit_inventory.read_credit_mode(request, session)
    except CreditEnforcementUnavailable:
        raise _media_error(503, CREDIT_ENFORCEMENT_UNAVAILABLE_CODE) from None


async def _rollback(session: AsyncSession) -> None:
    await asyncio.shield(session.rollback())


def _cursor_payload(tenant_id: UUID, created_at: datetime, media_id: UUID) -> bytes:
    return json.dumps(
        {
            "v": _CURSOR_VERSION,
            "tenant_id": str(tenant_id),
            "created_at": created_at.isoformat(),
            "id": str(media_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _encode_cursor(tenant_id: UUID, created_at: datetime, media_id: UUID) -> str:
    payload = _cursor_payload(tenant_id, created_at, media_id)
    signature = hmac.new(
        settings.secret_key.encode("utf-8"), payload, hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str, tenant_id: UUID) -> tuple[datetime, UUID]:
    try:
        padding = "=" * (-len(cursor) % 4)
        signed = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        canonical = base64.urlsafe_b64encode(signed).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(cursor, canonical):
            raise ValueError("cursor encoding is not canonical")
        if len(signed) <= hashlib.sha256().digest_size:
            raise ValueError("cursor is too short")
        payload = signed[: -hashlib.sha256().digest_size]
        signature = signed[-hashlib.sha256().digest_size :]
        expected = hmac.new(
            settings.secret_key.encode("utf-8"), payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("cursor signature is invalid")
        values = json.loads(payload)
        if not isinstance(values, dict) or set(values) != {
            "v",
            "tenant_id",
            "created_at",
            "id",
        }:
            raise ValueError("cursor shape is invalid")
        if (
            values["v"] != _CURSOR_VERSION
            or not isinstance(values["tenant_id"], str)
            or not isinstance(values["created_at"], str)
            or not isinstance(values["id"], str)
            or UUID(values["tenant_id"]) != tenant_id
        ):
            raise ValueError("cursor tenant or version is invalid")
        created_at = datetime.fromisoformat(values["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return created_at, UUID(values["id"])
    except (
        binascii.Error,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise _media_error(400, "media_cursor_invalid") from error


def _name_search_pattern(name: str) -> str:
    """Fold *name* like the SQL side and make ``%``, ``_`` and ``\\`` literal."""
    folded = name.lower().translate(_NAME_SEARCH_TABLE)
    escaped = folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _folded_name(column: Any) -> Any:
    return func.translate(
        func.lower(column),
        _NAME_SEARCH_ACCENTED + _NAME_SEARCH_COMBINING,
        _NAME_SEARCH_PLAIN,
    )


def _metadata_values(row: Any) -> dict[str, Any]:
    return {column.key: getattr(row, column.key) for column in _METADATA_COLUMNS}


def _download_permit(request: Request) -> ImmediatePermit:
    permit = cast(
        ImmediatePermit | None,
        request.app.state.media_concurrency.downloads.try_acquire(),
    )
    if permit is None:
        raise _media_error(503, "media_concurrency_exceeded")
    return permit


class _PermitStreamingResponse(StreamingResponse):
    """Hold one download permit for the complete ASGI response lifecycle."""

    def __init__(self, *args: Any, permit: ImmediatePermit, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._permit = permit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._permit.release()


async def _single_chunk(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def _content_disposition(asset_id: UUID, original_filename: str, mime_type: str) -> str:
    extension = MEDIA_FILE_EXTENSIONS[mime_type]
    fallback = f"media-{asset_id}{extension}"
    encoded = quote(original_filename, safe="", encoding="utf-8", errors="strict")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


async def _parse_upload(
    request: Request,
) -> tuple[UploadFile, dict[str, str], list[str]]:
    """Parse exactly one file part, the four approved text fields (each at
    most once) and the repeated ``trigger_intents`` parts, in arrival order."""
    _, content_type_options = parse_options_header(
        request.headers.get("content-type", "")
    )
    declared_charset = content_type_options.get(b"charset")
    if declared_charset is not None:
        try:
            charset = declared_charset.decode("ascii")
            canonical_charset = codecs.lookup(charset).name
        except (LookupError, UnicodeDecodeError) as error:
            raise _media_error(422, "media_multipart_invalid") from error
        if canonical_charset not in {"ascii", "utf-8"}:
            raise _media_error(422, "media_multipart_invalid")

    try:
        form = await request.form(
            max_files=1,
            max_fields=len(_TEXT_FIELDS) + MEDIA_TRIGGER_INTENTS_MAX_ITEMS,
            max_part_size=1_000_000,
        )
    except (StarletteHTTPException, ValueError) as error:
        raise _media_error(422, "media_multipart_invalid") from error

    upload: UploadFile | None = None
    fields: dict[str, str] = {}
    trigger_intents: list[str] = []
    seen: set[str] = set()
    for key, value in form.multi_items():
        if key == _TRIGGER_INTENTS_FIELD:
            if (
                not isinstance(value, str)
                or len(trigger_intents) >= MEDIA_TRIGGER_INTENTS_MAX_ITEMS
            ):
                raise _media_error(422, "media_multipart_invalid")
            trigger_intents.append(value)
            continue
        if key in seen or key not in _TEXT_FIELDS | {_FILE_FIELD}:
            raise _media_error(422, "media_multipart_invalid")
        seen.add(key)
        if key == _FILE_FIELD:
            if not isinstance(value, UploadFile):
                raise _media_error(422, "media_multipart_invalid")
            upload = value
            continue
        if not isinstance(value, str):
            raise _media_error(422, "media_multipart_invalid")
        fields[key] = value

    if (
        upload is None
        or "display_name" not in fields
        or "original_filename" not in fields
    ):
        raise _media_error(422, "media_multipart_invalid")
    return upload, fields, trigger_intents


async def _authorized_target(
    request: Request,
    tenant: TenantContext,
    requested_tenant_id: UUID | None,
    session: AsyncSession,
) -> Tenant:
    effective_tenant_id = await resolve_effective_tenant(
        request,
        tenant,
        requested_tenant_id,
        session,
    )
    if tenant.role == "builder" and tenant.builder_id is None:
        raise _media_error(status.HTTP_403_FORBIDDEN, "media_tenant_forbidden")

    target_filters = [
        Tenant.id == effective_tenant_id,
        Tenant.is_active.is_(True),
        Tenant.plan_type != "admin",
        real_clinic_tenant_filter(),
    ]
    if tenant.role == "builder":
        assert tenant.builder_id is not None
        target_filters.append(Tenant.builder_id == tenant.builder_id)

    target = (
        await session.execute(select(Tenant).where(*target_filters))
    ).scalar_one_or_none()
    if target is None:
        raise _media_error(status.HTTP_403_FORBIDDEN, "media_tenant_forbidden")
    return target


def _authorized_media_target_exists(
    target_id: UUID,
    tenant: TenantContext,
) -> Any:
    """Revalidate the G5 target in the same statement that accesses media."""
    target_filters = [
        Tenant.id == target_id,
        Tenant.is_active.is_(True),
        Tenant.plan_type != "admin",
        real_clinic_tenant_filter(),
    ]
    if tenant.role == "builder":
        if tenant.builder_id is None:
            return Tenant.id.is_(None)
        target_filters.append(Tenant.builder_id == tenant.builder_id)
    return select(Tenant.id).where(*target_filters).exists()


@router.post(
    "",
    response_model=MediaAssetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload media asset",
    description=(
        "Persist exactly one file plus the approved metadata fields. The payload "
        f"limit depends on the derived category ({_MEDIA_LIMIT_SUMMARY}) and the "
        f"complete multipart request is limited to {MEDIA_MULTIPART_MAX_BYTES:,} "
        "bytes."
    ),
    responses={
        400: {
            "description": "Invalid or ambiguous Content-Length.",
            "model": MediaAssetErrorResponse,
        },
        401: _AUTHENTICATION_RESPONSE,
        402: _CREDIT_PENDING_RESPONSE,
        403: _FORBIDDEN_RESPONSE,
        409: {
            "description": (
                "The owner storage cap would be exceeded or its lock-time ownership "
                "changed (media_storage_cap_exceeded or media_storage_conflict)."
            ),
            "model": MediaAssetErrorResponse,
        },
        413: {
            "description": (
                "Multipart request or file payload exceeds its documented byte limit, "
                "including the per-category payload limit "
                "(media_request_size_exceeded or media_size_exceeded)."
            ),
            "model": MediaAssetErrorResponse,
        },
        422: _VALIDATION_RESPONSE,
        503: _UNAVAILABLE_RESPONSE,
    },
    openapi_extra={
        "security": _MEDIA_SECURITY,
        "requestBody": {
            "required": True,
            "description": (
                "Closed multipart body with exactly one file part, the four named "
                "metadata fields at most once each and trigger_intents repeated "
                "once per bound intent (unknown names are "
                "media_trigger_intent_unknown; duplicates are removed). Maximum "
                f"request size: {MEDIA_MULTIPART_MAX_BYTES:,} bytes."
            ),
            "x-maxBytes": MEDIA_MULTIPART_MAX_BYTES,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "maxProperties": 6,
                        "required": ["file", "display_name", "original_filename"],
                        "properties": {
                            "file": {
                                "type": "string",
                                "format": "binary",
                                "description": (
                                    "Exactly one MP4, MP3, OGG-Opus (mono only), M4A, "
                                    "PDF, JPEG or PNG file; the payload limit is the "
                                    "one of its derived category "
                                    f"({_MEDIA_LIMIT_SUMMARY})."
                                ),
                                "x-maxBytes": MEDIA_FILE_MAX_BYTES,
                            },
                            "display_name": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 200,
                            },
                            "original_filename": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 255,
                            },
                            "usage_trigger": {"type": "string", "maxLength": 500},
                            "auto_suggest": {
                                "type": "string",
                                "enum": ["true", "false"],
                                "default": "false",
                            },
                            "trigger_intents": TRIGGER_INTENTS_SCHEMA,
                        },
                    }
                }
            },
        },
    },
)
async def upload_media_asset(
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description=_TENANT_ID_DESCRIPTION,
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> MediaAsset:
    """Validate and atomically persist one tenant-scoped media asset."""
    upload: UploadFile | None = None
    target: Tenant | None = None
    try:
        mode = await _credit_mode(request, session)
        # Authorization and target classification deliberately precede multipart
        # parsing, validation and every access to media_assets.
        target = await _authorized_target(request, tenant, tenant_id, session)
        upload, fields, raw_trigger_intents = await _parse_upload(request)
        metadata = validate_media_metadata(
            display_name=fields["display_name"],
            original_filename=fields["original_filename"],
            usage_trigger=fields.get("usage_trigger"),
            auto_suggest=fields.get("auto_suggest"),
        )
        trigger_intents = validate_trigger_intents(raw_trigger_intents)
        payload = await read_media_payload_limited(upload)
        classification = validate_media_classification(
            payload,
            upload.content_type or "",
        )

        async with media_storage_capacity_guard(
            session,
            tenant.user_id,
            target,
        ) as capacity:
            await capacity.validate_projection(len(payload))
            asset = MediaAsset(
                tenant_id=target.id,
                display_name=metadata.display_name,
                original_filename=metadata.original_filename,
                category=classification.category,
                mime_type=classification.mime_type,
                size_bytes=len(payload),
                sha256=media_payload_sha256(payload),
                payload=payload,
                usage_trigger=metadata.usage_trigger,
                auto_suggest=metadata.auto_suggest,
                trigger_intents=trigger_intents,
            )
            session.add(asset)
            await session.flush()
            if mode.enforced:
                assert mode.enforcement_at is not None
                reserved = await credit_inventory.reserve_upload(
                    session,
                    tenant_id=target.id,
                    category="media",
                    object_ids=[asset.id],
                    upload_id=asset.id,
                    enforcement_at=mode.enforcement_at,
                )
                if reserved.status == "credit_pending":
                    raise credit_inventory.CreditPendingError()
                # Group 5: decided with the reservation, under the account
                # lock it took -- an exhausted builder adds no media that
                # needs new capacity (everything rolls back).
                if target.builder_id is not None and (
                    await credit_lifecycle.reservation_verdict(
                        session,
                        builder_id=target.builder_id,
                        effect_id=reservation_effect_id(target.id, "media", asset.id),
                        charged_mc=reserved.charged_mc,
                    )
                    == "category_exhausted"
                ):
                    raise _media_error(402, "credit_category_exhausted")
        await session.commit()
        logger.info(
            "media_asset_upload",
            tenant_id=str(target.id),
            media_id=str(asset.id),
            category=asset.category,
            size_bytes=asset.size_bytes,
            result="created",
        )
        if mode.observational:
            await credit_inventory.record_observational_upload(
                session,
                tenant_id=asset.tenant_id,
                category="media",
                object_ids=[asset.id],
                upload_id=asset.id,
            )
        # Group 4b's S3/Qdrant work is a post-commit, additive queue.  An
        # external-store outage must never turn a successful authoritative
        # PostgreSQL upload into an error or alter its credit reservation.
        try:
            await credit_inventory.enqueue_object_copy(
                session,
                source_kind="media",
                source_id=asset.id,
                tenant_id=asset.tenant_id,
                switch_on=mode.switch_on,
            )
        except Exception as error:
            logger.warning(
                "credit object-copy enqueue failed",
                alert="credit_object_copy",
                error_type=type(error).__name__,
            )
            await _rollback(session)
        return asset
    except credit_inventory.CreditPendingError:
        await _rollback(session)
        raise _media_error(402, "credit_pending") from None
    except CreditTenantGone:
        await _rollback(session)
        raise _media_error(
            status.HTTP_403_FORBIDDEN, "media_tenant_forbidden"
        ) from None
    except MediaAssetDomainError as error:
        await _rollback(session)
        status_code = 413 if error.code == "media_size_exceeded" else 422
        raise _media_error(status_code, error.code) from error
    except MediaStorageCapacityExceeded as error:
        await _rollback(session)
        raise _media_error(409, error.code) from error
    except MediaStorageGuardError as error:
        await _rollback(session)
        raise _media_error(409, "media_storage_conflict") from error
    except HTTPException:
        await _rollback(session)
        raise
    except SQLAlchemyError as error:
        await _rollback(session)
        raise _media_error(503, "media_storage_unavailable") from error
    except BaseException:
        await _rollback(session)
        raise
    finally:
        if upload is not None:
            await upload.close()


@router.get(
    "",
    response_model=MediaAssetListResponse,
    summary="List media assets",
    description=(
        "Return a descending keyset page of metadata only, optionally filtered by "
        "category and by a name substring. Binary payloads are never selected or "
        "returned by this operation."
    ),
    responses={
        400: {
            "description": "Cursor is malformed, tampered or belongs to another tenant.",
            "model": MediaAssetErrorResponse,
        },
        401: _AUTHENTICATION_RESPONSE,
        403: _FORBIDDEN_RESPONSE,
        422: _VALIDATION_RESPONSE,
        503: _UNAVAILABLE_RESPONSE,
    },
    openapi_extra={"security": _MEDIA_SECURITY},
)
async def list_media_assets(
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description=_TENANT_ID_DESCRIPTION,
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque tenant-bound cursor returned by the previous page.",
    ),
    page_size: int = Query(
        default=25,
        ge=1,
        le=100,
        description="Number of metadata records to return (default 25, maximum 100).",
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
    category: Annotated[
        MediaCategoryFilter | None,
        Query(
            description=(
                "Only return assets of this server-derived category. Omit for "
                "every category."
            ),
        ),
    ] = None,
    name: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=_NAME_SEARCH_MAX_LENGTH,
            pattern=r"^[^\x00\r\n]*$",
            description=(
                "Only return assets whose original_filename (the name the "
                "catalog shows, downloads and sends) contains this text, "
                "ignoring case and accents. % and _ match themselves; NUL, CR "
                "and LF are rejected. Keep the same filters when following "
                "next_cursor."
            ),
        ),
    ] = None,
) -> MediaAssetListResponse:
    """Return one descending keyset page without loading binary payloads."""
    try:
        target = await _authorized_target(request, tenant, tenant_id, session)
        cursor_position = (
            _decode_cursor(cursor, target.id) if cursor is not None else None
        )
        statement = select(*_METADATA_COLUMNS).where(
            MediaAsset.tenant_id == target.id,
            _authorized_media_target_exists(target.id, tenant),
        )
        if category is not None:
            statement = statement.where(MediaAsset.category == category)
        if name is not None:
            statement = statement.where(
                _folded_name(MediaAsset.original_filename).like(
                    _name_search_pattern(name), escape="\\"
                )
            )
        if cursor_position is not None:
            created_at, media_id = cursor_position
            statement = statement.where(
                or_(
                    MediaAsset.created_at < created_at,
                    and_(
                        MediaAsset.created_at == created_at,
                        MediaAsset.id < media_id,
                    ),
                )
            )
        rows = (
            await session.execute(
                statement.order_by(
                    MediaAsset.created_at.desc(),
                    MediaAsset.id.desc(),
                ).limit(page_size + 1)
            )
        ).all()
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _encode_cursor(target.id, last.created_at, last.id)
        logger.info(
            "media_asset_list",
            tenant_id=str(target.id),
            result="listed",
        )
        return MediaAssetListResponse(
            items=[
                MediaAssetResponse.model_validate(_metadata_values(row)) for row in page
            ],
            next_cursor=next_cursor,
            has_more=has_more,
        )
    except HTTPException:
        await _rollback(session)
        raise
    except SQLAlchemyError as error:
        await _rollback(session)
        raise _media_error(503, "media_storage_unavailable") from error
    except BaseException:
        await _rollback(session)
        raise


def _clinic_sendable_mime_types(credentials: object) -> list[str]:
    """Sorted MIME types the clinic's WhatsApp provider sends; [] for none."""

    # The provider map every webhook maps its own provider through
    # (``media_delivery_provider``): the screen, the selection and the
    # delivery name the same provider.
    delivery_provider = media_delivery_provider(
        credentials.get("whatsapp_provider") if isinstance(credentials, dict) else None
    )
    if delivery_provider is None:
        return []
    return sorted(sendable_mime_types(delivery_provider))


@router.get(
    "/bindable-intents",
    response_model=MediaBindableIntentsResponse,
    summary="List bindable intents and delivery state",
    description=(
        "Return the intents a media asset may be bound to (trigger_intents), "
        "the topics that group them on the screen (topics), the intents whose "
        "turns may send any automatic media (implicit_intents), "
        "whether the agent sends bound media for the effective clinic today "
        "(delivery_enabled), the byte limit of each category and the formats "
        "the clinic's WhatsApp provider sends (sendable_mime_types). Reads no "
        "media."
    ),
    responses={
        401: _AUTHENTICATION_RESPONSE,
        403: _FORBIDDEN_RESPONSE,
        422: _VALIDATION_RESPONSE,
        503: _UNAVAILABLE_RESPONSE,
    },
    openapi_extra={"security": _MEDIA_SECURITY},
)
async def get_media_bindable_intents(
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description=_TENANT_ID_DESCRIPTION,
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> MediaBindableIntentsResponse:
    """Publish the allowlist, the effective delivery state and the limits.

    ``delivery_enabled`` is the promise the Central de Mídias makes: true only
    when the clinic's WhatsApp provider delivers production turns
    (:func:`media_delivery_provider_ready`) and the ``agent_media_send_enabled``
    flag is effective.  The flag is read only for a ready provider; it never
    raises (closed on any doubt).  The credentials are revalidated against the
    authorized target in the same statement, like every media read: a target
    revoked in between reads no row and answers ``false``.

    ``sendable_mime_types`` comes from the same credentials and the senders'
    own set (:func:`sendable_mime_types`), whatever the readiness: the screen
    warns about a format only while ``delivery_enabled`` is true.
    """
    try:
        target = await _authorized_target(request, tenant, tenant_id, session)
        # Read before the rollback below: it expires every loaded instance.
        target_id = target.id
        credentials = (
            await session.execute(
                select(ClinicConfigTable.api_credentials).where(
                    ClinicConfigTable.tenant_id == target_id,
                    _authorized_media_target_exists(target_id, tenant),
                )
            )
        ).scalar_one_or_none()
        # End the read transaction before the flag read (an Agent HTTP call
        # under APPCONFIG_SOURCE=agent): no connection is held across it.
        await _rollback(session)
        provider_ready = media_delivery_provider_ready(credentials or {})
        delivery_enabled = provider_ready and await agent_media_send_enabled()
        logger.info(
            "media_bindable_intents",
            tenant_id=str(target_id),
            delivery_enabled=delivery_enabled,
            result="read",
        )
        return MediaBindableIntentsResponse(
            intents=list(MEDIA_BINDABLE_INTENT_SEQUENCE),
            topics=[
                MediaTopic(id=topic, intents=list(intents))
                for topic, intents in MEDIA_TOPIC_INTENTS.items()
            ],
            implicit_intents=list(MEDIA_IMPLICIT_INTENT_SEQUENCE),
            delivery_enabled=delivery_enabled,
            max_bytes_by_category=media_category_byte_limits(),
            sendable_mime_types=_clinic_sendable_mime_types(credentials),
        )
    except HTTPException:
        await _rollback(session)
        raise
    except SQLAlchemyError as error:
        await _rollback(session)
        raise _media_error(503, "media_storage_unavailable") from error
    except BaseException:
        await _rollback(session)
        raise


@router.get(
    "/{media_id}/content",
    summary="Download media asset content",
    description=(
        "Return the exact stored bytes as an attachment with nosniff and safe "
        "Content-Disposition headers."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Exact media bytes as a non-sniffable attachment.",
            "content": {
                mime_type: {"schema": {"type": "string", "format": "binary"}}
                for mime_type in MEDIA_FILE_EXTENSIONS
            },
            "headers": {
                "Content-Length": {"schema": {"type": "integer"}},
                "Content-Disposition": {"schema": {"type": "string"}},
                "X-Content-Type-Options": {
                    "schema": {"type": "string", "enum": ["nosniff"]}
                },
            },
        },
        401: _AUTHENTICATION_RESPONSE,
        402: _CREDIT_PENDING_RESPONSE,
        403: _FORBIDDEN_RESPONSE,
        404: {
            "description": "Asset is absent or not visible in the effective tenant.",
            "model": MediaAssetErrorResponse,
        },
        422: _VALIDATION_RESPONSE,
        503: _UNAVAILABLE_RESPONSE,
    },
    openapi_extra={"security": _MEDIA_SECURITY},
)
async def download_media_asset(
    media_id: UUID,
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description=_TENANT_ID_DESCRIPTION,
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> StreamingResponse:
    """Stream exact stored bytes as a non-sniffable attachment."""
    permit: ImmediatePermit | None = None
    try:
        mode = await _credit_mode(request, session)
        target = await _authorized_target(request, tenant, tenant_id, session)
        target_id = target.id
        permit = _download_permit(request)
        asset = (
            await session.execute(
                select(
                    MediaAsset.id,
                    MediaAsset.original_filename,
                    MediaAsset.category,
                    MediaAsset.mime_type,
                    MediaAsset.size_bytes,
                    MediaAsset.payload,
                ).where(
                    MediaAsset.id == media_id,
                    MediaAsset.tenant_id == target_id,
                    _authorized_media_target_exists(target_id, tenant),
                )
            )
        ).one_or_none()
        if asset is None:
            permit.release()
            permit = None
            raise _media_error(404, "media_asset_not_found")
        if mode.enforced:
            # The egress overage is charged BEFORE the answer (owner G).
            assert mode.enforcement_at is not None
            await session.rollback()
            if classify_interaction_origin(tenant) is InteractionOrigin.PRODUCTION:
                # Group 5: past its 72 h of debt a suspended builder's media
                # is suspended for API keys (the gate's transition commits
                # either way); no byte is sent and nothing is charged.
                suspended = await credit_lifecycle.tenant_suspended(session, target_id)
                await session.commit()
                if suspended:
                    permit.release()
                    permit = None
                    raise _media_error(402, "credit_suspended")
            charged = await credit_inventory.charge_egress(
                session,
                tenant_id=target_id,
                media_id=asset.id,
                size_bytes=asset.size_bytes,
                enforcement_at=mode.enforcement_at,
            )
            if charged.status == "credit_pending":
                await _rollback(session)
                permit.release()
                permit = None
                raise _media_error(402, "credit_pending")
            await session.commit()
        response = _PermitStreamingResponse(
            _single_chunk(asset.payload),
            permit=permit,
            media_type=asset.mime_type,
            headers={
                "Content-Length": str(asset.size_bytes),
                "Content-Disposition": _content_disposition(
                    asset.id,
                    asset.original_filename,
                    asset.mime_type,
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )
        await session.rollback()
        if mode.observational:
            await credit_inventory.record_observational_egress(
                session,
                tenant_id=target_id,
                media_id=asset.id,
                size_bytes=asset.size_bytes,
            )
        permit = None
        logger.info(
            "media_asset_download",
            tenant_id=str(target_id),
            media_id=str(asset.id),
            category=asset.category,
            size_bytes=asset.size_bytes,
            result="downloaded",
        )
        return response
    except CreditTenantGone:
        if permit is not None:
            permit.release()
        await _rollback(session)
        raise _media_error(404, "media_asset_not_found") from None
    except HTTPException:
        if permit is not None:
            permit.release()
        await _rollback(session)
        raise
    except SQLAlchemyError as error:
        if permit is not None:
            permit.release()
        await _rollback(session)
        raise _media_error(503, "media_storage_unavailable") from error
    except BaseException:
        if permit is not None:
            permit.release()
        await _rollback(session)
        raise


@router.patch(
    "/{media_id}",
    response_model=MediaAssetResponse,
    summary="Update media asset metadata",
    description=(
        "Update one or more approved metadata fields. Unknown fields are rejected; "
        "usage_trigger may be null to clear it, and trigger_intents replaces the "
        "bound intents ([] unbinds every one; unknown names are "
        "media_trigger_intent_unknown; duplicates are removed)."
    ),
    responses={
        401: _AUTHENTICATION_RESPONSE,
        403: _FORBIDDEN_RESPONSE,
        404: {
            "description": "Asset is absent or not visible in the effective tenant.",
            "model": MediaAssetErrorResponse,
        },
        422: _VALIDATION_RESPONSE,
        503: _UNAVAILABLE_RESPONSE,
    },
    openapi_extra={"security": _MEDIA_SECURITY},
)
async def patch_media_asset(
    media_id: UUID,
    patch: MediaAssetPatchRequest,
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description=_TENANT_ID_DESCRIPTION,
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> MediaAssetResponse:
    """Atomically update only the five approved author metadata fields."""
    target: Tenant | None = None
    try:
        target = await _authorized_target(request, tenant, tenant_id, session)
        current = (
            await session.execute(
                select(*_METADATA_COLUMNS)
                .where(
                    MediaAsset.id == media_id,
                    MediaAsset.tenant_id == target.id,
                    _authorized_media_target_exists(target.id, tenant),
                )
                .with_for_update()
            )
        ).one_or_none()
        if current is None:
            raise _media_error(404, "media_asset_not_found")

        fields = patch.model_fields_set
        if "auto_suggest" in fields and patch.auto_suggest is None:
            raise MediaAssetDomainError("media_metadata_invalid")
        display_name = (
            patch.display_name if "display_name" in fields else current.display_name
        )
        original_filename = (
            patch.original_filename
            if "original_filename" in fields
            else current.original_filename
        )
        if display_name is None or original_filename is None:
            raise MediaAssetDomainError("media_metadata_invalid")
        auto_suggest = current.auto_suggest
        if "auto_suggest" in fields:
            assert patch.auto_suggest is not None
            auto_suggest = patch.auto_suggest
        trigger_intents = list(current.trigger_intents)
        if "trigger_intents" in fields:
            if patch.trigger_intents is None:
                raise MediaAssetDomainError("media_metadata_invalid")
            trigger_intents = validate_trigger_intents(patch.trigger_intents)
        metadata = validate_media_metadata(
            display_name=display_name,
            original_filename=original_filename,
            usage_trigger=(
                patch.usage_trigger
                if "usage_trigger" in fields
                else current.usage_trigger
            ),
            auto_suggest="true" if auto_suggest else "false",
        )
        updated = (
            await session.execute(
                update(MediaAsset)
                .where(
                    MediaAsset.id == media_id,
                    MediaAsset.tenant_id == target.id,
                    _authorized_media_target_exists(target.id, tenant),
                )
                .values(
                    display_name=metadata.display_name,
                    original_filename=metadata.original_filename,
                    usage_trigger=metadata.usage_trigger,
                    auto_suggest=metadata.auto_suggest,
                    trigger_intents=trigger_intents,
                )
                .returning(*_METADATA_COLUMNS)
            )
        ).one_or_none()
        if updated is None:
            raise _media_error(404, "media_asset_not_found")
        await session.commit()
        logger.info(
            "media_asset_patch",
            tenant_id=str(target.id),
            media_id=str(updated.id),
            category=updated.category,
            size_bytes=updated.size_bytes,
            result="updated",
        )
        return MediaAssetResponse.model_validate(_metadata_values(updated))
    except MediaAssetDomainError as error:
        await _rollback(session)
        raise _media_error(422, error.code) from error
    except HTTPException:
        await _rollback(session)
        raise
    except SQLAlchemyError as error:
        await _rollback(session)
        raise _media_error(503, "media_storage_unavailable") from error
    except BaseException:
        await _rollback(session)
        raise


@router.delete(
    "/{media_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete media asset",
    description="Permanently delete the asset and release its billed bytes immediately.",
    responses={
        401: _AUTHENTICATION_RESPONSE,
        403: _FORBIDDEN_RESPONSE,
        404: {
            "description": "Asset is absent or not visible in the effective tenant.",
            "model": MediaAssetErrorResponse,
        },
        422: _VALIDATION_RESPONSE,
        503: _UNAVAILABLE_RESPONSE,
    },
    openapi_extra={"security": _MEDIA_SECURITY},
)
async def delete_media_asset(
    media_id: UUID,
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description=_TENANT_ID_DESCRIPTION,
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> Response:
    """Permanently remove one asset from the active tenant catalog."""
    target: Tenant | None = None
    try:
        target = await _authorized_target(request, tenant, tenant_id, session)
        deleted = (
            await session.execute(
                delete(MediaAsset)
                .where(
                    MediaAsset.id == media_id,
                    MediaAsset.tenant_id == target.id,
                    _authorized_media_target_exists(target.id, tenant),
                )
                .returning(
                    MediaAsset.id,
                    MediaAsset.category,
                    MediaAsset.size_bytes,
                )
            )
        ).one_or_none()
        if deleted is None:
            raise _media_error(404, "media_asset_not_found")
        await session.commit()
        try:
            await credit_inventory.enqueue_source_collection(
                session,
                source_kind="media",
                source_id=deleted.id,
                tenant_id=target.id,
                switch_on=request_credit_switch(request),
            )
        except Exception as error:
            logger.warning(
                "credit object-copy collection enqueue failed",
                alert="credit_object_copy",
                error_type=type(error).__name__,
            )
            await _rollback(session)
        logger.info(
            "media_asset_delete",
            tenant_id=str(target.id),
            media_id=str(deleted.id),
            category=deleted.category,
            size_bytes=deleted.size_bytes,
            result="deleted",
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HTTPException:
        await _rollback(session)
        raise
    except SQLAlchemyError as error:
        await _rollback(session)
        raise _media_error(503, "media_storage_unavailable") from error
    except BaseException:
        await _rollback(session)
        raise
