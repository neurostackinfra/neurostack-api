"""
NeuroStack API - Knowledge Endpoint
Knowledge base management and RAG operations.

Provides endpoints for:
- Document upload and indexing
- Semantic search/retrieval
- Knowledge base management

Tenant authorization: every route resolves exactly one authorized tenant with
the canonical ``resolve_effective_tenant`` before it reads or mutates the
knowledge base, and the repository is called only with that tenant. A
client-supplied ``tenant_id`` (query or body) is an override request, never an
authorization: API keys and clinic administrators stay in their own tenant,
builders may select only clinics they own, super administrators may select any
tenant, and other JWT roles are denied. A document requested by ID is looked up
by ID and authorized tenant together, so another tenant's document is a 404
identical to a missing one. Knowledge responses are not cacheable by the
response cache, and every successful response carries ``Cache-Control:
no-store``.

Persistence: the request session only closes, so each mutating route commits
explicitly, once, after the authorization check, the write and the response
are complete. Any failure before that commit leaves nothing behind. An upload
into a tenant without a row is a 404 ``Tenant not found`` (identical for every
caller allowed to name that tenant); a builder still gets the ownership 403.

Input: the schemas reject what PostgreSQL cannot store (NUL, unpaired
surrogates, non-finite numbers, a title over the column length) and bound the
metadata, so such input is a 422, never a driver error. Every 422 of these
routes (except the search stub) lists only ``type``, ``loc`` and ``msg`` per
issue: free text is never echoed (Pydantic's UUID message quotes one
character of a malformed UUID). ``metadata.custom`` cannot use a
reserved key, and every response is built from the stored row, so an upload
answers exactly what a later read returns.
"""

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    resolve_effective_tenant,
)
from src.api.schemas.base import ErrorResponse
from src.api.schemas.knowledge import (
    KNOWLEDGE_CATEGORY_MAX_LENGTH,
    KNOWLEDGE_RESERVED_METADATA_KEYS,
    CategoryText,
    DocumentListResponse,
    DocumentMetadata,
    DocumentResponse,
    DocumentUploadRequest,
    KnowledgeValidationErrorResponse,
    SearchRequest,
    SearchResponse,
)
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories import KnowledgeDocumentRepository, get_db_session_dependency


def _no_store(response: Response) -> None:
    """Forbid browsers and shared caches from storing tenant knowledge data."""
    response.headers["Cache-Control"] = "no-store"


def _validation_response_without_input(error: RequestValidationError) -> JSONResponse:
    """FastAPI's 422 shape without the ``input`` and ``ctx`` fields."""
    issues = [
        {
            "type": str(issue.get("type", "invalid")),
            "loc": list(issue.get("loc", ())),
            "msg": str(issue.get("msg", "")),
        }
        for issue in error.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": issues},
        headers={"Cache-Control": "no-store"},
    )


# The search stub keeps FastAPI's default 422 until search itself is built.
_FRAMEWORK_VALIDATION_ENDPOINTS = frozenset({"search_knowledge"})


class _KnowledgeValidationRoute(APIRoute):
    """Answer request validation errors without the ``input`` field."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        if self.endpoint.__name__ in _FRAMEWORK_VALIDATION_ENDPOINTS:
            return original

        async def knowledge_route_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as error:
                return _validation_response_without_input(error)

        return knowledge_route_handler


router = APIRouter(
    dependencies=[Depends(_no_store)], route_class=_KnowledgeValidationRoute
)

_TENANT_OVERRIDE_DESCRIPTION = (
    "Tenant to act on. Defaults to the caller's tenant; another tenant is "
    "allowed only for a builder that owns the clinic or a super admin."
)
_TENANT_FORBIDDEN_RESPONSE: dict[int | str, dict[str, Any]] = {
    403: {
        "description": (
            "Caller may not act on the requested tenant "
            "(tenant_override_forbidden) or its role is not allowed "
            "(caller_role_forbidden)"
        ),
        "model": ErrorResponse,
    },
}
_DOCUMENT_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {
        "description": (
            "Document not found in the authorized tenant (another tenant's "
            "document is indistinguishable from a missing one)"
        ),
        "model": ErrorResponse,
    },
}


def _validation_error_response(
    error_type: str, loc: list[str], msg: str
) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entry for the 422 without ``input``, with a route-specific example."""
    return {
        422: {
            "description": (
                "Invalid input. Each issue carries only `type`, `loc` and "
                "`msg`; submitted free text is never echoed."
            ),
            "model": KnowledgeValidationErrorResponse,
            "content": {
                "application/json": {
                    "example": {
                        "detail": [{"type": error_type, "loc": loc, "msg": msg}]
                    }
                }
            },
        },
    }


