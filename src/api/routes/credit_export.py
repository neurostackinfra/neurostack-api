"""``GET /v1/credits/export`` -- the tenant's authoritative bytes (Group 5).

A credit surface (``credit_router``): with the credit switch not effective it
answers the stable ``503 {"code": "credit_unavailable"}`` before any
dependency or database work.  With the switch effective the export is
available before and after ``credit_enforcement_at`` and in every lifecycle
state (grace, suspended, deletion_due): it is how the retention promise is
kept, so no lifecycle rule ever refuses it.

Authorization is the platform's (``resolve_effective_tenant``): an API key
exports its own tenant; a JWT ``clinic_admin`` its own; a ``builder`` its own
or, with ``?tenant_id=``, a clinic of its builder; a ``super_admin`` any
clinic.  For a builder the route then re-reads ``tenants.builder_id``, which
must equal the JWT claim, with the one fallback of the credit reads
(:mod:`src.api.helpers.builder_home_tenant`): the builder's OWN tenant (no
clinic named) linked to no builder is exported once the token's ``users``
row confirms it (active, role builder, same builder and tenant) and no
builder user of another builder (or of none) shares that tenant.  A clinic
of another builder (or of none), an own tenant linked to another builder, a
stale token, an unlinked tenant shared with a builder user of another
builder (or of none) and a tenant that does not exist get the same ``403 {"code":
"tenant_override_forbidden"}``: nothing reveals existence, nothing is
locked, nothing is written.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.helpers.builder_home_tenant import builder_home_tenant_account
from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    resolve_effective_tenant,
)
from src.models.tenant import Tenant
from src.repositories.database import get_db_session_dependency
from src.services.credit_export import build_tenant_export
from src.services.credit_switch import credit_router

router = credit_router()

_FORBIDDEN: dict[str, Any] = {"code": "tenant_override_forbidden"}


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_FORBIDDEN)


async def _export_tenant(
    request: Request,
    tenant: TenantContext,
    requested_tenant_id: UUID | None,
    session: AsyncSession,
) -> UUID:
    effective = await resolve_effective_tenant(
        request, tenant, requested_tenant_id, session
    )
    builder_jwt = tenant.role == "builder" and not tenant.api_key
    if builder_jwt and tenant.builder_id is None:
        raise _forbidden()
    found = (
        await session.execute(
            select(Tenant.id, Tenant.builder_id).where(Tenant.id == effective)
        )
    ).one_or_none()
    if found is None:
        raise _forbidden()
    if not builder_jwt or found.builder_id == tenant.builder_id:
        return effective
    # The one fallback of the credit reads: the builder's own (home) tenant,
    # no clinic named, linked to no builder, with a current ``users`` row and
    # no builder user of another builder (or of none) on it.  Everything else
    # (a clinic of another builder or of none, an own tenant linked to
    # another builder, a stale token, a tenant shared with a builder user of
    # another builder or of none) is the absent tenant's 403.
    home = await builder_home_tenant_account(
        session, tenant, effective, found.builder_id
    )
    if home is None:
        raise _forbidden()
    return effective


@router.get(
    "/export",
    summary="Export the tenant's media and knowledge bytes",
    description=(
        "Download a ZIP of the exact bytes PostgreSQL holds for the tenant "
        "(media_assets.payload, knowledge_documents.content) with "
        "manifest.json listing every entry's SHA-256 and, when an additional "
        "S3 copy with the same SHA-256 is registered, its exact VersionId. "
        "Answers 503 credit_unavailable while the credit switch is off."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"application/zip": {"schema": {"type": "string"}}},
            "description": "The archive.",
        },
        401: {"description": "Not authenticated."},
        403: {"description": "The tenant is not visible to the caller."},
        503: {"description": "credit_unavailable: the credit switch is off."},
    },
)
async def export_tenant_content(
    request: Request,
    tenant_id: Optional[UUID] = Query(
        default=None,
        description="Clinic to export (builders: one of theirs; default: own).",
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> StreamingResponse:
    try:
        target = await _export_tenant(request, tenant, tenant_id, session)
        archive = await build_tenant_export(session, target)
    finally:
        # A read-only transaction: end it before the body streams, so the
        # connection is back in the pool while the client downloads.
        await session.rollback()
    return StreamingResponse(
        archive.chunks(),
        media_type="application/zip",
        headers={
            "Content-Length": str(archive.size_bytes),
            "Content-Disposition": f'attachment; filename="{archive.filename()}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Credit-Export-Manifest-SHA256": archive.manifest_sha256,
            "X-Credit-Export-SHA256": archive.archive_sha256,
        },
    )


__all__ = ["router"]
