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
"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    resolve_effective_tenant,
)
from src.api.schemas.base import ErrorResponse
from src.api.schemas.knowledge import (
    DocumentListResponse,
    DocumentMetadata,
    DocumentResponse,
    DocumentUploadRequest,
    SearchRequest,
    SearchResponse,
)
from src.repositories import KnowledgeDocumentRepository, get_db_session_dependency


def _no_store(response: Response) -> None:
    """Forbid browsers and shared caches from storing tenant knowledge data."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(_no_store)])

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
    repo = KnowledgeDocumentRepository(session)

    # Create document
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

    # TODO: Trigger indexing job (Task 19.0)
    # For now, mark as pending

    return DocumentResponse(
        id=str(document.id),
        tenant_id=document.tenant_id,
        status="pending",
        metadata=DocumentMetadata(
            title=request.metadata.title,
            source=request.metadata.source,
            category=request.metadata.category,
            tags=request.metadata.tags,
            custom=request.metadata.custom,
        ),
        chunk_count=0,
        created_at=document.created_at.isoformat(),
    )


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
    category: Optional[str] = Query(default=None, description="Filter by category"),
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

    if category:
        documents_list = await repo.find_by_category(
            tenant_id=effective_tenant_id,
            category=category,
            skip=skip,
            limit=page_size,
        )
    else:
        documents_list = await repo.find_by_tenant(
            tenant_id=effective_tenant_id,
            skip=skip,
            limit=page_size,
        )

    total = await repo.count_by_tenant(effective_tenant_id)

    document_responses = [
        DocumentResponse(
            id=str(doc.id),
            tenant_id=doc.tenant_id,
            status="indexed" if doc.is_indexed else "pending",
            metadata=DocumentMetadata(
                title=(
                    doc.document_metadata.get("title")
                    if doc.document_metadata
                    else None
                ),
                source=(
                    doc.document_metadata.get("source")
                    if doc.document_metadata
                    else None
                ),
                category=(
                    doc.document_metadata.get("category")
                    if doc.document_metadata
                    else None
                ),
                tags=(
                    doc.document_metadata.get("tags", [])
                    if doc.document_metadata
                    else []
                ),
                custom={
                    k: v
                    for k, v in (doc.document_metadata or {}).items()
                    if k not in ["title", "source", "category", "tags"]
                },
            ),
            chunk_count=doc.chunk_count,
            created_at=doc.created_at.isoformat(),
        )
        for doc in documents_list
    ]

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

    return DocumentResponse(
        id=str(document.id),
        tenant_id=document.tenant_id,
        status="indexed" if document.is_indexed else "pending",
        metadata=DocumentMetadata(
            title=(
                document.document_metadata.get("title")
                if document.document_metadata
                else None
            ),
            source=(
                document.document_metadata.get("source")
                if document.document_metadata
                else None
            ),
            category=(
                document.document_metadata.get("category")
                if document.document_metadata
                else None
            ),
            tags=(
                document.document_metadata.get("tags", [])
                if document.document_metadata
                else []
            ),
            custom={
                k: v
                for k, v in (document.document_metadata or {}).items()
                if k not in ["title", "source", "category", "tags"]
            },
        ),
        chunk_count=document.chunk_count,
        created_at=document.created_at.isoformat(),
    )


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

    # TODO: Delete from vector database (Task 19.0)