_NUL_MESSAGE = "Text must not contain the NUL character (U+0000)"
_UUID_MESSAGE = "Input should be a valid UUID, invalid length: expected length 32 for simple format, found 3"


_TENANT_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {
        "description": "The authorized tenant has no tenant record",
        "model": ErrorResponse,
    },
}
_TENANT_NOT_FOUND_DETAIL = "Tenant not found"
# PostgreSQL foreign_key_violation and the knowledge_documents -> tenants key.
_FOREIGN_KEY_VIOLATION = "23503"
_TENANT_FOREIGN_KEY = "knowledge_documents_tenant_id_fkey"


def _tenant_not_found() -> HTTPException:
    """Same body for every caller; never names the requested tenant."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=_TENANT_NOT_FOUND_DETAIL,
    )


async def _require_tenant_row(session: AsyncSession, tenant_id: UUID) -> None:
    """Refuse a write into a tenant that has no row, before the insert."""
    found = await session.execute(select(Tenant.id).where(Tenant.id == tenant_id))
    if found.scalar_one_or_none() is None:
        raise _tenant_not_found()


def _is_missing_tenant_violation(error: IntegrityError) -> bool:
    """True only for the knowledge_documents.tenant_id foreign-key violation.

    This is the race where the tenant row disappears between
    ``_require_tenant_row`` and the insert; every other integrity error
    (another constraint, another SQLSTATE) is not a missing tenant.
    """
    driver_error = error.orig
    candidates = (driver_error, getattr(driver_error, "__cause__", None))
    sqlstates = {getattr(candidate, "sqlstate", None) for candidate in candidates}
    constraints = {
        getattr(candidate, "constraint_name", None) for candidate in candidates
    }
    return _FOREIGN_KEY_VIOLATION in sqlstates and _TENANT_FOREIGN_KEY in constraints


def _stored_metadata(stored: Optional[dict[str, Any]]) -> DocumentMetadata:
    """Split the stored metadata back into its fields and ``custom``."""
    metadata = stored or {}
    return DocumentMetadata(
        title=metadata.get("title"),
        source=metadata.get("source"),
        category=metadata.get("category"),
        tags=metadata.get("tags", []),
        custom={
            key: value
            for key, value in metadata.items()
            if key not in KNOWLEDGE_RESERVED_METADATA_KEYS
        },
    )


def _document_response(document: KnowledgeDocument) -> DocumentResponse:
    """The API view of a stored document; the same for every route."""
    return DocumentResponse(
        id=str(document.id),
        tenant_id=document.tenant_id,
        status="indexed" if document.is_indexed else "pending",
        metadata=_stored_metadata(document.document_metadata),
        chunk_count=document.chunk_count,
        created_at=document.created_at.isoformat(),
    )


def _document_not_found(document_id: UUID) -> HTTPException:
    """Same body for a missing document and another tenant's document."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Document {document_id} not found",
    )


# ============================================
# Endpoints
# ============================================


