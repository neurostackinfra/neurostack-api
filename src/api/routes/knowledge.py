"""
NeuroStack API - Knowledge Endpoint
Knowledge base management and RAG operations.

Provides endpoints for:
- Document upload and indexing
- Semantic search/retrieval
- Knowledge base management
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

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

router = APIRouter()


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
    },
    tags=["Knowledge"],
)
async def upload_document(
    request: DocumentUploadRequest,
    session: AsyncSession = Depends(get_db_session_dependency),
) -> DocumentResponse:
    """
    Upload and index a document.
    """
    repo = KnowledgeDocumentRepository(session)
    
    # Create document
    document = await repo.create(
        tenant_id=request.tenant_id,
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
    """,
    responses={
        200: {
            "description": "Search completed successfully",
            "model": SearchResponse,
        },
    },
    tags=["Knowledge"],
)
async def search_knowledge(request: SearchRequest) -> SearchResponse:
    """
    Perform semantic search in the knowledge base.
    """
    start_time = datetime.utcnow()

    # TODO: Implement semantic search (Task 19.0)
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
List all documents in the knowledge base for a tenant.

Supports pagination and filtering by metadata.
    """,
    responses={
        200: {
            "description": "Documents listed successfully",
            "model": DocumentListResponse,
        },
    },
    tags=["Knowledge"],
)
async def list_documents(
    tenant_id: UUID = Query(..., description="Tenant ID"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    category: str | None = Query(default=None, description="Filter by category"),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> DocumentListResponse:
    """
    List documents for a tenant.
    """
    repo = KnowledgeDocumentRepository(session)
    skip = (page - 1) * page_size
    
    if category:
        documents_list = await repo.find_by_category(
            tenant_id=tenant_id,
            category=category,
            skip=skip,
            limit=page_size,
        )
    else:
        documents_list = await repo.find_by_tenant(
            tenant_id=tenant_id,
            skip=skip,
            limit=page_size,
        )
    
    total = await repo.count_by_tenant(tenant_id)
    
    document_responses = [
        DocumentResponse(
            id=str(doc.id),
            tenant_id=doc.tenant_id,
            status="indexed" if doc.is_indexed else "pending",
            metadata=DocumentMetadata(
                title=doc.document_metadata.get("title") if doc.document_metadata else None,
                source=doc.document_metadata.get("source") if doc.document_metadata else None,
                category=doc.document_metadata.get("category") if doc.document_metadata else None,
                tags=doc.document_metadata.get("tags", []) if doc.document_metadata else [],
                custom={k: v for k, v in (doc.document_metadata or {}).items() 
                       if k not in ["title", "source", "category", "tags"]},
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
    description="Retrieve a document by ID.",
    responses={
        200: {
            "description": "Document retrieved successfully",
            "model": DocumentResponse,
        },
        404: {
            "description": "Document not found",
            "model": ErrorResponse,
        },
    },
    tags=["Knowledge"],
)
async def get_document(
    document_id: UUID,
    session: AsyncSession = Depends(get_db_session_dependency),
) -> DocumentResponse:
    """
    Retrieve a document by ID.
    """
    repo = KnowledgeDocumentRepository(session)
    document = await repo.get_by_id(document_id)
    
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found",
        )
    
    return DocumentResponse(
        id=str(document.id),
        tenant_id=document.tenant_id,
        status="indexed" if document.is_indexed else "pending",
        metadata=DocumentMetadata(
            title=document.document_metadata.get("title") if document.document_metadata else None,
            source=document.document_metadata.get("source") if document.document_metadata else None,
            category=document.document_metadata.get("category") if document.document_metadata else None,
            tags=document.document_metadata.get("tags", []) if document.document_metadata else [],
            custom={k: v for k, v in (document.document_metadata or {}).items() 
                   if k not in ["title", "source", "category", "tags"]},
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
    """,
    responses={
        204: {
            "description": "Document deleted successfully",
        },
        404: {
            "description": "Document not found",
            "model": ErrorResponse,
        },
    },
    tags=["Knowledge"],
)
async def delete_document(
    document_id: UUID,
    session: AsyncSession = Depends(get_db_session_dependency),
) -> None:
    """
    Delete a document.
    """
    repo = KnowledgeDocumentRepository(session)
    deleted = await repo.delete(document_id)
    
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found",
        )
    
    # TODO: Delete from vector database (Task 19.0)