@router.post(
    "/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload Document",
    description="""
Upload and index a document to the knowledge base.

Documents are automatically chunked, embedded, and indexed in the
vector database for semantic retrieval.

## Supported Content

- Plain text
- Markdown (converted to text)
- HTML (tags stripped)

## Chunking Strategy

Documents are split into chunks of ~500 tokens with overlap for
context preservation. Each chunk is individually embedded and indexed.

## Tenant

`tenant_id` in the body defaults to the caller's tenant. Another tenant is
accepted only for a builder that owns the clinic or a super admin.
    """,
    responses={
        201: {
            "description": "Document uploaded and indexing started",
            "model": DocumentResponse,
        },
        400: {
            "description": "Invalid document content",
            "model": ErrorResponse,
        },
        **_TENANT_FORBIDDEN_RESPONSE,
        **_TENANT_NOT_FOUND_RESPONSE,
        **_validation_error_response(
            "reserved_metadata_key",
            ["body", "metadata", "custom"],
            "metadata.custom must not use the reserved keys: title",
        ),
    },
    tags=["Knowledge"],
)
async def upload_document(
    request: DocumentUploadRequest,
    http_request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> DocumentResponse:
    """
    Upload and index a document.
    """
    # The payload tenant is an override request; authorize it before use.
    effective_tenant_id = await resolve_effective_tenant(
        http_request, tenant, request.tenant_id, session
    )
    await _require_tenant_row(session, effective_tenant_id)
    repo = KnowledgeDocumentRepository(session)

    # The schema already refused reserved keys in custom, so it cannot
    # overwrite the fields stored next to it.
    try:
        document = await repo.create(
            tenant_id=effective_tenant_id,
            title=request.metadata.title or "Untitled Document",
            content=request.content,
            content_type="text",
            document_metadata={
                "title": request.metadata.title,
                "source": request.metadata.source,
                "category": request.metadata.category,
                "tags": request.metadata.tags,
                **request.metadata.custom,
            },
            is_indexed=False,
            chunk_count=0,
            size_bytes=len(request.content.encode("utf-8")),
        )
    except IntegrityError as error:
        await session.rollback()
        if _is_missing_tenant_violation(error):
            raise _tenant_not_found() from None
        raise

    # TODO: Trigger indexing job (Task 19.0)
    # For now, mark as pending

    # Built from the row ``create`` refreshed, never from the request, so the
    # response is exactly what a later read returns.
    response = _document_response(document)
    # Commit only once the write and its response are complete.
    await session.commit()
    return response


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Semantic Search",
    description="""
Perform semantic search across the knowledge base.

Uses vector similarity to find the most relevant content for
the given query. Results are ordered by relevance score.

## Filtering

Apply metadata filters to narrow down results:
- `category`: Filter by document category
- `tags`: Filter by tags (any match)
- Custom metadata fields

## Tenant

`tenant_id` in the body defaults to the caller's tenant. Another tenant is
accepted only for a builder that owns the clinic or a super admin.
    """,
    responses={
        200: {
            "description": "Search completed successfully",
            "model": SearchResponse,
        },
        **_TENANT_FORBIDDEN_RESPONSE,
    },
    tags=["Knowledge"],
)
async def search_knowledge(
    request: SearchRequest,
    http_request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> SearchResponse:
    """
    Perform semantic search in the knowledge base.
    """
    start_time = datetime.utcnow()

    # Authorize before searching: retrieval may only ever use this tenant,
    # never request.tenant_id.
    await resolve_effective_tenant(http_request, tenant, request.tenant_id, session)

    # TODO: Implement semantic search (Task 19.0) scoped to the resolved tenant
    end_time = datetime.utcnow()
    processing_time_ms = int((end_time - start_time).total_seconds() * 1000)

    return SearchResponse(
        query=request.query,
        results=[],
        total=0,
        processing_time_ms=processing_time_ms,
    )


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="List Documents",
    description="""
List the documents of the authorized tenant.

Supports pagination and filtering by metadata.

## Tenant

- **tenant_id**: Tenant to list (default: the caller's tenant; another tenant
  only for a builder that owns the clinic or a super admin)
    """,
    responses={
        200: {
            "description": "Documents listed successfully",
            "model": DocumentListResponse,
        },
        **_TENANT_FORBIDDEN_RESPONSE,
        **_validation_error_response(
            "nul_character", ["query", "category"], _NUL_MESSAGE
        ),
    },
    tags=["Knowledge"],
)
async def list_documents(
    request: Request,
    tenant_id: Optional[UUID] = Query(
        default=None, description=_TENANT_OVERRIDE_DESCRIPTION
    ),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    category: Optional[CategoryText] = Query(
        default=None,
        description=(
            "Filter by category (at most "
            f"{KNOWLEDGE_CATEGORY_MAX_LENGTH} characters, no NUL)"
        ),
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> DocumentListResponse:
    """
    List documents for the authorized tenant (default: the caller's tenant).
    """
    effective_tenant_id = await resolve_effective_tenant(
        request, tenant, tenant_id, session
    )
    repo = KnowledgeDocumentRepository(session)
    skip = (page - 1) * page_size

    # One branch picks the filter for both the page and its total, so they
    # always describe the same set. An empty ``?category=`` does not filter.
    if category:
        documents_list = await repo.find_by_category(
            tenant_id=effective_tenant_id,
            category=category,
            skip=skip,
            limit=page_size,
        )
        total = await repo.count_by_category(
            tenant_id=effective_tenant_id, category=category
        )
    else:
        documents_list = await repo.find_by_tenant(
            tenant_id=effective_tenant_id,
            skip=skip,
            limit=page_size,
        )
        total = await repo.count_by_tenant(effective_tenant_id)

    document_responses = [_document_response(doc) for doc in documents_list]

    return DocumentListResponse(
        documents=document_responses,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentResponse,
    summary="Get Document",
    description="""
Retrieve a document by ID.

The document is returned only inside the authorized tenant (`tenant_id`,
default: the caller's tenant); another tenant's document is a 404, identical
to a missing one.
    """,
    responses={
        200: {
            "description": "Document retrieved successfully",
            "model": DocumentResponse,
        },
        **_TENANT_FORBIDDEN_RESPONSE,
        **_DOCUMENT_NOT_FOUND_RESPONSE,
        **_validation_error_response(
            "uuid_parsing", ["path", "document_id"], _UUID_MESSAGE
        ),
    },
    tags=["Knowledge"],
)
async def get_document(
    document_id: UUID,
    request: Request,
    tenant_id: Optional[UUID] = Query(
        default=None, description=_TENANT_OVERRIDE_DESCRIPTION
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> DocumentResponse:
    """
    Retrieve a document by ID.
    """
    effective_tenant_id = await resolve_effective_tenant(
        request, tenant, tenant_id, session
    )
    repo = KnowledgeDocumentRepository(session)
    # Only the authorized tenant's documents are visible.
    document = await repo.get_by_id_and_tenant(document_id, effective_tenant_id)

    if not document:
        raise _document_not_found(document_id)

    return _document_response(document)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Document",
    description="""
Delete a document from the knowledge base.

This removes the document and all its indexed chunks from the
vector database.

Only a document of the authorized tenant (`tenant_id`, default: the caller's
tenant) can be deleted; another tenant's document is a 404, identical to a
missing one.
    """,
    responses={
        204: {
            "description": "Document deleted successfully",
        },
        **_TENANT_FORBIDDEN_RESPONSE,
        **_DOCUMENT_NOT_FOUND_RESPONSE,
        **_validation_error_response(
            "uuid_parsing", ["path", "document_id"], _UUID_MESSAGE
        ),
    },
    tags=["Knowledge"],
)
async def delete_document(
    document_id: UUID,
    request: Request,
    tenant_id: Optional[UUID] = Query(
        default=None, description=_TENANT_OVERRIDE_DESCRIPTION
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> None:
    """
    Delete a document.
    """
    effective_tenant_id = await resolve_effective_tenant(
        request, tenant, tenant_id, session
    )
    repo = KnowledgeDocumentRepository(session)
    # Another tenant's document is indistinguishable from a missing one.
    deleted = await repo.delete_by_id_and_tenant(document_id, effective_tenant_id)

    if not deleted:
        raise _document_not_found(document_id)

    await session.commit()
    # TODO: Delete from vector database (Task 19.0). It must run only after the
    # commit above, never while the row lock taken by delete_by_id_and_tenant
    # is held (a slow external call would block every writer of this row) nor
    # before the commit (a rollback would leave the vectors already deleted).
